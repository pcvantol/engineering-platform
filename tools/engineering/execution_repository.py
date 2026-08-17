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


class RepositoryClient(Protocol):
    def inspect(self, root: Path) -> RepositoryEvidence: ...
    def main_contains(self, root: Path, sha: str) -> bool: ...
    def refresh_main_reference(self, root: Path) -> None: ...
    def remote_main_contains(self, root: Path, sha: str) -> bool: ...
    def synchronize_main(self, root: Path) -> None: ...


class GitHubClient(Protocol):
    def pull_request(self, number: int) -> PullRequestEvidence: ...
    def ready(self, number: int) -> None: ...
    def merge(self, number: int) -> None: ...


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
        repository = remote.removesuffix(".git").split(":")[-1].replace("github.com/", "")
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

    def synchronize_main(self, root: Path) -> None:
        self._synchronize_command(root, "git", "switch", "main")
        self._synchronize_command(root, "git", "pull", "--ff-only")

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
    def __init__(self, provider: GitHubProvider | None = None) -> None: self.provider = provider or GitHubProvider()
    def pull_request(self, number: int) -> PullRequestEvidence:
        try: raw = json.loads(self.provider.github("pr", "view", str(number), "--json", "number,state,isDraft,mergeCommit,statusCheckRollup,headRefName,baseRefName"))
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
            raw.get("headRefName"), raw.get("baseRefName"),
        )
    def ready(self, number: int) -> None:
        try: self.provider.github("pr", "ready", str(number))
        except RuntimeError as error:
            if "already ready" not in str(error).lower(): raise RunnerError(str(error)) from error
    def merge(self, number: int) -> None:
        try: self.provider.github("pr", "merge", str(number), "--squash", "--delete-branch")
        except RuntimeError as error: raise RunnerError(str(error)) from error
