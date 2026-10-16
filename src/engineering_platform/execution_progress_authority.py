"""Finite EXECUTION_PROGRESS_READ grants over existing CENTRAL identities.

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
SCHEMA_VERSION = 77
TABLE = "ep_execution_progress_grants"
FIELDS = frozenset({"identity", "progress", "result", "history"})
IDENTITY_FIELDS = FIELDS
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_DOMAIN = b"engineering-platform.workspace-execution-progress.verifier.v1\0"


class ProgressError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ProgressError("PROGRESS_ID_INVALID")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _selections(values: object) -> list[dict[str, object]]:
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ProgressError("PROGRESS_SELECTION_INVALID")
    result = []
    for entry in values:
        if not isinstance(entry, dict) or set(entry) != {"project_id", "authority_repository_id", "repository_ids", "mission_ids", "producer_ids"}:
            raise ProgressError("PROGRESS_SELECTION_INVALID")
        row = {key: _id(entry[key]) for key in ("project_id", "authority_repository_id")}
        for key in ("repository_ids", "mission_ids", "producer_ids"):
            ids = entry[key]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 32:
                raise ProgressError("PROGRESS_SELECTION_INVALID")
            row[key] = sorted(_id(v) for v in ids)
            if len(set(row[key])) != len(ids):
                raise ProgressError("PROGRESS_SELECTION_INVALID")
        result.append(row)
    if len({r["project_id"] for r in result}) != len(result):
        raise ProgressError("PROGRESS_SELECTION_INVALID")
    return sorted(result, key=lambda r: r["project_id"])


def require_selections(connection, selections):
    for entry in selections:
        project = connection.execute("SELECT attachment_contract FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'", (entry["project_id"],)).fetchone()
        if project is None:
            raise ProgressError("PROGRESS_PROJECT_DENIED", 403)
        if json.loads(project[0]).get("authority_repository_id") != entry["authority_repository_id"]:
            raise ProgressError("PROGRESS_AUTHORITY_CHANGED", 403)
        for repo in entry["repository_ids"]:
            row = connection.execute("SELECT project_id,authority_repository_id FROM ep_repository_registrations WHERE repository_id=?", (repo,)).fetchone()
            if row is None or tuple(row) != (entry["project_id"], entry["authority_repository_id"]):
                raise ProgressError("PROGRESS_REPOSITORY_DENIED", 403)


def _fields(values: object) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ProgressError("PROGRESS_FIELD_SCOPE_INVALID")
    if len(values) != len(set(values)) or not IDENTITY_FIELDS <= set(values) <= FIELDS:
        raise ProgressError("PROGRESS_FIELD_SCOPE_INVALID")
    return sorted(values)


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE ep_execution_progress_grants (
        grant_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
        consumer_id TEXT NOT NULL, workspace_instance_id TEXT NOT NULL,
        audience TEXT NOT NULL CHECK(audience='workspace'), selections TEXT NOT NULL,
        fields TEXT NOT NULL, issuance_operation_id TEXT NOT NULL UNIQUE,
        verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32),
        cursor_key BLOB NOT NULL CHECK(length(cursor_key)=32),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        revoked_at TEXT, revocation_operation_id TEXT UNIQUE)""")


def validate_schema(connection: sqlite3.Connection) -> None:
    expected = (
        ("grant_id", "TEXT", 0, 1), ("instance_id", "TEXT", 1, 0),
        ("consumer_id", "TEXT", 1, 0), ("workspace_instance_id", "TEXT", 1, 0),
        ("audience", "TEXT", 1, 0), ("selections", "TEXT", 1, 0),
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
        raise ProgressError("PROGRESS_SCHEMA_INVALID", 500)


def issue(connection: sqlite3.Connection, *, grant_id: str, instance_id: str,
          consumer_id: str, workspace_instance_id: str, selections: list[dict[str, object]],
          fields: list[str], operation_id: str, ttl_seconds: int) -> dict[str, object]:
    for value in (grant_id, instance_id, consumer_id, workspace_instance_id, operation_id):
        _id(value)
    projects, selected_fields = _selections(selections), _fields(fields)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise ProgressError("PROGRESS_LIFETIME_INVALID")
    require_current_instance(connection, instance_id)
    require_selections(connection, projects)
    if connection.execute("SELECT 1 FROM ep_execution_progress_grants WHERE grant_id=? OR issuance_operation_id=?", (grant_id, operation_id)).fetchone():
        raise ProgressError("PROGRESS_ALREADY_ISSUED_USE_STATUS", 409)
    if connection.execute("SELECT COUNT(*) FROM ep_execution_progress_grants").fetchone()[0] >= 128:
        raise ProgressError("PROGRESS_GRANT_LIMIT", 409)
    token = secrets.token_urlsafe(32)
    verifier = hashlib.sha256(_DOMAIN + token.encode("ascii")).digest()
    now = _now()
    connection.execute("""INSERT INTO ep_execution_progress_grants
        (grant_id,instance_id,consumer_id,workspace_instance_id,audience,selections,fields,
         issuance_operation_id,verifier,cursor_key,issued_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (grant_id, instance_id, consumer_id, workspace_instance_id, AUDIENCE,
         _canonical(projects).decode(), _canonical(selected_fields).decode(), operation_id,
         verifier, secrets.token_bytes(32), now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()))
    return {**status(connection, grant_id=grant_id), "credential": token}


def status(connection: sqlite3.Connection, *, grant_id: str) -> dict[str, object]:
    _id(grant_id)
    row = connection.execute("""SELECT instance_id,consumer_id,workspace_instance_id,audience,
        selections,fields,issued_at,expires_at,revoked_at FROM ep_execution_progress_grants WHERE grant_id=?""", (grant_id,)).fetchone()
    if row is None:
        raise ProgressError("PROGRESS_GRANT_UNKNOWN", 404)
    try:
        expires = datetime.fromisoformat(row[7])
        issued = datetime.fromisoformat(row[6])
        projects, selected_fields = _selections(json.loads(row[4])), _fields(json.loads(row[5]))
        for value in row[:3]:
            _id(value)
        if (expires.tzinfo is None or issued.tzinfo is None or row[3] != AUDIENCE
                or not timedelta(0) < expires - issued <= timedelta(days=1)):
            raise ValueError("Invalid finite grant")
    except (ValueError, TypeError) as error:
        raise ProgressError("PROGRESS_GRANT_CORRUPT", 500) from error
    return dict(grant_id=grant_id, instance_id=row[0], consumer_id=row[1], workspace_instance_id=row[2],
        audience=row[3], selections=projects, fields=selected_fields,
        issued_at=row[6], expires_at=row[7], status="REVOKED" if row[8] else (
            "EXPIRED" if _now() >= expires else "ACTIVE"))


def revoke(connection: sqlite3.Connection, *, grant_id: str, operation_id: str) -> dict[str, object]:
    _id(grant_id); _id(operation_id)
    row = connection.execute("SELECT revoked_at,revocation_operation_id FROM ep_execution_progress_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None:
        raise ProgressError("PROGRESS_GRANT_UNKNOWN", 404)
    if row[0] is not None:
        if row[1] != operation_id:
            raise ProgressError("PROGRESS_REVOCATION_CONFLICT", 409)
    else:
        connection.execute("UPDATE ep_execution_progress_grants SET revoked_at=?,revocation_operation_id=? WHERE grant_id=?", (_now().isoformat(), operation_id, grant_id))
    return status(connection, grant_id=grant_id)


def require_current_instance(connection: sqlite3.Connection, expected_instance: str) -> str:
    """Noncreating owner/read context; initialization is a separate command."""
    _id(expected_instance)
    validate_schema(connection)
    metadata = dict(connection.execute("SELECT key,value FROM engineering_metadata WHERE key IN ('installation.instance_id','installation.schema_version')"))
    if metadata.get("installation.instance_id") != expected_instance:
        raise ProgressError("PROGRESS_INSTANCE_MISMATCH", 403)
    schema = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0]
    installation = connection.execute("SELECT schema_version FROM ep_installations WHERE instance_id=?", (expected_instance,)).fetchone()
    if metadata.get("installation.schema_version") != str(SCHEMA_VERSION) or schema != SCHEMA_VERSION or installation != (SCHEMA_VERSION,):
        raise ProgressError("PROGRESS_SCHEMA_UNSUPPORTED", 422)
    return expected_instance


def _authenticate(connection: sqlite3.Connection, token: object, headers: dict[str, str]) -> dict[str, object]:
    # Supported owning journal modes only; reads never change journal policy.
    # Fresh publication checks hold no lock over serialization or socketwait.
    if connection.execute("PRAGMA journal_mode").fetchone()[0] not in {"delete", "truncate", "persist"}:
        raise ProgressError("PROGRESS_JOURNAL_UNSUPPORTED", 422)
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
        raise ProgressError("UNAUTHENTICATED", 401)
    validate_schema(connection)
    value = hashlib.sha256(_DOMAIN + token.encode("ascii")).digest()
    row = connection.execute("SELECT grant_id,verifier FROM ep_execution_progress_grants WHERE verifier=?", (value,)).fetchone()
    if row is None or not hmac.compare_digest(bytes(row[1]), value):
        raise ProgressError("UNAUTHENTICATED", 401)
    scope = status(connection, grant_id=row[0])
    if scope["status"] != "ACTIVE":
        raise ProgressError("PROGRESS_INACTIVE", 401)
    expected = {"EP-Instance-ID": scope["instance_id"], "EP-Consumer-ID": scope["consumer_id"],
                "Workspace-Instance-ID": scope["workspace_instance_id"], "EP-Audience": AUDIENCE}
    if any(headers.get(k) != v for k, v in expected.items()):
        raise ProgressError("PROGRESS_SCOPE_MISMATCH", 403)
    require_current_instance(connection, str(scope["instance_id"]))
    require_selections(connection, scope["selections"])
    return scope


