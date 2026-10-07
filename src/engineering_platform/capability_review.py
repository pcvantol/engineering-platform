"""Deterministic, read-only capability reviewer selection for the Execution Host."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Callable, Protocol

from .agent_state import redact_diagnostic
from .provider_context import ProviderRole, project_context
from .provider_context_scope import ContextScope, POLICY_ID, provider_instruction
from .reviewer_evidence import ReviewerEvidence


REVIEWER_ORDER = (
    "apple_platform",
    "windows_platform",
    "home_assistant_integration",
    "esphome_firmware",
    "pi_renderer",
    "universal_receiver",
    "website",
    "api",
    "repository_governance",
    "validation",
    "documentation",
    "finalization",
)
REVIEWER_LABELS = {
    "quality": "Quality Reviewer",
    "security": "Security Reviewer",
    "apple_platform": "Apple Platform Reviewer",
    "windows_platform": "Windows Platform Reviewer",
    "home_assistant_integration": "Home Assistant Integration Reviewer",
    "esphome_firmware": "ESPHome Firmware Reviewer",
    "pi_renderer": "Pi Renderer Reviewer",
    "universal_receiver": "Universal Receiver Reviewer",
    "website": "Website Reviewer",
    "api": "API Reviewer",
    "repository_governance": "Repository Governance Reviewer",
    "validation": "Validation Reviewer",
    "documentation": "Documentation Reviewer",
    "finalization": "Finalization Reviewer",
}
ADVISORY_REVIEW_OUTPUT_CONTRACT_VERSION = "1.0"
MANDATORY_REVIEW_OUTPUT_CONTRACT_VERSION = "3.0"
MANDATORY_FINDING_FIELDS = frozenset({
    "id", "observation", "category", "criterion", "severity", "confidence", "evidence_ref",
})
MANDATORY_COVERAGE_FIELDS = frozenset({"surface", "status", "evidence_ref"})
MANDATORY_DISPOSITION_FIELDS = frozenset({"finding_id", "disposition", "evidence_ref"})
MANDATORY_COVERAGE_SURFACES = {
    "EFFECT_RESULT": {
        "quality": ("approved_criteria", "source_evidence", "meaningful_result", "output_scope",
                    "document_design_content", "validation_controls", "restart_identity"),
        "security": ("approved_criteria", "source_evidence", "authority_scope", "sensitive_data",
                     "containment", "artifact_integrity", "restart_identity"),
    },
    "IMPLEMENTATION": {
        "quality": (
            "approved_criteria", "full_candidate_diff", "callers_and_consumers",
            "service_lifecycle", "maintenance_recovery_cleanup", "persistence_concurrency",
            "transport_schema_compatibility", "regression_validation",
        ),
        "security": (
            "approved_criteria", "full_candidate_diff", "trust_authentication",
            "data_integrity_concurrency", "maintenance_recovery_cleanup",
            "failure_redaction", "availability_resource_bounds", "security_validation",
        ),
    },
    "FINALIZATION": {
        "quality": (
            "delivery_role_scope", "full_candidate_diff", "record_consistency",
            "lifecycle_state_claims", "regression_validation",
        ),
        "security": (
            "delivery_role_scope", "full_candidate_diff", "authority_scope",
            "sensitive_data", "lifecycle_state_claims", "security_validation",
        ),
    },
    "RECONCILIATION": {
        "quality": (
            "delivery_role_scope", "full_candidate_diff", "record_consistency",
            "lifecycle_state_claims", "regression_validation",
        ),
        "security": (
            "delivery_role_scope", "full_candidate_diff", "authority_scope",
            "sensitive_data", "lifecycle_state_claims", "security_validation",
        ),
    },
}


@dataclass(frozen=True)
class ReviewerSelection:
    reviewer: str
    selected_because: str
    confidence: float | None
    capability: str = "engineering"
    # Mandatory assurance is selected by the host for one immutable delivery
    # role and one exact set of still-open findings.  Carry that host-owned
    # contract across the provider boundary so the structured-output schema
    # cannot admit a broader, incomplete response that the deterministic
    # validator must subsequently reject.
    required_coverage_surfaces: tuple[str, ...] = ()
    required_finding_ids: tuple[str, ...] = ()
    specialist_binding: dict[str, str] = field(default_factory=dict)
    specialist_question: str = ""
    specialist_paths: tuple[str, ...] = ()
    specialist_source_blobs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReviewerResult:
    reviewer: str
    contribution: str
    recommendations: tuple[str, ...] = ()
    findings: tuple[dict[str, str], ...] = ()
    # In-process deterministic/test adapters construct this typed result only
    # after choosing the mandatory contract. Raw provider JSON is still
    # required to carry this field by ``CodexCliClient.review``.
    contract_version: str | None = ADVISORY_REVIEW_OUTPUT_CONTRACT_VERSION
    failed: bool = False
    usage: dict[str, object] = field(default_factory=dict)
    runtime_metadata: dict[str, object] = field(default_factory=dict)
    churn: dict[str, object] = field(default_factory=dict)
    duration_seconds: float | None = None
    usage_snapshots: tuple[dict[str, int], ...] = ()
    coverage: tuple[dict[str, str], ...] = ()
    finding_dispositions: tuple[dict[str, str], ...] = ()
    specialist_binding: dict[str, str] = field(default_factory=dict)


class ReviewerClient(Protocol):
    def review(
        self,
        root: Path,
        selection: ReviewerSelection,
        objective: str,
        evidence: ReviewerEvidence | None = None,
    ) -> ReviewerResult: ...


@dataclass(frozen=True)
class ReviewerPlan:
    selections: tuple[ReviewerSelection, ...]
    decisions: tuple[dict[str, object], ...]


def select_reviewers(objective: str, prompt_path: Path, transaction_kind: str, memory: object, *,
    root: Path | None = None, run_id: str = "", repository: str = "", candidate_sha: str = "", qualified_roles: tuple[str, ...] = (),
    remaining_percent: float | None = None, reserve_percent: int = 0, consumed_invocations: int = 0,
) -> tuple[ReviewerSelection, ...] | ReviewerPlan:
    """Select bounded questions against actual immutable Git path evidence."""
    if root is None:
        return ()
    profile = _digest({"version": SPECIALIST_PROFILE_VERSION, "max_optional": MAX_OPTIONAL_INVOCATIONS,
                       "reserved_mandatory_capacity_percent": max(50, reserve_percent), "parallel": False})
    decisions = []
    selected = []
    try:
        requests = specialist_requests(objective)
    except ValueError as error:
        requests = ()
        malformed = str(error)
    else:
        malformed = "no_relevant_question_or_consumer"
    seen = set()
    # Always project registered roles, including their concrete skip reasons.
    priorities = {"HIGH": 0, "NORMAL": 1, "LOW": 2}
    order = sorted(REVIEWER_ORDER, key=lambda role: (min((priorities.get(r.get("risk"), 3) if isinstance(r.get("risk"), str) else 3 for r in requests if isinstance(r, dict) and r.get("reviewer") == role), default=3), REVIEWER_ORDER.index(role)))
    for reviewer in order:
        matching = [r for r in requests if isinstance(r, dict) and r.get("reviewer") == reviewer]
        request = matching[0] if len(matching) == 1 else None
        reason = "overlapping_or_ambiguous_question" if len(matching) > 1 else malformed
        paths = ()
        question = ""
        source = None
        if request is not None:
            reason = "invalid_question_contract"
            if (set(request) == {"reviewer", "question", "paths", "consumer", "risk"}
                    and _safe_text(request["question"]) and isinstance(request["paths"], list)
                    and 0 < len(request["paths"]) <= 8 and all(_safe_path(p) for p in request["paths"])
                    and len(set(request["paths"])) == len(request["paths"])
                    and isinstance(request["risk"], str) and request["risk"] in {"LOW", "NORMAL", "HIGH"}):
                paths = tuple(request["paths"])
                question = request["question"]
                reason = "no_consumer" if request["consumer"] != "EXECUTE_AGENT" else "no_primary_consumer_for_lifecycle" if transaction_kind != "IMPLEMENTATION" else "selected"
                if reason == "selected" and not any(any(p == prefix or p.startswith(prefix) for prefix in _ROLE_PATHS[reviewer]) for p in paths):
                    reason = "irrelevant_task_paths"
                if reason == "selected":
                    try:
                        source = []
                        for p in paths:
                            from .effect_workspace import git as safe_git
                            raw = safe_git(root, "ls-tree", "-z", candidate_sha, "--", p).split(b"\0")
                            if len(raw) != 2 or raw[1] != b"":
                                raise ValueError("path is not one exact blob")
                            metadata, encoded_path = raw[0].split(b"\t", 1)
                            entry = metadata.decode("ascii").split()
                            if len(entry) != 3 or entry[0] not in {"100644", "100755"} or entry[1] != "blob" or encoded_path.decode("utf-8") != p:
                                raise ValueError("path is not an exact non-symlink blob")
                            source.append((p, entry[2]))
                        if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha in source):
                            raise ValueError("invalid source")
                    except (subprocess.CalledProcessError, OSError, ValueError):
                        reason = "missing_snapshot_path"
        fingerprint = _digest({"question": question.casefold(), "paths": sorted(paths)})
        if reason == "selected" and fingerprint in seen:
            reason = "overlapping_question"
        if reason == "selected":
            seen.add(fingerprint)
            if reviewer not in qualified_roles:
                reason = "capability_unqualified"
            elif not isinstance(remaining_percent, (int, float)) or isinstance(remaining_percent, bool) or not 0 <= remaining_percent <= 100:
                reason = "capacity_unknown"
            elif remaining_percent <= max(50, reserve_percent):
                reason = "mandatory_controls_assurance_and_repair_reserved"
            elif consumed_invocations + len(selected) >= MAX_OPTIONAL_INVOCATIONS:
                reason = "finite_optional_allowance_exhausted"
            elif project_context(ProviderRole.SPECIALIST_REVIEW, objective).telemetry["context_budget_overflow_bytes"]:
                reason = "complete_context_overflow"
        source_digest = _digest({"source": source, "objective_digest": _digest(objective), "risk": request.get("risk") if request and isinstance(request.get("risk"), str) and request.get("risk") in {"LOW", "NORMAL", "HIGH"} else None})
        request_id = _digest({"run": run_id, "reviewer": reviewer, "question": question, "source_digest": source_digest, "profile": profile})
        binding = {"run_id": run_id, "repository": repository, "request_id": request_id, "invocation_id": "", "candidate_sha": candidate_sha,
                   "source_digest": source_digest, "profile_digest": profile, "reviewer": reviewer, "consumer": "EXECUTE_AGENT"}
        decision = {**binding, "kind": "SELECTION", "payload": {"status": "SELECTED" if reason == "selected" else "SKIPPED", "reason": reason,
                    "question": question, "paths": list(paths), "source_blobs": dict(source or ()), "risk": request.get("risk") if request and isinstance(request.get("risk"), str) and request.get("risk") in {"LOW", "NORMAL", "HIGH"} else None}}
        decisions.append(decision)
        if reason == "selected":
            selected.append(ReviewerSelection(reviewer, "explicit question, relevant snapshot paths and primary consumer", None,
                                              reviewer, specialist_binding=binding, specialist_question=question, specialist_paths=paths, specialist_source_blobs=tuple(source or ())))
    return ReviewerPlan(tuple(selected), tuple(decisions))


ReviewerProgressCallback = Callable[[ReviewerSelection, str, ReviewerResult | None], None]


def run_reviews(
    root: Path,
    selections: tuple[ReviewerSelection, ...],
    objective: str,
    client: ReviewerClient | None,
    progress: ReviewerProgressCallback | None = None,
    evidence: ReviewerEvidence | None = None,
) -> tuple[ReviewerResult, ...]:
    """Run bounded read-only invocations sequentially; host validates mandatory output."""
    mandatory = len(selections) == 1 and selections[0].reviewer in {"quality", "security"} and not selections[0].specialist_binding
    if not mandatory and (len(selections) > 2 or any(s.reviewer not in REVIEWER_ORDER for s in selections)):
        raise ValueError("optional review wave exceeds its bounded role allowance")
    if not selections:
        return ()
    if client is None:
        results = tuple(ReviewerResult(item.reviewer, "Reviewer client unavailable; primary review continues.", failed=True) for item in selections)
        if progress:
            for selection, result in zip(selections, results, strict=True):
                progress(selection, "failed", result)
        return results

    def invoke(selection: ReviewerSelection) -> ReviewerResult:
        if progress:
            progress(selection, "started", None)
        try:
            result = client.review(root, replace(selection, specialist_binding=dict(selection.specialist_binding)), objective, evidence)
            if result.reviewer != selection.reviewer:
                result = ReviewerResult(selection.reviewer, "Reviewer identity mismatch; primary review continues.", failed=True)
            else:
                result = ReviewerResult(
                selection.reviewer,
                redact_diagnostic(result.contribution, limit=240),
                tuple(redact_diagnostic(value, limit=240) for value in result.recommendations),
                tuple(dict(item) for item in result.findings if isinstance(item, dict)),
                result.contract_version,
                result.failed,
                dict(result.usage),
                dict(result.runtime_metadata),
                dict(result.churn),
                result.duration_seconds,
                tuple(dict(item) for item in result.usage_snapshots),
                tuple(dict(item) for item in result.coverage if isinstance(item, dict)),
                tuple(dict(item) for item in result.finding_dispositions if isinstance(item, dict)),
                specialist_binding=dict(result.specialist_binding),
            )
        except Exception:  # Reviewer failure is advisory and cannot block the transaction.
            result = ReviewerResult(selection.reviewer, "Reviewer failed; primary review continues.", failed=True)
        if progress:
            progress(selection, "failed" if result.failed else "completed", result)
        return result

    # Optional work is finite and sequential; mandatory assurance is not dispatched here.
    return tuple(invoke(item) for item in selections)


def reconciled_recommendations(results: tuple[ReviewerResult, ...]) -> tuple[str, ...]:
    """Deduplicate safe advisory recommendations for the primary agent prompt."""
    accepted: list[str] = []
    for result in results:
        if result.failed:
            continue
        for recommendation in result.recommendations:
            if recommendation and recommendation not in accepted:
                accepted.append(recommendation)
    return tuple(accepted[:8])


def mandatory_findings(result: ReviewerResult) -> tuple[dict[str, str], ...] | None:
    """Return validated mandatory output, never advice-shaped pseudo-evidence."""
    if result.failed or result.contract_version != MANDATORY_REVIEW_OUTPUT_CONTRACT_VERSION:
        return None
    finding_limit = 12 if result.reviewer in {"quality", "security"} else 64
    if not isinstance(result.findings, tuple) or len(result.findings) > finding_limit:
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for finding in result.findings:
        if not isinstance(finding, dict) or set(finding) != MANDATORY_FINDING_FIELDS:
            return None
        if any(not isinstance(value, str) or not value.strip() or len(value) > 240 for value in finding.values()):
            return None
        if finding["severity"] not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} or finding["confidence"] not in {"LOW", "MEDIUM", "HIGH"}:
            return None
        if finding["id"] in seen:
            return None
        seen.add(finding["id"])
        normalized.append({key: redact_diagnostic(value, limit=240) for key, value in finding.items()})
    return tuple(normalized)


def mandatory_coverage_surfaces(reviewer: str, delivery_role: str) -> tuple[str, ...]:
    """Return the host-owned complete impact checklist for one assurance role."""
    return MANDATORY_COVERAGE_SURFACES.get(delivery_role, {}).get(reviewer, ())


def mandatory_assessment(
    result: ReviewerResult,
    *,
    delivery_role: str,
    prior_finding_ids: tuple[str, ...],
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...], tuple[dict[str, str], ...]] | None:
    """Validate complete impact coverage and explicit prior-finding closure.

    A syntactically valid list of findings is insufficient for mandatory
    assurance.  Each reviewer must cover every host-owned impact surface and
    explicitly reassess every still-open finding previously raised by that
    same independent role.
    """
    findings = mandatory_findings(result)
    required_surfaces = mandatory_coverage_surfaces(result.reviewer, delivery_role)
    if findings is None or not required_surfaces:
        return None
    coverage = result.coverage
    dispositions = result.finding_dispositions
    if (
        not isinstance(coverage, tuple)
        or len(coverage) != len(required_surfaces)
        or any(not isinstance(item, dict) or set(item) != MANDATORY_COVERAGE_FIELDS for item in coverage)
        or any(
            item["surface"] not in required_surfaces
            or item["status"] not in {"REVIEWED", "NOT_APPLICABLE"}
            or not isinstance(item["evidence_ref"], str) or not item["evidence_ref"].strip()
            or len(item["evidence_ref"]) > 240
            for item in coverage
        )
        or {item["surface"] for item in coverage} != set(required_surfaces)
    ):
        return None
    if (
        not isinstance(dispositions, tuple)
        or len(dispositions) != len(prior_finding_ids)
        or any(not isinstance(item, dict) or set(item) != MANDATORY_DISPOSITION_FIELDS for item in dispositions)
        or any(
            item["finding_id"] not in prior_finding_ids
            or item["disposition"] not in {"RESOLVED", "OPEN"}
            or not isinstance(item["evidence_ref"], str) or not item["evidence_ref"].strip()
            or len(item["evidence_ref"]) > 240
            for item in dispositions
        )
        or {item["finding_id"] for item in dispositions} != set(prior_finding_ids)
    ):
        return None
    return (
        findings,
        tuple({key: redact_diagnostic(value, limit=240) for key, value in item.items()} for item in coverage),
        tuple({key: redact_diagnostic(value, limit=240) for key, value in item.items()} for item in dispositions),
    )


def records_for_storage(selections: tuple[ReviewerSelection, ...], results: tuple[ReviewerResult, ...]) -> tuple[dict[str, object], ...]:
    by_reviewer = {result.reviewer: result for result in results}
    records: list[dict[str, object]] = []
    for selection in selections:
        result = by_reviewer.get(selection.reviewer, ReviewerResult(selection.reviewer, "No result.", failed=True))
        records.append({
            "reviewer": selection.reviewer,
            "capability": selection.capability,
            "selected_because": selection.selected_because,
            "confidence": selection.confidence,
            "contribution": result.contribution,
            "accepted_recommendations": 0,
            "proposed_recommendations": len(result.recommendations) if not result.failed else 0,
            "rejected_recommendations": 0,
            "failed": result.failed,
            "codex_commands_executed": _command_count(result.churn),
        })
    return tuple(records)


def _command_count(churn: object) -> int:
    """Return the safe, per-review command total without sharing agent state."""
    if not isinstance(churn, dict):
        return 0
    value = churn.get("tool_loop_operations", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0




def reviewer_prompt(
    selection: ReviewerSelection,
    objective: str,
    evidence: ReviewerEvidence | None = None,
) -> str:
    """Build the bounded read-only reviewer instruction without lifecycle authority."""
    role = {
        "quality": ProviderRole.QUALITY_REVIEW,
        "security": ProviderRole.SECURITY_REVIEW,
    }.get(selection.reviewer, ProviderRole.SPECIALIST_REVIEW)
    projection = project_context(role, objective)
    prompt: dict[str, object] = {
        "reviewer": REVIEWER_LABELS[selection.reviewer],
        "capability": selection.capability,
        "selected_because": selection.selected_because,
        "objective": projection.text,
        "context_projection": {
            "role": projection.role.value,
            "budget_version": projection.budget_version,
            "source_item_count": projection.source_item_count,
            "omitted_low_priority_count": projection.omitted_low_priority_count,
            "budget_overflow_bytes": projection.telemetry["context_budget_overflow_bytes"],
        },
        "provider_context_scope": {
            "policy": POLICY_ID,
            "initial_scope": ContextScope.NORMAL.value,
            "instruction": provider_instruction(ContextScope.NORMAL),
        },
        "authority": "Read-only inspection and recommendations only. Do not edit, commit, push, merge, create pull requests, finalize, or change lifecycle state.",
        "scope": "Analyse only the declared capability. Cross-capability analysis requires objective repository evidence.",
    }
    if selection.specialist_binding:
        prompt["specialist_contract"] = {"version": SPECIALIST_CONTRACT_VERSION, "binding": selection.specialist_binding,
            "question": selection.specialist_question, "allowed_evidence_paths": list(selection.specialist_paths),
            "allowed_evidence_refs": {path: "git-blob:" + sha for path, sha in selection.specialist_source_blobs},
            "result_boundary": "Return only bounded typed findings, not private reasoning/transcripts, executable links or approval. The primary consumer may explicitly dispose these untrusted optional findings; independent Quality/Security receive no specialist conclusions."}
    if selection.reviewer in {"quality", "security"}:
        prompt["mandatory_review_contract"] = {
            "version": MANDATORY_REVIEW_OUTPUT_CONTRACT_VERSION,
            "required_coverage_surfaces": list(selection.required_coverage_surfaces),
            "required_finding_ids": list(selection.required_finding_ids),
        }
    if evidence is not None:
        prompt["run_scoped_repository_evidence"] = evidence.to_dict()
        prompt["evidence_instructions"] = (
            "These are host-observed facts for this exact Run ID, collected after "
            "synchronization and before this reviewer wave. Reuse them for ordinary "
            "repository-state questions; do not rediscover branch, HEAD, worktree, "
            "repository identity, or main ancestry with Git/GitHub. They are facts, "
            "not conclusions. Retrieve only narrower additional evidence that your "
            "capability review genuinely requires. This snapshot expires at any "
            "repository mutation, validation, PR/merge, finalization, or cleanup boundary."
        )
    prompt["invocation_read_reuse"] = (
        "Within this one reviewer invocation, reuse already inspected immutable file "
        "content for factual inspection rather than accidentally rereading it. "
        "Do not share content, conclusions, or reasoning with another reviewer or "
        "Run ID. Reread after an edit, generated-artifact refresh, repository "
        "change, validation, PR/merge, finalization, cleanup, or whenever freshness "
        "is uncertain. Deliberate verification reads remain required."
    )
    return json.dumps(prompt, sort_keys=True)

# Selected SA-SEL/SA-LOOP contract. This extends the existing capability route;
# mandatory Q/S output and approval contracts remain separate.
SPECIALIST_CONTRACT_VERSION = "2.0"
SPECIALIST_PROFILE_VERSION = "specialist-selection-v1"
MAX_OPTIONAL_INVOCATIONS = 2
SPECIALIST_FIELDS = frozenset({"id", "summary", "path", "evidence_ref", "proposed_disposition"})
_BINDING_FIELDS = frozenset({"run_id", "repository", "request_id", "invocation_id", "candidate_sha", "source_digest", "profile_digest", "reviewer", "consumer"})
_EVENT_FIELDS = _BINDING_FIELDS | {"kind", "payload"}
_ROLE_PATHS = {
    "apple_platform": ("apps/apple/",), "windows_platform": ("apps/windows/",),
    "home_assistant_integration": ("custom_components/",), "esphome_firmware": ("esphome/", "firmware/"),
    "pi_renderer": ("pi/", "renderer/"), "universal_receiver": ("receiver/",),
    "website": ("website/", "web/"), "api": ("api/", "src/api", "src/http", "src/server", "src/engineering_platform/server", "src/engineering_platform/platform_api"),
    "repository_governance": (".github/", "BOOTSTRAP.md"),
    "validation": ("tests/", "src/", "tools/"), "documentation": ("docs/", "README.md"),
    "finalization": ("docs/", "README.md"),
}


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_text(value: object, limit: int = 240) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and 0 < len(value) <= limit and value == redact_diagnostic(value, limit=limit)
            and not re.search(r"https?://|[`\x00-\x1f]|(?:private reasoning|transcript|bearer|password|api.key)\b", value, re.I))


def _safe_path(value: object) -> bool:
    return (isinstance(value, str) and _safe_text(value) and not value.startswith(("/", "~"))
            and "\\" not in value and all(part not in {"", ".", "..", ".git"} for part in value.split("/")))


def specialist_requests(objective: str) -> tuple[dict[str, object], ...]:
    """Read advisory questions only from the approved, explicit declaration.

    These are questions and existing scope references, never capability grants.
    Ambiguous/invalid declarations select nothing and have a durable skip reason.
    """
    lines = [line.split(":", 1)[1].strip() for line in objective.splitlines()
             if line.startswith("Specialist review requests:")]
    if not lines:
        return ()
    if len(lines) != 1 or len(lines[0]) > 4000:
        raise ValueError("ambiguous_specialist_requests")
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise ValueError("invalid_specialist_requests") from error
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValueError("invalid_specialist_requests")
    return tuple(raw)




def specialist_findings(selection: ReviewerSelection, result: ReviewerResult) -> tuple[dict[str, str], ...]:
    """Reject untrusted output that is foreign, stale, executable or approval-shaped."""
    if (result.failed or result.recommendations or not _safe_text(result.contribution)
            or result.contract_version != SPECIALIST_CONTRACT_VERSION
            or result.specialist_binding != selection.specialist_binding or result.reviewer != selection.reviewer
            or not isinstance(result.findings, tuple) or len(result.findings) > 8):
        raise ValueError("specialist_result_binding_invalid")
    findings = []
    seen = {}
    for item in result.findings:
        if (not isinstance(item, dict) or set(item) != SPECIALIST_FIELDS or not _safe_text(item.get("id"), 80)
                or not _safe_text(item.get("summary")) or item.get("path") not in selection.specialist_paths
                or item.get("evidence_ref") != "git-blob:" + dict(selection.specialist_source_blobs).get(item.get("path"), "UNAVAILABLE")
                or item.get("proposed_disposition") not in {"ACCEPTED", "REJECTED", "DEFERRED"}):
            raise ValueError("specialist_finding_invalid_or_out_of_scope")
        if item["id"] in seen:
            if seen[item["id"]] != item:
                raise ValueError("conflicting_specialist_finding_id")
            continue
        seen[item["id"]] = item
        fingerprint = _digest({"candidate_sha": selection.specialist_binding["candidate_sha"], "path": item["path"], "summary": item["summary"], "evidence_ref": item["evidence_ref"]})
        identifier = _digest({"request": selection.specialist_binding["request_id"], "provider_id": item["id"]})
        findings.append({**item, "id": identifier, "provider_finding_id": item["id"], "fingerprint": fingerprint})
    return tuple(findings)


def specialist_readback(records: tuple[dict[str, object], ...]) -> dict[str, object]:
    """Project proposal, disposition and host verification as distinct observations."""
    findings = {}
    for event in records:
        payload = event["payload"]
        if event["kind"] == "RESULT":
            for finding in payload.get("findings", []):
                findings[finding["id"]] = {**finding, "request_id": event["request_id"], "invocation_id": event["invocation_id"],
                    "candidate_sha": event["candidate_sha"], "repository": event["repository"], "reviewer": event["reviewer"], "source_digest": event["source_digest"], "profile_digest": event["profile_digest"],
                    "consumer": event["consumer"], "disposition": "PROPOSED"}
        if event["kind"] in {"DISPOSITION", "APPLICATION", "VERIFICATION"}:
            finding = findings.get(payload["finding_id"])
            if finding is not None:
                finding.update(payload)
    return {"decisions": [r for r in records if r["kind"] == "SELECTION"], "dispatch_skips": [r for r in records if r["kind"] == "SKIP"],
            "reserved_invocation_count": sum(r["kind"] == "DISPATCH" for r in records), "completed_invocation_count": sum(r["kind"] == "RESULT" for r in records), "uncertain_invocation_count": sum(r["kind"] == "UNCERTAIN" for r in records), "findings": list(findings.values()), "duplicate_observations": sum(r["payload"].get("duplicates", 0) for r in records if r["kind"] == "RESULT")}


def specialist_dispositions(records: tuple[dict[str, object], ...], supplied: object, *, candidate_sha: str,
                            changed_paths: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    """The existing primary owner must explicitly consume every proposed finding."""
    pending = {f["id"]: f for f in specialist_readback(records)["findings"] if f["disposition"] == "PROPOSED"}
    if not pending and supplied in ((), []):
        return ()
    # Exact replay of already-consumed primary output is idempotent. Changed
    # candidate/evidence must go through actual validation, never relabelling.
    prior = {r["payload"]["finding_id"]: r["payload"] for r in records if r["kind"] == "DISPOSITION"}
    applications = {r["payload"]["finding_id"]: r["payload"] for r in records if r["kind"] == "APPLICATION"}
    if isinstance(supplied, (tuple, list)):
        fresh = []
        replayed = set()
        for item in supplied:
            if isinstance(item, dict) and item.get("finding_id") in prior:
                old = prior[item["finding_id"]]
                expected = {k: old[k] for k in ("finding_id", "disposition", "reason", "changed_paths")}
                if item.get("disposition") == "IMPLEMENTED" and item["finding_id"] in applications:
                    expected["disposition"] = "IMPLEMENTED"
                    expected["changed_paths"] = applications[item["finding_id"]]["changed_paths"]
                if item != expected or old["result_candidate_sha"] != candidate_sha or item["finding_id"] in replayed:
                    raise ValueError("conflicting_specialist_disposition_replay")
                replayed.add(item["finding_id"])
            else:
                fresh.append(item)
        supplied = tuple(fresh)
    if (not isinstance(supplied, (tuple, list)) or len(supplied) != len(pending)
            or any(not isinstance(p, dict) for p in supplied)
            or {p.get("finding_id") for p in supplied} != set(pending)):
        raise ValueError("specialist_disposition_set_invalid")
    events = []
    for item in supplied:
        if (set(item) != {"finding_id", "disposition", "reason", "changed_paths"}
                or item["disposition"] not in {"ACCEPTED", "REJECTED", "DEFERRED", "IMPLEMENTED"}
                or not _safe_text(item["reason"]) or not isinstance(item["changed_paths"], (tuple, list))):
            raise ValueError("specialist_disposition_invalid")
        finding = pending[item["finding_id"]]
        if item["disposition"] == "IMPLEMENTED":
            if list(item["changed_paths"]) != [finding["path"]] or finding["path"] not in changed_paths or candidate_sha == finding["candidate_sha"]:
                raise ValueError("specialist_implementation_evidence_invalid")
        elif item["changed_paths"]:
            raise ValueError("specialist_disposition_cannot_claim_change")
        original = next(r for r in records if r["kind"] == "RESULT" and r["request_id"] == finding["request_id"])
        events.append({**original, "kind": "DISPOSITION", "payload": {**item, "changed_paths": [], "disposition": "ACCEPTED" if item["disposition"] == "IMPLEMENTED" else item["disposition"], "result_candidate_sha": candidate_sha}})
        if item["disposition"] == "IMPLEMENTED":
            events.append({**original, "kind": "APPLICATION", "payload": {"finding_id": item["finding_id"], "disposition": "IMPLEMENTED", "changed_paths": list(item["changed_paths"]), "result_candidate_sha": candidate_sha}})
    return tuple(events)


def validate_specialist_records(records: object, *, run_id: str, repository: str) -> None:
    """Validate the bounded capability journal before storage or public readback."""
    if not isinstance(records, (tuple, list)) or len(records) > 80:
        raise ValueError("specialist_records_invalid")
    selections, dispatches, results, dispositions, applications = {}, {}, {}, {}, {}
    seen = set()
    terminal_requests = set()
    consumer_result = None
    for event in records:
        if (not isinstance(event, dict) or set(event) != _EVENT_FIELDS or event.get("run_id") != run_id
                or not isinstance(event.get("reviewer"), str) or event["reviewer"] not in REVIEWER_ORDER
                or event.get("consumer") != "EXECUTE_AGENT" or event.get("repository") != repository
                or not re.fullmatch(r"[0-9a-f]{40}", str(event.get("candidate_sha")))
                or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(event.get(key))) for key in ("request_id", "source_digest", "profile_digest"))
                or not isinstance(event.get("payload"), dict)):
            raise ValueError("specialist_record_identity_invalid")
        kind, payload, rid = event["kind"], event["payload"], event["request_id"]
        identity = (rid, kind, payload.get("finding_id", ""))
        if not isinstance(identity[2], str) or identity in seen:
            raise ValueError("specialist_event_duplicate")
        seen.add(identity)
        if kind == "SELECTION":
            if (event["invocation_id"] != "" or set(payload) != {"status", "reason", "question", "paths", "source_blobs", "risk"}
                    or payload["status"] not in {"SELECTED", "SKIPPED"} or not _safe_text(payload["reason"])
                    or not isinstance(payload["question"], str) or len(payload["question"]) > 240
                    or (payload["question"] and not _safe_text(payload["question"]))
                    or not isinstance(payload["paths"], list) or len(payload["paths"]) > 8
                    or not all(_safe_path(p) for p in payload["paths"])
                    or not isinstance(payload["source_blobs"], dict) or not set(payload["source_blobs"]).issubset(payload["paths"])
                    or any(not re.fullmatch(r"[0-9a-f]{40}", str(sha)) for sha in payload["source_blobs"].values())
                    or (payload["status"] == "SELECTED" and set(payload["source_blobs"]) != set(payload["paths"]))
                    or payload["risk"] not in {None, "LOW", "NORMAL", "HIGH"}):
                raise ValueError("specialist_selection_invalid")
            selections[rid] = event
            continue
        selection = selections.get(rid)
        if kind == "SKIP":
            if (selection is None or event["invocation_id"] != "" or set(payload) != {"reason"}
                    or not _safe_text(payload["reason"]) or rid in dispatches
                    or any(event[k] != selection[k] for k in _BINDING_FIELDS)):
                raise ValueError("specialist_dispatch_skip_invalid")
            terminal_requests.add(rid)
            continue
        if (selection is None or selection["payload"]["status"] != "SELECTED"
                or not re.fullmatch(r"[0-9a-f]{32}", str(event["invocation_id"]))
                or any(event[k] != selection[k] for k in _BINDING_FIELDS - {"invocation_id"})):
            raise ValueError("specialist_event_binding_invalid")
        if kind == "DISPATCH":
            if payload != {"status": "DISPATCHED"} or len(dispatches) >= MAX_OPTIONAL_INVOCATIONS or rid in terminal_requests:
                raise ValueError("specialist_dispatch_allowance_invalid")
            dispatches[rid] = event
            continue
        dispatch = dispatches.get(rid)
        if dispatch is None or dispatch["invocation_id"] != event["invocation_id"]:
            raise ValueError("specialist_dispatch_missing")
        if kind == "UNCERTAIN":
            if set(payload) != {"reason"} or not _safe_text(payload["reason"]) or rid in terminal_requests:
                raise ValueError("specialist_uncertainty_invalid")
            terminal_requests.add(rid)
        elif kind == "RESULT":
            if (set(payload) != {"status", "findings", "duplicates"} or payload["status"] not in {"COMPLETE", "FAILED"}
                    or not isinstance(payload["findings"], list) or len(payload["findings"]) > 8
                    or not isinstance(payload["duplicates"], int) or isinstance(payload["duplicates"], bool)
                    or not 0 <= payload["duplicates"] <= 8 or (payload["status"] == "FAILED" and payload["findings"])
                    or rid in terminal_requests):
                raise ValueError("specialist_result_invalid")
            terminal_requests.add(rid)
            ids = set()
            for finding in payload["findings"]:
                if (not isinstance(finding, dict) or set(finding) != SPECIALIST_FIELDS | {"provider_finding_id", "fingerprint"}
                        or finding["path"] not in selection["payload"]["paths"]
                        or not _safe_text(finding["summary"]) or not _safe_text(finding["provider_finding_id"], 80)
                        or finding["proposed_disposition"] not in {"ACCEPTED", "REJECTED", "DEFERRED"}
                        or finding["evidence_ref"] != "git-blob:" + selection["payload"]["source_blobs"].get(finding["path"], "UNAVAILABLE")
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", finding["id"])
                        or finding["id"] in ids):
                    raise ValueError("specialist_finding_invalid")
                expected = _digest({"candidate_sha": event["candidate_sha"], "path": finding["path"], "summary": finding["summary"], "evidence_ref": finding["evidence_ref"]})
                if finding["fingerprint"] != expected or finding["id"] != _digest({"request": rid, "provider_id": finding["provider_finding_id"]}):
                    raise ValueError("specialist_finding_identity_invalid")
                ids.add(finding["id"]);results[finding["id"]] = event
        elif kind == "CONSUMER_RESULT":
            if (set(payload) != {"consumer_invocation_id", "result_candidate_sha", "result_ref"}
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", str(payload["consumer_invocation_id"]))
                    or not re.fullmatch(r"[0-9a-f]{40}", str(payload["result_candidate_sha"]))
                    or payload["result_ref"] != f"artifact:provider-recovery-result:{run_id}:{payload['consumer_invocation_id']}"):
                raise ValueError("specialist_consumer_result_invalid")
            if consumer_result is not None:
                raise ValueError("specialist_consumer_result_duplicate")
            consumer_result = payload
        elif kind == "DISPOSITION":
            finding_id = payload.get("finding_id")
            if (finding_id not in results or results[finding_id]["request_id"] != rid
                    or set(payload) != {"finding_id", "disposition", "reason", "changed_paths", "result_candidate_sha"}
                    or payload["disposition"] not in {"ACCEPTED", "REJECTED", "DEFERRED", "DUPLICATE"}
                    or not _safe_text(payload["reason"]) or payload["changed_paths"] != []
                    or (payload["disposition"] != "DUPLICATE" and (consumer_result is None or consumer_result["result_candidate_sha"] != payload["result_candidate_sha"]))
                    or not re.fullmatch(r"[0-9a-f]{40}", str(payload["result_candidate_sha"]))):
                raise ValueError("specialist_disposition_invalid")
            dispositions[finding_id] = event
        elif kind == "APPLICATION":
            finding_id = payload.get("finding_id")
            prior = dispositions.get(finding_id)
            result = results.get(finding_id)
            if (prior is None or result is None or prior["payload"]["disposition"] != "ACCEPTED"
                    or set(payload) != {"finding_id", "disposition", "changed_paths", "result_candidate_sha"}
                    or payload["disposition"] != "IMPLEMENTED" or payload["changed_paths"] != [next(f["path"] for f in result["payload"]["findings"] if f["id"] == finding_id)]
                    or payload["result_candidate_sha"] != prior["payload"]["result_candidate_sha"]
                    or payload["result_candidate_sha"] == event["candidate_sha"]):
                raise ValueError("specialist_application_invalid")
            applications[finding_id] = event
        elif kind == "VERIFICATION":
            finding_id = payload.get("finding_id")
            prior = applications.get(finding_id)
            if (prior is None or set(payload) != {"finding_id", "disposition", "result_candidate_sha", "validation_profile_digest", "control_refs"}
                    or payload["disposition"] != "VERIFIED" or payload["result_candidate_sha"] != prior["payload"]["result_candidate_sha"]
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload["validation_profile_digest"]))
                    or not isinstance(payload["control_refs"], list) or not payload["control_refs"]
                    or len(payload["control_refs"]) > 16 or not all(_safe_text(ref) for ref in payload["control_refs"])):
                raise ValueError("specialist_verification_invalid")
        else:
            raise ValueError("specialist_event_kind_invalid")


def specialist_verified(records: tuple[dict[str, object], ...], context: dict[str, object], *, candidate_sha: str) -> tuple[dict[str, object], ...]:
    """Append verified only from the existing complete, current control receipts."""
    from .validation_profile import strict_required_controls_pass
    if context.get("candidate_sha") != candidate_sha or not strict_required_controls_pass(context, candidate_sha=candidate_sha, currentness=context.get("currentness")):
        return ()
    events = []
    for finding in specialist_readback(records)["findings"]:
        if finding["disposition"] != "IMPLEMENTED" or finding["result_candidate_sha"] != candidate_sha:
            continue
        original = next(r for r in records if r["kind"] == "APPLICATION" and r["payload"]["finding_id"] == finding["id"])
        events.append({**original, "kind": "VERIFICATION", "payload": {"finding_id": finding["id"], "disposition": "VERIFIED",
            "result_candidate_sha": candidate_sha, "validation_profile_digest": context["profile_digest"],
            "control_refs": [context["controls"][name]["command_id"] for name in context["required_validation_controls"]]}})
    return tuple(events)
