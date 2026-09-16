from __future__ import annotations

import inspect
import json
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from engineering_platform import owner_credential_recovery as recovery
from engineering_platform import server
from engineering_platform.ep_consumer_credentials import fingerprint, verifier


INSTANCE = "63bda874-6521-44c7-bd1d-78398cdeb6c0"
PEER_RUNTIME = "forge-runtime-735b0321-c4bf-41cd-81d3-9ee00249254b"
PEER_DIGEST = "sha256:" + "c" * 64
SYNTHETIC_SECRET = "SYNTHETIC_OWNER_RECOVERY_SECRET_MATERIAL"


class FakeKeychain:
    def __init__(self, value: str | None = None, *, read_error: str | None = None,
                 write_error: str | None = None, delete_error: str | None = None) -> None:
        self.value = value
        self.read_error = read_error
        self.write_error = write_error
        self.delete_error = delete_error
        self.writes = 0

    def __repr__(self) -> str:
        return "FakeKeychain(service='forge.ep', account='consumer')"

    def read(self) -> str | None:
        if self.read_error:
            raise recovery.CredentialRecoveryError(self.read_error)
        return self.value

    def replace(self, value: str) -> None:
        if self.write_error:
            raise recovery.CredentialRecoveryError(self.write_error)
        self.value = value
        self.writes += 1

    def delete(self) -> None:
        if self.delete_error:
            raise recovery.CredentialRecoveryError(self.delete_error)
        self.value = None


class OwnerCredentialRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "ep"
        identity = server.initialize(self.root)
        self.assertEqual(server.SERVER_STORE_SCHEMA_VERSION, 64)
        self.instance = identity.instance_id
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) "
                "VALUES('forge','{}','ACTIVE','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,"
                "attachment_contract,created_at,updated_at) VALUES('forge','forge','forge','authority','{}',"
                "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO ep_local_repository_bindings(project_id,repository_id,local_root,state,created_at,updated_at) "
                "VALUES('forge','forge','/bounded/forge','BOUND','2026-01-01T00:00:00+00:00',"
                "'2026-01-01T00:00:00+00:00')"
            )
            self.insert_consumer(
                connection, "forge-managed-e2e", "2026-01-01T00:01:00+00:00",
                (("production-one", "old-one", "2026-01-01T00:02:00+00:00"),
                 ("production-two", "old-two", "2026-01-01T00:03:00+00:00")),
            )
            self.insert_exchange(connection, "2026-01-01T00:10:00+00:00")
            self.insert_consumer(
                connection, "human-bootstrap-later", "2026-01-01T00:20:00+00:00",
                (("production-human", "human-token", "2026-01-01T00:21:00+00:00"),),
            )
        self.authority = recovery.OwnerAuthority(
            self.instance, os.geteuid(), "/installed/python", str(self.root), "PASS",
            ("ep_server", "platform_database", "http_ingress"), ("dashboard_relay",),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def insert_consumer(self, connection: sqlite3.Connection, consumer: str, created_at: str,
                        credentials: tuple[tuple[str, str, str], ...]) -> None:
        connection.execute(
            "INSERT INTO ep_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata) "
            "VALUES(?,'forge','ACTIVE',?,?,'{}')", (consumer, created_at, created_at),
        )
        for credential_id, material, issued_at in credentials:
            connection.execute(
                "INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) "
                "VALUES(?,?,'forge',?,?,?)",
                (credential_id, consumer, verifier(material), fingerprint(material), issued_at),
            )

    def insert_exchange(self, connection: sqlite3.Connection, recorded_at: str) -> None:
        connection.execute(
            "INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,"
            "producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,"
            "mission_id,engineering_action_id,state,admission,created_at) VALUES("
            "'submission-first','forge','forge','forge','FORGE','2.7.17','HTTP','bounded','sha256:p','{}',"
            "'first','correlation-first','MISSION-0001','action-first','QUEUED','ADMITTED',?)",
            (recorded_at,),
        )
        connection.execute(
            "INSERT INTO ep_forge_exchange_audit(audit_id,submission_id,project_id,direction,event_kind,"
            "receipt_id,producer_contract_version,forge_provenance_contract_version,forge_application_version,"
            "ep_application_version,producer_readback_contract_version,accepted_request_digest,recorded_at) "
            "VALUES('audit-first','submission-first','forge','FORGE_TO_EP','FORGE_SUBMISSION_ACCEPTED',"
            "'receipt-first','1.0','1.3','2.7.17','2.3.65','1.2','sha256:accepted',?)",
            (recorded_at,),
        )

    def binding(self) -> recovery.ConsumerBinding:
        with self.connection() as connection:
            return recovery.scoped_consumer_readback(
                connection,
                expected_instance_id=self.instance,
                project_id="forge",
                repository_id="forge",
                expected_consumer_id="forge-managed-e2e",
                peer_binding_id="forge-ep-primary",
                peer_runtime_id=PEER_RUNTIME,
                peer_configuration_digest=PEER_DIGEST,
            )

    def authenticates(self, material: str, binding: recovery.ConsumerBinding) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT c.consumer_id,c.project_id,r.status FROM ep_consumer_credentials c "
                "JOIN ep_consumer_registrations r ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id "
                "WHERE c.verifier=? AND c.revoked_at IS NULL",
                (verifier(material),),
            ).fetchone()
        return row == (binding.consumer_id, binding.project_id, "ACTIVE")

    def test_exact_readback_uses_topology_exchange_and_temporal_consumer_evidence(self) -> None:
        binding = self.binding()
        self.assertEqual(binding.consumer_id, "forge-managed-e2e")
        safe = binding.safe_dict()
        self.assertEqual(safe["credential_destination"], "keychain://forge.ep/consumer")
        self.assertEqual(
            safe["binding_evidence"]["historically_eligible_consumer_count"], 1,
        )
        self.assertNotIn("human-bootstrap", json.dumps(safe))

    def test_missing_ambiguous_wrong_scope_and_inactive_bindings_are_typed(self) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) "
                "VALUES('empty','{}','ACTIVE','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,"
                "attachment_contract,created_at,updated_at) VALUES('empty','empty','empty','authority','{}',"
                "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO ep_local_repository_bindings(project_id,repository_id,local_root,state,created_at,updated_at) "
                "VALUES('empty','empty','/bounded/empty','BOUND','2026-01-01T00:00:00+00:00',"
                "'2026-01-01T00:00:00+00:00')"
            )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "FORGE_BINDING_EXCHANGE_NOT_FOUND"):
            with self.connection() as connection:
                recovery.scoped_consumer_readback(
                    connection, expected_instance_id=self.instance, project_id="empty",
                    repository_id="empty", expected_consumer_id="forge-managed-e2e",
                    peer_binding_id="forge-ep-primary", peer_runtime_id=PEER_RUNTIME,
                    peer_configuration_digest=PEER_DIGEST,
                )
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_registrations SET created_at='2026-01-01T00:01:30+00:00' "
                "WHERE consumer_id='human-bootstrap-later'"
            )
            connection.execute(
                "UPDATE ep_consumer_credentials SET issued_at='2026-01-01T00:02:30+00:00' "
                "WHERE consumer_id='human-bootstrap-later'"
            )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "FORGE_CONSUMER_BINDING_AMBIGUOUS"):
            self.binding()
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_registrations SET created_at='2026-01-01T00:20:00+00:00' "
                "WHERE consumer_id='human-bootstrap-later'"
            )
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='DISABLED' WHERE consumer_id='forge-managed-e2e'"
            )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "FORGE_CONSUMER_DISABLED"):
            self.binding()
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='REVOKED' WHERE consumer_id='forge-managed-e2e'"
            )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "FORGE_CONSUMER_REVOKED"):
            self.binding()
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='ACTIVE' WHERE consumer_id='forge-managed-e2e'"
            )
            with self.assertRaisesRegex(
                recovery.CredentialRecoveryError, "PEER_CONSUMER_BINDING_MISMATCH"
            ):
                recovery.scoped_consumer_readback(
                    connection, expected_instance_id=self.instance, project_id="forge",
                    repository_id="forge", expected_consumer_id="another-consumer",
                    peer_binding_id="forge-ep-primary", peer_runtime_id=PEER_RUNTIME,
                    peer_configuration_digest=PEER_DIGEST,
                )
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "INSTALLATION_INSTANCE_MISMATCH"):
                recovery.scoped_consumer_readback(
                    connection, expected_instance_id=INSTANCE, project_id="forge", repository_id="forge",
                    expected_consumer_id="forge-managed-e2e",
                    peer_binding_id="forge-ep-primary", peer_runtime_id=PEER_RUNTIME,
                    peer_configuration_digest=PEER_DIGEST,
                )
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "PROJECT_SCOPE_NOT_FOUND"):
                recovery.scoped_consumer_readback(
                    connection, expected_instance_id=self.instance, project_id="missing", repository_id="forge",
                    expected_consumer_id="forge-managed-e2e",
                    peer_binding_id="forge-ep-primary", peer_runtime_id=PEER_RUNTIME,
                    peer_configuration_digest=PEER_DIGEST,
                )

    def test_existing_credential_is_reused_without_issuance_or_write(self) -> None:
        store = FakeKeychain("old-two")
        before = self.credential_rows()
        result = recovery.recover_credential(
            self.root, operation_id="forge-consumer-recovery-reuse-001", binding=self.binding(),
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        self.assertEqual((result["state"], result["disposition"]), ("SUCCEEDED", "REUSED"))
        self.assertEqual(store.writes, 0)
        self.assertEqual(self.credential_rows(), before)

    def test_replacement_respects_limit_and_is_secret_free(self) -> None:
        store = FakeKeychain()
        observed_before_authentication: list[set[str]] = []

        def authenticate_after_safe_overlap(
            material: str, binding: recovery.ConsumerBinding,
        ) -> bool:
            observed_before_authentication.append({
                str(row[0]) for row in self.credential_rows()
                if (
                    row[1] == "forge-managed-e2e"
                    and row[3] is None
                    and str(row[0]).startswith("production-")
                )
            })
            return self.authenticates(material, binding)

        with patch("engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
                   return_value=SYNTHETIC_SECRET):
            result = recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-replace-001", binding=self.binding(),
                authority=self.authority, store=store,
                authenticate=authenticate_after_safe_overlap,
            )
        self.assertEqual((result["state"], result["disposition"]), ("SUCCEEDED", "REPLACED"))
        self.assertEqual(
            observed_before_authentication,
            [{"production-one", "production-two"}],
        )
        self.assertTrue(str(result["credential_id"]).startswith("production-forge-recovery-"))
        self.assertEqual(store.value, SYNTHETIC_SECRET)
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)
        self.assertEqual(result["credential_destination"], recovery.KEYCHAIN_REFERENCE)
        rendered = json.dumps(result, sort_keys=True) + repr(store)
        self.assertNotIn(SYNTHETIC_SECRET, rendered)
        self.assertFalse(result["secret_material_exposed"])

    def test_write_and_authentication_failures_preserve_safe_state(self) -> None:
        binding = self.binding()
        store = FakeKeychain(write_error="KEYCHAIN_LOCKED_OR_INTERACTION_NOT_ALLOWED")
        before = self.credential_rows()
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "KEYCHAIN_LOCKED") as error:
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-locked-001", binding=binding,
                authority=self.authority, store=store, authenticate=self.authenticates,
            )
        self.assertNotIn(SYNTHETIC_SECRET, repr(error.exception))
        self.assertEqual(self.credential_rows(), before)

        failed = FakeKeychain()
        with patch("engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
                   return_value=SYNTHETIC_SECRET):
            result = recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-auth-fail-001", binding=binding,
                authority=self.authority, store=failed, authenticate=lambda *_: False,
            )
        self.assertEqual((result["state"], result["last_error_code"]),
                         ("FAILED_SAFE", "REPLACEMENT_AUTHENTICATION_FAILED"))
        self.assertIsNone(failed.value)
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)
        active = {row[0] for row in self.credential_rows() if row[3] is None}
        self.assertTrue({"production-one", "production-two"} <= active)

    def test_process_interruption_replays_without_duplicate_issuance(self) -> None:
        store = FakeKeychain()

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("simulated")

        with patch("engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
                   return_value=SYNTHETIC_SECRET):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id="forge-consumer-recovery-replay-001", binding=self.binding(),
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_keychain_write=interrupt,
                )
        self.assertEqual(
            recovery.recovery_status(self.root, "forge-consumer-recovery-replay-001")["state"],
            "PREPARED",
        )
        result = recovery.recover_credential(
            self.root, operation_id="forge-consumer-recovery-replay-001", binding=self.binding(),
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        self.assertEqual(result["state"], "SUCCEEDED")
        matching = [row for row in self.credential_rows() if row[0] == result["credential_id"]]
        self.assertEqual(len(matching), 1)

    def test_post_activation_interruption_replays_the_same_candidate(self) -> None:
        store = FakeKeychain()

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("simulated")

        with patch("engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
                   return_value=SYNTHETIC_SECRET):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id="forge-consumer-recovery-post-central-001", binding=self.binding(),
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_central_activation=interrupt,
                )
        pending = recovery.recovery_status(self.root, "forge-consumer-recovery-post-central-001")
        self.assertEqual(pending["state"], "CENTRAL_ACTIVATED")
        result = recovery.recover_credential(
            self.root, operation_id="forge-consumer-recovery-post-central-001", binding=self.binding(),
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertTrue(str(pending["credential_id"]).startswith("recovery-pending-forge-"))
        self.assertTrue(str(result["credential_id"]).startswith("production-forge-recovery-"))
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)

    def test_concurrent_recovery_is_rejected_while_first_operation_is_active(self) -> None:
        binding = self.binding()
        store = FakeKeychain()
        entered, release = Event(), Event()
        observed: list[Exception] = []

        def pause() -> None:
            entered.set()
            release.wait(5)

        def first() -> None:
            try:
                recovery.recover_credential(
                    self.root, operation_id="forge-consumer-recovery-concurrent-a", binding=binding,
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_keychain_write=pause,
                )
            except Exception as error:  # pragma: no cover - surfaced below
                observed.append(error)

        thread = Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "CONCURRENT_RECOVERY"):
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-concurrent-b", binding=binding,
                authority=self.authority, store=FakeKeychain(), authenticate=self.authenticates,
            )
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [])

    def test_owner_authority_ignores_optional_relay_but_requires_core_and_owner(self) -> None:
        runtime = {
            "running": True, "instance_id": self.instance, "store": "ready",
            "components": {
                "ep_server": {"critical": True, "healthy": True},
                "platform_database": {"critical": True, "healthy": True},
                "http_ingress": {"critical": True, "healthy": True},
                "dashboard_relay": {"critical": False, "healthy": False},
            },
        }
        selected = Path("/installed/python")
        with patch("engineering_platform.owner_credential_recovery.configured_interpreter",
                   return_value=selected):
            authority = recovery.validate_owner_authority(
                self.root, expected_instance_id=self.instance, selected_interpreter=selected,
                running_status=runtime, effective_uid=os.geteuid(),
            )
            self.assertEqual(authority.optional_degraded_capabilities, ("dashboard_relay",))
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "OWNER_AUTHORITY"):
                recovery.validate_owner_authority(
                    self.root, expected_instance_id=self.instance, selected_interpreter=selected,
                    running_status=runtime, effective_uid=os.geteuid() + 1,
                )
            runtime["components"]["http_ingress"]["healthy"] = False
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "REQUIRED_RUNTIME"):
                recovery.validate_owner_authority(
                    self.root, expected_instance_id=self.instance, selected_interpreter=selected,
                    running_status=runtime, effective_uid=os.geteuid(),
                )

    def test_native_adapter_has_no_security_subprocess_or_secret_repr_surface(self) -> None:
        source = inspect.getsource(recovery.NativeMacOSKeychainStore)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("add-generic-password", source)
        self.assertNotIn("-w", source)
        self.assertNotIn(SYNTHETIC_SECRET, source)

    def test_owner_readback_never_initializes_an_absent_runtime(self) -> None:
        absent = Path(self.temporary.name) / "must-remain-absent"
        output = StringIO()
        with redirect_stdout(output):
            result = server.main([
                "owner-consumer-readback", "--data-root", str(absent),
                "--expected-instance-id", INSTANCE,
                "--project-id", "forge", "--repository-id", "forge",
                "--consumer-id", "forge-managed-e2e",
                "--peer-binding-id", "forge-ep-primary",
                "--peer-runtime-id", PEER_RUNTIME,
                "--peer-configuration-digest", PEER_DIGEST,
            ])
        self.assertEqual(result, 2)
        self.assertIn("CENTRAL_DATABASE_UNAVAILABLE", output.getvalue())
        self.assertFalse(absent.exists())

    def test_schema_63_installation_upgrades_to_durable_recovery_journal(self) -> None:
        with self.connection() as connection:
            connection.execute("DROP INDEX ep_consumer_credential_recovery_scope_lookup")
            connection.execute("DROP INDEX ep_consumer_credential_recovery_active_scope")
            connection.execute("DROP TABLE ep_consumer_credential_recovery_operations")
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version=64")
            connection.execute(
                "INSERT INTO engineering_schema_migrations(version) VALUES(63)"
            )
            connection.execute(
                "UPDATE engineering_metadata SET value='63' "
                "WHERE key='installation.schema_version'"
            )
            connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema64")
            connection.execute(
                "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL CHECK(schema_version IN "
                "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63)))"
            )
            connection.execute(
                "INSERT INTO ep_installations SELECT instance_id,created_at,63 "
                "FROM ep_installations_schema64"
            )
            connection.execute("DROP TABLE ep_installations_schema64")

        identity = server.initialize(self.root)
        report = server.validate_store(self.root, identity)
        self.assertEqual(report["schema_version"], 64)
        with self.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM ep_installations"
                ).fetchone(),
                (64,),
            )
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='ep_consumer_credential_recovery_operations'"
            ).fetchone())

    def credential_rows(self) -> list[tuple[object, ...]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT credential_id,consumer_id,project_id,revoked_at,replaced_by_credential_id "
                "FROM ep_consumer_credentials ORDER BY credential_id"
            ).fetchall()

    def active_credentials(self, consumer: str) -> int:
        with self.connection() as connection:
            return int(connection.execute(
                "SELECT count(*) FROM ep_consumer_credentials WHERE consumer_id=? AND project_id='forge' "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
                (consumer,),
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
