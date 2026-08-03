"""Fail-closed Level 2 checks for an Engineering execution workspace.

The checks in this module inspect only the selected target repository.  They
never claim Inbox work, change a branch, contact a remote, or execute an
engineering action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from time import monotonic

from .platform_api import PlatformConfiguration, PlatformConfigurationError


@dataclass(frozen=True)
class WorkspacePreflightCheck:
    identifier: str
    outcome: str
    reason: str
    recovery: str


@dataclass(frozen=True)
class WorkspacePreflightResult:
    outcome: str
    workspace: str
    target_repository: str
    branch: str
    execution_mode: str
    timestamp: str
    duration_ms: int
    checks: tuple[WorkspacePreflightCheck, ...]

    def payload(self, run_id: str | None = None) -> dict[str, object]:
        value = asdict(self)
        value["checks"] = [asdict(check) for check in self.checks]
        value["run_id"] = run_id
        return value


def _check(identifier: str, passed: bool, reason: str, recovery: str) -> WorkspacePreflightCheck:
    return WorkspacePreflightCheck(identifier, "PASS" if passed else "FAIL", reason, recovery)


def _git(target: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", "-C", str(target), *arguments), text=True, capture_output=True, check=False, timeout=3)


def _prompt_value(prompt: str, field: str) -> str | None:
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() == f"{field}:".casefold():
            for following in lines[index + 1 :]:
                value = following.strip()
                if value:
                    return value
        prefix = f"{field}:"
        if line.strip().casefold().startswith(prefix.casefold()):
            value = line.strip()[len(prefix) :].strip()
            return value or None
    return None


def _execution_mode(prompt: str) -> str:
    return "GENESIS" if (_prompt_value(prompt, "Execution Mode") or "").casefold() == "genesis" else "MANAGED"


def _resolve_target(root: Path, prompt: str, mode: str, configuration: PlatformConfiguration | None) -> Path | None:
    if mode == "MANAGED":
        return root.resolve()
    requested = _prompt_value(prompt, "Target repository")
    if not requested:
        return None
    return Path(requested).expanduser().resolve()


def _writable(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace-preflight-", dir=path)
        os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _persist(root: Path, result: WorkspacePreflightResult, run_id: str | None) -> None:
    directory = root / ".engineering" / "status"
    if not _writable(directory):
        return
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace-preflight-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(result.payload(run_id), separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "workspace_preflight.json")
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def execute(root: Path, prompt: str, *, run_id: str | None = None) -> WorkspacePreflightResult:
    """Run Level 2 workspace checks without mutating the selected repository."""
    started = monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    checks: list[WorkspacePreflightCheck] = []
    try:
        configuration = PlatformConfiguration.load(root)
        checks.append(_check("workspace_identity", True, "Workspace identity is available.", "No action required."))
    except PlatformConfigurationError:
        configuration = None
        checks.append(_check("workspace_identity", False, "Workspace identity is unavailable.", "Restore a valid Engineering Platform configuration."))
    mode = _execution_mode(prompt)
    target = _resolve_target(root, prompt, mode, configuration)
    target_display = str(target) if target else "unavailable"
    target_exists = target is not None and target.is_dir()
    checks.append(_check("target_repository", target_exists, "Target repository resolves to an existing directory." if target_exists else "Target repository cannot be resolved to an existing directory.", "Select an existing engineering target repository."))
    approved = False
    if target and target_exists:
        if mode == "MANAGED":
            approved = target == root.resolve()
        elif configuration and configuration.workspace.provisioning_root:
            approved_root = Path(configuration.workspace.provisioning_root).resolve()
            approved = target.parent == approved_root
    checks.append(_check("approved_workspace_root", approved, "Target repository belongs to an approved workspace root." if approved else "Target repository is outside the approved workspace root.", "Use the configured workspace root for the target repository."))

    git_directory: Path | None = None
    if target_exists:
        repository = _git(target, "rev-parse", "--git-dir")
        if repository.returncode == 0 and repository.stdout.strip():
            git_directory = Path(repository.stdout.strip())
            if not git_directory.is_absolute():
                git_directory = (target / git_directory).resolve()
    checks.append(_check("git_repository", git_directory is not None, "Target is a valid Git repository." if git_directory else "Target is not a valid Git repository.", "Initialize or select a valid Git repository."))
    if git_directory:
        checks.append(_check("git_metadata_access", git_directory.is_dir(), "Git metadata is accessible." if git_directory.is_dir() else "Git metadata is inaccessible.", "Restore access to the repository Git metadata."))
        metadata_writable = _writable(git_directory)
        checks.append(_check("git_metadata_writable", metadata_writable, "Git metadata is writable." if metadata_writable else "Git metadata is not writable.", "Restore write access to the repository Git metadata."))
        status = _git(target, "status", "--porcelain=v1", "--untracked-files=all")
        entries = status.stdout.splitlines() if status.returncode == 0 else []
        staged = any(len(entry) >= 2 and entry[:2] != "??" and entry[0] != " " for entry in entries)
        unstaged = any(len(entry) >= 2 and entry[:2] != "??" and entry[1] != " " for entry in entries)
        untracked = any(entry.startswith("??") for entry in entries)
        checks.extend((
            _check("worktree_staged", not staged, "No staged changes are present." if not staged else "Staged changes are present.", "Commit, stash, or remove staged changes before execution."),
            _check("worktree_unstaged", not unstaged, "No unstaged changes are present." if not unstaged else "Unstaged changes are present.", "Commit, stash, or remove unstaged changes before execution."),
            _check("worktree_untracked", not untracked, "No untracked files are present." if not untracked else "Untracked files are present.", "Commit, remove, or explicitly ignore untracked files before execution."),
        ))
        operation_checks = (
            ("git_index_lock", git_directory / "index.lock", "Remove the stale Git index lock after confirming no Git process is running."),
            ("git_merge", git_directory / "MERGE_HEAD", "Finish or abort the merge before execution."),
            ("git_rebase", git_directory / "rebase-merge", "Finish or abort the rebase before execution."),
            ("git_cherry_pick", git_directory / "CHERRY_PICK_HEAD", "Finish or abort the cherry-pick before execution."),
            ("git_revert", git_directory / "REVERT_HEAD", "Finish or abort the revert before execution."),
            ("git_bisect", git_directory / "BISECT_LOG", "Finish or reset the bisect before execution."),
        )
        for identifier, marker, recovery in operation_checks:
            checks.append(_check(identifier, not marker.exists(), "No unfinished Git operation is present." if not marker.exists() else "An unfinished Git operation is present.", recovery))
    branch = "unavailable"
    if git_directory and target:
        branch_result = _git(target, "branch", "--show-current")
        branch = branch_result.stdout.strip() or "detached"
        checks.append(_check("target_repository_identity", branch != "unavailable", "Target repository identity is available." if branch != "unavailable" else "Target repository identity is unavailable.", "Restore repository metadata and branch identity."))
        if mode == "GENESIS":
            checks.append(_check("genesis_local_repository", branch != "detached", "Genesis target is a local repository." if branch != "detached" else "Genesis target has no active local branch.", "Select a local repository with an active branch."))
        else:
            expected = configuration.workspace.default_branch if configuration else "main"
            checks.append(_check("managed_expected_branch", branch == expected, "Managed target is on the expected branch." if branch == expected else f"Managed target is not on the expected branch {expected}.", f"Switch the repository to {expected} before submitting work."))
            remote = _git(target, "remote", "get-url", "origin")
            remote_valid = remote.returncode == 0 and bool(remote.stdout.strip())
            checks.append(_check("managed_remote", remote_valid, "Managed target has a valid origin remote." if remote_valid else "Managed target has no valid origin remote.", "Configure the managed repository origin remote."))
            divergence = _git(target, "rev-list", "--left-right", "--count", "@{upstream}...HEAD") if remote_valid else None
            synchronized = bool(divergence and divergence.returncode == 0 and divergence.stdout.strip() == "0\t0")
            checks.append(_check("managed_synchronization", synchronized, "Managed target is synchronized with its upstream." if synchronized else "Managed target is not synchronized with its upstream.", "Synchronize the expected branch with its configured upstream."))
    workspace = configuration.workspace.name if configuration else "unavailable"
    outcome = "FAIL" if any(check.outcome == "FAIL" for check in checks) else "PASS"
    result = WorkspacePreflightResult(outcome, workspace, target_display, branch, mode, timestamp, round((monotonic() - started) * 1000), tuple(checks))
    _persist(root, result, run_id)
    return result


def latest(root: Path) -> dict[str, object]:
    """Return compact, safe Workspace Preflight evidence."""
    try:
        payload = json.loads((root / ".engineering" / "status" / "workspace_preflight.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in ("outcome", "workspace", "target_repository", "branch", "execution_mode", "timestamp", "duration_ms", "checks", "run_id") if key in payload}
