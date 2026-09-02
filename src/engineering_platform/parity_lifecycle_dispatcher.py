"""Phase-P composition of canonical submissions with the preserved local host.

This module intentionally owns only the CENTRAL claim and the translation of a
canonical submission into the existing Execution Host input.  Queue ordering,
predecessors, leases, provider invocation, recovery, finalization, reporting,
and telemetry remain owned by their historical implementations.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Protocol

from . import inbox_watcher
from . import submission_service
from .agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from .execution_errors import RunnerError
from .execution_host import EngineeringRunner
from .execution_repository import GhCliClient, SubprocessRepositoryClient
from .parity_context import HistoricalCandidate, ParityProjectContext, historical_candidate, project_context
from .platform_bootstrap import provision_runtime_workspace
from .providers import CodexCliProvider, GitProvider
from .execution_executor import CodexCliClient
from .execution_reporting import generate_terminal_report
from .storage import record_run_qualification_context, record_submission
from .prompt_history import prompt_history, record_terminal_report
from .report_analysis import analyze as analyze_terminal_report


TERMINAL_STATES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
OPERATOR_RESOLUTION_OPEN = "OPEN"
OPERATOR_RESOLUTION_DISMISSED = "DISMISSED"
OPERATOR_RESOLUTION_RETRIED = "RETRIED"
_LOCAL_PATH = re.compile(r"(?:/[^\s:]+)+")


class ParityLifecycleDispatchError(RuntimeError):
    """A stable failure at the Phase-P composition boundary."""


def dismiss_operator_gate(data_root: Path, *, project_id: str, run_id: str) -> dict[str, str]:
    """Explicitly release a failed CENTRAL run's project FIFO gate."""
    with sqlite3.connect(data_root / "engineering.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE ep_parity_lifecycle_dispatches
                SET operator_resolution=?,updated_at=?
                WHERE project_id=? AND run_id=? AND state IN ('BLOCKED','FAILED')
                  AND operator_resolution=?""",
            (OPERATOR_RESOLUTION_DISMISSED, _utcnow(), project_id, run_id, OPERATOR_RESOLUTION_OPEN),
        )
        if cursor.rowcount != 1:
            connection.execute("ROLLBACK")
            raise ParityLifecycleDispatchError("PROJECT_RUN_NOT_AWAITING_OPERATOR")
        connection.execute("COMMIT")
    return {"run_id": run_id, "handling_state": OPERATOR_RESOLUTION_DISMISSED}


def retry_operator_gate(data_root: Path, *, project_id: str, run_id: str) -> submission_service.SubmissionResult:
    """Create the only FIFO-successor allowed to resolve a failed run."""
    with sqlite3.connect(data_root / "engineering.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT d.repository_id,s.producer_id,s.producer_type,s.producer_version,
                      s.prompt,s.transport,s.correlation_id,s.mission_id,s.engineering_action_id,s.constraints
                FROM ep_parity_lifecycle_dispatches AS d
                JOIN ep_submissions AS s ON s.submission_id=d.submission_id
                WHERE d.project_id=? AND d.run_id=? AND d.state IN ('BLOCKED','FAILED')
                  AND d.operator_resolution=?""",
            (project_id, run_id, OPERATOR_RESOLUTION_OPEN),
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            raise ParityLifecycleDispatchError("PROJECT_RUN_NOT_AWAITING_OPERATOR")
        try:
            constraints = json.loads(str(row[9]))
        except (TypeError, ValueError, json.JSONDecodeError):
            connection.execute("ROLLBACK")
            raise ParityLifecycleDispatchError("RETRY_CONSTRAINTS_INVALID") from None
        if not isinstance(constraints, dict):
            connection.execute("ROLLBACK")
            raise ParityLifecycleDispatchError("RETRY_CONSTRAINTS_INVALID")
        request = submission_service.SubmissionRequest(
            project_id=project_id, repository_id=str(row[0]), producer_id=str(row[1]),
            producer_type=str(row[2]), producer_version=str(row[3]) if row[3] is not None else None,
            prompt=f"Retry-Of: {run_id}\n\n{str(row[4])}", transport=str(row[5]),
            correlation_id=str(row[6]) if row[6] is not None else None,
            mission_id=str(row[7]) if row[7] is not None else None,
            engineering_action_id=str(row[8]) if row[8] is not None else None, constraints=constraints,
        )
        result = submission_service.submit(connection, request)
        cursor = connection.execute(
            """UPDATE ep_parity_lifecycle_dispatches
                SET operator_resolution=?,resolution_submission_id=?,updated_at=?
                WHERE project_id=? AND run_id=? AND operator_resolution=?""",
            (OPERATOR_RESOLUTION_RETRIED, result.submission_id, _utcnow(), project_id, run_id, OPERATOR_RESOLUTION_OPEN),
        )
        if cursor.rowcount != 1:
            connection.execute("ROLLBACK")
            raise ParityLifecycleDispatchError("PROJECT_RETRY_RESOLUTION_CONFLICT")
        connection.execute("COMMIT")
    return result


class Runner(Protocol):
    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState: ...


RunnerFactory = Callable[[Path], Runner]


@dataclass(frozen=True)
class DispatchReceipt:
    submission_id: str
    project_id: str
    repository_id: str
    run_id: str
    state: str
    duplicate_claim: bool


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_runner(repository_root: Path) -> EngineeringRunner:
    """Construct the installed historical runner without a watcher or Agent."""
    remote = GitProvider().execute(repository_root, "git", "remote", "get-url", "origin")
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote.stdout.strip())
    repository = match.group(1) if remote.returncode == 0 and match else None
    return EngineeringRunner(
        repository_root,
        StateStore(repository_root / ".engineering" / "engineering-runs"),
        SubprocessRepositoryClient(),
        GhCliClient(repository=repository),
        CodexCliClient(CodexCliProvider()),
    )


@contextmanager
def _historical_admission_environment(repository_root: Path):
    """Keep the runner's existing persisted-admission guard in force."""
    keys = ("DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA", "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ[keys[0]] = str(inbox_watcher.ENGINEERING_STORAGE_SCHEMA_VERSION)
    os.environ[keys[1]] = str(repository_root)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ParityLifecycleDispatcher:
    """The one installed local writer allowed to claim a parity submission."""

    def __init__(self, data_root: Path, *, runner_factory: RunnerFactory = _default_runner) -> None:
        self.data_root = data_root.resolve()
        self.runner_factory = runner_factory

    def _prompt_path(self, context: ParityProjectContext, run_id: str) -> Path:
        # Prompts and mutable lifecycle evidence are installation-owned.  The
        # checked-out repository is an execution target, never a queue store.
        path = self.data_root / "artifacts" / "projects" / context.project_id / "runs" / run_id / "submission.md"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def _claim(self, submission_id: str) -> tuple[ParityProjectContext, HistoricalCandidate, str, Path, bool]:
        with sqlite3.connect(self.data_root / "engineering.db") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT project_id,repository_id,run_id,prompt_path FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if existing is not None:
                context = project_context(connection, data_root=self.data_root, project_id=str(existing[0]), repository_id=str(existing[1]))
                candidate = historical_candidate(connection, context=context, submission_id=submission_id)
                connection.execute("COMMIT")
                return context, candidate, str(existing[2]), Path(str(existing[3])), True
            row = connection.execute("SELECT project_id,repository_id FROM ep_submissions WHERE submission_id=?", (submission_id,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise ParityLifecycleDispatchError("UNKNOWN_SUBMISSION")
            context = project_context(connection, data_root=self.data_root, project_id=str(row[0]), repository_id=str(row[1]))
            active = connection.execute(
                """SELECT run_id FROM ep_parity_lifecycle_dispatches
                    WHERE project_id=? AND (
                        state IN ('CLAIMED','RUNNING')
                        OR (state IN ('BLOCKED','FAILED') AND operator_resolution='OPEN')
                        OR (operator_resolution='RETRIED' AND resolution_submission_id!=?)
                    ) LIMIT 1""",
                (context.project_id, submission_id),
            ).fetchone()
            if active is not None:
                connection.execute("ROLLBACK")
                raise ParityLifecycleDispatchError("PROJECT_RUN_ALREADY_ACTIVE")
            candidate = historical_candidate(connection, context=context, submission_id=submission_id)
            run_id = inbox_watcher._allocate_run_id()
            prompt = self._prompt_path(context, run_id)
            now = _utcnow()
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?, 'CLAIMED', ?, ?, ?)",
                (run_id, context.project_id, now, now, candidate.execution_mode),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?, 'CLAIMED', ?,?,?)",
                (submission_id, context.project_id, context.repository_id, run_id, str(prompt), now, now),
            )
            connection.execute("COMMIT")
            return context, candidate, run_id, prompt, False

    @staticmethod
    def _persist_historical_input(repository_root: Path, candidate: HistoricalCandidate, run_id: str, prompt: Path) -> None:
        """Use the existing watcher-owned storage and admission primitives."""
        # The preserved host preflight requires the standard runtime layout.
        # Reuse its product bootstrap rather than creating a P-A-specific one.
        provision_runtime_workspace(repository_root)
        prompt.write_text(candidate.prompt, encoding="utf-8")
        now = _utcnow()
        record_submission(
            repository_root, submission_id=candidate.submission_id,
            producer_id=candidate.producer_id, producer_type=candidate.producer_type,
            producer_version=candidate.producer_version, contract_version="1.0",
            prompt_content=candidate.prompt,
            prompt_metadata={"filename": prompt.name, "digest": candidate.prompt_digest, "title": "CENTRAL parity submission"},
            target_identity={"project_id": candidate.context.project_id, "repository_id": candidate.context.repository_id, "path": str(repository_root)},
            original_envelope=candidate.producer_envelope(), correlation_id=candidate.correlation_id,
            mission_id=candidate.mission_id, engineering_action_id=candidate.engineering_action_id,
            link_run_id=run_id, received_at=now,
        )
        record_run_qualification_context(
            repository_root, run_id=run_id, submission_id=candidate.submission_id,
            fresh_submission=True, retry_parent_run_id=None, resume_parent_run_id=None, recorded_at=now,
        )
        # These are the exact admission primitives used by the former watcher;
        # no CENTRAL scheduler or provider decision is introduced here.
        host = inbox_watcher.execute_host_preflight(repository_root, run_id=run_id)
        workspace = inbox_watcher.execute_workspace_preflight(repository_root, candidate.prompt, run_id=run_id)
        capability = inbox_watcher.execute_capability_preflight(repository_root, candidate.prompt, run_id=run_id)
        decision, _ = inbox_watcher._record_provider_free_admission(
            repository_root, run_id=run_id, submission_id=candidate.submission_id,
            execution_mode=candidate.execution_mode, results=(host, workspace, capability),
        )
        if decision != "PASS":
            raise ParityLifecycleDispatchError("HISTORICAL_ADMISSION_BLOCKED")

    def _set_state(self, submission_id: str, run_id: str, state: str) -> None:
        if state not in {"CLAIMED", "RUNNING", *TERMINAL_STATES}:
            raise ParityLifecycleDispatchError("INVALID_DISPATCH_STATE")
        with sqlite3.connect(self.data_root / "engineering.db") as connection:
            now = _utcnow()
            connection.execute("UPDATE ep_execution_runs SET state=?,updated_at=? WHERE run_id=?", (state, now, run_id))
            resolution = OPERATOR_RESOLUTION_OPEN if state in {"BLOCKED", "FAILED"} else "NONE"
            connection.execute(
                """UPDATE ep_parity_lifecycle_dispatches
                    SET state=?,operator_resolution=?,updated_at=?
                    WHERE submission_id=? AND run_id=?""",
                (state, resolution, now, submission_id, run_id),
            )

    @staticmethod
    def _terminal_history_exists(repository_root: Path, run_id: str) -> bool:
        return any(item.get("run_id") == run_id for item in prompt_history(repository_root))

    @classmethod
    def _project_terminal_history(cls, repository_root: Path, state: TransactionState,
                                  runner: object | None = None) -> None:
        """Preserve the terminal report and full historical Console projection.

        CENTRAL composes the historical runner directly, bypassing its CLI
        entrypoint. The entrypoint normally writes this evidence; retaining
        that post-run boundary here keeps a completed CENTRAL run visible in
        its project-scoped Operations Console.
        """
        if not state.terminal or cls._terminal_history_exists(repository_root, state.run_id):
            return
        report = generate_terminal_report(
            repository_root, state,
            getattr(runner, "platform_manifest", None),
            getattr(runner, "detected_codex_cli", None),
            getattr(runner, "reviewer_records", ()),
            getattr(getattr(runner, "agent", None), "last_runtime_metadata", None),
            getattr(getattr(runner, "agent", None), "last_execution_metadata", None),
        )
        record_terminal_report(repository_root, report)
        analyze_terminal_report(repository_root, state.run_id, report)

    def reconcile_terminal_history(self) -> None:
        """Backfill only missing Console rows for terminal CENTRAL runs."""
        with sqlite3.connect(self.data_root / "engineering.db") as connection:
            rows = connection.execute(
                "SELECT project_id,repository_id,run_id FROM ep_parity_lifecycle_dispatches "
                "WHERE state IN ('COMPLETE','BLOCKED','FAILED') ORDER BY claimed_at"
            ).fetchall()
            contexts = [
                (project_context(connection, data_root=self.data_root, project_id=str(project_id), repository_id=str(repository_id)), str(run_id))
                for project_id, repository_id, run_id in rows
            ]
        for context, run_id in contexts:
            if context.local_repository_root is None:
                continue
            try:
                state = StateStore(
                    context.local_repository_root / ".engineering" / "engineering-runs"
                ).load(run_id)
            except StateError:
                # CENTRAL terminal history can outlive the local retained
                # checkpoint. It is already durable history, not an active
                # recovery candidate; never let such a row prevent Server
                # startup or fresh project-scoped queue processing.
                continue
            self._project_terminal_history(context.local_repository_root, state)

    def _record_early_runner_failure(self, *, submission_id: str, context: ParityProjectContext,
                                     run_id: str, error: RunnerError) -> None:
        """Persist only the missing pre-checkpoint explanation, never a run state."""
        message = _LOCAL_PATH.sub("[LOCAL_PATH]", redact_diagnostic(str(error), limit=500))
        path = self.data_root / "artifacts" / "projects" / context.project_id / "runs" / run_id / "early-runner-failure.json"
        payload = {"submission_id": submission_id, "project_id": context.project_id,
                   "repository_id": context.repository_id, "run_id": run_id,
                   "failure_stage": "RUNNER_INITIALIZATION", "error_type": type(error).__name__,
                   "diagnostic_code": "RUNNER_EARLY_FAILURE", "message": message,
                   "recorded_at": _utcnow()}
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def dispatch(self, submission_id: str) -> DispatchReceipt:
        context, candidate, run_id, prompt, duplicate = self._claim(submission_id)
        if context.local_repository_root is None:
            raise ParityLifecycleDispatchError("LOCAL_BINDING_UNBOUND")
        repository_root = context.local_repository_root
        self._set_state(submission_id, run_id, "RUNNING")
        try:
            if not duplicate:
                self._persist_historical_input(repository_root, candidate, run_id, prompt)
            runner = self.runner_factory(repository_root)
            with _historical_admission_environment(repository_root):
                state = runner.run(
                    prompt, run_id=run_id, resume=duplicate,
                    owner_authorized=candidate.execution_mode == "MANAGED",
                )
            terminal = state.phase if state.phase in TERMINAL_STATES else "RUNNING"
            self._project_terminal_history(repository_root, state, runner)
            self._set_state(submission_id, run_id, terminal)
            return DispatchReceipt(submission_id, context.project_id, context.repository_id, run_id, terminal, duplicate)
        except RunnerError as error:
            self._record_early_runner_failure(submission_id=submission_id, context=context, run_id=run_id, error=error)
            self._set_state(submission_id, run_id, "BLOCKED")
            raise
        except Exception:
            # A nonterminal checkpoint is deliberately resumable with the same
            # run ID; an uncheckpointed admission failure is visible as BLOCKED.
            self._set_state(submission_id, run_id, "BLOCKED")
            raise
