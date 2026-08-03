from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class EngineeringOperationalDocumentationTest(unittest.TestCase):
    """Keep the operator-facing EP contract aligned with the local implementation."""

    def test_onboarding_describes_iCloud_as_transport_only(self) -> None:
        onboarding = (ROOT / "onboarding" / "README.md").read_text(encoding="utf-8")
        self.assertIn("iCloud is transport only.", onboarding)
        self.assertIn(".engineering/status/", onboarding)
        self.assertIn(".engineering/reports/", onboarding)
        self.assertIn(".engineering/engineering.db", onboarding)
        self.assertIn("WAITING_FOR_PREDECESSOR", onboarding)
        self.assertNotIn("local-run reports to `Reports`", onboarding)

    def test_onboarding_installs_watcher_and_dashboard_together(self) -> None:
        script = (ROOT / "onboarding" / "dev_onboarding_macos.sh").read_text(encoding="utf-8")
        self.assertIn("tools.engineering.inbox_watcher install", script)
        self.assertIn("tools.engineering.dashboard install", script)
        self.assertIn("iCloud is transport only; prompts, reports and status", script)
        self.assertNotIn("Reports: iCloud Drive/DJConnect Engineering/Reports", script)

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
        self.assertIn("Dashboard module boundaries", dashboard)
        self.assertIn("dashboard_state.py", dashboard)
        self.assertIn("## Browser state and evidence views", dashboard)
        self.assertIn("independent filtering, sorting and\npagination", dashboard)
        reporting = (ROOT / "docs" / "engineering" / "ENGINEERING_REPORTING.md").read_text(encoding="utf-8")
        self.assertIn("## Runtime provenance", reporting)
        self.assertIn("Reasoning Profile", reporting)
        self.assertIn("Configuration Profile", reporting)
        self.assertIn("## Private dashboard evidence access", reporting)
        self.assertIn("not in an editor", reporting)
        storage = (ROOT / "docs" / "engineering" / "ENGINEERING_STORAGE.md").read_text(encoding="utf-8")
        self.assertIn("schema `6`", storage)
        self.assertIn("engineering_schema_migrations", storage)
        self.assertIn("prompt_execution_history", storage)
        self.assertIn("dashboard user actions", storage)

    def test_local_runner_documents_orchestrator_module_boundaries(self) -> None:
        runner = (ROOT / "docs" / "development" / "LOCAL_AGENT_RUNNER.md").read_text(encoding="utf-8")
        self.assertIn("## Runner module boundaries", runner)
        self.assertIn("codex_observability.py", runner)
        self.assertIn("engineering_memory.py", runner)
        self.assertIn("live_status.py", runner)

    def test_roadmap_and_active_backlog_distinguish_completed_1_5_from_maintenance(self) -> None:
        roadmap = (ROOT / "docs" / "development" / "ENGINEERING_PLATFORM_ROADMAP.md").read_text(
            encoding="utf-8"
        )
        backlog = (ROOT / "PLATFORM_EVOLUTION_BACKLOG.md").read_text(encoding="utf-8")
        self.assertIn("## 1.5 — Platform Productization\n\nCompleted and operational.", roadmap)
        self.assertIn("## 1.6 — Repository Extraction Readiness\n\nPlanned.", roadmap)
        self.assertIn("Engineering Platform 1.5 operational hardening", backlog)
        self.assertIn("Legacy iCloud Engineering archive migration", backlog)
        self.assertIn("sole iCloud engineering folder", backlog)
