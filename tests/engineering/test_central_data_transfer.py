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

    def test_import_replaces_durable_state_and_preserves_target_runtime(self) -> None:
        _, content = central_data_transfer.export_snapshot(self.root)
        archive = Path(self.temporary.name) / "backup.zip"
        archive.write_bytes(content)
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        with sqlite3.connect(target / "engineering.db") as connection:
            connection.execute("CREATE TABLE stale (value TEXT)")
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

    def test_archive_rejects_path_traversal(self) -> None:
        archive = Path(self.temporary.name) / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as snapshot:
            snapshot.writestr("../outside", "bad")
            snapshot.writestr(central_data_transfer.MANIFEST_NAME, '{"format_version":1,"kind":"engineering-platform-central-data","entries":[]}')
        with self.assertRaisesRegex(central_data_transfer.CentralDataTransferError, "CENTRAL_ARCHIVE"):
            central_data_transfer.inspect_archive(archive)
