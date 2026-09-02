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
import signal
import sqlite3
# The lifecycle starts this module with a fixed argv; no shell is used.
import subprocess  # nosec B404
import sys
import time
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit
from uuid import uuid4

from . import agent_trust
from . import dashboard
from . import local_repository_binding
from . import project_topology
from . import submission_service
from . import managed_codex_runtime
from .lifecycle_worker import LifecycleWorker, WORKER_RUNNING
from .local_api_credentials import verifier


SERVER_CONFIGURATION_FILENAME = "server.json"
SERVER_IDENTITY_FILENAME = "runtime-identity.json"
SERVER_RUNTIME_FILENAME = "runtime.json"
SERVER_DATABASE_FILENAME = "engineering.db"
SERVER_CONFIGURATION_VERSION = 1
# ADR-0026 defines the first standalone store as the canonical schema-40
# product definitions plus immutable control provenance.  This server-owned
# bootstrap is deliberately separate from the retired DJConnect migration
# machinery: it creates a clean installation only and never accepts a source
# database path.
SERVER_STORE_SCHEMA_VERSION = 45
SERVER_ENVIRONMENT_DATA_ROOT = "EP_SERVER_DATA_ROOT"
_CHILDREN: dict[int, subprocess.Popen[object]] = {}


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

    @classmethod
    def load(cls, data_root: Path) -> "ServerConfiguration":
        path = data_root / SERVER_CONFIGURATION_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server configuration is unavailable.") from error
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "bind_host", "bind_port"}
            or raw["version"] != SERVER_CONFIGURATION_VERSION
            or not isinstance(raw["bind_host"], str)
            or raw["bind_host"] != "127.0.0.1"
            or not isinstance(raw["bind_port"], int)
            or not 1 <= raw["bind_port"] <= 65535
        ):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        return cls(raw["version"], raw["bind_host"], raw["bind_port"])


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
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','LEGACY_FILE')),
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
        raise ServerConfigurationError("EP Server store is not a valid official schema-45 installation.")
    return {"schema_version": schema, "integrity": "PASS", "required_tables": sorted(SERVER_REQUIRED_TABLES), "required_indexes": sorted(SERVER_REQUIRED_INDEXES)}


def initialize(data_root: Path, *, bind_host: str = "127.0.0.1", bind_port: int = 8765) -> RuntimeIdentity:
    """Create or validate an empty, installation-owned server instance."""
    data_root = data_root.resolve()
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = data_root / SERVER_CONFIGURATION_FILENAME
    if not config_path.exists():
        if bind_host != "127.0.0.1" or not 1 <= bind_port <= 65535:
            raise ServerConfigurationError("EP Server initial bind configuration is invalid.")
        _write_json(config_path, asdict(ServerConfiguration(SERVER_CONFIGURATION_VERSION, bind_host, bind_port)))
    ServerConfiguration.load(data_root)
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
                    if current_schema not in {41, 42, 43, 44, SERVER_STORE_SCHEMA_VERSION}:
                        raise ServerConfigurationError(
                            "EP Server store is not a valid official schema-45 installation."
                        )
                    if current_schema == SERVER_STORE_SCHEMA_VERSION:
                        validate_store(data_root, identity)
                        return identity
                    if current_schema in {42, 43, 44}:
                        with sqlite3.connect(database_path) as connection:
                            connection.execute("PRAGMA foreign_keys=ON")
                            connection.execute("BEGIN IMMEDIATE")
                            if current_schema == 42:
                                _migrate_schema_43(connection)
                            if current_schema in {42, 43}:
                                _migrate_schema_44(connection)
                            _migrate_schema_45(connection)
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


def status(data_root: Path) -> dict[str, object]:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    runtime = _runtime(data_root)
    running = bool(runtime and _alive(runtime.get("pid")))
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


def _bound_console_projects(data_root: Path) -> list[dict[str, str]]:
    """Return project identities with a validated schema-44 Console binding.

    Local roots are resolved only at this Server boundary and are never sent to
    the browser.  The historical dashboard remains root-based; CENTRAL decides
    which root is permitted for each request.
    """
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute("""SELECT p.project_id, r.repository_id
            FROM ep_project_registrations p JOIN ep_repository_registrations r
              ON r.project_id=p.project_id JOIN ep_local_repository_bindings b
              ON b.project_id=p.project_id AND b.repository_id=r.repository_id
            WHERE p.status='ACTIVE' AND b.state='BOUND'
            ORDER BY p.project_id, CASE r.role WHEN 'authority' THEN 0 ELSE 1 END, r.repository_id""").fetchall()
        projects: list[dict[str, str]] = []
        seen: set[str] = set()
        for project_id, repository_id in rows:
            if str(project_id) in seen:
                continue
            try:
                local_repository_binding.resolve_execution_repository(
                    connection, project_id=str(project_id), repository_id=str(repository_id), data_root=data_root,
                )
            except local_repository_binding.LocalRepositoryBindingError:
                continue
            seen.add(str(project_id))
            projects.append({"project_id": str(project_id), "repository_id": str(repository_id)})
    return projects


def _console_root(data_root: Path, project_id: str) -> Path:
    """Resolve the selected project through schema-44 before every request."""
    for project in _bound_console_projects(data_root):
        if project["project_id"] == project_id:
            with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
                return local_repository_binding.resolve_execution_repository(
                    connection, project_id=project_id, repository_id=project["repository_id"], data_root=data_root,
                ).local_root
    raise local_repository_binding.LocalRepositoryBindingError("CONSOLE_PROJECT_UNAVAILABLE")


def _historical_dashboard_path(request: object) -> str:
    """Remove the Server-only project selector before historical routing."""
    path = str(getattr(request, "path"))
    query = str(getattr(request, "query"))
    retained = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key != "project"]
    return path if not retained else f"{path}?{urlencode(retained, doseq=True)}"


def _console_document_transform(project_id: str, projects: list[dict[str, str]], root: Path):
    """Bind CENTRAL project scope to the historical title-bar selector.

    The Operations Console already owns its project selector in the title bar.
    The Server supplies its authoritative options and request scoping there;
    it must not add a second selector to the dashboard content.
    """
    options = "".join(
        f'<option value="{escape(item["project_id"], quote=True)}"'
        f'{" selected" if item["project_id"] == project_id else ""}>'
        f'{escape(item["project_id"])}</option>'
        for item in projects
    )
    boundary = f'''<script>(function(){{const project={json.dumps(project_id)},options={json.dumps(options)},nativeFetch=window.fetch.bind(window);window.fetch=(input,init={{}})=>{{const headers=new Headers(init.headers || (input instanceof Request ? input.headers : undefined));headers.set('X-Engineering-Platform-Project',project);return nativeFetch(input,{{...init,headers}})}};const NativeEventSource=window.EventSource;window.EventSource=function(url,config){{const target=new URL(url,window.location.href);target.searchParams.set('project',project);return new NativeEventSource(target,config)}};window.EventSource.prototype=NativeEventSource.prototype;window.addEventListener('DOMContentLoaded',()=>{{const select=document.getElementById('dashboardProject');if(!select)return;select.innerHTML=options;select.value=project;select.addEventListener('change',()=>{{const url=new URL(window.location.href);url.searchParams.set('project',select.value);window.location.assign(url)}})}})}})();</script>'''
    root_bytes = str(root).encode("utf-8")
    return lambda document: document.replace(root_bytes, b"Project-scoped local workspace").replace(
        b"</main>", boundary.encode("utf-8") + b"</main>", 1,
    )


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
        self.end_headers()
        self.wfile.write(encoded)

    def _delegate_dashboard(self, method: str) -> None:
        """Run the preserved handler after CENTRAL validates the selected scope."""
        request = urlsplit(self.path)
        selected = self.headers.get("X-Engineering-Platform-Project")
        if not selected:
            selected = (parse_qs(request.query).get("project") or [None])[0]
        projects = _bound_console_projects(self.server.data_root)  # type: ignore[attr-defined]
        project_ids = {item["project_id"] for item in projects}
        if not isinstance(selected, str) or selected not in project_ids:
            # Static package assets are scope-neutral and load before the
            # document's project-aware fetch wrapper exists.  Resolve a valid
            # bound root solely to reuse the installed asset handler; no
            # project data is read or disclosed by these routes.
            if (request.path == "/" or request.path.startswith("/assets/") or request.path in {"/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}) and projects:
                selected = projects[0]["project_id"]
            else:
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
        try:
            root = _console_root(self.server.data_root, selected)  # type: ignore[attr-defined]
            historical = dashboard.handler(
                root, document_transform=_console_document_transform(selected, projects, root),
            )
        except (OSError, ValueError, local_repository_binding.LocalRepositoryBindingError):
            self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
            return
        # The historical handler is deliberately reused verbatim.  Bind its
        # small private helpers to this request instance, then invoke its route
        # method so every historical asset, modal, SSE endpoint and action
        # remains available without a route-by-route copy.
        self._send = historical._send.__get__(self, type(self))  # type: ignore[method-assign]
        self._same_origin = historical._same_origin.__get__(self, type(self))  # type: ignore[attr-defined]
        # EventSource cannot supply the scope header.  The document wrapper
        # therefore uses `?project=...`; it is consumed above and must not
        # reach the historical routes, several of which correctly compare
        # their request path exactly (including `/api/events`).
        original_path = self.path
        self.path = _historical_dashboard_path(request)
        try:
            getattr(historical, method)(self)
        finally:
            self.path = original_path

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
    server = http.server.ThreadingHTTPServer((config.bind_host, config.bind_port), _HealthHandler)
    server.data_root = data_root.resolve()  # type: ignore[attr-defined]
    worker = LifecycleWorker(data_root)
    server.lifecycle_worker = worker  # type: ignore[attr-defined]
    _write_json(data_root / SERVER_RUNTIME_FILENAME, {"pid": os.getpid(), "instance_id": identity.instance_id, "started_at": _utcnow()})
    def stop(_signum: int, _frame: object) -> None:
        # ``shutdown`` must run outside the serve_forever thread.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    try:
        server.serve_forever()
    finally:
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
    # npm is the preserved managed-runtime installer.  These are fixed host
    # tool directories, never a provider-executable fallback or caller PATH.
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
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
    parser.add_argument("command", choices=("init", "start", "serve", "stop", "status", "health", "pairing-create", "agent-status", "agent-revoke", "agent-reset", "topology", "bind-repository", "rebind-repository", "unbind-repository", "resolve-repository"))
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--bind-port", type=int, default=8765)
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--repository-id")
    parser.add_argument("--path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = {"instance_id": initialize(args.data_root, bind_host=args.bind_host, bind_port=args.bind_port).instance_id, "initialized": True}
        elif args.command == "start": result = start(args.data_root)
        elif args.command == "serve": return serve(args.data_root)
        elif args.command == "stop": result = stop(args.data_root)
        elif args.command == "status": result = status(args.data_root)
        elif args.command == "health": result = health(args.data_root)
        elif args.command == "topology":
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.topology(connection)
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
