"""Installed CENTRAL lifecycle-worker composition.

The worker deliberately has a very small authority: it observes CENTRAL's
durable submission records and asks :class:`ParityLifecycleDispatcher` to
continue one eligible item.  Claiming, run identity, historical admission,
recovery, provider invocation and finalization remain in that dispatcher and
the preserved EngineeringRunner.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from threading import Event, Lock, Thread
from typing import Callable, Protocol

from .parity_lifecycle_dispatcher import ParityLifecycleDispatcher


WORKER_RUNNING = "RUNNING"
WORKER_STOPPED = "STOPPED"
WORKER_DEGRADED = "DEGRADED"


class Dispatcher(Protocol):
    def dispatch(self, submission_id: str) -> object: ...


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
    """One serial, installation-owned observer for canonical submissions."""

    def __init__(self, data_root, *, dispatcher_factory: DispatcherFactory | None = None,
                 idle_seconds: float = 0.25, failure_seconds: float = 1.0) -> None:
        self.data_root = data_root.resolve()
        self._dispatcher_factory = dispatcher_factory or (lambda: ParityLifecycleDispatcher(self.data_root))
        self._idle_seconds = idle_seconds
        self._failure_seconds = failure_seconds
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
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
        """Discover candidates only; the dispatcher remains the claim authority.

        A serial creation-order scan preserves the former single-worker shape.
        It intentionally does not classify provider work, construct lanes, or
        reinterpret predecessor policy.  A persisted nonterminal claim is
        revisited after restart with its existing run identity.
        """
        with sqlite3.connect(self.data_root / "engineering.db") as connection:
            rows = connection.execute("""SELECT s.submission_id
                FROM ep_submissions s
                LEFT JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id
                WHERE s.state='QUEUED' AND s.admission='ADMITTED'
                  AND (d.submission_id IS NULL OR d.state IN ('CLAIMED','RUNNING'))
                ORDER BY s.created_at, s.submission_id""").fetchall()
        return [str(row[0]) for row in rows]

    def run_once(self) -> bool:
        candidates = self.eligible_submission_ids()
        if not candidates:
            return False
        submission_id = candidates[0]
        current = self.diagnostics()
        self._replace(observed=current.observed + 1, last_submission_id=submission_id, last_error=None)
        try:
            self._dispatcher_factory().dispatch(submission_id)
        except Exception as error:  # Dispatcher persists its own terminal/recovery boundary.
            current = self.diagnostics()
            self._replace(state=WORKER_DEGRADED, failures=current.failures + 1,
                          last_error=type(error).__name__)
            return True
        current = self.diagnostics()
        self._replace(state=WORKER_RUNNING, dispatched=current.dispatched + 1)
        return True

    def _loop(self) -> None:
        self._replace(state=WORKER_RUNNING)
        while not self._stop.is_set():
            self.run_once()
            # Even a successful nonterminal continuation gets a short yield:
            # recovery must never become a busy loop if the historical runner
            # deliberately returns a resumable checkpoint.
            self._stop.wait(self._failure_seconds if self.diagnostics().state == WORKER_DEGRADED else self._idle_seconds)
        self._replace(state=WORKER_STOPPED)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="engineering-platform-lifecycle-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._thread is None or not self._thread.is_alive():
            self._replace(state=WORKER_STOPPED)
