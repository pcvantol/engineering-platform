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
import stat
import subprocess
import tempfile
from typing import Iterator, Mapping

from . import central_database, development_profile
from .platform_admin import require_installation_owner
from .platform_version import CURRENT_PLATFORM_VERSION
from .operational_installation_lock import (
    OperationalInstallationLock, OperationalInstallationLockError,
)
from .storage import sqlite_connection


PROFILE = "EP_CENTRAL_OPERATIONAL_HISTORY_V1"
PLAN_VERSION = 2
SCHEMA_VERSION = 69
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_INSTANCE_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UPDATE_STEPS = (
    "INSTALLATION_LOCK", "INVENTORY_AND_COMPATIBILITY", "EXACT_ARTIFACT",
    "QUIESCE", "BACKUP_AND_MIGRATION", "ACTIVATE", "VERIFY", "CLEANUP",
)
_UPDATE_CLEANUP_DIRECTORIES = ("build", "download", "pip-cache")
_ACTIVE_STATES = frozenset({
    "PREPARING", "AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED",
    "DB_APPLIED", "VERIFIED", "FAILED",
})
_APPLY_ENTRY_STATES = frozenset({
    "AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED",
})
_APPLY_IDEMPOTENT_STATES = frozenset({"DB_APPLIED", "VERIFIED"})
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
    "ep_merge_delegations",
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
_EFFECT_DIRECTORIES = ("artifacts", "file-inbox", "runtime/central-data-imports")
_ACTIVE_ROOT_MODE = 0o700
_FROZEN_ROOT_MODE = 0o500
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
_MAINTENANCE_GUARD_TRIGGERS = frozenset({
    "ep_operational_reset_operations_insert_owner",
    "ep_operational_reset_operations_update_owner",
    "ep_operational_reset_operations_delete_immutable",
    "ep_operational_dataset_state_insert_owner",
    "ep_operational_dataset_state_update_owner",
    "ep_operational_dataset_state_delete_immutable",
    "ep_operational_identity_tombstones_insert_owner",
    "ep_operational_identity_tombstones_update_immutable",
    "ep_operational_identity_tombstones_delete_immutable",
})


class OperationalResetError(RuntimeError):
    """Stable fail-closed maintenance error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


@contextmanager
def _central_connection(*arguments: object, **keywords: object) -> Iterator[sqlite3.Connection]:
    """Open owning CENTRAL with enforced referential integrity for maintenance."""
    with sqlite_connection(*arguments, **keywords) as connection:
        # Persistent maintenance triggers call this connection-local function.
        # Normal product connections cannot forge owning reset/audit writes.
        connection.create_function("ep_reset_maintenance_owner", 0, lambda: 1)
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


def _implementation_digest() -> str:
    return _digest({
        "module_digest": _file_digest(Path(__file__)),
        "product_version": CURRENT_PLATFORM_VERSION,
    })


def _implementation_source_revision() -> str:
    try:
        module = Path(__file__).resolve()
        root = Path(subprocess.check_output(
            ("git", "-C", str(module.parent), "rev-parse", "--show-toplevel"),
            text=True, stderr=subprocess.DEVNULL,
        ).strip()).resolve()
        relative = module.relative_to(root).as_posix()
        subprocess.run(
            ("git", "-C", str(root), "ls-files", "--error-unmatch", relative),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        revision = subprocess.check_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision):
            return revision
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return "UNAVAILABLE_IN_INSTALLED_PACKAGE"


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


def install_writer_fences(connection: sqlite3.Connection) -> None:
    """Fence newly installed schema tables against an active reset."""
    _install_writer_blocks(connection)


def _expected_writer_fences(tables: set[str]) -> set[str]:
    return {
        f"ep_operational_reset_block_{table}_{operation.casefold()}"
        for table in tables - _RESET_TABLES
        for operation in ("INSERT", "UPDATE", "DELETE")
    } | set(_MAINTENANCE_GUARD_TRIGGERS)


def _install_maintenance_guards(connection: sqlite3.Connection) -> None:
    """Make reset state, audit bindings and tombstones owner-only/immutable."""
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_reset_operations_insert_owner "
        "BEFORE INSERT ON ep_operational_reset_operations "
        "WHEN ep_reset_maintenance_owner()!=1 BEGIN "
        "SELECT RAISE(ABORT,'EP_RESET_MAINTENANCE_OWNER_REQUIRED'); END"
    )
    # Recreate this schema-owned trigger when the maintenance audit gains a
    # new immutable effect field within the same unreleased schema revision.
    connection.execute("DROP TRIGGER IF EXISTS ep_operational_reset_operations_update_owner")
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_reset_operations_update_owner "
        "BEFORE UPDATE ON ep_operational_reset_operations BEGIN "
        "SELECT CASE WHEN ep_reset_maintenance_owner()!=1 THEN "
        "RAISE(ABORT,'EP_RESET_MAINTENANCE_OWNER_REQUIRED') END; "
        "SELECT CASE WHEN NEW.operation_id!=OLD.operation_id OR NEW.actor!=OLD.actor "
        "OR NEW.plan_digest!=OLD.plan_digest OR NEW.target_digest!=OLD.target_digest "
        "OR NEW.source_revision!=OLD.source_revision OR NEW.request_digest!=OLD.request_digest "
        "OR NEW.plan_json!=OLD.plan_json OR NEW.allowed_fk_json!=OLD.allowed_fk_json "
        "OR NEW.backup_root!=OLD.backup_root OR NEW.generation_before!=OLD.generation_before "
        "OR NEW.created_at!=OLD.created_at THEN "
        "RAISE(ABORT,'EP_RESET_AUDIT_BINDING_IMMUTABLE') END; "
        "SELECT CASE WHEN ((NEW.backup_path IS NOT OLD.backup_path "
        "OR NEW.backup_sha256 IS NOT OLD.backup_sha256 "
        "OR NEW.backup_manifest_digest IS NOT OLD.backup_manifest_digest) "
        "AND NOT (OLD.state='PREPARING' AND NEW.state='AUTHORIZED')) "
        "OR (OLD.state='PREPARING' AND NEW.state='AUTHORIZED' AND "
        "(NEW.backup_path IS NULL OR NEW.backup_sha256 IS NULL "
        "OR NEW.backup_manifest_digest IS NULL)) "
        "OR (NEW.generation_after IS NOT OLD.generation_after AND NOT "
        "(OLD.state='ARTIFACTS_ARCHIVED' AND NEW.state='DB_APPLIED')) "
        "OR (NEW.finish_boundary_path IS NOT OLD.finish_boundary_path AND NOT "
        "(OLD.state='VERIFIED' AND NEW.state='VERIFIED' "
        "AND OLD.finish_boundary_path IS NULL AND NEW.finish_boundary_path IS NOT NULL "
        "AND NEW.finish_boundary_digest IS OLD.finish_boundary_digest)) "
        "OR (NEW.finish_boundary_digest IS NOT OLD.finish_boundary_digest AND NOT "
        "(OLD.state='VERIFIED' AND NEW.state='COMPLETED' "
        "AND OLD.finish_boundary_digest IS NULL AND NEW.finish_boundary_digest IS NOT NULL)) "
        "OR (OLD.state='VERIFIED' AND NEW.state='COMPLETED' AND "
        "(NEW.finish_boundary_path IS NULL OR NEW.finish_boundary_digest IS NULL)) "
        "OR (NEW.verification_json IS NOT OLD.verification_json AND NOT "
        "((OLD.state='DB_APPLIED' AND NEW.state='VERIFIED') OR "
        "(OLD.state='VERIFIED' AND NEW.state='COMPLETED'))) THEN "
        "RAISE(ABORT,'EP_RESET_AUDIT_EFFECT_IMMUTABLE') END; "
        "SELECT CASE WHEN NEW.state!=OLD.state AND NOT ("
        "(OLD.state='PREPARING' AND NEW.state IN ('AUTHORIZED','ABORTED','FAILED')) OR "
        "(OLD.state='AUTHORIZED' AND NEW.state IN ('ARTIFACTS_ARCHIVING','ABORTED','FAILED')) OR "
        "(OLD.state='ARTIFACTS_ARCHIVING' AND NEW.state IN ('ARTIFACTS_ARCHIVED','FAILED')) OR "
        "(OLD.state='ARTIFACTS_ARCHIVED' AND NEW.state IN ('DB_APPLIED','FAILED')) OR "
        "(OLD.state='DB_APPLIED' AND NEW.state IN ('VERIFIED','FAILED')) OR "
        "(OLD.state='VERIFIED' AND NEW.state IN ('COMPLETED','FAILED'))) THEN "
        "RAISE(ABORT,'EP_RESET_STATE_TRANSITION_INVALID') END; END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_reset_operations_delete_immutable "
        "BEFORE DELETE ON ep_operational_reset_operations BEGIN "
        "SELECT RAISE(ABORT,'EP_RESET_AUDIT_IMMUTABLE'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_dataset_state_insert_owner "
        "BEFORE INSERT ON ep_operational_dataset_state "
        "WHEN ep_reset_maintenance_owner()!=1 BEGIN "
        "SELECT RAISE(ABORT,'EP_RESET_MAINTENANCE_OWNER_REQUIRED'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_dataset_state_update_owner "
        "BEFORE UPDATE ON ep_operational_dataset_state BEGIN "
        "SELECT CASE WHEN ep_reset_maintenance_owner()!=1 THEN "
        "RAISE(ABORT,'EP_RESET_MAINTENANCE_OWNER_REQUIRED') END; "
        "SELECT CASE WHEN NEW.singleton!=OLD.singleton OR NEW.generation NOT IN "
        "(OLD.generation,OLD.generation+1) THEN "
        "RAISE(ABORT,'EP_RESET_DATASET_GENERATION_INVALID') END; END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_dataset_state_delete_immutable "
        "BEFORE DELETE ON ep_operational_dataset_state BEGIN "
        "SELECT RAISE(ABORT,'EP_RESET_DATASET_STATE_IMMUTABLE'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS ep_operational_identity_tombstones_insert_owner "
        "BEFORE INSERT ON ep_operational_identity_tombstones "
        "WHEN ep_reset_maintenance_owner()!=1 BEGIN "
        "SELECT RAISE(ABORT,'EP_RESET_MAINTENANCE_OWNER_REQUIRED'); END"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS ep_operational_identity_tombstones_{operation.casefold()}_immutable "
            f"BEFORE {operation} ON ep_operational_identity_tombstones BEGIN "
            "SELECT RAISE(ABORT,'EP_RESET_TOMBSTONE_IMMUTABLE'); END"
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
    if connection.execute(
        "SELECT 1 FROM ep_operational_dataset_state WHERE singleton=1"
    ).fetchone() is None:
        connection.execute(
            "INSERT INTO ep_operational_dataset_state(singleton,generation,updated_at) "
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
        "finish_boundary_path TEXT,finish_boundary_digest TEXT,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,verification_json TEXT)"
    )
    operation_columns = {
        str(row[1]) for row in connection.execute(
            "PRAGMA table_info(ep_operational_reset_operations)"
        )
    }
    for column in ("finish_boundary_path", "finish_boundary_digest"):
        if column not in operation_columns:
            connection.execute(
                f"ALTER TABLE ep_operational_reset_operations ADD COLUMN {column} TEXT"
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
    _install_maintenance_guards(connection)
    _install_writer_blocks(connection)


def _actor(data_root: Path) -> str:
    uid = require_installation_owner(data_root)
    try:
        name = pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        name = "unknown"
    return f"uid:{uid}:{name}"


def _valid_instance_id(value: object) -> bool:
    return isinstance(value, str) and _INSTANCE_ID.fullmatch(value) is not None


def _identity(data_root: Path, connection: sqlite3.Connection) -> dict[str, object]:
    try:
        file_identity = json.loads((data_root / "runtime-identity.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalResetError("TARGET_IDENTITY_UNAVAILABLE") from error
    row = connection.execute("SELECT instance_id,schema_version FROM ep_installations").fetchone()
    metadata = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key='installation.instance_id'"
    ).fetchone()
    runtime_instance = file_identity.get("instance_id") if isinstance(file_identity, dict) else None
    database_instance = row[0] if row is not None else None
    metadata_instance = metadata[0] if metadata is not None else None
    if (
        not _valid_instance_id(runtime_instance)
        or not _valid_instance_id(database_instance)
        or not _valid_instance_id(metadata_instance)
        or runtime_instance != database_instance
        or runtime_instance != metadata_instance
        or database_instance != metadata_instance
    ):
        raise OperationalResetError("TARGET_IDENTITY_CONFLICT")
    database = (data_root / central_database.DATABASE_FILENAME).resolve()
    stat = database.stat()
    return {
        "product": "engineering-platform", "instance_id": str(database_instance),
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


def _walk_regular_files(directory: Path, *, code: str) -> list[Path]:
    """Walk without following any symlink or special-file boundary."""
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise OperationalResetError(code)
    files: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            raise OperationalResetError(code) from error
        for entry in entries:
            try:
                if entry.is_symlink():
                    raise OperationalResetError(code)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
                else:
                    raise OperationalResetError(code)
            except OSError as error:
                raise OperationalResetError(code) from error
    return sorted(files)


def _safe_files(directory: Path, logical_root: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in _walk_regular_files(directory, code="EXTERNAL_SYMLINK_UNSAFE"):
        relative = path.relative_to(directory).as_posix()
        rows.append({
            "root": logical_root, "path": relative, "size_bytes": path.stat().st_size,
            "sha256": _file_digest(path), "effect": "ARCHIVE_OUTSIDE_ACTIVE_ROUTE",
        })
    return rows


def _candidate_runtime_marker(
    operation_root: Path, *, expected_installation_id: str,
    require_staged_artifact: bool,
) -> dict[str, object] | None:
    """Read the updater's exact, closed, token-free candidate identity."""
    if not _valid_instance_id(expected_installation_id):
        return None
    operation_id = operation_root.name
    marker = _regular_json_object(operation_root / "candidate-runtime.json")
    if marker is None or _OPERATION.fullmatch(operation_id) is None:
        return None
    expected_candidate = str(operation_root / "candidate-venv")
    if (
        set(marker) != {
            "schema_version", "operation_id", "installation_id", "target_version",
            "target_digest", "target_source_revision", "staged_artifact", "candidate_venv",
        }
        or marker.get("schema_version") != 1
        or marker.get("operation_id") != operation_id
        or not _valid_instance_id(marker.get("installation_id"))
        or marker.get("installation_id") != expected_installation_id
        or marker.get("candidate_venv") != expected_candidate
        or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                        str(marker.get("target_version", ""))) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(marker.get("target_digest", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(marker.get("target_source_revision", ""))) is None
    ):
        return None
    staged = marker.get("staged_artifact")
    if not isinstance(staged, str):
        return None
    staged_path = Path(staged)
    if (
        not staged_path.is_absolute()
        or staged_path != Path(os.path.normpath(staged))
        or staged_path.parent != operation_root / "download"
        or staged_path.name != (
            f"engineering_platform-{marker['target_version']}-py3-none-any.whl"
        )
    ):
        return None
    if require_staged_artifact:
        try:
            metadata = staged_path.lstat()
        except OSError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _regular_file_digest(staged_path) != marker["target_digest"]
        ):
            return None
    return marker


def _path_is_absent(path: Path) -> bool:
    """Distinguish an absent path from a dangling link or unreadable entry."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _unbound_candidate_runtime_marker(
    operation_root: Path, *, expected_installation_id: str,
) -> dict[str, object] | None:
    """Return a closed candidate marker only in the pre-journal crash window."""
    if not _path_is_absent(operation_root / "operation.json"):
        return None
    return _candidate_runtime_marker(
        operation_root, expected_installation_id=expected_installation_id,
        require_staged_artifact=True,
    )


def _opaque_update_directory_classification(
    root_name: str, directory: Path, relative: str, *, expected_installation_id: str,
) -> str | None:
    """Recognize only an identity-bound updater runtime/staging directory.

    Merely finding ``operation.json`` beside a venv-shaped directory is not
    ownership evidence: an arbitrary file must not turn unknown application
    data into an opaque preserve boundary.  The journal and the updater's
    candidate marker therefore have to bind the same operation, installation,
    plan and exact paths before normal Python venv symlinks become opaque.
    """
    parts = Path(relative).parts
    if (
        root_name != "operations" or len(parts) != 2
        or _OPERATION.fullmatch(parts[0]) is None
        or parts[1] not in {"candidate-venv", "download", "pip-cache"}
    ):
        return None

    operation_id = parts[0]
    operation_root = directory.parent
    unbound_marker = _unbound_candidate_runtime_marker(
        operation_root, expected_installation_id=expected_installation_id,
    )
    if unbound_marker is not None:
        return "INSTALLATION_RUNTIME_STAGING_UNBOUND"
    if parts[1] != "candidate-venv":
        return None
    data_root = operation_root.parent.parent
    journal = _regular_json_object(operation_root / "operation.json")
    marker = _candidate_runtime_marker(
        operation_root, expected_installation_id=expected_installation_id,
        require_staged_artifact=False,
    )
    if journal is None:
        return None
    if marker is None:
        return None
    schema = journal.get("schema_version")
    base_fields = {
        "schema_version", "operation_id", "plan", "plan_digest", "state", "events",
    }
    fields_by_schema = {
        1: base_fields,
        2: base_fields | {"prepared_candidate", "prepared_candidate_digest"},
        3: base_fields | {
            "prepared_candidate", "prepared_candidate_digest",
            "prepared_record_provenance", "prepared_record_provenance_digest",
        },
        4: base_fields | {
            "prepared_candidate", "prepared_candidate_digest",
            "prepared_record_provenance", "prepared_record_provenance_digest",
            "execution_admission", "execution_admission_digest",
        },
    }
    if (
        type(schema) is not int
        or set(journal) != fields_by_schema.get(schema)
        or not isinstance(journal.get("events"), list)
        or journal.get("state") not in {
            "PREPARED", "INVENTORIED", "QUIESCING", "QUIESCED", "BACKED_UP",
            "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE",
        }
    ):
        return None
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        return None
    plan_fields = {
        "operation_id", "installation_id", "data_root", "current_version",
        "current_digest", "target_version", "target_digest", "target_source_revision",
        "artifact", "cleanup_targets", "steps",
    }
    installation_id = plan.get("installation_id")
    expected_cleanup = [
        str(operation_root / name) for name in _UPDATE_CLEANUP_DIRECTORIES
    ]
    if (
        frozenset(plan) not in {frozenset(plan_fields), frozenset(plan_fields | {"legacy_adoption"})}
        or journal.get("operation_id") != operation_id
        or plan.get("operation_id") != operation_id
        or installation_id != expected_installation_id
        or plan.get("data_root") != str(data_root)
        or plan.get("cleanup_targets") != expected_cleanup
        or plan.get("steps") != list(_UPDATE_STEPS)
        or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                        str(plan.get("current_version", ""))) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(plan.get("current_digest", ""))) is None
    ):
        return None
    try:
        canonical_plan = json.dumps(
            plan, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if journal.get("plan_digest") != "sha256:" + hashlib.sha256(canonical_plan).hexdigest():
        return None

    expected_candidate = str(directory)
    expected_operation = str(operation_root)
    expected_interpreter = str(directory / "bin" / "python")
    if (
        set(marker) != {
            "schema_version", "operation_id", "installation_id", "target_version",
            "target_digest", "target_source_revision", "staged_artifact", "candidate_venv",
        }
        or marker.get("schema_version") != 1
        or marker.get("operation_id") != operation_id
        or marker.get("installation_id") != installation_id
        or marker.get("candidate_venv") != expected_candidate
        or marker.get("target_version") != plan.get("target_version")
        or marker.get("target_digest") != plan.get("target_digest")
        or marker.get("target_source_revision") != plan.get("target_source_revision")
        or marker.get("staged_artifact") != plan.get("artifact")
    ):
        return None
    staged_artifact = marker.get("staged_artifact")
    if (
        not isinstance(staged_artifact, str)
        or Path(staged_artifact).parent != operation_root / "download"
    ):
        return None

    prepared = journal.get("prepared_candidate")
    if prepared is None:
        return (
            "INSTALLATION_RUNTIME"
            if journal.get("prepared_candidate_digest") is None else None
        )
    if not isinstance(prepared, dict):
        return None
    package = prepared.get("package")
    if (
        set(prepared) != {
            "operation_id", "installation_id", "operation_root", "staged_artifact",
            "artifact_digest", "candidate_venv", "interpreter", "pip_cache", "package",
        }
        or prepared.get("operation_id") != operation_id
        or prepared.get("installation_id") != installation_id
        or prepared.get("operation_root") != expected_operation
        or prepared.get("candidate_venv") != expected_candidate
        or prepared.get("interpreter") != expected_interpreter
        or prepared.get("staged_artifact") != staged_artifact
        or prepared.get("artifact_digest") != plan.get("target_digest")
        or prepared.get("pip_cache") != str(operation_root / "pip-cache")
        or not isinstance(package, dict)
        or set(package) != {"interpreter", "version", "metadata", "package"}
        or package.get("interpreter") != expected_interpreter
        or package.get("version") != plan.get("target_version")
        or not all(isinstance(package.get(field), str) and package[field]
                   for field in ("version", "metadata", "package"))
        or not all(_lexically_within(directory, Path(str(package[field])))
                   for field in ("metadata", "package"))
    ):
        return None
    try:
        canonical_prepared = json.dumps(
            prepared, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if journal.get("prepared_candidate_digest") != (
        "sha256:" + hashlib.sha256(canonical_prepared).hexdigest()
    ):
        return None
    return "INSTALLATION_RUNTIME"


def _lexically_within(root: Path, candidate: Path) -> bool:
    """Check an already persisted absolute path without resolving any link."""
    if (
        not candidate.is_absolute()
        or candidate != Path(os.path.normpath(str(candidate)))
    ):
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate != root


def _regular_json_object(path: Path) -> dict[str, object] | None:
    """Read one bounded regular JSON file without following its final link."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4 * 1024 * 1024:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return value if isinstance(value, dict) else None


def _regular_file_digest(path: Path) -> str | None:
    """Hash one regular file while refusing a final-component link swap."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _walk_preserved_files(
    directory: Path, *, root_name: str, code: str, expected_installation_id: str,
    opaque_bindings: list[tuple[Path, str, int, int, int]],
) -> tuple[list[Path], list[tuple[Path, str, int, int, int]]]:
    """Walk preserved product data while treating exact updater venvs as opaque."""
    if not directory.exists():
        return [], []
    if directory.is_symlink() or not directory.is_dir():
        raise OperationalResetError(code)
    files: list[Path] = []
    opaque: list[tuple[Path, str, int, int, int]] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            raise OperationalResetError(code) from error
        for entry in entries:
            try:
                path = Path(entry.path)
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise OperationalResetError(code)
                if stat.S_ISDIR(metadata.st_mode):
                    relative = path.relative_to(directory).as_posix()
                    classification = _opaque_update_directory_classification(
                        root_name, path, relative,
                        expected_installation_id=expected_installation_id,
                    )
                    if classification is not None:
                        descriptor = _open_opaque_directory(
                            path, device=metadata.st_dev, inode=metadata.st_ino,
                        )
                        binding = (
                            path, classification, metadata.st_dev, metadata.st_ino,
                            descriptor,
                        )
                        opaque.append(binding)
                        opaque_bindings.append(binding)
                    else:
                        pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    files.append(path)
                else:
                    raise OperationalResetError(code)
            except OSError as error:
                raise OperationalResetError(code) from error
    return sorted(files), sorted(opaque, key=lambda item: item[0])


def _open_opaque_directory(path: Path, *, device: int, inode: int) -> int:
    """Pin the accepted real directory until the inventory is published."""
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != device
            or metadata.st_ino != inode
        ):
            raise OperationalResetError("EXTERNAL_SYMLINK_UNSAFE")
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise OperationalResetError("EXTERNAL_SYMLINK_UNSAFE") from error
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _recheck_opaque_directory(
    path: Path, *, device: int, inode: int, descriptor: int,
) -> None:
    """Bind the final opaque receipt to the still-pinned original directory."""
    try:
        pinned = os.fstat(descriptor)
        current = path.lstat()
    except OSError as error:
        raise OperationalResetError("EXTERNAL_SYMLINK_UNSAFE") from error
    if (
        not stat.S_ISDIR(pinned.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or pinned.st_dev != device or pinned.st_ino != inode
        or current.st_dev != device or current.st_ino != inode
    ):
        raise OperationalResetError("EXTERNAL_SYMLINK_UNSAFE")


def _known_top_level(name: str, *, development_owned: frozenset[str] = frozenset()) -> bool:
    return (name in _KNOWN_TOP_LEVEL or name in development_owned
            or _VERSIONED_RECOVERY_BACKUP.fullmatch(name) is not None)


def _nested_preserved_classification(
    root_name: str, relative: str, path: Path, *, expected_installation_id: str,
) -> str | None:
    """Classify only product-owned shapes inside preserved top-level roots."""
    parts = Path(relative).parts
    if root_name == "runtime":
        if relative in {
            "store-authority.json", "engineering-dashboard-relay",
            "server-launchagent.out.log", "server-launchagent.err.log",
        }:
            return "INSTALLATION_RUNTIME"
        if relative == "pending-central-data-import.json":
            return "ACTIVE_INGEST_CONTROL"
        return None
    if root_name == "operations":
        if len(parts) == 1 and path.is_file() and parts[0].endswith(".json"):
            return "INSTALLATION_OPERATION_AUDIT"
        if parts and _OPERATION.fullmatch(parts[0]) is not None:
            operation_root = path.parents[len(parts) - 2]
            if _unbound_candidate_runtime_marker(
                operation_root, expected_installation_id=expected_installation_id,
            ) is not None:
                if parts[1:] == ("candidate-runtime.json",):
                    return "INSTALLATION_RUNTIME_STAGING_UNBOUND"
                # The exact staging subtrees are normally opaque. This branch
                # also keeps a race/retry readback classified if a leaf was
                # enumerated just before the boundary recognition.
                if len(parts) >= 3 and parts[1] in {"download", "pip-cache"}:
                    return "INSTALLATION_RUNTIME_STAGING_UNBOUND"
            if (operation_root / "operation.json").is_file() and len(parts) >= 2:
                if parts[1] in {"operation.json", "candidate-runtime.json"} and len(parts) == 2:
                    return "INSTALLATION_OPERATION_AUDIT"
                if parts[1:] == ("backup", "central.sqlite"):
                    return "FORENSIC_OR_RECOVERY"
                if parts[1] in {"build", "download", "pip-cache"}:
                    return "INSTALLATION_RUNTIME_STAGING"
        return None
    if root_name == "migration":
        if (len(parts) == 1 and parts[0].endswith(".json")) or (
            len(parts) == 2 and parts[0] == "contamination-attestations"
            and parts[1].endswith(".json")
        ):
            return "MIGRATION_AUDIT"
        return None
    if root_name == "backups":
        if len(parts) == 1 and re.fullmatch(r"legacy-schema40-[A-Za-z0-9._-]+\.db", parts[0]):
            return "FORENSIC_OR_RECOVERY"
        return None
    if root_name == "recovery":
        # The topology reserves this root, but no current product writer owns
        # an arbitrary nested payload shape. Fail closed until one is defined.
        return None
    if root_name == "operational-reset-archive":
        if (
            len(parts) >= 3 and _OPERATION.fullmatch(parts[0]) is not None
            and (parts[1] in {"artifacts", "file-inbox"}
                 or parts[1:3] == ("runtime", "central-data-imports"))
        ):
            return "MAINTENANCE_AUDIT_OR_RECOVERY"
        if (
            len(parts) >= 4 and _OPERATION.fullmatch(parts[0]) is not None
            and parts[1] == "finish-boundary"
            and re.fullmatch(r"generation-(?:0|[1-9][0-9]*)", parts[2]) is not None
            and (
                parts[3] in {
                    "artifacts", "file-inbox", "manifest.json", "manifest.json.partial",
                }
                or parts[3:5] == ("runtime", "central-data-imports")
            )
        ):
            return "MAINTENANCE_AUDIT_OR_RECOVERY"
        return None
    return None


def _external_inventory_bound(
    data_root: Path, *, expected_installation_id: str,
    opaque_bindings: list[tuple[Path, str, int, int, int]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str], list[str]]:
    try:
        profile = development_profile.describe(data_root)
    except development_profile.DevelopmentProfileError as error:
        raise OperationalResetError("DEVELOPMENT_PROFILE_INVALID") from error
    development_owned = (
        frozenset({development_profile.CACHE_DIRECTORY, development_profile.LOG_DIRECTORY})
        if profile["kind"] == development_profile.PROFILE else frozenset()
    )
    unknown = sorted(path.name for path in data_root.iterdir()
                     if not _known_top_level(path.name, development_owned=development_owned))
    rows: list[dict[str, object]] = []
    for name in _EFFECT_DIRECTORIES:
        rows.extend(_safe_files(data_root / Path(name), name))
    preserved: list[dict[str, object]] = []
    active_ingest_controls: list[str] = []
    for path in sorted(data_root.iterdir(), key=lambda item: item.name):
        if (path.name in {Path(name).parts[0] for name in _EFFECT_DIRECTORIES}
                or not _known_top_level(path.name, development_owned=development_owned)):
            if path.name != "runtime":
                continue
        if path.name in {"artifacts", "file-inbox"}:
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
        elif path.name in development_owned:
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
        if path.is_dir() and not path.is_symlink() and path.name in {
            "runtime", "operations", "recovery", "migration", "backups",
            "operational-reset-archive",
        }:
            nested_files, opaque_boundaries = _walk_preserved_files(
                path, root_name=path.name, code="EXTERNAL_SYMLINK_UNSAFE",
                expected_installation_id=expected_installation_id,
                opaque_bindings=opaque_bindings,
            )
            for boundary, classification, _device, _inode, _descriptor in opaque_boundaries:
                relative = boundary.relative_to(path).as_posix()
                entry = {
                    "path": f"{path.name}/{relative}",
                    "classification": classification,
                    "effect": "PRESERVE", "kind": "directory",
                    "opaque_boundary": True, "symlinks_followed": False,
                }
                if classification == "INSTALLATION_RUNTIME_STAGING_UNBOUND":
                    entry["installation_update_state"] = "INCOMPLETE_NO_OPERATION_JOURNAL"
                preserved.append(entry)
            for nested in nested_files:
                relative = nested.relative_to(path).as_posix()
                if path.name == "runtime" and relative.startswith("central-data-imports/"):
                    continue
                classification = _nested_preserved_classification(
                    path.name, relative, nested,
                    expected_installation_id=expected_installation_id,
                )
                if classification is None:
                    unknown.append(f"{path.name}/{relative}")
                    continue
                preserved.append({
                    "path": f"{path.name}/{relative}", "classification": classification,
                    "effect": "PRESERVE", "kind": "file", "size_bytes": nested.stat().st_size,
                    "sha256": _file_digest(nested),
                })
                if classification == "ACTIVE_INGEST_CONTROL":
                    active_ingest_controls.append(f"{path.name}/{relative}")
    return rows, preserved, sorted(set(unknown)), active_ingest_controls


def _external_inventory(
    data_root: Path, *, expected_installation_id: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str], list[str]]:
    """Inventory external data while pinning every published opaque boundary."""
    if not _valid_instance_id(expected_installation_id):
        raise OperationalResetError("TARGET_IDENTITY_CONFLICT")
    bindings: list[tuple[Path, str, int, int, int]] = []
    try:
        result = _external_inventory_bound(
            data_root, expected_installation_id=expected_installation_id,
            opaque_bindings=bindings,
        )
        # This is intentionally the final work before returning the receipt.
        # A path swapped after initial acceptance cannot be represented as the
        # pinned directory that was actually classified.
        for path, _classification, device, inode, descriptor in bindings:
            _recheck_opaque_directory(
                path, device=device, inode=inode, descriptor=descriptor,
            )
        return result
    finally:
        for _path, _classification, _device, _inode, descriptor in bindings:
            try:
                os.close(descriptor)
            except OSError:
                pass


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
            installed_triggers = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            missing_writer_fences = sorted(
                _expected_writer_fences(tables) - installed_triggers
            )
            active_rows = [
                {"operation_id": str(row[0]), "state": str(row[1])}
                for row in connection.execute(
                    "SELECT operation_id,state FROM ep_operational_reset_operations "
                    "WHERE state NOT IN ('COMPLETED','ABORTED') ORDER BY operation_id"
                )
            ] if "ep_operational_reset_operations" in tables else []
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise OperationalResetError("CENTRAL_INSPECTION_FAILED") from error
    external_error: str | None = None
    try:
        external, preserved_external, unknown_paths, active_ingest_controls = _external_inventory(
            root, expected_installation_id=str(identity["instance_id"]),
        )
    except OperationalResetError as error:
        external, preserved_external, unknown_paths, active_ingest_controls = [], [], [], []
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
    if active_ingest_controls:
        blockers.append("ACTIVE_INGEST_PENDING")
    if missing_writer_fences:
        blockers.append("WRITER_FENCE_INCOMPLETE")
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
    if active_rows:
        blockers.append("MAINTENANCE_ALREADY_ACTIVE")
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
        "plan_version": PLAN_VERSION, "operator_contract": "operational-reset-v1",
        "profile": PROFILE, "product_version": CURRENT_PLATFORM_VERSION,
        "implementation_source_revision": _implementation_source_revision(),
        "implementation_digest": _implementation_digest(), "target": identity,
        "target_digest": target_digest, "source_revision": source_revision,
        "meaningful_source_revision": source_revision,
        "preserved_bindings_digest": preserved_digest, "dataset_generation": generation,
        "classification_counts": counts, "foreign_key_findings": foreign_keys,
        "external_files": external,
        "preserved_external": [
            item for item in preserved_external
            if item["classification"] != "SYSTEM_RUNTIME_CONTROL"
        ],
        "unsafe_preserved_paths": unsafe_preserved_paths,
        "schema_objects": schema_objects,
        "missing_writer_fences": missing_writer_fences,
        "external_inventory_error": external_error,
        "active_ingest_controls": active_ingest_controls,
        "unknown_tables": unknown_tables,
        "missing_tables": missing_tables, "unknown_external_paths": unknown_paths,
        "effect_set": effect_set,
    }
    return {
        **plan_basis, "plan_digest": _digest(plan_basis), "quick_check": quick,
        "runtime_activity": activity, "blocking_codes": sorted(set(blockers)),
        "active_maintenance": active_rows,
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


def _backup_destination(backup_root: Path, operation_id: str) -> tuple[Path, Path | None]:
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
    if destination.exists():
        if not destination.is_dir():
            raise OperationalResetError("BACKUP_PATH_UNSAFE")
        try:
            if next(destination.iterdir(), None) is not None:
                # A crash may have atomically published the exact same complete
                # backup before its DB binding was stored. The caller may only
                # reuse it after full operation/plan verification.
                return destination, None
        except OSError as error:
            raise OperationalResetError("BACKUP_PATH_UNSAFE") from error
        raise OperationalResetError("BACKUP_DESTINATION_PREEXISTS")
    staging = Path(tempfile.mkdtemp(prefix=f".{operation_id}.", suffix=".partial", dir=root))
    staging.chmod(0o700)
    return staging, destination


def _bound_regular_file(root: Path, relative: str, *, code: str) -> Path:
    logical = Path(relative)
    if logical.is_absolute() or not logical.parts or any(part in {"", ".", ".."} for part in logical.parts):
        raise OperationalResetError(code)
    parent = _trusted_directory(root / logical.parent, code=code)
    try:
        parent.relative_to(root)
    except ValueError as error:
        raise OperationalResetError(code) from error
    candidate = parent / logical.name
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise OperationalResetError(code) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OperationalResetError(code)
    return candidate


def _secure_mkdirs(root: Path, relative: Path, *, code: str) -> Path:
    """Create a relative directory tree only after checking every component."""
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise OperationalResetError(code)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                metadata = current.lstat()
            except OSError as error:
                raise OperationalResetError(code) from error
        except OSError as error:
            raise OperationalResetError(code) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OperationalResetError(code)
    return current


def _exclusive_copy(source: Path, target: Path) -> None:
    """Copy one regular file without following source or destination symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OperationalResetError("BACKUP_SOURCE_CHANGED")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source_handle, os.fdopen(
                target_fd, "wb", closefd=False,
            ) as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
        finally:
            os.close(target_fd)
    except FileExistsError as error:
        raise OperationalResetError("BACKUP_DESTINATION_CONFLICT") from error
    finally:
        os.close(source_fd)


def _copy_external(data_root: Path, destination: Path, entries: list[dict[str, object]]) -> None:
    for entry in entries:
        source_root = _trusted_directory(
            data_root / Path(str(entry["root"])), code="BACKUP_SOURCE_CHANGED",
        )
        source = _bound_regular_file(
            source_root, str(entry["path"]), code="BACKUP_SOURCE_CHANGED",
        )
        relative_target = Path("files") / Path(str(entry["root"])) / Path(str(entry["path"]))
        if relative_target.is_absolute() or ".." in relative_target.parts:
            raise OperationalResetError("BACKUP_DESTINATION_CONFLICT")
        target = destination / relative_target
        _secure_mkdirs(
            destination, relative_target.parent, code="BACKUP_PATH_UNSAFE",
        )
        if _file_digest(source) != entry["sha256"]:
            raise OperationalResetError("BACKUP_SOURCE_CHANGED")
        _exclusive_copy(source, target)
        if _file_digest(source) != entry["sha256"] or _file_digest(target) != entry["sha256"]:
            raise OperationalResetError("BACKUP_SOURCE_CHANGED")


def _backup(
    data_root: Path, operation_id: str, backup_root: Path, plan: dict[str, object],
) -> dict[str, object]:
    destination, publish_destination = _backup_destination(backup_root, operation_id)
    selected_destination = publish_destination or destination
    try:
        selected_destination.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise OperationalResetError("BACKUP_PATH_INSIDE_ACTIVE_DATA_ROOT")
    if publish_destination is None:
        verified = verify_backup(
            destination, operation_id=operation_id, plan_digest=str(plan["plan_digest"]),
        )
        return {
            "backup_path": str(destination),
            "backup_sha256": _file_digest(destination / "central.sqlite"),
            "backup_manifest_digest": verified["manifest_digest"],
        }
    database_backup = destination / "central.sqlite"
    temporary = destination / ".central.sqlite.partial"
    if temporary.exists() or temporary.is_symlink() or database_backup.exists() or database_backup.is_symlink():
        raise OperationalResetError("BACKUP_DESTINATION_CONFLICT")
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
        manifest_digest = _file_digest(manifest_path)
        # Re-read every protected byte; a manifest hash alone is not restore proof.
        verify_backup(destination, operation_id=operation_id, plan_digest=str(plan["plan_digest"]))
        os.replace(destination, publish_destination)
        destination = publish_destination
        return {
            "backup_path": str(destination), "backup_sha256": manifest["database"]["sha256"],
            "backup_manifest_digest": manifest_digest,
        }
    except OperationalResetError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        raise OperationalResetError("BACKUP_WRITE_OR_VERIFY_FAILED") from error
    finally:
        temporary.unlink(missing_ok=True)


def verify_backup(
    path: Path, *, operation_id: str, plan_digest: str,
    expected_manifest_digest: str | None = None,
) -> dict[str, object]:
    root = _trusted_directory(path, code="BACKUP_PATH_UNSAFE")
    try:
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise OperationalResetError("BACKUP_PERMISSIONS_UNSAFE")
        manifest_path = _bound_regular_file(root, "manifest.json", code="BACKUP_INVALID")
        manifest_digest = _file_digest(manifest_path)
        if expected_manifest_digest is not None and manifest_digest != expected_manifest_digest:
            raise OperationalResetError("BACKUP_MANIFEST_DIGEST_MISMATCH")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        database = _bound_regular_file(root, "central.sqlite", code="BACKUP_INVALID")
        if (
            manifest.get("operation_id") != operation_id or manifest.get("plan_digest") != plan_digest
            or manifest.get("database", {}).get("sha256") != _file_digest(database)
        ):
            raise OperationalResetError("BACKUP_BINDING_INVALID")
        included_files = manifest.get("included_files", [])
        if not isinstance(included_files, list):
            raise OperationalResetError("BACKUP_INVALID")
        expected_files = {"manifest.json", "central.sqlite"}
        for entry in included_files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise OperationalResetError("BACKUP_INVALID")
            candidate = _bound_regular_file(root, str(entry["path"]), code="BACKUP_FILE_INVALID")
            if _file_digest(candidate) != entry["sha256"]:
                raise OperationalResetError("BACKUP_FILE_INVALID")
            expected_files.add(str(entry["path"]))
        actual_files = {
            candidate.relative_to(root).as_posix()
            for candidate in _walk_regular_files(root, code="BACKUP_FILE_INVALID")
        }
        if actual_files != expected_files:
            raise OperationalResetError("BACKUP_FILE_SET_INVALID")
        with _central_connection(f"file:{database}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            counts = {table: _row_count(connection, table) for table in sorted(_tables(connection))}
        if integrity != ["ok"] or counts != manifest["database"]["row_counts"]:
            raise OperationalResetError("BACKUP_RESTORE_INTEGRITY_FAILED")
        return {"state": "VERIFIED", "operation_id": operation_id,
                "manifest_digest": manifest_digest}
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


def _transition(
    connection: sqlite3.Connection, operation_id: str, before: str, after: str,
    assignments: str = "", parameters: tuple[object, ...] = (),
) -> None:
    prefix = f"state='{after}',updated_at=?"
    if assignments:
        prefix += "," + assignments
    cursor = connection.execute(
        f"UPDATE ep_operational_reset_operations SET {prefix} "
        "WHERE operation_id=? AND state=?",
        (_utcnow(), *parameters, operation_id, before),
    )
    if cursor.rowcount != 1:
        raise OperationalResetError("OPERATION_STATE_TRANSITION_CONFLICT")


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
        non_maintenance_blockers = [
            code for code in plan["blocking_codes"] if code != "MAINTENANCE_ALREADY_ACTIVE"
        ]
        if non_maintenance_blockers:
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
            if plan["blocking_codes"]:
                raise OperationalResetError("PLAN_BLOCKED", ",".join(plan["blocking_codes"]))
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
                _transition(
                    connection, operation_id, "PREPARING", "AUTHORIZED",
                    "backup_path=?,backup_sha256=?,backup_manifest_digest=?",
                    (backup["backup_path"], backup["backup_sha256"],
                     backup["backup_manifest_digest"]),
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
        "generation_after", "finish_boundary_path", "finish_boundary_digest",
        "created_at", "updated_at", "verification_json",
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


def revalidate(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    """Read-only proof for one already prepared owning operation."""
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    actor = _actor(root)
    database = root / central_database.DATABASE_FILENAME
    with _central_connection(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = _operation(connection, operation_id)
    if row is None or row["plan_digest"] != plan_digest:
        raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
    if row["state"] != "AUTHORIZED":
        raise OperationalResetError("OPERATION_STATE_INVALID_FOR_REVALIDATION")
    if row["actor"] != actor:
        raise OperationalResetError("OPERATOR_AUTHORITY_CHANGED")
    plan = json.loads(str(row["plan_json"]))
    if int(plan.get("plan_version", -1)) != PLAN_VERSION:
        raise OperationalResetError("PLAN_VERSION_UNSUPPORTED")
    observed = preview(root)
    if observed["active_maintenance"] != [{
        "operation_id": operation_id, "state": "AUTHORIZED",
    }]:
        raise OperationalResetError("WRITER_FENCE_OWNER_CHANGED")
    if observed["blocking_codes"] != ["MAINTENANCE_ALREADY_ACTIVE"]:
        raise OperationalResetError("REVALIDATION_BLOCKED")
    for key in (
        "plan_digest", "target_digest", "source_revision", "preserved_bindings_digest",
        "dataset_generation", "effect_set", "schema_objects", "foreign_key_findings",
        "external_files", "preserved_external",
    ):
        expected = row["plan_digest"] if key == "plan_digest" else plan[key]
        if observed[key] != expected:
            raise OperationalResetError("REVALIDATION_CHANGED", key)
    if int(observed["dataset_generation"]) != int(row["generation_before"]):
        raise OperationalResetError("DATASET_GENERATION_CONFLICT")
    backup_path = row.get("backup_path")
    backup_digest = row.get("backup_manifest_digest")
    if not isinstance(backup_path, str) or not isinstance(backup_digest, str):
        raise OperationalResetError("BACKUP_BINDING_INVALID")
    verify_backup(
        Path(backup_path), operation_id=operation_id, plan_digest=plan_digest,
        expected_manifest_digest=backup_digest,
    )
    evidence = {
        "operation_id": operation_id, "state": str(row["state"]),
        "writer_fence_owner": operation_id, "actor": actor,
        "target_digest": str(row["target_digest"]),
        "plan_digest": str(row["plan_digest"]),
        "request_digest": str(row["request_digest"]),
        "source_revision": str(row["source_revision"]),
        "implementation_source_revision": str(plan["implementation_source_revision"]),
        "implementation_digest": str(plan["implementation_digest"]),
        "product_version": str(plan["product_version"]),
        "preserved_bindings_digest": str(plan["preserved_bindings_digest"]),
        "backup_manifest_digest": backup_digest,
        "dataset_generation": int(row["generation_before"]),
    }
    evidence["revalidation_digest"] = _digest(evidence)
    result = _public_status(row)
    result["revalidation"] = evidence
    return result


def _archive_effects(data_root: Path, operation_id: str, plan: dict[str, object]) -> None:
    archive = _secure_mkdirs(
        data_root, Path("operational-reset-archive") / operation_id,
        code="ARCHIVE_PATH_UNSAFE",
    )
    expected = list(plan["external_files"])  # type: ignore[arg-type]
    for name in _EFFECT_DIRECTORIES:
        relative = Path(name)
        source, target = data_root / relative, archive / relative
        _secure_mkdirs(archive, relative.parent, code="ARCHIVE_PATH_UNSAFE")
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise OperationalResetError("ARCHIVE_PATH_UNSAFE")
            if source.exists() and any(source.iterdir()):
                raise OperationalResetError("ARCHIVE_RECONCILIATION_AMBIGUOUS", name)
        elif source.exists():
            if source.is_symlink() or not source.is_dir():
                raise OperationalResetError("EXTERNAL_PATH_UNSAFE", name)
            os.replace(source, target)
        source.mkdir(mode=0o700, parents=True, exist_ok=True)
    observed: list[dict[str, object]] = []
    for name in _EFFECT_DIRECTORIES:
        observed.extend(_safe_files(archive / name, name))
    comparable = lambda rows: sorted((row["root"], row["path"], row["size_bytes"], row["sha256"]) for row in rows)
    if comparable(observed) != comparable(expected):
        raise OperationalResetError("ARCHIVE_RECONCILIATION_FAILED")


def _finish_boundary_relative(operation_id: str, generation: int) -> Path:
    return (
        Path("operational-reset-archive") / operation_id / "finish-boundary"
        / f"generation-{generation}"
    )


def _finish_boundary_path(data_root: Path, operation_id: str, generation: int) -> Path:
    return data_root / _finish_boundary_relative(operation_id, generation)


def _finish_boundary_files(boundary: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for name in _EFFECT_DIRECTORIES:
        files.extend(_safe_files(boundary / Path(name), name))
    return files


def _active_root_mode(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OperationalResetError("ACTIVE_ROOT_UNSAFE") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OperationalResetError("ACTIVE_ROOT_UNSAFE")
        return stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _set_active_root_mode(path: Path, mode: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OperationalResetError("ACTIVE_ROOT_UNSAFE") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OperationalResetError("ACTIVE_ROOT_UNSAFE")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError as error:
        raise OperationalResetError("ACTIVE_ROOT_MODE_CHANGE_FAILED") from error
    finally:
        os.close(descriptor)


def _active_roots_state(data_root: Path) -> dict[str, int]:
    return {
        name: _active_root_mode(data_root / Path(name))
        for name in _EFFECT_DIRECTORIES
    }


def _freeze_empty_active_roots(data_root: Path) -> dict[str, int]:
    for name in _EFFECT_DIRECTORIES:
        path = data_root / Path(name)
        if path.is_symlink() or not path.is_dir():
            raise OperationalResetError("ACTIVE_ROOT_UNSAFE", name)
        try:
            if next(path.iterdir(), None) is not None:
                raise OperationalResetError("FINISH_ACTIVE_ROOT_NOT_EMPTY", name)
        except OSError as error:
            raise OperationalResetError("ACTIVE_ROOT_UNSAFE", name) from error
        _set_active_root_mode(path, _FROZEN_ROOT_MODE)
    modes = _active_roots_state(data_root)
    if any(mode != _FROZEN_ROOT_MODE for mode in modes.values()):
        raise OperationalResetError("ACTIVE_ROOT_FREEZE_FAILED")
    return modes


def _verify_frozen_empty_active_roots(data_root: Path) -> dict[str, int]:
    modes = _active_roots_state(data_root)
    if any(mode != _FROZEN_ROOT_MODE for mode in modes.values()):
        raise OperationalResetError("ACTIVE_ROOT_NOT_FROZEN")
    for name in _EFFECT_DIRECTORIES:
        path = data_root / Path(name)
        try:
            if next(path.iterdir(), None) is not None:
                raise OperationalResetError("FINISH_ACTIVE_ROOT_NOT_EMPTY", name)
        except OSError as error:
            raise OperationalResetError("ACTIVE_ROOT_UNSAFE", name) from error
    return modes


def _thaw_active_roots(data_root: Path) -> dict[str, int]:
    # COMPLETED may have been committed just before process loss. Accept an
    # idempotent mix of already-thawed and still-frozen roots, but no other
    # mode or path type.
    modes = _active_roots_state(data_root)
    if any(mode not in {_FROZEN_ROOT_MODE, _ACTIVE_ROOT_MODE} for mode in modes.values()):
        raise OperationalResetError("ACTIVE_ROOT_MODE_INVALID")
    for name, mode in modes.items():
        if mode == _FROZEN_ROOT_MODE:
            _set_active_root_mode(data_root / Path(name), _ACTIVE_ROOT_MODE)
    thawed = _active_roots_state(data_root)
    if any(mode != _ACTIVE_ROOT_MODE for mode in thawed.values()):
        raise OperationalResetError("ACTIVE_ROOT_THAW_FAILED")
    return thawed


def _verify_finish_boundary(
    boundary: Path, *, operation_id: str, generation: int,
    expected_digest: str | None = None,
) -> dict[str, object]:
    if boundary.is_symlink() or not boundary.is_dir():
        raise OperationalResetError("FINISH_BOUNDARY_UNSAFE")
    marker = _bound_regular_file(
        boundary, "manifest.json", code="FINISH_BOUNDARY_INVALID",
    )
    digest = _file_digest(marker)
    if expected_digest is not None and digest != expected_digest:
        raise OperationalResetError("FINISH_BOUNDARY_DIGEST_MISMATCH")
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationalResetError("FINISH_BOUNDARY_INVALID") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("manifest_version") != 1
        or manifest.get("kind") != "EP_OPERATIONAL_RESET_FINISH_BOUNDARY"
        or manifest.get("operation_id") != operation_id
        or manifest.get("dataset_generation") != generation
        or manifest.get("active_roots") != list(_EFFECT_DIRECTORIES)
    ):
        raise OperationalResetError("FINISH_BOUNDARY_INVALID")
    observed = _finish_boundary_files(boundary)
    comparable = lambda rows: sorted(
        (item["root"], item["path"], item["size_bytes"], item["sha256"])
        for item in rows
    )
    expected = manifest.get("preserved_files")
    if not isinstance(expected, list) or comparable(observed) != comparable(expected):
        raise OperationalResetError("FINISH_BOUNDARY_CHANGED")
    return {
        "path": str(boundary), "digest": digest,
        "preserved_file_count": len(observed), "verified": True,
    }


def _rotate_finish_boundary(
    data_root: Path, *, operation_id: str, generation: int, boundary: Path,
) -> dict[str, object]:
    expected = _finish_boundary_path(data_root, operation_id, generation)
    if boundary != expected:
        raise OperationalResetError("FINISH_BOUNDARY_BINDING_MISMATCH")
    _secure_mkdirs(
        data_root, _finish_boundary_relative(operation_id, generation),
        code="FINISH_BOUNDARY_UNSAFE",
    )
    marker = boundary / "manifest.json"
    if marker.exists() or marker.is_symlink():
        _freeze_empty_active_roots(data_root)
        return _verify_finish_boundary(
            boundary, operation_id=operation_id, generation=generation,
        )
    for name in _EFFECT_DIRECTORIES:
        relative = Path(name)
        source = data_root / relative
        target = boundary / relative
        _secure_mkdirs(data_root, relative.parent, code="FINISH_BOUNDARY_UNSAFE")
        _secure_mkdirs(boundary, relative.parent, code="FINISH_BOUNDARY_UNSAFE")
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise OperationalResetError("FINISH_BOUNDARY_UNSAFE")
        else:
            if source.exists() or source.is_symlink():
                if source.is_symlink() or not source.is_dir():
                    raise OperationalResetError("EXTERNAL_PATH_UNSAFE", name)
            else:
                _secure_mkdirs(data_root, relative, code="FINISH_BOUNDARY_UNSAFE")
            try:
                os.rename(source, target)
            except OSError as error:
                raise OperationalResetError("FINISH_BOUNDARY_ROTATION_FAILED", name) from error
        # A path lookup after the atomic rename belongs to the new generation;
        # writers with an already-open directory descriptor remain isolated in
        # the archived inode. The replacement stays frozen through the DB
        # COMPLETED commit, closing the post-rename/pre-commit race.
        _secure_mkdirs(data_root, relative, code="FINISH_BOUNDARY_UNSAFE")
        _set_active_root_mode(source, _FROZEN_ROOT_MODE)
    _verify_frozen_empty_active_roots(data_root)
    manifest = {
        "manifest_version": 1, "kind": "EP_OPERATIONAL_RESET_FINISH_BOUNDARY",
        "operation_id": operation_id, "dataset_generation": generation,
        "active_roots": list(_EFFECT_DIRECTORIES),
        "preserved_files": _finish_boundary_files(boundary),
    }
    manifest_bytes = _canonical(manifest) + b"\n"
    temporary = boundary / "manifest.json.partial"
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or not temporary.is_file():
            raise OperationalResetError("FINISH_BOUNDARY_DESTINATION_CONFLICT")
        # This exact operation-owned staging name cannot predate the durable
        # boundary intent.  Recover both a fully written pre-rename marker and
        # an interrupted partial write without touching any archived payload.
        if temporary.read_bytes() == manifest_bytes:
            os.rename(temporary, marker)
            return _verify_finish_boundary(
                boundary, operation_id=operation_id, generation=generation,
            )
        temporary.unlink()
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise OperationalResetError("FINISH_BOUNDARY_DESTINATION_CONFLICT") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if marker.exists() or marker.is_symlink():
            raise OperationalResetError("FINISH_BOUNDARY_DESTINATION_CONFLICT")
        os.rename(temporary, marker)
        directory_descriptor = os.open(boundary, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return _verify_finish_boundary(
        boundary, operation_id=operation_id, generation=generation,
    )


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
        actor = _actor(root)
        database = root / central_database.DATABASE_FILENAME
        with _central_connection(database) as connection:
            row = _operation(connection, operation_id)
        if row is None or row["plan_digest"] != plan_digest:
            raise OperationalResetError("AUTHORIZED_OPERATION_NOT_FOUND")
        if row["actor"] != actor:
            raise OperationalResetError("OPERATOR_AUTHORITY_CHANGED")
        state = str(row["state"])
        if state == "PREPARING":
            raise OperationalResetError("BACKUP_NOT_AUTHORIZED")
        if state not in _APPLY_ENTRY_STATES | _APPLY_IDEMPOTENT_STATES:
            raise OperationalResetError("OPERATION_STATE_INVALID_FOR_APPLY")
        if not isinstance(row.get("backup_path"), str) or not isinstance(
            row.get("backup_manifest_digest"), str
        ):
            raise OperationalResetError("BACKUP_BINDING_INVALID")
        verify_backup(
            Path(str(row["backup_path"])), operation_id=operation_id,
            plan_digest=plan_digest,
            expected_manifest_digest=str(row["backup_manifest_digest"]),
        )
        if state in _APPLY_IDEMPOTENT_STATES:
            return _public_status(row)
        plan = json.loads(str(row["plan_json"]))
        if state == "AUTHORIZED":
            revalidate(root, operation_id=operation_id, plan_digest=plan_digest)
            with _central_connection(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _transition(connection, operation_id, "AUTHORIZED", "ARTIFACTS_ARCHIVING")
                connection.execute("COMMIT")
            state = "ARTIFACTS_ARCHIVING"
        if state != "ARTIFACTS_ARCHIVING" and state != "ARTIFACTS_ARCHIVED":
            raise OperationalResetError("OPERATION_STATE_INVALID_FOR_APPLY")
        _archive_effects(root, operation_id, plan)
        if state == "ARTIFACTS_ARCHIVING":
            with _central_connection(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _transition(connection, operation_id, "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED")
                connection.execute("COMMIT")
        verify_backup(
            Path(str(row["backup_path"])), operation_id=operation_id,
            plan_digest=plan_digest,
            expected_manifest_digest=str(row["backup_manifest_digest"]),
        )
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = _operation(connection, operation_id)
                if current is None or current["state"] != "ARTIFACTS_ARCHIVED":
                    raise OperationalResetError("OPERATION_STATE_TRANSITION_CONFLICT")
                if _actor(root) != row["actor"]:
                    raise OperationalResetError("OPERATOR_AUTHORITY_CHANGED")
                tables = _tables(connection)
                if tables != MAPPED_TABLES:
                    raise OperationalResetError("TABLE_CLASSIFICATION_INCOMPLETE")
                active = [
                    (str(item[0]), str(item[1])) for item in connection.execute(
                        "SELECT operation_id,state FROM ep_operational_reset_operations "
                        "WHERE state NOT IN ('COMPLETED','ABORTED') ORDER BY operation_id"
                    )
                ]
                if active != [(operation_id, "ARTIFACTS_ARCHIVED")]:
                    raise OperationalResetError("WRITER_FENCE_OWNER_CHANGED")
                identity = _identity(root, connection)
                if identity != plan["target"]:
                    raise OperationalResetError("TARGET_IDENTITY_CONFLICT")
                if _logical_digest(connection, tables) != row["source_revision"]:
                    raise OperationalResetError("SOURCE_REVISION_CHANGED")
                if _logical_digest(connection, tables, preserved_only=True) != plan["preserved_bindings_digest"]:
                    raise OperationalResetError("PRESERVED_BINDINGS_CHANGED")
                if _schema_objects(connection) != plan["schema_objects"]:
                    raise OperationalResetError("SCHEMA_OBJECTS_CHANGED")
                if (
                    plan.get("product_version") != CURRENT_PLATFORM_VERSION
                    or plan.get("implementation_source_revision") != _implementation_source_revision()
                    or plan.get("implementation_digest") != _implementation_digest()
                ):
                    raise OperationalResetError("IMPLEMENTATION_PROVENANCE_CHANGED")
                generation_before = int(connection.execute(
                    "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
                ).fetchone()[0])
                if generation_before != int(row["generation_before"]):
                    raise OperationalResetError("DATASET_GENERATION_CONFLICT")
                # Immutable-evidence and maintenance-block triggers are removed and
                # recreated inside this one uncommitted transaction. Other writers
                # cannot observe an unfenced schema window.
                triggers = [
                    (str(name), str(sql)) for name, sql in connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ("
                        + ",".join("?" for _ in (OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION)) + ")",
                        tuple(sorted(OPERATIONAL_HISTORY | DERIVED_CACHE_OR_PROJECTION)),
                    ).fetchall() if sql is not None
                ]
                for name, _sql in triggers:
                    connection.execute(f"DROP TRIGGER {_quote(name)}")
                _record_tombstones(connection, operation_id)
                _delete_operational(connection)
                generation = int(row["generation_before"]) + 1
                cursor = connection.execute(
                    "UPDATE ep_operational_dataset_state SET generation=?,updated_at=? "
                    "WHERE singleton=1 AND generation=?",
                    (generation, _utcnow(), row["generation_before"]),
                )
                if cursor.rowcount != 1:
                    raise OperationalResetError("DATASET_GENERATION_CONFLICT")
                for _name, sql in triggers:
                    connection.execute(sql)
                if list(connection.execute("PRAGMA foreign_key_check")):
                    raise OperationalResetError("POST_RESET_FOREIGN_KEY_FAILED")
                _transition(
                    connection, operation_id, "ARTIFACTS_ARCHIVED", "DB_APPLIED",
                    "generation_after=?", (generation,),
                )
                connection.execute("COMMIT")
                return _public_status(_operation(connection, operation_id) or {})
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise


def _verify_under_lock(
    root: Path, *, operation_id: str, plan_digest: str, promote: bool,
    allow_boundary_recovery: bool = False,
) -> dict[str, object]:
    database = root / central_database.DATABASE_FILENAME
    with _central_connection(database) as connection:
        row = _operation(connection, operation_id)
        if (
            row is None or row["plan_digest"] != plan_digest
            or row["state"] not in {"DB_APPLIED", "VERIFIED", "COMPLETED"}
        ):
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
        expected_installation_id = str(_identity(root, connection)["instance_id"])
    archive = root / "operational-reset-archive" / operation_id
    boundary: dict[str, object] | None = None
    boundary_path_value = row.get("finish_boundary_path")
    frozen_modes: dict[str, int] | None = None
    if boundary_path_value is None:
        if allow_boundary_recovery:
            raise OperationalResetError("FINISH_BOUNDARY_RECOVERY_INVALID")
        _archive_effects(root, operation_id, plan)
    else:
        expected_boundary = _finish_boundary_path(root, operation_id, generation)
        if str(expected_boundary) != boundary_path_value:
            raise OperationalResetError("FINISH_BOUNDARY_BINDING_MISMATCH")
        marker = expected_boundary / "manifest.json"
        if marker.exists() or marker.is_symlink():
            boundary = _verify_finish_boundary(
                expected_boundary, operation_id=operation_id, generation=generation,
                expected_digest=(
                    str(row["finish_boundary_digest"])
                    if row.get("finish_boundary_digest") is not None else None
                ),
            )
        elif row["state"] == "COMPLETED":
            raise OperationalResetError("FINISH_BOUNDARY_INVALID")
        if not allow_boundary_recovery:
            frozen_modes = _verify_frozen_empty_active_roots(root)
    external, _preserved, unknown, active_controls = _external_inventory(
        root, expected_installation_id=expected_installation_id,
    )
    backup = verify_backup(
        Path(str(row["backup_path"])), operation_id=operation_id, plan_digest=plan_digest,
        expected_manifest_digest=str(row["backup_manifest_digest"]),
    )
    failures = {table: count for table, count in remaining.items() if count}
    result = {
        "quick_check": quick, "foreign_key_errors": len(foreign_keys),
        "operational_rows_remaining": failures,
        "preserved_bindings_digest": preserved_digest,
        "expected_preserved_bindings_digest": plan["preserved_bindings_digest"],
        "dataset_generation": generation, "expected_generation": row["generation_after"],
        "project_registrations": projects, "repository_registrations": repositories,
        "consumer_credentials": credentials, "backup": backup,
        "active_artifact_roots_empty": not external,
        "active_ingest_controls": active_controls,
        "unknown_external_paths": unknown,
        "archive_path": str(archive),
        "finish_boundary": boundary,
        "active_root_modes": frozen_modes,
    }
    if (
        quick != ["ok"] or foreign_keys or failures
        or preserved_digest != plan["preserved_bindings_digest"]
        or generation != row["generation_after"]
        or (external and not allow_boundary_recovery) or active_controls or unknown
    ):
        raise OperationalResetError("POST_RESET_VERIFICATION_FAILED")
    if promote and row["state"] == "DB_APPLIED":
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _transition(
                connection, operation_id, "DB_APPLIED", "VERIFIED",
                "verification_json=?", (json.dumps(result, sort_keys=True),),
            )
            connection.execute("COMMIT")
    return result


def _verify_completed_finish_binding(
    root: Path, *, operation_id: str, plan_digest: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Verify immutable finish proof without reasserting old-generation emptiness."""
    database = root / central_database.DATABASE_FILENAME
    with _central_connection(database) as connection:
        row = _operation(connection, operation_id)
        if (
            row is None or row["state"] != "COMPLETED"
            or row["plan_digest"] != plan_digest
        ):
            raise OperationalResetError("COMPLETED_OPERATION_BINDING_INVALID")
        try:
            plan = json.loads(str(row["plan_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OperationalResetError("COMPLETED_OPERATION_BINDING_INVALID") from error
        generation = int(connection.execute(
            "SELECT generation FROM ep_operational_dataset_state WHERE singleton=1"
        ).fetchone()[0])
        identity = _identity(root, connection)
        quick = [str(item[0]) for item in connection.execute("PRAGMA quick_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    expected_boundary = _finish_boundary_path(root, operation_id, generation)
    digest = row.get("finish_boundary_digest")
    if (
        generation != row.get("generation_after")
        or plan.get("target_digest") != row.get("target_digest")
        or _digest(identity) != row.get("target_digest")
        or row.get("finish_boundary_path") != str(expected_boundary)
        or not isinstance(digest, str)
        or quick != ["ok"] or foreign_keys
    ):
        raise OperationalResetError("COMPLETED_OPERATION_BINDING_INVALID")
    boundary = _verify_finish_boundary(
        expected_boundary, operation_id=operation_id, generation=generation,
        expected_digest=digest,
    )
    modes = _active_roots_state(root)
    if any(mode not in {_FROZEN_ROOT_MODE, _ACTIVE_ROOT_MODE} for mode in modes.values()):
        raise OperationalResetError("ACTIVE_ROOT_MODE_INVALID")
    return row, {
        "state": "COMPLETED", "operation_id": operation_id,
        "dataset_generation": generation, "target_digest": row["target_digest"],
        "finish_boundary": boundary, "active_root_modes": modes,
        "quick_check": quick, "foreign_key_errors": 0,
    }


def verify(data_root: Path, *, operation_id: str, plan_digest: str) -> dict[str, object]:
    root = _trusted_directory(data_root, code="DATA_ROOT_UNSAFE")
    with _operation_lock(root, operation_id):
        _actor(root)
        with _central_connection(root / central_database.DATABASE_FILENAME) as connection:
            row = _operation(connection, operation_id)
        if row is not None and row.get("state") == "COMPLETED":
            _row, proof = _verify_completed_finish_binding(
                root, operation_id=operation_id, plan_digest=plan_digest,
            )
            return proof
        return _verify_under_lock(
            root, operation_id=operation_id, plan_digest=plan_digest, promote=True,
        )


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
                completed, _proof = _verify_completed_finish_binding(
                    root, operation_id=operation_id, plan_digest=plan_digest,
                )
                _thaw_active_roots(root)
                return _public_status(completed)
            if row["state"] != "VERIFIED":
                raise OperationalResetError("VERIFICATION_REQUIRED_BEFORE_FINISH")
        # First re-prove the exact database, backup, bindings and active routes.
        # If a prior crash already persisted the boundary intent this is a
        # recovery verification and the still-fenced active paths may contain
        # post-boundary arrivals.
        _verify_under_lock(
            root, operation_id=operation_id, plan_digest=plan_digest, promote=False,
            allow_boundary_recovery=row.get("finish_boundary_path") is not None,
        )
        generation = int(row["generation_after"])
        boundary = _finish_boundary_path(root, operation_id, generation)
        if row.get("finish_boundary_path") is None:
            if boundary.exists() or boundary.is_symlink():
                raise OperationalResetError("FINISH_BOUNDARY_DESTINATION_CONFLICT")
            with _central_connection(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _transition(
                    connection, operation_id, "VERIFIED", "VERIFIED",
                    "finish_boundary_path=?", (str(boundary),),
                )
                connection.execute("COMMIT")
            row["finish_boundary_path"] = str(boundary)
        elif row["finish_boundary_path"] != str(boundary):
            raise OperationalResetError("FINISH_BOUNDARY_BINDING_MISMATCH")
        boundary_proof = _rotate_finish_boundary(
            root, operation_id=operation_id, generation=generation, boundary=boundary,
        )
        verification = _verify_under_lock(
            root, operation_id=operation_id, plan_digest=plan_digest, promote=False,
        )
        if verification.get("finish_boundary") != boundary_proof:
            raise OperationalResetError("FINISH_BOUNDARY_CHANGED")
        with _central_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _transition(
                connection, operation_id, "VERIFIED", "COMPLETED",
                "finish_boundary_digest=?,verification_json=?",
                (boundary_proof["digest"], json.dumps(verification, sort_keys=True)),
            )
            connection.execute("COMMIT")
            completed = _operation(connection, operation_id) or {}
        _thaw_active_roots(root)
        return _public_status(completed)


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
        with _operation_lock(root, operation_id):
            _actor(root)
            with _central_connection(root / central_database.DATABASE_FILENAME) as connection:
                row = _operation(connection, operation_id) or {}
                if row.get("state") != "PREPARING" or row.get("plan_digest") != plan_digest:
                    raise OperationalResetError("OPERATION_STATE_INVALID_FOR_RESUME")
                plan = json.loads(str(row["plan_json"]))
            requested_backup_root = str(backup_root.expanduser().resolve(strict=False))
            if requested_backup_root != row.get("backup_root"):
                raise OperationalResetError("BACKUP_ROOT_BINDING_MISMATCH")
            backup = _backup(root, operation_id, backup_root, plan)
            with _central_connection(root / central_database.DATABASE_FILENAME) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _transition(
                    connection, operation_id, "PREPARING", "AUTHORIZED",
                    "backup_path=?,backup_sha256=?,backup_manifest_digest=?",
                    (backup["backup_path"], backup["backup_sha256"],
                     backup["backup_manifest_digest"]),
                )
                connection.execute("COMMIT")
        state = "AUTHORIZED"
    if state in {"AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED"}:
        apply(data_root, operation_id=operation_id, plan_digest=plan_digest)
        state = "DB_APPLIED"
    if state == "DB_APPLIED":
        verify(data_root, operation_id=operation_id, plan_digest=plan_digest)
    elif state == "COMPLETED":
        # A process may have died after the durable completion commit but
        # before thawing the new-generation ingest roots. The same operation
        # verifies its immutable boundary and idempotently finishes the thaw.
        finish(data_root, operation_id=operation_id, plan_digest=plan_digest)
    elif state not in {"VERIFIED", "COMPLETED"}:
        raise OperationalResetError("OPERATION_STATE_INVALID_FOR_RESUME")
    return status(data_root, operation_id=operation_id)


def contract_readback(
    data_root: Path, *, command: str, operation_id: str | None,
    revalidation_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project the shared operator contract from persisted, public state only.

    Command return values can contain implementation diagnostics or protected
    configuration.  They are deliberately not accepted by this projection and
    can therefore never be copied to stdout by the maintenance CLI.
    """
    observed = preview(data_root)
    persisted: object = None
    if operation_id is not None:
        persisted = status(data_root, operation_id=operation_id).get("operation")
    operation = persisted if isinstance(persisted, dict) else {}
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
                expected_manifest_digest=(
                    str(operation["backup_manifest_digest"])
                    if isinstance(operation.get("backup_manifest_digest"), str) else None
                ),
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
    revalidation_evidence: dict[str, object] | None = None
    if command == "revalidate":
        candidate = None if revalidation_result is None else revalidation_result.get("revalidation")
        if not isinstance(candidate, Mapping):
            raise OperationalResetError("REVALIDATION_EVIDENCE_INVALID")
        allowed_keys = {
            "operation_id", "state", "writer_fence_owner", "actor", "target_digest",
            "plan_digest", "request_digest", "source_revision", "preserved_bindings_digest",
            "backup_manifest_digest", "dataset_generation", "implementation_source_revision",
            "implementation_digest", "product_version", "revalidation_digest",
        }
        if set(candidate) != allowed_keys:
            raise OperationalResetError("REVALIDATION_EVIDENCE_INVALID")
        revalidation_evidence = dict(candidate)
        blockers = []
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
        "details": {
            "credentials_included_in_receipt": False,
            "projection": "PERSISTED_PUBLIC_MAINTENANCE_STATE",
            **({"revalidation": revalidation_evidence} if revalidation_evidence is not None else {}),
        },
    }


class _ReceiptArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OperationalResetError("CLI_ARGUMENT_INVALID")


def _error_receipt(command: str, operation_id: str | None, code: str) -> dict[str, object]:
    """Return the shared contract shape without paths, exception text or secrets."""
    return {
        "contract_version": "operational-reset-v1", "product": "engineering-platform",
        "command": command, "operation_id": operation_id, "state": "ERROR", "allowed": False,
        "target": {
            "instance_id": None, "database_path": None, "database_identity": None,
            "schema_version": None,
        },
        "profile": PROFILE, "dataset_generation": None, "plan_digest": None,
        "relevant_revision_digest": None, "backup": None, "counts": {},
        "blockers": [code], "integrity": {"quick_check": [], "foreign_key_findings": []},
        "preserved_bindings_digest": None, "error": code, "error_code": code,
        "details": {"credentials_included_in_receipt": False},
    }


def main(argv: list[str] | None = None) -> int:
    parser = _ReceiptArgumentParser(prog="engineering-platform-maintenance")
    parser.add_argument(
        "command",
        choices=("preview", "prepare", "revalidate", "apply", "status", "resume", "verify", "finish", "abort"),
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--plan-digest")
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--allow-operational-fk", action="append", default=[])
    raw = list(argv) if argv is not None else []
    command = raw[0] if raw and not raw[0].startswith("-") else "unknown"
    operation_id: str | None = None
    try:
        args = parser.parse_args(argv)
        command = args.command
        operation_id = args.operation_id
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
            elif args.command == "revalidate":
                result = revalidate(
                    args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest,
                )
            elif args.command == "resume":
                result = resume(args.data_root, operation_id=args.operation_id,
                                 plan_digest=args.plan_digest, backup_root=args.backup_root)
            elif args.command == "verify":
                result = verify(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
            elif args.command == "abort":
                result = abort(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
            else:
                result = finish(args.data_root, operation_id=args.operation_id, plan_digest=args.plan_digest)
        # Do not serialize ``result``.  The shared receipt is reconstructed
        # from the schema-owned public projection after the command completes.
        print(json.dumps(contract_readback(
            args.data_root, command=args.command, operation_id=args.operation_id,
            revalidation_result=result if args.command == "revalidate" else None,
        ), sort_keys=True))
        return 0
    except OperationalResetError as error:
        print(json.dumps(_error_receipt(command, operation_id, error.code), sort_keys=True))
        return 2
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps(
            _error_receipt(command, operation_id, "MAINTENANCE_COMMAND_FAILED"), sort_keys=True,
        ))
        return 2
    except Exception:
        print(json.dumps(
            _error_receipt(command, operation_id, "INTERNAL_MAINTENANCE_ERROR"), sort_keys=True,
        ))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
