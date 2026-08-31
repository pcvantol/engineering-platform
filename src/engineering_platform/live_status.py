"""Atomic local status projection for the foreground engineering runner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from .agent_state import TransactionState, redact_diagnostic
from .storage import EngineeringStorageError, load_execution_context_snapshot, load_forge_governance_handoff_snapshot, load_projection, open_storage, store_projection
from .providers import GitProvider
from .execution_activity import cumulative_activity, live_worktree_snapshot

_REVIEWER_PROJECTION_PHASE = "CAPABILITY_REVIEW"


def _successful_reviewer_agents(value: object) -> list[dict[str, object]]:
    """Return immutable review evidence only after every reviewer succeeded."""
    if not isinstance(value, list):
        return []
    reviewers = [item for item in value if isinstance(item, dict)]
    if not reviewers or len(reviewers) != len(value):
        return []
    return reviewers if all(item.get("status") == "completed" for item in reviewers) else []


def write_live_status(
    root: Path,
    state: TransactionState,
    action: str,
    reviewer_agents: list[dict[str, object]] | None = None,
    runtime_metadata: Mapping[str, str] | None = None,
    workspace_progress: Mapping[str, int] | None = None,
    transient_action: str | None = None,
) -> Path:
    """Atomically publish the advisory current transaction state."""
    directory = root / ".engineering" / "status"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "current.json"
    checkout = (
        Path(state.genesis_repository_path).expanduser()
        if state.execution_mode == "GENESIS" and state.genesis_repository_path
        else root
    )
    try:
        observed_branch = GitProvider().execute(checkout, "git", "branch", "--show-current").stdout.strip()
    except OSError:
        observed_branch = ""
    try:
        prompt_characters = len(Path(state.prompt_path).read_text(encoding="utf-8"))
    except OSError:
        prompt_characters = None
    previous_reviewers: list[dict[str, object]] = []
    previous_runtime: dict[str, str] = {}
    previous_transient_action: str | None = None
    previous_recovery: dict[str, object] | None = None
    try:
        previous_file = json.loads(path.read_text(encoding="utf-8"))
        if previous_file.get("run_id") == state.run_id and isinstance(previous_file.get("transient_action"), str):
            previous_transient_action = redact_diagnostic(previous_file["transient_action"], limit=160)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        previous = load_projection(root, "live_status") or {}
    except EngineeringStorageError:
        # A live operation must not publish a filesystem-only state if the
        # canonical store is unavailable.
        raise
    reviewer_projection_active = state.phase == _REVIEWER_PROJECTION_PHASE
    if previous.get("run_id") == state.run_id:
        stored_reviewers = previous.get("reviewer_agents")
        if (
            reviewer_projection_active
            and reviewer_agents is None
            and isinstance(stored_reviewers, list)
        ):
            previous_reviewers = [item for item in stored_reviewers if isinstance(item, dict)]
        elif not reviewer_projection_active:
            # Once the review is wholly successful, retain its compact,
            # completed result as historical evidence for this live run. A
            # partial, failed or malformed projection is never carried into a
            # later lifecycle phase.
            previous_reviewers = _successful_reviewer_agents(stored_reviewers)
        if runtime_metadata is None and isinstance(previous.get("runtime_metadata"), dict):
            previous_runtime = {
                key: value[:120]
                for key, value in previous["runtime_metadata"].items()
                if key in {"runtime_provider", "model", "reasoning_profile", "configuration_profile", "codex_cli_installation_path"}
                and isinstance(value, str)
            }
        if isinstance(previous.get("workspace_recovery"), dict):
            previous_recovery = dict(previous["workspace_recovery"])
    safe_runtime = previous_runtime if runtime_metadata is None else {
        key: value[:120]
        for key, value in runtime_metadata.items()
        if key in {"runtime_provider", "model", "reasoning_profile", "configuration_profile", "codex_cli_installation_path"}
        and isinstance(value, str)
    }
    previous_progress = previous.get("workspace_progress") if previous.get("run_id") == state.run_id else None
    safe_progress = previous_progress if workspace_progress is None else workspace_progress
    if not isinstance(safe_progress, Mapping):
        safe_progress = {"modified": 0, "created": 0, "deleted": 0}
    safe_progress = {
        key: max(0, int(safe_progress.get(key, 0)))
        for key in ("modified", "created", "deleted", "codex_commands_executed")
        if isinstance(safe_progress.get(key, 0), int)
    }
    if previous_recovery is None:
        try:
            provider = GitProvider()
            head_result = provider.execute(checkout, "git", "rev-parse", "HEAD")
            status_result = provider.execute(checkout, "git", "status", "--porcelain", "--untracked-files=all")
            branches_result = provider.execute(checkout, "git", "for-each-ref", "--format=%(refname:short)", "refs/heads")
            if head_result.returncode or status_result.returncode or branches_result.returncode:
                raise OSError("Git recovery baseline is unavailable")
            baseline_head = head_result.stdout.strip()
            baseline_status = status_result.stdout.strip()
            branches = branches_result.stdout.splitlines()
            previous_recovery = {
                "baseline_branch": observed_branch,
                "baseline_head": baseline_head,
                "baseline_clean": not baseline_status,
                "preexisting_branches": [branch for branch in branches if branch],
            }
        except OSError:
            previous_recovery = {"baseline_clean": False}
    terminal_phase = state.phase in {"COMPLETE", "BLOCKED", "FAILED"}
    # SQLite recovery evidence is authoritative across host restarts. The
    # checkpoint ledger is retained only as a compatibility projection.
    try:
        from .provider_recovery import load_recovery_state
        recovery_record = load_recovery_state(root, state.run_id)
    except EngineeringStorageError:
        recovery_record = None
    if recovery_record is None:
        recovery_record = state.provider_recovery_attempts[0] if state.provider_recovery_attempts else None
    recovery_result = recovery_record.get("result") if isinstance(recovery_record, dict) else None
    recovery_state = recovery_record.get("state") if isinstance(recovery_record, dict) else None
    provider_recovery = {
        "state": (
            "RECOVERING" if recovery_state in {"RECOVERY_AVAILABLE", "RECOVERY_STARTING", "RECOVERY_IN_PROGRESS"} or recovery_result == "ACTIVE" else
            "RECOVERED" if recovery_state == "RECOVERED" or recovery_result == "RECOVERED" else
            "EXHAUSTED" if recovery_state == "EXHAUSTED" or recovery_result == "INTERRUPTED_AGAIN" else
            "NOT_APPLICABLE"
        ),
        "automatic_recovery_attempt": "1/1" if recovery_record else "0/1",
        "trigger": (recovery_record.get("classification") or "provider_turn_interrupted") if isinstance(recovery_record, dict) else None,
        "original_provider_invocation": (recovery_record.get("triggering_invocation_id") or recovery_record.get("original_invocation_id")) if isinstance(recovery_record, dict) else None,
        "replacement_provider_invocation": recovery_record.get("replacement_invocation_id") if isinstance(recovery_record, dict) else None,
        "result": recovery_result,
    }
    payload = {
        "run_id": state.run_id,
        "phase": state.phase,
        "current_action": redact_diagnostic(action),
        "objective": state.prompt_path,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
        "reconciliation_pr": state.reconciliation_pull_request,
        "pull_request": state.pull_request,
        "waiting_for_merge_since": state.waiting_for_merge_since,
        "repair_iteration": state.repair_iterations,
        "repository_state": "MERGED_RECONCILED" if state.phase == "COMPLETE" else state.phase if terminal_phase else "ACTIVE",
        "workspace_state": "WORKSPACE_READY" if state.phase == "COMPLETE" else "TERMINAL" if terminal_phase else "ACTIVE",
        "last_update": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 0,
        "prompt_characters": prompt_characters,
        "diagnostic": state.diagnostic,
        "resume_command": None if terminal_phase else f"engineering-execution-host {state.prompt_path} --run-id {state.run_id} --resume",
        "execution_mode": state.execution_mode,
        "target_repository": checkout.name if state.execution_mode == "GENESIS" else state.repository,
        "checkout_path": str(checkout),
        "active_branch": observed_branch or state.branch or "unavailable",
        # Reviewer progress is phase-scoped, while a fully successful review
        # remains compact historical evidence during the rest of this active
        # run. The dashboard renders that retained list as completed, never as
        # live work.
        "reviewer_agents": (
            reviewer_agents if reviewer_projection_active and reviewer_agents is not None
            else previous_reviewers
        ),
        "runtime_metadata": safe_runtime,
        "workspace_progress": safe_progress,
        # This is intentionally absent from a terminal receipt/report.  It is
        # a current dirty-worktree observation, not delivery evidence.
        "live_worktree_snapshot": None if terminal_phase else live_worktree_snapshot(checkout),
        "cumulative_activity": cumulative_activity(root, state.run_id),
        # A recovery baseline is captured once, after admission has proven the
        # workspace clean. It allows the emergency control to fail closed when
        # a run has commits, a pre-existing branch, or an unknown base.
        "workspace_recovery": previous_recovery,
        "provider_recovery": provider_recovery,
    }
    transient = None if state.terminal or terminal_phase else transient_action or previous_transient_action
    try:
        payload["execution_context"] = load_execution_context_snapshot(root, state.run_id)
        payload["forge_governance_handoff"] = load_forge_governance_handoff_snapshot(root, state.run_id)
    except EngineeringStorageError:
        payload["execution_context"] = None
        payload["forge_governance_handoff"] = None
    connection = open_storage(root)
    try:
        store_projection(connection, "live_status", payload)
    finally:
        connection.close()
    # This action title is intentionally absent from the stored projection.
    # It is a short-lived UI hint sourced from Codex reasoning metadata only.
    file_payload = dict(payload)
    if transient:
        file_payload["transient_action"] = transient
    descriptor, temporary = tempfile.mkstemp(prefix=".current.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(file_payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def print_live_status(root: Path) -> int:
    try:
        current = load_projection(root, "live_status")
    except EngineeringStorageError:
        print("Current engineering status is unavailable.")
        return 2
    if current is None:
        print("No active engineering status is available.")
        return 1
    print(
        f"Run:\n{current['run_id']}\n\nCurrent Phase:\n{current['phase']}\n\nImplementation PR:\n{current['implementation_pr']}\n\nRepair Iteration:\n{current['repair_iteration']}\n\nCurrent Action:\n{current['current_action']}\n\nElapsed:\n{current['elapsed_seconds']}s"
    )
    return 0


def write_runner_process(root: Path, run_id: str, process: Mapping[str, int] | None) -> None:
    """Atomically record only the Execution Host-owned Codex process group."""
    directory = root / ".engineering" / "status"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "runner_process.json"
    if process is None:
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = {}
        if recorded.get("run_id") == run_id:
            path.unlink(missing_ok=True)
        return
    pid, process_group = process.get("pid"), process.get("process_group")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(process_group, int) or process_group <= 0:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".runner-process.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "pid": pid, "process_group": process_group}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
