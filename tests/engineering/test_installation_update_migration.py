from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_migration import InstallationUpdateMigrationError, migrate
from engineering_platform.installation_update_plan import InstallationUpdatePlan
from engineering_platform.operational_installation import OperationalInstallationError


class InstallationUpdateMigrationTests(unittest.TestCase):
    def _plan(self, root: Path) -> InstallationUpdatePlan:
        return InstallationUpdatePlan("update-0001", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64,
                                      "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), (), ())

    def test_runs_only_the_exact_target_interpreter_and_returns_schema_evidence(self):
        with TemporaryDirectory() as temporary:
            root, target = Path(temporary), Path(temporary) / "target python"
            target.touch(mode=0o700)
            observed = []

            def runner(command, **_kwargs):
                observed.append(command)
                return subprocess.CompletedProcess(command, 0, '{"instance_id":"installation-1","integrity":"PASS","interpreter":"' + str(target) + '","schema_version":56}\n', "")

            with patch("engineering_platform.installation_update_migration.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.2", "metadata": "metadata", "package": "package"}):
                evidence = migrate(self._plan(root), interpreter=target, runner=runner)
            self.assertEqual(evidence, {"interpreter": str(target), "instance_id": "installation-1", "schema_version": 56, "integrity": "PASS", "package_version": "2.3.2"})
            self.assertEqual(observed[0][:3], (str(target), "-I", "-c"))
            self.assertEqual(observed[0][-1], str(root.resolve()))

    def test_rejects_a_target_version_or_instance_mismatch(self):
        with TemporaryDirectory() as temporary:
            root, target = Path(temporary), Path(temporary) / "target"
            target.touch(mode=0o700)
            plan = self._plan(root)
            with patch("engineering_platform.installation_update_migration.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.1", "metadata": "metadata", "package": "package"}):
                with self.assertRaisesRegex(InstallationUpdateMigrationError, "planned EP version"):
                    migrate(plan, interpreter=target)

    def test_fails_closed_when_target_identity_or_migration_fails(self):
        with TemporaryDirectory() as temporary:
            root, target = Path(temporary), Path(temporary) / "target"
            target.touch(mode=0o700)
            plan = self._plan(root)
            with patch("engineering_platform.installation_update_migration.operational_installation.package_identity", side_effect=OperationalInstallationError("absent")):
                with self.assertRaisesRegex(InstallationUpdateMigrationError, "interpreter is unavailable"):
                    migrate(plan, interpreter=target)
            identity = {"interpreter": str(target), "version": "2.3.2", "metadata": "metadata", "package": "package"}
            with patch("engineering_platform.installation_update_migration.operational_installation.package_identity", return_value=identity):
                def failed(command, **_kwargs):
                    return subprocess.CompletedProcess(command, 1, "", "migration error")
                with self.assertRaisesRegex(InstallationUpdateMigrationError, "migration failed"):
                    migrate(plan, interpreter=target, runner=failed)

    def test_rejects_invalid_or_wrong_instance_migration_evidence(self):
        with TemporaryDirectory() as temporary:
            root, target = Path(temporary), Path(temporary) / "target"
            target.touch(mode=0o700)
            identity = {"interpreter": str(target), "version": "2.3.2", "metadata": "metadata", "package": "package"}
            with patch("engineering_platform.installation_update_migration.operational_installation.package_identity", return_value=identity):
                def malformed(command, **_kwargs):
                    return subprocess.CompletedProcess(command, 0, "not json", "")
                with self.assertRaisesRegex(InstallationUpdateMigrationError, "invalid evidence"):
                    migrate(self._plan(root), interpreter=target, runner=malformed)
                def wrong_instance(command, **_kwargs):
                    return subprocess.CompletedProcess(command, 0, '{"instance_id":"other","integrity":"PASS","interpreter":"' + str(target) + '","schema_version":56}', "")
                with self.assertRaisesRegex(InstallationUpdateMigrationError, "different instance"):
                    migrate(self._plan(root), interpreter=target, runner=wrong_instance)
