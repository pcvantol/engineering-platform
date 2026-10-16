"""Bounded authenticated composition of existing native, Git and B8R services.

One original intent/reservation, one finite owner grant, one step at a time.
HTTP GET never resumes physical work. No Mission, run, scheduler or renderer.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import fcntl
import hmac
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time

from . import genesis_authority as authority, genesis_native, project_topology, local_repository_binding
from .genesis_contract import BootstrapError, canonical, digest, identifier, request, load, MAX_BODY
from .platform_admin import require_installation_owner
from .preproject_inspection import directory, identity, selected_bytes
from .provider_process_identity import ProcessIdentity, capture_process_identity, verify_process_identity
from .platform_version import CURRENT_PLATFORM_VERSION

STEPS = ("PREFLIGHT", "CREATE_TARGET", "WRITE_FILES", "VALIDATE_DOCS", "GIT_INIT", "GIT_PREPARE",
    "GIT_OBJECT", "GIT_REF", "VERIFY_COMMIT")


def openapi_paths() -> dict:
    from importlib.resources import files
    schema = json.loads(files("engineering_platform").joinpath("schemas/genesis-local-bootstrap-request-v1.schema.json").read_text())
    parameters = [{"name": key, "in": "header", "required": True, "schema": {"type": "string"}} for key in authority.HEADERS]
    common = {"security": [{"bootstrapBearer": []}], "parameters": parameters,
        "responses": {str(code): {"description": text} for code, text in (
            (200, "Original scoped operation receipt; PARTIAL is not COMPLETE"), (400, "Invalid closed request"),
            (401, "Invalid or inactive separate provisioning grant"), (403, "Exact scope denied"),
            (404, "Original operation absent"), (408, "Bounded body deadline"), (409, "Conflict, partial or uncertain effect"),
            (413, "Finite byte capacity"), (422, "Unsupported profile, schema or native runtime"),
            (500, "Corrupt initialized source/evidence"), (503, "Source or qualified host unavailable"))},
        "x-response-json-schema": "urn:engineering-platform:genesis-local-bootstrap:1.0"}
    item = {**common, "parameters": parameters+[{"name": "operation_id", "in": "path", "required": True,
        "schema": {"type": "string", "maxLength": 128}}]}
    return {"/v1/bootstrap/genesis/operations": {"post": {**common,
        "summary": "Admit exact CREATE_GENESIS_LOCAL intent; no Mission or remote effects",
        "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}},
        "/v1/bootstrap/genesis/operations/{operation_id}": {"get": {**item, "summary": "Read original operation only; never resume"}},
        "/v1/bootstrap/genesis/operations/{operation_id}/resume": {"post": {**item, "summary": "Resume only original scope under current original owner grant"}}}


def initialize(root: Path, *, expected_instance: str) -> dict:
    require_installation_owner(root)
    with authority.store(root) as db:
        actual = db.execute("SELECT instance_id FROM ep_installations").fetchone()[0]
    if actual != expected_instance:
        raise BootstrapError("BOOTSTRAP_INSTANCE_MISMATCH", 403)
    if (root / "genesis-local-v1").exists() or (root / "genesis-local-v1.identity.json").exists():
        raise BootstrapError("BOOTSTRAP_PROTOTYPE_PRIVATE_LAYOUT_INCOMPATIBLE", 422)
    path = root / "g"
    marker = root / "g.identity.json"
    if path.exists() or marker.exists():
        return {"state": "ALREADY_INITIALIZED", "instance_id": _private(root)[1]["instance_id"]}
    path.mkdir(mode=0o700)
    for name in ("evidence", "locks", "a"):
        (path / name).mkdir(mode=0o700)
    from .effect_workspace import immutable_json
    value = {"instance_id": actual, "host_id": authority.host_id(), "private_identity": list(identity(path.stat()))}
    immutable_json(marker, value)
    return {"state": "INITIALIZED", "instance_id": actual}


def _private(root: Path) -> tuple[Path, dict]:
    with directory(root) as fd:
        raw, _ = selected_bytes(fd, "g.identity.json", maximum=4096, deadline=time.monotonic()+1)
        marker = load(raw)
        info = os.stat("g", dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or list(identity(info)) != marker["private_identity"] or marker["host_id"] != authority.host_id()):
            raise BootstrapError("BOOTSTRAP_PRIVATE_EVIDENCE_UNAVAILABLE", 503)
    with authority.store(root) as db:
        if db.execute("SELECT instance_id FROM ep_installations").fetchone()[0] != marker["instance_id"]:
            raise BootstrapError("BOOTSTRAP_PRIVATE_INSTANCE_MISMATCH", 503)
    return root / "g", marker


@contextmanager
def _lock(root: Path, operation_id: str):
    path, marker = _private(root)
    with directory(path / "locks") as parent:
        fd = os.open(identifier(operation_id) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise BootstrapError("BOOTSTRAP_RESOURCE_LOCK_UNSAFE", 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BootstrapError("BOOTSTRAP_RESOURCE_BUSY", 409) from error
        yield fd
    finally:
        os.close(fd)


def _operation_mac(row: dict, token: str) -> str:
    fields = ("operation_id", "grant_id", "request_digest", "target_identity", "project_id", "repository_id",
        "state", "evidence", "created_at", "updated_at")
    body = {key: row[key] for key in fields}
    return hmac.new(token.encode("ascii"), authority.DOMAIN+b"operation\0"+canonical(body), hashlib.sha256).hexdigest()


def _signed_evidence(row: dict, token: str) -> str:
    return canonical({**row["evidence"], "_operation_mac": _operation_mac(row, token)}).decode()


def _row(db, operation_id: str, token: str) -> dict:
    row = db.execute("SELECT grant_id,request_digest,original_request,state,evidence,created_at,updated_at,target_identity,project_id,repository_id FROM ep_genesis_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise BootstrapError("BOOTSTRAP_OPERATION_UNKNOWN", 404)
    try:
        original = load(row[2].encode())
        evidence = load(row[4].encode())
        if not isinstance(evidence, dict):
            raise ValueError("invalid operation evidence")
        mac = evidence.pop("_operation_mac", None)
        result = {"operation_id": operation_id, "grant_id": row[0], "request_digest": row[1], "request": original,
            "state": row[3], "evidence": evidence, "created_at": row[5], "updated_at": row[6],
            "target_identity": row[7], "project_id": row[8], "repository_id": row[9]}
        if (not isinstance(mac, str) or not hmac.compare_digest(mac, _operation_mac(result, token))
                or digest(canonical(original)) != row[1]):
            raise ValueError("original reservation/state/evidence changed")
        completed = evidence["completed_steps"]
        all_steps = [*STEPS, "REGISTER_AND_ATTACH"]
        if (not isinstance(completed, list) or completed != all_steps[:len(completed)]
                or row[3] not in {"RESERVED", "EXECUTING", "PARTIAL", "EFFECT_UNCERTAIN", "COMPLETE"}):
            raise ValueError("invalid operation progression")
        if row[3] == "COMPLETE":
            known = evidence["known"]
            if (completed != all_steps or known.get("committed_declaration_verified") is not True
                    or known.get("commit_ref") != known.get("commit") or not known.get("commit")
                    or not evidence.get("registered_at")):
                raise ValueError("incomplete committed/registered evidence")
        return result
    except (BootstrapError, ValueError, KeyError, TypeError) as error:
        raise BootstrapError("BOOTSTRAP_OPERATION_CORRUPT", 500) from error


def _save(db, row: dict, token: str) -> None:
    row["updated_at"] = authority.now().isoformat()
    db.execute("UPDATE ep_genesis_operations SET state=?,evidence=?,updated_at=? WHERE operation_id=?",
        (row["state"], _signed_evidence(row, token), row["updated_at"], row["operation_id"]))


def _context(db, token, headers, operation_id: str) -> tuple[str, dict]:
    grant_id, scope = authority.authenticate(db, token, headers)
    if scope["operation_id"] != operation_id:
        raise BootstrapError("BOOTSTRAP_OPERATION_SCOPE_DENIED", 403)
    return grant_id, scope


def _require_original(row: dict, grant_id: str, scope: dict) -> None:
    if row["grant_id"] != grant_id or row["request_digest"] != scope["request_digest"]:
        raise BootstrapError("BOOTSTRAP_OPERATION_SCOPE_DENIED", 403)


def _conflicts(db, original, scope: dict) -> None:
    if db.execute("SELECT 1 FROM ep_project_registrations WHERE project_id=?", (original.project_id,)).fetchone():
        raise BootstrapError("BOOTSTRAP_PROJECT_ALREADY_REGISTERED", 409)
    if db.execute("SELECT 1 FROM ep_repository_registrations WHERE repository_id=? OR authority_repository_id=?",
            (original.repository_id, original.authority_repository_id)).fetchone():
        raise BootstrapError("BOOTSTRAP_REPOSITORY_ALREADY_REGISTERED", 409)
    target = str(Path(scope["workspace_root"]) / scope["target_name"])
    if db.execute("SELECT 1 FROM ep_local_repository_bindings WHERE local_root=? COLLATE NOCASE AND state='BOUND'", (target,)).fetchone():
        raise BootstrapError("BOOTSTRAP_TARGET_ALREADY_BOUND", 409)


def _still_absent(scope: dict) -> None:
    with directory(Path(scope["workspace_root"])) as fd:
        if list(identity(os.fstat(fd))) != scope["root_identity"]:
            raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
        try:
            os.stat(scope["target_name"], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise BootstrapError("BOOTSTRAP_TARGET_EXISTS", 409)


def admit(root: Path, *, token, headers: dict, value: dict, source_identity=None) -> dict:
    parsed, original = request(value)
    operation_id = parsed["operation_id"]
    _private(root)
    with authority.store(root, write=True, expected_source=source_identity) as db:
        grant_id, scope = _context(db, token, headers, operation_id)
        if digest(canonical(parsed)) != scope["request_digest"] or parsed["target_ref"] != scope["target_ref"]:
            raise BootstrapError("BOOTSTRAP_REQUEST_SCOPE_CONFLICT", 409)
        exists = db.execute("SELECT 1 FROM ep_genesis_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if exists:
            row = _row(db, operation_id, token)
            _require_original(row, grant_id, scope)
            return {**row, "newly_admitted": False}
        _conflicts(db, original, scope)
        _still_absent(scope)
        target_digest = digest(canonical({"root_identity": scope["root_identity"], "target_name": scope["target_name"].casefold()}))
        at = authority.now().isoformat()
        evidence = {"known": {}, "completed_steps": [], "attempts": [], "accepted_at": int(datetime.fromisoformat(at).timestamp())}
        first = {"operation_id": operation_id, "grant_id": grant_id, "request_digest": scope["request_digest"],
            "target_identity": target_digest, "project_id": original.project_id, "repository_id": original.repository_id,
            "state": "RESERVED", "evidence": evidence, "created_at": at, "updated_at": at}
        if db.execute("SELECT COUNT(*) FROM ep_genesis_operations").fetchone()[0] >= 4096:
            raise BootstrapError("BOOTSTRAP_OPERATION_CAPACITY", 409)
        try:
            db.execute("INSERT INTO ep_genesis_operations(operation_id,grant_id,request_digest,original_request,target_identity,project_id,repository_id,state,evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'RESERVED',?,?,?)",
                (operation_id, grant_id, scope["request_digest"], canonical(parsed).decode(), target_digest,
                 original.project_id, original.repository_id, _signed_evidence(first, token), at, at))
        except sqlite3.IntegrityError as error:
            raise BootstrapError("BOOTSTRAP_RESERVATION_CONFLICT", 409) from error
        return {**_row(db, operation_id, token), "newly_admitted": True}


def _line(stream, deadline: float, *, guard=None, records=None) -> bytes:
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(data) <= 262144:
            if guard is not None:
                guard()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise BootstrapError("BOOTSTRAP_HOST_DEADLINE", 409)
            if not selector.select(min(remaining, .05)):
                continue
            chunk = os.read(stream.fileno(), 1)
            if not chunk:
                raise BootstrapError("BOOTSTRAP_HOST_ENDED_WITHOUT_RECEIPT", 409)
            data.extend(chunk)
            if records is not None:
                if len(records) >= 262144:
                    raise BootstrapError("BOOTSTRAP_HOST_OUTPUT_LIMIT", 409)
                records.extend(chunk)
            if chunk == b"\n":
                return bytes(data)
    raise BootstrapError("BOOTSTRAP_HOST_OUTPUT_LIMIT", 409)


def _write(stream, payload: bytes, deadline: float, *, guard=None) -> None:
    """Upload has the same finite deadline as startup/readback, even at EAGAIN."""
    fd = stream.fileno()
    os.set_blocking(fd, False)
    pending = memoryview(payload)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while pending:
            if guard is not None:
                guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BootstrapError("BOOTSTRAP_HOST_DEADLINE", 409)
            if not selector.select(min(remaining, .05)):
                continue
            try:
                written = os.write(fd, pending)
            except BlockingIOError:
                continue
            if written <= 0:
                raise BootstrapError("BOOTSTRAP_HOST_ENDED_WITHOUT_RECEIPT", 409)
            pending = pending[written:]


def _execute_step(root: Path, *, operation_id: str, step: str, token, headers: dict, source_identity, lock_fd: int) -> dict:
    path, marker = _private(root)
    native = genesis_native.runtime()
    with authority.store(root, write=True, expected_source=source_identity) as db:
        grant_id, scope = _context(db, token, headers, operation_id)
        row = _row(db, operation_id, token)
        _require_original(row, grant_id, scope)
        attempt = {"attempt_id": secrets.token_hex(16), "step": step, "state": "INTENT_RECORDED"}
        if len(row["evidence"]["attempts"]) >= 256:
            raise BootstrapError("BOOTSTRAP_EVIDENCE_CAPACITY", 409)
        row["evidence"]["attempts"].append(attempt)
        row["state"] = "EXECUTING"
        _save(db, row, token)
    job = {"scope": scope, "known": row["evidence"]["known"], "step": step,
        "accepted_at": row["evidence"]["accepted_at"], "bundle": row["request"]["bundle"]}
    mutating = step not in {"PREFLIGHT", "VALIDATE_DOCS", "VERIFY_COMMIT"}
    target = Path(scope["workspace_root"]) / scope["target_name"]
    command = genesis_native.command(native=native, anchor=path / "a", target=target,
        writable=mutating, git_metadata=step.startswith("GIT_"), readable=(Path(scope["validator_path"]), Path(__file__).resolve().parent.parent),
        prepared=Path(scope["git_staging_path"] if step == "GIT_INIT" else scope["staging_path"]) if step in {"CREATE_TARGET", "GIT_INIT"} else None,
        profile_id="ep-genesis-"+attempt["attempt_id"])
    stderr_path = path / "evidence" / (attempt["attempt_id"]+".stderr")
    stdout_path = path / "evidence" / (attempt["attempt_id"]+".stdout")
    process = None
    process_identity = None
    records = bytearray()
    error_bytes = 0
    error_sink = None
    error_descriptor = None
    def drain_errors():
        nonlocal error_bytes
        if process is None or process.stderr is None or error_sink is None:
            return
        os.set_blocking(process.stderr.fileno(), False)
        while True:
            try:
                chunk = os.read(process.stderr.fileno(), 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            room = 262144-error_bytes
            error_sink.write(chunk[:room])
            error_sink.flush()
            error_bytes += min(room, len(chunk))
            if len(chunk) > room:
                raise BootstrapError("BOOTSTRAP_HOST_ERROR_OUTPUT_LIMIT", 409)
    def drain_tail():
        if process is None or process.stdout is None or process.stdout.closed:
            return
        os.set_blocking(process.stdout.fileno(), False)
        while True:
            try:
                chunk = os.read(process.stdout.fileno(), 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            room = 262144-len(records)
            records.extend(chunk[:room])
            if len(chunk) > room:
                raise BootstrapError("BOOTSTRAP_HOST_OUTPUT_LIMIT", 409)
    deadline = time.monotonic()+30
    try:
        with stderr_path.open("xb") as errors:
            error_sink = errors
            error_descriptor = os.dup(errors.fileno())
            stderr_path.chmod(0o600)
            process = subprocess.Popen(command, cwd=path / "a", env=genesis_native.environment(native),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=(lock_fd,))
            process_identity = capture_process_identity(process.pid, process.pid)
            if process_identity is None:
                raise BootstrapError("BOOTSTRAP_HOST_IDENTITY_UNAVAILABLE", 503)
            with authority.store(root, write=True, expected_source=source_identity) as db:
                fresh = _row(db, operation_id, token)
                fresh["evidence"]["attempts"][-1]["process"] = asdict(process_identity)
                _save(db, fresh, token)
            _write(process.stdin, canonical(job), deadline, guard=drain_errors)
            ready_raw = _line(process.stdout, deadline, guard=drain_errors, records=records)
            ready = load(ready_raw)
            if ready != {"state": "STEP_READY", "job_digest": digest(canonical(job))}:
                raise BootstrapError("BOOTSTRAP_HOST_READY_CONFLICT", 409)
            # One tiny local pipe permit is the step admission linearization.
            # No network or validator/process wait is held under authoritylock.
            with authority.store(root, write=True, expected_source=source_identity) as db:
                current_grant, current_scope = _context(db, token, headers, operation_id)
                fresh = _row(db, operation_id, token); _require_original(fresh, current_grant, current_scope)
                if current_scope != scope:
                    raise BootstrapError("BOOTSTRAP_SCOPE_CHANGED", 403)
                try:
                    if os.write(process.stdin.fileno(), b"EXECUTE\n") != 8:
                        raise BootstrapError("BOOTSTRAP_PERMIT_NOT_DELIVERED", 409)
                except BlockingIOError as error:
                    raise BootstrapError("BOOTSTRAP_PERMIT_NOT_DELIVERED", 409) from error
                fresh["evidence"]["attempts"][-1]["state"] = "STEP_PERMITTED"
                _save(db, fresh, token)
            process.stdin.close()
            result_raw = _line(process.stdout, deadline, guard=drain_errors, records=records)
            result = load(result_raw)
            protocol_size = len(records)
            while process.poll() is None:
                drain_errors()
                drain_tail()
                if time.monotonic() >= deadline:
                    raise BootstrapError("BOOTSTRAP_HOST_DEADLINE", 409)
                time.sleep(.01)
            exit_code = process.wait(timeout=.01)
            drain_errors()
            drain_tail()
            if len(records) != protocol_size:
                raise BootstrapError("BOOTSTRAP_HOST_UNEXPECTED_OUTPUT", 409)
            _require_group_stopped(process.pid)
            if exit_code or result.get("state") != "STEP_COMPLETED" or result.get("step") != step:
                raise BootstrapError(result.get("error", "BOOTSTRAP_HOST_STEP_FAILED"), 409)
    finally:
        primary = sys.exc_info()[1]
        cleanup_errors = []
        if process is not None:
            # Each close/stop/wait/drain is independent. A failure in one may
            # not skip actual process reconciliation or erase the primary.
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            except OSError as error:
                cleanup_errors.append(error)
            try:
                if process.poll() is None and process_identity is not None and verify_process_identity(process_identity) == "MATCH":
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError as error:
                cleanup_errors.append(error)
            try:
                process.wait(timeout=5)
            except subprocess.SubprocessError as error:
                cleanup_errors.append(error)
            try:
                _require_group_stopped(process.pid)
            except (OSError, BootstrapError) as error:
                cleanup_errors.append(error)
            try:
                # The context-managed sink may have closed while unwinding.
                # Reopen only that same private file, retaining the final tail.
                if error_descriptor is not None:
                    with os.fdopen(error_descriptor, "ab", closefd=False) as saved_errors:
                        error_sink = saved_errors
                        drain_errors()
                drain_tail()
            except (OSError, ValueError, BootstrapError) as error:
                cleanup_errors.append(error)
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except OSError as error:
                    cleanup_errors.append(error)
        if error_descriptor is not None:
            try:
                os.close(error_descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        try:
            with stdout_path.open("xb") as saved:
                saved.write(records)
            stdout_path.chmod(0o600)
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            cleanup_path = path / "evidence" / (attempt["attempt_id"]+".cleanup.json")
            try:
                with cleanup_path.open("xb") as saved:
                    saved.write(canonical({"primary": _private_error(primary),
                        "cleanup_errors": [_private_error(error) for error in cleanup_errors],
                        "parent_stopped": process is None or process.poll() is not None,
                        "process": asdict(process_identity) if process_identity else None}))
                cleanup_path.chmod(0o600)
            except OSError as error:
                cleanup_errors.append(error)
            raise CleanupFailure(primary, cleanup_errors) from primary
    # Only durable primary logs plus confirmed whole-group stop can advance the
    # original step. These are observations, not a fresh effect authorization.
    with authority.store(root, write=True, expected_source=source_identity) as db:
        fresh = _row(db, operation_id, token)
        fresh["evidence"]["known"].update(result["result"])
        fresh["evidence"]["completed_steps"].append(step)
        fresh["evidence"]["attempts"][-1].update(state="STEP_COMPLETED", exit_code=exit_code)
        _save(db, fresh, token)
    return result["result"]


def _private_error(error):
    return None if error is None else {"type": type(error).__name__, "detail": str(error)[:2048]}


class CleanupFailure(BootstrapError):
    """Keep the actual primary and every cleanup exception; never a PASS."""
    def __init__(self, primary, cleanup_errors):
        super().__init__("BOOTSTRAP_HOST_CLEANUP_OR_EVIDENCE_FAILED", 503)
        self.primary = primary
        self.cleanup_errors = tuple(cleanup_errors)


def _require_group_stopped(group: int) -> None:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return
    raise BootstrapError("BOOTSTRAP_HOST_GROUP_ACTIVE_OR_UNCERTAIN", 503)


def _active_previous(row: dict) -> None:
    for attempt in row["evidence"]["attempts"]:
        record = attempt.get("process")
        if record is None:
            continue
        expected = ProcessIdentity(**record)
        state = verify_process_identity(expected)
        if state != "NOT_ACTIVE":
            raise BootstrapError("BOOTSTRAP_PRIOR_HOST_ACTIVE_OR_UNCERTAIN", 409)
        try:
            os.killpg(expected.process_group, 0)
        except ProcessLookupError:
            continue
        raise BootstrapError("BOOTSTRAP_PRIOR_HOST_GROUP_UNCERTAIN", 409)


def maintenance_activity(root: Path) -> bool:
    """Preserved bootstrap work must quiesce even after its Server has died."""
    try:
        path, _ = _private(root)
        with directory(path / "locks") as parent:
            names = os.listdir(parent)
            if len(names) > 4096:
                return True
            for name in names:
                if not name.endswith(".lock"):
                    return True
                identifier(name[:-5])
                fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
        with authority.store(root) as db:
            for (raw,) in db.execute("SELECT evidence FROM ep_genesis_operations"):
                _active_previous({"evidence": load(raw.encode())})
        return False
    except (OSError, BootstrapError, ValueError, KeyError, TypeError, sqlite3.Error):
        return True


def execute(root: Path, *, operation_id: str, token, headers: dict, source_identity=None) -> None:
    with _lock(root, operation_id) as lock_fd:
        with authority.store(root, expected_source=source_identity) as db:
            grant_id, scope = _context(db, token, headers, operation_id)
            row = _row(db, operation_id, token); _require_original(row, grant_id, scope)
            _active_previous(row)
        if row["state"] == "COMPLETE":
            return
        try:
            for step in STEPS:
                with authority.store(root, expected_source=source_identity) as db:
                    row = _row(db, operation_id, token)
                if step in row["evidence"]["completed_steps"]:
                    continue
                _execute_step(root, operation_id=operation_id, step=step, token=token, headers=headers,
                    source_identity=source_identity, lock_fd=lock_fd)
            with authority.store(root, write=True, expected_source=source_identity) as db:
                grant_id, scope = _context(db, token, headers, operation_id)
                row = _row(db, operation_id, token); _require_original(row, grant_id, scope)
                parsed, original = request(row["request"])
                _conflicts(db, original, scope)
                known = row["evidence"]["known"]
                target_path = Path(scope["workspace_root"]) / scope["target_name"]
                # These are bounded no-follow reads, not physical writes or
                # unqualified Git execution in CENTRAL. Helpers otherwise
                # validate only declaration IDs from the working checkout.
                from .genesis_worker import verify_files, git_snapshot
                def registration_boundary():
                    verify_files(scope, known, original.files, git=True)
                    if not known.get("verified_git_snapshot") or git_snapshot(scope, known) != known["verified_git_snapshot"]:
                        raise BootstrapError("BOOTSTRAP_COMMITTED_METADATA_CHANGED", 409)
                    current_grant, current_scope = _context(db, token, headers, operation_id)
                    _require_original(row, current_grant, current_scope)
                    if current_scope != scope:
                        raise BootstrapError("BOOTSTRAP_SCOPE_CHANGED", 403)
                registration_boundary()
                # Existing helpers are intentionally upserts. The preceding
                # exact-identity conflict guard makes this creation-only.
                declaration = load(original.files[".engineering-platform/repository.json"])
                project_topology.register_server_local_topology(db, declaration=declaration)
                local_repository_binding.bind_local_repository(db, project_id=original.project_id,
                    repository_id=original.repository_id, local_root=target_path, data_root=root)
                # Helpers may read a resolved path; any late physical change
                # rolls back their writes and cannot become a canonical bind.
                registration_boundary()
                row["state"] = "COMPLETE"
                row["evidence"]["completed_steps"].append("REGISTER_AND_ATTACH")
                row["evidence"]["registered_at"] = authority.now().isoformat()
                _save(db, row, token)
        except BaseException as error:
            # Observation/evidence is not rollback authority. Never erase or
            # compensate partially created physical resources or declarations.
            with authority.store(root, write=True, expected_source=source_identity) as db:
                row = _row(db, operation_id, token)
                if row["state"] != "COMPLETE":
                    row["state"] = "PARTIAL" if any(s != "PREFLIGHT" for s in row["evidence"]["completed_steps"]) else "EFFECT_UNCERTAIN"
                    row["evidence"]["last_error"] = error.code if isinstance(error, BootstrapError) else "BOOTSTRAP_EXECUTION_INTERRUPTED"
                    _save(db, row, token)
            raise


def read(root: Path, *, operation_id: str, token, headers: dict, source_identity=None) -> dict:
    identifier(operation_id)
    _private(root)
    with authority.store(root, expected_source=source_identity) as db:
        grant_id, scope = _context(db, token, headers, operation_id)
        row = _row(db, operation_id, token); _require_original(row, grant_id, scope)
        _, original = request(row["request"])
        known = row["evidence"]["known"]
        return {"contract_version": "1.0", "operation_kind": "CREATE_GENESIS_LOCAL", "operation_id": operation_id,
            "producer_version": CURRENT_PLATFORM_VERSION, "execution_mode": "GENESIS", "baseline": "portable-docs/1.0.0",
            "authorization_ref": row["grant_id"], "placement_precondition": "ABSENT_VERIFIED_AT_ADMISSION",
            "request_digest": row["request_digest"], "target_ref": scope["target_ref"], "state": row["state"],
            "effect_plan_binding": scope["effect_plan_binding"], "bundle_digest": original.bundle_digest,
            "manifest_digest": original.manifest_digest, "input_digest": original.input_digest,
            "instance_id": scope["instance_id"], "host_id": scope["host_id"],
            "identity": {"project_id": original.project_id, "authority_repository_id": original.authority_repository_id,
                "repository_id": original.repository_id, "state": "REGISTERED" if row["state"] == "COMPLETE" else
                "COMMITTED" if known.get("committed_declaration_verified") else "RESERVED"},
            "completed_steps": row["evidence"]["completed_steps"], "commit": known.get("commit_ref"), "tree": known.get("tree"),
            "step_effects": [{"attempt_id": a["attempt_id"], "step": a["step"], "outcome": a["state"],
                "exit_code": a.get("exit_code"), "private_evidence_ref": "genesis-attempt:"+a["attempt_id"]} for a in row["evidence"]["attempts"]],
            "validator_sha256": known.get("validator_sha256"), "validation": known.get("validation"),
            "last_error": row["evidence"].get("last_error"), "source_updated_at": row["updated_at"],
            "observed_at": authority.now().isoformat(), "forge_readiness": "NOT_OWNED_BY_EP",
            "remote_governance": "NOT_APPLICABLE_GENESIS", "product_code_coverage": "NOT_APPLICABLE_NO_PRODUCT_CODE",
            "in_repository_validation_command": "NOT_QUALIFIED_AFTER_GIT_METADATA",
            "effect_result_semantics": "ORIGINAL_OPERATION_AS_OF_RECORDED_STEP_EVIDENCE"}


def operation(root: Path, *, token, headers: dict, operation_id: str, body: dict | None = None,
              resume: bool = False, source_identity=None, publish) -> None:
    if source_identity is None:
        source_identity = authority.database_identity(root)
    if body is not None:
        accepted = admit(root, token=token, headers=headers, value=body, source_identity=source_identity)
        if accepted["operation_id"] != operation_id:
            raise BootstrapError("BOOTSTRAP_OPERATION_SCOPE_DENIED", 403)
        if accepted["newly_admitted"]:
            execute(root, operation_id=operation_id, token=token, headers=headers, source_identity=source_identity)
    elif resume:
        execute(root, operation_id=operation_id, token=token, headers=headers, source_identity=source_identity)
    result = read(root, operation_id=operation_id, token=token, headers=headers, source_identity=source_identity)
    def check():
        # The transport calls this after bounded serialization, before headers.
        with authority.store(root, expected_source=source_identity) as db:
            grant_id, scope = _context(db, token, headers, operation_id)
            _require_original(_row(db, operation_id, token), grant_id, scope)
    publish(result, result["instance_id"], check)
