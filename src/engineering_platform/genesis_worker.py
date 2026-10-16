"""Fixed bounded local-host steps, executed only inside the owning native sandbox.

No credential, CENTRAL writer, scheduler, renderer, target-code import or shell.
Every invocation performs one explicitly admitted infrastructure step and exits.
The controller durably journals its intent and checks current authority first.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import signal
import ctypes
import errno
import traceback

from .genesis_contract import BootstrapError, bundle, canonical, digest, load, MAX_BODY, VALIDATOR_SHA256
from .preproject_inspection import directory, identity, selected_bytes, inventory, read_relative
from .repository_attachment import parse_repository_attachment
from .effect_workspace import child_environment


@contextmanager
def target(scope: dict, known: dict):
    root = Path(scope["workspace_root"])
    with directory(root) as parent:
        if list(identity(os.fstat(parent))) != scope["root_identity"]:
            raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
        fd = os.open(scope["target_name"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if list(identity(os.fstat(fd))) != known.get("target_identity"):
                raise BootstrapError("BOOTSTRAP_TARGET_OWNERSHIP_UNCERTAIN", 409)
            yield fd
            current = os.stat(scope["target_name"], dir_fd=parent, follow_symlinks=False)
            if identity(current) != identity(os.fstat(fd)):
                raise BootstrapError("BOOTSTRAP_TARGET_CHANGED", 409)
            with directory(root) as fresh:
                if identity(os.fstat(fresh)) != identity(os.fstat(parent)):
                    raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
        finally:
            os.close(fd)


@contextmanager
def prepared_capability(scope: dict, parent_fd: int | None = None):
    # The native worker's CWD is the exact owning private anchor. Opening from
    # that pinned directory avoids granting its absolute private ancestors.
    source = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if parent_fd is None else os.dup(parent_fd)
    prepared = None
    try:
        if list(identity(os.fstat(source))) != scope["staging_parent_identity"]:
            raise BootstrapError("BOOTSTRAP_PREPARED_CAPABILITY_CHANGED", 409)
        prepared = os.open(Path(scope["staging_path"]).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source)
        yield source, prepared
    finally:
        if prepared is not None:
            os.close(prepared)
        os.close(source)


def create(scope: dict, *, prepared_parent_fd: int | None = None) -> dict:
    staging = Path(scope["staging_path"])
    with prepared_capability(scope, prepared_parent_fd) as (source, prepared), directory(Path(scope["workspace_root"])) as parent:
        if (list(identity(os.fstat(prepared))) != scope["staging_identity"]
                or list(identity(os.fstat(source))) != scope["staging_parent_identity"] or os.listdir(prepared)):
            raise BootstrapError("BOOTSTRAP_PREPARED_CAPABILITY_CHANGED", 409)
        if list(identity(os.fstat(parent))) != scope["root_identity"]:
            raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
        if os.fstat(prepared).st_dev != os.fstat(parent).st_dev or sys.platform != "darwin":
            raise BootstrapError("BOOTSTRAP_ATOMIC_PUBLICATION_UNSUPPORTED", 422)
        # Darwin's no-replace rename publishes the already owner-provisioned
        # inode. mkdir does not return an inode and cannot establish this proof.
        # No copy/delete, replacement, or exists-check fallback is permitted.
        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renameatx_np
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(source, os.fsencode(staging.name), parent, os.fsencode(scope["target_name"]), 0x4):
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise BootstrapError("BOOTSTRAP_TARGET_EXISTS", 409)
            raise BootstrapError("BOOTSTRAP_ATOMIC_PUBLICATION_UNAVAILABLE", 422)
        fd = os.open(scope["target_name"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if identity(info) != identity(os.fstat(prepared)):
                raise BootstrapError("BOOTSTRAP_TARGET_OWNERSHIP_UNCERTAIN", 409)
            with directory(Path(scope["workspace_root"])) as fresh:
                if identity(os.fstat(fresh)) != identity(os.fstat(parent)):
                    raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
            os.fsync(parent)
            os.fsync(source)
            return {"target_identity": list(identity(info))}
        finally:
            os.close(fd)


def _read_file(root: int, name: str, directories: dict) -> tuple[bytes, list[int]]:
    fd = os.dup(root)
    try:
        parts = name.split("/")
        prefix = ""
        for part in parts[:-1]:
            prefix += part + "/"
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if prefix in directories and list(identity(os.fstat(child))) != directories[prefix]:
                os.close(child)
                raise BootstrapError("BOOTSTRAP_DIRECTORY_CHANGED", 409)
            os.close(fd)
            fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o644:
            raise BootstrapError("BOOTSTRAP_FILE_CHANGED", 409)
        raw, _ = selected_bytes(fd, parts[-1], maximum=131072, deadline=time.monotonic()+2)
        return raw, list(identity(info))
    finally:
        os.close(fd)


def verify_files(scope: dict, known: dict, files: dict, *, git: bool) -> dict:
    with target(scope, known) as fd:
        items = inventory(fd, deadline=time.monotonic()+3, maximum=2048)
        actual = {p for p, v in items.items() if not stat.S_ISDIR(v[2]) and not (git and p.startswith(".git/"))}
        if actual != set(files) or any(p == ".git" and not stat.S_ISDIR(v[2]) for p, v in items.items()):
            raise BootstrapError("BOOTSTRAP_FILE_INVENTORY_CONFLICT", 409)
        if not git and any(p == ".git" or p.startswith(".git/") for p in items):
            raise BootstrapError("BOOTSTRAP_UNEXPECTED_GIT", 409)
        expected_directories = {"/".join(name.split("/")[:i]) for name in files for i in range(1, len(name.split("/")))}
        actual_directories = {p for p, v in items.items() if stat.S_ISDIR(v[2]) and p != ".git" and not p.startswith(".git/")}
        if actual_directories != expected_directories:
            raise BootstrapError("BOOTSTRAP_DIRECTORY_INVENTORY_CONFLICT", 409)
        directory_identities = {name + "/": list(items[name][:2]) for name in actual_directories}
        if known.get("directory_identities") and directory_identities != known["directory_identities"]:
            raise BootstrapError("BOOTSTRAP_DIRECTORY_CHANGED", 409)
        observations = {}
        for name, expected in files.items():
            raw, file_identity = _read_file(fd, name, directory_identities)
            if raw != expected or (name in known.get("file_identities", {}) and file_identity != known["file_identities"][name]):
                raise BootstrapError("BOOTSTRAP_FILE_CHANGED", 409)
            observations[name] = file_identity
        return {"file_identities": observations, "directory_identities": directory_identities}


def write_files(scope: dict, known: dict, files: dict) -> dict:
    if known.get("file_identities"):
        return verify_files(scope, known, files, git=False)
    with target(scope, known) as root:
        if os.listdir(root):
            raise BootstrapError("BOOTSTRAP_PARTIAL_FILES_OWNERSHIP_UNCERTAIN", 409)
        directories = {}
        observations = {}
        for name, raw in sorted(files.items()):
            fd = os.dup(root)
            prefix = ""
            try:
                parts = name.split("/")
                for part in parts[:-1]:
                    prefix += part + "/"
                    if prefix not in directories:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        directories[prefix] = list(identity(os.stat(part, dir_fd=fd, follow_symlinks=False)))
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    if list(identity(os.fstat(child))) != directories[prefix]:
                        os.close(child)
                        raise BootstrapError("BOOTSTRAP_DIRECTORY_CHANGED", 409)
                    os.close(fd)
                    fd = child
                item = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    remaining = memoryview(raw)
                    while remaining:
                        count = os.write(item, remaining)
                        if count <= 0:
                            raise OSError("short document write")
                        remaining = remaining[count:]
                    os.fchmod(item, 0o644)
                    os.fsync(item)
                    observations[name] = list(identity(os.fstat(item)))
                finally:
                    os.close(item)
                os.fsync(fd)
            finally:
                os.close(fd)
    return verify_files(scope, {**known, "file_identities": observations, "directory_identities": directories}, files, git=False)


def validate_documents(scope: dict, known: dict, files: dict, manifest_digest: str) -> dict:
    verify_files(scope, known, files, git=False)
    # Read and hash the exact bytes that are compiled inside the child. A
    # pre-exec path checksum followed by an ordinary file exec would race.
    wrapper = """import os,stat,hashlib,sys
f=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
try:
 s=os.fstat(f)
 if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_size>131072:raise ValueError('trusted validator file')
 b=b''
 while len(b)<=131072:
  c=os.read(f,min(65536,131073-len(b)))
  if not c:break
  b+=c
 if hashlib.sha256(b).hexdigest()!=sys.argv[2]:raise ValueError('external trusted validator pin')
finally:os.close(f)
p=sys.argv[1];root=sys.argv[3];sys.argv=[p,root]
exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})
"""
    result = subprocess.run((sys.executable, "-I", "-S", "-c", wrapper,
        scope["validator_path"], VALIDATOR_SHA256, str(_git_root(scope))),
        env=child_environment(), capture_output=True, timeout=5, check=False)
    sys.stderr.buffer.write(result.stderr[:65536])
    if result.returncode or len(result.stdout) > 65536:
        sys.stderr.buffer.write(result.stdout[:65536])
        raise BootstrapError("BOOTSTRAP_TRUSTED_VALIDATION_FAILED", 409)
    receipt = load(result.stdout)
    if receipt != {"state": "VALIDATED_PROPOSAL", "manifest_digest": manifest_digest,
            "product_code_coverage": "NOT_APPLICABLE_NO_PRODUCT_CODE", "artifacts": 37}:
        raise BootstrapError("BOOTSTRAP_VALIDATION_RECEIPT_CONFLICT", 409)
    verify_files(scope, known, files, git=False)
    return {"validation": receipt, "validator_sha256": VALIDATOR_SHA256,
        "validation_scope": "EXACT_MANIFEST_DOCUMENTS_BEFORE_GIT_METADATA",
        "in_repository_command_after_git": "NOT_QUALIFIED_OR_CLAIMED"}


def _git(root: Path, *args: str, known: dict, input_bytes: bytes | None = None) -> bytes:
    command = ("git", "--git-dir=.", "--work-tree="+str(root), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", "-c", "protocol.allow=never",
        "-c", "diff.autoRefreshIndex=false", *args)
    # Pin the metadata directory as the child's actual working directory.
    # A replaced .git path cannot redirect a mutation to foreign metadata.
    with directory(root) as parent:
        if list(identity(os.fstat(parent))) != known["target_identity"]:
            raise BootstrapError("BOOTSTRAP_TARGET_CHANGED", 409)
        metadata = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if list(identity(os.fstat(metadata))) != known["git_identity"]:
                raise BootstrapError("BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN", 409)
            # This dedicated worker is single-threaded; fchdir in its forked
            # child retains the validated inode through exec without a path race.
            result = subprocess.run(command, preexec_fn=lambda: os.fchdir(metadata), pass_fds=(metadata,),
                env=child_environment(), input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, check=False)
        finally:
            os.close(metadata)
    if result.returncode:
        # Original stderr is private step evidence, not a public error string.
        sys.stderr.buffer.write(result.stderr[:65536])
        raise BootstrapError("BOOTSTRAP_LOCAL_GIT_FAILED", 409)
    if len(result.stdout) > 262144:
        raise BootstrapError("BOOTSTRAP_GIT_OUTPUT_LIMIT", 409)
    return result.stdout


def _git_root(scope: dict) -> Path:
    return Path(scope["workspace_root"]) / scope["target_name"]


def _git_guard(scope: dict, known: dict) -> None:
    with target(scope, known) as fd:
        metadata = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if list(identity(os.fstat(metadata))) != known.get("git_identity"):
                raise BootstrapError("BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN", 409)
            config, config_digest = selected_bytes(metadata, "config", maximum=65536, deadline=time.monotonic()+2)
            if config_digest != known.get("git_config_digest"):
                raise BootstrapError("BOOTSTRAP_GIT_CONFIG_CHANGED", 409)
            for name in ("objects/info/alternates", "info/grafts", "shallow", "refs/replace"):
                if os.path.lexists(_git_root(scope) / ".git" / name):
                    raise BootstrapError("BOOTSTRAP_GIT_FOREIGN_CONFIGURATION", 409)
        finally:
            os.close(metadata)


def git_snapshot(scope: dict, known: dict) -> dict:
    """Finite, no-follow physical/content binding of the verified metadata.

    CENTRAL compares this original observation without launching Git while
    holding its transaction. Unknown files and metadata replacements differ.
    """
    observations = {}
    total = 0
    deadline = time.monotonic() + 3
    with target(scope, known) as root:
        metadata = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            if list(identity(os.fstat(metadata))) != known["git_identity"]:
                raise BootstrapError("BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN", 409)
            entries = inventory(metadata, deadline=deadline, maximum=256)
            for name, info in sorted(entries.items()):
                if stat.S_ISDIR(info[2]):
                    observations[name] = {"identity": list(info[:2]), "kind": "directory"}
                    continue
                raw = read_relative(metadata, name, maximum=262144, deadline=deadline)
                sha = digest(raw)
                total += len(raw)
                if total > 2097152:
                    raise BootstrapError("BOOTSTRAP_GIT_METADATA_CAPACITY", 409)
                observations[name] = {"identity": list(info[:2]), "sha256": sha}
        finally:
            os.close(metadata)
    return observations


def git_initialize(scope: dict, known: dict, files: dict) -> dict:
    if known.get("git_identity"):
        _git_guard(scope, known)
        return {k: known[k] for k in ("git_identity", "git_config_digest")}
    verify_files(scope, known, files, git=False)
    with target(scope, known) as fd:
        os.mkdir(".git", 0o700, dir_fd=fd)
        metadata = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            initializing = {**known, "git_identity": list(identity(os.fstat(metadata)))}
        finally:
            os.close(metadata)
    _git(_git_root(scope), "-c", "init.templateDir=", "init", "--initial-branch=main", known=initializing)
    with target(scope, known) as fd:
        gitfd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            _, config_digest = selected_bytes(gitfd, "config", maximum=65536, deadline=time.monotonic()+2)
            return {"git_identity": list(identity(os.fstat(gitfd))), "git_config_digest": config_digest}
        finally:
            os.close(gitfd)


def prepare_commit(scope: dict, known: dict, files: dict, accepted_at: int) -> dict:
    _git_guard(scope, known)
    verify_files(scope, known, files, git=True)
    root = _git_root(scope)
    # Object/index plumbing consumes only the admitted bytes. `git add` can
    # execute a target-configured clean filter after an earlier file guard.
    hierarchy = {}
    for name, raw in sorted(files.items()):
        node = hierarchy
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        object_id = _git(root, "hash-object", "--no-filters", "-w", "--stdin", known=known, input_bytes=raw).decode().strip()
        expected = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if object_id != expected:
            raise BootstrapError("BOOTSTRAP_BLOB_CONFLICT", 409)
        node[parts[-1]] = object_id
    def write_tree(node):
        entries = []
        for name, value in sorted(node.items()):
            if isinstance(value, dict):
                entries.append(b"040000 tree " + write_tree(value).encode() + b"\t" + name.encode() + b"\0")
            else:
                entries.append(b"100644 blob " + value.encode() + b"\t" + name.encode() + b"\0")
        return _git(root, "mktree", "-z", known=known, input_bytes=b"".join(entries)).decode().strip()
    tree = write_tree(hierarchy)
    declaration = b"tree " + tree.encode() + b"\nauthor Engineering Platform Bootstrap <bootstrap@localhost> "
    author = str(accepted_at).encode() + b" +0000"
    raw = declaration + author + b"\ncommitter Engineering Platform Bootstrap <bootstrap@localhost> " + author
    raw += b"\n\nCREATE_GENESIS_LOCAL " + scope["operation_id"].encode() + b"\n"
    commit = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return {"tree": tree, "commit": commit, "commit_utf8": raw.decode()}


def commit_object(scope: dict, known: dict) -> dict:
    _git_guard(scope, known)
    raw = known["commit_utf8"].encode()
    root = _git_root(scope)
    object_path = root / ".git/objects" / known["commit"][:2] / known["commit"][2:]
    if os.path.lexists(object_path):
        if _git(root, "cat-file", "commit", known["commit"], known=known) != raw:
            raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
        return {"commit_object": known["commit"], "recovered_existing_object": True}
    actual = _git(root, "hash-object", "-t", "commit", "-w", "--stdin", known=known, input_bytes=raw).decode().strip()
    if actual != known["commit"]:
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    return {"commit_object": actual}


def commit_ref(scope: dict, known: dict) -> dict:
    _git_guard(scope, known)
    root = _git_root(scope)
    raw = _git(root, "cat-file", "commit", known["commit"], known=known)
    if raw != known["commit_utf8"].encode():
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    reference = root / ".git/refs/heads/main"
    if os.path.lexists(reference):
        if _git(root, "rev-parse", "refs/heads/main", known=known).decode().strip() != known["commit"]:
            raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
        return {"commit_ref": known["commit"], "recovered_existing_ref": True}
    # Creation-only CAS; an unrelated concurrent ref is never overwritten.
    _git(root, "update-ref", "refs/heads/main", known["commit"], "0"*40, known=known)
    return {"commit_ref": known["commit"]}


def verify_commit(scope: dict, known: dict, files: dict) -> dict:
    _git_guard(scope, known)
    verify_files(scope, known, files, git=True)
    before = git_snapshot(scope, known)
    root = _git_root(scope)
    if _git(root, "rev-parse", "HEAD", known=known).decode().strip() != known["commit"]:
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    if _git(root, "cat-file", "commit", known["commit"], known=known) != known["commit_utf8"].encode():
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    rows = _git(root, "ls-tree", "-rz", "--full-tree", known["commit"], known=known).split(b"\0")
    observed = {}
    for row in filter(None, rows):
        info, name = row.split(b"\t", 1)
        mode, kind, object_id = info.decode("ascii").split()
        name = name.decode("utf-8")
        if mode != "100644" or kind != "blob" or name not in files:
            raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
        raw = _git(root, "cat-file", "blob", object_id, known=known)
        if raw != files[name]:
            raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
        observed[name] = digest(raw)
    if set(observed) != set(files) or _git(root, "rev-parse", "HEAD^{tree}", known=known).decode().strip() != known["tree"]:
        raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
    declaration = parse_repository_attachment(load(files[".engineering-platform/repository.json"]))
    if before != git_snapshot(scope, known):
        raise BootstrapError("BOOTSTRAP_GIT_CHANGED_DURING_VERIFICATION", 409)
    return {"committed_files": observed, "project_id": declaration.project_id,
        "repository_id": declaration.repository_id, "authority_repository_id": declaration.authority_repository_id,
        "commit": known["commit"], "tree": known["tree"], "committed_declaration_verified": True,
        "verified_git_snapshot": before}


def main() -> int:
    try:
        job = load(sys.stdin.buffer.readline(MAX_BODY+1))
        scope, known, step = job["scope"], job["known"], job["step"]
        original = bundle(job["bundle"])
        if original.bundle_digest != scope["bundle_digest"]:
            raise BootstrapError("BOOTSTRAP_WORKER_BINDING_CONFLICT", 409)
        functions = {
            "PREFLIGHT": lambda: {"native_profile_started": True, "business_effects": 0},
            "CREATE_TARGET": lambda: create(scope),
            "WRITE_FILES": lambda: write_files(scope, known, original.files),
            "VALIDATE_DOCS": lambda: validate_documents(scope, known, original.files, original.manifest_digest),
            "GIT_INIT": lambda: git_initialize(scope, known, original.files),
            "GIT_PREPARE": lambda: prepare_commit(scope, known, original.files, job["accepted_at"]),
            "GIT_OBJECT": lambda: commit_object(scope, known),
            "GIT_REF": lambda: commit_ref(scope, known),
            "VERIFY_COMMIT": lambda: verify_commit(scope, known, original.files),
        }
        if step not in functions:
            raise BootstrapError("BOOTSTRAP_WORKER_STEP_UNSUPPORTED", 422)
        # The parent rechecks current scope/expiry/source after installed
        # startup and immediately before this one effect is admitted.
        print(canonical({"state": "STEP_READY", "job_digest": digest(canonical(job))}).decode(), end="", flush=True)
        signal.alarm(5)
        permit = sys.stdin.buffer.readline(16)
        signal.alarm(0)
        if permit != b"EXECUTE\n":
            raise BootstrapError("BOOTSTRAP_FRESH_PERMIT_REQUIRED", 403)
        result = functions[step]()
        print(canonical({"state": "STEP_COMPLETED", "step": step, "result": result}).decode(), end="", flush=True)
        return 0
    except (BootstrapError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # This stream is the owning private step evidence. Preserve the actual
        # failure at occurrence, never reconstruct missing historical stderr.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"state": "STEP_NOT_COMPLETED", "error": error.code if isinstance(error, BootstrapError) else "BOOTSTRAP_HOST_STEP_FAILED"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
