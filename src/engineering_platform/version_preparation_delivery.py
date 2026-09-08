"""Bounded EP execution of a product-owned version preparation operation.

The product helper owns version rules; this module owns only admission,
isolated candidate construction and allow-listed diff verification.  It never
infers a release, runs arbitrary commands, or grants merge authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Protocol

from .execution_errors import RunnerError
from .providers import GitProvider, ProcessProvider

_SHA = re.compile(r"^[0-9a-f]{40}$")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_PATH = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUEST_KEYS = frozenset({"contract_version", "operation_id", "product_id", "component_id", "repository_id", "policy_revision", "policy_digest", "source_event_set", "source_event_policy", "expected_source_revision", "expected_target_branch_revision", "expected_version", "requested_change", "determined_target_version", "allowed_projection_paths", "prepared_operation_digest", "authorization_reference", "delivery_mode"})


class VersionPreparationError(RunnerError):
    pass


@dataclass(frozen=True)
class VersionPreparationRequest:
    contract_version: str
    operation_id: str
    product_id: str
    component_id: str | None
    repository_id: str
    policy_revision: str
    policy_digest: str
    source_event_set: tuple[str, ...]
    source_event_policy: str
    expected_source_revision: str
    expected_target_branch_revision: str | None
    expected_version: str
    requested_change: str
    determined_target_version: str
    allowed_projection_paths: tuple[str, ...]
    prepared_operation_digest: str
    authorization_reference: str
    delivery_mode: str

    @classmethod
    def parse(cls, value: object) -> "VersionPreparationRequest":
        if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
            raise VersionPreparationError("version preparation request has unknown or missing fields")
        def text(key: str, optional: bool = False) -> str | None:
            item = value[key]
            if optional and item is None: return None
            if not isinstance(item, str) or not item or len(item) > 512: raise VersionPreparationError(f"invalid {key}")
            return item
        events = value["source_event_set"]
        paths = value["allowed_projection_paths"]
        if (not isinstance(events, list) or not events or len(set(events)) != len(events)
                or not all(isinstance(item, str) and item for item in events)):
            raise VersionPreparationError("source event set is invalid")
        if (not isinstance(paths, list) or not paths or len(set(paths)) != len(paths)
                or not all(isinstance(item, str) and _PATH.fullmatch(item) for item in paths)):
            raise VersionPreparationError("allowed projection paths are invalid")
        request = cls(*(text(key, key in {"component_id", "expected_target_branch_revision"}) for key in (
            "contract_version", "operation_id", "product_id", "component_id", "repository_id", "policy_revision", "policy_digest")),
            tuple(events), text("source_event_policy"), text("expected_source_revision"),
            text("expected_target_branch_revision", True), text("expected_version"), text("requested_change"),
            text("determined_target_version"), tuple(paths), text("prepared_operation_digest"),
            text("authorization_reference"), text("delivery_mode"))
        if request.contract_version != "1" or not _OPERATION.fullmatch(request.operation_id):
            raise VersionPreparationError("unsupported contract version or operation ID")
        if not _SHA.fullmatch(request.expected_source_revision):
            raise VersionPreparationError("expected source revision must be an exact SHA")
        if request.delivery_mode not in {"EXISTING_FEATURE_CANDIDATE", "PROTECTED_VERSION_PREPARATION_CANDIDATE"}:
            raise VersionPreparationError("unsupported delivery mode")
        if request.requested_change not in {"patch", "minor", "exact-version"}:
            raise VersionPreparationError("unsupported requested change")
        return request


class ProductHelper(Protocol):
    def apply(self, worktree: Path, request: VersionPreparationRequest) -> None: ...


class VersionPreparationDelivery:
    """Deterministic candidate builder; publication/merge remain provider authority."""
    def __init__(self, git: GitProvider, helper: ProductHelper) -> None:
        self.git, self.helper = git, helper

    def prepare(self, repository: Path, worktree: Path, request: VersionPreparationRequest) -> dict[str, object]:
        head = self.git.command(repository, "git", "rev-parse", "HEAD")
        if head != request.expected_source_revision:
            raise VersionPreparationError("stale expected source revision")
        status = self.git.command(repository, "git", "status", "--porcelain", "--untracked-files=all")
        if status:
            raise VersionPreparationError("repository checkout is not clean")
        self.helper.apply(worktree, request)
        changed = tuple(filter(None, self.git.command(worktree, "git", "diff", "--name-only").splitlines()))
        receipt = tuple(path for path in changed if path.endswith(f"/{request.operation_id}.json"))
        allowed = set(request.allowed_projection_paths) | set(receipt)
        if not receipt or set(changed) - allowed:
            raise VersionPreparationError("version preparation changed a path outside its declared operation")
        digest = hashlib.sha256("\n".join(changed).encode()).hexdigest()
        if digest != request.prepared_operation_digest:
            raise VersionPreparationError("prepared operation digest does not bind the candidate diff")
        return {"operation_id": request.operation_id, "changed_paths": changed, "prepared_operation_digest": digest}
