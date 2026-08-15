"""Terminal-report persistence coordination for the Execution Host."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .execution_errors import RunnerError


class ReportingCoordinator:
    """Own report delivery and validation; lifecycle remains caller-owned."""

    def deliver(
        self,
        *,
        path: Path,
        body: str,
        validate: Callable[[str], tuple[str, ...]],
        terminal_matches: Callable[[str], bool],
    ) -> Path:
        errors = validate(body)
        if not terminal_matches(body) or errors:
            details = "; ".join(errors) or "terminal state validation failed"
            raise RunnerError(f"Engineering Report consistency validation failed: {details}")
        path.write_text(body, encoding="utf-8")
        return path


# Report formatting is deliberately pure: lifecycle only supplies the persisted
# transaction and evidence inputs.
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Mapping

from .agent_state import TransactionState, redact_diagnostic
from .capability_review import ReviewerResult
from .drift_diagnostics import summary as drift_summary
from .execution_evidence import TerminalEvidenceBundle
from .execution_lease import history as lease_history, liveness as lease_liveness
from .execution_models import PullRequestEvidence, RepositoryEvidence
from .execution_readiness import ReadinessDecision
from .host_preflight import latest as latest_host_preflight
from .workspace_preflight import latest as latest_workspace_preflight
from .capability_preflight import latest as latest_capability_preflight
from .platform_version import EngineeringPlatformManifest
from .producer import ProducerMetadata, parse_producer_metadata
from .providers import GitProvider
from .qualification import latest_qualification
from .recommendation_handoff import ForgeGovernanceHandoff, report_lines as recommendation_handoff_report_lines
from .status_model import build as build_canonical_status, publish as publish_canonical_status
from .storage import EngineeringStorageError, load_readiness_evaluation, load_submission_for_run

RETRY_REPORT_HEADERS = {
    "retry_of": re.compile(r"(?mi)^retry[ _-]of\s*:\s*(inbox-[a-z0-9-]{6,64})\s*$"),
    "original_run_id": re.compile(r"(?mi)^original[ _-]run[ _-]id\s*:\s*(inbox-[a-z0-9-]{6,64})\s*$"),
    "retry_generation": re.compile(r"(?mi)^retry[ _-]generation\s*:\s*(\d+)\s*$"),
    "retry_timestamp": re.compile(r"(?mi)^retry[ _-]timestamp\s*:\s*([^\n]{1,80})\s*$"),
}


def _retry_relationship(state: TransactionState) -> tuple[str, ...]:
    """Render only explicit retry lineage, never the submitted prompt body."""
    try:
        prompt = Path(state.prompt_path).read_text(encoding="utf-8")
    except OSError:
        return ()
    values = {key: pattern.search(prompt) for key, pattern in RETRY_REPORT_HEADERS.items()}
    parent = values["retry_of"]
    if parent is None:
        return ()
    original = values["original_run_id"].group(1) if values["original_run_id"] else parent.group(1)
    generation = values["retry_generation"].group(1) if values["retry_generation"] else "1"
    timestamp = values["retry_timestamp"].group(1).strip() if values["retry_timestamp"] else "not recorded"
    return (
        "## Retry Relationship",
        f"- Retry Of: `{parent.group(1)}`",
        f"- Original Run: `{original}`",
        f"- Retry Generation: `{generation}`",
        f"- Retry Timestamp: {timestamp}",
        f"- Current Run: `{state.run_id}`",
        f"- Terminal State: `{state.phase}`",
        f"- Repository Context: `{state.repository}`",
        "",
    )


REPORT_REQUIREMENT_EXCLUDED_HEADINGS = frozenset({"context", "canonical principle"})


def _component_inventory(bundle: TerminalEvidenceBundle) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Derive architectural components from changed implementation files."""
    components: dict[str, list[str]] = {}
    for path in bundle.changed_files:
        if path.startswith("tests/") or path.endswith(".md"):
            continue
        lower = path.casefold()
        if path == "tools/engineering/execution_host.py":
            name = "Engineering Report Generator"
        elif path.startswith("tools/engineering/assets/") or path == "tools/engineering/dashboard.py":
            name = "Engineering Evidence Dashboard"
        elif "report_analysis" in lower:
            name = "Engineering Report Analysis"
        elif path.startswith("tools/engineering/"):
            name = Path(path).stem.replace("_", " ").title()
        else:
            name = Path(path).stem.replace("_", " ").title()
        components.setdefault(name, []).append(path)
    return tuple((name, tuple(sorted(paths))) for name, paths in sorted(components.items()))


def _objective_requirements(objective: str) -> tuple[str, ...]:
    """Extract reportable requirements from prompt sections without manual metadata."""
    heading: str | None = None
    requirements: list[str] = []
    for line in objective.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().casefold()
            continue
        value = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).strip()
        if (
            heading
            and heading not in REPORT_REQUIREMENT_EXCLUDED_HEADINGS
            and value
            and not value.startswith("```")
            and not re.match(r"^(?:execution mode|target repository):", value, re.IGNORECASE)
        ):
            requirements.append(value)
    if requirements:
        return tuple(dict.fromkeys(requirements))
    first = next((line.strip() for line in objective.splitlines() if line.strip()), "Objective unavailable.")
    return (first,)


def _deliverable_answer(objective: str, state: TransactionState) -> str:
    """Answer explicit binary delivery requests from the persisted terminal state."""
    requested = re.search(r"\bYES\b|\bPASS\b|\bGO\b|\bNO-GO\b", objective, re.IGNORECASE)
    if not requested:
        return "Not explicitly requested by the prompt."
    if state.phase == "COMPLETE":
        return "YES / PASS / GO — the persisted terminal checkpoint is COMPLETE."
    if state.phase == "BLOCKED":
        return "NO / FAIL / NO-GO — the persisted terminal checkpoint is BLOCKED."
    return "NO / FAIL / NO-GO — the persisted terminal checkpoint is FAILED."

def _next_action_message(action: str) -> str:
    return {
        "external_action_required": "Resolve the reported external dependency, then resume the run.",
        "external_merge_authorization_required": "Obtain the required merge authorization.",
        "required_checks_failed": "Inspect and resolve the failed required CI check.",
        "inspect_codex_cli": "Inspect the redacted Codex CLI details above, then resume after correction.",
    }.get(action, "Inspect current repository and GitHub evidence before resuming.")


def _format_terminal_report(state: TransactionState) -> str:
    return f"{state.phase}\n\nReason:\n{state.diagnostic or 'No safe diagnostic was available.'}\n\nNext action:\n{_next_action_message(state.next_action)}"


def _persisted_producer_submission(root: Path, state: TransactionState, fallback_prompt: str) -> tuple[ProducerMetadata, dict[str, object] | None]:
    """Use immutable Producer submission evidence before legacy prompt compatibility."""
    try:
        submission = load_submission_for_run(root, state.run_id)
    except EngineeringStorageError:
        submission = None
    if submission is None:
        return parse_producer_metadata(fallback_prompt), None
    return ProducerMetadata(
        producer_id=str(submission["producer_id"]), producer_type=str(submission["producer_type"]),
        producer_version=submission.get("producer_version") if isinstance(submission.get("producer_version"), str) else None,
        correlation_id=submission.get("correlation_id") if isinstance(submission.get("correlation_id"), str) else None,
        mission_id=submission.get("mission_id") if isinstance(submission.get("mission_id"), str) else None,
        engineering_action_id=submission.get("engineering_action_id") if isinstance(submission.get("engineering_action_id"), str) else None,
    ), submission


def _producer_submission_contract_lines(submission: dict[str, object] | None, state: TransactionState) -> tuple[str, ...]:
    context = submission.get("execution_context") if isinstance(submission, dict) else None
    return (
        "## Producer Submission Contract",
        f"- Submission ID: `{submission.get('submission_id') if submission else 'legacy'}`",
        f"- Contract Version: `{submission.get('contract_version') if submission else 'legacy prompt'}`",
        "- Submission Status: `PERSISTED_IMMUTABLY`",
        "",
        "## Execution Context Contract",
        f"- Execution Context Status: `{'SUPPLIED' if isinstance(context, dict) else 'NOT_SUPPLIED_BY_PRODUCER'}`",
        f"- Execution Context Version: `{submission.get('execution_context_version') if isinstance(context, dict) else 'not supplied'}`",
        "- Snapshot: " + (json.dumps(context, sort_keys=True) if isinstance(context, dict) else "Not supplied by Producer."),
        f"- Execution Status: `{state.phase}`",
        "",
    )


def _repository_summary(evidence: RepositoryEvidence) -> str:
    return redact_diagnostic(
        f"branch={evidence.branch}; head={evidence.head_sha}; clean={evidence.clean}; main_contains_head={evidence.main_contains_head}"
    )


def _pull_request_summary(evidence: PullRequestEvidence) -> str:
    failed = ",".join(evidence.failed_checks) or "none"
    return redact_diagnostic(
        f"pr={evidence.number}; state={evidence.state}; terminal={evidence.checks_terminal}; passed={evidence.checks_passed}; failed_checks={failed}"
    )


def _git_output(root: Path, *args: str) -> str | None:
    """Return bounded Git output without allowing evidence collection to affect a run."""
    try:
        result = GitProvider().execute(root, "git", *args)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _target_workspace(root: Path, state: TransactionState) -> Path:
    """Resolve the engineering target without changing execution selection."""
    return Path(state.genesis_repository_path).expanduser().resolve() if state.execution_mode == "GENESIS" and state.genesis_repository_path else root.resolve()


def _target_repository_name(target: Path, fallback: str) -> str:
    remote = _git_output(target, "remote", "get-url", "origin")
    if remote:
        return remote.removesuffix(".git").split(":")[-1].replace("github.com/", "")
    return fallback


def _evidence_baseline(state: TransactionState, target: Path, target_commit: str) -> str | None:
    """Find the parent preceding the terminal transaction when Git can prove it."""
    first_commit = (
        state.genesis_commit_sha
        if state.execution_mode == "GENESIS"
        else state.implementation_merge_commit or state.finalization_merge_commit
    )
    if not first_commit:
        return None
    parent = _git_output(target, "rev-parse", f"{first_commit}^")
    return parent if parent and _git_output(target, "rev-parse", target_commit) else None


def collect_terminal_evidence(root: Path, state: TransactionState) -> TerminalEvidenceBundle:
    """Collect a bounded, read-only target-repository evidence bundle."""
    target = _target_workspace(root, state)
    branch = _git_output(target, "branch", "--show-current") or "unavailable"
    commit = state.genesis_commit_sha or _git_output(target, "rev-parse", "HEAD") or "unavailable"
    status = _git_output(target, "status", "--porcelain", "--untracked-files=all")
    worktree = "unavailable" if status is None else ("clean" if not status else "dirty")
    baseline = _evidence_baseline(state, target, commit)
    root_genesis_commit = state.execution_mode == "GENESIS" and state.genesis_commit_sha == commit
    names = (
        _git_output(target, "diff", "--name-status", baseline, commit)
        if baseline
        else _git_output(target, "diff-tree", "--root", "--no-commit-id", "-r", "--name-status", commit)
        if root_genesis_commit
        else None
    )
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    if names:
        for row in names.splitlines():
            status_code, _, path = row.partition("\t")
            if not path:
                continue
            if status_code.startswith("A"):
                added.append(path)
            elif status_code.startswith("D"):
                removed.append(path)
            else:
                modified.append(path)
    changed = tuple(sorted(set(added + modified + removed)))
    diff = (
        _git_output(target, "diff", "--check", baseline, commit)
        if baseline
        else _git_output(target, "diff-tree", "--root", "--check", commit)
        if root_genesis_commit
        else _git_output(target, "diff", "--check")
    )
    diff_check = "PASS" if diff == "" else "FAIL" if diff is not None else "UNAVAILABLE"
    resulting_commit = (
        state.genesis_commit_sha if state.execution_mode == "GENESIS" else
        state.finalization_merge_commit or state.implementation_merge_commit or state.implementation_head_sha
    )
    return TerminalEvidenceBundle(
        target_workspace=str(target),
        # Genesis evidence belongs to the selected local target, never to the
        # Engineering Platform host repository when that target has no origin.
        target_repository=_target_repository_name(
            target,
            target.name if state.execution_mode == "GENESIS" else state.repository,
        ),
        target_branch=branch,
        target_commit=commit,
        worktree_state=worktree,
        changed_files=changed,
        files_added=tuple(added),
        files_modified=tuple(modified),
        files_removed=tuple(removed),
        diff_check=diff_check,
        transaction_baseline="AVAILABLE" if baseline or root_genesis_commit else "UNAVAILABLE",
        resulting_commit=resulting_commit,
        lease=lease_history(root, state.run_id),
        readiness=load_readiness_evaluation(root, state.run_id),
    )


def _evidence_lines(label: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        return (f"- {label}: none recorded",)
    return tuple(f"- {label}: `{value}`" for value in values)


def _implementation_evidence(bundle: TerminalEvidenceBundle) -> str:
    """Classify file-level evidence without inferring unrecorded implementation intent."""
    changed = bundle.changed_files
    groups = {
        "Implemented components": tuple(path for path in changed if path.startswith("tools/engineering/")),
        "Updated models": tuple(path for path in changed if "model" in path.casefold() or "state" in path.casefold()),
        "Updated documentation": tuple(path for path in changed if path.endswith(".md")),
        "Updated tests": tuple(path for path in changed if path.startswith("tests/") or "/test_" in path),
        "Updated contracts": tuple(path for path in changed if any(token in path.casefold() for token in ("contract", "schema", "openapi"))),
        "Updated schemas": tuple(path for path in changed if path.endswith((".json", ".yaml", ".yml"))),
    }
    lines: list[str] = []
    for label, files in groups.items():
        lines.extend(_evidence_lines(label, files))
    return "\n".join(lines)


def _component_inventory_lines(bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    inventory = _component_inventory(bundle)
    if not inventory:
        return ("- No implementation components were detected from changed repository files.",)
    lines: list[str] = []
    for component, files in inventory:
        lines.append(f"- Component: `{component}`")
        lines.extend(f"  - Repository file: `{path}`" for path in files)
        lines.extend(
            f"  - Change classification: `{classification}`"
            for classification, candidates in (
                ("added", bundle.files_added),
                ("modified", bundle.files_modified),
                ("removed", bundle.files_removed),
            )
            if any(path in candidates for path in files)
        )
    lines.append("- Generated Components: none recorded by repository evidence.")
    return tuple(lines)


def _commit_strategy(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    if state.execution_mode == "GENESIS":
        return (
            "- Strategy: `Genesis Local Commit`",
            f"- Resulting local commit: `{state.genesis_commit_sha or bundle.target_commit}`",
        )
    if state.finalization_merge_commit:
        strategy = "Managed Merge"
    elif state.implementation_pull_request:
        strategy = "Managed Pull Request"
    else:
        strategy = "Finalization" if state.transaction_kind == "FINALIZATION" else "Managed execution"
    return (
        f"- Strategy: `{strategy}`",
        f"- Implementation PR: `{state.implementation_pull_request or 'not recorded'}`",
        f"- Implementation merge: `{state.implementation_merge_commit or 'not recorded'}`",
        f"- Finalization PR: `{state.finalization_pull_request or 'not recorded'}`",
        f"- Finalization merge: `{state.finalization_merge_commit or 'not recorded'}`",
    )


def _branch_traceability(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    preflight = state.branch or "not recorded"
    execution = state.implementation_branch or state.branch or bundle.target_branch
    final_branch = bundle.target_branch
    transition = "unchanged" if preflight == execution == final_branch else "recorded lifecycle transition"
    mutation = "NONE" if bundle.resulting_commit is None else ("NONE" if not bundle.changed_files else "RECORDED")
    return (
        f"- Initial Repository Baseline: `{bundle.transaction_baseline}`",
        f"- Repository Mutation: `{mutation}`",
        f"- Files Changed By This Execution: {', '.join(f'`{path}`' for path in bundle.changed_files) or 'NONE'}",
        f"- Preflight branch: `{preflight}`",
        f"- Execution branch: `{execution}`",
        f"- Final repository branch: `{final_branch}`",
        f"- Final repository commit: `{bundle.target_commit}`",
        f"- Resulting New Commit: `{bundle.resulting_commit or 'NONE'}`",
        f"- Target Commit: `{bundle.target_commit}`",
        f"- Repository state transition: {transition}.",
    )


def _requirement_traceability(objective: str, state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    requirements = _objective_requirements(objective)
    components = _component_inventory(bundle)
    component_names = ", ".join(f"`{name}`" for name, _ in components) or "No implementation component detected"
    files = ", ".join(f"`{path}`" for path in bundle.changed_files) or "No changed files recorded"
    tests = ", ".join(f"`{path}`" for path in bundle.changed_files if path.startswith("tests/")) or "No regression test file recorded"
    validation = "; ".join(item["result"] for item in state.validation_evidence) or "Not recorded by the runner"
    lines: list[str] = []
    for requirement in requirements:
        lines.extend((
            f"- Requirement: {requirement}",
            f"  - Implemented component: {component_names}",
            f"  - Repository files: {files}",
            f"  - Runtime evidence: run `{state.run_id}`; execution mode `{state.execution_mode}`.",
            f"  - Execution evidence: terminal checkpoint `{state.phase}`.",
            f"  - Regression tests: {tests}",
            f"  - Validation evidence: {validation}",
            "  - Report evidence: this immutable Engineering Report.",
        ))
    return tuple(lines)


def _validation_traceability(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    records = list(state.validation_evidence)
    records.append({"command": "git diff --check", "result": bundle.diff_check})
    records.append({"command": "Transaction Baseline", "result": bundle.transaction_baseline})
    records.append({"command": "Documentation validation", "result": "report documentation is rendered from the canonical reporting contract"})
    return tuple(
        line
        for record in records
        for line in (
            f"- Executed validation: `{record['command']}`",
            "  - Purpose: repository regression, quality or documentation evidence.",
            f"  - Result: {record['result']}",
            "  - Repository evidence: persisted terminal checkpoint and Evidence Bundle.",
        )
    )


def _execution_statistics(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    return (
        "- Execution Count: `1`",
        f"- Engineering Actions: `{len(bundle.changed_files) + len(state.validation_evidence)}` evidence-backed action(s)",
        "- Mission Count (Forge): `0` (Forge is outside this reporting increment)",
        f"- Repair Iterations: `{state.repair_iterations}`",
        f"- Execution Duration: `{state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'}` seconds",
        f"- Validation Duration: `not measured` ({len(state.validation_evidence)} recorded validation(s))",
    )


def _statistics_projection(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    """Project separately scoped metrics without inferring mission completion."""
    return (
        "### Mission Statistics",
        "- Mission Count: `0` (Forge mission state is not inferred by Engineering Platform).",
        "### Execution Statistics",
        "- Execution Count: `1`",
        f"- Execution Duration: `{state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'}` seconds",
        "### Engineering Action Statistics",
        f"- Evidence-backed actions: `{len(bundle.changed_files) + len(state.validation_evidence)}`",
        "### Runtime Statistics",
        "- Runtime execution count: `1` for this report-bound Run ID.",
    )


def _deliverable_projection(
    objective: str,
    state: TransactionState,
    bundle: TerminalEvidenceBundle,
    handoff: ForgeGovernanceHandoff | None = None,
) -> tuple[str, ...]:
    """Project requested outcomes and repository artefacts without claiming intent."""
    requested = _objective_requirements(objective)
    delivered = bundle.changed_files if state.phase == "COMPLETE" else ()
    documentation = tuple(path for path in delivered if path.endswith(".md"))
    validation = tuple(path for path in delivered if path.startswith("tests/"))
    runtime = tuple(path for path in delivered if path.startswith("tools/engineering/"))
    projection = (
        "### Requested Deliverables",
        *(f"- Requested: {item}" for item in requested),
        "### Delivered Artefacts",
        *_evidence_lines("Delivered artifact", delivered),
        "### Undelivered Artefacts",
        *(
            ("- None recorded: terminal checkpoint is COMPLETE.",)
            if state.phase == "COMPLETE"
            else ("- Requested deliverables are not claimed as delivered by this terminal checkpoint.",)
        ),
        "### Runtime Deliverables",
        *_evidence_lines("Runtime deliverable", runtime),
        "### Documentation Deliverables",
        *_evidence_lines("Documentation deliverable", documentation),
        "### Validation Deliverables",
        *_evidence_lines("Validation deliverable", validation),
    )
    if handoff is None:
        return projection
    return (
        *projection,
        "### Forge Governance Handoff Deliverable",
        "- Governance values are rendered only in the dedicated read-only handoff section.",
    )


def _qualification_projection(
    state: TransactionState,
    qualification_status: object,
    runtime_provider: str,
) -> tuple[str, ...]:
    """Keep execution, qualification, runtime and governance outcomes distinct."""
    validation = "recorded" if state.validation_evidence else "not recorded"
    return (
        f"- Execution Status: `{state.phase}`",
        f"- Qualification Status: `{qualification_status or 'not recorded'}`",
        f"- Runtime Status: `{'reported' if runtime_provider != 'unavailable' else 'not reported'}`",
        f"- Validation Status: `{validation}`",
        "- Governance Status: see the Forge Governance Handoff projection above.",
    )


def _runtime_projection(
    state: TransactionState,
    producer: ProducerMetadata,
    runtime_provider: str,
    reported_model: str,
) -> tuple[str, ...]:
    """Render only persisted runtime provenance and Producer references."""
    return (
        f"- Runtime Instance: `{state.run_id}`",
        f"- Runtime Identity: provider `{runtime_provider}`; model `{reported_model}`",
        f"- Mission State: `{producer.mission_id or 'not recorded'}`",
        "- Dispatcher: `not recorded by the runner`",
        "- Queue: `not recorded by the runner`",
        f"- Execution Receipt Reference: `{state.run_id}`",
        f"- Decision Evidence Reference: `{producer.correlation_id or producer.engineering_action_id or 'not recorded'}`",
    )


def _execution_receipt_projection(state: TransactionState, producer: ProducerMetadata) -> tuple[str, ...]:
    return (
        f"- Receipt ID: `{state.run_id}`",
        "- Execution Host: `Engineering Platform`",
        f"- Run ID: `{state.run_id}`",
        f"- Correlation ID: `{producer.correlation_id or 'not recorded'}`",
        f"- Receipt Status: `{state.phase}`",
        f"- Receipt Resolution: `{state.terminal_condition}`",
    )


def _decision_evidence_projection(producer: ProducerMetadata) -> tuple[str, ...]:
    if not any((producer.correlation_id, producer.mission_id, producer.engineering_action_id)):
        return ("- No Decision Evidence reference was recorded by the Producer.",)
    return (
        f"- Decision Evidence ID: `{producer.correlation_id or 'not recorded'}`",
        f"- Decision Type: `{producer.producer_type}` provenance reference",
        f"- Mission: `{producer.mission_id or 'not recorded'}`",
        "- Confidence: `not recorded by Engineering Platform`",
        f"- Reasoning Reference: `{producer.engineering_action_id or 'not recorded'}`",
    )


def _evidence_summary(state: TransactionState, bundle: TerminalEvidenceBundle, objective: str) -> str:
    """Return a compact, machine-readable summary derived only from report evidence."""
    return json.dumps(
        {
            "repository_commit": bundle.target_commit,
            "implemented_components": [name for name, _ in _component_inventory(bundle)],
            "regression_coverage": [path for path in bundle.changed_files if path.startswith("tests/")],
            "deliverable_answer": _deliverable_answer(objective, state),
            "commit_strategy": _commit_strategy(state, bundle)[0].removeprefix("- Strategy: `").removesuffix("`"),
            "execution_strategy": state.execution_mode,
            "repository_state": bundle.worktree_state,
        },
        indent=2,
        sort_keys=True,
    )


def report_consistency_errors(body: str, state: TransactionState, bundle: TerminalEvidenceBundle, objective: str) -> tuple[str, ...]:
    """Validate mandatory Evidence 2.0 sections before a report is published."""
    required = (
        "## Component Inventory",
        "## Deliverable Projection",
        "## Qualification Projection",
        "## Runtime Projection",
        "## Execution Receipt Projection",
        "## Decision Evidence Projection",
        "## Statistics Projection",
        "## Commit Strategy",
        "## Branch Traceability",
        "## Requirement Traceability",
        "## Validation Traceability",
        "## Execution Statistics",
        "## Engineering Evidence Summary",
    )
    errors = [f"missing required section: {section}" for section in required if section not in body]
    if "Implemented Components:\n\nnone recorded" in body:
        errors.append("component inventory is missing")
    if re.search(r"\bYES\b|\bPASS\b|\bGO\b|\bNO-GO\b", objective, re.IGNORECASE) and _deliverable_answer(objective, state) not in body:
        errors.append("explicit deliverable answer is missing")
    if bundle.target_commit not in body:
        errors.append("repository commit is missing")
    if state.phase == "COMPLETE" and "## Evidence Bundle" not in body:
        errors.append("complete report is missing Evidence Bundle")
    return tuple(errors)


def _validation_evidence_lines(state: TransactionState) -> tuple[str, ...]:
    if not state.validation_evidence:
        return ("- Executed tests: not recorded by the runner.", "- Test results: not recorded by the runner.")
    return tuple(
        line
        for item in state.validation_evidence
        for line in (
            f"- Executed test: `{item['command']}`",
            f"  - Result: {item['result']}",
        )
    )


def _reconciliation_evidence(objective: str, state: TransactionState, bundle: TerminalEvidenceBundle) -> str:
    if "reconcil" not in objective.casefold():
        return ""
    changed = ", ".join(f"`{path}`" for path in bundle.changed_files) or "no changed files recorded"
    return "\n".join(
        (
            "## Reconciliation Evidence",
            "- Initial classification: not separately persisted by the runner.",
            f"- Final classification: `{state.phase}`.",
            "- Required assessment items: target identity, repository evidence, validation evidence and terminal checkpoint are included in this report.",
            f"- Changes made: {changed}.",
            "- Remaining limitations: historical assessment and per-test execution details are not persisted by the runner.",
            "",
        )
    )


def format_management_summary(state: TransactionState) -> str:
    """Return a checkpoint-only completion summary without exposing prompt text."""
    return "\n".join(
        (
            "COMPLETE — IMPLEMENTATION_AND_FINALIZATION_RECONCILED",
            "Objective: bounded objective recorded at the supplied prompt path.",
            f"Implementation: branch={state.implementation_branch or state.branch}; PR={state.implementation_pull_request}; merge={state.implementation_merge_commit}.",
            f"Repair iterations: {state.repair_iterations}.",
            f"Finalization: branch={state.finalization_branch}; PR={state.finalization_pull_request}; merge={state.finalization_merge_commit}.",
            "Repository Cleanup: fetched and pruned; local main synchronized; transaction branches removed or already absent; workspace clean.",
            "Authority: owner-authorized bounded lifecycle; ready-for-review, merge and Finalization automated.",
            "No release, deployment or publication performed. Rolling Horizon unchanged.",
        )
    )


def format_terminal_management_summary(state: TransactionState) -> str:
    """Return evidence bounded by the persisted terminal checkpoint phase."""
    if state.phase == "COMPLETE":
        return format_management_summary(state)
    outcome = (
        "BLOCKED — no engineering changes were executed or delivered."
        if state.phase == "BLOCKED"
        else "FAILED — the engineering transaction did not complete successfully."
    )
    target = state.genesis_repository_path or state.repository
    codex = (
        "not started"
        if state.terminal_condition in {"genesis_workspace_preflight", "execution_context_resolution"}
        else "not confirmed by the terminal checkpoint"
    )
    return "\n".join(
        (
            outcome,
            f"Execution mode: {state.execution_mode}.",
            f"Target repository: {target}.",
            f"Terminal checkpoint: {state.phase}.",
            f"Codex execution: {codex}.",
            f"Implementation: branch={state.implementation_branch}; PR={state.implementation_pull_request}; merge={state.implementation_merge_commit}.",
            f"Finalization: branch={state.finalization_branch}; PR={state.finalization_pull_request}; merge={state.finalization_merge_commit}.",
            "No release, deployment or publication was performed.",
        )
    )


def terminal_report_matches_state(body: str, state: TransactionState) -> bool:
    """Reject report prose that conflicts with its immutable terminal checkpoint."""
    if f"- Terminal state: `{state.phase}`" not in body:
        return False
    required_sections = (
        "## Initial Repository Assessment",
        "## Engineering Outcome",
        "## Reviewer Findings",
        "## Repository Truth",
        "## Management Summary",
    )
    if any(section not in body for section in required_sections):
        return False
    if "## Execution Target Identity" not in body:
        return False
    if state.phase == "COMPLETE" and "## Evidence Bundle" not in body:
        return False
    if state.phase == "BLOCKED":
        return "BLOCKED — no engineering changes were executed or delivered." in body and "COMPLETE —" not in body
    if state.phase == "FAILED":
        return "FAILED — the engineering transaction did not complete successfully." in body and "COMPLETE —" not in body
    return state.phase == "COMPLETE" and "COMPLETE —" in body


def corrected_terminal_report(state: TransactionState) -> str:
    """Generate a minimal replacement when richer report assembly is inconsistent."""
    try:
        producer_prompt = Path(state.prompt_path).read_text(encoding="utf-8")
    except OSError:
        producer_prompt = ""
    producer = parse_producer_metadata(producer_prompt)
    submission = None
    return "\n".join(
        (
            "# Engineering Report",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Terminal state: `{state.phase}`",
            "",
            "## Producer",
            f"- Producer ID: `{producer.producer_id}`",
            f"- Producer Type: `{producer.producer_type}`",
            f"- Producer Version: `{producer.producer_version or 'not supplied'}`",
            f"- Correlation ID: `{producer.correlation_id or 'not supplied'}`",
            f"- Mission ID: `{producer.mission_id or 'not supplied'}`",
            f"- Engineering Action ID: `{producer.engineering_action_id or 'not supplied'}`",
            f"- Execution Constraint Version: `{producer.execution_constraint_version or 'not supplied'}`",
            "",
            *_producer_submission_contract_lines(submission, state),
            "## Execution Target Identity",
            f"- Execution Host Repository: `{state.repository}`",
            f"- Execution Mode: `{state.execution_mode}`",
            "- Target Workspace: unavailable",
            "- Target Repository: unavailable",
            "- Target Branch: unavailable",
            "- Target Commit: unavailable",
            "",
            "## Initial Repository Assessment",
            "Assessment evidence is unavailable. This section describes only the repository before any attempted implementation.",
            "",
            "## Engineering Outcome",
            format_terminal_management_summary(state),
            "",
            *_retry_relationship(state),
            "## Reviewer Findings",
            "No reviewer findings were retained. Reviewer observations are advisory initial observations only.",
            "",
            "## Repository Truth",
            "Execution Host, Target Repository, Target Commit, Repository Evidence and Evidence Bundle are canonical repository truth.",
            "Priority: persisted repository state, resulting commits, validation results, then reviewer observations.",
            "",
            *(
                (
                    "## Evidence Bundle",
                    "Repository evidence is unavailable because the richer report assembly was inconsistent.",
                    "",
                )
                if state.phase == "COMPLETE"
                else ()
            ),
            "## Management Summary",
            format_terminal_management_summary(state),
            "",
            "## Diagnostics",
            state.diagnostic or "No terminal diagnostic.",
            "",
        )
    )


def _format_reviewer_records(records: tuple[dict[str, object], ...], phase: str) -> str:
    if not records:
        return "No specialist reviewers required. Any future reviewer observations remain advisory initial observations."
    lines: list[str] = []
    for record in records:
        lines.extend(
            (
                f"- Reviewer: {record['reviewer']}",
                f"  - Capability: {record.get('capability', 'engineering')}",
                f"  - Selected because: {record['selected_because']}",
                f"  - Initial observation: {record['contribution']}",
                f"  - Accepted recommendations: {record['accepted_recommendations']}",
                f"  - Rejected recommendations: {record['rejected_recommendations']}",
                "  - Resolved by: implementation evidence, changed components and repository evidence in the Evidence Bundle below."
                if phase == "COMPLETE"
                else "  - Outcome: Not a final repository statement; consult the terminal checkpoint and diagnostics.",
            )
        )
    return "\n".join(lines)


def _format_engineering_outcome(state: TransactionState) -> str:
    """Describe final delivery from checkpoint and repository evidence, never advice."""
    if state.phase != "COMPLETE":
        return "\n".join(
            (
                f"- Final checkpoint: `{state.phase}`",
                "- Completed work: no successful engineering delivery is claimed.",
                f"- Remaining limitation: {state.diagnostic or 'Terminal outcome requires follow-up.'}",
            )
        )
    return "\n".join(
        (
            "- Final checkpoint: `COMPLETE`",
            "- Completed work: implementation and any required reconciliation completed according to the persisted checkpoint.",
            f"- Resulting commits: implementation `{state.implementation_merge_commit or 'not applicable'}`; finalization `{state.finalization_merge_commit or 'not applicable'}`.",
            f"- Repository state: {state.latest_repository_evidence or 'Recorded by the terminal COMPLETE checkpoint.'}",
            "- Remaining limitations: none recorded by the terminal checkpoint.",
        )
    )


def generate_terminal_report(
    root: Path,
    state: TransactionState,
    manifest: EngineeringPlatformManifest | None = None,
    detected_cli: str | None = None,
    reviewer_records: tuple[dict[str, object], ...] = (),
    runtime_metadata: Mapping[str, str] | None = None,
) -> Path:
    """Write one immutable, local-only report for a terminal transaction."""
    reports = root / ".engineering" / "reports"
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = reports / f"{timestamp}_{state.run_id}.md"
    objective = "Objective unavailable because the prompt file is no longer local."
    try:
        objective = Path(state.prompt_path).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    producer, submission = _persisted_producer_submission(root, state, objective)
    raw_handoff = submission.get("forge_governance_handoff") if isinstance(submission, dict) else None
    handoff = ForgeGovernanceHandoff.from_snapshot(raw_handoff) if isinstance(raw_handoff, dict) else None
    manifest = manifest or EngineeringPlatformManifest.load(
        root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
    )
    qualification = latest_qualification(root)
    qualification_summary = (
        "No local Engineering Platform Qualification evidence is available."
        if qualification is None
        else f"Version: `{qualification.get('engineering_platform_version')}`\n- Latest Qualification: `{qualification.get('qualification')}`\n- Executed: `{qualification.get('executed_at')}`\n- Qualification Coverage: `{qualification.get('coverage_percent')}%`"
    )
    runtime_metadata = runtime_metadata or {"runtime_provider": "codex_cli"}
    runtime_provider = runtime_metadata.get("runtime_provider", "unavailable")
    reported_model = runtime_metadata.get("model", "not reported")
    reported_reasoning = runtime_metadata.get("reasoning_profile", "not reported")
    reported_configuration = runtime_metadata.get("configuration_profile", "not reported")
    bundle = collect_terminal_evidence(root, state)
    qualification_status = qualification.get("qualification") if qualification else "not recorded"
    qualification_summary_line = (
        f"`{qualification_status}`" if qualification else "not recorded"
    )
    evidence_bundle = "\n".join(
        (
            "## Evidence Bundle",
            "### Repository Evidence",
            f"- Target repository: `{bundle.target_repository}`",
            f"- Target commit: `{bundle.target_commit}`",
            f"- Worktree state: `{bundle.worktree_state}`",
            *_evidence_lines("Changed file", bundle.changed_files),
            *_evidence_lines("File added", bundle.files_added),
            *_evidence_lines("File modified", bundle.files_modified),
            *_evidence_lines("File removed", bundle.files_removed),
            "",
            "### Validation Evidence",
            *_validation_evidence_lines(state),
            f"- Qualification status: {qualification_summary_line}.",
            "- Schema validation: persisted terminal checkpoint accepted by the report generator.",
            "- Example validation: not recorded by the runner.",
            f"- git diff --check result: {bundle.diff_check}.",
            "",
            "### Implementation Evidence",
            _implementation_evidence(bundle),
            "",
        )
    ) if state.phase == "COMPLETE" else ""
    preflight = latest_host_preflight(root)
    if preflight.get("run_id") not in {None, state.run_id}:
        preflight = {}
    preflight_checks = preflight.get("checks") or [] if isinstance(preflight, dict) else []
    if not isinstance(preflight_checks, (list, tuple)):
        preflight_checks = ()
    preflight_outcome = preflight.get("outcome", "unavailable") if isinstance(preflight, dict) else "unavailable"
    preflight_timestamp = preflight.get("timestamp", "unavailable") if isinstance(preflight, dict) else "unavailable"
    preflight_duration = preflight.get("duration_ms", "unavailable") if isinstance(preflight, dict) else "unavailable"
    preflight_summary = ", ".join(
        f"{item.get('identifier')}={item.get('outcome')}"
        for item in preflight_checks
        if isinstance(item, dict) and isinstance(item.get("identifier"), str)
    ) or "unavailable"
    workspace_preflight = latest_workspace_preflight(root)
    if workspace_preflight.get("run_id") not in {None, state.run_id}:
        workspace_preflight = {}
    workspace_checks = workspace_preflight.get("checks") or [] if isinstance(workspace_preflight, dict) else []
    if not isinstance(workspace_checks, (list, tuple)):
        workspace_checks = ()
    workspace_summary = ", ".join(
        f"{item.get('identifier')}={item.get('outcome')}"
        for item in workspace_checks
        if isinstance(item, dict) and isinstance(item.get("identifier"), str)
    ) or "unavailable"
    capability_preflight = latest_capability_preflight(root)
    if capability_preflight.get("run_id") not in {None, state.run_id}:
        capability_preflight = {}
    drift_evidence = [
        item for preflight in (preflight, workspace_preflight, capability_preflight)
        for item in preflight.get("drift_evidence", [])
        if isinstance(item, dict)
    ]
    liveness = lease_liveness(root, state.run_id)
    recovery = (
        "Resume available under the existing lifecycle policy."
        if liveness.get("reconciliation_outcome") == "RECOVERABLE"
        else "Terminal evidence reconciled."
        if liveness.get("reconciliation_outcome") == "TERMINAL_EVIDENCE_PRESENT"
        else "No recovery action is required."
    )
    body = "\n".join(
        (
            "# Engineering Report",
            "",
            f"- Timestamp: {timestamp}",
            f"- Run ID: `{state.run_id}`",
            f"- Prompt: `{state.prompt_path}`",
            f"- Terminal state: `{state.phase}`",
            f"- Execution lifecycle: `{state.phase}`",
            f"- Execution liveness: `{liveness.get('state', 'UNAVAILABLE')}`",
            f"- Recovery action: {recovery}",
            f"- Objective: {objective}",
            "",
            "## Producer",
            "Forge owns Producer Contract semantics. Engineering Platform consumes this metadata for auditability only.",
            f"- Producer ID: `{producer.producer_id}`",
            f"- Producer Type: `{producer.producer_type}`",
            f"- Producer Version: `{producer.producer_version or 'not supplied'}`",
            f"- Correlation ID: `{producer.correlation_id or 'not supplied'}`",
            f"- Mission ID: `{producer.mission_id or 'not supplied'}`",
            f"- Engineering Action ID: `{producer.engineering_action_id or 'not supplied'}`",
            f"- Execution Constraint Version: `{producer.execution_constraint_version or 'not supplied'}`",
            "",
            *_producer_submission_contract_lines(submission, state),
            "## Execution Target Identity",
            "- Execution Host: `Engineering Platform`",
            f"- Execution Host Repository: `{state.repository}`",
            f"- Execution Mode: `{state.execution_mode}`",
            f"- Target Workspace: `{bundle.target_workspace}`",
            f"- Target Repository: `{bundle.target_repository}`",
            f"- Target Branch: `{bundle.target_branch}`",
            f"- Target Commit: `{bundle.target_commit}`",
            f"- Execution Host Version: `{manifest.platform_version}`",
            f"- Runner Version: `{manifest.runner_version}`",
            f"- Lease Host: `{bundle.lease.get('host_identity', 'unavailable')}`",
            f"- Lease Instance: `{bundle.lease.get('host_instance_id', 'unavailable')}`",
            f"- Lease State: `{bundle.lease.get('lease_state', 'unavailable')}`",
            f"- Readiness Profile: `{(bundle.readiness or {}).get('profile_id', 'unavailable')}`",
            f"- Readiness Profile Version: `{(bundle.readiness or {}).get('profile_version', 'unavailable')}`",
            f"- Readiness Decision: `{(bundle.readiness or {}).get('result', 'unavailable')}`",
            f"- Readiness Failed Requirements: `{', '.join((bundle.readiness or {}).get('failed_requirements', [])) or 'none'}`",
            f"- Bootstrap Contract: `{manifest.bootstrap_contract}`",
            f"- Checkpoint Format: `{manifest.checkpoint_format}`",
            "",
            "## Engineering Platform",
            f"- Platform Version: `{manifest.platform_version}`",
            f"- Runner Version: `{manifest.runner_version}`",
            f"- Bootstrap Contract: `{manifest.bootstrap_contract}`",
            f"- Checkpoint Format: `{manifest.checkpoint_format}`",
            f"- Memory Format: `{manifest.memory_format}`",
            f"- Report Format: `{manifest.report_format}`",
            f"- Runtime Provider: `{runtime_provider}`",
            f"- AI Model: `{reported_model}`",
            f"- Reasoning Profile: `{reported_reasoning}`",
            f"- Configuration Profile: `{reported_configuration}`",
            f"- Codex CLI Version: `{detected_cli or 'unavailable'}`",
            "",
            "## Engineering Platform Qualification",
            qualification_summary,
            "",
            "## Execution Host Preflight",
            f"- Outcome: `{preflight_outcome}`",
            f"- Timestamp: `{preflight_timestamp}`",
            f"- Duration: `{preflight_duration}` ms",
            f"- Checks: {preflight_summary}",
            "",
            "## Workspace Preflight",
            f"- Outcome: `{workspace_preflight.get('outcome', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Workspace: `{workspace_preflight.get('workspace', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Target repository: `{workspace_preflight.get('target_repository', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Canonical target path: `{workspace_preflight.get('canonical_target_path', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Authorization match: `{workspace_preflight.get('authorization_match', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Authorization policy: `{workspace_preflight.get('authorization_policy', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Branch: `{workspace_preflight.get('branch', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Execution mode: `{workspace_preflight.get('execution_mode', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Timestamp: `{workspace_preflight.get('timestamp', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}`",
            f"- Duration: `{workspace_preflight.get('duration_ms', 'unavailable') if isinstance(workspace_preflight, dict) else 'unavailable'}` ms",
            f"- Checks: {workspace_summary}",
            "",
            "## Capability Preflight",
            f"- Outcome: `{capability_preflight.get('outcome', 'unavailable') if isinstance(capability_preflight, dict) else 'unavailable'}`",
            f"- Recoverability: `{capability_preflight.get('recoverability', 'unavailable') if isinstance(capability_preflight, dict) else 'unavailable'}`",
            f"- Failure Origin: `{capability_preflight.get('failure_origin', 'none') if isinstance(capability_preflight, dict) else 'none'}`",
            f"- Recommendation: {capability_preflight.get('recommendation', 'unavailable') if isinstance(capability_preflight, dict) else 'unavailable'}",
            "",
            "## Development Host Drift Diagnostics",
            "- Detected Drift: " + (str(len(drift_evidence)) if drift_evidence else "none"),
            *(
                line
                for item in drift_evidence
                for line in (
                    f"- Drift ID: `{item.get('drift_id', 'unavailable')}`",
                    f"  - Category: `{item.get('category', 'unavailable')}`; Severity: `{item.get('severity', 'unavailable')}`",
                    f"  - Expected State: {item.get('expected_value', 'unavailable')}",
                    f"  - Observed State: {item.get('observed_value', 'unavailable')}",
                    f"  - Blocking Reason: {item.get('affected_component', 'unavailable')}",
                    f"  - Recommended Resolution / Required Action: {item.get('resolution_recommendation', 'unavailable')}",
                    f"  - Affected Component: `{item.get('affected_component', 'unavailable')}`; Affected Repository: `{item.get('affected_repository', 'unavailable')}`; Affected Runtime: `{item.get('affected_runtime', 'unavailable')}`",
                )
            ),
            "- Resume Guidance: " + (
                "Resolve the listed prerequisite, then retry; resume is not appropriate while drift remains."
                if drift_evidence else "No current development-host drift is recorded."
            ),
            "",
            "## Authorization",
            f"- Owner authorization: `{state.owner_authorized}`",
            "- Ready for Review, merge and Finalization authority remain runner-controlled.",
            "",
            "## Lifecycle Timeline",
            f"`INITIALIZE → IMPLEMENTATION → VALIDATION → REPAIR ({state.repair_iterations}) → MERGE → FINALIZATION → REPOSITORY_CLEANUP → {state.phase}`",
            "",
            "## Pull Requests",
            f"- Implementation: branch `{state.implementation_branch}`, PR `{state.implementation_pull_request}`, merge `{state.implementation_merge_commit}`",
            f"- Finalization: branch `{state.finalization_branch}`, PR `{state.finalization_pull_request}`, merge `{state.finalization_merge_commit}`",
            "",
            *_retry_relationship(state),
            "## Initial Repository Assessment",
            "This assessment describes the repository before implementation. Reviewer observations are advisory and cannot describe the final repository state.",
            "",
            "## Engineering Outcome",
            _format_engineering_outcome(state),
            "",
            "## Reviewer Findings",
            "Initial observations only. They are not final repository claims.",
            _format_reviewer_records(reviewer_records, state.phase),
            "",
            "## Repository Truth",
            "Execution Host, Target Repository, Target Commit, Repository Evidence and Evidence Bundle are the canonical engineering outcome.",
            "Priority: persisted repository state, resulting commits, validation results, then reviewer observations.",
            "The Engineering Outcome and Management Summary above are derived from that priority order.",
            "",
            "## Component Inventory",
            "Automatically derived from changed implementation files in the Repository Evidence; it is not manually authored.",
            *_component_inventory_lines(bundle),
            "",
            "## Deliverable Projection",
            *_deliverable_projection(objective, state, bundle, handoff),
            "",
            *recommendation_handoff_report_lines(handoff, state.phase),
            "## Qualification Projection",
            *_qualification_projection(state, qualification_status, runtime_provider),
            "",
            "## Runtime Projection",
            *_runtime_projection(state, producer, runtime_provider, reported_model),
            "",
            "## Execution Receipt Projection",
            *_execution_receipt_projection(state, producer),
            "",
            "## Decision Evidence Projection",
            *_decision_evidence_projection(producer),
            "",
            "## Deliverable Answer",
            f"- Final Deliverable Answer: {_deliverable_answer(objective, state)}",
            "",
            "## Commit Strategy",
            *_commit_strategy(state, bundle),
            "",
            "## Branch Traceability",
            *_branch_traceability(state, bundle),
            "",
            "## Requirement Traceability",
            "Each row links the prompt requirement to repository-derived implementation, test and validation evidence.",
            *_requirement_traceability(objective, state, bundle),
            "",
            "## Validation Traceability",
            *_validation_traceability(state, bundle),
            "",
            "## Execution Statistics",
            *_execution_statistics(state, bundle),
            "",
            "## Statistics Projection",
            *_statistics_projection(state, bundle),
            "",
            "## Engineering Evidence Summary",
            "```json",
            _evidence_summary(state, bundle, objective),
            "```",
            "",
            evidence_bundle,
            _reconciliation_evidence(objective, state, bundle),
            "## Validation",
            "Repository validation is recorded by the runner and required GitHub Actions; inspect the linked PR evidence for durations."
            if state.phase == "COMPLETE"
            else "No successful engineering validation or delivery is claimed for this terminal transaction.",
            "",
            "## Repair History",
            "No repair iterations were required."
            if not state.repair_iterations
            else f"{state.repair_iterations} bounded repair iteration(s) were recorded.",
            "",
            "## Repository Cleanup",
            state.latest_repository_evidence or "Cleanup evidence unavailable.",
            "",
            "## Specialist Agent Reviews",
            "Specialist review agents are read-only advisory helpers. Their initial observations are listed above; the primary runner retains lifecycle authority.",
            "",
            "## Management Summary",
            "Final repository outcome; it does not restate initial reviewer observations as current state.",
            format_terminal_management_summary(state),
            "",
            "## Diagnostics",
            state.diagnostic or drift_summary(drift_evidence),
            f"Resume: `engineering-execution-host {state.prompt_path} --run-id {state.run_id} --resume`",
            "",
            "## Metrics",
            f"- Codex CLI execution time: {state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'} seconds",
            f"- Repair iterations: {state.repair_iterations}",
            f"- PRs created: {sum(value is not None for value in (state.implementation_pull_request, state.finalization_pull_request))}",
            f"- Merges performed: {sum(value is not None for value in (state.implementation_merge_commit, state.finalization_merge_commit))}",
            "",
        )
    )
    return ReportingCoordinator().deliver(
        path=path,
        body=body,
        validate=lambda value: report_consistency_errors(value, state, bundle, objective),
        terminal_matches=lambda value: terminal_report_matches_state(value, state),
    )
