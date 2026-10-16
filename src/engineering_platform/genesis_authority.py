"""Owner-only finite Genesis provisioning grants and durable CENTRAL reservation.

Bootstrap ownership is not an engineering run or Mission. Its original intent,
proposed identities and unique target reservation survive every process exit.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from . import storage
from .genesis_contract import BootstrapError, canonical, digest, identifier, request, VALIDATOR_SHA256, EFFECTS
from .platform_admin import require_installation_owner
from .preproject_inspection import directory, identity, component, selected_bytes

HEADERS = ("EP-Bootstrap-Contract", "EP-Instance-ID", "EP-Host-ID", "EP-Consumer-ID", "EP-Principal", "EP-Audience")
DOMAIN = b"EP.CREATE_GENESIS_LOCAL.V1\0"
STATEMENTS = (
    """CREATE TABLE ep_genesis_grants (
        grant_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
        verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32), scope TEXT NOT NULL,
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
        issuance_id TEXT NOT NULL UNIQUE, revocation_id TEXT UNIQUE)""",
    """CREATE TABLE ep_genesis_operations (
        operation_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL UNIQUE REFERENCES ep_genesis_grants(grant_id),
        request_digest TEXT NOT NULL, original_request TEXT NOT NULL,
        target_identity TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL UNIQUE,
        repository_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
        evidence TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def host_id() -> str:
    from .preproject_service import host_identity
    return host_identity()


def install_schema(connection) -> None:
    for sql in STATEMENTS:
        connection.execute(sql)


def validate_schema(connection) -> None:
    for sql in STATEMENTS:
        name = sql.split()[2]
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row is None or "".join(row[0].lower().split()) != "".join(sql.lower().split()):
            raise BootstrapError("BOOTSTRAP_LEDGER_SCHEMA_INVALID", 500)


def database_identity(root: Path) -> tuple[int, int]:
    info = (root / "epdata.sqlite").lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise BootstrapError("BOOTSTRAP_SOURCE_UNSAFE", 503)
    return identity(info)


@contextmanager
def store(root: Path, *, write: bool = False, expected_source=None):
    from .execution_progress_authority import require_current_instance
    before = database_identity(root)
    if expected_source is not None and before != tuple(expected_source):
        raise BootstrapError("BOOTSTRAP_SOURCE_CHANGED", 503)
    path = root / "epdata.sqlite"
    uri = path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
    with storage.sqlite_connection(uri, uri=True, isolation_level=None, timeout=.2) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=200")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            row = connection.execute("SELECT instance_id,schema_version FROM ep_installations").fetchall()
            if len(row) != 1 or row[0][1] != 77:
                raise BootstrapError("BOOTSTRAP_INSTALLATION_INCOMPATIBLE", 422)
            require_current_instance(connection, row[0][0])
            validate_schema(connection)
            if before != database_identity(root):
                raise BootstrapError("BOOTSTRAP_SOURCE_CHANGED", 503)
            yield connection
            if before != database_identity(root):
                raise BootstrapError("BOOTSTRAP_SOURCE_CHANGED", 503)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def _private_placement(root: Path, workspace: object, target_name: object) -> dict:
    if not isinstance(workspace, str):
        raise BootstrapError("BOOTSTRAP_OWNER_PLACEMENT_REQUIRED", 403)
    target_name = component(target_name)
    path = Path(workspace)
    # The descriptor reader rejects symlinks in every ancestor.
    with directory(path) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BootstrapError("BOOTSTRAP_PRIVATE_ROOT_REQUIRED", 403)
        root_id = list(identity(info))
        try:
            os.stat(target_name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BootstrapError("BOOTSTRAP_TARGET_EXISTS", 409)
    protected = [root.absolute(), Path(__file__).resolve().parents[1], Path.home().absolute()]
    if path == Path(path.anchor) or any(path == p or path.is_relative_to(p) or p.is_relative_to(path) for p in protected[:2]) or path == protected[2]:
        raise BootstrapError("BOOTSTRAP_PLACEMENT_UNSAFE", 403)
    for ancestor in (path, *path.parents):
        if os.path.lexists(ancestor / ".git"):
            raise BootstrapError("BOOTSTRAP_NESTED_REPOSITORY", 403)
    return {"workspace_root": str(path), "root_identity": root_id, "target_name": target_name}


def issue(root: Path, *, expected_instance: str, config: dict) -> dict:
    """Private owner setup, never a remote route or inherited peer permission."""
    import time
    from .genesis_contract import closed
    require_installation_owner(root)
    closed(config, {"grant_id", "issuance_id", "consumer_id", "principal", "target_name", "workspace_root",
        "validator_path", "request", "ttl_seconds"})
    parsed, original = request(config["request"])
    for key in ("grant_id", "issuance_id", "consumer_id", "principal"):
        identifier(config[key])
    ttl = config["ttl_seconds"]
    if type(ttl) is not int or not 1 <= ttl <= 86400:
        raise BootstrapError("BOOTSTRAP_LIFETIME_INVALID")
    placement = _private_placement(root, config["workspace_root"], config["target_name"])
    validator = Path(config["validator_path"])
    with directory(validator.parent) as fd:
        raw, observed_hash = selected_bytes(fd, validator.name, maximum=131072, deadline=time.monotonic()+2)
    if observed_hash != VALIDATOR_SHA256:
        raise BootstrapError("BOOTSTRAP_EXTERNAL_VALIDATOR_PIN_INVALID", 409)
    # Explicit installation-owner preparation, before any runtime admission.
    # This trusted empty capability is never inferred from a later target or
    # automatically recovered after an interrupted/unknown preparation.
    from .genesis_service import _private
    private_root, marker = _private(root)
    if marker["instance_id"] != expected_instance:
        raise BootstrapError("BOOTSTRAP_INSTANCE_MISMATCH", 403)
    staging = private_root / "anchor" / ("stage-" + secrets.token_hex(16))
    with directory(staging.parent) as parent:
        parent_info = os.fstat(parent)
        if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o077:
            raise BootstrapError("BOOTSTRAP_PREPARATION_UNSAFE", 403)
        os.mkdir(staging.name, 0o700, dir_fd=parent)
        with directory(staging) as prepared:
            prepared_identity = list(identity(os.fstat(prepared)))
            if os.listdir(prepared):
                raise BootstrapError("BOOTSTRAP_PREPARATION_UNSAFE", 403)
        os.fsync(parent)
    scope = {**placement, "validator_path": str(validator), "validator_sha256": observed_hash,
        "staging_path": str(staging), "staging_identity": prepared_identity,
        "staging_parent_identity": list(identity(parent_info)),
        "instance_id": identifier(expected_instance), "host_id": host_id(),
        "consumer_id": config["consumer_id"], "principal": config["principal"], "audience": "genesis-bootstrap",
        "purpose": "CREATE_GENESIS_LOCAL", "operation_id": parsed["operation_id"], "target_ref": parsed["target_ref"],
        "request_digest": digest(canonical(parsed)), "bundle_digest": original.bundle_digest,
        "manifest_digest": original.manifest_digest, "input_digest": original.input_digest,
        "effect_plan_binding": parsed["effect_plan_binding"], "project_id": original.project_id,
        "authority_repository_id": original.authority_repository_id, "repository_id": original.repository_id,
        "effects": list(EFFECTS), "maximum_files": 37, "maximum_payload_bytes": 262144, "worker_seconds": 30}
    token = secrets.token_urlsafe(32)
    at = now()
    scope.update(issued_at=at.isoformat(), expires_at=(at+timedelta(seconds=ttl)).isoformat())
    scope["scope_mac"] = hmac.new(token.encode("ascii"), DOMAIN+b"scope\0"+canonical(scope), hashlib.sha256).hexdigest()
    with store(root, write=True) as connection:
        if connection.execute("SELECT instance_id FROM ep_installations").fetchone()[0] != expected_instance:
            raise BootstrapError("BOOTSTRAP_INSTANCE_MISMATCH", 403)
        if connection.execute("SELECT COUNT(*) FROM ep_genesis_grants").fetchone()[0] >= 128:
            raise BootstrapError("BOOTSTRAP_GRANT_CAPACITY", 409)
        try:
            connection.execute("INSERT INTO ep_genesis_grants(grant_id,instance_id,verifier,scope,issued_at,expires_at,issuance_id) VALUES(?,?,?,?,?,?,?)",
                (config["grant_id"], expected_instance, hashlib.sha256(DOMAIN+token.encode()).digest(), canonical(scope).decode(),
                 at.isoformat(), (at+timedelta(seconds=ttl)).isoformat(), config["issuance_id"]))
        except sqlite3.IntegrityError as error:
            raise BootstrapError("BOOTSTRAP_GRANT_ALREADY_ISSUED", 409) from error
    return {"grant_id": config["grant_id"], "purpose": scope["purpose"], "expires_at": (at+timedelta(seconds=ttl)).isoformat(), "credential": token}


def status(connection, grant_id: str) -> dict:
    identifier(grant_id)
    row = connection.execute("SELECT instance_id,scope,issued_at,expires_at,revoked_at FROM ep_genesis_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None:
        raise BootstrapError("BOOTSTRAP_GRANT_UNKNOWN", 404)
    scope = json.loads(row[1])
    return {"grant_id": grant_id, "instance_id": row[0], "purpose": "CREATE_GENESIS_LOCAL", "operation_id": scope["operation_id"],
        "target_ref": scope["target_ref"], "request_digest": scope["request_digest"], "issued_at": row[2], "expires_at": row[3],
        "state": "REVOKED" if row[4] else "EXPIRED" if now() >= datetime.fromisoformat(row[3]) else "ACTIVE"}


def revoke(connection, *, grant_id: str, operation_id: str) -> dict:
    identifier(operation_id)
    current = status(connection, grant_id)
    row = connection.execute("SELECT revocation_id FROM ep_genesis_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row[0] not in (None, operation_id):
        raise BootstrapError("BOOTSTRAP_REVOCATION_CONFLICT", 409)
    connection.execute("UPDATE ep_genesis_grants SET revoked_at=COALESCE(revoked_at,?),revocation_id=? WHERE grant_id=?",
        (now().isoformat(), operation_id, grant_id))
    return {**current, "state": "REVOKED"}


def authenticate(connection, token: object, headers: dict) -> tuple[str, dict]:
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise BootstrapError("BOOTSTRAP_UNAUTHENTICATED", 401)
    verifier = hashlib.sha256(DOMAIN+token.encode()).digest()
    row = connection.execute("SELECT grant_id,verifier,scope,issued_at,expires_at,revoked_at,revocation_id FROM ep_genesis_grants WHERE verifier=?", (verifier,)).fetchone()
    if row is None or not hmac.compare_digest(verifier, row[1]):
        raise BootstrapError("BOOTSTRAP_UNAUTHENTICATED", 401)
    from .genesis_contract import load
    try:
        scope = load(row[2].encode())
        if not isinstance(scope, dict):
            raise ValueError("invalid scope object")
        mac = scope.pop("scope_mac", None)
        expected_mac = hmac.new(token.encode("ascii"), DOMAIN+b"scope\0"+canonical(scope), hashlib.sha256).hexdigest()
        if (not isinstance(mac, str) or not hmac.compare_digest(mac, expected_mac)
                or scope.get("issued_at") != row[3] or scope.get("expires_at") != row[4]
                or (row[5] is None) != (row[6] is None)):
            raise ValueError("issued scope or lifetime changed")
        start, end = datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4])
    except (ValueError, TypeError, BootstrapError) as error:
        raise BootstrapError("BOOTSTRAP_GRANT_CORRUPT", 500) from error
    at = now()
    if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end-start <= timedelta(days=1):
        raise BootstrapError("BOOTSTRAP_GRANT_CORRUPT", 500)
    if row[5] is not None or at < start or at >= end:
        raise BootstrapError("BOOTSTRAP_GRANT_INACTIVE", 401)
    expected = dict(zip(HEADERS, ("1.0", scope["instance_id"], scope["host_id"], scope["consumer_id"], scope["principal"], scope["audience"])))
    actual_instance = connection.execute("SELECT instance_id FROM ep_installations").fetchone()[0]
    if headers != expected or scope["instance_id"] != actual_instance or scope["host_id"] != host_id() or scope["purpose"] != "CREATE_GENESIS_LOCAL":
        raise BootstrapError("BOOTSTRAP_SCOPE_DENIED", 403)
    return row[0], scope
