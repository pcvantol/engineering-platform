from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from engineering_platform import local_repository_binding, parity_context, producer, server, submission_service


class ParityContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.data = Path(self.temp.name) / "central"
        server.initialize(self.data)
        self.roots: dict[str, Path] = {}
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            for identity in ("alpha", "beta"):
                root = Path(self.temp.name) / identity; (root / ".engineering-platform").mkdir(parents=True)
                (root / ".engineering-platform" / "repository.json").write_text(json.dumps({
                    "schema_version": "1.0", "project": {"id": identity, "authority_repository_id": identity},
                    "repository": {"id": identity, "role": "authority"},
                    "validation": {"kind": "command", "entrypoint": "python -m unittest"}, "requirements": {"host": {}, "tools": {}}, "integrations": {},
                }), encoding="utf-8"); self.roots[identity] = root
                now = "2026-01-01T00:00:00+00:00"
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", (identity, json.dumps({"authority_repository_id": identity}), "ACTIVE", now, now))
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", (identity, identity, identity, "authority", "{}", now, now))
                local_repository_binding.bind_local_repository(connection, project_id=identity, repository_id=identity, local_root=root, data_root=self.data)

    def tearDown(self) -> None: self.temp.cleanup()

    def _submit(self, project: str, prompt: str = "Validate only.") -> str:
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            result = submission_service.submit(connection, submission_service.SubmissionRequest(project, project, "test", "HUMAN", "1", prompt, "HTTP"))
            return result.submission_id

    def test_context_candidate_and_console_data_are_project_scoped(self) -> None:
        alpha, beta = self._submit("alpha"), self._submit("beta")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            context = parity_context.project_context(connection, data_root=self.data, project_id="alpha", repository_id="alpha")
            candidate = parity_context.historical_candidate(connection, context=context, submission_id=alpha)
            self.assertEqual(candidate.execution_mode, "MANAGED")
            self.assertEqual(json.loads(candidate.producer_envelope())["submission"]["id"], alpha)
            self.assertEqual(producer.parse_producer_submission(candidate.producer_envelope()).submission_id, alpha)
            store = parity_context.ParityProjectStore(connection, context)
            self.assertEqual([item["submission_id"] for item in store.dashboard_projection()["queue"]], [alpha])
            console_queue = store.console_queue_projection()
            self.assertEqual(console_queue["queue_depth"], 1)
            self.assertEqual([item["submission_id"] for item in console_queue["queue_items"]], [alpha])
            self.assertEqual(console_queue["queue_items"][0]["queue_source"], "CENTRAL")
            with self.assertRaisesRegex(parity_context.ParityContextError, "SUBMISSION_OUTSIDE_CONTEXT"):
                parity_context.historical_candidate(connection, context=context, submission_id=beta)
            with self.assertRaisesRegex(parity_context.ParityContextError, "PROJECT_REPOSITORY_MISMATCH"):
                store.validate_action_scope(repository_id="beta")

    def test_genesis_is_transported_and_read_context_needs_no_root(self) -> None:
        # Genesis metadata is deliberately line-oriented at the canonical
        # admission boundary.  Keep this parity test on the valid, explicit
        # public contract rather than relying on the retired multiline form.
        submission = self._submit("alpha", "Execution Mode: Genesis\nTarget repository: /tmp/target\n")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            local_repository_binding.unbind_local_repository(connection, project_id="alpha", repository_id="alpha")
            readonly = parity_context.project_context(connection, data_root=self.data, project_id="alpha", repository_id="alpha", require_local_root=False)
            self.assertIsNone(readonly.local_repository_root)
            candidate = parity_context.historical_candidate(connection, context=readonly, submission_id=submission)
            self.assertEqual(candidate.execution_mode, "GENESIS")
            with self.assertRaisesRegex(local_repository_binding.LocalRepositoryBindingError, "LOCAL_BINDING_UNBOUND"):
                parity_context.project_context(connection, data_root=self.data, project_id="alpha", repository_id="alpha")
