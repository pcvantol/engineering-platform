"""Fail-closed emergency stop and workspace rollback for one live run."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone

from .agent_state import StateStore
from .execution_lease import Lease, liveness, release
from .execution_timing import complete_active_phase
from .inbox_watcher import status as publish_watcher_status
from .live_status import write_runner_process
from .prompt_history import record_prompt_execution
from .providers import GitProvider, LocalProcessProvider
from .storage import EngineeringStorageError, load_projection, open_storage, record_emergency_recovery, record_execution_dismissal, store_projection


RUN_ID_PATTERN = re.compile(r"inbox-[a-z0-9-]{6,64}$")
BRANCH_PATTERN = re.compile(r"codex/[A-Za-z0-9._/-]+$")


class EmergencyRecoveryError(RuntimeError):
    """Raised when a destructive recovery cannot be proven safe."""


@dataclass(frozen=True)
class RecoveryPlan:
    run_id: str
    branch: str
    baseline_branch: str
    baseline_head: str
    process_group: int | None
    host_pid: int


def _git(root: Path, *args: str) -> str:
    result = GitProvider().execute(root, "git", *args)
    if result.returncode:
        raise EmergencyRecoveryError("De Git-werkmap kan niet veilig worden gecontroleerd.")
    return result.stdout.strip()


def _live(root: Path, run_id: str) -> dict[str, object]:
    live = load_projection(root, "live_status") or {}
    if live.get("run_id") != run_id:
        raise EmergencyRecoveryError("Deze uitvoering is niet de huidige uitvoering.")
    if liveness(root, run_id).get("state") != "LIVE":
        raise EmergencyRecoveryError("Deze uitvoering is niet meer actief; de noodactie is niet nodig.")
    if live.get("execution_mode") != "MANAGED":
        raise EmergencyRecoveryError("Noodherstel met rollback is alleen beschikbaar voor een beheerde uitvoering.")
    return live


def _runner(root: Path, run_id: str) -> tuple[int, int] | None:
    try:
        runner = json.loads((root / ".engineering" / "status" / "runner_process.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid, group = runner.get("pid"), runner.get("process_group")
    if runner.get("run_id") != run_id:
        return None
    if not isinstance(pid, int) or pid <= 0 or not isinstance(group, int) or group <= 0:
        raise EmergencyRecoveryError("De door deze uitvoering beheerde Codex-procesgroep is ongeldig.")
    return pid, group


def _host_pid(root: Path, run_id: str, *, central_database: Path | None = None) -> int:
    connection = open_storage(root) if central_database is None else sqlite3.connect(central_database.resolve(), isolation_level=None)
    try:
        row = connection.execute(
            "SELECT process_id FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    pid = row[0] if row else None
    if not isinstance(pid, int) or pid <= 0:
        raise EmergencyRecoveryError("De Execution Host van deze uitvoering is niet beschikbaar.")
    return pid


def _process_command(root: Path, pid: int) -> str:
    result = LocalProcessProvider().execute(root, ("ps", "-p", str(pid), "-o", "command="))
    return result.stdout.strip() if result.returncode == 0 else ""


def _plan(root: Path, run_id: str, *, central_database: Path | None = None) -> RecoveryPlan:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise EmergencyRecoveryError("De opgegeven run-ID is ongeldig.")
    live = _live(root, run_id)
    state = StateStore(root / ".engineering" / "engineering-runs", central_database=central_database, emit_local_projection=central_database is None).load(run_id)
    if state is None:
        raise EmergencyRecoveryError("De actieve uitvoering heeft geen canoniek checkpoint.")
    if any(
        pull_request is not None
        for pull_request in (
            state.pull_request, state.implementation_pull_request,
            state.finalization_pull_request, state.reconciliation_pull_request,
        )
    ):
        raise EmergencyRecoveryError("Er is al een pull request geregistreerd; de noodknop verwijdert geen pull requests of hun branches.")
    recovery = live.get("workspace_recovery")
    if not isinstance(recovery, dict):
        raise EmergencyRecoveryError("Deze uitvoering heeft geen veilige herstelbasis geregistreerd.")
    baseline_branch = recovery.get("baseline_branch")
    baseline_head = recovery.get("baseline_head")
    preexisting = recovery.get("preexisting_branches")
    if (
        recovery.get("baseline_clean") is not True
        or baseline_branch != "main"
        or not isinstance(baseline_head, str)
        or not re.fullmatch(r"[0-9a-f]{40}", baseline_head)
        or not isinstance(preexisting, list)
    ):
        raise EmergencyRecoveryError("De uitvoering begon niet vanaf een aantoonbaar schone main-basis.")
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "HEAD")
    if head != baseline_head:
        raise EmergencyRecoveryError("Er zijn commits gemaakt; de noodknop verwijdert geen gecommitteerd werk of pull requests.")
    if branch != "main" and (not BRANCH_PATTERN.fullmatch(branch) or branch in preexisting):
        raise EmergencyRecoveryError("De actieve branch is niet aantoonbaar door deze uitvoering aangemaakt.")
    runner = _runner(root, run_id)
    host_pid = _host_pid(root, run_id, central_database=central_database)
    runner_pid, group = runner if runner is not None else (None, None)
    runner_command = _process_command(root, runner_pid) if runner_pid is not None else ""
    host_command = _process_command(root, host_pid)
    if runner_pid is not None and (not runner_command or "codex" not in runner_command.casefold()):
        raise EmergencyRecoveryError("De geregistreerde Codex-runner is niet meer veilig identificeerbaar.")
    if not host_command or ("execution_host" not in host_command and "engineering-execution-host" not in host_command):
        raise EmergencyRecoveryError("De geregistreerde Execution Host is niet meer veilig identificeerbaar.")
    return RecoveryPlan(run_id, branch, baseline_branch, baseline_head, group, host_pid)


def preview(root: Path, run_id: object, *, central_database: Path | None = None) -> dict[str, object]:
    """Return a display-safe, non-mutating emergency recovery eligibility view."""
    if not isinstance(run_id, str):
        return {"available": False}
    try:
        plan = _plan(root, run_id, central_database=central_database)
    except (EmergencyRecoveryError, EngineeringStorageError, OSError):
        return {"available": False}
    return {
        "available": True,
        "run_id": plan.run_id,
        "branch": plan.branch,
        "baseline_branch": plan.baseline_branch,
        "rollback_changes": True,
        "remove_branch": plan.branch != plan.baseline_branch,
    }


def _stop(plan: RecoveryPlan, root: Path) -> None:
    try:
        if plan.process_group is not None:
            try:
                os.killpg(plan.process_group, signal.SIGTERM)
            except ProcessLookupError:
                # Codex can finish between preview and confirmation. The
                # leased Execution Host is still the authoritative stop target.
                pass
        os.kill(plan.host_pid, signal.SIGTERM)
    except ProcessLookupError as error:
        raise EmergencyRecoveryError("De uitvoering stopte al voordat de noodactie kon worden uitgevoerd.") from error
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _process_command(root, plan.host_pid):
            return
        time.sleep(0.1)
    raise EmergencyRecoveryError("De Execution Host reageert niet op de noodstop; er is niets teruggedraaid.")


def _release_lease(root: Path, run_id: str, *, central_database: Path | None = None) -> None:
    connection = open_storage(root) if central_database is None else sqlite3.connect(central_database.resolve(), isolation_level=None)
    try:
        row = connection.execute(
            "SELECT lease_id,host_identity,host_instance_id,acquired_at,last_heartbeat_at,expires_at,lease_state FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return
    release(root, Lease(row[0], run_id, row[1], row[2], row[3], row[4], row[5], row[6]), central_database=central_database)


def execute(root: Path, run_id: str, *, central_database: Path | None = None) -> dict[str, object]:
    """Stop exactly one verified host, then restore its clean local baseline."""
    plan = _plan(root, run_id, central_database=central_database)
    _stop(plan, root)
    _release_lease(root, run_id, central_database=central_database)
    _git(root, "restore", "--source", plan.baseline_head, "--staged", "--worktree", "--", ".")
    _git(root, "clean", "-fd", "--", ".")
    removed_branch: str | None = None
    if plan.branch != plan.baseline_branch:
        _git(root, "switch", plan.baseline_branch)
        _git(root, "branch", "-D", "--", plan.branch)
        removed_branch = plan.branch
    if _git(root, "rev-parse", "HEAD") != plan.baseline_head or _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise EmergencyRecoveryError("De noodstop is uitgevoerd, maar de werkmap kon niet volledig worden teruggedraaid.")
    write_runner_process(root, run_id, None)
    try:
        state = StateStore(root / ".engineering" / "engineering-runs", central_database=central_database, emit_local_projection=central_database is None).load(run_id)
    except (EngineeringStorageError, ValueError) as error:
        raise EmergencyRecoveryError("De annulering kon niet veilig als eindstatus worden vastgelegd.") from error
    if state is None:
        raise EmergencyRecoveryError("De actieve uitvoering heeft geen canoniek checkpoint.")
    cancelled_at = datetime.now(timezone.utc).isoformat()
    cancelled = replace(
        state, phase="FAILED", terminal=True, next_action="operator_emergency_rollback",
        terminal_condition="operator_emergency_rollback",
        diagnostic="De operator heeft deze uitvoering via de noodstop geannuleerd en de lokale werkmap teruggedraaid.",
    )
    StateStore(root / ".engineering" / "engineering-runs", central_database=central_database, emit_local_projection=central_database is None).save(cancelled)
    complete_active_phase(root, run_id, "TOTAL_EXECUTION", outcome="FAILED", central_database=central_database)
    record_prompt_execution(
        root, run_id=run_id, terminal_state="FAILED", prompt_title=Path(state.prompt_path).stem,
        executed_at=cancelled_at, target_branch=plan.branch,
        central_database=central_database,
    )
    record_execution_dismissal(
        root, run_id=run_id, terminal_state="FAILED", dismissed_at=cancelled_at,
        dismissed_by="dashboard_emergency_recovery",
        central_database=central_database,
    )
    record_emergency_recovery(
        root, run_id=run_id, cancelled_at=cancelled_at, rolled_back=True,
        removed_branch=removed_branch,
        central_database=central_database,
    )
    outcome = {"run_id": run_id, "stopped": True, "rolled_back": True, "removed_branch": removed_branch, "branch": plan.baseline_branch, "cancelled_at": cancelled_at}
    connection = open_storage(root) if central_database is None else sqlite3.connect(central_database.resolve(), isolation_level=None)
    try:
        store_projection(connection, f"emergency_recovery:{run_id}", outcome, classification="RECOVERY_EXPORT")
    finally:
        connection.close()
    publish_watcher_status(
        root, "JOB_FAILED", run_id=run_id, queued_jobs=0, queue_items=[],
        runner_phase="FAILED", diagnostic=cancelled.diagnostic,
        last_executed_title=Path(state.prompt_path).stem,
        last_executed_run=run_id, last_executed_phase="FAILED",
        current_action="Uitvoering geannuleerd via noodstop; werkmap teruggedraaid.",
    )
    return outcome
