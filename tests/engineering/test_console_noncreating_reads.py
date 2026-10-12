"""Filesystem effects and real HTTP outcomes of CENTRAL source-loss races."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from engineering_platform import server
from tests.engineering import test_console_stream_access as access


class ConsoleNoncreatingReadsTests(unittest.TestCase):
    setUp = access.ConsoleStreamAccessTests.setUp
    stop = access.ConsoleStreamAccessTests.stop
    request = access.ConsoleStreamAccessTests.request
    def lose_after_real_list(self):
        original = server._console_projects
        displaced = self.fixture.database.with_suffix(".original")
        before = self.fixture.database.read_bytes()
        self.preserve_original(before)
        called = []
        def listing(root):
            records = original(root)
            if not called:
                called.append(records)
                self.fixture.database.rename(displaced)
            return records
        return listing, displaced, before, called

    def restore_original(self, displaced, before):
        self.assertEqual(hashlib.sha256(displaced.read_bytes()).digest(), hashlib.sha256(before).digest())
        if self.fixture.database.exists():
            self.fixture.database.rename(self.fixture.database.with_suffix(".replacement"))
        displaced.rename(self.fixture.database)

    def test_actual_list_queue_race_is_unavailable_without_replacement(self):
        listing, displaced, before, called = self.lose_after_real_list()
        try:
            with patch.object(server, "_console_projects", listing):
                with self.assertRaises(server._ConsoleReadError) as rejected:
                    server._console_project_read(self.fixture.data, "project", server._central_console_project_snapshot)
            self.assertEqual(rejected.exception.outcome, "UNAVAILABLE")
            self.assertTrue(called)
            self.assertFalse(self.fixture.database.exists())
        finally:
            if displaced.exists():
                self.restore_original(displaced, before)

    def test_actual_http_list_queue_race_is_503_without_replacement_or_handler_error(self):
        original = server._console_queue_projection
        listing, displaced, before, called = self.lose_after_real_list()
        def queue(root, project):
            with patch.object(server, "_console_projects", listing):
                return original(root, project)
        try:
            with patch.object(server, "_console_queue_projection", queue):
                with self.assertRaises(HTTPError) as rejected:
                    self.request("/api/dashboard-snapshot")
                with rejected.exception as response:
                    self.assertEqual(response.code, 503)
                    self.assertEqual(json.load(response), {"error": "CENTRAL_UNAVAILABLE"})
            self.assertTrue(called)
            self.assertFalse(self.fixture.database.exists())
            self.assertEqual(self.errors, [])
        finally:
            if displaced.exists():
                self.restore_original(displaced, before)

    def test_present_empty_store_is_not_unavailable_or_repaired(self):
        displaced = self.fixture.database.with_suffix(".original")
        before = self.fixture.database.read_bytes()
        self.preserve_original(before)
        self.fixture.database.rename(displaced)
        self.fixture.database.touch()
        try:
            with self.assertRaises((sqlite3.OperationalError, server.ServerConfigurationError)) as rejected:
                server._console_project_read(self.fixture.data, "project", server._central_console_project_snapshot)
            self.assertNotIsInstance(rejected.exception, server._ConsoleReadError)
            self.assertEqual(self.fixture.database.stat().st_size, 0)
        finally:
            self.restore_original(displaced, before)

    def test_read_open_quotes_uri_and_cannot_write_or_create(self):
        from pathlib import Path
        from engineering_platform import storage
        root = self.fixture.area / 'CENTRAL ? # % ruimte 漢'
        root.mkdir()
        database = root / 'epdata.sqlite'
        with storage.sqlite_connection(database) as connection:
            connection.execute('CREATE TABLE marker(value TEXT)')
            connection.execute("INSERT INTO marker VALUES('intended')")
        before = database.read_bytes()
        with storage.sqlite_read_connection(database) as connection:
            self.assertEqual(connection.execute('SELECT value FROM marker').fetchone(), ('intended',))
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO marker VALUES('forbidden')")
        self.assertEqual(database.read_bytes(), before)
        displaced = database.with_suffix('.original')
        database.rename(displaced)
        with self.assertRaises(sqlite3.OperationalError) as rejected:
            storage.open_sqlite_read_connection(database)
        self.assertTrue(server._console_source_unavailable(rejected.exception))
        self.assertFalse(database.exists())
        # An unescaped URI would point at this different existing neighbour.
        bait = self.fixture.area / 'CENTRAL '
        bait.touch()
        with self.assertRaises(sqlite3.OperationalError):
            storage.open_sqlite_read_connection(database)
        self.assertFalse(database.exists())
        self.assertEqual(bait.stat().st_size, 0)

    def test_direct_helpers_never_create_missing_central(self):
        from engineering_platform import execution_lifecycle, execution_timing, provider_recovery, provider_usage, storage, telemetry_contract
        database = self.fixture.database
        readers = {
            'projects': lambda: server._console_projects(self.fixture.data),
            'queue': lambda: server._console_queue_projection(self.fixture.data, 'project'),
            'runs': lambda: server._central_console_run_records(self.fixture.data, 'project'),
            'pr-context': lambda: server._central_console_pull_request_context(self.fixture.data, 'run'),
            'reviewers': lambda: server._central_console_invocation_reviewers(self.fixture.data, 'run'),
            'validation': lambda: server._central_console_validation_evidence(self.fixture.data, 'project', 'run'),
            'report': lambda: server._central_console_report_path(self.fixture.data, 'project', 'run'),
            'history': lambda: server._central_console_chat_history(self.fixture.data, 'project', 'run'),
            'configuration': lambda: server._central_console_configuration(self.fixture.data),
            'lifecycle': lambda: execution_lifecycle.projection(self.fixture.data, 'run', central_database=database),
            'recovery': lambda: provider_recovery.load_recovery_state(self.fixture.data, 'run', central_database=database),
            'qualification': lambda: storage.load_run_qualification_snapshot(self.fixture.data, 'run', central_database=database),
            'timing': lambda: execution_timing.timing_summary(self.fixture.data, 'run', central_database=database),
            'timing-population': lambda: execution_timing.timing_summaries(self.fixture.data, ['run'], central_database=database),
            'usage': lambda: provider_usage.provider_usage_summary(self.fixture.data, 'run', central_database=database),
            'usage-population': lambda: provider_usage.provider_usage_summaries(self.fixture.data, ['run'], central_database=database),
            'telemetry': lambda: telemetry_contract.run_telemetry_snapshot(self.fixture.data, 'run', central_database=database),
        }
        displaced = database.with_suffix('.original')
        before = database.read_bytes()
        self.preserve_original(before)
        database.rename(displaced)
        try:
            for name, reader in readers.items():
                with self.subTest(reader=name):
                    with self.assertRaises(sqlite3.OperationalError) as rejected:
                        reader()
                    self.assertTrue(server._console_source_unavailable(rejected.exception))
                    self.assertFalse(database.exists())
        finally:
            self.restore_original(displaced, before)
        # Recovery is a fresh real list/context/FIFO read on the restored bytes.
        self.assertIsInstance(server._console_queue_projection(self.fixture.data, 'project'), dict)

    def test_open_race_before_context_and_schema_validation_cannot_create(self):
        from engineering_platform import storage
        original_open = storage.open_sqlite_read_connection
        for reader in (lambda: server._console_queue_projection(self.fixture.data, 'project'),
                       lambda: server.validate_store(self.fixture.data, server._console_source_identity(self.fixture.data))):
            calls = []
            displaced = self.fixture.database.with_suffix('.original')
            before = self.fixture.database.read_bytes()
            self.preserve_original(before)
            def opening(database):
                calls.append(database)
                if len(calls) == 2:
                    self.fixture.database.rename(displaced)
                return original_open(database)
            try:
                with patch.object(storage, 'open_sqlite_read_connection', opening):
                    with self.assertRaises((sqlite3.OperationalError, server._ConsoleReadError, server.ServerConfigurationError)):
                        reader()
                self.assertTrue(displaced.exists())
                self.assertFalse(self.fixture.database.exists())
            finally:
                if displaced.exists():
                    self.restore_original(displaced, before)

    def test_corrupt_and_missing_schema_remain_ordinary_errors(self):
        from engineering_platform import storage, execution_lifecycle
        displaced = self.fixture.database.with_suffix('.original')
        before = self.fixture.database.read_bytes()
        self.preserve_original(before)
        self.fixture.database.rename(displaced)
        try:
            for content in (b'not a SQLite authority', b''):
                self.fixture.database.write_bytes(content)
                with self.assertRaises(sqlite3.DatabaseError):
                    server._console_projects(self.fixture.data)
                with self.assertRaises(sqlite3.DatabaseError):
                    storage.load_run_qualification_snapshot(self.fixture.data, 'run', central_database=self.fixture.database)
                with self.assertRaises(sqlite3.DatabaseError):
                    execution_lifecycle.projection(self.fixture.data, 'run', central_database=self.fixture.database)
                self.assertEqual(self.fixture.database.read_bytes(), content)
        finally:
            self.restore_original(displaced, before)

    def test_replaced_instance_with_same_project_repository_is_not_published(self):
        from tests.engineering.dashboard_run_evidence_fixture import RealTopologyFixture
        foreign = RealTopologyFixture()
        self.addCleanup(foreign.close)
        original_identity = (self.fixture.data / server.SERVER_IDENTITY_FILENAME).read_bytes()
        before = self.fixture.database.read_bytes()
        self.preserve_original(before)
        displaced = self.fixture.database.with_suffix('.original')
        def replace(root, project):
            self.fixture.database.rename(displaced)
            self.fixture.database.write_bytes(foreign.database.read_bytes())
            (self.fixture.data / server.SERVER_IDENTITY_FILENAME).write_bytes((foreign.data / server.SERVER_IDENTITY_FILENAME).read_bytes())
            # The new store is internally valid and has the same project/repo.
            server._console_source_identity(self.fixture.data)
            return server._console_queue_projection(root, project)
        try:
            with self.assertRaises(server._ConsoleReadError) as rejected:
                server._console_project_read(self.fixture.data, 'project', replace)
            self.assertEqual(rejected.exception.outcome, 'CONFLICT')
        finally:
            if displaced.exists():
                self.restore_original(displaced, before)
            (self.fixture.data / server.SERVER_IDENTITY_FILENAME).write_bytes(original_identity)

    def preserve_original(self, content):
        import os
        from pathlib import Path
        target = os.environ.get('EPR12_EVIDENCE_ROOT')
        if target:
            root = Path(target)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = root / (self.id().split('.')[-1] + '-' + hashlib.sha256(content).hexdigest() + '.sqlite')
            if path.exists():
                self.assertEqual(path.read_bytes(), content)
                return
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as evidence:
                evidence.write(content)
                evidence.flush()
                os.fsync(evidence.fileno())

    def test_no_project_initial_http_loss_is_503_without_database_creation(self):
        from urllib.request import urlopen
        original = server._no_project_console_snapshot
        displaced = self.fixture.database.with_suffix('.original')
        before = self.fixture.database.read_bytes()
        self.preserve_original(before)
        def snapshot(root):
            result = original(root)
            self.fixture.database.rename(displaced)
            return result
        try:
            with patch.object(server, '_no_project_console_snapshot', snapshot):
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(f'http://127.0.0.1:{self.httpd.server_port}/api/events', timeout=5)
                with rejected.exception as response:
                    self.assertEqual(response.code, 503)
                    self.assertEqual(json.load(response), {'error': 'CENTRAL_UNAVAILABLE'})
            self.assertFalse(self.fixture.database.exists())
            self.assertEqual(self.errors, [])
        finally:
            if displaced.exists():
                self.restore_original(displaced, before)
