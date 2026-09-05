from __future__ import annotations

from pathlib import Path
import tempfile
from http.client import HTTPConnection
import json
from threading import Thread
import unittest
from engineering_platform.local_api import LOOPBACK_ADDRESS, LocalApiServer
from engineering_platform.local_api_credentials import CredentialAuthority, create_qualification_credential, qualification_status, revoke_consumer, revoke_credential, revoke_qualification_credential, rotate_credential, consumer_status, disable_consumer, issue_credential, register_consumer
from engineering_platform.local_api_keychain import KeychainError, MacOSKeychainCredentialStore


class ConsumerCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_registration_issue_bound_and_state_enforcement(self) -> None:
        self.assertFalse(register_consumer(self.root, consumer_id="consumer", project_id="project")["idempotent"])
        self.assertTrue(register_consumer(self.root, consumer_id="consumer", project_id="project")["idempotent"])
        first = issue_credential(self.root, consumer_id="consumer", project_id="project")
        second = issue_credential(self.root, consumer_id="consumer", project_id="project")
        with self.assertRaisesRegex(ValueError, "limit"):
            issue_credential(self.root, consumer_id="consumer", project_id="project")
        self.assertIsNotNone(CredentialAuthority(self.root).authenticate(first.credential))
        self.assertTrue(disable_consumer(self.root, consumer_id="consumer", project_id="project"))
        self.assertIsNotNone(CredentialAuthority(self.root).authenticate(first.credential))
        self.assertFalse(disable_consumer(self.root, consumer_id="consumer", project_id="project"))
        self.assertTrue(revoke_credential(self.root, second.credential_id))
        self.assertFalse(revoke_credential(self.root, second.credential_id))

    def test_disabled_registration_is_authorization_denied_not_unauthenticated(self) -> None:
        register_consumer(self.root, consumer_id="consumer", project_id="project")
        credential = issue_credential(self.root, consumer_id="consumer", project_id="project")
        server = LocalApiServer(self.root, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(token: str, project_id: str = "project") -> int:
                connection = HTTPConnection(LOOPBACK_ADDRESS, server.server_port, timeout=3)
                body = json.dumps({"contract_version":"1.0","request_type":"contract.foundation","request_id":"authz-state","project_id":project_id,"consumer":{"consumer_id":"consumer"},"auth":{"scheme":"bearer","credential":"carrier"},"payload":{}})
                connection.request("POST", "/v1/capabilities", body=body, headers={"Content-Type":"application/json","Authorization":f"Bearer {token}"})
                status = connection.getresponse().status
                connection.close()
                return status
            self.assertEqual(request(credential.credential), 200)
            self.assertEqual(request("invalid"), 401)
            self.assertEqual(request(credential.credential, "other-project"), 403)
            disable_consumer(self.root, consumer_id="consumer", project_id="project")
            self.assertEqual(request(credential.credential), 403)
            revoke_credential(self.root, credential.credential_id)
            self.assertEqual(request(credential.credential), 401)
        finally:
            server.shutdown()
            server.server_close()

    def test_keychain_fails_closed_without_secret_echo(self) -> None:
        store = MacOSKeychainCredentialStore(executable="missing-security")
        with self.assertRaises(KeychainError):
            store.put_credential("consumer", "project", "sentinel-secret")
        self.assertFalse(store.credential_present("consumer", "project"))

    def test_rotation_keeps_old_credential_until_replacement_is_proven(self) -> None:
        register_consumer(self.root, consumer_id="rotate", project_id="project")
        old = issue_credential(self.root, consumer_id="rotate", project_id="project")

        class Store:
            def put_credential(self, *_: str) -> None:
                return None

        replacement = rotate_credential(
            self.root, consumer_id="rotate", project_id="project", old_credential_id=old.credential_id,
            store=Store(), authenticate=lambda token: CredentialAuthority(self.root).authenticate(token) is not None,
        )
        self.assertIsNone(CredentialAuthority(self.root).authenticate(old.credential))
        self.assertIsNotNone(CredentialAuthority(self.root).authenticate(replacement.credential))

    def test_credential_authority_tracks_qualification_and_rolls_back_an_unproven_rotation(self) -> None:
        register_consumer(self.root, consumer_id="consumer", project_id="project")
        qualification = create_qualification_credential(self.root, consumer_id="consumer", project_id="project")
        self.assertTrue(qualification_status(self.root)[0]["active"])
        self.assertTrue(revoke_qualification_credential(self.root, qualification.credential_id))
        self.assertFalse(qualification_status(self.root)[0]["active"])
        with self.assertRaises(ValueError):
            revoke_qualification_credential(self.root, "production-not-qualification")
        original = issue_credential(self.root, consumer_id="consumer", project_id="project")
        class Store:
            def put_credential(self, *_: str) -> None: raise OSError("keychain unavailable")
        with self.assertRaisesRegex(OSError, "keychain"):
            rotate_credential(self.root, consumer_id="consumer", project_id="project", old_credential_id=original.credential_id, store=Store(), authenticate=lambda _: True)
        self.assertIsNotNone(CredentialAuthority(self.root).authenticate(original.credential))
        self.assertEqual(consumer_status(self.root, consumer_id="consumer", project_id="project")["active_production_credentials"], 1)
        self.assertTrue(revoke_consumer(self.root, consumer_id="consumer", project_id="project"))
        with self.assertRaisesRegex(ValueError, "state conflicts"):
            disable_consumer(self.root, consumer_id="consumer", project_id="project")
