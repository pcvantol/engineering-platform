"""Qualification-only credential boundary coverage."""

from __future__ import annotations

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from tools.engineering.local_api import LOOPBACK_ADDRESS, LocalApiServer
from tools.engineering.local_api_credentials import (
    CredentialAuthority,
    create_qualification_credential,
    main,
    qualification_status,
    revoke_qualification_credential,
    verifier,
)
from tools.engineering.storage import open_storage


class QualificationCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creation_is_verifier_only_and_single_active_credential(self) -> None:
        created = create_qualification_credential(
            self.root, consumer_id="qualification-client", project_id="qualification-project"
        )
        self.assertNotIn(created.credential, repr(created))
        self.assertEqual(
            CredentialAuthority(self.root).authenticate(created.credential).consumer_id,
            "qualification-client",
        )
        connection = open_storage(self.root)
        try:
            row = connection.execute(
                "SELECT credential_id,verifier,fingerprint,expires_at,revoked_at FROM local_api_credentials"
            ).fetchone()
            self.assertTrue(str(row[0]).startswith("qualification-"))
            self.assertEqual(bytes(row[1]), verifier(created.credential))
            self.assertNotIn(created.credential.encode(), bytes(row[1]))
            self.assertIsNotNone(row[3])
            self.assertIsNone(row[4])
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "active qualification"):
            create_qualification_credential(
                self.root, consumer_id="another-client", project_id="another-project"
            )

    def test_scope_authentication_revoke_and_safe_status(self) -> None:
        created = create_qualification_credential(
            self.root, consumer_id="qualification-client", project_id="qualification-project"
        )
        status = qualification_status(self.root)
        self.assertEqual(status[0]["purpose"], "QUALIFICATION")
        self.assertNotIn(created.credential, json.dumps(status))
        self.assertTrue(revoke_qualification_credential(self.root, created.credential_id))
        self.assertIsNone(CredentialAuthority(self.root).authenticate(created.credential))
        self.assertFalse(qualification_status(self.root)[0]["active"])

    def test_invalid_scope_and_cli_do_not_disclose_status_plaintext(self) -> None:
        with self.assertRaises(ValueError):
            create_qualification_credential(self.root, consumer_id="INVALID", project_id="project")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["create-qualification-credential", "--repo", str(self.root)]), 2)
        self.assertNotIn('credential":', output.getvalue())

    def test_cli_discloses_plaintext_once_and_status_redacts_it(self) -> None:
        disclosure = io.StringIO()
        with redirect_stdout(disclosure):
            self.assertEqual(
                main(
                    [
                        "create-qualification-credential",
                        "--repo",
                        str(self.root),
                        "--consumer-id",
                        "qualification-client",
                        "--project-id",
                        "qualification-project",
                    ]
                ),
                0,
            )
        token = json.loads(disclosure.getvalue())["credential"]
        status = io.StringIO()
        with redirect_stdout(status):
            self.assertEqual(main(["qualification-status", "--repo", str(self.root)]), 0)
        self.assertNotIn(token, status.getvalue())

    def test_real_http_runtime_honors_scope_and_revoke(self) -> None:
        created = create_qualification_credential(
            self.root, consumer_id="qualification-client", project_id="qualification-project"
        )
        server = LocalApiServer(self.root, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(project_id: str, token: str) -> tuple[int, str]:
                connection = HTTPConnection(LOOPBACK_ADDRESS, server.server_port, timeout=3)
                body = json.dumps(
                    {
                        "contract_version": "1.0",
                        "request_type": "contract.foundation",
                        "request_id": "qualification-request",
                        "project_id": project_id,
                        "consumer": {"consumer_id": "qualification-client"},
                        "auth": {"scheme": "bearer", "credential": "operator-carrier"},
                        "payload": {},
                    }
                )
                connection.request(
                    "POST",
                    "/v1/capabilities",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
                response = connection.getresponse()
                content = response.read().decode()
                connection.close()
                return response.status, content

            self.assertEqual(request("qualification-project", created.credential)[0], 200)
            mismatch_status, mismatch_body = request("other-project", created.credential)
            self.assertEqual(mismatch_status, 403)
            self.assertNotIn(created.credential, mismatch_body)
            self.assertEqual(request("qualification-project", "invalid-sentinel")[0], 401)
            revoke_qualification_credential(self.root, created.credential_id)
            self.assertEqual(request("qualification-project", created.credential)[0], 401)
        finally:
            server.shutdown()
            server.server_close()
