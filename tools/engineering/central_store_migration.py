"""Read-only Engineering Platform central-store migration preflight.

This module deliberately never opens the legacy store through ``open_storage``:
that API owns normal write/migration behaviour.  Increment 2 may inspect and
compare stores, but it cannot copy, create, checkpoint, freeze, stop, or hand
off an authority.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from .storage import DATABASE_FILENAME, ENGINEERING_STORAGE_SCHEMA_VERSION, database_path, legacy_database_path
from .providers import LaunchdProvider


TOOL_VERSION = "2.0.0-phase2-increment3"
EXPECTED_SCHEMA = ENGINEERING_STORAGE_SCHEMA_VERSION
REQUIRED_TABLES = frozenset({
    "engineering_schema_migrations", "engineering_metadata", "engineering_transactions",
    "execution_run_leases", "provider_recovery_attempts", "local_api_credentials",
    "local_api_consumer_registrations", "execution_runs", "execution_submissions",
    "provider_invocation_receipts", "prompt_execution_history",
    "execution_run_qualification_snapshots",
})
TERMINAL_PHASES = ("COMPLETE", "BLOCKED", "FAILED")
ACTIVE_RECOVERY_STATES = ("RECOVERY_AVAILABLE", "RECOVERY_STARTING", "RECOVERY_IN_PROGRESS")
FAILURE_CODES = frozenset({
    "LEGACY_STORE_NOT_FOUND", "LEGACY_STORE_AMBIGUOUS", "TARGET_STORE_CONFLICT",
    "ACTIVE_EXECUTION", "ACTIVE_LEASE", "SOURCE_SCHEMA_MISMATCH",
    "SOURCE_INTEGRITY_FAILED", "BACKUP_NOT_READY", "TARGET_UNREADABLE",
    "PROJECT_SCOPE_UNRESOLVED", "AUTHORITY_HANDOFF_NOT_SAFE", "ABORT_PRE_HANDOFF_FAILED",
})
CONTROL_KEY = "admission_freeze.v1"
STATE_KEY = "central_store_cutover.v1"
POINTER_VERSION = 1
STATES = (
    "PRECHECK", "ADMISSION_FROZEN", "QUIESCENT_SOURCE_BASELINE", "BACKUP_VERIFIED",
    "CENTRAL_STORE_CREATED", "TARGET_VERIFIED", "AUTHORITY_SWITCHED",
    "SERVICES_RESTARTED", "POST_CUTOVER_VERIFIED",
    "LEGACY_ROLLBACK_COMPATIBLE", "CENTRAL_STORE_ACTIVE_POST_WRITE", "ABORTED_PRE_HANDOFF",
)
ABORTABLE_STATES = frozenset({"PRECHECK", "ADMISSION_FROZEN", "QUIESCENT_SOURCE_BASELINE"})
ABORT_REASONS = frozenset({"CONTROLLER_VERSION_INCOMPATIBLE", "PRE_HANDOFF_CONTROLLER_DEFECT"})
SERVICE_STOP_ORDER = (
    "com.djconnect.engineering-inbox", "com.djconnect.engineering-local-api",
    "com.djconnect.engineering-dashboard-relay", "com.djconnect.engineering-dashboard",
)
SERVICE_START_ORDER = (
    "com.djconnect.engineering-local-api", "com.djconnect.engineering-dashboard",
    "com.djconnect.engineering-dashboard-relay", "com.djconnect.engineering-inbox",
)
EXPECTED_RUNNING_LOCKS = {
    "dashboard.lock": ("dashboard", "tools.engineering.dashboard", "com.djconnect.engineering-dashboard"),
    "inbox-watcher.lock": ("inbox-watcher", "tools.engineering.inbox_watcher", "com.djconnect.engineering-inbox"),
}


class CutoverError(RuntimeError):
    """Stable fail-closed cutover error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


class LaunchAgentServiceControl:
    """Canonical macOS LaunchAgent adapter; invoked only by explicit cutover CLI."""

    def __init__(self, uid: int | None = None) -> None:
        self._domain = f"gui/{uid if uid is not None else os.getuid()}"
        self._launchd = LaunchdProvider()

    @staticmethod
    def _plist(label: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def stop(self, label: str) -> None:
        try:
            self._launchd.quiesce(label, self._plist(label))
        except OSError as error:
            raise CutoverError("SERVICE_STOP_FAILED", label) from error
        if not self.stopped(label):
            raise CutoverError("SERVICE_STOP_FAILED", label)

    def start(self, label: str) -> None:
        try:
            self._launchd.resume(label, self._plist(label))
        except OSError as error:
            raise CutoverError("SERVICE_RESTART_FAILED", label) from error
        if not self.running(label):
            raise CutoverError("SERVICE_RESTART_FAILED", label)

    def stopped(self, label: str) -> bool:
        return not self._launchd.inspect(label)

    def running(self, label: str) -> bool:
        result = subprocess.run(["launchctl", "print", f"{self._domain}/{label}"], capture_output=True, text=True, check=False)
        return result.returncode == 0 and "state = running" in result.stdout


@dataclass(frozen=True)
class StoreCandidate:
    path: str
    resolved_path: str
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class StoreIdentity:
    path: str
    resolved_path: str
    size_bytes: int
    modified_ns: int
    fingerprint_sha256: str
    schema_version: int | None
    provenance: tuple[str, ...]


def user_data_dir(app_name: str = "Engineering Platform") -> Path:
    """Return the portable per-user application-data directory.

    This is the repository's dependency-free equivalent of
    ``platformdirs.user_data_dir``.  It follows platform application-data
    conventions without embedding a user or checkout path in the contract.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / app_name if base else Path.home() / "AppData" / "Local" / app_name
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) / app_name if base else Path.home() / ".local" / "share" / app_name


def installation_data_root() -> Path:
    """Resolve the one canonical, installation-owned EP data root."""
    return user_data_dir("Engineering Platform")


def central_store_path() -> Path:
    """Return the future central store path without creating it."""
    return installation_data_root() / DATABASE_FILENAME


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _schema(connection: sqlite3.Connection, tables: set[str] | None = None) -> int | None:
    tables = tables if tables is not None else _tables(connection)
    if "engineering_schema_migrations" not in tables:
        return None
    row = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def discover_legacy_stores(repo: Path, *, extra_runtime_roots: tuple[Path, ...] = ()) -> tuple[StoreCandidate, ...]:
    """Discover only canonical runtime-resolver candidates; never scan disks."""
    roots = ((repo.resolve(), "storage.database_path(repo)"),) + tuple(
        (root.resolve(), "explicit_runtime_evidence") for root in extra_runtime_roots
    )
    grouped: dict[Path, set[str]] = {}
    for root, provenance in roots:
        candidate = legacy_database_path(root).resolve()
        if candidate.is_file():
            grouped.setdefault(candidate, set()).add(provenance)
    return tuple(
        StoreCandidate(str(path), str(path), tuple(sorted(provenance)))
        for path, provenance in sorted(grouped.items(), key=lambda item: str(item[0]))
    )


def source_identity(candidate: StoreCandidate) -> StoreIdentity:
    path = Path(candidate.resolved_path)
    stat = path.stat()
    schema: int | None = None
    try:
        with _readonly(path) as connection:
            schema = _schema(connection)
    except (OSError, sqlite3.DatabaseError):
        pass
    return StoreIdentity(candidate.path, candidate.resolved_path, stat.st_size, stat.st_mtime_ns, _fingerprint(path), schema, candidate.provenance)


def _same_source_content(left: StoreIdentity, right: StoreIdentity | dict[str, object]) -> bool:
    """Compare source authority content; filesystem timestamps are not evidence writes."""
    values = asdict(left)
    other = asdict(right) if isinstance(right, StoreIdentity) else right
    return all(values.get(key) == other.get(key) for key in ("resolved_path", "size_bytes", "fingerprint_sha256", "schema_version"))


def classify_target(path: Path) -> dict[str, object]:
    """Classify a target without creating, modifying, or repairing it."""
    if not path.exists():
        return {"state": "ABSENT", "path": str(path), "blocking_code": None}
    if not path.is_file():
        return {"state": "UNKNOWN", "path": str(path), "blocking_code": "TARGET_UNREADABLE"}
    try:
        if path.stat().st_size == 0:
            return {"state": "EMPTY_NEW", "path": str(path), "blocking_code": None}
        with _readonly(path) as connection:
            tables = _tables(connection)
            if not tables:
                return {"state": "EMPTY_NEW", "path": str(path), "blocking_code": None}
            schema = _schema(connection, tables)
            if schema == EXPECTED_SCHEMA and REQUIRED_TABLES <= tables:
                return {"state": "COMPATIBLE_EXISTING", "path": str(path), "blocking_code": "TARGET_STORE_CONFLICT"}
            return {"state": "CONFLICTING_EXISTING", "path": str(path), "blocking_code": "TARGET_STORE_CONFLICT"}
    except (OSError, sqlite3.DatabaseError):
        return {"state": "CORRUPT_UNREADABLE", "path": str(path), "blocking_code": "TARGET_UNREADABLE"}


def inspect_source(candidate: StoreCandidate) -> dict[str, object]:
    """Read source schema/integrity/table facts without a write transaction."""
    path = Path(candidate.resolved_path)
    result: dict[str, object] = {"identity": asdict(source_identity(candidate)), "tables": [], "integrity": "FAILED", "blocking_codes": []}
    try:
        with _readonly(path) as connection:
            tables = _tables(connection)
            schema = _schema(connection, tables)
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            result.update({"tables": sorted(tables), "schema_version": schema, "integrity": "PASS" if integrity == ["ok"] else "FAILED"})
            if schema != EXPECTED_SCHEMA:
                result["blocking_codes"].append("SOURCE_SCHEMA_MISMATCH")
            if integrity != ["ok"] or not REQUIRED_TABLES <= tables:
                result["blocking_codes"].append("SOURCE_INTEGRITY_FAILED")
            result["required_tables_present"] = sorted(REQUIRED_TABLES & tables)
            result["required_tables_missing"] = sorted(REQUIRED_TABLES - tables)
            result["journal_mode"] = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    except (OSError, sqlite3.DatabaseError):
        result["blocking_codes"].append("SOURCE_INTEGRITY_FAILED")
    return result


def _count(connection: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    return int(connection.execute(query, params).fetchone()[0])


def _lock_owner(lock: Path) -> tuple[str, int] | None:
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
        component = payload.get("component") if isinstance(payload, dict) else None
        process_id = payload.get("pid") if isinstance(payload, dict) else None
        if isinstance(component, str) and isinstance(process_id, int) and process_id > 0:
            return component, process_id
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _process_command(process_id: int) -> str | None:
    result = subprocess.run(["ps", "-p", str(process_id), "-o", "command="], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _classify_lock(lock: Path, *, pre_stop: bool, services: LaunchAgentServiceControl | None) -> str:
    """Classify a held component lock without treating its filename as ownership."""
    try:
        with lock.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                owner = _lock_owner(lock)
                expected = EXPECTED_RUNNING_LOCKS.get(lock.name)
                if owner is not None and expected is not None and owner[0] == expected[0]:
                    try:
                        os.kill(owner[1], 0)
                    except ProcessLookupError:
                        return "INACTIVE_EXPECTED_SERVICE_LOCK"
                    except PermissionError:
                        return "UNKNOWN_LOCK"
                return "STALE_UNOWNED_LOCK"
    except OSError:
        return "UNKNOWN_LOCK"
    owner = _lock_owner(lock)
    expected = EXPECTED_RUNNING_LOCKS.get(lock.name)
    if not pre_stop or owner is None or expected is None or services is None:
        return "UNKNOWN_LOCK"
    component, process_id = owner
    expected_component, expected_module, expected_service = expected
    command = _process_command(process_id)
    if component == expected_component and command is not None and expected_module in command and services.running(expected_service):
        return "EXPECTED_RUNNING_SERVICE_LOCK"
    return "UNEXPECTED_LIVE_LOCK"


def inspect_quiescence(path: Path, *, pre_stop: bool = False, services: LaunchAgentServiceControl | None = None) -> dict[str, object]:
    """Inspect lifecycle blockers; only verified canonical service locks are allowed pre-stop."""
    facts: dict[str, object] = {"non_terminal_transactions": 0, "active_leases": 0, "active_recovery": 0, "unsafe_locks": [], "lock_classifications": {}, "blocking_codes": []}
    try:
        with _readonly(path) as connection:
            tables = _tables(connection)
            if "engineering_transactions" in tables:
                facts["non_terminal_transactions"] = _count(connection, "SELECT COUNT(*) FROM engineering_transactions WHERE phase NOT IN (?,?,?)", TERMINAL_PHASES)
            if "execution_run_leases" in tables:
                facts["active_leases"] = _count(connection, "SELECT COUNT(*) FROM execution_run_leases WHERE lease_state='ACTIVE'")
            if "provider_recovery_attempts" in tables:
                placeholders = ",".join("?" for _ in ACTIVE_RECOVERY_STATES)
                facts["active_recovery"] = _count(connection, f"SELECT COUNT(*) FROM provider_recovery_attempts WHERE state IN ({placeholders})", ACTIVE_RECOVERY_STATES)
    except (OSError, sqlite3.DatabaseError):
        facts["blocking_codes"].append("AUTHORITY_HANDOFF_NOT_SAFE")
    locks = path.parent / "locks"
    if locks.is_dir():
        for lock in sorted(locks.glob("*.lock")):
            classification = _classify_lock(lock, pre_stop=pre_stop, services=services)
            facts["lock_classifications"][lock.name] = classification
            allowed = {"EXPECTED_RUNNING_SERVICE_LOCK"} if pre_stop else {"INACTIVE_EXPECTED_SERVICE_LOCK"}
            if classification not in allowed:
                facts["unsafe_locks"].append(lock.name)
    if facts["non_terminal_transactions"]:
        facts["blocking_codes"].append("ACTIVE_EXECUTION")
    if facts["active_leases"]:
        facts["blocking_codes"].append("ACTIVE_LEASE")
    if facts["active_recovery"] or facts["unsafe_locks"]:
        facts["blocking_codes"].append("AUTHORITY_HANDOFF_NOT_SAFE")
    facts["eligible"] = not facts["blocking_codes"]
    return facts


def project_scope_inventory(path: Path) -> dict[str, object]:
    """Return project and credential metadata counts only; never credential values."""
    result: dict[str, object] = {"project_ids": [], "consumer_registrations": 0, "credential_scopes": 0, "run_project_associations": "NOT_PERSISTED", "prompt_history_project_relationships": "NOT_PERSISTED", "plaintext_credential_columns": [], "blocking_codes": []}
    try:
        with _readonly(path) as connection:
            tables = _tables(connection)
            if "local_api_consumer_registrations" in tables:
                rows = connection.execute("SELECT DISTINCT project_id FROM local_api_consumer_registrations ORDER BY project_id").fetchall()
                result["project_ids"] = [str(row[0]) for row in rows]
                result["consumer_registrations"] = _count(connection, "SELECT COUNT(*) FROM local_api_consumer_registrations")
            if "local_api_credentials" in tables:
                result["credential_scopes"] = _count(connection, "SELECT COUNT(*) FROM local_api_credentials")
                columns = {str(row[1]).casefold() for row in connection.execute("PRAGMA table_info(local_api_credentials)")}
                result["plaintext_credential_columns"] = sorted(columns & {"credential", "token", "bearer", "secret", "plaintext"})
            for table in ("engineering_transactions", "prompt_execution_history"):
                if table in tables:
                    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                    key = "run_project_associations" if table == "engineering_transactions" else "prompt_history_project_relationships"
                    result[key] = "PERSISTED" if "project_id" in columns else "NOT_PERSISTED"
            if result["plaintext_credential_columns"]:
                result["blocking_codes"].append("SOURCE_INTEGRITY_FAILED")
    except (OSError, sqlite3.DatabaseError):
        result["blocking_codes"].append("PROJECT_SCOPE_UNRESOLVED")
    return result


def backup_readiness(identity: StoreIdentity, root: Path) -> dict[str, object]:
    backup = root / "backups" / f"legacy-schema40-{identity.fingerprint_sha256[:16]}-<migration-id>.db"
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    writable = os.access(ancestor, os.W_OK | os.X_OK)
    available = shutil.disk_usage(ancestor).free if ancestor.exists() else 0
    ready = writable and available >= identity.size_bytes
    return {"backup_path": str(backup), "root_exists": root.exists(), "writable_ancestor": str(ancestor), "available_bytes": available, "required_bytes": identity.size_bytes, "integrity_method": "PRAGMA integrity_check", "ready": ready, "blocking_code": None if ready else "BACKUP_NOT_READY"}


def snapshot_plan(source: dict[str, object]) -> dict[str, object]:
    identity = source.get("identity", {})
    path = Path(str(identity.get("resolved_path", "")))
    return {"strategy": "sqlite_backup_api", "requires_future_quiescence": True, "source_read_only": True, "temporary_target_then_fsync_atomic_rename": True, "raw_copy_forbidden": True, "journal_mode": source.get("journal_mode"), "wal_present": path.with_name(path.name + "-wal").exists(), "shm_present": path.with_name(path.name + "-shm").exists(), "checkpoint_requirement": "PRAGMA wal_checkpoint(TRUNCATE) by the sole controlled writer before copy"}


def _table_counts(path: Path) -> dict[str, int]:
    with _readonly(path) as connection:
        return {table: _count(connection, f'SELECT COUNT(*) FROM "{table}"') for table in sorted(_tables(connection)) if not table.startswith("sqlite_")}


def validate_target_equivalence(source: Path, target: Path) -> dict[str, object]:
    """Compare a candidate target read-only; intended for test/future cutover use."""
    result: dict[str, object] = {"equivalent": False, "blocking_codes": [], "differences": []}
    try:
        source_facts, target_facts = inspect_source(StoreCandidate(str(source), str(source.resolve()), ("comparison_source",))), inspect_source(StoreCandidate(str(target), str(target.resolve()), ("comparison_target",)))
        if source_facts.get("schema_version") != target_facts.get("schema_version"):
            result["differences"].append("schema_version")
        if source_facts.get("tables") != target_facts.get("tables"):
            result["differences"].append("tables")
        if _table_counts(source) != _table_counts(target):
            result["differences"].append("table_counts")
        if project_scope_inventory(source) != project_scope_inventory(target):
            result["differences"].append("project_scope")
        result["equivalent"] = not result["differences"]
    except (OSError, sqlite3.DatabaseError):
        result["differences"].append("target_unreadable")
        result["blocking_codes"].append("TARGET_UNREADABLE")
    if not result["equivalent"] and not result["blocking_codes"]:
        result["blocking_codes"].append("TARGET_STORE_CONFLICT")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata(path: Path, key: str) -> dict[str, object] | None:
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    if not isinstance(value, dict):
        raise CutoverError("ADMISSION_FREEZE_FAILED", "metadata is malformed")
    return value


def admission_status(repo: Path) -> dict[str, object]:
    path = database_path(repo)
    if not path.is_file():
        raise CutoverError("ADMISSION_FREEZE_FAILED", "authority is unresolved")
    payload = _metadata(path, CONTROL_KEY)
    return payload or {"state": "INACTIVE"}


def set_admission_freeze(repo: Path, *, migration_id: str | None = None, reason: str, operator: str = "operator") -> dict[str, object]:
    """Explicit control-plane mutation; no prompt/provider path calls this."""
    candidates = discover_legacy_stores(repo)
    if len(candidates) != 1 or not reason.strip():
        raise CutoverError("ADMISSION_FREEZE_FAILED")
    migration_id = migration_id or str(uuid.uuid4())
    active = admission_status(repo)
    if active.get("state") == "ACTIVE" and active.get("migration_id") != migration_id:
        raise CutoverError("ADMISSION_FREEZE_FAILED", "conflicting active migration")
    path = Path(candidates[0].resolved_path)
    payload = {"version": 1, "migration_id": migration_id, "state": "ACTIVE", "reason": reason.strip(), "operator": operator, "created_at": _now()}
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CONTROL_KEY, json.dumps(payload, sort_keys=True, separators=(",", ":"))))
    # This identity documents the pre-stop source only.  Service shutdown is
    # allowed to write bounded lifecycle evidence, so it is never the copy
    # baseline or a source-drift gate.
    receipt = load_receipt(migration_id)
    if receipt is None:
        receipt = {
            "receipt_version": 1,
            "migration_id": migration_id,
            "schema": EXPECTED_SCHEMA,
            "operator": operator,
            "legacy_path": str(path),
            "pre_stop_source_identity": asdict(source_identity(candidates[0])),
            "rollback_mode": "PRE_WRITE_DIRECT",
        }
        transition_receipt(receipt, "PRECHECK")
        transition_receipt(receipt, "ADMISSION_FROZEN", admission_freeze=payload)
    return payload


def thaw_admission(repo: Path, *, migration_id: str, operator: str = "operator") -> dict[str, object]:
    state = admission_status(repo)
    if state.get("state") != "ACTIVE" or state.get("migration_id") != migration_id:
        raise CutoverError("THAW_FAILED")
    path = database_path(repo)
    payload = {"version": 1, "migration_id": migration_id, "state": "INACTIVE", "operator": operator, "thawed_at": _now()}
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE engineering_metadata SET value=? WHERE key=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), CONTROL_KEY))
    receipt = load_receipt(migration_id)
    if receipt is not None and receipt.get("state") == "ABORTED_PRE_HANDOFF":
        receipt["thaw"] = {"operator": operator, "timestamp": payload["thawed_at"], "state": "INACTIVE"}
        _atomic_json(receipt_path(migration_id), receipt)
    return payload


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def receipt_path(migration_id: str) -> Path:
    return installation_data_root() / "migration" / f"{migration_id}.json"


def load_receipt(migration_id: str) -> dict[str, object] | None:
    path = receipt_path(migration_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CutoverError("AUTHORITY_SWITCH_FAILED", "receipt is malformed") from error
    if not isinstance(payload, dict) or payload.get("migration_id") != migration_id:
        raise CutoverError("AUTHORITY_SWITCH_FAILED", "receipt identity is invalid")
    return payload


def transition_receipt(receipt: dict[str, object], state: str, **details: object) -> dict[str, object]:
    """Persist only adjacent forward transitions; state cannot be skipped/backtracked."""
    if state not in STATES:
        raise CutoverError("AUTHORITY_SWITCH_FAILED", "unknown state")
    prior = receipt.get("state")
    if prior is not None:
        if state == "ABORTED_PRE_HANDOFF":
            if prior not in ABORTABLE_STATES:
                raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "migration is not pre-handoff")
        else:
            try:
                expected = STATES[STATES.index(str(prior)) + 1]
            except (ValueError, IndexError) as error:
                raise CutoverError("AUTHORITY_SWITCH_FAILED", "terminal or invalid transition") from error
            if state != expected:
                raise CutoverError("AUTHORITY_SWITCH_FAILED", "non-monotonic transition")
    receipt["state"] = state
    receipt.setdefault("transitions", []).append({"state": state, "timestamp": _now()})
    receipt.update(details)
    _atomic_json(receipt_path(str(receipt["migration_id"])), receipt)
    return receipt


def abort_pre_handoff(
    repo: Path,
    *,
    migration_id: str,
    reason: str,
    operator: str = "operator",
    services: LaunchAgentServiceControl | None = None,
) -> dict[str, object]:
    """Retire one frozen migration before any backup, target, or authority handoff."""
    if reason not in ABORT_REASONS:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "reason is not allowed")
    try:
        freeze = admission_status(repo)
    except CutoverError as error:
        if authority_pointer_path().exists():
            raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "authority handoff is present") from error
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "freeze authority is unresolved") from error
    receipt = load_receipt(migration_id)
    if freeze.get("state") == "ACTIVE" and freeze.get("migration_id") != migration_id:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "active freeze belongs to another migration")
    if receipt is not None and receipt.get("state") == "ABORTED_PRE_HANDOFF":
        return {"migration_id": migration_id, "state": "ABORTED_PRE_HANDOFF", "already_aborted": True}
    if freeze.get("state") != "ACTIVE" or freeze.get("migration_id") != migration_id:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "matching active freeze is required")
    candidates = discover_legacy_stores(repo)
    if len(candidates) != 1:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "legacy authority is unresolved")
    source = Path(candidates[0].resolved_path)
    if database_path(repo).resolve() != source.resolve() or authority_pointer_path().exists():
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "authority handoff is present")
    target_state = classify_target(central_store_path())["state"]
    backups = installation_data_root() / "backups"
    if target_state != "ABSENT" or (backups.exists() and any(backups.glob(f"*{migration_id}*"))):
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "target or backup is present")
    if receipt is not None and receipt.get("state") not in ABORTABLE_STATES:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "migration is beyond the abort boundary")
    quiescence = inspect_quiescence(source, pre_stop=True, services=services or LaunchAgentServiceControl())
    if not quiescence["eligible"]:
        raise CutoverError("ABORT_PRE_HANDOFF_FAILED", "active execution state is unsafe")
    if receipt is None:
        receipt = {
            "receipt_version": 1,
            "migration_id": migration_id,
            "schema": EXPECTED_SCHEMA,
            "legacy_path": str(source),
            "historical_freeze": freeze,
            "pre_stop_source": asdict(source_identity(candidates[0])),
            "rollback_mode": "NOT_REQUIRED_PRE_HANDOFF",
        }
        transition_receipt(receipt, "PRECHECK")
        transition_receipt(receipt, "ADMISSION_FROZEN", admission_freeze=freeze)
    receipt.setdefault("historical_freeze", freeze)
    return transition_receipt(
        receipt,
        "ABORTED_PRE_HANDOFF",
        abort={
            "reason": reason,
            "operator": operator,
            "timestamp": _now(),
            "authority": "LEGACY",
            "central_target_state": target_state,
            "authority_pointer": "ABSENT",
            "quiescence": quiescence,
            "tool_version": TOOL_VERSION,
        },
    )


def authority_pointer_path() -> Path:
    return installation_data_root() / "runtime" / "store-authority.json"


def write_authority_pointer(*, migration_id: str, authority: Path, legacy: Path, state: str) -> dict[str, object]:
    if state not in STATES or not authority.is_file():
        raise CutoverError("AUTHORITY_SWITCH_FAILED")
    payload = {"version": POINTER_VERSION, "migration_id": migration_id, "authoritative_path": str(authority.resolve()), "legacy_path": str(legacy.resolve()), "schema": EXPECTED_SCHEMA, "state": state, "timestamp": _now(), "fingerprint_sha256": _fingerprint(authority)}
    _atomic_json(authority_pointer_path(), payload)
    return payload


def copy_snapshot(source: Path, destination: Path) -> None:
    """Create an fsynced SQLite backup snapshot, never a raw file copy."""
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with _readonly(source) as read_connection, sqlite3.connect(temporary) as write_connection:
            read_connection.backup(write_connection)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except (OSError, sqlite3.DatabaseError) as error:
        raise CutoverError("BACKUP_FAILED", str(error)) from error
    finally:
        if temporary.exists():
            temporary.unlink()


def service_binding_proof(repo: Path, *, expected: Path, services: tuple[str, ...] = SERVICE_STOP_ORDER) -> dict[str, object]:
    """All runtime surfaces share storage.database_path; mixed paths block."""
    try:
        resolved = database_path(repo).resolve()
    except Exception as error:
        raise CutoverError("CENTRAL_STORE_NOT_IN_USE") from error
    consistent = resolved == expected.resolve()
    return {"consistent": consistent, "authoritative_store": str(resolved), "services": {label: str(resolved) for label in services}}


def quiescent_source_baseline(candidate: StoreCandidate) -> dict[str, object]:
    """Capture the one authoritative source identity after strict quiescence."""
    source = Path(candidate.resolved_path)
    facts = inspect_source(candidate)
    scope = project_scope_inventory(source)
    if facts["blocking_codes"] or scope["blocking_codes"]:
        raise CutoverError("SOURCE_CHANGED_AFTER_PREFLIGHT")
    return {
        "state": "QUIESCENT_SOURCE_BASELINE",
        "captured_at": _now(),
        "source": asdict(source_identity(candidate)),
        "integrity": facts["integrity"],
        "critical_table_counts": _table_counts(source),
        "project_scope": scope,
    }


def _require_quiescent_source_stable(candidate: StoreCandidate, baseline: dict[str, object]) -> StoreIdentity:
    identity = baseline.get("source")
    if not isinstance(identity, dict) or not _same_source_content(source_identity(candidate), identity):
        raise CutoverError("SOURCE_CHANGED_AFTER_PREFLIGHT")
    return source_identity(candidate)


def controlled_cutover(repo: Path, *, operator: str = "operator", services: LaunchAgentServiceControl | None = None) -> dict[str, object]:
    """Perform one frozen cutover transaction through staged handoff gates."""
    candidate = discover_legacy_stores(repo)
    if len(candidate) != 1:
        raise CutoverError("QUIESCENCE_FAILED")
    source = Path(candidate[0].resolved_path)
    freeze = admission_status(repo)
    migration_id = freeze.get("migration_id")
    if freeze.get("state") != "ACTIVE" or not isinstance(migration_id, str) or not migration_id:
        raise CutoverError("ADMISSION_FREEZE_FAILED")
    existing = load_receipt(migration_id)
    if existing is not None and existing.get("state") not in {"PRECHECK", "ADMISSION_FROZEN", "QUIESCENT_SOURCE_BASELINE"}:
        if existing.get("state") in STATES:
            return existing
        raise CutoverError("AUTHORITY_SWITCH_FAILED", "migration receipt is invalid")
    receipt: dict[str, object] = existing or {
        "receipt_version": 1, "migration_id": migration_id, "schema": EXPECTED_SCHEMA,
        "operator": operator, "legacy_path": str(source), "rollback_mode": "PRE_WRITE_DIRECT",
    }
    if receipt.get("state") != "QUIESCENT_SOURCE_BASELINE":
        # A live watcher/dashboard lock is expected before maintenance, but a
        # failed pre-baseline attempt may already have durably unloaded every
        # owned LaunchAgent.  That bounded state is safe to resume only when
        # the strict post-stop gate proves every service is still stopped.
        pre_stop = inspect_quiescence(source, pre_stop=True, services=services)
        already_quiesced = False
        if not pre_stop["eligible"]:
            post_stop = inspect_quiescence(source, services=services)
            services_stopped = services is not None and all(services.stopped(label) for label in SERVICE_STOP_ORDER)
            already_quiesced = existing is not None and post_stop["eligible"] and services_stopped
            if not already_quiesced:
                raise CutoverError("QUIESCENCE_FAILED")
        if services is not None and not already_quiesced:
            for label in SERVICE_STOP_ORDER:
                services.stop(label)
                if not services.stopped(label):
                    raise CutoverError("SERVICE_STOP_FAILED", label)
        quiescence = inspect_quiescence(source, services=services)
        if not quiescence["eligible"]:
            raise CutoverError("QUIESCENCE_FAILED")
        baseline = quiescent_source_baseline(candidate[0])
        transition_receipt(
            receipt,
            "QUIESCENT_SOURCE_BASELINE",
            quiescence=quiescence,
            source=baseline["source"],
            quiescent_source_baseline=baseline,
        )
    else:
        quiescence = inspect_quiescence(source, services=services)
        if not quiescence["eligible"]:
            raise CutoverError("QUIESCENCE_FAILED")
    baseline = receipt.get("quiescent_source_baseline")
    if not isinstance(baseline, dict):
        raise CutoverError("AUTHORITY_SWITCH_FAILED", "quiescent source baseline is missing")
    _require_quiescent_source_stable(candidate[0], baseline)
    target = central_store_path()
    if classify_target(target)["state"] != "ABSENT":
        raise CutoverError("TARGET_CREATE_FAILED")
    _require_quiescent_source_stable(candidate[0], baseline)
    backup = installation_data_root() / "backups" / f"legacy-schema40-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{migration_id}.db"
    copy_snapshot(source, backup)
    if not validate_target_equivalence(source, backup)["equivalent"]:
        raise CutoverError("BACKUP_FAILED")
    transition_receipt(receipt, "BACKUP_VERIFIED", backup={"path": str(backup), "fingerprint_sha256": _fingerprint(backup)})
    copy_snapshot(source, target)
    transition_receipt(receipt, "CENTRAL_STORE_CREATED", target={"path": str(target)})
    equivalent = validate_target_equivalence(source, target)
    if not equivalent["equivalent"]:
        raise CutoverError("TARGET_EQUIVALENCE_FAILED")
    transition_receipt(receipt, "TARGET_VERIFIED", equivalence=equivalent)
    pointer = write_authority_pointer(migration_id=migration_id, authority=target, legacy=source, state="AUTHORITY_SWITCHED")
    transition_receipt(receipt, "AUTHORITY_SWITCHED", authority_pointer=pointer)
    if services is not None:
        for label in SERVICE_START_ORDER:
            services.start(label)
        transition_receipt(receipt, "SERVICES_RESTARTED", service_binding=service_binding_proof(repo, expected=target))
    return receipt


def rollback(repo: Path, *, migration_id: str, operator: str = "operator") -> dict[str, object]:
    """Restore legacy authority only before the first central production write."""
    receipt = load_receipt(migration_id)
    if receipt is None or receipt.get("state") != "LEGACY_ROLLBACK_COMPATIBLE":
        raise CutoverError("DIRECT_ROLLBACK_NOT_SAFE")
    freeze = admission_status(repo)
    if freeze.get("state") != "ACTIVE" or freeze.get("migration_id") != migration_id:
        raise CutoverError("ROLLBACK_FAILED", "freeze is not active")
    legacy = Path(str(receipt["legacy_path"]))
    source = receipt.get("source")
    if not legacy.is_file() or not isinstance(source, dict) or _fingerprint(legacy) != source.get("fingerprint_sha256"):
        raise CutoverError("ROLLBACK_FAILED", "legacy identity changed")
    target = central_store_path()
    if not inspect_quiescence(target)["eligible"]:
        raise CutoverError("ROLLBACK_FAILED", "central is not quiescent")
    pointer = write_authority_pointer(migration_id=migration_id, authority=legacy, legacy=legacy, state="LEGACY_ROLLBACK_COMPATIBLE")
    receipt["rollback"] = {"operator": operator, "timestamp": _now(), "authority_pointer": pointer}
    _atomic_json(receipt_path(migration_id), receipt)
    return receipt


def complete_stage_a(repo: Path, *, migration_id: str) -> dict[str, object]:
    """Record read-only qualification before an operator may thaw admission."""
    receipt = load_receipt(migration_id)
    if receipt is None or receipt.get("state") != "AUTHORITY_SWITCHED":
        raise CutoverError("POST_CUTOVER_READINESS_FAILED")
    target = central_store_path()
    facts = inspect_source(StoreCandidate(str(target), str(target.resolve()), ("central_authority",)))
    if facts["blocking_codes"] or database_path(repo).resolve() != target.resolve():
        raise CutoverError("CENTRAL_STORE_NOT_IN_USE")
    transition_receipt(receipt, "SERVICES_RESTARTED", service_binding={"consistent": True, "authoritative_store": str(target.resolve())})
    transition_receipt(receipt, "POST_CUTOVER_VERIFIED", readonly_qualification="PASS")
    return transition_receipt(receipt, "LEGACY_ROLLBACK_COMPATIBLE", rollback_mode="PRE_WRITE_DIRECT")


def mark_central_post_write(repo: Path) -> None:
    """One-way data-loss guard called only after an admitted central write."""
    pointer_path = authority_pointer_path()
    if not pointer_path.is_file():
        return
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        migration_id = str(pointer["migration_id"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise CutoverError("CENTRAL_STORE_NOT_IN_USE") from error
    receipt = load_receipt(migration_id)
    if receipt is not None and receipt.get("state") == "LEGACY_ROLLBACK_COMPATIBLE":
        transition_receipt(receipt, "CENTRAL_STORE_ACTIVE_POST_WRITE", rollback_mode="REVERSE_MIGRATION_REQUIRED")


def preflight(repo: Path, *, extra_runtime_roots: tuple[Path, ...] = ()) -> dict[str, object]:
    """Compute the complete migration plan strictly read-only."""
    candidates = discover_legacy_stores(repo, extra_runtime_roots=extra_runtime_roots)
    try:
        freeze = admission_status(repo)
    except CutoverError:
        freeze = {"state": "UNRESOLVED"}
    receipt: dict[str, object] = {"receipt_version": 1, "mode": "DRY_RUN", "timestamp": datetime.now(timezone.utc).isoformat(), "tool_version": TOOL_VERSION, "target_data_root": str(installation_data_root()), "target_store": classify_target(central_store_path()), "source_candidates": [asdict(item) for item in candidates], "blocking_codes": [], "service_stop_plan": ["inbox_watcher", "separately_managed_execution_service", "local_consumer_api", "dashboard_relay", "dashboard"], "admission_freeze": freeze}
    if not candidates:
        receipt["blocking_codes"].append("LEGACY_STORE_NOT_FOUND")
        receipt["eligible"] = False
        return receipt
    if len(candidates) > 1:
        receipt["blocking_codes"].append("LEGACY_STORE_AMBIGUOUS")
        receipt["eligible"] = False
        return receipt
    candidate = candidates[0]
    source = inspect_source(candidate)
    identity = source_identity(candidate)
    quiescence = inspect_quiescence(Path(candidate.resolved_path))
    inventory = project_scope_inventory(Path(candidate.resolved_path))
    backup = backup_readiness(identity, installation_data_root())
    receipt.update({"source": source, "quiescence": quiescence, "backup_readiness": backup, "snapshot_strategy": snapshot_plan(source), "project_scope": inventory, "critical_table_counts": _table_counts(Path(candidate.resolved_path))})
    codes = list(source["blocking_codes"]) + list(quiescence["blocking_codes"]) + list(inventory["blocking_codes"])
    if backup["blocking_code"]:
        codes.append(backup["blocking_code"])
    target_code = receipt["target_store"].get("blocking_code")
    if target_code:
        codes.append(str(target_code))
    receipt["blocking_codes"] = sorted(set(codes))
    receipt["eligible"] = not receipt["blocking_codes"]
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-central-store-migration")
    parser.add_argument("command", choices=("preflight", "dry-run", "freeze", "freeze-status", "abort", "thaw", "cutover", "rollback", "status"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--migration-id")
    parser.add_argument("--reason")
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--execute", action="store_true", help="required for a mutating production operation")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        if args.command in {"preflight", "dry-run"}:
            result = preflight(repo)
        elif args.command == "freeze-status":
            result = admission_status(repo)
        elif args.command == "status":
            result = {"admission_freeze": admission_status(repo), "authority_pointer": str(authority_pointer_path()), "authoritative_store": str(database_path(repo))}
        elif not args.execute:
            raise CutoverError("ADMISSION_FREEZE_FAILED", "--execute is required")
        elif args.command == "freeze":
            if not args.reason:
                raise CutoverError("ADMISSION_FREEZE_FAILED")
            result = set_admission_freeze(repo, migration_id=args.migration_id, reason=args.reason, operator=args.operator)
        elif args.command == "abort":
            if not args.migration_id or not args.reason:
                raise CutoverError("ABORT_PRE_HANDOFF_FAILED")
            result = abort_pre_handoff(repo, migration_id=args.migration_id, reason=args.reason, operator=args.operator)
        elif args.command == "thaw":
            if not args.migration_id:
                raise CutoverError("THAW_FAILED")
            result = thaw_admission(repo, migration_id=args.migration_id, operator=args.operator)
        elif args.command == "rollback":
            if not args.migration_id:
                raise CutoverError("ROLLBACK_FAILED")
            result = rollback(repo, migration_id=args.migration_id, operator=args.operator)
        else:
            result = controlled_cutover(repo, operator=args.operator, services=LaunchAgentServiceControl())
    except CutoverError as error:
        result = {"ok": False, "code": error.code}
        if args.json:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            print(error.code)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result, sort_keys=True))
    return 0 if args.command not in {"preflight", "dry-run"} or result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
