"""Bounded EP execution of a product-owned version preparation operation.

The product helper owns version rules; this module owns only admission,
isolated candidate construction and allow-listed diff verification.  It never
infers a release, runs arbitrary commands, or grants merge authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from .execution_errors import RunnerError
from .providers import GitProvider
from .execution_repository import GitHubClient

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_PATH = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPOSITORY_PATH = re.compile(r"^(?:[A-Za-z0-9.][A-Za-z0-9._-]*/)*[A-Za-z0-9.][A-Za-z0-9._-]*$")
_REQUEST_KEYS = frozenset({"contract_version", "operation_id", "product_id", "component_id", "repository_id", "policy_revision", "policy_digest", "source_event_set", "source_event_policy", "expected_source_revision", "expected_target_branch_revision", "expected_version", "requested_change", "determined_target_version", "allowed_projection_paths", "prepared_operation_digest", "authorization_reference", "delivery_mode"})
_HELPER_KEYS = frozenset({"contract_version", "product_id", "repository_id", "helper_path", "receipt_directory", "allowed_projection_paths", "policy_revision"})


class VersionPreparationError(RunnerError):
    pass


@dataclass(frozen=True)
class ProductHelperDeclaration:
    product_id: str
    repository_id: str
    helper_path: str
    receipt_directory: str
    allowed_projection_paths: tuple[str, ...]
    policy_revision: str

    @classmethod
    def load(cls, worktree: Path) -> "ProductHelperDeclaration":
        try: value = json.loads((worktree / ".version-preparation.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error: raise VersionPreparationError("product version helper declaration is unreadable") from error
        if not isinstance(value, dict) or set(value) != _HELPER_KEYS or value.get("contract_version") != "1":
            raise VersionPreparationError("product version helper declaration has unknown fields or schema")
        fields = ("product_id", "repository_id", "helper_path", "policy_revision")
        receipt_directory = value.get("receipt_directory")
        if (not all(isinstance(value.get(key), str) and _PATH.fullmatch(value[key]) for key in fields)
                or not isinstance(receipt_directory, str) or not _REPOSITORY_PATH.fullmatch(receipt_directory)
                or any(part in {".", ".."} for part in receipt_directory.split("/"))):
            raise VersionPreparationError("product version helper declaration has invalid paths or identities")
        paths = value.get("allowed_projection_paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and _PATH.fullmatch(path) for path in paths):
            raise VersionPreparationError("product version helper declaration has invalid projection paths")
        return cls(value["product_id"], value["repository_id"], value["helper_path"], receipt_directory, tuple(paths), value["policy_revision"])


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
        if request.expected_target_branch_revision is not None and not _SHA.fullmatch(request.expected_target_branch_revision):
            raise VersionPreparationError("expected target branch revision must be an exact SHA")
        if not _SEMVER.fullmatch(request.expected_version) or not _SEMVER.fullmatch(request.determined_target_version):
            raise VersionPreparationError("expected and determined versions must be stable SemVer")
        if not _SHA256.fullmatch(request.policy_digest) or not _SHA256.fullmatch(request.prepared_operation_digest):
            raise VersionPreparationError("policy and prepared operation digests must be SHA-256 identities")
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

    def _changed_paths(self, worktree: Path) -> tuple[str, ...]:
        """Return every changed path, including the helper's new receipt.

        ``git diff --name-only`` alone omits untracked receipts, which would
        let a candidate commit omit its operation evidence.  NUL-delimited
        Git path inventories avoid whitespace/quote interpretation entirely.
        """
        tracked = self.git.command(worktree, "git", "diff", "--no-renames", "--name-only", "-z", "HEAD")
        untracked = self.git.command(worktree, "git", "ls-files", "--others", "--exclude-standard", "-z")
        paths = [path for path in (tracked + untracked).split("\0") if path]
        for path in paths:
            if not _REPOSITORY_PATH.fullmatch(path) or any(part in {".", ".."} for part in path.split("/")):
                raise VersionPreparationError("version preparation has an invalid changed path")
        if len(set(paths)) != len(paths):
            raise VersionPreparationError("version preparation has duplicate changed paths")
        return tuple(sorted(paths))

    def prepare(self, repository: Path, worktree: Path, request: VersionPreparationRequest) -> dict[str, object]:
        head = self.git.command(repository, "git", "rev-parse", "HEAD")
        if head != request.expected_source_revision:
            raise VersionPreparationError("stale expected source revision")
        status = self.git.command(repository, "git", "status", "--porcelain", "--untracked-files=all")
        if status:
            raise VersionPreparationError("repository checkout is not clean")
        if self._changed_paths(worktree):
            raise VersionPreparationError("isolated version preparation worktree is not clean")
        declaration = ProductHelperDeclaration.load(worktree)
        if (declaration.product_id != request.product_id or declaration.repository_id != request.repository_id
                or declaration.policy_revision != request.policy_revision
                or tuple(request.allowed_projection_paths) != declaration.allowed_projection_paths):
            raise VersionPreparationError("product helper declaration does not bind the admitted operation")
        self.helper.apply(worktree, request)
        changed = self._changed_paths(worktree)
        receipt = f"{declaration.receipt_directory}/{request.operation_id}.json"
        allowed = set(request.allowed_projection_paths) | {receipt}
        if receipt not in changed or set(changed) - allowed:
            raise VersionPreparationError("version preparation changed a path outside its declared operation")
        digest = "sha256:" + hashlib.sha256("\n".join(changed).encode()).hexdigest()
        if digest != request.prepared_operation_digest:
            raise VersionPreparationError("prepared operation digest does not bind the candidate diff")
        return {"operation_id": request.operation_id, "changed_paths": changed, "prepared_operation_digest": digest}

    @staticmethod
    def branch_name(request: VersionPreparationRequest) -> str:
        return f"ep/version-preparation/{request.operation_id}"

    def create_isolated_worktree(self, repository: Path, worktree: Path, request: VersionPreparationRequest) -> None:
        """Create the one deterministic candidate worktree from the pinned source."""
        if worktree.exists():
            raise VersionPreparationError("version preparation worktree path already exists")
        if self.git.command(repository, "git", "rev-parse", request.expected_source_revision) != request.expected_source_revision:
            raise VersionPreparationError("expected source revision is unavailable")
        self.git.command(repository, "git", "worktree", "add", "-b", self.branch_name(request), str(worktree), request.expected_source_revision)
        if self.git.command(worktree, "git", "rev-parse", "HEAD") != request.expected_source_revision:
            raise VersionPreparationError("isolated worktree did not start at the expected source revision")
        if self.git.command(worktree, "git", "status", "--porcelain", "--untracked-files=all"):
            raise VersionPreparationError("isolated version preparation worktree is not clean")

    def publish_candidate(
        self, worktree: Path, request: VersionPreparationRequest, prepared: Mapping[str, object], github: GitHubClient,
        *, base_branch: str,
    ) -> dict[str, object]:
        """Commit only the verified candidate diff and create/recover one PR."""
        branch = self.branch_name(request)
        paths = prepared.get("changed_paths")
        if not isinstance(paths, tuple) or not paths or not all(isinstance(path, str) for path in paths):
            raise VersionPreparationError("prepared candidate has no bounded changed paths")
        if self.git.command(worktree, "git", "branch", "--show-current") != branch:
            raise VersionPreparationError("isolated worktree branch does not bind the operation ID")
        if request.expected_target_branch_revision is not None:
            target = self.git.command(worktree, "git", "rev-parse", f"origin/{base_branch}")
            if target != request.expected_target_branch_revision:
                raise VersionPreparationError("target branch revision changed before candidate publication")
        self.git.command(worktree, "git", "add", "--", *paths)
        self.git.command(worktree, "git", "commit", "-m", f"build: prepare version operation {request.operation_id}")
        candidate_sha = self.git.command(worktree, "git", "rev-parse", "HEAD")
        candidate_tree_sha = self.git.command(worktree, "git", "rev-parse", "HEAD^{tree}")
        if not _SHA.fullmatch(candidate_tree_sha):
            raise VersionPreparationError("candidate tree identity is unavailable")
        self.git.command(worktree, "git", "push", "origin", f"HEAD:{branch}")
        body = "\n".join((
            "Bounded EP version-preparation candidate.",
            f"operation_id: `{request.operation_id}`",
            f"prepared_operation_digest: `{request.prepared_operation_digest}`",
            f"expected_source_revision: `{request.expected_source_revision}`",
        ))
        pr = github.create_or_recover_pull_request(branch, base_branch, f"build: prepare version {request.determined_target_version}", body)
        if pr.head_branch != branch:
            raise VersionPreparationError("recovered pull request does not bind the candidate branch")
        if pr.head_sha != candidate_sha:
            raise VersionPreparationError("pull request head changed after version candidate publication")
        return {
            **prepared,
            "candidate_commit_sha": candidate_sha,
            "candidate_tree_sha": candidate_tree_sha,
            "branch": branch,
            "pull_request_id": pr.number,
            "pull_request_head_sha": pr.head_sha,
            "authorization_reference": request.authorization_reference,
            "delivery_mode": request.delivery_mode,
        }

    @staticmethod
    def qualify_candidate(candidate: Mapping[str, object], github: GitHubClient) -> dict[str, object]:
        """Bind read-only repository qualification to the candidate's exact SHA."""
        sha, number = candidate.get("candidate_commit_sha"), candidate.get("pull_request_id")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not isinstance(number, int):
            raise VersionPreparationError("candidate lacks exact SHA/PR binding")
        evidence = github.qualification_for_exact_head(number, sha)
        if evidence.get("exact_qualified_sha") != sha or evidence.get("conclusion") != "PASS":
            raise VersionPreparationError("candidate qualification is not bound to the exact candidate SHA")
        return dict(evidence)

    @staticmethod
    def record_delivery_evidence(evidence_root: Path, candidate: Mapping[str, object], qualification: Mapping[str, object]) -> Path:
        """Append immutable delivery evidence outside the tracked prepared receipt."""
        operation, sha, tree, branch, pr, digest, authorization = (
            candidate.get(key) for key in (
                "operation_id", "candidate_commit_sha", "candidate_tree_sha", "branch", "pull_request_id",
                "prepared_operation_digest", "authorization_reference",
            )
        )
        if (not all(isinstance(value, str) and value for value in (operation, sha, tree, branch, digest, authorization))
                or not _SHA.fullmatch(sha) or not _SHA.fullmatch(tree) or not isinstance(pr, int)):
            raise VersionPreparationError("candidate is incomplete for delivery evidence")
        if qualification.get("exact_qualified_sha") != sha or qualification.get("conclusion") != "PASS":
            raise VersionPreparationError("delivery evidence requires exact successful qualification")
        payload = {
            "schema_version": 1,
            "operation_id": operation,
            "prepared_operation_digest": digest,
            "candidate_commit_sha": sha,
            "candidate_tree_sha": tree,
            "branch": branch,
            "pull_request_id": pr,
            "pull_request_head_sha": candidate.get("pull_request_head_sha"),
            "qualification": dict(qualification),
            "authorization_reference": authorization,
            # This adapter never merges.  A later authorized delivery route
            # may append separate merge evidence; it cannot relabel this as a
            # completed protected delivery.
            "delivery": {"state": "PENDING_PROTECTED_MERGE"},
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        directory = evidence_root / "version-preparation-delivery"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{operation}-{sha}.json"
        if target.exists():
            if target.read_bytes() != encoded:
                raise VersionPreparationError("delivery evidence identity conflicts with existing bytes")
            return target
        temporary = target.with_suffix(".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, target)
        except FileExistsError as error:
            raise VersionPreparationError("delivery evidence write collided") from error
        return target

    def execute(
        self, repository: Path, worktree: Path, request: VersionPreparationRequest, github: GitHubClient,
        *, base_branch: str, evidence_root: Path,
    ) -> dict[str, object]:
        """Run one bounded preparation transaction; never merge or publish a release."""
        prepared = self.prepare(repository, worktree, request)
        candidate = self.publish_candidate(worktree, request, prepared, github, base_branch=base_branch)
        qualification = self.qualify_candidate(candidate, github)
        evidence = self.record_delivery_evidence(evidence_root, candidate, qualification)
        return {**candidate, "qualification": qualification, "delivery_evidence_path": str(evidence)}
