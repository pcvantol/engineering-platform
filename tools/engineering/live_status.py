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


def write_live_status(
    root: Path,
    state: TransactionState,
    action: str,
    reviewer_agents: list[dict[str, object]] | None = None,
    runtime_metadata: Mapping[str, str] | None = None,
    workspace_progress: Mapping[str, int] | None = None,
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
    try:
        previous = load_projection(root, "live_status") or {}
    except EngineeringStorageError:
        # A live operation must not publish a filesystem-only state if the
        # canonical store is unavailable.
        raise
    if previous.get("run_id") == state.run_id:
        if reviewer_agents is None and isinstance(previous.get("reviewer_agents"), list):
            previous_reviewers = [item for item in previous["reviewer_agents"] if isinstance(item, dict)]
        if runtime_metadata is None and isinstance(previous.get("runtime_metadata"), dict):
            previous_runtime = {
                key: value[:120]
                for key, value in previous["runtime_metadata"].items()
                if key in {"runtime_provider", "model", "reasoning_profile", "configuration_profile"}
                and isinstance(value, str)
            }
    safe_runtime = previous_runtime if runtime_metadata is None else {
        key: value[:120]
        for key, value in runtime_metadata.items()
        if key in {"runtime_provider", "model", "reasoning_profile", "configuration_profile"}
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
    payload = {
        "run_id": state.run_id,
        "phase": state.phase,
        "current_action": redact_diagnostic(action),
        "objective": state.prompt_path,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
        "pull_request": state.pull_request,
        "waiting_for_merge_since": state.waiting_for_merge_since,
        "repair_iteration": state.repair_iterations,
        "repository_state": "MERGED_RECONCILED" if state.phase == "COMPLETE" else "ACTIVE",
        "workspace_state": "WORKSPACE_READY" if state.phase == "COMPLETE" else "ACTIVE",
        "last_update": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 0,
        "prompt_characters": prompt_characters,
        "diagnostic": state.diagnostic,
        "resume_command": f"engineering-execution-host {state.prompt_path} --run-id {state.run_id} --resume",
        "execution_mode": state.execution_mode,
        "target_repository": checkout.name if state.execution_mode == "GENESIS" else state.repository,
        "checkout_path": str(checkout),
        "active_branch": observed_branch or state.branch or "unavailable",
        "reviewer_agents": reviewer_agents if reviewer_agents is not None else previous_reviewers,
        "runtime_metadata": safe_runtime,
        "workspace_progress": safe_progress,
    }
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
    descriptor, temporary = tempfile.mkstemp(prefix=".current.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
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
