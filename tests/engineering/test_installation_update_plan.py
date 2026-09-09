from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_plan import InstallationUpdatePlanError, prepare
from engineering_platform.operational_installation_record import record


class InstallationUpdatePlanTests(unittest.TestCase):
    def _record(self, root: Path, executable: Path) -> None:
        record(root, installation_id="installation-1", version="2.3.1", channel="stable",
               artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40,
               interpreter=executable, roles={"server": "com.engineeringplatform.server"},
               desired_state="ACTIVE", observed_state="ACTIVE", verification={"result": "PASS"},
               cleanup={"result": "COMPLETE"})

    def test_plan_binds_exact_registered_identity_and_operation_cleanup_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "EP Runtime With Spaces"; executable = root / "venv" / "bin" / "python"
            executable.parent.mkdir(parents=True); executable.write_text("#!/bin/sh\n"); executable.chmod(0o755)
            self._record(root, executable)
            wheel = Path(temporary) / "exact.whl"; wheel.write_bytes(b"exact wheel bytes")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            plan = prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.2",
                           target_digest=digest, target_source_revision="c" * 40)
            self.assertEqual((plan.installation_id, plan.current_version, plan.target_version), ("installation-1", "2.3.1", "2.3.2"))
            self.assertEqual(plan.cleanup_targets, tuple(str(root.resolve() / "operations" / "update-0001" / name) for name in ("build", "download", "pip-cache")))
            self.assertIn("BACKUP_AND_MIGRATION", plan.steps)

    def test_plan_rejects_unregistered_or_non_exact_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; wheel = Path(temporary) / "wrong.whl"; wheel.write_bytes(b"wrong")
            with self.assertRaisesRegex(InstallationUpdatePlanError, "registered"):
                prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.2", target_digest="sha256:" + "a" * 64, target_source_revision="c" * 40)
            executable = root / "python"; root.mkdir(); executable.write_text(""); executable.chmod(0o755); self._record(root, executable)
            with self.assertRaisesRegex(InstallationUpdatePlanError, "does not match"):
                prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.2", target_digest="sha256:" + "a" * 64, target_source_revision="c" * 40)

    def test_plan_refuses_downgrade_without_a_compatible_recovery_operation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; root.mkdir()
            executable = root / "python"; executable.write_text(""); executable.chmod(0o755)
            self._record(root, executable)
            wheel = Path(temporary) / "older.whl"; wheel.write_bytes(b"older wheel")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            with self.assertRaisesRegex(InstallationUpdatePlanError, "downgrade or rollback"):
                prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.0",
                        target_digest=digest, target_source_revision="c" * 40)

    def test_plan_refuses_same_bytes_from_a_different_source_revision(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; root.mkdir()
            executable = root / "python"; executable.write_text(""); executable.chmod(0o755)
            wheel = Path(temporary) / "same.whl"; wheel.write_bytes(b"same qualified bytes")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            record(root, installation_id="installation-1", version="2.3.1", channel="stable",
                   artifact_digest=digest, source_revision="b" * 40, interpreter=executable,
                   roles={"server": "com.engineeringplatform.server"}, desired_state="ACTIVE",
                   observed_state="ACTIVE", verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
            with self.assertRaisesRegex(InstallationUpdatePlanError, "different source revision"):
                prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.1",
                        target_digest=digest, target_source_revision="c" * 40)
