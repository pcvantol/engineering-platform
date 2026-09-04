"""Standalone Engineering Platform Server foundation.

This module intentionally owns no project, Agent transport, credential, or
execution authority.  It is the installation-owned runtime boundary on which
those later capabilities can be composed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
# The lifecycle starts this module with a fixed argv; no shell is used.
import subprocess  # nosec B404
import sys
import time
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import SplitResult, parse_qs, parse_qsl, urlencode, urlsplit
from uuid import uuid4

from . import agent_trust
from . import central_database
from . import console_route_ownership
from . import dashboard
from . import dashboard_translation
from . import file_inbox
from . import local_repository_binding
from . import project_topology
from . import submission_service
from . import storage
from . import managed_codex_runtime
from . import provider_readiness
from .lifecycle_worker import LifecycleWorker, WORKER_RUNNING
from .parity_lifecycle_dispatcher import ParityLifecycleDispatchError, dismiss_operator_gate, retry_operator_gate
from .local_api_credentials import verifier
from .parity_context import ParityProjectStore, project_context
from .platform_version import EngineeringPlatformManifest
from .providers import (
    MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT,
    LocalProcessProvider,
    default_engineering_platform_codex_cli_prefix,
)
from .resources import package_path


SERVER_CONFIGURATION_FILENAME = "server.json"
SERVER_IDENTITY_FILENAME = "runtime-identity.json"
SERVER_RUNTIME_FILENAME = "runtime.json"
SERVER_DATABASE_FILENAME = "engineering.db"
SERVER_CONFIGURATION_VERSION = 2
# ADR-0026 defines the first standalone store as the canonical schema-40
# product definitions plus immutable control provenance.  This server-owned
# bootstrap is deliberately separate from the retired DJConnect migration
# machinery: it creates a clean installation only and never accepts a source
# database path.
SERVER_STORE_SCHEMA_VERSION = 49
SERVER_ENVIRONMENT_DATA_ROOT = "EP_SERVER_DATA_ROOT"
FILE_INBOX_DIRECTORY = "file-inbox"
PLATFORM_COMPONENT_DEFINITIONS: dict[str, dict[str, str]] = {
    "ep_server": {"kind": "DAEMON", "active": "EP_SERVER_ACTIVE", "inactive": "EP_SERVER_UNAVAILABLE", "detail": "EP_SERVER_ENDPOINT"},
    "platform_database": {"kind": "STORAGE", "active": "PLATFORM_DATABASE_HEALTHY", "inactive": "PLATFORM_DATABASE_UNAVAILABLE", "detail": "PLATFORM_DATABASE_STORAGE"},
    "lifecycle_worker": {"kind": "IN_PROCESS_COMPONENT", "active": "LIFECYCLE_WORKER_ACTIVE", "inactive": "LIFECYCLE_WORKER_UNAVAILABLE", "detail": "LIFECYCLE_WORKER_SERVER_HOSTED"},
    "operations_console": {"kind": "UI_SERVICE", "active": "OPERATIONS_CONSOLE_AVAILABLE", "inactive": "OPERATIONS_CONSOLE_UNAVAILABLE", "detail": "OPERATIONS_CONSOLE_SERVER_NATIVE"},
    "dashboard_relay": {"kind": "UI_SERVICE", "active": "DASHBOARD_RELAY_ACTIVE", "inactive": "DASHBOARD_RELAY_UNAVAILABLE", "detail": "DASHBOARD_RELAY_SERVER_NATIVE"},
}
PLATFORM_COMPONENT_IDS = frozenset((*PLATFORM_COMPONENT_DEFINITIONS, "http_ingress", "cli_ingress", "file_inbox_ingress"))
_CHILDREN: dict[int, subprocess.Popen[object]] = {}
_SAFE_ATTACHMENT_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_REPORT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _attachment_content_disposition(filename: object) -> str:
    """Build a fail-closed attachment header from a bounded ASCII filename.

    Route validation is not a response-header security boundary.  This helper
    rejects control characters and all non-allowlisted filenames before a
    value reaches ``BaseHTTPRequestHandler.send_header``.
    """
    if not isinstance(filename, str):
        raise ValueError("attachment filename is invalid")
    sanitized = filename.replace("\r", "").replace("\n", "")
    if sanitized != filename or not _SAFE_ATTACHMENT_FILENAME.fullmatch(sanitized):
        raise ValueError("attachment filename is invalid")
    return f'attachment; filename="{sanitized}"'


def _report_content_disposition(report_id: object) -> str:
    """Compose the report filename only after independently validating its id."""
    if not isinstance(report_id, str) or not _SAFE_REPORT_ID.fullmatch(report_id):
        raise ValueError("report identifier is invalid")
    return _attachment_content_disposition(f"engineering-report-{report_id}.md")


class ServerConfigurationError(ValueError):
    """Raised when an installation-owned server configuration is invalid."""


SERVER_REQUIRED_TABLES = frozenset(
    {
        "engineering_schema_migrations",
        "engineering_metadata",
        "ep_installations",
        "ep_control_provenance",
        "local_api_credentials",
        "local_api_consumer_registrations",
        "ep_project_registrations",
        "ep_execution_runs",
        "ep_execution_leases",
        "prompt_execution_history",
        "ep_agent_registrations",
        "ep_agent_pairing_codes",
        "ep_repository_registrations",
        "ep_agent_repository_attachments",
        "ep_local_repository_bindings",
        "ep_submissions",
        "ep_submission_events",
        "ep_submission_prompt_history",
        "ep_parity_lifecycle_dispatches",
        "engineering_transactions",
        "execution_lifecycle_events",
    }
)
SERVER_REQUIRED_INDEXES = frozenset(
    {
        "local_api_credentials_scope_lookup",
        "local_api_consumer_registrations_status_lookup",
        "ep_project_registrations_status_lookup",
        "ep_execution_runs_project_lookup",
        "ep_control_provenance_subject_lookup",
        "ep_repository_registrations_project_lookup",
        "ep_agent_repository_attachments_repository_lookup",
        "ep_local_repository_bindings_repository_lookup",
        "ep_submissions_project_lookup",
        "ep_submissions_idempotency_lookup",
        "ep_parity_lifecycle_dispatches_run_lookup",
    }
)


@dataclass(frozen=True)
class ServerConfiguration:
    version: int
    bind_host: str
    bind_port: int
    managed_codex_cli_prefix: str

    @classmethod
    def load(cls, data_root: Path) -> "ServerConfiguration":
        path = data_root / SERVER_CONFIGURATION_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server configuration is unavailable.") from error
        if not isinstance(raw, dict):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        legacy_keys = {"version", "bind_host", "bind_port"}
        current_keys = legacy_keys | {"managed_codex_cli_prefix"}
        if set(raw) == legacy_keys and raw.get("version") == 1:
            prefix = str(default_engineering_platform_codex_cli_prefix())
        elif set(raw) == current_keys and raw.get("version") == SERVER_CONFIGURATION_VERSION:
            prefix = raw.get("managed_codex_cli_prefix")
        else:
            raise ServerConfigurationError("EP Server configuration is invalid.")
        candidate = Path(prefix).expanduser() if isinstance(prefix, str) else None
        if (
            not isinstance(raw["bind_host"], str)
            or raw["bind_host"] != "127.0.0.1"
            or not isinstance(raw["bind_port"], int)
            or not 1 <= raw["bind_port"] <= 65535
            or candidate is None
            or not candidate.is_absolute()
        ):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        return cls(int(raw["version"]), raw["bind_host"], raw["bind_port"], str(candidate.resolve(strict=False)))


@dataclass(frozen=True)
class RuntimeIdentity:
    instance_id: str
    created_at: str


@dataclass(frozen=True)
class AgentRegistrationRequest:
    """Transport-neutral future Agent registration input.

    B3 deliberately does not define authentication, enrollment persistence,
    project attachment, or any network representation for this request.
    """

    agent_id: str
    agent_kind: str
    capabilities: tuple[str, ...]


class AgentRegistrationIntake(Protocol):
    """Future internal extension point; no transport/auth contract is implied."""

    def accept(self, request: AgentRegistrationRequest) -> None: ...


def default_data_root() -> Path:
    override = os.environ.get(SERVER_ENVIRONMENT_DATA_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Engineering Platform Server"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Engineering Platform Server"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "engineering-platform-server"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _schema_version(connection: sqlite3.Connection) -> int:
    if "engineering_schema_migrations" not in _table_names(connection):
        return 0
    row = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _install_schema_41(connection: sqlite3.Connection, identity: RuntimeIdentity) -> None:
    """Install the clean standalone schema and immutable control provenance."""
    for statement in (
        "CREATE TABLE IF NOT EXISTS engineering_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS engineering_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version=41))",
        "CREATE TABLE IF NOT EXISTS ep_control_provenance (event_id INTEGER PRIMARY KEY, event_kind TEXT NOT NULL CHECK(event_kind IN ('INSTALLATION_CREATED','CREDENTIAL_LIFECYCLE','CONSUMER_REGISTRATION','PROJECT_SCOPE_MUTATION')), subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_control_provenance_subject_lookup ON ep_control_provenance(subject_kind,subject_id,event_id DESC)",
        "CREATE TABLE IF NOT EXISTS local_api_credentials (credential_id TEXT PRIMARY KEY CHECK(length(credential_id) BETWEEN 1 AND 128), consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32), fingerprint BLOB NOT NULL UNIQUE CHECK(length(fingerprint)=32), issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, replaced_by_credential_id TEXT REFERENCES local_api_credentials(credential_id))",
        "CREATE INDEX IF NOT EXISTS local_api_credentials_scope_lookup ON local_api_credentials(consumer_id,project_id,revoked_at)",
        "CREATE TABLE IF NOT EXISTS local_api_consumer_registrations (consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, disabled_at TEXT, revoked_at TEXT, audit_metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(consumer_id,project_id))",
        "CREATE INDEX IF NOT EXISTS local_api_consumer_registrations_status_lookup ON local_api_consumer_registrations(consumer_id,project_id,status)",
        "CREATE TABLE IF NOT EXISTS ep_project_registrations (project_id TEXT PRIMARY KEY CHECK(length(project_id) BETWEEN 1 AND 128), attachment_contract TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_project_registrations_status_lookup ON ep_project_registrations(status,project_id)",
        "CREATE TABLE IF NOT EXISTS ep_execution_runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id), state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_execution_runs_project_lookup ON ep_execution_runs(project_id,state,created_at DESC)",
        "CREATE TABLE IF NOT EXISTS ep_execution_leases (lease_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_execution_runs(run_id), holder_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT)",
        "CREATE TABLE IF NOT EXISTS prompt_execution_history (run_id TEXT PRIMARY KEY REFERENCES ep_execution_runs(run_id), prompt_digest TEXT NOT NULL, recorded_at TEXT NOT NULL)",
    ):
        connection.execute(statement)
    agent_trust.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(41)")
    connection.execute("INSERT OR IGNORE INTO engineering_metadata(key,value) VALUES('installation.instance_id',?)", (identity.instance_id,))
    connection.execute("INSERT OR IGNORE INTO engineering_metadata(key,value) VALUES('installation.schema_version','41')")
    connection.execute("INSERT OR IGNORE INTO ep_installations(instance_id,created_at,schema_version) VALUES(?,?,41)", (identity.instance_id, identity.created_at))
    connection.execute("INSERT OR IGNORE INTO ep_control_provenance(event_kind,subject_kind,subject_id,payload,recorded_at) VALUES('INSTALLATION_CREATED','installation',?,?,?)", (identity.instance_id, json.dumps({'schema_version': 41}, sort_keys=True), identity.created_at))
    for table in ("ep_control_provenance",):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, '{table} evidence is immutable.'); END")


def _migrate_schema_42(connection: sqlite3.Connection) -> None:
    """Forward-only topology extension; schema-41 structures remain intact."""
    # Schema 41 deliberately constrained the bootstrap record to 41.  Preserve
    # its row while widening that bootstrap-only constraint for official
    # forward migrations; no operational rows are rewritten.
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema41")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,42 FROM ep_installations_schema41")
    connection.execute("DROP TABLE ep_installations_schema41")
    project_topology.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(42)")
    connection.execute("UPDATE engineering_metadata SET value='42' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=42")


def _migrate_schema_43(connection: sqlite3.Connection) -> None:
    """Add CENTRAL-owned canonical submission persistence.

    This is deliberately a forward migration from schema 42; historical
    schema-40 execution databases are neither inspected nor imported.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema42")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,43 FROM ep_installations_schema42")
    connection.execute("DROP TABLE ep_installations_schema42")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        producer_id TEXT NOT NULL, producer_type TEXT NOT NULL, producer_version TEXT,
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL,
        idempotency_key TEXT, correlation_id TEXT, mission_id TEXT, engineering_action_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED')), admission TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE TABLE ep_submission_events (event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id), event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE ep_submission_prompt_history (submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(43)")
    connection.execute("UPDATE engineering_metadata SET value='43' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=43")


def _migrate_schema_44(connection: sqlite3.Connection) -> None:
    """Add the private, explicit Phase-P local checkout binding surface."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema43")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,44 FROM ep_installations_schema43")
    connection.execute("DROP TABLE ep_installations_schema43")
    local_repository_binding.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(44)")
    connection.execute("UPDATE engineering_metadata SET value='44' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=44")


def _migrate_schema_45(connection: sqlite3.Connection) -> None:
    """Add the single-writer CENTRAL-to-historical lifecycle association."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema44")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,45 FROM ep_installations_schema44")
    connection.execute("DROP TABLE ep_installations_schema44")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
        prompt_path TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(45)")
    connection.execute("UPDATE engineering_metadata SET value='45' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=45")


def _migrate_schema_46(connection: sqlite3.Connection) -> None:
    """Persist the admitted execution mode with the CENTRAL run.

    The mode is decided before a submission is claimed.  It is therefore run
    evidence, rather than a presentation value to be rediscovered from a
    mutable prompt or a repository-local telemetry row.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema45")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,46 FROM ep_installations_schema45")
    connection.execute("DROP TABLE ep_installations_schema45")
    # Existing CENTRAL runs predate this evidence field.  Keep them NULL so
    # the Console accurately reports that their mode was not recorded, rather
    # than silently inventing MANAGED during migration.
    connection.execute("ALTER TABLE ep_execution_runs ADD COLUMN execution_mode TEXT CHECK(execution_mode IN ('MANAGED','GENESIS'))")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(46)")
    connection.execute("UPDATE engineering_metadata SET value='46' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=46")


def _migrate_schema_47(connection: sqlite3.Connection) -> None:
    """Keep failed project runs FIFO-blocking until CENTRAL records a resolution."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema46")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,47 FROM ep_installations_schema46")
    connection.execute("DROP TABLE ep_installations_schema46")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches ADD COLUMN operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED'))")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches ADD COLUMN resolution_submission_id TEXT REFERENCES ep_submissions(submission_id)")
    connection.execute("UPDATE ep_parity_lifecycle_dispatches SET operator_resolution='OPEN' WHERE state IN ('BLOCKED','FAILED')")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(47)")
    connection.execute("UPDATE engineering_metadata SET value='47' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=47")


def _migrate_schema_48(connection: sqlite3.Connection) -> None:
    """Move retained lifecycle persistence into the one CENTRAL database.

    No repository is opened or scanned.  These are empty compatibility tables
    for new standalone runs while the preserved runner is being invoked via
    the explicit CENTRAL operational context.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema47")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48)))"
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) "
        "SELECT instance_id,created_at,48 FROM ep_installations_schema47"
    )
    connection.execute("DROP TABLE ep_installations_schema47")
    storage.install_central_operational_compatibility_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(48)")
    connection.execute("UPDATE engineering_metadata SET value='48' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=48")


def _migrate_schema_49(connection: sqlite3.Connection) -> None:
    """Record bounded ingress receipts in the canonical submission row."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema48")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49)))"
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) "
        "SELECT instance_id,created_at,49 FROM ep_installations_schema48"
    )
    connection.execute("DROP TABLE ep_installations_schema48")
    # SQLite cannot widen the schema-43 transport CHECK in place.  Rebuild the
    # parent table while foreign-key enforcement is temporarily disabled by
    # the caller; SQLite keeps dependent references pointed at its canonical
    # name.  No submission facts are rewritten or inferred.
    # Child tables must be rebuilt too: SQLite otherwise retains a foreign-key
    # reference to the renamed historical parent table.
    connection.execute("ALTER TABLE ep_submission_events RENAME TO ep_submission_events_schema48")
    connection.execute("ALTER TABLE ep_submission_prompt_history RENAME TO ep_submission_prompt_history_schema48")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches RENAME TO ep_parity_lifecycle_dispatches_schema48")
    connection.execute("ALTER TABLE ep_submissions RENAME TO ep_submissions_schema48")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        producer_id TEXT NOT NULL, producer_type TEXT NOT NULL, producer_version TEXT,
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL,
        idempotency_key TEXT, correlation_id TEXT, mission_id TEXT, engineering_action_id TEXT,
        transport_receipt_id TEXT, transport_received_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED')), admission TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submissions(
        submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,state,admission,created_at)
        SELECT submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,state,admission,created_at
        FROM ep_submissions_schema48""")
    connection.execute("""CREATE TABLE ep_submission_events (
        event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id),
        event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_events(event_id,submission_id,event_kind,payload,recorded_at)
        SELECT event_id,submission_id,event_kind,payload,recorded_at FROM ep_submission_events_schema48""")
    connection.execute("""CREATE TABLE ep_submission_prompt_history (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_prompt_history(submission_id,prompt_digest,recorded_at)
        SELECT submission_id,prompt_digest,recorded_at FROM ep_submission_prompt_history_schema48""")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
        prompt_path TEXT NOT NULL, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED')),
        resolution_submission_id TEXT REFERENCES ep_submissions(submission_id))""")
    connection.execute("""INSERT INTO ep_parity_lifecycle_dispatches(
        submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id)
        SELECT submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id
        FROM ep_parity_lifecycle_dispatches_schema48""")
    connection.execute("DROP TABLE ep_submission_events_schema48")
    connection.execute("DROP TABLE ep_submission_prompt_history_schema48")
    connection.execute("DROP TABLE ep_parity_lifecycle_dispatches_schema48")
    connection.execute("DROP TABLE ep_submissions_schema48")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(49)")
    connection.execute("UPDATE engineering_metadata SET value='49' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=49")


def validate_store(data_root: Path, identity: RuntimeIdentity) -> dict[str, object]:
    """Return a deterministic fail-closed current-schema structural report."""
    path = data_root / SERVER_DATABASE_FILENAME
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = _table_names(connection)
            indexes = _index_names(connection)
            schema = _schema_version(connection)
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            metadata = dict(connection.execute("SELECT key,value FROM engineering_metadata WHERE key IN ('installation.instance_id','installation.schema_version')"))
            installation = connection.execute("SELECT instance_id FROM ep_installations WHERE instance_id=?", (identity.instance_id,)).fetchone()
    except (OSError, sqlite3.DatabaseError) as error:
        raise ServerConfigurationError("EP Server store is unavailable.") from error
    valid = schema == SERVER_STORE_SCHEMA_VERSION and SERVER_REQUIRED_TABLES <= tables and SERVER_REQUIRED_INDEXES <= indexes and integrity == ["ok"] and metadata == {"installation.instance_id": identity.instance_id, "installation.schema_version": str(SERVER_STORE_SCHEMA_VERSION)} and installation is not None
    if not valid:
        raise ServerConfigurationError(
            f"EP Server store is not a valid official schema-{SERVER_STORE_SCHEMA_VERSION} installation."
        )
    return {"schema_version": schema, "integrity": "PASS", "required_tables": sorted(SERVER_REQUIRED_TABLES), "required_indexes": sorted(SERVER_REQUIRED_INDEXES)}


def initialize(data_root: Path, *, bind_host: str = "127.0.0.1", bind_port: int = 8765) -> RuntimeIdentity:
    """Create or validate an empty, installation-owned server instance."""
    data_root = data_root.resolve()
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = data_root / SERVER_CONFIGURATION_FILENAME
    if not config_path.exists():
        if bind_host != "127.0.0.1" or not 1 <= bind_port <= 65535:
            raise ServerConfigurationError("EP Server initial bind configuration is invalid.")
        _write_json(config_path, asdict(ServerConfiguration(
            SERVER_CONFIGURATION_VERSION, bind_host, bind_port,
            str(default_engineering_platform_codex_cli_prefix()),
        )))
    configuration = ServerConfiguration.load(data_root)
    # Version 1 inferred the CLI installation at each process boundary from
    # HOME.  Upgrade it once, under the server's stable account identity, so
    # child workers and later restarts inherit one installation authority.
    if configuration.version != SERVER_CONFIGURATION_VERSION:
        configuration = ServerConfiguration(
            SERVER_CONFIGURATION_VERSION,
            configuration.bind_host,
            configuration.bind_port,
            configuration.managed_codex_cli_prefix,
        )
        _write_json(config_path, asdict(configuration))
    identity_path = data_root / SERVER_IDENTITY_FILENAME
    if identity_path.exists():
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
            identity = RuntimeIdentity(str(raw["instance_id"]), str(raw["created_at"]))
            if not identity.instance_id or not identity.created_at:
                raise ValueError("empty identity")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server runtime identity is invalid.") from error
    else:
        identity = RuntimeIdentity(str(uuid4()), _utcnow())
        _write_json(identity_path, asdict(identity))
    database_path = data_root / SERVER_DATABASE_FILENAME
    if database_path.exists():
        try:
            with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as existing:
                existing_tables = _table_names(existing)
                if existing_tables:
                    current_schema = _schema_version(existing)
                    if current_schema not in {41, 42, 43, 44, 45, 46, 47, 48, SERVER_STORE_SCHEMA_VERSION}:
                        raise ServerConfigurationError(
                            f"EP Server store is not a valid official schema-{SERVER_STORE_SCHEMA_VERSION} installation."
                        )
                    if current_schema == SERVER_STORE_SCHEMA_VERSION:
                        validate_store(data_root, identity)
                        return identity
                    if current_schema in {42, 43, 44, 45, 46, 47, 48}:
                        with sqlite3.connect(database_path) as connection:
                            # Schema-49 rebuilds the submission parent table
                            # to widen its immutable transport constraint.
                            connection.execute("PRAGMA foreign_keys=OFF")
                            connection.execute("BEGIN IMMEDIATE")
                            if current_schema == 42:
                                _migrate_schema_43(connection)
                            if current_schema in {42, 43}:
                                _migrate_schema_44(connection)
                            if current_schema in {42, 43, 44}:
                                _migrate_schema_45(connection)
                            if current_schema in {42, 43, 44, 45}:
                                _migrate_schema_46(connection)
                            if current_schema in {42, 43, 44, 45, 46}:
                                _migrate_schema_47(connection)
                            if current_schema in {42, 43, 44, 45, 46, 47}:
                                _migrate_schema_48(connection)
                            _migrate_schema_49(connection)
                            connection.execute("COMMIT")
                        validate_store(data_root, identity)
                        return identity
        except sqlite3.DatabaseError as error:
            raise ServerConfigurationError("EP Server store is unavailable.") from error
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _install_schema_41(connection, identity)
        _migrate_schema_42(connection)
        _migrate_schema_43(connection)
        _migrate_schema_44(connection)
        _migrate_schema_45(connection)
        _migrate_schema_46(connection)
        _migrate_schema_47(connection)
        _migrate_schema_48(connection)
        _migrate_schema_49(connection)
        connection.execute("COMMIT")
    database_path.chmod(0o600)
    validate_store(data_root, identity)
    return identity


def _runtime(data_root: Path) -> dict[str, object] | None:
    try:
        raw = json.loads((data_root / SERVER_RUNTIME_FILENAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _transport_components(data_root: Path, *, server_running: bool) -> dict[str, dict[str, object]]:
    """Return secret-free, platform-scoped ingress observability from CENTRAL.

    Submission timestamps are observation only.  They never change queue or
    execution semantics and intentionally retain no credential or raw prompt.
    """
    latest: dict[str, str] = {}
    with sqlite3.connect(f"file:{data_root / SERVER_DATABASE_FILENAME}?mode=ro", uri=True) as connection:
        for transport, created_at in connection.execute(
            "SELECT transport,MAX(created_at) FROM ep_submissions GROUP BY transport"
        ):
            latest[str(transport)] = str(created_at)
    http_status_code = "HTTP_INGRESS_HEALTHY" if server_running else "HTTP_INGRESS_DOWN"
    cli_status_code = "CLI_INGRESS_AVAILABLE" if server_running else "CLI_INGRESS_DEGRADED"
    file_last = latest.get("FILE_INBOX")
    heartbeat = file_inbox.read_heartbeat(data_root / FILE_INBOX_DIRECTORY)
    heartbeat_at = str(heartbeat.get("updated_at", "")) if heartbeat else ""
    try:
        heartbeat_fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat_at)).total_seconds() <= 10
    except ValueError:
        heartbeat_fresh = False
    delivery_retry = str(heartbeat.get("delivery_retry", "NONE")) if heartbeat else "NONE"
    quarantine_count = int(heartbeat.get("quarantine_count", 0)) if heartbeat and isinstance(heartbeat.get("quarantine_count", 0), int) else 0
    recent_error = heartbeat.get("recent_error") if heartbeat else None
    # A live watcher with pending delivery, a bounded adapter diagnostic, or
    # quarantined ingress is operational but needs attention.  It is not a
    # CENTRAL execution/run failure and cannot affect queue authority.
    file_attention_needed = delivery_retry != "NONE" or bool(recent_error) or quarantine_count > 0
    file_status_code = (
        "FILE_INGRESS_STOPPED" if not server_running or not heartbeat_fresh
        else "FILE_INGRESS_DEGRADED" if file_attention_needed else "FILE_INGRESS_RUNNING"
    )
    return {
        "http_ingress": {
            "healthy": server_running, "status_code": http_status_code,
            "detail_code": "CENTRAL_LISTENER_ENDPOINT" if server_running else "CENTRAL_LISTENER_UNAVAILABLE",
            "version": "1",  # canonical submission protocol version
            "last_successful_submission": latest.get("HTTP"), "recent_error": None,
        },
        "cli_ingress": {
            "healthy": server_running, "status_code": cli_status_code,
            "detail_code": "CANONICAL_SUBMISSION_COMPATIBILITY" if server_running else "CENTRAL_ENDPOINT_UNAVAILABLE",
            "version": "1", "last_successful_submission": latest.get("CLI"), "recent_error": None,
        },
        "file_inbox_ingress": {
            "healthy": file_status_code == "FILE_INGRESS_RUNNING", "status_code": file_status_code,
            "detail_code": "FILE_INBOX_HEARTBEAT" if server_running and heartbeat_fresh else "FILE_INBOX_HEARTBEAT_MISSING",
            "watched_location": heartbeat.get("watched_location") if heartbeat else str(data_root / FILE_INBOX_DIRECTORY),
            "heartbeat": heartbeat_at or None,
            "last_successful_submission": file_last,
            "delivery_retry_code": f"FILE_INGRESS_DELIVERY_RETRY_{delivery_retry}",
            "quarantine_count": quarantine_count,
            # Never transport a raw exception into a presentation projection.
            "reason_code": "FILE_INBOX_DIAGNOSTIC" if recent_error else None,
        },
    }


def _platform_component_detail(data_root: Path, component_id: str) -> dict[str, object] | None:
    """Expose one secret-free detail view from the same platform projection."""
    component = status(data_root)["components"].get(component_id)  # type: ignore[index]
    if not isinstance(component, dict):
        return None
    return {"component": component_id, "machine": os.uname().nodename, "restart_supported": False, **component}


def status(data_root: Path) -> dict[str, object]:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    runtime = _runtime(data_root)
    running = bool(runtime and _alive(runtime.get("pid")))
    components = _transport_components(data_root, server_running=running)
    # One Server-native inventory feeds Components, the titlebar popout and
    # detail modals. It deliberately contains no watcher/check-out model.
    for component_id, definition in PLATFORM_COMPONENT_DEFINITIONS.items():
        healthy = True if component_id == "platform_database" else running
        components[component_id] = {
            "kind": definition["kind"], "healthy": healthy,
            "status_code": definition["active"] if healthy else definition["inactive"],
            "detail_code": definition["detail"],
            **({"version": str(SERVER_STORE_SCHEMA_VERSION)} if component_id == "platform_database" else {}),
        }
    return {
        "service": "engineering-platform-server",
        "instance_id": identity.instance_id,
        "store": "ready",
        "schema_version": SERVER_STORE_SCHEMA_VERSION,
        "operational_state": "empty-valid",
        "running": running,
        "managed_codex_runtime": managed_codex_runtime.inspect(data_root),
        "lifecycle_worker": {
            # The worker is hosted by the sole installed Server process.  A
            # stopped process is never reported as an active worker.
            "state": WORKER_RUNNING if running else "STOPPED",
        },
        "bind": {"host": config.bind_host, "port": config.bind_port},
        "components": components,
    }


def operations_projection(data_root: Path) -> dict[str, object]:
    """Return the installed CENTRAL's secret-free Console projection.

    The selected project remains a browser presentation preference.  This
    endpoint intentionally returns topology only; no checkout path, Agent
    credential, or execution capability is exposed here.
    """
    identity = initialize(data_root)
    with sqlite3.connect(f"file:{data_root / SERVER_DATABASE_FILENAME}?mode=ro", uri=True) as connection:
        topology = project_topology.topology(connection)
    return {
        "installation_id": identity.instance_id,
        "schema_version": SERVER_STORE_SCHEMA_VERSION,
        "managed_codex_runtime": managed_codex_runtime.inspect(data_root),
        "projects": topology["projects"],
    }


def _operations_console_document() -> bytes:
    """Small CENTRAL-owned console shell; all topology is loaded from its API."""
    return b'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Engineering Platform Operations Console</title></head><body><main><h1>Engineering Platform Operations Console</h1><label for="project">Project</label><select id="project" aria-label="Project"></select><pre id="topology" aria-live="polite">Loading...</pre></main><script>const select=document.querySelector('#project'),view=document.querySelector('#topology');fetch('/v1/operations/projects').then(r=>r.ok?r.json():Promise.reject()).then(data=>{for(const p of data.projects){const o=document.createElement('option');o.value=p.project_id;o.textContent=p.project_id==='djconnect'?'DJConnect':p.project_id==='engineering-platform'?'Engineering Platform':p.project_id;select.append(o)}const render=()=>{const p=data.projects.find(x=>x.project_id===select.value);view.textContent=JSON.stringify({installation_id:data.installation_id,schema_version:data.schema_version,project:p},null,2)};select.onchange=render;render()}).catch(()=>view.textContent='Operations Console unavailable');</script></body></html>'''


def _console_projects(data_root: Path) -> list[dict[str, str]]:
    """List CENTRAL project identities without opening their checkouts.

    The selector is a logical CENTRAL projection.  A local binding is checked
    only later, when a user explicitly selects that project for a transitional
    root-bound route.
    """
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute("""SELECT p.project_id, r.repository_id
            FROM ep_project_registrations AS p
            JOIN ep_repository_registrations AS r
              ON r.project_id=p.project_id AND r.role='authority'
            WHERE p.status='ACTIVE'
            ORDER BY p.project_id""").fetchall()
    return [{"project_id": str(project_id), "repository_id": str(repository_id)}
            for project_id, repository_id in rows]


def _console_queue_projection(data_root: Path, project_id: str) -> dict[str, object]:
    """Read the selected project's single transport-neutral CENTRAL FIFO."""
    for project in _console_projects(data_root):
        if project["project_id"] != project_id:
            continue
        with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
            context = project_context(
                connection,
                data_root=data_root,
                project_id=project_id,
                repository_id=project["repository_id"],
                require_local_root=False,
            )
            queue = ParityProjectStore(connection, context).console_queue_projection()
            rows = connection.execute(
                """SELECT run_id,operator_resolution FROM ep_parity_lifecycle_dispatches
                    WHERE project_id=? AND operator_resolution IN ('DISMISSED','RETRIED')""",
                (project_id,),
            ).fetchall()
            return {**queue, "operator_handling": {str(run_id): str(resolution) for run_id, resolution in rows}}
    raise local_repository_binding.LocalRepositoryBindingError("CONSOLE_PROJECT_UNAVAILABLE")


def _console_platform_version() -> str:
    """Read the installed platform version once for CENTRAL Console snapshots."""
    return EngineeringPlatformManifest.load(
        package_path("ENGINEERING_PLATFORM_VERSION.json")
    ).platform_version


def _central_console_project_snapshot(data_root: Path, project_id: str) -> dict[str, object]:
    """Return the Slice-B project status/history projection from CENTRAL only."""
    queue = _console_queue_projection(data_root, project_id)
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        runs = connection.execute(
            """SELECT run_id,state,created_at,updated_at,execution_mode
                FROM ep_execution_runs WHERE project_id=?
                ORDER BY created_at DESC,run_id DESC LIMIT 1000""",
            (project_id,),
        ).fetchall()
        dispatches = dict(connection.execute(
            "SELECT run_id,state FROM ep_parity_lifecycle_dispatches WHERE project_id=?",
            (project_id,),
        ).fetchall())
    records = [
        {
            "run_id": str(run_id), "status": str(dispatches.get(run_id, state)),
            "state": str(dispatches.get(run_id, state)), "created_at": str(created_at),
            "updated_at": str(updated_at), "execution_mode": execution_mode,
            "project_id": project_id,
        }
        for run_id, state, created_at, updated_at, execution_mode in runs
    ]
    active = next((record for record in records if record["state"] in {"CLAIMED", "RUNNING"}), None)
    return {
        "project_id": project_id,
        "scope": "PROJECT",
        "status": {
            "project_id": project_id,
            "platform_version": _console_platform_version(),
            "queue_depth": queue["queue_depth"],
            "queue_items": queue["queue_items"],
            "active_run": active["run_id"] if active else None,
            "last_executed_run": records[0]["run_id"] if records else None,
            "lifecycle_source": "CENTRAL",
        },
        "runs": records,
        "queue": queue,
        "telemetry": _central_console_telemetry(data_root, project_id),
    }


def _no_project_console_snapshot(data_root: Path) -> dict[str, object]:
    """Give the Console a loadable CENTRAL-only snapshot at ``<geen>``.

    The no-project document deliberately hides project state, but the shared
    dashboard shell still hydrates through the snapshot endpoint. Returning a
    minimal platform projection prevents it from remaining behind the loading
    overlay while preserving the fail-closed boundary for all project routes.
    """
    # Aggregate the canonical CENTRAL submission state only. File Inbox files,
    # watcher backlogs and transport retry diagnostics never affect this count.
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        queue_depth = int(connection.execute(
            "SELECT COUNT(*) FROM ep_submissions WHERE state IN ('QUEUED','ADMITTED')"
        ).fetchone()[0])
    queue = {"operator_handling": {}, "queue_depth": queue_depth, "queue_items": [], "scope": "ALL_PROJECTS"}
    return {
        "scope": "PLATFORM",
        "queue": queue,
        "runs": [],
        "telemetry": [],
        "status": {
            "lifecycle_source": "CENTRAL",
            "platform_version": _console_platform_version(),
            **queue,
        },
    }


def _central_console_run_detail(data_root: Path, project_id: str, run_id: str) -> dict[str, object] | None:
    """Resolve a run by canonical project/run identity, never by checkout."""
    snapshot = _central_console_project_snapshot(data_root, project_id)
    return next((record for record in snapshot["runs"] if record["run_id"] == run_id), None)


def _central_console_telemetry(data_root: Path, project_id: str) -> list[dict[str, object]]:
    """Read bounded daily telemetry through CENTRAL's run/project lineage."""
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute(
            """SELECT r.execution_date,COUNT(*),
                      SUM(r.terminal_state='COMPLETE'),SUM(r.terminal_state='BLOCKED'),SUM(r.terminal_state='FAILED'),
                      AVG(r.execution_seconds),AVG(r.total_execution_seconds),AVG(r.queue_wait_seconds),
                      SUM(r.input_tokens),SUM(r.output_tokens),SUM(r.total_tokens)
                 FROM execution_runs AS r
                 JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=r.run_id
                 WHERE d.project_id=?
                 GROUP BY r.execution_date ORDER BY r.execution_date DESC LIMIT 360""",
            (project_id,),
        ).fetchall()
    keys = (
        "date", "prompt_count", "complete_count", "blocked_count", "failed_count",
        "average_execution_seconds", "average_total_execution_seconds", "average_queue_wait_seconds",
        "input_tokens", "output_tokens", "total_tokens",
    )
    return [dict(zip(keys, row, strict=True)) | {
        "average_provider_execution_seconds": None, "average_validation_seconds": None,
    } for row in rows]


def _central_console_telemetry_detail(data_root: Path, project_id: str, execution_date: str) -> dict[str, object] | None:
    """Provide a project-isolated CENTRAL telemetry day without root fallback."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", execution_date):
        return None
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute(
            """SELECT r.run_id,r.execution_started_at,r.terminal_state,r.total_execution_seconds,
                      r.queue_wait_seconds,r.runtime_provider,r.runtime_model,r.reasoning_profile,
                      r.producer_type,r.repository
                 FROM execution_runs AS r
                 JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=r.run_id
                 WHERE d.project_id=? AND r.execution_date=?
                 ORDER BY r.execution_started_at DESC LIMIT 250""",
            (project_id, execution_date),
        ).fetchall()
    if not rows:
        return None
    run_rows = [{
        "run_id": str(row[0]), "started_at": row[1], "status": row[2],
        "total_duration_ms": round(float(row[3]) * 1000) if isinstance(row[3], (float, int)) else None,
        "queue_wait_ms": round(float(row[4]) * 1000) if isinstance(row[4], (float, int)) else None,
        "provider_duration_ms": None, "validation_duration_ms": None, "external_wait_ms": None,
        "largest_phase": None, "producer_type": row[8], "repository": row[9],
        "provider": row[5], "model": row[6], "reasoning_profile": row[7],
        "phase_telemetry": "NOT_RECORDED",
    } for row in rows]
    durations = [row["total_duration_ms"] for row in run_rows if isinstance(row["total_duration_ms"], int)]
    waits = [row["queue_wait_ms"] for row in run_rows if isinstance(row["queue_wait_ms"], int)]
    def aggregate(values: list[int]) -> dict[str, int] | None:
        return {"average_ms": round(sum(values) / len(values)), "median_ms": sorted(values)[len(values) // 2], "total_ms": sum(values), "runs": len(values)} if values else None
    return {
        "date": execution_date, "timezone": "UTC", "runs": run_rows, "phases": [], "phase_telemetry_available": False,
        "summary": {"executions": len(run_rows), "completed": sum(row["status"] == "COMPLETE" for row in run_rows),
                    "blocked": sum(row["status"] == "BLOCKED" for row in run_rows), "failed": sum(row["status"] == "FAILED" for row in run_rows),
                    "total_wall_time": aggregate(durations), "queue_wait": aggregate(waits),
                    "active_processing_time": None, "provider_execution": None, "validation": None, "external_wait": None, "overhead": None,
                    "report_generation": None, "evidence_persistence": None},
        "bottlenecks": {"longest_average_phase": None, "largest_accumulated_phase": None, "top_time_consumers": [], "shares": {}},
    }


def _central_console_report(data_root: Path, project_id: str, run_id: str) -> bytes | None:
    """Read one CENTRAL-indexed immutable report with project authorization."""
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            """SELECT h.report_path FROM prompt_execution_history AS h
                 JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=h.run_id
                 WHERE d.project_id=? AND h.run_id=?""",
            (project_id, run_id),
        ).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0].startswith("CENTRAL:"):
        return None
    candidate = (data_root / "artifacts" / row[0].removeprefix("CENTRAL:")).resolve()
    try:
        candidate.relative_to((data_root / "artifacts").resolve())
        return candidate.read_bytes() if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _central_console_chat_history(data_root: Path, project_id: str, run_id: str) -> list[dict[str, object]] | None:
    """Return a project-authorized CENTRAL transcript; no root fallback exists."""
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        belongs = connection.execute(
            "SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchone()
        if belongs is None:
            return None
        rows = connection.execute(
            "SELECT role,content,model,created_at FROM execution_chat_messages WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [{"role": str(role), "content": str(content), "model": model, "created_at": str(created_at)}
            for role, content, model, created_at in rows]


def _central_console_component_logs(data_root: Path, component: str) -> dict[str, object] | None:
    """Read Server-owned component logs only from CENTRAL's log index."""
    if component not in {"dashboard", "inbox"}:
        return None
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute(
            "SELECT id,payload,created_at FROM engineering_component_logs WHERE component=? ORDER BY id DESC LIMIT 200",
            (component,),
        ).fetchall()
    entries: list[dict[str, object]] = []
    for identifier, payload, created_at in reversed(rows):
        try:
            decoded = json.loads(str(payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {"event": "malformed_central_log"}
        entries.append({"line": int(identifier), "timestamp": str(created_at), **(decoded if isinstance(decoded, dict) else {})})
    return {"scope": "PLATFORM", "component": component, "entries": entries, "total": len(entries), "events": sorted({str(item.get("event")) for item in entries if item.get("event")})}


def _central_console_configuration(data_root: Path) -> dict[str, object]:
    """Expose only configuration already owned by CENTRAL in this phase."""
    return {
        "scope": "PLATFORM",
        **central_database.maintenance_configuration(data_root),
        **central_database.capacity_configuration(data_root),
        **central_database.console_interval_configuration(data_root),
    }


def _central_provider_readiness(data_root: Path) -> dict[str, dict[str, object]]:
    """Project-independent, token-free authentication readiness for CENTRAL."""
    statuses = provider_readiness.host_status(data_root)
    runtime = provider_readiness.runtime_details(data_root)
    return {
        provider: {**value, **runtime.get(provider, {}), "scope": "PLATFORM"}
        for provider, value in statuses.items()
    }


def _central_provider_repair(data_root: Path, payload: object) -> None:
    """Start one validated host-wide provider action without a checkout."""
    if not isinstance(payload, dict) or set(payload) != {"provider", "action"}:
        raise ValueError("Invalid provider repair request.")
    provider, action = str(payload["provider"]), str(payload["action"])
    if provider not in {"CODEX", "GITHUB"} or action not in {"login", "install"}:
        raise ValueError("Invalid provider repair request.")
    readiness = _central_provider_readiness(data_root)
    state = str(readiness[provider.lower()]["state"])
    if (action == "login" and state != "AUTH_REQUIRED") or (action == "install" and state != "UNAVAILABLE"):
        raise ValueError("Provider is not ready for the requested repair.")
    if action == "login":
        dashboard._start_provider_login(data_root, provider)  # type: ignore[attr-defined]
    else:
        dashboard._install_provider(data_root, provider)  # type: ignore[attr-defined]


def _central_provider_logout(data_root: Path, payload: object) -> None:
    """Remove one verified host-wide provider session through CENTRAL.

    The Console never owns provider credentials.  It can only request this
    narrowly validated host operation while the provider is known ready, so a
    stale or fabricated UI request cannot be delegated to a checkout-bound
    legacy handler.
    """
    if not isinstance(payload, dict) or set(payload) != {"provider"}:
        raise ValueError("Invalid provider logout request.")
    provider = str(payload["provider"])
    if provider not in {"CODEX", "GITHUB"}:
        raise ValueError("Invalid provider logout request.")
    readiness = _central_provider_readiness(data_root)
    if str(readiness[provider.lower()]["state"]) != "READY":
        raise ValueError("Provider is not ready for logout.")
    dashboard._logout_provider(data_root, provider)  # type: ignore[attr-defined]


def _with_console_queue(payload: bytes, *, queue: dict[str, object], data_root: Path) -> bytes:
    """Overlay CENTRAL-only queue and provider evidence onto legacy payloads."""
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return payload
    if not isinstance(decoded, dict):
        return payload
    handling = queue.get("operator_handling")
    if isinstance(handling, dict) and isinstance(decoded.get("runs"), list):
        for run in decoded["runs"]:
            if isinstance(run, dict) and handling.get(run.get("run_id")) == "DISMISSED":
                run["dismissed"] = True
                run["handling_state"] = "DISMISSED"
    status_payload = decoded.get("status")
    if isinstance(status_payload, dict):
        decoded["status"] = {**status_payload, **queue}
    else:
        decoded = {**decoded, **queue}
    rate_limits = decoded.get("rate_limits")
    if isinstance(rate_limits, dict):
        provider = rate_limits.get("provider")
        remaining = dashboard._remaining_rate_limit_capacity(rate_limits)
        if isinstance(provider, str) and remaining is not None:
            decoded["ai_capacity_history"] = central_database.record_provider_capacity(
                data_root, provider=provider, remaining_percent=remaining,
            )
            decoded["capacity_scope"] = "EP"
            decoded["capacity_configuration"] = central_database.capacity_configuration(data_root)
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


def _provider_capacity_projection(data_root: Path) -> dict[str, object]:
    """Read the account-owned quota once and project it from CENTRAL."""
    try:
        payload = json.loads(dashboard._codex_rate_limits())
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    provider = payload.get("provider")
    remaining = dashboard._remaining_rate_limit_capacity(payload)
    history: list[dict[str, object]] = []
    if isinstance(provider, str) and remaining is not None:
        history = central_database.record_provider_capacity(
            data_root, provider=provider, remaining_percent=remaining,
        )
    return {
        "rate_limits": payload,
        "ai_capacity_history": history,
        "scope": "EP",
        "configuration": central_database.capacity_configuration(data_root),
    }


def _historical_dashboard_path(request: SplitResult) -> str:
    """Remove the Server-only project selector before historical routing."""
    retained = [
        (key, value)
        for key, value in parse_qsl(request.query, keep_blank_values=True)
        if key != "project"
    ]
    return request.path if not retained else f"{request.path}?{urlencode(retained, doseq=True)}"


def _console_project_options(project_id: str | None, projects: list[dict[str, str]]) -> str:
    """Render the safe empty choice plus registered CENTRAL identities."""
    empty = '<option value=""' + (" selected" if project_id is None else "") + '>&lt;geen&gt;</option>'
    return empty + "".join(
        f'<option value="{escape(item["project_id"], quote=True)}"'
        f'{" selected" if item["project_id"] == project_id else ""}>'
        f'{escape(item["project_id"])}</option>'
        for item in projects
    )


def _central_database_section(data_root: Path) -> str:
    """Render the one installation-owned EP database panel for Configuration."""
    details = central_database.details(data_root)
    interval = central_database.maintenance_configuration(data_root)["interval_seconds"]
    size = f"{int(details['size_bytes']) / 1_000_000:.2f}".replace(".", ",") + " MB"
    options = "".join(
        f'<option value="{value}"{" selected" if value == interval else ""}>{label}</option>'
        for value, label in ((60, "1 minuut"), (3600, "1 uur"), (86400, "1 dag"), (604800, "1 week"))
    )
    return (
        '<section class="configuration-central-database" aria-labelledby="centralDatabaseHeading">'
        '<header class="configuration-central-database__header">'
        '<div><h2 id="centralDatabaseHeading" data-i18n="configuration.ep_database">EP-database</h2>'
        '<p data-i18n="configuration.ep_database_description">Platformbrede opslag voor projecten, uitvoeringen en configuratie.</p></div>'
        '<a class="dashboard-action dashboard-action--download" href="/api/central-database/download" download '
        'data-i18n="configuration.ep_database_download" data-i18n-aria-label="configuration.ep_database_download" '
        'aria-label="Download EP-database">Download EP-database</a></header>'
        '<dl class="configuration-central-database__facts">'
        '<div><dt class="label" data-i18n="configuration.database_owner">Database-eigendom</dt><dd data-i18n="configuration.ep_database_owner">Engineering Platform</dd></div>'
        f'<div class="configuration-central-database__location"><dt class="label" data-i18n="configuration.database_location">Databaselocatie</dt><dd><button id="centralDatabaseLocation" class="local-folder-link configuration-central-database__location-link" type="button" data-i18n-aria-label="configuration.ep_database_open_folder" aria-label="Open EP-databasemap in Finder">{escape(str(details["path"]))}</button></dd></div>'
        f'<div><dt class="label" data-i18n="configuration.database_size">Databasegrootte</dt><dd>{size}</dd></div>'
        f'<div><dt class="label" data-i18n="configuration.schema_version">Schema-versie</dt><dd>{details["schema_version"]}</dd></div>'
        f'<div><dt class="label" data-i18n="configuration.integrity">Integriteit</dt><dd>{details["integrity"]}</dd></div>'
        '</dl>'
        '<div class="configuration-central-database__maintenance">'
        '<div><span class="label" id="centralDatabaseMaintenanceLabel" data-i18n="configuration.ep_database_maintenance">Databaseonderhoud</span>'
        '<p id="centralDatabaseMaintenanceHelp" data-i18n="configuration.ep_database_maintenance_help">Optimaliseert de EP-database wanneer geen uitvoering actief is.</p></div>'
        f'<select id="centralDatabaseMaintenanceInterval" aria-labelledby="centralDatabaseMaintenanceLabel" aria-describedby="centralDatabaseMaintenanceHelp centralDatabaseMaintenanceStatus" data-saved-value="{interval}">{options}</select>'
        '</div>'
        '<p id="centralDatabaseMaintenanceStatus" role="status" aria-live="polite"></p></section>'
    )


def _central_database_script() -> str:
    """Keep the EP database maintenance preference host-scoped in the Console."""
    return '''const maintenance=document.getElementById('centralDatabaseMaintenanceInterval'),maintenanceStatus=document.getElementById('centralDatabaseMaintenanceStatus');if(maintenance)maintenance.addEventListener('change',async()=>{const previous=maintenance.dataset.savedValue||maintenance.value,requested=Number(maintenance.value);maintenance.disabled=true;maintenance.setAttribute('aria-busy','true');if(maintenanceStatus)maintenanceStatus.textContent='';try{const response=await fetch('/api/central-database/configuration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_seconds:requested})});const result=response.ok?await response.json():null;if(!result||Number(result.interval_seconds)!==requested)throw Error();maintenance.dataset.savedValue=String(requested);if(maintenanceStatus)maintenanceStatus.textContent='Databaseonderhoud bijgewerkt.'}catch{maintenance.value=previous;if(maintenanceStatus)maintenanceStatus.textContent='Databaseonderhoud kon niet worden bijgewerkt.'}finally{maintenance.disabled=false;maintenance.removeAttribute('aria-busy')}});'''


def _open_central_database_directory(data_root: Path) -> dict[str, str]:
    """Open exactly CENTRAL's owning directory in Finder, never a request path."""
    directory = central_database.path(data_root).parent.resolve()
    if sys.platform != "darwin" or directory != data_root.resolve() or not directory.is_dir():
        raise RuntimeError("CENTRAL_DATABASE_DIRECTORY_UNAVAILABLE")
    try:
        outcome = LocalProcessProvider().execute(directory, ("open", str(directory)))
    except OSError as error:
        raise RuntimeError("CENTRAL_DATABASE_DIRECTORY_UNAVAILABLE") from error
    if outcome.returncode:
        raise RuntimeError("CENTRAL_DATABASE_DIRECTORY_UNAVAILABLE")
    return {"opened_directory": str(directory)}


_WORKSPACE_ID_FIELD = re.compile(
    br'(<span class="label" data-workspace-label="workspace\.name" data-i18n="workspace\.name"></span><span>)[^<]*(</span>)'
)


def _retire_legacy_inbox_configuration(document: bytes, data_root: Path) -> bytes:
    """Remove checkout/watcher Inbox controls from the installed Console.

    File Inbox is an installation-owned transport.  The retained dashboard
    template may contain historical controls, but they are not a supported
    CENTRAL Console surface and must not be reachable by a selected project.
    """
    document = re.sub(br'<div class="field configuration-field">.*?id="configurationInboxOpen".*?</div>', b"", document, count=1)
    document = re.sub(br'<label for="configurationInboxScanInterval">.*?</label>', b"", document, count=1)
    document = re.sub(br'<dialog class="dashboard-modal-shell.*?id="configurationInboxModal".*?</dialog>', b"", document, count=1)
    location = escape(str((data_root / FILE_INBOX_DIRECTORY).resolve())).encode("utf-8")
    readonly = b'<p class="field configuration-field configuration-file-inbox-readonly"><span class="label" data-i18n="transport.file"></span><span>' + location + b'</span></p>'
    return document.replace(
        b'<p class="category-description" data-i18n="description.configuration"></p>',
        b'<p class="category-description" data-i18n="description.configuration"></p>' + readonly,
        1,
    )


def _centralize_workspace_identity(document: bytes, project_id: str) -> bytes:
    """Make CENTRAL's selected project the sole visible workspace identity."""
    replacement = rb"\1" + escape(project_id).encode("utf-8") + rb"\2"
    return _WORKSPACE_ID_FIELD.sub(replacement, document, count=1)


def _console_project_boundary(project_id: str, options: str) -> str:
    """Bind CENTRAL selector options and request scope to a dashboard document.

    The historical dashboard initially renders its own selector.  Replacing
    those options happens after the generic visual picker is initialized, so
    the explicit event is the boundary contract that keeps the two controls
    synchronized without exposing CENTRAL details to dashboard internals.
    """
    return '''<script>
(() => {
  const project = $PROJECT;
  const options = $OPTIONS;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-Engineering-Platform-Project', project);
    return nativeFetch(input, { ...init, headers });
  };
  const NativeEventSource = window.EventSource;
  window.EventSource = function(url, config) {
    const target = new URL(url, window.location.href);
    target.searchParams.set('project', project);
    return new NativeEventSource(target, config);
  };
  window.EventSource.prototype = NativeEventSource.prototype;
  window.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('dashboardProject');
    if (!select) return;
    select.innerHTML = options;
    select.value = project;
    select.dispatchEvent(new Event('dashboard-select-options-changed', { bubbles: true }));
    select.addEventListener('change', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('project', select.value);
      window.location.assign(url);
    });
    $CENTRAL_DATABASE_SCRIPT
  });
})();
</script>'''.replace("$PROJECT", json.dumps(project_id)).replace("$OPTIONS", json.dumps(options)).replace(
        "$CENTRAL_DATABASE_SCRIPT", _central_database_script(),
    )


def _console_document_transform(project_id: str, projects: list[dict[str, str]], root: Path, data_root: Path):
    """Bind CENTRAL project scope to the historical title-bar selector.

    The Operations Console already owns its project selector in the title bar.
    The Server supplies its authoritative options and request scoping there;
    it must not add a second selector to the dashboard content.
    """
    options = _console_project_options(project_id, projects)
    scoped_body = (
        f'<body data-project-id="{escape(project_id, quote=True)}" '
        f'data-project-name="{escape(project_id, quote=True)}">'
    ).encode("utf-8")
    boundary = _console_project_boundary(project_id, options)
    def transform(document: bytes) -> bytes:
        scoped = re.sub(
            br'<body data-project-id="[^"]*" data-project-name="[^"]*">',
            scoped_body,
            document,
            count=1,
        )
        scoped = _centralize_workspace_identity(scoped, project_id)
        scoped = _retire_legacy_inbox_configuration(scoped, data_root)
        central_section = _central_database_section(data_root).encode("utf-8")
        # The repository binding is not project identity: that remains wholly
        # CENTRAL-owned above.  It is, however, useful operational evidence
        # and the dashboard turns this concrete local path into its approved
        # Finder link.  Do not redact it into prose, otherwise there is no
        # valid path left for that safe, allowlisted action.
        scoped = scoped.replace(
            b'<p class="category-description" data-i18n="description.configuration"></p>',
            b'<p class="category-description" data-i18n="description.configuration"></p>' + central_section,
            1,
        )
        return scoped.replace(
            b"</main>", boundary.encode("utf-8") + b"</main>", 1,
        )

    return transform


def _no_project_console_document(projects: list[dict[str, str]], data_root: Path) -> bytes:
    """Render global Console controls without selecting project-owned data."""
    document = dashboard._dashboard_html(
        "EP Operations",
        workspace_id="none",
        project_name="<geen>",
        workspace_location="Niet beschikbaar",
        configuration_inbox="Niet beschikbaar",
    )
    options = _console_project_options(None, projects)
    selector = f'''<label class="dashboard-project" for="dashboardProject"><span>Project</span><select id="dashboardProject" aria-label="Project">{options}</select></label>'''
    boundary = '''<script>window.ENGINEERING_PLATFORM_NO_PROJECT=true;(function(){const select=document.getElementById('dashboardProject');if(select)select.addEventListener('change',()=>{const url=new URL(window.location.href);if(select.value)url.searchParams.set('project',select.value);else url.searchParams.delete('project');window.location.assign(url)});$CENTRAL_DATABASE_SCRIPT})();</script>'''.replace("$CENTRAL_DATABASE_SCRIPT", _central_database_script())
    empty_state = '''<aside class="dashboard-status-banner dashboard-status-banner--no-project" id="noProjectSelected" role="status" aria-live="polite" data-testid="no-project-selected"><strong>Geen project gekozen</strong><span>Kies bovenin een project om uitsluitend de wachtrij, uitvoeringsgeschiedenis en configuratie van dat project te tonen. Hostbrede logs en configuratie blijven hieronder beschikbaar.</span></aside>'''
    scoped_style = '''<style>
body[data-project-id="none"] #queueItems,
body[data-project-id="none"] #promptHistory,
body[data-project-id="none"] #currentRun,
body[data-project-id="none"] #technicalDetails,
body[data-project-id="none"] #workspaceCard { display: none !important; }
</style>'''
    document = re.sub(
        br'<body data-project-id="[^"]*" data-project-name="[^"]*">',
        b'<body data-project-id="none" data-project-name="&lt;geen&gt;">',
        document,
        count=1,
    )
    document = document.replace(
        b'<label class="dashboard-locale"',
        selector.encode("utf-8") + b'<label class="dashboard-locale"',
        1,
    )
    document = _retire_legacy_inbox_configuration(document, data_root)
    # Keep the unscoped explanation in the sticky header.  It is operational
    # context, not a project card that should scroll away with the dashboard.
    document = document.replace(
        b'<aside class="dashboard-status-banner dashboard-status-banner--usage-limit"',
        empty_state.encode("utf-8")
        + b'<aside class="dashboard-status-banner dashboard-status-banner--usage-limit"',
        1,
    )
    document = document.replace(
        b'<main class="dashboard-grid"',
        boundary.encode("utf-8") + b'<main class="dashboard-grid"',
        1,
    )
    document = document.replace(
        b'<p class="category-description" data-i18n="description.configuration"></p>',
        b'<p class="category-description" data-i18n="description.configuration"></p>' + _central_database_section(data_root).encode("utf-8"),
        1,
    )
    return document.replace(b"</head>", scoped_style.encode("utf-8") + b"</head>", 1)


def _selected_project_console_document(project_id: str, projects: list[dict[str, str]], data_root: Path) -> bytes:
    """Render the installed Console shell without loading a project checkout."""
    document = dashboard._dashboard_html(
        "EP Operations", workspace_id=project_id, project_name=project_id,
        workspace_location="Physical binding is not Console authority.",
        configuration_inbox="Not available from the CENTRAL Console.",
    )
    options = _console_project_options(project_id, projects)
    selector = f'''<label class="dashboard-project" for="dashboardProject"><span>Project</span><select id="dashboardProject" aria-label="Project">{options}</select></label>'''
    document = document.replace(
        b'<label class="dashboard-locale"', selector.encode("utf-8") + b'<label class="dashboard-locale"', 1,
    )
    document = document.replace(
        b'<p class="category-description" data-i18n="description.configuration"></p>',
        b'<p class="category-description" data-i18n="description.configuration"></p>' + _central_database_section(data_root).encode("utf-8"), 1,
    )
    return document.replace(b"</main>", _console_project_boundary(project_id, options).encode("utf-8") + b"</main>", 1)


_CONSOLE_STATIC_ASSETS = {
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/assets/dashboard_locales.mjs": ("dashboard_locales.mjs", "text/javascript; charset=utf-8"),
    "/assets/dashboard_status_store.mjs": ("dashboard_status_store.mjs", "text/javascript; charset=utf-8"),
    "/assets/operations-console/icon-dark.png": ("operations-console/icon-dark.png", "image/png"),
    "/assets/operations-console/icon-light.png": ("operations-console/icon-light.png", "image/png"),
    "/assets/operations-console/icon-transparent.png": ("operations-console/icon-transparent.png", "image/png"),
    "/assets/operations-console/apple-touch-icon-dark.png": (dashboard.APP_ICON_DARK, "image/png"),
    "/assets/operations-console/apple-touch-icon-light.png": (dashboard.APP_ICON_LIGHT, "image/png"),
    "/assets/operations-console/manifest.webmanifest": (dashboard.WEB_MANIFEST, "application/manifest+json; charset=utf-8"),
    "/favicon.ico": (dashboard.APP_ICON_DARK, "image/png"),
    "/apple-touch-icon.png": (dashboard.APP_ICON_DARK, "image/png"),
    "/apple-touch-icon-precomposed.png": (dashboard.APP_ICON_DARK, "image/png"),
}


def _no_project_platform_projection(data_root: Path) -> dict[str, object]:
    """Return a checkout-free platform projection for the ``<geen>`` view.

    This deliberately has no project fallback.  It uses only installed Server
    state and CENTRAL metadata, so rendering a Console before a checkout is
    bound is a supported operation.
    """
    return {
        "scope": "PLATFORM",
        "server": status(data_root),
        "central_database": central_database.details(data_root),
        "capacity_configuration": central_database.capacity_configuration(data_root),
    }


def _authenticated_consumer(connection: sqlite3.Connection, token: object, project_id: str) -> str | None:
    """Authenticate an existing scoped CENTRAL consumer credential."""
    if not isinstance(token, str) or not token or len(token) > 4096:
        return None
    row = connection.execute("""SELECT c.consumer_id FROM local_api_credentials c
        JOIN local_api_consumer_registrations r ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id
        WHERE c.verifier=? AND c.project_id=? AND c.revoked_at IS NULL
        AND (c.expires_at IS NULL OR c.expires_at>CURRENT_TIMESTAMP) AND r.status='ACTIVE'""", (verifier(token), project_id)).fetchone()
    return str(row[0]) if row else None


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def _status(self) -> dict[str, object]:
        report = status(self.server.data_root)  # type: ignore[attr-defined]
        worker = getattr(self.server, "lifecycle_worker", None)
        if worker is not None:
            report["lifecycle_worker"] = worker.diagnostics().to_dict()
        return report

    def _send(self, status_code: int, payload: dict[str, object], instance_id: str | None = None) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if instance_id:
            self.send_header("EP-Server-Instance", instance_id)
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_console_asset(self, request: SplitResult) -> bool:
        """Serve installed Console assets without selecting a project/root."""
        asset = _CONSOLE_STATIC_ASSETS.get(request.path)
        if asset is None:
            return False
        name, content_type = asset
        try:
            content = (dashboard.ASSET_DIRECTORY / name).read_bytes()
        except OSError:
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)
        return True

    def _no_project_platform_route(self, method: str, request: SplitResult) -> bool:
        """Serve only explicit platform data when no project is selected.

        Unsupported historical endpoints fail closed.  In particular, this
        method never resolves a local repository binding merely to satisfy an
        old Dashboard helper.
        """
        if method != "do_GET":
            return False
        if request.path == "/api/platform-status":
            self._send(200, _no_project_platform_projection(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path in {"/api/dashboard-snapshot", "/api/status"}:
            self._send(200, _no_project_console_snapshot(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path == "/api/events":
            self._stream_no_project_console_events()
            return True
        if request.path == "/health":
            report = status(self.server.data_root)  # type: ignore[attr-defined]
            self._send(200 if report["store"] == "ready" else 503, report, str(report["instance_id"]))
            return True
        if request.path == "/api/configuration":
            # CENTRAL-only settings presently supported by this phase.  The
            # root-local dashboard configuration is intentionally unavailable.
            self._send(200, _central_console_configuration(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path == "/api/execution-runtime-status":
            self._send(200, dashboard._execution_runtime_status())
            return True
        if request.path == "/api/github-rate-limit":
            self._send(200, dashboard._github_rate_limit_status())
            return True
        if request.path == "/api/provider-login-status":
            self._send(200, {"providers": _central_provider_readiness(self.server.data_root)})  # type: ignore[attr-defined]
            return True
        if request.path in {"/api/process-metrics", "/api/usage"}:
            self._send(200, {"scope": "PLATFORM", "available": False})
            return True
        if request.path in {"/api/logs/dashboard", "/api/logs/inbox"}:
            component = request.path.rsplit("/", 1)[-1]
            self._send(200, _central_console_component_logs(self.server.data_root, component) or {"error": "LOG_COMPONENT_UNKNOWN"})  # type: ignore[attr-defined]
            return True
        return False

    def _send_central_database_backup(self) -> None:
        snapshot = central_database.snapshot(self.server.data_root)  # type: ignore[attr-defined]
        if snapshot is None:
            self._send(503, {"error": "CENTRAL_DATABASE_UNAVAILABLE"})
            return
        filename = f"engineering-platform-central-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Disposition", _attachment_content_disposition(filename))
        self.send_header("Content-Length", str(len(snapshot)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        self.wfile.write(snapshot)

    def _central_database_configuration(self, method: str) -> bool:
        request = urlsplit(self.path)
        if request.path == "/api/central-database/download" and method == "do_GET":
            self._send_central_database_backup()
            return True
        if request.path == "/api/central-database/open-directory" and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                if self.rfile.read(int(self.headers.get("Content-Length", "0"))) != b"{}":
                    raise ValueError
                self._send(202, _open_central_database_directory(self.server.data_root))  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError):
                self._send(409, {"error": "CENTRAL_DATABASE_DIRECTORY_UNAVAILABLE"})
            return True
        if request.path != "/api/central-database/configuration":
            return False
        if method == "do_GET":
            self._send(200, central_database.maintenance_configuration(self.server.data_root))  # type: ignore[attr-defined]
            return True
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            result = central_database.update_maintenance_configuration(
                self.server.data_root, payload.get("interval_seconds"),  # type: ignore[attr-defined]
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID"})
            return True
        self._send(200, result)
        return True

    def _stream_console_events(self, root: Path, project_id: str) -> None:
        """Stream preserved dashboard state with the selected CENTRAL FIFO."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            stream_interval = int(dashboard.dashboard_configuration(root)["dashboard_stream_interval_seconds"])
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                snapshot = _with_console_queue(
                    dashboard._sse_snapshot(root),
                    queue=_console_queue_projection(self.server.data_root, project_id), data_root=self.server.data_root,  # type: ignore[attr-defined]
                )
                if snapshot != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + snapshot + b"\n\n")
                    self.wfile.flush()
                    previous = snapshot
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                interval = int(dashboard.dashboard_configuration(root)["dashboard_stream_interval_seconds"])
                if interval != stream_interval:
                    self.wfile.write(f"retry: {interval * 1000}\n\n".encode())
                    self.wfile.flush()
                    stream_interval = interval
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_no_project_console_events(self) -> None:
        """Send a CENTRAL-only event that completes the shared Console shell."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("EP-Console-Route-Owner", "PLATFORM")
        self.end_headers()
        try:
            stream_interval = int(
                central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                    "dashboard_stream_interval_seconds"
                ]
            )
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                payload = json.dumps(
                    _no_project_console_snapshot(self.server.data_root), separators=(",", ":")  # type: ignore[attr-defined]
                ).encode("utf-8")
                if payload != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + payload + b"\n\n")
                    self.wfile.flush()
                    previous = payload
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_project_console_events(self, project_id: str) -> None:
        """Keep the selected project's CENTRAL-only dashboard stream alive."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("EP-Console-Route-Owner", "PLATFORM")
        self.end_headers()
        try:
            stream_interval = int(
                central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                    "dashboard_stream_interval_seconds"
                ]
            )
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                payload = json.dumps(
                    _central_console_project_snapshot(self.server.data_root, project_id),  # type: ignore[attr-defined]
                    separators=(",", ":"),
                ).encode("utf-8")
                if payload != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + payload + b"\n\n")
                    self.wfile.flush()
                    previous = payload
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _delegate_dashboard(self, method: str) -> None:
        """Route the transitional Console after CENTRAL validates its scope."""
        request = urlsplit(self.path)
        # Resolve ownership once, before any project identity is read.  The
        # header makes the runtime contract observable to browser/integration
        # coverage without granting a selected project any authority.
        self._console_route = console_route_ownership.route_owner(method.removeprefix("do_"), request.path)
        if method == "do_GET" and self._send_console_asset(request):
            return
        # Platform health is deliberately independent of the browser's
        # selected project preference, so the same components remain visible
        # in both Console modes.
        if method == "do_GET" and request.path == "/health":
            report = status(self.server.data_root)  # type: ignore[attr-defined]
            self._send(200 if report["store"] == "ready" else 503, report, str(report["instance_id"]))
            return
        component_match = re.fullmatch(r"/api/components/([a-z_]+)/details", request.path)
        if component_match and component_match.group(1) not in PLATFORM_COMPONENT_IDS:
            component_match = None
        if method == "do_GET" and component_match:
            detail = _platform_component_detail(self.server.data_root, component_match.group(1))  # type: ignore[attr-defined]
            self._send(200, detail) if detail is not None else self._send(404, {"error": "COMPONENT_UNKNOWN"})
            return
        if self._central_database_configuration(method):
            return
        if request.path == "/api/provider-capacity":
            if method != "do_GET":
                self._send(405, {"error": "METHOD_NOT_ALLOWED"})
            else:
                self._send(200, _provider_capacity_projection(self.server.data_root))  # type: ignore[attr-defined]
            return
        if request.path == "/api/provider-login-status" and method == "do_GET":
            self._send(200, {"providers": _central_provider_readiness(self.server.data_root)})  # type: ignore[attr-defined]
            return
        if request.path == "/api/execution-runtime-status" and method == "do_GET":
            # Validation is an installation capability, independent of the
            # selected project.  Keep it out of the legacy checkout delegate.
            self._send(200, dashboard._execution_runtime_status())
            return
        if request.path == "/api/execution-runtime/repair" and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                if self.rfile.read(int(self.headers.get("Content-Length", "0"))) != b"{}":
                    raise ValueError
                runtime = dashboard._execution_runtime_status()
                if runtime["state"] != "READY":
                    raise ValueError
            except (ValueError, OSError):
                self._send(409, {"error": "EXECUTION_RUNTIME_UNAVAILABLE"})
                return
            self._send(200, {"rechecked": True, "runtime": runtime, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-login/repair" and method == "do_POST":
            # Provider installation and interactive sign-in are host-wide
            # operations.  They must never fall through to the historical
            # checkout-bound dashboard handler: on the <geen> projection that
            # handler rejects the request for lack of a selected project and
            # the subsequent readiness refresh misleadingly becomes a check
            # failure.
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                _central_provider_repair(self.server.data_root, payload)  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "PROVIDER_REPAIR_UNAVAILABLE"})
                return
            self._send(202, {"started": True, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-login/logout" and method == "do_POST":
            # Logout is the companion host-wide action to login.  Do not let
            # the installed no-project Console fall through to the retired
            # checkout handler, which rejects it before the CLI can run.
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                _central_provider_logout(self.server.data_root, payload)  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "PROVIDER_LOGOUT_UNAVAILABLE"})
                return
            self._send(200, {"logged_out": True, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-capacity/configuration":
            if method == "do_GET":
                self._send(200, central_database.capacity_configuration(self.server.data_root))  # type: ignore[attr-defined]
                return
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
                reserve = payload.get("codex_capacity_reserve_percent") if isinstance(payload, dict) else None
                live = _provider_capacity_projection(self.server.data_root)  # type: ignore[attr-defined]
                remaining = dashboard._remaining_rate_limit_capacity(live["rate_limits"])
                if not isinstance(reserve, int) or isinstance(reserve, bool) or (reserve and (remaining is None or reserve > remaining)):
                    raise ValueError
                self._send(200, central_database.update_capacity_configuration(self.server.data_root, reserve))  # type: ignore[attr-defined]
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "CODEX_CAPACITY_RESERVE_INVALID"})
            return
        if request.path == "/api/configuration" and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"key", "value", "previous"}:
                    raise ValueError
                result = central_database.update_console_interval_configuration(
                    self.server.data_root, payload["key"], payload["value"],
                )  # type: ignore[attr-defined]
                self._send(200, result)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "CONSOLE_CONFIGURATION_INVALID"})
            return
        if method == "do_POST" and request.path == "/api/runtime-directory/open":
            # This action historically resolved the first bound checkout.
            # The installed CENTRAL Console deliberately has no root-bound
            # runtime action; runtime paths remain display-only diagnostics.
            self._send(410, {"error": "RUNTIME_DIRECTORY_RETIRED"})
            return
        selected = self.headers.get("X-Engineering-Platform-Project")
        if not selected:
            selected = (parse_qs(request.query).get("project") or [None])[0]
        # Listing projects is a CENTRAL-only operation.  Do not validate or
        # inspect any checkout until a selected project needs a transitional
        # project route below.
        projects = _console_projects(self.server.data_root)  # type: ignore[attr-defined]
        project_ids = {item["project_id"] for item in projects}
        if method == "do_GET" and isinstance(selected, str) and selected in project_ids:
            # Slice B: the core project read model is available even when its
            # checkout has been deleted or rebound.  Do this before the
            # transitional handler can resolve a root.
            if request.path == "/":
                document = _selected_project_console_document(selected, projects, self.server.data_root)  # type: ignore[attr-defined]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(document)
                return
            if request.path == "/api/configuration":
                self._send(200, _central_console_configuration(self.server.data_root))  # type: ignore[attr-defined]
                return
            if request.path in {"/api/logs/dashboard", "/api/logs/inbox"}:
                component = request.path.rsplit("/", 1)[-1]
                payload = _central_console_component_logs(self.server.data_root, component)  # type: ignore[attr-defined]
                if payload is None:
                    self._send(404, {"error": "LOG_COMPONENT_UNKNOWN"})
                else:
                    self._send(200, payload)
                return
            if request.path in {"/api/dashboard-snapshot", "/api/status"}:
                self._send(200, _central_console_project_snapshot(self.server.data_root, selected))  # type: ignore[attr-defined]
                return
            if request.path == "/api/prompt-history":
                snapshot = _central_console_project_snapshot(self.server.data_root, selected)  # type: ignore[attr-defined]
                # Keep the established Console list contract while changing
                # only its authority source.
                encoded = json.dumps(snapshot["runs"], separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)
                return
            report_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/report", request.path)
            if report_match:
                content = _central_console_report(self.server.data_root, selected, report_match.group(1))  # type: ignore[attr-defined]
                if content is None:
                    self._send(404, {"error": "REPORT_NOT_FOUND"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    _report_content_disposition(report_match.group(1)),
                )
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            chat_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/chat", request.path)
            if chat_match:
                messages = _central_console_chat_history(self.server.data_root, selected, chat_match.group(1))  # type: ignore[attr-defined]
                if messages is None:
                    self._send(404, {"error": "RUN_NOT_FOUND"})
                else:
                    self._send(200, {"messages": messages, "source": "CENTRAL"})
                return
            detail_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/details", request.path)
            if detail_match:
                detail = _central_console_run_detail(self.server.data_root, selected, detail_match.group(1))  # type: ignore[attr-defined]
                if detail is None:
                    self._send(404, {"error": "RUN_NOT_FOUND"})
                else:
                    self._send(200, {"project_id": selected, "run": detail, "source": "CENTRAL"})
                return
            telemetry_match = re.fullmatch(r"/api/telemetry/([0-9]{4}-[0-9]{2}-[0-9]{2})", request.path)
            if telemetry_match:
                detail = _central_console_telemetry_detail(
                    self.server.data_root, selected, telemetry_match.group(1),  # type: ignore[attr-defined]
                )
                if detail is None:
                    self._send(404, {"error": "TELEMETRY_NOT_FOUND"})
                else:
                    self._send(200, detail)
                return
            if request.path == "/api/events":
                self._stream_project_console_events(selected)
                return
        if method == "do_POST" and isinstance(selected, str) and selected in project_ids and (
            request.path == "/api/configuration" or request.path.startswith("/api/logs/")
        ):
            # The local dashboard's mutable metadata/log controls have no
            # CENTRAL contract yet.  Fail closed rather than mutating a
            # checkout-local store for compatibility.
            self._send(405, {"error": "CONSOLE_CONFIGURATION_MUTATION_UNAVAILABLE"})
            return
        if method == "do_POST" and request.path in {"/api/execution-dismiss", "/api/execution-retry"}:
            if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 256:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                run_id = payload.get("run_id") if isinstance(payload, dict) and set(payload) == {"run_id"} else None
                if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
                    raise ValueError
                if request.path == "/api/execution-dismiss":
                    result: dict[str, object] = dismiss_operator_gate(
                        self.server.data_root, project_id=selected, run_id=run_id,  # type: ignore[attr-defined]
                    )
                else:
                    result = retry_operator_gate(
                        self.server.data_root, project_id=selected, run_id=run_id,  # type: ignore[attr-defined]
                    ).to_dict()
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "INVALID_REQUEST"})
                return
            except ParityLifecycleDispatchError as error:
                self._send(409, {"error": str(error)})
                return
            self._send(200, result)
            return
        if isinstance(selected, str) and selected in project_ids:
            # No supported CENTRAL Console route may fall through to the
            # retained dashboard handler.  New routes must be added above
            # with an explicit Server/CENTRAL authority classification.
            self._send(404 if method == "do_GET" else 405, {"error": "CENTRAL_CONSOLE_ROUTE_UNAVAILABLE"})
            return
        if method == "do_POST" and request.path == "/api/dashboard-translate":
            if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 4096:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"locale", "texts"}:
                    raise ValueError
                translations = dashboard_translation.translate(payload["locale"], payload["texts"])
            except dashboard_translation.DashboardTranslationError as error:
                status_code = 400 if str(error).endswith(("LOCALE_INVALID", "REQUEST_INVALID")) else 503
                self._send(status_code, {"error": str(error)})
                return
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "DASHBOARD_TRANSLATION_REQUEST_INVALID"})
                return
            self._send(200, {"translations": translations})
            return
        if method == "do_GET" and request.path == "/" and selected in {None, ""}:
            # No selection is a valid view.  It renders only the host-wide
            # controls and never substitutes the first project for content.
            document = _no_project_console_document(projects, self.server.data_root)  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(document)
            return
        if selected in {None, ""}:
            if self._no_project_platform_route(method, request):
                return
            self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
            return
        if not isinstance(selected, str) or selected not in project_ids:
            self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
            return
        # Reaching this point would mean a route escaped the explicit Console
        # projection table above. Never restore the historical root delegate.
        self._send(404 if method == "do_GET" else 405, {"error": "CENTRAL_CONSOLE_ROUTE_UNAVAILABLE"})

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path == "/diagnostics/topology":
            body = _operations_console_document()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/operations/projects":
            try:
                self._send(200, operations_projection(self.server.data_root), initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except ServerConfigurationError:
                self._send(503, {"error": "operations projection unavailable"})
            return
        if request.path == "/" or request.path.startswith("/api/") or request.path.startswith("/assets/") or request.path in {"/health", "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
            self._delegate_dashboard("do_GET")
            return
        if self.path not in {"/healthz", "/readyz"}:
            self.send_error(404)
            return
        try:
            report = self._status()
        except ServerConfigurationError:
            self._send(503, {"healthy": False, "ready": False})
            return
        self._send(200, report, str(report["instance_id"]))

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path.startswith("/api/"):
            self._delegate_dashboard("do_POST")
            return
        if self.path.startswith("/v1/projects/") and self.path.endswith("/submissions"):
            parts = self.path.split("/")
            if len(parts) != 5 or not parts[3]:
                self._send(404, {"error": "not found"})
                return
            project_id = parts[3]
            try:
                if self.headers.get_content_type() != "application/json":
                    raise submission_service.SubmissionError("UNSUPPORTED_MEDIA_TYPE", 415)
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= 131072:
                    raise submission_service.SubmissionError("PAYLOAD_TOO_LARGE", 413)
                raw = self.rfile.read(length)
                if b"\0" in raw:
                    raise submission_service.SubmissionError("MALFORMED_REQUEST")
                payload = json.loads(raw.decode("utf-8"))
                authorization = self.headers.get("Authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else None
                # CLI uses this same authenticated HTTP boundary, but the
                # durable receipt must retain the original adapter.  It is
                # observational provenance only: callers cannot select an
                # execution implementation through this header.
                transport = self.headers.get("EP-Submission-Transport", "HTTP")
                with sqlite3.connect(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    if _authenticated_consumer(connection, token, project_id) is None:
                        raise submission_service.SubmissionError("UNAUTHENTICATED", 401)
                    request = submission_service.request_from_mapping(project_id, payload, transport=transport)
                    result = submission_service.submit(connection, request)
                self._send(200, result.to_dict(), initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except UnicodeDecodeError:
                self._send(400, {"error": "MALFORMED_REQUEST"})
            except json.JSONDecodeError:
                self._send(400, {"error": "MALFORMED_REQUEST"})
            except submission_service.SubmissionError as error:
                self._send(error.status, {"error": error.code})
            return
        routes = {"/v1/agent/pair": agent_trust.pair, "/v1/agent/register": agent_trust.register, "/v1/agent/heartbeat": agent_trust.heartbeat, "/v1/agent/attachment": agent_trust.register_attachment}
        action = routes.get(self.path)
        if action is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                raise agent_trust.AgentTrustError("request body is invalid")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
            with sqlite3.connect(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                result = action(connection, body) if action is agent_trust.pair else action(connection, body, token)
            self._send(200, result, initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
        except (ValueError, OSError, json.JSONDecodeError, agent_trust.AgentTrustError):
            self._send(400 if self.path == "/v1/agent/pair" else 401, {"error": "agent request rejected"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(data_root: Path) -> int:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    os.environ[SERVER_ENVIRONMENT_DATA_ROOT] = str(data_root.resolve())
    os.environ[MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT] = config.managed_codex_cli_prefix
    server = http.server.ThreadingHTTPServer((config.bind_host, config.bind_port), _HealthHandler)
    server.data_root = data_root.resolve()  # type: ignore[attr-defined]
    worker = LifecycleWorker(data_root)
    # The File Inbox is an installed Server child, not a Dashboard or
    # checkout-owned watcher.  Its heartbeat is the source for its platform
    # component health; a prior successful file is never treated as liveness.
    inbox_service = file_inbox.FileInboxService(
        data_root / FILE_INBOX_DIRECTORY,
        server=f"http://{config.bind_host}:{config.bind_port}",
        credential=os.environ.get("EP_CONSUMER_TOKEN"),
    )
    server.lifecycle_worker = worker  # type: ignore[attr-defined]
    _write_json(data_root / SERVER_RUNTIME_FILENAME, {"pid": os.getpid(), "instance_id": identity.instance_id, "started_at": _utcnow()})
    def stop(_signum: int, _frame: object) -> None:
        # ``shutdown`` must run outside the serve_forever thread.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    inbox_service.start()
    try:
        server.serve_forever()
    finally:
        inbox_service.stop()
        worker.stop()
        server.server_close()
        (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
    return 0


def start(data_root: Path) -> dict[str, object]:
    current = status(data_root)
    if current["running"]:
        return current
    # The installed entrypoint supplies the interpreter.  Run from the
    # installation-owned data root and discard Python import overrides so a
    # caller's checkout can never become the child Server's import authority.
    runtime_root = data_root.resolve()
    configuration = ServerConfiguration.load(runtime_root)
    # npm is the preserved managed-runtime installer.  These are fixed host
    # tool directories, never a provider-executable fallback or caller PATH.
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT: configuration.managed_codex_cli_prefix,
        SERVER_ENVIRONMENT_DATA_ROOT: str(runtime_root),
    }
    if home := os.environ.get("HOME"):
        environment["HOME"] = home
    # Unit tests exercise the lifecycle from an unpackaged source tree.  This
    # explicit test-only bridge is never inherited by an installed process.
    if "unittest" in sys.argv[0]:
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen([sys.executable, "-m", "engineering_platform.server", "serve", "--data-root", str(runtime_root)], cwd=str(runtime_root), env=environment, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603
    _CHILDREN[child.pid] = child
    for _ in range(40):
        time.sleep(0.05)
        current = status(data_root)
        if current["running"]:
            return current
    raise RuntimeError("EP Server did not become ready.")


def stop(data_root: Path) -> dict[str, object]:
    runtime = _runtime(data_root)
    if runtime and _alive(runtime.get("pid")):
        os.kill(int(runtime["pid"]), signal.SIGTERM)
        child = _CHILDREN.pop(int(runtime["pid"]), None)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for _ in range(40):
            time.sleep(0.05)
            if not _alive(runtime["pid"]):
                break
    (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
    return status(data_root)


def health(data_root: Path) -> dict[str, object]:
    result = status(data_root)
    if not result["running"]:
        return {**result, "healthy": False, "ready": False}
    bind = result["bind"]
    try:
        # Server configuration permits loopback host only.
        with urlopen(f"http://{bind['host']}:{bind['port']}/readyz", timeout=1) as response:  # nosec B310
            response.read()
        return {**result, "healthy": True, "ready": True}
    except (URLError, OSError):
        return {**result, "healthy": False, "ready": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engineering-platform-server", description="Manage the standalone Engineering Platform Server foundation")
    parser.add_argument("command", choices=("init", "start", "serve", "stop", "status", "health", "pairing-create", "agent-status", "agent-revoke", "agent-reset", "topology", "submission-diagnose", "bootstrap-topology", "register-topology", "provision-declaration", "issue-consumer-credential", "bind-repository", "rebind-repository", "unbind-repository", "resolve-repository"))
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--bind-port", type=int, default=8765)
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--repository-id")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--consumer-id")
    parser.add_argument("--submission-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = {"instance_id": initialize(args.data_root, bind_host=args.bind_host, bind_port=args.bind_port).instance_id, "initialized": True}
        elif args.command == "start":
            initialize(args.data_root)
            configuration = ServerConfiguration.load(args.data_root)
            if (configuration.bind_host, configuration.bind_port) != (args.bind_host, args.bind_port):
                _write_json(args.data_root / SERVER_CONFIGURATION_FILENAME, {
                    "version": configuration.version, "bind_host": args.bind_host,
                    "bind_port": args.bind_port,
                    "managed_codex_cli_prefix": configuration.managed_codex_cli_prefix,
                })
            result = start(args.data_root)
        elif args.command == "serve": return serve(args.data_root)
        elif args.command == "stop": result = stop(args.data_root)
        elif args.command == "status": result = status(args.data_root)
        elif args.command == "health": result = health(args.data_root)
        elif args.command == "topology":
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.topology(connection)
        elif args.command == "submission-diagnose":
            if not args.submission_id:
                raise ServerConfigurationError("--submission-id is required for submission diagnostics.")
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                row = connection.execute("SELECT s.project_id,s.repository_id,s.state,s.admission,d.run_id,d.state,d.operator_resolution FROM ep_submissions s LEFT JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id WHERE s.submission_id=?", (args.submission_id,)).fetchone()
                if row is None:
                    raise ServerConfigurationError("UNKNOWN_SUBMISSION")
                project_id, repository_id, state, admission, run_id, dispatch_state, resolution = row
                blocked = connection.execute("SELECT run_id,state FROM ep_parity_lifecycle_dispatches WHERE project_id=? AND state IN ('CLAIMED','RUNNING','BLOCKED','FAILED') AND run_id!=? ORDER BY updated_at LIMIT 1", (project_id, run_id or "")).fetchone()
            early = None
            if run_id:
                early_path = args.data_root / "artifacts" / "projects" / str(project_id) / "runs" / str(run_id) / "early-runner-failure.json"
                if early_path.is_file():
                    try:
                        early = json.loads(early_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        early = {"diagnostic_code": "EARLY_FAILURE_EVIDENCE_UNAVAILABLE"}
            result = {"submission_id": args.submission_id, "project_id": project_id, "repository_id": repository_id, "submission_state": state, "admission": admission, "run_id": run_id, "dispatch_state": dispatch_state, "operator_resolution": resolution, "lane_blocker": {"run_id": blocked[0], "state": blocked[1]} if blocked else None, "early_failure": early, "worker_eligible": state == "QUEUED" and admission == "ADMITTED" and blocked is None}
        elif args.command == "register-topology":
            if args.declaration is None:
                raise ServerConfigurationError("--declaration is required for explicit topology registration.")
            initialize(args.data_root)
            declaration = args.declaration.read_text(encoding="utf-8")
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.register_server_local_topology(connection, declaration=declaration)
        elif args.command == "bootstrap-topology":
            if not args.project_id or not args.repository_id:
                raise ServerConfigurationError("--project-id and --repository-id are required for topology bootstrap.")
            initialize(args.data_root)
            declaration = {"schema_version": "1.0", "project": {"id": args.project_id, "authority_repository_id": args.repository_id}, "repository": {"id": args.repository_id, "role": "authority"}, "validation": {"kind": "none"}}
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.register_server_local_topology(connection, declaration=declaration)
        elif args.command == "issue-consumer-credential":
            if not args.project_id or not args.consumer_id:
                raise ServerConfigurationError("--project-id and --consumer-id are required for credential issuance.")
            initialize(args.data_root)
            from .submission_service import issue_consumer_credential
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = issue_consumer_credential(connection, consumer_id=args.consumer_id, project_id=args.project_id)
        elif args.command == "provision-declaration":
            if not args.project_id or not args.repository_id or args.path is None:
                raise ServerConfigurationError("--project-id, --repository-id and --path are required for declaration provisioning.")
            initialize(args.data_root)
            from .repository_attachment import config_path, load_repository_attachment, parse_repository_attachment
            root = args.path.resolve(strict=True)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                row = connection.execute("SELECT attachment_contract FROM ep_repository_registrations WHERE project_id=? AND repository_id=?", (args.project_id, args.repository_id)).fetchone()
            if row is None:
                raise ServerConfigurationError("CENTRAL_REPOSITORY_NOT_REGISTERED")
            declaration = json.loads(str(row[0]))
            parse_repository_attachment(declaration)
            target = config_path(root)
            if target.exists():
                existing = load_repository_attachment(root)
                if (existing.project_id, existing.repository_id) != (args.project_id, args.repository_id):
                    raise ServerConfigurationError("REPOSITORY_DECLARATION_CONFLICT")
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_text(json.dumps(declaration, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                target.chmod(0o600)
            result = {"project_id": args.project_id, "repository_id": args.repository_id, "path": str(target), "result": "PROVISIONED"}
        elif args.command in {"bind-repository", "rebind-repository", "unbind-repository", "resolve-repository"}:
            if not args.project_id or not args.repository_id:
                raise ServerConfigurationError("--project-id and --repository-id are required for local binding commands.")
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command in {"bind-repository", "rebind-repository"}:
                    if args.path is None:
                        raise ServerConfigurationError("--path is required when binding a repository.")
                    binding = local_repository_binding.bind_local_repository(connection, project_id=args.project_id, repository_id=args.repository_id, local_root=args.path, data_root=args.data_root, rebind=args.command == "rebind-repository")
                    result = {"project_id": binding.project_id, "repository_id": binding.repository_id, "state": binding.state}
                elif args.command == "unbind-repository":
                    local_repository_binding.unbind_local_repository(connection, project_id=args.project_id, repository_id=args.repository_id)
                    result = {"project_id": args.project_id, "repository_id": args.repository_id, "state": "UNBOUND"}
                else:
                    binding = local_repository_binding.resolve_execution_repository(connection, project_id=args.project_id, repository_id=args.repository_id, data_root=args.data_root)
                    result = {"project_id": binding.project_id, "repository_id": binding.repository_id, "state": binding.state}
        else:
            if not args.agent_id:
                raise ServerConfigurationError("--agent-id is required for Agent lifecycle commands.")
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command == "pairing-create": result = agent_trust.create_pairing_code(connection, args.agent_id)
                elif args.command == "agent-status": result = agent_trust.registration_status(connection, args.agent_id)
                elif args.command == "agent-revoke": result = {"agent_id": args.agent_id, "revoked": agent_trust.revoke(connection, args.agent_id)}
                else: result = {"agent_id": args.agent_id, "reset": agent_trust.reset(connection, args.agent_id)}
    except (OSError, RuntimeError, ServerConfigurationError, local_repository_binding.LocalRepositoryBindingError) as error:
        print(json.dumps({"error": str(error), "ready": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
