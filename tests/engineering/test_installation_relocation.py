from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertFalse(self.root.exists())
        self.assertFalse(self.root.is_symlink())
        self.assertTrue((destination / "file-inbox/incoming").is_dir())
        self.assertEqual((destination / "artifacts/proof.txt").read_text(encoding="utf-8"), "retained")
        with sqlite3.connect(destination / "engineering.db") as connection:
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

    def test_platform_data_can_be_relocated_again_from_its_new_canonical_location(self) -> None:
        installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        installation_relocation.apply_pending(self.root)
        first_destination = self.target / self.root.name
        second_parent = Path(self.temporary.name) / "selected-again"
        second_parent.mkdir()
        installation_relocation.request(first_destination, "PLATFORM_DATA", str(second_parent))
        installation_relocation.apply_pending(first_destination)
        destination = second_parent / self.root.name
        self.assertFalse(first_destination.exists())
        self.assertEqual((destination / "artifacts/proof.txt").read_text(encoding="utf-8"), "retained")

    def test_only_one_platform_move_can_be_pending_and_payload_must_be_valid(self) -> None:
        installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_ALREADY_PENDING"):
            installation_relocation.request(self.root, "PLATFORM_DATA", str(self.target))
        pending = self.root / "runtime/pending-platform-data-relocation.json"
        pending.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_REQUEST_INVALID"):
            installation_relocation.apply_pending(self.root)

    def test_relocation_rejects_unusable_roots_and_destination_directories(self) -> None:
        """Operator input is validated before a durable move is recorded."""
        for value, code in ((None, "LOCATION_REQUIRED"), ("", "LOCATION_REQUIRED"), ("relative", "LOCATION_NOT_WRITABLE")):
            with self.subTest(value=value), self.assertRaisesRegex(installation_relocation.RelocationError, code):
                installation_relocation.request(self.root, "PLATFORM_DATA", value)

        missing = Path(self.temporary.name) / "missing-data"
        with self.assertRaisesRegex(installation_relocation.RelocationError, "PLATFORM_DATA_UNAVAILABLE"):
            installation_relocation.request(missing, "PLATFORM_DATA", str(self.target))
        pending = self.root / "runtime/pending-platform-data-relocation.json"
        pending.write_text('{"kind":"DATABASE","directory":"/tmp"}', encoding="utf-8")
        with self.assertRaisesRegex(installation_relocation.RelocationError, "RELOCATION_KIND_RETIRED"):
            installation_relocation.apply_pending(self.root)

    def test_relocation_is_atomic_without_creating_a_compatibility_link(self) -> None:
        """A completed move has one canonical location and no old-path link."""
        destination = self.target / self.root.name
        installation_relocation.relocate_platform_data(self.root, str(self.target))
        self.assertFalse(self.root.exists())
        self.assertFalse(self.root.is_symlink())
        self.assertTrue((destination / "engineering.db").is_file())
        with self.assertRaisesRegex(installation_relocation.RelocationError, "PLATFORM_DATA_UNAVAILABLE"):
            installation_relocation.relocate_platform_data(self.root, str(self.target))
