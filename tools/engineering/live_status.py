"""Atomic local status projection for the foreground engineering runner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .agent_state import TransactionState, redact_diagnostic


def write_live_status(root: Path, state: TransactionState, action: str) -> Path:
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
        observed_branch = subprocess.run(
            ("git", "-C", str(checkout), "branch", "--show-current"),
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
    except OSError:
        observed_branch = ""
    try:
        prompt_characters = len(Path(state.prompt_path).read_text(encoding="utf-8"))
    except OSError:
        prompt_characters = None
    payload = {
        "run_id": state.run_id,
        "phase": state.phase,
        "current_action": redact_diagnostic(action),
        "objective": state.prompt_path,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
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
    }
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
    path = root / ".engineering" / "status" / "current.json"
    if not path.is_file():
        print("No active engineering status is available.")
        return 1
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("Current engineering status is unavailable.")
        return 2
    print(
        f"Run:\n{current['run_id']}\n\nCurrent Phase:\n{current['phase']}\n\nImplementation PR:\n{current['implementation_pr']}\n\nRepair Iteration:\n{current['repair_iteration']}\n\nCurrent Action:\n{current['current_action']}\n\nElapsed:\n{current['elapsed_seconds']}s"
    )
    return 0
