from pathlib import Path
import http.server
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import installation_pairing as pairing, server, storage, central_operational_reset


class InstallationPairingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.identity = server.initialize(self.root).instance_id
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        with storage.sqlite_connection(self.database) as connection:
            pairing.register(connection, binding_id='installation-binding', ep_instance_id=self.identity,
                             forge_instance_id='forge-instance', consumer_id='forge', operation_id='register-one')
            self.issued = pairing.issue(connection, binding_id='installation-binding', operation_id='issue-one')
        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server._HealthHandler)
        self.http.data_root = self.root
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, path='/v1/installation-compatibility', **headers):
        request = Request('http://127.0.0.1:' + str(self.http.server_port) + path, headers={
            'Authorization': 'Bearer ' + self.issued.credential,
            'EP-Instance-ID': self.identity, 'Forge-Instance-ID': 'forge-instance', **headers})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers
        except HTTPError as response:
            with response:
                return response.code, json.loads(response.read()), response.headers

    def test_empty_project_installation_is_authenticated_without_execution_authority(self):
        code, value, headers = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(value['authentication']['forge_instance_id'], 'forge-instance')
        self.assertEqual(value['authentication']['purpose'], pairing.PURPOSE)
        self.assertEqual(value['authority'], dict(installation_readback=True, project_access=False,
                                                 submission=False, execution=False, governance=False))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(self.issued.credential, json.dumps(value))
        with storage.sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM ep_project_registrations').fetchone()[0], 0)
            self.assertIsNone(server._authenticated_consumer_scope(connection, self.issued.credential))

    def test_project_scope_headers_are_rejected(self):
        self.assertEqual(self.request(**{'EP-Project-ID':'project','EP-Repository-ID':'repository'})[0], 400)
        self.assertEqual(self.request('/v1/installation-compatibility?project=project')[0], 400)

    def test_wrong_instance_or_bearer_is_rejected(self):
        self.assertEqual(self.request(**{'Forge-Instance-ID':'other'})[0], 403)
        self.assertEqual(self.request(**{'EP-Instance-ID':'other'})[0], 403)
        self.assertEqual(self.request(**{'Authorization':'Bearer invalid'})[0], 401)

    def test_installation_credential_does_not_authorize_project_compatibility(self):
        self.assertEqual(self.request('/v1/producer-compatibility', **{'EP-Project-ID':'project','EP-Repository-ID':'repository'})[0], 401)

    def test_plaintext_is_not_persisted_or_in_representation(self):
        self.assertNotIn(self.issued.credential, repr(self.issued))
        self.assertNotIn(self.issued.credential.encode(), self.database.read_bytes())
        with storage.sqlite_connection(self.database) as connection:
            self.assertNotIn(self.issued.credential, json.dumps(pairing.status(connection, binding_id='installation-binding')))

    def test_lost_issue_response_cannot_redisclose_or_issue_again(self):
        with storage.sqlite_connection(self.database) as connection:
            with self.assertRaisesRegex(pairing.InstallationPairingError, 'ALREADY_ISSUED'):
                pairing.issue(connection, binding_id='installation-binding', operation_id='issue-one')
            value = pairing.status(connection, binding_id='installation-binding')
            self.assertEqual(len(value['credentials']), 1)
            self.assertEqual(value['credentials'][0]['operation_id'], 'issue-one')

    def test_revoke_is_exact_idempotent_and_preserves_history(self):
        with storage.sqlite_connection(self.database) as connection:
            for _ in range(2):
                pairing.revoke_credential(connection, binding_id='installation-binding', credential_id=self.issued.credential_id, operation_id='revoke-one')
            self.assertIsNone(pairing.authenticate(connection, self.issued.credential))
            with self.assertRaisesRegex(pairing.InstallationPairingError, 'REVOCATION_CONFLICT'):
                pairing.revoke_credential(connection, binding_id='installation-binding', credential_id=self.issued.credential_id, operation_id='other')
            self.assertEqual(pairing.status(connection,binding_id='installation-binding')['credentials'][0]['status'], 'REVOKED')
        self.assertEqual(self.request()[0], 401)

    def test_detach_disables_all_binding_credentials_and_keeps_history(self):
        with storage.sqlite_connection(self.database) as connection:
            for _ in range(2):
                pairing.revoke_pairing(connection,binding_id='installation-binding',operation_id='detach-one')
            self.assertIsNone(pairing.authenticate(connection,self.issued.credential))
            self.assertEqual(len(pairing.status(connection,binding_id='installation-binding')['credentials']),1)
            with self.assertRaisesRegex(pairing.InstallationPairingError,'REVOKED'):
                pairing.issue(connection,binding_id='installation-binding',operation_id='issue-two')
        self.assertEqual(self.request()[0],401)

    def test_missing_structural_guards_fail_closed(self):
        with storage.sqlite_connection(self.database) as connection:
            pairing.validate_schema(connection)
            connection.execute('ALTER TABLE ep_installation_pairing_credentials ADD COLUMN unexpected TEXT')
            with self.assertRaisesRegex(pairing.InstallationPairingError,'SCHEMA_INVALID'):
                pairing.validate_schema(connection)
        with self.assertRaises(server.ServerConfigurationError):
            server.initialize(self.root)

    def test_operational_reset_recognizes_and_preserves_pairing_authority(self):
        preview=central_operational_reset.preview(self.root)
        self.assertNotIn('SCHEMA_UNSUPPORTED',preview['blocking_codes'])
        self.assertNotIn('TABLE_CLASSIFICATION_INCOMPLETE',preview['blocking_codes'])
        self.assertIn('ep_installation_pairings',central_operational_reset.INSTALLATION_AND_CONFIGURATION)
        self.assertIn('ep_installation_pairing_credentials',central_operational_reset.SECURITY_AND_AUTHORITY_LEDGER)
        with storage.sqlite_connection(self.database) as connection:
            scope=pairing.authenticate(connection,self.issued.credential)
            self.assertEqual(scope['binding_id'],'installation-binding')

    def test_registration_cannot_change_registered_instance(self):
        with storage.sqlite_connection(self.database) as connection:
            with self.assertRaisesRegex(pairing.InstallationPairingError, 'CONFLICT'):
                pairing.register(connection, binding_id='installation-binding', ep_instance_id=self.identity,
                                 forge_instance_id='other', consumer_id='forge', operation_id='register-one')

if __name__ == '__main__': unittest.main()
