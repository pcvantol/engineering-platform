"""Deterministic V2 enrichment of an existing forensic attribution report.

V2 consumes only V1 JSON and committed repository evidence.  It never opens a
database or replays the delta exporter.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from .forensic_attribution import _test_literal_index, canonical_attribution_json


ATTRIBUTION_VERSION = "2.0"
RULES_VERSION = "2.0"
V1_VERSION = "1.0"
_ORIGINS = ("FORGE_CONTROL", "MAINTENANCE", "OPERATOR_CONTROL", "PRODUCTION_RUNTIME", "TEST_HARNESS", "UNKNOWN")
_ANCESTRIES = ("FORGE", "OPERATOR", "PRODUCTION", "TEST_HARNESS", "UNKNOWN")
_STATUSES = ("PROVEN", "UNRESOLVED")


class ForensicAttributionV2Error(RuntimeError):
    """Raised when a V1 report cannot be safely enriched."""

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "FORENSIC_ATTRIBUTION_V2_FAILED"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _revision(root: Path) -> str:
    completed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    if completed.returncode:
        raise ForensicAttributionV2Error("REPOSITORY_REVISION_UNAVAILABLE")
    return completed.stdout.strip()


def _verify_v1(report: dict[str, Any], expected_digest: str | None) -> str:
    if report.get("attribution_version") != V1_VERSION:
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_VERSION_INVALID")
    input_report = report.get("input_report")
    if not isinstance(input_report, dict) or not isinstance(input_report.get("report_digest"), str):
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_INPUT_INVALID")
    observed = report.get("report_digest")
    unsigned = dict(report)
    unsigned.pop("report_digest", None)
    if not isinstance(observed, str) or _digest(unsigned) != observed:
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_DIGEST_INVALID")
    if expected_digest and observed != expected_digest:
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_DIGEST_MISMATCH")
    return observed


def _component_literal(component_id: object) -> str | None:
    if not isinstance(component_id, str) or not component_id.startswith(("run:", "submission:")):
        return None
    value = component_id.split(":", 1)[1]
    return value or None


def _counts(rows: list[dict[str, Any]], key: str, vocabulary: tuple[str, ...]) -> dict[str, int]:
    observed = Counter(str(row.get(key)) for row in rows)
    return {value: observed[value] for value in vocabulary}


def enrich_attribution_v2(report: dict[str, Any], *, repository_root: Path, expected_attribution_digest: str | None = None, repository_revision: str | None = None) -> dict[str, object]:
    """Apply only direct, exact-test-component evidence to V1 unresolved rows."""
    v1_digest = _verify_v1(report, expected_attribution_digest)
    root = repository_root.resolve()
    literals = _test_literal_index(root)
    rows: list[dict[str, Any]] = []
    for original in report.get("rows", []):
        if not isinstance(original, dict):
            continue
        row = dict(original)
        literal = _component_literal(row.get("component_id"))
        sources = literals.get(literal or "", [])
        if row.get("evidence_status") == "UNRESOLVED" and literal and sources:
            # This applies only when the V1 row directly names the run or
            # submission component; a test-like string or ancestry alone never
            # qualifies. The pre-existing semantics remain unchanged.
            row.update({
                "ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS",
                "evidence_status": "PROVEN", "rule_id": "EXACT_TEST_COMPONENT_FIXTURE",
                "evidence": [*list(row.get("evidence", [])), {
                    "rule_id": "EXACT_TEST_COMPONENT_FIXTURE", "type": "exact_fixture_component",
                    "sources": sources, "signals": ["direct_run_or_submission_component", "exact_committed_test_literal"],
                }],
            })
        rows.append(row)
    rows.sort(key=_canonical_json)
    summary = {
        "changed_row_count": len(rows),
        "writer_origin": _counts(rows, "writer_origin", _ORIGINS),
        "ancestry_origin": _counts(rows, "ancestry_origin", _ANCESTRIES),
        "evidence_status": _counts(rows, "evidence_status", _STATUSES),
        "v1_unresolved_count": sum(1 for row in report.get("rows", []) if isinstance(row, dict) and row.get("evidence_status") == "UNRESOLVED"),
        "v2_resolved_count": sum(1 for row in rows if row.get("rule_id") == "EXACT_TEST_COMPONENT_FIXTURE"),
    }
    result: dict[str, object] = {
        "attribution_version": ATTRIBUTION_VERSION, "attribution_rules_version": RULES_VERSION,
        "input_attribution": {"report_digest": v1_digest, "attribution_version": report["attribution_version"], "forensic_report_digest": report["input_report"]["report_digest"]},
        "repository_revision": repository_revision or _revision(root), "summary": summary,
        "schema_findings": report.get("schema_findings", []), "rows": rows,
    }
    result["report_digest"] = _digest(result)
    return result


def load_and_enrich_v2(path: Path, *, repository_root: Path, expected_attribution_digest: str | None = None) -> dict[str, object]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_UNREADABLE") from error
    if not isinstance(report, dict):
        raise ForensicAttributionV2Error("ATTRIBUTION_V1_INPUT_INVALID")
    return enrich_attribution_v2(report, repository_root=repository_root, expected_attribution_digest=expected_attribution_digest)


def canonical_attribution_v2_json(report: dict[str, object]) -> str:
    """Keep the public V2 JSON renderer explicit at the command boundary."""
    return canonical_attribution_json(report)
