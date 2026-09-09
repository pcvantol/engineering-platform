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
import tempfile
from typing import Mapping

from .installation_update_plan import InstallationUpdatePlan


_STATES = ("PREPARED", "INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE")
_NEXT = {state: _STATES[index + 1:index + 2] for index, state in enumerate(_STATES)}
_NEXT["VERIFIED"] = ("CLEANUP_PENDING", "COMPLETE")


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
