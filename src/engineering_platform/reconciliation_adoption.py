"""Guarded adoption of one legacy direct reconciliation commit into a PR.

This route exists only for runs blocked by the retired direct-to-main policy.
It does not retry execution, rewrite the terminal run, or create new changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Protocol

from .agent_state import RUN_ID_PATTERN, StateStore, TransactionState
from .execution_errors import RunnerError
from .execution_models import PullRequestEvidence
from .execution_repository import GhCliClient, github_repository_slug
from .providers import GitProvider


ROLLING_RECORDS = frozenset({
    "ARCHITECT_SESSION.md",
    "BOOTSTRAP.md",
    "HANDOFF.md",
    "README.md",
})
LEGACY_ADOPTION_REPOSITORY = "pcvantol/forge"


class AdoptionGitHub(Protocol):
    def create_or_recover_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False,
    ) -> PullRequestEvidence: ...
    def ready(self, number: int) -> None: ...


def _command(provider: GitProvider, root: Path, *args: str) -> str:
    try:
        return provider.command(root, *args)
    except RuntimeError as error:
        raise RunnerError(str(error)) from error


def _verified_candidate(state: TransactionState, explicit: str | None) -> str:
    candidates = [
        item.get("commit_sha")
        for item in state.commit_evidence
        if item.get("description") == "end_reconciliation_commit_verified"
    ]
    if len(candidates) == 1 and isinstance(candidates[0], str):
        if explicit is not None and explicit != candidates[0]:
            raise RunnerError("Explicit candidate conflicts with recorded reconciliation evidence.")
        return candidates[0]
    if explicit is None:
        raise RunnerError("Legacy reconciliation requires an explicit candidate for guarded adoption.")
    return explicit


def adopt_legacy_direct_reconciliation(
    root: Path,
    state: TransactionState,
    *,
    provider: GitProvider,
    github: AdoptionGitHub,
    candidate: str | None = None,
) -> PullRequestEvidence:
    """Move exactly one verified four-record commit behind the protected PR gate."""
    root = root.resolve()
    if (
        not state.terminal
        or state.phase != "BLOCKED"
        or state.transaction_kind != "RECONCILIATION"
        or not state.owner_authorized
        or state.repository != LEGACY_ADOPTION_REPOSITORY
        or state.reconciliation_pull_request is not None
        or not state.finalization_merge_commit
    ):
        raise RunnerError("Run is not an owner-authorized blocked legacy reconciliation.")
    candidate = _verified_candidate(state, candidate)
    if re.fullmatch(r"[0-9a-f]{40}", candidate) is None:
        raise RunnerError("Legacy reconciliation candidate identity is invalid.")
    branch = f"codex/reconcile-{state.run_id}"
    current_branch = _command(provider, root, "git", "branch", "--show-current")
    if current_branch not in {"main", branch}:
        raise RunnerError("Legacy reconciliation adoption requires main or its deterministic branch.")
    if _command(provider, root, "git", "status", "--porcelain", "--untracked-files=all"):
        raise RunnerError("Legacy reconciliation adoption requires a clean checkout.")
    _command(provider, root, "git", "fetch", "origin", "main")
    remote_main = _command(provider, root, "git", "rev-parse", "origin/main")
    local_main = _command(provider, root, "git", "rev-parse", "main")
    if provider.execute(
        root, "git", "merge-base", "--is-ancestor", state.finalization_merge_commit, "origin/main"
    ).returncode:
        raise RunnerError("Finalization merge is not present on the protected base.")
    if provider.execute(
        root, "git", "merge-base", "--is-ancestor", "origin/main", candidate
    ).returncode:
        raise RunnerError("Legacy reconciliation candidate is not based on current origin/main.")
    count = _command(provider, root, "git", "rev-list", "--count", f"origin/main..{candidate}")
    if count != "1":
        raise RunnerError("Legacy reconciliation adoption requires exactly one unpublished commit.")
    changed = frozenset(
        line for line in _command(
            provider, root, "git", "diff", "--name-only", "--diff-filter=ACMRT",
            f"origin/main..{candidate}",
        ).splitlines() if line
    )
    if changed != ROLLING_RECORDS:
        raise RunnerError("Legacy reconciliation candidate is not the exact four-record update.")

    branch_ref = provider.execute(root, "git", "rev-parse", "--verify", branch)
    if branch_ref.returncode == 0:
        if branch_ref.stdout.strip() != candidate:
            raise RunnerError("Deterministic reconciliation branch points to another commit.")
        if current_branch != branch:
            _command(provider, root, "git", "switch", branch)
    else:
        _command(provider, root, "git", "switch", "-c", branch, candidate)
    if local_main == candidate:
        _command(provider, root, "git", "update-ref", "refs/heads/main", remote_main, candidate)
    elif local_main != remote_main:
        raise RunnerError("Local main changed during guarded reconciliation adoption.")
    _command(provider, root, "git", "push", "--set-upstream", "origin", branch)

    pull_request = github.create_or_recover_pull_request(
        branch,
        "main",
        f"docs: adopt protected reconciliation for {state.run_id}",
        "\n".join((
            "## Scope",
            "Adopt the already verified legacy end-reconciliation commit behind the protected merge boundary.",
            "",
            f"- Run: `{state.run_id}`",
            f"- Candidate: `{candidate}`",
            "- Changes: the four canonical rolling records only",
            "- No execution retry or historical evidence rewrite",
        )),
        draft=True,
    )
    if (
        pull_request.head_branch != branch
        or pull_request.base_branch != "main"
        or pull_request.state not in {"OPEN", "MERGED"}
    ):
        raise RunnerError("Recovered reconciliation pull request does not match its guarded identity.")
    if pull_request.state == "OPEN":
        github.ready(pull_request.number)
    return pull_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engineering-reconciliation-adopt",
        description="Adopt one blocked legacy direct reconciliation commit into its protected PR.",
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--central-database", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    if RUN_ID_PATTERN.fullmatch(args.run_id) is None:
        parser.error("run ID is invalid")
    root = args.repo.resolve()
    state = StateStore(
        root / ".engineering" / "engineering-runs",
        central_database=args.central_database.resolve(),
        emit_local_projection=False,
    ).load(args.run_id)
    provider = GitProvider()
    repository = github_repository_slug(_command(provider, root, "git", "remote", "get-url", "origin"))
    result = adopt_legacy_direct_reconciliation(
        root, state, provider=provider, github=GhCliClient(repository=repository),
        candidate=args.candidate,
    )
    print(json.dumps({
        "run_id": args.run_id,
        "branch": result.head_branch,
        "pull_request": result.number,
        "state": result.state,
        "base": result.base_branch,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
