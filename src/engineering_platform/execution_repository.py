"""Repository and GitHub coordination behind canonical providers."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Protocol

from .execution_errors import RunnerError
from .execution_models import PullRequestEvidence, RepositoryEvidence
from .providers import GitProvider, GitHubProvider


GIT_SYNC_LOCK_RETRY_ATTEMPTS = 3
GIT_SYNC_LOCK_RETRY_DELAY_SECONDS = 0.5
_TRANSIENT_INDEX_LOCK_CONFLICT = re.compile(
    r"(?:index\.lock.*(?:file exists|already exists)|another git process)",
    re.IGNORECASE,
)


def github_repository_slug(remote: str) -> str:
    """Return the canonical owner/repository slug for GitHub origin forms."""
    value = remote.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    if value.startswith("https://github.com/"):
        return value.removeprefix("https://github.com/")
    return value


class RepositoryClient(Protocol):
    def inspect(self, root: Path) -> RepositoryEvidence: ...
    def main_contains(self, root: Path, sha: str) -> bool: ...
    def refresh_main_reference(self, root: Path) -> None: ...
    def remote_main_contains(self, root: Path, sha: str) -> bool: ...
    def synchronize_main(self, root: Path) -> None: ...


class GitHubClient(Protocol):
    def pull_request(self, number: int) -> PullRequestEvidence: ...
    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None: ...
    def ready(self, number: int) -> None: ...
    def normalize_markdown_body(self, number: int) -> bool: ...
    def merge(self, number: int) -> None: ...
    def create_or_recover_pull_request(self, branch: str, base: str, title: str, body: str) -> PullRequestEvidence: ...
    def qualification_for_exact_head(self, number: int, head_sha: str) -> dict[str, object]: ...
    def version_preparation_writer(self) -> dict[str, object]: ...


class SubprocessRepositoryClient:
    """Repository workflow implementation; process ownership remains GitProvider."""
    def __init__(self, provider: GitProvider | None = None) -> None:
        self.provider = provider or GitProvider()

    def _run(self, root: Path, *args: str) -> str:
        try: return self.provider.command(root, *args)
        except RuntimeError as error: raise RunnerError(str(error)) from error

    def inspect(self, root: Path) -> RepositoryEvidence:
        if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
            raise RunnerError("this is not a repository with canonical BOOTSTRAP.md")
        remote = self._run(root, "git", "remote", "get-url", "origin")
        repository = github_repository_slug(remote)
        branch, head_sha = self._run(root, "git", "branch", "--show-current"), self._run(root, "git", "rev-parse", "HEAD")
        clean = not self._run(root, "git", "status", "--porcelain", "--untracked-files=all")
        contained = self.provider.execute(root, "git", "merge-base", "--is-ancestor", head_sha, "main").returncode == 0
        return RepositoryEvidence(repository, branch, head_sha, clean, contained)

    def main_contains(self, root: Path, sha: str) -> bool:
        return self.provider.execute(root, "git", "merge-base", "--is-ancestor", sha, "main").returncode == 0

    def refresh_main_reference(self, root: Path) -> None:
        """Refresh remote main evidence without changing the shared checkout."""
        self._synchronize_command(root, "git", "fetch", "origin", "main")

    def remote_main_contains(self, root: Path, sha: str) -> bool:
        return self.provider.execute(root, "git", "merge-base", "--is-ancestor", sha, "origin/main").returncode == 0

    @staticmethod
    def _is_transient_index_lock_conflict(error: RuntimeError) -> bool:
        """Recognize only Git's contention signal, never a permission failure."""
        return bool(_TRANSIENT_INDEX_LOCK_CONFLICT.search(str(error)))

    def _synchronize_command(self, root: Path, *args: str) -> None:
        """Run one synchronization step with bounded, non-destructive lock retries."""
        for attempt in range(1, GIT_SYNC_LOCK_RETRY_ATTEMPTS + 1):
            try:
                self._run(root, *args)
                return
            except RunnerError as error:
                if (
                    attempt == GIT_SYNC_LOCK_RETRY_ATTEMPTS
                    or not self._is_transient_index_lock_conflict(error)
                ):
                    raise
                time.sleep(GIT_SYNC_LOCK_RETRY_DELAY_SECONDS * attempt)

    def _managed_sync_diagnostics(self, root: Path) -> str:
        """Return bounded, read-only context for a failed managed synchronization."""
        execute = getattr(self.provider, "execute", None)
        if not callable(execute):
            return (
                "target_branch=main authoritative_ref=origin/main local_sha=unavailable "
                "remote_sha=unavailable configured_upstream=unavailable fast_forward_state=unknown"
            )

        def value(*args: str) -> str:
            try:
                completed = execute(root, "git", *args)
            except (OSError, RuntimeError):
                return "unavailable"
            if completed.returncode:
                return "unavailable"
            candidate = completed.stdout.strip()
            return candidate if re.fullmatch(r"[0-9a-f]{40}|[A-Za-z0-9_./@{}-]+", candidate) else "unavailable"

        local_sha = value("rev-parse", "--verify", "main")
        remote_sha = value("rev-parse", "--verify", "origin/main")
        upstream = value("rev-parse", "--abbrev-ref", "--symbolic-full-name", "main@{upstream}")
        state = "unknown"
        if local_sha != "unavailable" and remote_sha != "unavailable":
            try:
                local_is_ancestor = execute(
                    root, "git", "merge-base", "--is-ancestor", "main", "origin/main"
                ).returncode == 0
                remote_is_ancestor = execute(
                    root, "git", "merge-base", "--is-ancestor", "origin/main", "main"
                ).returncode == 0
            except (OSError, RuntimeError):
                pass
            else:
                state = (
                    "synchronized" if local_is_ancestor and remote_is_ancestor
                    else "fast_forward_possible" if local_is_ancestor
                    else "local_ahead" if remote_is_ancestor
                    else "diverged"
                )
        return (
            "target_branch=main authoritative_ref=origin/main "
            f"local_sha={local_sha} remote_sha={remote_sha} configured_upstream={upstream} "
            f"fast_forward_state={state}"
        )

    @staticmethod
    def _managed_sync_error_detail(error: RunnerError) -> str:
        """Keep a Git failure useful without persisting unbounded process output."""
        return re.sub(r"\s+", " ", str(error)).strip()[:512]

    def synchronize_main(self, root: Path) -> None:
        self._synchronize_command(root, "git", "switch", "main")
        # Managed synchronization has one authority.  Do not let any local
        # branch.*.merge, pull.*, or upstream configuration select its source.
        try:
            self._synchronize_command(root, "git", "fetch", "origin", "main")
        except RunnerError as error:
            raise RunnerError(
                "MANAGED_MAIN_FETCH_FAILED: operation=git-fetch-origin-main "
                f"{self._managed_sync_diagnostics(root)}; {self._managed_sync_error_detail(error)}"
            ) from error
        try:
            self._synchronize_command(root, "git", "merge", "--ff-only", "origin/main")
        except RunnerError as error:
            raise RunnerError(
                "MANAGED_MAIN_FAST_FORWARD_FAILED: operation=git-merge-ff-only-origin-main "
                f"{self._managed_sync_diagnostics(root)}; {self._managed_sync_error_detail(error)}"
            ) from error

    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str:
        self._run(root, "git", "fetch", "--prune"); self.synchronize_main(root)
        if not self.inspect(root).clean: raise RunnerError("Cleanup blocked: workspace is not clean.")
        removed: list[str] = []; squash_reconciled: list[str] = []
        for branch in dict.fromkeys(branch for branch in branches if branch):
            if branch == "main": raise RunnerError("Cleanup blocked: transaction branch resolves to main.")
            if self.provider.execute(root, "git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0: continue
            if self.provider.execute(root, "git", "branch", "-d", branch).returncode:
                if self.provider.execute(root, "git", "branch", "-D", branch).returncode:
                    raise RunnerError(f"Cleanup blocked: transaction branch {branch} could not be safely removed.")
                squash_reconciled.append(branch)
            removed.append(branch)
        evidence = self.inspect(root)
        if evidence.branch != "main" or not evidence.clean or not evidence.main_contains_head:
            raise RunnerError("Cleanup blocked: main synchronization or workspace verification failed.")
        squash = f"; squash-reconciled={','.join(squash_reconciled)}" if squash_reconciled else ""
        return f"fetched/pruned; main synchronized; removed={','.join(removed) or 'already-absent'}{squash}"


class GhCliClient:
    def __init__(self, provider: GitHubProvider | None = None, repository: str | None = None) -> None:
        self.provider = provider or GitHubProvider()
        self.repository = repository

    def _github(self, *args: str) -> str:
        scoped = (*args, "--repo", self.repository) if self.repository else args
        return self.provider.github(*scoped)

    def version_preparation_writer(self) -> dict[str, object]:
        """Read the configured writer's safe identity and repository scope.

        This is a preflight observation, never a credential issuer or branch
        protection bypass.  GitHub does not expose a general branch-write
        guarantee here, so a protected merge remains GitHub's authority.
        """
        if not self.repository or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise RunnerError("Version preparation writer requires one exact GitHub repository scope.")
        try:
            identity = json.loads(self.provider.github("api", "user"))
            repository = json.loads(self._github("api", f"repos/{self.repository}"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation writer identity could not be read.") from error
        actor = identity.get("login") if isinstance(identity, dict) else None
        permissions = repository.get("permissions") if isinstance(repository, dict) else None
        if not isinstance(actor, str) or not actor or not isinstance(permissions, dict):
            raise RunnerError("Version preparation writer identity is incomplete.")
        return {"actor": actor, "repository_id": self.repository, "can_push": permissions.get("push") is True}

    def pull_request(self, number: int) -> PullRequestEvidence:
        try: raw = json.loads(self._github("pr", "view", str(number), "--json", "number,state,isDraft,mergeCommit,statusCheckRollup,headRefName,headRefOid,baseRefName,mergeStateStatus"))
        except RuntimeError as error: raise RunnerError(str(error)) from error
        # GitHub can append an empty rollup entry to an otherwise completed
        # merged PR. It is not a check and must not keep terminal evidence in
        # an endless polling loop.
        checks = [
            item
            for item in (raw.get("statusCheckRollup") or [])
            if isinstance(item, dict) and isinstance(item.get("status"), str)
        ]
        terminal = bool(checks) and all(item.get("status") == "COMPLETED" for item in checks)
        passed = terminal and all(item.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks)
        failed = tuple(str(item.get("name") or "unnamed check") for item in checks if item.get("status") == "COMPLETED" and item.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"})
        merge = raw.get("mergeCommit") or {}
        return PullRequestEvidence(
            raw["number"], raw["state"], terminal, passed, merge.get("oid"), raw["isDraft"], failed,
            raw.get("headRefName"), raw.get("baseRefName"), raw.get("mergeStateStatus"), raw.get("headRefOid"),
        )

    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None:
        """Return exactly one current PR for an already-checkpointed branch."""
        try:
            raw = json.loads(self._github(
                "pr", "list", "--head", branch, "--state", "all", "--json", "number"
            ))
        except RuntimeError as error:
            raise RunnerError(str(error)) from error
        numbers = [item.get("number") for item in raw if isinstance(item, dict) and isinstance(item.get("number"), int)]
        if not numbers:
            return None
        if len(numbers) != 1:
            raise RunnerError("Finalization recovery found more than one pull request for its checkpointed branch.")
        return self.pull_request(numbers[0])

    def create_or_recover_pull_request(self, branch: str, base: str, title: str, body: str) -> PullRequestEvidence:
        """Create one bounded PR or recover the sole existing branch identity."""
        existing = self.pull_request_for_head_branch(branch)
        if existing is not None:
            if existing.base_branch != base:
                raise RunnerError("Version preparation branch already has a pull request for another base.")
            return existing
        try:
            raw = self._github("pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body)
        except RuntimeError as error:
            # A successful create may lose its acknowledgement.  Only recover
            # the deterministic branch identity; never create a second PR.
            recovered = self.pull_request_for_head_branch(branch)
            if recovered is None:
                raise RunnerError(str(error)) from error
            if recovered.base_branch != base:
                raise RunnerError("Version preparation PR recovery found wrong base.") from error
            return recovered
        match = re.search(r"/pull/(\d+)(?:\s|$)", raw)
        if match is None:
            recovered = self.pull_request_for_head_branch(branch)
            if recovered is None:
                raise RunnerError("Version preparation PR create acknowledgement is ambiguous.")
            return recovered
        return self.pull_request(int(match.group(1)))

    def qualification_for_exact_head(self, number: int, head_sha: str) -> dict[str, object]:
        """Read the base branch's required checks for this exact candidate."""
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise RunnerError("Qualification requires an exact candidate SHA.")
        try:
            raw = json.loads(self._github("pr", "view", str(number), "--json", "headRefOid,baseRefOid,baseRefName,statusCheckRollup"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation qualification could not be read.") from error
        if raw.get("headRefOid") != head_sha:
            raise RunnerError("Qualification evidence belongs to a different pull request head.")
        base = raw.get("baseRefName")
        if not self.repository or not isinstance(base, str) or not base:
            raise RunnerError("Version preparation qualification lacks an exact protected base branch.")
        try:
            required = json.loads(self._github(
                "api", f"repos/{self.repository}/branches/{base}/protection/required_status_checks",
            ))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation required-check policy could not be read.") from error
        contexts = required.get("contexts") if isinstance(required, dict) else None
        protected_checks = required.get("checks") if isinstance(required, dict) else None
        names = set()
        if isinstance(contexts, list):
            names.update(item for item in contexts if isinstance(item, str) and item)
        if isinstance(protected_checks, list):
            names.update(item.get("context") for item in protected_checks if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"])
        if not names:
            raise RunnerError("Version preparation qualification has no required checks configured.")
        checks = [item for item in (raw.get("statusCheckRollup") or []) if isinstance(item, dict) and isinstance(item.get("status"), str)]
        if not checks or any(item.get("status") != "COMPLETED" for item in checks):
            raise RunnerError("Version preparation qualification is incomplete.")
        failed = [str(item.get("name") or "unnamed check") for item in checks if item.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"}]
        if failed:
            raise RunnerError("Version preparation qualification failed: " + ", ".join(failed))
        observed = {item.get("name") for item in checks if isinstance(item.get("name"), str) and item["name"]}
        missing = sorted(names - observed)
        if missing:
            raise RunnerError("Version preparation qualification is missing required checks: " + ", ".join(missing))
        return {"pull_request_id": number, "exact_qualified_sha": head_sha, "base_revision": raw.get("baseRefOid"), "required_checks": sorted(names), "checks": checks, "conclusion": "PASS"}
    def ready(self, number: int) -> None:
        try: self._github("pr", "ready", str(number))
        except RuntimeError as error:
            if "already ready" not in str(error).lower(): raise RunnerError(str(error)) from error

    def normalize_markdown_body(self, number: int) -> bool:
        """Repair only a fully escaped PR body generated by an agent."""
        try:
            raw = json.loads(self._github("pr", "view", str(number), "--json", "body"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Pull request Markdown could not be inspected.") from error
        body = raw.get("body") if isinstance(raw, dict) else None
        # Preserve real Markdown, including code snippets that deliberately
        # contain an escaped newline.  Only a one-line serialized body is bad.
        if not isinstance(body, str) or "\n" in body or "\\n" not in body:
            return False
        normalized = body.replace("\\r\\n", "\n").replace("\\n", "\n")
        try:
            self._github("pr", "edit", str(number), "--body", normalized)
        except RuntimeError as error:
            raise RunnerError("Pull request Markdown could not be normalized.") from error
        return True
    def merge(self, number: int) -> None:
        try: self._github("pr", "merge", str(number), "--squash", "--delete-branch")
        except RuntimeError as error: raise RunnerError(str(error)) from error
