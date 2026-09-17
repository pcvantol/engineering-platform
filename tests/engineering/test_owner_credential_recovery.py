from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import ctypes
from contextlib import redirect_stdout
from dataclasses import replace
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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


class SharedKeychain:
    """Multiprocess synthetic adapter; material never leaves test process memory."""

    def __init__(self, memory) -> None:
        self.buffer, self.write_count = memory

    def read(self) -> str | None:
        raw = bytes(self.buffer.value)
        return raw.decode("ascii") if raw else None

    def replace(self, value: str) -> None:
        self.buffer.value = value.encode("ascii")
        self.write_count.value += 1

    def delete(self) -> None:
        self.buffer.value = b""


def _process_authenticates(
    root: str, material: str, binding: recovery.ConsumerBinding,
) -> bool:
    with sqlite3.connect(Path(root) / server.SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            "SELECT c.consumer_id,c.project_id,r.status FROM ep_consumer_credentials c "
            "JOIN ep_consumer_registrations r "
            "ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id "
            "WHERE c.verifier=? AND c.revoked_at IS NULL",
            (verifier(material),),
        ).fetchone()
    return row == (binding.consumer_id, binding.project_id, "ACTIVE")


def _multiprocess_recovery_worker(
    root: str,
    operation_id: str,
    binding: recovery.ConsumerBinding,
    authority: recovery.OwnerAuthority,
    shared_keychain,
    generated_material: str,
    entered,
    release,
    results,
) -> None:
    def pause_after_fingerprint() -> None:
        if entered is not None:
            entered.set()
        if release is not None:
            release.wait(10)

    try:
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value=generated_material,
        ):
            result = recovery.recover_credential(
                Path(root), operation_id=operation_id, binding=binding,
                authority=authority, store=SharedKeychain(shared_keychain),
                authenticate=lambda material, selected: _process_authenticates(
                    root, material, selected,
                ),
                after_fingerprint_binding=pause_after_fingerprint,
            )
        results.put(("OK", result["state"], result["disposition"], result["credential_id"]))
    except Exception as error:  # pragma: no cover - surfaced in parent result
        results.put(("ERROR", type(error).__name__, str(error), None))


class FakeCFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeCoreFoundation:
    def __init__(self) -> None:
        self.next_pointer = 100
        self.data: dict[int, bytes] = {}
        self.buffers: list[object] = []
        self.released: list[int] = []
        self.CFStringCreateWithCString = FakeCFunction(self._string)
        self.CFDataCreate = FakeCFunction(self._data)
        self.CFDataGetLength = FakeCFunction(self._length)
        self.CFDataGetBytePtr = FakeCFunction(self._bytes)
        self.CFDictionaryCreateMutable = FakeCFunction(lambda *_: self.pointer())
        self.CFDictionarySetValue = FakeCFunction(lambda *_: None)
        self.CFRelease = FakeCFunction(self._release)

    def pointer(self, value: bytes | None = None) -> int:
        self.next_pointer += 1
        if value is not None:
            self.data[self.next_pointer] = value
        return self.next_pointer

    def _string(self, _allocator, value, _encoding):
        return self.pointer(bytes(value))

    def _data(self, _allocator, value, length):
        return self.pointer(ctypes.string_at(value, int(length)))

    def _length(self, value):
        return len(self.data.get(int(value.value), b""))

    def _bytes(self, value):
        buffer = (ctypes.c_ubyte * len(self.data[int(value.value)]))(
            *self.data[int(value.value)]
        )
        self.buffers.append(buffer)
        return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))

    def _release(self, value):
        self.released.append(int(value.value))


class FakeSecurityFramework:
    def __init__(self, core: FakeCoreFoundation) -> None:
        self.core = core
        self.material: bytes | None = None
        self.copy_status = recovery.NativeMacOSKeychainStore.SUCCESS
        self.update_status = recovery.NativeMacOSKeychainStore.SUCCESS
        self.add_status = recovery.NativeMacOSKeychainStore.SUCCESS
        self.delete_status = recovery.NativeMacOSKeychainStore.SUCCESS
        self.SecItemAdd = FakeCFunction(lambda *_: self.add_status)
        self.SecItemCopyMatching = FakeCFunction(self._copy)
        self.SecItemUpdate = FakeCFunction(lambda *_: self.update_status)
        self.SecItemDelete = FakeCFunction(lambda *_: self.delete_status)

    def _copy(self, _query, output):
        if self.copy_status == recovery.NativeMacOSKeychainStore.SUCCESS and self.material is not None:
            output._obj.value = self.core.pointer(self.material)
        return self.copy_status


class OwnerCredentialRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "ep"
        identity = server.initialize(self.root)
        self.assertEqual(server.SERVER_STORE_SCHEMA_VERSION, 68)
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
        self.assertEqual(recovery.safe_json({"b": 2, "a": 1}), '{"a":1,"b":2}')

    def test_legacy_discovery_and_guarded_operation_configuration_adoption(self) -> None:
        with self.connection() as connection:
            discovered = recovery.scoped_consumer_readback(
                connection,
                expected_instance_id=self.instance,
                project_id="forge",
                repository_id="forge",
                expected_consumer_id=None,
                peer_binding_id="forge-ep-primary",
                peer_runtime_id=PEER_RUNTIME,
                peer_configuration_digest=PEER_DIGEST,
            )
        self.assertEqual(discovered.consumer_id, "forge-managed-e2e")
        output = StringIO()
        with patch(
            "engineering_platform.server._owner_credential_authority",
            return_value=self.authority,
        ), redirect_stdout(output):
            self.assertEqual(server.main([
                "owner-consumer-readback", "--data-root", str(self.root),
                "--expected-instance-id", self.instance,
                "--project-id", "forge", "--repository-id", "forge",
                "--peer-binding-id", "forge-ep-primary",
                "--peer-runtime-id", PEER_RUNTIME,
                "--peer-configuration-digest", PEER_DIGEST,
            ]), 0)
        self.assertEqual(
            json.loads(output.getvalue())["consumer_binding"]["consumer_id"],
            "forge-managed-e2e",
        )
        operation_id = "forge-consumer-recovery-config-adoption"
        prepared = recovery._prepare_operation(
            self.root, operation_id, discovered, self.authority,
        )
        self.assertEqual(prepared["state"], "PREPARED")
        next_digest = "sha256:" + "d" * 64
        adopted = recovery.adopt_peer_configuration(
            self.root,
            operation_id=operation_id,
            binding=replace(discovered, peer_configuration_digest=next_digest),
            authority=self.authority,
            previous_peer_configuration_digest=PEER_DIGEST,
        )
        self.assertEqual(adopted["peer_configuration_digest"], next_digest)
        self.assertEqual(adopted["adopted_from_peer_configuration_digest"], PEER_DIGEST)
        self.assertIsNotNone(adopted["configuration_adopted_at"])
        result = recovery.recover_credential(
            self.root, operation_id=operation_id,
            binding=replace(discovered, peer_configuration_digest=next_digest),
            authority=self.authority, store=FakeKeychain("old-two"),
            authenticate=self.authenticates,
        )
        self.assertEqual((result["state"], result["disposition"]), ("SUCCEEDED", "REUSED"))
        guarded_operation = "forge-consumer-recovery-config-adoption-guard"
        recovery._prepare_operation(
            self.root, guarded_operation, discovered, self.authority,
        )
        recovery._bind_prepared_fingerprint(
            self.root, guarded_operation, "SYNTHETIC_ALREADY_BOUND",
            expected_fingerprint=None,
        )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "ADOPTION_NOT_SAFE",
        ):
            recovery.adopt_peer_configuration(
                self.root,
                operation_id=guarded_operation,
                binding=replace(discovered, peer_configuration_digest=next_digest),
                authority=self.authority,
                previous_peer_configuration_digest=PEER_DIGEST,
            )

    def test_destination_lock_and_configuration_adoption_guards_fail_closed(self) -> None:
        another_root = self.root.parent / "another-root"
        another_root.mkdir()
        mismatched_authority = replace(
            self.authority, data_root=str(another_root),
        )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_DATA_ROOT_AUTHORITY_MISMATCH",
        ):
            with recovery._destination_lock(self.root, mismatched_authority):
                self.fail("a mismatched data root must never acquire the destination lock")

        lock_path = self.root / recovery._DESTINATION_LOCK_FILENAME
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o644)
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_DESTINATION_LOCK_UNSAFE",
        ):
            with recovery._destination_lock(self.root, self.authority):
                self.fail("an unsafe lock file must never be accepted")
        lock_path.chmod(0o600)

        with patch(
            "engineering_platform.owner_credential_recovery.os.open",
            side_effect=OSError("synthetic open denial"),
        ), self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_DESTINATION_LOCK_UNAVAILABLE",
        ):
            with recovery._destination_lock(self.root, self.authority):
                self.fail("an unavailable lock must not enter its protected section")

        with patch(
            "engineering_platform.owner_credential_recovery.fcntl.flock",
            side_effect=OSError("synthetic flock denial"),
        ), self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_DESTINATION_LOCK_UNAVAILABLE",
        ):
            with recovery._destination_lock(self.root, self.authority):
                self.fail("a failed kernel lock must not enter its protected section")

        binding = self.binding()
        next_digest = "sha256:" + "d" * 64
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "PREVIOUS_PEER_CONFIGURATION_DIGEST_INVALID",
        ):
            recovery.adopt_peer_configuration(
                self.root, operation_id="forge-consumer-recovery-invalid-adoption",
                binding=replace(binding, peer_configuration_digest=next_digest),
                authority=self.authority, previous_peer_configuration_digest="not-a-digest",
            )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "PEER_CONFIGURATION_DIGEST_UNCHANGED",
        ):
            recovery.adopt_peer_configuration(
                self.root, operation_id="forge-consumer-recovery-unchanged-adoption",
                binding=binding, authority=self.authority,
                previous_peer_configuration_digest=PEER_DIGEST,
            )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_OPERATION_NOT_FOUND",
        ):
            recovery.adopt_peer_configuration(
                self.root, operation_id="forge-consumer-recovery-missing-adoption",
                binding=replace(binding, peer_configuration_digest=next_digest),
                authority=self.authority, previous_peer_configuration_digest=PEER_DIGEST,
            )

        operation_id = "forge-consumer-recovery-identity-adoption"
        recovery._prepare_operation(self.root, operation_id, binding, self.authority)
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_OPERATION_IDENTITY_CONFLICT",
        ):
            recovery.adopt_peer_configuration(
                self.root, operation_id=operation_id,
                binding=replace(
                    binding, consumer_id="another-consumer",
                    peer_configuration_digest=next_digest,
                ),
                authority=self.authority, previous_peer_configuration_digest=PEER_DIGEST,
            )

    def test_fingerprint_and_reuse_state_transitions_are_compare_and_swap_guarded(self) -> None:
        binding = self.binding()
        reuse_operation = "forge-consumer-recovery-reuse-guard"
        recovery._prepare_operation(self.root, reuse_operation, binding, self.authority)
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "EXISTING_KEYCHAIN_CREDENTIAL_IDENTITY_CONFLICT",
        ):
            recovery._mark_reused(self.root, reuse_operation, "production-one", "old-two")
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET state='SUCCEEDED' "
                "WHERE operation_id=?", (reuse_operation,),
            )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_OPERATION_STATE_CONFLICT",
        ):
            recovery._mark_reused(self.root, reuse_operation, "production-one", "old-one")

        fingerprint_operation = "forge-consumer-recovery-fingerprint-guard"
        recovery._prepare_operation(self.root, fingerprint_operation, binding, self.authority)
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_OPERATION_STATE_CONFLICT",
        ):
            recovery._bind_prepared_fingerprint(
                self.root, fingerprint_operation, "SYNTHETIC_COMPARE_AND_SWAP",
                expected_fingerprint="sha256-does-not-match",
            )
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET state='SUCCEEDED' "
                "WHERE operation_id=?", (fingerprint_operation,),
            )
        with self.assertRaisesRegex(
            recovery.CredentialRecoveryError, "RECOVERY_OPERATION_STATE_CONFLICT",
        ):
            recovery._bind_prepared_fingerprint(
                self.root, fingerprint_operation, "SYNTHETIC_COMPARE_AND_SWAP",
                expected_fingerprint=None,
            )

    def test_owner_authority_precedes_consumer_discovery(self) -> None:
        arguments = SimpleNamespace(
            data_root=self.root, expected_instance_id=self.instance,
            project_id="forge", repository_id="forge", consumer_id=None,
            peer_binding_id="forge-ep-primary", peer_runtime_id=PEER_RUNTIME,
            peer_configuration_digest=PEER_DIGEST,
        )
        with patch(
            "engineering_platform.server._owner_credential_authority",
            side_effect=recovery.CredentialRecoveryError("OWNER_AUTHORITY_DENIED"),
        ), patch("engineering_platform.server._owner_binding_from_args") as binding:
            output = StringIO()
            with redirect_stdout(output):
                result = server.main([
                    "owner-consumer-readback", "--data-root", str(self.root),
                    "--expected-instance-id", self.instance,
                    "--project-id", "forge", "--repository-id", "forge",
                    "--peer-binding-id", "forge-ep-primary",
                    "--peer-runtime-id", PEER_RUNTIME,
                    "--peer-configuration-digest", PEER_DIGEST,
                ])
        self.assertEqual(result, 2)
        self.assertIn("OWNER_AUTHORITY_DENIED", output.getvalue())
        binding.assert_not_called()
        self.assertIsNone(arguments.consumer_id)

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

    def test_fingerprint_interruption_is_reconciled_before_one_keychain_write(self) -> None:
        store = FakeKeychain()

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("after fingerprint")

        operation_id = "forge-consumer-recovery-fingerprint-interruption"
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_FINGERPRINT_ONLY_A",
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=operation_id, binding=self.binding(),
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_fingerprint_binding=interrupt,
                )
        interrupted = recovery.recovery_status(self.root, operation_id)
        self.assertEqual(interrupted["state"], "PREPARED")
        self.assertIsNotNone(interrupted["credential_fingerprint"])
        self.assertEqual((store.value, store.writes), (None, 0))
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_FINGERPRINT_REPLAY_B",
        ):
            result = recovery.recover_credential(
                self.root, operation_id=operation_id, binding=self.binding(),
                authority=self.authority, store=store, authenticate=self.authenticates,
            )
        self.assertEqual((result["state"], store.writes), ("SUCCEEDED", 1))

    def test_pending_candidate_is_probe_only_before_promotion(self) -> None:
        store = FakeKeychain()
        operation_id = "forge-consumer-recovery-probe-policy"

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("before probe")

        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value=SYNTHETIC_SECRET,
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=operation_id, binding=self.binding(),
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_central_activation=interrupt,
                )
        with self.connection() as connection:
            self.assertIsNone(server._authenticated_consumer_scope(connection, store.value))
            self.assertIsNone(server._authenticated_consumer(connection, store.value, "forge"))
            self.assertIsNone(
                server._operator_capability(connection, store.value, "forge", "QUEUE_DECLINE")
            )
            self.assertEqual(
                server._authenticated_consumer_scope(
                    connection, store.value, recovery_operation_id=operation_id,
                ),
                ("forge-managed-e2e", "forge"),
            )

    def test_pending_candidate_probe_is_enforced_by_the_real_http_route(self) -> None:
        store = FakeKeychain()
        operation_id = "forge-consumer-recovery-http-route-probe"

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("before real HTTP probe")

        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value=SYNTHETIC_SECRET,
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=operation_id, binding=self.binding(),
                    authority=self.authority, store=store, authenticate=self.authenticates,
                    after_central_activation=interrupt,
                )

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        httpd.data_root = self.root  # type: ignore[attr-defined]
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{httpd.server_port}/v1/owner-credential-recovery-probe"
        try:
            request = Request(endpoint, headers={
                "Authorization": f"Bearer {store.value}",
                "EP-Recovery-Operation-ID": operation_id,
                "EP-Project-ID": "forge",
                "EP-Repository-ID": "forge",
            })
            with urlopen(request) as response:  # nosec B310 -- isolated loopback fixture
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {
                "contract_version": "1.0",
                "instance_id": self.instance,
                "operation_id": operation_id,
                "consumer_id": "forge-managed-e2e",
                "project_id": "forge",
                "repository_id": "forge",
                "credential_status": "PENDING_RECOVERY_PROBE",
                "authorization": "RECOVERY_PROBE_ONLY",
            })

            with self.assertRaises(HTTPError) as incomplete:
                urlopen(Request(endpoint))  # nosec B310 -- isolated loopback fixture
            self.assertEqual(incomplete.exception.code, 400)
            with self.assertRaises(HTTPError) as unauthenticated:
                urlopen(Request(endpoint, headers={  # nosec B310 -- isolated loopback fixture
                    "Authorization": "Bearer invalid-recovery-material",
                    "EP-Recovery-Operation-ID": operation_id,
                    "EP-Project-ID": "forge",
                    "EP-Repository-ID": "forge",
                }))
            self.assertEqual(unauthenticated.exception.code, 401)
            with self.assertRaises(HTTPError) as wrong_scope:
                urlopen(Request(endpoint, headers={  # nosec B310 -- isolated loopback fixture
                    "Authorization": f"Bearer {store.value}",
                    "EP-Recovery-Operation-ID": operation_id,
                    "EP-Project-ID": "forge",
                    "EP-Repository-ID": "another-repository",
                }))
            self.assertEqual(wrong_scope.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)

    def test_uncertain_replay_reconciles_each_pre_promotion_edge(self) -> None:
        binding = self.binding()

        active_store = FakeKeychain()
        active_operation = "forge-consumer-recovery-uncertain-active"
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_UNCERTAIN_ACTIVE",
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=active_operation, binding=binding,
                    authority=self.authority, store=active_store,
                    authenticate=self.authenticates,
                    after_central_activation=lambda: (_ for _ in ()).throw(
                        recovery.RecoveryInterrupted("after activation")
                    ),
                )
        recovery._stop_uncertain(self.root, active_operation, "SYNTHETIC_UNCERTAIN")
        resumed_active = recovery.recover_credential(
            self.root, operation_id=active_operation, binding=binding,
            authority=self.authority, store=active_store, authenticate=self.authenticates,
        )
        self.assertEqual(
            (resumed_active["state"], resumed_active["disposition"]),
            ("SUCCEEDED", "REPLACED"),
        )

        empty_store = FakeKeychain()
        empty_operation = "forge-consumer-recovery-uncertain-empty"
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_UNCERTAIN_EMPTY_FIRST",
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=empty_operation, binding=binding,
                    authority=self.authority, store=empty_store,
                    authenticate=self.authenticates,
                    after_fingerprint_binding=lambda: (_ for _ in ()).throw(
                        recovery.RecoveryInterrupted("after fingerprint")
                    ),
                )
        recovery._stop_uncertain(self.root, empty_operation, "SYNTHETIC_UNCERTAIN")
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_UNCERTAIN_EMPTY_REPLAY",
        ):
            resumed_empty = recovery.recover_credential(
                self.root, operation_id=empty_operation, binding=binding,
                authority=self.authority, store=empty_store, authenticate=self.authenticates,
            )
        self.assertEqual(resumed_empty["state"], "SUCCEEDED")

        matching_store = FakeKeychain()
        matching_operation = "forge-consumer-recovery-uncertain-matching"
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_UNCERTAIN_MATCHING",
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=matching_operation, binding=binding,
                    authority=self.authority, store=matching_store,
                    authenticate=self.authenticates,
                    after_keychain_write=lambda: (_ for _ in ()).throw(
                        recovery.RecoveryInterrupted("after keychain write")
                    ),
                )
        recovery._stop_uncertain(self.root, matching_operation, "SYNTHETIC_UNCERTAIN")
        resumed_matching = recovery.recover_credential(
            self.root, operation_id=matching_operation, binding=binding,
            authority=self.authority, store=matching_store, authenticate=self.authenticates,
        )
        self.assertEqual(resumed_matching["state"], "SUCCEEDED")

        changed_store = FakeKeychain()
        changed_operation = "forge-consumer-recovery-uncertain-changed"
        with patch(
            "engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
            return_value="SYNTHETIC_UNCERTAIN_CHANGED",
        ):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id=changed_operation, binding=binding,
                    authority=self.authority, store=changed_store,
                    authenticate=self.authenticates,
                    after_fingerprint_binding=lambda: (_ for _ in ()).throw(
                        recovery.RecoveryInterrupted("before keychain write")
                    ),
                )
        recovery._stop_uncertain(self.root, changed_operation, "SYNTHETIC_UNCERTAIN")
        changed_store.value = "SYNTHETIC_UNRELATED_VALUE"
        stopped = recovery.recover_credential(
            self.root, operation_id=changed_operation, binding=binding,
            authority=self.authority, store=changed_store, authenticate=self.authenticates,
        )
        self.assertEqual(
            (stopped["state"], stopped["last_error_code"]),
            ("FAILED_SAFE", "UNCERTAIN_KEYCHAIN_VALUE_NOT_OWNED"),
        )
        self.assertEqual(changed_store.value, "SYNTHETIC_UNRELATED_VALUE")

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
        self.assertEqual(
            recovery._activate_candidate(
                self.root, "forge-consumer-recovery-post-central-001", self.binding(),
                str(store.value),
            )[0],
            pending["credential_id"],
        )
        result = recovery.recover_credential(
            self.root, operation_id="forge-consumer-recovery-post-central-001", binding=self.binding(),
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertTrue(str(pending["credential_id"]).startswith("recovery-pending-forge-"))
        self.assertTrue(str(result["credential_id"]).startswith("production-forge-recovery-"))
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)

    def test_different_operation_is_serialized_on_the_fixed_destination(self) -> None:
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
        second_result: list[dict[str, object]] = []

        def second() -> None:
            second_result.append(recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-concurrent-b", binding=binding,
                authority=self.authority, store=store, authenticate=self.authenticates,
            ))

        second_thread = Thread(target=second)
        second_thread.start()
        self.assertTrue(second_thread.is_alive())
        release.set()
        thread.join(5)
        second_thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(observed, [])
        self.assertEqual(second_result[0]["disposition"], "REUSED")
        self.assertEqual(store.writes, 1)

    def test_same_operation_replay_is_exclusive_across_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        shared_keychain = (context.Array(ctypes.c_char, 256), context.Value("i", 0))
        entered, release = context.Event(), context.Event()
        results = context.Queue()
        arguments = (
            str(self.root), "forge-consumer-recovery-process-replay",
            self.binding(), self.authority, shared_keychain,
        )
        first = context.Process(
            target=_multiprocess_recovery_worker,
            args=(*arguments, "SYNTHETIC_PROCESS_A", entered, release, results),
        )
        first.start()
        self.assertTrue(entered.wait(10))
        descriptor = os.open(
            self.root / recovery._DESTINATION_LOCK_FILENAME, os.O_RDWR,
        )
        try:
            with self.assertRaises(BlockingIOError):
                recovery.fcntl.flock(
                    descriptor, recovery.fcntl.LOCK_EX | recovery.fcntl.LOCK_NB,
                )
        finally:
            os.close(descriptor)
        second = context.Process(
            target=_multiprocess_recovery_worker,
            args=(*arguments, "SYNTHETIC_PROCESS_B", None, None, results),
        )
        second.start()
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))
        observed = [results.get(timeout=5), results.get(timeout=5)]
        self.assertEqual({item[0] for item in observed}, {"OK"})
        self.assertEqual({item[1] for item in observed}, {"SUCCEEDED"})
        self.assertEqual({item[2] for item in observed}, {"REPLACED"})
        self.assertEqual(len({item[3] for item in observed}), 1)
        self.assertEqual(shared_keychain[1].value, 1)
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)

    def test_different_operations_share_one_process_safe_destination(self) -> None:
        context = multiprocessing.get_context("spawn")
        shared_keychain = (context.Array(ctypes.c_char, 256), context.Value("i", 0))
        entered, release = context.Event(), context.Event()
        results = context.Queue()
        common = (str(self.root), self.binding(), self.authority, shared_keychain)
        first = context.Process(
            target=_multiprocess_recovery_worker,
            args=(
                common[0], "forge-consumer-recovery-process-a", common[1], common[2],
                common[3], "SYNTHETIC_PROCESS_A", entered, release, results,
            ),
        )
        second = context.Process(
            target=_multiprocess_recovery_worker,
            args=(
                common[0], "forge-consumer-recovery-process-b", common[1], common[2],
                common[3], "SYNTHETIC_PROCESS_B", None, None, results,
            ),
        )
        first.start()
        self.assertTrue(entered.wait(10))
        second.start()
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))
        observed = [results.get(timeout=5), results.get(timeout=5)]
        self.assertEqual({item[0] for item in observed}, {"OK"})
        self.assertEqual({item[2] for item in observed}, {"REPLACED", "REUSED"})
        self.assertEqual(shared_keychain[1].value, 1)
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)

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

    def test_native_adapter_executes_fixed_target_security_framework_contract(self) -> None:
        core = FakeCoreFoundation()
        security = FakeSecurityFramework(core)
        with patch(
            "engineering_platform.owner_credential_recovery.ctypes.CDLL",
            side_effect=(security, core),
        ):
            store = recovery.NativeMacOSKeychainStore()
        self.assertEqual(
            repr(store),
            "NativeMacOSKeychainStore(service='forge.ep', account='consumer')",
        )
        constant = lambda *_args, **_kwargs: ctypes.c_void_p(1)
        with patch.object(store, "_constant", side_effect=constant):
            security.copy_status = store.ITEM_NOT_FOUND
            self.assertIsNone(store.read())

            security.copy_status = store.SUCCESS
            security.material = b"synthetic-native-material"
            self.assertEqual(store.read(), "synthetic-native-material")

            security.material = b"\xff"
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "MATERIAL_INVALID"):
                store.read()

            security.material = None
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "OPERATION_FAILED"):
                store.read()

            security.update_status = store.SUCCESS
            store.replace("replacement-one")
            security.update_status = store.ITEM_NOT_FOUND
            security.add_status = store.SUCCESS
            store.replace("replacement-two")
            security.add_status = store.AUTH_FAILED
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "AUTHENTICATION_FAILED"):
                store.replace("replacement-three")

            security.delete_status = store.SUCCESS
            store.delete()
            security.delete_status = store.ITEM_NOT_FOUND
            store.delete()
            security.delete_status = store.NOT_AVAILABLE
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "KEYCHAIN_UNAVAILABLE"):
                store.delete()

            original_string = core.CFStringCreateWithCString.callback
            core.CFStringCreateWithCString.callback = lambda *_: 0
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "API_UNAVAILABLE"):
                store._string("unavailable")
            core.CFStringCreateWithCString.callback = original_string
            original_data = core.CFDataCreate.callback
            core.CFDataCreate.callback = lambda *_: 0
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "API_UNAVAILABLE"):
                store._data("unavailable")
            core.CFDataCreate.callback = original_data
            original_dictionary = core.CFDictionaryCreateMutable.callback
            core.CFDictionaryCreateMutable.callback = lambda *_: 0
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "API_UNAVAILABLE"):
                store._dictionary()
            core.CFDictionaryCreateMutable.callback = original_dictionary

        self.assertEqual(store._failure(store.INTERACTION_NOT_ALLOWED).code,
                         "KEYCHAIN_LOCKED_OR_INTERACTION_NOT_ALLOWED")
        self.assertEqual(store._failure(12345).code, "KEYCHAIN_OPERATION_FAILED")
        with patch(
            "engineering_platform.owner_credential_recovery.ctypes.CDLL",
            side_effect=OSError("framework absent"),
        ):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "NATIVE_KEYCHAIN_UNAVAILABLE"):
                recovery.NativeMacOSKeychainStore()

    def test_additional_readback_and_authority_fail_closed_branches(self) -> None:
        binding = self.binding()
        self.assertEqual(
            recovery.readback_from_data_root(
                self.root, expected_instance_id=self.instance, project_id="forge",
                repository_id="forge", expected_consumer_id=binding.consumer_id,
                peer_binding_id=binding.peer_binding_id, peer_runtime_id=binding.peer_runtime_id,
                peer_configuration_digest=binding.peer_configuration_digest,
            ).consumer_id,
            binding.consumer_id,
        )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "PEER_BINDING_METADATA_INVALID"):
            with self.connection() as connection:
                recovery.scoped_consumer_readback(
                    connection, expected_instance_id=self.instance, project_id="forge",
                    repository_id="forge", expected_consumer_id=binding.consumer_id,
                    peer_binding_id="invalid binding", peer_runtime_id=binding.peer_runtime_id,
                    peer_configuration_digest=binding.peer_configuration_digest,
                )
        with self.connection() as connection:
            connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='forge'")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "PROJECT_SCOPE_NOT_ACTIVE"):
            self.binding()
        with self.connection() as connection:
            connection.execute("UPDATE ep_project_registrations SET status='ACTIVE' WHERE project_id='forge'")
            connection.execute("UPDATE ep_repository_registrations SET role='child' WHERE repository_id='forge'")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "REPOSITORY_SCOPE_NOT_AUTHORITY"):
            self.binding()
        with self.connection() as connection:
            connection.execute("UPDATE ep_repository_registrations SET role='authority' WHERE repository_id='forge'")
            connection.execute("UPDATE ep_local_repository_bindings SET state='UNBOUND' WHERE repository_id='forge'")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "REPOSITORY_SCOPE_NOT_BOUND"):
            self.binding()
        with self.connection() as connection:
            connection.execute("UPDATE ep_local_repository_bindings SET state='BOUND' WHERE repository_id='forge'")
            connection.execute("UPDATE ep_consumer_credentials SET revoked_at='2025-12-31T00:00:00+00:00' "
                               "WHERE consumer_id='forge-managed-e2e'")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "FORGE_CONSUMER_BINDING_NOT_FOUND"):
            self.binding()

        runtime = {
            "running": True, "instance_id": self.instance, "store": "ready",
            "components": {"ep_server": {"critical": True, "healthy": True}},
        }
        selected = Path("/installed/python")
        with patch("engineering_platform.owner_credential_recovery.configured_interpreter",
                   return_value=Path("/another/python")):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "INTERPRETER_BINDING"):
                recovery.validate_owner_authority(
                    self.root, expected_instance_id=self.instance, selected_interpreter=selected,
                    running_status=runtime, effective_uid=os.geteuid(),
                )
        with patch("engineering_platform.owner_credential_recovery.configured_interpreter",
                   return_value=selected):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "REQUIRED_RUNTIME"):
                recovery.validate_owner_authority(
                    self.root, expected_instance_id=self.instance, selected_interpreter=selected,
                    running_status={**runtime, "components": "invalid"}, effective_uid=os.geteuid(),
                )

    def test_recovery_rejects_wrong_existing_scope_and_reports_replay_status(self) -> None:
        binding = self.binding()
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "OPERATION_ID_INVALID"):
            recovery.recover_credential(
                self.root, operation_id="bad", binding=binding, authority=self.authority,
                store=FakeKeychain(), authenticate=self.authenticates,
            )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "OPERATION_ID_INVALID"):
            recovery.recovery_status(self.root, "bad")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "OPERATION_NOT_FOUND"):
            recovery.recovery_status(self.root, "forge-consumer-recovery-missing-001")

        wrong = FakeKeychain("human-token")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "SCOPE_MISMATCH"):
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-wrong-scope-001",
                binding=binding, authority=self.authority, store=wrong,
                authenticate=self.authenticates,
            )
        failed = recovery.recovery_status(self.root, "forge-consumer-recovery-wrong-scope-001")
        self.assertEqual((failed["state"], failed["ready"]), ("FAILED_SAFE", False))
        self.assertEqual(
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-wrong-scope-001",
                binding=binding, authority=self.authority, store=wrong,
                authenticate=self.authenticates,
            )["state"],
            "FAILED_SAFE",
        )

        rejected = FakeKeychain("old-two")
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "EXISTING_KEYCHAIN"):
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-existing-rejected-001",
                binding=binding, authority=self.authority, store=rejected,
                authenticate=lambda *_: False,
            )
        uncertain = FakeKeychain()
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "WRITE_OUTCOME_UNCERTAIN"):
            with patch.object(uncertain, "replace", side_effect=RuntimeError("unknown write")):
                recovery.recover_credential(
                    self.root, operation_id="forge-consumer-recovery-write-uncertain-001",
                    binding=binding, authority=self.authority, store=uncertain,
                    authenticate=self.authenticates,
                )

    def test_exact_scoped_revoked_keychain_value_is_safely_replaced(self) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE ep_consumer_credentials SET revoked_at=? "
                "WHERE credential_id='production-one'",
                ("2026-09-16T00:00:00+00:00",),
            )
        store = FakeKeychain("old-one")
        result = recovery.recover_credential(
            self.root,
            operation_id="forge-consumer-recovery-revoked-value-001",
            binding=self.binding(),
            authority=self.authority,
            store=store,
            authenticate=self.authenticates,
        )
        self.assertEqual((result["state"], result["disposition"]), ("SUCCEEDED", "REPLACED"))
        self.assertNotEqual(store.value, "old-one")
        with self.connection() as connection:
            old = connection.execute(
                "SELECT revoked_at,replaced_by_credential_id FROM ep_consumer_credentials "
                "WHERE credential_id='production-one'"
            ).fetchone()
        self.assertEqual(old, ("2026-09-16T00:00:00+00:00", None))

    def test_activated_replay_without_keychain_material_stops_uncertain(self) -> None:
        store = FakeKeychain()

        def interrupt() -> None:
            raise recovery.RecoveryInterrupted("simulated")

        with patch("engineering_platform.owner_credential_recovery.secrets.token_urlsafe",
                   return_value=SYNTHETIC_SECRET):
            with self.assertRaises(recovery.RecoveryInterrupted):
                recovery.recover_credential(
                    self.root, operation_id="forge-consumer-recovery-missing-replay-material",
                    binding=self.binding(), authority=self.authority, store=store,
                    authenticate=self.authenticates, after_central_activation=interrupt,
                )
        store.value = None
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "MATERIAL_UNAVAILABLE"):
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-missing-replay-material",
                binding=self.binding(), authority=self.authority, store=store,
                authenticate=self.authenticates,
            )
        self.assertEqual(
            recovery.recovery_status(
                self.root, "forge-consumer-recovery-missing-replay-material"
            )["state"],
            "STOPPED_UNCERTAIN",
        )
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "CONCURRENT_RECOVERY"):
            recovery.recover_credential(
                self.root, operation_id="forge-consumer-recovery-cannot-bypass-uncertain",
                binding=self.binding(), authority=self.authority, store=store,
                authenticate=self.authenticates,
            )
        reconciled = recovery.recover_credential(
            self.root, operation_id="forge-consumer-recovery-missing-replay-material",
            binding=self.binding(), authority=self.authority, store=store,
            authenticate=self.authenticates,
        )
        self.assertEqual(
            (reconciled["state"], reconciled["last_error_code"]),
            ("FAILED_SAFE", "UNCERTAIN_CANDIDATE_RECONCILED_ABORTED"),
        )
        with self.connection() as connection:
            pending = connection.execute(
                "SELECT revoked_at FROM ep_consumer_credentials WHERE credential_id=?",
                (reconciled["credential_id"],),
            ).fetchone()
        self.assertIsNotNone(pending[0])
        self.assertEqual(self.active_credentials("forge-managed-e2e"), 2)

    def test_owner_http_authentication_and_cli_routes_use_derived_identity(self) -> None:
        binding = self.binding()
        payload = {
            "contract_version": "1.1",
            "instance": {"id": binding.instance_id},
            "contracts": {"producer_readback": ["1.2"], "terminal_evidence": ["1.4"]},
            "authentication": {
                "consumer_id": binding.consumer_id, "consumer_status": "ACTIVE",
                "project_id": binding.project_id, "project_status": "ACTIVE",
                "repository_id": binding.repository_id, "repository_role": "authority",
                "local_repository_binding": "BOUND",
                "submission_authorization": "AUTHORIZED",
            },
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return json.dumps(payload).encode()

        class Opener:
            request = None

            def open(self, request, **_kwargs):
                self.request = request
                return Response()

        configuration = server.ServerConfiguration(
            3, "localhost", 8765, "/managed/codex", "2.3.66",
        )
        opener = Opener()
        with patch.object(server.ServerConfiguration, "load", return_value=configuration), \
             patch("engineering_platform.server.build_opener", return_value=opener):
            authenticate = server._owner_credential_authenticate(self.root)
            self.assertTrue(authenticate("synthetic-http-material", binding))
            self.assertEqual(opener.request.full_url, "http://127.0.0.1:8765/v1/producer-compatibility")
            payload["authentication"]["consumer_id"] = "another-consumer"
            self.assertFalse(authenticate("synthetic-http-material", binding))
            operation_id = "forge-consumer-recovery-http-probe"
            payload.clear()
            payload.update({
                "contract_version": "1.0", "instance_id": binding.instance_id,
                "operation_id": operation_id, "consumer_id": binding.consumer_id,
                "project_id": binding.project_id, "repository_id": binding.repository_id,
                "credential_status": "PENDING_RECOVERY_PROBE",
                "authorization": "RECOVERY_PROBE_ONLY",
            })
            candidate_authenticate = server._owner_credential_authenticate(
                self.root, operation_id,
            )
            self.assertTrue(candidate_authenticate("synthetic-http-material", binding))
            self.assertEqual(
                opener.request.full_url,
                "http://127.0.0.1:8765/v1/owner-credential-recovery-probe",
            )
            self.assertEqual(
                dict(opener.request.header_items())["Ep-recovery-operation-id"], operation_id,
            )
        with patch.object(
            server.ServerConfiguration, "load",
            return_value=server.ServerConfiguration(3, "192.0.2.1", 8765, "/managed/codex", "2.3.66"),
        ):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "LOOPBACK_HTTP"):
                server._owner_credential_authenticate(self.root)

        common = [
            "--data-root", str(self.root), "--expected-instance-id", self.instance,
            "--project-id", "forge", "--repository-id", "forge",
            "--consumer-id", binding.consumer_id, "--peer-binding-id", binding.peer_binding_id,
            "--peer-runtime-id", binding.peer_runtime_id,
            "--peer-configuration-digest", binding.peer_configuration_digest,
        ]
        result = {"operation_id": "forge-consumer-recovery-cli-001", "state": "SUCCEEDED",
                  "instance_id": self.instance, "ready": True}
        with patch("engineering_platform.server._owner_binding_from_args", return_value=binding), \
             patch("engineering_platform.server._owner_credential_authority", return_value=self.authority), \
             patch("engineering_platform.server.owner_credential_recovery.NativeMacOSKeychainStore",
                   return_value=FakeKeychain("old-two")), \
             patch("engineering_platform.server.owner_credential_recovery.recover_credential",
                   return_value=result), redirect_stdout(StringIO()):
            self.assertEqual(server.main(["owner-consumer-readback", *common]), 0)
            self.assertEqual(server.main([
                "owner-credential-recover", *common, "--operation-id",
                "forge-consumer-recovery-cli-001",
            ]), 0)
            self.assertEqual(server.main(["owner-credential-recover", *common]), 2)
        with patch("engineering_platform.server.owner_credential_recovery.recovery_status",
                   return_value=result), \
             patch("engineering_platform.server._owner_credential_authority", return_value=self.authority), \
             redirect_stdout(StringIO()):
            self.assertEqual(server.main([
                "owner-credential-recovery-status", "--data-root", str(self.root),
                "--operation-id", "forge-consumer-recovery-cli-001",
            ]), 0)
            self.assertEqual(server.main([
                "owner-credential-recovery-status", "--data-root", str(self.root),
            ]), 2)

    def test_server_owner_authority_binds_installed_interpreter_and_instance(self) -> None:
        selected = Path(os.sys.executable).absolute()
        installation = SimpleNamespace(instance_id=self.instance)
        package = {"version": "2.3.66"}
        with patch("engineering_platform.server.server_service.configured_interpreter", return_value=None):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "BINDING_UNAVAILABLE"):
                server._owner_credential_authority(self.root, self.instance)
        with patch("engineering_platform.server.server_service.configured_interpreter",
                   return_value=Path("/another/python")):
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "INSTALLED_INTERPRETER"):
                server._owner_credential_authority(self.root, self.instance)
        with patch("engineering_platform.server.server_service.configured_interpreter", return_value=selected), \
             patch("engineering_platform.server.operational_installation.resolve", return_value=installation), \
             patch("engineering_platform.server.operational_installation.package_identity", return_value=package), \
             patch("engineering_platform.server.operational_installation.validate_package_identity"), \
             patch("engineering_platform.server.operational_installation.record_status", return_value={}), \
             patch("engineering_platform.server.operational_installation.validate_registered_package_identity"), \
             patch("engineering_platform.server.status", return_value={}), \
             patch("engineering_platform.server.owner_credential_recovery.validate_owner_authority",
                   return_value=self.authority) as validate:
            self.assertEqual(server._owner_credential_authority(self.root, self.instance), self.authority)
            validate.assert_called_once()
            with self.assertRaisesRegex(recovery.CredentialRecoveryError, "INSTANCE_MISMATCH"):
                server._owner_credential_authority(self.root, "another-instance")

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
        self.assertIn("INSTALLED_INTERPRETER_BINDING_UNAVAILABLE", output.getvalue())
        self.assertFalse(absent.exists())

    def test_schema_63_installation_upgrades_to_durable_recovery_journal(self) -> None:
        with self.connection() as connection:
            connection.execute("DROP INDEX ep_consumer_credential_recovery_scope_lookup")
            connection.execute("DROP INDEX ep_consumer_credential_recovery_active_scope")
            connection.execute("DROP TABLE ep_consumer_credential_recovery_operations")
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version>=64")
            connection.execute(
                "INSERT INTO engineering_schema_migrations(version) VALUES(63)"
            )
            connection.execute(
                "UPDATE engineering_metadata SET value='63' "
                "WHERE key='installation.schema_version'"
            )
            connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema65")
            connection.execute(
                "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL CHECK(schema_version IN "
                "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63)))"
            )
            connection.execute(
                "INSERT INTO ep_installations SELECT instance_id,created_at,63 "
                "FROM ep_installations_schema65"
            )
            connection.execute("DROP TABLE ep_installations_schema65")

        identity = server.initialize(self.root)
        report = server.validate_store(self.root, identity)
        self.assertEqual(report["schema_version"], server.SERVER_STORE_SCHEMA_VERSION)
        with self.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM ep_installations"
                ).fetchone(),
                (server.SERVER_STORE_SCHEMA_VERSION,),
            )
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='ep_consumer_credential_recovery_operations'"
            ).fetchone())

    def test_schema_64_migration_preserves_legacy_uncertain_and_successor_receipts(self) -> None:
        binding = self.binding()
        first_id = "forge-consumer-recovery-legacy-uncertain-001"
        second_id = "forge-consumer-recovery-legacy-successor-001"
        recovery._prepare_operation(self.root, first_id, binding, self.authority)
        recovery._stop_uncertain(self.root, first_id, "LEGACY_UNCERTAIN_FIXTURE")
        with self.connection() as connection:
            connection.execute("DROP INDEX ep_consumer_credential_recovery_active_scope")
            connection.execute(
                "CREATE UNIQUE INDEX ep_consumer_credential_recovery_active_scope "
                "ON ep_consumer_credential_recovery_operations(consumer_id,project_id) "
                "WHERE state IN ('PREPARED','CENTRAL_ACTIVATED')"
            )
            connection.execute(
                "INSERT INTO ep_consumer_credential_recovery_operations("
                "operation_id,instance_id,consumer_id,project_id,repository_id,peer_binding_id,"
                "peer_runtime_id,peer_configuration_digest,operator_uid,keychain_service,keychain_account,"
                "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'PREPARED',?,?)",
                (
                    second_id, self.instance, binding.consumer_id, binding.project_id,
                    binding.repository_id, binding.peer_binding_id, binding.peer_runtime_id,
                    binding.peer_configuration_digest, self.authority.operator_uid,
                    recovery.KEYCHAIN_SERVICE, recovery.KEYCHAIN_ACCOUNT,
                    "9999-01-01T00:00:00+00:00", "9999-01-01T00:00:00+00:00",
                ),
            )
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema65")
            connection.execute(
                "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL CHECK(schema_version IN "
                "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64)))"
            )
            connection.execute(
                "INSERT INTO ep_installations SELECT instance_id,created_at,64 "
                "FROM ep_installations_schema65"
            )
            connection.execute("DROP TABLE ep_installations_schema65")
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version>=65")
            connection.execute(
                "INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(64)"
            )
            connection.execute(
                "UPDATE engineering_metadata SET value='64' "
                "WHERE key='installation.schema_version'"
            )

        identity = server.initialize(self.root)
        self.assertEqual(
            server.validate_store(self.root, identity)["schema_version"],
            server.SERVER_STORE_SCHEMA_VERSION,
        )
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,state FROM ep_consumer_credential_recovery_operations "
                "ORDER BY created_at,operation_id"
            ).fetchall()
            index = next(
                row for row in connection.execute(
                    "PRAGMA index_list(ep_consumer_credential_recovery_operations)"
                ) if row[1] == "ep_consumer_credential_recovery_active_scope"
            )
        self.assertEqual(rows, [(first_id, "STOPPED_UNCERTAIN"), (second_id, "PREPARED")])
        self.assertEqual(index[2], 0, "legacy overlapping receipts require an ordered non-unique index")

        store = FakeKeychain()
        with self.assertRaisesRegex(recovery.CredentialRecoveryError, "CONCURRENT_RECOVERY"):
            recovery.recover_credential(
                self.root, operation_id=second_id, binding=binding,
                authority=self.authority, store=store, authenticate=self.authenticates,
            )
        first = recovery.recover_credential(
            self.root, operation_id=first_id, binding=binding,
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        second = recovery.recover_credential(
            self.root, operation_id=second_id, binding=binding,
            authority=self.authority, store=store, authenticate=self.authenticates,
        )
        self.assertEqual((first["state"], first["disposition"]), ("SUCCEEDED", "REPLACED"))
        self.assertEqual((second["state"], second["disposition"]), ("SUCCEEDED", "REUSED"))
        self.assertEqual(store.writes, 1)

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
