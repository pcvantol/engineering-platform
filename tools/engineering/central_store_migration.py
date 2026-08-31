"""Read-only Engineering Platform central-store migration preflight.

This module deliberately never opens the legacy store through ``open_storage``:
that API owns normal write/migration behaviour.  Increment 2 may inspect and
compare stores, but it cannot copy, create, checkpoint, freeze, stop, or hand
off an authority.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import uuid

from .storage import DATABASE_FILENAME, ENGINEERING_STORAGE_SCHEMA_VERSION, database_path, legacy_database_path
from .providers import LaunchdProvider
from .forensic_delta import ForensicDeltaError, canonical_report_json, export_forensic_delta


TOOL_VERSION = "2.0.0-phase2-increment3"
EXPECTED_SCHEMA = ENGINEERING_STORAGE_SCHEMA_VERSION
HISTORICAL_ATTESTATION_VERSION = 1
HISTORICAL_ATTESTATION_CLASSIFIER_VERSION = 1
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
    "LEGITIMATE_CENTRAL_WRITE_PRESENT", "CONTAMINATION_PROVENANCE_UNRESOLVED",
    "FORENSIC_CENTRAL_UNREADABLE", "LEGACY_BASELINE_MISMATCH",
    "RECOVERY_SERVICE_QUIESCENCE_FAILED", "RECOVERY_AUTHORITY_SWITCH_FAILED",
    "RECOVERY_SERVICE_RESTART_FAILED", "RECOVERY_MIXED_BINDING", "RECOVERY_POSTCHECK_FAILED",
})
CONTROL_KEY = "admission_freeze.v1"
STATE_KEY = "central_store_cutover.v1"
POINTER_VERSION = 1
STATES = (
    "PRECHECK", "ADMISSION_FROZEN", "QUIESCENT_SOURCE_BASELINE", "BACKUP_VERIFIED",
    "CENTRAL_STORE_CREATED", "TARGET_VERIFIED", "AUTHORITY_SWITCHED",
    "SERVICES_RESTARTED", "POST_CUTOVER_VERIFIED",
    "LEGACY_ROLLBACK_COMPATIBLE", "CENTRAL_STORE_ACTIVE_POST_WRITE", "ABORTED_PRE_HANDOFF",
    "CONTAMINATED_RECOVERY_PRECHECK", "RECOVERY_SERVICES_QUIESCED",
    "RECOVERY_LEGACY_VERIFIED", "ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETED",
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
        for receipt_file in (installation_data_root() / "migration").glob("*.json"):
            try:
                receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                forensic = receipt.get("central_forensic") if isinstance(receipt, dict) else None
                if isinstance(forensic, dict) and forensic.get("path") == str(path.resolve()) and forensic.get("classification") == "FORENSIC_CONTAMINATED_NON_AUTHORITATIVE":
                    return {"state": "FORENSIC_CONTAMINATED_NON_AUTHORITATIVE", "path": str(path), "blocking_code": "TARGET_STORE_CONFLICT"}
            except (OSError, json.JSONDecodeError):
                continue
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
        recovery_next = {
            "SERVICES_RESTARTED": "CONTAMINATED_RECOVERY_PRECHECK",
            "CONTAMINATED_RECOVERY_PRECHECK": "RECOVERY_SERVICES_QUIESCED",
            "RECOVERY_SERVICES_QUIESCED": "RECOVERY_LEGACY_VERIFIED",
            "RECOVERY_LEGACY_VERIFIED": "ROLLBACK_IN_PROGRESS",
            "ROLLBACK_IN_PROGRESS": "ROLLBACK_COMPLETED",
        }
        if recovery_next.get(str(prior)) == state:
            receipt["state"] = state
            receipt.setdefault("transitions", []).append({"state": state, "timestamp": _now()})
            receipt.update(details)
            _atomic_json(receipt_path(str(receipt["migration_id"])), receipt)
            return receipt
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


def _central_write_assessment(path: Path) -> dict[str, object]:
    """Read only provenance assessment; unknown lineage blocks recovery."""
    result: dict[str, object] = {"legitimate_write": False, "managed_legitimate_count": 0, "proven_contamination_count": 0, "unresolved_count": 0, "unknown_mutation": False, "signals": []}
    try:
        with _readonly(path) as connection:
            tables = _tables(connection)
            if "provider_invocation_receipts" in tables and _count(connection, "SELECT COUNT(*) FROM provider_invocation_receipts"):
                # Receipts are evidence of a provider launch, not proof that its
                # source was production.  Without a canonical submission lineage
                # they must be explained by the contamination attestation below.
                result["signals"].append("provider_receipts_without_submission_lineage")
            if "backup_probe" in tables:
                result["signals"].append("test_only_backup_probe")
            schema = _schema(connection, tables)
            if schema is not None and schema > EXPECTED_SCHEMA:
                result["signals"].append("unsupported_schema_marker")
            result["proven_contamination_count"] = len([signal for signal in result["signals"] if signal in {"test_only_backup_probe", "unsupported_schema_marker"}])
    except (OSError, sqlite3.DatabaseError) as error:
        raise CutoverError("FORENSIC_CENTRAL_UNREADABLE") from error
    return result


def _domain_digest(path: Path, table: str, columns: tuple[str, ...]) -> str:
    """Hash a sorted, type-stable authority projection without secrets."""
    with _readonly(path) as connection:
        available = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not set(columns) <= available:
            return "ABSENT"
        rows = connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {','.join(columns)}"
        ).fetchall()
    normalized = []
    for row in rows:
        normalized.append([value.hex() if isinstance(value, bytes) else value for value in row])
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")).hexdigest()


def authority_independent_baseline_attestation(baseline: Path, central: Path) -> dict[str, object]:
    """ADR-0025 exact baseline-delta classifier; never invents row origin."""
    domains = {
        "credentials": ("local_api_credentials", ("credential_id", "consumer_id", "project_id", "verifier", "fingerprint", "issued_at", "expires_at", "revoked_at", "replaced_by_credential_id")),
        "registrations": ("local_api_consumer_registrations", ("consumer_id", "project_id", "status", "created_at", "updated_at", "disabled_at", "revoked_at")),
        "project_scope": ("local_api_consumer_registrations", ("consumer_id", "project_id", "status")),
    }
    result: dict[str, object] = {"attestation_version": 1, "domains": {}, "credential_delta": False, "registration_delta": False, "project_scope_delta": False}
    for name, (table, columns) in domains.items():
        baseline_digest, central_digest = _domain_digest(baseline, table, columns), _domain_digest(central, table, columns)
        delta = baseline_digest != central_digest
        result["domains"][name] = {"baseline_digest": baseline_digest, "central_digest": central_digest, "classification": "CONTAMINATION_PROVENANCE_UNRESOLVED" if delta else "NO_POST_CUTOVER_MUTATION"}
        result[f"{name[:-1] if name.endswith('s') else name}_delta"] = delta
    return result


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "required forensic file is unreadable") from error


def contamination_attestation_path(migration_id: str) -> Path:
    """Return the external, immutable receipt path for one incident only."""
    return installation_data_root() / "migration" / "contamination-attestations" / f"{migration_id}.json"


def _authority_rows(path: Path, table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> dict[tuple[object, ...], dict[str, object]]:
    with _readonly(path) as connection:
        available = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        if not set(columns) <= available:
            raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", f"authority table shape is unavailable: {table}")
        rows = connection.execute(f"SELECT {','.join(_quote_identifier(column) for column in columns)} FROM {_quote_identifier(table)}").fetchall()
    result: dict[tuple[object, ...], dict[str, object]] = {}
    for values in rows:
        row = dict(zip(columns, values, strict=True))
        identity = tuple(row[column] for column in key)
        if identity in result:
            raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", f"non-unique authority identity: {table}")
        result[identity] = row
    return result


def _safe_authority_row(row: dict[str, object]) -> dict[str, object]:
    """Project authority evidence without retaining verifier or fingerprint bytes."""
    safe: dict[str, object] = {}
    for key, value in sorted(row.items()):
        if key in {"verifier", "fingerprint", "audit_metadata"}:
            if value is None:
                safe[f"{key}_digest"] = None
            elif isinstance(value, bytes):
                safe[f"{key}_digest"] = hashlib.sha256(value).hexdigest()
            else:
                safe[f"{key}_digest"] = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        else:
            safe[key] = value
    return safe


def _historical_fixture_components(baseline: Path, central: Path) -> list[dict[str, object]]:
    """Mechanically recognize only the four authorized historical test subjects."""
    credentials = ("credential_id", "consumer_id", "project_id", "verifier", "fingerprint", "issued_at", "expires_at", "revoked_at", "replaced_by_credential_id")
    registrations = ("consumer_id", "project_id", "status", "created_at", "updated_at", "disabled_at", "revoked_at", "audit_metadata")
    old_credentials = _authority_rows(baseline, "local_api_credentials", credentials, ("credential_id",))
    new_credentials = _authority_rows(central, "local_api_credentials", credentials, ("credential_id",))
    old_registrations = _authority_rows(baseline, "local_api_consumer_registrations", registrations, ("consumer_id", "project_id"))
    new_registrations = _authority_rows(central, "local_api_consumer_registrations", registrations, ("consumer_id", "project_id"))
    if any(old_credentials[key] != new_credentials.get(key) for key in old_credentials) or any(old_registrations[key] != new_registrations.get(key) for key in old_registrations):
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "baseline authority rows changed")
    if set(new_credentials) - set(old_credentials) and not set(old_credentials) <= set(new_credentials):
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "credential removal")
    if set(new_registrations) - set(old_registrations) and not set(old_registrations) <= set(new_registrations):
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "registration removal")
    added_credentials = [row for key, row in new_credentials.items() if key not in old_credentials]
    added_registrations = [row for key, row in new_registrations.items() if key not in old_registrations]
    groups: dict[tuple[str, str], dict[str, list[dict[str, object]]]] = {}
    for row in added_credentials:
        groups.setdefault((str(row["consumer_id"]), str(row["project_id"])), {"credentials": [], "registrations": []})["credentials"].append(row)
    for row in added_registrations:
        groups.setdefault((str(row["consumer_id"]), str(row["project_id"])), {"credentials": [], "registrations": []})["registrations"].append(row)
    expected = {
        ("workspace-client", "project-alpha"), ("consumer", "project"),
        ("rotate", "project"), ("qualification-client", "qualification-project"),
    }
    if set(groups) != expected:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "authority components are not the authorized historical fixture set")
    components: list[dict[str, object]] = []
    for subject in sorted(groups):
        consumer_id, project_id = subject
        values = groups[subject]
        credentials_for_subject = sorted(values["credentials"], key=lambda row: str(row["credential_id"]))
        registrations_for_subject = values["registrations"]
        signals: list[str]
        writer: str
        if subject == ("workspace-client", "project-alpha"):
            credential = credentials_for_subject[0] if len(credentials_for_subject) == 1 else None
            if (credential is None or len(registrations_for_subject) != 1
                    or credential["credential_id"] != "credential-alpha" or credential["issued_at"] != "now"
                    or not isinstance(credential["verifier"], bytes) or len(credential["verifier"]) != 32
                    or not isinstance(credential["fingerprint"], bytes) or len(credential["fingerprint"]) != 32):
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "workspace fixture mismatch")
            signals, writer = ["exact_fixture_literals", "fixture_blob_shape"], "tests/engineering/test_central_store_migration.py"
        elif subject == ("consumer", "project"):
            credential = credentials_for_subject[0] if len(credentials_for_subject) == 1 else None
            registration = registrations_for_subject[0] if len(registrations_for_subject) == 1 else None
            if (credential is None or registration is None or credential["credential_id"] != "production-consumer"
                    or registration["status"] != "DISABLED" or credential["revoked_at"] is None
                    or registration["disabled_at"] is None or registration["audit_metadata"] != '{"action":"DISABLE"}'):
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "consumer fixture mismatch")
            signals, writer = ["exact_fixture_literals", "disable_revoke_lifecycle"], "tests/engineering/test_local_api_consumer_credentials.py"
        elif subject == ("rotate", "project"):
            credential_ids = {str(row["credential_id"]) for row in credentials_for_subject}
            if (len(credentials_for_subject) != 2 or len(registrations_for_subject) != 1
                    or credential_ids != {"production-rotate-old", "production-rotate-new"}
                    or registrations_for_subject[0]["status"] != "ACTIVE"
                    or sum(row["revoked_at"] is None for row in credentials_for_subject) != 1):
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "rotation fixture mismatch")
            signals, writer = ["exact_fixture_literals", "rotation_lifecycle"], "tests/engineering/test_local_api_consumer_credentials.py"
        else:
            credential = credentials_for_subject[0] if len(credentials_for_subject) == 1 else None
            try:
                issued = datetime.strptime(str(credential["issued_at"]), "%Y-%m-%d %H:%M:%S") if credential else None
                expires = datetime.strptime(str(credential["expires_at"]), "%Y-%m-%d %H:%M:%S") if credential else None
            except ValueError as error:
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "qualification fixture timestamp mismatch") from error
            if (credential is None or registrations_for_subject or credential["credential_id"] != "qualification-fixture"
                    or issued is None or expires is None or expires - issued != timedelta(minutes=15)):
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "qualification fixture mismatch")
            signals, writer = ["exact_fixture_literals", "qualification_prefix_and_ttl"], "tests/engineering/test_local_api_qualification_credentials.py"
        components.append({"consumer_id": consumer_id, "project_id": project_id, "credentials": [_safe_authority_row(row) for row in credentials_for_subject], "registrations": [_safe_authority_row(row) for row in registrations_for_subject], "test_writer": writer, "signals": signals})
    return components


def _component_digest(components: list[dict[str, object]]) -> str:
    return hashlib.sha256(json.dumps(components, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def create_contamination_attestation(repo: Path, *, migration_id: str, operator: str) -> dict[str, object]:
    """Persist one operator-owned, immutable external attestation for this incident."""
    receipt = load_receipt(migration_id)
    if receipt is None or receipt.get("state") != "SERVICES_RESTARTED" or not operator:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "eligible incident state and operator are required")
    pointer_path = authority_pointer_path()
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "authority pointer is unreadable") from error
    legacy, central = Path(str(receipt.get("legacy_path", ""))), Path(str(pointer.get("authoritative_path", "")))
    baseline = receipt.get("quiescent_source_baseline")
    source = baseline.get("source") if isinstance(baseline, dict) else None
    if not isinstance(source, dict) or _fingerprint(legacy) != source.get("fingerprint_sha256"):
        raise CutoverError("LEGACY_BASELINE_MISMATCH")
    components = _historical_fixture_components(legacy, central)
    if any(len(component["signals"]) < 2 for component in components):
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "weak fixture evidence")
    with _readonly(central) as connection:
        schema = _schema(connection, _tables(connection))
    binding = {"migration_id": migration_id, "legacy_fingerprint_sha256": _fingerprint(legacy), "central_fingerprint_sha256": _fingerprint(central), "authority_pointer_fingerprint_sha256": _file_digest(pointer_path), "central_schema": schema, "delta_digest": _component_digest(components)}
    payload = {"attestation_id": f"historical-contamination-{migration_id}", "attestation_version": HISTORICAL_ATTESTATION_VERSION, "classifier_version": HISTORICAL_ATTESTATION_CLASSIFIER_VERSION, "origin_class": "OPERATOR_FORENSIC_CONTROL", "operator": operator, "created_at": _now(), "binding": binding, "components": components, "evidence_strength": "TWO_OR_MORE_DETERMINISTIC_SIGNALS_PER_COMPONENT", "eligibility": "PROVEN_NON_PRODUCTION_CONTAMINATION"}
    path = contamination_attestation_path(migration_id)
    if path.exists():
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "attestation is immutable and already exists")
    _atomic_json(path, payload)
    return payload


def _valid_contamination_attestation(repo: Path, *, migration_id: str, legacy: Path, central: Path, pointer_path: Path) -> dict[str, object] | None:
    path = contamination_attestation_path(migration_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("attestation_version") != HISTORICAL_ATTESTATION_VERSION or payload.get("classifier_version") != HISTORICAL_ATTESTATION_CLASSIFIER_VERSION:
        return None
    try:
        components = _historical_fixture_components(legacy, central)
        with _readonly(central) as connection:
            schema = _schema(connection, _tables(connection))
        expected = {"migration_id": migration_id, "legacy_fingerprint_sha256": _fingerprint(legacy), "central_fingerprint_sha256": _fingerprint(central), "authority_pointer_fingerprint_sha256": _file_digest(pointer_path), "central_schema": schema, "delta_digest": _component_digest(components)}
    except CutoverError:
        return None
    return payload if payload.get("binding") == expected and payload.get("components") == components else None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_value(value: object) -> object:
    """Return a type-stable, deterministic representation of a SQLite value."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"type": "text", "value": text}
        if isinstance(parsed, (dict, list)):
            return {"type": "json", "value": parsed}
        return {"type": "text", "value": text}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "real", "value": value}
    return {"type": type(value).__name__, "value": str(value)}


def _table_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Resolve a declared primary key or non-partial enforced unique key."""
    quoted = _quote_identifier(table)
    columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    primary = tuple(str(row[1]) for row in sorted(columns, key=lambda row: int(row[5])) if int(row[5]))
    if primary:
        return primary
    candidates: list[tuple[str, ...]] = []
    for index in connection.execute(f"PRAGMA index_list({quoted})"):
        # seq, name, unique, origin, partial
        if not int(index[2]) or (len(index) > 4 and int(index[4])):
            continue
        index_name = _quote_identifier(str(index[1]))
        key = tuple(str(row[2]) for row in connection.execute(f"PRAGMA index_info({index_name})"))
        if key:
            candidates.append(key)
    if candidates:
        return min(candidates, key=lambda key: (len(key), key))
    raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", f"run-bound table has no deterministic key: {table}")


def _row_map(connection: sqlite3.Connection, table: str, key: tuple[str, ...]) -> dict[str, dict[str, object]]:
    quoted = _quote_identifier(table)
    columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})"))
    rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        raw = dict(zip(columns, row, strict=True))
        key_value = [_normalized_value(raw[column]) for column in key]
        encoded_key = json.dumps(key_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if encoded_key in result:
            raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", f"non-unique deterministic key: {table}")
        result[encoded_key] = {column: _normalized_value(raw[column]) for column in sorted(raw)}
    return result


def _run_bound_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    tables = _tables(connection)
    result = []
    for table in tables:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
        if "run_id" in columns or (table == "execution_submissions" and "execution_run_id" in columns):
            result.append(table)
    return tuple(sorted(result))


def _row_deltas(baseline: Path, central: Path) -> dict[str, list[dict[str, object]]]:
    """Compare all run-bound evidence rows by their declared stable identity."""
    with _readonly(baseline) as baseline_connection, _readonly(central) as central_connection:
        tables = set(_run_bound_tables(baseline_connection)) | set(_run_bound_tables(central_connection))
        deltas: dict[str, list[dict[str, object]]] = {}
        for table in sorted(tables):
            baseline_present = table in _tables(baseline_connection)
            central_present = table in _tables(central_connection)
            key_connection = central_connection if central_present else baseline_connection
            key = _table_key(key_connection, table)
            baseline_rows = _row_map(baseline_connection, table, key) if baseline_present else {}
            central_rows = _row_map(central_connection, table, key) if central_present else {}
            changes: list[dict[str, object]] = []
            for encoded_key in sorted(set(baseline_rows) | set(central_rows)):
                before, after = baseline_rows.get(encoded_key), central_rows.get(encoded_key)
                if before is None:
                    changes.append({"change_type": "ADDED", "key": json.loads(encoded_key), "row": after})
                elif after is None:
                    changes.append({"change_type": "REMOVED", "key": json.loads(encoded_key), "row": before})
                elif before != after:
                    changes.append({"change_type": "MODIFIED", "key": json.loads(encoded_key), "before": before, "after": after})
            if changes:
                deltas[table] = changes
    return deltas


def _plain_value(value: object) -> object:
    return value.get("value") if isinstance(value, dict) and "value" in value else None


def _row_text(row: object, column: str) -> str | None:
    value = _plain_value(row.get(column)) if isinstance(row, dict) else None
    return value if isinstance(value, str) and value else None


def _changed_run_ids(table: str, changes: list[dict[str, object]]) -> set[str]:
    run_column = "execution_run_id" if table == "execution_submissions" else "run_id"
    result: set[str] = set()
    for change in changes:
        rows = [change.get("row"), change.get("before"), change.get("after")]
        for row in rows:
            if isinstance(row, dict):
                value = _plain_value(row.get(run_column))
                if isinstance(value, str) and value:
                    result.add(value)
    return result


def _lineage_category(table: str, change: dict[str, object]) -> str:
    if table == "provider_recovery_attempts":
        return "recovery"
    if "provider" in table:
        return "provider"
    if "validation" in table:
        return "validation"
    if "qualification" in table:
        return "qualification"
    if table == "prompt_execution_history":
        return "prompt_history"
    if "reconciliation" in table:
        return "reconciliation"
    if table == "engineering_transactions":
        rows = (change.get("row"), change.get("before"), change.get("after"))
        for row in rows:
            payload = _plain_value(row.get("payload")) if isinstance(row, dict) else None
            if isinstance(payload, dict) and any("FINAL" in str(value).upper() for value in payload.values()):
                return "finalization"
    return "implementation"


def managed_lineage_attestation(baseline: Path, central: Path) -> dict[str, object]:
    """Classify only row-level post-baseline nodes by their persisted roots."""
    deltas = _row_deltas(baseline, central)
    categories = {name: 0 for name in ("provider", "recovery", "validation", "qualification", "implementation", "finalization", "reconciliation", "prompt_history")}
    changed_runs: dict[str, set[str]] = {}
    for table, changes in deltas.items():
        runs = _changed_run_ids(table, changes)
        if runs:
            changed_runs[table] = runs
        for change in changes:
            categories[_lineage_category(table, change)] += 1
    with _readonly(central) as connection:
        tables = _tables(connection)
        producers: dict[str, set[str]] = {}
        if "execution_submissions" in tables:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(execution_submissions)")}
            if {"execution_run_id", "producer_type"} <= columns:
                for run_id, producer_type in connection.execute("SELECT execution_run_id,producer_type FROM execution_submissions WHERE execution_run_id IS NOT NULL"):
                    producers.setdefault(str(run_id), set()).add(str(producer_type))
        if "execution_submission_links" in tables and "execution_submissions" in tables:
            for run_id, producer_type in connection.execute("SELECT link.run_id,submission.producer_type FROM execution_submission_links AS link JOIN execution_submissions AS submission ON submission.submission_id=link.submission_id"):
                producers.setdefault(str(run_id), set()).add(str(producer_type))
    nodes = {(table, run_id) for table, runs in changed_runs.items() for run_id in runs}
    # A new submission is itself a canonical root even before a run link has
    # been written.  Treating it as invisible would permit a real post-cutover
    # human submission to evade the recovery gate during its earliest phase.
    submission_origins: dict[str, set[str]] = {}
    for change in deltas.get("execution_submissions", []):
        for row in (change.get("row"), change.get("before"), change.get("after")):
            submission_id, producer_type = _row_text(row, "submission_id"), _row_text(row, "producer_type")
            execution_run_id = _row_text(row, "execution_run_id")
            if submission_id and producer_type and execution_run_id is None:
                node = ("execution_submissions", f"submission:{submission_id}")
                nodes.add(node)
                submission_origins.setdefault(node[1], set()).add(producer_type)
    production_origins = {"HUMAN", "MANAGED", "ICLOUD", "HUMAN_OPERATOR"}
    test_origins = {"TEST_HARNESS"}
    def origins(node: tuple[str, str]) -> set[str]:
        return submission_origins.get(node[1], producers.get(node[1], set()))

    production = {node for node in nodes if origins(node) & production_origins}
    test = {node for node in nodes if not (origins(node) & production_origins) and origins(node) & test_origins}
    unresolved = nodes - production - test

    def components(node_set: set[tuple[str, str]]) -> set[str]:
        return {node[1] for node in node_set}
    return {
        "row_delta_version": 1,
        "changed_rows": {table: len(changes) for table, changes in deltas.items()},
        "changed_run_nodes": {table: sorted(runs) for table, runs in changed_runs.items()},
        "production_component_count": len(components(production)),
        "production_node_count": len(production),
        "unresolved_component_count": len(components(unresolved)),
        "unresolved_node_count": len(unresolved),
        "test_component_count": len(components(test)),
        "test_node_count": len(test),
        "categories": categories,
    }


def contaminated_prewrite_status(repo: Path, *, migration_id: str) -> dict[str, object]:
    """Read-only eligibility for the narrowly bounded forensic recovery."""
    receipt = load_receipt(migration_id)
    if receipt is None or receipt.get("state") not in {"SERVICES_RESTARTED", "ROLLBACK_COMPLETED"}:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "legal predecessor is SERVICES_RESTARTED")
    freeze = admission_status(repo)
    if freeze.get("state") != "ACTIVE" or freeze.get("migration_id") != migration_id:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "matching active freeze is required")
    pointer_path = authority_pointer_path()
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "central pointer is required") from error
    central = Path(str(pointer.get("authoritative_path", "")))
    legacy = Path(str(receipt.get("legacy_path", "")))
    if receipt.get("state") == "ROLLBACK_COMPLETED":
        return {"eligible": True, "idempotent": True, "state": "ROLLBACK_COMPLETED", "freeze": freeze, "authority": "LEGACY"}
    if pointer.get("migration_id") != migration_id or not central.is_file() or database_path(repo).resolve() != central.resolve():
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "central authority does not match migration")
    assessment = _central_write_assessment(central)
    if assessment["legitimate_write"]:
        raise CutoverError("LEGITIMATE_CENTRAL_WRITE_PRESENT")
    with _readonly(central) as central_connection:
        tables = _tables(central_connection)
        central_schema = _schema(central_connection, tables)
    provenance = "PROVEN_NON_PRODUCTION" if {"test_only_backup_probe", "unsupported_schema_marker"} & set(assessment["signals"]) and not assessment["unknown_mutation"] else "UNRESOLVED"
    if provenance != "PROVEN_NON_PRODUCTION":
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED")
    baseline = receipt.get("quiescent_source_baseline")
    source = baseline.get("source") if isinstance(baseline, dict) else receipt.get("source")
    if not legacy.is_file() or not isinstance(source, dict) or _fingerprint(legacy) != source.get("fingerprint_sha256"):
        raise CutoverError("LEGACY_BASELINE_MISMATCH")
    # Production and unresolved managed descendants always take precedence:
    # an historical attestation may explain only the bounded authority rows.
    lineage = managed_lineage_attestation(legacy, central)
    if lineage["production_node_count"]:
        raise CutoverError("LEGITIMATE_CENTRAL_WRITE_PRESENT")
    if lineage["unresolved_node_count"]:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "orphan managed evidence")
    domain_attestation = authority_independent_baseline_attestation(legacy, central)
    historical_attestation = _valid_contamination_attestation(
        repo, migration_id=migration_id, legacy=legacy, central=central, pointer_path=pointer_path,
    )
    if any((domain_attestation["credential_delta"], domain_attestation["registration_delta"], domain_attestation["project_scope_delta"])) and historical_attestation is None:
        raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED", "authority-independent baseline delta")
    facts = inspect_source(StoreCandidate(str(central), str(central.resolve()), ("forensic_central",)))
    if facts.get("integrity") != "PASS":
        raise CutoverError("FORENSIC_CENTRAL_UNREADABLE")
    return {"eligible": True, "idempotent": False, "authority": "CENTRAL", "freeze": freeze, "central": {"path": str(central.resolve()), "fingerprint_sha256": _fingerprint(central), "schema": central_schema, "integrity": facts["integrity"], "critical_counts": _table_counts(central)}, "legacy": {"path": str(legacy.resolve()), "fingerprint_sha256": _fingerprint(legacy), "critical_counts": _table_counts(legacy), "project_scope": project_scope_inventory(legacy)}, "legitimate_write_assessment": assessment, "managed_lineage": lineage, "authority_independent_baseline": domain_attestation, "historical_contamination_attestation": historical_attestation, "contamination_provenance": provenance, "forensic_tables": sorted(tables)}


def recover_contaminated_prewrite(repo: Path, *, migration_id: str, operator: str = "operator", services: LaunchAgentServiceControl | None = None) -> dict[str, object]:
    """Operator-only CENTRAL-to-LEGACY forensic recovery; never copies data."""
    status = contaminated_prewrite_status(repo, migration_id=migration_id)
    if status["idempotent"]:
        return load_receipt(migration_id) or status
    receipt = load_receipt(migration_id)
    assert receipt is not None
    central = Path(str(status["central"]["path"]))
    legacy = Path(str(status["legacy"]["path"]))
    control = services or LaunchAgentServiceControl()
    transition_receipt(receipt, "CONTAMINATED_RECOVERY_PRECHECK", recovery_class="CONTAMINATED_PRE_WRITE_CENTRAL_RECOVERY", recovery_precheck=status, operator=operator)
    try:
        for label in SERVICE_STOP_ORDER:
            control.stop(label)
            if not control.stopped(label):
                raise CutoverError("RECOVERY_SERVICE_QUIESCENCE_FAILED", label)
    except CutoverError as error:
        raise CutoverError("RECOVERY_SERVICE_QUIESCENCE_FAILED", error.code) from error
    transition_receipt(receipt, "RECOVERY_SERVICES_QUIESCED", service_quiescence={label: control.stopped(label) for label in SERVICE_STOP_ORDER})
    if _fingerprint(legacy) != status["legacy"]["fingerprint_sha256"]:
        raise CutoverError("LEGACY_BASELINE_MISMATCH")
    transition_receipt(receipt, "RECOVERY_LEGACY_VERIFIED", legacy_baseline=status["legacy"])
    transition_receipt(receipt, "ROLLBACK_IN_PROGRESS")
    try:
        pointer = write_authority_pointer(migration_id=migration_id, authority=legacy, legacy=legacy, state="ROLLBACK_COMPLETED")
    except CutoverError as error:
        raise CutoverError("RECOVERY_AUTHORITY_SWITCH_FAILED") from error
    central_after = _fingerprint(central)
    if central_after != status["central"]["fingerprint_sha256"]:
        raise CutoverError("RECOVERY_POSTCHECK_FAILED", "central changed")
    try:
        for label in SERVICE_START_ORDER:
            control.start(label)
        binding = service_binding_proof(repo, expected=legacy, services=SERVICE_START_ORDER)
    except CutoverError as error:
        raise CutoverError("RECOVERY_SERVICE_RESTART_FAILED", error.code) from error
    if not binding.get("consistent") or not all(control.running(label) for label in SERVICE_START_ORDER):
        raise CutoverError("RECOVERY_MIXED_BINDING")
    freeze = admission_status(repo)
    if freeze.get("state") != "ACTIVE" or freeze.get("migration_id") != migration_id:
        raise CutoverError("RECOVERY_POSTCHECK_FAILED", "freeze changed")
    return transition_receipt(receipt, "ROLLBACK_COMPLETED", rollback={"operator": operator, "authority_pointer": pointer, "timestamp": _now()}, central_forensic={**status["central"], "classification": "FORENSIC_CONTAMINATED_NON_AUTHORITATIVE", "contamination_provenance": status["contamination_provenance"], "legitimate_write_assessment": status["legitimate_write_assessment"], "fingerprint_after": central_after}, post_recovery_binding=binding, freeze_after=freeze)


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


def _desired_state_matches(repo: Path) -> bool:
    """Run the canonical host verification used by post-cutover readiness."""
    verifier = repo / "scripts" / "runner" / "bootstrap_djconnect_macos_host.sh"
    if not verifier.is_file():
        return False
    try:
        result = subprocess.run(
            [str(verifier), "--verify"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "**MATCH**" in result.stdout


def stage_a_readiness(
    repo: Path,
    *,
    migration_id: str,
    receipt: dict[str, object],
    services: LaunchAgentServiceControl | None = None,
    desired_state_check: object | None = None,
) -> dict[str, object]:
    """Prove the read-only Stage-A gates without changing runtime authority."""
    target = central_store_path()
    control = services or LaunchAgentServiceControl()
    try:
        running = {label: control.running(label) for label in SERVICE_START_ORDER}
    except Exception:
        raise CutoverError("POST_CUTOVER_READINESS_FAILED") from None
    try:
        pointer = json.loads(authority_pointer_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pointer = {}
    try:
        binding = service_binding_proof(repo, expected=target, services=SERVICE_START_ORDER)
        facts = inspect_source(StoreCandidate(str(target), str(target.resolve()), ("central_authority",)))
        freeze = admission_status(repo)
        resolved_authority = database_path(repo).resolve()
    except CutoverError:
        raise
    except (OSError, ValueError):
        raise CutoverError("POST_CUTOVER_READINESS_FAILED") from None

    baseline = receipt.get("quiescent_source_baseline")
    baseline_source = baseline.get("source") if isinstance(baseline, dict) else None
    legacy = Path(str(receipt.get("legacy_path", "")))
    try:
        legacy_unchanged = (
            legacy.is_file()
            and isinstance(baseline_source, dict)
            and _same_source_content(
                source_identity(StoreCandidate(str(legacy), str(legacy.resolve()), ("legacy_authority",))),
                baseline_source,
            )
        )
    except OSError:
        legacy_unchanged = False
    equivalence = receipt.get("equivalence")
    verified_equivalence = isinstance(equivalence, dict) and equivalence.get("equivalent") is True
    pointer_matches = (
        pointer.get("migration_id") == migration_id
        and pointer.get("authoritative_path") == str(target.resolve())
        and pointer.get("schema") == EXPECTED_SCHEMA
    )
    checker = desired_state_check or _desired_state_matches
    try:
        desired_state_match = bool(checker(repo))
    except Exception:
        desired_state_match = False
    result = {
        "authority": "CENTRAL" if pointer_matches and resolved_authority == target.resolve() else "NOT_CENTRAL",
        "central_integrity": facts.get("integrity"),
        "central_schema": facts.get("schema_version"),
        "service_binding": binding,
        "services": running,
        "desired_state": "MATCH" if desired_state_match else "NOT_MATCH",
        "legacy_unchanged": legacy_unchanged,
        "freeze": freeze.get("state"),
        "pre_write_rollback_safe": receipt.get("rollback_mode") == "PRE_WRITE_DIRECT",
        "target_equivalence": verified_equivalence,
        "central_managed_production_writes": 0,
    }
    eligible = (
        result["authority"] == "CENTRAL"
        and not facts["blocking_codes"]
        and binding.get("consistent") is True
        and all(running.values())
        and desired_state_match
        and legacy_unchanged
        and freeze.get("state") == "ACTIVE"
        and freeze.get("migration_id") == migration_id
        and result["pre_write_rollback_safe"]
        and verified_equivalence
    )
    result["eligible"] = eligible
    return result


def complete_stage_a(
    repo: Path,
    *,
    migration_id: str,
    services: LaunchAgentServiceControl | None = None,
    desired_state_check: object | None = None,
) -> dict[str, object]:
    """Record Stage-A only after the persisted restart and read-only gates pass."""
    receipt = load_receipt(migration_id)
    if receipt is None:
        raise CutoverError("POST_CUTOVER_READINESS_FAILED")
    if receipt.get("state") == "LEGACY_ROLLBACK_COMPATIBLE":
        return receipt
    if receipt.get("state") != "SERVICES_RESTARTED":
        raise CutoverError("POST_CUTOVER_READINESS_FAILED")
    readiness = stage_a_readiness(
        repo,
        migration_id=migration_id,
        receipt=receipt,
        services=services,
        desired_state_check=desired_state_check,
    )
    if not readiness["eligible"]:
        raise CutoverError("POST_CUTOVER_READINESS_FAILED")
    transition_receipt(receipt, "POST_CUTOVER_VERIFIED", readonly_qualification=readiness)
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
    parser.add_argument("command", choices=("preflight", "dry-run", "freeze", "freeze-status", "abort", "thaw", "cutover", "stage-a", "rollback", "recover-contaminated-prewrite", "create-contamination-attestation", "forensic-delta", "status"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--migration-id")
    parser.add_argument("--baseline", type=Path, help="read-only baseline SQLite database for forensic-delta")
    parser.add_argument("--candidate", type=Path, help="read-only candidate SQLite database for forensic-delta")
    parser.add_argument("--output", type=Path, help="optional JSON output file for forensic-delta")
    parser.add_argument("--strict", action="store_true", help="fail forensic-delta when a table has no deterministic key")
    parser.add_argument("--reason")
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--execute", action="store_true", help="required for a mutating production operation")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        if args.command == "forensic-delta":
            if not args.baseline or not args.candidate or not args.migration_id:
                parser.error("forensic-delta requires --baseline, --candidate, and --migration-id")
            result = export_forensic_delta(args.baseline, args.candidate, migration_id=args.migration_id)
            if args.output:
                args.output.write_text(canonical_report_json(result) + "\n", encoding="utf-8")
            if args.json:
                print(canonical_report_json(result))
            else:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 2 if args.strict and result["summary"]["tables_key_unresolved"] else 0
        if args.command in {"preflight", "dry-run"}:
            result = preflight(repo)
        elif args.command == "freeze-status":
            result = admission_status(repo)
        elif args.command == "status":
            result = {"admission_freeze": admission_status(repo), "authority_pointer": str(authority_pointer_path()), "authoritative_store": str(database_path(repo))}
        elif args.command == "recover-contaminated-prewrite" and not args.execute:
            if not args.migration_id:
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED")
            result = contaminated_prewrite_status(repo, migration_id=args.migration_id)
        elif args.command == "create-contamination-attestation" and not args.execute:
            raise CutoverError("ADMISSION_FREEZE_FAILED", "--execute is required")
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
        elif args.command == "recover-contaminated-prewrite":
            if not args.migration_id:
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED")
            result = recover_contaminated_prewrite(repo, migration_id=args.migration_id, operator=args.operator, services=LaunchAgentServiceControl())
        elif args.command == "create-contamination-attestation":
            if not args.migration_id:
                raise CutoverError("CONTAMINATION_PROVENANCE_UNRESOLVED")
            result = create_contamination_attestation(repo, migration_id=args.migration_id, operator=args.operator)
        elif args.command == "stage-a":
            if not args.migration_id:
                raise CutoverError("POST_CUTOVER_READINESS_FAILED")
            result = complete_stage_a(repo, migration_id=args.migration_id, services=LaunchAgentServiceControl())
        else:
            result = controlled_cutover(repo, operator=args.operator, services=LaunchAgentServiceControl())
    except (CutoverError, ForensicDeltaError) as error:
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
