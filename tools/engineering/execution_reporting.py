"""Terminal-report persistence coordination for the Execution Host."""
from __future__ import annotations

# Report formatting is deliberately pure: lifecycle only supplies the persisted
# transaction and evidence inputs.
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Callable, Mapping

from .agent_state import TransactionState, redact_diagnostic
from .drift_diagnostics import summary as drift_summary
from .execution_evidence import TerminalEvidenceBundle
from .execution_errors import RunnerError
from .execution_lease import history as lease_history, liveness as lease_liveness
from .execution_timing import timing_summary
from .execution_models import PullRequestEvidence, RepositoryEvidence
from .host_preflight import latest as latest_host_preflight
from .workspace_preflight import latest as latest_workspace_preflight
from .capability_preflight import latest as latest_capability_preflight
from .platform_version import EngineeringPlatformManifest
from .producer import ProducerMetadata, parse_producer_metadata
from .providers import GitProvider
from .qualification import latest_qualification
from .recommendation_handoff import ForgeGovernanceHandoff, report_lines as recommendation_handoff_report_lines
from .storage import EngineeringStorageError, load_readiness_evaluation, load_run_qualification_snapshot, load_submission_for_run, load_run_lineage, load_validation_context
from .provider_usage import provider_usage_summary
from .execution_activity import build_terminal_activity_summary, persist_terminal_activity_summary, terminal_activity_summary
from .managed_autonomy import terminal_snapshot as managed_autonomy_snapshot
from .validation_identity import is_canonical_dashboard_command
from .execution_executor import load_validation_failure_diagnostic
from .dashboard_browser_validation import load_dashboard_evidence


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


def _producer_submission_contract_lines(
    submission: dict[str, object] | None, state: TransactionState, root: Path | None = None,
) -> tuple[str, ...]:
    context = submission.get("execution_context") if isinstance(submission, dict) else None
    profile = context.get("validation_profile") if isinstance(context, dict) else None
    validation_context = None
    if root is not None:
        try:
            validation_context = load_validation_context(root, state.run_id)
        except EngineeringStorageError:
            validation_context = None
    profile_source = (
        validation_context.get("profile_selection_source", "not recorded")
        if isinstance(validation_context, dict) and isinstance(profile, dict)
        else "not supplied by Producer"
    )
    return (
        "## Producer Submission Contract",
        f"- Submission ID: `{submission.get('submission_id') if submission else 'legacy'}`",
        f"- Contract Version: `{submission.get('contract_version') if submission else 'legacy prompt'}`",
        "- Submission Status: `PERSISTED_IMMUTABLY`",
        "",
        "## Execution Context Contract",
        f"- Execution Context Status: `{'SUPPLIED_BY_PRODUCER' if isinstance(context, dict) else 'NOT_SUPPLIED_BY_PRODUCER'}`",
        f"- Execution Context Version: `{submission.get('execution_context_version') if isinstance(context, dict) else 'not supplied'}`",
        f"- Execution Context Reference: `execution-submission:{submission.get('submission_id') if isinstance(context, dict) else 'legacy'}`",
        f"- Action Intent: `{context.get('action_intent', 'not supplied') if isinstance(context, dict) else 'not supplied'}`",
        f"- Validation Profile: `{profile.get('tier', 'not supplied') if isinstance(profile, dict) else 'not supplied'}`",
        f"- Validation Profile Source: `{profile_source}`",
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
        _git_output(target, "diff", "--name-status", "-M", baseline, commit)
        if baseline
        else _git_output(target, "diff-tree", "--root", "--no-commit-id", "-r", "--name-status", commit)
        if root_genesis_commit
        else None
    )
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    renamed: list[tuple[str, str]] = []
    if names:
        for row in names.splitlines():
            fields = row.split("\t")
            status_code = fields[0] if fields else ""
            if len(fields) < 2:
                continue
            path = fields[1]
            if status_code.startswith("R") and len(fields) >= 3:
                renamed.append((fields[1], fields[2]))
                continue
            if status_code.startswith("A"):
                added.append(path)
            elif status_code.startswith("D"):
                removed.append(path)
            else:
                modified.append(path)
    changed = tuple(sorted(set(added + modified + removed + [path for pair in renamed for path in pair])))
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
        files_renamed=tuple(renamed),
        diff_check=diff_check,
        transaction_baseline="AVAILABLE" if baseline or root_genesis_commit else "UNAVAILABLE",
        transaction_baseline_sha=baseline,
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
        "- Files Changed By Provider Execution: UNAVAILABLE (provider-stage mutation is not persisted by the terminal checkpoint).",
        f"- Files Changed In Run Delivery Diff: `{len(bundle.changed_files)}`",
        f"- Run Delivery Files: {', '.join(f'`{path}`' for path in bundle.changed_files) or 'NONE'}",
        "- Generated / Projection Files: UNAVAILABLE unless classified by repository evidence.",
        "- Pre-existing Local Changes: NONE when the terminal worktree is clean; otherwise UNAVAILABLE.",
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
    # Command summaries returned by an agent are advisory only.  The canonical
    # control section below projects the persisted invocation/terminal lineage;
    # repeating an independently inferred inclusion state here caused Proof v5's
    # AVAILABLE/UNAVAILABLE contradiction.
    return (
        "- Executed Validation Command: `Documentation validation`",
        "  - Result: report documentation is rendered from the canonical reporting contract",
    ) + tuple(
        line
        for record in state.validation_evidence
        if not is_canonical_dashboard_command(record["command"])
        for line in (
            f"- Executed Validation Command: `{record['command']}`",
            f"  - Result: {record['result']}",
        )
    ) + (
        "- Individual validation inclusion and results are projected only from persisted Validation Control Results.",
        f"- Transaction Baseline Availability: `{bundle.transaction_baseline}` (repository evidence; not a validation control).",
    )


_VALIDATION_CONTROLS = (
    ("full_regression", "Full regression suite", "Regression", "LOCAL", ("unittest", "pytest", "regression")),
    ("ruff", "Ruff", "Lint", "LOCAL", ("ruff",)),
    ("bandit", "Bandit", "Security", "LOCAL", ("bandit",)),
    ("dependency_audit", "Dependency audit", "Security", "LOCAL", ("pip-audit", "dependency audit", "safety")),
    ("codeql", "CodeQL", "Security", "GITHUB_CI", ("codeql",)),
    ("semgrep", "Semgrep", "Security", "GITHUB_CI", ("semgrep",)),
    ("dashboard_browser", "Dashboard/browser tests", "Browser", "LOCAL", ("playwright", "browser", "dashboard.spec")),
    ("git_diff_check", "git diff --check", "Repository", "LOCAL", ("git diff --check",)),
)


def _validation_control_projection(root: Path, state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    """Render every required control without promoting absent evidence to PASS."""
    records = tuple(state.validation_evidence) + ({"command": "git diff --check", "result": bundle.diff_check},)
    try:
        validation_context = load_validation_context(root, state.run_id)
    except EngineeringStorageError:
        validation_context = None
    stored_controls = validation_context.get("controls", {}) if isinstance(validation_context, dict) else {}
    required_controls = validation_context.get("required_validation_controls", ()) if isinstance(validation_context, dict) else ()
    bindings = validation_context.get("control_bindings", ()) if isinstance(validation_context, dict) else ()
    binding_by_id = {
        binding.get("validation_id"): binding
        for binding in bindings
        if isinstance(binding, dict) and isinstance(binding.get("validation_id"), str)
    }
    lines = ["## Validation Control Results", "- Engineering Platform Qualification is reported separately from these individual controls."]
    if isinstance(required_controls, tuple) and required_controls:
        controls = tuple(
            (
                control_id,
                f"Required control {control_id}",
                str(binding_by_id.get(control_id, {}).get("category") or "Unspecified").title(),
                "PERSISTED_PROFILE",
                (),
            )
            for control_id in required_controls
        )
    else:
        controls = _VALIDATION_CONTROLS
    for control_id, name, category, source, markers in controls:
        stored = stored_controls.get(control_id) if isinstance(stored_controls, dict) else None
        # The terminal Evidence Bundle is appended last and therefore wins over
        # historical checkpoint entries for the current projection.
        match = next((
            record for record in reversed(records)
            if (
                is_canonical_dashboard_command(record["command"])
                if control_id == "dashboard_browser"
                else any(marker in record["command"].casefold() for marker in markers)
            )
        ), None)
        if isinstance(stored, dict):
            result = str(stored.get("result") or "UNAVAILABLE")
            reference = str(stored.get("control_identity") or "not recorded")
            execution_status = str(stored.get("execution_status") or "UNAVAILABLE")
            # The persisted control binding is authoritative.  Re-parsing a
            # provider's shell transport here can disagree with the command
            # receipt that produced this control.
            included = "AVAILABLE" if execution_status == "EXECUTED" else "UNAVAILABLE"
        elif match is None:
            result, reference = "NOT_EXECUTED", "not recorded"
            execution_status, included = "NOT_EXECUTED", "UNAVAILABLE"
        else:
            raw = match["result"].casefold()
            result = (
                "NOT_APPLICABLE" if "not applicable" in raw else
                "UNAVAILABLE" if "unavailable" in raw else
                "FAIL" if any(value in raw for value in ("fail", "error", "blocked")) else "PASS"
            )
            reference = match["command"]
            execution_status = "EXECUTED"
            included = "AVAILABLE" if execution_status == "EXECUTED" else "UNAVAILABLE"
        lines.extend((
            f"- {name}: `{result}` — `{source}`",
            f"  - Validation ID: `{control_id}`; Category: `{category}`; Check: `{reference}`.",
            f"  - Execution status: `{execution_status}`.",
            "  - Start/End/Duration: bounded by the canonical validation span when recorded.",
            f"  - Evidence Reference: `{stored.get('evidence_ref', 'UNAVAILABLE')}`." if isinstance(stored, dict) else "  - Evidence Reference: persisted terminal checkpoint and Evidence Bundle.",
            f"  - Execution inclusion: `{included}`.",
        ))
        if isinstance(stored, dict) and stored.get("exit_code") is not None:
            lines.append(f"  - Authoritative Exit Code: `{stored['exit_code']}`.")
        if isinstance(stored, dict) and result != "PASS":
            diagnostic_reference = str(stored.get("diagnostic_evidence_ref") or "UNAVAILABLE")
            diagnostic = load_validation_failure_diagnostic(root, diagnostic_reference)
            lines.append(f"  - Failure Diagnostic Evidence: `{diagnostic_reference}`.")
            if isinstance(diagnostic, dict):
                identities = diagnostic.get("failing_test_identities")
                identity_text = ", ".join(f"`{identity}`" for identity in identities) if isinstance(identities, list) and identities else "`UNAVAILABLE`"
                lines.extend((
                    f"  - Failing Test Identities: {identity_text}.",
                    f"  - Failure Diagnostic Capture: `{diagnostic.get('capture_status', 'UNAVAILABLE')}`; Redaction: `{diagnostic.get('redaction_applied', False)}`; Truncation: `stdout={diagnostic.get('stdout_truncated', False)}, stderr={diagnostic.get('stderr_truncated', False)}`.",
                ))
                summary = redact_diagnostic(
                    str(diagnostic.get("stderr_tail") or diagnostic.get("stdout_tail") or "(empty)"), limit=600
                )
                lines.append(f"  - Bounded Failure Summary: `{summary}`.")
        if control_id == "dashboard_browser" and isinstance(stored, dict):
            shard_evidence = load_dashboard_evidence(root, state.run_id)
            if str(stored.get("evidence_ref", "")).startswith("artifact:") and shard_evidence is not None:
                shard_results = ", ".join(
                    f"{item['shard']}={item['result']}" for item in shard_evidence["shards"]
                )
                lines.extend((
                    f"  - Shard Topology: `{shard_evidence['actual_shard_count']}/{shard_evidence['expected_shard_count']}` shards; `{shard_evidence['workers_per_shard']}` worker per shard.",
                    f"  - Shard Results: `{shard_results}`.",
                    f"  - Cleanup Evidence: `{shard_evidence['cleanup']}` (separate from the canonical terminal result).",
                ))
    lines.extend((
        f"- Transaction Baseline Availability: `{bundle.transaction_baseline}` (repository evidence; not a validation control).",
        "- Qualification and individual-control results are intentionally independent.",
    ))
    return tuple(lines)


def _execution_statistics(
    state: TransactionState, bundle: TerminalEvidenceBundle, timing: Mapping[str, object]
) -> tuple[str, ...]:
    validation_duration = (
        f"`{int(timing['validation_time_ms']) / 1000:.3f}` seconds"
        if timing.get("phase_telemetry_available") and isinstance(timing.get("validation_time_ms"), int)
        else "not measured"
    )
    return (
        "- Execution Count: `1`",
        f"- Engineering Actions: `{len(bundle.changed_files) + len(state.validation_evidence)}` evidence-backed action(s)",
        "- Mission Count (Forge): `0` (Forge is outside this reporting increment)",
        f"- Repair Iterations: `{state.repair_iterations}`",
        f"- Provider Execution Time: `{state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'}` seconds",
        "- Execution Duration (legacy): Provider Execution Time.",
        f"- Validation Time: {validation_duration} ({len(state.validation_evidence)} recorded validation(s))",
    )


def _statistics_projection(state: TransactionState, bundle: TerminalEvidenceBundle) -> tuple[str, ...]:
    """Project separately scoped metrics without inferring mission completion."""
    return (
        "### Mission Statistics",
        "- Mission Count: `0` (Forge mission state is not inferred by Engineering Platform).",
        "### Execution Statistics",
        "- Execution Count: `1`",
        f"- Provider Execution Time: `{state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'}` seconds",
        "- Execution Duration (legacy): Provider Execution Time.",
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
        f"- Platform Qualification Status: `{qualification_status or 'not recorded'}`",
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


def _execution_receipt_projection(root: Path, state: TransactionState, producer: ProducerMetadata) -> tuple[str, ...]:
    """Render receipt qualification fields only from the persisted terminal snapshot."""
    try:
        snapshot = load_run_qualification_snapshot(root, state.run_id) or {}
    except EngineeringStorageError:
        snapshot = {}
    conflicts = snapshot.get("projection_conflicts", [])
    activity = terminal_activity_summary(root, state.run_id)
    activity_total = activity.get("activity", {}).get("overall_activity_total", "UNAVAILABLE") if isinstance(activity, dict) else "UNAVAILABLE"
    delivery_paths = activity.get("terminal_delivery_diff", {}).get("total_unique_changed_paths", "UNAVAILABLE") if isinstance(activity, dict) else "UNAVAILABLE"
    conflicts_text = ", ".join(conflicts) if isinstance(conflicts, list) and conflicts else "NONE"
    return (
        f"- Receipt ID: `{state.run_id}`",
        "- Execution Host: `Engineering Platform`",
        f"- Run ID: `{state.run_id}`",
        f"- Correlation ID: `{producer.correlation_id or 'not recorded'}`",
        f"- Receipt Status: `{state.phase}`",
        f"- Receipt Resolution: `{state.terminal_condition}`",
        f"- Qualification Snapshot: `{snapshot.get('qualification_snapshot_id', 'UNAVAILABLE')}`",
        f"- Required Validation State: `{snapshot.get('required_validation_state', 'UNAVAILABLE')}`",
        f"- Implementation Delivery: `{snapshot.get('implementation_delivery', 'UNAVAILABLE')}`",
        f"- Finalization Delivery: `{snapshot.get('finalization_delivery', 'UNAVAILABLE')}`",
        f"- Cleanup Outcome: `{snapshot.get('cleanup_outcome', 'UNAVAILABLE')}`",
        f"- Repository State: `{snapshot.get('reconciliation_evidence', {}).get('repository_state', 'UNAVAILABLE')}`",
        f"- Workspace State: `{snapshot.get('reconciliation_evidence', {}).get('workspace_state', 'UNAVAILABLE')}`",
        f"- Run Qualification: `{snapshot.get('run_qualification', 'UNAVAILABLE')}`",
        f"- Projection Conflicts: `{conflicts_text}`",
        f"- Execution Activity Summary: `{'v' + str(activity.get('summary_version')) if isinstance(activity, dict) else 'UNAVAILABLE'}`",
        f"- Receipt Activity Total: `{activity_total}`",
        f"- Receipt Terminal Delivery Paths: `{delivery_paths}`",
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
    fresh = re.search(r"^- Fresh Submission: `([^`]+)`$", body, re.MULTILINE)
    retry = re.search(r"^- Retry Parent: `([^`]+)`$", body, re.MULTILINE)
    resume = re.search(r"^- Resume Parent: `([^`]+)`$", body, re.MULTILINE)
    if fresh and fresh.group(1) == "YES" and (
        not retry or retry.group(1) != "NONE" or not resume or resume.group(1) != "NONE"
    ):
        errors.append("fresh submission conflicts with retry or resume parent")
    for role in ("IMPLEMENTATION", "FINALIZATION"):
        pattern = rf"- PR Role: `{role}`(?P<details>.*?)(?=^- PR Role:|^- Autonomous EP Action Count:|\Z)"
        match = re.search(pattern, body, re.MULTILINE | re.DOTALL)
        if match and "- Current PR State: `MERGED`" in match.group("details"):
            checks = re.search(r"- Required Checks State: `([^`]+)`", match.group("details"))
            reference = re.search(r"- Required Checks Evidence Reference: `([^`]+)`", match.group("details"))
            if checks and checks.group(1) == "PASS" and (not reference or reference.group(1) == "UNAVAILABLE"):
                errors.append(f"{role.lower()} required checks pass lacks evidence")
    if bundle.changed_files and re.search(r"^- Files Modified: `0`$", body, re.MULTILINE):
        errors.append("ambiguous zero changed-file projection")
    diff_states = set(re.findall(r"^- git diff --check: `(PASS|FAIL)`", body, re.MULTILINE))
    if len(diff_states) > 1:
        errors.append("current git diff --check state conflicts")
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


def _repair_audit_lines(state: TransactionState) -> tuple[str, ...]:
    if not state.repair_audit:
        return ("No repair iterations were required.",)
    return tuple(line for item in state.repair_audit for line in (
        f"### Repair iteration {item['iteration']}", f"- Observed at: {item['observed_at']}",
        f"- Failed checks: {item['failed_checks']}", f"- Proposed action: {item['proposed_action']}",
        f"- AI repair summary: {item['agent_summary']}", f"- Commit: `{item['commit_sha']}`", f"- Outcome: `{item['outcome']}`",
    ))


def _local_validation_audit_lines(state: TransactionState) -> tuple[str, ...]:
    """Render bounded local-validation repair evidence in terminal reports."""
    if not state.local_validation_audit:
        return ("No local repository validation iterations were required.",)
    return tuple(line for item in state.local_validation_audit for line in (
        f"### Local validation iteration {item['iteration']}",
        f"- Observed at: {item['observed_at']}",
        f"- Failed checks: {item['failed_checks']}",
        f"- Proposed action: {item['proposed_action']}",
        f"- AI repair summary: {item['agent_summary']}",
        f"- Commit: `{item['commit_sha']}`",
        f"- Outcome: `{item['outcome']}`",
    ))


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
            "Authority: owner-authorized bounded lifecycle; ready-for-review and Finalization automated, pull-request merge operator-owned.",
            "No release, deployment or publication performed. Rolling Horizon unchanged.",
        )
    )


def format_terminal_management_summary(state: TransactionState) -> str:
    """Return evidence bounded by the persisted terminal checkpoint phase."""
    if state.phase == "COMPLETE":
        return format_management_summary(state)
    outcome = (
        _blocked_management_outcome(state)
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


def _blocked_management_outcome(state: TransactionState) -> str:
    """State a verified implementation merge without overstating final delivery."""
    if state.implementation_merge_commit:
        return (
            "BLOCKED — implementation merge was verified, but Finalization and "
            "end reconciliation did not complete."
        )
    return "BLOCKED — no engineering changes were executed or delivered."


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
        return _blocked_management_outcome(state) in body and "COMPLETE —" not in body
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
        completed_work = (
            "- Completed work: implementation merge was verified; this is not a complete delivery."
            if state.implementation_merge_commit
            else "- Completed work: no successful engineering delivery is claimed."
        )
        return "\n".join(
            (
                f"- Final checkpoint: `{state.phase}`",
                completed_work,
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


def _managed_autonomy_projection(root: Path, state: TransactionState, bundle: TerminalEvidenceBundle, reviewer_records: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    """Project canonical evidence only; legacy runs intentionally fail closed."""
    try:
        lineage = load_run_lineage(root, state.run_id)
    except EngineeringStorageError:
        lineage = None
    try:
        submission = load_submission_for_run(root, state.run_id)
    except EngineeringStorageError:
        submission = None
    snapshot = managed_autonomy_snapshot(
        root, run_id=state.run_id, execution_outcome=state.phase,
        implementation_pr=state.implementation_pull_request, finalization_pr=state.finalization_pull_request,
        repository_state="MERGED_RECONCILED" if state.phase == "COMPLETE" and (state.finalization_merge_commit or state.action_intent == "VALIDATION_ONLY") else "UNAVAILABLE",
        workspace_state="WORKSPACE_READY" if state.phase == "COMPLETE" and bundle.worktree_state == "clean" else "UNAVAILABLE",
        main_origin_sync="YES" if bundle.target_branch == "main" else "UNAVAILABLE",
        worktree_state=bundle.worktree_state.upper(), active_blocker="NONE" if state.phase == "COMPLETE" else "UNAVAILABLE",
        recovery_required="NO" if state.phase == "COMPLETE" else "UNAVAILABLE",
        retry_parent=lineage.get("retry_parent") if lineage else None,
        # Resume reuses the canonical run ID. No separate resume parent is persisted
        # by the existing lifecycle, so retain an explicit unavailable boundary.
        resume_parent=None,
        submission_id=str(submission["submission_id"]) if submission else None,
        lineage_available=lineage is not None,
        reviewer_records=reviewer_records,
        execution_mode=state.execution_mode,
        action_intent=state.action_intent,
        persist=True,
    )
    validation_profile = snapshot.get("validation_profile")
    validation_profile = validation_profile if isinstance(validation_profile, dict) else {}
    def pr_lines(role: str) -> tuple[str, ...]:
        item = snapshot["pr_checks"].get(role, {})
        not_required = state.action_intent == "VALIDATION_ONLY"
        return (
            f"- PR Role: `{role}`",
            f"  - PR Number: `{item.get('pr_number') or snapshot[f'{role.lower()}_pr'] or ('NOT_REQUIRED' if not_required else 'UNAVAILABLE')}`",
            f"  - Current PR State: `{item.get('pr_state', 'NOT_REQUIRED' if not_required else 'UNAVAILABLE')}`",
            f"  - Merge State: `{item.get('merge_state', 'UNAVAILABLE')}`",
            f"  - Merge Commit: `{item.get('merge_commit') or 'UNAVAILABLE'}`",
            f"  - Required Checks State: `{item.get('required_checks_state', 'UNAVAILABLE')}`",
            f"  - Required Checks Observed At: `{item.get('observed_at', 'UNAVAILABLE')}`",
            f"  - Required Checks Evidence Reference: `{item.get('evidence_ref', 'UNAVAILABLE')}`",
            f"  - Historical Check Observations: `{item.get('historical_observation_count', 'UNAVAILABLE')}`",
        )
    return (
        "## Run Qualification",
        f"- Execution: `{snapshot['terminal_execution_state']}`",
        f"- Action Intent: `{snapshot['action_intent']}`",
        f"- Execution Mode: `{snapshot['execution_mode']}`",
        f"- Run Qualification: `{snapshot['run_qualification']}`",
        "- Platform Qualification is reported separately and cannot upgrade this run.",
        f"- Fresh Submission: `{snapshot['fresh_submission']}`",
        f"- Retry Parent: `{snapshot['retry_parent']}`",
        f"- Resume Parent: `{snapshot['resume_parent']}`",
        f"- Submission Lineage / Submission ID: `{snapshot['submission_id']}`",
        "### Current Terminal Required Checks",
        *pr_lines("IMPLEMENTATION"),
        *pr_lines("FINALIZATION"),
        f"- Autonomous EP Action Count: `{snapshot['autonomous_ep_action_count']}`",
        f"- Expected Operator Gates: `{snapshot['expected_operator_gate_count']}`",
        f"- External Platform Event Count: `{snapshot['external_platform_event_count']}`",
        f"- Unexpected Manual Interventions: `{snapshot['unplanned_manual_intervention_count']}`",
        f"- Unknown Authority Actions: `{snapshot['unknown_authority_count']}`",
        f"- Required Validation State: `{snapshot['required_validation_state']}`",
        f"- Selected Validation Profile: `{validation_profile.get('selected_validation_tier', 'UNAVAILABLE')}`",
        f"- Validation Profile Version: `{validation_profile.get('validation_profile_version', 'UNAVAILABLE')}`",
        f"- Validation Profile Reference: `{validation_profile.get('profile_reference', 'UNAVAILABLE')}`",
        f"- Validation Profile Source: `{validation_profile.get('profile_selection_source', 'UNAVAILABLE')}`",
        f"- Required-control Snapshot: `{snapshot.get('required_control_snapshot_ref', 'UNAVAILABLE')}`",
        f"- Implementation Delivery: `{snapshot.get('implementation_delivery', 'UNAVAILABLE')}`",
        f"- Finalization Delivery: `{snapshot.get('finalization_delivery', 'UNAVAILABLE')}`",
        f"- Cleanup Outcome: `{snapshot.get('cleanup_outcome', 'UNAVAILABLE')}`",
        f"- Repository State: `{snapshot.get('reconciliation_evidence', {}).get('repository_state', 'UNAVAILABLE')}`",
        f"- Workspace State: `{snapshot.get('reconciliation_evidence', {}).get('workspace_state', 'UNAVAILABLE')}`",
        f"- Projection Conflicts: `{', '.join(snapshot.get('projection_conflicts', [])) or 'NONE'}`",
        f"- Qualification Snapshot: `{snapshot.get('qualification_snapshot_id', 'UNAVAILABLE')}`",
        f"- Qualification Reasons: `{', '.join(snapshot['qualification_failure_reasons']) or 'none'}`",
        "",
    )


def generate_terminal_report(
    root: Path,
    state: TransactionState,
    manifest: EngineeringPlatformManifest | None = None,
    detected_cli: str | None = None,
    reviewer_records: tuple[dict[str, object], ...] = (),
    runtime_metadata: Mapping[str, str] | None = None,
    execution_metadata: Mapping[str, int] | None = None,
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
    # The submitted prompt is immutable input, not execution evidence.  Keep
    # using it internally for traceability, but never splice its arbitrary
    # multi-line content into the report header where requested outcomes can
    # visually resemble the terminal result.
    objective_header = (
        "Submitted runtime prompt retained at the supplied prompt path; "
        "non-authoritative input."
    )
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
    reported_cli_installation_path = (
        runtime_metadata.get("codex_cli_installation_path", "not reported")
        if runtime_provider == "codex_cli" else "not applicable"
    )
    raw_execution_metadata = execution_metadata or {}
    safe_execution_metadata = {
        key: max(0, value)
        for key in ("modified", "created", "deleted", "codex_commands_executed")
        for value in (raw_execution_metadata.get(key, 0),)
        if isinstance(value, int) and not isinstance(value, bool)
    }
    bundle = collect_terminal_evidence(root, state)
    activity_summary = persist_terminal_activity_summary(
        root, build_terminal_activity_summary(root, state, bundle)
    )
    timing = timing_summary(root, state.run_id)
    provider_usage = provider_usage_summary(root, state.run_id)
    churn = provider_usage.get("context_churn") if isinstance(provider_usage.get("context_churn"), dict) else {}
    provider_usage_lines = (
        "## Provider Usage",
        f"- Provider Invocations: `{provider_usage.get('provider_invocation_count', 0)}`",
        f"- Run Cumulative Input Tokens: `{provider_usage.get('input_tokens', 'UNAVAILABLE')}`",
        f"- Cached Input Tokens: `{provider_usage.get('cached_input_tokens', 'UNAVAILABLE')}`",
        f"- Uncached Input Tokens: `{provider_usage.get('uncached_input_tokens', 'UNAVAILABLE')}`",
        f"- Output Tokens: `{provider_usage.get('output_tokens', 'UNAVAILABLE')}`",
        f"- Maximum Provider Invocation Cumulative Input: `{provider_usage.get('max_input_tokens_per_invocation', 'UNAVAILABLE')}`",
        f"- Observed Final Usage Snapshots: `{provider_usage.get('usage_snapshot_count') or 'UNAVAILABLE'}`",
        f"- Intermediate Usage Delta Available: `{'yes' if provider_usage.get('intermediate_usage_delta_available') else 'no'}`",
        f"- Maximum Intermediate Input Delta: `{provider_usage.get('maximum_incremental_input_tokens') or 'UNAVAILABLE'}`",
        "- Actual Single-Request Context Size: `UNAVAILABLE` (not emitted by Codex CLI JSONL).",
        "- Active Context Size: `UNAVAILABLE` (not emitted by Codex CLI JSONL).",
        f"- Estimated Credits: `{provider_usage.get('estimated_credits', 'UNAVAILABLE')}`",
        f"- Estimated EUR: `{provider_usage.get('estimated_eur', 'UNAVAILABLE')}` (derived estimate; not account billing)",
        f"- Rate Table Version: `{provider_usage.get('rate_table_version', 'UNAVAILABLE')}`",
        f"- Usage Authority: `{provider_usage.get('usage_authority', 'UNAVAILABLE')}`",
        f"- Speed State: `{provider_usage.get('speed_state', 'UNKNOWN')}`",
        "",
        "## Observable Provider Input Correlation",
        f"- File Reads: `{churn.get('file_read_count', 'UNAVAILABLE')}`",
        f"- Repeated File Reads: `{churn.get('repeated_file_read_count', 'UNAVAILABLE')}`",
        f"- Tool Output Bytes: `{churn.get('tool_output_bytes', 'UNAVAILABLE')}`",
        f"- Test Output Bytes: `{(churn.get('passing_test_output_bytes', 0) + churn.get('failed_test_diagnostic_bytes', 0)) if churn else 'UNAVAILABLE'}`",
        "- Dominant Churn Indicators: derived only from bounded invocation counters; raw prompts and outputs are not retained.",
        "",
        "## Provider Context Scope",
        f"- Policy: `{churn.get('context_scope_policy', 'UNAVAILABLE')}`",
        f"- Initial Scope: `{churn.get('context_scope_initial', 'UNAVAILABLE')}`",
        f"- Effective Scope: `{churn.get('context_scope_effective', 'UNAVAILABLE')}`",
        f"- Context Escalations: `{churn.get('context_escalation_count', 'UNAVAILABLE')}`",
        f"- Escalation Reasons: `{churn.get('context_escalation_reasons', 'NONE')}`",
        f"- Historical PRs Inspected: `{provider_usage.get('historical_pr_results') if provider_usage.get('historical_context_metrics_authority') != 'UNAVAILABLE' else 'UNAVAILABLE'}`",
        f"- Historical Commits Inspected: `{provider_usage.get('historical_commit_results') if provider_usage.get('historical_context_metrics_authority') != 'UNAVAILABLE' else 'UNAVAILABLE'}`",
        f"- Historical Context Bytes: `{provider_usage.get('historical_context_bytes') if provider_usage.get('historical_context_metrics_authority') != 'UNAVAILABLE' else 'UNAVAILABLE'}`",
        "",
    )
    timing_lines = ["## Execution Phase Timing"]
    if timing.get("phase_telemetry_available"):
        occurred = set(timing.get("occurred_phases", ()))
        timing_lines.extend((
            f"- Total Wall Time: `{timing['total_wall_time_ms'] / 1000:.3f}` s",
            f"- Active EP Processing Time: `{timing['active_ep_processing_time_ms'] / 1000:.3f}` s",
        ))
        for phase, label, value, share in (
            ("PROVIDER_EXECUTION", "Provider Execution Time", "provider_execution_time_ms", "provider_share_percent"),
            ("VALIDATION", "Validation Time", "validation_time_ms", "validation_share_percent"),
            ("EXTERNAL_CI_WAIT", "External Wait Time", "external_wait_time_ms", "external_wait_share_percent"),
            ("QUEUE_WAIT", "Queue Wait Time", "queue_wait_time_ms", "queue_share_percent"),
            ("REPORT_GENERATION", "Report Generation Time", "report_generation_time_ms", None),
            ("EVIDENCE_PERSISTENCE", "Evidence Persistence Time", "evidence_persistence_time_ms", None),
            ("REPOSITORY_FINALIZATION", "Repository Finalization Time", "repository_finalization_time_ms", None),
        ):
            if phase in occurred:
                suffix = f" ({timing[share]:.3f}%)" if share else ""
                timing_lines.append(f"- {label}: `{timing[value] / 1000:.3f}` s{suffix}")
        timing_lines.extend((
            f"- Overhead Time: `{timing['overhead_time_ms'] / 1000:.3f}` s ({timing['overhead_share_percent']:.3f}%)",
            "### Top Phase Categories",
            *(f"- {index}. {item['phase']} — `{item['duration_ms'] / 1000:.3f}` s" for index, item in enumerate(timing["top_phase_categories"], 1)),
            "### Longest Individual Spans",
            *(f"- {index}. {item['label']} — `{item['duration_ms'] / 1000:.3f}` s" for index, item in enumerate(timing["longest_individual_spans"], 1)),
            "- Aggregation: category totals suppress only a same-category ancestor; ties sort by canonical phase name. Individual spans are independently retained and ranked by duration, phase name and ordinal.",
            "- Overhead: Total Wall Time excludes Queue Wait; it subtracts only External Wait and outer provider/validation processing coverage, so nested spans are not double-counted.",
        ))
    else:
        timing_lines.append("- Phase-level telemetry: unavailable for this historical run.")
        if timing.get("historical_total_available"):
            timing_lines.append(
                f"- Historical Total Wall Time: `{timing['total_wall_time_ms'] / 1000:.3f}` s (phase telemetry incomplete)."
            )
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
    terminal_reconciled = state.phase == "COMPLETE" and liveness.get("lease_state") == "RELEASED"
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
            f"- Terminal Execution State: `{state.phase}`",
            f"- Execution lifecycle: `{state.phase}`",
            f"- Execution liveness: `{liveness.get('state', 'UNAVAILABLE')}` (historical compatibility)",
            f"- Current / Final Lease State: `{liveness.get('lease_state', 'UNAVAILABLE')}`",
            f"- Historical Liveness Event: `{'STALE detected and reconciled' if terminal_reconciled and liveness.get('state') == 'STALE' else liveness.get('state', 'UNAVAILABLE')}`",
            f"- Recovery Required: `{'NO' if terminal_reconciled else 'YES' if liveness.get('reconciliation_outcome') == 'RECOVERABLE' else 'NO'}`",
            f"- Recovery action: {recovery}",
            f"- Objective: {objective_header}",
            f"- Submitted Prompt Characters: `{len(objective)}`",
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
            *_producer_submission_contract_lines(submission, state, root),
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
            f"- Codex CLI Installation Path: `{reported_cli_installation_path}`",
            "",
            "## Execution Metadata",
            f"- Provider-Stage Files Modified: `{safe_execution_metadata.get('modified', 0)}`",
            f"- Provider-Stage Files Created: `{safe_execution_metadata.get('created', 0)}`",
            f"- Provider-Stage Files Deleted: `{safe_execution_metadata.get('deleted', 0)}`",
            f"- Files Changed In Run Delivery Diff: `{len(bundle.changed_files)}`",
            f"- Codex Commands Executed: `{safe_execution_metadata.get('codex_commands_executed', 0)}`",
            "",
            "## Execution Activity Summary",
            f"- Summary Version: `{activity_summary['summary_version']}`",
            "- Scope: terminal persisted activity and repository delivery evidence; the live worktree snapshot is volatile and is not a terminal result.",
            f"- Codex Command Definition: {activity_summary['activity']['codex_command_definition']}",
            f"- Primary Codex Commands: `{activity_summary['activity']['primary_codex_commands_total']}`",
            f"- Reviewer Codex Commands: `{activity_summary['activity']['reviewer_codex_commands_total']}`",
            f"- Host Validation Commands: `{activity_summary['activity']['host_validation_commands_total']}`",
            f"- Overall Activity Total: `{activity_summary['activity']['overall_activity_total']}`",
            f"- Terminal Delivery Baseline SHA: `{activity_summary['terminal_delivery_diff']['transaction_baseline_sha']}`",
            f"- Terminal Delivery Target SHA: `{activity_summary['terminal_delivery_diff']['terminal_target_sha']}`",
            f"- Terminal Delivery Unique Changed Paths: `{activity_summary['terminal_delivery_diff']['total_unique_changed_paths']}`",
            f"- Terminal Delivery Added / Modified / Removed / Renamed: `{len(activity_summary['terminal_delivery_diff']['added'])}` / `{len(activity_summary['terminal_delivery_diff']['modified'])}` / `{len(activity_summary['terminal_delivery_diff']['removed'])}` / `{len(activity_summary['terminal_delivery_diff']['renamed'])}`",
            "- Per-PR Changed Files: GitHub evidence scoped to each pull request; never summed into the terminal run delivery diff.",
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
            f"`INITIALIZE → CAPABILITY_REVIEW → IMPLEMENTATION → VALIDATION → REPAIR ({state.repair_iterations}) → MERGE → FINALIZATION → REPOSITORY_CLEANUP → {state.phase}`",
            "",
            *timing_lines,
            "",
            *provider_usage_lines,
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
            *_managed_autonomy_projection(root, state, bundle, reviewer_records),
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
            *_execution_receipt_projection(root, state, producer),
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
            *_execution_statistics(state, bundle, timing),
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
            *_validation_control_projection(root, state, bundle),
            "",
            _reconciliation_evidence(objective, state, bundle),
            "## Validation",
            "Repository validation is recorded by the runner and required GitHub Actions; inspect the linked PR evidence for durations."
            if state.phase == "COMPLETE"
            else "No successful engineering validation or delivery is claimed for this terminal transaction.",
            "",
            "## Repair History",
            *_repair_audit_lines(state),
            "",
            "## Local Repository Validation History",
            *_local_validation_audit_lines(state),
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
