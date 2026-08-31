"""Verifier-only Local Consumer API credentials and qualification seam.

Increment 2a permits one short-lived, operator-created qualification credential.
It is not consumer registration or a general credential-issuance workflow.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from .contracts.local_consumer_api import RequestEnvelope
from .storage import open_storage

VERIFIER_DOMAIN = b"engineering-platform.local-api.verifier.v1\0"
FINGERPRINT_DOMAIN = b"engineering-platform.local-api.fingerprint.v1\0"
QUALIFICATION_PREFIX = "qualification-"
QUALIFICATION_TTL = timedelta(minutes=15)


def verifier(credential: str) -> bytes:
    return hashlib.sha256(VERIFIER_DOMAIN + credential.encode("ascii")).digest()


def fingerprint(credential: str) -> bytes:
    return hashlib.sha256(FINGERPRINT_DOMAIN + credential.encode("ascii")).digest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _scope(consumer_id: str, project_id: str) -> tuple[str, str]:
    """Reuse the v1 contract validator rather than duplicating identity rules."""

    envelope = RequestEnvelope.parse(
        {
            "contract_version": "1.0",
            "request_type": "contract.foundation",
            "request_id": "qualification-credential",
            "project_id": project_id,
            "consumer": {"consumer_id": consumer_id},
            "auth": {"scheme": "bearer", "credential": "operator-carrier"},
            "payload": {},
        }
    )
    return envelope.consumer.consumer_id, envelope.project_id


@dataclass(frozen=True)
class CredentialScope:
    consumer_id: str
    project_id: str
    verifier_value: bytes


@dataclass(frozen=True)
class QualificationCredential:
    credential_id: str
    consumer_id: str
    project_id: str
    fingerprint_hex: str
    created_at: str
    expires_at: str
    credential: str = field(repr=False)

    def disclosure(self) -> dict[str, str]:
        """The sole plaintext handoff, used only by the create CLI command."""

        return {
            "credential_id": self.credential_id,
            "consumer_id": self.consumer_id,
            "project_id": self.project_id,
            "purpose": "QUALIFICATION",
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "credential": self.credential,
        }


def create_qualification_credential(
    root: Path, *, consumer_id: str, project_id: str, now: datetime | None = None
) -> QualificationCredential:
    """Create the one active, short-lived qualification credential."""

    consumer_id, project_id = _scope(consumer_id, project_id)
    moment = now or datetime.now(timezone.utc)
    created_at = _timestamp(moment)
    expires_at = _timestamp(moment + QUALIFICATION_TTL)
    credential = secrets.token_urlsafe(32)
    credential_id = QUALIFICATION_PREFIX + secrets.token_hex(16)
    candidate_verifier = verifier(credential)
    candidate_fingerprint = fingerprint(credential)
    connection = open_storage(root)
    try:
        active = connection.execute(
            "SELECT 1 FROM local_api_credentials WHERE credential_id LIKE ? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) LIMIT 1",
            (f"{QUALIFICATION_PREFIX}%",),
        ).fetchone()
        if active is not None:
            raise ValueError("An active qualification credential already exists; revoke it first.")
        connection.execute(
            "INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                credential_id,
                consumer_id,
                project_id,
                candidate_verifier,
                candidate_fingerprint,
                created_at,
                expires_at,
            ),
        )
    finally:
        connection.close()
    return QualificationCredential(
        credential_id=credential_id,
        consumer_id=consumer_id,
        project_id=project_id,
        fingerprint_hex=candidate_fingerprint.hex(),
        created_at=created_at,
        expires_at=expires_at,
        credential=credential,
    )


def qualification_status(root: Path) -> list[dict[str, str | bool | None]]:
    """Return safe, bounded qualification metadata without token material."""

    connection = open_storage(root)
    try:
        rows = connection.execute(
            "SELECT credential_id,consumer_id,project_id,fingerprint,issued_at,expires_at,revoked_at "
            "FROM local_api_credentials WHERE credential_id LIKE ? ORDER BY issued_at DESC",
            (f"{QUALIFICATION_PREFIX}%",),
        ).fetchall()
    finally:
        connection.close()
    now = _timestamp(datetime.now(timezone.utc))
    return [
        {
            "credential_id": str(row[0]),
            "consumer_id": str(row[1]),
            "project_id": str(row[2]),
            "fingerprint": bytes(row[3]).hex(),
            "purpose": "QUALIFICATION",
            "created_at": str(row[4]),
            "expires_at": str(row[5]) if row[5] is not None else None,
            "active": row[6] is None and (row[5] is None or str(row[5]) > now),
            "revoked_at": str(row[6]) if row[6] is not None else None,
        }
        for row in rows
    ]


def revoke_qualification_credential(root: Path, credential_id: str) -> bool:
    """Explicitly deactivate a qualification credential without deleting evidence."""

    if not credential_id.startswith(QUALIFICATION_PREFIX):
        raise ValueError("credential_id is not a qualification credential.")
    connection = open_storage(root)
    try:
        result = connection.execute(
            "UPDATE local_api_credentials SET revoked_at=? WHERE credential_id=? AND revoked_at IS NULL",
            (_timestamp(datetime.now(timezone.utc)), credential_id),
        )
        return result.rowcount == 1
    finally:
        connection.close()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-local-api-credentials")
    parser.add_argument(
        "command",
        choices=(
            "create-qualification-credential",
            "qualification-status",
            "revoke-qualification-credential",
        ),
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--consumer-id")
    parser.add_argument("--project-id")
    parser.add_argument("--credential-id")
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    try:
        if args.command == "create-qualification-credential":
            if args.consumer_id is None or args.project_id is None:
                raise ValueError("--consumer-id and --project-id are required.")
            print(
                json.dumps(
                    create_qualification_credential(
                        root, consumer_id=args.consumer_id, project_id=args.project_id
                    ).disclosure(),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "qualification-status":
            print(json.dumps(qualification_status(root), sort_keys=True))
            return 0
        if args.credential_id is None:
            raise ValueError("--credential-id is required.")
        if not revoke_qualification_credential(root, args.credential_id):
            raise ValueError("qualification credential is absent or already inactive.")
        print(json.dumps({"credential_id": args.credential_id, "revoked": True}, sort_keys=True))
        return 0
    except ValueError as error:
        print(f"ERROR: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
