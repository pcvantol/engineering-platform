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
from . import genesis_git_runtime


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
        expected_directories = scope.get("prepared_directory_identities", {})
        contents = inventory(prepared, deadline=time.monotonic()+3, maximum=2048)
        observed_directories = {name+"/": list(info[:2]) for name, info in contents.items() if stat.S_ISDIR(info[2])}
        if (list(identity(os.fstat(prepared))) != scope["staging_identity"]
                or list(identity(os.fstat(source))) != scope["staging_parent_identity"]
                or len(contents) != len(observed_directories) or observed_directories != expected_directories):
            raise BootstrapError("BOOTSTRAP_PREPARED_CAPABILITY_CHANGED", 409)
        if list(identity(os.fstat(parent))) != scope["root_identity"]:
            raise BootstrapError("BOOTSTRAP_PARENT_CHANGED", 409)
        if os.fstat(prepared).st_dev != os.fstat(parent).st_dev or sys.platform not in {"darwin", "linux"}:
            raise BootstrapError("BOOTSTRAP_ATOMIC_PUBLICATION_UNSUPPORTED", 422)
        # Native no-replace rename publishes the already owner-provisioned
        # inode. mkdir does not return an inode and cannot establish this proof.
        # No copy/delete, replacement, or exists-check fallback is permitted.
        library = ctypes.CDLL(None, use_errno=True)
        name, flags = ("renameatx_np", 0x4) if sys.platform == "darwin" else ("renameat2", 0x1)
        rename = getattr(library, name, None)
        if rename is None:
            raise BootstrapError("BOOTSTRAP_ATOMIC_PUBLICATION_UNSUPPORTED", 422)
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(source, os.fsencode(staging.name), parent, os.fsencode(scope["target_name"]), flags):
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
            return {"target_identity": list(identity(info)), "directory_identities": observed_directories}
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
        contents = inventory(root, deadline=time.monotonic()+3, maximum=2048)
        directories = known["directory_identities"]
        if ({name+"/": list(info[:2]) for name, info in contents.items() if stat.S_ISDIR(info[2])} != directories
                or any(not stat.S_ISDIR(info[2]) for info in contents.values())):
            raise BootstrapError("BOOTSTRAP_PARTIAL_FILES_OWNERSHIP_UNCERTAIN", 409)
        observations = {}
        for name, raw in sorted(files.items()):
            fd = os.dup(root)
            prefix = ""
            try:
                parts = name.split("/")
                for part in parts[:-1]:
                    prefix += part + "/"
                    if prefix not in directories:
                        raise BootstrapError("BOOTSTRAP_DIRECTORY_OWNERSHIP_UNCERTAIN", 409)
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


def _git_executable(scope: dict, known: dict) -> str:
    if sys.platform != 'darwin':
        if 'git_runtime' in known:
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_MODE_UNSUPPORTED', 422)
        return 'git'
    return genesis_git_runtime.verify(known.get('git_runtime'),
        operation_id=scope['operation_id'], host_id=scope['host_id'])


def _private_git_output(stdout: bytes, stderr: bytes) -> None:
    # These are original received bytes, confined to the parent's private
    # bounded stderr evidence. Never put them in the JSON receipt protocol.
    failures = []
    for label, raw, maximum in ((b'STDOUT', stdout, 262144), (b'STDERR', stderr, 65536)):
        if len(raw) > maximum:
            failure = BootstrapError('BOOTSTRAP_GIT_OUTPUT_LIMIT' if label == b'STDOUT'
                else 'BOOTSTRAP_GIT_ERROR_OUTPUT_LIMIT', 409)
            failure.add_note(f'Original {label.decode()} bytes={len(raw)}, retained={maximum}')
            failures.append(failure)
        if raw:
            try:
                sys.stderr.buffer.write(b'\nGENESIS_GIT_'+label+b'\n'+raw[:maximum])
            except (OSError, ValueError) as error:
                failures.append(error)
    if failures:
        raise ExceptionGroup('Original private Git output could not be preserved', failures)


def _run_git(command, metadata: int, input_bytes: bytes | None = None):
    try:
        result = subprocess.run(command, preexec_fn=lambda: os.fchdir(metadata), pass_fds=(metadata,),
            env=child_environment(), input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, check=False)
    except subprocess.TimeoutExpired as error:
        try:
            _private_git_output(error.stdout or b'', error.stderr or b'')
        except ExceptionGroup as evidence_error:
            raise error from evidence_error
        raise
    primary = None
    if result.returncode:
        primary = BootstrapError('BOOTSTRAP_LOCAL_GIT_FAILED', 409)
    elif len(result.stdout) > 262144:
        primary = BootstrapError('BOOTSTRAP_GIT_OUTPUT_LIMIT', 409)
    elif len(result.stderr) > 65536:
        primary = BootstrapError('BOOTSTRAP_GIT_ERROR_OUTPUT_LIMIT', 409)
    original_status = f'Original Git exit={result.returncode}, stdout_bytes={len(result.stdout)}, stderr_bytes={len(result.stderr)}'
    if primary is not None:
        primary.add_note(original_status)
    try:
        _private_git_output(result.stdout, result.stderr)
    except ExceptionGroup as evidence_error:
        if primary is not None:
            raise primary from evidence_error
        evidence_error.add_note(original_status)
        raise
    if primary is not None:
        raise primary
    return result.stdout


def preflight(scope: dict, runtime) -> dict:
    result = {'native_profile_started': True, 'business_effects': 0}
    if sys.platform != 'darwin':
        return result
    known = {'git_runtime': runtime}
    executable = _git_executable(scope, known)
    # Existing installed package read capability; the target is still absent.
    # No additional native directory grants or filesystem writes are needed.
    with directory(Path(__file__).resolve().parent) as metadata:
        raw = _run_git((executable, '--version'), metadata)
    _git_executable(scope, known)
    if not raw.startswith(b'git version ') or len(raw.splitlines()) != 1:
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_RECEIPT_INVALID', 409)
    return {**result, 'git_runtime': runtime, 'git_version_sha256': digest(raw)}


def _git(root: Path, *args: str, known: dict, scope: dict, input_bytes: bytes | None = None,
         initializing_files: dict | None = None) -> bytes:
    command = (_git_executable(scope, known), "--git-dir=.", "--work-tree="+str(root), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", "-c", "protocol.allow=never",
        "-c", "diff.autoRefreshIndex=false", "-c", "gc.auto=0", "-c", "gc.autoPackLimit=0",
        "-c", "maintenance.auto=false", *args)
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
            # Recheck after the held target/metadata identities and immediately
            # before execution. Same-owner hostile binary swaps are outside
            # the explicitly qualified trusted-host boundary.
            _git_executable(scope, known)
            if initializing_files is not None:
                verify_files(scope, known, initializing_files, git=True)
                expected = scope['git_staging_identity']
                if (list(identity(os.fstat(metadata))) != expected or os.listdir(metadata)
                        or list(identity(os.stat('.git', dir_fd=parent, follow_symlinks=False))) != expected):
                    raise BootstrapError('BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN', 409)
            raw = _run_git(command, metadata, input_bytes)
        finally:
            os.close(metadata)
    _git_executable(scope, known)
    return raw


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


def git_initialize(scope: dict, known: dict, files: dict, *, prepared_parent_fd: int | None = None) -> dict:
    if known.get("git_identity"):
        _git_guard(scope, known)
        return {k: known[k] for k in ("git_identity", "git_config_digest")}
    recovered_empty = os.path.lexists(_git_root(scope)/'.git')
    if recovered_empty:
        # A failed original init may have published its granted empty inode
        # before the tool shim timed out. This is not generic Git adoption:
        # only that exact capability, still empty, can enter fixed Git init.
        verify_files(scope, known, files, git=True)
        with target(scope, known) as parent:
            metadata = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                expected = scope['git_staging_identity']
                if (list(identity(os.fstat(metadata))) != expected or os.listdir(metadata)
                        or list(identity(os.stat('.git', dir_fd=parent, follow_symlinks=False))) != expected):
                    raise BootstrapError('BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN', 409)
                verify_files(scope, known, files, git=True)
                if os.listdir(metadata):
                    raise BootstrapError('BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN', 409)
                claim = {'target_identity': expected}
            finally:
                os.close(metadata)
    else:
        verify_files(scope, known, files, git=False)
        metadata_scope = {**scope, "workspace_root": str(_git_root(scope)), "root_identity": known["target_identity"],
            "target_name": ".git", "staging_path": scope["git_staging_path"], "staging_identity": scope["git_staging_identity"],
            "prepared_directory_identities": {}}
        claim = create(metadata_scope, prepared_parent_fd=prepared_parent_fd)
    initializing = {**known, "git_identity": claim["target_identity"]}
    _git(_git_root(scope), "-c", "init.templateDir=", "init", "--initial-branch=main", known=initializing,
        scope=scope, initializing_files=files)
    with target(scope, known) as fd:
        gitfd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if list(identity(os.fstat(gitfd))) != claim["target_identity"]:
                raise BootstrapError("BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN", 409)
            _, config_digest = selected_bytes(gitfd, "config", maximum=65536, deadline=time.monotonic()+2)
            if identity(os.stat(".git", dir_fd=fd, follow_symlinks=False)) != identity(os.fstat(gitfd)):
                raise BootstrapError("BOOTSTRAP_GIT_OWNERSHIP_UNCERTAIN", 409)
            return {"git_identity": claim["target_identity"], "git_config_digest": config_digest,
                'recovered_original_empty_git_capability': recovered_empty}
        finally:
            os.close(gitfd)


def prepare_commit(scope: dict, known: dict, files: dict, accepted_at: int) -> dict:
    _git_guard(scope, known)
    verify_files(scope, known, files, git=True)
    root = _git_root(scope)
    # Object/index plumbing consumes only the admitted bytes. `git add` can
    # execute a target-configured clean filter after an earlier file guard.
    hierarchy, blob_commands, expected_blobs = {}, [], []
    for mark, (name, raw) in enumerate(sorted(files.items()), 1):
        node = hierarchy
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        # Git's object-format identity is compatibility data. Authorization
        # and exact original document integrity use independent SHA256/HMAC.
        expected = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
        node[parts[-1]] = expected
        expected_blobs.append(expected)
        blob_commands.append(b"blob\nmark :"+str(mark).encode()+b"\ndata "+str(len(raw)).encode()+b"\n"+raw+b"\nget-mark :"+str(mark).encode()+b"\n")
    actual_blobs = _git(root, "fast-import", "--quiet", "--done", known=known, scope=scope,
        input_bytes=b"".join(blob_commands)+b"done\n").decode().splitlines()
    if actual_blobs != expected_blobs:
        raise BootstrapError("BOOTSTRAP_BLOB_CONFLICT", 409)
    tree_commands, expected_trees = [], []
    def plan_tree(node):
        entries, raw_entries = [], []
        for name, value in sorted(node.items(), key=lambda item: item[0]+("/" if isinstance(item[1], dict) else "\0")):
            if isinstance(value, dict):
                value = plan_tree(value)
                mode, kind = b"40000", b"tree"
            else:
                mode, kind = b"100644", b"blob"
            entries.append(mode+b" "+kind+b" "+value.encode()+b"\t"+name.encode()+b"\0")
            raw_entries.append(mode+b" "+name.encode()+b"\0"+bytes.fromhex(value))
        raw = b"".join(raw_entries)
        object_id = hashlib.sha1(b"tree "+str(len(raw)).encode()+b"\0"+raw, usedforsecurity=False).hexdigest()
        tree_commands.append(b"".join(entries)+b"\0")
        expected_trees.append(object_id)
        return object_id
    tree = plan_tree(hierarchy)
    actual_trees = _git(root, "mktree", "--batch", "-z", known=known, scope=scope, input_bytes=b"".join(tree_commands)).decode().splitlines()
    if actual_trees != expected_trees:
        raise BootstrapError("BOOTSTRAP_TREE_CONFLICT", 409)
    _git(root, "read-tree", tree, known=known, scope=scope)
    declaration = b"tree " + tree.encode() + b"\nauthor Engineering Platform Bootstrap <bootstrap@localhost> "
    author = str(accepted_at).encode() + b" +0000"
    raw = declaration + author + b"\ncommitter Engineering Platform Bootstrap <bootstrap@localhost> " + author
    raw += b"\n\nCREATE_GENESIS_LOCAL " + scope["operation_id"].encode() + b"\n"
    commit = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
    return {"tree": tree, "commit": commit, "commit_utf8": raw.decode()}


def commit_object(scope: dict, known: dict) -> dict:
    _git_guard(scope, known)
    raw = known["commit_utf8"].encode()
    root = _git_root(scope)
    object_path = root / ".git/objects" / known["commit"][:2] / known["commit"][2:]
    if os.path.lexists(object_path):
        if _git(root, "cat-file", "commit", known["commit"], known=known, scope=scope) != raw:
            raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
        return {"commit_object": known["commit"], "recovered_existing_object": True}
    actual = _git(root, "hash-object", "-t", "commit", "-w", "--stdin", known=known, scope=scope, input_bytes=raw).decode().strip()
    if actual != known["commit"]:
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    return {"commit_object": actual}


def commit_ref(scope: dict, known: dict) -> dict:
    _git_guard(scope, known)
    root = _git_root(scope)
    raw = _git(root, "cat-file", "commit", known["commit"], known=known, scope=scope)
    if raw != known["commit_utf8"].encode():
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    reference = root / ".git/refs/heads/main"
    if os.path.lexists(reference):
        if _git(root, "rev-parse", "refs/heads/main", known=known, scope=scope).decode().strip() != known["commit"]:
            raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
        return {"commit_ref": known["commit"], "recovered_existing_ref": True}
    # Creation-only CAS; an unrelated concurrent ref is never overwritten.
    _git(root, "update-ref", "refs/heads/main", known["commit"], "0"*40, known=known, scope=scope)
    return {"commit_ref": known["commit"]}


def verify_commit(scope: dict, known: dict, files: dict) -> dict:
    _git_guard(scope, known)
    verify_files(scope, known, files, git=True)
    before = git_snapshot(scope, known)
    root = _git_root(scope)
    if _git(root, "rev-parse", "HEAD", known=known, scope=scope).decode().strip() != known["commit"]:
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    if _git(root, "cat-file", "commit", known["commit"], known=known, scope=scope) != known["commit_utf8"].encode():
        raise BootstrapError("BOOTSTRAP_COMMIT_CONFLICT", 409)
    rows = _git(root, "ls-tree", "-rz", "--full-tree", known["commit"], known=known, scope=scope).split(b"\0")
    observed = {}
    for row in filter(None, rows):
        info, name = row.split(b"\t", 1)
        mode, kind, object_id = info.decode("ascii").split()
        name = name.decode("utf-8")
        if mode != "100644" or kind != "blob" or name not in files:
            raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
        observed[name] = object_id
    if set(observed) != set(files) or _git(root, "rev-parse", "HEAD^{tree}", known=known, scope=scope).decode().strip() != known["tree"]:
        raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
    batch = _git(root, "cat-file", "--batch", known=known, scope=scope,
        input_bytes=b"".join(observed[name].encode()+b"\n" for name in sorted(files)))
    for name in sorted(files):
        header, batch = batch.split(b"\n", 1)
        if header != observed[name].encode()+b" blob "+str(len(files[name])).encode():
            raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
        size = len(files[name])
        if batch[:size] != files[name] or batch[size:size+1] != b"\n":
            raise BootstrapError("BOOTSTRAP_COMMITTED_TREE_CONFLICT", 409)
        batch = batch[size+1:]
        observed[name] = digest(files[name])
    if batch or _git(root, "diff-index", "--cached", "--name-only", known["commit"], known=known, scope=scope):
        raise BootstrapError("BOOTSTRAP_COMMITTED_INDEX_CONFLICT", 409)
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
            "PREFLIGHT": lambda: preflight(scope, job.get('git_runtime')),
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
    except (BootstrapError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ExceptionGroup) as error:
        # This stream is the owning private step evidence. Preserve the actual
        # failure at occurrence, never reconstruct missing historical stderr.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"state": "STEP_NOT_COMPLETED", "error": error.code if isinstance(error, BootstrapError) else "BOOTSTRAP_HOST_STEP_FAILED"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
