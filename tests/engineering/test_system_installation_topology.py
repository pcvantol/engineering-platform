from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform import system_installation_topology as topology_module


class SystemInstallationTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.system_root = self.base / "EP System Root With Spaces"
        self.release = topology_module.ReleaseIdentity(
            version="2.3.3",
            artifact_digest="sha256:" + "a" * 64,
            source_revision="b" * 40,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _topology(self) -> topology_module.SystemInstallationTopology:
        return topology_module.system_installation_topology(self.system_root)

    def test_derives_a_disjoint_machine_tree_without_creating_it(self) -> None:
        topology = self._topology()

        root = self.system_root.resolve()
        self.assertFalse(self.system_root.exists())
        self.assertEqual(topology.system_root, root)
        self.assertEqual(topology.data_root, root / "data")
        self.assertEqual(topology.runtime_root, root / "runtimes")
        self.assertEqual(topology.operations_root, root / "operations")
        self.assertEqual(topology.recovery_root, root / "recovery")
        self.assertEqual(
            topology.machine_lock, root / "locks" / "engineering-platform-server.lock"
        )
        self.assertEqual(topology.service_label, "com.engineeringplatform.server")
        self.assertEqual(topology.service_account, "_engineeringplatform")
        self.assertEqual(
            len(
                {
                    topology.data_root,
                    topology.runtime_root,
                    topology.operations_root,
                    topology.recovery_root,
                    topology.machine_lock.parent,
                }
            ),
            5,
        )
        self.assertFalse(self.system_root.exists())

    def test_normalizes_an_existing_symlink_alias_without_blessing_it_as_a_writer(
        self,
    ) -> None:
        physical = self.base / "physical system root"
        physical.mkdir()
        alias = self.base / "system root alias"
        alias.symlink_to(physical, target_is_directory=True)

        topology = topology_module.system_installation_topology(alias)

        self.assertEqual(topology.system_root, physical.resolve())
        self.assertFalse((physical / "runtimes").exists())

    def test_normalizes_tmp_aliases_without_creating_a_product_tree(self) -> None:
        root = Path("/tmp") / f"ep-topology-{self.base.name}"

        topology = topology_module.system_installation_topology(root)

        self.assertEqual(topology.system_root, root.resolve(strict=False))
        self.assertFalse(root.resolve(strict=False).exists())

    def test_rejects_unsafe_or_invalid_topology_inputs(self) -> None:
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "absolute"
        ):
            topology_module.system_installation_topology("relative-root")
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "filesystem root"
        ):
            topology_module.system_installation_topology("/")
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "service account"
        ):
            topology_module.system_installation_topology(
                self.system_root, service_account="root"
            )
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "service account"
        ):
            topology_module.system_installation_topology(
                self.system_root, service_account="not/a/user"
            )
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "unavailable"
        ):
            topology_module.system_installation_topology([])  # type: ignore[arg-type]

    def test_release_identity_is_closed_and_validated(self) -> None:
        self.assertEqual(
            topology_module.ReleaseIdentity.from_payload(self.release.payload()),
            self.release,
        )
        for kwargs in (
            {
                "version": "2.3",
                "artifact_digest": self.release.artifact_digest,
                "source_revision": self.release.source_revision,
            },
            {
                "version": self.release.version,
                "artifact_digest": "sha256:" + "A" * 64,
                "source_revision": self.release.source_revision,
            },
            {
                "version": self.release.version,
                "artifact_digest": self.release.artifact_digest,
                "source_revision": "not-a-revision",
            },
        ):
            with self.assertRaisesRegex(
                topology_module.SystemInstallationTopologyError, "release identity"
            ):
                topology_module.ReleaseIdentity(**kwargs)
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "release identity"
        ):
            topology_module.ReleaseIdentity.from_payload({"version": "2.3.3"})

    def test_runtime_slot_is_digest_pinned_and_is_final_not_operation_scoped(
        self,
    ) -> None:
        topology = self._topology()
        first = topology_module.runtime_slot(topology, self.release)
        second = topology_module.runtime_slot(topology, self.release)
        different = topology_module.runtime_slot(
            topology,
            topology_module.ReleaseIdentity(
                version="2.3.3",
                artifact_digest="sha256:" + "c" * 64,
                source_revision="d" * 40,
            ),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.slot_id, "sha256-" + "a" * 64)
        self.assertEqual(first.root, topology.runtime_root / "slots" / first.slot_id)
        self.assertEqual(first.venv, first.root / "venv")
        self.assertEqual(first.interpreter, first.root / "venv" / "bin" / "python")
        self.assertNotEqual(first.root, different.root)
        self.assertFalse(first.root.is_relative_to(topology.operations_root))
        self.assertFalse(first.root.is_relative_to(topology.recovery_root))
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "runtime slot inputs"
        ):
            topology_module.runtime_slot(object(), self.release)  # type: ignore[arg-type]

    def test_plan_install_is_pure_and_protects_runtime_and_recovery_paths(self) -> None:
        topology = self._topology()
        transition = topology_module.plan_install(
            topology,
            operation_id="install-0001",
            release=self.release,
        )
        payload = transition.payload()

        self.assertEqual(transition.mode, "INSTALL")
        self.assertIsNone(transition.current_interpreter)
        self.assertEqual(
            transition.operation_root, topology.operations_root / "install-0001"
        )
        self.assertEqual(
            transition.staging_targets,
            (
                transition.operation_root / "build",
                transition.operation_root / "download",
                transition.operation_root / "pip-cache",
            ),
        )
        self.assertEqual(
            transition.recovery_root, topology.recovery_root / "install-0001"
        )
        self.assertEqual(
            payload["activation"],
            {
                "service_label": topology.service_label,
                "interpreter": str(transition.target.interpreter),
                "state": "NOT_EXECUTED",
            },
        )
        cleanup = payload["cleanup"]
        self.assertEqual(
            cleanup["ephemeral_targets"],
            [str(path) for path in transition.staging_targets],
        )
        self.assertIn(str(topology.data_root), cleanup["protected_targets"])
        self.assertIn(str(topology.runtime_root), cleanup["protected_targets"])
        self.assertIn(str(transition.target.root), cleanup["protected_targets"])
        self.assertIn(str(transition.recovery_root), cleanup["protected_targets"])
        self.assertEqual(
            payload["recovery"]["retention"],
            "REQUIRED_UNTIL_EXPLICIT_RECOVERY_DISPOSITION",
        )
        self.assertFalse(self.system_root.exists())

    def test_plan_update_uses_the_same_machine_lock_across_old_data_roots(self) -> None:
        topology = self._topology()
        first = topology_module.plan_update(
            topology,
            operation_id="update-0001",
            release=self.release,
            current_interpreter=self.base
            / "old data root one"
            / "venv"
            / "bin"
            / "python",
        )
        second = topology_module.plan_update(
            topology,
            operation_id="update-0002",
            release=topology_module.ReleaseIdentity(
                version="2.3.4",
                artifact_digest="sha256:" + "e" * 64,
                source_revision="f" * 40,
            ),
            current_interpreter=self.base
            / "old data root two"
            / "venv"
            / "bin"
            / "python3.12",
        )

        self.assertEqual(first.mode, "UPDATE")
        self.assertEqual(
            first.current_interpreter,
            (self.base / "old data root one" / "venv" / "bin").resolve(strict=False)
            / "python",
        )
        self.assertEqual(first.topology.machine_lock, second.topology.machine_lock)
        self.assertNotEqual(first.operation_root, second.operation_root)
        self.assertNotEqual(first.target.root, second.target.root)

    def test_refuses_an_operation_candidate_or_protected_path_as_the_current_runtime(
        self,
    ) -> None:
        topology = self._topology()
        candidates = (
            topology.operations_root
            / "update-0001"
            / "candidate-venv"
            / "bin"
            / "python",
            topology.data_root / "venv" / "bin" / "python",
            topology.recovery_root / "update-0001" / "venv" / "bin" / "python",
        )
        for candidate in candidates:
            with (
                self.subTest(candidate=candidate),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError,
                    "eligible operational runtime",
                ),
            ):
                topology_module.plan_update(
                    topology,
                    operation_id="update-0001",
                    release=self.release,
                    current_interpreter=candidate,
                )

        topology.system_root.mkdir()
        topology.operations_root.mkdir()
        operation_alias = self.base / "operation alias"
        operation_alias.symlink_to(topology.operations_root, target_is_directory=True)
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError,
            "eligible operational runtime",
        ):
            topology_module.plan_update(
                topology,
                operation_id="update-0002",
                release=self.release,
                current_interpreter=operation_alias
                / "update-0002"
                / "candidate-venv"
                / "bin"
                / "python",
            )

    def test_refuses_a_non_venv_current_path_or_a_target_already_selected(self) -> None:
        topology = self._topology()
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "venv bin/python"
        ):
            topology_module.plan_update(
                topology,
                operation_id="update-0001",
                release=self.release,
                current_interpreter=self.base / "old-runtime" / "python",
            )
        target = topology_module.runtime_slot(topology, self.release)
        for launcher_name in ("python", "python3", "python3.12"):
            with (
                self.subTest(launcher_name=launcher_name),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError,
                    "eligible operational runtime|already selected",
                ),
            ):
                topology_module.plan_update(
                    topology,
                    operation_id="update-0001",
                    release=self.release,
                    current_interpreter=target.venv / "bin" / launcher_name,
                )

    def test_refuses_an_invalid_operation_id_before_any_path_can_be_used(self) -> None:
        for operation_id in ("short", "Update-0001"):
            with (
                self.subTest(operation_id=operation_id),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError, "operation ID"
                ),
            ):
                topology_module.plan_install(
                    self._topology(), operation_id=operation_id, release=self.release
                )

    def test_refuses_unclassified_system_tree_paths_as_current_runtime(self) -> None:
        topology = self._topology()
        for current_interpreter in (
            topology.machine_lock.parent / "surprise" / "venv" / "bin" / "python",
            topology.system_root / "logs" / "venv" / "bin" / "python",
            topology.runtime_root / "legacy" / "venv" / "bin" / "python",
            topology.runtime_root
            / "slots"
            / ("sha256-" + "c" * 64)
            / "venv"
            / "bin"
            / "python3",
        ):
            with (
                self.subTest(current_interpreter=current_interpreter),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError,
                    "eligible operational runtime",
                ),
            ):
                topology_module.plan_update(
                    topology,
                    operation_id="update-0001",
                    release=self.release,
                    current_interpreter=current_interpreter,
                )

        existing_slot = topology_module.runtime_slot(
            topology,
            topology_module.ReleaseIdentity(
                version="2.3.2",
                artifact_digest="sha256:" + "c" * 64,
                source_revision="d" * 40,
            ),
        )
        transition = topology_module.plan_update(
            topology,
            operation_id="update-0001",
            release=self.release,
            current_interpreter=existing_slot.interpreter,
        )
        self.assertEqual(transition.current_interpreter, existing_slot.interpreter)

    def test_transition_payload_round_trips_only_when_all_boundary_facts_match(
        self,
    ) -> None:
        transition = topology_module.plan_update(
            self._topology(),
            operation_id="update-0001",
            release=self.release,
            current_interpreter=self.base / "legacy" / "venv" / "bin" / "python",
        )
        payload = transition.payload()

        self.assertEqual(topology_module.transition_from_payload(payload), transition)

        mutations = (
            (
                ("target", "interpreter"),
                str(self.base / "foreign" / "venv" / "bin" / "python"),
            ),
            (("operation", "staging_targets"), [str(transition.target.root)]),
            (("recovery", "retention"), "DELETE_NOW"),
            (("activation", "service_label"), "foreign.service"),
            (("cleanup", "protected_targets"), []),
            (("topology", "service_label"), "foreign.service"),
        )
        for path, replacement in mutations:
            tampered = deepcopy(payload)
            cursor = tampered
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = replacement
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError,
                    "provisioning transition|system installation topology",
                ),
            ):
                topology_module.transition_from_payload(tampered)

    def test_transition_payload_rejects_invalid_mode_and_install_current_runtime(
        self,
    ) -> None:
        transition = topology_module.plan_install(
            self._topology(),
            operation_id="install-0001",
            release=self.release,
        )
        invalid_mode = deepcopy(transition.payload())
        invalid_mode["mode"] = "REPAIR"
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "provisioning transition"
        ):
            topology_module.transition_from_payload(invalid_mode)

        invalid_install = deepcopy(transition.payload())
        invalid_install["current_interpreter"] = str(
            self.base / "old" / "venv" / "bin" / "python"
        )
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "provisioning transition"
        ):
            topology_module.transition_from_payload(invalid_install)

        for non_integer_schema_version in (True, 1.0):
            with (
                self.subTest(non_integer_schema_version=non_integer_schema_version),
                self.assertRaisesRegex(
                    topology_module.SystemInstallationTopologyError,
                    "provisioning transition",
                ),
            ):
                invalid_schema_version = deepcopy(transition.payload())
                invalid_schema_version["schema_version"] = non_integer_schema_version
                topology_module.transition_from_payload(invalid_schema_version)

    def test_topology_payload_is_closed_and_canonical(self) -> None:
        topology = self._topology()
        self.assertEqual(
            topology_module.SystemInstallationTopology.from_payload(topology.payload()),
            topology,
        )
        tampered = topology.payload()
        tampered["machine_lock"] = str(self.base / "another.lock")
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError,
            "system installation topology",
        ):
            topology_module.SystemInstallationTopology.from_payload(tampered)

    def test_directly_constructed_topology_or_transition_cannot_bypass_validation(
        self,
    ) -> None:
        topology = self._topology()
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError,
            "system installation topology",
        ):
            replace(topology, machine_lock=self.base / "foreign.lock")

        transition = topology_module.plan_install(
            topology,
            operation_id="install-0001",
            release=self.release,
        )
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "provisioning transition"
        ):
            replace(transition, recovery_root=self.base / "foreign-recovery")
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "provisioning transition"
        ):
            replace(transition, mode=[])  # type: ignore[arg-type]

        slot = topology_module.runtime_slot(topology, self.release)
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "runtime slot"
        ):
            topology_module.RuntimeSlot(
                topology=topology,
                release=self.release,
                slot_id="forged-slot",
                root=self.base / "foreign-slot",
                venv=self.base / "foreign-slot" / "venv",
                interpreter=self.base / "foreign-slot" / "venv" / "bin" / "python",
            )
        with self.assertRaisesRegex(
            topology_module.SystemInstallationTopologyError, "runtime slot"
        ):
            replace(slot, root=self.base / "foreign-slot")


if __name__ == "__main__":
    unittest.main()
