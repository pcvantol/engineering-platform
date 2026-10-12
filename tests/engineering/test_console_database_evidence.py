"""Actual file/domain faults must stay red and remain available after restore."""
import json,sqlite3,tempfile,unittest,os
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from engineering_platform import central_database
from tests.engineering.console_database_evidence import DatabaseEvidence

class DatabaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ep-fixture-evidence-')
        self.addCleanup(self.temp.cleanup)
        self.area=Path(self.temp.name);self.database=self.area/'epdata.sqlite'
        parent=Path(os.environ.get('EP_DATABASE_EVIDENCE_TEST_ROOT',tempfile.gettempdir()));parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.proof=Path(tempfile.mkdtemp(prefix='ep-db-evidence-proof-',dir=parent))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE engineering_metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE domain(value TEXT)');db.execute("INSERT INTO domain VALUES('original')");db.commit()
        self.e=DatabaseEvidence(self.database,self.proof);self.e.snapshot('T0',self.database)

    def test_unchanged_real_loss_restore_and_identity(self):
        self.e.loss();self.assertFalse(self.database.exists());self.e.restore()
        v=self.e.verdict();self.assertTrue(v['qualified']);self.assertTrue(v['restored'])
        self.assertEqual(v['phases']['T1'][''],v['phases']['T2-before-restore'][''])

    def test_supported_explicit_setup_is_replayed_not_blank_baseline_reset(self):
        self.e.event('declared_fixture_setup',action='console.log_level=DEBUG')
        with patch.object(sqlite3,'connect',self.e.traced_connect):
            central_database.update_console_interval_configuration(self.area,'log_level','DEBUG')
        self.e.loss();self.e.restore();v=self.e.verdict();self.assertTrue(v['qualified'])
        self.assertNotEqual(v['phases']['T0']['']['sha256'],v['phases']['T1']['']['sha256'])
        self.assertEqual(v['phases']['T0-replayed-owned-writes']['']['sha256'],v['phases']['T1']['']['sha256'])

    def test_unknown_pre_loss_domain_write_is_not_masked(self):
        with patch.object(sqlite3,'connect',self.e.traced_connect):
            with closing(sqlite3.connect(self.database)) as db:db.execute("UPDATE domain SET value='unknown'");db.commit()
        self.e.loss();self.e.restore();v=self.e.verdict();self.assertFalse(v['qualified'])
        self.assertIn('unapproved_pre_loss_writer',v['violations'])
        self.assertIn('unexplained_pre_loss_domain_change',v['violations'])
        self.assertTrue((self.proof/'T0/T0.sqlite').exists());self.assertTrue((self.proof/'T1/T1.sqlite').exists())

    def test_displaced_mutation_is_retained_and_never_restored_as_original(self):
        self.e.loss()
        with closing(sqlite3.connect(self.e.moved)) as db:db.execute("UPDATE domain SET value='tampered'");db.commit()
        self.e.restore();v=self.e.verdict();self.assertFalse(v['qualified']);self.assertFalse(v['restored'])
        self.assertIn('moved_source_bytes_changed',v['violations'])
        self.assertIn('moved_source_domain_changed',v['violations'])
        self.assertNotEqual((self.proof/'T1/T1.sqlite').read_bytes(),(self.proof/'T2-before-restore/T2-before-restore.sqlite').read_bytes())
        self.assertFalse(self.database.exists());self.assertTrue(self.e.moved.exists())

    def test_replacement_preserved_before_non_overwriting_restore(self):
        self.e.loss();self.database.write_bytes(b'replacement failure evidence')
        self.e.restore();v=self.e.verdict();self.assertFalse(v['qualified']);self.assertTrue(v['restored'])
        self.assertIn('replacement_file_created',v['violations'])
        self.assertEqual((self.proof/'replacement.sqlite').read_bytes(),b'replacement failure evidence')
        self.assertEqual((self.proof/'replacement-retained.sqlite').read_bytes(),b'replacement failure evidence')
        self.assertEqual(self.database.read_bytes(),(self.proof/'T1/T1.sqlite').read_bytes())

    def test_retention_failure_does_not_delete_live_failure_source(self):
        self.e.loss()
        with patch.object(self.e,'write',side_effect=OSError('controlled evidence retention failure')):
            self.e.restore()
        self.assertTrue(self.e.retention_errors);self.assertTrue(self.e.moved.exists())
        v=self.e.verdict();self.assertFalse(v['qualified']);self.assertFalse(v['restored'])
        self.assertTrue((self.proof/'T1/T1.sqlite').exists())
        self.e.write(self.proof/'failed-source-retained.sqlite',self.e.moved.read_bytes())

    def test_open_connection_after_rename_cannot_mutate_original_inode(self):
        connection=sqlite3.connect(self.database)
        try:
            self.e.loss()
            with self.assertRaises(sqlite3.OperationalError) as rejected:
                connection.execute("UPDATE domain SET value='inflight'")
            self.assertEqual(rejected.exception.sqlite_errorcode & 255,sqlite3.SQLITE_READONLY)
            connection.rollback()
            self.e.restore();v=self.e.verdict();self.assertTrue(v['qualified'])
            self.assertTrue((self.proof/'T2-before-restore/T2-before-restore.sqlite').exists())
        finally:connection.close()

    def test_old_prebrowser_hash_assertion_refault_is_red_on_proved_setup(self):
        original=(self.proof/'T0/T0.sqlite').read_bytes()
        self.e.event('declared_fixture_setup',action='console.log_level=DEBUG')
        with patch.object(sqlite3,'connect',self.e.traced_connect):
            central_database.update_console_interval_configuration(self.area,'log_level','DEBUG')
        self.e.loss()
        with self.assertRaises(AssertionError):
            self.assertEqual(self.e.moved.read_bytes(),original)
        self.e.restore();self.assertTrue(self.e.verdict()['qualified'])
        self.assertTrue((self.proof/'T0-T1-domain-diff.json').exists())
