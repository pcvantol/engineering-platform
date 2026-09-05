from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from engineering_platform import file_inbox, server, submission_service


class FileInboxTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        self.inbox = Path(self.temporary.name) / "file-inbox"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("project-a", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("project-b", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("repository-a", "project-a", "repository-a", "authority", "{}", now, now))
            self.credential = submission_service.issue_consumer_credential(connection, consumer_id="file-canary", project_id="project-a")["credential"]
        server.start(self.root)

    def tearDown(self) -> None:
        server.stop(self.root)
        self.temporary.cleanup()

    def _write(self, name: str, project: str = "project-a", repository: str = "repository-a") -> Path:
        path = self.inbox / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"project_id": project, "submission": {
            "repository_id": repository, "producer": {"id": "canary", "type": "HUMAN", "version": "1"},
            "prompt": "File transport equivalence canary.", "constraints": {"mode": "canary"},
        }}), encoding="utf-8")
        return path

    def test_file_is_admitted_once_and_archived_with_central_receipt(self) -> None:
        self._write("submission.json")
        first = file_inbox.process_once(self.inbox, server=f"http://127.0.0.1:{self.port}", credential=self.credential)
        second = file_inbox.process_once(self.inbox, server=f"http://127.0.0.1:{self.port}", credential=self.credential)
        self.assertEqual(first, {"accepted": 1, "quarantined": 0, "retryable": 0})
        self.assertEqual(second, {"accepted": 0, "quarantined": 0, "retryable": 0})
        receipts = list((self.inbox / "accepted").glob("*.receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute("SELECT transport,transport_receipt_id,transport_received_at,state FROM ep_submissions").fetchone()
        self.assertEqual(row[0], "FILE_INBOX")
        self.assertEqual(row[1], receipt["receipt_id"])
        self.assertTrue(row[2])
        self.assertEqual(row[3], "QUEUED")

    def test_file_cannot_cross_project_binding_or_infer_authority(self) -> None:
        self._write("wrong-project.json", project="project-b")
        self._write("missing-project.json", project="unknown")
        outcome = file_inbox.process_once(self.inbox, server=f"http://127.0.0.1:{self.port}", credential=self.credential)
        self.assertEqual(outcome, {"accepted": 0, "quarantined": 2, "retryable": 0})
        self.assertEqual(len(list((self.inbox / "quarantine").glob("*.json"))), 4)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 0)

    def test_installed_service_heartbeat_marks_the_platform_ingress_running(self) -> None:
        """Liveness comes from the real adapter service, never a UI fixture."""
        installation_inbox = self.root / server.FILE_INBOX_DIRECTORY
        service = file_inbox.FileInboxService(
            installation_inbox,
            admission=lambda envelope, receipt_id, received_at: server._admit_server_owned_file_inbox(
                self.root, envelope, receipt_id, received_at,
            ),
            interval_seconds=0.02,
        )
        service.start()
        try:
            for _ in range(50):
                component = server.status(self.root)["components"]["file_inbox_ingress"]
                if component["status_code"] == "FILE_INGRESS_RUNNING":
                    break
                time.sleep(0.02)
            self.assertEqual(component["status_code"], "FILE_INGRESS_RUNNING")
            self.assertTrue(component["healthy"])
        finally:
            service.stop()

    def test_live_file_inbox_without_submission_auth_is_not_ready(self) -> None:
        """A child thread without admission auth is never a healthy ingress."""
        installation_inbox = self.root / server.FILE_INBOX_DIRECTORY
        service = file_inbox.FileInboxService(installation_inbox, interval_seconds=0.02)
        service.start()
        try:
            for _ in range(50):
                component = server.status(self.root)["components"]["file_inbox_ingress"]
                if component["status_code"] == "FILE_INGRESS_NOT_READY":
                    break
                time.sleep(0.02)
            self.assertEqual(component["status_code"], "FILE_INGRESS_NOT_READY")
            self.assertFalse(component["healthy"])
        finally:
            service.stop()
