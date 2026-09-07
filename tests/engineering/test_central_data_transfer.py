from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from engineering_platform import central_data_transfer


class CentralDataTransferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        self.root.mkdir()
        with sqlite3.connect(self.root / "engineering.db") as connection:
            connection.execute("CREATE TABLE proof (value TEXT)")
            connection.execute("INSERT INTO proof VALUES ('exported')")
            connection.execute("CREATE TABLE engineering_schema_migrations(version INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO engineering_schema_migrations VALUES (53)")
        (self.root / "file-inbox" / "accepted").mkdir(parents=True)
        (self.root / "file-inbox" / "accepted" / "receipt.json").write_text("{}", encoding="utf-8")
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts" / "report.md").write_text("evidence", encoding="utf-8")
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "runtime-only.txt").write_text("not state", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_export_contains_all_durable_data_but_never_runtime(self) -> None:
        filename, content = central_data_transfer.export_snapshot(self.root)
        archive = Path(self.temporary.name) / filename
        archive.write_bytes(content)
        details = central_data_transfer.inspect_archive(archive)
        with zipfile.ZipFile(archive) as snapshot:
            self.assertIn("engineering.db", snapshot.namelist())
            self.assertIn("file-inbox/accepted/receipt.json", snapshot.namelist())
            self.assertIn("artifacts/report.md", snapshot.namelist())
            self.assertNotIn("runtime/runtime-only.txt", snapshot.namelist())
        self.assertGreaterEqual(details["entries"], 3)
        self.assertEqual(details["schema_version"], 53)
        self.assertEqual(archive.suffix, ".epdata")

    def test_import_replaces_durable_state_and_preserves_target_runtime(self) -> None:
        _, content = central_data_transfer.export_snapshot(self.root)
        archive = Path(self.temporary.name) / "backup.zip"
        archive.write_bytes(content)
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        with sqlite3.connect(target / "engineering.db") as connection:
            connection.execute("CREATE TABLE stale (value TEXT)")
            connection.execute("CREATE TABLE engineering_schema_migrations(version INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO engineering_schema_migrations VALUES (53)")
        (target / "obsolete.txt").write_text("remove", encoding="utf-8")
        (target / "runtime").mkdir()
        (target / "runtime" / "installed-runtime.txt").write_text("keep", encoding="utf-8")
        central_data_transfer.stage_import(target, archive)
        central_data_transfer.apply_pending_import(target)
        self.assertFalse((target / "obsolete.txt").exists())
        self.assertEqual((target / "runtime/installed-runtime.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((target / "artifacts/report.md").read_text(encoding="utf-8"), "evidence")
        with sqlite3.connect(target / "engineering.db") as connection:
            self.assertEqual(connection.execute("SELECT value FROM proof").fetchone()[0], "exported")

    def test_import_rejects_an_archive_from_a_different_schema(self) -> None:
        _, content = central_data_transfer.export_snapshot(self.root)
        archive = Path(self.temporary.name) / "schema-40.zip"
        archive.write_bytes(content)
        with zipfile.ZipFile(archive) as original:
            manifest = __import__("json").loads(original.read(central_data_transfer.MANIFEST_NAME))
            database = original.read("engineering.db")
            other_entries = {name: original.read(name) for name in original.namelist() if name not in {central_data_transfer.MANIFEST_NAME, "engineering.db"}}
        with tempfile.TemporaryDirectory() as database_directory:
            database_path = Path(database_directory) / "engineering.db"
            database_path.write_bytes(database)
            with sqlite3.connect(database_path) as connection:
                connection.execute("DELETE FROM engineering_schema_migrations")
                connection.execute("INSERT INTO engineering_schema_migrations VALUES (40)")
            database = database_path.read_bytes()
        manifest["schema_version"] = 40
        manifest["entries"] = [
            {**entry, "sha256": __import__("hashlib").sha256(database).hexdigest(), "size": len(database)} if entry["path"] == "engineering.db" else entry
            for entry in manifest["entries"]
        ]
        manifest["content_sha256"] = central_data_transfer._content_checksum(manifest["entries"])
        with zipfile.ZipFile(archive, "w") as rewritten:
            rewritten.writestr("engineering.db", database)
            for name, value in other_entries.items():
                rewritten.writestr(name, value)
            rewritten.writestr(central_data_transfer.MANIFEST_NAME, __import__("json").dumps(manifest))
        with self.assertRaisesRegex(central_data_transfer.CentralDataTransferError, "CENTRAL_ARCHIVE_SCHEMA_INCOMPATIBLE"):
            central_data_transfer.stage_import(self.root, archive)

    def test_archive_rejects_path_traversal(self) -> None:
        archive = Path(self.temporary.name) / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as snapshot:
            snapshot.writestr("../outside", "bad")
            snapshot.writestr(central_data_transfer.MANIFEST_NAME, '{"format_version":1,"kind":"engineering-platform-central-data","entries":[]}')
        with self.assertRaisesRegex(central_data_transfer.CentralDataTransferError, "CENTRAL_ARCHIVE"):
            central_data_transfer.inspect_archive(archive)

    def test_archive_rejects_a_tampered_complete_content_checksum(self) -> None:
        _, content = central_data_transfer.export_snapshot(self.root)
        archive = Path(self.temporary.name) / "tampered.epdata"
        archive.write_bytes(content)
        with zipfile.ZipFile(archive) as original:
            manifest = __import__("json").loads(original.read(central_data_transfer.MANIFEST_NAME))
            files = {name: original.read(name) for name in original.namelist() if name != central_data_transfer.MANIFEST_NAME}
        manifest["content_sha256"] = "0" * 64
        with zipfile.ZipFile(archive, "w") as rewritten:
            for name, value in files.items():
                rewritten.writestr(name, value)
            rewritten.writestr(central_data_transfer.MANIFEST_NAME, __import__("json").dumps(manifest))
        with self.assertRaisesRegex(central_data_transfer.CentralDataTransferError, "CENTRAL_ARCHIVE_INTEGRITY_INVALID"):
            central_data_transfer.inspect_archive(archive)
