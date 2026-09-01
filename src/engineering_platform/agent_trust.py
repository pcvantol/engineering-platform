"""B6A Agent-to-Server trust, registration, and liveness contract.

This is deliberately a small localhost MVP.  A pairing code is created by the
Server operator, consumed once by an Agent, and exchanged for a random bearer
credential.  The Server retains only a domain-separated verifier, never the
credential itself.  Loopback is a transport constraint, not authentication.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import Any


PROTOCOL_VERSION = "1.0"
PAIRING_CODE_TTL_SECONDS = 600
STALE_AFTER_SECONDS = 90
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")
_TOKEN_DOMAIN = b"engineering-platform.agent-trust.token.v1\0"
_PAIRING_DOMAIN = b"engineering-platform.agent-trust.pairing.v1\0"


class AgentTrustError(ValueError):
    """A safe, diagnosable trust protocol rejection."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify(domain: bytes, value: str) -> bytes:
    return hashlib.sha256(domain + value.encode("ascii")).digest()


def _identifier(value: object, field: str = "agent_id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AgentTrustError(f"{field} is invalid")
    return value


def _payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentTrustError("request payload must be an object")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise AgentTrustError("unsupported protocol version")
    return value


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_agent_registrations (
        agent_id TEXT PRIMARY KEY, state TEXT NOT NULL, credential_id TEXT NOT NULL,
        credential_verifier BLOB NOT NULL, host_metadata TEXT, capabilities TEXT,
        repositories TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        last_seen_at TEXT, revoked_at TEXT)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_agent_pairing_codes (
        agent_id TEXT PRIMARY KEY, verifier BLOB NOT NULL, expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL)""")


def create_pairing_code(connection: sqlite3.Connection, agent_id: str) -> dict[str, str]:
    agent_id = _identifier(agent_id)
    now = datetime.now(timezone.utc)
    code = secrets.token_urlsafe(24)
    expires_at = (now + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)).isoformat()
    connection.execute("DELETE FROM ep_agent_pairing_codes WHERE agent_id=?", (agent_id,))
    connection.execute("INSERT INTO ep_agent_pairing_codes(agent_id,verifier,expires_at,created_at) VALUES(?,?,?,?)", (agent_id, _verify(_PAIRING_DOMAIN, code), expires_at, now.isoformat()))
    return {"agent_id": agent_id, "pairing_code": code, "expires_at": expires_at}


def pair(connection: sqlite3.Connection, body: object) -> dict[str, str]:
    raw = _payload(body)
    if set(raw) != {"protocol_version", "agent_id", "pairing_code"}:
        raise AgentTrustError("pairing payload is malformed")
    agent_id = _identifier(raw["agent_id"])
    code = raw["pairing_code"]
    if not isinstance(code, str) or not code:
        raise AgentTrustError("pairing code is invalid")
    row = connection.execute("SELECT verifier,expires_at FROM ep_agent_pairing_codes WHERE agent_id=?", (agent_id,)).fetchone()
    if row is None or str(row[1]) <= utcnow() or not hmac.compare_digest(bytes(row[0]), _verify(_PAIRING_DOMAIN, code)):
        raise AgentTrustError("pairing is not approved or has expired")
    connection.execute("DELETE FROM ep_agent_pairing_codes WHERE agent_id=?", (agent_id,))
    token, credential_id, now = secrets.token_urlsafe(32), "agent-" + secrets.token_hex(12), utcnow()
    existing = connection.execute("SELECT state FROM ep_agent_registrations WHERE agent_id=?", (agent_id,)).fetchone()
    if existing is not None and existing[0] == "REVOKED":
        raise AgentTrustError("agent is revoked; reset it before pairing")
    connection.execute("""INSERT INTO ep_agent_registrations(agent_id,state,credential_id,credential_verifier,created_at,updated_at)
        VALUES(?, 'PAIRED', ?, ?, ?, ?) ON CONFLICT(agent_id) DO UPDATE SET
        state='PAIRED', credential_id=excluded.credential_id, credential_verifier=excluded.credential_verifier, updated_at=excluded.updated_at, revoked_at=NULL""", (agent_id, credential_id, _verify(_TOKEN_DOMAIN, token), now, now))
    return {"agent_id": agent_id, "credential": token, "credential_id": credential_id}


def authenticate(connection: sqlite3.Connection, agent_id: object, token: str | None) -> None:
    agent_id = _identifier(agent_id)
    if not token:
        raise AgentTrustError("authentication is required")
    row = connection.execute("SELECT state,credential_verifier FROM ep_agent_registrations WHERE agent_id=?", (agent_id,)).fetchone()
    if row is None:
        raise AgentTrustError("unknown agent")
    if row[0] == "REVOKED":
        raise AgentTrustError("agent is revoked")
    if row[0] not in {"PAIRED", "REGISTERED"}:
        raise AgentTrustError("agent is not paired")
    if not hmac.compare_digest(bytes(row[1]), _verify(_TOKEN_DOMAIN, token)):
        raise AgentTrustError("invalid credential")


def register(connection: sqlite3.Connection, body: object, token: str | None) -> dict[str, str]:
    raw = _payload(body)
    if set(raw) != {"protocol_version", "agent_id", "host", "capabilities", "repositories"}:
        raise AgentTrustError("registration payload is malformed")
    agent_id = _identifier(raw["agent_id"])
    authenticate(connection, agent_id, token)
    host, capabilities, repositories = raw["host"], raw["capabilities"], raw["repositories"]
    if not isinstance(host, dict) or set(host) != {"hostname", "os_user", "operating_system", "architecture"} or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in host.values()):
        raise AgentTrustError("host metadata is invalid")
    if not isinstance(capabilities, dict) or not isinstance(repositories, list) or len(repositories) > 128:
        raise AgentTrustError("capability or repository metadata is invalid")
    # B5 declarations, not paths, are reported.  They remain logical evidence only.
    for item in repositories:
        if not isinstance(item, dict) or set(item) != {"attachment"} or not isinstance(item["attachment"], dict):
            raise AgentTrustError("repository metadata is invalid")
    now = utcnow()
    connection.execute("UPDATE ep_agent_registrations SET state='REGISTERED',host_metadata=?,capabilities=?,repositories=?,updated_at=?,last_seen_at=? WHERE agent_id=?", (json.dumps(host, sort_keys=True), json.dumps(capabilities, sort_keys=True), json.dumps(repositories, sort_keys=True), now, now, agent_id))
    return {"agent_id": agent_id, "state": "REGISTERED"}


def heartbeat(connection: sqlite3.Connection, body: object, token: str | None) -> dict[str, str]:
    raw = _payload(body)
    if set(raw) != {"protocol_version", "agent_id"}:
        raise AgentTrustError("heartbeat payload is malformed")
    agent_id = _identifier(raw["agent_id"])
    authenticate(connection, agent_id, token)
    now = utcnow()
    connection.execute("UPDATE ep_agent_registrations SET last_seen_at=?,updated_at=? WHERE agent_id=?", (now, now, agent_id))
    return {"agent_id": agent_id, "state": "ONLINE"}


def register_attachment(connection: sqlite3.Connection, body: object, token: str | None) -> dict[str, str]:
    """Accept a B5 declaration only from an authenticated paired Agent."""
    raw = _payload(body)
    if set(raw) != {"protocol_version", "agent_id", "attachment", "availability"}:
        raise AgentTrustError("attachment registration payload is malformed")
    agent_id = _identifier(raw["agent_id"])
    authenticate(connection, agent_id, token)
    from .project_topology import TopologyRegistrationError, register_attachment as persist_attachment
    try:
        return persist_attachment(connection, agent_id=agent_id, declaration=raw["attachment"], availability=raw["availability"])
    except TopologyRegistrationError as error:
        raise AgentTrustError(str(error)) from error


def revoke(connection: sqlite3.Connection, agent_id: str) -> bool:
    result = connection.execute("UPDATE ep_agent_registrations SET state='REVOKED',revoked_at=?,updated_at=? WHERE agent_id=? AND state != 'REVOKED'", (utcnow(), utcnow(), _identifier(agent_id)))
    return result.rowcount == 1


def reset(connection: sqlite3.Connection, agent_id: str) -> bool:
    agent_id = _identifier(agent_id)
    connection.execute("DELETE FROM ep_agent_pairing_codes WHERE agent_id=?", (agent_id,))
    return connection.execute("DELETE FROM ep_agent_registrations WHERE agent_id=?", (agent_id,)).rowcount == 1


def registration_status(connection: sqlite3.Connection, agent_id: str) -> dict[str, object]:
    row = connection.execute("SELECT agent_id,state,credential_id,host_metadata,capabilities,repositories,last_seen_at,revoked_at FROM ep_agent_registrations WHERE agent_id=?", (_identifier(agent_id),)).fetchone()
    if row is None:
        raise AgentTrustError("unknown agent")
    last_seen = row[6]
    online = bool(last_seen and datetime.fromisoformat(str(last_seen)) >= datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS))
    return {"agent_id": row[0], "state": row[1], "credential_id": row[2], "host": json.loads(row[3]) if row[3] else None, "capabilities": json.loads(row[4]) if row[4] else None, "repositories": json.loads(row[5]) if row[5] else [], "last_seen_at": last_seen, "liveness": "ONLINE" if online else "STALE", "revoked_at": row[7]}
