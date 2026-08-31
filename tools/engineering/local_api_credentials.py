"""Verifier-only credential lookup for the Local Consumer API.

Increment 3 owns credential issuance, registration, rotation and revocation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path

from .storage import open_storage

VERIFIER_DOMAIN = b"engineering-platform.local-api.verifier.v1\0"
FINGERPRINT_DOMAIN = b"engineering-platform.local-api.fingerprint.v1\0"


def verifier(credential: str) -> bytes:
    return hashlib.sha256(VERIFIER_DOMAIN + credential.encode("ascii")).digest()


def fingerprint(credential: str) -> bytes:
    return hashlib.sha256(FINGERPRINT_DOMAIN + credential.encode("ascii")).digest()


@dataclass(frozen=True)
class CredentialScope:
    consumer_id: str
    project_id: str
    verifier_value: bytes


class CredentialAuthority:
    """Read-only production authority with explicit in-memory test fixtures."""

    def __init__(self, root: Path | None = None, record: CredentialScope | None = None) -> None:
        self.root, self.record = root, record

    @classmethod
    def test_fixture(
        cls, credential: str, *, consumer_id: str, project_id: str
    ) -> "CredentialAuthority":
        return cls(record=CredentialScope(consumer_id, project_id, verifier(credential)))

    def ready(self) -> bool:
        if self.root is None:
            return True
        try:
            connection = open_storage(self.root)
            try:
                connection.execute("SELECT 1 FROM local_api_credentials LIMIT 1").fetchone()
            finally:
                connection.close()
            return True
        except Exception:
            return False

    def authenticate(self, credential: str) -> CredentialScope | None:
        try:
            candidate = verifier(credential)
        except UnicodeEncodeError:
            return None
        if self.record is not None:
            return (
                self.record if hmac.compare_digest(self.record.verifier_value, candidate) else None
            )
        if self.root is None:
            return None
        try:
            connection = open_storage(self.root)
            try:
                row = connection.execute(
                    "SELECT consumer_id,project_id,verifier FROM local_api_credentials "
                    "WHERE verifier=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
                    (candidate,),
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            return None
        if row is None or not hmac.compare_digest(bytes(row[2]), candidate):
            return None
        return CredentialScope(str(row[0]), str(row[1]), bytes(row[2]))
