from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import server, submission_service


class CanonicalSubmissionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("djconnect", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("djconnect", "djconnect", "djconnect", "authority", "{}", now, now))
            self.credential = submission_service.issue_consumer_credential(connection, consumer_id="cli", project_id="djconnect")["credential"]

    def tearDown(self) -> None:
        server.stop(self.root)
        self.temporary.cleanup()

    def payload(self, key: str = "same") -> dict[str, object]:
        return {"repository_id": "djconnect", "producer": {"id": "test", "type": "HUMAN", "version": "1"}, "prompt": "Validate only; do not execute.", "idempotency_key": key}

    def test_service_preserves_cross_transport_idempotency_and_history(self) -> None:
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            http = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload(), transport="HTTP"))
            cli = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload(), transport="CLI"))
            path = self.root / "inbox.json"
            path.write_text(json.dumps({"project_id": "djconnect", "submission": self.payload()}), encoding="utf-8")
            legacy = submission_service.submit_legacy_file(connection, path)
            self.assertFalse(http.duplicate)
            self.assertTrue(cli.duplicate)
            self.assertTrue(legacy.duplicate)
            self.assertEqual(http.submission_id, cli.submission_id)
            self.assertEqual(connection.execute("SELECT count(*) FROM ep_submission_prompt_history").fetchone()[0], 1)

    def test_http_auth_scope_and_acceptance(self) -> None:
        server.start(self.root)
        request = Request(f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions", data=json.dumps(self.payload("http")).encode(), headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:  # nosec B310
            accepted = json.loads(response.read())
        self.assertEqual(accepted["state"], "QUEUED")
        wrong = Request(f"http://127.0.0.1:{self.port}/v1/projects/other/submissions", data=b"{}", headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as rejected:
            urlopen(wrong)  # nosec B310
        self.assertEqual(rejected.exception.code, 401)

    def test_authenticated_producer_readback_is_exactly_correlated_and_terminal_evidence_backed(self) -> None:
        server.start(self.root)
        payload = self.payload("readback")
        payload.update({
            "correlation_id": "forge-correlation-1", "mission_id": "mission-1",
            "engineering_action_id": "action-1",
        })
        submit = Request(
            f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST",
        )
        with urlopen(submit) as response:  # nosec B310
            accepted = json.loads(response.read())
        submission_id = accepted["submission_id"]
        endpoint = f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions/{submission_id}"
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            initial = json.loads(response.read())
        self.assertEqual(initial["contract_version"], "1.0")
        self.assertEqual(initial["correlation"], {
            "correlation_id": "forge-correlation-1", "mission_id": "mission-1", "engineering_action_id": "action-1",
        })
        self.assertIsNone(initial["run"])
        self.assertEqual(initial["result"], {"state": "NOT_STARTED", "terminal": False})

        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", ("run-readback", "djconnect", "COMPLETE", "now", "now", "MANAGED"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)", (submission_id, "djconnect", "djconnect", "run-readback", "COMPLETE", "/private/prompt", "now", "now", "NONE"))
            connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("run-readback", json.dumps({"phase": "COMPLETE", "terminal": True}), "COMPLETE", "now"))
            connection.execute("INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)", ("run-readback", "COMPLETE", "safe", "now", None, "/private/report", "now"))
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            terminal = json.loads(response.read())
        self.assertEqual(terminal["run"]["run_id"], "run-readback")
        self.assertEqual(terminal["result"], {"state": "COMPLETE", "terminal": True})
        self.assertEqual(terminal["evidence"]["terminal_checkpoint"], "central-transaction:run-readback")
        self.assertEqual(terminal["evidence"]["terminal_report"], "central-report:run-readback")
        self.assertNotIn("/private", json.dumps(terminal))

        # The projection is CENTRAL state, not a process-local cache: a Server
        # restart preserves the exact submission/run/evidence correlation.
        server.stop(self.root)
        server.start(self.root)
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            recovered = json.loads(response.read())
        self.assertEqual(recovered, terminal)

        with self.assertRaises(HTTPError) as unauthenticated:
            urlopen(endpoint)  # nosec B310
        self.assertEqual(unauthenticated.exception.code, 401)
        with self.assertRaises(HTTPError) as cross_project:
            urlopen(Request(
                f"http://127.0.0.1:{self.port}/v1/projects/other/submissions/{submission_id}",
                headers={"Authorization": f"Bearer {self.credential}"},
            ))  # nosec B310
        self.assertEqual(cross_project.exception.code, 401)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("other", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("other", "other", "other", "authority", "{}", now, now))
            other_credential = submission_service.issue_consumer_credential(connection, consumer_id="other-client", project_id="other")["credential"]
        with self.assertRaises(HTTPError) as isolated:
            urlopen(Request(
                f"http://127.0.0.1:{self.port}/v1/projects/other/submissions/{submission_id}",
                headers={"Authorization": f"Bearer {other_credential}"},
            ))  # nosec B310
        self.assertEqual(isolated.exception.code, 404)
