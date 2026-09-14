from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_executor import (
    InstallationUpdateActions, InstallationUpdateExecutorError, execute,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession
from engineering_platform.installation_update_plan import prepare
from engineering_platform.operational_installation_record import load, record, replace_for_update


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

    def test_reboot_recovery_resumes_after_each_durable_transition(self) -> None:
        expected_calls = {
            "PREPARED": ["inventory", "quiesce", "backup", "migrate", "activate", "verify"],
            "INVENTORIED": ["quiesce", "backup", "migrate", "activate", "verify"],
            "QUIESCED": ["backup", "migrate", "activate", "verify"],
            "BACKED_UP": ["migrate", "activate", "verify"],
            "MIGRATED": ["activate", "verify"],
            "ACTIVATED": ["verify"],
            "VERIFIED": [],
        }
        for recovered_state, expected in expected_calls.items():
            with self.subTest(recovered_state=recovered_state), TemporaryDirectory() as temporary:
                plan = self._plan(Path(temporary)); staging_calls: list[str] = []
                actions = self._actions(plan, staging_calls)
                with InstallationUpdateSession(plan) as session:
                    current = "PREPARED"
                    for state, action in (("INVENTORIED", actions.inventory), ("QUIESCED", actions.quiesce),
                                          ("BACKED_UP", actions.backup), ("MIGRATED", actions.migrate)):
                        if recovered_state == current:
                            break
                        session.advance(state, action(plan))
                        current = state
                    if recovered_state in {"ACTIVATED", "VERIFIED"}:
                        replacement = actions.activate(plan)
                        replace_for_update(
                            Path(plan.data_root), expected_version=plan.current_version,
                            expected_artifact_digest=plan.current_digest, replacement=replacement,
                        )
                        session.advance("ACTIVATED", replacement)
                    if recovered_state == "VERIFIED":
                        session.advance("VERIFIED", actions.verify(plan))
                calls: list[str] = []
                self.assertEqual(execute(plan, self._actions(plan, calls))["state"], "COMPLETE")
                self.assertEqual(calls, expected)

    def test_rejects_activation_that_does_not_bind_the_target_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); calls: list[str] = []
            actions = self._actions(plan, calls)
            def wrong(_plan):
                value = dict(actions.activate(_plan)); value["version"] = "2.3.3"; return value
            with self.assertRaisesRegex(InstallationUpdateExecutorError, "target identity"):
                execute(plan, InstallationUpdateActions(actions.inventory, actions.quiesce, actions.backup, actions.migrate, wrong, actions.verify))

    def test_changed_wheel_fails_before_inventory_or_service_quiescence(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); calls: list[str] = []
            Path(plan.artifact).write_bytes(b"different bytes")
            with self.assertRaisesRegex(InstallationUpdateExecutorError, "exact artifact"):
                execute(plan, self._actions(plan, calls))
            self.assertEqual(calls, [])

    def test_cleanup_pending_resumes_without_repeating_runtime_actions(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); first_calls: list[str] = []
            for target in plan.cleanup_targets:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            # A symlinked operation cache is deliberately retained and makes
            # the first cleanup fail closed after the runtime is verified.
            outside = Path(temporary) / "outside"; outside.mkdir()
            Path(plan.cleanup_targets[0]).symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(InstallationUpdateExecutorError, "execution failed"):
                execute(plan, self._actions(plan, first_calls))
            self.assertEqual(first_calls, ["inventory", "quiesce", "backup", "migrate", "activate", "verify"])
            Path(plan.cleanup_targets[0]).unlink()
            resumed_calls: list[str] = []
            self.assertEqual(execute(plan, self._actions(plan, resumed_calls))["state"], "COMPLETE")
            self.assertEqual(resumed_calls, [])

    def test_crash_during_migration_keeps_last_durable_transition_for_reboot_resume(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary)); failed_calls: list[str] = []
            actions = self._actions(plan, failed_calls)

            def crash(_plan):
                failed_calls.append("migrate")
                raise RuntimeError("simulated reboot")

            with self.assertRaisesRegex(RuntimeError, "simulated reboot"):
                execute(plan, InstallationUpdateActions(
                    actions.inventory, actions.quiesce, actions.backup, crash,
                    actions.activate, actions.verify,
                ))
            from engineering_platform.installation_update_operation import status
            self.assertEqual(status(Path(plan.data_root), plan.operation_id)["state"], "BACKED_UP")
            self.assertEqual(failed_calls, ["inventory", "quiesce", "backup", "migrate"])
            resumed_calls: list[str] = []
            self.assertEqual(execute(plan, self._actions(plan, resumed_calls))["state"], "COMPLETE")
            self.assertEqual(resumed_calls, ["migrate", "activate", "verify"])

    def test_quiesce_crash_resumes_from_durable_intent_without_repeating_preflight(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
            calls: list[str] = []
            actions = self._actions(plan, calls)

            def preflight(_plan):
                calls.append("quiesce-preflight")
                return {"result": "PASS", "service": "LOADED_AND_BOUND"}

            def crash_after_quiesce(_plan):
                calls.append("quiesce")
                raise RuntimeError("lost quiesce acknowledgement")

            guarded = InstallationUpdateActions(
                actions.inventory, crash_after_quiesce, actions.backup,
                actions.migrate, actions.activate, actions.verify,
                quiesce_preflight=preflight,
            )
            with self.assertRaisesRegex(RuntimeError, "lost quiesce acknowledgement"):
                execute(plan, guarded)

            from engineering_platform.installation_update_operation import status
            self.assertEqual(status(Path(plan.data_root), plan.operation_id)["state"], "QUIESCING")
            self.assertEqual(calls, ["inventory", "quiesce-preflight", "quiesce"])

            resumed_calls: list[str] = []
            resumed = self._actions(plan, resumed_calls)
            self.assertEqual(execute(plan, resumed)["state"], "COMPLETE")
            self.assertEqual(
                resumed_calls,
                ["quiesce", "backup", "migrate", "activate", "verify"],
            )

    def test_same_release_record_does_not_skip_a_distinct_expected_activation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            current = load(root)
            same_artifact = Path(plan.artifact)
            same_artifact.write_bytes(b"original wheel")
            same_digest = "sha256:" + hashlib.sha256(same_artifact.read_bytes()).hexdigest()
            current = replace_for_update(
                root,
                expected_version=plan.current_version,
                expected_artifact_digest=plan.current_digest,
                replacement={**current, "artifact_digest": same_digest},
            )
            same_plan = prepare(
                root, operation_id="update-0002", artifact=same_artifact,
                target_version=plan.current_version, target_digest=same_digest,
                target_source_revision=str(current["source_revision"]),
            )
            with InstallationUpdateSession(same_plan) as session:
                for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED"):
                    session.advance(state, {"result": "PASS", "step": state})
            calls: list[str] = []
            target = root / "same release target" / "bin" / "python"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)
            replacement = {
                **current,
                "interpreter": str(target),
                "observed_state": "ACTIVATING",
                "verification": {"result": "PENDING"},
                "cleanup": {"result": "PENDING"},
            }

            def step(name: str):
                def action(_plan):
                    calls.append(name)
                    return {"result": "PASS", "step": name}
                return action

            def activate(_plan):
                calls.append("activate")
                return replacement

            guarded = InstallationUpdateActions(
                step("inventory"), step("quiesce"), step("backup"), step("migrate"),
                activate, step("verify"), replacement,
            )
            self.assertEqual(execute(same_plan, guarded)["state"], "COMPLETE")
            self.assertEqual(calls, ["activate", "verify"])

    def test_six_callback_cas_gap_retries_idempotent_activation(self) -> None:
        with TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
            calls: list[str] = []
            actions = self._actions(plan, calls)
            original_advance = InstallationUpdateSession.advance

            def lose_activated_event(session, state, evidence):
                if state == "ACTIVATED":
                    raise RuntimeError("lost ACTIVATED event")
                return original_advance(session, state, evidence)

            with patch.object(
                InstallationUpdateSession, "advance", new=lose_activated_event,
            ), self.assertRaisesRegex(RuntimeError, "lost ACTIVATED event"):
                execute(plan, actions)
            calls.clear()
            self.assertEqual(execute(plan, self._actions(plan, calls))["state"], "COMPLETE")
            self.assertEqual(calls, ["activate", "verify"])
