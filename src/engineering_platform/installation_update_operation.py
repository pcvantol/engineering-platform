"""Durable, resumable state journal for one EP installation update.

This module deliberately records lifecycle progress only.  Product-owned
backup, migration, service activation and cleanup implementations call it; it
does not create a second installer or mutate an operational runtime itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping
import re

from .installation_update_plan import InstallationUpdatePlan
from .operational_installation_lock import OperationalInstallationLock


_STATES = ("PREPARED", "INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE")
_NEXT = {state: _STATES[index + 1:index + 2] for index, state in enumerate(_STATES)}
_NEXT["VERIFIED"] = ("CLEANUP_PENDING", "COMPLETE")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


class InstallationUpdateOperationError(ValueError):
    """The persisted update operation is malformed, conflicted, or out of order."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _path(plan: InstallationUpdatePlan) -> Path:
    return Path(plan.data_root).resolve() / "operations" / plan.operation_id / "operation.json"


def _plan(plan: InstallationUpdatePlan) -> dict[str, object]:
    # JSON is the durable representation; normalize tuples from the dataclass
    # before comparing a reopened operation with its in-memory plan.
    return json.loads(_canonical(plan.payload()))


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".operation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value) + b"\n"); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _load(plan: InstallationUpdatePlan) -> dict[str, object]:
    try:
        value = json.loads(_path(plan).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallationUpdateOperationError("installation update operation is unreadable") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "operation_id", "plan", "plan_digest", "state", "events"}:
        raise InstallationUpdateOperationError("installation update operation is invalid")
    expected = _plan(plan)
    if value["schema_version"] != 1 or value["operation_id"] != plan.operation_id or value["plan"] != expected:
        raise InstallationUpdateOperationError("installation update operation does not bind the exact plan")
    if value["plan_digest"] != "sha256:" + hashlib.sha256(_canonical(expected)).hexdigest() or value["state"] not in _STATES or not isinstance(value["events"], list):
        raise InstallationUpdateOperationError("installation update operation is invalid")
    return value


def create(plan: InstallationUpdatePlan) -> dict[str, object]:
    """Create or recover the exact operation before any lifecycle side effect."""
    path, payload = _path(plan), _plan(plan)
    if path.exists():
        return _load(plan)
    value: dict[str, object] = {"schema_version": 1, "operation_id": plan.operation_id, "plan": payload,
                                "plan_digest": "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest(),
                                "state": "PREPARED", "events": [{"state": "PREPARED", "evidence": {}}]}
    _write(path, value)
    return value


def status(data_root: Path, operation_id: str) -> dict[str, object]:
    """Return an identity-bearing, read-only view of a persisted operation."""
    if _OPERATION.fullmatch(operation_id) is None:
        raise InstallationUpdateOperationError("installation update operation ID is invalid")
    root = Path(data_root).expanduser().resolve()
    path = root / "operations" / operation_id / "operation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallationUpdateOperationError("installation update operation is unreadable") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("operation_id") != operation_id:
        raise InstallationUpdateOperationError("installation update operation is invalid")
    plan = value.get("plan")
    if not isinstance(plan, dict) or value.get("state") not in _STATES or not isinstance(value.get("events"), list):
        raise InstallationUpdateOperationError("installation update operation is invalid")
    return {"operation_id": operation_id, "state": value["state"], "plan_digest": value.get("plan_digest"),
            "installation_id": plan.get("installation_id"), "current_version": plan.get("current_version"),
            "target_version": plan.get("target_version"), "target_digest": plan.get("target_digest"),
            "target_source_revision": plan.get("target_source_revision"), "cleanup_targets": plan.get("cleanup_targets"),
            "events": value["events"]}


def transition(plan: InstallationUpdatePlan, state: str, evidence: Mapping[str, object]) -> dict[str, object]:
    """Advance one ordered, idempotent lifecycle step using bounded evidence."""
    value = _load(plan)
    if not isinstance(evidence, Mapping):
        raise InstallationUpdateOperationError("installation update evidence is invalid")
    current = str(value["state"])
    if state == current:
        last = value["events"][-1] if value["events"] else None
        if isinstance(last, dict) and last.get("evidence") == dict(evidence):
            return value
        raise InstallationUpdateOperationError("installation update transition conflicts with existing evidence")
    if state not in _NEXT[current]:
        raise InstallationUpdateOperationError("installation update transition is out of order")
    value["state"] = state
    value["events"].append({"state": state, "evidence": dict(evidence)})
    _write(_path(plan), value)
    return value


def cleanup(plan: InstallationUpdatePlan) -> dict[str, object]:
    """Remove only exact operation-owned temporary paths after verification.

    The operation journal, backup evidence, data root, caches outside the
    operation and every development environment are deliberately excluded.
    Symlinks fail closed instead of being followed or removed as a shortcut.
    """
    value = _load(plan)
    if value["state"] not in {"VERIFIED", "CLEANUP_PENDING"}:
        raise InstallationUpdateOperationError("installation update cleanup requires verified runtime")
    operation_root = _path(plan).parent.resolve()
    expected = tuple(operation_root / name for name in ("build", "download", "pip-cache"))
    targets = tuple(Path(item) for item in plan.cleanup_targets)
    if targets != expected:
        raise InstallationUpdateOperationError("installation update cleanup targets are not operation-owned")
    failed: list[str] = []
    for target in targets:
        try:
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != operation_root.parent):
                raise InstallationUpdateOperationError("operation cleanup refuses a symlinked path")
            if target.exists():
                shutil.rmtree(target)
        except (OSError, InstallationUpdateOperationError):
            failed.append(str(target))
    if failed:
        evidence = {"failed_targets": failed}
        if value["state"] == "VERIFIED":
            transition(plan, "CLEANUP_PENDING", evidence)
        raise InstallationUpdateOperationError("installation update cleanup is pending")
    return transition(plan, "COMPLETE", {"removed_targets": [str(path) for path in targets]})


class InstallationUpdateSession:
    """The one lock-owning boundary for a resumable update operation.

    A process crash releases ``flock`` while the journal remains on disk; a
    later session with the same plan can therefore resume but a concurrent
    installer cannot obtain a second writer.
    """
    def __init__(self, plan: InstallationUpdatePlan) -> None:
        self.plan = plan
        self._lock = OperationalInstallationLock(Path(plan.data_root))
        self._owned = False

    def __enter__(self) -> "InstallationUpdateSession":
        self._lock.acquire(self.plan.operation_id)
        try:
            create(self.plan)
        except BaseException:
            self._lock.release(self.plan.operation_id)
            raise
        self._owned = True
        return self

    def advance(self, state: str, evidence: Mapping[str, object]) -> dict[str, object]:
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return transition(self.plan, state, evidence)

    def cleanup(self) -> dict[str, object]:
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return cleanup(self.plan)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._owned:
            self._lock.release(self.plan.operation_id)
            self._owned = False
