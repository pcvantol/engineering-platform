from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import local_repository_binding, server, submission_service
from engineering_platform.agent_state import TransactionState
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher


class _PassingPreflight:
    timestamp = "2026-01-01T00:00:00+00:00"
    checks = ()


class _Runner:
    calls: list[tuple[Path, str | None, bool]] = []

    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState:
        self.calls.append((prompt_path, run_id, resume))
        return TransactionState(run_id or "inbox-missing", "fixture", str(prompt_path), "COMPLETE", terminal=True)


class ParityLifecycleDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name) / "central"
        server.initialize(self.data)
        self.roots: dict[str, Path] = {}
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            for project in ("alpha", "beta"):
                root = Path(self.temporary.name) / project
                (root / ".engineering-platform").mkdir(parents=True); self.roots[project] = root
                (root / ".engineering-platform" / "repository.json").write_text(json.dumps({
                    "schema_version": "1.0", "project": {"id": project, "authority_repository_id": project},
                    "repository": {"id": project, "role": "authority"},
                    "validation": {"kind": "command", "entrypoint": "python -m unittest"},
                    "requirements": {"host": {}, "tools": {}}, "integrations": {},
                }), encoding="utf-8")
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", (project, "{}", "ACTIVE", now, now))
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", (project, project, project, "authority", "{}", now, now))
                local_repository_binding.bind_local_repository(connection, project_id=project, repository_id=project, local_root=root, data_root=self.data)
        _Runner.calls = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _submission(self, project: str) -> str:
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            return submission_service.submit(connection, submission_service.SubmissionRequest(
                project, project, "canary", "HUMAN", "1", "Validate only.", "HTTP",
            )).submission_id

    def test_claims_one_submission_once_and_preserves_central_run_linkage(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_capability_preflight", return_value=_PassingPreflight()):
            first = dispatcher.dispatch(submission)
            second = dispatcher.dispatch(submission)
        self.assertEqual(first.run_id, second.run_id)
        self.assertFalse(first.duplicate_claim)
        self.assertTrue(second.duplicate_claim)
        self.assertEqual(first.state, "COMPLETE")
        self.assertEqual(len(_Runner.calls), 2)
        self.assertFalse(_Runner.calls[0][2])
        self.assertTrue(_Runner.calls[1][2])
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute("SELECT project_id,repository_id,run_id,state FROM ep_parity_lifecycle_dispatches").fetchone()
        self.assertEqual(row, ("alpha", "alpha", first.run_id, "COMPLETE"))
        self.assertTrue((self.data / "artifacts" / "projects" / "alpha" / "runs" / first.run_id / "submission.md").is_file())

    def test_context_never_crosses_project_binding(self) -> None:
        alpha, beta = self._submission("alpha"), self._submission("beta")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.inbox_watcher.execute_capability_preflight", return_value=_PassingPreflight()):
            alpha_receipt = dispatcher.dispatch(alpha)
            beta_receipt = dispatcher.dispatch(beta)
        self.assertNotEqual(alpha_receipt.run_id, beta_receipt.run_id)
        self.assertEqual(_Runner.calls[0][0].parents[2].resolve(), (self.data / "artifacts" / "projects" / "alpha").resolve())
        self.assertEqual(_Runner.calls[1][0].parents[2].resolve(), (self.data / "artifacts" / "projects" / "beta").resolve())
