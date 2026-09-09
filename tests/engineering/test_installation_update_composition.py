from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_composition import (
    InstallationUpdateCompositionError,
    InstallationUpdateOperationalActions,
    compose,
    execute,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession
from engineering_platform.installation_update_plan import prepare
from engineering_platform.operational_installation_lock import (
    OperationalInstallationLock,
    OperationalInstallationLockError,
)
from engineering_platform.operational_installation_record import load, record


class InstallationUpdateCompositionTests(unittest.TestCase):
    def _plan(self, root: Path):
        current = root / "current runtime" / "bin" / "python"
        current.parent.mkdir(parents=True); current.write_text("#!\n"); current.chmod(0o700)
        record(root, installation_id="installation-1", version="2.3.1", channel="stable",
               artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40, interpreter=current,
               roles={"server": "com.engineeringplatform.server"}, desired_state="ACTIVE", observed_state="ACTIVE",
               verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
        artifact = root / "exact target.whl"; artifact.write_bytes(b"exact wheel")
        return prepare(root, operation_id="update-0001", artifact=artifact, target_version="2.3.2",
                       target_digest="sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
                       target_source_revision="c" * 40)

    @staticmethod
    def _target(root: Path) -> Path:
        target = root / "target runtime with spaces" / "bin" / "python"
        target.parent.mkdir(parents=True); target.write_text("#!\n"); target.chmod(0o700)
        return target

    @staticmethod
    def _replacement(plan, target: Path) -> dict[str, object]:
        return {
            "schema_version": 1, "installation_id": plan.installation_id,
            "version": plan.target_version, "channel": "stable",
            "artifact_digest": plan.target_digest, "source_revision": plan.target_source_revision,
            "interpreter": str(target), "roles": {"server": "com.engineeringplatform.server"},
            "desired_state": "ACTIVE", "observed_state": "ACTIVE",
            "verification": {"result": "PASS"}, "cleanup": {"result": "PENDING"},
        }

    def test_wires_ep_primitives_in_order_under_the_existing_installation_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); plan, target = self._plan(root), self._target(root)
            calls: list[str] = []
            migration_runner = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", "")
            activation_home = root / "Launch Agents Home"

            def inventory(_plan):
                calls.append("inventory")
                contender = OperationalInstallationLock(root)
                with self.assertRaises(OperationalInstallationLockError):
                    contender.acquire("contender-0001")
                return {"result": "PASS"}

            def step(name: str):
                def action(_plan):
                    calls.append(name)
                    return {"result": "PASS", "step": name}
                return action

            def backup(bound_plan):
                calls.append("backup")
                self.assertIs(bound_plan, plan)
                return {"retention": "UPDATE_RECOVERY", "integrity": "PASS"}

            def migrate(bound_plan, *, interpreter, runner):
                calls.append("migrate")
                self.assertIs(bound_plan, plan); self.assertEqual(interpreter, target)
                self.assertIs(runner, migration_runner)
                return {"interpreter": str(target), "integrity": "PASS", "schema_version": 56}

            def activate(bound_plan, *, interpreter, home, runner):
                calls.append("activate")
                self.assertIs(bound_plan, plan); self.assertEqual(interpreter, target)
                self.assertEqual(home, activation_home); self.assertIs(runner, migration_runner)
                return self._replacement(bound_plan, target)

            with patch("engineering_platform.installation_update_composition.operational_installation.package_identity", return_value={"interpreter": str(target), "version": plan.target_version, "metadata": "metadata", "package": "package"}), patch("engineering_platform.installation_update_composition.installation_update_backup.backup", side_effect=backup), patch("engineering_platform.installation_update_composition.installation_update_migration.migrate", side_effect=migrate), patch("engineering_platform.installation_update_composition.installation_update_activation.activate", side_effect=activate):
                result = execute(
                    plan, target_interpreter=target,
                    actions=InstallationUpdateOperationalActions(inventory, step("quiesce"), step("verify")),
                    migration_runner=migration_runner, activation_home=activation_home,
                    activation_runner=migration_runner,
                )

            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(calls, ["inventory", "quiesce", "backup", "migrate", "activate", "verify"])
            self.assertEqual(load(root)["version"], plan.target_version)

    def test_refuses_missing_or_relative_target_before_any_service_action(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); plan = self._plan(root); calls: list[str] = []
            actions = InstallationUpdateOperationalActions(
                lambda _plan: calls.append("inventory") or {"result": "PASS"},
                lambda _plan: calls.append("quiesce") or {"result": "PASS"},
                lambda _plan: calls.append("verify") or {"result": "PASS"},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "absolute path"):
                execute(plan, target_interpreter="relative/python", actions=actions)
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "unavailable"):
                execute(plan, target_interpreter=root / "missing" / "python", actions=actions)
            self.assertEqual(calls, [])

    def test_refuses_changed_record_wrong_target_package_and_empty_inventory_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); plan, target = self._plan(root), self._target(root)
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {}, lambda _plan: {"result": "PASS"}, lambda _plan: {"result": "PASS"},
            )
            current = {"installation_id": "another-installation", "version": plan.current_version,
                       "artifact_digest": plan.current_digest}
            with patch("engineering_platform.installation_update_composition.operational_installation_record.load", return_value=current):
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "changed before update"):
                    execute(plan, target_interpreter=target, actions=actions)

            with patch("engineering_platform.installation_update_composition.operational_installation.package_identity", return_value={"interpreter": str(target), "version": "2.3.3", "metadata": "metadata", "package": "package"}):
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "planned EP package"):
                    execute(plan, target_interpreter=target, actions=actions)

            with patch("engineering_platform.installation_update_composition.operational_installation.package_identity", return_value={"interpreter": str(target), "version": plan.target_version, "metadata": "metadata", "package": "package"}):
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "inventory evidence"):
                    execute(plan, target_interpreter=target, actions=actions)

    def test_recovery_cannot_substitute_a_different_target_interpreter(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); plan, original = self._plan(root), self._target(root)
            replacement = root / "other runtime" / "bin" / "python"
            replacement.parent.mkdir(parents=True); replacement.write_text("#!\n"); replacement.chmod(0o700)
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {"result": "PASS"}, lambda _plan: {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
            )
            with patch("engineering_platform.installation_update_composition.operational_installation.package_identity", return_value={"interpreter": str(original), "version": plan.target_version, "metadata": "metadata", "package": "package"}):
                composed = compose(plan, target_interpreter=original, actions=actions)
                with InstallationUpdateSession(plan) as session:
                    session.advance("INVENTORIED", composed.inventory(plan))
                    session.advance("QUIESCED", {"result": "PASS"})
                    session.advance("BACKED_UP", {"result": "PASS"})
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "changed during recovery"):
                execute(plan, target_interpreter=replacement, actions=actions)
