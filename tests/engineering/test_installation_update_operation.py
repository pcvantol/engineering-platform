from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_plan import InstallationUpdatePlan
from engineering_platform.installation_update_operation import InstallationUpdateOperationError, InstallationUpdateSession, cleanup, create, status, transition


def plan(root: Path) -> InstallationUpdatePlan:
    root = root.resolve()
    cleanup = tuple(str(root / "operations" / "update-0001" / name) for name in ("build", "download", "pip-cache"))
    return InstallationUpdatePlan("update-0001", "installation-1", str(root), "2.3.1", "sha256:" + "a" * 64,
        "2.3.2", "sha256:" + "b" * 64, "c" * 40, str(root / "exact.whl"), cleanup, ("INSTALLATION_LOCK",))


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

    def test_session_serializes_concurrent_installers_and_reopens_the_same_journal(self):
        with TemporaryDirectory() as temporary:
            update = plan(Path(temporary))
            with InstallationUpdateSession(update) as owner:
                owner.advance("INVENTORIED", {"inventory": "PASS"})
                with self.assertRaisesRegex(ValueError, "another operational"):
                    with InstallationUpdateSession(update):
                        pass
            with InstallationUpdateSession(update) as resumed:
                self.assertEqual(resumed.advance("QUIESCED", {"service": "STOPPED"})["state"], "QUIESCED")

    def test_cleanup_is_operation_scoped_and_symlink_failure_is_visible(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "data root with spaces"; update = plan(root)
            with InstallationUpdateSession(update) as session:
                for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED"):
                    session.advance(state, {})
                for target in update.cleanup_targets:
                    path = Path(target); path.mkdir(parents=True); (path / "temporary").write_text("x")
                self.assertEqual(session.cleanup()["state"], "COMPLETE")
                self.assertTrue(all(not Path(target).exists() for target in update.cleanup_targets))
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"; update = plan(root); outside = Path(temporary) / "outside"; outside.mkdir()
            with InstallationUpdateSession(update) as session:
                for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED"):
                    session.advance(state, {})
                target = Path(update.cleanup_targets[0]); target.parent.mkdir(parents=True, exist_ok=True); target.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(InstallationUpdateOperationError, "pending"):
                    session.cleanup()
                self.assertEqual(create(update)["state"], "CLEANUP_PENDING")
                self.assertTrue(outside.exists())

    def test_status_exposes_identity_and_pending_cleanup_without_mutation(self):
        with TemporaryDirectory() as temporary:
            update = plan(Path(temporary)); create(update)
            for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED", "CLEANUP_PENDING"):
                transition(update, state, {})
            observed = status(Path(temporary), "update-0001")
            self.assertEqual((observed["state"], observed["target_version"], observed["target_digest"]), ("CLEANUP_PENDING", "2.3.2", "sha256:" + "b" * 64))

    def test_status_and_transition_reject_malformed_or_conflicting_recovery_evidence(self):
        with TemporaryDirectory() as temporary:
            root, update = Path(temporary), plan(Path(temporary))
            with self.assertRaisesRegex(InstallationUpdateOperationError, "ID is invalid"):
                status(root, "../not-an-operation")
            with self.assertRaisesRegex(InstallationUpdateOperationError, "unreadable"):
                status(root, "update-0001")
            create(update)
            self.assertEqual(transition(update, "PREPARED", {})["state"], "PREPARED")
            with self.assertRaisesRegex(InstallationUpdateOperationError, "conflicts"):
                transition(update, "PREPARED", {"changed": True})
            with self.assertRaisesRegex(InstallationUpdateOperationError, "evidence is invalid"):
                transition(update, "INVENTORIED", ["not-a-mapping"])  # type: ignore[arg-type]
            operation = root / "operations" / "update-0001" / "operation.json"
            operation.write_text("{}")
            with self.assertRaisesRegex(InstallationUpdateOperationError, "invalid"):
                create(update)

    def test_cleanup_rejects_preverification_and_non_operation_targets(self):
        with TemporaryDirectory() as temporary:
            update = plan(Path(temporary)); create(update)
            with self.assertRaisesRegex(InstallationUpdateOperationError, "requires verified"):
                cleanup(update)
            for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED", "ACTIVATED", "VERIFIED"):
                transition(update, state, {})
            invalid = InstallationUpdatePlan(**{**update.payload(), "cleanup_targets": (str(Path(temporary) / "outside"),)})
            with self.assertRaisesRegex(InstallationUpdateOperationError, "exact plan"):
                cleanup(invalid)

    def test_session_refuses_actions_before_lock_ownership(self):
        with TemporaryDirectory() as temporary:
            session = InstallationUpdateSession(plan(Path(temporary)))
            with self.assertRaisesRegex(InstallationUpdateOperationError, "does not own"):
                session.advance("INVENTORIED", {})
            with self.assertRaisesRegex(InstallationUpdateOperationError, "does not own"):
                session.cleanup()
