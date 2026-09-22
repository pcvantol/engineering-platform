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
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
