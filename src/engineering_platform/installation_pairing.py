"""Instance-scoped readback credentials, separate from project authority.

Only the installation compatibility endpoint consumes these verifiers. No
project registration, checkout, submission, execution or operator capability is
created by this module. Plaintext is returned once to the bounded issuer.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
import sqlite3

CONTRACT_VERSION = '1.0'
PURPOSE = 'INSTALLATION_READBACK'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_DOMAIN = b'engineering-platform.installation-pairing.verifier.v1\0'


class InstallationPairingError(RuntimeError):
    """Stable nonsecret rejection of an installation pairing transition."""


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise InstallationPairingError('INSTALLATION_PAIRING_ID_INVALID')
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute('''CREATE TABLE IF NOT EXISTS ep_installation_pairings (
        binding_id TEXT PRIMARY KEY, ep_instance_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
        forge_instance_id TEXT NOT NULL, consumer_id TEXT NOT NULL,
        registration_operation_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        revoked_at TEXT, revocation_operation_id TEXT UNIQUE)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS ep_installation_pairing_credentials (
        credential_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES ep_installation_pairings(binding_id),
        issuance_operation_id TEXT NOT NULL UNIQUE, verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32),
        issued_at TEXT NOT NULL, revoked_at TEXT, revocation_operation_id TEXT UNIQUE)''')
    connection.execute('CREATE INDEX IF NOT EXISTS ep_installation_pairing_credential_lookup ON ep_installation_pairing_credentials(binding_id,revoked_at)')


def register(connection: sqlite3.Connection, *, binding_id: str, ep_instance_id: str,
             forge_instance_id: str, consumer_id: str, operation_id: str) -> dict[str, object]:
    target = tuple(_id(v) for v in (binding_id, ep_instance_id, forge_instance_id, consumer_id, operation_id))
    row = connection.execute('SELECT binding_id,ep_instance_id,forge_instance_id,consumer_id,registration_operation_id,revoked_at FROM ep_installation_pairings WHERE binding_id=? OR registration_operation_id=?', (binding_id, operation_id)).fetchall()
    if row:
        if len(row) != 1 or tuple(row[0][:5]) != target or row[0][5] is not None:
            raise InstallationPairingError('INSTALLATION_PAIRING_CONFLICT')
    else:
        connection.execute('INSERT INTO ep_installation_pairings(binding_id,ep_instance_id,forge_instance_id,consumer_id,registration_operation_id,created_at) VALUES(?,?,?,?,?,?)', (*target, _now()))
    return status(connection, binding_id=binding_id)


def status(connection: sqlite3.Connection, *, binding_id: str) -> dict[str, object]:
    _id(binding_id)
    row = connection.execute('SELECT ep_instance_id,forge_instance_id,consumer_id,revoked_at FROM ep_installation_pairings WHERE binding_id=?', (binding_id,)).fetchone()
    if row is None:
        raise InstallationPairingError('INSTALLATION_PAIRING_NOT_REGISTERED')
    credentials = connection.execute('SELECT credential_id,issuance_operation_id,issued_at,revoked_at FROM ep_installation_pairing_credentials WHERE binding_id=? ORDER BY credential_id', (binding_id,)).fetchall()
    return dict(binding_id=binding_id, ep_instance_id=row[0], forge_instance_id=row[1], consumer_id=row[2], purpose=PURPOSE, status='REVOKED' if row[3] else 'ACTIVE', credentials=[dict(credential_id=r[0], operation_id=r[1], issued_at=r[2], status='REVOKED' if r[3] else 'ACTIVE') for r in credentials])


@dataclass(frozen=True)
class IssuedCredential:
    credential_id: str
    binding_id: str
    operation_id: str
    credential: str = field(repr=False)

    def disclosure(self) -> dict[str, str]:
        return dict(credential_id=self.credential_id, binding_id=self.binding_id,
                    operation_id=self.operation_id, purpose=PURPOSE, credential=self.credential)


def issue(connection: sqlite3.Connection, *, binding_id: str, operation_id: str) -> IssuedCredential:
    _id(binding_id); _id(operation_id)
    if status(connection, binding_id=binding_id)['status'] != 'ACTIVE':
        raise InstallationPairingError('INSTALLATION_PAIRING_REVOKED')
    if connection.execute('SELECT 1 FROM ep_installation_pairing_credentials WHERE issuance_operation_id=?', (operation_id,)).fetchone():
        raise InstallationPairingError('INSTALLATION_CREDENTIAL_ALREADY_ISSUED_USE_STATUS')
    active = connection.execute('SELECT count(*) FROM ep_installation_pairing_credentials WHERE binding_id=? AND revoked_at IS NULL', (binding_id,)).fetchone()[0]
    if active >= 2:
        raise InstallationPairingError('INSTALLATION_CREDENTIAL_ROTATION_LIMIT')
    material = secrets.token_urlsafe(32)
    credential_id = 'installation-' + secrets.token_hex(16)
    verifier = hashlib.sha256(_DOMAIN + material.encode('ascii')).digest()
    connection.execute('INSERT INTO ep_installation_pairing_credentials(credential_id,binding_id,issuance_operation_id,verifier,issued_at) VALUES(?,?,?,?,?)', (credential_id, binding_id, operation_id, verifier, _now()))
    return IssuedCredential(credential_id, binding_id, operation_id, material)


def authenticate(connection: sqlite3.Connection, token: object) -> dict[str, str] | None:
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        return None
    verifier = hashlib.sha256(_DOMAIN + token.encode('ascii')).digest()
    row = connection.execute('''SELECT p.binding_id,p.ep_instance_id,p.forge_instance_id,p.consumer_id,c.verifier
        FROM ep_installation_pairing_credentials c JOIN ep_installation_pairings p ON p.binding_id=c.binding_id
        WHERE c.verifier=? AND c.revoked_at IS NULL AND p.revoked_at IS NULL''', (verifier,)).fetchone()
    if row is None or not hmac.compare_digest(bytes(row[4]), verifier):
        return None
    return dict(binding_id=row[0], ep_instance_id=row[1], forge_instance_id=row[2], consumer_id=row[3], purpose=PURPOSE)


def revoke_credential(connection: sqlite3.Connection, *, binding_id: str, credential_id: str, operation_id: str) -> dict[str, str]:
    for value in (binding_id, credential_id, operation_id): _id(value)
    row = connection.execute('SELECT binding_id,revoked_at,revocation_operation_id FROM ep_installation_pairing_credentials WHERE credential_id=?', (credential_id,)).fetchone()
    if row is None or row[0] != binding_id:
        raise InstallationPairingError('INSTALLATION_CREDENTIAL_TARGET_MISMATCH')
    if row[1] is not None:
        if row[2] != operation_id:
            raise InstallationPairingError('INSTALLATION_CREDENTIAL_REVOCATION_CONFLICT')
    else:
        connection.execute('UPDATE ep_installation_pairing_credentials SET revoked_at=?,revocation_operation_id=? WHERE credential_id=?', (_now(), operation_id, credential_id))
    return dict(binding_id=binding_id, credential_id=credential_id, operation_id=operation_id, status='REVOKED')


def revoke_pairing(connection: sqlite3.Connection, *, binding_id: str, operation_id: str) -> dict[str, str]:
    """Disable this binding without deleting credentials or project history."""
    _id(binding_id); _id(operation_id)
    row = connection.execute('SELECT revoked_at,revocation_operation_id FROM ep_installation_pairings WHERE binding_id=?', (binding_id,)).fetchone()
    if row is None:
        raise InstallationPairingError('INSTALLATION_PAIRING_NOT_REGISTERED')
    if row[0] is not None:
        if row[1] != operation_id:
            raise InstallationPairingError('INSTALLATION_PAIRING_REVOCATION_CONFLICT')
    else:
        connection.execute('UPDATE ep_installation_pairings SET revoked_at=?,revocation_operation_id=? WHERE binding_id=?', (_now(), operation_id, binding_id))
    return dict(binding_id=binding_id, operation_id=operation_id, status='REVOKED')


def validate_schema(connection: sqlite3.Connection) -> None:
    """Read-only structural guard; missing constraints must not become authority."""
    shapes = {
        'ep_installation_pairings': (
            ('binding_id','TEXT',0,1),('ep_instance_id','TEXT',1,0),('forge_instance_id','TEXT',1,0),
            ('consumer_id','TEXT',1,0),('registration_operation_id','TEXT',1,0),('created_at','TEXT',1,0),
            ('revoked_at','TEXT',0,0),('revocation_operation_id','TEXT',0,0)),
        'ep_installation_pairing_credentials': (
            ('credential_id','TEXT',0,1),('binding_id','TEXT',1,0),('issuance_operation_id','TEXT',1,0),
            ('verifier','BLOB',1,0),('issued_at','TEXT',1,0),('revoked_at','TEXT',0,0),
            ('revocation_operation_id','TEXT',0,0)),
    }
    for table,expected in shapes.items():
        observed = tuple((row[1],row[2].upper(),row[3],row[5]) for row in connection.execute('SELECT * FROM pragma_table_info(?)',(table,)))
        unique = {tuple(row[0] for row in connection.execute('SELECT name FROM pragma_index_info(?) ORDER BY seqno',(index[1],)))
                  for index in connection.execute('SELECT * FROM pragma_index_list(?)',(table,)) if index[2]}
        required = {('binding_id',),('registration_operation_id',),('revocation_operation_id',)} if table=='ep_installation_pairings' else {('credential_id',),('issuance_operation_id',),('verifier',),('revocation_operation_id',)}
        foreign = {(row[2],row[3],row[4]) for row in connection.execute('SELECT * FROM pragma_foreign_key_list(?)',(table,))}
        expected_foreign = {('ep_installations','ep_instance_id','instance_id')} if table=='ep_installation_pairings' else {('ep_installation_pairings','binding_id','binding_id')}
        if observed != expected or not required <= unique or foreign != expected_foreign:
            raise InstallationPairingError('INSTALLATION_PAIRING_SCHEMA_INVALID')
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name='ep_installation_pairing_credentials'").fetchone()
    if row is None or 'check(length(verifier)=32)' not in ''.join(row[0].lower().split()):
        raise InstallationPairingError('INSTALLATION_PAIRING_SCHEMA_INVALID')
