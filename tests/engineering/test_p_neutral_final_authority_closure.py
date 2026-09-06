from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from engineering_platform import central_store_migration, local_api
from engineering_platform.platform_components import (
    PLATFORM_COMPONENTS,
    SUPPORTED_SUBMISSION_INGRESS_COUNT,
    SUPPORTED_SUBMISSION_INGRESSES,
)
from tools.qualification import p_neutral_authority_guard as guard


ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src"


class PNeutralFinalAuthorityClosureTests(unittest.TestCase):
    def test_current_product_has_no_djconnect_authority(self) -> None:
        self.assertEqual(guard.violations(SOURCE), [])
        self.assertEqual(
            guard.report(SOURCE),
            {
                "CURRENT_AUTHORITY_VIOLATIONS": 0,
                "HISTORICAL_REFERENCE_FALSE_POSITIVES": 0,
                "LOCAL_API_SUPPORTED_INGRESS": False,
                "SUPPORTED_SUBMISSION_INGRESS_COUNT": 3,
                "findings": [],
            },
        )

    def test_current_component_and_ingress_inventory_is_neutral(self) -> None:
        self.assertEqual(SUPPORTED_SUBMISSION_INGRESSES, ("HTTP_JSON", "INSTALLED_CLI", "FILE_INBOX"))
        self.assertEqual(SUPPORTED_SUBMISSION_INGRESS_COUNT, 3)
        self.assertFalse(any(component.id == "local_api" for component in PLATFORM_COMPONENTS))
        self.assertFalse(any((component.lifecycle_label or "").startswith("com.djconnect.") for component in PLATFORM_COMPONENTS))
        self.assertEqual(central_store_migration.SERVICE_START_ORDER, ("com.engineeringplatform.dashboard-relay",))
        self.assertEqual(central_store_migration.SERVICE_STOP_ORDER, ("com.engineeringplatform.dashboard-relay",))
        status = local_api.retirement_status()
        self.assertEqual(status["state"], "RETIRED")
        self.assertFalse(status["installable"])
        self.assertFalse(status["lifecycle_owned"])
        self.assertFalse(status["supported_submission_ingress"])

    def test_new_djconnect_service_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            package = source / "engineering_platform"
            package.mkdir(parents=True)
            for name, literals in guard.HISTORICAL_SOURCE_LITERALS.items():
                (package / name).write_text("\n".join(literal for literal, count in literals.items() for _ in range(count)), encoding="utf-8")
            (package / "platform_components.py").write_text(
                'SUPPORTED_SUBMISSION_INGRESSES = ("HTTP_JSON", "INSTALLED_CLI", "FILE_INBOX")\n'
                "SUPPORTED_SUBMISSION_INGRESS_COUNT = len(SUPPORTED_SUBMISSION_INGRESSES)\n"
                'lifecycle_label = "com.djconnect.engineering-dashboard"\n', encoding="utf-8"
            )
            (package / "central_store_migration.py").write_text(
                'com.djconnect.engineering-dashboard\ncom.djconnect.engineering-inbox\n'
                'SERVICE_START_ORDER = ("com.engineeringplatform.dashboard-relay",)\n'
                'SERVICE_STOP_ORDER = ("com.engineeringplatform.dashboard-relay",)\nHISTORICAL_LOCK_IDENTITIES = {}\n', encoding="utf-8"
            )
            (package / "server_relay.py").write_text(
                'LEGACY_RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"\nPLATFORM_COMPONENT_BY_ID = {}\n', encoding="utf-8"
            )
            (package / "local_api.py").write_text(
                'HISTORICAL_LEGACY_LABEL = "com.djconnect.engineering-local-api"\n'
                'return {"state": "RETIRED", "installable": False}\n', encoding="utf-8"
            )
            (package / "producer.py").write_text('LEGACY_ENVELOPE_CONTRACT_NAME = "djconnect.producer_submission"\n', encoding="utf-8")
            (package / "platform_bootstrap.py").write_text('.djconnect\n.djconnect\n', encoding="utf-8")
            findings = guard.violations(source)
            self.assertIn("RETIRED_COMPONENT_IN_CURRENT_INVENTORY", findings)

    def test_new_retired_environment_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            package = source / "engineering_platform"
            package.mkdir(parents=True)
            (package / "new_runtime.py").write_text('value = "DJCONNECT_ENGINEERING_LOG_LEVEL"\n', encoding="utf-8")
            self.assertIn(
                "RETIRED_CONFIGURATION_AUTHORITY:new_runtime.py:DJCONNECT_ENGINEERING_",
                guard.violations(source),
            )

    def test_authority_register_documents_the_installer_boundary(self) -> None:
        register = (ROOT / "docs" / "engineering" / "P_NEUTRAL_FINAL_AUTHORITY_CLOSURE.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "P_INSTALLER_V1_PROFILE = EP_SERVER_ONLY",
            "com.djconnect.*",
            "Local Consumer API",
            "Forge",
            "Workspace",
            "Project Agent productization",
        ):
            self.assertIn(required, register)


if __name__ == "__main__":
    unittest.main()
