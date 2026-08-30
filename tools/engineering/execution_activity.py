"""Versioned execution-activity evidence with explicit scope boundaries.

Only aggregate counters and Git-derived path metadata are retained.  Raw
commands, prompts, output, tokens, and workspace paths are deliberately out
of scope for this projection.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .storage import EngineeringStorageError, open_storage


EXECUTION_ACTIVITY_SUMMARY_VERSION = 1
CODEX_COMMAND_DEFINITION = (
    "One persisted Codex CLI provider invocation. It is not a shell command, "
    "tool call, prompt, token count, or GitHub API request."
)


def _git(root: Path, *args: str) -> str | None:
    """Use a small, read-only Git command without retaining its output."""
    from .providers import GitProvider
    try:
        result = GitProvider().execute(root, "git", *args)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def live_worktree_snapshot(root: Path) -> dict[str, object]:
    """Return a volatile current-uncommitted-state snapshot for active runs."""
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    counts = {"modified": 0, "added": 0, "deleted": 0}
    if status is None:
        availability = "UNAVAILABLE"
    else:
        availability = "AVAILABLE"
        for row in status.splitlines():
            code = row[:2]
            if "D" in code:
                counts["deleted"] += 1
            elif "A" in code or "?" in code:
                counts["added"] += 1
            elif code.strip():
                counts["modified"] += 1
    return {
        "kind": "LIVE_WORKTREE_SNAPSHOT",
        "volatile": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "availability": availability,
        "worktree_identity": "managed_target_checkout",
        "branch": _git(root, "branch", "--show-current") or "UNAVAILABLE",
        "head_sha": _git(root, "rev-parse", "HEAD") or "UNAVAILABLE",
        "uncommitted": counts,
        "meaning": "Current uncommitted worktree state only; never cumulative run delivery.",
    }


def _path_sets(names: str | None) -> dict[str, object]:
    added: set[str] = set()
    modified: set[str] = set()
    removed: set[str] = set()
    renamed: list[dict[str, str]] = []
    if names:
        for row in names.splitlines():
            fields = row.split("\t")
            if len(fields) < 2:
                continue
            status = fields[0]
            if status.startswith("R") and len(fields) >= 3:
                renamed.append({"from": fields[1], "to": fields[2]})
            elif status.startswith("A"):
                added.add(fields[1])
            elif status.startswith("D"):
                removed.add(fields[1])
            else:
                modified.add(fields[1])
    unique = added | modified | removed | {item["from"] for item in renamed} | {item["to"] for item in renamed}
    return {
        "added": sorted(added), "modified": sorted(modified), "removed": sorted(removed),
        "renamed": sorted(renamed, key=lambda item: (item["from"], item["to"])),
        "total_unique_changed_paths": len(unique),
    }


def _phase_paths(root: Path, baseline: str | None, commit: str | None) -> dict[str, object] | None:
    if not baseline or not commit or _git(root, "rev-parse", commit) is None:
        return None
    return _path_sets(_git(root, "diff", "--name-status", "-M", baseline, commit))


def _command_activity(root: Path, run_id: str) -> dict[str, object]:
    connection = open_storage(root)
    try:
        provider_rows = connection.execute(
            "SELECT phase,role,COUNT(*) FROM provider_invocations "
            "WHERE run_id=? AND provider='codex_cli' GROUP BY phase,role", (run_id,)
        ).fetchall()
        validation_rows = connection.execute(
            "SELECT COUNT(*) FROM execution_validation_command_invocations WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    primary: dict[str, int] = {}
    reviewers: dict[str, dict[str, int]] = {}
    for phase, role, count in provider_rows:
        if not isinstance(phase, str) or not isinstance(role, str) or not isinstance(count, int):
            continue
        if role.casefold().startswith("reviewer"):
            reviewers.setdefault(role, {})[phase] = count
        else:
            primary[phase] = primary.get(phase, 0) + count
    reviewer_total = sum(sum(phases.values()) for phases in reviewers.values())
    primary_total = sum(primary.values())
    host_validation_total = int(validation_rows[0]) if validation_rows and isinstance(validation_rows[0], int) else 0
    return {
        "metric_definition_version": 1,
        "codex_command_definition": CODEX_COMMAND_DEFINITION,
        "primary_codex_commands_by_phase": primary,
        "primary_codex_commands_total": primary_total,
        "reviewer_codex_commands_by_reviewer_and_phase": reviewers,
        "reviewer_codex_commands_total": reviewer_total,
        "host_validation_commands_by_phase": {"VALIDATION": host_validation_total},
        "host_validation_commands_total": host_validation_total,
        "overall_activity_total": primary_total + reviewer_total + host_validation_total,
    }


def cumulative_activity(root: Path, run_id: str) -> dict[str, object]:
    """Project current persisted counters using the terminal metric definition."""
    return _command_activity(root, run_id)


def build_terminal_activity_summary(root: Path, state: object, bundle: object) -> dict[str, object]:
    """Build the canonical terminal summary solely from persisted/Git evidence."""
    run_id = str(getattr(state, "run_id"))
    target = Path(str(getattr(bundle, "target_workspace")))
    # The Evidence Bundle owns the authoritative transaction range.  Do not
    # recompute it from a later lifecycle commit: that can exclude an earlier
    # implementation merge or silently turn a known range into a different one.
    baseline = getattr(bundle, "transaction_baseline_sha", None)
    if not isinstance(baseline, str) or not baseline:
        baseline = None
    target_sha = str(getattr(bundle, "target_commit"))
    renamed = [
        {"from": source, "to": destination}
        for source, destination in getattr(bundle, "files_renamed", ())
        if isinstance(source, str) and isinstance(destination, str)
    ]
    delivery = {
        "added": sorted(set(getattr(bundle, "files_added", ()))),
        "modified": sorted(set(getattr(bundle, "files_modified", ()))),
        "removed": sorted(set(getattr(bundle, "files_removed", ()))),
        "renamed": sorted(renamed, key=lambda item: (item["from"], item["to"])),
        # Evidence Bundle changed_files is the canonical run-delivery path
        # count, including both sides of a proven rename.
        "total_unique_changed_paths": len(set(getattr(bundle, "changed_files", ()))),
    }
    implementation = getattr(state, "implementation_merge_commit", None)
    finalization = getattr(state, "finalization_merge_commit", None)
    phase_attribution: dict[str, object] = {}
    implementation_parent = _git(target, "rev-parse", f"{implementation}^") if implementation else None
    if implementation:
        phase_attribution["implementation"] = _phase_paths(target, implementation_parent, implementation) or "UNAVAILABLE"
    if finalization:
        phase_attribution["finalization"] = _phase_paths(target, implementation, finalization) or "UNAVAILABLE"
    return {
        "summary_version": EXECUTION_ACTIVITY_SUMMARY_VERSION,
        "run_id": run_id,
        "activity": _command_activity(root, run_id),
        "terminal_delivery_diff": {
            "authority": "AUTHORITATIVE_REPOSITORY_EVIDENCE",
            "transaction_baseline_sha": baseline or "UNAVAILABLE",
            "terminal_target_sha": target_sha,
            **delivery,
            "phase_attribution": phase_attribution or "UNAVAILABLE",
            "implementation_pr": getattr(state, "implementation_pull_request", None),
            "finalization_pr": getattr(state, "finalization_pull_request", None),
            "reconciliation_pr": getattr(state, "reconciliation_pull_request", None),
            "implementation_merge_commit": implementation,
            "finalization_merge_commit": finalization,
            "per_pr_changed_file_counts": "GitHub evidence scoped to each PR; never summed into this run total.",
        },
    }


def persist_terminal_activity_summary(root: Path, summary: dict[str, object]) -> dict[str, object]:
    """Insert once; legacy runs remain unavailable rather than inferred."""
    run_id = summary.get("run_id")
    if not isinstance(run_id, str):
        raise EngineeringStorageError("Execution activity summary run identity is invalid.")
    payload = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_activity_summaries(run_id,summary_version,payload,persisted_at) VALUES(?,?,?,?)",
            (run_id, EXECUTION_ACTIVITY_SUMMARY_VERSION, payload, datetime.now(timezone.utc).isoformat()),
        )
        row = connection.execute("SELECT payload FROM execution_activity_summaries WHERE run_id=?", (run_id,)).fetchone()
    finally:
        connection.close()
    return json.loads(row[0]) if row else summary


def terminal_activity_summary(root: Path, run_id: str) -> dict[str, object] | None:
    """Return only a persisted canonical summary; never reconstruct legacy rows."""
    try:
        connection = open_storage(root)
        try:
            row = connection.execute("SELECT payload FROM execution_activity_summaries WHERE run_id=?", (run_id,)).fetchone()
        finally:
            connection.close()
        return json.loads(row[0]) if row and isinstance(row[0], str) else None
    except (EngineeringStorageError, json.JSONDecodeError):
        return None
