"""Verifier-only Local Consumer API credentials and qualification seam.

Increment 2a permits one short-lived, operator-created qualification credential.
It is not consumer registration or a general credential-issuance workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.client import HTTPConnection
import json
from pathlib import Path
import secrets

from .contracts.local_consumer_api import RequestEnvelope
from .storage import open_storage

VERIFIER_DOMAIN = b"engineering-platform.local-api.verifier.v1\0"
FINGERPRINT_DOMAIN = b"engineering-platform.local-api.fingerprint.v1\0"
QUALIFICATION_PREFIX = "qualification-"
QUALIFICATION_TTL = timedelta(minutes=15)
PRODUCTION_PREFIX = "production-"
MAX_ACTIVE_PRODUCTION_CREDENTIALS = 2


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


def _registration(root: Path, consumer_id: str, project_id: str) -> tuple[str, ...] | None:
    connection = open_storage(root)
    try:
        return connection.execute(
            "SELECT consumer_id,project_id,status,created_at,updated_at,disabled_at,revoked_at "
            "FROM local_api_consumer_registrations WHERE consumer_id=? AND project_id=?",
            (consumer_id, project_id),
        ).fetchone()
    finally:
        connection.close()


def register_consumer(root: Path, *, consumer_id: str, project_id: str) -> dict[str, str | bool | None]:
    consumer_id, project_id = _scope(consumer_id, project_id)
    now = _timestamp(datetime.now(timezone.utc))
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT status,created_at,updated_at,disabled_at,revoked_at FROM local_api_consumer_registrations "
            "WHERE consumer_id=? AND project_id=?", (consumer_id, project_id)
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata) "
                "VALUES(?,?, 'ACTIVE',?,?,?)", (consumer_id, project_id, now, now, json.dumps({"action":"REGISTER"}, sort_keys=True))
            )
            return {"consumer_id":consumer_id,"project_id":project_id,"status":"ACTIVE","created_at":now,"updated_at":now,"idempotent":False}
        if row[0] != "ACTIVE":
            raise ValueError("consumer registration is not active.")
        return {"consumer_id":consumer_id,"project_id":project_id,"status":"ACTIVE","created_at":str(row[1]),"updated_at":str(row[2]),"idempotent":True}
    finally:
        connection.close()


def consumer_status(root: Path, *, consumer_id: str, project_id: str) -> dict[str, object]:
    consumer_id, project_id = _scope(consumer_id, project_id)
    row = _registration(root, consumer_id, project_id)
    if row is None:
        raise ValueError("consumer registration is absent.")
    connection = open_storage(root)
    try:
        count = connection.execute(
            "SELECT count(*) FROM local_api_credentials WHERE consumer_id=? AND project_id=? AND credential_id LIKE ? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
            (consumer_id, project_id, f"{PRODUCTION_PREFIX}%"),
        ).fetchone()[0]
    finally:
        connection.close()
    return {"consumer_id":row[0],"project_id":row[1],"status":row[2],"created_at":row[3],"updated_at":row[4],"disabled_at":row[5],"revoked_at":row[6],"active_production_credentials":count}


def _set_registration_state(root: Path, *, consumer_id: str, project_id: str, state: str) -> bool:
    consumer_id, project_id = _scope(consumer_id, project_id)
    now = _timestamp(datetime.now(timezone.utc))
    column = "disabled_at" if state == "DISABLED" else "revoked_at"
    connection = open_storage(root)
    try:
        result = connection.execute(
            f"UPDATE local_api_consumer_registrations SET status=?,updated_at=?,{column}=?,audit_metadata=? "
            "WHERE consumer_id=? AND project_id=? AND status='ACTIVE'",
            (state, now, now, json.dumps({"action": "DISABLE" if state == "DISABLED" else "REVOKE_REGISTRATION"}, sort_keys=True), consumer_id, project_id),
        )
        existing = connection.execute("SELECT status FROM local_api_consumer_registrations WHERE consumer_id=? AND project_id=?", (consumer_id, project_id)).fetchone()
        if existing is None:
            raise ValueError("consumer registration is absent.")
        if existing[0] != state and result.rowcount != 1:
            raise ValueError("consumer registration state conflicts.")
        return result.rowcount == 1
    finally:
        connection.close()


def disable_consumer(root: Path, *, consumer_id: str, project_id: str) -> bool:
    return _set_registration_state(root, consumer_id=consumer_id, project_id=project_id, state="DISABLED")


def revoke_consumer(root: Path, *, consumer_id: str, project_id: str) -> bool:
    return _set_registration_state(root, consumer_id=consumer_id, project_id=project_id, state="REVOKED")


@dataclass(frozen=True)
class ProductionCredential:
    credential_id: str
    consumer_id: str
    project_id: str
    fingerprint_hex: str
    created_at: str
    credential: str = field(repr=False)

    def disclosure(self) -> dict[str, str]:
        return {"credential_id":self.credential_id,"consumer_id":self.consumer_id,"project_id":self.project_id,"purpose":"PRODUCTION_CONSUMER","created_at":self.created_at,"credential":self.credential}


def issue_credential(root: Path, *, consumer_id: str, project_id: str) -> ProductionCredential:
    consumer_id, project_id = _scope(consumer_id, project_id)
    status = _registration(root, consumer_id, project_id)
    if status is None or status[2] != "ACTIVE":
        raise ValueError("active consumer registration is required.")
    token = secrets.token_urlsafe(32)
    now = _timestamp(datetime.now(timezone.utc))
    credential_id = PRODUCTION_PREFIX + secrets.token_hex(16)
    connection = open_storage(root)
    try:
        count = connection.execute("SELECT count(*) FROM local_api_credentials WHERE consumer_id=? AND project_id=? AND credential_id LIKE ? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)", (consumer_id,project_id,f"{PRODUCTION_PREFIX}%")).fetchone()[0]
        if count >= MAX_ACTIVE_PRODUCTION_CREDENTIALS:
            raise ValueError("active production credential limit reached.")
        connection.execute("INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)", (credential_id,consumer_id,project_id,verifier(token),fingerprint(token),now))
    finally:
        connection.close()
    return ProductionCredential(credential_id,consumer_id,project_id,fingerprint(token).hex(),now,token)


def credential_status(root: Path, *, consumer_id: str, project_id: str) -> list[dict[str, object]]:
    consumer_id, project_id = _scope(consumer_id, project_id)
    connection = open_storage(root)
    try:
        rows = connection.execute("SELECT credential_id,fingerprint,issued_at,expires_at,revoked_at FROM local_api_credentials WHERE consumer_id=? AND project_id=? AND credential_id LIKE ? ORDER BY issued_at DESC", (consumer_id,project_id,f"{PRODUCTION_PREFIX}%")).fetchall()
    finally:
        connection.close()
    now = _timestamp(datetime.now(timezone.utc))
    return [{"credential_id":str(r[0]),"fingerprint":bytes(r[1]).hex(),"purpose":"PRODUCTION_CONSUMER","created_at":str(r[2]),"expires_at":r[3],"revoked_at":r[4],"active":r[4] is None and (r[3] is None or str(r[3]) > now)} for r in rows]


def revoke_credential(root: Path, credential_id: str) -> bool:
    if not credential_id.startswith(PRODUCTION_PREFIX):
        raise ValueError("credential_id is not a production credential.")
    connection = open_storage(root)
    try:
        result = connection.execute("UPDATE local_api_credentials SET revoked_at=? WHERE credential_id=? AND revoked_at IS NULL", (_timestamp(datetime.now(timezone.utc)),credential_id))
        if result.rowcount == 0 and connection.execute(
            "SELECT 1 FROM local_api_credentials WHERE credential_id=?", (credential_id,)
        ).fetchone() is None:
            raise ValueError("production credential is absent.")
        return result.rowcount == 1
    finally:
        connection.close()


def rotate_credential(
    root: Path, *, consumer_id: str, project_id: str, old_credential_id: str,
    store: object, authenticate: Callable[[str], bool],
) -> ProductionCredential:
    """Issue, securely store and prove a replacement before revoking the old token."""
    replacement = issue_credential(root, consumer_id=consumer_id, project_id=project_id)
    try:
        store.put_credential(consumer_id, project_id, replacement.credential)
        if not authenticate(replacement.credential):
            raise ValueError("replacement credential did not authenticate.")
    except Exception:
        revoke_credential(root, replacement.credential_id)
        raise
    revoke_credential(root, old_credential_id)
    return replacement


def verify_capabilities_over_http(credential: str, *, consumer_id: str, project_id: str, port: int = 8766) -> bool:
    """Prove a credential through the real, read-only Local API transport."""
    body = json.dumps({"contract_version":"1.0","request_type":"contract.foundation","request_id":"credential-rotation","project_id":project_id,"consumer":{"consumer_id":consumer_id},"auth":{"scheme":"bearer","credential":"operator-carrier"},"payload":{}})
    connection = HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("POST", "/v1/capabilities", body=body, headers={"Content-Type":"application/json","Authorization":f"Bearer {credential}"})
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
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
                connection.execute("SELECT 1 FROM local_api_consumer_registrations LIMIT 1").fetchone()
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
                    "SELECT credential_id,consumer_id,project_id,verifier FROM local_api_credentials "
                    "WHERE verifier=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
                    (candidate,),
                ).fetchone()
            finally:
                connection.close()
        except Exception:
            return None
        if row is None or not hmac.compare_digest(bytes(row[3]), candidate):
            return None
        if str(row[0]).startswith(PRODUCTION_PREFIX):
            registration = connection = None
            try:
                registration = _registration(self.root, str(row[1]), str(row[2]))
            except Exception:
                return None
            if registration is None or registration[2] != "ACTIVE":
                return None
        return CredentialScope(str(row[1]), str(row[2]), bytes(row[3]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-local-api-credentials")
    parser.add_argument(
        "command",
        choices=(
            "create-qualification-credential",
            "qualification-status",
            "revoke-qualification-credential", "consumer-register", "consumer-status",
            "consumer-disable", "consumer-revoke", "credential-issue", "credential-status", "credential-revoke", "credential-rotate",
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
        if args.command in {"consumer-register", "consumer-status", "consumer-disable", "consumer-revoke", "credential-issue", "credential-status"}:
            if args.consumer_id is None or args.project_id is None:
                raise ValueError("--consumer-id and --project-id are required.")
            actions = {"consumer-register": register_consumer, "consumer-status": consumer_status, "consumer-disable": disable_consumer, "consumer-revoke": revoke_consumer, "credential-issue": issue_credential, "credential-status": credential_status}
            result = actions[args.command](root, consumer_id=args.consumer_id, project_id=args.project_id)
            if isinstance(result, (ProductionCredential, QualificationCredential)):
                result = result.disclosure()
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "credential-rotate":
            if args.consumer_id is None or args.project_id is None or args.credential_id is None:
                raise ValueError("--consumer-id, --project-id and --credential-id are required.")
            from .local_api_keychain import MacOSKeychainCredentialStore
            result = rotate_credential(root, consumer_id=args.consumer_id, project_id=args.project_id, old_credential_id=args.credential_id, store=MacOSKeychainCredentialStore(), authenticate=lambda token: verify_capabilities_over_http(token, consumer_id=args.consumer_id, project_id=args.project_id))
            print(json.dumps(result.disclosure(), sort_keys=True))
            return 0
        if args.credential_id is None:
            raise ValueError("--credential-id is required.")
        if args.command == "credential-revoke":
            changed = revoke_credential(root, args.credential_id)
        else:
            changed = revoke_qualification_credential(root, args.credential_id)
        print(json.dumps({"credential_id": args.credential_id, "revoked": True, "changed": changed}, sort_keys=True))
        return 0
    except ValueError as error:
        print(f"ERROR: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
