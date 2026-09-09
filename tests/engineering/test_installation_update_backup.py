from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_backup import InstallationUpdateBackupError, backup
from engineering_platform.installation_update_plan import InstallationUpdatePlan


class InstallationUpdateBackupTests(unittest.TestCase):
    def test_retains_verified_backup_outside_disposable_cleanup(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); database = root / "epdata.sqlite"
            with sqlite3.connect(database) as connection: connection.execute("CREATE TABLE evidence(value TEXT)")
            plan = InstallationUpdatePlan("update-0001", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64, "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), tuple(str(root / "operations" / "update-0001" / name) for name in ("build", "download", "pip-cache")), ())
            evidence = backup(plan)
            self.assertEqual((evidence["integrity"], evidence["retention"]), ("PASS", "UPDATE_RECOVERY"))
            self.assertTrue(Path(str(evidence["path"])).is_file())
            self.assertNotIn(str(evidence["path"]), plan.cleanup_targets)

            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO evidence VALUES ('after-backup')")
            self.assertEqual(backup(plan), evidence)

    def test_fails_closed_when_central_is_unavailable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = InstallationUpdatePlan("update-0002", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64, "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), (), ())
            with patch("engineering_platform.installation_update_backup.central_database.snapshot", return_value=None):
                with self.assertRaisesRegex(InstallationUpdateBackupError, "source is unavailable"):
                    backup(plan)

    def test_rejects_a_preexisting_corrupt_recovery_artifact(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "operations" / "update-0003" / "backup" / "central.sqlite"
            path.parent.mkdir(parents=True); path.write_text("not sqlite", encoding="utf-8")
            plan = InstallationUpdatePlan("update-0003", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64, "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), (), ())
            with self.assertRaisesRegex(InstallationUpdateBackupError, "could not be verified"):
                backup(plan)
