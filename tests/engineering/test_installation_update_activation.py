from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_activation import InstallationUpdateActivationError, activate
from engineering_platform.installation_update_plan import prepare
from engineering_platform.operational_installation_record import record


class InstallationUpdateActivationTests(unittest.TestCase):
    def _plan(self, root: Path):
        old = root / "old" / "bin" / "python"; old.parent.mkdir(parents=True); old.write_text("#!\n")
        record(root, installation_id="installation-1", version="2.3.1", channel="stable",
               artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40, interpreter=old,
               roles={"server": "com.engineeringplatform.server"}, desired_state="ACTIVE", observed_state="ACTIVE",
               verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
        wheel = root / "target.whl"; wheel.write_bytes(b"wheel")
        return prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.2",
                       target_digest="sha256:" + hashlib.sha256(b"wheel").hexdigest(), target_source_revision="c" * 40)

    def test_activates_only_exact_target_package_and_service(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); target = Path(temporary) / "new" / "bin" / "python"
            target.parent.mkdir(parents=True); target.write_text("#!\n")
            with patch("engineering_platform.installation_update_activation.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.2", "metadata": "x", "package": "x"}), patch("engineering_platform.installation_update_activation.server_service.replace_runtime", return_value={"state": "replaced"}) as replace:
                replacement = activate(plan, interpreter=target)
            self.assertEqual((replacement["version"], replacement["artifact_digest"], replacement["interpreter"]), ("2.3.2", plan.target_digest, str(target)))
            replace.assert_called_once()

    def test_rejects_a_replacement_with_the_wrong_package_version(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); target = Path(temporary) / "new" / "bin" / "python"
            target.parent.mkdir(parents=True); target.write_text("#!\n")
            with patch("engineering_platform.installation_update_activation.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.3", "metadata": "x", "package": "x"}), self.assertRaisesRegex(InstallationUpdateActivationError, "target EP version"):
                activate(plan, interpreter=target)

