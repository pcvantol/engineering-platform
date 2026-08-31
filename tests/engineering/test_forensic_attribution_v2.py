"""Qualification for V2 attribution enrichment from immutable V1 JSON."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from tools.engineering.central_store_migration import main
from tools.engineering.forensic_attribution_v2 import (
    ForensicAttributionV2Error,
    _digest,
    canonical_attribution_v2_json,
    enrich_attribution_v2,
)


ROOT = Path(__file__).resolve().parents[2]


def _v1(rows: list[dict[str, object]]) -> dict[str, object]:
    report: dict[str, object] = {
        "attribution_version": "1.0", "input_report": {"report_digest": "d" * 64},
        "repository_revision": "v1-revision", "summary": {}, "schema_findings": [], "rows": rows,
    }
    report["report_digest"] = _digest(report)
    return report


class ForensicAttributionV2Tests(unittest.TestCase):
    def test_direct_committed_component_literal_proves_test_writer_only_for_that_row(self) -> None:
        fixture_run = "v2-fixture-run"
        report = _v1([
            {"table_name": "execution_pr_evidence_backfills", "canonical_key": [1], "component_id": "run:" + fixture_run, "ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": "EXECUTION_EVIDENCE", "evidence_status": "UNRESOLVED", "rule_id": "NO_POSITIVE_EVIDENCE", "evidence": []},
            {"table_name": "execution_pr_evidence_backfills", "canonical_key": [2], "component_id": "run:" + ("v2-" + "test-looking"), "ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": "EXECUTION_EVIDENCE", "evidence_status": "UNRESOLVED", "rule_id": "NO_POSITIVE_EVIDENCE", "evidence": []},
        ])
        result = enrich_attribution_v2(report, repository_root=ROOT, expected_attribution_digest=report["report_digest"], repository_revision="v2-revision")
        proven, unknown = result["rows"]
        self.assertEqual((proven["ancestry_origin"], proven["writer_origin"], proven["evidence_status"]), ("TEST_HARNESS", "TEST_HARNESS", "PROVEN"))
        self.assertEqual(proven["state_semantics"], "EXECUTION_EVIDENCE")
        self.assertEqual((unknown["writer_origin"], unknown["evidence_status"]), ("UNKNOWN", "UNRESOLVED"))
        self.assertEqual(result["summary"]["v2_resolved_count"], 1)

    def test_digest_is_deterministic_mismatch_fails_closed_and_cli_writes_requested_file(self) -> None:
        report = _v1([])
        first = enrich_attribution_v2(report, repository_root=ROOT, expected_attribution_digest=report["report_digest"], repository_revision="v2-revision")
        second = enrich_attribution_v2(report, repository_root=ROOT, expected_attribution_digest=report["report_digest"], repository_revision="v2-revision")
        self.assertEqual(canonical_attribution_v2_json(first), canonical_attribution_v2_json(second))
        with self.assertRaisesRegex(ForensicAttributionV2Error, "ATTRIBUTION_V1_DIGEST_MISMATCH"):
            enrich_attribution_v2(report, repository_root=ROOT, expected_attribution_digest="0" * 64, repository_revision="v2-revision")
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "v1.json", Path(temporary) / "v2.json"
            source.write_text(json.dumps(report), encoding="utf-8")
            stream = StringIO()
            with redirect_stdout(stream):
                status = main(["forensic-attribution-v2", "--repo", str(ROOT), "--report", str(source), "--expected-report-digest", report["report_digest"], "--json", "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stream.getvalue()), json.loads(output.read_text(encoding="utf-8")))

