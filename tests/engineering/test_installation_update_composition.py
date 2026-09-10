from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Mapping
import unittest
from unittest.mock import patch

from engineering_platform.installation_update_activation import replacement_record
from engineering_platform.installation_update_admission import ExecutionAdmission, admit
from engineering_platform.installation_update_composition import (
    InstallationUpdateCompositionError,
    InstallationUpdateOperationalActions,
    compose,
    execute,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession, status
from engineering_platform.installation_update_plan import prepare
from engineering_platform.installation_update_preparation import prepare_candidate, staged_execution_plan
from engineering_platform.operational_installation_lock import (
    OperationalInstallationLock,
    OperationalInstallationLockError,
)
from engineering_platform.operational_installation_record import load, record, replace_for_update


class CandidateRunner:
    """Deterministic operation-owned venv/package fixture."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.installed = False

    def __call__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(command)
        if command[2:4] == ("-m", "venv"):
            interpreter = Path(command[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!\n", encoding="utf-8")
            interpreter.chmod(0o700)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2:4] == ("-m", "pip"):
            candidate = Path(command[0]).parent.parent
            site = candidate / "lib" / "python3.12" / "site-packages"
            (site / "engineering_platform").mkdir(parents=True, exist_ok=True)
            (site / f"engineering_platform-{self.version}.dist-info").mkdir(exist_ok=True)
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ("-I", "-c"):
            if not self.installed:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            candidate = Path(command[0]).parent.parent
            site = candidate / "lib" / "python3.12" / "site-packages"
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "interpreter": command[0],
                "version": self.version,
                "metadata": str(site / f"engineering_platform-{self.version}.dist-info"),
                "package": str(site / "engineering_platform"),
            }), "")
        raise AssertionError(command)


class InstallationUpdateCompositionTests(unittest.TestCase):
    def _plan(self, root: Path):
        current = root / "current runtime" / "bin" / "python"
        current.parent.mkdir(parents=True)
        current.write_text("#!\n")
        current.chmod(0o700)
        record(
            root,
            installation_id="installation-1",
            version="2.3.1",
            channel="stable",
            artifact_digest="sha256:" + "a" * 64,
            source_revision="b" * 40,
            interpreter=current,
            roles={"server": "com.engineeringplatform.server"},
            desired_state="ACTIVE",
            observed_state="ACTIVE",
            verification={"result": "PASS"},
            cleanup={"result": "COMPLETE"},
        )
        artifact = root / "exact target.whl"
        artifact.write_bytes(b"exact wheel")
        return prepare(
            root,
            operation_id="update-0001",
            artifact=artifact,
            target_version="2.3.2",
            target_digest="sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
            target_source_revision="c" * 40,
        )

    def _admitted(self, root: Path):
        source = self._plan(root)
        builder = root / "candidate builder"
        builder.write_text("#!\n", encoding="utf-8")
        builder.chmod(0o700)
        runner = CandidateRunner(source.target_version)
        candidate = prepare_candidate(source, venv_builder=builder, runner=runner)
        plan = staged_execution_plan(source, candidate=candidate, runner=runner)
        with InstallationUpdateSession(plan) as session:
            session.bind_prepared_candidate(candidate, runner=runner)
        return plan, candidate, admit(plan, runner=runner), runner

    @staticmethod
    def _replacement(plan, target: Path, *, original: Mapping[str, object] | None = None) -> dict[str, object]:
        return replacement_record(
            plan,
            current=load(Path(plan.data_root)) if original is None else original,
            interpreter=target,
        )

    @staticmethod
    def _legacy_admission(root: Path, plan, admission: ExecutionAdmission) -> ExecutionAdmission:
        """Downgrade persisted evidence solely to exercise the v1 boundary."""
        journal = root / "operations" / plan.operation_id / "operation.json"
        payload = json.loads(journal.read_text(encoding="utf-8"))
        legacy = json.loads(json.dumps(admission.payload()))
        legacy["schema_version"] = 1
        legacy["registered_installation"].pop("record")
        payload["execution_admission"] = legacy
        payload["execution_admission_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        journal.write_text(json.dumps(payload), encoding="utf-8")
        return ExecutionAdmission(
            schema_version=1,
            operation_id=admission.operation_id,
            plan_digest=admission.plan_digest,
            installation_id=admission.installation_id,
            registered_installation=legacy["registered_installation"],
            prepared_candidate=admission.prepared_candidate,
        )

    def test_wires_ep_primitives_to_the_admitted_candidate_under_the_existing_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, candidate, admission, migration_runner = self._admitted(root)
            target = Path(candidate.interpreter)
            calls: list[str] = []
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
                self.assertIs(bound_plan, plan)
                self.assertEqual(interpreter, target)
                self.assertIs(runner, migration_runner)
                return {"interpreter": str(target), "integrity": "PASS", "schema_version": 56}

            def activate(bound_plan, *, interpreter, pre_activation_record, home, runner):
                calls.append("activate")
                self.assertIs(bound_plan, plan)
                self.assertEqual(interpreter, target)
                self.assertEqual(pre_activation_record, admission.registered_installation["record"])
                self.assertEqual(home, activation_home)
                self.assertIs(runner, migration_runner)
                return self._replacement(bound_plan, target, original=pre_activation_record)

            with patch(
                "engineering_platform.installation_update_composition.installation_update_backup.backup",
                side_effect=backup,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_migration.migrate",
                side_effect=migrate,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_activation.activate",
                side_effect=activate,
            ):
                result = execute(
                    plan,
                    admission=admission,
                    actions=InstallationUpdateOperationalActions(inventory, step("quiesce"), step("verify")),
                    migration_runner=migration_runner,
                    activation_home=activation_home,
                    activation_runner=migration_runner,
                )

            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(calls, ["inventory", "quiesce", "backup", "migrate", "activate", "verify"])
            self.assertEqual(load(root)["version"], plan.target_version)
            self.assertEqual(load(root)["interpreter"], str(target))

    def test_crash_after_record_cas_before_activated_journal_resumes_exactly_once(self) -> None:
        """Exercise the real executor gap: CAS succeeds, journal write crashes."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, candidate, admission, candidate_runner = self._admitted(root)
            target = Path(candidate.interpreter)
            original = dict(admission.registered_installation["record"])
            first_calls: list[str] = []

            def action(name: str, calls: list[str]):
                def invoke(_plan):
                    calls.append(name)
                    return {"result": "PASS", "step": name}
                return invoke

            def backup(_plan):
                first_calls.append("backup")
                return {"result": "PASS", "retention": "UPDATE_RECOVERY"}

            def migrate(_plan, *, interpreter, runner):
                first_calls.append("migrate")
                self.assertEqual(interpreter, target)
                self.assertIs(runner, candidate_runner)
                return {"result": "PASS", "interpreter": str(interpreter)}

            def activate(_plan, *, interpreter, pre_activation_record, home, runner):
                first_calls.append("activate")
                self.assertEqual(interpreter, target)
                self.assertEqual(pre_activation_record, original)
                return self._replacement(plan, target, original=pre_activation_record)

            operational = InstallationUpdateOperationalActions(
                action("inventory", first_calls), action("quiesce", first_calls), action("verify", first_calls),
            )
            original_advance = InstallationUpdateSession.advance

            def crash_after_cas(session, state, evidence):
                if state == "ACTIVATED":
                    raise RuntimeError("simulated reboot after record CAS")
                return original_advance(session, state, evidence)

            with patch(
                "engineering_platform.installation_update_composition.installation_update_backup.backup",
                side_effect=backup,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_migration.migrate",
                side_effect=migrate,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_activation.activate",
                side_effect=activate,
            ), patch.object(InstallationUpdateSession, "advance", new=crash_after_cas):
                with self.assertRaisesRegex(RuntimeError, "after record CAS"):
                    execute(plan, admission=admission, actions=operational, migration_runner=candidate_runner)

            self.assertEqual(status(root, plan.operation_id)["state"], "MIGRATED")
            self.assertEqual(load(root), self._replacement(plan, target, original=original))
            self.assertEqual(first_calls, ["inventory", "quiesce", "backup", "migrate", "activate"])

            resumed_calls: list[str] = []

            def resumed_backup(_plan):
                resumed_calls.append("backup")
                return {"result": "PASS"}

            def resumed_migrate(_plan, *, interpreter, runner):
                resumed_calls.append("migrate")
                return {"result": "PASS"}

            def resumed_activate(_plan, *, interpreter, pre_activation_record, home, runner):
                resumed_calls.append("activate")
                self.assertEqual(interpreter, target)
                self.assertEqual(pre_activation_record, original)
                return self._replacement(plan, target, original=pre_activation_record)

            resumed = InstallationUpdateOperationalActions(
                action("inventory", resumed_calls), action("quiesce", resumed_calls), action("verify", resumed_calls),
            )
            with patch(
                "engineering_platform.installation_update_composition.installation_update_backup.backup",
                side_effect=resumed_backup,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_migration.migrate",
                side_effect=resumed_migrate,
            ), patch(
                "engineering_platform.installation_update_composition.installation_update_activation.activate",
                side_effect=resumed_activate,
            ):
                result = execute(plan, admission=admission, actions=resumed, migration_runner=candidate_runner)

            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(resumed_calls, ["activate", "verify"])

    def test_rejects_every_noncanonical_target_record_during_migrated_recovery(self) -> None:
        changes = (
            ("roles", {"server": "com.engineeringplatform.other"}),
            ("channel", "preview"),
            ("desired_state", "STOPPED"),
            ("observed_state", "ACTIVE"),
            ("verification", {"result": "PASS"}),
            ("cleanup", {"result": "COMPLETE"}),
        )
        for field, value in changes:
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan, candidate, admission, runner = self._admitted(root)
                original = dict(admission.registered_installation["record"])
                target = Path(candidate.interpreter)
                with InstallationUpdateSession(plan) as session:
                    for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED"):
                        session.advance(state, {"result": "PASS", "step": state})
                altered = self._replacement(plan, target, original=original)
                altered[field] = value
                replace_for_update(
                    root,
                    expected_version=plan.current_version,
                    expected_artifact_digest=plan.current_digest,
                    replacement=altered,
                )
                calls: list[str] = []
                actions = InstallationUpdateOperationalActions(
                    lambda _plan: calls.append("inventory") or {"result": "PASS"},
                    lambda _plan: calls.append("quiesce") or {"result": "PASS"},
                    lambda _plan: calls.append("verify") or {"result": "PASS"},
                )
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                    compose(plan, admission=admission, actions=actions, migration_runner=runner)
                self.assertEqual(calls, [])

    def test_legacy_v1_admission_never_reaches_mutating_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, candidate, admission, runner = self._admitted(root)
            original = dict(admission.registered_installation["record"])
            with InstallationUpdateSession(plan) as session:
                for state in ("INVENTORIED", "QUIESCED", "BACKED_UP", "MIGRATED"):
                    session.advance(state, {"result": "PASS", "step": state})
            replace_for_update(
                root,
                expected_version=plan.current_version,
                expected_artifact_digest=plan.current_digest,
                replacement=self._replacement(plan, Path(candidate.interpreter), original=original),
            )
            legacy = self._legacy_admission(root, plan, admission)
            calls: list[str] = []
            actions = InstallationUpdateOperationalActions(
                lambda _plan: calls.append("inventory") or {"result": "PASS"},
                lambda _plan: calls.append("quiesce") or {"result": "PASS"},
                lambda _plan: calls.append("verify") or {"result": "PASS"},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "v2 execution admission"):
                execute(plan, admission=legacy, actions=actions, migration_runner=runner)
            self.assertEqual(calls, [])

    def test_refuses_missing_or_forged_admission_before_any_service_action(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            calls: list[str] = []
            actions = InstallationUpdateOperationalActions(
                lambda _plan: calls.append("inventory") or {"result": "PASS"},
                lambda _plan: calls.append("quiesce") or {"result": "PASS"},
                lambda _plan: calls.append("verify") or {"result": "PASS"},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                execute(plan, admission=None, actions=actions)  # type: ignore[arg-type]

            plan, candidate, admission, runner = self._admitted(root / "admitted")
            forged = replace(
                admission,
                prepared_candidate={**candidate.payload(), "interpreter": str(root / "foreign" / "python")},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                execute(plan, admission=forged, actions=actions, migration_runner=runner)
            self.assertEqual(calls, [])

    def test_refuses_changed_record_changed_candidate_and_empty_inventory_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, _candidate, admission, runner = self._admitted(root)
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {},
                lambda _plan: {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
            )
            current = {
                "installation_id": "another-installation",
                "version": plan.current_version,
                "artifact_digest": plan.current_digest,
            }
            with patch(
                "engineering_platform.installation_update_admission.operational_installation_record.load",
                return_value=current,
            ):
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                    execute(plan, admission=admission, actions=actions, migration_runner=runner)

            runner.version = "2.3.3"
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                execute(plan, admission=admission, actions=actions, migration_runner=runner)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, _candidate, admission, runner = self._admitted(root)
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {},
                lambda _plan: {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "inventory evidence"):
                execute(plan, admission=admission, actions=actions, migration_runner=runner)

    def test_recovery_cannot_substitute_a_different_candidate_in_the_admission(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, candidate, admission, runner = self._admitted(root)
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
            )
            composed = compose(plan, admission=admission, actions=actions, migration_runner=runner)
            with InstallationUpdateSession(plan) as session:
                session.advance("INVENTORIED", composed.inventory(plan))
                session.advance("QUIESCED", {"result": "PASS"})
                session.advance("BACKED_UP", {"result": "PASS"})

            replacement = root / "other runtime" / "bin" / "python"
            replacement.parent.mkdir(parents=True)
            replacement.write_text("#!\n")
            replacement.chmod(0o700)
            forged = replace(
                admission,
                prepared_candidate={**candidate.payload(), "interpreter": str(replacement)},
            )
            with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                execute(plan, admission=forged, actions=actions, migration_runner=runner)

    def test_tampered_candidate_blocks_quiesce_after_composition_before_service_action(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, _candidate, admission, runner = self._admitted(root)
            calls: list[str] = []
            actions = InstallationUpdateOperationalActions(
                lambda _plan: {"result": "PASS"},
                lambda _plan: calls.append("quiesce") or {"result": "PASS"},
                lambda _plan: {"result": "PASS"},
            )
            composed = compose(plan, admission=admission, actions=actions, migration_runner=runner)
            (root / "operations" / plan.operation_id / "candidate-runtime.json").write_text(
                "{}",
                encoding="utf-8",
            )
            with InstallationUpdateSession(plan):
                with self.assertRaisesRegex(InstallationUpdateCompositionError, "execution admission"):
                    composed.quiesce(plan)
            self.assertEqual(calls, [])
