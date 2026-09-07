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
from engineering_platform.agent_state import TransactionState


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
        payload.update({"producer": {"id": "forge", "type": "FORGE", "version": "1.0"},
                        "correlation_id": "forge-correlation-1", "mission_id": "mission-1",
                        "engineering_action_id": "action-1", "constraints": {"forge_execution": {
                            "contract_version": "1.0", "host_id": "engineering-platform",
                            "repository_id": "djconnect", "correlation_id": "forge-correlation-1",
                            "mission_id": "mission-1", "mission_revision": "1", "intent_id": "intent-1",
                            "intent_revision": "1", "action_id": "action-1",
                            "runtime_prompt": {"id": "prompt-1", "content_digest": "sha256:" + "a" * 64},
                            "retry_of_correlation_id": None,
                        }}})
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
        self.assertEqual(initial["contract_version"], "1.1")
        self.assertEqual(initial["correlation"], {
            "correlation_id": "forge-correlation-1", "mission_id": "mission-1", "engineering_action_id": "action-1",
        })
        self.assertIsNone(initial["run"])
        self.assertEqual(initial["result"], {"outcome": "NOT_STARTED", "terminal": False, "delivery_qualified": False})
        self.assertEqual(initial["provenance"]["status"], "PERSISTED")

        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", ("run-readback", "djconnect", "COMPLETE", "now", "now", "MANAGED"))
            connection.execute("INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("run-readback", "2026-01-01", "now", "now", "now", 0, 0, "COMPLETE", None, None, None, "MANAGED", "djconnect", "djconnect", "test"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)", (submission_id, "djconnect", "djconnect", "run-readback", "COMPLETE", "/private/prompt", "now", "now", "NONE"))
            checkpoint = TransactionState(run_id="run-readback", repository="djconnect", prompt_path="prompt", phase="COMPLETE", terminal=True, action_intent="VALIDATION_ONLY")
            connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("run-readback", json.dumps(checkpoint.to_dict()), "COMPLETE", "now"))
            connection.execute("INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)", ("run-readback", "COMPLETE", "safe", "now", None, "/private/report", "now"))
        artifact_id = submission_service.write_terminal_evidence(self.root, repository_root=self.root, run_id="run-readback")
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            direct = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submission_id)
            self.assertIsNotNone(direct)
            stored_artifact = submission_service.producer_evidence_artifact(connection, project_id="djconnect", artifact_id=artifact_id)
            self.assertIsNotNone(stored_artifact)
            self.assertEqual(json.loads(stored_artifact or b"{}")['run']['id'], "run-readback")
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            terminal = json.loads(response.read())
        self.assertEqual(terminal["run"]["id"], "run-readback")
        self.assertEqual(terminal["result"], {"outcome": "COMPLETE", "terminal": True, "delivery_qualified": True})
        self.assertEqual(terminal["evidence"]["status"], "AVAILABLE")
        self.assertEqual(terminal["evidence"]["terminal_artifact"]["id"], artifact_id)
        self.assertEqual(terminal["evidence"]["repository"]["revision"], None)
        self.assertNotIn("/private", json.dumps(terminal))
        with urlopen(Request(f"http://127.0.0.1:{self.port}/v1/projects/djconnect/artifacts/{artifact_id}", headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            artifact = json.loads(response.read())
        self.assertEqual(artifact["submission"]["id"], submission_id)
        self.assertEqual(artifact["run"]["id"], "run-readback")

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
        artifact_path = self.root / "artifacts" / "projects" / "djconnect" / "runs" / "run-readback" / "terminal-evidence-v1.json"
        artifact_path.write_text("{}\n", encoding="utf-8")
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            corrupt = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submission_id)
            self.assertEqual(corrupt["evidence"]["status"], "CORRUPT")  # type: ignore[index]
            self.assertFalse(corrupt["result"]["delivery_qualified"])  # type: ignore[index]
            self.assertIsNone(submission_service.producer_evidence_artifact(connection, project_id="djconnect", artifact_id=artifact_id))

    def test_forge_provenance_is_required_and_part_of_idempotency_identity(self) -> None:
        payload = self.payload("forge-replay")
        payload.update({"producer": {"id": "forge", "type": "FORGE", "version": "1.0"},
                        "correlation_id": "corr-1", "mission_id": "mission-1", "engineering_action_id": "action-1",
                        "constraints": {"forge_execution": {"contract_version": "1.0", "host_id": "engineering-platform",
                            "repository_id": "djconnect", "correlation_id": "corr-1", "mission_id": "mission-1",
                            "mission_revision": "1", "intent_id": "intent-1", "intent_revision": "1", "action_id": "action-1",
                            "runtime_prompt": {"id": "prompt-1", "content_digest": "sha256:" + "b" * 64},
                            "retry_of_correlation_id": None}}})
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            first = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            replay = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            self.assertTrue(replay.duplicate)
            payload["constraints"]["forge_execution"]["runtime_prompt"]["id"] = "prompt-2"  # type: ignore[index]
            with self.assertRaisesRegex(submission_service.SubmissionError, "IDEMPOTENCY_CONFLICT"):
                submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            self.assertEqual(first.submission_id, replay.submission_id)
            malformed = self.payload("missing-forge")
            malformed["producer"] = {"id": "forge", "type": "FORGE", "version": "1.0"}
            with self.assertRaisesRegex(submission_service.SubmissionError, "FORGE_PROVENANCE_REQUIRED"):
                submission_service.request_from_mapping("djconnect", malformed, transport="HTTP")

    def test_submission_rejects_malformed_and_unbound_inputs(self) -> None:
        with self.assertRaisesRegex(submission_service.SubmissionError, "MALFORMED_REQUEST"):
            submission_service.request_from_mapping("djconnect", [], transport="HTTP")
        for payload, code in (
            ({"unknown": True}, "UNKNOWN_FIELD"),
            ({"repository_id": "djconnect", "producer": {}, "prompt": "x"}, "INVALID_PRODUCER"),
            ({"repository_id": "djconnect", "producer": {"id": "x", "type": "HUMAN"}, "prompt": ""}, "INVALID_PROMPT"),
        ):
            with self.assertRaisesRegex(submission_service.SubmissionError, code):
                submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            unknown_project = submission_service.SubmissionRequest("absent", "djconnect", "x", "HUMAN", None, "x", "HTTP")
            with self.assertRaisesRegex(submission_service.SubmissionError, "UNKNOWN_PROJECT"):
                submission_service.submit(connection, unknown_project)
            unknown_repository = submission_service.SubmissionRequest("djconnect", "absent", "x", "HUMAN", None, "x", "HTTP")
            with self.assertRaisesRegex(submission_service.SubmissionError, "UNKNOWN_REPOSITORY"):
                submission_service.submit(connection, unknown_repository)
