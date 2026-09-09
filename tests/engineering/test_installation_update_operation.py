from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_plan import InstallationUpdatePlan
from engineering_platform.installation_update_operation import InstallationUpdateOperationError, create, transition


def plan(root: Path) -> InstallationUpdatePlan:
    return InstallationUpdatePlan("update-0001", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64,
        "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), (), ("INSTALLATION_LOCK",))


class InstallationUpdateOperationTests(unittest.TestCase):
    def test_ordered_resume_and_cleanup_recovery_are_durable(self):
        with TemporaryDirectory() as temporary:
            update = plan(Path(temporary))
            self.assertEqual(create(update)["state"], "PREPARED")
            for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE"):
                self.assertEqual(transition(update, state, {"step": state})["state"], state)
            self.assertEqual(create(update)["state"], "COMPLETE")

    def test_rejects_concurrent_plan_or_out_of_order_transition(self):
        with TemporaryDirectory() as temporary:
            update = plan(Path(temporary)); create(update)
            with self.assertRaisesRegex(InstallationUpdateOperationError, "out of order"):
                transition(update, "ACTIVATED", {})
            different = InstallationUpdatePlan(**{**update.payload(), "target_version": "2.3.3"})
            with self.assertRaisesRegex(InstallationUpdateOperationError, "exact plan"):
                create(different)
