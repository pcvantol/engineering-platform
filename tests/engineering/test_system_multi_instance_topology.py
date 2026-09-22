from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context


class SystemMultiInstanceTopologyTests(unittest.TestCase):
    def test_two_instances_share_only_the_immutable_digest_slot(self) -> None:
        with TemporaryDirectory() as temporary:
            product = topology.system_installation_topology(
                Path(temporary) / "Engineering Platform",
            )
            production = topology.system_instance_topology(
                product,
                instance_id="ep-production-0001",
                display_label="EP production",
            )
            development = topology.system_instance_topology(
                product,
                instance_id="ep-development-0001",
                display_label="EP development",
            )
            release = topology.ReleaseIdentity(
                "2.3.102", "sha256:" + "a" * 64, "b" * 40,
            )

            self.assertEqual(
                topology.runtime_slot(product, release),
                topology.runtime_slot(production.product, release),
            )
            self.assertNotEqual(production.root, development.root)
            self.assertNotEqual(production.data_root, development.data_root)
            self.assertNotEqual(production.lifecycle_lock, development.lifecycle_lock)
            self.assertNotEqual(production.service_label, development.service_label)
            self.assertNotEqual(production.service_account, development.service_account)
            self.assertEqual(production.artifact_lock, development.artifact_lock)
            system_provider_context.assert_distinct(production, development)

            first = topology.plan_instance_update(
                production,
                operation_id="update-production-0001",
                release=release,
                current_interpreter=Path(temporary) / "legacy" / "venv" / "bin" / "python",
            )
            second = topology.plan_instance_install(
                development,
                operation_id="install-development-0001",
                release=release,
            )
            self.assertNotEqual(first.operation_root, second.operation_root)
            self.assertNotEqual(first.recovery_root, second.recovery_root)
            self.assertEqual(first.target, second.target)
            self.assertEqual(first.payload()["coordination"]["other_instances_mutable_state"], "OUT_OF_SCOPE")

    def test_duplicate_identity_and_display_label_authority_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            product = topology.system_installation_topology(Path(temporary) / "EP")
            with self.assertRaisesRegex(topology.SystemInstallationTopologyError, "instance ID"):
                topology.system_instance_topology(
                    product, instance_id="UPPERCASE", display_label="EP production",
                )
            first = topology.system_instance_topology(
                product, instance_id="ep-production-0001", display_label="same label",
            )
            second = topology.system_instance_topology(
                product, instance_id="ep-production-0002", display_label="same label",
            )
            self.assertNotEqual(first.service_label, second.service_label)
            self.assertNotEqual(first.root, second.root)


if __name__ == "__main__":
    unittest.main()
