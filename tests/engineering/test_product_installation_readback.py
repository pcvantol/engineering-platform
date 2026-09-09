from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform import product_installation_readback
from engineering_platform.operational_installation import (
    launcher,
    record_status,
    resolve,
)
from engineering_platform.operational_installation_record import record


class ProductInstallationReadbackTests(unittest.TestCase):
    def _installation(
        self,
        directory: str,
        *,
        artifact_digest: str = "sha256:" + "a" * 64,
        source_revision: str = "b" * 40,
    ) -> tuple[Path, object, dict[str, object], dict[str, str], dict[str, object]]:
        root = Path(directory) / "EP Runtime With Spaces"
        root.mkdir()
        (root / "server.json").write_text(json.dumps({"version": 2, "product_version": "2.3.1"}))
        (root / "runtime-identity.json").write_text(json.dumps({"instance_id": "instance-1"}))
        interpreter = root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
        installation = resolve(root, interpreter=interpreter)
        record(
            root,
            installation_id="instance-1",
            version="2.3.1",
            channel="stable",
            artifact_digest=artifact_digest,
            source_revision=source_revision,
            interpreter=interpreter,
            roles={"server": "com.engineeringplatform.server"},
            desired_state="ACTIVE",
            observed_state="ACTIVE",
            verification={"result": "PASS"},
            cleanup={"result": "COMPLETE"},
        )
        package_root = root / "venv" / "site-packages" / "engineering_platform"
        metadata = root / "venv" / "site-packages" / "engineering_platform-2.3.1.dist-info"
        package = {
            "interpreter": str(interpreter.absolute()),
            "version": "2.3.1",
            "metadata": str(metadata),
            "package": str(package_root),
        }
        response = {
            "service": "engineering-platform-server",
            "instance_id": "instance-1",
            "healthy": True,
            "product_version": "2.3.1",
            "runtime_identity": {
                "interpreter": str(interpreter.absolute()),
                "executable": str(interpreter.absolute()),
                "package": str(package_root),
                "package_version": "2.3.1",
                "metadata": str(metadata),
                "artifact": {"state": "OBSERVED", "digest": artifact_digest},
            },
        }
        return root, installation, dict(record_status(installation)), package, response

    @staticmethod
    def _inventory(installation: object, *, conflicts: list[object] | None = None) -> dict[str, object]:
        # ``installation`` is kept opaque in test fixtures so the contract
        # does not accidentally accept a coordinator-supplied runtime path.
        interpreter = str(launcher(getattr(installation, "interpreter")))
        return {
            "selected_interpreter": interpreter,
            "coverage": "EXPLICIT_PATHS_ONLY",
            "scope": {
                "required": "MACOS_MACHINE",
                "observed": "CURRENT_OS_USER_EXPLICIT_REFERENCES_ONLY",
                "status": "INCOMPLETE",
            },
            "entries": [{"interpreter": interpreter, "status": "SELECTED"}],
            "conflicting_service_references": conflicts or [],
            "single_operational_installation": False,
            "single_operational_installation_status": "UNVERIFIED_SCOPE",
        }

    def test_active_readback_binds_actual_runtime_and_retains_partial_scope(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            observation = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
                inventory=self._inventory(installation),
            )
        self.assertEqual(observation["contract_version"], "1.0")
        self.assertEqual(observation["state"], "ACTIVE")
        self.assertEqual(observation["health_state"], "HEALTHY")
        self.assertEqual(observation["selected_runtime_identity"], package["interpreter"])
        self.assertEqual(observation["artifact"], {
            "version": "2.3.1",
            "digest": "sha256:" + "a" * 64,
            "source_revision": "b" * 40,
            "channel": "stable",
        })
        self.assertEqual((observation["inventory_coverage"], observation["conflict_state"]), ("PARTIAL", "UNKNOWN"))
        self.assertFalse(observation["single_operational_installation_verified"])

    def test_wrong_healthy_instance_and_unavailable_health_are_not_active(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            wrong_instance = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response={**response, "instance_id": "wrong-instance"},
                inventory=self._inventory(installation),
            )
            unavailable = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=None,
                inventory=self._inventory(installation),
            )
        self.assertEqual((wrong_instance["state"], wrong_instance["health_state"]), ("UNHEALTHY", "UNHEALTHY"))
        self.assertEqual(wrong_instance["evidence"]["health_qualification"], "FAILED")
        self.assertEqual(wrong_instance["evidence"]["health_failure"], "IDENTITY_MISMATCH")
        self.assertEqual((unavailable["state"], unavailable["health_state"]), ("UNHEALTHY", "UNHEALTHY"))
        self.assertEqual(unavailable["evidence"]["health_qualification"], "UNAVAILABLE")

    def test_absent_record_never_promotes_a_package_to_an_operational_runtime(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "EP Runtime"
            root.mkdir()
            (root / "server.json").write_text(json.dumps({"product_version": "2.3.1"}))
            (root / "runtime-identity.json").write_text(json.dumps({"instance_id": "instance-1"}))
            interpreter = root / "old PATH venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("#!/bin/sh\n")
            interpreter.chmod(0o755)
            installation = resolve(root, interpreter=interpreter)
            observation = product_installation_readback.readback(
                installation,
                record={"state": "UNREGISTERED"},
                package=None,
                health_response=None,
                inventory=self._inventory(installation),
            )
        self.assertEqual(observation["state"], "ABSENT")
        self.assertIsNone(observation["selected_runtime_identity"])
        self.assertIsNone(observation["artifact"])
        self.assertFalse(observation["single_operational_installation_verified"])

    def test_unavailable_owned_service_has_no_runtime_fallback(self) -> None:
        observation = product_installation_readback.unavailable_readback(
            reason="OWNED_SERVICE_INTERPRETER_UNAVAILABLE",
        )
        self.assertEqual((observation["state"], observation["health_state"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIsNone(observation["installation_identity"])
        self.assertIsNone(observation["selected_runtime_identity"])

    def test_explicit_conflicting_service_is_reported_without_machine_wide_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            conflict = {
                "interpreter": "/old/ep-2.3.0/bin/python",
                "status": "CONFLICTING_SERVICE_REFERENCE",
                "service_references": ["com.example.legacy"],
            }
            observation = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
                inventory=self._inventory(installation, conflicts=[conflict]),
            )
        self.assertEqual(observation["conflict_state"], "CONFLICTING")
        self.assertEqual(observation["evidence"]["inventory"]["conflicting_service_references"], [conflict])
        self.assertFalse(observation["single_operational_installation_verified"])

    def test_foreign_or_machine_wide_inventory_cannot_upgrade_ep_scope(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            inventory = self._inventory(installation)
            inventory["coverage"] = "MACHINE_WIDE"
            inventory["scope"] = {"required": "MACOS_MACHINE", "observed": "MACOS_MACHINE", "status": "COMPLETE"}
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "inventory"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package=package,
                    health_response=response,
                    inventory=inventory,
                )

    def test_malformed_qualification_never_falls_back_to_selected_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            with patch(
                "engineering_platform.product_installation_readback.operational_installation.qualify_runtime_response",
                return_value={"live": {}},
            ), self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "qualification"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package=package,
                    health_response=response,
                    inventory=self._inventory(installation),
                )

    def test_invalid_readback_inputs_fail_closed(self) -> None:
        """Incomplete EP-owned facts cannot be reinterpreted by a consumer."""
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "record"):
                product_installation_readback.readback(
                    installation,
                    record=[registered],  # type: ignore[arg-type]
                    package=package,
                    health_response=response,
                )
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "record state"):
                product_installation_readback.readback(
                    installation,
                    record={**registered, "state": "FOREIGN"},
                    package=package,
                    health_response=response,
                )
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "package identity"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package=None,
                    health_response=response,
                )
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "does not match"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package={**package, "version": "2.3.0"},
                    health_response=response,
                )
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "incomplete"):
                product_installation_readback.readback(
                    installation,
                    record={**registered, "channel": ""},
                    package=package,
                    health_response=response,
                )

    def test_inventory_and_unavailable_readback_reject_foreign_assertions(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            no_inventory = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
            )
            self.assertEqual((no_inventory["inventory_coverage"], no_inventory["conflict_state"]), ("PARTIAL", "UNKNOWN"))
            with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "inventory"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package=package,
                    health_response=response,
                    inventory="foreign inventory",  # type: ignore[arg-type]
                )
        with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "reason"):
            product_installation_readback.unavailable_readback(reason="PATH_RUNTIME")
        with self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "selected runtime"):
            product_installation_readback.unavailable_readback(
                reason="OWNED_SERVICE_INTERPRETER_UNAVAILABLE",
                inventory={"foreign": "inventory"},
            )

    def test_health_failures_and_malformed_qualification_remain_non_operational(self) -> None:
        with TemporaryDirectory() as temporary:
            _root, installation, registered, package, response = self._installation(temporary)
            unhealthy = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response={**response, "healthy": False},
            )
            missing_live_identity = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response={**response, "runtime_identity": None},
            )
            with patch(
                "engineering_platform.product_installation_readback.operational_installation.qualify_runtime_response",
                return_value={},
            ), self.assertRaisesRegex(product_installation_readback.ProductInstallationReadbackError, "qualification"):
                product_installation_readback.readback(
                    installation,
                    record=registered,
                    package=package,
                    health_response=response,
                )
        self.assertEqual(unhealthy["evidence"]["health_failure"], "SERVER_UNHEALTHY")
        self.assertEqual(missing_live_identity["evidence"]["health_failure"], "INVALID_HEALTH_IDENTITY")

    def test_assessment_requires_current_product_health_and_exact_source_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "candidate.whl"
            wheel.write_bytes(b"exact candidate bytes")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            root, installation, registered, package, response = self._installation(
                temporary,
                artifact_digest=digest,
            )
            active = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
                inventory=self._inventory(installation),
            )
            up_to_date = product_installation_readback.assess_update(
                root,
                operation_id="update-0001",
                artifact=wheel,
                target_version="2.3.1",
                target_digest=digest,
                target_source_revision="b" * 40,
                current_observation=active,
            )
            different_source = product_installation_readback.assess_update(
                root,
                operation_id="update-0002",
                artifact=wheel,
                target_version="2.3.1",
                target_digest=digest,
                target_source_revision="c" * 40,
                current_observation=active,
            )
            unavailable = product_installation_readback.assess_update(
                root,
                operation_id="update-0003",
                artifact=wheel,
                target_version="2.3.2",
                target_digest=digest,
                target_source_revision="c" * 40,
                current_observation={**active, "state": "UNHEALTHY", "health_state": "UNHEALTHY"},
            )
        self.assertEqual(up_to_date["state"], "UP_TO_DATE")
        self.assertEqual(up_to_date["evidence"]["current_source_revision"], "b" * 40)
        self.assertEqual(different_source["state"], "INCOMPATIBLE")
        self.assertEqual(unavailable["state"], "UNKNOWN")
        self.assertEqual(unavailable["evidence"]["reason"], "selected operational runtime is not qualified")

    def test_assessment_rejects_changed_or_missing_registered_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "candidate.whl"
            wheel.write_bytes(b"candidate bytes")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            root, installation, registered, package, response = self._installation(temporary)
            active = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
            )
            changed = product_installation_readback.assess_update(
                root,
                operation_id="update-0004",
                artifact=wheel,
                target_version="2.3.2",
                target_digest=digest,
                target_source_revision="c" * 40,
                current_observation={**active, "installation_identity": "stale-instance"},
            )
            missing = product_installation_readback.assess_update(
                root / "missing operational record",
                operation_id="update-0005",
                artifact=wheel,
                target_version="2.3.2",
                target_digest=digest,
                target_source_revision="c" * 40,
                current_observation=active,
            )
            wrong_digest = product_installation_readback.assess_update(
                root,
                operation_id="update-0006",
                artifact=wheel,
                target_version="2.3.2",
                target_digest="sha256:" + "d" * 64,
                target_source_revision="c" * 40,
                current_observation=active,
            )
        self.assertEqual(changed["evidence"]["reason"], "registered installation changed before update assessment")
        self.assertEqual(missing["installation_identity"], "instance-1")
        self.assertEqual(wrong_digest["state"], "UNKNOWN")
        self.assertEqual(wrong_digest["evidence"]["reason"], "target artifact does not match the requested digest")

    def test_forward_exact_candidate_is_only_an_assessment_not_an_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "candidate.whl"
            wheel.write_bytes(b"forward candidate bytes")
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            root, installation, registered, package, response = self._installation(temporary)
            active = product_installation_readback.readback(
                installation,
                record=registered,
                package=package,
                health_response=response,
                inventory=self._inventory(installation),
            )
            assessment = product_installation_readback.assess_update(
                root,
                operation_id="update-0001",
                artifact=wheel,
                target_version="2.3.2",
                target_digest=digest,
                target_source_revision="c" * 40,
                current_observation=active,
            )
        self.assertEqual(assessment["state"], "UPDATE_AVAILABLE")
        self.assertEqual(assessment["installation_identity"], "instance-1")
        self.assertEqual(assessment["evidence"]["assessment"], "PREPARED")


if __name__ == "__main__":
    unittest.main()
