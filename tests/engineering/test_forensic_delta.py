"""Qualification for the read-only forensic delta exporter."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest

from engineering_platform.forensic_delta import canonical_report_json, export_forensic_delta
from engineering_platform.central_store_migration import main


class ForensicDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.baseline = Path(self.temporary.name) / "baseline.db"
        self.candidate = Path(self.temporary.name) / "candidate.db"
        for path in (self.baseline, self.candidate):
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE execution_submissions (
                        submission_id TEXT PRIMARY KEY, producer_id TEXT, producer_type TEXT,
                        prompt_content TEXT, received_at TEXT
                    );
                    CREATE TABLE execution_runs (run_id TEXT PRIMARY KEY, submission_id TEXT, status TEXT);
                    CREATE TABLE provider_invocations (
                        invocation_id TEXT PRIMARY KEY, run_id TEXT, payload BLOB
                    );
                    CREATE TABLE qualification_results (id INTEGER PRIMARY KEY, run_id TEXT, status TEXT);
                    CREATE TABLE reconciliation_history (id INTEGER PRIMARY KEY, run_id TEXT, submission_id TEXT);
                    CREATE TABLE unique_rows (external_id TEXT NOT NULL UNIQUE, value TEXT);
                    CREATE TABLE json_rows (id TEXT PRIMARY KEY, payload TEXT);
                    CREATE TABLE local_api_consumer_registrations (
                        consumer_id TEXT, project_id TEXT, status TEXT
                    );
                    CREATE TABLE unresolved_rows (value TEXT);
                """)
        with sqlite3.connect(self.baseline) as connection:
            connection.executescript("""
                INSERT INTO execution_submissions VALUES ('sub-1','human','HUMAN','baseline prompt','2026-01-01');
                INSERT INTO execution_runs VALUES ('run-1','sub-1','COMPLETE');
                INSERT INTO provider_invocations VALUES ('invoke-1','run-1',X'00ff');
                INSERT INTO qualification_results VALUES (1,'run-1','PASS');
                INSERT INTO reconciliation_history VALUES (1,'run-1','sub-1');
                INSERT INTO unique_rows VALUES ('unique-1','same');
                INSERT INTO json_rows VALUES ('json-1','{"alpha":1,"beta":2}');
                INSERT INTO local_api_consumer_registrations VALUES ('consumer-1','project-1','ACTIVE');
                INSERT INTO unresolved_rows VALUES ('baseline only');
            """)
        with sqlite3.connect(self.candidate) as connection:
            connection.executescript("""
                INSERT INTO execution_submissions VALUES ('sub-1','human','HUMAN','candidate prompt','2026-01-01');
                INSERT INTO execution_submissions VALUES ('sub-2','human','HUMAN','new prompt','2026-01-02');
                INSERT INTO execution_runs VALUES ('run-1','sub-1','COMPLETE');
                INSERT INTO execution_runs VALUES ('run-2','sub-2','ACTIVE');
                INSERT INTO provider_invocations VALUES ('invoke-1','run-1',X'00ff');
                INSERT INTO qualification_results VALUES (1,'run-1','PASS');
                INSERT INTO reconciliation_history VALUES (1,'run-1','sub-1');
                INSERT INTO unique_rows VALUES ('unique-1','same');
                INSERT INTO json_rows VALUES ('json-1','{"beta":2,"alpha":1}');
                INSERT INTO local_api_consumer_registrations VALUES ('consumer-1','project-1','DISABLED');
                INSERT INTO unresolved_rows VALUES ('candidate only');
                CREATE TABLE backup_probe (id TEXT PRIMARY KEY, marker INTEGER);
                INSERT INTO backup_probe VALUES ('marker-41',41);
            """)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_classifies_rows_safely_and_deterministically(self) -> None:
        before = (self.baseline.read_bytes(), self.candidate.read_bytes())
        first = export_forensic_delta(self.baseline, self.candidate, migration_id="migration-1")
        second = export_forensic_delta(self.baseline, self.candidate, migration_id="migration-1")
        self.assertEqual(before, (self.baseline.read_bytes(), self.candidate.read_bytes()))
        self.assertEqual(canonical_report_json(first), canonical_report_json(second))
        self.assertEqual(first["report_digest"], second["report_digest"])
        self.assertTrue(first["read_only_verified"])
        self.assertEqual(first["schema_difference"]["tables_candidate_only"], ["backup_probe"])
        self.assertGreater(first["summary"]["graph_edge_count"], 0)

        tables = {table["table_name"]: table for table in first["tables"]}
        self.assertEqual(tables["execution_submissions"]["modified_count"], 1)
        self.assertEqual(tables["execution_submissions"]["added_count"], 1)
        self.assertEqual(tables["local_api_consumer_registrations"]["key_definition"]["source"], "REGISTERED_COMPOSITE")
        self.assertEqual(tables["unique_rows"]["key_definition"]["source"], "UNIQUE_INDEX")
        self.assertEqual(tables["unresolved_rows"]["key_status"], "KEY_UNRESOLVED")
        self.assertEqual(first["diagnostics"]["key_unresolved_tables"], ["unresolved_rows"])
        self.assertEqual(tables["json_rows"]["unchanged_count"], 1)
        changed = tables["execution_submissions"]["changes"][0]
        self.assertEqual(changed["change_type"], "MODIFIED")
        self.assertNotIn("baseline prompt", json.dumps(first))
        prompt_change = next(item for item in changed["changed_fields"] if item["column"] == "prompt_content")
        self.assertEqual(prompt_change["baseline"], "REDACTED")
        self.assertEqual(prompt_change["candidate"], "REDACTED")
        self.assertEqual(tables["provider_invocations"]["changes"], [])

    def test_removed_rows_schema_columns_and_blob_digest_are_reported_without_plaintext(self) -> None:
        with sqlite3.connect(self.baseline) as connection:
            connection.execute("INSERT INTO provider_invocations VALUES ('invoke-removed','run-1',?)", (b"credential plaintext",))
            connection.execute("ALTER TABLE unique_rows ADD COLUMN baseline_only TEXT")
        with sqlite3.connect(self.candidate) as connection:
            connection.execute("ALTER TABLE unique_rows ADD COLUMN candidate_only TEXT")
            connection.execute("UPDATE provider_invocations SET payload=? WHERE invocation_id='invoke-1'", (b"changed blob",))
        report = export_forensic_delta(self.baseline, self.candidate, migration_id="migration-2")
        tables = {table["table_name"]: table for table in report["tables"]}
        self.assertEqual(tables["provider_invocations"]["removed_count"], 1)
        self.assertEqual(tables["provider_invocations"]["modified_count"], 1)
        removed = next(item for item in tables["provider_invocations"]["changes"] if item["change_type"] == "REMOVED")
        self.assertNotIn("credential plaintext", json.dumps(removed))
        self.assertEqual(tables["unique_rows"]["columns_baseline_only"], ["baseline_only"])
        self.assertEqual(tables["unique_rows"]["columns_candidate_only"], ["candidate_only"])
        modified = next(item for item in tables["provider_invocations"]["changes"] if item["change_type"] == "MODIFIED")
        blob_change = next(item for item in modified["changed_fields"] if item["column"] == "payload")
        self.assertIn("sha256", blob_change["baseline"])
        self.assertNotIn("changed blob", json.dumps(blob_change))

    def test_cli_writes_only_requested_report_and_strict_mode_flags_unresolved_keys(self) -> None:
        output = Path(self.temporary.name) / "report.json"
        stream = StringIO()
        with redirect_stdout(stream):
            status = main([
                "forensic-delta", "--baseline", str(self.baseline), "--candidate", str(self.candidate),
                "--migration-id", "migration-3", "--json", "--output", str(output), "--strict",
            ])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stream.getvalue()), json.loads(output.read_text(encoding="utf-8")))
