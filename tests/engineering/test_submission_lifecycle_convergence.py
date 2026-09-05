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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import file_inbox, server, submission_cli, submission_service


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

    def _payload(self, key: str, mode: str = "MANAGED") -> dict[str, object]:
        target = "\nTarget repository: /tmp/isolated-genesis-target" if mode == "GENESIS" else ""
        return {
            "repository_id": "isolated-repository",
            "producer": {"id": "canary", "type": "HUMAN", "version": "1"},
            "prompt": f"Execution Mode: {mode.title()}{target}\n\nValidate only; this isolated canary must not execute.",
            "idempotency_key": key,
            "correlation_id": "canary-correlation",
            "mission_id": "canary-mission",
            "engineering_action_id": "canary-action",
            "constraints": {"mode": mode, "canary": True},
        }

    def _http_submit(self, payload: dict[str, object], *, credential: str | None = None) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(
            f"http://127.0.0.1:{self.port}/v1/projects/isolated-project/submissions",
            data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
        )
        with urlopen(request) as response:  # nosec B310 -- isolated loopback canary
            return json.loads(response.read())

    def _cli_submit(self, payload: dict[str, object], mode: str) -> dict[str, object]:
        prompt = self.root / f"cli-{mode.lower()}.md"
        constraints = self.root / f"constraints-{mode.lower()}.json"
        prompt.write_text(str(payload["prompt"]), encoding="utf-8")
        constraints.write_text(json.dumps(payload["constraints"]), encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"EP_CONSUMER_TOKEN": self.credential}, clear=False), redirect_stdout(output):
            self.assertEqual(submission_cli.main([
                "submit", "--server", f"http://127.0.0.1:{self.port}", "--project", "isolated-project",
                "--repository", "isolated-repository", "--producer-id", "canary", "--producer-type", "HUMAN",
                "--producer-version", "1", "--prompt-file", str(prompt), "--idempotency-key", str(payload["idempotency_key"]),
                "--correlation-id", "canary-correlation", "--mission-id", "canary-mission",
                "--engineering-action-id", "canary-action", "--constraints-file", str(constraints),
            ]), 0)
        return json.loads(output.getvalue())

    def _file_inbox_submit(self, payload: dict[str, object], mode: str) -> dict[str, object]:
        root = self.root / "file-inbox"
        incoming = root / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        receipts_before = set((root / "accepted").glob("*.receipt.json")) if (root / "accepted").exists() else set()
        (incoming / f"{mode.lower()}.json").write_text(
            json.dumps({"project_id": "isolated-project", "submission": payload}), encoding="utf-8",
        )
        self.assertEqual(
            file_inbox.process_once(root, server=f"http://127.0.0.1:{self.port}", credential=self.credential),
            {"accepted": 1, "quarantined": 0, "retryable": 0},
        )
        receipt = next(iter(set((root / "accepted").glob("*.receipt.json")) - receipts_before))
        return json.loads(receipt.read_text(encoding="utf-8"))

    def test_every_transport_and_execution_mode_share_the_durable_non_dispatch_lifecycle(self) -> None:
        server.start(self.root)
        for mode in ("MANAGED", "GENESIS"):
            with self.subTest(transport="HTTP", mode=mode):
                self._assert_lifecycle(self._http_submit(self._payload(f"http-{mode}", mode), credential=self.credential), "HTTP")
            with self.subTest(transport="CLI", mode=mode):
                self._assert_lifecycle(self._cli_submit(self._payload(f"cli-{mode}", mode), mode), "CLI")
            with self.subTest(transport="FILE_INBOX", mode=mode):
                receipt = self._file_inbox_submit(self._payload(f"file-{mode}", mode), mode)
                with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
                    lifecycle = submission_service.lifecycle(connection, str(receipt["submission_id"]))
                self.assertEqual(lifecycle["transport"], "FILE_INBOX")
                self.assertEqual(lifecycle["execution"], "NOT_DISPATCHED")

        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_execution_runs").fetchone()[0], 0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_submission_events").fetchone()[0], 18,
            )
            for submission_id, in connection.execute("SELECT submission_id FROM ep_submissions"):
                self.assertEqual(
                    submission_service.lifecycle(connection, submission_id)["execution"],
                    "NOT_DISPATCHED",
                )

    def test_negative_transport_matrix_rejects_admission_without_creating_a_run(self) -> None:
        """Each ingress preserves CENTRAL's terminal rejection without dispatching."""
        server.start(self.root)
        rejected = self._payload("http-rejected")
        rejected["repository_id"] = "unknown-repository"
        with self.assertRaises(HTTPError) as http_error:
            self._http_submit(rejected, credential=self.credential)
        self.assertEqual(http_error.exception.code, 404)

        prompt, constraints, output = self.root / "rejected.md", self.root / "rejected.json", io.StringIO()
        prompt.write_text(str(rejected["prompt"]), encoding="utf-8")
        constraints.write_text(json.dumps(rejected["constraints"]), encoding="utf-8")
        with patch.dict(os.environ, {"EP_CONSUMER_TOKEN": self.credential}, clear=False), redirect_stdout(output):
            self.assertEqual(submission_cli.main([
                "submit", "--server", f"http://127.0.0.1:{self.port}", "--project", "isolated-project",
                "--repository", "unknown-repository", "--prompt-file", str(prompt), "--constraints-file", str(constraints),
            ]), 1)
        self.assertIn("UNKNOWN_REPOSITORY", output.getvalue())

        inbox_root = self.root / "rejected-file-inbox"
        (inbox_root / "incoming").mkdir(parents=True)
        (inbox_root / "incoming" / "rejected.json").write_text(
            json.dumps({"project_id": "isolated-project", "submission": rejected}), encoding="utf-8",
        )
        self.assertEqual(
            file_inbox.process_once(inbox_root, server=f"http://127.0.0.1:{self.port}", credential=self.credential),
            {"accepted": 0, "quarantined": 1, "retryable": 0},
        )
        receipt = json.loads(next((inbox_root / "quarantine").glob("*.receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["reason"], "UNKNOWN_REPOSITORY")
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_execution_runs").fetchone()[0], 0)

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


class RetiredInboxWatcherTest(unittest.TestCase):
    def test_retired_watcher_runtime_is_not_packaged_source(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        self.assertFalse((repository / "src/engineering_platform/inbox_watcher.py").exists())
