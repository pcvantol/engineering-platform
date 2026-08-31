from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class EngineeringOperationalDocumentationTest(unittest.TestCase):
    """Keep the operator-facing EP contract aligned with the local implementation."""

    def test_onboarding_describes_iCloud_as_transport_only(self) -> None:
        manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())
        retained = {rule["path"] for rule in manifest["path_rules"] if rule["classification"] == "DJCONNECT_RETAINED"}
        self.assertIn("onboarding/README.md", retained)

    def test_onboarding_installs_watcher_and_dashboard_together(self) -> None:
        manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())
        retained = {rule["path"] for rule in manifest["path_rules"] if rule["classification"] == "DJCONNECT_RETAINED"}
        self.assertIn("onboarding/dev_onboarding_macos.sh", retained)

    def test_clean_slate_adr_preserves_extraction_provenance(self) -> None:
        adr = (ROOT / "docs/adr/0026-ep-clean-slate-standalone-store-and-migration-retirement.md").read_text(encoding="utf-8")
        self.assertIn("ADR-0026", adr)
        self.assertIn("RETIRED_FOR_CLEAN_SLATE_EXTRACTION", adr)

    def test_local_runner_and_dashboard_docs_use_canonical_local_storage(self) -> None:
        runner = (ROOT / "docs" / "development" / "LOCAL_AGENT_RUNNER.md").read_text(encoding="utf-8")
        dashboard = (ROOT / "docs" / "engineering" / "LOCAL_DASHBOARD_SUPERVISOR.md").read_text(encoding="utf-8")
        self.assertIn(".engineering/status/status.json", runner)
        self.assertNotIn("DJConnect Engineering/status.json", runner)
        self.assertNotIn("DJConnect Engineering/Reports/", runner)
        self.assertIn("server-sent events", dashboard)
        self.assertIn("Private read-only AI advice", dashboard)
        self.assertIn("Runtime Provider", dashboard)
        self.assertIn("Codex CLI Version", dashboard)
        self.assertIn("its installation path", dashboard)
        self.assertIn("Dashboard module boundaries", dashboard)
        self.assertIn("dashboard_state.py", dashboard)
        self.assertIn("## Browser state and evidence views", dashboard)
        self.assertIn("independent filtering, sorting and\npagination", dashboard)
        self.assertIn("Promptgeschiedenis** is the sole entry point", dashboard)
        self.assertIn("near-fullscreen, read-only detail dialog", dashboard)
        self.assertIn("adjacent AI-chat glyph", dashboard)
        self.assertIn("private, run-scoped transcript", dashboard)
        self.assertIn("top-level, collapsible operational category immediately after **Technische\ndetails**", dashboard)
        self.assertIn("Playwright covers them with\nreal touch input one at a time", dashboard)
        self.assertIn("## Git workspace lock status", dashboard)
        self.assertIn("fails closed", dashboard)
        reporting = (ROOT / "docs" / "engineering" / "ENGINEERING_REPORTING.md").read_text(encoding="utf-8")
        self.assertIn("## Runtime provenance", reporting)
        self.assertIn("Reasoning Profile", reporting)
        self.assertIn("Configuration Profile", reporting)
        self.assertIn("## Private dashboard evidence access", reporting)
        self.assertIn("not in an editor", reporting)
        self.assertIn("matching Promptgeschiedenis\nrow", reporting)
        self.assertIn("near-fullscreen operational-detail dialog", reporting)
        self.assertIn("complete report and advisory analysis remain separate evidence\nactions", reporting)
        storage = (ROOT / "docs" / "engineering" / "ENGINEERING_STORAGE.md").read_text(encoding="utf-8")
        self.assertIn("Schema `9`", storage)
        self.assertIn("execution_receipts", storage)
        self.assertIn("engineering_schema_migrations", storage)
        self.assertIn("prompt_execution_history", storage)
        self.assertIn("dashboard user actions", storage)

    def test_local_runner_documents_orchestrator_module_boundaries(self) -> None:
        runner = (ROOT / "docs" / "development" / "LOCAL_AGENT_RUNNER.md").read_text(encoding="utf-8")
        self.assertIn("## Runner module boundaries", runner)
        self.assertIn("codex_observability.py", runner)
        self.assertIn("engineering_memory.py", runner)
        self.assertIn("live_status.py", runner)

    def test_execution_host_operations_documents_the_recent_host_increments(self) -> None:
        operations = (ROOT / "docs" / "engineering" / "EXECUTION_HOST_OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn("PRs #715–#723", operations)
        self.assertIn("Workspace Preflight", operations)
        self.assertIn("Workspace authorization", operations)
        self.assertIn("Configuration Resolver", operations)
        self.assertIn("Dismiss Execution", operations)
        self.assertIn("Retry Execution", operations)
        self.assertIn("Queue Recovery", operations)

    def test_execution_host_architecture_bounds_provider_interruption_recovery(self) -> None:
        architecture = (ROOT / "docs" / "engineering" / "EXECUTION_HOST_ARCHITECTURE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Provider interruption recovery boundary", architecture)
        self.assertIn("only within the existing run", architecture)
        self.assertIn("at most one automatic recovery attempt", architecture)
        self.assertIn("distinct invocation identity", architecture)
        self.assertIn("not a\nretry submission", architecture)
        self.assertIn("A second interruption or any ambiguous recovery evidence fails closed", architecture)

    def test_platform_architecture_handbook_bounds_local_consumer_api_to_loopback(self) -> None:
        handbook = (ROOT / "docs" / "engineering" / "ENGINEERING_PLATFORM_ARCHITECTURE_HANDBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("The Local Consumer API is loopback-only.", handbook)

    def test_roadmap_and_active_backlog_distinguish_completed_1_5_from_maintenance(self) -> None:
        roadmap = (ROOT / "docs" / "development" / "ENGINEERING_PLATFORM_ROADMAP.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## 1.5 — Platform Productization\n\nCompleted and operational.", roadmap)
        self.assertIn("## 1.6 — Repository Extraction Readiness\n\nPlanned.", roadmap)
        self.assertIn("## 2.0 — Versioned Platform Boundary\n\nIn review.", roadmap)
