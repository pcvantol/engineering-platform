from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from engineering_platform import dashboard_state
from engineering_platform.drift_diagnostics import DRIFT_CATEGORIES, evidence_for_checks, guidance, persist, summary
from engineering_platform.host_preflight import HostPreflightCheck


class DriftDiagnosticsTest(unittest.TestCase):
    def test_every_canonical_category_is_supported(self) -> None:
        identifiers = {
            "Runtime Database": ("telemetry_storage", "Execution Host Preflight"),
            "Runtime Identity": ("host_identity", "Execution Host Preflight"),
            "Runtime Schema": ("storage_schema", "Capability Preflight"),
            "Execution Host Version": ("execution_host_version", "Capability Preflight"),
            "Bootstrap Contract": ("bootstrap_contract", "Capability Preflight"),
            "Checkpoint Format": ("checkpoint_format", "Capability Preflight"),
            "Memory Format": ("memory_format", "Capability Preflight"),
            "Report Format": ("report_format", "Capability Preflight"),
            "Configuration": ("configuration", "Execution Host Preflight"),
            "Workspace": ("target_repository", "Workspace Preflight"),
            "Repository": ("git_repository", "Workspace Preflight"),
            "Capability": ("required_capabilities", "Capability Preflight"),
            "Producer Contract": ("producer_contract", "Capability Preflight"),
            "Execution Policy": ("execution_mode", "Capability Preflight"),
        }
        self.assertEqual(set(identifiers), DRIFT_CATEGORIES)
        for category, (identifier, stage) in identifiers.items():
            with self.subTest(category=category):
                item = evidence_for_checks(
                    [HostPreflightCheck(identifier, "FAIL", "actual value", "repair it")],
                    stage=stage, repository="/repository",
                )[0]
                self.assertEqual(item.category, category)
                self.assertEqual(item.severity, "BLOCKING")
                self.assertTrue(item.expected_value)
                self.assertEqual(item.observed_value, "actual value")
                self.assertEqual(item.resolution_recommendation, "repair it")

    def test_evidence_is_immutable_and_guidance_is_actionable(self) -> None:
        evidence = evidence_for_checks(
            [HostPreflightCheck("runtime_executable", "FAIL", "CLI unavailable", "Install Codex.")],
            stage="Execution Host Preflight", repository="/repository",
        )
        with tempfile.TemporaryDirectory() as temporary:
            stored = persist(Path(temporary), evidence)
            path = Path(temporary) / ".engineering" / "drift-evidence" / f"{evidence[0].drift_id}.json"
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text()), stored[0])
        self.assertIn("Expected:", summary(stored))
        self.assertEqual(guidance(stored), {
            "retry_appropriate": True, "resume_appropriate": False,
            "operator_intervention_required": True, "prerequisite": "Install Codex.",
        })

    def test_dashboard_snapshot_projects_current_drift_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            evidence = {
                "drift_id": "drift-1", "severity": "BLOCKING", "affected_component": "disk_space",
                "expected_value": "disk_space: PASS", "observed_value": "disk low",
                "resolution_recommendation": "Free disk space.",
            }
            (status / "host_preflight.json").write_text(json.dumps({"drift_evidence": [evidence]}))
            payload = json.loads(dashboard_state.snapshot(
                root, status_reader=lambda _: b"{}", unavailable_reader=dashboard_state.unavailable_status,
                prompt_started_reader=lambda _: b"{}", usage_reader=lambda _: b"{}", rate_limits_reader=lambda: b"{}",
                usage_for_run_reader=lambda _, __: b"{}", completion_commits_reader=lambda _: b"{}",
                last_executed_commits_reader=lambda _: b"{}", reviewer_agents_reader=lambda _, __: b"[]",
                execution_reader=lambda _, __: b"{}", runtime_metadata_reader=lambda _, __: b"{}",
                report_analysis_available_reader=lambda _, __: False, telemetry_reader=lambda _: [],
                process_metrics_reader=lambda: b"{}", build_commit_reader=lambda _: "", component_log_versions_reader=lambda _: {},
                dashboard_version="test", worker_version="test",
            ))
        self.assertEqual(payload["current_drift"], evidence)
        self.assertTrue(payload["resume_guidance"]["operator_intervention_required"])
