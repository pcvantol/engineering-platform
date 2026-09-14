from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_activation import (
    InstallationUpdateActivationError,
    activate,
    legacy_replacement_record,
    replacement_record,
)
from engineering_platform.installation_update_plan import InstallationUpdatePlan, prepare
from engineering_platform.operational_installation_record import load, record, replace_for_update


class InstallationUpdateActivationTests(unittest.TestCase):
    def test_legacy_baseline_has_no_record_until_it_derives_the_target_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); old, target = root / "old", root / "target"
            old.write_text(""); target.write_text(""); old.chmod(0o700); target.chmod(0o700)
            plan = InstallationUpdatePlan("legacy-update-0001", "installation-1", str(root.resolve()), "2.3.1", "sha256:" + "a" * 64,
                "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "target.whl"), (), (),
                {"instance_id": "installation-1", "data_root": str(root.resolve()), "service_label": "com.engineeringplatform.server",
                 "interpreter": str(old), "version": "2.3.1", "artifact_digest": "sha256:" + "a" * 64, "source_revision": None})
            replacement = legacy_replacement_record(plan, interpreter=target)
            self.assertEqual(replacement["source_revision"], "c" * 40)
            self.assertFalse((root / "operational-installation.json").exists())

    def test_legacy_activation_uses_observed_service_and_target_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); old, target = root / "old", root / "target"
            old.write_text(""); target.write_text(""); old.chmod(0o700); target.chmod(0o700)
            plan = InstallationUpdatePlan("legacy-update-0001", "installation-1", str(root.resolve()), "2.3.1", "sha256:" + "a" * 64,
                "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "target.whl"), (), (),
                {"instance_id": "installation-1", "data_root": str(root.resolve()), "service_label": "service", "interpreter": str(old),
                 "version": "2.3.1", "artifact_digest": "sha256:" + "a" * 64, "source_revision": None})
            with patch("engineering_platform.installation_update_activation.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.2"}), patch("engineering_platform.installation_update_activation.server_service.replace_runtime") as replace:
                activate(plan, interpreter=target)
            self.assertEqual(replace.call_args.kwargs["expected_interpreter"], old)

    def test_legacy_replacement_rejects_missing_or_changed_binding(self) -> None:
        plan = InstallationUpdatePlan("legacy-update-0001", "installation-1", "/root", "2.3.1", "sha256:" + "a" * 64, "2.3.2", "sha256:" + "b" * 64, "c" * 40, "/target.whl", (), (), None)
        with self.assertRaisesRegex(InstallationUpdateActivationError, "baseline is invalid"):
            legacy_replacement_record(plan, interpreter="/target")
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

    def test_retries_only_the_exact_record_cas_replacement_after_a_reboot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            target = root / "new" / "bin" / "python"
            target.parent.mkdir(parents=True); target.write_text("#!\n")
            original = load(root)
            expected = replacement_record(plan, current=original, interpreter=target)
            replace_for_update(
                root,
                expected_version=plan.current_version,
                expected_artifact_digest=plan.current_digest,
                replacement=expected,
            )

            with patch(
                "engineering_platform.installation_update_activation.operational_installation.package_identity",
                return_value={"interpreter": str(target), "version": plan.target_version, "metadata": "x", "package": "x"},
            ), patch(
                "engineering_platform.installation_update_activation.server_service.replace_runtime",
                return_value={"state": "unchanged"},
            ) as replace:
                observed = activate(
                    plan,
                    interpreter=target,
                    pre_activation_record=original,
                )

            self.assertEqual(observed, expected)
            self.assertEqual(replace.call_args.kwargs["expected_interpreter"], target)
            self.assertEqual(replace.call_args.kwargs["interpreter"], target)
