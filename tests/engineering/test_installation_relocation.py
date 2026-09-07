from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from engineering_platform import installation_relocation


class InstallationRelocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.root.mkdir()
        self.target = Path(self.temporary.name) / "selected"
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def database(self) -> Path:
        path = self.root / "engineering.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE proof (value TEXT)")
            connection.execute("INSERT INTO proof VALUES ('retained')")
        return path

    def inbox(self) -> Path:
        source = self.root / "file-inbox"
        for name in ("incoming", "accepted", "processing", "quarantine"):
            (source / name).mkdir(parents=True, exist_ok=True)
        return source

    def test_database_request_is_applied_only_after_clean_start(self) -> None:
        source = self.database()
        requested = installation_relocation.request(self.root, "DATABASE", str(self.target))
        self.assertTrue(source.is_file())
        self.assertTrue((self.root / "runtime/pending-relocation.json").exists())

        applied = installation_relocation.apply_pending(self.root)

        self.assertEqual(requested, {key: applied[key] for key in requested})
        self.assertTrue(source.is_symlink())
        self.assertEqual(source.resolve(), (self.target / "engineering.db").resolve())
        with sqlite3.connect(source) as connection:
            self.assertEqual(connection.execute("SELECT value FROM proof").fetchone()[0], "retained")
        self.assertFalse((self.root / "runtime/pending-relocation.json").exists())

    def test_database_rejects_existing_destination(self) -> None:
        source = self.database()
        (self.target / "engineering.db").write_bytes(b"not ours")
        with self.assertRaisesRegex(installation_relocation.RelocationError, "DATABASE_DESTINATION_EXISTS"):
            installation_relocation.request(self.root, "DATABASE", str(self.target))
        self.assertFalse(source.is_symlink())

    def test_inbox_rejects_items_and_moves_only_empty_inbox(self) -> None:
        source = self.inbox()
        (source / "incoming" / "waiting.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(installation_relocation.RelocationError, "INBOX_NOT_EMPTY"):
            installation_relocation.request(self.root, "FILE_INBOX", str(self.target))
        (source / "incoming" / "waiting.json").unlink()

        installation_relocation.request(self.root, "FILE_INBOX", str(self.target))
        applied = installation_relocation.apply_pending(self.root)

        self.assertEqual(applied["kind"], "FILE_INBOX")
        self.assertTrue(source.is_symlink())
        self.assertEqual(source.resolve(), (self.target / "file-inbox").resolve())
        self.assertTrue((self.target / "file-inbox" / "quarantine").is_dir())

    def test_only_one_request_can_be_pending(self) -> None:
        self.database()
        self.inbox()
        installation_relocation.request(self.root, "DATABASE", str(self.target))
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_ALREADY_PENDING"):
            installation_relocation.request(self.root, "FILE_INBOX", str(self.target))
