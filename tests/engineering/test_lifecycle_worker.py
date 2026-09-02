from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from engineering_platform import server, submission_service
from engineering_platform.lifecycle_worker import LifecycleWorker, WORKER_DEGRADED, WORKER_RUNNING, WORKER_STOPPED


class _Dispatcher:
    calls: list[str] = []
    fail = False

    def dispatch(self, submission_id: str) -> None:
        self.calls.append(submission_id)
        if self.fail:
            raise RuntimeError("controlled failure")


class LifecycleWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name) / "central"
        server.initialize(self.data)
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            for project in ("alpha", "beta"):
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", (project, "{}", "ACTIVE", "now", "now"))
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", (project, project, project, "authority", "{}", "now", "now"))
        _Dispatcher.calls, _Dispatcher.fail = [], False

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _submit(self, project: str) -> str:
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            return submission_service.submit(connection, submission_service.SubmissionRequest(project, project, "test", "HUMAN", None, "Do the bounded thing.", "HTTP")).submission_id

    def test_idle_queue_is_healthy_and_writes_nothing(self) -> None:
        worker = LifecycleWorker(self.data, dispatcher_factory=_Dispatcher)
        self.assertFalse(worker.run_once())
        self.assertEqual(worker.diagnostics().state, WORKER_STOPPED)
        self.assertEqual(_Dispatcher.calls, [])

    def test_serial_observation_delegates_claim_and_preserves_project_scope(self) -> None:
        alpha, beta = self._submit("alpha"), self._submit("beta")
        worker = LifecycleWorker(self.data, dispatcher_factory=_Dispatcher)
        self.assertTrue(worker.run_once())
        self.assertEqual(_Dispatcher.calls, [alpha])
        self.assertEqual(worker.eligible_submission_ids(), [alpha, beta])
        # A dispatcher claim, not the worker, removes a candidate from future observation.
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_execution_runs VALUES(?,?,?,?,?)", ("run-alpha", "alpha", "COMPLETE", "now", "now"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches VALUES(?,?,?,?,?,?,?,?)", (alpha, "alpha", "alpha", "run-alpha", "COMPLETE", "prompt", "now", "now"))
        self.assertEqual(worker.eligible_submission_ids(), [beta])

    def test_claimed_run_is_revisited_for_dispatcher_owned_restart_recovery(self) -> None:
        submission = self._submit("alpha")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_execution_runs VALUES(?,?,?,?,?)", ("stable-run", "alpha", "RUNNING", "now", "now"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches VALUES(?,?,?,?,?,?,?,?)", (submission, "alpha", "alpha", "stable-run", "RUNNING", "prompt", "now", "now"))
        worker = LifecycleWorker(self.data, dispatcher_factory=_Dispatcher)
        worker.run_once()
        self.assertEqual(_Dispatcher.calls, [submission])

    def test_dispatch_failure_is_bounded_and_worker_can_stop(self) -> None:
        self._submit("alpha")
        _Dispatcher.fail = True
        worker = LifecycleWorker(self.data, dispatcher_factory=_Dispatcher)
        self.assertTrue(worker.run_once())
        self.assertEqual(worker.diagnostics().state, WORKER_DEGRADED)
        worker.start(); worker.stop()
        self.assertEqual(worker.diagnostics().state, WORKER_STOPPED)

    def test_start_marks_worker_running(self) -> None:
        worker = LifecycleWorker(self.data, dispatcher_factory=_Dispatcher, idle_seconds=0.01)
        worker.start()
        import time
        time.sleep(0.02)
        self.assertEqual(worker.diagnostics().state, WORKER_RUNNING)
        worker.stop()

