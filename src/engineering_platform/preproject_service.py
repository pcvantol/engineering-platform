"""Private durable admission for the installation-owned local inspection host.

CENTRAL is not a filesystem scanner. Only trusted local owner setup binds a
direct-child host source; HTTP references that opaque binding and cannot widen
it. The private ledger is separately initialized and never recreated on read.
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
import selectors
import shutil
import signal
import subprocess
import sys
import time

from .platform_admin import require_installation_owner
from .platform_api import PlatformConfiguration
from .preproject_inspection import InspectionError, component, directory, identity, placement, validate_selection
from .platform_version import CURRENT_PLATFORM_VERSION

CONTRACT = "1.0"
_DOMAIN = b"EP.LOCAL.PREPROJECT.INSPECT_READ.V1\0"
_SCHEMA = """
CREATE TABLE identity (store_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, host_id TEXT NOT NULL);
CREATE TABLE sources (source_ref TEXT PRIMARY KEY, binding TEXT NOT NULL);
CREATE TABLE grants (grant_id TEXT PRIMARY KEY, verifier BLOB NOT NULL UNIQUE, scope TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE operations (operation_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, intent TEXT NOT NULL,
intent_sha256 TEXT NOT NULL, result TEXT, result_sha256 TEXT, created_at TEXT NOT NULL);
PRAGMA user_version=1;
"""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def openapi_paths() -> dict[str, object]:
    """Only the selected INSPECT_READ subset, not optional assessment proposals."""
    common = {"summary": "Bounded local pre-project INSPECT_READ; no registration or target effects",
        "security": [{"inspectionBearer": []}], "responses": {
            str(s): {"description": d} for s, d in ((200, "Original closed inspection receipt"),
                (400, "Invalid request"), (401, "Revoked/expired/invalid separate INSPECT_READ"),
                (403, "Scope denied"), (404, "Operation unknown"), (409, "Drift/limit/idempotency/uncertainty"),
                (422, "Unsupported profile or source format"), (500, "Corrupt initialized private store"),
                (503, "Source/owner/store unavailable"))},
        "x-response-json-schema": "urn:engineering-platform:preproject-local-inspection:1.0"}
    headers = [{"name": k, "in": "header", "required": True, "schema": {"type": "string"}} for k in (
        "EP-Inspection-Contract", "EP-Instance-ID", "EP-Host-ID", "EP-Consumer-ID", "EP-Principal", "EP-Audience")]
    return {"/v1/onboarding/inspections": {"post": {**common, "parameters": headers,
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False,
            "required": ["contract_version", "operation_id", "source_ref"], "properties": {
                "contract_version": {"type": "string", "enum": [CONTRACT]},
                "operation_id": {"type": "string", "maxLength": 128},
                "source_ref": {"type": "string", "maxLength": 128}}}}}}}},
        "/v1/onboarding/inspections/{operation_id}": {"get": {**common, "parameters": headers + [
            {"name": "operation_id", "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}}]}}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def host_identity() -> str:
    return hashlib.sha256((os.uname().nodename + ":" + str(os.geteuid())).encode()).hexdigest()


def _instance(root: Path) -> str:
    from .server import SERVER_IDENTITY_FILENAME
    from .effect_workspace import read_json
    try:
        return component(read_json(root / SERVER_IDENTITY_FILENAME)["instance_id"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise InspectionError("INSPECTION_INSTANCE_UNAVAILABLE", 503) from error


def _private(root: Path) -> Path:
    require_installation_owner(root)
    path = root / "preproject-inspection-v1"
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise InspectionError("INSPECTION_PRIVATE_STORE_UNSAFE", 503)
    return path


def initialize(root: Path, *, expected_instance: str) -> dict[str, object]:
    """Explicit owner initialization only; a historical marker fences data loss."""
    require_installation_owner(root)
    if _instance(root) != expected_instance:
        raise InspectionError("INSPECTION_INSTANCE_MISMATCH", 403)
    from .effect_workspace import immutable_json
    marker = {"store_id": secrets.token_hex(16), "instance_id": expected_instance, "host_id": host_identity()}
    # Separate durable anchor survives loss of the entire subordinate store.
    # A partial initialization also stays uncertain rather than silently reset.
    anchor = root / "preproject-inspection-v1-initialized.json"
    if anchor.exists() or anchor.is_symlink():
        raise InspectionError("INSPECTION_ALREADY_INITIALIZED_OR_STORAGE_LOST", 409)
    immutable_json(anchor, marker)
    path = root / "preproject-inspection-v1"
    path.mkdir(mode=0o700)
    immutable_json(path / "initialized.json", marker)
    database = path / "inspection.sqlite3"
    fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(database)
    try:
        db.executescript(_SCHEMA)
        db.execute("INSERT INTO identity VALUES(?,?,?)", tuple(marker.values()))
        db.commit()
    finally:
        db.close()
    (path / "worker-anchor").mkdir(mode=0o700)
    return {"state": "INITIALIZED", "schema_version": 1, **marker}


@contextmanager
def store(root: Path, *, write: bool = False):
    """All normal commands require an intact pre-existing private store."""
    from .effect_workspace import read_json
    try:
        path = _private(root)
        marker = read_json(path / "initialized.json")
        if marker != read_json(root / "preproject-inspection-v1-initialized.json"):
            raise InspectionError("INSPECTION_STORE_IDENTITY_CONFLICT", 500)
        database = path / "inspection.sqlite3"
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise InspectionError("INSPECTION_PRIVATE_STORE_UNSAFE", 503)
        db = sqlite3.connect(database.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=.5)
        try:
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            row = db.execute("SELECT store_id,instance_id,host_id FROM identity").fetchall()
            expected = (marker["store_id"], marker["instance_id"], marker["host_id"])
            if (row != [expected] or marker["host_id"] != host_identity()
                    or marker["instance_id"] != _instance(root) or db.execute("PRAGMA user_version").fetchone() != (1,)
                    or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or identity(database.lstat()) != identity(info)):
                raise InspectionError("INSPECTION_STORE_CORRUPT", 500)
            yield db, marker
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    except sqlite3.Error as error:
        unavailable = getattr(error, "sqlite_errorcode", 0) & 255 in {5, 6, 10, 14}
        raise InspectionError("INSPECTION_STORE_UNAVAILABLE" if unavailable else "INSPECTION_STORE_CORRUPT",
            503 if unavailable else 500) from error
    except InspectionError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise InspectionError("INSPECTION_STORE_CORRUPT", 500) from error
    except OSError as error:
        raise InspectionError("INSPECTION_STORE_UNAVAILABLE", 503) from error


def bind_source(root: Path, *, source_ref: str, host_root: Path, workspace_root: Path, child: str) -> dict[str, object]:
    component(source_ref); component(child)
    config = PlatformConfiguration.load(host_root)
    policy = config.resolve_repository_authorization_policy()
    permitted = [r for r in policy.allowed_roots if r.repository_scope == "direct_children"]
    if policy.symlink_policy != "reject" or not any(Path(r.path) == workspace_root for r in permitted):
        raise InspectionError("INSPECTION_PLACEMENT_DENIED", 403)
    target = workspace_root / child
    if (target == host_root.resolve() or root.resolve().is_relative_to(target)
            or target.is_relative_to(root.resolve()) or target == Path(__file__).resolve().parents[2]):
        raise InspectionError("INSPECTION_HOST_TARGET_FORBIDDEN", 403)
    if any(target == Path(p) for p in policy.denied_repositories):
        raise InspectionError("INSPECTION_PLACEMENT_DENIED", 403)
    with directory(workspace_root) as fd:
        try:
            target_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            target_info = None
        if target_info is not None and not stat.S_ISDIR(target_info.st_mode):
            raise InspectionError("INSPECTION_TARGET_UNSUPPORTED", 422)
        binding = {"workspace_root": str(workspace_root), "child": child,
            "root_identity": list(identity(os.fstat(fd))),
            "target_identity": None if target_info is None else list(identity(target_info)),
            "host_root": str(host_root.resolve())}
    check_root, check_target = placement(binding)
    os.close(check_root)
    if check_target is not None:
        os.close(check_target)
    with store(root, write=True) as (db, marker):
        if db.execute("SELECT count(*) FROM sources").fetchone()[0] >= 64:
            raise InspectionError("INSPECTION_STORAGE_LIMIT", 409)
        db.execute("INSERT INTO sources VALUES(?,?)", (source_ref, canonical(binding)))
    return {"source_ref": source_ref, "host_id": marker["host_id"], "kind": "LOCAL_GENESIS_SOURCE_NOT_PROJECT"}


def issue(root: Path, *, grant_id: str, source_ref: str, consumer_id: str, principal: str,
          audience: str, paths: list[str], content_paths: list[str], ttl_seconds: int = 900,
          max_operations: int = 4) -> dict[str, object]:
    for value in (grant_id, source_ref, consumer_id, principal, audience):
        component(value)
    paths, content_paths = validate_selection(paths, content_paths)
    if (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600
            or type(max_operations) is not int or not 1 <= max_operations <= 10):
        raise InspectionError("INSPECTION_GRANT_LIMIT_INVALID")
    with store(root, write=True) as (db, marker):
        if db.execute("SELECT 1 FROM sources WHERE source_ref=?", (source_ref,)).fetchone() is None:
            raise InspectionError("INSPECTION_SOURCE_UNKNOWN", 404)
        if db.execute("SELECT count(*) FROM grants").fetchone()[0] >= 128:
            raise InspectionError("INSPECTION_STORAGE_LIMIT", 409)
        now = _now()
        scope = {"grant_id": grant_id, "source_ref": source_ref, "consumer_id": consumer_id,
            "principal": principal, "audience": audience, "paths": paths, "content_paths": content_paths,
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "max_operations": max_operations, "instance_id": marker["instance_id"], "host_id": marker["host_id"]}
        scope["profile"] = "LOCAL_GENESIS_INSPECT_READ_V1"
        scope["metadata_fields"] = ["placement_state", "git_identity", "git_worktree_state", "remote_presence", "selected_declaration"]
        scope["limits"] = {"entries": 2048, "documents": 16, "document_bytes": 65536,
            "all_document_bytes": 262144, "hashed_worktree_bytes": 4194304, "worker_seconds": 15}
        credential = secrets.token_urlsafe(32)
        digest = hashlib.sha256(_DOMAIN + credential.encode()).digest()
        db.execute("INSERT INTO grants(grant_id,verifier,scope) VALUES(?,?,?)", (grant_id, digest, canonical(scope)))
    return {**scope, "credential": credential}


def grant_status(root: Path, grant_id: str, *, revoke: bool = False) -> dict[str, object]:
    component(grant_id)
    with store(root, write=revoke) as (db, _):
        row = db.execute("SELECT scope,revoked FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise InspectionError("INSPECTION_GRANT_UNKNOWN", 404)
        scope = json.loads(row[0])
        if revoke:
            db.execute("UPDATE grants SET revoked=1 WHERE grant_id=?", (grant_id,))
        return {**scope, "state": "REVOKED" if revoke or row[1] else (
            "EXPIRED" if _now() >= datetime.fromisoformat(scope["expires_at"]) else "ACTIVE")}


def authenticate(db: sqlite3.Connection, token: object, headers: dict[str, str]) -> dict[str, object]:
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise InspectionError("INSPECTION_UNAUTHENTICATED", 401)
    digest = hashlib.sha256(_DOMAIN + token.encode()).digest()
    row = db.execute("SELECT verifier,scope,revoked FROM grants WHERE verifier=?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row[0], digest) or row[2]:
        raise InspectionError("INSPECTION_UNAUTHENTICATED", 401)
    scope = json.loads(row[1])
    if _now() >= datetime.fromisoformat(scope["expires_at"]):
        raise InspectionError("INSPECTION_GRANT_EXPIRED", 401)
    expected = {"EP-Inspection-Contract": CONTRACT, "EP-Instance-ID": scope["instance_id"],
        "EP-Host-ID": scope["host_id"], "EP-Consumer-ID": scope["consumer_id"],
        "EP-Principal": scope["principal"], "EP-Audience": scope["audience"]}
    if headers.get("EP-Inspection-Contract") != CONTRACT:
        raise InspectionError("INSPECTION_CONTRACT_UNSUPPORTED", 422)
    if any(headers.get(k) != v for k, v in expected.items()):
        raise InspectionError("INSPECTION_SCOPE_DENIED", 403)
    return scope


def _authorization(root: Path, token: object, headers: dict[str, str], source_ref: str):
    with store(root) as (db, _):
        scope = authenticate(db, token, headers)
        if scope["source_ref"] != source_ref:
            raise InspectionError("INSPECTION_SOURCE_DENIED", 403)
        row = db.execute("SELECT binding FROM sources WHERE source_ref=?", (source_ref,)).fetchone()
        if row is None:
            raise InspectionError("INSPECTION_SOURCE_UNAVAILABLE", 503)
        binding = json.loads(row[0])
    config = PlatformConfiguration.load(Path(binding["host_root"]))
    policy = config.resolve_repository_authorization_policy()
    target = Path(binding["workspace_root"]) / binding["child"]
    if (policy.symlink_policy != "reject" or not any(r.repository_scope == "direct_children"
            and r.path == binding["workspace_root"] for r in policy.allowed_roots)
            or any(target == Path(p) for p in policy.denied_repositories)):
        raise InspectionError("INSPECTION_PLACEMENT_REVOKED", 403)
    return scope, binding


def _worker(root: Path, binding: dict[str, object], scope: dict[str, object], recheck) -> dict[str, object]:
    """Existing native FME isolation, no model invocation; own whole-group reap."""
    from . import effect_workspace
    from .effect_contract import EffectContractError
    from .providers import codex_cli_executable
    executable = codex_cli_executable()
    if executable is None:
        raise InspectionError("INSPECTION_SANDBOX_UNAVAILABLE", 422)
    version = subprocess.run((executable, "--version"), env=effect_workspace.child_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    if version.returncode or version.stdout.strip() != b"codex-cli 0.160.1":
        raise InspectionError("INSPECTION_SANDBOX_UNQUALIFIED", 422)
    target = Path(binding["workspace_root"]) / binding["child"]
    try:
        anchor = root / "preproject-inspection-v1/worker-anchor"
        name = "ep-inspection-" + secrets.token_hex(12)
        options = tuple(value.replace("ep-effects", name) for value in effect_workspace.sandbox_options(
            anchor, readable=(target, Path(__file__).resolve().parent.parent), runtime=Path(executable)))
        # Preserve the actual installed venv launcher/import authority. The
        # generic control helper resolves it to base Python for stdlib probes.
        command = (executable, "sandbox", "-P", name, *options, "-C", str(anchor), "--",
            sys.executable, "-m", "engineering_platform.preproject_worker")
    except (EffectContractError, OSError) as error:
        raise InspectionError("INSPECTION_SANDBOX_UNAVAILABLE", 422) from error
    environment = effect_workspace.child_environment()
    # The selected package is normal installed runtime authority; a source
    # checkout is never injected via PYTHONPATH or used as the child cwd.
    environment["PATH"] = str(Path(executable).parent) + os.pathsep + os.defpath
    job = canonical({"binding": binding, "paths": scope["paths"], "content_paths": scope["content_paths"]}).encode()
    retained = bytearray()
    deadline = time.monotonic() + 15
    process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    primary = None
    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        pending = memoryview(job)
        with selectors.DefaultSelector() as events:
            events.register(process.stdout, selectors.EVENT_READ, "stdout")
            events.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            while events.get_map() or process.poll() is None:
                recheck()
                if time.monotonic() >= deadline:
                    raise InspectionError("INSPECTION_TIME_LIMIT", 409)
                for key, _ in events.select(.1):
                    if key.data == "stdin":
                        try:
                            pending = pending[os.write(key.fd, pending[:4096]):]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            events.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        events.unregister(key.fileobj)
                    retained.extend(chunk)
                    if len(retained) > 1048576:
                        raise InspectionError("INSPECTION_OUTPUT_LIMIT", 409)
        try:
            result = json.loads(retained)
        except (ValueError, UnicodeError) as error:
            raise InspectionError("INSPECTION_WORKER_UNAVAILABLE", 503) from error
        if process.returncode != 0:
            if set(result) == {"error", "status"} and type(result["status"]) is int:
                raise InspectionError(result["error"], result["status"])
            raise InspectionError("INSPECTION_WORKER_UNAVAILABLE", 503)
        if set(result) != {"result", "runtime"}:
            raise InspectionError("INSPECTION_WORKER_INVALID", 500)
        runtime = result["runtime"]
        worker_file = Path(__file__).with_name("preproject_worker.py").resolve()
        if (runtime != {"executable": sys.executable, "prefix": sys.prefix, "module": str(worker_file),
                "module_sha256": hashlib.sha256(worker_file.read_bytes()).hexdigest(), "version": CURRENT_PLATFORM_VERSION}):
            raise InspectionError("INSPECTION_WORKER_RUNTIME_MISMATCH", 500)
        result["result"]["worker_runtime_binding"] = {"module_sha256": runtime["module_sha256"],
            "installed_prefix_sha256": hashlib.sha256(runtime["prefix"].encode()).hexdigest(),
            "state": "CURRENT_INSTALLED_PACKAGE_VERIFIED"}
        return result["result"]
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup = []
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup.append(error)
        # Independent bounded wait, even if signaling failed. Never enter
        # Popen.__exit__'s unbounded wait on an uncertain live worker.
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup.append(error)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError as error:
                cleanup.append(error)
        if cleanup:
            original = ([primary] if primary is not None else []) + cleanup
            raise InspectionError("INSPECTION_WORKER_STOP_UNCERTAIN", 503) from BaseExceptionGroup(
                "Original inspection and owned worker stop failures", original)


def _receipt(db: sqlite3.Connection, operation_id: str, grant_id: str, intent: str | None):
    row = db.execute("SELECT grant_id,intent,result,result_sha256 FROM operations WHERE operation_id=?",
        (operation_id,)).fetchone()
    if row is None:
        return None
    if row[0] != grant_id or (intent is not None and row[1] != intent):
        raise InspectionError("INSPECTION_OPERATION_CONFLICT", 409)
    if row[2] is None:
        raise InspectionError("INSPECTION_IN_PROGRESS_OR_INTERRUPTED_READBACK_ONLY", 409)
    if hashlib.sha256(row[2].encode()).hexdigest() != row[3]:
        raise InspectionError("INSPECTION_RESULT_CORRUPT", 500)
    return json.loads(row[2])


def operation(root: Path, *, token: object, headers: dict[str, str], operation_id: str,
              request: dict[str, object] | None = None, publish=None):
    """POST admits once; GET/duplicate returns original bytes and never rescans."""
    component(operation_id)
    if request is not None and (set(request) != {"contract_version", "operation_id", "source_ref"}
            or request["contract_version"] != CONTRACT or request["operation_id"] != operation_id):
        raise InspectionError("INSPECTION_REQUEST_INVALID")
    with store(root) as (db, _):
        scope = authenticate(db, token, headers)
    source_ref = component(scope["source_ref"])
    if request is not None and request["source_ref"] != source_ref:
        raise InspectionError("INSPECTION_SOURCE_DENIED", 403)
    intent = None if request is None else canonical(request)
    with store(root, write=request is not None) as (db, _):
        scope = authenticate(db, token, headers)
        result = _receipt(db, operation_id, scope["grant_id"], intent)
        if result is None:
            if request is None:
                raise InspectionError("INSPECTION_OPERATION_UNKNOWN", 404)
            if (db.execute("SELECT count(*) FROM operations WHERE grant_id=?", (scope["grant_id"],)).fetchone()[0]
                    >= scope["max_operations"] or db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 128
                    or db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] > 33554432):
                raise InspectionError("INSPECTION_OPERATION_LIMIT", 409)
            db.execute("INSERT INTO operations(operation_id,grant_id,intent,intent_sha256,created_at) VALUES(?,?,?,?,?)",
                (operation_id, scope["grant_id"], intent, hashlib.sha256(intent.encode()).hexdigest(), _now().isoformat()))
    fresh = result is None
    if fresh:
        def recheck():
            current_scope, current_binding = _authorization(root, token, headers, source_ref)
            for fd in placement(current_binding):
                if fd is not None:
                    os.close(fd)
            return current_scope, current_binding
        try:
            current_scope, binding = recheck()
            observation = _worker(root, binding, current_scope, recheck)
            recheck()
            result = {"contract_version": CONTRACT, "operation_id": operation_id,
                "source_ref": source_ref, "instance_id": scope["instance_id"], "host_id": scope["host_id"],
                "outcome": "COMPLETE", "error": None, "http_status": 200,
                "freshness": "AS_OF_OBSERVATION_NOT_CONTINUOUS", "observation": observation,
                "administrative_records": 2, "target_write_operations": 0, "b8r_write_operations": 0}
        except InspectionError as error:
            result = {"contract_version": CONTRACT, "operation_id": operation_id,
                "source_ref": source_ref, "instance_id": scope["instance_id"], "host_id": scope["host_id"],
                "outcome": "REFUSED", "error": error.code, "http_status": error.status,
                "freshness": "NO_COMPLETE_OBSERVATION", "observation": None,
                "administrative_records": 2, "target_write_operations": 0, "b8r_write_operations": 0}
        result.update(producer_version=CURRENT_PLATFORM_VERSION, profile=scope["profile"], limits=scope["limits"])
        encoded = canonical(result)
        with store(root, write=True) as (db, _):
            db.execute("UPDATE operations SET result=?,result_sha256=? WHERE operation_id=? AND result IS NULL",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(), operation_id))
    def publication_check():
        # No scan or target touch on replay. Fresh results additionally rewalk
        # exact placement; no lock is held across scanning or socket waits.
        current_scope, binding = _authorization(root, token, headers, source_ref)
        if fresh and result["outcome"] == "COMPLETE":
            for fd in placement(binding):
                if fd is not None:
                    os.close(fd)
        # Last authority linearization point AFTER all policy/placement reads,
        # immediately before the owning transport's headers; release promptly.
        with store(root) as (db, _):
            final = authenticate(db, token, headers)
            if final != current_scope:
                raise InspectionError("INSPECTION_AUTHORITY_CHANGED", 403)
    publication_check()
    if publish is not None:
        publish(result, scope["instance_id"], publication_check)
    return result
