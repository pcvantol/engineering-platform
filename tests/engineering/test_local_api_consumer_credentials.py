from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from tools.engineering.local_api_credentials import CredentialAuthority, disable_consumer, issue_credential, register_consumer, revoke_credential, rotate_credential
from tools.engineering.local_api_keychain import KeychainError, MacOSKeychainCredentialStore


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
        self.assertIsNone(CredentialAuthority(self.root).authenticate(first.credential))
        self.assertFalse(disable_consumer(self.root, consumer_id="consumer", project_id="project"))
        self.assertTrue(revoke_credential(self.root, second.credential_id))
        self.assertFalse(revoke_credential(self.root, second.credential_id))
        self.assertFalse(revoke_credential(self.root, second.credential_id))

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
