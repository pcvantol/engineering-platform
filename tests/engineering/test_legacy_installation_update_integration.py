from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import zipfile

from engineering_platform import (
    installation_update_admission,
    installation_update_composition,
    installation_update_preparation,
    operational_installation,
    server,
    server_service,
)
from engineering_platform.installation_update_admission import admit
from engineering_platform.installation_update_composition import (
    InstallationUpdateOperationalActions,
    execute,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession, reopen_plan, status
from engineering_platform.installation_update_plan import prepare
from engineering_platform.installation_update_preparation import prepare_candidate, staged_execution_plan
from engineering_platform.legacy_installation_adoption import (
    LegacyAdoptionAuthorization,
    adopt,
    inspect,
)
from engineering_platform.operational_installation_record import load


class LegacyAndCandidateRunner:
    """Simulate only the isolated interpreter/process boundary."""

    def __init__(self, *, old_interpreter: Path, old_package: Path,
                 old_version: str, target_version: str, instance_id: str) -> None:
        self.old_interpreter = old_interpreter
        self.old_package = old_package
        self.old_version = old_version
        self.target_version = target_version
        self.instance_id = instance_id
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
            (site / f"engineering_platform-{self.target_version}.dist-info").mkdir(exist_ok=True)
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ("-I", "-c"):
            if Path(command[0]).absolute() == self.old_interpreter.absolute():
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "interpreter": str(self.old_interpreter),
                    "version": self.old_version,
                    "metadata": str(self.old_package.parent / f"engineering_platform-{self.old_version}.dist-info"),
                    "package": str(self.old_package),
                }), "")
            if not self.installed:
                return subprocess.CompletedProcess(command, 1, "", "candidate missing")
            candidate = Path(command[0]).parent.parent
            site = candidate / "lib" / "python3.12" / "site-packages"
            if len(command) > 4:
                # Simulate the isolated target process running initialize(),
                # including its product-version configuration migration.
                configuration_path = Path(command[-1]) / "server.json"
                configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
                configuration["product_version"] = self.target_version
                configuration_path.write_text(json.dumps(configuration), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "interpreter": command[0], "instance_id": self.instance_id,
                    "schema_version": 56, "integrity": "PASS",
                }), "")
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "interpreter": command[0], "version": self.target_version,
                "metadata": str(site / f"engineering_platform-{self.target_version}.dist-info"),
                "package": str(site / "engineering_platform"),
            }), "")
        raise AssertionError(command)


class LegacyInstallationUpdateIntegrationTests(unittest.TestCase):
    @staticmethod
    def _wheel(path: Path, *, version: str, content: bytes) -> str:
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("engineering_platform/__init__.py", content)
            archive.writestr(
                f"engineering_platform-{version}.dist-info/METADATA",
                f"Name: engineering-platform\nVersion: {version}\n",
            )
            archive.writestr(
                f"engineering_platform-{version}.dist-info/RECORD",
                f"engineering_platform/__init__.py,sha256={encoded},{len(content)}\n",
            )
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _prepared(self, base: Path):  # type: ignore[no-untyped-def]
        root, home = base / "runtime", base / "home"
        identity = server.initialize(root)
        configuration = json.loads((root / "server.json").read_text(encoding="utf-8"))
        configuration["product_version"] = "2.3.34"
        (root / "server.json").write_text(json.dumps(configuration), encoding="utf-8")

        old_package = base / "old-site" / "engineering_platform"
        old_package.mkdir(parents=True)
        old_content = b"__version__ = '2.3.34'\n"
        (old_package / "__init__.py").write_bytes(old_content)
        old_interpreter = base / "old-venv" / "bin" / "python"
        old_interpreter.parent.mkdir(parents=True)
        old_interpreter.write_text("#!\n", encoding="utf-8")
        old_interpreter.chmod(0o700)
        old_wheel = base / "preserved-old.whl"
        old_digest = self._wheel(old_wheel, version="2.3.34", content=old_content)
        server_service.write_plist(server_service.default_paths(root, home), old_interpreter)

        runner = LegacyAndCandidateRunner(
            old_interpreter=old_interpreter, old_package=old_package,
            old_version="2.3.34", target_version="2.3.35", instance_id=identity.instance_id,
        )
        installation = operational_installation.resolve(root, interpreter=old_interpreter)
        observation = inspect(
            installation=installation, service_label=server_service.LABEL,
            service_interpreter=old_interpreter, preserved_wheel=old_wheel, runner=runner,
        )
        target = base / "target.whl"
        target.write_bytes(b"qualified target wheel")
        target_digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        authorization = LegacyAdoptionAuthorization(
            identity.instance_id, str(root.resolve()), server_service.LABEL,
            str(old_interpreter.absolute()), old_digest, "2.3.35", target_digest,
            "c" * 40, "legacy-update-0001", True,
        )
        adopt(observation=observation, authorization=authorization)
        source = prepare(
            root, operation_id="legacy-update-0001", artifact=target,
            target_version="2.3.35", target_digest=target_digest,
            target_source_revision="c" * 40,
        )
        builder = base / "builder-python"
        builder.write_text("#!\n", encoding="utf-8")
        builder.chmod(0o700)
        candidate = prepare_candidate(source, venv_builder=builder, runner=runner)
        plan = staged_execution_plan(source, candidate=candidate, runner=runner)
        with InstallationUpdateSession(plan) as session:
            session.bind_prepared_candidate(candidate, runner=runner)
        admission = admit(plan, runner=runner, service_home=home)
        return root, home, old_package, plan, candidate, admission, runner

    @staticmethod
    def _service_runner(command):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 0, "", "")

    def _actions(self, root: Path, home: Path, target: Path):
        def inventory(_plan):
            return {"result": "PASS", "service_interpreter": str(server_service.configured_interpreter(root, home=home))}

        def quiesce(_plan):
            return {"result": "PASS", "service": "QUIESCED"}

        def verify(_plan):
            self.assertEqual(server_service.configured_interpreter(root, home=home), target)
            self.assertEqual(load(root)["interpreter"], str(target))
            return {"result": "PASS", "service": "HEALTHY", "identity": "EXACT_TARGET"}

        return InstallationUpdateOperationalActions(inventory, quiesce, verify)

    @patch("engineering_platform.server_service.platform.system", return_value="Darwin")
    def test_authorized_legacy_baseline_runs_the_real_composition_and_reopens(self, _platform) -> None:  # type: ignore[no-untyped-def]
        with TemporaryDirectory() as temporary:
            root, home, _old_package, plan, candidate, admission, runner = self._prepared(Path(temporary))
            self.assertEqual(admission.schema_version, 3)
            self.assertIsNone(plan.legacy_adoption["source_revision"])
            target = Path(candidate.interpreter)
            result = execute(
                plan, admission=admission, actions=self._actions(root, home, target),
                migration_runner=runner, activation_home=home,
                activation_runner=self._service_runner,
            )
            self.assertEqual(result["state"], "COMPLETE")
            installed = load(root)
            self.assertEqual(installed["source_revision"], "c" * 40)
            self.assertEqual(installed["artifact_digest"], plan.target_digest)
            self.assertIsNone(admission.registered_installation["decision"]["observation"]["source_revision"])
            self.assertEqual(status(root, plan.operation_id)["state"], "COMPLETE")
            self.assertEqual(reopen_plan(root, plan.operation_id), plan)
            program = (
                "import json,os,pathlib,sys;os.chdir(sys.argv[1]);"
                "from engineering_platform.installation_update_operation import reopen_plan,status;"
                "p=reopen_plan(pathlib.Path(sys.argv[2]),sys.argv[3]);"
                "print(json.dumps({'state':status(pathlib.Path(sys.argv[2]),sys.argv[3])['state'],"
                "'target':p.target_version,'legacy_source':p.legacy_adoption['source_revision']}))"
            )
            reopened = subprocess.run(
                (sys.executable, "-I", "-c", program, str(Path(temporary)), str(root), plan.operation_id),
                cwd=Path(temporary), capture_output=True, text=True, check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
            if reopened.returncode and (
                "cannot import name 'reopen_plan'" in reopened.stderr
                or "No module named 'engineering_platform'" in reopened.stderr
            ):
                self.skipTest("local source run has no current installed wheel; canonical installed qualification covers it")
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            self.assertEqual(json.loads(reopened.stdout), {
                "state": "COMPLETE", "target": "2.3.35", "legacy_source": None,
            })

    @patch("engineering_platform.server_service.platform.system", return_value="Darwin")
    def test_public_maintenance_commands_drive_the_full_legacy_update(self, _platform) -> None:  # type: ignore[no-untyped-def]
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, home = base / "runtime", base / "home"
            identity = server.initialize(root)
            configuration = json.loads((root / "server.json").read_text(encoding="utf-8"))
            configuration["product_version"] = "2.3.34"
            (root / "server.json").write_text(json.dumps(configuration), encoding="utf-8")

            old_package = base / "old-site" / "engineering_platform"
            old_package.mkdir(parents=True)
            old_content = b"__version__ = '2.3.34'\n"
            (old_package / "__init__.py").write_bytes(old_content)
            old_interpreter = base / "old-venv" / "bin" / "python"
            old_interpreter.parent.mkdir(parents=True)
            old_interpreter.write_text("#!\n", encoding="utf-8")
            old_interpreter.chmod(0o700)
            old_wheel = base / "preserved-old.whl"
            self._wheel(old_wheel, version="2.3.34", content=old_content)
            server_service.write_plist(server_service.default_paths(root, home), old_interpreter)

            runner = LegacyAndCandidateRunner(
                old_interpreter=old_interpreter, old_package=old_package,
                old_version="2.3.34", target_version="2.3.35", instance_id=identity.instance_id,
            )
            target = base / "target.whl"
            target.write_bytes(b"qualified target wheel")
            target_digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            builder = base / "builder-python"
            builder.write_text("#!\n", encoding="utf-8")
            builder.chmod(0o700)
            operation_id = "legacy-update-0001"
            target_revision = "c" * 40

            real_default_paths = server_service.default_paths
            real_package_identity = operational_installation.package_identity
            real_prepare_candidate = installation_update_preparation.prepare_candidate
            real_staged_plan = installation_update_preparation.staged_execution_plan
            real_admit = installation_update_admission.admit
            real_execute = installation_update_composition.execute
            fixture_home = home

            def redirected_paths(data_root, home=None):  # type: ignore[no-untyped-def]
                return real_default_paths(data_root, fixture_home if home is None else home)

            def isolated_package_identity(interpreter, **_kwargs):  # type: ignore[no-untyped-def]
                return real_package_identity(interpreter, runner=runner)

            def public_prepare(plan, *, venv_builder):  # type: ignore[no-untyped-def]
                return real_prepare_candidate(plan, venv_builder=venv_builder, runner=runner)

            def public_staged_plan(plan, *, candidate=None):  # type: ignore[no-untyped-def]
                return real_staged_plan(plan, candidate=candidate, runner=runner)

            def public_admit(plan):  # type: ignore[no-untyped-def]
                return real_admit(plan, runner=runner, service_home=home)

            def public_execute(plan, *, admission, actions):  # type: ignore[no-untyped-def]
                return real_execute(
                    plan, admission=admission, actions=actions,
                    migration_runner=runner, activation_home=home,
                    activation_runner=self._service_runner,
                )

            candidate_interpreter = root.resolve() / "operations" / operation_id / "candidate-venv" / "bin" / "python"
            candidate_site = candidate_interpreter.parent.parent / "lib" / "python3.12" / "site-packages"
            health = {
                "service": "engineering-platform-server", "instance_id": identity.instance_id,
                "healthy": True, "product_version": "2.3.35",
                "runtime_identity": {
                    "interpreter": str(candidate_interpreter), "executable": str(candidate_interpreter),
                    "package": str(candidate_site / "engineering_platform"),
                    "package_version": "2.3.35",
                    "metadata": str(candidate_site / "engineering_platform-2.3.35.dist-info"),
                    "artifact": {"state": "UNAVAILABLE"},
                },
            }
            lifecycle = type("Lifecycle", (), {"loaded": True})()
            command_output = io.StringIO()
            with patch("engineering_platform.server_service.default_paths", side_effect=redirected_paths), patch(
                "engineering_platform.operational_installation.package_identity",
                side_effect=isolated_package_identity,
            ), patch(
                "engineering_platform.installation_update_preparation.prepare_candidate",
                side_effect=public_prepare,
            ), patch(
                "engineering_platform.installation_update_preparation.staged_execution_plan",
                side_effect=public_staged_plan,
            ), patch(
                "engineering_platform.installation_update_admission.admit", side_effect=public_admit,
            ), patch(
                "engineering_platform.installation_update_composition.execute", side_effect=public_execute,
            ), patch("engineering_platform.server.LaunchdProvider") as lifecycle_type, patch(
                "engineering_platform.server._health_response", return_value=health,
            ), redirect_stdout(command_output):
                lifecycle_type.return_value.runtime_details.return_value = lifecycle
                lifecycle_type.return_value.quiesce.return_value = None
                common_target = (
                    "--data-root", str(root), "--operation-id", operation_id,
                    "--target-version", "2.3.35", "--target-digest", target_digest,
                    "--target-source-revision", target_revision,
                )
                self.assertEqual(server.main((
                    "legacy-adoption-inspect", "--data-root", str(root),
                    "--preserved-wheel", str(old_wheel),
                )), 0)
                self.assertEqual(server.main((
                    "legacy-adoption-authorize", *common_target,
                    "--preserved-wheel", str(old_wheel),
                    "--acknowledge-unknown-source-revision",
                )), 0)
                self.assertEqual(server.main((
                    "installation-update-plan", *common_target, "--artifact", str(target),
                )), 0)
                self.assertEqual(server.main((
                    "installation-update-prepare", *common_target, "--artifact", str(target),
                    "--venv-builder", str(builder),
                )), 0)
                self.assertEqual(server.main((
                    "installation-update-admit", "--data-root", str(root),
                    "--operation-id", operation_id,
                )), 0)
                self.assertEqual(server.main((
                    "installation-update-apply", "--data-root", str(root),
                    "--operation-id", operation_id,
                )), 0, command_output.getvalue())
                self.assertEqual(server.main((
                    "installation-update-resume", "--data-root", str(root),
                    "--operation-id", operation_id,
                )), 0)

            self.assertEqual(status(root, operation_id)["state"], "COMPLETE")
            installed = load(root)
            self.assertEqual(installed["interpreter"], str(candidate_interpreter))
            self.assertEqual(installed["source_revision"], target_revision)
            self.assertFalse((root / "operations" / operation_id / "download").exists())

    @patch("engineering_platform.server_service.platform.system", return_value="Darwin")
    def test_service_switch_before_record_and_record_before_event_are_both_resumable(self, _platform) -> None:  # type: ignore[no-untyped-def]
        with TemporaryDirectory() as temporary:
            root, home, _old_package, plan, candidate, admission, runner = self._prepared(Path(temporary))
            target = Path(candidate.interpreter)
            real_record = __import__(
                "engineering_platform.installation_update_executor", fromlist=["operational_installation_record"]
            ).operational_installation_record.record
            writes = 0

            def lose_first_record(*args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal writes
                writes += 1
                if writes == 1:
                    raise RuntimeError("lost service acknowledgement")
                return real_record(*args, **kwargs)

            with patch(
                "engineering_platform.installation_update_executor.operational_installation_record.record",
                side_effect=lose_first_record,
            ), self.assertRaisesRegex(RuntimeError, "lost service acknowledgement"):
                execute(
                    plan, admission=admission, actions=self._actions(root, home, target),
                    migration_runner=runner, activation_home=home,
                    activation_runner=self._service_runner,
                )
            self.assertEqual(status(root, plan.operation_id)["state"], "MIGRATED")
            self.assertEqual(server_service.configured_interpreter(root, home=home), target)

            result = execute(
                plan, admission=admission, actions=self._actions(root, home, target),
                migration_runner=runner, activation_home=home,
                activation_runner=self._service_runner,
            )
            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(writes, 1)

    @patch("engineering_platform.server_service.platform.system", return_value="Darwin")
    def test_exact_target_record_before_activated_event_is_acknowledged_without_second_activation(self, _platform) -> None:  # type: ignore[no-untyped-def]
        with TemporaryDirectory() as temporary:
            root, home, _old_package, plan, candidate, admission, runner = self._prepared(Path(temporary))
            target = Path(candidate.interpreter)
            original_advance = InstallationUpdateSession.advance

            def lose_activated_event(session, state, evidence):  # type: ignore[no-untyped-def]
                if state == "ACTIVATED":
                    raise RuntimeError("lost ACTIVATED event")
                return original_advance(session, state, evidence)

            with patch.object(InstallationUpdateSession, "advance", new=lose_activated_event), self.assertRaisesRegex(
                RuntimeError, "lost ACTIVATED event",
            ):
                execute(
                    plan, admission=admission, actions=self._actions(root, home, target),
                    migration_runner=runner, activation_home=home,
                    activation_runner=self._service_runner,
                )
            self.assertEqual(status(root, plan.operation_id)["state"], "MIGRATED")
            self.assertEqual(load(root)["interpreter"], str(target))

            with patch(
                "engineering_platform.installation_update_activation.server_service.replace_runtime"
            ) as second_activation:
                result = execute(
                    plan, admission=admission, actions=self._actions(root, home, target),
                    migration_runner=runner, activation_home=home,
                    activation_runner=self._service_runner,
                )
            self.assertEqual(result["state"], "COMPLETE")
            second_activation.assert_not_called()
            self.assertTrue((root / "operations" / plan.operation_id / "backup" / "central.sqlite").is_file())

    @patch("engineering_platform.server_service.platform.system", return_value="Darwin")
    def test_migration_side_effect_before_event_is_resumable(self, _platform) -> None:  # type: ignore[no-untyped-def]
        with TemporaryDirectory() as temporary:
            root, home, _old_package, plan, candidate, admission, runner = self._prepared(Path(temporary))
            target = Path(candidate.interpreter)
            original_advance = InstallationUpdateSession.advance

            def lose_migrated_event(session, state, evidence):  # type: ignore[no-untyped-def]
                if state == "MIGRATED":
                    raise RuntimeError("lost MIGRATED event")
                return original_advance(session, state, evidence)

            with patch.object(InstallationUpdateSession, "advance", new=lose_migrated_event), self.assertRaisesRegex(
                RuntimeError, "lost MIGRATED event",
            ):
                execute(
                    plan, admission=admission, actions=self._actions(root, home, target),
                    migration_runner=runner, activation_home=home,
                    activation_runner=self._service_runner,
                )
            self.assertEqual(status(root, plan.operation_id)["state"], "BACKED_UP")

            result = execute(
                plan, admission=admission, actions=self._actions(root, home, target),
                migration_runner=runner, activation_home=home,
                activation_runner=self._service_runner,
            )
            self.assertEqual(result["state"], "COMPLETE")

    def test_stale_service_or_package_blocks_admission_before_mutation(self) -> None:
        for changed in ("service", "service_data_root", "package"):
            with self.subTest(changed=changed), TemporaryDirectory() as temporary:
                root, home, old_package, plan, candidate, _admission, runner = self._prepared(Path(temporary))
                # Recreate the PREPARED state without consuming the existing
                # immutable evidence; admission must revalidate material state.
                journal = root / "operations" / plan.operation_id / "operation.json"
                payload = json.loads(journal.read_text(encoding="utf-8"))
                payload["schema_version"] = 3
                payload.pop("execution_admission")
                payload.pop("execution_admission_digest")
                journal.write_text(json.dumps(payload), encoding="utf-8")
                if changed == "service":
                    other = Path(temporary) / "other-python"
                    other.write_text("#!\n", encoding="utf-8"); other.chmod(0o700)
                    server_service.write_plist(server_service.default_paths(root, home), other)
                elif changed == "service_data_root":
                    paths = server_service.default_paths(root, home)
                    with paths.plist_path.open("rb") as stream:
                        payload = plistlib.load(stream)
                    payload["ProgramArguments"][-1] = str(Path(temporary) / "other-instance")
                    with paths.plist_path.open("wb") as stream:
                        plistlib.dump(payload, stream)
                else:
                    (old_package / "__init__.py").write_text("changed\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "legacy installation changed"):
                    admit(plan, runner=runner, service_home=home)
                self.assertEqual(status(root, plan.operation_id)["state"], "PREPARED")


if __name__ == "__main__":
    unittest.main()
