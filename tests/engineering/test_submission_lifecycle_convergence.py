from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from engineering_platform import server, submission_cli, submission_service


class CanonicalSubmissionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "isolated-central"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                "INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                ("isolated-project", "{}", "ACTIVE", now, now),
            )
            connection.execute(
                "INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)",
                ("isolated-repository", "isolated-project", "isolated-repository", "authority", "{}", now, now),
            )
            self.credential = submission_service.issue_consumer_credential(
                connection, consumer_id="canary-cli", project_id="isolated-project",
            )["credential"]

    def tearDown(self) -> None:
        server.stop(self.root)
        self.temporary.cleanup()

    @staticmethod
    def _assert_lifecycle(receipt: dict[str, object], transport: str) -> None:
        lifecycle = receipt["lifecycle"]
        assert isinstance(lifecycle, dict)
        assert lifecycle == {
            "version": "1",
            "submission": "ACCEPTED",
            "admission": "ADMITTED",
            "execution": "NOT_DISPATCHED",
            "transport": transport,
            "producer_id": "canary",
        }
        assert receipt["state"] == "QUEUED"
        assert receipt["admission"] == "ADMITTED"
        assert receipt["transport"] == transport

    def _payload(self, key: str) -> dict[str, object]:
        return {
            "repository_id": "isolated-repository",
            "producer": {"id": "canary", "type": "HUMAN", "version": "1"},
            "prompt": "Validate only; this isolated canary must not execute.",
            "idempotency_key": key,
            "correlation_id": "canary-correlation",
            "mission_id": "canary-mission",
            "engineering_action_id": "canary-action",
            "constraints": {"mode": "canary"},
        }

    def test_http_cli_and_legacy_file_share_the_durable_non_dispatch_lifecycle(self) -> None:
        server.start(self.root)
        http_request = Request(
            f"http://127.0.0.1:{self.port}/v1/projects/isolated-project/submissions",
            data=json.dumps(self._payload("http")).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"},
        )
        with urlopen(http_request) as response:  # nosec B310 -- isolated loopback canary
            http_receipt = json.loads(response.read())
        self._assert_lifecycle(http_receipt, "HTTP")

        prompt = self.root / "cli-prompt.md"
        prompt.write_text("Validate only; this isolated canary must not execute.", encoding="utf-8")
        constraints = self.root / "constraints.json"
        constraints.write_text(json.dumps({"mode": "canary"}), encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"EP_CONSUMER_TOKEN": self.credential}, clear=False), redirect_stdout(output):
            self.assertEqual(
                submission_cli.main([
                    "submit", "--server", f"http://127.0.0.1:{self.port}", "--project", "isolated-project",
                    "--repository", "isolated-repository", "--producer-id", "canary", "--producer-type", "HUMAN",
                    "--producer-version", "1", "--prompt-file", str(prompt), "--idempotency-key", "cli",
                    "--correlation-id", "canary-correlation", "--mission-id", "canary-mission",
                    "--engineering-action-id", "canary-action", "--constraints-file", str(constraints),
                ]),
                0,
            )
        cli_receipt = json.loads(output.getvalue())
        self._assert_lifecycle(cli_receipt, "CLI")

        legacy = self.root / "legacy.json"
        legacy.write_text(
            json.dumps({"project_id": "isolated-project", "submission": self._payload("legacy")}),
            encoding="utf-8",
        )
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            legacy_receipt = submission_service.submit_legacy_file(connection, legacy).to_dict()
            self._assert_lifecycle(legacy_receipt, "LEGACY_FILE")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_execution_runs").fetchone()[0], 0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_submission_events").fetchone()[0], 9,
            )
            for submission_id, in connection.execute("SELECT submission_id FROM ep_submissions"):
                self.assertEqual(
                    submission_service.lifecycle(connection, submission_id)["execution"],
                    "NOT_DISPATCHED",
                )

    def test_idempotency_key_cannot_alias_a_different_lifecycle_request(self) -> None:
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submission_service.submit(
                connection,
                submission_service.request_from_mapping("isolated-project", self._payload("same"), transport="HTTP"),
            )
            changed = self._payload("same")
            changed["prompt"] = "A different submission must not alias the first."
            with self.assertRaisesRegex(submission_service.SubmissionError, "IDEMPOTENCY_CONFLICT"):
                submission_service.submit(
                    connection,
                    submission_service.request_from_mapping("isolated-project", changed, transport="CLI"),
                )


class InboxWatcherSemanticInventoryTest(unittest.TestCase):
    def test_inventory_covers_each_watcher_function_exactly_once(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        inventory = json.loads(
            (repository / "docs/engineering/INBOX_WATCHER_SEMANTIC_INVENTORY.json").read_text(encoding="utf-8")
        )
        source = repository / str(inventory["source"])
        functions = {
            node.name for node in ast.parse(source.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        mapped = [
            symbol for item in inventory["inventory"] for symbol in item["symbols"]
        ]
        self.assertEqual(len(mapped), len(set(mapped)))
        self.assertEqual(set(mapped), functions)
        self.assertTrue(
            all(item["classification"] in inventory["classification_vocabulary"] for item in inventory["inventory"])
        )
        self.assertEqual(inventory["summary"]["declared_function_responsibilities"], len(functions))
        self.assertEqual(inventory["summary"]["unclassified"], 0)
        self.assertEqual(inventory["summary"]["ambiguous"], 0)
        self.assertFalse(inventory["summary"]["server_to_codex_shortcut"])
        self.assertEqual(inventory["summary"]["execution_protocol_status"], "NOT_IMPLEMENTED")
