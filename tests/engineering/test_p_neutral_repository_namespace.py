from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src" / "engineering_platform"


class PNeutralRepositoryNamespaceTests(unittest.TestCase):
    def test_active_source_has_no_predecessor_environment_or_service_authority(self) -> None:
        allowed = {
            "central_store_migration.py": {
                "com.djconnect.engineering-dashboard",
                "com.djconnect.engineering-inbox",
            },
            "local_api.py": {"com.djconnect.engineering-local-api"},
            "platform_bootstrap.py": {".djconnect"},
            "producer.py": {"djconnect.producer_submission"},
            "server_relay.py": {"com.djconnect.engineering-dashboard-relay"},
        }
        for path in SOURCE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for retired in (
                "DJCONNECT_ENGINEERING_",
                "DJCONNECT_EVIDENCE_",
                "DJCONNECT_CONTEXT_ESCALATION_FILE",
                "com.djconnect.",
                "djconnect.producer_submission",
            ):
                if retired not in text:
                    continue
                self.assertIn(path.name, allowed, f"unclassified predecessor authority in {path.name}")
                for occurrence in (line.strip() for line in text.splitlines() if retired in line):
                    self.assertTrue(
                        any(value in occurrence for value in allowed[path.name]),
                        f"unclassified predecessor authority in {path.name}: {occurrence}",
                    )

    def test_current_lifecycle_orders_are_neutral_and_singular(self) -> None:
        from engineering_platform import central_store_migration

        self.assertEqual(central_store_migration.SERVICE_START_ORDER, ("com.engineeringplatform.dashboard-relay",))
        self.assertEqual(central_store_migration.SERVICE_STOP_ORDER, ("com.engineeringplatform.dashboard-relay",))

    def test_environment_namespace_is_canonical(self) -> None:
        from engineering_platform import producer, storage

        self.assertTrue(storage.ADMITTED_STORAGE_SCHEMA_ENVIRONMENT.startswith("ENGINEERING_PLATFORM_"))
        self.assertTrue(storage.ADMITTED_STORAGE_ROOT_ENVIRONMENT.startswith("ENGINEERING_PLATFORM_"))
        self.assertEqual(producer.ENVELOPE_CONTRACT_NAME, "engineering_platform.producer_submission")
        self.assertNotEqual(producer.ENVELOPE_CONTRACT_NAME, producer.LEGACY_ENVELOPE_CONTRACT_NAME)

    def test_active_workflows_have_no_predecessor_identity(self) -> None:
        workflows = ROOT / ".github" / "workflows"
        for path in workflows.glob("*.yml"):
            self.assertNotIn(
                "djconnect",
                path.read_text(encoding="utf-8").lower(),
                f"predecessor identity remains active in workflow {path.name}",
            )


if __name__ == "__main__":
    unittest.main()
