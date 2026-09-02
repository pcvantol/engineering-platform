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
import os
from pathlib import Path
import sqlite3
from typing import Callable, Protocol

from . import inbox_watcher
from .agent_state import StateStore, TransactionState
from .execution_host import EngineeringRunner
from .execution_repository import GhCliClient, SubprocessRepositoryClient
from .parity_context import HistoricalCandidate, ParityProjectContext, historical_candidate, project_context
from .platform_bootstrap import provision_runtime_workspace
from .providers import CodexCliProvider
from .execution_executor import CodexCliClient
from .storage import record_run_qualification_context, record_submission


TERMINAL_STATES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


class ParityLifecycleDispatchError(RuntimeError):
    """A stable failure at the Phase-P composition boundary."""


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
    return EngineeringRunner(
        repository_root,
        StateStore(repository_root / ".engineering" / "engineering-runs"),
        SubprocessRepositoryClient(),
        GhCliClient(),
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
            candidate = historical_candidate(connection, context=context, submission_id=submission_id)
            run_id = inbox_watcher._allocate_run_id()
            prompt = self._prompt_path(context, run_id)
            now = _utcnow()
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?, 'CLAIMED', ?, ?)",
                (run_id, context.project_id, now, now),
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
            connection.execute("UPDATE ep_parity_lifecycle_dispatches SET state=?,updated_at=? WHERE submission_id=? AND run_id=?", (state, now, submission_id, run_id))

    def dispatch(self, submission_id: str) -> DispatchReceipt:
        context, candidate, run_id, prompt, duplicate = self._claim(submission_id)
        if context.local_repository_root is None:
            raise ParityLifecycleDispatchError("LOCAL_BINDING_UNBOUND")
        repository_root = context.local_repository_root
        self._set_state(submission_id, run_id, "RUNNING")
        try:
            if not duplicate:
                self._persist_historical_input(repository_root, candidate, run_id, prompt)
            with _historical_admission_environment(repository_root):
                state = self.runner_factory(repository_root).run(prompt, run_id=run_id, resume=duplicate)
            terminal = state.phase if state.phase in TERMINAL_STATES else "RUNNING"
            self._set_state(submission_id, run_id, terminal)
            return DispatchReceipt(submission_id, context.project_id, context.repository_id, run_id, terminal, duplicate)
        except Exception:
            # A nonterminal checkpoint is deliberately resumable with the same
            # run ID; an uncheckpointed admission failure is visible as BLOCKED.
            self._set_state(submission_id, run_id, "BLOCKED")
            raise
