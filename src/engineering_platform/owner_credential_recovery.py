"""Installed-owner recovery for one existing Forge HTTP consumer credential.

This is deliberately a local maintenance boundary.  It is not an HTTP admin
endpoint, does not register a consumer, and never accepts a secret destination
or plaintext credential from the caller.  CENTRAL remains credential authority;
the current user's macOS Keychain is only the fixed consumer-side destination.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Protocol

from .ep_consumer_credentials import (
    MAX_ACTIVE_PRODUCTION_CREDENTIALS,
    PRODUCTION_PREFIX,
    fingerprint,
    verifier,
)
from .server_service import configured_interpreter


KEYCHAIN_SERVICE = "forge.ep"
KEYCHAIN_ACCOUNT = "consumer"
KEYCHAIN_REFERENCE = "keychain://forge.ep/consumer"
OPERATION_PREFIX = "forge-consumer-recovery-"
RECOVERY_CANDIDATE_PREFIX = "recovery-pending-forge-"
RECOVERY_CREDENTIAL_PREFIX = "production-forge-recovery-"
_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED_SAFE"})
_DESTINATION_LOCK_FILENAME = ".forge-ep-consumer-recovery.lock"
_CANDIDATE_VALIDITY = timedelta(minutes=15)


class CredentialRecoveryError(RuntimeError):
    """A typed, secret-free owner-recovery failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RecoveryInterrupted(RuntimeError):
    """Test seam representing process loss at a durable reconciliation edge."""


class KeychainStore(Protocol):
    def read(self) -> str | None: ...
    def replace(self, value: str) -> None: ...
    def delete(self) -> None: ...


@dataclass(frozen=True)
class OwnerAuthority:
    instance_id: str
    operator_uid: int
    interpreter: str
    data_root: str
    central_integrity: str
    required_capabilities: tuple[str, ...]
    optional_degraded_capabilities: tuple[str, ...]

    def safe_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "operator_uid": self.operator_uid,
            "interpreter": self.interpreter,
            "data_root": self.data_root,
            "central_integrity": self.central_integrity,
            "required_capabilities": list(self.required_capabilities),
            "optional_degraded_capabilities": list(self.optional_degraded_capabilities),
        }


@dataclass(frozen=True)
class ConsumerBinding:
    instance_id: str
    consumer_id: str
    project_id: str
    repository_id: str
    first_exchange_submission_id: str
    first_exchange_receipt_id: str
    first_exchange_recorded_at: str
    credential_ids: tuple[str, ...]
    peer_binding_id: str
    peer_runtime_id: str
    peer_configuration_digest: str

    def safe_dict(self) -> dict[str, object]:
        return {
            "status": "EXACT",
            "instance_id": self.instance_id,
            "consumer_id": self.consumer_id,
            "project_id": self.project_id,
            "repository_id": self.repository_id,
            "peer_binding_id": self.peer_binding_id,
            "peer_runtime_id": self.peer_runtime_id,
            "peer_configuration_digest": self.peer_configuration_digest,
            "credential_destination": KEYCHAIN_REFERENCE,
            "binding_evidence": {
                "consumer_registration": "ACTIVE",
                "project_registration": "ACTIVE",
                "repository_role": "authority",
                "local_repository_binding": "BOUND",
                "first_forge_exchange_submission_id": self.first_exchange_submission_id,
                "first_forge_exchange_receipt_id": self.first_exchange_receipt_id,
                "first_forge_exchange_recorded_at": self.first_exchange_recorded_at,
                "historically_eligible_consumer_count": 1,
                "active_production_credential_ids": list(self.credential_ids),
            },
        }


def install_schema(connection: sqlite3.Connection) -> None:
    """Install the durable, secret-free operation journal."""

    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS ep_consumer_credential_recovery_operations (
            operation_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
            consumer_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
            peer_binding_id TEXT NOT NULL,
            peer_runtime_id TEXT NOT NULL,
            peer_configuration_digest TEXT NOT NULL,
            adopted_from_peer_configuration_digest TEXT,
            configuration_adopted_at TEXT,
            operator_uid INTEGER NOT NULL CHECK(operator_uid > 0),
            keychain_service TEXT NOT NULL CHECK(keychain_service='{KEYCHAIN_SERVICE}'),
            keychain_account TEXT NOT NULL CHECK(keychain_account='{KEYCHAIN_ACCOUNT}'),
            state TEXT NOT NULL CHECK(state IN ('PREPARED','CENTRAL_ACTIVATED','SUCCEEDED','FAILED_SAFE','STOPPED_UNCERTAIN')),
            disposition TEXT CHECK(disposition IN ('REUSED','REPLACED')),
            credential_id TEXT REFERENCES ep_consumer_credentials(credential_id),
            replaced_credential_id TEXT REFERENCES ep_consumer_credentials(credential_id),
            credential_fingerprint TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            last_error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS ep_consumer_credential_recovery_active_scope
            ON ep_consumer_credential_recovery_operations(consumer_id,project_id)
            WHERE state IN ('PREPARED','CENTRAL_ACTIVATED','STOPPED_UNCERTAIN');
        CREATE INDEX IF NOT EXISTS ep_consumer_credential_recovery_scope_lookup
            ON ep_consumer_credential_recovery_operations(project_id,consumer_id,created_at DESC);
        """
    )


def _now() -> str:
    return _utcnow_datetime().isoformat()


def _utcnow_datetime() -> datetime:
    return datetime.now(UTC)


def _require_peer_metadata(
    *, peer_binding_id: str, peer_runtime_id: str, peer_configuration_digest: str,
) -> None:
    if (
        _IDENTITY.fullmatch(peer_binding_id) is None
        or _IDENTITY.fullmatch(peer_runtime_id) is None
        or _DIGEST.fullmatch(peer_configuration_digest) is None
    ):
        raise CredentialRecoveryError("PEER_BINDING_METADATA_INVALID")


def scoped_consumer_readback(
    connection: sqlite3.Connection,
    *,
    expected_instance_id: str,
    project_id: str,
    repository_id: str,
    expected_consumer_id: str | None,
    peer_binding_id: str,
    peer_runtime_id: str,
    peer_configuration_digest: str,
) -> ConsumerBinding:
    """Identify one historical Forge consumer through multiple owned records.

    The consumer is not inferred from its name or the Keychain account.  The
    decisive record is the earliest immutable accepted Forge HTTP exchange for
    the exact active project/repository topology.  Exactly one production
    consumer must have been registered and credentialed when that exchange was
    accepted; a later bootstrap consumer therefore cannot become the binding.
    """

    _require_peer_metadata(
        peer_binding_id=peer_binding_id,
        peer_runtime_id=peer_runtime_id,
        peer_configuration_digest=peer_configuration_digest,
    )
    installation = connection.execute(
        "SELECT instance_id FROM ep_installations"
    ).fetchall()
    if len(installation) != 1 or str(installation[0][0]) != expected_instance_id:
        raise CredentialRecoveryError("INSTALLATION_INSTANCE_MISMATCH")
    project = connection.execute(
        "SELECT status FROM ep_project_registrations WHERE project_id=?", (project_id,)
    ).fetchone()
    if project is None:
        raise CredentialRecoveryError("PROJECT_SCOPE_NOT_FOUND")
    if str(project[0]) != "ACTIVE":
        raise CredentialRecoveryError("PROJECT_SCOPE_NOT_ACTIVE")
    repository = connection.execute(
        "SELECT authority_repository_id,role FROM ep_repository_registrations "
        "WHERE project_id=? AND repository_id=?",
        (project_id, repository_id),
    ).fetchone()
    if repository is None:
        raise CredentialRecoveryError("REPOSITORY_SCOPE_NOT_FOUND")
    if (str(repository[0]), str(repository[1])) != (repository_id, "authority"):
        raise CredentialRecoveryError("REPOSITORY_SCOPE_NOT_AUTHORITY")
    local_binding = connection.execute(
        "SELECT state FROM ep_local_repository_bindings WHERE project_id=? AND repository_id=?",
        (project_id, repository_id),
    ).fetchone()
    if local_binding is None or str(local_binding[0]) != "BOUND":
        raise CredentialRecoveryError("REPOSITORY_SCOPE_NOT_BOUND")
    exchange = connection.execute(
        "SELECT s.submission_id,a.receipt_id,a.recorded_at FROM ep_forge_exchange_audit a "
        "JOIN ep_submissions s ON s.submission_id=a.submission_id AND s.project_id=a.project_id "
        "WHERE a.project_id=? AND s.repository_id=? AND s.producer_id='forge' "
        "AND s.producer_type='FORGE' AND s.transport='HTTP' "
        "AND a.direction='FORGE_TO_EP' AND a.event_kind='FORGE_SUBMISSION_ACCEPTED' "
        "ORDER BY a.recorded_at,a.submission_id LIMIT 1",
        (project_id, repository_id),
    ).fetchone()
    if exchange is None:
        raise CredentialRecoveryError("FORGE_BINDING_EXCHANGE_NOT_FOUND")
    exchange_at = str(exchange[2])
    candidates = connection.execute(
        "SELECT DISTINCT r.consumer_id,r.status FROM ep_consumer_registrations r "
        "JOIN ep_consumer_credentials c ON c.consumer_id=r.consumer_id AND c.project_id=r.project_id "
        "WHERE r.project_id=? AND r.created_at<=? AND c.issued_at<=? "
        "AND c.credential_id LIKE ? AND (c.revoked_at IS NULL OR c.revoked_at>?) "
        "AND (c.expires_at IS NULL OR c.expires_at>?) ORDER BY r.consumer_id",
        (project_id, exchange_at, exchange_at, f"{PRODUCTION_PREFIX}%", exchange_at, exchange_at),
    ).fetchall()
    if not candidates:
        raise CredentialRecoveryError("FORGE_CONSUMER_BINDING_NOT_FOUND")
    if len(candidates) != 1:
        raise CredentialRecoveryError("FORGE_CONSUMER_BINDING_AMBIGUOUS")
    consumer_id, state = str(candidates[0][0]), str(candidates[0][1])
    if expected_consumer_id is not None:
        if _IDENTITY.fullmatch(expected_consumer_id) is None:
            raise CredentialRecoveryError("PEER_CONSUMER_ID_INVALID")
        if consumer_id != expected_consumer_id:
            raise CredentialRecoveryError("PEER_CONSUMER_BINDING_MISMATCH")
    if state == "DISABLED":
        raise CredentialRecoveryError("FORGE_CONSUMER_DISABLED")
    if state == "REVOKED":
        raise CredentialRecoveryError("FORGE_CONSUMER_REVOKED")
    if state != "ACTIVE":
        raise CredentialRecoveryError("FORGE_CONSUMER_NOT_ACTIVE")
    credentials = connection.execute(
        "SELECT credential_id FROM ep_consumer_credentials "
        "WHERE consumer_id=? AND project_id=? AND credential_id LIKE ? "
        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) "
        "ORDER BY issued_at,credential_id",
        (consumer_id, project_id, f"{PRODUCTION_PREFIX}%"),
    ).fetchall()
    if not credentials:
        raise CredentialRecoveryError("FORGE_CONSUMER_HAS_NO_ACTIVE_CREDENTIAL")
    return ConsumerBinding(
        instance_id=expected_instance_id,
        consumer_id=consumer_id,
        project_id=project_id,
        repository_id=repository_id,
        first_exchange_submission_id=str(exchange[0]),
        first_exchange_receipt_id=str(exchange[1]),
        first_exchange_recorded_at=exchange_at,
        credential_ids=tuple(str(row[0]) for row in credentials),
        peer_binding_id=peer_binding_id,
        peer_runtime_id=peer_runtime_id,
        peer_configuration_digest=peer_configuration_digest,
    )


class NativeMacOSKeychainStore:
    """Security.framework generic-password adapter for the one fixed target."""

    SUCCESS = 0
    DUPLICATE_ITEM = -25299
    ITEM_NOT_FOUND = -25300
    AUTH_FAILED = -25293
    INTERACTION_NOT_ALLOWED = -25308
    NOT_AVAILABLE = -25291
    _UTF8 = 0x08000100

    def __init__(self) -> None:
        try:
            self._security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            self._core = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as error:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_UNAVAILABLE") from error
        self._configure()

    def __repr__(self) -> str:
        return "NativeMacOSKeychainStore(service='forge.ep', account='consumer')"

    def _configure(self) -> None:
        self._core.CFStringCreateWithCString.argtypes = (
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        )
        self._core.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._core.CFDataCreate.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)
        self._core.CFDataCreate.restype = ctypes.c_void_p
        self._core.CFDataGetLength.argtypes = (ctypes.c_void_p,)
        self._core.CFDataGetLength.restype = ctypes.c_long
        self._core.CFDataGetBytePtr.argtypes = (ctypes.c_void_p,)
        self._core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        self._core.CFDictionaryCreateMutable.argtypes = (
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
        )
        self._core.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        self._core.CFDictionarySetValue.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        )
        self._core.CFRelease.argtypes = (ctypes.c_void_p,)
        self._security.SecItemAdd.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemCopyMatching.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        )
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = (ctypes.c_void_p,)
        self._security.SecItemDelete.restype = ctypes.c_int32

    def _constant(self, name: str, *, core: bool = False) -> ctypes.c_void_p:
        library = self._core if core else self._security
        try:
            value = ctypes.c_void_p.in_dll(library, name).value
        except ValueError as error:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_API_UNAVAILABLE") from error
        if not value:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_API_UNAVAILABLE")
        return ctypes.c_void_p(value)

    def _string(self, value: str) -> ctypes.c_void_p:
        result = self._core.CFStringCreateWithCString(None, value.encode("utf-8"), self._UTF8)
        if not result:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_API_UNAVAILABLE")
        return ctypes.c_void_p(result)

    def _data(self, value: str) -> ctypes.c_void_p:
        encoded = value.encode("ascii")
        buffer = ctypes.create_string_buffer(encoded)
        result = self._core.CFDataCreate(None, ctypes.cast(buffer, ctypes.c_void_p), len(encoded))
        if not result:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_API_UNAVAILABLE")
        return ctypes.c_void_p(result)

    def _dictionary(self) -> ctypes.c_void_p:
        value = self._core.CFDictionaryCreateMutable(None, 0, None, None)
        if not value:
            raise CredentialRecoveryError("NATIVE_KEYCHAIN_API_UNAVAILABLE")
        return ctypes.c_void_p(value)

    def _set(self, dictionary: ctypes.c_void_p, key: ctypes.c_void_p, value: ctypes.c_void_p) -> None:
        self._core.CFDictionarySetValue(dictionary, key, value)

    def _query(self, *, return_data: bool = False) -> tuple[ctypes.c_void_p, list[ctypes.c_void_p]]:
        query = self._dictionary()
        temporary: list[ctypes.c_void_p] = []
        try:
            self._set(query, self._constant("kSecClass"), self._constant("kSecClassGenericPassword"))
            for key, value in (
                ("kSecAttrService", KEYCHAIN_SERVICE),
                ("kSecAttrAccount", KEYCHAIN_ACCOUNT),
            ):
                item = self._string(value)
                temporary.append(item)
                self._set(query, self._constant(key), item)
            if return_data:
                self._set(query, self._constant("kSecReturnData"), self._constant("kCFBooleanTrue", core=True))
                self._set(query, self._constant("kSecMatchLimit"), self._constant("kSecMatchLimitOne"))
            return query, temporary
        except Exception:
            self._release(temporary)
            self._core.CFRelease(query)
            raise

    def _value_attributes(self, value: str) -> tuple[ctypes.c_void_p, list[ctypes.c_void_p]]:
        attributes = self._dictionary()
        item = self._data(value)
        try:
            self._set(attributes, self._constant("kSecValueData"), item)
            self._set(
                attributes,
                self._constant("kSecAttrAccessible"),
                self._constant("kSecAttrAccessibleWhenUnlockedThisDeviceOnly"),
            )
            return attributes, [item]
        except Exception:
            self._core.CFRelease(item)
            self._core.CFRelease(attributes)
            raise

    def _release(self, values: list[ctypes.c_void_p]) -> None:
        for value in values:
            self._core.CFRelease(value)

    def _failure(self, status: int) -> CredentialRecoveryError:
        if status == self.AUTH_FAILED:
            return CredentialRecoveryError("KEYCHAIN_AUTHENTICATION_FAILED")
        if status == self.INTERACTION_NOT_ALLOWED:
            return CredentialRecoveryError("KEYCHAIN_LOCKED_OR_INTERACTION_NOT_ALLOWED")
        if status == self.NOT_AVAILABLE:
            return CredentialRecoveryError("KEYCHAIN_UNAVAILABLE")
        return CredentialRecoveryError("KEYCHAIN_OPERATION_FAILED")

    def read(self) -> str | None:
        query, temporary = self._query(return_data=True)
        result = ctypes.c_void_p()
        try:
            status = int(self._security.SecItemCopyMatching(query, ctypes.byref(result)))
            if status == self.ITEM_NOT_FOUND:
                return None
            if status != self.SUCCESS or not result.value:
                raise self._failure(status)
            length = int(self._core.CFDataGetLength(result))
            pointer = self._core.CFDataGetBytePtr(result)
            if length <= 0 or not pointer:
                raise CredentialRecoveryError("KEYCHAIN_MATERIAL_INVALID")
            try:
                return bytes(pointer[index] for index in range(length)).decode("ascii")
            except (UnicodeDecodeError, ValueError) as error:
                raise CredentialRecoveryError("KEYCHAIN_MATERIAL_INVALID") from error
        finally:
            if result.value:
                self._core.CFRelease(result)
            self._release(temporary)
            self._core.CFRelease(query)

    def replace(self, value: str) -> None:
        query, query_values = self._query()
        attributes, attribute_values = self._value_attributes(value)
        try:
            status = int(self._security.SecItemUpdate(query, attributes))
            if status == self.ITEM_NOT_FOUND:
                self._set(attributes, self._constant("kSecClass"), self._constant("kSecClassGenericPassword"))
                for key, text in (
                    ("kSecAttrService", KEYCHAIN_SERVICE),
                    ("kSecAttrAccount", KEYCHAIN_ACCOUNT),
                ):
                    item = self._string(text)
                    attribute_values.append(item)
                    self._set(attributes, self._constant(key), item)
                status = int(self._security.SecItemAdd(attributes, None))
            if status != self.SUCCESS:
                raise self._failure(status)
        finally:
            self._release(query_values)
            self._release(attribute_values)
            self._core.CFRelease(query)
            self._core.CFRelease(attributes)

    def delete(self) -> None:
        query, temporary = self._query()
        try:
            status = int(self._security.SecItemDelete(query))
            if status not in {self.SUCCESS, self.ITEM_NOT_FOUND}:
                raise self._failure(status)
        finally:
            self._release(temporary)
            self._core.CFRelease(query)


def validate_owner_authority(
    data_root: Path,
    *,
    expected_instance_id: str,
    selected_interpreter: Path,
    running_status: Mapping[str, object],
    effective_uid: int | None = None,
) -> OwnerAuthority:
    """Bind the caller to the owned installation and required live capabilities."""

    root = data_root.expanduser().resolve(strict=True)
    uid = os.geteuid() if effective_uid is None else effective_uid
    metadata = root.stat()
    if uid <= 0 or metadata.st_uid != uid or metadata.st_mode & 0o022:
        raise CredentialRecoveryError("INSTALLATION_OWNER_AUTHORITY_REQUIRED")
    configured = configured_interpreter(root)
    if configured is None or configured.absolute() != selected_interpreter.absolute():
        raise CredentialRecoveryError("INSTALLED_INTERPRETER_BINDING_MISMATCH")
    if (
        running_status.get("running") is not True
        or running_status.get("instance_id") != expected_instance_id
        or running_status.get("store") != "ready"
    ):
        raise CredentialRecoveryError("REQUIRED_RUNTIME_CAPABILITY_UNAVAILABLE")
    components = running_status.get("components")
    if not isinstance(components, Mapping):
        raise CredentialRecoveryError("REQUIRED_RUNTIME_CAPABILITY_UNAVAILABLE")
    required: list[str] = []
    optional_degraded: list[str] = []
    for identifier, value in components.items():
        if not isinstance(identifier, str) or not isinstance(value, Mapping):
            raise CredentialRecoveryError("REQUIRED_RUNTIME_CAPABILITY_UNAVAILABLE")
        if value.get("critical") is True:
            if value.get("healthy") is not True:
                raise CredentialRecoveryError("REQUIRED_RUNTIME_CAPABILITY_UNAVAILABLE")
            required.append(identifier)
        elif value.get("healthy") is not True:
            optional_degraded.append(identifier)
    database = root / "epdata.sqlite"
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            installed = connection.execute("SELECT instance_id FROM ep_installations").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise CredentialRecoveryError("CENTRAL_INTEGRITY_UNAVAILABLE") from error
    if integrity != ("ok",) or installed != [(expected_instance_id,)]:
        raise CredentialRecoveryError("CENTRAL_INTEGRITY_FAILED")
    return OwnerAuthority(
        instance_id=expected_instance_id,
        operator_uid=uid,
        interpreter=str(selected_interpreter.absolute()),
        data_root=str(root),
        central_integrity="PASS",
        required_capabilities=tuple(sorted(required)),
        optional_degraded_capabilities=tuple(sorted(optional_degraded)),
    )


def _database(data_root: Path) -> Path:
    path = data_root.expanduser().resolve() / "epdata.sqlite"
    if not path.is_file():
        raise CredentialRecoveryError("CENTRAL_DATABASE_UNAVAILABLE")
    return path


@contextmanager
def _destination_lock(data_root: Path, authority: OwnerAuthority):
    """Serialize the complete CENTRAL/Keychain transition across processes.

    The lock is tied to the one product-owned fixed destination and is held
    across every external-store read/write and every guarded journal change.
    ``flock`` is released by the kernel after process loss, so a crashed
    worker cannot leave a permanent lock behind.
    """

    root = data_root.expanduser().resolve(strict=True)
    if root != Path(authority.data_root).expanduser().resolve(strict=True):
        raise CredentialRecoveryError("RECOVERY_DATA_ROOT_AUTHORITY_MISMATCH")
    lock_path = root / _DESTINATION_LOCK_FILENAME
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise CredentialRecoveryError("RECOVERY_DESTINATION_LOCK_UNAVAILABLE") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != authority.operator_uid
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise CredentialRecoveryError("RECOVERY_DESTINATION_LOCK_UNSAFE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise CredentialRecoveryError("RECOVERY_DESTINATION_LOCK_UNAVAILABLE") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _connect(data_root: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(_database(data_root), timeout=10, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection
    except sqlite3.Error as error:
        raise CredentialRecoveryError("CENTRAL_DATABASE_UNAVAILABLE") from error


def _operation_row(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        "SELECT * FROM ep_consumer_credential_recovery_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()


def _operation_status(row: sqlite3.Row) -> dict[str, object]:
    return {
        "operation_id": str(row["operation_id"]),
        "state": str(row["state"]),
        "ready": str(row["state"]) == "SUCCEEDED",
        "disposition": row["disposition"],
        "instance_id": str(row["instance_id"]),
        "consumer_id": str(row["consumer_id"]),
        "project_id": str(row["project_id"]),
        "repository_id": str(row["repository_id"]),
        "peer_binding_id": str(row["peer_binding_id"]),
        "peer_runtime_id": str(row["peer_runtime_id"]),
        "peer_configuration_digest": str(row["peer_configuration_digest"]),
        "adopted_from_peer_configuration_digest": row["adopted_from_peer_configuration_digest"],
        "configuration_adopted_at": row["configuration_adopted_at"],
        "operator_uid": int(row["operator_uid"]),
        "credential_destination": KEYCHAIN_REFERENCE,
        "credential_id": row["credential_id"],
        "replaced_credential_id": row["replaced_credential_id"],
        "credential_fingerprint": row["credential_fingerprint"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
        "last_error_code": row["last_error_code"],
        "secret_material_exposed": False,
    }


def recovery_status(data_root: Path, operation_id: str) -> dict[str, object]:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise CredentialRecoveryError("RECOVERY_OPERATION_ID_INVALID")
    connection = _connect(data_root)
    try:
        row = _operation_row(connection, operation_id)
        if row is None:
            raise CredentialRecoveryError("RECOVERY_OPERATION_NOT_FOUND")
        return _operation_status(row)
    finally:
        connection.close()


def _same_operation(row: sqlite3.Row, binding: ConsumerBinding, authority: OwnerAuthority) -> bool:
    return (
        str(row["instance_id"]), str(row["consumer_id"]), str(row["project_id"]),
        str(row["repository_id"]), str(row["peer_binding_id"]), str(row["peer_runtime_id"]),
        str(row["peer_configuration_digest"]), int(row["operator_uid"]),
        str(row["keychain_service"]), str(row["keychain_account"]),
    ) == (
        authority.instance_id, binding.consumer_id, binding.project_id,
        binding.repository_id, binding.peer_binding_id, binding.peer_runtime_id,
        binding.peer_configuration_digest, authority.operator_uid,
        KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT,
    )


def _prepare_operation(
    data_root: Path, operation_id: str, binding: ConsumerBinding, authority: OwnerAuthority,
) -> dict[str, object]:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise CredentialRecoveryError("RECOVERY_OPERATION_ID_INVALID")
    connection = _connect(data_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _operation_row(connection, operation_id)
        if row is not None:
            if not _same_operation(row, binding, authority):
                raise CredentialRecoveryError("RECOVERY_OPERATION_IDENTITY_CONFLICT")
            if str(row["state"]) in {"PREPARED", "CENTRAL_ACTIVATED", "STOPPED_UNCERTAIN"}:
                first = connection.execute(
                    "SELECT operation_id FROM ep_consumer_credential_recovery_operations "
                    "WHERE consumer_id=? AND project_id=? "
                    "AND state IN ('PREPARED','CENTRAL_ACTIVATED','STOPPED_UNCERTAIN') "
                    "ORDER BY created_at,operation_id LIMIT 1",
                    (binding.consumer_id, binding.project_id),
                ).fetchone()
                if first is None or str(first[0]) != operation_id:
                    raise CredentialRecoveryError("CONCURRENT_RECOVERY_OPERATION_ACTIVE")
            connection.execute("COMMIT")
            return _operation_status(row)
        active = connection.execute(
            "SELECT operation_id FROM ep_consumer_credential_recovery_operations "
            "WHERE consumer_id=? AND project_id=? "
            "AND state IN ('PREPARED','CENTRAL_ACTIVATED','STOPPED_UNCERTAIN')",
            (binding.consumer_id, binding.project_id),
        ).fetchone()
        if active is not None:
            raise CredentialRecoveryError("CONCURRENT_RECOVERY_OPERATION_ACTIVE")
        now = _now()
        connection.execute(
            "INSERT INTO ep_consumer_credential_recovery_operations("
            "operation_id,instance_id,consumer_id,project_id,repository_id,peer_binding_id,"
            "peer_runtime_id,peer_configuration_digest,operator_uid,keychain_service,keychain_account,"
            "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'PREPARED',?,?)",
            (
                operation_id, authority.instance_id, binding.consumer_id, binding.project_id,
                binding.repository_id, binding.peer_binding_id, binding.peer_runtime_id,
                binding.peer_configuration_digest, authority.operator_uid,
                KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, now, now,
            ),
        )
        connection.execute("COMMIT")
        return recovery_status(data_root, operation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _credential_scope(
    connection: sqlite3.Connection, material: str,
) -> tuple[str, str, str, str | None] | None:
    row = connection.execute(
        "SELECT credential_id,consumer_id,project_id,revoked_at FROM ep_consumer_credentials "
        "WHERE verifier=?",
        (verifier(material),),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3])


def _mark_reused(
    data_root: Path, operation_id: str, credential_id: str, material: str,
) -> dict[str, object]:
    connection = _connect(data_root)
    try:
        now = _now()
        connection.execute("BEGIN IMMEDIATE")
        row = _operation_row(connection, operation_id)
        if row is None or str(row["state"]) != "PREPARED":
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        credential = connection.execute(
            "SELECT consumer_id,project_id,fingerprint,revoked_at FROM ep_consumer_credentials "
            "WHERE credential_id=? AND verifier=?",
            (credential_id, verifier(material)),
        ).fetchone()
        if credential is None or (
            str(credential[0]), str(credential[1]), bytes(credential[2]), credential[3]
        ) != (
            str(row["consumer_id"]), str(row["project_id"]), fingerprint(material), None,
        ):
            raise CredentialRecoveryError("EXISTING_KEYCHAIN_CREDENTIAL_IDENTITY_CONFLICT")
        connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='SUCCEEDED',"
            "disposition='REUSED',credential_id=?,credential_fingerprint=?,updated_at=?,completed_at=?,"
            "last_error_code=NULL WHERE operation_id=?",
            (credential_id, fingerprint(material).hex(), now, now, operation_id),
        )
        connection.execute("COMMIT")
        return recovery_status(data_root, operation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _candidate_id(operation_id: str) -> str:
    return RECOVERY_CANDIDATE_PREFIX + hashlib.sha256(
        operation_id.encode("ascii")
    ).hexdigest()[:32]


def _final_credential_id(operation_id: str) -> str:
    return RECOVERY_CREDENTIAL_PREFIX + hashlib.sha256(
        operation_id.encode("ascii")
    ).hexdigest()[:32]


def _bind_prepared_fingerprint(
    data_root: Path, operation_id: str, material: str,
    *, expected_fingerprint: str | None,
) -> None:
    """Bind generated material to a PREPARED operation without persisting it."""

    connection = _connect(data_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        operation = _operation_row(connection, operation_id)
        if operation is None or str(operation["state"]) != "PREPARED":
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        changed = connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations "
            "SET credential_fingerprint=?,updated_at=? WHERE operation_id=? AND state='PREPARED' "
            "AND credential_fingerprint IS ?",
            (fingerprint(material).hex(), _now(), operation_id, expected_fingerprint),
        ).rowcount
        if changed != 1:
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def adopt_peer_configuration(
    data_root: Path,
    *,
    operation_id: str,
    binding: ConsumerBinding,
    authority: OwnerAuthority,
    previous_peer_configuration_digest: str,
) -> dict[str, object]:
    """Guard one no-material PREPARED operation across Forge config migration.

    This is deliberately narrower than recovery: only the digest may change,
    only once, before any fingerprint, Keychain value or CENTRAL candidate is
    bound.  The previous digest and adoption timestamp remain in the durable
    operation receipt as secret-free migration evidence.
    """

    if _DIGEST.fullmatch(previous_peer_configuration_digest) is None:
        raise CredentialRecoveryError("PREVIOUS_PEER_CONFIGURATION_DIGEST_INVALID")
    if previous_peer_configuration_digest == binding.peer_configuration_digest:
        raise CredentialRecoveryError("PEER_CONFIGURATION_DIGEST_UNCHANGED")
    with _destination_lock(data_root, authority):
        connection = _connect(data_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _operation_row(connection, operation_id)
            if row is None:
                raise CredentialRecoveryError("RECOVERY_OPERATION_NOT_FOUND")
            stable = (
                str(row["instance_id"]), str(row["consumer_id"]), str(row["project_id"]),
                str(row["repository_id"]), str(row["peer_binding_id"]),
                str(row["peer_runtime_id"]), int(row["operator_uid"]),
            )
            expected = (
                authority.instance_id, binding.consumer_id, binding.project_id,
                binding.repository_id, binding.peer_binding_id,
                binding.peer_runtime_id, authority.operator_uid,
            )
            if stable != expected:
                raise CredentialRecoveryError("RECOVERY_OPERATION_IDENTITY_CONFLICT")
            if (
                str(row["state"]) != "PREPARED"
                or row["credential_fingerprint"] is not None
                or row["credential_id"] is not None
                or row["disposition"] is not None
                or str(row["peer_configuration_digest"]) != previous_peer_configuration_digest
                or row["adopted_from_peer_configuration_digest"] is not None
            ):
                raise CredentialRecoveryError("RECOVERY_CONFIGURATION_ADOPTION_NOT_SAFE")
            changed = connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET "
                "peer_configuration_digest=?,adopted_from_peer_configuration_digest=?,"
                "configuration_adopted_at=?,updated_at=? WHERE operation_id=? "
                "AND state='PREPARED' AND peer_configuration_digest=? "
                "AND credential_fingerprint IS NULL AND credential_id IS NULL",
                (
                    binding.peer_configuration_digest, previous_peer_configuration_digest,
                    _now(), _now(), operation_id, previous_peer_configuration_digest,
                ),
            ).rowcount
            if changed != 1:
                raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
            connection.execute("COMMIT")
            return recovery_status(data_root, operation_id)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _mark_failed_safe(data_root: Path, operation_id: str, code: str) -> None:
    connection = _connect(data_root)
    try:
        now = _now()
        connection.execute("BEGIN IMMEDIATE")
        operation = _operation_row(connection, operation_id)
        if operation is None or str(operation["state"]) != "PREPARED":
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='FAILED_SAFE',"
            "updated_at=?,completed_at=?,last_error_code=? WHERE operation_id=?",
            (now, now, code, operation_id),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _activate_candidate(
    data_root: Path,
    operation_id: str,
    binding: ConsumerBinding,
    material: str,
) -> tuple[str, str | None]:
    """Stage one bounded candidate without revoking a working credential.

    The pending identifier is deliberately outside the production namespace.
    Its operation state and bounded expiry are enforced by the dedicated
    recovery-probe authorization path; normal producer/operator authentication
    must reject it until promotion.
    """

    candidate_id = _candidate_id(operation_id)
    candidate_verifier, candidate_fingerprint = verifier(material), fingerprint(material)
    connection = _connect(data_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        operation = _operation_row(connection, operation_id)
        # Interpreter/root authority is re-proven before every public call;
        # the durable operation itself binds only secret-free identity facts.
        if operation is None or (
            str(operation["instance_id"]), str(operation["consumer_id"]),
            str(operation["project_id"]), str(operation["repository_id"]),
            str(operation["peer_binding_id"]), str(operation["peer_runtime_id"]),
            str(operation["peer_configuration_digest"]),
        ) != (
            binding.instance_id, binding.consumer_id, binding.project_id,
            binding.repository_id, binding.peer_binding_id, binding.peer_runtime_id,
            binding.peer_configuration_digest,
        ):
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        state = str(operation["state"])
        if operation["credential_fingerprint"] is None or not hmac.compare_digest(
            str(operation["credential_fingerprint"]), candidate_fingerprint.hex()
        ):
            raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        if state == "CENTRAL_ACTIVATED":
            existing = connection.execute(
                "SELECT verifier,fingerprint,revoked_at,"
                "(expires_at IS NOT NULL AND expires_at>CURRENT_TIMESTAMP) "
                "FROM ep_consumer_credentials "
                "WHERE credential_id=? AND consumer_id=? AND project_id=?",
                (candidate_id, binding.consumer_id, binding.project_id),
            ).fetchone()
            if (
                existing is None or existing[2] is not None
                or int(existing[3]) != 1
                or not hmac.compare_digest(bytes(existing[0]), candidate_verifier)
                or not hmac.compare_digest(bytes(existing[1]), candidate_fingerprint)
            ):
                raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
            connection.execute("COMMIT")
            replaced = operation["replaced_credential_id"]
            return candidate_id, None if replaced is None else str(replaced)
        if state != "PREPARED":
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        registration = connection.execute(
            "SELECT status FROM ep_consumer_registrations WHERE consumer_id=? AND project_id=?",
            (binding.consumer_id, binding.project_id),
        ).fetchone()
        if registration is None or str(registration[0]) != "ACTIVE":
            raise CredentialRecoveryError("FORGE_CONSUMER_NOT_ACTIVE")
        collision = connection.execute(
            "SELECT credential_id,consumer_id,project_id,verifier,fingerprint,revoked_at "
            "FROM ep_consumer_credentials WHERE credential_id=? OR verifier=? OR fingerprint=?",
            (candidate_id, candidate_verifier, candidate_fingerprint),
        ).fetchall()
        if collision:
            if len(collision) != 1 or (
                str(collision[0][0]), str(collision[0][1]), str(collision[0][2]),
                bytes(collision[0][3]), bytes(collision[0][4]), collision[0][5],
            ) != (
                candidate_id, binding.consumer_id, binding.project_id,
                candidate_verifier, candidate_fingerprint, None,
            ):
                raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        else:
            connection.execute(
                "INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,"
                "fingerprint,issued_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    candidate_id, binding.consumer_id, binding.project_id,
                    candidate_verifier, candidate_fingerprint, _now(),
                    (_utcnow_datetime() + _CANDIDATE_VALIDITY).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        active = connection.execute(
            "SELECT credential_id FROM ep_consumer_credentials WHERE consumer_id=? AND project_id=? "
            "AND credential_id LIKE ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) "
            "ORDER BY issued_at,credential_id",
            (binding.consumer_id, binding.project_id, f"{PRODUCTION_PREFIX}%"),
        ).fetchall()
        replaced: str | None = None
        if len(active) > MAX_ACTIVE_PRODUCTION_CREDENTIALS:
            raise CredentialRecoveryError("ACTIVE_CREDENTIAL_LIMIT_CONFLICT")
        if len(active) == MAX_ACTIVE_PRODUCTION_CREDENTIALS:
            replaced = str(active[0][0])
            scoped = connection.execute(
                "SELECT consumer_id,project_id,revoked_at FROM ep_consumer_credentials "
                "WHERE credential_id=?", (replaced,),
            ).fetchone()
            if scoped is None or (
                str(scoped[0]), str(scoped[1]), scoped[2]
            ) != (binding.consumer_id, binding.project_id, None):
                raise CredentialRecoveryError("REPLACED_CREDENTIAL_SCOPE_MISMATCH")
        now = _now()
        connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='CENTRAL_ACTIVATED',"
            "disposition='REPLACED',credential_id=?,replaced_credential_id=?,credential_fingerprint=?,"
            "updated_at=?,last_error_code=NULL WHERE operation_id=? AND state='PREPARED'",
            (candidate_id, replaced, candidate_fingerprint.hex(), now, operation_id),
        )
        connection.execute("COMMIT")
        return candidate_id, replaced
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _complete_replacement(
    data_root: Path, operation_id: str, material: str,
) -> dict[str, object]:
    connection = _connect(data_root)
    try:
        now = _now()
        connection.execute("BEGIN IMMEDIATE")
        operation = _operation_row(connection, operation_id)
        if operation is None or str(operation["state"]) != "CENTRAL_ACTIVATED":
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        candidate = operation["credential_id"]
        replaced = operation["replaced_credential_id"]
        if not isinstance(candidate, str) or not candidate.startswith(RECOVERY_CANDIDATE_PREFIX):
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        candidate_scope = connection.execute(
            "SELECT consumer_id,project_id,verifier,fingerprint,revoked_at,"
            "(expires_at IS NOT NULL AND expires_at>CURRENT_TIMESTAMP) "
            "FROM ep_consumer_credentials "
            "WHERE credential_id=?", (candidate,),
        ).fetchone()
        if candidate_scope is None or (
            str(candidate_scope[0]), str(candidate_scope[1]), bytes(candidate_scope[2]),
            bytes(candidate_scope[3]), candidate_scope[4],
        ) != (
            str(operation["consumer_id"]), str(operation["project_id"]), verifier(material),
            fingerprint(material), None,
        ) or int(candidate_scope[5]) != 1 or not hmac.compare_digest(
            str(operation["credential_fingerprint"]), fingerprint(material).hex()
        ):
            raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        active = connection.execute(
            "SELECT credential_id FROM ep_consumer_credentials WHERE consumer_id=? AND project_id=? "
            "AND credential_id LIKE ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) "
            "ORDER BY issued_at,credential_id",
            (operation["consumer_id"], operation["project_id"], f"{PRODUCTION_PREFIX}%"),
        ).fetchall()
        if len(active) > MAX_ACTIVE_PRODUCTION_CREDENTIALS:
            raise CredentialRecoveryError("ACTIVE_CREDENTIAL_LIMIT_CONFLICT")
        final_id = _final_credential_id(operation_id)
        if connection.execute(
            "SELECT 1 FROM ep_consumer_credentials WHERE credential_id=?", (final_id,),
        ).fetchone() is not None:
            raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        if isinstance(replaced, str):
            if len(active) != MAX_ACTIVE_PRODUCTION_CREDENTIALS or replaced not in {
                str(row[0]) for row in active
            }:
                raise CredentialRecoveryError("REPLACED_CREDENTIAL_SCOPE_MISMATCH")
        elif len(active) >= MAX_ACTIVE_PRODUCTION_CREDENTIALS:
            raise CredentialRecoveryError("ACTIVE_CREDENTIAL_LIMIT_CONFLICT")
        # Drop the operation's immediate FK reference while renaming the
        # already-proven verifier, then bind the final identifier atomically.
        connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET credential_id=NULL "
            "WHERE operation_id=? AND state='CENTRAL_ACTIVATED'", (operation_id,),
        )
        changed = connection.execute(
            "UPDATE ep_consumer_credentials SET credential_id=?,expires_at=NULL WHERE credential_id=? "
            "AND consumer_id=? AND project_id=? AND revoked_at IS NULL",
            (final_id, candidate, operation["consumer_id"], operation["project_id"]),
        ).rowcount
        if changed != 1:
            raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        if isinstance(replaced, str):
            changed = connection.execute(
                "UPDATE ep_consumer_credentials SET revoked_at=?,replaced_by_credential_id=? "
                "WHERE credential_id=? AND consumer_id=? AND project_id=? AND revoked_at IS NULL",
                (
                    now, final_id, replaced,
                    operation["consumer_id"], operation["project_id"],
                ),
            ).rowcount
            if changed != 1:
                raise CredentialRecoveryError("REPLACED_CREDENTIAL_SCOPE_MISMATCH")
        connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='SUCCEEDED',"
            "credential_id=?,updated_at=?,completed_at=?,last_error_code=NULL "
            "WHERE operation_id=? AND state='CENTRAL_ACTIVATED'",
            (final_id, now, now, operation_id),
        )
        connection.execute("COMMIT")
        return recovery_status(data_root, operation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _rollback_replacement(
    data_root: Path, operation_id: str, *, code: str,
) -> dict[str, object]:
    connection = _connect(data_root)
    try:
        now = _now()
        connection.execute("BEGIN IMMEDIATE")
        operation = _operation_row(connection, operation_id)
        if operation is None or str(operation["state"]) not in {
            "CENTRAL_ACTIVATED", "STOPPED_UNCERTAIN",
        }:
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        candidate = operation["credential_id"]
        if not isinstance(candidate, str) or not candidate.startswith(RECOVERY_CANDIDATE_PREFIX):
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        candidate_scope = connection.execute(
            "SELECT consumer_id,project_id,revoked_at FROM ep_consumer_credentials "
            "WHERE credential_id=?", (candidate,),
        ).fetchone()
        if candidate_scope is None or (
            str(candidate_scope[0]), str(candidate_scope[1])
        ) != (str(operation["consumer_id"]), str(operation["project_id"])):
            raise CredentialRecoveryError("RECOVERY_CANDIDATE_IDENTITY_CONFLICT")
        connection.execute(
            "UPDATE ep_consumer_credentials SET revoked_at=? WHERE credential_id=? "
            "AND consumer_id=? AND project_id=? AND revoked_at IS NULL",
            (now, candidate, operation["consumer_id"], operation["project_id"]),
        )
        changed = connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='FAILED_SAFE',"
            "updated_at=?,completed_at=?,last_error_code=? WHERE operation_id=? "
            "AND state IN ('CENTRAL_ACTIVATED','STOPPED_UNCERTAIN')",
            (now, now, code, operation_id),
        ).rowcount
        if changed != 1:
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        connection.execute("COMMIT")
        return recovery_status(data_root, operation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _stop_uncertain(data_root: Path, operation_id: str, code: str) -> None:
    connection = _connect(data_root)
    try:
        now = _now()
        changed = connection.execute(
            "UPDATE ep_consumer_credential_recovery_operations SET state='STOPPED_UNCERTAIN',"
            "updated_at=?,completed_at=NULL,last_error_code=? WHERE operation_id=? "
            "AND state IN ('PREPARED','CENTRAL_ACTIVATED','STOPPED_UNCERTAIN')",
            (now, code, operation_id),
        ).rowcount
        if changed != 1:
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
    finally:
        connection.close()


def _delete_if_operation_owns_value(
    store: KeychainStore, expected_fingerprint: str,
) -> bool:
    """Delete only the currently observed operation-bound Keychain value."""

    current = store.read()
    if current is None:
        return True
    if not hmac.compare_digest(fingerprint(current).hex(), expected_fingerprint):
        return False
    store.delete()
    return store.read() is None


def _resume_uncertain(
    data_root: Path,
    operation_id: str,
    *,
    store: KeychainStore,
) -> dict[str, object]:
    """Reconcile an uncertain operation without issuing another candidate."""

    operation = recovery_status(data_root, operation_id)
    if operation["state"] != "STOPPED_UNCERTAIN":
        return operation
    expected_fingerprint = operation.get("credential_fingerprint")
    candidate_id = operation.get("credential_id")
    try:
        material = store.read()
    except Exception as error:
        raise CredentialRecoveryError("UNCERTAIN_KEYCHAIN_READ_UNAVAILABLE") from error

    material_matches = (
        isinstance(expected_fingerprint, str)
        and material is not None
        and hmac.compare_digest(expected_fingerprint, fingerprint(material).hex())
    )
    if isinstance(candidate_id, str):
        connection = _connect(data_root)
        try:
            candidate = connection.execute(
                "SELECT verifier,fingerprint,revoked_at,"
                "(expires_at IS NOT NULL AND expires_at>CURRENT_TIMESTAMP) "
                "FROM ep_consumer_credentials WHERE credential_id=? AND consumer_id=? AND project_id=?",
                (candidate_id, operation["consumer_id"], operation["project_id"]),
            ).fetchone()
        finally:
            connection.close()
        candidate_active = (
            candidate is not None and candidate[2] is None and int(candidate[3]) == 1
            and material_matches
            and hmac.compare_digest(bytes(candidate[0]), verifier(material))
            and hmac.compare_digest(bytes(candidate[1]), fingerprint(material))
        )
        if candidate_active:
            connection = _connect(data_root)
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    "UPDATE ep_consumer_credential_recovery_operations "
                    "SET state='CENTRAL_ACTIVATED',completed_at=NULL,updated_at=?,last_error_code=NULL "
                    "WHERE operation_id=? AND state='STOPPED_UNCERTAIN'",
                    (_now(), operation_id),
                ).rowcount
                if changed != 1:
                    raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
            return recovery_status(data_root, operation_id)
        if material_matches:
            try:
                deleted = _delete_if_operation_owns_value(store, str(expected_fingerprint))
            except Exception as error:
                raise CredentialRecoveryError("UNCERTAIN_KEYCHAIN_DELETE_UNRESOLVED") from error
            if not deleted:
                raise CredentialRecoveryError("UNCERTAIN_KEYCHAIN_DELETE_UNRESOLVED")
        return _rollback_replacement(
            data_root, operation_id, code="UNCERTAIN_CANDIDATE_RECONCILED_ABORTED",
        )

    connection = _connect(data_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if material is None:
            changed = connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET state='PREPARED',"
                "credential_fingerprint=NULL,completed_at=NULL,updated_at=?,last_error_code=NULL "
                "WHERE operation_id=? AND state='STOPPED_UNCERTAIN' AND credential_id IS NULL",
                (_now(), operation_id),
            ).rowcount
        elif material_matches:
            changed = connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET state='PREPARED',"
                "completed_at=NULL,updated_at=?,last_error_code=NULL "
                "WHERE operation_id=? AND state='STOPPED_UNCERTAIN' AND credential_id IS NULL",
                (_now(), operation_id),
            ).rowcount
        else:
            changed = connection.execute(
                "UPDATE ep_consumer_credential_recovery_operations SET state='FAILED_SAFE',"
                "completed_at=?,updated_at=?,last_error_code='UNCERTAIN_KEYCHAIN_VALUE_NOT_OWNED' "
                "WHERE operation_id=? AND state='STOPPED_UNCERTAIN' AND credential_id IS NULL",
                (_now(), _now(), operation_id),
            ).rowcount
        if changed != 1:
            raise CredentialRecoveryError("RECOVERY_OPERATION_STATE_CONFLICT")
        connection.execute("COMMIT")
        return recovery_status(data_root, operation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def recover_credential(
    data_root: Path,
    *,
    operation_id: str,
    binding: ConsumerBinding,
    authority: OwnerAuthority,
    store: KeychainStore,
    authenticate: Callable[[str, ConsumerBinding], bool],
    authenticate_candidate: Callable[[str, ConsumerBinding], bool] | None = None,
    after_fingerprint_binding: Callable[[], None] | None = None,
    after_keychain_write: Callable[[], None] | None = None,
    after_central_activation: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Reconcile one replayable credential recovery without exporting a token."""

    candidate_authenticate = authenticate_candidate or authenticate
    with _destination_lock(data_root, authority):
        prepared = _prepare_operation(data_root, operation_id, binding, authority)
        if prepared["state"] in _TERMINAL_STATES:
            return prepared
        if prepared["state"] == "STOPPED_UNCERTAIN":
            prepared = _resume_uncertain(data_root, operation_id, store=store)
            if prepared["state"] in _TERMINAL_STATES:
                return prepared
        try:
            material = store.read()
        except Exception as error:
            if prepared.get("credential_fingerprint") is not None:
                _stop_uncertain(data_root, operation_id, "KEYCHAIN_READ_OUTCOME_UNCERTAIN")
            raise CredentialRecoveryError("KEYCHAIN_READ_OUTCOME_UNCERTAIN") from error

        if material is not None:
            connection = _connect(data_root)
            try:
                scope = _credential_scope(connection, material)
            finally:
                connection.close()
            if scope is not None:
                credential_id, consumer_id, project_id, revoked_at = scope
                if (consumer_id, project_id) != (binding.consumer_id, binding.project_id):
                    if prepared["state"] == "PREPARED":
                        _mark_failed_safe(
                            data_root, operation_id, "KEYCHAIN_CREDENTIAL_SCOPE_MISMATCH"
                        )
                    raise CredentialRecoveryError("KEYCHAIN_CREDENTIAL_SCOPE_MISMATCH")
                if (
                    prepared["state"] == "CENTRAL_ACTIVATED"
                    and prepared["credential_id"] == credential_id
                    and revoked_at is None
                ):
                    pass
                elif prepared["state"] == "PREPARED" and revoked_at is None:
                    stored = store.read()
                    if stored is None or not hmac.compare_digest(
                        fingerprint(stored), fingerprint(material)
                    ):
                        _stop_uncertain(
                            data_root, operation_id, "KEYCHAIN_VALUE_CHANGED_BEFORE_REUSE"
                        )
                        raise CredentialRecoveryError("KEYCHAIN_VALUE_CHANGED_BEFORE_REUSE")
                    if not authenticate(stored, binding):
                        _mark_failed_safe(
                            data_root,
                            operation_id,
                            "EXISTING_KEYCHAIN_CREDENTIAL_AUTHENTICATION_FAILED",
                        )
                        raise CredentialRecoveryError(
                            "EXISTING_KEYCHAIN_CREDENTIAL_AUTHENTICATION_FAILED"
                        )
                    return _mark_reused(data_root, operation_id, credential_id, stored)
                elif prepared["state"] == "PREPARED" and revoked_at is not None:
                    # This is EP-owned material for the exact consumer/project,
                    # but no longer an authorization credential. It is safe to
                    # replace inside the locked, durable recovery operation.
                    material = None
                else:
                    if prepared["state"] == "PREPARED":
                        _mark_failed_safe(
                            data_root, operation_id, "KEYCHAIN_CREDENTIAL_NOT_ACTIVE"
                        )
                    raise CredentialRecoveryError("KEYCHAIN_CREDENTIAL_NOT_ACTIVE")
            else:
                expected_fingerprint = prepared.get("credential_fingerprint")
                if not (
                    prepared["state"] == "PREPARED"
                    and isinstance(expected_fingerprint, str)
                    and hmac.compare_digest(
                        expected_fingerprint, fingerprint(material).hex()
                    )
                ):
                    if prepared["state"] == "PREPARED":
                        _mark_failed_safe(
                            data_root, operation_id, "KEYCHAIN_DESTINATION_NOT_OPERATION_OWNED"
                        )
                    raise CredentialRecoveryError(
                        "KEYCHAIN_DESTINATION_NOT_OPERATION_OWNED"
                    )

        operation = recovery_status(data_root, operation_id)
        if operation["state"] == "CENTRAL_ACTIVATED":
            candidate_id = operation["credential_id"]
            expected_fingerprint = operation.get("credential_fingerprint")
            if (
                material is None or not isinstance(candidate_id, str)
                or not isinstance(expected_fingerprint, str)
                or not hmac.compare_digest(
                    expected_fingerprint, fingerprint(material).hex()
                )
            ):
                _stop_uncertain(
                    data_root, operation_id, "ACTIVATED_CREDENTIAL_MATERIAL_UNAVAILABLE"
                )
                raise CredentialRecoveryError(
                    "ACTIVATED_CREDENTIAL_MATERIAL_UNAVAILABLE"
                )
        else:
            if material is None:
                material = secrets.token_urlsafe(32)
                previous_fingerprint = operation.get("credential_fingerprint")
                _bind_prepared_fingerprint(
                    data_root, operation_id, material,
                    expected_fingerprint=(
                        previous_fingerprint if isinstance(previous_fingerprint, str) else None
                    ),
                )
                if after_fingerprint_binding is not None:
                    after_fingerprint_binding()
                try:
                    store.replace(material)
                except CredentialRecoveryError as error:
                    _mark_failed_safe(data_root, operation_id, error.code)
                    raise
                except Exception as error:
                    _stop_uncertain(
                        data_root, operation_id, "KEYCHAIN_WRITE_OUTCOME_UNCERTAIN"
                    )
                    raise CredentialRecoveryError(
                        "KEYCHAIN_WRITE_OUTCOME_UNCERTAIN"
                    ) from error
                if after_keychain_write is not None:
                    after_keychain_write()
                stored = store.read()
                if stored is None or not hmac.compare_digest(
                    fingerprint(stored), fingerprint(material)
                ):
                    _stop_uncertain(
                        data_root, operation_id, "KEYCHAIN_WRITE_READBACK_MISMATCH"
                    )
                    raise CredentialRecoveryError("KEYCHAIN_WRITE_READBACK_MISMATCH")
                material = stored
            _activate_candidate(data_root, operation_id, binding, material)
            if after_central_activation is not None:
                after_central_activation()

        operation = recovery_status(data_root, operation_id)
        expected_fingerprint = operation.get("credential_fingerprint")
        candidate_id = operation.get("credential_id")
        stored = store.read()
        if (
            stored is None or not isinstance(expected_fingerprint, str)
            or not isinstance(candidate_id, str)
            or not hmac.compare_digest(
                expected_fingerprint, fingerprint(stored).hex()
            )
        ):
            _stop_uncertain(
                data_root, operation_id, "ACTIVATED_CREDENTIAL_IDENTITY_MISMATCH"
            )
            raise CredentialRecoveryError("ACTIVATED_CREDENTIAL_IDENTITY_MISMATCH")
        connection = _connect(data_root)
        try:
            scope = _credential_scope(connection, stored)
        finally:
            connection.close()
        if scope is None or scope[:3] != (
            candidate_id, binding.consumer_id, binding.project_id,
        ) or scope[3] is not None:
            _stop_uncertain(
                data_root, operation_id, "ACTIVATED_CREDENTIAL_IDENTITY_MISMATCH"
            )
            raise CredentialRecoveryError("ACTIVATED_CREDENTIAL_IDENTITY_MISMATCH")
        if candidate_authenticate(stored, binding):
            return _complete_replacement(data_root, operation_id, stored)
        try:
            deleted = _delete_if_operation_owns_value(store, expected_fingerprint)
        except Exception as error:
            _stop_uncertain(data_root, operation_id, "REPLACEMENT_ROLLBACK_UNCERTAIN")
            raise CredentialRecoveryError("REPLACEMENT_ROLLBACK_UNCERTAIN") from error
        return _rollback_replacement(
            data_root,
            operation_id,
            code=(
                "REPLACEMENT_AUTHENTICATION_FAILED"
                if deleted else "REPLACEMENT_AUTHENTICATION_FAILED_KEYCHAIN_CHANGED"
            ),
        )


def readback_from_data_root(
    data_root: Path,
    *,
    expected_instance_id: str,
    project_id: str,
    repository_id: str,
    expected_consumer_id: str | None,
    peer_binding_id: str,
    peer_runtime_id: str,
    peer_configuration_digest: str,
) -> ConsumerBinding:
    connection = _connect(data_root)
    try:
        return scoped_consumer_readback(
            connection,
            expected_instance_id=expected_instance_id,
            project_id=project_id,
            repository_id=repository_id,
            expected_consumer_id=expected_consumer_id,
            peer_binding_id=peer_binding_id,
            peer_runtime_id=peer_runtime_id,
            peer_configuration_digest=peer_configuration_digest,
        )
    finally:
        connection.close()


def safe_json(value: Mapping[str, object]) -> str:
    """Stable output helper kept here so every CLI path uses the safe shape."""

    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
