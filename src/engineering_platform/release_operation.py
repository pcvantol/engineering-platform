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
import math
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


def _json_compatible(value: object) -> bool:
    """Reject evidence that cannot survive a deterministic JSON restart."""
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_compatible(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_json_compatible(item) for item in value)
    return False


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
            if value[key] is not None and (
                not isinstance(value[key], Mapping) or not value[key] or not _json_compatible(value[key])
            ):
                raise ReleaseOperationError(f"release operation {key} is invalid")
        if state in {"QUALIFIED", "PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"} and value["qualification"] is None:
            raise ReleaseOperationError("release operation is missing qualification evidence")
        if state in {"PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"} and value["publication_receipt"] is None:
            raise ReleaseOperationError("published release operation is missing publication receipt")
        if state in {"CLEANUP_PENDING", "RELEASE_COMPLETE"} and value["cleanup"] is None:
            raise ReleaseOperationError("post-publication release operation is missing cleanup evidence")
        return cls(**{**asdict(operation), "state": state, "qualification": value["qualification"],
                      "publication_receipt": value["publication_receipt"], "cleanup": value["cleanup"]})

    def transition(self, state: str, *, evidence: Mapping[str, object]) -> "ReleaseOperation":
        if state not in _ALLOWED.get(self.state, frozenset()):
            raise ReleaseOperationError(f"release transition {self.state} -> {state} is not permitted")
        if not isinstance(evidence, Mapping) or not evidence or not _json_compatible(evidence):
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
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
            raise ReleaseOperationError("this release operation store already owns the release lock")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self._lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise ReleaseOperationError("another release operation owns the release lock") from error
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
            raise ReleaseOperationError("release operation does not own the release lock")
        try:
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_descriptor)
            self._lock_descriptor, self._lock_owner = None, None

    def _require_lock(self, operation_id: str) -> None:
        if self._lock_descriptor is None or self._lock_owner != operation_id:
            raise ReleaseOperationError("release operation must own the release lock")

    def load(self, operation_id: str) -> ReleaseOperation | None:
        path = self._path(operation_id)
        if not path.exists(): return None
        try: return ReleaseOperation.parse(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise ReleaseOperationError("release operation record is unreadable") from error

    def save(self, operation: ReleaseOperation) -> ReleaseOperation:
        self._require_lock(operation.operation_id)
        existing = self.load(operation.operation_id)
        if existing is not None and existing != operation:
            raise ReleaseOperationError("release operation record is immutable; save only an identical recovery record")
        _atomic_json(self._path(operation.operation_id), asdict(operation))
        return operation

    @staticmethod
    def same_identity(left: ReleaseOperation, right: ReleaseOperation) -> bool:
        """Compare immutable release facts without accepting later-state drift."""
        return (
            left.operation_id, left.product, left.component, left.version,
            left.policy_revision, left.source_revision, dict(left.artifacts),
        ) == (
            right.operation_id, right.product, right.component, right.version,
            right.policy_revision, right.source_revision, dict(right.artifacts),
        )

    def prepare_qualified(self, operation: ReleaseOperation, *, evidence: Mapping[str, object]) -> ReleaseOperation:
        """Create or reopen one exact pre-publication operation.

        This boundary is intentionally usable before a registry side effect.
        A retry can only reuse the same operation when its source, policy and
        both artifact bytes are identical. Later persisted states are returned
        unchanged after their original qualification evidence is checked.
        """
        self._require_lock(operation.operation_id)
        existing = self.load(operation.operation_id)
        if existing is None:
            existing = self.save(operation)
        elif not self.same_identity(existing, operation):
            raise ReleaseOperationError("release operation ID already binds different immutable identity")
        if existing.state == "PREPARED":
            return self.replace(existing, existing.transition("QUALIFIED", evidence=evidence))
        if existing.state not in {"QUALIFIED", "PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"}:
            raise ReleaseOperationError("release operation cannot resume from its current state")
        if existing.qualification != dict(evidence):
            raise ReleaseOperationError("release operation qualification evidence changed during recovery")
        return existing

    def mark_published(self, operation: ReleaseOperation, *, evidence: Mapping[str, object]) -> ReleaseOperation:
        """Persist a verified registry readback without changing release identity."""
        self._require_lock(operation.operation_id)
        current = self.load(operation.operation_id)
        if current is None or not self.same_identity(current, operation):
            raise ReleaseOperationError("release operation does not bind the exact requested identity")
        if current.state == "QUALIFIED":
            current = self.replace(current, current.transition("PUBLISHED", evidence=evidence))
        elif current.state not in {"PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"}:
            raise ReleaseOperationError("release operation cannot be published from its current state")
        elif current.publication_receipt != dict(evidence):
            raise ReleaseOperationError("published release receipt changed during recovery")
        self.record_publication(current)
        return current

    def mark_cleanup_pending(self, operation: ReleaseOperation, *, evidence: Mapping[str, object]) -> ReleaseOperation:
        """Record an incomplete exact cleanup list for controlled resume."""
        self._require_lock(operation.operation_id)
        current = self.load(operation.operation_id)
        if current is None or not self.same_identity(current, operation):
            raise ReleaseOperationError("release operation does not bind the exact requested identity")
        if current.state == "PUBLISHED":
            return self.replace(current, current.transition("CLEANUP_PENDING", evidence=evidence))
        if current.state == "CLEANUP_PENDING" and current.cleanup == dict(evidence):
            return current
        raise ReleaseOperationError("release operation cannot record this cleanup-pending result")

    def complete(self, operation: ReleaseOperation, *, evidence: Mapping[str, object]) -> ReleaseOperation:
        """Terminalize only after all declared operation-local cleanup succeeds."""
        self._require_lock(operation.operation_id)
        current = self.load(operation.operation_id)
        if current is None or not self.same_identity(current, operation):
            raise ReleaseOperationError("release operation does not bind the exact requested identity")
        if current.state in {"PUBLISHED", "CLEANUP_PENDING"}:
            current = self.replace(current, current.transition("RELEASE_COMPLETE", evidence=evidence))
        elif current.state != "RELEASE_COMPLETE" or current.cleanup != dict(evidence):
            raise ReleaseOperationError("release operation cannot complete from its current state")
        self.record_publication(current)
        return current

    def replace(self, previous: ReleaseOperation, current: ReleaseOperation) -> ReleaseOperation:
        self._require_lock(previous.operation_id)
        if not self.same_identity(previous, current) or self.load(previous.operation_id) != previous:
            raise ReleaseOperationError("release operation changed before transition")
        _atomic_json(self._path(current.operation_id), asdict(current))
        return current

    def record_publication(self, operation: ReleaseOperation) -> None:
        self._require_lock(operation.operation_id)
        if operation.state not in {"PUBLISHED", "CLEANUP_PENDING", "RELEASE_COMPLETE"}:
            raise ReleaseOperationError("only a published release may reserve its immutable identity")
        path = self.root / "published" / f"{operation.product}-{operation.component}-{operation.version}.json"
        identity = {"operation_id": operation.operation_id, "policy_revision": operation.policy_revision,
                    "source_revision": operation.source_revision,
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
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
