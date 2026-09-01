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
