"""Installed CENTRAL lifecycle-worker composition.

The worker deliberately has a very small authority: it observes CENTRAL's
durable submission records and asks :class:`ParityLifecycleDispatcher` to
continue one eligible item.  Claiming, run identity, historical admission,
recovery, provider invocation and finalization remain in that dispatcher and
the preserved EngineeringRunner.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import subprocess
import sys
from types import SimpleNamespace
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, Protocol

from .parity_lifecycle_dispatcher import ParityLifecycleDispatcher, ParityLifecycleDispatchError
from . import central_database
from . import parallel_action_admission, parallel_action_delivery
from .component_logging import component_logger, log_event
from .storage import sqlite_connection


WORKER_RUNNING = "RUNNING"
WORKER_STOPPED = "STOPPED"
WORKER_DEGRADED = "DEGRADED"
OPERATOR_MERGE_RESUME_SECONDS = 60.0


class Dispatcher(Protocol):
    def dispatch(self, submission_id: str) -> object: ...
    def reconcile_terminal_history(self) -> None: ...
    def reconcile_report_analyses(self) -> None: ...


DispatcherFactory = Callable[[], Dispatcher]


@dataclass(frozen=True)
class LifecycleWorkerDiagnostics:
    state: str
    observed: int
    dispatched: int
    failures: int
    last_submission_id: str | None
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "observed": self.observed,
            "dispatched": self.dispatched,
            "failures": self.failures,
            "last_submission_id": self.last_submission_id,
            "last_error": self.last_error,
        }


class LifecycleWorker:
    """Installation observer with legacy project FIFO and bounded PA-E2 delivery."""

    def __init__(self, data_root, *, dispatcher_factory: DispatcherFactory | None = None,
                 idle_seconds: float = 0.25, failure_seconds: float = 1.0) -> None:
        self.data_root = data_root.resolve()
        self._logger = component_logger(
            self.data_root,
            "lifecycle_worker",
            central_database=central_database.path(self.data_root),
        )
        self._default_dispatcher = dispatcher_factory is None
        self._dispatcher_factory = dispatcher_factory or (lambda: ParityLifecycleDispatcher(self.data_root))
        self._idle_seconds = idle_seconds
        self._failure_seconds = failure_seconds
        self._stop = Event()
        self._ready = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._inflight: set[str] = set()
        self._next_merge_resume_at: dict[str, float] = {}
        self._diagnostics = LifecycleWorkerDiagnostics(WORKER_STOPPED, 0, 0, 0, None, None)

    def diagnostics(self) -> LifecycleWorkerDiagnostics:
        with self._lock:
            return self._diagnostics

    def _replace(self, **changes: object) -> None:
        with self._lock:
            values = self._diagnostics.to_dict()
            values.update(changes)
            self._diagnostics = LifecycleWorkerDiagnostics(**values)  # type: ignore[arg-type]

    def eligible_submission_ids(self) -> list[str]:
        """Return ready legacy heads and independent PA-E2 Actions in FIFO order."""
        with sqlite_connection(central_database.path(self.data_root)) as connection:
            rows = connection.execute("""SELECT s.submission_id,s.project_id,s.repository_id,
                                               d.state,d.claimed_at,s.created_at,d.run_id,
                                               i.policy_digest
                FROM ep_submissions s
                LEFT JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id
                LEFT JOIN ep_parallel_action_submission_links l ON l.submission_id=s.submission_id
                LEFT JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
                WHERE s.state='QUEUED' AND s.admission='ADMITTED'
                  AND (
                    d.submission_id IS NULL OR d.state IN ('CLAIMED','RUNNING')
                    OR (
                      d.state='BLOCKED' AND d.operator_resolution='OPEN'
                      AND EXISTS (
                        SELECT 1 FROM engineering_transactions wait_state
                        WHERE wait_state.run_id=d.run_id
                          AND wait_state.phase='WAIT_FOR_OPERATOR_MERGE'
                          AND COALESCE(json_extract(wait_state.payload, '$.terminal'), 0) IN (0, 'false')
                      )
                    )
                  )
                  AND (i.policy_digest=? OR NOT EXISTS (
                    SELECT 1 FROM ep_parity_lifecycle_dispatches prior
                    WHERE prior.project_id=s.project_id AND (
                      (prior.state IN ('CLAIMED','RUNNING') AND prior.submission_id!=s.submission_id)
                      OR (prior.state IN ('BLOCKED','FAILED') AND prior.operator_resolution='OPEN'
                          AND prior.submission_id!=s.submission_id)
                      OR (prior.operator_resolution='RETRIED' AND prior.resolution_submission_id!=s.submission_id
                          AND NOT EXISTS (SELECT 1 FROM ep_parity_lifecycle_dispatches retry
                              WHERE retry.submission_id=prior.resolution_submission_id
                                AND retry.state IN ('COMPLETE','BLOCKED','FAILED')
                                AND retry.operator_resolution IN ('NONE','RETRIED','DISMISSED')))
                    )
                  ))
                ORDER BY
                  CASE WHEN d.state IN ('CLAIMED','RUNNING') THEN 0 ELSE 1 END,
                  COALESCE(d.claimed_at,s.created_at),s.created_at,s.submission_id""",
                (parallel_action_admission.SUPPORTED_PA_E2_POLICY_DIGEST,)).fetchall()
            candidates: list[str] = []
            project_modes: dict[str, str] = {}
            for submission_id, project_id, repository_id, _state, _claimed_at, _created_at, run_id, policy in rows:
                project = str(project_id)
                pa_e2 = policy == parallel_action_admission.SUPPORTED_PA_E2_POLICY_DIGEST
                selected_mode = project_modes.get(project)
                if selected_mode == "legacy" or (selected_mode == "pa-e2" and not pa_e2):
                    continue
                try:
                    decision = parallel_action_admission.linked_submission_decision(
                        connection, submission_id=str(submission_id),
                        continuation_run_id=str(run_id) if run_id is not None else None,
                    )
                except parallel_action_admission.ParallelAdmissionError:
                    continue
                if decision is not None and decision["state"] != "DEPENDENCY_ELIGIBLE":
                    continue
                if pa_e2:
                    try:
                        gate = parallel_action_delivery.gate(
                            connection, data_root=self.data_root, project_id=project,
                            repository_id=str(repository_id),
                            run_id=str(run_id) if run_id is not None else None,
                        )
                    except (parallel_action_delivery.DeliveryScopeError, ValueError, OSError):
                        continue
                    if gate.state != "READY":
                        continue
                    project_modes[project] = "pa-e2"
                else:
                    project_modes[project] = "legacy"
                candidates.append(str(submission_id))
        return candidates

    def _dispatch(self, submission_id: str) -> None:
        try:
            if self._default_dispatcher:
                with sqlite_connection(central_database.path(self.data_root)) as connection:
                    pa_e2 = (parallel_action_delivery.policy_for_submission(connection, submission_id)
                             == parallel_action_admission.SUPPORTED_PA_E2_POLICY_DIGEST)
            else:
                pa_e2 = False
            if pa_e2:
                completed = subprocess.run(
                    (sys.executable, "-m", "engineering_platform.pa_e2_dispatch",
                     str(self.data_root), submission_id),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
                )
                if completed.returncode:
                    raise ParityLifecycleDispatchError("PA_E2_DISPATCH_FAILED")
                payload = json.loads(completed.stdout.splitlines()[-1])
                if "error" in payload:
                    raise ParityLifecycleDispatchError(str(payload["error"]))
                receipt = SimpleNamespace(**payload)
            else:
                receipt = self._dispatcher_factory().dispatch(submission_id)
        except ParityLifecycleDispatchError as error:
            if str(error) in {"WAITING_RESOURCE", "WAITING_CAPACITY", "PROJECT_RUN_ALREADY_ACTIVE"}:
                with self._lock:
                    self._next_merge_resume_at.pop(submission_id, None)
                return
            current = self.diagnostics()
            self._replace(state=WORKER_DEGRADED, failures=current.failures + 1,
                          last_error=type(error).__name__)
            log_event(self._logger, logging.ERROR, "lifecycle_dispatch_failed",
                      diagnostic=type(error).__name__, context={"submission_id": submission_id})
        except Exception as error:  # Dispatcher persists its own terminal/recovery boundary.
            current = self.diagnostics()
            self._replace(state=WORKER_DEGRADED, failures=current.failures + 1,
                          last_error=type(error).__name__)
            log_event(
                self._logger,
                logging.ERROR,
                "lifecycle_dispatch_failed",
                diagnostic=type(error).__name__,
                context={"submission_id": submission_id},
            )
        else:
            current = self.diagnostics()
            self._replace(state=WORKER_RUNNING, dispatched=current.dispatched + 1)
            run_id = getattr(receipt, "run_id", None)
            state = getattr(receipt, "state", None)
            log_event(
                self._logger,
                logging.INFO,
                "lifecycle_dispatch_finished",
                run_id=run_id if isinstance(run_id, str) else None,
                context={
                    "submission_id": submission_id,
                    "dispatch_state": state if isinstance(state, str) else "UNAVAILABLE",
                },
            )
        finally:
            with self._lock:
                self._inflight.discard(submission_id)

    def run_once(self) -> bool:
        with self._lock:
            inflight = frozenset(self._inflight)
        with sqlite_connection(central_database.path(self.data_root)) as connection:
            pa_e2_inflight = sum(
                parallel_action_delivery.policy_for_submission(connection, submission_id)
                == parallel_action_admission.SUPPORTED_PA_E2_POLICY_DIGEST
                for submission_id in inflight
            )
        pa_e2_budget = max(0, parallel_action_delivery.MAX_PROVIDER_INVOCATIONS - pa_e2_inflight)
        now = monotonic()
        candidates = [
            submission_id for submission_id in self.eligible_submission_ids()
            if submission_id not in inflight
            and now >= self._next_merge_resume_at.get(submission_id, 0.0)
        ]
        if not candidates:
            return False
        selected = []
        with sqlite_connection(central_database.path(self.data_root)) as connection:
            for submission_id in candidates:
                pa_e2 = (parallel_action_delivery.policy_for_submission(connection, submission_id)
                         == parallel_action_admission.SUPPORTED_PA_E2_POLICY_DIGEST)
                if pa_e2:
                    if pa_e2_budget == 0:
                        continue
                    pa_e2_budget -= 1
                selected.append(submission_id)
        if not selected:
            return False
        for submission_id in selected:
            current = self.diagnostics()
            self._replace(observed=current.observed + 1, last_submission_id=submission_id, last_error=None)
            log_event(
                self._logger,
                logging.INFO,
                "lifecycle_submission_selected",
                context={"submission_id": submission_id},
            )
            with self._lock:
                self._inflight.add(submission_id)
                # A nonterminal merge wait returns quickly after its one
                # authoritative remote poll.  Keep that same canonical run
                # resumable, but never race it with another local lease
                # acquisition before the next bounded poll window.
                self._next_merge_resume_at[submission_id] = now + OPERATOR_MERGE_RESUME_SECONDS
            Thread(
                target=self._dispatch,
                args=(submission_id,),
                name=f"engineering-platform-lifecycle-{submission_id[:12]}",
                daemon=True,
            ).start()
        return True

    def _loop(self) -> None:
        self._replace(state=WORKER_RUNNING)
        self._ready.set()
        while not self._stop.is_set():
            # CENTRAL is the sole operational store.  Its maintenance routine
            # is interval-bound and skips every active lifecycle.
            central_database.run_periodic_maintenance(self.data_root)
            self.run_once()
            # Even a successful nonterminal continuation gets a short yield:
            # recovery must never become a busy loop if the historical runner
            # deliberately returns a resumable checkpoint.
            self._stop.wait(self._failure_seconds if self.diagnostics().state == WORKER_DEGRADED else self._idle_seconds)
        self._replace(state=WORKER_STOPPED)

    def _reconcile_terminal_history(self) -> None:
        """Restore additive terminal projections without delaying readiness."""
        dispatcher = self._dispatcher_factory()
        reconcile = getattr(dispatcher, "reconcile_terminal_history", None)
        if not callable(reconcile):
            return
        try:
            reconcile()
        except Exception as error:
            # Terminal-history projection is additive only. A stale retained
            # row must not prevent the HTTP Server from accepting fresh
            # CENTRAL submissions or prevent the worker from servicing a
            # project queue.
            current = self.diagnostics()
            self._replace(failures=current.failures + 1, last_error=type(error).__name__)
            return
        # Advisory analyses are independent, redacted derived artifacts.  A
        # missing pre-feature artifact must not require re-running a terminal
        # lifecycle or delay HTTP Server readiness.
        reconcile_analyses = getattr(dispatcher, "reconcile_report_analyses", None)
        if not callable(reconcile_analyses):
            return
        try:
            reconcile_analyses()
        except Exception as error:
            current = self.diagnostics()
            self._replace(failures=current.failures + 1, last_error=type(error).__name__)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = Thread(target=self._loop, name="engineering-platform-lifecycle-worker", daemon=True)
        self._thread.start()
        # CENTRAL owns claims; the preserved Console owns terminal evidence.
        # Historical projection is additive, so it must never make Server
        # readiness depend on an old report or a retained stale row.
        Thread(
            target=self._reconcile_terminal_history,
            name="engineering-platform-terminal-history-reconciliation",
            daemon=True,
        ).start()

    def wait_until_running(self, timeout: float = 5.0) -> bool:
        """Wait for the child loop's real readiness transition.

        Server-owned producers depend on lifecycle admission being available;
        this is a composition boundary, not a timing heuristic.
        """
        return self._ready.wait(timeout) and self.diagnostics().state == WORKER_RUNNING

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._thread is None or not self._thread.is_alive():
            self._replace(state=WORKER_STOPPED)
