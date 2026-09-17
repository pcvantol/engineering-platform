"""Product-owned, fail-closed operational reset for installed EP CENTRAL.

The service is deliberately local and installation-owner only.  It has no
remote route, never discovers a target, and treats every unclassified table or
external path as a blocker.  Preview is read-only; all destructive work is
bound to a prepared operation, a verified SQLite backup, and durable
maintenance state.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import tempfile
from typing import Iterator

from . import central_database
from .platform_admin import require_installation_owner
from .operational_installation_lock import (
    OperationalInstallationLock, OperationalInstallationLockError,
)
from .storage import sqlite_connection


PROFILE = "EP_CENTRAL_OPERATIONAL_HISTORY_V1"
PLAN_VERSION = 1
SCHEMA_VERSION = 68
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_ACTIVE_STATES = frozenset({
    "PREPARING", "AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED",
    "DB_APPLIED", "VERIFIED", "FAILED",
})
_RESET_TABLES = frozenset({
    "ep_operational_reset_operations", "ep_operational_dataset_state",
    "ep_operational_identity_tombstones",
})

# These sets are the schema-owned contract.  Prefix matching and implicit
# "everything but configuration" are intentionally forbidden.
INSTALLATION_AND_CONFIGURATION = frozenset({
    "engineering_schema_migrations", "engineering_metadata", "ep_installations",
    "ep_project_registrations", "ep_repository_registrations",
    "ep_agent_registrations", "ep_agent_repository_attachments",
    "ep_local_repository_bindings", "ep_external_producer_bindings",
    "execution_migration_provenance",
})
SECURITY_AND_AUTHORITY_LEDGER = frozenset({
    "ep_agent_pairing_codes", "ep_consumer_credential_recovery_operations",
    "ep_consumer_credentials", "ep_consumer_registrations",
    "ep_control_provenance", "ep_external_producer_binding_audit",
    "ep_operator_capabilities",
})
DERIVED_CACHE_OR_PROJECTION = frozenset({
    "daily_execution_statistics", "engineering_component_logs", "engineering_status",
    "execution_activity_summaries", "execution_projections", "execution_runs",
    "provider_usage_snapshots",
})
OPERATIONAL_HISTORY = frozenset({
    "dependabot_admission_events", "engineering_artifacts", "engineering_transactions",
    "ep_execution_host_evidence", "ep_execution_leases", "ep_execution_runs",
    "ep_forge_action_context_envelopes", "ep_forge_exchange_audit",
    "ep_forge_planning_context_envelopes", "ep_parity_lifecycle_dispatches",
    "ep_queue_disposition_operations", "ep_receipt_run_provenance",
    "ep_submission_events", "ep_submission_prompt_history", "ep_submissions",
    "ep_technical_diagnostics", "ep_terminal_evidence_reconciliation_operations",
    "execution_admission_decisions", "execution_artifact_records",
    "execution_chat_messages", "execution_dismissals", "execution_emergency_recoveries",
    "execution_lease_events", "execution_lifecycle_events", "execution_phase_spans",
    "execution_pr_evidence_backfills", "execution_readiness_evaluations",
    "execution_receipts", "execution_run_leases", "execution_run_qualification_context",
    "execution_run_qualification_snapshots", "execution_run_reconciliations",
    "execution_submission_attempt_links", "execution_submission_attempts",
    "execution_submission_links", "execution_submissions",
    "execution_validation_command_invocations", "execution_validation_command_terminals",
    "execution_validation_control_results", "execution_validation_profile_identities",
    "execution_validation_profiles", "managed_autonomy_actions",
    "managed_governance_gates", "managed_pr_check_observations",
    "managed_validation_observations", "prompt_execution_history",
    "provider_invocation_receipts", "provider_invocations",
    "provider_recovery_attempts", "terminal_telemetry_outbox",
})
MAINTENANCE_AUDIT = _RESET_TABLES
MAPPED_TABLES = (
    INSTALLATION_AND_CONFIGURATION | SECURITY_AND_AUTHORITY_LEDGER |
    DERIVED_CACHE_OR_PROJECTION | OPERATIONAL_HISTORY | MAINTENANCE_AUDIT
)

# Child-to-parent order for all schema-68 operational records.  Deleting a
# table as one statement also handles its self-references.  Foreign keys stay
# enabled throughout.
PURGE_ORDER = (
    "ep_terminal_evidence_reconciliation_operations",
    "ep_forge_action_context_envelopes", "ep_forge_planning_context_envelopes",
    "ep_forge_exchange_audit", "ep_queue_disposition_operations",
    "ep_receipt_run_provenance", "ep_parity_lifecycle_dispatches",
    "ep_submission_events", "ep_submission_prompt_history",
    "execution_chat_messages", "execution_dismissals", "execution_emergency_recoveries",
    "execution_validation_command_terminals", "execution_validation_command_invocations",
    "execution_admission_decisions", "execution_submission_attempt_links",
    "execution_submission_attempts", "execution_submission_links",
    "execution_artifact_records", "provider_usage_snapshots",
    "provider_invocation_receipts", "provider_recovery_attempts",
    "execution_lease_events", "execution_run_reconciliations",
    "execution_readiness_evaluations", "execution_run_leases",
    "execution_lifecycle_events", "execution_pr_evidence_backfills",
    "ep_execution_host_evidence", "ep_execution_leases",
    "execution_submissions", "execution_runs", "ep_execution_runs", "ep_submissions",
    "engineering_transactions", "prompt_execution_history",
    "provider_invocations", "dependabot_admission_events", "engineering_artifacts",
    "ep_technical_diagnostics", "execution_phase_spans", "execution_receipts",
    "execution_run_qualification_context", "execution_run_qualification_snapshots",
    "execution_validation_control_results", "execution_validation_profile_identities",
    "execution_validation_profiles", "managed_autonomy_actions",
    "managed_governance_gates", "managed_pr_check_observations",
    "managed_validation_observations", "terminal_telemetry_outbox",
    "engineering_component_logs", "engineering_status", "execution_activity_summaries",
    "daily_execution_statistics", "provider_usage_snapshots", "execution_projections",
)

_OPERATIONAL_METADATA_KEYS = frozenset({
    central_database.MAINTENANCE_LAST_ATTEMPT_KEY,
    central_database.PROVIDER_CAPACITY_HISTORY_KEY,
})
_EFFECT_DIRECTORIES = ("artifacts", "file-inbox")
_KNOWN_TOP_LEVEL = frozenset({
    central_database.DATABASE_FILENAME, f"{central_database.DATABASE_FILENAME}-journal",
    f"{central_database.DATABASE_FILENAME}-shm", f"{central_database.DATABASE_FILENAME}-wal",
    "server.json", "runtime-identity.json", "runtime.json", "runtime",
    "artifacts", "file-inbox", "operations", "recovery", "migration", "backups",
    "operational-reset-archive", "operational-reset.lock", "operational-installation.lock",
    "operational-installation.json", "legacy-installation-adoption.json",
    "development-profile.json", "dependabot-producer-heartbeat.json",
    "central.sqlite", ".forge-ep-consumer-recovery.lock",
})
_VERSIONED_RECOVERY_BACKUP = re.compile(
    r"epdata\.sqlite\.pre-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.backup"
)


class OperationalResetError(RuntimeError):
    """Stable fail-closed maintenance error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


@contextmanager
def _central_connection(*arguments: object, **keywords: object) -> Iterator[sqlite3.Connection]:
    """Open owning CENTRAL with enforced referential integrity for maintenance."""
    with sqlite_connection(*arguments, **keywords) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        yield connection


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _trusted_directory(path: Path, *, code: str, must_exist: bool = True) -> Path:
    """Resolve one operator-selected directory without following symlink components."""
    candidate = path.expanduser().absolute()
    # macOS exposes the root-owned compatibility alias /var -> /private/var.
    # Normalize that fixed system alias before rejecting any operator-selected
    # symlink component below it.
    if len(candidate.parts) >= 2 and candidate.parts[:2] == ("/", "var"):
        system_var = Path("/var")
        if system_var.is_symlink() and system_var.resolve() == Path("/private/var"):
            candidate = Path("/private/var", *candidate.parts[2:])
    component = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        component /= part
        if component.is_symlink():
            raise OperationalResetError(code)
        if not component.exists():
            break
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise OperationalResetError(code) from error
    if must_exist and not resolved.is_dir():
        raise OperationalResetError(code)
    return resolved


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _schema_objects(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Inventory non-table schema objects without treating them as datasets."""
    return [
        {
            "type": str(kind), "name": str(name), "table": str(table),
            "definition_digest": _digest(str(sql)) if sql is not None else None,
            "effect": "PRESERVE",
        }
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('index','trigger','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    ]


def _install_writer_blocks(connection: sqlite3.Connection) -> None:
    for table in sorted(_tables(connection) - _RESET_TABLES):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"ep_operational_reset_block_{table}_{operation.casefold()}"
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(name)} BEFORE {operation} ON {_quote(table)} "
                "WHEN EXISTS(SELECT 1 FROM ep_operational_reset_operations "
                "WHERE state NOT IN ('COMPLETED','ABORTED')) BEGIN "
                "SELECT RAISE(ABORT,'EP_OPERATIONAL_MAINTENANCE_ACTIVE'); END"
            )


def install_schema(connection: sqlite3.Connection) -> None:
    """Install schema-68 maintenance state and the canonical chat relation."""
    tables = _tables(connection)
    if "execution_chat_messages" in tables:
        foreign_keys = list(connection.execute("PRAGMA foreign_key_list(execution_chat_messages)"))
        parent = str(foreign_keys[0][2]) if foreign_keys else ""
        if parent != "ep_execution_runs":
            missing = connection.execute(
                "SELECT chat.id FROM execution_chat_messages AS chat "
                "LEFT JOIN ep_execution_runs AS run ON run.run_id=chat.run_id "
                "WHERE run.run_id IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise OperationalResetError(
                    "CHAT_CANONICAL_RUN_PARENT_MISSING", str(missing[0])
                )
            connection.execute("DROP TRIGGER IF EXISTS execution_chat_messages_immutable_update")
            connection.execute("DROP INDEX IF EXISTS execution_chat_messages_run_created")
            connection.execute(
                "CREATE TABLE execution_chat_messages_schema68 ("
                "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES ep_execution_runs(run_id),"
                "role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,"
                "model TEXT,created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO execution_chat_messages_schema68(id,run_id,role,content,model,created_at) "
                "SELECT id,run_id,role,content,model,created_at FROM execution_chat_messages"
            )
            connection.execute("DROP TABLE execution_chat_messages")
            connection.execute(
                "ALTER TABLE execution_chat_messages_schema68 RENAME TO execution_chat_messages"
            )
            connection.execute(
                "CREATE INDEX execution_chat_messages_run_created "
                "ON execution_chat_messages(run_id,id)"
            )
            connection.execute(
                "CREATE TRIGGER execution_chat_messages_immutable_update "
                "BEFORE UPDATE ON execution_chat_messages BEGIN "
                "SELECT RAISE(ABORT,'Chat transcript messages are immutable.'); END"
            )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ep_operational_dataset_state ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation INTEGER NOT NULL CHECK(generation>=0),"
        "updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO ep_operational_dataset_state(singleton,generation,updated_at) "
        "VALUES(1,0,CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ep_operational_reset_operations ("
        "operation_id TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ("
        "'PREPARING','AUTHORIZED','ARTIFACTS_ARCHIVING','ARTIFACTS_ARCHIVED',"
        "'DB_APPLIED','VERIFIED','COMPLETED','ABORTED','FAILED'))," \
        "actor TEXT NOT NULL,plan_digest TEXT NOT NULL,target_digest TEXT NOT NULL,"
        "source_revision TEXT NOT NULL,request_digest TEXT NOT NULL,plan_json TEXT NOT NULL,"
        "allowed_fk_json TEXT NOT NULL,backup_root TEXT NOT NULL,backup_path TEXT,backup_sha256 TEXT,"
        "backup_manifest_digest TEXT,generation_before INTEGER NOT NULL,generation_after INTEGER,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,verification_json TEXT)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ep_operational_reset_one_active "
        "ON ep_operational_reset_operations((1)) WHERE state NOT IN ('COMPLETED','ABORTED')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ep_operational_identity_tombstones ("
        "identity_kind TEXT NOT NULL,identity_digest TEXT NOT NULL,request_digest TEXT,"
        "operation_id TEXT NOT NULL REFERENCES ep_operational_reset_operations(operation_id),"
        "recorded_at TEXT NOT NULL,PRIMARY KEY(identity_kind,identity_digest))"
    )
    _install_writer_blocks(connection)


def _actor(data_root: Path) -> str:
    uid = require_installation_owner(data_root)
    try:
        name = pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        name = "unknown"
    return f"uid:{uid}:{name}"


def _identity(data_root: Path, connection: sqlite3.Connection) -> dict[str, object]:
    try:
        file_identity = json.loads((data_root / "runtime-identity.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalResetError("TARGET_IDENTITY_UNAVAILABLE") from error
    row = connection.execute("SELECT instance_id,schema_version FROM ep_installations").fetchone()
    metadata = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key='installation.instance_id'"
    ).fetchone()
    if (
        not isinstance(file_identity, dict) or not isinstance(file_identity.get("instance_id"), str)
        or row is None or metadata is None or file_identity["instance_id"] != row[0] != ""
        or row[0] != metadata[0]
    ):
        raise OperationalResetError("TARGET_IDENTITY_CONFLICT")
    database = (data_root / central_database.DATABASE_FILENAME).resolve()
    stat = database.stat()
    return {
        "product": "engineering-platform", "instance_id": str(row[0]),
        "data_root": str(data_root), "database": str(database), "schema_version": int(row[1]),
        "database_device": stat.st_dev, "database_inode": stat.st_ino,
    }


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    if table == "execution_projections":
        return int(connection.execute(
            "SELECT COUNT(*) FROM execution_projections WHERE classification!='CONFIGURATION'"
        ).fetchone()[0])
    if table == "engineering_metadata":
        placeholders = ",".join("?" for _ in _OPERATIONAL_METADATA_KEYS)
        return int(connection.execute(
            f"SELECT COUNT(*) FROM engineering_metadata WHERE key IN ({placeholders})",
            tuple(sorted(_OPERATIONAL_METADATA_KEYS)),
        ).fetchone()[0])
    return int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])


def _total_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])


def _value_bytes(value: object) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bytes):
        return b"B" + value
    return (type(value).__name__[0] + str(value)).encode("utf-8", "surrogateescape")


def _logical_digest(
    connection: sqlite3.Connection, tables: set[str], *, preserved_only: bool = False,
) -> str:
    digest = hashlib.sha256()
    for table in sorted(tables - _RESET_TABLES):
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]
        pk = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})") if int(row[5])]
        where, parameters = "", ()
        if table == "engineering_metadata" and preserved_only:
            placeholders = ",".join("?" for _ in _OPERATIONAL_METADATA_KEYS)
            where, parameters = f" WHERE key NOT IN ({placeholders})", tuple(sorted(_OPERATIONAL_METADATA_KEYS))
        elif table == "execution_projections" and preserved_only:
            where = " WHERE classification='CONFIGURATION'"
        elif preserved_only and table != "execution_projections" and table not in INSTALLATION_AND_CONFIGURATION | SECURITY_AND_AUTHORITY_LEDGER:
            continue
        order = ",".join(_quote(item) for item in (pk or columns))
        query = f"SELECT {','.join(_quote(item) for item in columns)} FROM {_quote(table)}{where} ORDER BY {order}"
        digest.update(table.encode("utf-8") + b"\0")
        for row in connection.execute(query, parameters):
            for value in row:
                encoded = _value_bytes(value)
                digest.update(len(encoded).to_bytes(8, "big") + encoded)
    return "sha256:" + digest.hexdigest()


def _safe_files(directory: Path, logical_root: str) -> list[dict[str, object]]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise OperationalResetError("EXTERNAL_PATH_UNSAFE", logical_root)
    rows: list[dict[str, object]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise OperationalResetError("EXTERNAL_SYMLINK_UNSAFE", str(path))
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            rows.append({
                "root": logical_root, "path": relative, "size_bytes": path.stat().st_size,
                "sha256": _file_digest(path), "effect": "ARCHIVE_OUTSIDE_ACTIVE_ROUTE",
            })
        elif not path.is_dir():
            raise OperationalResetError("EXTERNAL_PATH_UNSAFE", str(path))
    return rows


def _known_top_level(name: str) -> bool:
    return name in _KNOWN_TOP_LEVEL or _VERSIONED_RECOVERY_BACKUP.fullmatch(name) is not None


def _external_inventory(
    data_root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    unknown = sorted(path.name for path in data_root.iterdir() if not _known_top_level(path.name))
    rows: list[dict[str, object]] = []
    for name in _EFFECT_DIRECTORIES:
        rows.extend(_safe_files(data_root / name, name))
    preserved: list[dict[str, object]] = []
    for path in sorted(data_root.iterdir(), key=lambda item: item.name):
        if path.name in _EFFECT_DIRECTORIES or not _known_top_level(path.name):
            continue
        if path.name == central_database.DATABASE_FILENAME:
            classification = "INSTALLATION_AND_CONFIGURATION"
        elif path.name in {f"{central_database.DATABASE_FILENAME}-journal",
                           f"{central_database.DATABASE_FILENAME}-shm",
                           f"{central_database.DATABASE_FILENAME}-wal"}:
            classification = "SYSTEM_RUNTIME_CONTROL"
        elif path.name in {"central.sqlite"} or _VERSIONED_RECOVERY_BACKUP.fullmatch(path.name):
            classification = "FORENSIC_OR_RECOVERY"
        elif path.name.endswith(".lock") or path.name in {
            "runtime.json", "dependabot-producer-heartbeat.json",
        }:
            classification = "SYSTEM_RUNTIME_CONTROL"
        elif path.name in {"operations", "recovery", "migration", "backups", "operational-reset-archive"}:
            classification = "MAINTENANCE_AUDIT_OR_RECOVERY"
        else:
            classification = "INSTALLATION_AND_CONFIGURATION"
        entry: dict[str, object] = {
            "path": path.name, "classification": classification, "effect": "PRESERVE",
            "kind": "symlink" if path.is_symlink() else ("directory" if path.is_dir() else "file"),
        }
        if path.is_file() and not path.is_symlink() and path.name != central_database.DATABASE_FILENAME:
            entry.update({"size_bytes": path.stat().st_size, "sha256": _file_digest(path)})
        preserved.append(entry)
    return rows, preserved, unknown


def _runtime_activity(data_root: Path) -> dict[str, object]:
    try:
        payload = json.loads((data_root / "runtime.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": False, "reason": "NO_RUNTIME_RECORD"}
    except (OSError, json.JSONDecodeError):
        return {"active": True, "reason": "RUNTIME_IDENTITY_UNREADABLE"}
    pid = payload.get("pid") if isinstance(payload, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return {"active": True, "reason": "RUNTIME_IDENTITY_INVALID"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"active": False, "reason": "STALE_RUNTIME_RECORD"}
    except PermissionError:
        pass
    return {"active": True, "reason": "TARGET_RUNTIME_ACTIVE", "pid": pid}


def _foreign_key_findings(connection: sqlite3.Connection) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for table, rowid, parent, fk_index in connection.execute("PRAGMA foreign_key_check"):
        classification = (
            "OPERATIONAL_PURGE_SET" if table in OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION
            and parent in OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION else
            "PRESERVATION_OR_UNKNOWN_SCOPE"
        )
        finding_id = f"fk:{table}:{rowid}:{parent}:{fk_index}"
        findings.append({
            "id": finding_id, "table": str(table), "rowid": rowid, "parent": str(parent),
            "foreign_key_index": int(fk_index), "classification": classification,
        })
    return findings


def preview(data_root: Path) -> dict[str, object]:
    """Build a read-only, canonical plan for exactly one installed CENTRAL."""
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    database = root / central_database.DATABASE_FILENAME
    try:
        with _central_connection(f"file:{database}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            tables = _tables(connection)
            unknown_tables = sorted(tables - MAPPED_TABLES)
            missing_tables = sorted(MAPPED_TABLES - tables)
            identity = _identity(root, connection)
            schema = max((int(row[0]) for row in connection.execute(
                "SELECT version FROM engineering_schema_migrations"
            )), default=0)
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            foreign_keys = _foreign_key_findings(connection)
            counts = {
                "INSTALLATION_AND_CONFIGURATION": {
                    table: (
                        _total_count(connection, table) - _row_count(connection, table)
                        if table == "engineering_metadata" else _total_count(connection, table)
                    ) for table in sorted(INSTALLATION_AND_CONFIGURATION & tables)
                },
                "SECURITY_AND_AUTHORITY_LEDGER": {
                    table: _row_count(connection, table) for table in sorted(SECURITY_AND_AUTHORITY_LEDGER & tables)
                },
                "OPERATIONAL_HISTORY": {
                    table: _row_count(connection, table) for table in sorted(OPERATIONAL_HISTORY & tables)
                },
                "DERIVED_CACHE_OR_PROJECTION": {
                    table: _row_count(connection, table) for table in sorted(DERIVED_CACHE_OR_PROJECTION & tables)
                },
                # Own audit writes cannot invalidate their operation's plan.
                "MAINTENANCE_AUDIT": {
                    table: "EXCLUDED_FROM_PLAN_REVISION" for table in sorted(MAINTENANCE_AUDIT & tables)
                },
            }
            counts["INSTALLATION_AND_CONFIGURATION"]["execution_projections[CONFIGURATION]"] = (
                _total_count(connection, "execution_projections") - _row_count(connection, "execution_projections")
            ) if "execution_projections" in tables else 0
            counts["DERIVED_CACHE_OR_PROJECTION"]["engineering_metadata[operational_keys]"] = (
                _row_count(connection, "engineering_metadata")
            ) if "engineering_metadata" in tables else 0
            source_revision = _logical_digest(connection, tables)
            preserved_digest = _logical_digest(connection, tables, preserved_only=True)
            generation = int(connection.execute(
                "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
            ).fetchone()[0]) if "ep_operational_dataset_state" in tables else -1
            schema_objects = _schema_objects(connection)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise OperationalResetError("CENTRAL_INSPECTION_FAILED") from error
    external_error: str | None = None
    try:
        external, preserved_external, unknown_paths = _external_inventory(root)
    except OperationalResetError as error:
        external, preserved_external, unknown_paths = [], [], []
        external_error = error.code
    unsafe_preserved_paths = [
        str(item["path"]) for item in preserved_external if item["kind"] == "symlink"
    ]
    activity = _runtime_activity(root)
    blockers: list[str] = []
    if schema != SCHEMA_VERSION or int(identity["schema_version"]) != SCHEMA_VERSION:
        blockers.append("SCHEMA_UNSUPPORTED")
    if quick != ["ok"]:
        blockers.append("PHYSICAL_INTEGRITY_FAILED")
    if unknown_tables or missing_tables:
        blockers.append("TABLE_CLASSIFICATION_INCOMPLETE")
    if unknown_paths:
        blockers.append("EXTERNAL_CLASSIFICATION_INCOMPLETE")
    if unsafe_preserved_paths:
        blockers.append("EXTERNAL_SYMLINK_UNSAFE")
    if external_error is not None:
        blockers.append(
            "EXTERNAL_SYMLINK_UNSAFE" if external_error in {
                "EXTERNAL_PATH_UNSAFE", "EXTERNAL_SYMLINK_UNSAFE",
            } else external_error
        )
    if any(item["classification"] != "OPERATIONAL_PURGE_SET" for item in foreign_keys):
        blockers.append("PRESERVATION_INTEGRITY_FAILED")
    if activity["active"]:
        blockers.append("TARGET_WRITER_ACTIVE")
    review = [str(item["id"]) for item in foreign_keys if item["classification"] == "OPERATIONAL_PURGE_SET"]
    target_digest = _digest(identity)
    effect_set = {
        "database_tables": sorted(OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION),
        "mixed_record_rules": {
            "engineering_metadata": {"purge_keys": sorted(_OPERATIONAL_METADATA_KEYS)},
            "execution_projections": {"preserve_classification": "CONFIGURATION"},
        },
        "external_roots": list(_EFFECT_DIRECTORIES),
    }
    plan_basis = {
        "plan_version": PLAN_VERSION, "profile": PROFILE, "target": identity,
        "target_digest": target_digest, "source_revision": source_revision,
        "preserved_bindings_digest": preserved_digest, "dataset_generation": generation,
        "classification_counts": counts, "foreign_key_findings": foreign_keys,
        "external_files": external,
        "preserved_external": [
            item for item in preserved_external
            if item["classification"] != "SYSTEM_RUNTIME_CONTROL"
        ],
        "unsafe_preserved_paths": unsafe_preserved_paths,
        "schema_objects": schema_objects,
        "external_inventory_error": external_error,
        "unknown_tables": unknown_tables,
        "missing_tables": missing_tables, "unknown_external_paths": unknown_paths,
        "effect_set": effect_set,
    }
    return {
        **plan_basis, "plan_digest": _digest(plan_basis), "quick_check": quick,
        "runtime_activity": activity, "blocking_codes": sorted(set(blockers)),
        "review_required": review,
        "runtime_control_external": [
            item for item in preserved_external
            if item["classification"] == "SYSTEM_RUNTIME_CONTROL"
        ],
        "execution_state": "BLOCKED" if blockers else ("REVIEW_REQUIRED" if review else "ALLOWED"),
        "namespace": {
            "runtime_runs": "EP CENTRAL run/submission identifiers",
            "forge_missions": "external Forge runtime namespace; not created or reset here",
            "repository_document": "missions/MISSION-0003.md is repository documentation only",
        },
    }


@contextmanager
def _operation_lock(data_root: Path, operation_id: str) -> Iterator[None]:
    if _OPERATION.fullmatch(operation_id) is None:
        raise OperationalResetError("OPERATION_ID_INVALID")
    lock = OperationalInstallationLock(data_root)
    try:
        try:
            lock.acquire(operation_id)
        except OperationalInstallationLockError as error:
            raise OperationalResetError("MAINTENANCE_LOCK_BUSY") from error
        yield
    finally:
        try:
            lock.release(operation_id)
        except OperationalInstallationLockError:
            pass


def _backup_destination(backup_root: Path, operation_id: str) -> Path:
    expanded = backup_root.expanduser().absolute()
    if expanded.is_symlink():
        raise OperationalResetError("BACKUP_PATH_UNSAFE")
    existed = expanded.exists()
    root = _trusted_directory(
        expanded, code="BACKUP_PATH_UNSAFE", must_exist=existed,
    )
    if existed:
        if root.stat().st_mode & 0o077:
            raise OperationalResetError("BACKUP_PERMISSIONS_UNSAFE")
    else:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        root.chmod(0o700)
    destination = root / operation_id
    if destination.is_symlink():
        raise OperationalResetError("BACKUP_PATH_UNSAFE")
    destination.mkdir(mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    return destination


def _copy_external(data_root: Path, destination: Path, entries: list[dict[str, object]]) -> None:
    for entry in entries:
        source = data_root / str(entry["root"]) / str(entry["path"])
        target = destination / "files" / str(entry["root"]) / str(entry["path"])
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if source.is_symlink() or not source.is_file() or _file_digest(source) != entry["sha256"]:
            raise OperationalResetError("BACKUP_SOURCE_CHANGED")
        shutil.copyfile(source, target)
        target.chmod(0o600)


def _backup(
    data_root: Path, operation_id: str, backup_root: Path, plan: dict[str, object],
) -> dict[str, object]:
    destination = _backup_destination(backup_root, operation_id)
    try:
        destination.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise OperationalResetError("BACKUP_PATH_INSIDE_ACTIVE_DATA_ROOT")
    database_backup = destination / "central.sqlite"
    temporary = destination / ".central.sqlite.partial"
    temporary.unlink(missing_ok=True)
    required = (data_root / central_database.DATABASE_FILENAME).stat().st_size + sum(
        int(item["size_bytes"]) for item in plan["external_files"]  # type: ignore[index]
    )
    if shutil.disk_usage(destination).free < required * 2 + 1024 * 1024:
        raise OperationalResetError("BACKUP_SPACE_INSUFFICIENT")
    try:
        with _central_connection(
            f"file:{data_root / central_database.DATABASE_FILENAME}?mode=ro", uri=True,
        ) as source, _central_connection(temporary) as target:
            source.backup(target)
        temporary.chmod(0o600)
        os.replace(temporary, database_backup)
        _copy_external(data_root, destination, list(plan["external_files"]))  # type: ignore[arg-type]
        with _central_connection(f"file:{database_backup}?mode=ro", uri=True) as restored:
            restored.execute("PRAGMA query_only=ON")
            if [str(row[0]) for row in restored.execute("PRAGMA integrity_check")] != ["ok"]:
                raise OperationalResetError("BACKUP_RESTORE_INTEGRITY_FAILED")
            counts = {table: _row_count(restored, table) for table in sorted(_tables(restored))}
            foreign_keys = _foreign_key_findings(restored)
        entries = []
        for path in sorted((destination / "files").rglob("*")) if (destination / "files").exists() else []:
            if path.is_file():
                entries.append({
                    "path": path.relative_to(destination).as_posix(), "size_bytes": path.stat().st_size,
                    "sha256": _file_digest(path),
                })
        manifest = {
            "manifest_version": 1, "kind": "EP_PROTECTED_OPERATIONAL_RESET_BACKUP",
            "operation_id": operation_id, "profile": PROFILE, "target": plan["target"],
            "target_digest": plan["target_digest"], "plan_digest": plan["plan_digest"],
            "source_revision": plan["source_revision"], "schema_version": SCHEMA_VERSION,
            "database": {"path": "central.sqlite", "sha256": _file_digest(database_backup),
                         "size_bytes": database_backup.stat().st_size, "row_counts": counts,
                         "foreign_key_findings": foreign_keys},
            "included_files": entries, "excluded": [
                "provider logins", "Keychain items", "runtime virtual environments", "source repositories",
            ],
            "created_at": _utcnow(),
        }
        manifest_path = destination / "manifest.json"
        manifest_path.write_bytes(_canonical(manifest) + b"\n")
        manifest_path.chmod(0o600)
        # Re-read every protected byte; a manifest hash alone is not restore proof.
        verify_backup(destination, operation_id=operation_id, plan_digest=str(plan["plan_digest"]))
        return {
            "backup_path": str(destination), "backup_sha256": manifest["database"]["sha256"],
            "backup_manifest_digest": _file_digest(manifest_path),
        }
    except OperationalResetError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        raise OperationalResetError("BACKUP_WRITE_OR_VERIFY_FAILED") from error
    finally:
        temporary.unlink(missing_ok=True)


def verify_backup(path: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    root = _trusted_directory(path, code="BACKUP_PATH_UNSAFE")
    try:
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise OperationalResetError("BACKUP_PERMISSIONS_UNSAFE")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        database = root / "central.sqlite"
        if (
            manifest.get("operation_id") != operation_id or manifest.get("plan_digest") != plan_digest
            or manifest.get("database", {}).get("sha256") != _file_digest(database)
        ):
            raise OperationalResetError("BACKUP_BINDING_INVALID")
        for entry in manifest.get("included_files", []):
            candidate = (root / str(entry["path"])).resolve(strict=True)
            candidate.relative_to(root)
            if candidate.is_symlink() or _file_digest(candidate) != entry["sha256"]:
                raise OperationalResetError("BACKUP_FILE_INVALID")
        with _central_connection(f"file:{database}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            counts = {table: _row_count(connection, table) for table in sorted(_tables(connection))}
        if integrity != ["ok"] or counts != manifest["database"]["row_counts"]:
            raise OperationalResetError("BACKUP_RESTORE_INTEGRITY_FAILED")
        return {"state": "VERIFIED", "operation_id": operation_id,
                "manifest_digest": _file_digest(root / "manifest.json")}
    except OperationalResetError:
        raise
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, sqlite3.DatabaseError) as error:
        raise OperationalResetError("BACKUP_INVALID") from error


def _operation(connection: sqlite3.Connection, operation_id: str) -> dict[str, object] | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM ep_operational_reset_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def prepare(
    data_root: Path, *, operation_id: str, plan_digest: str, backup_root: Path,
    allowed_fk_findings: tuple[str, ...] = (),
) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        actor = _actor(root)
        plan = preview(root)
        if plan["plan_digest"] != plan_digest:
            raise OperationalResetError("PLAN_DIGEST_MISMATCH")
        if plan["blocking_codes"]:
            raise OperationalResetError("PLAN_BLOCKED", ",".join(plan["blocking_codes"]))
        expected_findings = tuple(sorted(str(item) for item in plan["review_required"]))
        allowed = tuple(sorted(set(allowed_fk_findings)))
        if allowed != expected_findings:
            raise OperationalResetError("OPERATIONAL_FK_APPROVAL_MISMATCH")
        request = {
            "operation_id": operation_id, "actor": actor, "plan_digest": plan_digest,
            "target_digest": plan["target_digest"], "profile": PROFILE,
            "effect_set": plan["effect_set"], "backup_root": str(backup_root.expanduser().resolve(strict=False)),
            "allowed_fk_findings": list(allowed),
        }
        request_digest = _digest(request)
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            existing = _operation(connection, operation_id)
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise OperationalResetError("OPERATION_REQUEST_CONFLICT")
                return _public_status(existing)
            generation = int(connection.execute(
                "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
            ).fetchone()[0])
            now = _utcnow()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO ep_operational_reset_operations("
                "operation_id,state,actor,plan_digest,target_digest,source_revision,request_digest,"
                "plan_json,allowed_fk_json,backup_root,generation_before,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, "PREPARING", actor, plan_digest, plan["target_digest"],
                 plan["source_revision"], request_digest, json.dumps(plan, sort_keys=True),
                 json.dumps(allowed), request["backup_root"], generation, now, now),
            )
            connection.execute("COMMIT")
        try:
            backup = _backup(root, operation_id, backup_root, plan)
            with _central_connection(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE ep_operational_reset_operations SET state='AUTHORIZED',backup_path=?,"
                    "backup_sha256=?,backup_manifest_digest=?,updated_at=? "
                    "WHERE operation_id=? AND state='PREPARING'",
                    (backup["backup_path"], backup["backup_sha256"],
                     backup["backup_manifest_digest"], _utcnow(), operation_id),
                )
                connection.execute("COMMIT")
                return _public_status(_operation(connection, operation_id) or {})
        except Exception:
            # PREPARING intentionally remains durable and writer-blocking.  A
            # resume retries the same backup instead of inventing a new reset.
            raise


def _public_status(row: dict[str, object]) -> dict[str, object]:
    allowed = {
        "operation_id", "state", "actor", "plan_digest", "target_digest", "source_revision",
        "backup_root", "backup_path", "backup_sha256", "backup_manifest_digest", "generation_before",
        "generation_after", "created_at", "updated_at", "verification_json",
    }
    result = {key: value for key, value in row.items() if key in allowed}
    if isinstance(result.get("verification_json"), str):
        result["verification"] = json.loads(str(result.pop("verification_json")))
    result["credentials_included_in_receipt"] = False
    return result


def status(data_root: Path, *, operation_id: str | None = None) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    _actor(root)
    with _central_connection(f"file:{root / central_database.DATABASE_FILENAME}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if operation_id is None:
            row = connection.execute(
                "SELECT * FROM ep_operational_reset_operations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM ep_operational_reset_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        generation = int(connection.execute(
            "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
        ).fetchone()[0])
    return {"dataset_generation": generation,
            "operation": _public_status(dict(row)) if row is not None else None}


def maintenance_active(data_root: Path) -> bool:
    """Read the durable fence without initializing or migrating CENTRAL."""
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    database = root / central_database.DATABASE_FILENAME
    try:
        with _central_connection(f"file:{database}?mode=ro", uri=True) as connection:
            if "ep_operational_reset_operations" not in _tables(connection):
                return False
            return connection.execute(
                "SELECT 1 FROM ep_operational_reset_operations "
                "WHERE state NOT IN ('COMPLETED','ABORTED') LIMIT 1"
            ).fetchone() is not None
    except sqlite3.DatabaseError as error:
        raise OperationalResetError("MAINTENANCE_STATE_UNAVAILABLE") from error


def _archive_effects(data_root: Path, operation_id: str, plan: dict[str, object]) -> None:
    archive = data_root / "operational-reset-archive" / operation_id
    if archive.is_symlink():
        raise OperationalResetError("ARCHIVE_PATH_UNSAFE")
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    expected = list(plan["external_files"])  # type: ignore[arg-type]
    for name in _EFFECT_DIRECTORIES:
        source, target = data_root / name, archive / name
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise OperationalResetError("ARCHIVE_PATH_UNSAFE")
            if source.exists() and any(source.iterdir()):
                raise OperationalResetError("ARCHIVE_RECONCILIATION_AMBIGUOUS", name)
        elif source.exists():
            if source.is_symlink() or not source.is_dir():
                raise OperationalResetError("EXTERNAL_PATH_UNSAFE", name)
            os.replace(source, target)
        source.mkdir(mode=0o700, exist_ok=True)
    observed: list[dict[str, object]] = []
    for name in _EFFECT_DIRECTORIES:
        observed.extend(_safe_files(archive / name, name))
    comparable = lambda rows: sorted((row["root"], row["path"], row["size_bytes"], row["sha256"]) for row in rows)
    if comparable(observed) != comparable(expected):
        raise OperationalResetError("ARCHIVE_RECONCILIATION_FAILED")


def _tombstone(connection: sqlite3.Connection, kind: str, identity: str,
               operation_id: str, request_digest: str | None = None) -> None:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO ep_operational_identity_tombstones("
        "identity_kind,identity_digest,request_digest,operation_id,recorded_at) VALUES(?,?,?,?,?)",
        (kind, digest, request_digest, operation_id, _utcnow()),
    )


def _submission_request_digest(row: sqlite3.Row) -> str:
    return hashlib.sha256(_canonical({
        "repository_id": row["repository_id"], "producer_id": row["producer_id"],
        "producer_type": row["producer_type"], "producer_version": row["producer_version"],
        "prompt": row["prompt"], "constraints": row["constraints"],
        "correlation_id": row["correlation_id"], "mission_id": row["mission_id"],
        "engineering_action_id": row["engineering_action_id"],
        "transport_receipt_id": row["transport_receipt_id"],
    })).hexdigest()


def _record_tombstones(connection: sqlite3.Connection, operation_id: str) -> None:
    connection.row_factory = sqlite3.Row
    for row in connection.execute("SELECT * FROM ep_submissions"):
        _tombstone(connection, "submission_id", str(row["submission_id"]), operation_id)
        if row["idempotency_key"] is not None:
            identity = f"{row['project_id']}\0{row['idempotency_key']}"
            _tombstone(connection, "submission_idempotency", identity, operation_id,
                       _submission_request_digest(row))
    for table, column, kind in (
        ("ep_execution_runs", "run_id", "run_id"),
        ("execution_runs", "run_id", "run_id"),
        ("engineering_transactions", "run_id", "run_id"),
        ("prompt_execution_history", "run_id", "run_id"),
        ("provider_invocations", "invocation_id", "provider_invocation_id"),
    ):
        for row in connection.execute(f"SELECT {_quote(column)} FROM {_quote(table)}"):
            _tombstone(connection, kind, str(row[0]), operation_id)


def _delete_operational(connection: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in _OPERATIONAL_METADATA_KEYS)
    connection.execute(
        f"DELETE FROM engineering_metadata WHERE key IN ({placeholders})",
        tuple(sorted(_OPERATIONAL_METADATA_KEYS)),
    )
    for table in PURGE_ORDER:
        if table == "execution_projections":
            connection.execute("DELETE FROM execution_projections WHERE classification!='CONFIGURATION'")
        else:
            connection.execute(f"DELETE FROM {_quote(table)}")


def apply(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        _actor(root)
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            row = _operation(connection, operation_id)
        if row is None or row["plan_digest"] != plan_digest:
            raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
        state = str(row["state"])
        if state in {"DB_APPLIED", "VERIFIED", "COMPLETED"}:
            return _public_status(row)
        if state == "PREPARING":
            raise OperationalResetError("BACKUP_NOT_AUTHORIZED")
        plan = json.loads(str(row["plan_json"]))
        archive = root / "operational-reset-archive" / operation_id
        if state == "AUTHORIZED" and not archive.exists():
            current = preview(root)
            if current["source_revision"] != row["source_revision"] or current["plan_digest"] != plan_digest:
                raise OperationalResetError("SOURCE_REVISION_CHANGED")
            verify_backup(Path(str(row["backup_path"])), operation_id=operation_id, plan_digest=plan_digest)
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='ARTIFACTS_ARCHIVING',updated_at=? "
                "WHERE operation_id=? AND state IN ('AUTHORIZED','ARTIFACTS_ARCHIVING')",
                (_utcnow(), operation_id),
            )
            connection.execute("COMMIT")
        _archive_effects(root, operation_id, plan)
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='ARTIFACTS_ARCHIVED',updated_at=? "
                "WHERE operation_id=? AND state='ARTIFACTS_ARCHIVING'", (_utcnow(), operation_id),
            )
            connection.execute("COMMIT")
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Immutable-evidence and maintenance-block triggers are removed and
            # recreated inside this one uncommitted transaction. Other writers
            # cannot observe an unfenced schema window.
            triggers = [
                (str(name), str(sql)) for name, sql in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ("
                    + ",".join("?" for _ in (OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION)) + ")",
                    tuple(sorted(OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION)),
                ) if sql is not None
            ]
            for name, _sql in triggers:
                connection.execute(f"DROP TRIGGER {_quote(name)}")
            _record_tombstones(connection, operation_id)
            _delete_operational(connection)
            generation = int(row["generation_before"]) + 1
            connection.execute(
                "UPDATE ep_operational_dataset_state SET generation=?,updated_at=? WHERE singleton=1",
                (generation, _utcnow()),
            )
            for _name, sql in triggers:
                connection.execute(sql)
            fk = list(connection.execute("PRAGMA foreign_key_check"))
            if fk:
                connection.execute("ROLLBACK")
                raise OperationalResetError("POST_RESET_FOREIGN_KEY_FAILED")
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='DB_APPLIED',generation_after=?,updated_at=? "
                "WHERE operation_id=? AND state='ARTIFACTS_ARCHIVED'",
                (generation, _utcnow(), operation_id),
            )
            connection.execute("COMMIT")
            return _public_status(_operation(connection, operation_id) or {})


def verify(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        _actor(root)
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            row = _operation(connection, operation_id)
            if row is None or row["plan_digest"] != plan_digest or row["state"] not in {"DB_APPLIED", "VERIFIED", "COMPLETED"}:
                raise OperationalResetError("RESET_NOT_APPLIED")
            plan = json.loads(str(row["plan_json"]))
            quick = [str(item[0]) for item in connection.execute("PRAGMA quick_check")]
            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            remaining = {
                table: _row_count(connection, table)
                for table in sorted(OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION)
            }
            remaining["engineering_metadata[operational_keys]"] = _row_count(
                connection, "engineering_metadata"
            )
            preserved_digest = _logical_digest(connection, _tables(connection), preserved_only=True)
            generation = int(connection.execute(
                "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
            ).fetchone()[0])
            credentials = int(connection.execute("SELECT COUNT(*) FROM ep_consumer_credentials").fetchone()[0])
            projects = int(connection.execute("SELECT COUNT(*) FROM ep_project_registrations").fetchone()[0])
            repositories = int(connection.execute("SELECT COUNT(*) FROM ep_repository_registrations").fetchone()[0])
        archive = root / "operational-reset-archive" / operation_id
        _archive_effects(root, operation_id, plan)
        backup = verify_backup(Path(str(row["backup_path"])), operation_id=operation_id, plan_digest=plan_digest)
        failures = {
            table: count for table, count in remaining.items()
            if count and not (table == "execution_projections" and count == 0)
        }
        result = {
            "quick_check": quick, "foreign_key_errors": len(foreign_keys),
            "operational_rows_remaining": failures,
            "preserved_bindings_digest": preserved_digest,
            "expected_preserved_bindings_digest": plan["preserved_bindings_digest"],
            "dataset_generation": generation, "expected_generation": row["generation_after"],
            "project_registrations": projects, "repository_registrations": repositories,
            "consumer_credentials": credentials, "backup": backup,
            "active_artifact_roots_empty": all(not any((root / name).iterdir()) for name in _EFFECT_DIRECTORIES),
            "archive_path": str(archive),
        }
        if (
            quick != ["ok"] or foreign_keys or failures
            or preserved_digest != plan["preserved_bindings_digest"]
            or generation != row["generation_after"]
            or not result["active_artifact_roots_empty"]
        ):
            raise OperationalResetError("POST_RESET_VERIFICATION_FAILED")
        if row["state"] == "DB_APPLIED":
            with _central_connection(database) as connection:
                connection.execute(
                    "UPDATE ep_operational_reset_operations SET state='VERIFIED',verification_json=?,updated_at=? "
                    "WHERE operation_id=? AND state='DB_APPLIED'",
                    (json.dumps(result, sort_keys=True), _utcnow(), operation_id),
                )
        return result


def finish(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        _actor(root)
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            row = _operation(connection, operation_id)
            if row is None or row["plan_digest"] != plan_digest:
                raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
            if row["state"] == "COMPLETED":
                return _public_status(row)
            if row["state"] != "VERIFIED":
                raise OperationalResetError("VERIFICATION_REQUIRED_BEFORE_FINISH")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='COMPLETED',updated_at=? "
                "WHERE operation_id=? AND state='VERIFIED'", (_utcnow(), operation_id),
            )
            connection.execute("COMMIT")
            return _public_status(_operation(connection, operation_id) or {})


def abort(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    """End a prepared operation only while no reset effect has started."""
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        _actor(root)
        archive = root / "operational-reset-archive" / operation_id
        if archive.exists():
            raise OperationalResetError("ABORT_AFTER_EFFECT_FORBIDDEN")
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            row = _operation(connection, operation_id)
            if row is None or row["plan_digest"] != plan_digest:
                raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
            if row["state"] == "ABORTED":
                return _public_status(row)
            if row["state"] not in {"PREPARING", "AUTHORIZED"}:
                raise OperationalResetError("ABORT_AFTER_EFFECT_FORBIDDEN")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='ABORTED',updated_at=? "
                "WHERE operation_id=? AND state IN ('PREPARING','AUTHORIZED')",
                (_utcnow(), operation_id),
            )
            connection.execute("COMMIT")
            return _public_status(_operation(connection, operation_id) or {})


def resume(data_root: Path, *, operation_id: str, plan_digest: str,
           backup_root: Path | None = None) -> dict[str, object]:
    current = status(data_root, operation_id=operation_id).get("operation")
    if not isinstance(current, dict):
        raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
    state = current.get("state")
    if state == "PREPARING":
        if backup_root is None:
            raise OperationalResetError("BACKUP_ROOT_REQUIRED_FOR_RESUME")
        root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
        with _central_connection(root / central_database.DATABASE_FILENAME) as connection:
            row = _operation(connection, operation_id) or {}
            plan = json.loads(str(row["plan_json"]))
        requested_backup_root = str(backup_root.expanduser().resolve(strict=False))
        if requested_backup_root != row.get("backup_root"):
            raise OperationalResetError("BACKUP_ROOT_BINDING_MISMATCH")
        backup = _backup(root, operation_id, backup_root, plan)
        with _central_connection(root / central_database.DATABASE_FILENAME) as connection:
            connection.execute(
                "UPDATE ep_operational_reset_operations SET state='AUTHORIZED',backup_path=?,"
                "backup_sha256=?,backup_manifest_digest=?,updated_at=? "
                "WHERE operation_id=? AND state='PREPARING'",
                (backup["backup_path"], backup["backup_sha256"], backup["backup_manifest_digest"],
                 _utcnow(), operation_id),
            )
        state = "AUTHORIZED"
    if state in {"AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED"}:
        apply(data_root, operation_id=operation_id, plan_digest=plan_digest)
        state = "DB_APPLIED"
    if state == "DB_APPLIED":
        verify(data_root, operation_id=operation_id, plan_digest=plan_digest)
    return status(data_root, operation_id=operation_id)


def contract_readback(
    data_root: Path, *, command: str, operation_id: str | None, details: dict[str, object],
) -> dict[str, object]:
    """Project the shared operator contract without exposing protected values."""
    observed = preview(data_root)
    persisted: object = None
    if operation_id is not None:
        persisted = status(data_root, operation_id=operation_id).get("operation")
    operation = (
        persisted if isinstance(persisted, dict) else
        (details.get("operation") if isinstance(details.get("operation"), dict) else details)
    )
    if not isinstance(operation, dict):
        operation = {}
    selected_operation = operation_id or (
        str(operation["operation_id"]) if isinstance(operation.get("operation_id"), str) else None
    )
    backup: dict[str, object] | None = None
    if isinstance(operation.get("backup_path"), str):
        verified = False
        try:
            verify_backup(
                Path(str(operation["backup_path"])), operation_id=str(selected_operation),
                plan_digest=str(operation.get("plan_digest")),
            )
            verified = True
        except OperationalResetError:
            verified = False
        backup = {
            "manifest": str(Path(str(operation["backup_path"])) / "manifest.json"),
            "digest": operation.get("backup_manifest_digest"), "verified": verified,
        }
    target = observed["target"]
    blockers = list(observed["blocking_codes"])
    if operation.get("state") == "FAILED":
        blockers.append("OPERATION_FAILED_RECONCILIATION_REQUIRED")
    return {
        "contract_version": "operational-reset-v1", "product": "engineering-platform",
        "command": command, "operation_id": selected_operation,
        "state": operation.get("state", observed["execution_state"]),
        "allowed": not blockers,
        "target": {
            "instance_id": target["instance_id"], "database_path": target["database"],
            "database_identity": observed["target_digest"],
            "schema_version": target["schema_version"],
        },
        "profile": PROFILE, "dataset_generation": observed["dataset_generation"],
        "plan_digest": operation.get("plan_digest", observed["plan_digest"]),
        "relevant_revision_digest": observed["source_revision"], "backup": backup,
        "counts": observed["classification_counts"], "blockers": blockers,
        "integrity": {
            "quick_check": observed["quick_check"],
            "foreign_key_findings": observed["foreign_key_findings"],
        },
        "preserved_bindings_digest": observed["preserved_bindings_digest"],
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-platform-maintenance")
    parser.add_argument(
        "command",
        choices=("preview", "prepare", "apply", "status", "resume", "verify", "finish", "abort"),
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--plan-digest")
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--allow-operational-fk", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            result = preview(args.data_root)
        elif args.command == "status":
            result = status(args.data_root, operation_id=args.operation_id)
        else:
            if not args.operation_id or not args.plan_digest:
                parser.error("--operation-id and --plan-digest are required")
            if args.command == "prepare":
                if args.backup_root is None:
                    parser.error("--backup-root is required")
                result = prepare(args.data_root, operation_id=args.operation_id,
                                 plan_digest=args.plan_digest, backup_root=args.backup_root,
                                 allowed_fk_findings=tuple(args.allow_operational_fk))
            elif args.command == "apply":
                result = apply(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
            elif args.command == "resume":
                result = resume(args.data_root, operation_id=args.operation_id,
                                 plan_digest=args.plan_digest, backup_root=args.backup_root)
            elif args.command == "verify":
                result = verify(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
            elif args.command == "abort":
                result = abort(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
            else:
                result = finish(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
        print(json.dumps(contract_readback(
            args.data_root, command=args.command, operation_id=args.operation_id, details=result,
        ), sort_keys=True))
        return 0
    except OperationalResetError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
