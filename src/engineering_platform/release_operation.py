"""Durable, fail-closed state for one Engineering Platform release.

This is deliberately a local operation journal, not a registry client or an
installer.  It binds the immutable source and artifact facts before a publisher
is called, makes a lost response resumable, and keeps ``PUBLISHED`` distinct
from post-publication cleanup and ``RELEASE_COMPLETE``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATES = ("PREPARED", "QUALIFIED", "PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE")
_ALLOWED = {
    "PREPARED": frozenset({"QUALIFIED"}),
    "QUALIFIED": frozenset({"PUBLISHED"}),
    "PUBLISHED": frozenset({"CLEANUP_PENDING", "RELEASE_COMPLETE"}),
    "CLEANUP_PENDING": frozenset({"RELEASE_COMPLETE"}),
    "RELEASE_COMPLETE": frozenset(),
}


class ReleaseOperationError(ValueError):
    """A release operation lacks immutable identity or a safe transition."""


@dataclass(frozen=True)
class ReleaseOperation:
    operation_id: str
    product: str
    component: str
    version: str
    policy_revision: str
    source_revision: str
    artifacts: Mapping[str, str]
    state: str = "PREPARED"
    qualification: Mapping[str, object] | None = None
    publication_receipt: Mapping[str, object] | None = None
    cleanup: Mapping[str, object] | None = None

    @classmethod
    def create(cls, *, operation_id: str, product: str, component: str,
               version: str, policy_revision: str, source_revision: str,
               artifacts: Mapping[str, str]) -> "ReleaseOperation":
        if not _OPERATION.fullmatch(operation_id):
            raise ReleaseOperationError("release operation ID is invalid")
        if product != "engineering-platform" or component != "server":
            raise ReleaseOperationError("release operation must identify the EP Server product")
        if not _SEMVER.fullmatch(version) or not _SHA.fullmatch(source_revision):
            raise ReleaseOperationError("release version or source revision is invalid")
        if not isinstance(policy_revision, str) or not policy_revision:
            raise ReleaseOperationError("release policy revision is invalid")
        if set(artifacts) != {"wheel", "sdist"} or not all(
            isinstance(value, str) and _SHA256.fullmatch(value) for value in artifacts.values()
        ):
            raise ReleaseOperationError("release artifacts must be exact wheel and sdist SHA-256 identities")
        return cls(operation_id, product, component, version, policy_revision, source_revision, dict(artifacts))

    @classmethod
    def parse(cls, value: object) -> "ReleaseOperation":
        if not isinstance(value, dict) or set(value) != {
            "operation_id", "product", "component", "version", "policy_revision", "source_revision",
            "artifacts", "state", "qualification", "publication_receipt", "cleanup",
        }:
            raise ReleaseOperationError("release operation record has unknown or missing fields")
        operation = cls.create(
            operation_id=value["operation_id"], product=value["product"], component=value["component"],
            version=value["version"], policy_revision=value["policy_revision"],
            source_revision=value["source_revision"], artifacts=value["artifacts"],
        )
        state = value["state"]
        if state not in _STATES:
            raise ReleaseOperationError("release operation state is invalid")
        for key in ("qualification", "publication_receipt", "cleanup"):
            if value[key] is not None and not isinstance(value[key], dict):
                raise ReleaseOperationError(f"release operation {key} is invalid")
        if state in {"QUALIFIED", "PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"} and value["qualification"] is None:
            raise ReleaseOperationError("release operation is missing qualification evidence")
        if state in {"PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"} and value["publication_receipt"] is None:
            raise ReleaseOperationError("published release operation is missing publication receipt")
        return cls(**{**asdict(operation), "state": state, "qualification": value["qualification"],
                      "publication_receipt": value["publication_receipt"], "cleanup": value["cleanup"]})

    def transition(self, state: str, *, evidence: Mapping[str, object]) -> "ReleaseOperation":
        if state not in _ALLOWED.get(self.state, frozenset()):
            raise ReleaseOperationError(f"release transition {self.state} -> {state} is not permitted")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ReleaseOperationError("release transition requires durable evidence")
        values = asdict(self)
        values["state"] = state
        if state == "QUALIFIED": values["qualification"] = dict(evidence)
        elif state == "PUBLISHED": values["publication_receipt"] = dict(evidence)
        else: values["cleanup"] = dict(evidence)
        return ReleaseOperation(**values)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class ReleaseOperationStore:
    """One-operation-at-a-time journal rooted in an EP-owned directory."""
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._lock_descriptor: int | None = None
        self._lock_owner: str | None = None

    def _path(self, operation_id: str) -> Path:
        if not _OPERATION.fullmatch(operation_id):
            raise ReleaseOperationError("release operation ID is invalid")
        return self.root / "operations" / f"{operation_id}.json"

    @property
    def _lock(self) -> Path:
        return self.root / "release-operation.lock"

    def acquire(self, operation_id: str) -> None:
        if not _OPERATION.fullmatch(operation_id):
            raise ReleaseOperationError("release operation ID is invalid")
        if self._lock_descriptor is not None:
            raise ReleaseOperationError("this release operation store already owns the installation lock")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self._lock, os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (FileExistsError, BlockingIOError) as error:
            raise ReleaseOperationError("another release operation owns the installation lock") from error
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (operation_id + "\n").encode("utf-8"))
            os.fsync(descriptor)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._lock_descriptor, self._lock_owner = descriptor, operation_id

    def release(self, operation_id: str) -> None:
        if self._lock_descriptor is None or self._lock_owner != operation_id:
            raise ReleaseOperationError("release operation does not own the installation lock")
        try:
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_descriptor)
            self._lock_descriptor, self._lock_owner = None, None

    def load(self, operation_id: str) -> ReleaseOperation | None:
        path = self._path(operation_id)
        if not path.exists(): return None
        try: return ReleaseOperation.parse(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise ReleaseOperationError("release operation record is unreadable") from error

    def save(self, operation: ReleaseOperation) -> ReleaseOperation:
        existing = self.load(operation.operation_id)
        if existing is not None and existing != operation:
            raise ReleaseOperationError("release operation record is immutable; save only an identical recovery record")
        _atomic_json(self._path(operation.operation_id), asdict(operation))
        return operation

    def replace(self, previous: ReleaseOperation, current: ReleaseOperation) -> ReleaseOperation:
        if current.operation_id != previous.operation_id or self.load(previous.operation_id) != previous:
            raise ReleaseOperationError("release operation changed before transition")
        _atomic_json(self._path(current.operation_id), asdict(current))
        return current

    def record_publication(self, operation: ReleaseOperation) -> None:
        if operation.state not in {"PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"}:
            raise ReleaseOperationError("only a published release may reserve its immutable identity")
        path = self.root / "published" / f"{operation.product}-{operation.component}-{operation.version}.json"
        identity = {"operation_id": operation.operation_id, "source_revision": operation.source_revision,
                    "artifacts": dict(operation.artifacts), "publication_receipt": operation.publication_receipt}
        if path.exists():
            try: existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error: raise ReleaseOperationError("published release identity is unreadable") from error
            if existing != identity:
                raise ReleaseOperationError("published release identity already exists with different bytes or provenance")
            return
        _atomic_json(path, identity)

    @staticmethod
    def artifact_digest(path: Path) -> str:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file(): raise ReleaseOperationError("release artifact is unavailable")
        return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
