"""One-shot, evidence-gated repair for a human-authored pull request.

This is deliberately separate from the managed execution lifecycle.  It never
claims an Inbox item, resumes a transaction, merges a pull request, or creates
a pull request.  It may only add one host-owned commit to the exact current
head of an eligible same-repository pull request.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .dashboard_configuration import get as dashboard_configuration
from .codex_capacity import read_remaining_percent
from .provider_readiness import failures as provider_readiness_failures
from .providers import CodexCliProvider, GitHubProvider, GitProvider


FAILED_CONCLUSIONS = frozenset({"ACTION_REQUIRED", "CANCELLED", "FAILURE", "STARTUP_FAILURE", "TIMED_OUT"})
SUCCESSFUL_CONCLUSIONS = frozenset({"NEUTRAL", "SKIPPED", "SUCCESS"})
STATE_DIRECTORY = Path(".engineering") / "status" / "pr-check-repairs"


class PullRequestCheckRepairError(RuntimeError):
    """The request is unsafe, stale, or cannot be admitted."""


def _repository(root: Path) -> str:
    remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
    if not match:
        raise PullRequestCheckRepairError("pr_check_repair_unavailable")
    return f"{match.group(1)}/{match.group(2)}"


def _state_path(root: Path, number: int, sha: str) -> Path:
    return root / STATE_DIRECTORY / f"{number}-{sha}.json"


def _read_state(root: Path, number: int, sha: str) -> dict[str, object] | None:
    try:
        value = json.loads(_state_path(root, number, sha).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def attempted(root: Path, number: int, sha: object) -> bool:
    """Whether this exact remote head already consumed its one repair action."""
    return isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha)) and _read_state(root, number, sha) is not None


def failed_check_names(checks: object) -> list[str]:
    """Return bounded terminal failures from GitHub's check-rollup shape."""
    if not isinstance(checks, list):
        return []
    return [
        str(check.get("name") or check.get("context") or "required check").strip()[:160]
        for check in checks if isinstance(check, dict)
        and str(check.get("conclusion") or check.get("state") or "").upper() in FAILED_CONCLUSIONS
    ]


def _write_state(root: Path, number: int, sha: str, payload: dict[str, object]) -> None:
    path = _state_path(root, number, sha)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pr-check-repair-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_summary(pull_request: dict[str, object]) -> tuple[list[str], bool]:
    checks = pull_request.get("statusCheckRollup")
    if not isinstance(checks, list) or not checks:
        return [], False
    failed = failed_check_names(checks)
    terminal = True
    for check in checks:
        if not isinstance(check, dict):
            return [], False
        conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion not in FAILED_CONCLUSIONS | SUCCESSFUL_CONCLUSIONS or (status and status != "COMPLETED"):
            terminal = False
    return failed, terminal


def current_evidence(root: Path, number: int) -> dict[str, object]:
    """Read the exact mutable PR evidence used for admission and projection."""
    if isinstance(number, bool) or number < 1:
        raise PullRequestCheckRepairError("pr_check_repair_invalid_request")
    repository = _repository(root)
    try:
        payload = GitHubProvider().github(
            "pr", "view", str(number), "--repo", repository,
            "--json", "number,state,isDraft,headRefOid,headRefName,baseRefName,headRepository,statusCheckRollup",
        )
        pull_request = json.loads(payload)
    except (RuntimeError, OSError, json.JSONDecodeError) as error:
        raise PullRequestCheckRepairError("pr_check_repair_unavailable") from error
    if not isinstance(pull_request, dict) or pull_request.get("number") != number:
        raise PullRequestCheckRepairError("pr_check_repair_unavailable")
    sha, branch = pull_request.get("headRefOid"), pull_request.get("headRefName")
    head_repository = pull_request.get("headRepository")
    head_repository_name = head_repository.get("nameWithOwner") if isinstance(head_repository, dict) else None
    failed_checks, terminal = _check_summary(pull_request)
    eligible = (
        str(pull_request.get("state") or "").upper() == "OPEN"
        and pull_request.get("isDraft") is not True
        and isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha))
        and isinstance(branch, str) and bool(branch)
        and head_repository_name == repository
        and terminal and bool(failed_checks)
    )
    state = _read_state(root, number, sha) if isinstance(sha, str) else None
    attempted = state is not None
    return {
        "number": number, "repository": repository, "head_sha": sha, "branch": branch,
        "failed_checks": failed_checks, "checks_terminal": terminal,
        "eligible": eligible and not attempted,
        "repair_state": str(state.get("status")) if state else None,
    }


def admit(root: Path, number: int) -> dict[str, object]:
    """Atomically reserve exactly one repair attempt for the current PR SHA."""
    evidence = current_evidence(root, number)
    if not evidence["eligible"]:
        raise PullRequestCheckRepairError("pr_check_repair_not_eligible")
    missing = provider_readiness_failures(root, require_github=True)
    if missing:
        raise PullRequestCheckRepairError("pr_check_repair_provider_not_ready")
    remaining = read_remaining_percent()
    reserve = int(dashboard_configuration(root)["codex_capacity_reserve_percent"])
    if remaining is None or remaining <= reserve:
        raise PullRequestCheckRepairError("pr_check_repair_capacity_unavailable")
    sha = str(evidence["head_sha"])
    payload = {
        "status": "QUEUED", "number": number, "head_sha": sha,
        "branch": evidence["branch"], "failed_checks": evidence["failed_checks"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_state(root, number, sha, payload)
    return evidence


def mark_dispatch_failed(root: Path, number: int, sha: str) -> None:
    state = _read_state(root, number, sha)
    if state and state.get("status") == "QUEUED":
        _write_state(root, number, sha, {**state, "status": "FAILED", "error": "pr_check_repair_dispatch_failed", "completed_at": datetime.now(timezone.utc).isoformat()})


def _command(root: Path, *arguments: str) -> str:
    completed = GitProvider().execute(root, "git", *arguments)
    if completed.returncode:
        raise PullRequestCheckRepairError("pr_check_repair_failed")
    return completed.stdout.strip()


def run(root: Path, number: int, sha: str) -> None:
    """Execute the already-admitted repair in an isolated, disposable worktree."""
    state = _read_state(root, number, sha)
    if not state or state.get("status") != "QUEUED":
        raise PullRequestCheckRepairError("pr_check_repair_not_eligible")
    evidence = current_evidence(root, number)
    # The durable reservation intentionally makes ``eligible`` false. Fresh
    # remote evidence must nevertheless still identify this exact failed head.
    if (
        evidence.get("head_sha") != sha
        or evidence.get("branch") != state.get("branch")
        or not evidence.get("checks_terminal")
        or not evidence.get("failed_checks")
    ):
        raise PullRequestCheckRepairError("pr_check_repair_stale")
    branch = str(state["branch"])
    _write_state(root, number, sha, {**state, "status": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat()})
    worktree = Path(tempfile.mkdtemp(prefix=f"ep-pr-{number}-", dir=root / ".engineering"))
    worktree.rmdir()
    try:
        _command(root, "fetch", "origin", branch)
        _command(root, "worktree", "add", "--detach", str(worktree), sha)
        prompt = (
            f"Repair only the failed GitHub checks for pull request #{number}.\n"
            f"Current head SHA: {sha}. Failed checks: {', '.join(str(item) for item in state['failed_checks'])}.\n"
            "Work only in this isolated worktree. Inspect the failed-check evidence and make the smallest focused source or test correction. "
            "Do not change the pull request scope. Do not create commits, push, open or merge a pull request, alter GitHub settings, or use destructive Git commands. "
            "Run focused verification where practical. Finish with the working tree containing only the proposed repair."
        )
        result = CodexCliProvider().invoke(
            worktree, ("codex", "exec", "--sandbox", "workspace-write", "-C", str(worktree), prompt),
        )
        if result.returncode:
            raise PullRequestCheckRepairError("pr_check_repair_agent_failed")
        if _command(worktree, "rev-parse", "HEAD") != sha:
            raise PullRequestCheckRepairError("pr_check_repair_scope_conflict")
        if not _command(worktree, "status", "--porcelain", "--untracked-files=all"):
            raise PullRequestCheckRepairError("pr_check_repair_no_change")
        _command(worktree, "diff", "--check")
        _command(worktree, "add", "--all")
        _command(worktree, "commit", "-m", f"fix(ci): repair failed checks for PR #{number}")
        commit = _command(worktree, "rev-parse", "HEAD")
        _command(worktree, "push", "--force-with-lease=refs/heads/" + branch + ":" + sha, "origin", "HEAD:refs/heads/" + branch)
        _write_state(root, number, sha, {**state, "status": "SUBMITTED", "commit_sha": commit, "submitted_at": datetime.now(timezone.utc).isoformat()})
    except PullRequestCheckRepairError as error:
        _write_state(root, number, sha, {**state, "status": "FAILED", "error": str(error), "completed_at": datetime.now(timezone.utc).isoformat()})
    finally:
        try:
            if worktree.exists() and not _command(worktree, "status", "--porcelain", "--untracked-files=all"):
                _command(root, "worktree", "remove", "--", str(worktree))
        except PullRequestCheckRepairError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    arguments = parser.parse_args()
    run(arguments.root.resolve(), arguments.pull_request, arguments.head_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
