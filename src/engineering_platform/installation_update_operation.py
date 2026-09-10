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
from . import operational_installation_record


_STATES = ("PREPARED", "INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE")
_NEXT = {state: _STATES[index + 1:index + 2] for index, state in enumerate(_STATES)}
_NEXT["VERIFIED"] = ("CLEANUP_PENDING", "COMPLETE")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_V1_FIELDS = frozenset({"schema_version", "operation_id", "plan", "plan_digest", "state", "events"})
_V2_FIELDS = _V1_FIELDS | frozenset({"prepared_candidate", "prepared_candidate_digest"})
_V3_FIELDS = _V2_FIELDS | frozenset({"prepared_record_provenance", "prepared_record_provenance_digest"})
_V4_FIELDS = _V3_FIELDS | frozenset({"execution_admission", "execution_admission_digest"})
_CANDIDATE_FIELDS = frozenset({
    "operation_id", "installation_id", "operation_root", "staged_artifact",
    "artifact_digest", "candidate_venv", "interpreter", "pip_cache", "package",
})
_PACKAGE_IDENTITY_FIELDS = frozenset({"interpreter", "version", "metadata", "package"})
_ADMISSION_FIELDS = frozenset({"schema_version", "operation_id", "plan_digest", "installation_id",
                               "registered_installation", "prepared_candidate"})
_ADMISSION_RECORD_V1_FIELDS = frozenset({"installation_id", "version", "artifact_digest", "source_revision",
                                         "interpreter", "roles", "record_digest"})
_ADMISSION_RECORD_V2_FIELDS = _ADMISSION_RECORD_V1_FIELDS | frozenset({"record"})


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


def _binding(value: object) -> dict[str, object] | None:
    """Validate the bounded, token-free candidate identity stored in a journal."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
        raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    for field in _CANDIDATE_FIELDS - {"package"}:
        if not isinstance(value.get(field), str) or not value[field]:
            raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    package = value.get("package")
    if (not isinstance(package, dict) or set(package) != _PACKAGE_IDENTITY_FIELDS
            or not all(isinstance(item, str) and item for item in package.values())):
        raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    return {
        **{field: str(value[field]) for field in _CANDIDATE_FIELDS - {"package"}},
        "package": {field: str(package[field]) for field in _PACKAGE_IDENTITY_FIELDS},
    }


def _admission_binding(value: object) -> dict[str, object] | None:
    """Validate the typed OI-4c evidence before treating its digest as useful."""
    if value is None:
        return None
    schema_value = value.get("schema_version") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != _ADMISSION_FIELDS
            or type(schema_value) is not int or schema_value not in {1, 2}):
        raise InstallationUpdateOperationError("execution admission binding is invalid")
    if not all(isinstance(value.get(field), str) and value[field]
               for field in ("operation_id", "plan_digest", "installation_id")):
        raise InstallationUpdateOperationError("execution admission binding is invalid")
    registered = value.get("registered_installation")
    schema = schema_value
    assert type(schema) is int  # narrowed by the closed schema check above
    fields = _ADMISSION_RECORD_V1_FIELDS if schema == 1 else _ADMISSION_RECORD_V2_FIELDS
    if (not isinstance(registered, dict) or set(registered) != fields
            or not all(isinstance(registered.get(field), str) and registered[field]
                       for field in _ADMISSION_RECORD_V1_FIELDS - {"roles"})
            or not isinstance(registered.get("roles"), dict)):
        raise InstallationUpdateOperationError("execution admission binding is invalid")
    if not all(isinstance(key, str) and isinstance(item, str) and item
               for key, item in registered["roles"].items()):
        raise InstallationUpdateOperationError("execution admission binding is invalid")
    if schema == 2:
        try:
            full_record = operational_installation_record.validate_record(registered["record"])
        except operational_installation_record.OperationalInstallationRecordError as error:
            raise InstallationUpdateOperationError("execution admission binding is invalid") from error
        digest = "sha256:" + hashlib.sha256(_canonical(full_record)).hexdigest()
        if (
            registered["record_digest"] != digest
            or registered["installation_id"] != full_record["installation_id"]
            or registered["version"] != full_record["version"]
            or registered["artifact_digest"] != full_record["artifact_digest"]
            or registered["source_revision"] != full_record["source_revision"]
            or registered["interpreter"] != full_record["interpreter"]
            or registered["roles"] != full_record["roles"]
        ):
            raise InstallationUpdateOperationError("execution admission binding is invalid")
    candidate = _binding(value.get("prepared_candidate"))
    if candidate is None:
        raise InstallationUpdateOperationError("execution admission binding is invalid")
    return {
        "schema_version": schema,
        "operation_id": str(value["operation_id"]),
        "plan_digest": str(value["plan_digest"]),
        "installation_id": str(value["installation_id"]),
        "registered_installation": {
            **{field: registered[field] for field in _ADMISSION_RECORD_V1_FIELDS},
            **({"record": dict(full_record)} if schema == 2 else {}),
        },
        "prepared_candidate": candidate,
    }


def _provenance_binding(value: object) -> dict[str, object] | None:
    """Canonical pre-admission record proof captured with the OI-4b binding."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != _ADMISSION_RECORD_V1_FIELDS
            or not all(isinstance(value.get(field), str) and value[field]
                       for field in _ADMISSION_RECORD_V1_FIELDS - {"roles"})
            or not isinstance(value.get("roles"), dict)):
        raise InstallationUpdateOperationError("prepared record provenance is invalid")
    return dict(value)


def _current_record_provenance(plan: InstallationUpdatePlan) -> dict[str, object]:
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateOperationError("registered installation provenance is unavailable") from error
    if (record["installation_id"] != plan.installation_id
            or record["version"] != plan.current_version
            or record["artifact_digest"] != plan.current_digest):
        raise InstallationUpdateOperationError("registered installation changed before prepared candidate binding")
    return {
        "installation_id": record["installation_id"],
        "version": record["version"],
        "artifact_digest": record["artifact_digest"],
        "source_revision": record["source_revision"],
        "interpreter": record["interpreter"],
        "roles": record["roles"],
        "record_digest": "sha256:" + hashlib.sha256(_canonical(record)).hexdigest(),
    }


def _normalize(value: object, *, expected: dict[str, object] | None = None,
               operation_id: str | None = None) -> dict[str, object]:
    """Read schema-1 journals and normalize new journals to schema 2.

    Earlier journals did not have a prepared-candidate binding.  They retain
    their exact plan and lifecycle history but are explicitly unbound until a
    current, EP-owned candidate is verified and persisted.
    """
    if not isinstance(value, dict):
        raise InstallationUpdateOperationError("installation update operation is invalid")
    schema = value.get("schema_version")
    if type(schema) is not int:
        raise InstallationUpdateOperationError("installation update operation is invalid")
    if schema == 1 and set(value) == _V1_FIELDS:
        normalized: dict[str, object] = {
            **value,
            "schema_version": 2,
            "prepared_candidate": None,
            "prepared_candidate_digest": None,
        }
    elif schema == 2 and set(value) == _V2_FIELDS:
        normalized = dict(value)
    elif schema == 3 and set(value) == _V3_FIELDS:
        normalized = dict(value)
    elif schema == 4 and set(value) == _V4_FIELDS:
        normalized = dict(value)
    else:
        raise InstallationUpdateOperationError("installation update operation is invalid")
    plan = normalized.get("plan")
    if not isinstance(plan, dict):
        raise InstallationUpdateOperationError("installation update operation is invalid")
    if expected is not None and plan != expected:
        raise InstallationUpdateOperationError("installation update operation does not bind the exact plan")
    if operation_id is not None and normalized.get("operation_id") != operation_id:
        raise InstallationUpdateOperationError("installation update operation does not bind the exact plan")
    plan_digest = "sha256:" + hashlib.sha256(_canonical(plan)).hexdigest()
    if (normalized.get("plan_digest") != plan_digest or normalized.get("state") not in _STATES
            or not isinstance(normalized.get("events"), list)):
        raise InstallationUpdateOperationError("installation update operation is invalid")
    binding = _binding(normalized.get("prepared_candidate"))
    binding_digest = normalized.get("prepared_candidate_digest")
    if binding is None:
        if binding_digest is not None:
            raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    elif binding_digest != "sha256:" + hashlib.sha256(_canonical(binding)).hexdigest():
        raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    normalized["prepared_candidate"] = binding
    if schema in {3, 4}:
        provenance = _provenance_binding(normalized.get("prepared_record_provenance"))
        provenance_digest = normalized.get("prepared_record_provenance_digest")
        if provenance is None or provenance_digest != "sha256:" + hashlib.sha256(_canonical(provenance)).hexdigest():
            raise InstallationUpdateOperationError("prepared record provenance is invalid")
        normalized["prepared_record_provenance"] = provenance
    if schema == 4:
        admission = normalized.get("execution_admission")
        admission_digest = normalized.get("execution_admission_digest")
        admission = _admission_binding(admission)
        if admission is None:
            if admission_digest is not None:
                raise InstallationUpdateOperationError("execution admission binding is invalid")
        elif admission_digest != "sha256:" + hashlib.sha256(_canonical(admission)).hexdigest():
            raise InstallationUpdateOperationError("execution admission binding is invalid")
    return normalized


def _load(plan: InstallationUpdatePlan) -> dict[str, object]:
    try:
        value = json.loads(_path(plan).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallationUpdateOperationError("installation update operation is unreadable") from error
    expected = _plan(plan)
    return _normalize(value, expected=expected, operation_id=plan.operation_id)


def create(plan: InstallationUpdatePlan) -> dict[str, object]:
    """Create or recover the exact operation before any lifecycle side effect."""
    path, payload = _path(plan), _plan(plan)
    if path.exists():
        return _load(plan)
    value: dict[str, object] = {"schema_version": 2, "operation_id": plan.operation_id, "plan": payload,
                                "plan_digest": "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest(),
                                "state": "PREPARED", "events": [{"state": "PREPARED", "evidence": {}}],
                                "prepared_candidate": None, "prepared_candidate_digest": None}
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
    normalized = _normalize(value, operation_id=operation_id)
    plan = normalized["plan"]
    assert isinstance(plan, dict)  # guaranteed by ``_normalize``
    binding = normalized["prepared_candidate"]
    return {"operation_id": operation_id, "state": normalized["state"], "plan_digest": normalized.get("plan_digest"),
            "installation_id": plan.get("installation_id"), "current_version": plan.get("current_version"),
            "target_version": plan.get("target_version"), "target_digest": plan.get("target_digest"),
            "target_source_revision": plan.get("target_source_revision"), "cleanup_targets": plan.get("cleanup_targets"),
            "events": normalized["events"],
            "prepared_candidate": binding,
            "prepared_candidate_digest": normalized["prepared_candidate_digest"],
            "prepared_record_provenance": normalized.get("prepared_record_provenance"),
            "prepared_record_provenance_digest": normalized.get("prepared_record_provenance_digest"),
            "execution_admission": normalized.get("execution_admission"),
            "execution_admission_digest": normalized.get("execution_admission_digest")}


def _prepared_candidate(plan: InstallationUpdatePlan, candidate: object, *, runner: object) -> dict[str, object]:
    """Re-verify a candidate and require the plan to name its staged wheel."""
    from . import installation_update_preparation

    try:
        observed = installation_update_preparation.verify_prepared_candidate(
            plan,
            candidate=candidate if isinstance(candidate, installation_update_preparation.PreparedUpdateCandidate) else None,
            runner=runner,
        )
    except installation_update_preparation.InstallationUpdatePreparationError as error:
        raise InstallationUpdateOperationError("prepared candidate binding is invalid") from error
    if not isinstance(candidate, installation_update_preparation.PreparedUpdateCandidate):
        raise InstallationUpdateOperationError("prepared candidate binding is invalid")
    payload = observed.payload()
    if candidate.payload() != payload:
        raise InstallationUpdateOperationError("prepared candidate binding conflicts with the exact plan")
    if Path(plan.artifact).expanduser().absolute() != Path(str(payload["staged_artifact"])).expanduser().absolute():
        raise InstallationUpdateOperationError("prepared candidate binding requires the staged execution plan")
    return _binding(payload) or {}


def _bind_prepared_candidate(plan: InstallationUpdatePlan, candidate: object, *, runner: object) -> dict[str, object]:
    """Persist one verified candidate while the existing session owns its lock."""
    binding = _prepared_candidate(plan, candidate, runner=runner)
    provenance = _current_record_provenance(plan)
    value = _load(plan)
    current = value["prepared_candidate"]
    if value["state"] != "PREPARED":
        raise InstallationUpdateOperationError("prepared candidate binding requires the prepared operation state")
    if current is not None:
        if current == binding:
            existing_provenance = value.get("prepared_record_provenance")
            if existing_provenance is None:
                value["schema_version"] = 3
                value["prepared_record_provenance"] = provenance
                value["prepared_record_provenance_digest"] = "sha256:" + hashlib.sha256(_canonical(provenance)).hexdigest()
                _write(_path(plan), value)
            elif existing_provenance != provenance:
                raise InstallationUpdateOperationError("registered installation changed after prepared candidate binding")
            return value
        raise InstallationUpdateOperationError("prepared candidate binding conflicts with existing operation evidence")
    value["schema_version"] = 3
    value["prepared_candidate"] = binding
    value["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(_canonical(binding)).hexdigest()
    value["prepared_record_provenance"] = provenance
    value["prepared_record_provenance_digest"] = "sha256:" + hashlib.sha256(_canonical(provenance)).hexdigest()
    _write(_path(plan), value)
    return value


def recover_prepared_candidate(plan: InstallationUpdatePlan, *, runner: object) -> object:
    """Reopen a durable binding and prove it still names the exact candidate.

    This is read-only.  A caller that needs to create the binding must use the
    existing :class:`InstallationUpdateSession`, rather than introducing a
    second lock or lifecycle engine.
    """
    from . import installation_update_preparation

    value = _load(plan)
    binding = value["prepared_candidate"]
    if binding is None:
        raise InstallationUpdateOperationError("installation update has no prepared candidate binding")
    try:
        candidate = installation_update_preparation.PreparedUpdateCandidate(**binding)
    except (TypeError, ValueError) as error:
        raise InstallationUpdateOperationError("prepared candidate binding is invalid") from error
    _prepared_candidate(plan, candidate, runner=runner)
    return candidate


def execution_admission(plan: InstallationUpdatePlan) -> dict[str, object] | None:
    """Return the immutable OI-4c admission evidence, if any, without mutation."""
    value = _load(plan)
    admission = value.get("execution_admission")
    return None if admission is None else dict(admission)


def prepared_record_provenance(plan: InstallationUpdatePlan) -> dict[str, object] | None:
    """Read the OI-4b provenance binding; legacy V1/V2 journals remain unbound."""
    value = _load(plan)
    provenance = value.get("prepared_record_provenance")
    return None if provenance is None else dict(provenance)


def bind_execution_admission(plan: InstallationUpdatePlan, admission: Mapping[str, object]) -> dict[str, object]:
    """Persist one exact pre-cleanup admission while the session owns the lock.

    The OI-4c module constructs and revalidates this typed evidence.  This
    journal boundary only makes it immutable and rejects attempts to attach it
    to an unbound or already-progressed operation.
    """
    if not isinstance(admission, Mapping):
        raise InstallationUpdateOperationError("execution admission evidence is invalid")
    candidate = _admission_binding(dict(admission))
    if candidate is None:
        raise InstallationUpdateOperationError("execution admission evidence is invalid")
    value = _load(plan)
    if (value["state"] != "PREPARED" or value["prepared_candidate"] is None
            or value.get("prepared_record_provenance") is None):
        raise InstallationUpdateOperationError("execution admission requires a bound prepared operation")
    if (candidate["operation_id"] != plan.operation_id
            or candidate["installation_id"] != plan.installation_id
            or candidate["plan_digest"] != value["plan_digest"]
            or candidate["prepared_candidate"] != value["prepared_candidate"]):
        raise InstallationUpdateOperationError("execution admission does not bind the exact prepared operation")
    existing = value.get("execution_admission")
    if existing is not None:
        if existing == candidate:
            return value
        # Schema v1 bound only a provenance projection.  While (and only
        # while) the operation is still PREPARED, an exact v2 extension may
        # replace it so the executor never begins a mutating run that cannot
        # prove the record-CAS crash window.  This is an evidence migration,
        # not a new admission or a record/service action.
        legacy = _admission_binding(existing)
        if (
            legacy is None
            or legacy["schema_version"] != 1
            or candidate["schema_version"] != 2
            or legacy["operation_id"] != candidate["operation_id"]
            or legacy["plan_digest"] != candidate["plan_digest"]
            or legacy["installation_id"] != candidate["installation_id"]
            or legacy["prepared_candidate"] != candidate["prepared_candidate"]
            or legacy["registered_installation"]
            != {field: candidate["registered_installation"][field]
                for field in _ADMISSION_RECORD_V1_FIELDS}
        ):
            raise InstallationUpdateOperationError("execution admission conflicts with existing evidence")
        value["execution_admission"] = candidate
        value["execution_admission_digest"] = "sha256:" + hashlib.sha256(_canonical(candidate)).hexdigest()
        _write(_path(plan), value)
        return value
    value["schema_version"] = 4
    value["execution_admission"] = candidate
    value["execution_admission_digest"] = "sha256:" + hashlib.sha256(_canonical(candidate)).hexdigest()
    _write(_path(plan), value)
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

    def bind_prepared_candidate(self, candidate: object, *, runner: object) -> dict[str, object]:
        """Durably bind a verified non-operational candidate under this lock."""
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return _bind_prepared_candidate(self.plan, candidate, runner=runner)

    def recover_prepared_candidate(self, *, runner: object) -> object:
        """Read and verify the bound candidate while this operation is serialized."""
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return recover_prepared_candidate(self.plan, runner=runner)

    def execution_admission(self) -> dict[str, object] | None:
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return execution_admission(self.plan)

    def prepared_record_provenance(self) -> dict[str, object] | None:
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return prepared_record_provenance(self.plan)

    def bind_execution_admission(self, admission: Mapping[str, object]) -> dict[str, object]:
        if not self._owned:
            raise InstallationUpdateOperationError("installation update session does not own the lock")
        return bind_execution_admission(self.plan, admission)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._owned:
            self._lock.release(self.plan.operation_id)
            self._owned = False
