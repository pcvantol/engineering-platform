"""Deterministic, evidence-only provenance attribution for forensic delta reports.

This module never opens a database.  It consumes the persisted JSON output of
``forensic_delta`` together with repository source and test evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


ATTRIBUTION_VERSION = "1.0"
REPORT_VERSION = "1.0"
ANCESTRY_ORIGINS = frozenset({"PRODUCTION", "TEST_HARNESS", "OPERATOR", "FORGE", "UNKNOWN"})
WRITER_ORIGINS = frozenset({"PRODUCTION_RUNTIME", "TEST_HARNESS", "OPERATOR_CONTROL", "FORGE_CONTROL", "MAINTENANCE", "UNKNOWN"})
STATE_SEMANTICS = frozenset({
    "IMMUTABLE_BUSINESS_STATE", "EXECUTION_EVIDENCE", "CONTROL_STATE", "CONFIGURATION",
    "MUTABLE_PROJECTION", "COMPONENT_LOG", "RETENTION_STATE", "TEST_ONLY_STRUCTURE", "UNKNOWN",
})
EVIDENCE_STATUSES = frozenset({"PROVEN", "UNRESOLVED"})


class ForensicAttributionError(RuntimeError):
    """Raised when a forensic report cannot be safely or deterministically used."""

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "FORENSIC_ATTRIBUTION_FAILED"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_attribution_json(report: dict[str, object]) -> str:
    """Render the stable on-disk JSON representation."""
    return _canonical_json(report)


def _source_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


@dataclass(frozen=True)
class WriterCandidate:
    table_name: str
    write_api: str
    production_callers: tuple[str, ...] = ()
    test_callers: tuple[str, ...] = ()
    operator_callers: tuple[str, ...] = ()
    maintenance_callers: tuple[str, ...] = ()

    def report(self) -> dict[str, object]:
        return {
            "table_name": self.table_name,
            "write_api": self.write_api,
            "production_callers": list(self.production_callers),
            "test_callers": list(self.test_callers),
            "operator_callers": list(self.operator_callers),
            "maintenance_callers": list(self.maintenance_callers),
        }


# This is an index of APIs, not a classification rule: shared writers never
# establish a row origin without a separate deterministic signal.
_WRITER_CANDIDATES = (
    WriterCandidate("execution_submissions", "storage.record_submission", ("src/engineering_platform/human_text_ingress.py", "src/engineering_platform/inbox_watcher.py"), ("tests/engineering",)),
    WriterCandidate("execution_runs", "storage.record_execution_run", ("src/engineering_platform/inbox_watcher.py",), ("tests/engineering",)),
    WriterCandidate("local_api_credentials", "local_api_credentials.issue_or_rotate", ("src/engineering_platform/local_api.py",), ("tests/engineering",), ("src/engineering_platform/dashboard.py",)),
    WriterCandidate("local_api_consumer_registrations", "local_api_credentials.register_consumer", ("src/engineering_platform/local_api.py",), ("tests/engineering",), ("src/engineering_platform/dashboard.py",)),
    WriterCandidate("engineering_component_logs", "component_logging.log_event", ("src/engineering_platform",), ("tests/engineering",), maintenance_callers=("src/engineering_platform/component_logging.py",)),
    WriterCandidate("engineering_metadata", "dashboard_configuration.update / database_maintenance._record_attempt", ("src/engineering_platform/dashboard.py",), ("tests/engineering",), maintenance_callers=("src/engineering_platform/database_maintenance.py",)),
    WriterCandidate("execution_projections", "storage.store_projection", ("src/engineering_platform",), ("tests/engineering",)),
)


def build_writer_index(repository_root: Path) -> list[dict[str, object]]:
    """Return the fixed, source-auditable candidate-writer index.

    Paths are retained only when present, so output remains useful after Phase
    3 extraction and does not claim a missing source file as evidence.
    """
    root = repository_root.resolve()
    result = []
    for candidate in _WRITER_CANDIDATES:
        item = candidate.report()
        for field in ("production_callers", "test_callers", "operator_callers", "maintenance_callers"):
            item[field] = [path for path in item[field] if (root / path).exists()]
        result.append(item)
    return result


def _test_literal_index(repository_root: Path) -> dict[str, list[str]]:
    """Index exact quoted test literals as deterministic fixture evidence."""
    root = repository_root.resolve()
    matches: dict[str, list[str]] = defaultdict(list)
    for folder in (root / "tests/engineering",):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            # Only quoted tokens are indexed.  A test-like name in a report is
            # not enough; a literal must appear in a committed test fixture.
            for literal in re.findall(r"(?<![\\w-])['\"]([A-Za-z][A-Za-z0-9_.:-]{2,})['\"]", text):
                matches[literal].append(_source_path(root, path))
    return {literal: sorted(set(paths)) for literal, paths in sorted(matches.items())}


def _safe_string(change: dict[str, Any], field: str) -> str | None:
    value = change.get(field)
    return value if isinstance(value, str) and value != "REDACTED" else None


def _canonical_key(change: dict[str, Any]) -> list[object]:
    key = change.get("canonical_key")
    return key if isinstance(key, list) else []


def _component_key(change: dict[str, Any]) -> str:
    for field, prefix in (("submission_id", "submission"), ("run_id", "run"), ("execution_run_id", "run")):
        value = _safe_string(change, field)
        if value:
            return f"{prefix}:{value}"
    consumer, project = _safe_string(change, "consumer_id"), _safe_string(change, "project_id")
    if consumer and project:
        return f"authority:{consumer}:{project}"
    return f"row:{change['table_name']}:{_canonical_json(_canonical_key(change))}"


def _evidence(rule_id: str, *items: dict[str, object]) -> list[dict[str, object]]:
    return [{"rule_id": rule_id, **item} for item in items]


def _unknown(change: dict[str, Any], *, semantics: str = "UNKNOWN", rule_id: str = "NO_POSITIVE_EVIDENCE") -> dict[str, object]:
    return {
        "ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": semantics,
        "evidence_status": "UNRESOLVED", "rule_id": rule_id,
        "evidence": _evidence(rule_id, {"type": "canonical_delta", "table_name": change["table_name"]}),
    }


def _fixture_evidence(change: dict[str, Any], literals: dict[str, list[str]]) -> dict[str, object] | None:
    """Recognize fixture families only from exact repository literals plus shape."""
    values = [value for value in (
        _safe_string(change, "submission_id"), _safe_string(change, "consumer_id"),
        _safe_string(change, "project_id"), _safe_string(change, "credential_id"),
    ) if value]
    sources = sorted({source for value in values for source in literals.get(value, [])})
    if change["table_name"] == "execution_submissions" and _safe_string(change, "submission_id") in literals:
        return {"ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS",
                "state_semantics": "IMMUTABLE_BUSINESS_STATE", "evidence_status": "PROVEN",
                "rule_id": "EXACT_TEST_SUBMISSION_FIXTURE",
                "evidence": _evidence("EXACT_TEST_SUBMISSION_FIXTURE", {"type": "exact_fixture_literal", "sources": sources})}
    if change["table_name"] in {"local_api_credentials", "local_api_consumer_registrations"} and len(sources) and len(values) >= 2:
        # Explicit test scopes prove fixed credentials/registrations. Generated
        # IDs require the scope plus an established lifecycle signal, never ID
        # shape alone.
        generated = _safe_string(change, "credential_id")
        lifecycle = bool(change.get("revoked_at")) or (generated or "").startswith("qualification-") or (generated or "").startswith("production-")
        if generated is None or generated in literals or lifecycle:
            return {"ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS",
                    "state_semantics": "CONTROL_STATE", "evidence_status": "PROVEN",
                    "rule_id": "TEST_AUTHORITY_FIXTURE_FAMILY",
                    "evidence": _evidence("TEST_AUTHORITY_FIXTURE_FAMILY", {"type": "fixture_scope_and_lifecycle", "sources": sources, "signals": ["exact_fixture_scope", "deterministic_lifecycle"]})}
    run_id = _safe_string(change, "run_id") or _safe_string(change, "execution_run_id")
    if change["table_name"].startswith(("execution_", "provider_")) and run_id and run_id in literals:
        return {"ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS",
                "state_semantics": "EXECUTION_EVIDENCE", "evidence_status": "PROVEN",
                "rule_id": "EXACT_TEST_RUN_FIXTURE",
                "evidence": _evidence("EXACT_TEST_RUN_FIXTURE", {"type": "exact_fixture_literal", "sources": literals[run_id], "signals": ["exact_run_fixture", "execution_evidence_row"]})}
    return None


def _classify(change: dict[str, Any], literals: dict[str, list[str]]) -> dict[str, object]:
    table, change_type = str(change["table_name"]), str(change["change_type"])
    submission_id = _safe_string(change, "submission_id")
    if table == "engineering_component_logs" and change_type == "REMOVED":
        return {"ancestry_origin": "UNKNOWN", "writer_origin": "MAINTENANCE", "state_semantics": "RETENTION_STATE",
                "evidence_status": "PROVEN", "rule_id": "COMPONENT_LOG_RETENTION",
                "evidence": _evidence("COMPONENT_LOG_RETENTION", {"type": "repository_semantics", "source": "src/engineering_platform/component_logging.py", "signals": ["bounded_log_pruning_api", "removed_component_log"]})}
    if table == "engineering_metadata":
        key = str(_canonical_key(change)[0]) if _canonical_key(change) else ""
        if key.startswith("dashboard_configuration."):
            return {"ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": "CONFIGURATION",
                    "evidence_status": "UNRESOLVED", "rule_id": "SHARED_CONFIGURATION_WRITER",
                    "evidence": _evidence("SHARED_CONFIGURATION_WRITER", {"type": "writer_index", "source": "src/engineering_platform/dashboard_configuration.py", "signals": ["configuration_key", "shared_api_no_caller_receipt"]})}
        if key == "database_maintenance.last_attempt_at":
            return {"ancestry_origin": "UNKNOWN", "writer_origin": "MAINTENANCE", "state_semantics": "RETENTION_STATE",
                    "evidence_status": "PROVEN", "rule_id": "DATABASE_MAINTENANCE_METADATA",
                    "evidence": _evidence("DATABASE_MAINTENANCE_METADATA", {"type": "repository_semantics", "source": "src/engineering_platform/database_maintenance.py", "signals": ["reserved_maintenance_key", "maintenance_writer"]})}
    if table == "execution_projections":
        return {"ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": "MUTABLE_PROJECTION",
                "evidence_status": "UNRESOLVED", "rule_id": "SHARED_PROJECTION_WRITER",
                "evidence": _evidence("SHARED_PROJECTION_WRITER", {"type": "writer_index", "source": "src/engineering_platform/storage.py", "signals": ["projection_table", "shared_api_no_caller_receipt"]})}
    fixture = _fixture_evidence(change, literals)
    if fixture:
        return fixture
    if table == "execution_submissions" and submission_id and re.fullmatch(r"human-ingress-[0-9a-f]{32,64}", submission_id) and (_safe_string(change, "producer_id") or "").startswith("human:"):
        return {"ancestry_origin": "PRODUCTION", "writer_origin": "PRODUCTION_RUNTIME", "state_semantics": "IMMUTABLE_BUSINESS_STATE",
                "evidence_status": "PROVEN", "rule_id": "CANONICAL_HUMAN_INGRESS_ENVELOPE",
                "evidence": _evidence("CANONICAL_HUMAN_INGRESS_ENVELOPE", {"type": "canonical_ingress_structure", "source": "src/engineering_platform/human_text_ingress.py", "signals": ["canonical_human_ingress_id", "human_producer_binding", "immutable_submission_row"]})}
    if table == "backup_probe":
        return {"ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS", "state_semantics": "TEST_ONLY_STRUCTURE",
                "evidence_status": "PROVEN", "rule_id": "TEST_ONLY_SCHEMA_STRUCTURE",
                "evidence": _evidence("TEST_ONLY_SCHEMA_STRUCTURE", {"type": "schema_difference", "signals": ["candidate_only_test_structure"]})}
    semantics = "EXECUTION_EVIDENCE" if table.startswith(("execution_", "provider_", "managed_")) else "UNKNOWN"
    return _unknown(change, semantics=semantics)


def _binding_for(component_id: str, evidence_bindings: dict[str, Any]) -> dict[str, Any] | None:
    binding = evidence_bindings.get(component_id)
    if not isinstance(binding, dict) or binding.get("ancestry_origin") not in ANCESTRY_ORIGINS:
        return None
    evidence = binding.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(item, dict) and item.get("source") for item in evidence):
        return None
    if "writer_origin" in binding and binding["writer_origin"] not in WRITER_ORIGINS:
        return None
    if "state_semantics" in binding and binding["state_semantics"] not in STATE_SEMANTICS:
        return None
    return binding


def _verify_input(report: dict[str, Any], expected_report_digest: str | None) -> str:
    if report.get("report_version") != REPORT_VERSION or not isinstance(report.get("migration_id"), str):
        raise ForensicAttributionError("FORENSIC_REPORT_CONTRACT_INVALID")
    for side in ("baseline", "candidate"):
        if not isinstance(report.get(side), dict) or not isinstance(report[side].get("sha256"), str):
            raise ForensicAttributionError("FORENSIC_REPORT_FINGERPRINT_MISSING")
    observed = report.get("report_digest")
    unsigned = dict(report)
    unsigned.pop("report_digest", None)
    if not isinstance(observed, str) or _digest(unsigned) != observed:
        raise ForensicAttributionError("FORENSIC_REPORT_DIGEST_INVALID")
    if expected_report_digest and observed != expected_report_digest:
        raise ForensicAttributionError("FORENSIC_REPORT_DIGEST_MISMATCH")
    return observed


def _repository_revision(repository_root: Path) -> str:
    completed = subprocess.run(["git", "-C", str(repository_root), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    if completed.returncode:
        raise ForensicAttributionError("REPOSITORY_REVISION_UNAVAILABLE")
    return completed.stdout.strip()


def _counts(rows: list[dict[str, object]], field: str, vocabulary: frozenset[str]) -> dict[str, int]:
    observed = Counter(str(row[field]) for row in rows)
    return {value: observed[value] for value in sorted(vocabulary)}


def _schema_findings(report: dict[str, Any]) -> list[dict[str, object]]:
    """Attribute candidate-only structures without pretending they are row deltas."""
    difference = report.get("schema_difference")
    candidates = difference.get("tables_candidate_only", []) if isinstance(difference, dict) else []
    findings = []
    for table_name in sorted(value for value in candidates if isinstance(value, str)):
        if table_name == "backup_probe":
            findings.append({"table_name": table_name, "ancestry_origin": "TEST_HARNESS", "writer_origin": "TEST_HARNESS", "state_semantics": "TEST_ONLY_STRUCTURE", "evidence_status": "PROVEN", "rule_id": "TEST_ONLY_SCHEMA_STRUCTURE", "evidence": _evidence("TEST_ONLY_SCHEMA_STRUCTURE", {"type": "schema_difference", "signals": ["candidate_only_test_structure"]})})
        else:
            findings.append({"table_name": table_name, "ancestry_origin": "UNKNOWN", "writer_origin": "UNKNOWN", "state_semantics": "UNKNOWN", "evidence_status": "UNRESOLVED", "rule_id": "CANDIDATE_ONLY_SCHEMA_UNRESOLVED", "evidence": _evidence("CANDIDATE_ONLY_SCHEMA_UNRESOLVED", {"type": "schema_difference"})})
    return findings


def attribute_forensic_delta(report: dict[str, Any], *, repository_root: Path, expected_report_digest: str | None = None, repository_revision: str | None = None, evidence_bindings: dict[str, Any] | None = None) -> dict[str, object]:
    """Attribute a verified forensic report without opening any storage input."""
    input_digest = _verify_input(report, expected_report_digest)
    root = repository_root.resolve()
    literals = _test_literal_index(root)
    bindings = evidence_bindings or {}
    rows: list[dict[str, object]] = []
    for table in sorted(report.get("tables", []), key=lambda item: str(item.get("table_name", ""))):
        if not isinstance(table, dict):
            continue
        for change in sorted(table.get("changes", []), key=_canonical_json):
            if not isinstance(change, dict) or change.get("change_type") not in {"ADDED", "REMOVED", "MODIFIED"}:
                continue
            material = dict(change)
            material["table_name"] = str(table.get("table_name", ""))
            attributed = _classify(material, literals)
            component_id = _component_key(material)
            binding = _binding_for(component_id, bindings)
            if binding:
                attributed = dict(attributed)
                attributed["ancestry_origin"] = binding["ancestry_origin"]
                if "writer_origin" in binding:
                    attributed["writer_origin"] = binding["writer_origin"]
                    attributed["evidence_status"] = "PROVEN"
                if "state_semantics" in binding:
                    attributed["state_semantics"] = binding["state_semantics"]
                attributed["evidence"] = [*attributed["evidence"], {"rule_id": "EXTERNAL_EVIDENCE_BINDING", "type": "immutable_evidence_binding", "source": binding["evidence"][0]["source"], "signals": binding["evidence"][0].get("signals", [])}]
            rows.append({
                "table_name": material["table_name"], "change_type": material["change_type"],
                "canonical_key": _canonical_key(material), "component_id": component_id,
                "ancestry_origin": attributed["ancestry_origin"], "writer_origin": attributed["writer_origin"],
                "state_semantics": attributed["state_semantics"], "evidence_status": attributed["evidence_status"],
                "rule_id": attributed["rule_id"], "evidence": attributed["evidence"],
            })
    rows.sort(key=_canonical_json)
    components: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        components[str(row["component_id"])].append(_digest(row))
    component_reports = [{"component_id": key, "row_digests": sorted(values)} for key, values in sorted(components.items())]
    schema_findings = _schema_findings(report)
    summary = {
        "changed_row_count": len(rows), "component_count": len(component_reports), "schema_finding_count": len(schema_findings),
        "writer_origin": _counts(rows, "writer_origin", WRITER_ORIGINS),
        "ancestry_origin": _counts(rows, "ancestry_origin", ANCESTRY_ORIGINS),
        "state_semantics": _counts(rows, "state_semantics", STATE_SEMANTICS),
        "evidence_status": _counts(rows, "evidence_status", EVIDENCE_STATUSES),
    }
    result: dict[str, object] = {
        "attribution_version": ATTRIBUTION_VERSION, "input_report": {"report_digest": input_digest, "report_version": report["report_version"], "migration_id": report["migration_id"], "baseline_fingerprint": report["baseline"]["sha256"], "candidate_fingerprint": report["candidate"]["sha256"]},
        "repository_revision": repository_revision or _repository_revision(root), "writer_index": build_writer_index(root),
        "summary": summary, "components": component_reports, "schema_findings": schema_findings, "rows": rows,
    }
    result["report_digest"] = _digest(result)
    return result


def load_and_attribute(path: Path, *, repository_root: Path, expected_report_digest: str | None = None, evidence_bindings: dict[str, Any] | None = None) -> dict[str, object]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ForensicAttributionError("FORENSIC_REPORT_UNREADABLE") from error
    if not isinstance(report, dict):
        raise ForensicAttributionError("FORENSIC_REPORT_CONTRACT_INVALID")
    return attribute_forensic_delta(report, repository_root=repository_root, expected_report_digest=expected_report_digest, evidence_bindings=evidence_bindings)
