"""Finite Workspace discovery reads of the existing B8R/CENTRAL authority.

Issuance/revocation are installation-owner operations. HTTP reads never open a
creating connection, migrate a store, register topology, or execute work.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable

from . import storage
from .platform_version import CURRENT_PLATFORM_VERSION

CONTRACT_VERSION = "1.0"
AUDIENCE = "workspace"
SCHEMA_VERSION = 76
TABLE = "ep_workspace_discovery_grants"
FIELDS = frozenset({"project_identity", "repository_identity", "attachment_availability"})
IDENTITY_FIELDS = frozenset({"project_identity", "repository_identity"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_DOMAIN = b"engineering-platform.workspace-discovery.verifier.v1\0"


class DiscoveryError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise DiscoveryError("DISCOVERY_ID_INVALID")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ids(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > 100:
        raise DiscoveryError("DISCOVERY_PROJECT_SCOPE_INVALID")
    result = [_id(v) for v in values]
    if len(set(result)) != len(result):
        raise DiscoveryError("DISCOVERY_PROJECT_SCOPE_INVALID")
    return sorted(result)


def _fields(values: object) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise DiscoveryError("DISCOVERY_FIELD_SCOPE_INVALID")
    if len(values) != len(set(values)) or not IDENTITY_FIELDS <= set(values) <= FIELDS:
        raise DiscoveryError("DISCOVERY_FIELD_SCOPE_INVALID")
    return sorted(values)


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE ep_workspace_discovery_grants (
        grant_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
        consumer_id TEXT NOT NULL, workspace_instance_id TEXT NOT NULL,
        audience TEXT NOT NULL CHECK(audience='workspace'), project_ids TEXT NOT NULL,
        fields TEXT NOT NULL, issuance_operation_id TEXT NOT NULL UNIQUE,
        verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32),
        cursor_key BLOB NOT NULL CHECK(length(cursor_key)=32),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        revoked_at TEXT, revocation_operation_id TEXT UNIQUE)""")


def openapi_paths() -> dict[str, object]:
    """Describe only the two owner GET routes and their separate authority."""
    headers = [dict(name=name, **{"in": "header"}, required=True,
        schema={"type": "string"}) for name in (
            "EP-Discovery-Contract", "EP-Instance-ID", "EP-Consumer-ID",
            "Workspace-Instance-ID", "EP-Audience")]
    responses = {str(code): {"description": description} for code, description in (
        (200, "Closed packaged DiscoverySnapshot/TopologySnapshot v1.0; no execution permission"),
        (400, "Malformed, duplicate or unknown query/body/cursor"), (401, "Invalid, expired or revoked discovery bearer"),
        (403, "Consumer/audience/instance/project refusal"), (409, "Snapshot or topology conflict"),
        (422, "Unsupported contract or store schema"), (500, "CENTRAL corrupt or invalid"),
        (503, "CENTRAL unavailable; never an empty healthy list"))}
    return {
        "/v1/onboarding/projects": {"get": dict(summary="Read an explicitly scoped B8R project page",
            security=[{"discoveryBearer": []}], parameters=headers + [
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}},
                {"name": "cursor", "in": "query", "schema": {"type": "string", "maxLength": 1024}}],
            responses=responses, **{"x-response-json-schema": "urn:engineering-platform:workspace-project-discovery:1.0#/$defs/DiscoverySnapshot"})},
        "/v1/onboarding/projects/{project_id}/topology": {"get": dict(summary="Read one selected project in the same authorized snapshot",
            security=[{"discoveryBearer": []}], parameters=headers + [
                {"name": "project_id", "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}},
                {"name": "expected_topology_revision", "in": "query", "required": True, "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}],
            responses=responses, **{"x-response-json-schema": "urn:engineering-platform:workspace-project-discovery:1.0#/$defs/TopologySnapshot"})},
    }


def validate_schema(connection: sqlite3.Connection) -> None:
    expected = (
        ("grant_id", "TEXT", 0, 1), ("instance_id", "TEXT", 1, 0),
        ("consumer_id", "TEXT", 1, 0), ("workspace_instance_id", "TEXT", 1, 0),
        ("audience", "TEXT", 1, 0), ("project_ids", "TEXT", 1, 0),
        ("fields", "TEXT", 1, 0), ("issuance_operation_id", "TEXT", 1, 0),
        ("verifier", "BLOB", 1, 0), ("cursor_key", "BLOB", 1, 0), ("issued_at", "TEXT", 1, 0),
        ("expires_at", "TEXT", 1, 0), ("revoked_at", "TEXT", 0, 0),
        ("revocation_operation_id", "TEXT", 0, 0),
    )
    observed = tuple((r[1], r[2].upper(), r[3], r[5]) for r in connection.execute(
        "SELECT * FROM pragma_table_info(?)", (TABLE,)))
    unique = {tuple(r[0] for r in connection.execute(
        "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)))
        for index in connection.execute("SELECT * FROM pragma_index_list(?)", (TABLE,)) if index[2]}
    foreign = {(r[2], r[3], r[4]) for r in connection.execute(
        "SELECT * FROM pragma_foreign_key_list(?)", (TABLE,))}
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    sql = "" if row is None else "".join(row[0].lower().split())
    if (observed != expected or not {("grant_id",), ("issuance_operation_id",),
            ("verifier",), ("revocation_operation_id",)} <= unique
            or foreign != {("ep_installations", "instance_id", "instance_id")}
            or "check(length(verifier)=32)" not in sql or "check(length(cursor_key)=32)" not in sql
            or "check(audience='workspace')" not in sql):
        raise DiscoveryError("DISCOVERY_SCHEMA_INVALID", 500)


def issue(connection: sqlite3.Connection, *, grant_id: str, instance_id: str,
          consumer_id: str, workspace_instance_id: str, project_ids: list[str],
          fields: list[str], operation_id: str, ttl_seconds: int) -> dict[str, object]:
    for value in (grant_id, instance_id, consumer_id, workspace_instance_id, operation_id):
        _id(value)
    projects, selected_fields = _ids(project_ids), _fields(fields)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise DiscoveryError("DISCOVERY_LIFETIME_INVALID")
    require_current_instance(connection, instance_id)
    for project_id in projects:
        if connection.execute("SELECT 1 FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'", (project_id,)).fetchone() is None:
            raise DiscoveryError("DISCOVERY_PROJECT_UNAVAILABLE", 403)
    if connection.execute("SELECT 1 FROM ep_workspace_discovery_grants WHERE grant_id=? OR issuance_operation_id=?", (grant_id, operation_id)).fetchone():
        raise DiscoveryError("DISCOVERY_ALREADY_ISSUED_USE_STATUS", 409)
    token = secrets.token_urlsafe(32)
    verifier = hashlib.sha256(_DOMAIN + token.encode("ascii")).digest()
    now = _now()
    connection.execute("""INSERT INTO ep_workspace_discovery_grants
        (grant_id,instance_id,consumer_id,workspace_instance_id,audience,project_ids,fields,
         issuance_operation_id,verifier,cursor_key,issued_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (grant_id, instance_id, consumer_id, workspace_instance_id, AUDIENCE,
         _canonical(projects).decode(), _canonical(selected_fields).decode(), operation_id,
         verifier, secrets.token_bytes(32), now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()))
    return {**status(connection, grant_id=grant_id), "credential": token}


def status(connection: sqlite3.Connection, *, grant_id: str) -> dict[str, object]:
    _id(grant_id)
    row = connection.execute("""SELECT instance_id,consumer_id,workspace_instance_id,audience,
        project_ids,fields,issued_at,expires_at,revoked_at FROM ep_workspace_discovery_grants WHERE grant_id=?""", (grant_id,)).fetchone()
    if row is None:
        raise DiscoveryError("DISCOVERY_GRANT_UNKNOWN", 404)
    try:
        expires = datetime.fromisoformat(row[7])
        issued = datetime.fromisoformat(row[6])
        projects, selected_fields = _ids(json.loads(row[4])), _fields(json.loads(row[5]))
        for value in row[:3]:
            _id(value)
        if (expires.tzinfo is None or issued.tzinfo is None or row[3] != AUDIENCE
                or not timedelta(0) < expires - issued <= timedelta(days=1)):
            raise ValueError("Invalid finite grant")
    except (ValueError, TypeError) as error:
        raise DiscoveryError("DISCOVERY_GRANT_CORRUPT", 500) from error
    return dict(grant_id=grant_id, instance_id=row[0], consumer_id=row[1], workspace_instance_id=row[2],
        audience=row[3], project_ids=projects, fields=selected_fields,
        issued_at=row[6], expires_at=row[7], status="REVOKED" if row[8] else (
            "EXPIRED" if _now() >= expires else "ACTIVE"))


def revoke(connection: sqlite3.Connection, *, grant_id: str, operation_id: str) -> dict[str, object]:
    _id(grant_id); _id(operation_id)
    row = connection.execute("SELECT revoked_at,revocation_operation_id FROM ep_workspace_discovery_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None:
        raise DiscoveryError("DISCOVERY_GRANT_UNKNOWN", 404)
    if row[0] is not None:
        if row[1] != operation_id:
            raise DiscoveryError("DISCOVERY_REVOCATION_CONFLICT", 409)
    else:
        connection.execute("UPDATE ep_workspace_discovery_grants SET revoked_at=?,revocation_operation_id=? WHERE grant_id=?", (_now().isoformat(), operation_id, grant_id))
    return status(connection, grant_id=grant_id)


def require_current_instance(connection: sqlite3.Connection, expected_instance: str) -> str:
    """Noncreating owner/read context; initialization is a separate command."""
    _id(expected_instance)
    validate_schema(connection)
    metadata = dict(connection.execute("SELECT key,value FROM engineering_metadata WHERE key IN ('installation.instance_id','installation.schema_version')"))
    if metadata.get("installation.instance_id") != expected_instance:
        raise DiscoveryError("DISCOVERY_INSTANCE_MISMATCH", 403)
    schema = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0]
    installation = connection.execute("SELECT schema_version FROM ep_installations WHERE instance_id=?", (expected_instance,)).fetchone()
    if metadata.get("installation.schema_version") != str(SCHEMA_VERSION) or schema != SCHEMA_VERSION or installation != (SCHEMA_VERSION,):
        raise DiscoveryError("DISCOVERY_SCHEMA_UNSUPPORTED", 422)
    return expected_instance


def _authenticate(connection: sqlite3.Connection, token: object, headers: dict[str, str]) -> dict[str, object]:
    # Publication relies on rollback-journal readers blocking owner commits.
    # Never change a store's journal mode as a side effect of discovery reads.
    if connection.execute("PRAGMA journal_mode").fetchone()[0] not in {"delete", "truncate", "persist"}:
        raise DiscoveryError("DISCOVERY_JOURNAL_UNSUPPORTED", 422)
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
        raise DiscoveryError("UNAUTHENTICATED", 401)
    validate_schema(connection)
    value = hashlib.sha256(_DOMAIN + token.encode("ascii")).digest()
    row = connection.execute("SELECT grant_id,verifier FROM ep_workspace_discovery_grants WHERE verifier=?", (value,)).fetchone()
    if row is None or not hmac.compare_digest(bytes(row[1]), value):
        raise DiscoveryError("UNAUTHENTICATED", 401)
    scope = status(connection, grant_id=row[0])
    if scope["status"] != "ACTIVE":
        raise DiscoveryError("DISCOVERY_INACTIVE", 401)
    expected = {"EP-Instance-ID": scope["instance_id"], "EP-Consumer-ID": scope["consumer_id"],
                "Workspace-Instance-ID": scope["workspace_instance_id"], "EP-Audience": AUDIENCE}
    if any(headers.get(k) != v for k, v in expected.items()):
        raise DiscoveryError("DISCOVERY_SCOPE_MISMATCH", 403)
    require_current_instance(connection, str(scope["instance_id"]))
    return scope


def _topology(connection: sqlite3.Connection, scope: dict[str, object]) -> list[dict[str, object]]:
    projects = []
    now = _now()
    cutoff = now - timedelta(seconds=90)
    for project_id in scope["project_ids"]:
        row = connection.execute("SELECT attachment_contract FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'", (project_id,)).fetchone()
        if row is None:
            raise DiscoveryError("DISCOVERY_PROJECT_UNAVAILABLE", 403)
        authority = json.loads(row[0])["authority_repository_id"]
        _id(authority)
        repositories = []
        for repository_id, authority_id, role in connection.execute("SELECT repository_id,authority_repository_id,role FROM ep_repository_registrations WHERE project_id=? ORDER BY repository_id LIMIT 101", (project_id,)):
            _id(repository_id)
            if authority_id != authority or role not in {"authority", "child"}:
                raise DiscoveryError("DISCOVERY_TOPOLOGY_CORRUPT", 500)
            repositories.append(dict(repository_id=repository_id, role=role))
        if len(repositories) > 100:
            raise DiscoveryError("DISCOVERY_TOPOLOGY_LIMIT", 409)
        authorities = [r["repository_id"] for r in repositories if r["role"] == "authority"]
        if authorities != [authority]:
            raise DiscoveryError("DISCOVERY_TOPOLOGY_INCOMPLETE", 409)
        attachments = None
        if "attachment_availability" in scope["fields"]:
            attachments = []
            rows = connection.execute("""SELECT a.repository_id,a.agent_id,a.availability,g.state,g.last_seen_at
                FROM ep_agent_repository_attachments a JOIN ep_repository_registrations r ON r.repository_id=a.repository_id
                JOIN ep_agent_registrations g ON g.agent_id=a.agent_id WHERE r.project_id=?
                ORDER BY a.repository_id,a.agent_id LIMIT 101""", (project_id,)).fetchall()
            if len(rows) > 100:
                raise DiscoveryError("DISCOVERY_TOPOLOGY_LIMIT", 409)
            for repo, agent, available, state, last_seen in rows:
                seen = None
                if last_seen is not None:
                    try:
                        seen = datetime.fromisoformat(last_seen)
                        if seen.tzinfo is None:
                            raise ValueError("Timestamp lacks timezone")
                    except (ValueError, TypeError) as error:
                        raise DiscoveryError("DISCOVERY_TOPOLOGY_CORRUPT", 500) from error
                attachments.append(dict(repository_id=_id(repo), agent_id=_id(agent),
                    availability="AVAILABLE" if available == "AVAILABLE" and state != "REVOKED" and seen is not None and cutoff <= seen <= now else "UNAVAILABLE"))
        projects.append(dict(project_id=project_id, authority_repository_id=authority,
                             repositories=repositories, attachments=attachments))
    return projects


def _snapshot(connection: sqlite3.Connection, scope: dict[str, object], identity: tuple[int, int]) -> tuple[str, list[dict[str, object]]]:
    data = _topology(connection, scope)
    digest = hashlib.sha256(_canonical([CONTRACT_VERSION, identity, scope, data])).hexdigest()
    return digest, data


def _cursor(key: bytes, payload: dict[str, object]) -> str:
    data = _canonical(payload)
    signature = hmac.new(key, b"discovery.cursor.v1\0" + data, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(data + signature).decode("ascii").rstrip("=")


def _decode_cursor(key: bytes, value: str) -> dict[str, object]:
    try:
        if len(value) > 1024 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError
        data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(data[:-32])
        if not isinstance(payload, dict) or not hmac.compare_digest(_cursor(key, payload), value):
            raise ValueError
        return payload
    except (ValueError, UnicodeError):
        raise DiscoveryError("DISCOVERY_CURSOR_INVALID", 400) from None


def _identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def read(data_root: Path, *, token: str | None, headers: dict[str, str], query: dict[str, list[str]],
         project_id: str | None, publish: Callable[[dict[str, object], str, Callable[[], None]], None]) -> None:
    """Two fresh read snapshots; publication is fenced by the final shared lock.

    A committed revocation/source change before publication is observed on the
    second connection. Owner revocation cannot commit during the final locked
    publication. No state/cursor is written by the read.
    """
    if headers.get("EP-Discovery-Contract") != CONTRACT_VERSION:
        raise DiscoveryError("DISCOVERY_CONTRACT_UNSUPPORTED", 422)
    permitted = {"expected_topology_revision"} if project_id is not None else {"cursor", "limit"}
    if not set(query) <= permitted or any(len(v) != 1 for v in query.values()):
        raise DiscoveryError("DISCOVERY_QUERY_INVALID")
    if project_id is not None:
        _id(project_id)
        expected = query.get("expected_topology_revision", [""])[0]
        if _DIGEST.fullmatch(expected) is None:
            raise DiscoveryError("DISCOVERY_REVISION_REQUIRED")
    else:
        try:
            limit = int(query.get("limit", ["25"])[0])
        except ValueError:
            raise DiscoveryError("DISCOVERY_LIMIT_INVALID") from None
        if not 1 <= limit <= 100:
            raise DiscoveryError("DISCOVERY_LIMIT_INVALID")
    path = data_root / "epdata.sqlite"
    identity = _identity(path)
    with storage.sqlite_read_connection(path) as connection:
        connection.execute("BEGIN")
        scope = _authenticate(connection, token, headers)
        cursor_key = bytes(connection.execute("SELECT cursor_key FROM ep_workspace_discovery_grants WHERE grant_id=?", (scope["grant_id"],)).fetchone()[0])
        revision, data = _snapshot(connection, scope, identity)
    offset = 0
    if project_id is not None:
        if project_id not in scope["project_ids"]:
            raise DiscoveryError("DISCOVERY_PROJECT_DENIED", 403)
        if expected != revision:
            raise DiscoveryError("DISCOVERY_SNAPSHOT_CONFLICT", 409)
    elif "cursor" in query:
        cursor = _decode_cursor(cursor_key, query["cursor"][0])
        if set(cursor) != {"snapshot", "offset", "limit"} or type(cursor["offset"]) is not int or cursor["limit"] != limit or not 0 < cursor["offset"] < len(data):
            raise DiscoveryError("DISCOVERY_CURSOR_INVALID")
        if cursor["snapshot"] != revision:
            raise DiscoveryError("DISCOVERY_SNAPSHOT_CONFLICT", 409)
        offset = cursor["offset"]
    with storage.sqlite_read_connection(path) as connection:
        connection.execute("BEGIN")
        current = _authenticate(connection, token, headers)
        current_revision, current_data = _snapshot(connection, current, _identity(path))
        if current_revision != revision or current != scope or _identity(path) != identity:
            raise DiscoveryError("DISCOVERY_SNAPSHOT_CONFLICT", 409)
        # Recheck finite lifetime after data assembly, immediately before send.
        _authenticate(connection, token, headers)
        payload = dict(contract_version=CONTRACT_VERSION,
            producer={"id": "engineering-platform", "version": CURRENT_PLATFORM_VERSION},
            instance_id=scope["instance_id"], snapshot_id=revision, topology_revision=revision,
            scope=scope, observed_at=_now().isoformat())
        if project_id is None:
            page = current_data[offset:offset + limit]
            end = offset + len(page)
            payload.update(projects=[{k: p[k] for k in ("project_id", "authority_repository_id")} for p in page],
                next_cursor=_cursor(cursor_key, dict(snapshot=revision, offset=end, limit=limit)) if end < len(data) else None,
                completeness="PARTIAL_PAGE" if end < len(data) else "COMPLETE_PAGE")
        else:
            selected = next(p for p in current_data if p["project_id"] == project_id)
            payload.update(project={k: selected[k] for k in ("project_id", "authority_repository_id")},
                repositories=selected["repositories"], attachments=selected["attachments"],
                attachment_visibility="GRANTED" if selected["attachments"] is not None else "NOT_GRANTED",
                completeness="COMPLETE")
        def publication_check() -> None:
            if _authenticate(connection, token, headers) != scope:
                raise DiscoveryError("DISCOVERY_SNAPSHOT_CONFLICT", 409)
            if _identity(path) != identity:
                raise DiscoveryError("DISCOVERY_SNAPSHOT_CONFLICT", 409)
        publication_check()
        publish(payload, str(scope["instance_id"]), publication_check)
