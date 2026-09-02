"""Thin foreground orchestrator for one bounded DJConnect engineering prompt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import subprocess  # noqa: F401 - compatibility export for host tests
import time
from threading import Lock
from typing import Protocol
import uuid
import re
import sqlite3

from .validation_identity import is_canonical_dashboard_command

from .agent_state import MAX_COMMIT_EVIDENCE_RECORDS, StateError, StateStore, TransactionState, redact_diagnostic, verified_commit_evidence_record
from .capability_review import (
    ReviewerResult,
    ReviewerSelection,
    records_for_storage,
    run_reviews,
    select_reviewers,
)
from .codex_observability import codex_final_message as _codex_final_message  # noqa: F401
from .codex_observability import extract_codex_runtime_metadata  # noqa: F401
from .codex_observability import extract_codex_usage  # noqa: F401
from .codex_observability import write_codex_usage
from .engineering_memory import (
    capture_engineering_memory,
    load_engineering_memory,
    retrieve_engineering_memory,
)
from .live_status import print_live_status, write_live_status, write_runner_process
from .platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    validate_compatibility,
)
from .qualification import dashboard, execute_qualification
from .report_analysis import analyze as analyze_terminal_report
from .prompt_history import record_terminal_report
from .status_model import build as build_canonical_status, publish as publish_canonical_status
from .platform_api import PlatformConfiguration, PlatformConfigurationError
from .resources import package_path
from .platform_bootstrap import runtime_workspace
from .providers import DeterministicValidationExecutor, GitProvider, CodexCliProvider
from .host_preflight import latest as latest_host_preflight
from .workspace_preflight import latest as latest_workspace_preflight
from .capability_preflight import latest as latest_capability_preflight
from .execution_lease import Lease, LeaseConflictError, LeaseHeartbeat, acquire as acquire_lease, heartbeat as heartbeat_lease, host_identity, host_instance_id, reconcile_stale, release as release_lease
from .execution_readiness import ReadinessFacts, decide as decide_readiness, evaluate as evaluate_readiness, selected_profile
from .execution_transaction import ExecutionTransaction
from .execution_context import (
    additional_workspace_write_roots as context_workspace_write_roots,
    execution_mode_for as context_execution_mode_for,
    genesis_target_for as context_genesis_target_for,
    genesis_workspace_preflight as context_genesis_workspace_preflight,
    resolve_execution_context as context_resolve_execution_context,
    target_repository_authorization as context_target_repository_authorization,
)
from .execution_models import AgentResult, PullRequestEvidence, RepositoryEvidence
from .validation_profile import (
    VALIDATION_PROFILE_VERSION, ValidationControlLauncher, ValidationProfile,
    ValidationProfileResolutionError, changed_paths, classify,
    profile_control_bindings, resolve_producer_profile,
)
from .reviewer_evidence import ReviewerEvidence
from .investigation_ledger import InvocationInvestigationLedger
from .execution_errors import CodexHandoffTimeout, CodexInvocationError, RunnerError
from .execution_errors import ProviderReadinessBlocked
from .execution_timeout_policy import FINALIZATION, REPAIR, agent_timeout
from .provider_readiness import failures as provider_readiness_failures
from .execution_repository import GitHubClient as ProviderGitHubClient, RepositoryClient as ProviderRepositoryClient
from .execution_repository import GhCliClient as ProviderGhCliClient, SubprocessRepositoryClient as ProviderRepositoryClientImpl
from .execution_executor import format_cli_failure as executor_format_cli_failure
from .execution_executor import (
    project_codex_activity as executor_project_codex_activity,
    project_codex_live_action_name as executor_project_codex_live_action_name,
)
from .execution_executor import redacted_cli_tail as executor_redacted_cli_tail
from .execution_executor import write_redacted_codex_cli_log as executor_write_redacted_codex_cli_log
from .execution_executor import persist_validation_failure_diagnostic
from .execution_executor import CodexCliClient
from .execution_finalization import FinalizationCoordinator
from .storage import EngineeringStorageError, load_admission_decision, load_submission_for_run, load_validation_context, record_artifact, record_readiness_evaluation, record_validation_command_invocation, record_validation_command_terminal, record_validation_control_result, record_validation_profile
from .dashboard_browser_validation import dashboard_evidence_path, load_dashboard_evidence
from .storage import dismissal_for_run
from .provider_usage import AUTHORITATIVE, ProviderInvocation, normalize_codex_model, persist_provider_invocation
from .provider_recovery import (
    claim_replacement_launch, create_recovery_available, load_recovery_state, record_provider_started,
    load_recovery_agent_result, mark_precheck_failed, persist_recovery_agent_result,
    record_pre_execution_launch_failure, record_replacement_terminal, reconcile_recovery,
    transition_recovery_state, consume_controlled_interruption_hook,
)
from .worktree_provenance import capture as capture_worktree_provenance, verify_recovery as verify_worktree_recovery
from .provider_context import ProviderRole, project_context, provider_need_for_phase, role_for_phase
from .provider_context_scope import ContextScope, POLICY_ID, initial_context_scope, provider_instruction
from .execution_timing import ActivePhase
from .execution_timing import complete_active_phase as _complete_active_phase
from .execution_timing import complete_phase as _complete_phase
from .execution_timing import start_or_resume_phase as _start_or_resume_phase
from .execution_timing import start_phase as _start_phase
from .managed_autonomy import (
    append_action as record_managed_action,
    append_pr_check_observation as record_managed_pr_check,
    append_validation_observation as record_managed_validation,
    record_gate as record_managed_gate,
)
from .component_logging import component_logger, shutdown_signal_logging


LOGGER = logging.getLogger(__name__)
# Compatibility exports for integrations that already import these names.
FINALIZATION_PR_HANDOFF_MAX_SECONDS = FINALIZATION.seconds
REPAIR_AGENT_MAX_SECONDS = REPAIR.seconds

# A repair remains scoped to its original PR, but it must also have a finite
# attempt budget. This prevents a persistently failing required check from
# repeatedly invoking the provider without an operator decision.
MAX_PR_CHECK_REPAIR_ATTEMPTS = 3
MAX_LOCAL_REPOSITORY_VALIDATION_ATTEMPTS = 3


def _timing_unavailable(error: EngineeringStorageError) -> None:
    """Keep optional phase telemetry from changing the run outcome."""
    LOGGER.warning("Execution phase telemetry is unavailable: %s", error)


def start_phase(root: Path, run_id: str, phase_name: str, **kwargs: object) -> ActivePhase | None:
    try:
        return _start_phase(root, run_id, phase_name, **kwargs)
    except EngineeringStorageError as error:
        _timing_unavailable(error)
        return None


def start_or_resume_phase(root: Path, run_id: str, phase_name: str, **kwargs: object) -> ActivePhase | None:
    try:
        return _start_or_resume_phase(root, run_id, phase_name, **kwargs)
    except EngineeringStorageError as error:
        _timing_unavailable(error)
        return None


def complete_phase(root: Path, active: ActivePhase | None, **kwargs: object) -> None:
    if active is None:
        return
    try:
        _complete_phase(root, active, **kwargs)
    except EngineeringStorageError as error:
        _timing_unavailable(error)


def complete_active_phase(root: Path, run_id: str, phase_name: str, **kwargs: object) -> bool:
    try:
        return _complete_active_phase(root, run_id, phase_name, **kwargs)
    except EngineeringStorageError as error:
        _timing_unavailable(error)
        return False

# Compatibility exports remain at this façade while implementation resides in
# the dedicated context, repository and executor modules.
RepositoryClient = ProviderRepositoryClient
GitHubClient = ProviderGitHubClient
SubprocessRepositoryClient = ProviderRepositoryClientImpl
GhCliClient = ProviderGhCliClient
additional_workspace_write_roots = context_workspace_write_roots
target_repository_authorization = context_target_repository_authorization
resolve_execution_context = context_resolve_execution_context
execution_mode_for = context_execution_mode_for
genesis_target_for = context_genesis_target_for
genesis_workspace_preflight = context_genesis_workspace_preflight



class AgentClient(Protocol):
    def available(self) -> bool: ...

    def version(self) -> str: ...

    def invoke(self, root: Path, prompt: str) -> AgentResult: ...


project_codex_activity = executor_project_codex_activity
project_codex_live_action_name = executor_project_codex_live_action_name
_redacted_cli_tail = executor_redacted_cli_tail
_format_cli_failure = executor_format_cli_failure
write_redacted_codex_cli_log = executor_write_redacted_codex_cli_log


def assemble_prompt(
    prompt_path: Path,
    state: TransactionState | None,
    *,
    managed_target: Path | None = None,
    reviewer_evidence: ReviewerEvidence | None = None,
    role: ProviderRole | None = None,
) -> str:
    objective = prompt_path.read_text(encoding="utf-8")
    provider_role = role or role_for_phase(state.phase if state else "EXECUTE_AGENT")
    projection = project_context(provider_role, objective)
    scope = initial_context_scope(
        phase=state.phase if state else "EXECUTE_AGENT",
        repair_iterations=state.repair_iterations if state else 0,
        objective=objective,
    )
    resume = (
        "No prior transaction checkpoint exists."
        if state is None
        else json.dumps(state.to_dict(), sort_keys=True)
    )
    authority = (
        """This is the sole automatic post-Finalization reconciliation. You may only update
the four canonical rolling records, commit them directly to the already synchronized `main`,
and push that one commit. Do not create a branch or pull request. Do not change runtime code,
authority, lifecycle, retry, validation, provider, Forge, queue, or delivery semantics. Return
`COMPLETE`, `repository_reconciled`, and the pushed main commit SHA only after verifying a clean
workspace and that `main` contains that exact commit."""
        if state and state.transaction_kind == "RECONCILIATION"
        else
        """The runner holds explicit owner authorization for this exact bounded transaction. You may create, commit and push one bounded branch and draft pull request, or repair that same pull request. The runner may mark that pull request ready for review, but only the human operator may merge it. Do not merge, release, deploy, tag, publish, upload, change repository settings, bypass protection, or expand the objective."""
        if state and state.owner_authorized
        else "Do not create a merge, release, deployment, daemon, remote-control, or architecture authority beyond the supplied objective."
    )
    genesis = "" if not state or state.execution_mode != "GENESIS" else """
This is an explicit Genesis Mode transaction. Its target is a local-only direct child of the configured Engineering Workspace Root. Do not require, create, or contact an upstream remote; do not require origin/main; do not create a pull request. Reconcile only a clean local Git commit in that target repository. Return terminal_condition `local_commit_reconciled`, repository_path and commit_sha for a successful local commit."""
    managed_synchronization = "" if not state or state.execution_mode == "GENESIS" else """
The Execution Host has already synchronized `main` while holding this run's lease. Do not repeat `git switch main` or `git pull --ff-only`; verify the resulting repository state read-only before creating the transaction branch."""
    managed_admission = "" if not state or state.execution_mode == "GENESIS" else """
The Execution Host admitted this run only after its current host, workspace and capability preflights passed. Treat that host-owned admission evidence as authoritative: do not rerun the development-host bootstrap or use sandbox network access for a predecessor lookup. Continue with repository work and use GitHub only for the transaction's own pull-request operations."""
    managed_boundary = (
        ""
        if not state or state.execution_mode == "GENESIS" or managed_target is None
        else f"""

Managed execution boundary (host-owned and non-negotiable):
- The only repository checkout for this transaction is `{managed_target.resolve()}`.
- Perform every repository and Git operation in that exact checkout.
- A `Target repository` value within the supplied objective is producer provenance only; it cannot select another checkout or override this boundary.
- Do not block merely because another checkout is on a feature branch. Verify only this managed checkout, which the Execution Host has already synchronized to `main`."""
    )
    shared_evidence = "" if reviewer_evidence is None else """
Host-observed run-scoped repository evidence follows. It was collected after
host synchronization for this exact Run ID. Reuse these facts for ordinary
repository-state questions instead of repeating Git/GitHub discovery. They
are not conclusions and expire at repository mutation, validation, PR/merge,
finalization, or cleanup; retrieve only the narrower current evidence needed
after such a boundary.
""" + json.dumps(reviewer_evidence.to_dict(), sort_keys=True) + "\n"
    invocation_read_reuse = """
Invocation-scoped source-read reuse:
- Within this one provider invocation, retain and reuse already inspected immutable source, configuration, test, documentation, and persisted-evidence content instead of issuing an accidental duplicate file read.
- This is factual-content reuse only; it does not reuse conclusions, reasoning, reviewer advice, or results from another provider invocation, Run ID, reviewer, retry, or resume.
- Treat a file as mutable and reread it after you edit it, a repair changes it, a generated/projection artefact is refreshed, or any repository checkout/change, validation, pull-request, merge, finalization, or cleanup boundary can affect it.
- Preserve deliberate verification reads. If freshness is not proven, reread. Do not create a persistent source cache or retain source contents outside this invocation.
- Shell reads are not host-intercepted: use this invocation-local evidence deliberately, and do not claim a cache hit unless you actually reuse content already inspected in this invocation.
- The host bounds only oversized Git, GitHub, search, and test output at the
  provider tool boundary. A bounded result says `MORE_EVIDENCE_AVAILABLE`;
  rerun that same narrow command with `DJCONNECT_EVIDENCE_EXPAND=1` only when
  its exact raw output is required. Source reads remain exact by default.
- A successful test result may be compact, but a failed test keeps its failing
  identity, assertion and diagnostic context. Never treat a bounded result as
  proof when it is ambiguous: expand it or fail closed.
"""
    context_scope_instruction = "\n" + provider_instruction(scope) + "\n"
    investigation_ledger = InvocationInvestigationLedger().record(
        "repository_identity", "repository_status", "git_ancestry"
    ) if reviewer_evidence is not None else InvocationInvestigationLedger()
    primary_tool_loop = """
Primary Invocation Investigation Ledger (ephemeral and primary-only):
Use this identifier-only ledger to avoid rediscovering a fact already established
in this invocation. Record a fact only after its narrow real check; never record
source text, paths, commands, tool output, prompts, conclusions, or reviewer
reasoning. Before a tool call, it must establish one missing fact, refresh an
invalidated fact, perform a mutation, or execute required validation.

For unchanged state, reuse an established source inspection, test surface,
repository status, or ancestry fact. Prefer exact branch/HEAD/status, named
diff/stat, and targeted ancestry queries over broad logs or full diffs. Do not
rerun a passing validation unless relevant code/test inputs changed or a
canonical boundary requires it. At every listed boundary, invalidate all
non-RUN-STABLE facts and obtain narrow fresh evidence; uncertainty is itself a
freshness boundary. Reviewer advice and primary conclusions are never ledger
facts and must never cross the primary/reviewer boundary.

Ledger bootstrap:
""" + json.dumps(investigation_ledger.to_prompt_dict(), sort_keys=True) + "\n"
    pr_handoff = "" if not state or state.execution_mode == "GENESIS" else """
PR hand-off boundary (host-owned and non-negotiable):
- Your work ends when the bounded branch and its pull request have been
  created or repaired, pushed, and locally validated.
- If this is a repair, preserve the exact checkpointed pull-request number
  and branch. Never create a replacement pull request.
- Return the required JSON object immediately after that hand-off. Do not
  poll or wait for GitHub checks, review, merge, Finalization, reconciliation,
  or any other external terminal evidence. The Execution Host alone records
  the pull request, polls checks, and schedules at most three bounded repairs.
- Write pull-request Markdown with real line breaks. Never serialize a line
  break as the literal characters `\\n`.
"""
    local_gate = "" if not state or not (
        state.execution_mode == "MANAGED" and state.transaction_kind == "IMPLEMENTATION" and state.phase == "EXECUTE_AGENT"
    ) else """
Local validation hand-off boundary:
- Create, commit and push the bounded implementation branch, but do not create a pull request yet.
- Return that branch with `pull_request: null` after relevant focused validation. The host owns the next local repository validation gate and only that gate may create the implementation PR after the canonical suite passes.
"""
    return f"""You are executing one bounded DJConnect engineering transaction.
Provider role: {provider_role.value}. Context projection: {projection.budget_version}; source items: {projection.source_item_count}; omitted lower-priority items: {projection.omitted_low_priority_count}.{context_scope_instruction}
Read BOOTSTRAP.md, ENGINEERING_METHOD.md, PROMPT_INITIALIZATION.md and AGENTS.md from the actual repository before acting. Repository and GitHub evidence override this checkpoint: {resume}
{authority}{genesis}{managed_synchronization}{managed_admission}{shared_evidence}{invocation_read_reuse}{primary_tool_loop}{local_gate}{pr_handoff}
Supplied bounded objective follows:\n\n{projection.text}\n{managed_boundary}\n\nReturn only one JSON object with terminal_state (COMPLETE, WAITING, BLOCKED, or FAILED), branch, pull_request, terminal_condition (repository_reconciled, open_pr_checks_terminal, external_blocked, or local_commit_reconciled), diagnostic, repository_path, commit_sha, validation_evidence, quality_evidence and validation_disposition. validation_evidence is a bounded list of executed validation {{command, result}} summaries; use [] when none ran. quality_evidence is [] except for the autonomous quality-control stage, where it contains only bounded, executed {{activity, result}} records. validation_disposition is product_failure unless the required suite failed for an environmental instability that you demonstrated with bounded evidence, such as an isolated rerun of the same failing check passing without a code change. It never makes a failed suite pass. Never include secrets, tokens, headers, environment values, prompts, repository file contents, stack traces, or raw command output. Use null for other fields that do not apply. The diagnostic must be a short human-readable reason without secrets, tokens, headers, environment values, prompt content, repository file content, stack traces, or raw command output."""


class EngineeringRunner:
    def __init__(
        self,
        root: Path,
        store: StateStore,
        repository: RepositoryClient,
        github: GitHubClient,
        agent: AgentClient,
        sleep=time.sleep,
        compatibility: RunnerCompatibility = RunnerCompatibility(),
    ) -> None:
        self.root, self.store, self.repository, self.github, self.agent, self.sleep = (
            root,
            store,
            repository,
            github,
            agent,
            sleep,
        )
        self.compatibility = compatibility
        self.platform_manifest: EngineeringPlatformManifest | None = None
        self.detected_codex_cli: str | None = None
        self.reviewer_records: tuple[dict[str, object], ...] = ()
        self.reviewer_runtime: list[dict[str, object]] = []
        self._reviewer_runtime_lock = Lock()
        self.console_detail: str | None = None
        self.host_identity = host_identity()
        self.host_instance_id = host_instance_id()
        self.active_lease: Lease | None = None
        self.transaction: ExecutionTransaction | None = None
        self.lease_heartbeat: LeaseHeartbeat | None = None
        self.finalization = FinalizationCoordinator()
        self.validation_executor = DeterministicValidationExecutor()
        self._total_phase: ActivePhase | None = None
        self._provider_context_telemetry: dict[str, int] = {}
        self._provider_dispatch_telemetry: dict[str, int] = {}
        self._dispatch_guard_enforced = False
        self._last_provider_invocation_id: str | None = None

    def _provider_recovery_preflight(self, state: TransactionState) -> str | None:
        """Return a fail-closed reason before the sole same-run restart.

        This deliberately relies on the host's already-held run lease and on
        the process receipt that the client removes in its ``finally`` block;
        a delay is never evidence that an old provider has stopped.
        """
        if state.terminal or dismissal_for_run(self.root, state.run_id):
            return "CANCELLED"
        if len(state.provider_recovery_attempts) >= 1 and state.provider_recovery_attempts[0].get("result") != "ACTIVE":
            return "PRECHECK_FAILED"
        if self.active_lease is None or self.active_lease.run_id != state.run_id:
            return "PRECHECK_FAILED"
        try:
            evidence = self.repository.inspect(self.root)
        except Exception:
            return "PRECHECK_FAILED"
        if state.branch and evidence.branch != state.branch:
            return "PRECHECK_FAILED"
        provenance = verify_worktree_recovery(
            self.root, run_id=state.run_id, branch=state.branch,
            transaction_baseline_sha=state.last_verified_sha,
        )
        if provenance is False:
            return "PRECHECK_FAILED"
        process_path = self.root / ".engineering" / "status" / "runner_process.json"
        try:
            process = json.loads(process_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            process = None
        if isinstance(process, dict) and process.get("run_id") == state.run_id:
            pid = process.get("pid")
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                except OSError:
                    process_path.unlink(missing_ok=True)
                else:
                    return "PRECHECK_FAILED"
        return None

    def _recovery_record(
        self, state: TransactionState, *, original: str, replacement: str,
        eligibility: str, result: str, requested_at: str, started_at: str,
        completed_at: str,
    ) -> TransactionState:
        return replace(state, provider_recovery_attempts=({
            "original_invocation_id": original, "replacement_invocation_id": replacement,
            "phase": state.phase, "classification": "provider_turn_interrupted",
            "eligibility": eligibility, "result": result,
            "requested_at": requested_at, "started_at": started_at, "completed_at": completed_at,
        },))

    def _controlled_interruption_requested(self, state: TransactionState) -> bool:
        """Read the opt-in, run-bound qualification hook; prompt text cannot set it.

        The value is deliberately exact and process-local.  It is useful only
        to qualification fixtures and remains inert for normal executions.
        Durable consumption is represented by the recovery ledger.
        """
        return consume_controlled_interruption_hook(
            self.root, run_id=state.run_id, phase=state.phase,
        )

    def _recovery_state(self, run_id: str) -> dict[str, object] | None:
        """Read recovery truth from this runner's explicit lifecycle authority."""
        return load_recovery_state(
            self.root, run_id, central_database=self.store.central_database,
        )

    def _provider_process_boundary(self, state: TransactionState, process: object) -> None:
        """Record the real provider start boundary for a claimed recovery."""
        write_runner_process(self.root, state.run_id, process)
        if not isinstance(process, dict):
            return
        recovery = self._recovery_state(state.run_id)
        receipt_id = recovery.get("process_receipt_id") if isinstance(recovery, dict) else None
        if recovery and recovery.get("state") == "RECOVERY_STARTING" and isinstance(receipt_id, str):
            record_provider_started(
                self.root, run_id=state.run_id, receipt_id=receipt_id,
                pid=int(process["pid"]), process_group=int(process["process_group"]),
                central_database=self.store.central_database,
            )

    def _project_durable_recovery(self, state: TransactionState, recovery: dict[str, object]) -> None:
        """Keep the legacy checkpoint field read-only-compatible with SQLite."""
        result = str(recovery.get("state") or "")
        projected_result = {
            "RECOVERED": "RECOVERED",
            "EXHAUSTED": "INTERRUPTED_AGAIN",
            "PRECHECK_FAILED": "PRECHECK_FAILED",
            "AMBIGUOUS": "PROVIDER_FAILED",
        }.get(result)
        if projected_result is None:
            return
        self.store.save(self._recovery_record(
            state,
            original=str(recovery.get("triggering_invocation_id") or "unavailable"),
            replacement=str(recovery.get("replacement_invocation_id") or "unavailable"),
            eligibility="ELIGIBLE" if result in {"RECOVERED", "EXHAUSTED"} else result,
            result=projected_result,
            requested_at=str(recovery.get("requested_at") or "unknown"),
            started_at=str(recovery.get("provider_confirmed_active_at") or "not_started"),
            completed_at=str(recovery.get("completed_at") or "not_completed"),
        ))

    def _confirm_deterministic_admission(self, state: TransactionState) -> tuple[TransactionState, str | None]:
        """Confirm the persisted provider-free decision at the dispatch boundary.

        Managed runs spawned by the Inbox watcher carry the storage schema it
        admitted.  Those runs must have an immutable watcher decision of PASS.
        Direct callers retain their provider-free host readiness path, but the
        resulting PASS is still checkpointed before any provider can run.
        """
        if state.admission_decision == "PASS" and state.admission_completed_at:
            return state, None
        source = "RUNNER"
        if state.execution_mode == "MANAGED" and os.environ.get("DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA"):
            try:
                admission = load_admission_decision(self.root, state.run_id)
            except EngineeringStorageError:
                admission = None
            # A recovered row remains durable historical evidence after its
            # originating phase; only that phase may consume the result.
            if (
                admission is None
                or admission.get("run_id") != state.run_id
                or not isinstance(admission.get("submission_id"), str)
                or not admission["submission_id"]
                or admission.get("decision") != "PASS"
                or admission.get("execution_mode") != "MANAGED"
            ):
                blocked = replace(
                    state,
                    admission_decision="BLOCKED",
                    admission_completed_at=datetime.now(timezone.utc).isoformat(),
                    admission_evidence_source="WATCHER",
                )
                self.store.save(blocked)
                return blocked, "Provider dispatch refused: deterministic admission is not a persisted PASS."
            source = "WATCHER"
        admitted = replace(
            state,
            admission_decision="PASS",
            admission_completed_at=datetime.now(timezone.utc).isoformat(),
            admission_evidence_source=source,
        )
        self.store.save(admitted)
        return admitted, None

    def _require_provider_dispatch_admission(self, state: TransactionState) -> None:
        """Fail closed for every Codex-backed path, including reviewers."""
        if self._dispatch_guard_enforced and (
            state.admission_decision != "PASS" or not state.admission_completed_at
        ):
            raise RunnerError("provider invocation refused: deterministic admission is not completed with PASS")

    def _heartbeat(self) -> None:
        if self.lease_heartbeat is not None and self.lease_heartbeat.error is not None:
            raise RunnerError("active-run lease heartbeat was lost") from self.lease_heartbeat.error
        if self.active_lease is not None:
            self.active_lease = heartbeat_lease(self.root, self.active_lease, central_database=self.store.central_database)
            if self.lease_heartbeat is not None:
                self.lease_heartbeat.lease = self.active_lease

    def _provider_readiness_gate(
        self, state: TransactionState, *, require_codex: bool, require_github: bool
    ) -> TransactionState:
        """Persist a fail-closed provider block without losing the original phase.

        A waiting pull request is passive GitHub observation, whereas every
        agent action requires Codex too.  The saved original action lets a
        verified resume continue exactly where it stopped.
        """
        missing = provider_readiness_failures(self.root, require_github=require_github)
        if not require_codex:
            missing = tuple(provider for provider in missing if provider != "CODEX")
        if missing:
            blocked = replace(
                state,
                next_action="provider_auth_repair_required",
                diagnostic=redact_diagnostic(
                    "Provider readiness required before "
                    f"{state.auth_recovery_phase or state.phase}: {', '.join(missing)}."
                ),
                auth_recovery_phase=state.auth_recovery_phase or state.phase,
                auth_recovery_next_action=state.auth_recovery_next_action or state.next_action,
                auth_recovery_providers=tuple(missing),
            )
            self.store.save(blocked)
            write_live_status(self.root, blocked, blocked.next_action)
            return blocked
        if state.auth_recovery_phase is not None:
            restored = replace(
                state,
                next_action=state.auth_recovery_next_action or state.next_action,
                diagnostic=None,
                auth_recovery_phase=None,
                auth_recovery_next_action=None,
                auth_recovery_providers=(),
            )
            self.store.save(restored)
            write_live_status(self.root, restored, restored.next_action)
            return restored
        return state

    def _require_agent_readiness(self, state: TransactionState) -> None:
        checked = self._provider_readiness_gate(
            state, require_codex=True, require_github=state.execution_mode == "MANAGED"
        )
        if checked.next_action == "provider_auth_repair_required":
            raise ProviderReadinessBlocked(checked)

    def _publish_reviewer_progress(
        self,
        state: TransactionState,
        selection: ReviewerSelection,
        event: str,
        result: ReviewerResult | None = None,
    ) -> None:
        """Publish bounded reviewer lifecycle status without granting reviewer authority."""
        self._heartbeat()
        status_by_event = {"started": "running", "completed": "completed", "failed": "failed"}
        status = status_by_event.get(event)
        if status is None:
            return
        with self._reviewer_runtime_lock:
            for reviewer in self.reviewer_runtime:
                if reviewer.get("reviewer") != selection.reviewer:
                    continue
                reviewer["status"] = status
                if event == "started":
                    reviewer["started_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    reviewer["finished_at"] = datetime.now(timezone.utc).isoformat()
                    reviewer["failed"] = bool(result and result.failed)
                    churn = result.churn if result is not None and isinstance(result.churn, dict) else {}
                    command_count = churn.get("tool_loop_operations", 0)
                    reviewer["codex_commands_executed"] = (
                        command_count
                        if isinstance(command_count, int) and not isinstance(command_count, bool)
                        else 0
                    )
                break
            write_live_status(self.root, state, "Capability review: " + selection.reviewer, self.reviewer_runtime)

    def _persist_agent_usage(self, run_id: str) -> None:
        usage = getattr(self.agent, "last_usage", None)
        if isinstance(usage, dict):
            write_codex_usage(self.root, run_id, usage)

    def _persist_provider_invocation(self, state: TransactionState, *, phase: str, role: str = "agent", started_at: str | None = None, observed_usage: dict[str, object] | None = None, observed_metadata: dict[str, object] | None = None, observed_churn: dict[str, object] | None = None, observed_duration: float | None = None, observed_snapshots: tuple[dict[str, int], ...] | None = None, interruption_reason: str | None = None, invocation_id: str | None = None) -> str | None:
        """Append safe per-invocation evidence without affecting execution outcome."""
        usage = observed_usage if observed_usage is not None else getattr(self.agent, "last_usage", None)
        snapshots = observed_snapshots if observed_snapshots is not None else getattr(self.agent, "last_usage_snapshots", ())
        if not isinstance(usage, dict):
            usage = {}
        metadata = observed_metadata if observed_metadata is not None else getattr(self.agent, "last_runtime_metadata", None)
        churn = observed_churn if observed_churn is not None else getattr(self.agent, "last_churn", None)
        churn = {
            **(churn if isinstance(churn, dict) else {}),
            **self._provider_context_telemetry,
            **self._provider_dispatch_telemetry,
        }
        escalations = getattr(self.agent, "last_context_escalations", ())
        if isinstance(escalations, tuple):
            safe_escalations = [item for item in escalations if isinstance(item, dict)]
            if safe_escalations:
                churn.update({
                    "context_scope_effective": ContextScope.INVESTIGATION.value,
                    "context_escalation_count": len(safe_escalations),
                    "context_escalation_reasons": ",".join(str(item.get("reason", "")) for item in safe_escalations),
                    "context_escalation_boundaries": ",".join(str(item.get("boundary_kind", "")) for item in safe_escalations),
                    "context_escalation_diagnostic": str(safe_escalations[-1].get("diagnostic", "")),
                })
        if interruption_reason:
            # Existing bounded invocation telemetry is the durable diagnostic
            # channel; an interrupted turn has no AgentResult or final usage.
            churn["interruption_classification"] = "provider_turn_interrupted"
            churn["interruption_reason"] = redact_diagnostic(interruption_reason, limit=120)
        duration = observed_duration if observed_duration is not None else getattr(self.agent, "last_execution_seconds", None)
        raw_model = metadata.get("raw_provider_model") if isinstance(metadata, dict) else None
        normalized_model = normalize_codex_model(raw_model)
        try:
            from .storage import open_storage
            connection = open_storage(self.root)
            try:
                ordinal = int(connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM provider_invocations WHERE run_id=?", (state.run_id,)
                ).fetchone()[0])
            finally:
                connection.close()
            now = datetime.now(timezone.utc).isoformat()
            identifier = persist_provider_invocation(self.root, ProviderInvocation(
                run_id=state.run_id, ordinal=ordinal, provider="codex_cli",
                model=normalized_model,
                model_authority=AUTHORITATIVE if isinstance(raw_model, str) else "UNAVAILABLE",
                raw_provider_model=raw_model if isinstance(raw_model, str) else None,
                phase=phase, role=role, started_at=started_at or now, completed_at=now,
                duration_ms=round(duration * 1000) if isinstance(duration, (int, float)) and duration >= 0 else None,
                usage=usage, runtime_metadata=metadata if isinstance(metadata, dict) else None,
                retry_ordinal=state.repair_iterations, churn=churn if isinstance(churn, dict) else None,
                usage_snapshots=snapshots if isinstance(snapshots, tuple) else (), invocation_id=invocation_id,
            ), central_database=self.store.central_database)
            self._last_provider_invocation_id = identifier
            return identifier
        except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
            LOGGER.warning("Provider invocation telemetry is unavailable for run %s", state.run_id)
            return None

    def _record_agent_execution_time(self, state: TransactionState) -> TransactionState:
        """Accumulate only measured Codex CLI invocation time for this run."""
        # A nested recovery invocation checkpoints its immutable ledger before
        # returning to the ordinary lifecycle caller. Merge that one durable
        # field here so a stale in-memory phase state cannot erase it.
        try:
            persisted = self.store.load(state.run_id)
            if persisted.provider_recovery_attempts != state.provider_recovery_attempts:
                state = replace(state, provider_recovery_attempts=persisted.provider_recovery_attempts)
        except StateError:
            pass
        measured = getattr(self.agent, "last_execution_seconds", None)
        if isinstance(measured, bool) or not isinstance(measured, (int, float)):
            return state
        if not 0 <= measured <= 86_400:
            return state
        return replace(
            state,
            agent_execution_seconds=round((state.agent_execution_seconds or 0) + measured, 3),
        )

    def _record_validation_evidence(self, state: TransactionState, result: AgentResult) -> TransactionState:
        """Persist only bounded report evidence; it has no lifecycle authority."""
        if not result.validation_evidence:
            return state
        try:
            profile_context = load_validation_context(self.root, state.run_id)
        except EngineeringStorageError:
            profile_context = None
        required_controls = set(profile_context["required_validation_controls"]) if profile_context else set()
        tier = profile_context.get("selected_validation_tier") if profile_context else None
        for item in result.validation_evidence:
            command, summary = item.get("command", ""), item.get("result", "")
            kind = self._validation_kind(command)
            if kind is None:
                continue
            status = self._validation_summary_status(summary)
            try:
                record_managed_validation(
                    self.root, run_id=state.run_id, control=f"validation_{kind}", state=status,
                    required=True, currentness=state.repair_iterations,
                )
                validation_id = (
                    "git_diff_check" if kind == "format_or_diff" else
                    "documentation_contract" if kind == "documentation_contract" else
                    self._validation_id(command, kind) if kind == "browser_e2e" else
                    "repository_suite" if kind == "tests" and tier == "FULL" else
                    "engineering_python" if kind == "tests" else f"validation_{kind}"
                )
                record_validation_control_result(
                    self.root, run_id=state.run_id, validation_id=validation_id, category="agent",
                    control_identity=command[:160], required_for_profile=validation_id in required_controls, execution_status="EXECUTED",
                    result=status, evidence_ref="agent_result", observed_at=datetime.now(timezone.utc).isoformat(),
                    currentness=state.repair_iterations,
                )
            except EngineeringStorageError:
                LOGGER.warning("Managed validation evidence is unavailable for run %s", state.run_id)
        return replace(state, validation_evidence=result.validation_evidence)

    @staticmethod
    def _validation_summary_status(summary: str) -> str:
        """Classify bounded validation prose without treating ``no errors`` as a failure."""
        normalized = summary.casefold()
        if "not applicable" in normalized:
            return "NOT_APPLICABLE"
        if any(token in normalized for token in ("unavailable", "not recorded")):
            return "UNAVAILABLE"
        # Remove explicitly negated error terms before looking for a failure.
        # This keeps a real ``ERROR:`` or ``error detected`` fail-closed while
        # allowing ordinary success summaries such as ``no whitespace errors``.
        failure_subject = re.sub(
            r"\b(?:no|without)(?:\s+[a-z0-9_-]+){0,4}\s+errors?\b", "", normalized,
        )
        if any(token in failure_subject for token in ("fail", "error", "blocked", "timeout", "timed out")):
            return "FAIL"
        if any(token in normalized for token in ("pass", "passed", "succeed", "no errors", "without errors")):
            return "PASS"
        return "UNAVAILABLE"

    def _bind_validation_only_profile(
        self, state: TransactionState, producer_context: object,
    ) -> TransactionState:
        """Resolve and snapshot a producer-selected registry profile.

        This runs after deterministic admission and before any provider or
        deterministic-control work.  Its single immutable storage record is
        the source of truth for the rest of the run.
        """
        profile_payload = producer_context.get("validation_profile") if isinstance(producer_context, dict) else None
        try:
            profile, reference = resolve_producer_profile(profile_payload)
            bindings = profile_control_bindings(profile)
            record_validation_profile(
                self.root, run_id=state.run_id, selected_validation_tier=profile.tier,
                validation_profile_version=VALIDATION_PROFILE_VERSION,
                required_validation_controls=profile.required_controls,
                profile_reference=reference,
                profile_selection_source="producer_execution_context",
                control_bindings=bindings,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )
        except (EngineeringStorageError, ValidationProfileResolutionError):
            return self._save_terminal(
                state, "BLOCKED", "validation_profile_resolution",
                "Selected validation profile is unresolved or unavailable.",
            )
        return state

    def _execute_required_validation_controls(self, state: TransactionState) -> TransactionState:
        """Execute already-persisted required controls before qualification."""
        try:
            validation_context = load_validation_context(self.root, state.run_id)
        except EngineeringStorageError:
            validation_context = None
        if not isinstance(validation_context, dict):
            return self._save_terminal(state, "BLOCKED", "validation_profile_persistence", "Required validation profile evidence could not be loaded.")
        required = validation_context.get("required_validation_controls")
        if not isinstance(required, tuple) or not required:
            return self._save_terminal(state, "BLOCKED", "validation_profile_persistence", "Required validation profile evidence does not contain required controls.")
        bindings = validation_context.get("control_bindings")
        if not isinstance(bindings, tuple) or len(bindings) != len(required):
            return self._save_terminal(state, "BLOCKED", "validation_profile_persistence", "Required validation profile control bindings could not be loaded.")
        binding_by_id = {binding.get("validation_id"): binding for binding in bindings if isinstance(binding, dict)}
        if tuple(binding_by_id) != required:
            return self._save_terminal(state, "BLOCKED", "validation_profile_persistence", "Required validation profile control bindings are invalid.")
        validation = replace(state, phase="LOCAL_REPOSITORY_VALIDATION", next_action="execute_required_validation_controls")
        self.store.save(validation)
        write_live_status(self.root, validation, validation.next_action)
        self._managed_action(validation, "VALIDATION_EXECUTION")
        for ordinal, validation_id in enumerate(required, start=1):
            binding = binding_by_id[validation_id]
            command = binding.get("command")
            if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
                return self._save_terminal(validation, "BLOCKED", "validation_profile_persistence", "Required validation profile control binding is invalid.")
            if not command:
                try:
                    record_validation_control_result(
                        self.root, run_id=validation.run_id, validation_id=validation_id,
                        category=str(binding.get("category") or "unavailable"),
                        control_identity=str(binding.get("control_identity") or validation_id),
                        required_for_profile=True, execution_status="NOT_EXECUTED",
                        result="UNAVAILABLE", evidence_ref="control_launcher_unavailable",
                        observed_at=datetime.now(timezone.utc).isoformat(), currentness=validation.repair_iterations,
                    )
                except EngineeringStorageError:
                    return self._save_terminal(validation, "BLOCKED", "validation_evidence_persistence", "Required validation control evidence could not be persisted.")
                continue
            launcher = ValidationControlLauncher(
                validation_id=validation_id,
                category=str(binding.get("category") or "unavailable"),
                control_identity=str(binding.get("control_identity") or validation_id),
                command=tuple(command),
            )
            observed_at = datetime.now(timezone.utc).isoformat()
            command_id = f"required-control-{ordinal}-{uuid.uuid4().hex[:12]}"
            span = start_phase(
                self.root, validation.run_id, "VALIDATION", category="DETERMINISTIC_CONTROL",
                attempt=max(1, validation.repair_iterations + 1),
                metadata={"validation_id": launcher.validation_id, "command_id": command_id},
            )
            try:
                record_validation_command_invocation(
                    self.root, run_id=validation.run_id, validation_id=launcher.validation_id,
                    command_id=command_id, category=launcher.category,
                    control_identity=launcher.control_identity, required_for_profile=True,
                    started_at=observed_at, currentness=validation.repair_iterations,
                )
            except EngineeringStorageError:
                complete_phase(self.root, span, outcome="FAILED")
                return self._save_terminal(validation, "BLOCKED", "validation_evidence_persistence", "Required validation control invocation evidence could not be persisted.")
            exit_code: int | None
            previous_run_id = os.environ.get("DJCONNECT_ENGINEERING_VALIDATION_RUN_ID")
            os.environ["DJCONNECT_ENGINEERING_VALIDATION_RUN_ID"] = validation.run_id
            try:
                command_outcome = self._run_required_validation_command(launcher.command)
            finally:
                if previous_run_id is None:
                    os.environ.pop("DJCONNECT_ENGINEERING_VALIDATION_RUN_ID", None)
                else:
                    os.environ["DJCONNECT_ENGINEERING_VALIDATION_RUN_ID"] = previous_run_id
            # Compatibility with direct host tests that intentionally stub the
            # old scalar boundary; production always supplies a structured
            # deterministic outcome with captured subprocess output.
            if isinstance(command_outcome, int) or command_outcome is None:
                exit_code = command_outcome
                diagnostic_stdout = diagnostic_stderr = None
                diagnostic_capture_available = False
            else:
                exit_code = command_outcome.exit_code
                diagnostic_stdout = command_outcome.stdout
                diagnostic_stderr = command_outcome.stderr
                diagnostic_capture_available = command_outcome.diagnostic_capture_available
            completed_at = datetime.now(timezone.utc).isoformat()
            try:
                record_validation_command_terminal(
                    self.root, run_id=validation.run_id, command_id=command_id,
                    completed_at=completed_at, exit_code=exit_code,
                )
                result = "PASS" if exit_code == 0 else "FAIL" if exit_code is not None else "UNAVAILABLE"
                record_validation_control_result(
                    self.root, run_id=validation.run_id, validation_id=launcher.validation_id,
                    category=launcher.category, control_identity=launcher.control_identity,
                    required_for_profile=True, execution_status="EXECUTED", result=result,
                    evidence_ref="command_terminal", observed_at=completed_at,
                    currentness=validation.repair_iterations,
                )
            except EngineeringStorageError:
                complete_phase(self.root, span, outcome="FAILED")
                return self._save_terminal(validation, "BLOCKED", "validation_evidence_persistence", "Required validation control terminal evidence could not be persisted.")
            if exit_code != 0:
                try:
                    persist_validation_failure_diagnostic(
                        self.root, run_id=validation.run_id, command_id=command_id,
                        validation_id=launcher.validation_id,
                        control_identity=launcher.control_identity, exit_code=exit_code,
                        stdout=diagnostic_stdout, stderr=diagnostic_stderr,
                        capture_available=diagnostic_capture_available,
                        captured_at=completed_at,
                        central_database=self.store.central_database,
                        artifact_root=(self.store.central_database.parent / "artifacts") if self.store.central_database else None,
                    )
                except (EngineeringStorageError, OSError):
                    # Diagnostics are supplementary.  A capture failure must
                    # never erase or weaken the authoritative terminal exit.
                    LOGGER.warning("Required validation diagnostic capture unavailable for %s", command_id)
            complete_phase(self.root, span, outcome="COMPLETE" if exit_code == 0 else "FAILED")
        return validation

    def _run_required_validation_command(self, command: tuple[str, ...]):
        """Run one deterministic control, preserving unavailable terminals."""
        return self.validation_executor.run(self.root, command)

    def _managed_action(self, state: TransactionState, action: str, authority: str = "AUTONOMOUS_EP_ACTION", *, actor: str = "execution_host", evidence_ref: str = "runtime") -> None:
        """Best-effort evidence instrumentation; it never changes lifecycle outcome."""
        try:
            record_managed_action(self.root, run_id=state.run_id, action=action, authority=authority, actor=actor, evidence_ref=evidence_ref)
        except EngineeringStorageError:
            LOGGER.warning("Managed-autonomy evidence is unavailable for run %s", state.run_id)

    def _managed_gate(self, state: TransactionState, gate_type: str, status: str, pr: int, *, resolved: bool = False) -> None:
        try:
            record_managed_gate(self.root, run_id=state.run_id, gate_type=gate_type, status=status, related_pr=pr, phase=state.phase, resolution_actor="operator" if resolved else None, resolved_at=datetime.now(timezone.utc).isoformat() if resolved else None)
        except EngineeringStorageError:
            LOGGER.warning("Managed governance-gate evidence is unavailable for run %s", state.run_id)

    def _managed_pr_check(self, state: TransactionState, pr: PullRequestEvidence) -> None:
        """Persist current GitHub required-check evidence separately from historical waits."""
        role = state.transaction_kind
        if role not in {"IMPLEMENTATION", "FINALIZATION"}:
            return
        check_state = "PASS" if pr.checks_terminal and pr.checks_passed else "FAIL" if pr.checks_terminal else "WAITING"
        try:
            record_managed_pr_check(
                self.root, run_id=state.run_id, pr_number=pr.number, pr_role=role,
                pr_state=pr.state, merge_commit=pr.merge_commit,
                required_checks_state=check_state, evidence_ref="github_pr_status_check_rollup",
                currentness=state.repair_iterations,
            )
        except EngineeringStorageError:
            LOGGER.warning("Managed PR check evidence is unavailable for run %s", state.run_id)

    @staticmethod
    def _audit_record(
        *, iteration: int, failed_checks: str, proposed_action: str,
        result: AgentResult | None, outcome: str, empty_summary: str,
    ) -> dict[str, str]:
        """Build one safe, schema-compatible bounded-attempt record."""
        summary = (result.diagnostic if result else None) or empty_summary
        return {
            "iteration": str(iteration),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "failed_checks": redact_diagnostic(failed_checks),
            "proposed_action": redact_diagnostic(proposed_action),
            "agent_summary": redact_diagnostic(summary),
            "commit_sha": result.commit_sha if result and result.commit_sha else "not_recorded",
            "outcome": outcome,
        }

    def _record_repair_audit(self, state: TransactionState, *, failed_checks: str, objective: str, result: AgentResult | None, outcome: str) -> TransactionState:
        """Persist one bounded repair attempt, replacing its prior durable plan."""
        record = self._audit_record(
            iteration=state.repair_iterations,
            failed_checks=failed_checks,
            proposed_action=objective,
            result=result,
            outcome=outcome,
            empty_summary="Agent invocation did not return a repair summary.",
        )
        if state.repair_audit and state.repair_audit[-1].get("iteration") == str(state.repair_iterations) and state.repair_audit[-1].get("outcome") == "planned":
            return replace(state, repair_audit=state.repair_audit[:-1] + (record,))
        return replace(state, repair_audit=state.repair_audit + (record,))

    def _repair_plan(self, state: TransactionState) -> dict[str, str] | None:
        """Reload the exact pre-invocation plan; never reconstruct it from a prompt."""
        if not state.repair_audit:
            return None
        plan = state.repair_audit[-1]
        if plan.get("iteration") != str(state.repair_iterations) or plan.get("outcome") != "planned":
            return None
        return plan

    def _advance_after_repair_agent_result(self, repair: TransactionState, result: AgentResult) -> TransactionState:
        """Apply live or recovered Repair success using its persisted plan."""
        plan = self._repair_plan(repair)
        if plan is None:
            return self._save_terminal(repair, "BLOCKED", "repair_plan_missing", "Repair result cannot be resumed without its persisted repair plan.")
        failed_checks, objective = plan["failed_checks"], plan["proposed_action"]
        repair = self._record_repair_audit(
            repair, failed_checks=failed_checks, objective=objective, result=result,
            outcome="agent_failed" if result.terminal_state in {"BLOCKED", "FAILED"} else "submitted_for_recheck",
        )
        self.store.save(repair)
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(repair, result.terminal_state, "external_action_required", result.diagnostic)
        if result.pull_request != repair.pull_request:
            return self._save_terminal(repair, "BLOCKED", "bounded_scope_conflict", "Repair did not preserve the bounded pull request.")
        return self._poll(replace(repair, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="poll_required_checks"), result)

    def _record_local_validation_audit(self, state: TransactionState, *, result: AgentResult | None, outcome: str, profile: ValidationProfile) -> TransactionState:
        """Append one bounded local-validation iteration without sharing PR repair budget."""
        return replace(state, local_validation_audit=state.local_validation_audit + (self._audit_record(
            iteration=state.local_validation_iterations,
            failed_checks=(result.diagnostic if result else None) or "Local repository validation did not return a passing result.",
            proposed_action=f"{profile.tier}: {'; '.join(profile.commands)}",
            result=result,
            outcome=outcome,
            empty_summary="Agent invocation did not return a validation summary.",
        ),))

    @staticmethod
    def _is_environmental_validation_instability(result: AgentResult) -> bool:
        """Require explicit classification and contradictory bounded test evidence.

        This does not pass the implementation run.  It only stops repeated
        implementation retries when the same validation environment has been
        demonstrated as unstable.
        """
        if result.validation_disposition != "environmental_instability" or not result.validation_evidence:
            return False
        summaries = " ".join(item.get("result", "").casefold() for item in result.validation_evidence)
        passed = any(token in summaries for token in ("pass", "passed", "succeeded"))
        failed = any(token in summaries for token in ("fail", "failed", "timeout", "timed out", "error"))
        return passed and failed

    @staticmethod
    def _has_failed_validation_evidence(result: AgentResult) -> bool:
        """Return whether an agent supplied bounded evidence of a failed local check."""
        if not result.validation_evidence:
            return False
        summaries = " ".join(
            item.get("result", "").casefold()
            for item in result.validation_evidence
            if isinstance(item, dict)
        )
        return any(token in summaries for token in ("fail", "failed", "timeout", "timed out", "error"))

    @staticmethod
    def _is_external_agent_block(result: AgentResult) -> bool:
        """Keep explicit external blocks out of the mutable validation route."""
        return result.terminal_condition == "external_blocked"

    def _is_recoverable_implementation_validation_failure(
        self, state: TransactionState, result: AgentResult
    ) -> bool:
        """Admit only a verified implementation commit into local validation repair.

        The implementation provider may faithfully return FAILED after it has
        committed a bounded change and discovered that the broader local suite
        still fails.  That is a product-validation result, not an external
        dependency.  It is safe to enter the existing three-attempt local gate
        only after the host recorded the exact branch/HEAD evidence.
        """
        if not (
            result.terminal_state == "FAILED"
            and result.branch
            and result.branch != "main"
            and result.commit_sha
            and not result.pull_request
            and not self._is_external_agent_block(result)
            and self._has_failed_validation_evidence(result)
        ):
            return False
        return any(
            item.get("phase") == "EXECUTE_AGENT"
            and item.get("commit_sha") == result.commit_sha
            for item in state.commit_evidence
        )

    @staticmethod
    def _append_verified_commit_evidence(
        state: TransactionState, *, phase: str, commit_sha: str, description: str
    ) -> TransactionState:
        """Append immutable, compact commit evidence after its caller verified it.

        The checkpoint save is the transaction boundary.  Deduplication by
        phase and SHA makes retries idempotent without rewriting earlier
        operational evidence.
        """
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            return state
        if len(state.commit_evidence) >= MAX_COMMIT_EVIDENCE_RECORDS:
            return state
        if any(item["phase"] == phase and item["commit_sha"] == commit_sha for item in state.commit_evidence):
            return state
        try:
            record = verified_commit_evidence_record(
                phase=phase,
                observed_at=datetime.now(timezone.utc).isoformat(),
                commit_sha=commit_sha,
                description=redact_diagnostic(description),
            )
        except StateError:
            return state
        return replace(state, commit_evidence=state.commit_evidence + (record,))

    def _record_verified_result_commit(
        self, state: TransactionState, result: AgentResult, *, phase: str, description: str
    ) -> TransactionState:
        """Record an agent commit only when the checked-out repository proves it.

        A reported SHA is intentionally insufficient: the live repository must
        be clean, on the reported transaction branch, and at that exact SHA.
        Any unavailable or mismatched evidence is simply not recorded.
        """
        if not result.commit_sha or not re.fullmatch(r"[0-9a-f]{40}", result.commit_sha):
            return state
        expected_branch = result.branch or state.branch
        if not expected_branch or expected_branch == "main":
            return state
        try:
            evidence = self.repository.inspect(self.root)
        except RunnerError:
            return state
        if not (evidence.clean and evidence.branch == expected_branch and evidence.head_sha == result.commit_sha):
            return state
        return self._append_verified_commit_evidence(
            state, phase=phase, commit_sha=result.commit_sha, description=description,
        )

    @staticmethod
    def _validation_kind(command: str) -> str | None:
        """Classify only known validation commands at their live boundary.

        The command itself is transient observability input: timing metadata
        retains the category, never command text or output.
        """
        normalized = command.casefold()
        if any(tool in normalized for tool in ("markdown", "link", "documentation", "document-contract")):
            return "documentation_contract"
        if any(tool in normalized for tool in ("ruff", "flake8", "mypy", "pyright")):
            return "static_analysis"
        if any(tool in normalized for tool in ("bandit", "semgrep", "codeql", "pip-audit", "safety")):
            return "security"
        if "git diff --check" in normalized or "prettier" in normalized or "black --check" in normalized:
            return "format_or_diff"
        if any(tool in normalized for tool in ("npm run test:engineering-dashboard", "playwright", "selenium", "cypress", "e2e")):
            return "browser_e2e"
        if any(tool in normalized for tool in ("pytest", "unittest", "tox", "nox")):
            return "tests"
        return None

    @staticmethod
    def _validation_id(command: str, kind: str) -> str:
        """Reserve dashboard_browser for the canonical dashboard suite only."""
        if kind == "browser_e2e" and is_canonical_dashboard_command(command):
            return "dashboard_browser"
        return f"validation_{kind}"

    def _invoke_provider_attempt_with_timing(self, state: TransactionState, prompt: str, *, repair: bool = False, quality: bool = False, local_validation: bool = False, attempt: int | None = None) -> AgentResult:
        """Run one provider attempt; recovery launch authority lives in storage."""
        # Baseline capture is content-free and idempotent. Recovery progress
        # is captured separately at the interruption boundary below.
        capture_worktree_provenance(
            self.root, run_id=state.run_id, phase=state.phase, stage="baseline",
            transaction_baseline_sha=state.last_verified_sha,
        )
        role = role_for_phase(state.phase, repair=repair, quality=quality)
        initial_scope = initial_context_scope(
            phase=state.phase, repair_iterations=state.repair_iterations,
            objective=Path(state.prompt_path).read_text(encoding="utf-8"),
        )
        decision = provider_need_for_phase(state.phase)
        if not decision.required:
            raise RunnerError(f"provider invocation refused: {decision.reason}")
        self._require_provider_dispatch_admission(state)
        self._provider_context_telemetry = {
            "context_projected_bytes": len(prompt.encode("utf-8")),
            "context_budget_version": 1,
            "context_scope_policy": POLICY_ID,
            "context_scope_initial": initial_scope.value,
            "context_scope_effective": initial_scope.value,
            "context_escalation_count": 0,
        }
        self._require_agent_readiness(state)
        process_callback = getattr(self.agent, "set_process_callback", None)
        if callable(process_callback):
            process_callback(lambda process: self._provider_process_boundary(state, process))
        parent = (
            start_phase(self.root, state.run_id, "REPAIR", attempt=state.repair_iterations, metadata={"iteration": state.repair_iterations})
            if repair else start_phase(self.root, state.run_id, "QUALITY_CONTROL", metadata={"kind": "autonomous_refactor_quality"}) if quality else None
        )
        provider_attempt = attempt if attempt is not None else max(1, state.repair_iterations + 1)
        provider_metadata: dict[str, object] = {"provider": "codex_cli"}
        if local_validation:
            # This invocation is distinct from the implementation agent even
            # though both emit PROVIDER_EXECUTION spans.  The read-only
            # lifecycle projection uses this bounded marker to avoid showing
            # their combined duration on both visible steps.
            provider_metadata["lifecycle_step"] = "LOCAL_REPOSITORY_VALIDATION"
        provider = start_phase(
            self.root, state.run_id, "PROVIDER_EXECUTION", parent_phase_id=parent.phase_id if parent else None,
            attempt=provider_attempt, metadata=provider_metadata,
        )
        validation_spans: dict[str, ActivePhase | None] = {}
        validation_commands: dict[str, tuple[str, str]] = {}

        def command_boundary(event: str, command_id: str, command: str, exit_code: int | None = None) -> None:
            if event == "started":
                kind = self._validation_kind(command)
                if kind is not None:
                    validation_id = self._validation_id(command, kind)
                    try:
                        profile = load_validation_context(self.root, state.run_id)
                        required = validation_id in set(profile["required_validation_controls"]) if profile else False
                        started_at = datetime.now(timezone.utc).isoformat()
                        record_validation_command_invocation(
                            self.root, run_id=state.run_id, validation_id=validation_id, command_id=command_id,
                            category="agent", control_identity=command[:160], required_for_profile=required,
                            started_at=started_at, currentness=state.repair_iterations,
                        )
                        validation_commands[command_id] = (validation_id, started_at)
                    except EngineeringStorageError:
                        LOGGER.warning("Validation command start evidence is unavailable for run %s", state.run_id)
                    validation_spans[command_id] = start_phase(
                        self.root,
                        state.run_id,
                        "VALIDATION",
                        parent_phase_id=provider.phase_id if provider else None,
                        attempt=provider_attempt,
                        # This persisted span is the invocation receipt.  The
                        # result remains separate and is recorded only after
                        # the agent returns explicit validation evidence.
                        metadata={
                            "validation_kind": kind,
                            "validation_id": validation_id,
                            "command_id": command_id,
                        },
                    )
            elif event == "completed":
                if command_id in validation_commands:
                    try:
                        validation_id, _ = validation_commands[command_id]
                        evidence_ref = "command_terminal"
                        if validation_id == "dashboard_browser":
                            evidence = load_dashboard_evidence(self.root, state.run_id)
                            evidence_path = dashboard_evidence_path(self.root, state.run_id)
                            if evidence is not None and evidence_path.is_file():
                                artifact_id = f"dashboard-browser:{state.run_id}:{command_id}"
                                record_artifact(
                                    self.root, evidence_path, artifact_id=artifact_id,
                                    artifact_type="DASHBOARD_BROWSER_SHARD_RESULTS", content_type="application/json",
                                    created_at=datetime.now(timezone.utc).isoformat(), run_id=state.run_id,
                                )
                                evidence_ref = f"artifact:{artifact_id}"
                        record_validation_command_terminal(
                            self.root, run_id=state.run_id, command_id=command_id,
                            completed_at=datetime.now(timezone.utc).isoformat(), exit_code=exit_code,
                            evidence_ref=evidence_ref,
                        )
                    except EngineeringStorageError:
                        LOGGER.warning("Validation command terminal evidence is unavailable for run %s", state.run_id)
                active = validation_spans.pop(command_id, None)
                complete_phase(self.root, active)

        command_callback = getattr(self.agent, "set_command_callback", None)
        if callable(command_callback):
            command_callback(command_boundary)
        invocation_started = datetime.now(timezone.utc).isoformat()
        set_handoff_deadline = getattr(self.agent, "set_handoff_deadline_callback", None)
        deadline_started = time.monotonic()
        timeout = agent_timeout(
            phase=state.phase, repair=repair, quality=quality,
            local_validation=local_validation,
        )
        if callable(set_handoff_deadline):
            # Every managed provider action has a host-owned maximum.  The
            # client terminates the whole invocation process group when it
            # expires, so an inherited stdout pipe cannot strand the worker.
            set_handoff_deadline(
                lambda: time.monotonic() - deadline_started >= timeout.seconds
            )
        prior_validation_run_id = os.environ.get("DJCONNECT_ENGINEERING_VALIDATION_RUN_ID")
        os.environ["DJCONNECT_ENGINEERING_VALIDATION_RUN_ID"] = state.run_id
        try:
            if self._controlled_interruption_requested(state):
                raise CodexInvocationError(
                    "Provider turn interrupted before returning the required structured AgentResult.",
                    "Controlled qualification interruption.", next_action="NONE",
                    terminal_condition="provider_turn_interrupted",
                    interruption_reason="controlled_qualification_interruption",
                )
            result = self.agent.invoke(self.root, prompt)
        except KeyboardInterrupt as error:
            # A managed SIGINT/SIGTERM while the provider is active means no
            # valid AgentResult exists. Persist the canonical interruption
            # before returning control to the normal terminal path.
            interruption_reason = "host_shutdown_during_provider_turn"
            self._persist_provider_invocation(
                state, phase="REPAIR" if repair else "QUALITY_CONTROL" if quality else "PROVIDER_EXECUTION",
                role=role.value, started_at=invocation_started, interruption_reason=interruption_reason,
            )
            for active in validation_spans.values():
                complete_phase(self.root, active, outcome="INTERRUPTED")
            complete_phase(self.root, provider, outcome="INTERRUPTED")
            if parent:
                complete_phase(self.root, parent, outcome="INTERRUPTED")
            raise CodexInvocationError(
                "Provider turn interrupted before returning the required structured AgentResult.",
                "Execution Host received a shutdown signal while a provider turn was active.",
                next_action="NONE",
                terminal_condition="provider_turn_interrupted",
                interruption_reason=interruption_reason,
            ) from error
        except CodexHandoffTimeout as error:
            # Finalization has a dedicated reconciliation path below.  Every
            # other timed-out action becomes an ordinary, durable provider
            # failure rather than leaving the project lease active forever.
            if repair or state.transaction_kind == "FINALIZATION" or state.phase.upper() == "FINALIZE_AGENT":
                raise
            raise CodexInvocationError(
                f"Provider action exceeded the {timeout.seconds // 60}-minute host-owned deadline.",
                "The provider invocation was stopped after its configured workflow deadline.",
                next_action="inspect_codex_cli",
                terminal_condition="provider_invocation_timeout",
            ) from error
        except Exception as error:
            interruption_reason = error.interruption_reason if isinstance(error, CodexInvocationError) else None
            recovery = self._recovery_state(state.run_id)
            replacement_id = (
                recovery.get("replacement_invocation_id")
                if isinstance(recovery, dict) and recovery.get("state") == "RECOVERY_IN_PROGRESS"
                else None
            )
            original_invocation = self._persist_provider_invocation(
                state, phase="REPAIR" if repair else "QUALITY_CONTROL" if quality else "PROVIDER_EXECUTION",
                role=role.value, started_at=invocation_started, interruption_reason=interruption_reason,
                invocation_id=replacement_id if isinstance(replacement_id, str) else None,
            )
            for active in validation_spans.values():
                complete_phase(self.root, active, outcome="INTERRUPTED")
            outcome = "INTERRUPTED" if interruption_reason else "FAILED"
            complete_phase(self.root, provider, outcome=outcome)
            if parent:
                complete_phase(self.root, parent, outcome=outcome)
            # A single provider-proven interruption may create durable
            # recovery availability.  This attempt never launches a retry:
            # the outer state-driven controller consumes that evidence.
            if (
                interruption_reason and isinstance(error, CodexInvocationError)
                and original_invocation and not isinstance(recovery, dict)
            ):
                try:
                    create_recovery_available(
                        self.root, run_id=state.run_id, triggering_invocation_id=original_invocation,
                        lifecycle_phase=state.phase, branch=state.branch,
                        worktree_identity=str(self.root.resolve()),
                        lease_id=self.active_lease.lease_id if self.active_lease else None,
                        central_database=self.store.central_database,
                    )
                    capture_worktree_provenance(
                        self.root, run_id=state.run_id, phase=state.phase, stage="interrupted",
                        transaction_baseline_sha=state.last_verified_sha,
                    )
                except EngineeringStorageError:
                    raise CodexInvocationError(
                        "Provider interruption recovery evidence could not be persisted.",
                        "Recovery storage is unavailable.", next_action="NONE",
                        terminal_condition="provider_turn_interrupted",
                        interruption_reason=interruption_reason,
                    ) from error
            elif isinstance(recovery, dict) and recovery.get("state") == "RECOVERY_IN_PROGRESS":
                # Attempt two is terminal evidence, never another launch
                # opportunity. Unknown non-interruption failures are marked
                # ambiguous by the controller.
                record_replacement_terminal(
                    self.root, run_id=state.run_id,
                    outcome="INTERRUPTED" if interruption_reason else "FAILED",
                    central_database=self.store.central_database,
                )
            elif isinstance(recovery, dict) and recovery.get("state") == "RECOVERY_STARTING":
                # No process callback was observed, so the provider adapter
                # authoritatively failed before entering provider execution.
                record_pre_execution_launch_failure(
                    self.root, run_id=state.run_id, diagnostic_code=type(error).__name__,
                )
            raise
        finally:
            if prior_validation_run_id is None:
                os.environ.pop("DJCONNECT_ENGINEERING_VALIDATION_RUN_ID", None)
            else:
                os.environ["DJCONNECT_ENGINEERING_VALIDATION_RUN_ID"] = prior_validation_run_id
            if callable(command_callback):
                command_callback(None)
            if callable(process_callback):
                process_callback(None)
            if callable(set_handoff_deadline):
                set_handoff_deadline(None)
        durable_recovery = self._recovery_state(state.run_id)
        replacement_id = (
            durable_recovery.get("replacement_invocation_id")
            if isinstance(durable_recovery, dict) and durable_recovery.get("state") == "RECOVERY_IN_PROGRESS"
            else None
        )
        replacement = self._persist_provider_invocation(state, phase="REPAIR" if repair else "QUALITY_CONTROL" if quality else "PROVIDER_EXECUTION", role=role.value, started_at=invocation_started, invocation_id=replacement_id if isinstance(replacement_id, str) else None)
        if isinstance(durable_recovery, dict) and durable_recovery.get("state") == "RECOVERY_IN_PROGRESS":
            result_reference = persist_recovery_agent_result(
                self.root, run_id=state.run_id, invocation_id=str(replacement_id), result=result,
                central_database=self.store.central_database,
                artifact_root=(self.store.central_database.parent / "artifacts") if self.store.central_database else None,
            )
            record_replacement_terminal(
                self.root, run_id=state.run_id, outcome="SUCCESS", result_evidence_ref=result_reference,
                central_database=self.store.central_database,
            )
        if state.provider_recovery_attempts and state.provider_recovery_attempts[0].get("result") in {"RECOVERY_AVAILABLE", "ACTIVE"}:
            prior = state.provider_recovery_attempts[0]
            recovered = self._recovery_record(
                state, original=prior["original_invocation_id"], replacement=replacement or "unavailable",
                eligibility="ELIGIBLE", result="RECOVERED", requested_at=prior["requested_at"],
                started_at=prior["started_at"], completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.store.save(recovered)
        complete_phase(self.root, provider)
        if parent:
            complete_phase(self.root, parent)
        return result

    def _invoke_agent_with_timing(self, state: TransactionState, prompt: str, *, repair: bool = False, quality: bool = False, local_validation: bool = False, attempt: int | None = None) -> AgentResult:
        """Consume the durable recovery controller around individual attempts.

        The controller is the sole authority for replacement identity and
        launch.  This deliberately uses an iterative control flow: a provider
        exception never recursively re-enters the lifecycle method.
        """
        current_attempt = attempt
        while True:
            recovery = self._recovery_state(state.run_id)
            if isinstance(recovery, dict) and recovery.get("state") == "RECOVERY_AVAILABLE":
                precheck = self._provider_recovery_preflight(state)
                if precheck is not None:
                    mark_precheck_failed(self.root, run_id=state.run_id, diagnostic_code=precheck)
                    self._project_durable_recovery(state, self._recovery_state(state.run_id) or recovery)
                    raise CodexInvocationError(
                        "Provider interruption recovery cannot continue.", "Recovery continuation preflight failed.",
                        next_action="NONE", terminal_condition="provider_turn_interrupted",
                    )
                if not transition_recovery_state(
                    self.root, run_id=state.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
                    central_database=self.store.central_database,
                ):
                    continue
                if claim_replacement_launch(
                    self.root, run_id=state.run_id, central_database=self.store.central_database,
                ) is None:
                    raise CodexInvocationError(
                        "Provider interruption recovery launch is ambiguous.", "Replacement launch is already claimed.",
                        next_action="NONE", terminal_condition="provider_turn_interrupted",
                    )
                self.sleep(0.25)
                current_attempt = (current_attempt or max(1, state.repair_iterations + 1)) + 1
                # The next iteration dispatches the exact persisted
                # replacement ID; it does not create a fresh invocation.
                try:
                    result = self._invoke_provider_attempt_with_timing(
                        state, prompt, repair=repair, quality=quality,
                        local_validation=local_validation, attempt=current_attempt,
                    )
                except CodexInvocationError:
                    completed_recovery = self._recovery_state(state.run_id)
                    if isinstance(completed_recovery, dict):
                        self._project_durable_recovery(state, completed_recovery)
                    raise
                completed_recovery = self._recovery_state(state.run_id)
                if isinstance(completed_recovery, dict):
                    self._project_durable_recovery(state, completed_recovery)
                return result
            if (
                isinstance(recovery, dict)
                and recovery.get("state") == "RECOVERED"
                and recovery.get("lifecycle_phase") == state.phase
            ):
                replacement_id = recovery.get("replacement_invocation_id")
                if (
                    recovery.get("lifecycle_phase") != state.phase
                    or not isinstance(replacement_id, str)
                ):
                    raise CodexInvocationError(
                        "Recovered provider result is invalid.", "Recovery phase or invocation lineage is inconsistent.",
                        next_action="NONE", terminal_condition="provider_turn_interrupted",
                    )
                payload = load_recovery_agent_result(
                    self.root, str(recovery.get("result_evidence_ref") or ""),
                    run_id=state.run_id, invocation_id=replacement_id,
                )
                if payload is None:
                    raise CodexInvocationError(
                        "Recovered provider result is unavailable.", "Recovery result evidence failed integrity verification.",
                        next_action="NONE", terminal_condition="provider_turn_interrupted",
                    )
                try:
                    return AgentResult(
                        terminal_state=str(payload["terminal_state"]), branch=payload.get("branch"),
                        pull_request=payload.get("pull_request"), terminal_condition=str(payload.get("terminal_condition") or "repository_reconciled"),
                        diagnostic=payload.get("diagnostic"), repository_path=payload.get("repository_path"),
                        commit_sha=payload.get("commit_sha"),
                        validation_evidence=tuple(payload.get("validation_evidence") or ()),
                        quality_evidence=tuple(payload.get("quality_evidence") or ()),
                        validation_disposition=str(payload.get("validation_disposition") or "product_failure"),
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise CodexInvocationError(
                        "Recovered provider result is invalid.", "Recovery result evidence cannot be consumed safely.",
                        next_action="NONE", terminal_condition="provider_turn_interrupted",
                    ) from error
            if isinstance(recovery, dict) and recovery.get("state") in {"EXHAUSTED", "PRECHECK_FAILED", "AMBIGUOUS"}:
                raise CodexInvocationError(
                    "Provider interruption recovery cannot continue.", "Recovery budget is exhausted or recovery evidence is unsafe.",
                    next_action="NONE", terminal_condition="provider_turn_interrupted",
                )
            if isinstance(recovery, dict) and recovery.get("state") in {"RECOVERY_STARTING", "RECOVERY_IN_PROGRESS"}:
                reconciliation = reconcile_recovery(self.root, run_id=state.run_id)
                if reconciliation == "LAUNCH_UNCLAIMED":
                    claim = claim_replacement_launch(
                        self.root, run_id=state.run_id, central_database=self.store.central_database,
                    )
                    if claim is None:
                        raise CodexInvocationError(
                            "Provider recovery launch is ambiguous.", "Replacement launch claim could not be acquired.",
                            next_action="NONE", terminal_condition="provider_turn_interrupted",
                        )
                    current_attempt = (current_attempt or max(1, state.repair_iterations + 1)) + 1
                    return self._invoke_provider_attempt_with_timing(
                        state, prompt, repair=repair, quality=quality,
                        local_validation=local_validation, attempt=current_attempt,
                    )
                if reconciliation == "LAUNCH_CLAIMED_PREEXEC_FAILURE":
                    current_attempt = (current_attempt or max(1, state.repair_iterations + 1)) + 1
                    return self._invoke_provider_attempt_with_timing(
                        state, prompt, repair=repair, quality=quality,
                        local_validation=local_validation, attempt=current_attempt,
                    )
                if reconciliation == "RECOVERED":
                    continue
                if reconciliation == "SAME_PROVIDER_STILL_ACTIVE":
                    # This is an active run, not a provider failure. The
                    # caller returns the durable checkpoint without creating
                    # a second provider attempt or terminalizing it.
                    raise ProviderReadinessBlocked(state)
                # No restart path exists after a claimed/provider-started
                # attempt unless a terminal result can be proven.
                raise CodexInvocationError(
                    "Provider recovery is awaiting process reconciliation.", f"Recovery reconciliation: {reconciliation}.",
                    next_action="NONE", terminal_condition="provider_turn_interrupted",
                )
            try:
                result = self._invoke_provider_attempt_with_timing(
                    state, prompt, repair=repair, quality=quality,
                    local_validation=local_validation, attempt=current_attempt,
                )
                recovery = self._recovery_state(state.run_id)
                if isinstance(recovery, dict):
                    self._project_durable_recovery(state, recovery)
                return result
            except CodexInvocationError as error:
                recovery = self._recovery_state(state.run_id)
                if isinstance(recovery, dict) and recovery.get("state") in {"EXHAUSTED", "PRECHECK_FAILED", "AMBIGUOUS"}:
                    self._project_durable_recovery(state, recovery)
                if not error.provider_turn_interrupted or not isinstance(recovery, dict):
                    raise
                if recovery.get("state") != "RECOVERY_AVAILABLE":
                    raise
                # The top of the loop now claims the durable launch intent.
                # This handler never allocates or authorizes a replacement.
                continue

    def _run_local_repository_validation(
        self, state: TransactionState, implementation: AgentResult
    ) -> tuple[TransactionState, AgentResult]:
        """Run the bounded, mutable local gate before an implementation PR exists."""
        if state.action_intent == "VALIDATION_ONLY":
            # This gate is a delivery-only boundary.  A producer-authorized
            # qualification run may supply validation evidence but must never
            # be forced to invent a branch, commit, or pull request.
            return state, implementation
        branch = implementation.branch or state.branch
        # Legacy/resumed checkpoints can already carry their implementation PR.
        # Never rewrite that evidence; new managed prompts are instructed to
        # stop before PR creation and therefore enter this gate normally.
        if implementation.pull_request:
            return state, implementation
        if not branch:
            return self._save_terminal(
                state, "BLOCKED", "local_validation_scope", "Implementation must return one branch and no pull request before local validation."
            ), implementation
        validation = replace(
            state, phase="LOCAL_REPOSITORY_VALIDATION", branch=branch, pull_request=None,
            next_action="run_local_repository_validation", local_validation_iterations=0,
            local_validation_audit=(),
        )
        for iteration in range(1, MAX_LOCAL_REPOSITORY_VALIDATION_ATTEMPTS + 1):
            try:
                profile = classify(changed_paths(self.root, "main"))
            except OSError:
                profile = classify(())
            try:
                record_validation_profile(
                    self.root, run_id=validation.run_id, selected_validation_tier=profile.tier,
                    validation_profile_version=VALIDATION_PROFILE_VERSION,
                    required_validation_controls=profile.required_controls,
                    profile_reference=f"validation-profile-registry:{profile.tier}@{VALIDATION_PROFILE_VERSION}",
                    profile_selection_source="diff_classification",
                    control_bindings=profile_control_bindings(profile),
                    recorded_at=datetime.now(timezone.utc).isoformat(),
                )
            except (EngineeringStorageError, ValidationProfileResolutionError):
                return self._save_terminal(
                    validation, "BLOCKED", "validation_profile_persistence",
                    "Required validation profile evidence could not be persisted."
                ), implementation
            validation = replace(validation, local_validation_iterations=iteration)
            self.store.save(validation)
            write_live_status(self.root, validation, validation.next_action)
            instruction = f"""

Local repository validation gate — iteration {iteration} of {MAX_LOCAL_REPOSITORY_VALIDATION_ATTEMPTS}:
- Stay on exactly `{branch}`. Do not merge or change scope.
- Diff-derived validation profile: `{profile.tier}`. Required evidence: {"; ".join(profile.commands)}. If the diff is unavailable or scope becomes mixed, use the full required suite.
- You may correct only the bounded production code and its tests, commit and push those corrections, then rerun the required validation.
- If validation still fails, return `WAITING` with a concise safe diagnostic; the host may allow the next bounded iteration.
- Create one draft implementation pull request only after the required local validation passes. Return that same branch and PR number. Never poll remote checks.
"""
            try:
                result = self._invoke_agent_with_timing(
                    validation,
                    assemble_prompt(Path(validation.prompt_path), validation, managed_target=self.root) + instruction,
                    local_validation=True,
                    attempt=iteration,
                )
                validation = self._record_agent_execution_time(validation)
                validation = self._record_validation_evidence(validation, result)
                validation = self._record_verified_result_commit(
                    validation,
                    result,
                    phase="LOCAL_REPOSITORY_VALIDATION",
                    description="local_repository_validation_commit_verified",
                )
                self._persist_agent_usage(validation.run_id)
            except ProviderReadinessBlocked as blocked:
                return blocked.state, implementation
            except CodexInvocationError as error:
                validation = self._record_agent_execution_time(validation)
                self.console_detail = error.console_detail
                validation = self._record_local_validation_audit(validation, result=None, outcome="agent_failed", profile=profile)
                return self._terminalize_provider_invocation_error(validation, error), implementation
            if result.terminal_state in {"BLOCKED", "FAILED"}:
                if (
                    result.terminal_state == "FAILED"
                    and not self._is_external_agent_block(result)
                    and self._has_failed_validation_evidence(result)
                ):
                    validation = self._record_local_validation_audit(
                        validation, result=result, outcome="validation_failed", profile=profile
                    )
                    if self._is_environmental_validation_instability(result):
                        return self._save_terminal(
                            validation,
                            "BLOCKED",
                            "validation_infrastructure_recovery_required",
                            "Required local validation is unstable: a failed required suite and a passing isolated rerun were recorded without an implementation correction. Preserve this run and create a separate validation-infrastructure recovery item.",
                        ), implementation
                    continue
                validation = self._record_local_validation_audit(validation, result=result, outcome="agent_failed", profile=profile)
                return self._save_terminal(validation, result.terminal_state, "local_repository_validation_failed", result.diagnostic or "Local repository validation failed."), implementation
            if result.branch and result.branch != branch:
                validation = self._record_local_validation_audit(validation, result=result, outcome="agent_failed", profile=profile)
                return self._save_terminal(validation, "BLOCKED", "local_validation_scope", "Local validation changed the bounded implementation branch."), implementation
            if result.pull_request:
                validation = self._record_local_validation_audit(validation, result=result, outcome="validated", profile=profile)
                return validation, replace(
                    result,
                    branch=branch,
                    validation_evidence=implementation.validation_evidence + result.validation_evidence,
                )
            validation = self._record_local_validation_audit(validation, result=result, outcome="validation_failed", profile=profile)
            if self._is_environmental_validation_instability(result):
                return self._save_terminal(
                    validation,
                    "BLOCKED",
                    "validation_infrastructure_recovery_required",
                    "Required local validation is unstable: a failed required suite and a passing isolated rerun were recorded without an implementation correction. Preserve this run and create a separate validation-infrastructure recovery item.",
                ), implementation
        return self._save_terminal(validation, "BLOCKED", "local_validation_attempt_limit_reached", "Required local repository validation did not pass after 3 bounded iterations."), implementation

    def _run_autonomous_quality_control(
        self, state: TransactionState, implementation: AgentResult
    ) -> tuple[TransactionState, AgentResult]:
        """Run the required post-implementation refactor and quality boundary.

        The controller is autonomous but cannot widen delivery scope: it may
        amend only the current transaction branch and its existing PR.
        """
        quality = replace(
            state,
            phase="QUALITY_CONTROL_AGENT",
            branch=implementation.branch or state.branch,
            pull_request=implementation.pull_request or state.pull_request,
            next_action="autonomous_refactor_and_quality_control",
        )
        self.store.save(quality)
        write_live_status(self.root, quality, quality.next_action)
        prompt = assemble_prompt(
            Path(quality.prompt_path), quality,
            managed_target=self.root if quality.execution_mode == "MANAGED" else None,
        ) + """

Mandatory autonomous refactor and quality-control stage:
- Inspect the implementation now present on this transaction branch.
- Autonomously make only demonstrable maintainability, clarity, safety, or
  test-coverage improvements within the original bounded objective.
- Assess test coverage for every changed behavior. Add or strengthen focused
  regression tests whenever existing coverage does not prove that behavior.
- Assess the applicable operator, contract, and implementation documentation.
  Update it whenever the bounded change affects documented behavior; only
  leave documentation unchanged when the inspection proves it is unaffected.
- Run the relevant focused validation, including the added or affected tests,
  and `git diff --check`.
- Preserve the existing transaction branch and pull request. If changes are
  needed, commit and push them to that same branch; do not create another PR,
  merge, alter authority, or expand scope.
- Return the same pull-request number and branch after the quality boundary.
- In quality_evidence, record only work actually performed in this stage. Use
  activity values REFACTOR, TEST_COVERAGE, DOCUMENTATION, VALIDATION, or
  NO_CHANGE_REQUIRED and a short safe result for each. Do not include raw
  commands, output, prompts, source content, paths, secrets, or reasoning.
"""
        try:
            result = self._invoke_agent_with_timing(quality, prompt, quality=True)
            quality = self._record_agent_execution_time(quality)
            quality = self._record_validation_evidence(quality, result)
            quality = replace(quality, quality_evidence=result.quality_evidence)
            quality = self._record_verified_result_commit(
                quality,
                result,
                phase="QUALITY_CONTROL_AGENT",
                description="quality_control_commit_verified",
            )
            self._persist_agent_usage(quality.run_id)
        except ProviderReadinessBlocked as blocked:
            return blocked.state, implementation
        except CodexInvocationError as error:
            quality = self._record_agent_execution_time(quality)
            self.console_detail = error.console_detail
            return self._terminalize_provider_invocation_error(quality, error), implementation
        return self._advance_after_quality_control_agent_result(quality, implementation, result)

    def _advance_after_quality_control_agent_result(
        self, quality: TransactionState, implementation: AgentResult, result: AgentResult,
    ) -> tuple[TransactionState, AgentResult]:
        """Apply live or recovered QC success without provider-session state."""
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(quality, result.terminal_state, "autonomous_quality_control_failed", result.diagnostic or "Autonomous quality control did not complete."), implementation
        if implementation.pull_request and result.pull_request and result.pull_request != implementation.pull_request:
            return self._save_terminal(quality, "BLOCKED", "autonomous_quality_control_scope", "Autonomous quality control returned a different pull request."), implementation
        if implementation.branch and result.branch and result.branch != implementation.branch:
            return self._save_terminal(quality, "BLOCKED", "autonomous_quality_control_scope", "Autonomous quality control returned a different branch."), implementation
        return quality, replace(
            implementation,
            branch=result.branch or implementation.branch,
            pull_request=result.pull_request or implementation.pull_request,
            validation_evidence=implementation.validation_evidence + result.validation_evidence,
        )

    def _reject_historical_agent_pull_request(
        self, state: TransactionState
    ) -> TransactionState | None:
        """Keep a newly invoked agent from reusing a merged PR as its evidence.

        A run has no transaction evidence until its first agent invocation has
        returned.  A merged PR at that point belongs to earlier work and must
        not be marked ready or silently adopted into this new transaction.
        """
        if not state.pull_request:
            return None
        try:
            pull_request = self.github.pull_request(state.pull_request)
        except RunnerError:
            # _poll owns bounded retry behaviour for transient GitHub reads.
            return None
        if pull_request.state != "MERGED":
            return None
        try:
            objective = Path(state.prompt_path).read_text(encoding="utf-8")
        except OSError:
            objective = ""
        has_retry_lineage = bool(re.search(r"(?mi)^Retry-Of:\s*[-a-z0-9]+\s*$", objective))
        is_reconcilable_lineage_merge = (
            has_retry_lineage
            and state.branch is not None
            and state.branch == pull_request.head_branch
            and pull_request.base_branch == "main"
            and pull_request.merge_commit is not None
            and self.repository.main_contains(self.root, pull_request.merge_commit)
        )
        if is_reconcilable_lineage_merge:
            evidence = self.repository.inspect(self.root)
            reconciled = self._record_merged_evidence(state, pull_request, evidence)
            if reconciled.owner_authorized and reconciled.transaction_kind == "IMPLEMENTATION":
                return self._start_finalization(reconciled, pull_request.number)
            return self._cleanup(reconciled)
        return self._save_terminal(
            state,
            "BLOCKED",
            "historical_pull_request_evidence",
            f"Agent result referenced already merged PR #{pull_request.number}; a new transaction must return its own open pull request.",
        )

    def _advance_after_primary_agent_result(
        self, state: TransactionState, result: AgentResult, evidence: RepositoryEvidence,
    ) -> TransactionState:
        """Shared post-provider transition for live and recovered results."""
        if state.execution_mode == "GENESIS":
            return self._reconcile_genesis_result(state, result)
        recoverable_local_failure = self._is_recoverable_implementation_validation_failure(state, result)
        if state.transaction_kind == "IMPLEMENTATION" and state.action_intent == "MUTATING_DELIVERY" and (
            result.terminal_state not in {"BLOCKED", "FAILED"} or recoverable_local_failure
        ):
            if state.owner_authorized:
                state, result = self._run_local_repository_validation(state, result)
                if state.terminal:
                    return state
            state, result = self._run_autonomous_quality_control(state, result)
            if state.terminal:
                return state
        return self._continue_after_quality_control(state, result, evidence)

    def _continue_after_quality_control(
        self, state: TransactionState, result: AgentResult, evidence: RepositoryEvidence,
    ) -> TransactionState:
        """Advance delivery only after QC is already complete."""
        state = replace(
            state, phase="WAIT_FOR_TERMINAL_EVIDENCE", branch=result.branch or evidence.branch,
            pull_request=result.pull_request, next_action="poll_required_checks",
            terminal_condition=result.terminal_condition,
            finalization_branch=(result.branch or evidence.branch)
            if state.transaction_kind == "FINALIZATION" else state.finalization_branch,
            finalization_pull_request=result.pull_request
            if state.transaction_kind == "FINALIZATION" else state.finalization_pull_request,
            reconciliation_pull_request=result.pull_request
            if state.transaction_kind == "RECONCILIATION" else state.reconciliation_pull_request,
        )
        self.store.save(state)
        write_live_status(self.root, state, state.next_action)
        if state.owner_authorized and state.pull_request:
            historical = self._reject_historical_agent_pull_request(state)
            if historical is not None:
                return historical
            self.github.normalize_markdown_body(state.pull_request)
            self.github.ready(state.pull_request)
        return self._poll(state, result)

    def _advance_after_recovered_provider_result(
        self, state: TransactionState, result: AgentResult, evidence: RepositoryEvidence,
        lifecycle_phase: str,
    ) -> TransactionState:
        """Route a validated durable result to its existing phase handler."""
        if lifecycle_phase == "EXECUTE_AGENT":
            return self._advance_after_primary_agent_result(state, result, evidence)
        if lifecycle_phase == "QUALITY_CONTROL_AGENT":
            implementation = AgentResult("COMPLETE", branch=state.branch, pull_request=state.pull_request)
            quality, implementation = self._advance_after_quality_control_agent_result(state, implementation, result)
            return quality if quality.terminal else self._continue_after_quality_control(quality, implementation, evidence)
        if lifecycle_phase == "REPAIR_AGENT":
            return self._advance_after_repair_agent_result(state, result)
        if lifecycle_phase == "FINALIZE_AGENT":
            return self._advance_after_finalization_agent_result(state, result)
        return self._save_terminal(state, "BLOCKED", "recovered_provider_phase_invalid", "Recovered provider result has an unsupported lifecycle phase.")

    def run(
        self,
        prompt_path: Path,
        run_id: str | None = None,
        resume: bool = False,
        owner_authorized: bool = False,
        transaction_kind: str = "IMPLEMENTATION",
    ) -> TransactionState:
        # Private helpers remain directly testable, while every public runner
        # invocation enforces the admission boundary before provider dispatch.
        self._dispatch_guard_enforced = True
        objective = prompt_path.read_text(encoding="utf-8")
        state = self.store.load(run_id) if resume else None
        if resume and state is not None and dismissal_for_run(self.root, state.run_id):
            raise RunnerError("This execution has already been dismissed and cannot be resumed.")
        if (
            state is not None
            and state.phase in {"WAIT_FOR_TERMINAL_EVIDENCE", "WAIT_FOR_OPERATOR_MERGE"}
            and state.pull_request is not None
        ):
            # A pull-request wait is deliberately passive. Resuming it must
            # therefore only re-read the remote pull-request state. Re-running workspace admission, repository
            # synchronization, reviewer selection and memory retrieval here
            # creates expensive local churn while there is no new work to do.
            # Once a merge is observed, _poll performs the required
            # repository reconciliation before cleanup or Finalization.
            if Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current prompt")
            state = self._provider_readiness_gate(
                state, require_codex=False, require_github=True
            )
            if state.next_action == "provider_auth_repair_required":
                return state
            self._verify_engineering_platform()
            # Fresh PR evidence can transition this wait into a bounded
            # same-PR repair.  Reacquire the lease before polling, so that a
            # running repair has one durable owner and the dashboard reflects
            # it instead of projecting a false idle state.
            self.transaction = ExecutionTransaction(state=state, target_repository=self.root)
            try:
                self.active_lease = acquire_lease(
                    self.root, state.run_id, identity=self.host_identity,
                    instance_id=self.host_instance_id, process_id=os.getpid(), central_database=self.store.central_database,
                )
            except LeaseConflictError as error:
                raise RunnerError("active-run ownership conflict; execution is refused") from error
            self.lease_heartbeat = LeaseHeartbeat(self.root, self.active_lease, central_database=self.store.central_database)
            self.transaction = self.transaction.with_lease(self.active_lease)
            self.lease_heartbeat.start()
            write_live_status(self.root, state, state.next_action)
            try:
                return self._poll(state)
            finally:
                # Terminal and operator-wait saves release their own lease.
                # A direct passive return must also leave no synthetic owner.
                if self.active_lease is not None and self.active_lease.run_id == state.run_id:
                    if self.lease_heartbeat is not None:
                        self.active_lease = self.lease_heartbeat.stop()
                        self.lease_heartbeat = None
                    release_lease(self.root, self.active_lease, central_database=self.store.central_database)
                    self.active_lease = None
        try:
            # Storage compatibility is verified below before reading the
            # producer-owned run snapshot.  Keep this preflight parser free
            # of datastore access.
            context = resolve_execution_context(objective, self.root)
        except RunnerError as error:
            evidence = self.repository.inspect(self.root)
            state = state or TransactionState(
                run_id or f"run-{uuid.uuid4().hex[:12]}",
                evidence.repository,
                str(prompt_path),
                "INITIALIZE",
                owner_authorized=owner_authorized,
                execution_mode="GENESIS"
                if any(line.strip().casefold() == "execution mode: genesis" for line in objective.splitlines())
                else "MANAGED",
                action_intent="MUTATING_DELIVERY",
            )
            return self._save_terminal(state, "BLOCKED", "execution_context_resolution", str(error))
        evidence = self.repository.inspect(self.root)
        if state is not None:
            if state.repository != evidence.repository or Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current repository or prompt")
            if state.execution_mode != context.execution_mode:
                raise RunnerError("checkpoint execution mode conflicts with the prompt")
            if (
                context.target_repository
                and state.genesis_repository_path
                and Path(state.genesis_repository_path) != context.target_repository
            ):
                raise RunnerError("checkpoint Genesis target conflicts with the prompt")
            if state.terminal:
                return state
        else:
            state = TransactionState(
                run_id or f"run-{uuid.uuid4().hex[:12]}",
                evidence.repository,
                str(prompt_path),
                "INITIALIZE",
                owner_authorized=owner_authorized,
                transaction_kind=transaction_kind,
                execution_mode=context.execution_mode,
                action_intent=context.action_intent,
            )
        context = replace(context, run_id=state.run_id)
        passive_pr_wait = state.pull_request is not None and state.phase in {
            "WAIT_FOR_TERMINAL_EVIDENCE", "WAIT_FOR_OPERATOR_MERGE"
        }
        state = self._provider_readiness_gate(
            state,
            require_codex=not passive_pr_wait,
            require_github=context.execution_mode == "MANAGED",
        )
        if state.next_action == "provider_auth_repair_required":
            return state
        self.transaction = ExecutionTransaction(
            state=state,
            target_repository=context.target_repository or self.root,
        )
        # The Inbox watcher admits a run with the schema it has loaded.  A
        # freshly spawned runner reads source files again, so it can otherwise
        # observe a newer manifest and migrate the database while the watcher
        # still runs the older code.  Verify that compatibility boundary before
        # StateStore.save() opens (and could migrate) the datastore.
        if not passive_pr_wait and not self.agent.available():
            raise RunnerError("Codex CLI is not installed or invokable")
        self._verify_engineering_platform()
        recovery_snapshot = self._recovery_state(state.run_id)
        recovered_resume = (
            isinstance(recovery_snapshot, dict)
            and recovery_snapshot.get("state") == "RECOVERED"
            and recovery_snapshot.get("lifecycle_phase") in {"EXECUTE_AGENT", "QUALITY_CONTROL_AGENT", "REPAIR_AGENT", "FINALIZE_AGENT"}
        )
        try:
            persisted_submission = load_submission_for_run(self.root, state.run_id)
        except EngineeringStorageError:
            persisted_submission = None
        producer_context = persisted_submission.get("execution_context") if isinstance(persisted_submission, dict) else None
        action_intent = producer_context.get("action_intent", "MUTATING_DELIVERY") if isinstance(producer_context, dict) else "MUTATING_DELIVERY"
        context = resolve_execution_context(objective, self.root, action_intent=action_intent)
        if state.action_intent != context.action_intent:
            if state.phase != "INITIALIZE":
                raise RunnerError("checkpoint action intent conflicts with the producer execution context")
            state = replace(state, action_intent=context.action_intent)
        # Establish canonical transaction identity before persisting readiness evidence.
        self.store.save(state)
        # This envelope is deliberately persisted once and can be resumed
        # after process restart.  It is excluded from bottleneck ranking.
        self._total_phase = start_or_resume_phase(
            self.root, state.run_id, "TOTAL_EXECUTION", category="EXECUTION"
        )
        initialization = start_phase(self.root, state.run_id, "INITIALIZATION")
        readiness = evaluate_readiness(
            selected_profile(context.execution_mode),
            host_root=self.root,
            target_root=context.target_repository,
            managed_clean=lambda candidate: self.repository.inspect(candidate).clean,
            genesis_preflight=genesis_workspace_preflight,
        )
        observed_host = latest_host_preflight(self.root)
        observed_workspace = latest_workspace_preflight(self.root)
        observed_capability = latest_capability_preflight(self.root)
        preflight_facts = ReadinessFacts.from_preflight(
            host=observed_host,
            workspace=observed_workspace,
            capability=observed_capability,
            lease_available=True,
        )
        # Direct runner callers predate admission preflights. Preserve that
        # public compatibility path while the watcher supplies the complete
        # observed preflight facts for normal Inbox execution.
        facts = replace(
            preflight_facts,
            host_ready=preflight_facts.host_ready or not observed_host,
            repository_present=preflight_facts.repository_present or context.target_repository is not None or self.root.is_dir(),
            repository_clean=preflight_facts.repository_clean if preflight_facts.repository_clean is not None else (evidence.clean if context.execution_mode == "MANAGED" else True),
            remote_present=preflight_facts.remote_present if preflight_facts.remote_present is not None else True,
            upstream_present=preflight_facts.upstream_present if preflight_facts.upstream_present is not None else True,
            branch_present=preflight_facts.branch_present if preflight_facts.branch_present is not None else True,
            workspace_authorized=preflight_facts.workspace_authorized if preflight_facts.workspace_authorized is not None else True,
            capabilities_available=preflight_facts.capabilities_available if preflight_facts.capabilities_available is not None else True,
            providers_available=preflight_facts.providers_available if preflight_facts.providers_available is not None else True,
            datastore_healthy=preflight_facts.datastore_healthy if preflight_facts.datastore_healthy is not None else True,
            producer_contract_valid=preflight_facts.producer_contract_valid if preflight_facts.producer_contract_valid is not None else True,
        )
        decision = decide_readiness(readiness.profile, facts)
        record_readiness_evaluation(
            self.root, run_id=state.run_id, profile_id=decision.profile_id, profile_version=decision.profile_version,
            execution_mode=context.execution_mode, passed=readiness.ready and decision.passed,
            failed_requirements=decision.failed_requirements,
            facts=vars(decision.facts),
            evaluated_at=decision.evaluated_at, diagnostic=readiness.diagnostic or decision.diagnostic,
        )
        complete_phase(self.root, initialization, outcome="COMPLETE" if readiness.ready else "FAILED")
        if not readiness.ready:
            if context.execution_mode == "GENESIS":
                return self._save_terminal(state, "BLOCKED", "genesis_workspace_preflight", readiness.diagnostic)
            raise RunnerError(readiness.diagnostic or "Execution readiness failed")
        if context.execution_mode == "GENESIS":
            state = replace(state, genesis_repository_path=str(context.target_repository))
            authorization_blocker = target_repository_authorization(self.root, context.target_repository)
            if authorization_blocker:
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "genesis_repository_scope",
                    authorization_blocker,
                )
            owner = self._active_genesis_transaction(context.target_repository, state.run_id)
            if owner:
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "genesis_workspace_conflict",
                    f"Genesis preflight blocked: target workspace is owned by active run {owner}.",
                )
        reconciliation = start_phase(self.root, state.run_id, "RECONCILIATION")
        try:
            reconcile_stale(self.root, central_database=self.store.central_database)
        except Exception:
            complete_phase(self.root, reconciliation, outcome="FAILED")
            raise
        complete_phase(self.root, reconciliation)
        self.store.save(state)
        try:
            self.active_lease = acquire_lease(self.root, state.run_id, identity=self.host_identity, instance_id=self.host_instance_id, process_id=os.getpid(), central_database=self.store.central_database)
        except LeaseConflictError as error:
            blocked = decide_readiness(
                readiness.profile,
                replace(facts, lease_available=False),
            )
            record_readiness_evaluation(
                self.root, run_id=state.run_id, profile_id=blocked.profile_id, profile_version=blocked.profile_version,
                execution_mode=context.execution_mode, passed=False, failed_requirements=blocked.failed_requirements,
                facts=vars(blocked.facts),
                evaluated_at=blocked.evaluated_at, diagnostic=blocked.diagnostic,
            )
            raise RunnerError("active-run ownership conflict; execution is refused") from error
        self.lease_heartbeat = LeaseHeartbeat(self.root, self.active_lease, central_database=self.store.central_database)
        self.transaction = self.transaction.with_lease(self.active_lease)
        self.lease_heartbeat.start()
        if recovered_resume:
            # Consume immutable attempt-two evidence before any fresh provider
            # preparation or repository synchronization. This preserves the
            # original branch/worktree and cannot allocate a new invocation.
            result = self._invoke_agent_with_timing(state, "")
            state = self._record_agent_execution_time(state)
            state = self._record_validation_evidence(state, result)
            state = self._record_verified_result_commit(
                state, result, phase="EXECUTE_AGENT", description="implementation_agent_commit_verified",
            )
            return self._advance_after_recovered_provider_result(
                state, result, evidence, str(recovery_snapshot["lifecycle_phase"]),
            )
        # Synchronization is a host-owned admission step.  Do it while this
        # run owns the lease so agents never race each other for index.lock,
        # and so the bounded retry policy in the repository client is used.
        if context.execution_mode == "MANAGED":
            try:
                self.repository.synchronize_main(self.root)
                evidence = self.repository.inspect(self.root)
            except RunnerError as error:
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "repository_synchronization",
                    f"Repository synchronization failed: {redact_diagnostic(str(error))}",
                )
            # The watcher checked the target before claim. Re-check the exact
            # checkout after the host-owned synchronization while the run lease
            # is held: an operator/worktree race must never reach reviewers,
            # providers, or validation on a feature branch.
            if not (evidence.clean and evidence.branch == "main" and evidence.main_contains_head):
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "managed_target_baseline",
                    "Managed target baseline verification failed: expected clean main synchronized with origin/main.",
                )
        admission_phase = start_phase(self.root, state.run_id, "DETERMINISTIC_ADMISSION", category="ADMISSION")
        state, admission_error = self._confirm_deterministic_admission(state)
        complete_phase(
            self.root,
            admission_phase,
            outcome="COMPLETE" if admission_error is None else "FAILED",
        )
        if admission_error is not None:
            return self._save_terminal(state, "BLOCKED", "deterministic_admission", admission_error)
        if state.action_intent == "VALIDATION_ONLY":
            state = self._bind_validation_only_profile(state, producer_context)
            if state.terminal:
                return state
        if context.execution_mode == "MANAGED":
            self._managed_action(state, "IMPLEMENTATION" if state.action_intent == "MUTATING_DELIVERY" else "VALIDATION_ONLY")
        self._provider_dispatch_telemetry = {
            "provider_dispatch_before_admission": 0,
            "admission_completed": 1,
        }
        state = replace(state, phase="CAPABILITY_REVIEW", next_action="capability_review")
        self.store.save(state)
        capability_review = start_phase(self.root, state.run_id, "CAPABILITY_REVIEW")
        reviewer_evidence = (
            ReviewerEvidence.from_repository(state.run_id, state.execution_mode, evidence)
            if state.execution_mode == "MANAGED" and state.action_intent == "MUTATING_DELIVERY"
            else None
        )
        memory = retrieve_engineering_memory(self.root, prompt_path)
        selections = select_reviewers(
            objective,
            prompt_path,
            state.transaction_kind if state else "IMPLEMENTATION",
            load_engineering_memory(self.root),
        ) if state.action_intent == "MUTATING_DELIVERY" else ()
        self.reviewer_runtime = [
            {
                "reviewer": item.reviewer,
                "capability": item.capability,
                "selected_because": item.selected_because,
                "status": "selected",
                "selected_at": datetime.now(timezone.utc).isoformat(),
            }
            for item in selections
        ]
        write_live_status(
            self.root,
            state
            or TransactionState(
                run_id or "pending-run", evidence.repository, str(prompt_path), "INITIALIZE"
            ),
            "Capability Selection: "
            + (
                ", ".join(item.reviewer for item in selections)
                or "No specialist reviewers required."
            ),
            self.reviewer_runtime,
        )
        self._require_provider_dispatch_admission(state)
        results = run_reviews(
            self.root,
            selections,
            objective,
            self.agent if hasattr(self.agent, "review") else None,
            progress=lambda selection, event, result: self._publish_reviewer_progress(
                state, selection, event, result
            ),
            evidence=reviewer_evidence,
        )
        # Reviewer result objects retain only their own safe structured
        # telemetry, avoiding shared-client attribution across concurrent work.
        for reviewer in results:
            self._persist_provider_invocation(
                state, phase="CAPABILITY_REVIEW", role=f"reviewer:{reviewer.reviewer}",
                observed_usage=reviewer.usage, observed_metadata=reviewer.runtime_metadata,
                observed_churn=reviewer.churn, observed_duration=reviewer.duration_seconds,
                observed_snapshots=reviewer.usage_snapshots,
            )
        self.reviewer_records = records_for_storage(selections, results)
        # Reviewer reasoning is intentionally not merged into the primary
        # provider context.  Reviewers share the bounded factual snapshot, but
        # retain independent reasoning responsibility and advisory records.
        state = (
            replace(state, phase="EXECUTE_AGENT", next_action="invoke_agent")
            if context.execution_mode == "GENESIS"
            else self._reconcile(state, evidence)
        )
        self.store.save(state)
        write_live_status(self.root, state, state.next_action)
        complete_phase(self.root, capability_review)
        if state.terminal or state.phase == "WAIT_FOR_TERMINAL_EVIDENCE":
            return self._poll(state)
        if state.action_intent == "VALIDATION_ONLY":
            # The provider-free path still executes the persisted controls;
            # absent pre-execution evidence is pending work, not terminally
            # missing evidence.
            state = self._execute_required_validation_controls(state)
            if state.terminal:
                return state
            try:
                validation_context = load_validation_context(self.root, state.run_id)
            except EngineeringStorageError:
                validation_context = None
            required = validation_context.get("required_validation_controls", ()) if validation_context else ()
            controls = validation_context.get("controls", {}) if validation_context else {}
            results = [controls.get(control, {}).get("result") for control in required]
            if any(result == "FAIL" for result in results):
                return self._save_terminal(state, "BLOCKED", "required_validation_failed", "Required validation controls did not pass.")
            if not required or not all(result == "PASS" for result in results):
                return self._save_terminal(state, "BLOCKED", "required_validation_unresolved", "Required validation controls do not have authoritative PASS evidence.")
            return self._poll(state, AgentResult("COMPLETE"))
        try:
            if hasattr(self.agent, "set_activity_callback"):
                self.agent.set_activity_callback(
                    lambda activity: (self._heartbeat(), write_live_status(self.root, state, activity))[1]
                )
            if hasattr(self.agent, "set_transient_action_callback"):
                self.agent.set_transient_action_callback(
                    lambda action: (self._heartbeat(), write_live_status(
                        self.root, state, state.next_action, transient_action=action
                    ))[1]
                )
            if hasattr(self.agent, "set_process_callback"):
                self.agent.set_process_callback(
                    lambda process: (
                        write_runner_process(self.root, state.run_id, process),
                        (
                            record_provider_started(
                                self.root, run_id=state.run_id,
                                receipt_id=str(self._recovery_state(state.run_id).get("process_receipt_id")),
                                pid=int(process["pid"]), process_group=int(process["process_group"]),
                                central_database=self.store.central_database,
                            )
                            if isinstance(process, dict)
                            and isinstance(self._recovery_state(state.run_id), dict)
                            and self._recovery_state(state.run_id).get("state") == "RECOVERY_STARTING"
                            and self._recovery_state(state.run_id).get("process_receipt_id")
                            else None
                        ),
                    )
                )
            if hasattr(self.agent, "set_runtime_metadata_callback"):
                self.agent.set_runtime_metadata_callback(
                    lambda metadata: write_live_status(
                        self.root, state, state.next_action, runtime_metadata=metadata
                    )
                )
            if hasattr(self.agent, "set_workspace_progress_callback"):
                self.agent.set_workspace_progress_callback(
                    lambda progress: (
                        self._heartbeat(),
                        write_live_status(
                            self.root,
                            state,
                            state.next_action,
                            workspace_progress=progress,
                        ),
                    )[1]
                )
            result = self._invoke_agent_with_timing(
                state,
                assemble_prompt(
                    prompt_path,
                    state,
                    managed_target=self.root if state.execution_mode == "MANAGED" else None,
                    reviewer_evidence=reviewer_evidence,
                )
                + memory,
            )
            state = self._record_agent_execution_time(state)
            state = self._record_validation_evidence(state, result)
            state = self._record_verified_result_commit(
                state,
                result,
                phase="EXECUTE_AGENT",
                description="implementation_agent_commit_verified",
            )
            self._persist_agent_usage(state.run_id)
        except ProviderReadinessBlocked as blocked:
            return blocked.state
        except CodexInvocationError as error:
            state = self._record_agent_execution_time(state)
            self.console_detail = error.console_detail
            return self._terminalize_provider_invocation_error(state, error)
        return self._advance_after_primary_agent_result(state, result, evidence)

    def _active_genesis_transaction(self, target: Path, run_id: str) -> str | None:
        """Return another active Genesis run that owns the same local workspace."""
        for checkpoint_id in self.store.run_ids():
            try:
                candidate = self.store.load(checkpoint_id)
            except StateError:
                continue
            if (
                candidate.run_id != run_id
                and not candidate.terminal
                and candidate.execution_mode == "GENESIS"
                and candidate.genesis_repository_path
                and Path(candidate.genesis_repository_path) == target
            ):
                return candidate.run_id
        return None

    def _reconcile_genesis_result(self, state: TransactionState, result: AgentResult) -> TransactionState:
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(state, result.terminal_state, "external_action_required", result.diagnostic)
        if result.terminal_state != "COMPLETE" or result.terminal_condition != "local_commit_reconciled":
            return self._save_terminal(state, "BLOCKED", "genesis_local_commit_required", "Genesis Mode requires a reconciled local commit.")
        if not result.repository_path or not result.commit_sha:
            return self._save_terminal(state, "BLOCKED", "genesis_checkpoint_required", "Genesis Mode requires repository path and commit checkpoint evidence.")
        target = Path(result.repository_path).expanduser()
        authorization_blocker = target_repository_authorization(self.root, target)
        if not target.is_absolute() or authorization_blocker:
            return self._save_terminal(state, "BLOCKED", "genesis_repository_scope", authorization_blocker or "Genesis preflight blocked: WORKSPACE_TARGET_AUTHORIZED: target path must be absolute.")
        try:
            git = getattr(self.repository, "provider", GitProvider())
            head = git.execute(target, "git", "rev-parse", "HEAD")
            clean = git.execute(target, "git", "status", "--porcelain", "--untracked-files=all")
        except OSError as error:
            return self._save_terminal(state, "BLOCKED", "genesis_local_repository_required", str(error))
        actual_head = head.stdout.strip()
        workspace = "clean" if not clean.stdout.strip() else "dirty"
        if head.returncode or clean.returncode or actual_head != result.commit_sha or workspace != "clean":
            diagnostic = (
                "Genesis reconciliation failed: "
                f"reported commit={result.commit_sha or 'missing'}; "
                f"actual HEAD={actual_head or 'unavailable'}; workspace={workspace}."
            )
            return self._save_terminal(state, "BLOCKED", "genesis_reconciliation_required", diagnostic)
        reconciled = replace(state, genesis_repository_path=str(target), genesis_commit_sha=result.commit_sha, latest_repository_evidence=f"local genesis commit {result.commit_sha}")
        reconciled = self._append_verified_commit_evidence(
            reconciled,
            phase="EXECUTE_AGENT",
            commit_sha=result.commit_sha,
            description="genesis_implementation_commit_verified",
        )
        return self._save_terminal(reconciled, "COMPLETE", "genesis_local_commit_reconciled")

    def _verify_engineering_platform(self) -> None:
        try:
            self.detected_codex_cli = self.agent.version()
            self.platform_manifest = EngineeringPlatformManifest.load(
                package_path("ENGINEERING_PLATFORM_VERSION.json")
            )
            validate_compatibility(
                self.platform_manifest, self.compatibility, self.detected_codex_cli
            )
            configuration = PlatformConfiguration.load(self.root)
            if configuration.platform.version != self.platform_manifest.platform_version:
                raise EngineeringPlatformCompatibilityError("Platform identity and manifest version mismatch")
        except (EngineeringPlatformCompatibilityError, PlatformConfigurationError) as error:
            raise RunnerError(str(error)) from error

    def _reconcile(self, state: TransactionState, evidence: RepositoryEvidence) -> TransactionState:
        if state.branch and evidence.branch not in {"main", state.branch}:
            raise RunnerError("current branch conflicts with active transaction")
        if state.pull_request:
            return replace(
                state,
                phase="WAIT_FOR_TERMINAL_EVIDENCE",
                last_verified_sha=evidence.head_sha,
                next_action="poll_required_checks",
            )
        if state.transaction_kind == "FINALIZATION" and state.phase == "FINALIZE_AGENT":
            if state.finalization_pull_request is None:
                # Finalization entry is persisted before its provider handoff.
                # A new host must continue that exact same entry rather than
                # requiring a PR that has not yet been created.
                return replace(
                    state,
                    last_verified_sha=evidence.head_sha,
                    next_action="create_finalization",
                )
            return self._recover_finalization_pull_request(state, evidence)
        if (
            state.transaction_kind == "FINALIZATION"
            and state.finalization_pull_request is None
            and state.finalization_branch is not None
        ):
            # Resumed-host setup temporarily projects CAPABILITY_REVIEW. Keep
            # the already durable Finalization entry instead of falling back
            # into the implementation execution phase.
            return replace(
                state,
                phase="FINALIZE_AGENT",
                last_verified_sha=evidence.head_sha,
                next_action="create_finalization",
            )
        if (
            state.transaction_kind == "FINALIZATION"
            and state.implementation_pull_request is None
            and not state.finalization_pull_request
        ):
            return replace(
                state,
                phase="FINALIZE_AGENT",
                last_verified_sha=evidence.head_sha,
                next_action="create_finalization",
            )
        if state.transaction_kind == "RECONCILIATION" and not state.reconciliation_pull_request:
            return replace(
                state, phase="RECONCILE_AGENT", last_verified_sha=evidence.head_sha,
                next_action="create_reconciliation",
            )
        if (
            state.transaction_kind == "FINALIZATION"
            and state.finalization_merge_commit
            and self.repository.main_contains(self.root, state.finalization_merge_commit)
        ):
            return self._cleanup(state)
        if (
            state.transaction_kind == "IMPLEMENTATION"
            and state.implementation_merge_commit
            and self.repository.main_contains(self.root, state.implementation_merge_commit)
        ):
            if state.owner_authorized:
                return self._start_finalization(state, state.implementation_pull_request or 0)
        return replace(
            state,
            phase="EXECUTE_AGENT",
            last_verified_sha=evidence.head_sha,
            next_action="invoke_agent",
        )

    def _recover_finalization_pull_request(
        self, state: TransactionState, evidence: RepositoryEvidence,
    ) -> TransactionState:
        """Persist an existing Finalization PR only after current proof.

        The checkpointed branch is the recovery identity. A missing or
        ambiguous match is terminally blocked rather than retried, so recovery
        never invokes an agent or creates a replacement Finalization PR.
        """
        if state.finalization_pull_request:
            return replace(
                state, pull_request=state.finalization_pull_request,
                branch=state.finalization_branch, phase="WAIT_FOR_TERMINAL_EVIDENCE",
                next_action="poll_required_checks",
            )
        if (
            not state.finalization_branch
            or not state.implementation_merge_commit
            or evidence.branch != "main"
            or not evidence.clean
            or not evidence.main_contains_head
            or not self.repository.main_contains(self.root, state.implementation_merge_commit)
        ):
            return self._save_terminal(
                state, "BLOCKED", "finalization_recovery_evidence_required",
                "Finalization recovery requires a clean current main checkout and a verified implementation merge.",
            )
        candidate = self.github.pull_request_for_head_branch(state.finalization_branch)
        if candidate is None:
            return self._save_terminal(
                state, "BLOCKED", "finalization_recovery_evidence_required",
                "No Finalization pull request matches the checkpointed branch; no replacement was created.",
            )
        if (
            candidate.head_branch != state.finalization_branch
            or candidate.base_branch != "main"
            or candidate.state not in {"OPEN", "MERGED"}
        ):
            return self._save_terminal(
                state, "BLOCKED", "finalization_recovery_evidence_invalid",
                "Finalization pull request evidence does not match the checkpointed branch and main base.",
            )
        recovered = replace(
            state,
            branch=state.finalization_branch,
            pull_request=candidate.number,
            finalization_pull_request=candidate.number,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            next_action="poll_required_checks",
            last_verified_sha=evidence.head_sha,
            latest_repository_evidence=_repository_summary(evidence),
            latest_github_evidence=_pull_request_summary(candidate),
        )
        self.store.save(recovered)
        write_live_status(self.root, recovered, recovered.next_action)
        return self._poll(recovered)

    def _poll(self, state: TransactionState, result: AgentResult | None = None) -> TransactionState:
        if state.pull_request:
            state = self._provider_readiness_gate(
                state, require_codex=False, require_github=True
            )
            if state.next_action == "provider_auth_repair_required":
                return state
        if result and result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(
                state, result.terminal_state, "external_action_required", result.diagnostic
            )
        if result and result.pull_request and result.branch in {None, "main"}:
            return self._save_terminal(
                state,
                "BLOCKED",
                "invalid_pull_request_evidence",
                "Agent result referenced a pull request without a transaction branch; the current main branch cannot be reused as execution evidence.",
            )
        if not state.pull_request:
            if state.transaction_kind == "RECONCILIATION" and result and result.pull_request:
                return self._save_terminal(
                    state, "BLOCKED", "reconciliation_pull_request_forbidden",
                    "Automatic reconciliation must commit directly to main and must not create a pull request.",
                )
            if state.transaction_kind == "RECONCILIATION" and result and result.terminal_state == "COMPLETE":
                evidence = self.repository.inspect(self.root)
                if (
                    result.terminal_condition == "repository_reconciled"
                    and result.commit_sha
                    and evidence.clean
                    and evidence.branch == "main"
                    and evidence.head_sha == result.commit_sha
                    and evidence.main_contains_head
                ):
                    reconciled = replace(state, last_verified_sha=result.commit_sha)
                    reconciled = self._append_verified_commit_evidence(
                        reconciled,
                        phase="RECONCILE_AGENT",
                        commit_sha=result.commit_sha,
                        description="end_reconciliation_commit_verified",
                    )
                    return self._cleanup(reconciled)
                return self._save_terminal(
                    state, "BLOCKED", "automatic_reconciliation_evidence_required",
                    "Automatic reconciliation requires a clean main checkout containing its reported commit.",
                )
            if result and result.terminal_state == "COMPLETE":
                evidence = self.repository.inspect(self.root)
                if evidence.clean and evidence.main_contains_head:
                    return self._cleanup(state)
            return replace(
                state, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="obtain_repository_evidence"
            )
        attempts = 0
        while True:
            pr_operation = start_phase(self.root, state.run_id, "PR_OR_MERGE")
            try:
                pr = self.github.pull_request(state.pull_request)
            except RunnerError:
                complete_phase(self.root, pr_operation, outcome="FAILED")
                attempts += 1
                if attempts >= 3:
                    # No provider or foreground GitHub operation remains after
                    # the bounded evidence retries.  This is a durable passive
                    # wait, so it must relinquish the run lease just like the
                    # operator-merge hand-off.  Otherwise a lifecycle worker
                    # cannot resume the same run after the transient outage.
                    return self._save_operator_merge_wait(
                        replace(
                            state,
                            phase="WAIT_FOR_TERMINAL_EVIDENCE",
                            next_action="retry_github_evidence",
                        )
                    )
                wait = start_phase(self.root, state.run_id, "EXTERNAL_CI_WAIT", metadata={"reason": "github_evidence_retry"})
                self.sleep(min(30, 2**attempts))
                complete_phase(self.root, wait)
                continue
            complete_phase(self.root, pr_operation)
            self._managed_pr_check(state, pr)
            # GitHub can omit or retire a check rollup once a pull request is
            # merged.  The merge is authoritative remote evidence and must
            # therefore take precedence over pre-merge check polling.
            if pr.state != "MERGED" and not pr.checks_terminal:
                self._managed_action(state, "GITHUB_REQUIRED_CHECK", "EXTERNAL_PLATFORM_EVENT", actor="github", evidence_ref="required_check_waiting")
                wait = start_phase(self.root, state.run_id, "EXTERNAL_CI_WAIT", metadata={"reason": "github_checks"})
                self.sleep(15)
                complete_phase(self.root, wait)
                continue
            repairable_mergeability = pr.state == "OPEN" and pr.merge_state_status in {"BEHIND", "DIRTY", "UNSTABLE"}
            if pr.state != "MERGED" and (not pr.checks_passed or repairable_mergeability):
                if state.owner_authorized:
                    failed = (
                        ", ".join(pr.failed_checks) or "required CI check"
                        if not pr.checks_passed
                        else "pull request is behind or cannot merge cleanly with main"
                    )
                    if state.repair_iterations >= MAX_PR_CHECK_REPAIR_ATTEMPTS:
                        return self._save_terminal(
                            state,
                            "BLOCKED",
                            "repair_attempt_limit_reached",
                            "Required CI checks still failed after "
                            f"{MAX_PR_CHECK_REPAIR_ATTEMPTS} bounded repair attempts: {failed}.",
                            terminal_condition="repair_attempt_limit_reached",
                        )
                    return self._repair(
                        state,
                        f"{failed} failed. Repair only the bounded transaction defects, commit and push the repair, then return the same pull request number.",
                    )
                return self._save_terminal(
                    state, "FAILED", "required_checks_failed", "Required CI check failed."
                )
            if pr.state == "MERGED":
                # A merge is remote evidence. Refresh origin/main before
                # verifying ancestry, without switching or fast-forwarding
                # the shared checkout during a passive wait poll. Local main
                # synchronization belongs to finalization/cleanup, after the
                # remote merge has been verified.
                try:
                    self.repository.refresh_main_reference(self.root)
                    evidence = self.repository.inspect(self.root)
                except RunnerError:
                    return self._save_operator_merge_wait(state)
                if pr.merge_commit and self.repository.remote_main_contains(self.root, pr.merge_commit):
                    gate_type = {"IMPLEMENTATION": "IMPLEMENTATION_MERGE_APPROVAL", "FINALIZATION": "FINALIZATION_MERGE_APPROVAL", "RECONCILIATION": "RECONCILIATION_MERGE_APPROVAL"}[state.transaction_kind]
                    self._managed_gate(state, gate_type, "SATISFIED", pr.number, resolved=True)
                    action = {"IMPLEMENTATION": "IMPLEMENTATION_MERGE", "FINALIZATION": "FINALIZATION_MERGE", "RECONCILIATION": "RECONCILIATION_MERGE"}[state.transaction_kind]
                    self._managed_action(state, action, "EXPECTED_OPERATOR_GATE", actor="operator", evidence_ref="github_merge")
                    state = self._record_merged_evidence(state, pr, evidence)
                    if state.owner_authorized and state.transaction_kind == "IMPLEMENTATION":
                        return self._start_finalization(state, pr.number)
                    if state.owner_authorized and state.transaction_kind == "FINALIZATION":
                        return self._start_automatic_reconciliation(state)
                    return self._cleanup(state)
            # A green PR is an explicit hand-off to the operator.  The runner
            # must not turn that durable waiting state into a synthetic failure
            # merely because its foreground process has ended.
            waiting = replace(
                state,
                phase="WAIT_FOR_OPERATOR_MERGE",
                next_action="await_operator_pr_merge",
                terminal_condition="operator_merge_required",
                diagnostic=None,
                waiting_for_merge_since=state.waiting_for_merge_since
                or datetime.now(timezone.utc).isoformat(),
            )
            gate_type = {"IMPLEMENTATION": "IMPLEMENTATION_MERGE_APPROVAL", "FINALIZATION": "FINALIZATION_MERGE_APPROVAL", "RECONCILIATION": "RECONCILIATION_MERGE_APPROVAL"}[state.transaction_kind]
            self._managed_gate(waiting, gate_type, "WAITING", state.pull_request)
            return self._save_operator_merge_wait(waiting)

    def _repair(self, state: TransactionState, objective: str) -> TransactionState:
        failed_checks = objective.split(" failed.", 1)[0]
        repair = replace(
            state,
            phase="REPAIR_AGENT",
            next_action="repair_bounded_validation_failure",
            repair_iterations=state.repair_iterations + 1,
        )
        # This plan is the canonical, bounded context needed to apply a
        # successful recovered result after a host restart. It is persisted
        # before the provider can begin; it is never rebuilt from prompt text.
        repair = self._record_repair_audit(
            repair, failed_checks=failed_checks, objective=objective, result=None, outcome="planned",
        )
        self.store.save(repair)
        write_live_status(self.root, repair, repair.next_action)
        try:
            result = self._invoke_agent_with_timing(
                repair,
                assemble_prompt(
                    Path(repair.prompt_path),
                    repair,
                    managed_target=self.root if repair.execution_mode == "MANAGED" else None,
                )
                + f"\n\nRepair objective: {objective}",
                repair=True,
            )
            repair = self._record_agent_execution_time(repair)
            repair = self._record_validation_evidence(repair, result)
            repair = self._record_verified_result_commit(
                repair,
                result,
                phase="REPAIR_AGENT",
                description="pull_request_repair_commit_verified",
            )
            self._persist_agent_usage(repair.run_id)
        except CodexHandoffTimeout:
            repair = self._record_agent_execution_time(repair)
            repair = self._record_repair_audit(
                repair, failed_checks=failed_checks, objective=objective, result=None,
                outcome="agent_timed_out",
            )
            return self._save_terminal(
                repair,
                "BLOCKED",
                "repair_agent_timeout",
                "Repair agent exceeded the host-owned deadline; no further repair was started.",
                terminal_condition="repair_agent_timeout",
            )
        except ProviderReadinessBlocked as blocked:
            return blocked.state
        except CodexInvocationError as error:
            repair = self._record_agent_execution_time(repair)
            self.console_detail = error.console_detail
            repair = self._record_repair_audit(repair, failed_checks=failed_checks, objective=objective, result=None, outcome="agent_failed")
            return self._terminalize_provider_invocation_error(repair, error)
        return self._advance_after_repair_agent_result(repair, result)

    def _start_finalization(
        self, state: TransactionState, implementation_pr: int
    ) -> TransactionState:
        self._managed_action(state, "POST_IMPLEMENTATION_MERGE")
        if state.finalization_pull_request:
            return replace(
                state,
                transaction_kind="FINALIZATION",
                pull_request=state.finalization_pull_request,
                branch=state.finalization_branch,
                phase="WAIT_FOR_TERMINAL_EVIDENCE",
                next_action="poll_required_checks",
            )
        finalization_phase = start_phase(self.root, state.run_id, "REPOSITORY_FINALIZATION")
        synchronize = getattr(self.repository, "synchronize_main", None)
        if callable(synchronize):
            synchronize(self.root)
        evidence = self.repository.inspect(self.root)
        if not evidence.clean or evidence.branch != "main":
            complete_phase(self.root, finalization_phase, outcome="FAILED")
            return self._save_post_merge_sync_wait(
                state,
                "Finalization is waiting for a clean, synchronized main checkout.",
            )
        expected_branch = state.finalization_branch or f"codex/finalize-{state.run_id}"
        finalization = replace(
            state,
            phase="FINALIZE_AGENT",
            transaction_kind="FINALIZATION",
            pull_request=None,
            branch=expected_branch,
            finalization_branch=expected_branch,
            next_action="create_finalization",
            implementation_pull_request=implementation_pr or state.implementation_pull_request,
            latest_repository_evidence=_repository_summary(evidence),
            waiting_for_merge_since=None,
        )
        self._managed_action(finalization, "FINALIZATION")
        self.store.save(finalization)
        write_live_status(self.root, finalization, finalization.next_action)
        complete_phase(self.root, finalization_phase)
        instruction = (
            f"\n\nThe implementation PR #{implementation_pr} is merged. Execute only its mandatory "
            "governance-only Finalization: reconcile the four rolling records and immutable Prompt "
            f"History, then create a draft Finalization PR on exactly `{expected_branch}`. After GitHub assigns its number, run "
            f"`python3 -m engineering_platform.repository_handoff --run-id {finalization.run_id} "
            f"--platform-version {self.platform_manifest.platform_version if self.platform_manifest else 'unknown'} "
            f"--implementation-pr {implementation_pr} --finalization-pr <PR_NUMBER>`, commit the "
            "resulting `docs/engineering/runs/` handoff records to that same Finalization branch, "
            "push it, and only then return that PR number."
        )
        finalization_span = start_phase(self.root, state.run_id, "FINALIZATION")
        handoff_started = time.monotonic()
        set_handoff_deadline = getattr(self.agent, "set_handoff_deadline_callback", None)
        if callable(set_handoff_deadline):
            set_handoff_deadline(lambda: time.monotonic() - handoff_started >= FINALIZATION_PR_HANDOFF_MAX_SECONDS)
        try:
            result = self._invoke_agent_with_timing(
                finalization,
                assemble_prompt(
                    Path(finalization.prompt_path),
                    finalization,
                    managed_target=self.root if finalization.execution_mode == "MANAGED" else None,
                )
                + instruction,
            )
            finalization = self._record_agent_execution_time(finalization)
            finalization = self._record_validation_evidence(finalization, result)
            finalization = self._record_verified_result_commit(
                finalization,
                result,
                phase="FINALIZE_AGENT",
                description="finalization_commit_verified",
            )
            self._persist_agent_usage(finalization.run_id)
        except CodexHandoffTimeout:
            complete_phase(self.root, finalization_span, outcome="FAILED")
            finalization = self._record_agent_execution_time(finalization)
            # The timeout is not a terminal agent result.  Reconcile only a
            # PR that current evidence proves already exists; otherwise the
            # recovery helper blocks fail-closed without another invocation.
            return self._recover_finalization_pull_request(finalization, self.repository.inspect(self.root))
        except ProviderReadinessBlocked as blocked:
            complete_phase(self.root, finalization_span, outcome="BLOCKED")
            return blocked.state
        except CodexInvocationError as error:
            complete_phase(
                self.root, finalization_span,
                outcome="INTERRUPTED" if error.provider_turn_interrupted else "FAILED",
            )
            finalization = self._record_agent_execution_time(finalization)
            self.console_detail = error.console_detail
            return self._terminalize_provider_invocation_error(finalization, error)
        finally:
            if callable(set_handoff_deadline):
                set_handoff_deadline(None)
        complete_phase(self.root, finalization_span)
        return self._advance_after_finalization_agent_result(finalization, result)

    def _advance_after_finalization_agent_result(
        self, finalization: TransactionState, result: AgentResult,
    ) -> TransactionState:
        """Apply a live or durably recovered Finalization result.

        Timing spans belong exclusively to the live caller.  This method uses
        only checkpointed lifecycle identity and the validated AgentResult so
        a restarted host cannot fabricate Finalization timing evidence.
        """
        if result.terminal_state in {"BLOCKED", "FAILED"} or not result.pull_request:
            return self._save_terminal(
                finalization,
                result.terminal_state
                if result.terminal_state in {"BLOCKED", "FAILED"}
                else "BLOCKED",
                "finalization_pr_required",
                result.diagnostic or "Finalization pull request was not created.",
            )
        finalization = replace(
            finalization,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            branch=result.branch,
            pull_request=result.pull_request,
            finalization_branch=result.branch,
            finalization_pull_request=result.pull_request,
            terminal_condition="repository_reconciled",
            next_action="poll_required_checks",
        )
        self.store.save(finalization)
        write_live_status(self.root, finalization, finalization.next_action)
        # A resumed transaction can discover that its mandatory Finalization
        # PR was already merged before the agent returned it.  It is valid
        # evidence for this same transaction, so reconcile it through the
        # normal merge/cleanup path instead of trying to mark a closed PR
        # ready for review.
        finalization_evidence = self.github.pull_request(result.pull_request)
        if finalization_evidence.state == "MERGED":
            return self._poll(finalization, result)
        self.github.normalize_markdown_body(result.pull_request)
        self.github.ready(result.pull_request)
        return self._poll(finalization, result)

    def _start_automatic_reconciliation(self, state: TransactionState) -> TransactionState:
        """Apply the bounded rolling-record update after Finalization merges.

        This is deliberately a direct, post-merge `main` commit: it has no PR,
        review, approval, or operator merge boundary. Its scope is enforced by
        the supplied reconciliation prompt and the exact commit evidence below.
        """
        synchronize = getattr(self.repository, "synchronize_main", None)
        if callable(synchronize):
            synchronize(self.root)
        evidence = self.repository.inspect(self.root)
        if not evidence.clean or evidence.branch != "main":
            return self._save_post_merge_sync_wait(
                state,
                "End reconciliation is waiting for a clean, synchronized main checkout.",
            )
        reconciliation = replace(
            state, phase="RECONCILE_AGENT", transaction_kind="RECONCILIATION",
            branch=None, pull_request=None, reconciliation_pull_request=None,
            next_action="reconcile_rolling_records_on_main", waiting_for_merge_since=None,
            latest_repository_evidence=_repository_summary(evidence),
        )
        self._managed_action(reconciliation, "AUTOMATIC_RECONCILIATION")
        self.store.save(reconciliation)
        write_live_status(self.root, reconciliation, reconciliation.next_action)
        try:
            result = self._invoke_agent_with_timing(
                reconciliation,
                assemble_prompt(
                    Path(reconciliation.prompt_path), reconciliation,
                    managed_target=self.root if reconciliation.execution_mode == "MANAGED" else None,
                ) + "\n\nReconcile only the four canonical rolling current-state records after the verified Finalization merge. Preserve immutable Prompt History.",
            )
            reconciliation = self._record_agent_execution_time(reconciliation)
            reconciliation = self._record_validation_evidence(reconciliation, result)
            reconciliation = self._record_verified_result_commit(
                reconciliation,
                result,
                phase="RECONCILE_AGENT",
                description="end_reconciliation_commit_verified",
            )
            self._persist_agent_usage(reconciliation.run_id)
        except ProviderReadinessBlocked as blocked:
            return blocked.state
        except CodexInvocationError as error:
            reconciliation = self._record_agent_execution_time(reconciliation)
            self.console_detail = error.console_detail
            return self._terminalize_provider_invocation_error(reconciliation, error)
        return self._poll(reconciliation, result)

    def _save_terminal(
        self,
        state: TransactionState,
        phase: str,
        action: str,
        diagnostic: str | None = None,
        *,
        terminal_condition: str | None = None,
    ) -> TransactionState:
        terminal = replace(
            state,
            phase=phase,
            terminal=True,
            next_action=action,
            terminal_condition=terminal_condition or state.terminal_condition,
            diagnostic=redact_diagnostic(diagnostic) if diagnostic else None,
        )
        self.store.save(terminal)
        if self.active_lease is not None and self.active_lease.run_id == terminal.run_id:
            lease = self.active_lease
            try:
                if self.lease_heartbeat is not None:
                    lease = self.lease_heartbeat.stop()
                    self.lease_heartbeat = None
                release_lease(self.root, lease, central_database=self.store.central_database)
            except Exception:
                # A durable terminal checkpoint is authoritative even when
                # post-terminal lease cleanup is unavailable.  Stale lease
                # reconciliation records that separate cleanup concern.
                LOGGER.exception("Terminal lease release failed for run %s", terminal.run_id)
            finally:
                self.active_lease = None
        if phase == "COMPLETE":
            capture_engineering_memory(self.root, terminal, self.reviewer_records)
        write_live_status(self.root, terminal, action)
        print(f"[{terminal.phase}] {action}")
        return terminal

    def _terminalize_provider_invocation_error(
        self, state: TransactionState, error: CodexInvocationError
    ) -> TransactionState:
        """Turn provider-proven interruption into one durable terminal truth."""
        if error.provider_turn_interrupted:
            return self._save_terminal(
                state,
                "FAILED",
                "NONE",
                str(error),
                terminal_condition="provider_turn_interrupted",
            )
        return self._save_terminal(
            state,
            "BLOCKED",
            error.next_action,
            str(error),
            terminal_condition=error.terminal_condition,
        )

    def _save_operator_merge_wait(self, state: TransactionState) -> TransactionState:
        """Persist a PR hand-off and release the foreground lease.

        The wait is deliberately durable, but there is no running agent to
        own a liveness lease while the human reviews or merges the pull
        request. The watcher recognises this checkpoint as queue-owning.
        """
        self.store.save(state)
        if self.active_lease is not None and self.active_lease.run_id == state.run_id:
            if self.lease_heartbeat is not None:
                self.active_lease = self.lease_heartbeat.stop()
                self.lease_heartbeat = None
            release_lease(self.root, self.active_lease, central_database=self.store.central_database)
            self.active_lease = None
        write_live_status(self.root, state, state.next_action)
        return state

    def _save_post_merge_sync_wait(
        self, state: TransactionState, diagnostic: str
    ) -> TransactionState:
        """Keep post-merge closure resumable when the shared checkout is busy.

        A dirty or non-main checkout is never force-cleaned.  The verified
        merge remains durable evidence and the watcher retries the same
        transaction through its normal merge-poll path once the checkout is
        available again.
        """
        waiting = replace(
            state,
            phase="WAIT_FOR_OPERATOR_MERGE",
            terminal=False,
            next_action="await_clean_synchronized_main",
            terminal_condition="post_merge_workspace_sync_required",
            diagnostic=redact_diagnostic(diagnostic),
        )
        return self._save_operator_merge_wait(waiting)

    def _cleanup(self, state: TransactionState) -> TransactionState:
        print("[REPOSITORY_CLEANUP] Repository cleanup in progress")
        self._managed_action(state, "RECONCILIATION")
        self._managed_action(state, "CLEANUP")
        cleanup = start_phase(self.root, state.run_id, "REPOSITORY_CLEANUP")
        try:
            result = self.finalization.cleanup(
                root=self.root,
                store=self.store,
                repository=self.repository,
                state=state,
                save_terminal=self._save_terminal,
            )
        except Exception:
            complete_phase(self.root, cleanup, outcome="FAILED")
            raise
        complete_phase(self.root, cleanup, outcome="COMPLETE" if result.phase == "COMPLETE" else "FAILED")
        return result

    def _record_merged_evidence(
        self, state: TransactionState, pr: PullRequestEvidence, evidence: RepositoryEvidence
    ) -> TransactionState:
        common = {
            "last_verified_sha": evidence.head_sha,
            "latest_repository_evidence": _repository_summary(evidence),
            "latest_github_evidence": _pull_request_summary(pr),
        }
        if state.transaction_kind == "IMPLEMENTATION":
            recorded = replace(
                state,
                implementation_branch=state.branch,
                implementation_pull_request=pr.number,
                implementation_head_sha=state.last_verified_sha,
                implementation_merge_commit=pr.merge_commit,
                **common,
            )
            description = "implementation_merge_verified"
        elif state.transaction_kind == "RECONCILIATION":
            recorded = replace(state, reconciliation_pull_request=pr.number, **common)
            description = "reconciliation_merge_verified"
        else:
            recorded = replace(
                state,
                finalization_branch=state.branch or state.finalization_branch,
                finalization_pull_request=pr.number,
                finalization_head_sha=state.last_verified_sha,
                finalization_merge_commit=pr.merge_commit,
                **common,
            )
            description = "finalization_merge_verified"
        return self._append_verified_commit_evidence(
            recorded,
            phase="WAIT_FOR_OPERATOR_MERGE",
            commit_sha=pr.merge_commit or "",
            description=description,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engineering-execution-host",
        description="Run one bounded Engineering Platform execution transaction",
    )
    parser.add_argument("prompt", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--transaction-kind",
        choices=("IMPLEMENTATION", "FINALIZATION", "RECONCILIATION"),
        default="IMPLEMENTATION",
        help="internal watcher-selected transaction kind",
    )
    parser.add_argument(
        "--admitted-storage-schema",
        type=int,
        help="storage schema admitted by the watcher that spawned this run",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--owner-authorized",
        action="store_true",
        help="record the owner's bounded branch and pull-request authorization; merges remain operator-owned",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = argv if argv is not None else __import__("sys").argv[1:]
    root = Path.cwd().resolve()
    runtime_workspace(root)
    if raw_args == ["status"]:
        return print_live_status(root)
    if raw_args == ["qualify"]:
        report = execute_qualification(root)
        print(dashboard(report))
        return 0 if report["qualification"] == "PASS" else 1
    args = build_parser().parse_args(raw_args)
    prompt_path = args.prompt.resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt does not exist: {prompt_path}")
    if args.resume and not args.run_id:
        raise SystemExit("--resume requires --run-id")
    if args.admitted_storage_schema is not None and args.admitted_storage_schema < 1:
        raise SystemExit("--admitted-storage-schema must be positive")
    if args.admitted_storage_schema is not None:
        # Keep the watcher admission boundary with every child process the
        # runner starts. Source may change during execution, but the canonical
        # live database must remain readable by the admitting components.
        os.environ["DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA"] = str(args.admitted_storage_schema)
        os.environ["DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT"] = str(root)
    compatibility = (
        RunnerCompatibility(storage_schemas=frozenset({args.admitted_storage_schema}))
        if args.admitted_storage_schema is not None
        else RunnerCompatibility()
    )
    try:
        runtime = PlatformConfiguration.load(root).resolver(root).resolve_runtime()
    except PlatformConfigurationError:
        runtime = None
    runner = EngineeringRunner(
        root,
        StateStore(root / ".engineering" / "engineering-runs"),
        SubprocessRepositoryClient(),
        GhCliClient(),
        CodexCliClient(CodexCliProvider(str(runtime)) if runtime is not None else CodexCliProvider()),
        compatibility=compatibility,
    )
    logger = component_logger(root, "execution-host")
    lifecycle_context = {"application_version": "2.0.0", "target_component": "execution-host"}
    try:
        with shutdown_signal_logging(logger, lifecycle_context):
            state = runner.run(
                prompt_path,
                args.run_id,
                args.resume,
                args.owner_authorized,
                args.transaction_kind,
            )
    except (RunnerError, StateError) as error:
        print(f"BLOCKED: {error}")
        return 2
    report_phase = start_phase(root, state.run_id, "REPORT_GENERATION") if state.terminal else None
    try:
        report_path = (
            generate_terminal_report(
                root,
                state,
                runner.platform_manifest,
                runner.detected_codex_cli,
                runner.reviewer_records,
                getattr(runner.agent, "last_runtime_metadata", None),
                getattr(runner.agent, "last_execution_metadata", None),
            )
            if state.terminal
            else None
        )
    except Exception:
        if report_phase is not None:
            complete_phase(root, report_phase, outcome="FAILED")
        raise
    if report_phase is not None:
        complete_phase(root, report_phase)
    if report_path:
        evidence_phase = start_phase(root, state.run_id, "EVIDENCE_PERSISTENCE")
        try:
            record_terminal_report(root, report_path)
            analyze_terminal_report(root, state.run_id, report_path)
        except Exception:
            complete_phase(root, evidence_phase, outcome="FAILED")
            raise
        complete_phase(root, evidence_phase)
    if runner.platform_manifest:
        publish_canonical_status(
            root / ".engineering" / "status",
            build_canonical_status(
                runner.platform_manifest,
                current_phase=state.phase,
                current_action=state.next_action,
                run_id=state.run_id,
                repair_iteration=state.repair_iterations,
                implementation_pr=state.implementation_pull_request,
                finalization_pr=state.finalization_pull_request,
                repository_state="MERGED_RECONCILED" if state.phase == "COMPLETE" else "ACTIVE",
                workspace_state="WORKSPACE_READY" if state.phase == "COMPLETE" else "ACTIVE",
                owner_authorized=state.owner_authorized,
                resume_available=not state.terminal,
                latest_report=str(report_path) if report_path else None,
                diagnostic=state.diagnostic,
            ),
        )
    if report_path:
        print(
            f"Engineering report generated:\n\n{report_path}\n\nAvailable in the Engineering Status dashboard."
        )
    if state.phase in {"BLOCKED", "FAILED"}:
        print(_format_terminal_report(state))
        if runner.console_detail:
            log_path = write_redacted_codex_cli_log(root, state.run_id, runner.console_detail)
            print(f"\nCodex CLI log: {log_path}")
            print(f"\nCodex CLI details:\n{runner.console_detail}")
    elif state.phase == "COMPLETE" and state.owner_authorized and state.finalization_merge_commit:
        print(format_management_summary(state))
    else:
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    # A watcher owns the outer envelope until it archives and persists its
    # evidence.  Direct invocations own that final boundary themselves.
    if args.admitted_storage_schema is None and state.terminal:
        complete_active_phase(root, state.run_id, "TOTAL_EXECUTION", outcome="COMPLETE" if state.phase == "COMPLETE" else "FAILED")
    return 0 if state.phase == "COMPLETE" else 1


# Reporting compatibility exports are implemented in execution_reporting.py.
from .execution_reporting import (  # noqa: E402, F401
    _format_engineering_outcome,
    _format_reviewer_records,
    _format_terminal_report,
    _next_action_message,
    _pull_request_summary,
    _repository_summary,
    collect_terminal_evidence,
    corrected_terminal_report,
    format_management_summary,
    format_terminal_management_summary,
    generate_terminal_report,
    report_consistency_errors,
    terminal_report_matches_state,
)
