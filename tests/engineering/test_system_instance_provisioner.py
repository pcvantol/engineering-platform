from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import pwd
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from engineering_platform import operational_installation_record
from engineering_platform import system_installation_topology as topology
from engineering_platform import system_instance_provisioner as provisioner_module
from engineering_platform import system_provider_context
from engineering_platform import system_server_service


class FakeSystemServiceController:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.loaded_instances: set[str] = set()

    def register(self, instance: topology.SystemInstanceTopology, interpreter: Path) -> dict[str, object]:
        paths = system_server_service.instance_default_paths(instance, self.directory)
        definition = system_server_service.instance_service_definition(instance, interpreter=interpreter)
        paths.plist_path.write_bytes(plistlib.dumps(system_server_service.instance_plist_payload(paths, definition)))
        return {"result": "REGISTERED", "label": instance.service_label}

    def quiesce(self, instance: topology.SystemInstanceTopology) -> dict[str, object]:
        self.loaded_instances.discard(instance.instance_id)
        return {"result": "QUIESCED", "label": instance.service_label}

    def start(self, instance: topology.SystemInstanceTopology) -> dict[str, object]:
        self.loaded_instances.add(instance.instance_id)
        return {"result": "RUNNING", "label": instance.service_label}

    def loaded(self, instance: topology.SystemInstanceTopology) -> bool:
        return instance.instance_id in self.loaded_instances

    def remove(self, instance: topology.SystemInstanceTopology) -> dict[str, object]:
        self.quiesce(instance)
        system_server_service.instance_default_paths(instance, self.directory).plist_path.unlink(missing_ok=True)
        return {"result": "REMOVED", "label": instance.service_label}


class FixtureProvisioner(provisioner_module.SystemInstanceProvisioner):
    def _install_slot(self, release: provisioner_module.ReleaseRequest) -> topology.RuntimeSlot:
        slot = topology.runtime_slot(self.product, release.identity())
        slot.interpreter.parent.mkdir(parents=True, exist_ok=True)
        slot.interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        slot.interpreter.chmod(0o755)
        (slot.venv / "pyvenv.cfg").write_text("home=/usr/bin\n", encoding="utf-8")
        (slot.root / "runtime-slot.json").write_text(json.dumps({
            "schema_version": 1, **release.identity().payload(), "venv": str(slot.venv),
        }), encoding="utf-8")
        return slot

    def _initialize_data(self, instance: topology.SystemInstanceTopology, interpreter: Path, port: int) -> None:
        instance.data_root.mkdir(parents=True, exist_ok=True)
        (instance.data_root / "runtime-identity.json").write_text(json.dumps({
            "instance_id": instance.instance_id, "created_at": "2026-09-22T00:00:00Z",
        }), encoding="utf-8")
        (instance.data_root / "server.json").write_text(json.dumps({
            "version": 3, "bind_host": "127.0.0.1", "bind_port": port,
            "managed_codex_cli_prefix": str(system_provider_context.provider_context(instance, "codex").installation_root),
            "product_version": "2.3.102",
        }), encoding="utf-8")


class SystemInstanceProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.product_root = self.base / "EP Product"
        self.launch_daemons = self.base / "LaunchDaemons"
        self.launch_daemons.mkdir()
        self.controller = FakeSystemServiceController(self.launch_daemons)
        self.accounts = {
            "ep-production-0001": "_ep_prod",
            "ep-development-0001": "_ep_dev",
        }
        self.provisioner = FixtureProvisioner(
            self.product_root,
            controller=self.controller,
            launch_daemons_dir=self.launch_daemons,
            health_verifier=lambda instance, _interpreter: {
                "result": "PASS", "instance_id": instance.instance_id, "identity_aware": True,
            },
            account_lookup=lambda name: (
                SimpleNamespace(pw_uid=501)
                if name in self.accounts.values()
                else (_ for _ in ()).throw(KeyError(name))
            ),
        )
        self.wheel = self.base / "engineering_platform-2.3.102-py3-none-any.whl"
        self.wheel.write_bytes(b"installed-artifact-fixture")
        self.release = provisioner_module.ReleaseRequest(
            "2.3.102",
            self.wheel,
            "sha256:" + hashlib.sha256(self.wheel.read_bytes()).hexdigest(),
            "a" * 40,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _instance(self, identity: str, label: str) -> topology.SystemInstanceTopology:
        return topology.system_instance_topology(
            self.provisioner.product,
            instance_id=identity,
            display_label=label,
            service_account=self.accounts[identity],
        )

    def _providers(self, identity: str, label: str) -> None:
        instance = self._instance(identity, label)
        for context in system_provider_context.provider_contexts(instance):
            context.executable.parent.mkdir(parents=True, exist_ok=True)
            context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            context.executable.chmod(0o755)
            self.provisioner.provider_register(
                instance_id=identity,
                display_label=label,
                service_account=self.accounts[identity],
                provider=context.provider,
                executable_sha256=system_provider_context.executable_digest(context.executable),
                version="1.0.0",
                auth_reference=f"{context.provider}:auth:{identity}",
                auth_bootstrap_receipt=f"{context.provider}:receipt:{identity}",
            )

    def _create(self, identity: str, label: str, port: int) -> dict[str, object]:
        self._providers(identity, label)
        return dict(self.provisioner.create(provisioner_module.InstanceRequest(
            f"install-{identity}", identity, label, self.accounts[identity], port, self.release,
        )))

    def test_create_inventory_readiness_and_remove_are_exact_instance_scoped(self) -> None:
        first = self._create("ep-production-0001", "EP production", 8765)
        second = self._create("ep-development-0001", "EP development", 8766)
        self.assertEqual(first["result"], "COMPLETE")
        self.assertEqual(second["result"], "COMPLETE")

        inventory = self.provisioner.inventory()
        self.assertEqual(inventory["state"], "OBSERVED")
        self.assertEqual(inventory["instance_count"], 2)
        self.assertEqual(
            {entry["instance_id"] for entry in inventory["instances"]},
            {"ep-production-0001", "ep-development-0001"},
        )
        self.assertTrue(self.provisioner.status("ep-production-0001")["ready"])
        self.assertTrue(self.provisioner.status("ep-development-0001")["ready"])
        self.assertEqual(
            operational_installation_record.load(
                self._instance("ep-production-0001", "EP production").data_root
            )["verification"],
            {"result": "PASS"},
        )

        second_instance = self._instance("ep-development-0001", "EP development")
        second_record = operational_installation_record.load(second_instance.data_root)
        second_descriptor = second_instance.descriptor.read_bytes()
        removed = self.provisioner.remove(
            "ep-production-0001", "remove-production-0001",
            confirm_instance_id="ep-production-0001",
        )
        self.assertEqual(removed["result"], "COMPLETE")
        self.assertTrue(second_instance.root.is_dir())
        self.assertEqual(operational_installation_record.load(second_instance.data_root), second_record)
        self.assertEqual(second_instance.descriptor.read_bytes(), second_descriptor)
        self.assertTrue(Path(second_record["interpreter"]).is_file())
        self.assertTrue(self.provisioner.status("ep-development-0001")["ready"])
        self.assertEqual(
            self.provisioner.remove(
                "ep-production-0001", "remove-production-0001",
                confirm_instance_id="ep-production-0001",
            ),
            removed,
        )

    def test_install_lost_response_returns_exact_terminal_receipt(self) -> None:
        first = self._create("ep-production-0001", "EP production", 8765)
        repeated = self.provisioner.create(provisioner_module.InstanceRequest(
            "install-ep-production-0001", "ep-production-0001", "EP production",
            self.accounts["ep-production-0001"], 8765, self.release,
        ))
        self.assertEqual(repeated, first)

        conflicting_wheel = self.base / "engineering_platform-2.3.103-py3-none-any.whl"
        conflicting_wheel.write_bytes(b"different-release")
        conflicting = provisioner_module.ReleaseRequest(
            "2.3.103", conflicting_wheel,
            "sha256:" + hashlib.sha256(conflicting_wheel.read_bytes()).hexdigest(),
            "b" * 40,
        )
        with self.assertRaisesRegex(
            provisioner_module.SystemInstanceProvisionerError,
            "receipt release identity conflict",
        ):
            self.provisioner.create(provisioner_module.InstanceRequest(
                "install-ep-production-0001", "ep-production-0001", "EP production",
                self.accounts["ep-production-0001"], 8765, conflicting,
            ))

    def test_foreign_or_tampered_terminal_receipt_fails_closed(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        receipt_path = (
            self.provisioner.product.system_root / "receipts" / "ep-production-0001"
            / "install-ep-production-0001.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["product_root"] = str(self.base / "foreign-product")
        unsigned = dict(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = provisioner_module._json_digest(unsigned)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        with self.assertRaisesRegex(
            provisioner_module.SystemInstanceProvisionerError,
            "receipt is invalid",
        ):
            self.provisioner.create(provisioner_module.InstanceRequest(
                "install-ep-production-0001", "ep-production-0001", "EP production",
                self.accounts["ep-production-0001"], 8765, self.release,
            ))

    def test_collision_wrong_target_and_provider_substitution_fail_closed(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        self._providers("ep-development-0001", "EP development")
        with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "endpoint collision"):
            self.provisioner.create(provisioner_module.InstanceRequest(
                "install-development-0001", "ep-development-0001", "EP development",
                self.accounts["ep-development-0001"], 8765, self.release,
            ))
        with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "confirmation mismatch"):
            self.provisioner.remove(
                "ep-production-0001", "remove-production-0001",
                confirm_instance_id="ep-development-0001",
            )
        instance = self._instance("ep-production-0001", "EP production")
        codex = system_provider_context.provider_context(instance, "codex")
        codex.executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        codex.executable.chmod(0o755)
        with self.assertRaisesRegex(system_provider_context.SystemProviderContextError, "substituted"):
            self.provisioner.status(instance.instance_id)

    def test_assessment_is_bound_to_exact_artifact_and_instance(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        no_change = self.provisioner.assess_update("ep-production-0001", self.release)
        self.assertEqual(no_change["state"], "NO_CHANGE")
        newer_wheel = self.base / "engineering_platform-2.3.103-py3-none-any.whl"
        newer_wheel.write_bytes(b"newer-installed-artifact")
        newer = provisioner_module.ReleaseRequest(
            "2.3.103", newer_wheel,
            "sha256:" + hashlib.sha256(newer_wheel.read_bytes()).hexdigest(),
            "b" * 40,
        )
        self.assertEqual(
            self.provisioner.assess_update("ep-production-0001", newer)["state"],
            "UPDATE_AVAILABLE",
        )
        with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "descriptor"):
            self.provisioner.assess_update("ep-missing-0001", newer)

    def test_update_resume_cli_reopens_durable_identity_without_artifact_arguments(self) -> None:
        captured: dict[str, object] = {}

        class FakeProvisioner:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def update(self, **kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"result": "COMPLETE"}

        output = io.StringIO()
        with patch.object(provisioner_module, "SystemInstanceProvisioner", FakeProvisioner), redirect_stdout(output):
            result = provisioner_module.main([
                "update-resume",
                "--product-root", str(self.product_root),
                "--instance-id", "ep-production-0001",
                "--operation-id", "update-production-0001",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(captured["release"], None)
        self.assertEqual(captured["resume"], True)

    def test_status_repair_and_assessment_failure_states(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        self.controller.loaded_instances.clear()
        self.assertEqual(self.provisioner.status(instance.instance_id)["state"], "NOT_READY")
        with patch.object(
            provisioner_module.operational_installation,
            "package_identity",
            return_value={"version": self.release.version},
        ):
            repaired = self.provisioner.repair(instance.instance_id, "repair-production-0001")
        self.assertEqual(repaired["result"], "COMPLETE")
        self.assertEqual(
            self.provisioner.repair(instance.instance_id, "repair-production-0001"),
            repaired,
        )
        older_wheel = self.base / "engineering_platform-2.3.101-py3-none-any.whl"
        older_wheel.write_bytes(b"older")
        older = provisioner_module.ReleaseRequest(
            "2.3.101", older_wheel,
            "sha256:" + hashlib.sha256(older_wheel.read_bytes()).hexdigest(), "b" * 40,
        )
        self.assertEqual(
            self.provisioner.assess_update(instance.instance_id, older)["state"],
            "BLOCKED_DOWNGRADE",
        )
        changed_wheel = self.base / "engineering_platform-2.3.102-rebuilt-py3-none-any.whl"
        changed_wheel.write_bytes(b"rebuilt")
        changed = provisioner_module.ReleaseRequest(
            "2.3.102", changed_wheel,
            "sha256:" + hashlib.sha256(changed_wheel.read_bytes()).hexdigest(), "c" * 40,
        )
        self.assertEqual(
            self.provisioner.assess_update(instance.instance_id, changed)["state"],
            "BLOCKED_IDENTITY_MISMATCH",
        )
        system_server_service.instance_default_paths(instance, self.launch_daemons).plist_path.unlink()
        self.assertEqual(self.provisioner.status(instance.instance_id)["state"], "SERVICE_ABSENT")

    def test_update_execute_resume_status_and_terminal_readback(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        newer_wheel = self.base / "engineering_platform-2.3.103-py3-none-any.whl"
        newer_wheel.write_bytes(b"newer")
        newer = provisioner_module.ReleaseRequest(
            "2.3.103", newer_wheel,
            "sha256:" + hashlib.sha256(newer_wheel.read_bytes()).hexdigest(), "b" * 40,
        )
        plan = SimpleNamespace(data_root=str(instance.data_root), operation_id="update-production-0001")

        class Session:
            def __init__(self, _plan: object) -> None:
                pass

            def __enter__(self) -> "Session":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def bind_prepared_candidate(self, _candidate: object, *, runner: object) -> None:
                self.runner = runner

        patches = (
            patch.object(provisioner_module.installation_update_plan, "prepare", return_value=plan),
            patch.object(provisioner_module.installation_update_preparation, "prepare_candidate", return_value={"candidate": True}),
            patch.object(provisioner_module.installation_update_preparation, "staged_execution_plan", return_value=plan),
            patch.object(provisioner_module.installation_update_operation, "InstallationUpdateSession", Session),
            patch.object(provisioner_module.installation_update_admission, "admit"),
            patch.object(provisioner_module.installation_update_operation, "execution_admission", return_value={}),
            patch.object(provisioner_module.installation_update_admission, "ExecutionAdmission", return_value=object()),
            patch.object(provisioner_module.installation_update_composition, "execute", return_value={"state": "COMPLETE"}),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            result = self.provisioner.update(
                instance_id=instance.instance_id,
                operation_id="update-production-0001",
                release=newer,
            )
        self.assertEqual(result["result"], "COMPLETE")
        self.assertEqual(
            self.provisioner.update(
                instance_id=instance.instance_id,
                operation_id="update-production-0001",
                release=None,
                resume=True,
            ),
            result,
        )
        with patch.object(provisioner_module.installation_update_operation, "status", return_value={"state": "COMPLETE"}):
            self.assertEqual(
                self.provisioner.update_status(instance.instance_id, "update-production-0001")["installation_update"],
                {"state": "COMPLETE"},
            )

        resume_plan = SimpleNamespace(data_root=str(instance.data_root), operation_id="update-production-0002")
        with (
            patch.object(provisioner_module.installation_update_operation, "reopen_plan", return_value=resume_plan),
            patch.object(provisioner_module.installation_update_operation, "execution_admission", return_value={}),
            patch.object(provisioner_module.installation_update_admission, "ExecutionAdmission", return_value=object()),
            patch.object(provisioner_module.installation_update_composition, "execute", return_value={"state": "COMPLETE"}),
        ):
            resumed = self.provisioner.update(
                instance_id=instance.instance_id,
                operation_id="update-production-0002",
                release=None,
                resume=True,
            )
        self.assertEqual(resumed["result"], "COMPLETE")

    def test_runtime_slot_and_data_initialization_boundaries(self) -> None:
        concrete = provisioner_module.SystemInstanceProvisioner(
            self.product_root,
            controller=self.controller,
            launch_daemons_dir=self.launch_daemons,
            health_verifier=self.provisioner.health_verifier,
            account_lookup=self.provisioner.account_lookup,
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(provisioner_module.subprocess, "run", return_value=completed),
            patch.object(provisioner_module.operational_installation, "package_identity", return_value={"version": self.release.version}),
        ):
            slot = concrete._install_slot(self.release)
            self.assertEqual(concrete._install_slot(self.release), slot)

        instance = self._instance("ep-production-0001", "EP production")
        interpreter = self.base / "python"
        interpreter.write_text("runtime", encoding="utf-8")
        with patch.object(provisioner_module.subprocess, "run", return_value=completed):
            concrete._initialize_data(instance, interpreter, 8765)
            concrete._initialize_data(instance, interpreter, 8765)
        identity = json.loads((instance.data_root / "runtime-identity.json").read_text())
        self.assertEqual(identity["instance_id"], instance.instance_id)

        (instance.data_root / "runtime-identity.json").write_text(
            json.dumps({"instance_id": "ep-development-0001"}), encoding="utf-8",
        )
        with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "another identity"):
            concrete._initialize_data(instance, interpreter, 8765)

    def test_macos_controller_lock_health_and_json_helpers(self) -> None:
        instance = self._instance("ep-production-0001", "EP production")
        controller = provisioner_module.MacOSSystemServiceController(self.launch_daemons)
        interpreter = self.base / "controller-venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        (interpreter.parent.parent / "pyvenv.cfg").write_text("home=/usr/bin\n", encoding="utf-8")
        interpreter.write_text("runtime", encoding="utf-8")
        interpreter.chmod(0o755)
        process = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(provisioner_module.os, "geteuid", return_value=0),
            patch.object(provisioner_module.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=501)),
            patch.object(controller, "_launchctl", return_value=process),
        ):
            self.assertEqual(controller.register(instance, interpreter)["result"], "REGISTERED")
            self.assertTrue(controller.loaded(instance))
            self.assertEqual(controller.start(instance)["result"], "RUNNING")
            self.assertEqual(controller.quiesce(instance)["result"], "QUIESCED")
            self.assertEqual(controller.remove(instance)["result"], "REMOVED")
        with patch.object(provisioner_module.os, "geteuid", return_value=501):
            with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "requires root"):
                controller.register(instance, interpreter)

        lock_path = self.base / "lock" / "instance.lock"
        with provisioner_module._Lock(lock_path):
            with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "owns"):
                with provisioner_module._Lock(lock_path):
                    pass

        record_path = self.base / "record.json"
        self.assertTrue(provisioner_module._publish_json(record_path, {"a": 1}))
        self.assertFalse(provisioner_module._publish_json(record_path, {"a": 1}))
        self.assertEqual(provisioner_module._read_json(record_path), {"a": 1})
        record_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(provisioner_module.SystemInstanceProvisionerError, "invalid"):
            provisioner_module._read_json(record_path)

    def test_live_health_and_update_action_adapters(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        record = operational_installation_record.load(instance.data_root)

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({
                    "service": "engineering-platform-server",
                    "instance_id": instance.instance_id,
                    "product_version": record["version"],
                    "healthy": True,
                }).encode()

        with patch.object(provisioner_module, "urlopen", return_value=Response()):
            self.assertEqual(
                self.provisioner._live_health(instance, Path(record["interpreter"]))["result"],
                "PASS",
            )
        actions = self.provisioner._update_actions(instance)
        plan = SimpleNamespace(data_root=str(instance.data_root))
        self.assertEqual(actions.inventory(plan)["result"], "PASS")
        self.assertEqual(actions.quiesce_preflight(plan)["result"], "PASS")
        self.assertEqual(actions.quiesce(plan)["result"], "QUIESCED")
        self.controller.start(instance)
        with patch.object(
            provisioner_module.installation_update_activation,
            "replacement_record",
            return_value={"version": "2.3.103"},
        ):
            self.assertEqual(
                actions.activate(plan, Path(record["interpreter"]), record)["version"],
                "2.3.103",
            )
        self.assertEqual(actions.verify(plan)["result"], "PASS")

    def test_cli_routes_and_validation_errors(self) -> None:
        calls: list[str] = []

        class FakeProvisioner:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __getattr__(self, name: str):
                def call(*_args: object, **_kwargs: object) -> dict[str, object]:
                    calls.append(name)
                    return {"result": name}
                return call

        digest = "sha256:" + hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        release_args = [
            "--artifact", str(self.wheel), "--artifact-digest", digest,
            "--version", "2.3.102", "--source-revision", "a" * 40,
        ]
        common = ["--product-root", str(self.product_root)]
        invocations = [
            ["inventory", *common],
            ["create", *common, "--operation-id", "install-production-0001", "--instance-id", "ep-production-0001", "--display-label", "EP production", "--service-account", "_ep_prod", "--bind-port", "8765", *release_args],
            ["status", *common, "--instance-id", "ep-production-0001"],
            ["update-assess", *common, "--instance-id", "ep-production-0001", *release_args],
            ["update-execute", *common, "--instance-id", "ep-production-0001", "--operation-id", "update-production-0001", *release_args],
            ["update-status", *common, "--instance-id", "ep-production-0001", "--operation-id", "update-production-0001"],
            ["repair", *common, "--instance-id", "ep-production-0001", "--operation-id", "repair-production-0001"],
            ["remove", *common, "--instance-id", "ep-production-0001", "--operation-id", "remove-production-0001", "--confirm-instance-id", "ep-production-0001"],
            ["provider-register", *common, "--instance-id", "ep-production-0001", "--display-label", "EP production", "--service-account", "_ep_prod", "--provider", "codex", "--provider-executable-digest", digest, "--provider-version", "1.0.0", "--auth-reference", "codex:auth:production", "--auth-bootstrap-receipt", "codex:receipt:production"],
        ]
        with patch.object(provisioner_module, "SystemInstanceProvisioner", FakeProvisioner):
            for invocation in invocations:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(provisioner_module.main(invocation), 0)
        self.assertEqual(
            calls,
            ["inventory", "create", "status", "assess_update", "update", "update_status", "repair", "remove", "provider_register"],
        )

        with (
            patch.object(provisioner_module, "SystemInstanceProvisioner", FakeProvisioner),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(provisioner_module.main(["create", *common]), 2)


    def test_preserve_restore_and_provider_reverification_are_product_owned(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        marker = instance.data_root / "preserved.json"
        marker.write_text('{"keep":true}\n', encoding="utf-8")

        preserved = self.provisioner.preserve(
            instance.instance_id,
            "preserve-production-0001",
            confirm_instance_id=instance.instance_id,
        )
        evidence = preserved["receipt"]["evidence"]
        self.assertEqual(evidence["lifecycle_state"], "UNINSTALLED_DATA_PRESERVED")
        self.assertEqual(evidence["instance_identity"], "PRESERVED")
        self.assertEqual(evidence["mutable_instance_data"], "PRESERVED")
        self.assertTrue(evidence["restorable"])
        self.assertEqual(evidence["service_state"], "REMOVED_OR_INACTIVE")
        self.assertEqual(
            evidence["provider_auth_state"], "PRESERVED_REQUIRES_REVERIFICATION",
        )
        self.assertTrue(instance.root.is_dir())
        self.assertFalse(self.controller.loaded(instance))
        self.assertFalse(
            system_server_service.instance_default_paths(instance, self.launch_daemons).plist_path.exists()
        )
        for context in system_provider_context.provider_contexts(instance):
            auth = json.loads(context.auth_receipt.read_text(encoding="utf-8"))
            self.assertEqual(auth["state"], "PRESERVED_REQUIRES_REVERIFICATION")
            with self.assertRaises(system_provider_context.SystemProviderContextError):
                system_provider_context.readback(context)

        restored = self.provisioner.restore(
            instance.instance_id,
            "restore-production-0001",
            preserve_operation_id="preserve-production-0001",
            release=self.release,
        )
        restore_evidence = restored["receipt"]["evidence"]
        self.assertEqual(
            restore_evidence["lifecycle_state"],
            "RESTORED_REQUIRES_PROVIDER_REVERIFICATION",
        )
        self.assertEqual(restore_evidence["service_state"], "REGISTERED_INACTIVE")
        self.assertFalse(restore_evidence["ready"])
        self.assertFalse(self.controller.loaded(instance))
        self.assertTrue(
            system_server_service.instance_default_paths(instance, self.launch_daemons).plist_path.is_file()
        )
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"keep":true}\n')

        self._providers("ep-production-0001", "EP production")
        with patch.object(
            provisioner_module.operational_installation,
            "package_identity",
            return_value={"version": self.release.version},
        ):
            repaired = self.provisioner.repair(instance.instance_id, "repair-restored-0001")
        self.assertEqual(repaired["result"], "COMPLETE")
        self.assertTrue(self.controller.loaded(instance))

    def test_restore_rejects_tampered_data_and_wrong_release(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        self.provisioner.preserve(
            instance.instance_id,
            "preserve-production-0001",
            confirm_instance_id=instance.instance_id,
        )
        (instance.data_root / "tamper").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "no longer matches",
        ):
            self.provisioner.restore(
                instance.instance_id,
                "restore-tampered-0001",
                preserve_operation_id="preserve-production-0001",
                release=self.release,
            )
        (instance.data_root / "tamper").unlink()

        other_wheel = self.base / "engineering_platform-2.3.103-py3-none-any.whl"
        other_wheel.write_bytes(b"different-release")
        other = provisioner_module.ReleaseRequest(
            "2.3.103",
            other_wheel,
            "sha256:" + hashlib.sha256(other_wheel.read_bytes()).hexdigest(),
            "b" * 40,
        )
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "does not match preserved runtime identity",
        ):
            self.provisioner.restore(
                instance.instance_id,
                "restore-wrong-release-0001",
                preserve_operation_id="preserve-production-0001",
                release=other,
            )

    def test_restore_rejects_foreign_service_before_first_restore_attempt(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        self.provisioner.preserve(
            instance.instance_id,
            "preserve-production-0001",
            confirm_instance_id=instance.instance_id,
        )
        record = operational_installation_record.load(instance.data_root)
        self.controller.register(instance, Path(str(record["interpreter"])))
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "foreign service definition",
        ):
            self.provisioner.restore(
                instance.instance_id,
                "restore-foreign-service",
                preserve_operation_id="preserve-production-0001",
                release=self.release,
            )

    def test_lifecycle_status_rejects_foreign_instance_path(self) -> None:
        with self.assertRaises(
            topology.SystemInstallationTopologyError,
        ):
            self.provisioner.lifecycle_status("../foreign", "restore-foreign")

    def test_purge_is_permanent_and_sibling_instance_is_untouched(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        self._create("ep-development-0001", "EP development", 8766)
        first = self._instance("ep-production-0001", "EP production")
        second = self._instance("ep-development-0001", "EP development")
        second_descriptor = second.descriptor.read_bytes()
        second_record = operational_installation_record.load(second.data_root)

        self.provisioner.preserve(
            first.instance_id,
            "preserve-production-0001",
            confirm_instance_id=first.instance_id,
        )
        purged = self.provisioner.purge(
            first.instance_id,
            "purge-production-0001",
            confirm_instance_id=first.instance_id,
        )
        evidence = purged["receipt"]["evidence"]
        self.assertEqual(evidence["lifecycle_state"], "PURGED")
        self.assertFalse(evidence["restorable"])
        self.assertFalse(first.root.exists())
        self.assertEqual(second.descriptor.read_bytes(), second_descriptor)
        self.assertEqual(operational_installation_record.load(second.data_root), second_record)

        with self.assertRaisesRegex(
            (provisioner_module.SystemInstanceProvisionerError, FileNotFoundError),
            "descriptor|unavailable",
        ):
            self.provisioner.restore(
                first.instance_id,
                "restore-after-purge-0001",
                preserve_operation_id="preserve-production-0001",
                release=self.release,
            )
        status = self.provisioner.lifecycle_status(first.instance_id, "purge-production-0001")
        self.assertEqual(status["state"], "COMPLETE")
        self.assertEqual(status["lifecycle_state"], "PURGED")

    def test_preserve_and_restore_resume_after_lost_terminal_response(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        original_receipt = provisioner_module.system_instance_lifecycle.SystemInstanceLifecycle._receipt

        with patch.object(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycle,
            "_receipt",
            side_effect=InterruptedError("lost preserve response"),
        ):
            with self.assertRaises(InterruptedError):
                self.provisioner.preserve(
                    instance.instance_id,
                    "preserve-lost-response",
                    confirm_instance_id=instance.instance_id,
                )
        preserved = self.provisioner.preserve(
            instance.instance_id,
            "preserve-lost-response",
            confirm_instance_id=instance.instance_id,
        )
        self.assertEqual(preserved["receipt"]["state"], "COMPLETE")

        with patch.object(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycle,
            "_receipt",
            side_effect=InterruptedError("lost restore response"),
        ):
            with self.assertRaises(InterruptedError):
                self.provisioner.restore(
                    instance.instance_id,
                    "restore-lost-response",
                    preserve_operation_id="preserve-lost-response",
                    release=self.release,
                )
        restored = self.provisioner.restore(
            instance.instance_id,
            "restore-lost-response",
            preserve_operation_id="preserve-lost-response",
            release=self.release,
        )
        self.assertEqual(restored["receipt"]["state"], "COMPLETE")
        self.assertIsNotNone(original_receipt)

    def test_purge_recovers_if_legacy_remove_receipt_precedes_rmtree(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        with patch.object(
            provisioner_module.shutil,
            "rmtree",
            side_effect=OSError("simulated crash after legacy remove receipt"),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.provisioner.purge(
                    instance.instance_id,
                    "purge-rmtree-interrupt",
                    confirm_instance_id=instance.instance_id,
                )
        self.assertTrue(instance.root.is_dir())
        legacy = self.provisioner._terminal_receipt(
            instance.instance_id, "purge-rmtree-interrupt", "REMOVE",
        )
        self.assertIsNotNone(legacy)

        purged = self.provisioner.purge(
            instance.instance_id,
            "purge-rmtree-interrupt",
            confirm_instance_id=instance.instance_id,
        )
        self.assertFalse(instance.root.exists())
        self.assertEqual(purged["receipt"]["evidence"]["lifecycle_state"], "PURGED")
        self.assertEqual(
            self.provisioner.purge(
                instance.instance_id,
                "purge-rmtree-interrupt",
                confirm_instance_id=instance.instance_id,
            ),
            purged,
        )

    def test_purge_recovers_after_remove_completed_before_lifecycle_projection(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        with patch.object(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycle,
            "record_purge",
            side_effect=InterruptedError("lost purge projection"),
        ):
            with self.assertRaises(InterruptedError):
                self.provisioner.purge(
                    instance.instance_id,
                    "purge-lost-response",
                    confirm_instance_id=instance.instance_id,
                )
        self.assertFalse(instance.root.exists())
        purged = self.provisioner.purge(
            instance.instance_id,
            "purge-lost-response",
            confirm_instance_id=instance.instance_id,
        )
        self.assertEqual(purged["receipt"]["evidence"]["lifecycle_state"], "PURGED")

    def test_lifecycle_helpers_reject_malformed_and_unsafe_evidence(self) -> None:
        lifecycle = provisioner_module.system_instance_lifecycle
        malformed = self.base / "malformed.json"
        malformed.write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "unavailable"):
            lifecycle._read_json(malformed)
        malformed.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "invalid"):
            lifecycle._read_json(malformed)

        missing = self.base / "missing-tree"
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "unavailable"):
            lifecycle._tree_digest(missing)
        regular = self.base / "regular-root"
        regular.write_text("not-a-directory", encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "unsafe"):
            lifecycle._tree_digest(regular)

        tree = self.base / "unsafe-tree"
        tree.mkdir()
        (tree / "safe").write_text("safe", encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "cannot ignore"):
            lifecycle._tree_digest(tree, ignored_roots=(tree,))
        (tree / "unsafe-link").symlink_to(tree / "safe")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "unsafe entry"):
            lifecycle._tree_digest(tree)

        lock = self.base / "lifecycle.lock"
        with lifecycle._Lock(lock):
            with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "another operation"):
                with lifecycle._Lock(lock):
                    self.fail("a second lifecycle lock unexpectedly succeeded")

    def test_lifecycle_tree_ownership_rejects_hardlinks_and_permissive_modes(self) -> None:
        lifecycle = provisioner_module.system_instance_lifecycle
        tree = self.base / "ownership-tree"
        tree.mkdir(mode=0o700)
        safe = tree / "safe"
        safe.write_text("safe", encoding="utf-8")
        safe.chmod(0o600)
        self.assertTrue(lifecycle._tree_digest(tree).startswith("sha256:"))

        foreign = self.base / "foreign-hardlink-source"
        foreign.write_text("foreign", encoding="utf-8")
        linked = tree / "linked"
        os.link(foreign, linked)
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "hardlinked"):
            lifecycle._tree_digest(tree)
        linked.unlink()

        for target in (tree, safe):
            original = target.stat().st_mode & 0o777
            target.chmod(original | 0o020)
            with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "group/world writable"):
                lifecycle._tree_digest(tree)
            target.chmod(original)

        directory = tree / "mutable-dir"
        directory.mkdir(mode=0o700)
        directory.chmod(0o777)
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "group/world writable"):
            lifecycle._tree_digest(tree)

    def test_preserve_restore_and_purge_reject_unsafe_tree_ownership(self) -> None:
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")

        foreign = self.base / "foreign-owned"
        foreign.write_text("foreign", encoding="utf-8")
        linked = instance.data_root / "foreign-hardlink"
        os.link(foreign, linked)
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "hardlinked",
        ):
            self.provisioner.preserve(
                instance.instance_id,
                "preserve-hardlink-0001",
                confirm_instance_id=instance.instance_id,
            )
        self.assertTrue(instance.root.is_dir())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")
        linked.unlink()

        preserved = self.provisioner.preserve(
            instance.instance_id,
            "preserve-safe-0001",
            confirm_instance_id=instance.instance_id,
        )
        self.assertEqual(
            preserved["receipt"]["evidence"]["lifecycle_state"],
            "UNINSTALLED_DATA_PRESERVED",
        )

        marker = instance.data_root / "server.json"
        original_mode = marker.stat().st_mode & 0o777
        marker.chmod(original_mode | 0o002)
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "group/world writable",
        ):
            self.provisioner.restore(
                instance.instance_id,
                "restore-mode-drift-0001",
                preserve_operation_id="preserve-safe-0001",
                release=self.release,
            )
        marker.chmod(original_mode)

        mutable = instance.data_root / "purge-hardlink"
        os.link(foreign, mutable)
        with self.assertRaisesRegex(
            provisioner_module.system_instance_lifecycle.SystemInstanceLifecycleError,
            "hardlinked",
        ):
            self.provisioner.purge(
                instance.instance_id,
                "purge-hardlink-0001",
                confirm_instance_id=instance.instance_id,
            )
        self.assertTrue(instance.root.is_dir())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")

    def test_lifecycle_operation_identity_and_terminal_evidence_fail_closed(self) -> None:
        lifecycle = provisioner_module.system_instance_lifecycle
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        engine = self.provisioner._instance_lifecycle()
        descriptor = json.loads(instance.descriptor.read_text(encoding="utf-8"))

        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "operation ID"):
            engine.status(instance.instance_id, "bad operation")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "confirmation mismatch"):
            engine.preserve(
                instance, descriptor, "preserve-confirmation", confirm_instance_id="ep-development-0001"
            )

        preserved = self.provisioner.preserve(
            instance.instance_id,
            "preserve-negative-0001",
            confirm_instance_id=instance.instance_id,
        )
        self.assertEqual(
            self.provisioner.preserve(
                instance.instance_id,
                "preserve-negative-0001",
                confirm_instance_id=instance.instance_id,
            ),
            preserved,
        )
        state_path = engine._operation_root(instance.instance_id, "preserve-negative-0001") / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["request_digest"] = "sha256:" + "0" * 64
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "operation identity conflict"):
            engine.preserve(
                instance, descriptor, "preserve-negative-0001", confirm_instance_id=instance.instance_id
            )

    def test_lifecycle_provider_and_service_drift_fail_closed(self) -> None:
        lifecycle = provisioner_module.system_instance_lifecycle
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        descriptor = json.loads(instance.descriptor.read_text(encoding="utf-8"))
        engine = self.provisioner._instance_lifecycle()

        codex = system_provider_context.provider_context(instance, "codex")
        auth = json.loads(codex.auth_receipt.read_text(encoding="utf-8"))
        auth["state"] = "FOREIGN"
        codex.auth_receipt.write_text(json.dumps(auth), encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "cannot be preserved"):
            engine.preserve(
                instance, descriptor, "preserve-bad-auth", confirm_instance_id=instance.instance_id
            )

        auth["state"] = "READY"
        codex.auth_receipt.write_text(json.dumps(auth), encoding="utf-8")
        self.controller.loaded_instances.add(instance.instance_id)
        original_remove = self.controller.remove
        with patch.object(self.controller, "remove", return_value={"result": "REMOVED"}):
            with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "not fully removed"):
                engine.preserve(
                    instance, descriptor, "preserve-loaded", confirm_instance_id=instance.instance_id
                )
        self.controller.remove = original_remove

    def test_lifecycle_tombstone_and_restore_replay_paths(self) -> None:
        lifecycle = provisioner_module.system_instance_lifecycle
        self._create("ep-production-0001", "EP production", 8765)
        instance = self._instance("ep-production-0001", "EP production")
        descriptor = json.loads(instance.descriptor.read_text(encoding="utf-8"))
        preserved = self.provisioner.preserve(
            instance.instance_id,
            "preserve-replay-0001",
            confirm_instance_id=instance.instance_id,
        )
        restored = self.provisioner.restore(
            instance.instance_id,
            "restore-replay-0001",
            preserve_operation_id="preserve-replay-0001",
            release=self.release,
        )
        self.assertEqual(
            self.provisioner.restore(
                instance.instance_id,
                "restore-replay-0001",
                preserve_operation_id="preserve-replay-0001",
                release=self.release,
            ),
            restored,
        )

        engine = self.provisioner._instance_lifecycle()
        tombstone = engine._root(instance.instance_id) / "purged.json"
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "permanently purged"):
            engine.preserve(
                instance, descriptor, "preserve-after-purge", confirm_instance_id=instance.instance_id
            )
        with self.assertRaisesRegex(lifecycle.SystemInstanceLifecycleError, "cannot be restored"):
            engine.restore(
                instance,
                descriptor,
                "restore-after-purge",
                "preserve-replay-0001",
                self.release,
                self.provisioner._install_slot,
            )
        self.assertEqual(
            preserved["receipt"]["evidence"]["provider_auth_state"],
            "PRESERVED_REQUIRES_REVERIFICATION",
        )



if __name__ == "__main__":
    unittest.main()
