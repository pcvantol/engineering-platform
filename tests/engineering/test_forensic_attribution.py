"""Qualification for deterministic, evidence-only forensic provenance attribution."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from engineering_platform.central_store_migration import main
from engineering_platform.forensic_attribution import (
    ForensicAttributionError,
    _digest,
    attribute_forensic_delta,
    canonical_attribution_json,
)


ROOT = Path(__file__).resolve().parents[2]


def _change(change_type: str, key: str, **fields: object) -> dict[str, object]:
    return {"change_type": change_type, "canonical_key": [key], "row_digest_candidate": "a" * 64, **fields}


def _report(*tables: dict[str, object]) -> dict[str, object]:
    report: dict[str, object] = {
        "report_version": "1.0", "migration_id": "fixture-migration",
        "baseline": {"sha256": "b" * 64}, "candidate": {"sha256": "c" * 64},
        "tables": list(tables), "summary": {}, "graph_edges": [],
    }
    report["report_digest"] = _digest(report)
    return report


class ForensicAttributionTests(unittest.TestCase):
    def _rows(self, result: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
        return {(str(row["table_name"]), str(row["canonical_key"][0])): row for row in result["rows"]}

    def test_proven_fixture_ingress_maintenance_and_projection_rules_are_independent(self) -> None:
        fixture_submission = "fixture-submission-1"
        report = _report(
            {"table_name": "execution_submissions", "changes": [
                _change("ADDED", "human-ingress-" + "a" * 40, submission_id="human-ingress-" + "a" * 40, producer_id="human:icloud"),
                _change("ADDED", fixture_submission, submission_id=fixture_submission),
            ]},
            {"table_name": "local_api_credentials", "changes": [
                _change("ADDED", "fixture-credential-1", credential_id="fixture-credential-1", consumer_id="fixture-consumer", project_id="fixture-project"),
            ]},
            {"table_name": "engineering_component_logs", "changes": [_change("REMOVED", "log-1")]},
            {"table_name": "engineering_metadata", "changes": [
                _change("MODIFIED", "database_maintenance.last_attempt_at"),
                _change("MODIFIED", "dashboard_configuration.log_level"),
            ]},
            {"table_name": "execution_projections", "changes": [_change("MODIFIED", "live_status")]},
            {"table_name": "backup_probe", "changes": [_change("ADDED", "marker-41")]},
        )
        result = attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a")
        repeat = attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a")
        self.assertEqual(canonical_attribution_json(result), canonical_attribution_json(repeat))
        self.assertEqual(result["report_digest"], repeat["report_digest"])
        rows = self._rows(result)
        human = rows[("execution_submissions", "human-ingress-" + "a" * 40)]
        self.assertEqual((human["ancestry_origin"], human["writer_origin"], human["state_semantics"], human["evidence_status"]), ("PRODUCTION", "PRODUCTION_RUNTIME", "IMMUTABLE_BUSINESS_STATE", "PROVEN"))
        fixture = rows[("execution_submissions", fixture_submission)]
        self.assertEqual((fixture["ancestry_origin"], fixture["writer_origin"], fixture["evidence_status"]), ("TEST_HARNESS", "TEST_HARNESS", "PROVEN"))
        authority = rows[("local_api_credentials", "fixture-credential-1")]
        self.assertEqual((authority["writer_origin"], authority["state_semantics"]), ("TEST_HARNESS", "CONTROL_STATE"))
        logs = rows[("engineering_component_logs", "log-1")]
        self.assertEqual((logs["writer_origin"], logs["state_semantics"], logs["evidence_status"]), ("MAINTENANCE", "RETENTION_STATE", "PROVEN"))
        self.assertEqual(rows[("engineering_metadata", "database_maintenance.last_attempt_at")]["writer_origin"], "MAINTENANCE")
        self.assertEqual(rows[("engineering_metadata", "dashboard_configuration.log_level")]["evidence_status"], "UNRESOLVED")
        self.assertEqual(rows[("execution_projections", "live_status")]["state_semantics"], "MUTABLE_PROJECTION")
        self.assertEqual(rows[("backup_probe", "marker-41")]["state_semantics"], "TEST_ONLY_STRUCTURE")

    def test_production_ancestry_does_not_override_test_writer(self) -> None:
        fixture_run = "fixture-run-1"
        report = _report({"table_name": "execution_lifecycle_events", "changes": [
            _change("ADDED", "event-1", run_id=fixture_run),
        ]})
        result = attribute_forensic_delta(
            report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a",
            evidence_bindings={"run:" + fixture_run: {"ancestry_origin": "PRODUCTION", "evidence": [{"source": "immutable-ingress-envelope.json", "signals": ["canonical_production_ancestry"]}]}},
        )
        row = result["rows"][0]
        self.assertEqual(row["ancestry_origin"], "PRODUCTION")
        self.assertEqual(row["writer_origin"], "TEST_HARNESS")
        self.assertEqual(row["evidence_status"], "PROVEN")

    def test_operator_and_forge_receipts_can_prove_only_the_bound_component(self) -> None:
        report = _report(
            {"table_name": "managed_governance_gates", "changes": [_change("ADDED", "operator-gate")]},
            {"table_name": "execution_artifact_records", "changes": [_change("ADDED", "forge-artifact")]},
            {"table_name": "execution_artifact_records", "changes": [_change("ADDED", "unbound-artifact")]},
        )
        bindings = {
            "row:managed_governance_gates:[\"operator-gate\"]": {
                "ancestry_origin": "OPERATOR", "writer_origin": "OPERATOR_CONTROL", "state_semantics": "CONTROL_STATE",
                "evidence": [{"source": "operator-receipt.json", "signals": ["immutable_operator_receipt"]}],
            },
            "row:execution_artifact_records:[\"forge-artifact\"]": {
                "ancestry_origin": "FORGE", "writer_origin": "FORGE_CONTROL", "state_semantics": "EXECUTION_EVIDENCE",
                "evidence": [{"source": "forge-producer-record.json", "signals": ["canonical_forge_binding"]}],
            },
        }
        result = attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a", evidence_bindings=bindings)
        rows = self._rows(result)
        operator = rows[("managed_governance_gates", "operator-gate")]
        forge = rows[("execution_artifact_records", "forge-artifact")]
        orphan = rows[("execution_artifact_records", "unbound-artifact")]
        self.assertEqual((operator["ancestry_origin"], operator["writer_origin"], operator["evidence_status"]), ("OPERATOR", "OPERATOR_CONTROL", "PROVEN"))
        self.assertEqual((forge["ancestry_origin"], forge["writer_origin"], forge["evidence_status"]), ("FORGE", "FORGE_CONTROL", "PROVEN"))
        self.assertEqual((orphan["writer_origin"], orphan["evidence_status"]), ("UNKNOWN", "UNRESOLVED"))

    def test_negative_evidence_never_becomes_writer_proof(self) -> None:
        timestamp = "2026-08-31T12:00:00Z"
        production_run = "historic-" + "production-run"
        test_name = "test-" + "looking-row"
        report = _report({"table_name": "execution_artifact_records", "changes": [
            _change("ADDED", timestamp, run_id=production_run, updated_at=timestamp),
            _change("ADDED", test_name, label=test_name),
        ]})
        result = attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a")
        for row in result["rows"]:
            self.assertEqual(row["writer_origin"], "UNKNOWN")
            self.assertEqual(row["evidence_status"], "UNRESOLVED")
        self.assertEqual(result["rows"][0]["state_semantics"], "EXECUTION_EVIDENCE")

    def test_candidate_only_probe_is_a_schema_finding_not_an_invented_row_delta(self) -> None:
        report = _report({"table_name": "backup_probe", "changes": []})
        report["schema_difference"] = {"tables_candidate_only": ["backup_probe"]}
        report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
        result = attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest=report["report_digest"], repository_revision="revision-a")
        self.assertEqual(result["summary"]["changed_row_count"], 0)
        self.assertEqual(result["schema_findings"][0]["state_semantics"], "TEST_ONLY_STRUCTURE")
        self.assertEqual(result["schema_findings"][0]["writer_origin"], "TEST_HARNESS")

    def test_report_digest_mismatch_fails_closed_and_cli_writes_requested_file(self) -> None:
        report = _report({"table_name": "execution_submissions", "changes": []})
        with self.assertRaisesRegex(ForensicAttributionError, "FORENSIC_REPORT_DIGEST_MISMATCH"):
            attribute_forensic_delta(report, repository_root=ROOT, expected_report_digest="0" * 64, repository_revision="revision-a")
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "forensic-delta.json", Path(temporary) / "forensic-attribution.json"
            source.write_text(json.dumps(report), encoding="utf-8")
            stream = StringIO()
            with redirect_stdout(stream):
                status = main(["forensic-attribution", "--repo", str(ROOT), "--report", str(source), "--expected-report-digest", report["report_digest"], "--json", "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stream.getvalue()), json.loads(output.read_text(encoding="utf-8")))
