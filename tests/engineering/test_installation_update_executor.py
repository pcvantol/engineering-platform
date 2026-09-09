from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_executor import (
    InstallationUpdateActions, InstallationUpdateExecutorError, execute,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession
from engineering_platform.installation_update_plan import prepare
from engineering_platform.operational_installation_record import record


class InstallationUpdateExecutorTests(unittest.TestCase):
    def _plan(self, root: Path):
        interpreter = root / "old runtime" / "bin" / "python"
        interpreter.parent.mkdir(parents=True); interpreter.write_text("#!/bin/sh\n"); interpreter.chmod(0o755)
        record(root, installation_id="installation-1", version="2.3.1", channel="stable",
               artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40, interpreter=interpreter,
               roles={"server": "com.engineeringplatform.server"}, desired_state="ACTIVE", observed_state="ACTIVE",
               verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
        artifact = root / "exact.whl"; artifact.write_bytes(b"exact wheel")
        return prepare(root, operation_id="update-0001", artifact=artifact, target_version="2.3.2",
                       target_digest="sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
                       target_source_revision="c" * 40)

    def _actions(self, plan, calls: list[str]) -> InstallationUpdateActions:
        new_interpreter = Path(plan.data_root) / "new runtime" / "bin" / "python"
        def step(name: str):
            def action(_plan): calls.append(name); return {"result": "PASS", "step": name}
            return action
        def activate(_plan):
            calls.append("activate"); new_interpreter.parent.mkdir(parents=True, exist_ok=True); new_interpreter.write_text("#!/bin/sh\n"); new_interpreter.chmod(0o755)
            return {"schema_version": 1, "installation_id": "installation-1", "version": "2.3.2", "channel": "stable",
                    "artifact_digest": plan.target_digest, "source_revision": plan.target_source_revision,
                    "interpreter": str(new_interpreter), "roles": {"server": "com.engineeringplatform.server"},
                    "desired_state": "ACTIVE", "observed_state": "ACTIVE", "verification": {"result": "PASS"}, "cleanup": {"result": "PENDING"}}
        return InstallationUpdateActions(step("inventory"), step("quiesce"), step("backup"), step("migrate"), activate, step("verify"))

    def test_executes_exact_order_replaces_record_and_cleans_only_operation_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); calls: list[str] = []
            for target in plan.cleanup_targets:
                Path(target).mkdir(parents=True); (Path(target) / "temporary").write_text("x")
            result = execute(plan, self._actions(plan, calls))
            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(calls, ["inventory", "quiesce", "backup", "migrate", "activate", "verify"])
            self.assertTrue(all(not Path(target).exists() for target in plan.cleanup_targets))

    def test_resume_skips_durable_pre_activation_steps(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
            with InstallationUpdateSession(plan) as session:
                for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED"):
                    session.advance(state, {"recovered": state})
            calls: list[str] = []
            self.assertEqual(execute(plan, self._actions(plan, calls))["state"], "COMPLETE")
            self.assertEqual(calls, ["activate", "verify"])

    def test_rejects_activation_that_does_not_bind_the_target_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); calls: list[str] = []
            actions = self._actions(plan, calls)
            def wrong(_plan):
                value = dict(actions.activate(_plan)); value["version"] = "2.3.3"; return value
            with self.assertRaisesRegex(InstallationUpdateExecutorError, "target identity"):
                execute(plan, InstallationUpdateActions(actions.inventory, actions.quiesce, actions.backup, actions.migrate, wrong, actions.verify))

