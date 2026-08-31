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
import sys
import uuid

from .storage import DATABASE_FILENAME, ENGINEERING_STORAGE_SCHEMA_VERSION, database_path


TOOL_VERSION = "2.0.0-phase2-increment2"
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
    "PROJECT_SCOPE_UNRESOLVED", "AUTHORITY_HANDOFF_NOT_SAFE",
})


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
        candidate = database_path(root).resolve()
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


def inspect_quiescence(path: Path) -> dict[str, object]:
    """Report future handoff blockers without stopping a process or taking ownership."""
    facts: dict[str, object] = {"non_terminal_transactions": 0, "active_leases": 0, "active_recovery": 0, "unsafe_locks": [], "watcher_admission": "FREEZE_NOT_ACTIVE", "blocking_codes": []}
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
            try:
                with lock.open("r", encoding="utf-8") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        facts["unsafe_locks"].append(lock.name)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
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
    migration_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ep-central-store:{identity.fingerprint_sha256}"))
    backup = root / "backups" / f"legacy-schema40-{identity.fingerprint_sha256[:16]}-{migration_id}.db"
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    writable = os.access(ancestor, os.W_OK | os.X_OK)
    available = shutil.disk_usage(ancestor).free if ancestor.exists() else 0
    ready = writable and available >= identity.size_bytes
    return {"backup_path": str(backup), "migration_id": migration_id, "root_exists": root.exists(), "writable_ancestor": str(ancestor), "available_bytes": available, "required_bytes": identity.size_bytes, "integrity_method": "PRAGMA integrity_check", "ready": ready, "blocking_code": None if ready else "BACKUP_NOT_READY"}


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


def preflight(repo: Path, *, extra_runtime_roots: tuple[Path, ...] = ()) -> dict[str, object]:
    """Compute the complete migration plan strictly read-only."""
    candidates = discover_legacy_stores(repo, extra_runtime_roots=extra_runtime_roots)
    receipt: dict[str, object] = {"receipt_version": 1, "mode": "DRY_RUN", "timestamp": datetime.now(timezone.utc).isoformat(), "tool_version": TOOL_VERSION, "target_data_root": str(installation_data_root()), "target_store": classify_target(central_store_path()), "source_candidates": [asdict(item) for item in candidates], "blocking_codes": [], "service_stop_plan": ["inbox_watcher", "separately_managed_execution_service", "local_consumer_api", "dashboard_relay", "dashboard"], "admission_freeze": {"state": "FREEZE_NOT_ACTIVE", "required": "explicit operator-recorded freeze before Increment 3"}}
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
    receipt.update({"migration_id": backup["migration_id"], "source": source, "quiescence": quiescence, "backup_readiness": backup, "snapshot_strategy": snapshot_plan(source), "project_scope": inventory, "critical_table_counts": _table_counts(Path(candidate.resolved_path))})
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
    parser.add_argument("command", choices=("preflight", "dry-run"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = preflight(args.repo.resolve())
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(f"{result['mode']} eligible={result['eligible']} source_candidates={len(result['source_candidates'])}")
        print("blocking_codes=" + ",".join(result["blocking_codes"]))
        print("target=" + str(result["target_store"]["path"]))
    return 0 if result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
