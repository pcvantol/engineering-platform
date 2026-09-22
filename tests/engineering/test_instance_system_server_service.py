from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import pwd
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context
from engineering_platform import system_server_service as service


class InstanceSystemServerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.product = topology.system_installation_topology(self.base / "EP")
        self.launch_daemons = self.base / "LaunchDaemons"
        self.launch_daemons.mkdir()
        self.accounts = {
            "ep-production-0001": "_ep_prod",
            "ep-development-0001": "_ep_dev",
        }

    def _account_lookup(self, name: str) -> SimpleNamespace:
        if name not in self.accounts.values():
            raise KeyError(name)
        return SimpleNamespace(pw_uid=501)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _instance(self, identity: str, port: int) -> topology.SystemInstanceTopology:
        instance = topology.system_instance_topology(
            self.product,
            instance_id=identity,
            display_label=identity.replace("-", " "),
            service_account=self.accounts[identity],
        )
        instance.data_root.mkdir(parents=True)
        for context in system_provider_context.provider_contexts(instance):
            context.executable.parent.mkdir(parents=True, exist_ok=True)
            context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            context.executable.chmod(0o755)
            digest = system_provider_context.executable_digest(context.executable)
            system_provider_context.record_context(
                context,
                executable_sha256=digest,
                version="1.0.0",
                auth_reference=f"{context.provider}:auth:{identity}",
                auth_bootstrap_receipt=f"{context.provider}:receipt:{identity}",
            )
        interpreter = instance.root / "runtime" / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(Path(sys.executable))
        (interpreter.parent.parent / "pyvenv.cfg").write_text("home=/usr/bin\n", encoding="utf-8")
        definition = service.instance_service_definition(instance, interpreter=interpreter)
        paths = service.instance_default_paths(instance, self.launch_daemons)
        paths.plist_path.write_bytes(plistlib.dumps(service.instance_plist_payload(paths, definition)))
        descriptor = {
            "schema_version": 1,
            "instance_id": instance.instance_id,
            "display_label": instance.display_label,
            "service_label": instance.service_label,
            "service_account": instance.service_account,
            "data_root": str(instance.data_root),
            "endpoint": f"http://127.0.0.1:{port}",
            "selected_runtime": {"version": "2.3.102", "artifact_digest": "sha256:" + "a" * 64, "source_revision": "b" * 40, "interpreter": str(interpreter)},
            "provider_contexts": {
                context.provider: {"home": str(context.home), "executable": str(context.executable), "executable_sha256": system_provider_context.executable_digest(context.executable)}
                for context in system_provider_context.provider_contexts(instance)
            },
            "lifecycle_lock": str(instance.lifecycle_lock),
            "desired_state": "ACTIVE",
        }
        instance.descriptor.write_text(json.dumps(descriptor), encoding="utf-8")
        return instance

    def test_instance_plist_binds_identity_providers_and_no_login_home(self) -> None:
        first = self._instance("ep-production-0001", 8765)
        configured = service.configured_instance_service(
            first, launch_daemons_dir=self.launch_daemons,
            account_lookup=self._account_lookup,
        )
        self.assertIsNotNone(configured)
        assert configured is not None
        paths = service.instance_default_paths(first, self.launch_daemons)
        payload = plistlib.loads(paths.plist_path.read_bytes())
        self.assertEqual(payload["Label"], first.service_label)
        self.assertEqual(payload["UserName"], first.service_account)
        self.assertEqual(payload["ProgramArguments"][-2:], ["--expected-instance-id", first.instance_id])
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["EP_SERVER_INSTANCE_ID"], first.instance_id)
        self.assertEqual(environment["CODEX_HOME"], str(system_provider_context.provider_context(first, "codex").home))
        self.assertEqual(environment["GH_CONFIG_DIR"], str(system_provider_context.provider_context(first, "github").home))
        self.assertEqual(environment["HOME"], str(first.root / "service-home"))

    def test_inventory_accepts_two_instances_and_detects_real_port_collision(self) -> None:
        first = self._instance("ep-production-0001", 8765)
        second = self._instance("ep-development-0001", 8766)
        observed = service.instance_machine_inventory(
            self.product, launch_daemons_dir=self.launch_daemons,
            account_lookup=self._account_lookup,
        )
        self.assertEqual(observed["state"], "OBSERVED")
        self.assertEqual(observed["instance_count"], 2)
        self.assertFalse(observed["singleton_assumption"])
        self.assertEqual({entry["status"] for entry in observed["instances"]}, {"READY"})

        descriptor = json.loads(second.descriptor.read_text(encoding="utf-8"))
        descriptor["endpoint"] = "http://127.0.0.1:8765"
        second.descriptor.write_text(json.dumps(descriptor), encoding="utf-8")
        collided = service.instance_machine_inventory(
            self.product, launch_daemons_dir=self.launch_daemons,
            account_lookup=self._account_lookup,
        )
        self.assertEqual(collided["state"], "AMBIGUOUS")
        self.assertIn("DUPLICATE_ENDPOINT", {item["code"] for item in collided["conflicts"]})

        # A healthy second instance was never classified as a conflict before
        # the explicit endpoint collision was introduced.
        self.assertNotEqual(first.service_label, second.service_label)


if __name__ == "__main__":
    unittest.main()
