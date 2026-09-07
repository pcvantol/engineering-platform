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
        with sqlite3.connect(self.root / "engineering.db") as connection:
            connection.execute("CREATE TABLE proof (value TEXT)")
            connection.execute("INSERT INTO proof VALUES ('retained')")
        (self.root / "file-inbox" / "incoming").mkdir(parents=True)
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts" / "proof.txt").write_text("retained", encoding="utf-8")
        (self.root / "runtime").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_platform_data_request_moves_every_durable_resource_as_one_unit(self) -> None:
        requested = installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        self.assertTrue((self.root / "runtime/pending-platform-data-relocation.json").exists())

        applied = installation_relocation.apply_pending(self.root)

        destination = self.target / self.root.name
        self.assertEqual(requested, {key: applied[key] for key in requested})
        self.assertEqual(applied["kind"], "PLATFORM_DATA")
        self.assertTrue(self.root.is_symlink())
        self.assertEqual(self.root.resolve(), destination.resolve())
        self.assertTrue((destination / "file-inbox/incoming").is_dir())
        self.assertEqual((destination / "artifacts/proof.txt").read_text(encoding="utf-8"), "retained")
        with sqlite3.connect(self.root / "engineering.db") as connection:
            self.assertEqual(connection.execute("SELECT value FROM proof").fetchone()[0], "retained")
        self.assertFalse((destination / "runtime/pending-platform-data-relocation.json").exists())

    def test_request_rejects_existing_or_nested_destinations(self) -> None:
        (self.target / self.root.name).mkdir()
        with self.assertRaisesRegex(installation_relocation.RelocationError, "PLATFORM_DATA_DESTINATION_EXISTS"):
            installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        with self.assertRaisesRegex(installation_relocation.RelocationError, "PLATFORM_DATA_DESTINATION_INVALID"):
            installation_relocation.request(self.root, "PLATFORM_DATA", str(self.root))

    def test_legacy_partial_relocations_are_explicitly_retired(self) -> None:
        for kind in ("DATABASE", "FILE_INBOX", "UNKNOWN"):
            with self.subTest(kind=kind), self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_KIND_RETIRED"):
                installation_relocation.request(self.root, kind, str(self.target))

    def test_platform_data_can_be_relocated_again_from_its_stable_link(self) -> None:
        installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        installation_relocation.apply_pending(self.root)
        second_parent = Path(self.temporary.name) / "selected-again"
        second_parent.mkdir()
        installation_relocation.request(self.root, "PLATFORM_DATA", str(second_parent))
        installation_relocation.apply_pending(self.root)
        self.assertEqual(self.root.resolve(), (second_parent / self.root.name).resolve())
        self.assertEqual((self.root / "artifacts/proof.txt").read_text(encoding="utf-8"), "retained")

    def test_only_one_platform_move_can_be_pending_and_payload_must_be_valid(self) -> None:
        installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_ALREADY_PENDING"):
            installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        pending = self.root / "runtime/pending-platform-data-relocation.json"
        pending.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_REQUEST_INVALID"):
            installation_relocation.apply_pending(self.root)
