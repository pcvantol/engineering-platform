"""Fail-closed recovery of missing historical Managed pull-request evidence.

This tool is intentionally operator-invoked and dry-run by default.  It never
creates, edits, merges, or closes a pull request.  It can only link a missing
checkpoint field after the live GitHub record exactly matches the checkpointed
branch and merge commit for that role.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable

from .agent_state import StateError, TransactionState
from .execution_models import PullRequestEvidence
from .execution_repository import GhCliClient, GitHubClient
from .providers import GitProvider
from .storage import EngineeringStorageError, open_storage


ROLES = ("IMPLEMENTATION", "FINALIZATION")
ACTOR = "operator_pr_evidence_backfill"


@dataclass(frozen=True)
class BackfillDecision:
    run_id: str
    role: str
    outcome: str
    reason: str
    pull_request: int | None = None
    expected_branch: str | None = None
    expected_merge_commit: str | None = None

    def report(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "role": self.role.lower(),
            "outcome": self.outcome.lower(),
            "reason": self.reason,
            "pull_request": self.pull_request,
            "expected_branch": self.expected_branch,
            "expected_merge_commit": self.expected_merge_commit,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _role_evidence(state: TransactionState, role: str) -> tuple[int | None, str | None, str | None]:
    if role == "IMPLEMENTATION":
        return state.implementation_pull_request, state.implementation_branch, state.implementation_merge_commit
    return state.finalization_pull_request, state.finalization_branch, state.finalization_merge_commit


def _candidate_decision(
    state: TransactionState, role: str, github: GitHubClient, repository: str | None,
    main_contains: Callable[[str], bool | None],
) -> BackfillDecision:
    recorded_pr, branch, merge_commit = _role_evidence(state, role)
    base = dict(run_id=state.run_id, role=role, expected_branch=branch, expected_merge_commit=merge_commit)
    if state.execution_mode != "MANAGED":
        return BackfillDecision(outcome="SKIPPED", reason="not_managed_execution", **base)
    if repository is None or state.repository != repository:
        return BackfillDecision(outcome="SKIPPED", reason="repository_evidence_unavailable_or_mismatched", **base)
    if not state.terminal:
        return BackfillDecision(outcome="SKIPPED", reason="run_not_terminal", **base)
    if recorded_pr is not None:
        return BackfillDecision(outcome="SKIPPED", reason="pull_request_already_recorded", pull_request=recorded_pr, **base)
    if not branch or not merge_commit:
        return BackfillDecision(outcome="SKIPPED", reason="checkpoint_evidence_incomplete", **base)
    try:
        evidence = github.pull_request_for_head_branch(branch)
    except Exception:
        return BackfillDecision(outcome="SKIPPED", reason="github_evidence_unavailable", **base)
    if evidence is None:
        return BackfillDecision(outcome="SKIPPED", reason="pull_request_not_found_for_checkpoint_branch", **base)
    if not _exact_match(evidence, branch, merge_commit):
        return BackfillDecision(outcome="SKIPPED", reason="github_evidence_does_not_exactly_match_checkpoint", **base)
    contained = main_contains(merge_commit)
    if contained is None:
        return BackfillDecision(outcome="SKIPPED", reason="repository_merge_evidence_unavailable", **base)
    if not contained:
        return BackfillDecision(outcome="SKIPPED", reason="merge_commit_not_in_origin_main", **base)
    return BackfillDecision(outcome="APPLIED", reason="exact_github_branch_and_merge_evidence", pull_request=evidence.number, **base)


def _exact_match(evidence: PullRequestEvidence, branch: str, merge_commit: str) -> bool:
    return (
        evidence.state == "MERGED"
        and evidence.head_branch == branch
        and evidence.base_branch == "main"
        and evidence.merge_commit == merge_commit
    )


def _load_states(root: Path, run_id: str | None) -> Iterable[TransactionState]:
    connection = open_storage(root, create=False)
    try:
        if run_id:
            rows = connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (run_id,)).fetchall()
        else:
            rows = connection.execute("SELECT payload FROM engineering_transactions ORDER BY run_id").fetchall()
    finally:
        connection.close()
    for (payload,) in rows:
        try:
            yield TransactionState.from_dict(json.loads(payload))
        except (StateError, TypeError, json.JSONDecodeError):
            # A malformed checkpoint is never eligible for recovery. There is
            # no safe run id to write against, so it remains database evidence.
            continue


def _current_repository(root: Path) -> str | None:
    result = GitProvider().execute(root, "git", "remote", "get-url", "origin")
    if result.returncode:
        return None
    value = result.stdout.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/")
    return value if value.count("/") == 1 else None


def _origin_main_contains(root: Path, commit: str) -> bool | None:
    """Refresh only the remote-tracking ref; never change the checkout or branch."""
    provider = GitProvider()
    if provider.execute(root, "git", "fetch", "origin", "main").returncode:
        return None
    result = provider.execute(root, "git", "merge-base", "--is-ancestor", commit, "origin/main")
    return result.returncode == 0


def _updated_state(state: TransactionState, decision: BackfillDecision) -> TransactionState:
    if decision.role == "IMPLEMENTATION":
        return replace(state, implementation_pull_request=decision.pull_request)
    return replace(state, finalization_pull_request=decision.pull_request)


def _record_skip(root: Path, decision: BackfillDecision, *, observed_at: str) -> None:
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO execution_pr_evidence_backfills(run_id,pr_role,outcome,reason,pr_number,expected_branch,expected_merge_commit,observed_at,actor) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision.run_id, decision.role, decision.outcome, decision.reason, decision.pull_request,
             decision.expected_branch, decision.expected_merge_commit, observed_at, ACTOR),
        )
    finally:
        connection.close()


def _write_projection(directory: Path, state: TransactionState) -> None:
    """Refresh the compatibility JSON only after the canonical commit succeeds."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"{state.run_id}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{state.run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except OSError:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _apply(root: Path, state: TransactionState, decision: BackfillDecision, *, observed_at: str) -> BackfillDecision:
    """Atomically persist the exact verified link and its audit record."""
    connection = open_storage(root)
    updated: TransactionState | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (state.run_id,)).fetchone()
        if not row:
            decision = replace(decision, outcome="SKIPPED", reason="checkpoint_disappeared", pull_request=None)
        else:
            current = TransactionState.from_dict(json.loads(row[0]))
            current_pr, current_branch, current_merge = _role_evidence(current, decision.role)
            if (
                current.execution_mode != "MANAGED" or not current.terminal
                or current_pr is not None
                or current_branch != decision.expected_branch
                or current_merge != decision.expected_merge_commit
            ):
                decision = replace(decision, outcome="SKIPPED", reason="checkpoint_changed_since_verification", pull_request=None)
            else:
                updated = _updated_state(current, decision)
                encoded = json.dumps(updated.to_dict(), separators=(",", ":"), sort_keys=True)
                connection.execute(
                    "UPDATE engineering_transactions SET payload=?,phase=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (encoded, updated.phase, updated.run_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (updated.run_id, updated.phase, encoded),
                )
        connection.execute(
            "INSERT INTO execution_pr_evidence_backfills(run_id,pr_role,outcome,reason,pr_number,expected_branch,expected_merge_commit,observed_at,actor) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision.run_id, decision.role, decision.outcome, decision.reason, decision.pull_request,
             decision.expected_branch, decision.expected_merge_commit, observed_at, ACTOR),
        )
        connection.execute("COMMIT")
    except (EngineeringStorageError, StateError, OSError, ValueError, json.JSONDecodeError):
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    if updated is not None:
        try:
            _write_projection(root / ".engineering" / "engineering-runs", updated)
        except OSError:
            return replace(decision, reason="exact_evidence_applied_projection_refresh_failed")
    return decision


def backfill(root: Path, *, apply: bool = False, run_id: str | None = None,
             github: GitHubClient | None = None, repository: str | None = None,
             main_contains: Callable[[str], bool | None] | None = None) -> dict[str, object]:
    """Inspect or atomically backfill exact historical Managed PR evidence."""
    client = github or GhCliClient()
    repository = repository if repository is not None else _current_repository(root)
    checked_merges: dict[str, bool | None] = {}

    def current_main_contains(commit: str) -> bool | None:
        if commit not in checked_merges:
            check = main_contains or (lambda value: _origin_main_contains(root, value))
            checked_merges[commit] = check(commit)
        return checked_merges[commit]

    decisions: list[BackfillDecision] = []
    for state in _load_states(root, run_id):
        for role in ROLES:
            decision = _candidate_decision(state, role, client, repository, current_main_contains)
            if apply:
                if decision.outcome == "APPLIED":
                    decision = _apply(root, state, decision, observed_at=_now())
                else:
                    _record_skip(root, decision, observed_at=_now())
            decisions.append(decision)
    applied = sum(item.outcome == "APPLIED" for item in decisions)
    skipped = len(decisions) - applied
    return {"mode": "apply" if apply else "dry_run", "applied": applied, "skipped": skipped, "decisions": [item.report() for item in decisions]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist only exact, verified evidence matches")
    parser.add_argument("--run-id", help="limit recovery to one canonical run id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = backfill(Path.cwd().resolve(), apply=args.apply, run_id=args.run_id)
    except EngineeringStorageError as error:
        raise SystemExit(f"PR-evidence recovery could not safely access storage: {error}") from error
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
