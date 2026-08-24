"""Thin foreground orchestrator for one bounded DJConnect engineering prompt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from threading import Lock
from typing import Callable, Mapping, Protocol
import uuid
import re
import sqlite3

from .agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from .capability_review import (
    ReviewerResult,
    ReviewerSelection,
    records_for_storage,
    reviewer_prompt,
    run_reviews,
    select_reviewers,
)
from .codex_observability import (
    codex_final_message as _codex_final_message,
    extract_codex_runtime_metadata,
    extract_codex_usage,
    write_codex_usage,
)
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
    detected_codex_cli_version,
    validate_compatibility,
)
from .qualification import dashboard, execute_qualification, latest_qualification
from .report_analysis import analyze as analyze_terminal_report
from .prompt_history import record_terminal_report
from .producer import ProducerMetadata, parse_producer_metadata
from .status_model import build as build_canonical_status, publish as publish_canonical_status
from .platform_api import PlatformConfiguration, PlatformConfigurationError, provider_registry
from .platform_bootstrap import migrate_legacy_workspace
from .providers import GitProvider, GitHubProvider, CodexCliProvider
from .host_preflight import latest as latest_host_preflight
from .workspace_preflight import latest as latest_workspace_preflight
from .capability_preflight import latest as latest_capability_preflight
from .drift_diagnostics import summary as drift_summary
from .execution_lease import Lease, LeaseConflictError, LeaseHeartbeat, acquire as acquire_lease, heartbeat as heartbeat_lease, history as lease_history, host_identity, host_instance_id, liveness as lease_liveness, reconcile_stale, release as release_lease
from .execution_readiness import ReadinessFacts, decide as decide_readiness, evaluate as evaluate_readiness, selected_profile
from .execution_transaction import ExecutionTransaction
from .execution_evidence import TerminalEvidenceBundle
from .execution_context import ExecutionContext
from .execution_context import (
    additional_workspace_write_roots as context_workspace_write_roots,
    execution_mode_for as context_execution_mode_for,
    genesis_target_for as context_genesis_target_for,
    genesis_workspace_preflight as context_genesis_workspace_preflight,
    resolve_execution_context as context_resolve_execution_context,
    target_repository_authorization as context_target_repository_authorization,
)
from .execution_models import AgentResult, PullRequestEvidence, RepositoryEvidence
from .reviewer_evidence import ReviewerEvidence
from .investigation_ledger import InvocationInvestigationLedger
from .execution_errors import CodexInvocationError, RunnerError
from .execution_repository import GitHubClient as ProviderGitHubClient, RepositoryClient as ProviderRepositoryClient
from .execution_repository import GhCliClient as ProviderGhCliClient, SubprocessRepositoryClient as ProviderRepositoryClientImpl
from .execution_executor import format_cli_failure as executor_format_cli_failure
from .execution_executor import (
    project_codex_activity as executor_project_codex_activity,
    project_codex_live_action_name as executor_project_codex_live_action_name,
)
from .execution_executor import redacted_cli_tail as executor_redacted_cli_tail
from .execution_executor import write_redacted_codex_cli_log as executor_write_redacted_codex_cli_log
from .execution_executor import CodexCliClient
from .execution_finalization import FinalizationCoordinator
from .execution_reporting import ReportingCoordinator
from .storage import EngineeringStorageError, load_readiness_evaluation, record_readiness_evaluation
from .storage import dismissal_for_run
from .provider_usage import AUTHORITATIVE, ProviderInvocation, normalize_codex_model, persist_provider_invocation
from .execution_timing import ActivePhase
from .execution_timing import complete_active_phase as _complete_active_phase
from .execution_timing import complete_phase as _complete_phase
from .execution_timing import start_or_resume_phase as _start_or_resume_phase
from .execution_timing import start_phase as _start_phase
from .managed_autonomy import (
    append_action as record_managed_action,
    append_validation_observation as record_managed_validation,
    record_gate as record_managed_gate,
)


LOGGER = logging.getLogger(__name__)

# A repair remains scoped to its original PR, but it must also have a finite
# attempt budget. This prevents a persistently failing required check from
# repeatedly invoking the provider without an operator decision.
MAX_PR_CHECK_REPAIR_ATTEMPTS = 3


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


def project_codex_activity(event: object) -> str | None:
    """Map a Codex JSONL event to bounded progress metadata.

    The dashboard receives only a fixed activity label. Raw reasoning, prompts,
    command text, tool arguments and tool output are intentionally ignored.
    """
    if not isinstance(event, dict) or event.get("type") not in {"item.started", "item.updated"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    labels = {
        "reasoning": "Codex plant de volgende stap",
        "command_execution": "Codex voert een opdracht uit",
        "file_change": "Codex bewerkt bestanden",
        "web_search": "Codex onderzoekt referentiemateriaal",
        "mcp_tool_call": "Codex gebruikt een ontwikkeltool",
        "agent_message": "Codex formuleert het resultaat",
    }
    return labels.get(item.get("type"))



def _redacted_cli_tail(value: str, prompt: str, *, limit: int = 1_200) -> str:
    """Keep the actionable end of CLI output without retaining the prompt echo."""
    without_prompt = value.replace(prompt, "[PROMPT_OMITTED]") if prompt else value
    tail = "\n".join(without_prompt.splitlines()[-60:])
    return redact_diagnostic(tail, limit=limit) or "(empty)"


def _format_cli_failure(exit_code: int, stderr: str, stdout: str, prompt: str = "") -> str:
    return "\n".join(
        (
            f"Codex CLI exit code: {exit_code}",
            f"stderr tail: {_redacted_cli_tail(stderr, prompt)}",
            f"stdout tail: {_redacted_cli_tail(stdout, prompt)}",
        )
    )


def write_redacted_codex_cli_log(root: Path, run_id: str, detail: str) -> Path:
    """Persist bounded, redacted CLI diagnostics for local troubleshooting."""
    directory = root / ".engineering" / "logs" / "codex"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{run_id}.log"
    content = "# Redacted Codex CLI diagnostic\n\n" + redact_diagnostic(detail, limit=3_000) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


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
) -> str:
    objective = prompt_path.read_text(encoding="utf-8")
    resume = (
        "No prior transaction checkpoint exists."
        if state is None
        else json.dumps(state.to_dict(), sort_keys=True)
    )
    authority = (
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
    return f"""You are executing one bounded DJConnect engineering transaction.
Read BOOTSTRAP.md, ENGINEERING_METHOD.md, PROMPT_INITIALIZATION.md and AGENTS.md from the actual repository before acting. Repository and GitHub evidence override this checkpoint: {resume}
{authority}{genesis}{managed_synchronization}{managed_admission}{shared_evidence}{invocation_read_reuse}{primary_tool_loop}Continue waiting for objective terminal repository evidence; pending CI and temporary failures are not completion.
Supplied bounded objective follows:\n\n{objective}\n{managed_boundary}\n\nReturn only one JSON object with terminal_state (COMPLETE, WAITING, BLOCKED, or FAILED), branch, pull_request, terminal_condition (repository_reconciled, open_pr_checks_terminal, external_blocked, or local_commit_reconciled), diagnostic, repository_path, commit_sha and validation_evidence. validation_evidence is a bounded list of executed validation {{command, result}} summaries; use [] when none ran. Never include secrets, tokens, headers, environment values, prompts, repository file contents, stack traces, or raw command output. Use null for other fields that do not apply. The diagnostic must be a short human-readable reason without secrets, tokens, headers, environment values, prompt content, repository file content, stack traces, or raw command output."""


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
        self._total_phase: ActivePhase | None = None

    def _heartbeat(self) -> None:
        if self.lease_heartbeat is not None and self.lease_heartbeat.error is not None:
            raise RunnerError("active-run lease heartbeat was lost") from self.lease_heartbeat.error
        if self.active_lease is not None:
            self.active_lease = heartbeat_lease(self.root, self.active_lease)
            if self.lease_heartbeat is not None:
                self.lease_heartbeat.lease = self.active_lease

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
                break
            write_live_status(self.root, state, "Capability review: " + selection.reviewer, self.reviewer_runtime)

    def _persist_agent_usage(self, run_id: str) -> None:
        usage = getattr(self.agent, "last_usage", None)
        if isinstance(usage, dict):
            write_codex_usage(self.root, run_id, usage)

    def _persist_provider_invocation(self, state: TransactionState, *, phase: str, role: str = "agent", started_at: str | None = None, observed_usage: dict[str, object] | None = None, observed_metadata: dict[str, object] | None = None, observed_churn: dict[str, object] | None = None, observed_duration: float | None = None, observed_snapshots: tuple[dict[str, int], ...] | None = None) -> None:
        """Append safe per-invocation evidence without affecting execution outcome."""
        usage = observed_usage if observed_usage is not None else getattr(self.agent, "last_usage", None)
        snapshots = observed_snapshots if observed_snapshots is not None else getattr(self.agent, "last_usage_snapshots", ())
        if not isinstance(usage, dict):
            usage = {}
        metadata = observed_metadata if observed_metadata is not None else getattr(self.agent, "last_runtime_metadata", None)
        churn = observed_churn if observed_churn is not None else getattr(self.agent, "last_churn", None)
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
            persist_provider_invocation(self.root, ProviderInvocation(
                run_id=state.run_id, ordinal=ordinal, provider="codex_cli",
                model=normalized_model,
                model_authority=AUTHORITATIVE if isinstance(raw_model, str) else "UNAVAILABLE",
                raw_provider_model=raw_model if isinstance(raw_model, str) else None,
                phase=phase, role=role, started_at=started_at or now, completed_at=now,
                duration_ms=round(duration * 1000) if isinstance(duration, (int, float)) and duration >= 0 else None,
                usage=usage, runtime_metadata=metadata if isinstance(metadata, dict) else None,
                retry_ordinal=state.repair_iterations, churn=churn if isinstance(churn, dict) else None,
                usage_snapshots=snapshots if isinstance(snapshots, tuple) else (),
            ))
        except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
            LOGGER.warning("Provider invocation telemetry is unavailable for run %s", state.run_id)

    def _record_agent_execution_time(self, state: TransactionState) -> TransactionState:
        """Accumulate only measured Codex CLI invocation time for this run."""
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
        for item in result.validation_evidence:
            command, summary = item.get("command", ""), item.get("result", "")
            kind = self._validation_kind(command)
            if kind is None:
                continue
            normalized = summary.casefold()
            status = "FAIL" if any(token in normalized for token in ("fail", "error", "blocked")) else "PASS"
            try:
                record_managed_validation(
                    self.root, run_id=state.run_id, control=f"validation_{kind}", state=status,
                    required=True, currentness=state.repair_iterations,
                )
            except EngineeringStorageError:
                LOGGER.warning("Managed validation evidence is unavailable for run %s", state.run_id)
        return replace(state, validation_evidence=result.validation_evidence)

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

    def _record_repair_audit(self, state: TransactionState, *, failed_checks: str, objective: str, result: AgentResult | None, outcome: str) -> TransactionState:
        """Append bounded per-repair evidence; never overwrite prior attempts."""
        record = {
            "iteration": str(state.repair_iterations), "observed_at": datetime.now(timezone.utc).isoformat(),
            "failed_checks": redact_diagnostic(failed_checks), "proposed_action": redact_diagnostic(objective),
            "agent_summary": redact_diagnostic((result.diagnostic if result else None) or "Agent invocation did not return a repair summary."),
            "commit_sha": result.commit_sha if result and result.commit_sha else "not_recorded", "outcome": outcome,
        }
        return replace(state, repair_audit=state.repair_audit + (record,))

    @staticmethod
    def _validation_kind(command: str) -> str | None:
        """Classify only known validation commands at their live boundary.

        The command itself is transient observability input: timing metadata
        retains the category, never command text or output.
        """
        normalized = command.casefold()
        if any(tool in normalized for tool in ("ruff", "flake8", "mypy", "pyright")):
            return "static_analysis"
        if any(tool in normalized for tool in ("bandit", "semgrep", "codeql", "pip-audit", "safety")):
            return "security"
        if "git diff --check" in normalized or "prettier" in normalized or "black --check" in normalized:
            return "format_or_diff"
        if any(tool in normalized for tool in ("playwright", "selenium", "cypress", "e2e")):
            return "browser_e2e"
        if any(tool in normalized for tool in ("pytest", "unittest", "tox", "nox")):
            return "tests"
        return None

    def _invoke_agent_with_timing(self, state: TransactionState, prompt: str, *, repair: bool = False) -> AgentResult:
        """Measure only the bounded runtime-provider process interval."""
        parent = start_phase(self.root, state.run_id, "REPAIR", attempt=state.repair_iterations, metadata={"iteration": state.repair_iterations}) if repair else None
        provider = start_phase(
            self.root, state.run_id, "PROVIDER_EXECUTION", parent_phase_id=parent.phase_id if parent else None,
            attempt=max(1, state.repair_iterations + 1), metadata={"provider": "codex_cli"},
        )
        validation_spans: dict[str, ActivePhase | None] = {}

        def command_boundary(event: str, command_id: str, command: str) -> None:
            if event == "started":
                kind = self._validation_kind(command)
                if kind is not None:
                    validation_spans[command_id] = start_phase(
                        self.root,
                        state.run_id,
                        "VALIDATION",
                        parent_phase_id=provider.phase_id if provider else None,
                        attempt=max(1, state.repair_iterations + 1),
                        metadata={"validation_kind": kind},
                    )
            elif event == "completed":
                active = validation_spans.pop(command_id, None)
                complete_phase(self.root, active)

        command_callback = getattr(self.agent, "set_command_callback", None)
        if callable(command_callback):
            command_callback(command_boundary)
        invocation_started = datetime.now(timezone.utc).isoformat()
        try:
            result = self.agent.invoke(self.root, prompt)
        except Exception:
            self._persist_provider_invocation(state, phase="REPAIR" if repair else "PROVIDER_EXECUTION", started_at=invocation_started)
            for active in validation_spans.values():
                complete_phase(self.root, active, outcome="INTERRUPTED")
            complete_phase(self.root, provider, outcome="FAILED")
            if parent:
                complete_phase(self.root, parent, outcome="FAILED")
            raise
        finally:
            if callable(command_callback):
                command_callback(None)
        self._persist_provider_invocation(state, phase="REPAIR" if repair else "PROVIDER_EXECUTION", started_at=invocation_started)
        complete_phase(self.root, provider)
        if parent:
            complete_phase(self.root, parent)
        return result

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

    def run(
        self,
        prompt_path: Path,
        run_id: str | None = None,
        resume: bool = False,
        owner_authorized: bool = False,
        transaction_kind: str = "IMPLEMENTATION",
    ) -> TransactionState:
        objective = prompt_path.read_text(encoding="utf-8")
        state = self.store.load(run_id) if resume else None
        if resume and state is not None and dismissal_for_run(self.root, state.run_id):
            raise RunnerError("This execution has already been dismissed and cannot be resumed.")
        if (
            state is not None
            and state.phase == "WAIT_FOR_OPERATOR_MERGE"
            and state.pull_request is not None
        ):
            # A green pull request is deliberately operator-owned.  Resuming
            # that wait must therefore only re-read the remote pull-request
            # state.  Re-running workspace admission, repository
            # synchronization, reviewer selection and memory retrieval here
            # creates expensive local churn while there is no new work to do.
            # Once a merge is observed, _poll performs the required
            # repository reconciliation before cleanup or Finalization.
            if Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current prompt")
            if not self.agent.available():
                raise RunnerError("Codex CLI is not installed or invokable")
            self._verify_engineering_platform()
            return self._poll(state)
        try:
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
            )
        context = replace(context, run_id=state.run_id)
        self.transaction = ExecutionTransaction(
            state=state,
            target_repository=context.target_repository or self.root,
        )
        # The Inbox watcher admits a run with the schema it has loaded.  A
        # freshly spawned runner reads source files again, so it can otherwise
        # observe a newer manifest and migrate the database while the watcher
        # still runs the older code.  Verify that compatibility boundary before
        # StateStore.save() opens (and could migrate) the datastore.
        if not self.agent.available():
            raise RunnerError("Codex CLI is not installed or invokable")
        self._verify_engineering_platform()
        # Establish canonical transaction identity before persisting readiness evidence.
        self.store.save(state)
        if context.execution_mode == "MANAGED":
            self._managed_action(state, "IMPLEMENTATION")
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
            reconcile_stale(self.root)
        except Exception:
            complete_phase(self.root, reconciliation, outcome="FAILED")
            raise
        complete_phase(self.root, reconciliation)
        self.store.save(state)
        try:
            self.active_lease = acquire_lease(self.root, state.run_id, identity=self.host_identity, instance_id=self.host_instance_id, process_id=os.getpid())
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
        self.lease_heartbeat = LeaseHeartbeat(self.root, self.active_lease)
        self.transaction = self.transaction.with_lease(self.active_lease)
        self.lease_heartbeat.start()
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
        preparation = start_phase(self.root, state.run_id, "EXECUTION_PREPARATION")
        reviewer_evidence = (
            ReviewerEvidence.from_repository(state.run_id, state.execution_mode, evidence)
            if state.execution_mode == "MANAGED"
            else None
        )
        memory = retrieve_engineering_memory(self.root, prompt_path)
        selections = select_reviewers(
            objective,
            prompt_path,
            state.transaction_kind if state else "IMPLEMENTATION",
            load_engineering_memory(self.root),
        )
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
        complete_phase(self.root, preparation)
        if state.terminal or state.phase == "WAIT_FOR_TERMINAL_EVIDENCE":
            return self._poll(state)
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
                    lambda process: write_runner_process(self.root, state.run_id, process)
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
            self._persist_agent_usage(state.run_id)
        except CodexInvocationError as error:
            state = self._record_agent_execution_time(state)
            self.console_detail = error.console_detail
            return self._save_terminal(
                state,
                "BLOCKED",
                error.next_action,
                str(error),
                terminal_condition=error.terminal_condition,
            )
        if state.execution_mode == "GENESIS":
            return self._reconcile_genesis_result(state, result)
        state = replace(
            state,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            branch=result.branch or evidence.branch,
            pull_request=result.pull_request,
            next_action="poll_required_checks",
            terminal_condition=result.terminal_condition,
            finalization_branch=(result.branch or evidence.branch)
            if state.transaction_kind == "FINALIZATION" else state.finalization_branch,
            finalization_pull_request=result.pull_request
            if state.transaction_kind == "FINALIZATION" else state.finalization_pull_request,
        )
        self.store.save(state)
        write_live_status(self.root, state, state.next_action)
        if state.owner_authorized and state.pull_request:
            historical = self._reject_historical_agent_pull_request(state)
            if historical is not None:
                return historical
            self.github.ready(state.pull_request)
        return self._poll(state, result)

    def _active_genesis_transaction(self, target: Path, run_id: str) -> str | None:
        """Return another active Genesis run that owns the same local workspace."""
        for checkpoint in self.store.directory.glob("*.json"):
            try:
                candidate = self.store.load(checkpoint.stem)
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
        return self._save_terminal(reconciled, "COMPLETE", "genesis_local_commit_reconciled")

    def _verify_engineering_platform(self) -> None:
        try:
            self.detected_codex_cli = self.agent.version()
            self.platform_manifest = EngineeringPlatformManifest.load(
                self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
            )
            validate_compatibility(
                self.platform_manifest, self.compatibility, self.detected_codex_cli
            )
            configuration_path = self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json"
            if configuration_path.is_file():
                configuration = PlatformConfiguration.load(self.root)
                if configuration.platform.version != self.platform_manifest.platform_version:
                    raise EngineeringPlatformCompatibilityError("Platform identity and manifest version mismatch")
                providers = provider_registry(self.root)
                if any(not item["status"].qualified for item in providers.values()):
                    raise EngineeringPlatformCompatibilityError("Configured Engineering Platform provider is unavailable")
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

    def _poll(self, state: TransactionState, result: AgentResult | None = None) -> TransactionState:
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
                    return replace(
                        state,
                        phase="WAIT_FOR_TERMINAL_EVIDENCE",
                        next_action="retry_github_evidence",
                    )
                wait = start_phase(self.root, state.run_id, "EXTERNAL_CI_WAIT", metadata={"reason": "github_evidence_retry"})
                self.sleep(min(30, 2**attempts))
                complete_phase(self.root, wait)
                continue
            complete_phase(self.root, pr_operation)
            if not pr.checks_terminal:
                self._managed_action(state, "GITHUB_REQUIRED_CHECK", "EXTERNAL_PLATFORM_EVENT", actor="github", evidence_ref="required_check_waiting")
                wait = start_phase(self.root, state.run_id, "EXTERNAL_CI_WAIT", metadata={"reason": "github_checks"})
                self.sleep(15)
                complete_phase(self.root, wait)
                continue
            if not pr.checks_passed:
                if state.owner_authorized:
                    failed = ", ".join(pr.failed_checks) or "required CI check"
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
                    gate_type = "IMPLEMENTATION_MERGE_APPROVAL" if state.transaction_kind == "IMPLEMENTATION" else "FINALIZATION_MERGE_APPROVAL"
                    self._managed_gate(state, gate_type, "SATISFIED", pr.number, resolved=True)
                    self._managed_action(state, "IMPLEMENTATION_MERGE" if state.transaction_kind == "IMPLEMENTATION" else "FINALIZATION_MERGE", "EXPECTED_OPERATOR_GATE", actor="operator", evidence_ref="github_merge")
                    state = self._record_merged_evidence(state, pr, evidence)
                    if state.owner_authorized and state.transaction_kind == "IMPLEMENTATION":
                        return self._start_finalization(state, pr.number)
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
            gate_type = "IMPLEMENTATION_MERGE_APPROVAL" if state.transaction_kind == "IMPLEMENTATION" else "FINALIZATION_MERGE_APPROVAL"
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
            repair = self._record_repair_audit(
                repair, failed_checks=failed_checks, objective=objective, result=result,
                outcome="agent_failed" if result.terminal_state in {"BLOCKED", "FAILED"} else "submitted_for_recheck",
            )
            self.store.save(repair)
            self._persist_agent_usage(repair.run_id)
        except CodexInvocationError as error:
            repair = self._record_agent_execution_time(repair)
            self.console_detail = error.console_detail
            repair = self._record_repair_audit(repair, failed_checks=failed_checks, objective=objective, result=None, outcome="agent_failed")
            return self._save_terminal(
                repair,
                "BLOCKED",
                error.next_action,
                str(error),
                terminal_condition=error.terminal_condition,
            )
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(
                repair, result.terminal_state, "external_action_required", result.diagnostic
            )
        if result.pull_request != repair.pull_request:
            return self._save_terminal(
                repair,
                "BLOCKED",
                "bounded_scope_conflict",
                "Repair did not preserve the bounded pull request.",
            )
        return self._poll(
            replace(repair, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="poll_required_checks"),
            result,
        )

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
            return self._save_terminal(
                state,
                "BLOCKED",
                "synchronize_main",
                "Finalization requires a clean, synchronized main checkout.",
            )
        finalization = replace(
            state,
            phase="FINALIZE_AGENT",
            transaction_kind="FINALIZATION",
            pull_request=None,
            branch=None,
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
            "History, then create a draft Finalization PR. After GitHub assigns its number, run "
            f"`python3 -m tools.engineering.repository_handoff --run-id {finalization.run_id} "
            f"--platform-version {self.platform_manifest.platform_version if self.platform_manifest else 'unknown'} "
            f"--implementation-pr {implementation_pr} --finalization-pr <PR_NUMBER>`, commit the "
            "resulting `docs/engineering/runs/` handoff records to that same Finalization branch, "
            "push it, and only then return that PR number."
        )
        finalization_span = start_phase(self.root, state.run_id, "FINALIZATION")
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
            self._persist_agent_usage(finalization.run_id)
        except CodexInvocationError as error:
            complete_phase(self.root, finalization_span, outcome="FAILED")
            finalization = self._record_agent_execution_time(finalization)
            self.console_detail = error.console_detail
            return self._save_terminal(
                finalization,
                "BLOCKED",
                error.next_action,
                str(error),
                terminal_condition=error.terminal_condition,
            )
        complete_phase(self.root, finalization_span)
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
        self.github.ready(result.pull_request)
        return self._poll(finalization, result)

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
            if self.lease_heartbeat is not None:
                self.active_lease = self.lease_heartbeat.stop()
                self.lease_heartbeat = None
            release_lease(self.root, self.active_lease)
            self.active_lease = None
        if phase == "COMPLETE":
            capture_engineering_memory(self.root, terminal, self.reviewer_records)
        write_live_status(self.root, terminal, action)
        print(f"[{terminal.phase}] {action}")
        return terminal

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
            release_lease(self.root, self.active_lease)
            self.active_lease = None
        write_live_status(self.root, state, state.next_action)
        return state

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
            return replace(
                state,
                implementation_branch=state.branch,
                implementation_pull_request=pr.number,
                implementation_head_sha=state.last_verified_sha,
                implementation_merge_commit=pr.merge_commit,
                **common,
            )
        return replace(
            state,
            finalization_branch=state.branch or state.finalization_branch,
            finalization_pull_request=pr.number,
            finalization_head_sha=state.last_verified_sha,
            finalization_merge_commit=pr.merge_commit,
            **common,
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
        choices=("IMPLEMENTATION", "FINALIZATION"),
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
    migrate_legacy_workspace(root)
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
    try:
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
from .execution_reporting import (
    _format_engineering_outcome, _format_reviewer_records, _format_terminal_report,
    _next_action_message, _pull_request_summary, _repository_summary,
    collect_terminal_evidence, corrected_terminal_report, format_management_summary,
    format_terminal_management_summary, generate_terminal_report, report_consistency_errors,
    terminal_report_matches_state,
)
