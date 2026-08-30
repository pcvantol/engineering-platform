"""Private Engineering Status dashboard with distinct queue recovery and execution retry actions."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
from datetime import datetime, timezone
from html import escape
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import select
import shlex
import shutil
import sqlite3
import socket
import subprocess  # noqa: F401 - Compatibility mock target; process execution is provider-owned.
import sys
import tempfile
from threading import Lock, Timer
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from .platform_api import PlatformConfiguration
from .platform_bootstrap import provision_runtime_workspace as provision_workspace
from .providers import CodexCliProvider, GitHubProvider, GitProvider, LaunchdProvider, LocalProcessProvider, TailscaleProvider, codex_cli_executable, engineering_platform_codex_cli_prefix
from .provider_readiness import status as provider_readiness_status
from .inbox_watcher import LABEL as WATCHER_LABEL
from .inbox_watcher import WATCHER_READY_PROJECTION, WATCHER_VERSION
from .inbox_watcher import RetrySubmissionError, abort_operator_merge_wait, check_operator_merge_status, cloud_root, defer_queued_prompt, dismiss_execution, predecessor_retry_admission_preflight, queued_retry_children, retry_admission_preflight, status_reconciliation_preview, submit_execution_retry, submit_predecessor_retry, submit_status_reconciliation
from . import inbox_watcher
from .component_logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENVIRONMENT,
    VALID_LEVELS,
    clear_component_log as clear_stored_component_log,
    component_log as stored_component_log,
    component_log_page as stored_component_log_page,
    component_log_version,
    component_lifecycle_context,
    component_logger,
    log_event,
    prune_component_logs,
    shutdown_signal_logging,
)
from .component_lock import DuplicateComponentInstanceError, single_instance
from .agent_state import is_valid_commit_evidence_record, redact_diagnostic
from .pr_check_repair import PullRequestCheckRepairError, admit as admit_pr_check_repair, attempted as pr_check_repair_attempted, check_summary as pr_check_repair_check_summary, mark_dispatch_failed as mark_pr_check_repair_dispatch_failed, repair_state as pr_check_repair_state
from .codex_chat import (
    CodexChatError,
    chat_model,
    clear_history as clear_codex_chat_history,
    history as codex_chat_history,
    respond as codex_chat_response,
)
from .codex_capacity import read_remaining_percent
from .telemetry import clear_telemetry, daily_statistics, daily_timing_detail, execution_timing, prune_telemetry
from .prompt_history import prompt_history, report_for_prompt_history, report_path_for_prompt_history
from .report_analysis import analyze as analyze_terminal_report
from .recommendation_handoff import handoff_from_report
from .storage import (
    EngineeringStorageError,
    ai_capacity_history,
    load_projection,
    open_storage,
    record_ai_capacity_bi_hourly,
    storage_activation_required,
)
from .provider_usage import provider_usage_summary
from .execution_activity import terminal_activity_summary
from .execution_lifecycle import projection as lifecycle_projection
from .emergency_recovery import EmergencyRecoveryError, execute as execute_emergency_recovery, preview as emergency_recovery_preview
from .platform_version import EngineeringPlatformManifest
from .dashboard_configuration import (
    DashboardConfigurationConflict,
    get as dashboard_configuration,
    inbox_root,
    restore_inbox_root,
    update as update_dashboard_configuration,
    update_inbox_root,
)
from . import dashboard_state
from .workspace_preflight import execute as execute_workspace_preflight

LABEL = "com.djconnect.engineering-dashboard"
RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"
DASHBOARD_VERSION = "2.0.0"
DASHBOARD_STARTED_AT = time.monotonic()
DASHBOARD_SNAPSHOT_SOURCE = str(uuid.uuid4())
ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_DARK = "operations-console/apple-touch-icon-dark.png"
APP_ICON_LIGHT = "operations-console/apple-touch-icon-light.png"
WEB_MANIFEST = "operations-console/manifest.webmanifest"
LOOPBACK_ADDRESS = "127.0.0.1"
CODEX_PROCESS = re.compile(r"(?:^|\s)(?:\S*/)?codex(?:\s|$)")
RATE_LIMIT_CACHE_SECONDS = 60
RETRYABLE_REPORT_ANALYSIS_STATUSES = frozenset({
    "provider_failed", "provider_unavailable", "invalid_structured_response",
})
_REPORT_ANALYSIS_RETRY_LOCK = Lock()
_REPORT_ANALYSIS_RETRY_RUNS: set[str] = set()


class CodexCapacityReserveConflict(ValueError):
    """A new reserve exceeds fresh capacity or cannot be safely verified."""

    def __init__(self, code: str, *, remaining_percent: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.remaining_percent = remaining_percent


def _validate_codex_capacity_reserve_update(root: Path, key: object, value: object) -> None:
    """Fail closed before a reserve increase can make future admission impossible."""
    if key != "codex_capacity_reserve_percent" or not isinstance(value, int) or isinstance(value, bool):
        return
    previous = int(dashboard_configuration(root)["codex_capacity_reserve_percent"])
    if value <= previous:
        return
    remaining = read_remaining_percent()
    if remaining is None:
        raise CodexCapacityReserveConflict("codex_capacity_reserve_unavailable")
    if value > remaining:
        raise CodexCapacityReserveConflict(
            "codex_capacity_reserve_exceeds_remaining", remaining_percent=remaining,
        )
_rate_limit_cache_lock = Lock()
_rate_limit_cache: tuple[float, bytes] | None = None
CODEX_IDENTITY_CACHE_SECONDS = 300
CODEX_UPDATE_CACHE_SECONDS = 900
CODEX_CLI_PACKAGE = "@openai/codex"
GIT_INDEX_LOCK_STALE_SECONDS = 300
_codex_identity_cache_lock = Lock()
_codex_identity_cache: tuple[float, dict[str, str]] | None = None
_codex_update_cache_lock = Lock()
_codex_update_cache: tuple[float, dict[str, object]] | None = None
_codex_update_install_lock = Lock()
_provider_install_lock = Lock()
_provider_login_lock = Lock()
_provider_login_active: str | None = None
_provider_login_started_at = 0.0
_PROVIDER_LOGIN_TIMEOUT_SECONDS = 300.0
_snapshot_revision_lock = Lock()
_snapshot_fingerprint: bytes | None = None
_snapshot_revision = 0

COMPONENT_LABELS = {
    "dashboard": LABEL,
    "inbox_watcher": WATCHER_LABEL,
    "dashboard_relay": RELAY_LABEL,
}
RESTARTABLE_COMPONENTS = frozenset(COMPONENT_LABELS)
AUDITABLE_USER_ACTIONS = frozenset(
    {
        "chat_downloaded",
        "component_log_downloaded",
        "telemetry_cleared",
        "telemetry_copied",
        "telemetry_downloaded",
        "telemetry_detail_json_downloaded",
        "telemetry_detail_markdown_downloaded",
        "prompt_history_report_copied",
        "prompt_history_report_downloaded",
        "prompt_history_analysis_copied",
        "prompt_history_analysis_downloaded",
        "prompt_history_details_json_downloaded",
        "prompt_history_details_markdown_downloaded",
        "engineering_database_downloaded",
        "report_copied",
        "report_analysis_copied",
    }
)


class DashboardHTTPServer(ThreadingHTTPServer):
    """Private dashboard listener with safe restart behavior."""

    allow_reuse_address = True


def _unavailable_status() -> bytes:
    """Compatibility façade for the dashboard state module."""
    return dashboard_state.unavailable_status()


def _canonical_checkpoint(root: Path, run_id: str | None) -> dict[str, object]:
    """Read one lifecycle checkpoint from SQLite, never from its JSON copy."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return {}
    try:
        connection = open_storage(root)
        try:
            row = connection.execute(
                "SELECT payload FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        payload = json.loads(row[0]) if row else {}
    except (EngineeringStorageError, TypeError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict) and payload:
        return payload
    # Compatibility-only reader for an already-terminal legacy checkpoint.
    try:
        legacy = json.loads(
            (root / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return legacy if isinstance(legacy, dict) else {}


def _status(root: Path) -> bytes:
    """Compatibility façade for the stable status projection."""
    return dashboard_state.status(root)


def _sse_status(root: Path) -> bytes:
    """Encode the status as a single SSE data line."""
    return dashboard_state.sse_status(root)


def _sse_snapshot(root: Path) -> bytes:
    """Return the complete read-only dashboard projection for one SSE update.

    The browser receives this snapshot when it connects and only when one of
    its observable values changes.  This keeps the dashboard event-driven
    without giving the dashboard any transaction authority.
    """
    snapshot = dashboard_state.snapshot(
        root,
        status_reader=_sse_status,
        unavailable_reader=_unavailable_status,
        prompt_started_reader=_prompt_started,
        usage_reader=_codex_usage,
        rate_limits_reader=_codex_rate_limits,
        usage_for_run_reader=_codex_usage_for_run,
        completion_commits_reader=_completion_commits,
        last_executed_commits_reader=_last_executed_commits,
        reviewer_agents_reader=_reviewer_agents_for_run,
        execution_reader=_last_executed_agent_execution,
        runtime_metadata_reader=_last_executed_runtime_metadata,
        report_analysis_available_reader=_report_analysis_available_for_run,
        telemetry_reader=lambda workspace: daily_statistics(
            workspace, days=int(dashboard_configuration(workspace)["telemetry_retention_days"])
        ),
        process_metrics_reader=_codex_process_metrics,
        build_commit_reader=_build_commit,
        component_log_versions_reader=_component_log_versions,
        dashboard_version=DASHBOARD_VERSION,
        worker_version=WATCHER_VERSION,
    )
    try:
        payload = json.loads(snapshot)
    except json.JSONDecodeError:
        return snapshot
    if not isinstance(payload, dict):
        return snapshot
    payload["workspace_git_lock"] = _workspace_git_lock(root)
    status = payload.get("status")
    payload["emergency_recovery"] = emergency_recovery_preview(
        root, status.get("run_id") if isinstance(status, dict) else None
    )
    # Git state is deliberately projected with the SSE payload instead of
    # being fixed in the initial HTML response. A managed execution can switch
    # to its transaction branch after the operator opens the dashboard.
    payload["workspace_git"] = _workspace_git_projection(root)
    payload["workspace_worktrees"] = _workspace_worktrees(root)
    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict):
        provider = rate_limits.get("provider")
        remaining = _remaining_rate_limit_capacity(rate_limits)
        if isinstance(provider, str) and remaining is not None:
            try:
                record_ai_capacity_bi_hourly(root, provider=provider, remaining_percent=remaining)
                payload["ai_capacity_history"] = ai_capacity_history(root, provider=provider)
            except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
                # The live quota status remains useful if local history is
                # temporarily unavailable; a chart must never block it.
                payload["ai_capacity_history"] = []
    fingerprint = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # HTTP refreshes and SSE delivery can complete out of order in a browser.
    # Attach one process-scoped monotone revision to every changed projection,
    # so the client can retain the newest coherent lifecycle snapshot.
    global _snapshot_fingerprint, _snapshot_revision
    with _snapshot_revision_lock:
        if fingerprint != _snapshot_fingerprint:
            _snapshot_fingerprint = fingerprint
            _snapshot_revision += 1
        payload["snapshot_source"] = DASHBOARD_SNAPSHOT_SOURCE
        payload["snapshot_revision"] = _snapshot_revision
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _prompt_history(root: Path) -> bytes:
    """Return the bounded, private SQLite prompt history projection."""
    try:
        try:
            queued_children = queued_retry_children(cloud_root(repo=root))
        except Exception:
            # Terminal history remains authoritative when a local test or a
            # temporarily unavailable transport cannot expose queued evidence.
            queued_children = []
        runs = prompt_history(root, queued_retry_children=queued_children)
        for run in runs:
            run["analysis_available"] = _report_analysis_available_for_run(
                root, run.get("run_id")
            )
        payload = {"runs": runs}
    except Exception:
        payload = {"runs": []}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _project_history_evidence(history: dict[str, object], report: str) -> list[str]:
    """Project bounded report evidence without mixing it into storage access."""
    target_repository = re.search(
        r"^- Target Repository: `([^`\n]+)`$", report, re.MULTILINE
    )
    if target_repository:
        history["target_repository"] = target_repository.group(1)
    evidence: list[str] = []
    for report_label, display_label in (
        ("Execution Host", "Execution Host"),
        ("Target Repository", "Target repository"),
        ("Target Commit", "Target commit"),
        ("Producer ID", "Producer"),
        ("Producer Type", "Producer type"),
        ("Mission ID", "Mission"),
        ("Engineering Action ID", "Engineering action"),
        ("Correlation ID", "Correlation"),
    ):
        match = re.search(
            rf"^- {re.escape(report_label)}: `([^`\n]+)`$", report, re.MULTILINE
        )
        if match:
            evidence.append(f"{display_label}: {match.group(1)}")
    changed = len(re.findall(r"^- Changed file: `", report, re.MULTILINE))
    if changed:
        evidence.append(f"Evidence Bundle: {changed} gewijzigde bestanden")
    return evidence


def _project_prompt_history_detail(
    entry: dict[str, object],
    *,
    execution: object,
    runtime: object,
    reviewers: object,
    commits: object,
    usage: dict[str, object],
    report: str | None,
    lifecycle: dict[str, object] | None = None,
    pull_requests: object = (),
    commit_timeline: object = (),
) -> bytes:
    """Project one immutable history row into dashboard detail JSON.

    Storage retrieval belongs to the route projection; this function owns the
    presentation shape and the bounded evidence derived from its report.
    """
    history = dict(entry)
    evidence = _project_history_evidence(history, report) if report is not None else []
    handoff = handoff_from_report(report) if report is not None else None
    return json.dumps(
        {
            "history": history,
            "execution": execution,
            "runtime": runtime,
            "reviewers": reviewers,
            "commits": commits,
            "commit_timeline": commit_timeline,
            "pull_requests": pull_requests,
            "usage": usage,
            "evidence": evidence,
            "recommendation_handoff": handoff,
            "lifecycle": lifecycle or {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _pull_requests_for_run(root: Path, run_id: str | None) -> list[dict[str, object]]:
    """Project only checkpoint-owned Managed pull-request evidence as links."""
    checkpoint = _canonical_checkpoint(root, run_id)
    repository = checkpoint.get("repository")
    if (
        checkpoint.get("execution_mode") != "MANAGED"
        or not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        return []
    links: list[dict[str, object]] = []
    for role, field in (
        ("implementation", "implementation_pull_request"),
        ("finalization", "finalization_pull_request"),
    ):
        number = checkpoint.get(field)
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            link: dict[str, object] = {
                "role": role,
                "number": number,
                "url": f"https://github.com/{repository}/pull/{number}",
            }
            link.update(_pull_request_github_metrics(root, repository, number))
            links.append(link)
    return links


def _pull_request_github_metrics(root: Path, repository: str, number: int) -> dict[str, int]:
    """Read bounded, display-only GitHub counts for already-linked PR evidence.

    The checkpoint remains the authority for the PR link. These metrics enrich
    its detail view only when the local checkout still proves that it belongs
    to the same GitHub repository; unavailable evidence is omitted rather than
    represented as a misleading zero.
    """
    try:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
        if not match or f"{match.group(1)}/{match.group(2)}" != repository:
            return {}
        payload = GitHubProvider().github(
            "pr", "view", str(number), "--repo", repository,
            "--json", "number,commits,changedFiles,statusCheckRollup",
        )
        pull_request = json.loads(payload)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return {}
    if not isinstance(pull_request, dict) or pull_request.get("number") != number:
        return {}
    metrics: dict[str, int] = {}
    commits = pull_request.get("commits")
    if isinstance(commits, list):
        metrics["commit_count"] = len(commits)
    changed_files = pull_request.get("changedFiles")
    if isinstance(changed_files, int) and not isinstance(changed_files, bool) and changed_files >= 0:
        metrics["changed_file_count"] = changed_files
    checks = pull_request.get("statusCheckRollup")
    if isinstance(checks, list):
        # GitHub's Checks tab counts check runs.  `statusCheckRollup` also
        # contains legacy status contexts such as Owner Authorization; those
        # belong to commit status, not the visible Checks count.
        metrics["check_count"] = sum(
            1 for check in checks
            if isinstance(check, dict)
            and check.get("__typename") == "CheckRun"
            and str(check.get("name") or "").strip()
        )
    return metrics


def _terminal_run_diagnostic(root: Path, run_id: str | None) -> str | None:
    """Return the checkpoint-owned block reason before consulting legacy logs."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return None
    checkpoint = _canonical_checkpoint(root, run_id)
    if (
        checkpoint.get("phase") in {"BLOCKED", "FAILED"}
        and checkpoint.get("terminal") is True
        and isinstance(checkpoint.get("diagnostic"), str)
        and checkpoint["diagnostic"].strip()
    ):
        return redact_diagnostic(checkpoint["diagnostic"], limit=500)
    try:
        connection = open_storage(root)
        try:
            rows = connection.execute(
                "SELECT payload FROM engineering_component_logs "
                "WHERE component='inbox' ORDER BY id DESC LIMIT 200"
            ).fetchall()
        finally:
            connection.close()
    except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
        return None
    for row in rows:
        try:
            event = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("run_id") != run_id or event.get("event") != "job_failed":
            continue
        diagnostic = event.get("diagnostic")
        if isinstance(diagnostic, str) and diagnostic.strip():
            return redact_diagnostic(diagnostic, limit=500)
    return None


def _prompt_history_detail(root: Path, run_id: str | None) -> bytes:
    """Return private, immutable operational evidence for one history row."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    entry = next((item for item in prompt_history(root) if item.get("run_id") == run_id), None)
    if entry is None:
        return b""
    execution = json.loads(_last_executed_agent_execution(root, run_id))
    runtime = json.loads(_last_executed_runtime_metadata(root, run_id))
    reviewers = json.loads(_reviewer_agents_for_run(root, run_id))
    commits = _commits_for_run(root, run_id)
    pull_requests = _pull_requests_for_run(root, run_id)
    commit_timeline = _commit_timeline_for_run(root, run_id)
    usage: dict[str, object] = {}
    try:
        provider_summary = provider_usage_summary(root, run_id)
        # Legacy runs have no invocation rows. Retain the historic aggregate
        # projection without fabricating invocation detail.
        if provider_summary.get("invocation_detail") == "UNAVAILABLE":
            usage = provider_summary
            connection = open_storage(root)
            row = connection.execute(
                "SELECT input_tokens, output_tokens, total_tokens FROM execution_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            connection.close()
            if row:
                usage.update(
                    {
                        label: value
                        for label, value in zip(("input_tokens", "output_tokens", "total_tokens"), row)
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    }
                )
        else:
            usage = provider_summary
    except Exception:
        usage = {}
    report: str | None = None
    try:
        report = _report_for_run(root, run_id).decode("utf-8")
    except UnicodeDecodeError:
        pass
    if diagnostic := _terminal_run_diagnostic(root, run_id):
        entry["execution_diagnostic"] = diagnostic
        if str(entry.get("status") or "").upper() in {"BLOCKED", "FAILED"}:
            entry["blocking_reason"] = diagnostic
    entry["execution_activity_summary"] = terminal_activity_summary(root, run_id)
    return _project_prompt_history_detail(
        entry,
        execution=execution,
        runtime=runtime,
        reviewers=reviewers,
        commits=commits,
        pull_requests=pull_requests,
        commit_timeline=commit_timeline,
        usage=usage,
        report=report,
        lifecycle=lifecycle_projection(root, run_id),
    )


def _rate_limit_window_label(duration_minutes: int) -> str:
    """Use a neutral label that reflects the window actually reported by Codex."""
    if duration_minutes == 300:
        return "5-uursvenster"
    if duration_minutes == 10_080:
        return "Weekvenster"
    if duration_minutes % 1_440 == 0:
        return f"{duration_minutes // 1_440}-daags venster"
    if duration_minutes % 60 == 0:
        return f"{duration_minutes // 60}-uursvenster"
    return f"{duration_minutes}-minutenvenster"


def _normalize_rate_limits(payload: object) -> dict[str, object]:
    """Keep only safe, displayable quota values from Codex's read-only response."""
    if not isinstance(payload, dict):
        return {}
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict):
        return {}
    windows: list[dict[str, int | str]] = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        used = item.get("usedPercent")
        duration = item.get("windowDurationMins")
        resets_at = item.get("resetsAt")
        if (
            not isinstance(used, (int, float))
            or isinstance(used, bool)
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or not isinstance(resets_at, int)
            or isinstance(resets_at, bool)
        ):
            continue
        windows.append(
            {
                "label": _rate_limit_window_label(duration),
                "used_percent": max(0, min(100, round(used))),
                "window_minutes": duration,
                "resets_at": resets_at,
            }
        )
    credits = payload.get("rateLimitResetCredits")
    available = credits.get("availableCount") if isinstance(credits, dict) else None
    normalized: dict[str, object] = {"windows": windows}
    if isinstance(available, int) and not isinstance(available, bool) and available >= 0:
        normalized["reset_credits"] = available
    return normalized if windows or "reset_credits" in normalized else {}


def _remaining_rate_limit_capacity(rate_limits: dict[str, object]) -> float | None:
    """Return the most restrictive safe remaining quota percentage."""
    windows = rate_limits.get("windows")
    if not isinstance(windows, list):
        return None
    remaining_values: list[float] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        remaining_values.append(max(0.0, min(100.0, 100.0 - float(used))))
    return min(remaining_values) if remaining_values else None


def _codex_cli_installation_path(executable: str | None) -> str | None:
    """Return EP's managed CLI prefix, never a PATH-resolved alternative."""
    if not executable:
        return None
    managed_prefix = engineering_platform_codex_cli_prefix()
    if Path(executable).expanduser() == managed_prefix / "bin" / "codex":
        return str(managed_prefix)
    return None


def _codex_provider_identity(*, refresh: bool = False) -> dict[str, str]:
    """Return the active provider identity and its locally resolved executable path."""
    global _codex_identity_cache
    now = time.monotonic()
    with _codex_identity_cache_lock:
        if not refresh and _codex_identity_cache and now - _codex_identity_cache[0] < CODEX_IDENTITY_CACHE_SECONDS:
            return dict(_codex_identity_cache[1])

    identity = {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"}
    executable = codex_cli_executable()
    if executable:
        if installation_path := _codex_cli_installation_path(executable):
            identity["provider_path"] = installation_path
        try:
            completed = LocalProcessProvider().execute(Path.cwd(), (executable, "--version"))
        except OSError:
            completed = None
        if completed and completed.returncode == 0:
            match = re.search(
                r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)",
                (completed.stdout or completed.stderr).strip(),
            )
            if match:
                identity["provider_version"] = match.group(1)
    with _codex_identity_cache_lock:
        _codex_identity_cache = (now, identity)
    return dict(identity)


def _codex_rate_limits() -> bytes:
    """Read current Codex quota windows without persisting account or credit data."""
    global _rate_limit_cache
    now = time.monotonic()
    with _rate_limit_cache_lock:
        if _rate_limit_cache and now - _rate_limit_cache[0] < RATE_LIMIT_CACHE_SECONDS:
            return _rate_limit_cache[1]
    identity = _codex_provider_identity()
    provider = CodexCliProvider()
    process = None
    try:
        process = provider.app_server()
        if process.stdin is None or process.stdout is None:
            return json.dumps(identity, separators=(",", ":")).encode()
        process.stdin.write(
            json.dumps(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "djconnect_engineering_dashboard",
                            "title": "EP Operations",
                            "version": DASHBOARD_VERSION,
                        }
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 5
        requested = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select((process.stdout,), (), (), max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == 1 and not requested:
                process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                process.stdin.write(
                    json.dumps({"method": "account/rateLimits/read", "id": 2, "params": {}})
                    + "\n"
                )
                process.stdin.flush()
                requested = True
            elif response.get("id") == 2:
                result = {**identity, **_normalize_rate_limits(response.get("result"))}
                encoded = json.dumps(result, separators=(",", ":")).encode()
                with _rate_limit_cache_lock:
                    _rate_limit_cache = (time.monotonic(), encoded)
                return encoded
    except (OSError, ValueError, json.JSONDecodeError):
        return json.dumps(identity, separators=(",", ":")).encode()
    finally:
        if process is not None:
            provider.close_app_server(process)
    return json.dumps(identity, separators=(",", ":")).encode()


def _github_rate_limit_status() -> dict[str, object]:
    """Read GitHub quota state without changing GitHub or repository state."""
    try:
        payload = json.loads(GitHubProvider().github("api", "rate_limit"))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        # A rate-limit response can itself be a 403/429.  Do not show a red
        # operator banner for unrelated authentication or network failures.
        return {"limited": "rate limit" in str(error).lower()}
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return {"limited": False}
    exhausted: list[tuple[str, int]] = []
    for name in ("core", "graphql", "search"):
        resource = resources.get(name)
        if not isinstance(resource, dict):
            continue
        remaining, reset = resource.get("remaining"), resource.get("reset")
        if isinstance(remaining, int) and remaining <= 0:
            exhausted.append((name, reset if isinstance(reset, int) else 0))
    if not exhausted:
        return {"limited": False}
    reset_at = min((reset for _, reset in exhausted if reset > 0), default=None)
    return {"limited": True, "reset_at": reset_at}


class RateLimitResetError(RuntimeError):
    """Raised when Codex cannot safely consume a reset credit."""


class CodexCliUpdateError(RuntimeError):
    """Raised when the local Codex CLI cannot be updated and verified safely."""


_CODEX_CLI_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _codex_cli_version(value: object) -> str | None:
    candidate = str(value or "").strip().removeprefix("v")
    return candidate if _CODEX_CLI_VERSION.fullmatch(candidate) else None


def _codex_cli_version_key(value: str) -> tuple[int, int, int, int, str]:
    """Compare normal releases above prereleases without accepting arbitrary text."""
    base, separator, prerelease = value.partition("-")
    major, minor, patch = (int(part) for part in base.split("."))
    return major, minor, patch, 1 if not separator else 0, prerelease


def _npm_executable() -> str | None:
    """Resolve npm for managed-CLI installation without selecting another CLI."""
    return shutil.which("npm")


def _execution_active(root: Path) -> bool:
    """Return whether an Execution Host lifecycle is actively using this installation."""
    try:
        status = json.loads(_status(root))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(status, dict)
        and status.get("watcher_state") in {"ENGINEERING_RUN_ACTIVE", "WAITING_FOR_OPERATOR_MERGE"}
        and isinstance(status.get("run_id"), str)
        and bool(status["run_id"])
    )


def _inbox_has_items(inbox: Path) -> bool:
    """Return whether the active Inbox contains work, conservatively on errors."""
    try:
        return any(not item.name.startswith(".") for item in inbox.iterdir())
    except OSError:
        return True


def _codex_cli_update_status(root: Path, *, refresh: bool = False) -> dict[str, object]:
    """Read the published Codex CLI version without exposing account or npm output."""
    global _codex_update_cache
    now = time.monotonic()
    with _codex_update_cache_lock:
        if not refresh and _codex_update_cache and now - _codex_update_cache[0] < CODEX_UPDATE_CACHE_SECONDS:
            return dict(_codex_update_cache[1])

    current = _codex_cli_version(_codex_provider_identity(refresh=refresh).get("provider_version"))
    npm = _npm_executable()
    if current is None or npm is None:
        status: dict[str, object] = {"state": "unavailable", "update_available": False}
    else:
        try:
            completed = LocalProcessProvider().execute(root, (npm, "view", CODEX_CLI_PACKAGE, "version", "--json"))
            latest_raw = json.loads(completed.stdout) if completed.returncode == 0 else None
            candidates = latest_raw if isinstance(latest_raw, list) else [latest_raw]
            versions = [version for value in candidates if (version := _codex_cli_version(value)) is not None]
            latest = max(versions, key=_codex_cli_version_key, default=None)
        except (OSError, ValueError, json.JSONDecodeError):
            latest = None
        if latest is None:
            status = {"state": "unavailable", "update_available": False, "current_version": current}
        else:
            update_available = _codex_cli_version_key(latest) > _codex_cli_version_key(current)
            status = {
                "state": "update_available" if update_available else "current",
                "update_available": update_available,
                "current_version": current,
                "latest_version": latest,
            }
    with _codex_update_cache_lock:
        _codex_update_cache = (now, status)
    return dict(status)


def _install_codex_cli_update(root: Path) -> dict[str, object]:
    """Install the exact checked release, then verify the executable's version."""
    global _codex_identity_cache, _codex_update_cache
    with _codex_update_install_lock:
        if _execution_active(root):
            raise CodexCliUpdateError("codex_cli_update_execution_active")
        status = _codex_cli_update_status(root, refresh=True)
        if status.get("state") == "unavailable":
            raise CodexCliUpdateError("codex_cli_update_unavailable")
        if not status.get("update_available"):
            return {"updated": False, "current_version": status.get("current_version")}
        latest = status.get("latest_version")
        npm = _npm_executable()
        if not isinstance(latest, str) or npm is None:
            raise CodexCliUpdateError("codex_cli_update_unavailable")
        try:
            completed = LocalProcessProvider().execute(
                root,
                (npm, "install", "--global", "--prefix", str(engineering_platform_codex_cli_prefix()), f"{CODEX_CLI_PACKAGE}@{latest}"),
            )
        except OSError as error:
            raise CodexCliUpdateError("codex_cli_update_failed") from error
        if completed.returncode:
            diagnostic = f"{completed.stdout}\n{completed.stderr}".lower()
            if "eacces" in diagnostic or "permission denied" in diagnostic:
                raise CodexCliUpdateError("codex_cli_update_permissions_required")
            raise CodexCliUpdateError("codex_cli_update_failed")
        with _codex_identity_cache_lock:
            _codex_identity_cache = None
        with _codex_update_cache_lock:
            _codex_update_cache = None
        installed = _codex_cli_version(_codex_provider_identity(refresh=True).get("provider_version"))
        if installed is None or _codex_cli_version_key(installed) < _codex_cli_version_key(latest):
            raise CodexCliUpdateError("codex_cli_update_failed")
        return {"updated": True, "current_version": installed}


def _consume_codex_rate_limit_reset_credit() -> str:
    """Consume exactly one available Codex reset credit through its app-server API."""
    global _rate_limit_cache
    provider = CodexCliProvider()
    process = None
    try:
        process = provider.app_server()
        if process.stdin is None or process.stdout is None:
            raise RateLimitResetError("Codex-reset is niet beschikbaar.")
        process.stdin.write(
            json.dumps(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "djconnect_engineering_dashboard",
                            "title": "EP Operations",
                            "version": DASHBOARD_VERSION,
                        }
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 5
        requested = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select((process.stdout,), (), (), max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == 1 and not requested:
                process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                process.stdin.write(
                    json.dumps(
                        {
                            "method": "account/rateLimitResetCredit/consume",
                            "id": 2,
                            "params": {"idempotencyKey": str(uuid.uuid4())},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                requested = True
            elif response.get("id") == 2:
                result = response.get("result")
                outcome = result.get("outcome") if isinstance(result, dict) else None
                if outcome not in {"reset", "nothingToReset", "noCredit", "alreadyRedeemed"}:
                    raise RateLimitResetError("Codex-reset kon niet worden bevestigd.")
                with _rate_limit_cache_lock:
                    _rate_limit_cache = None
                return outcome
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RateLimitResetError("Codex-reset is niet beschikbaar.") from error
    finally:
        if process is not None:
            provider.close_app_server(process)
    raise RateLimitResetError("Codex-reset reageerde niet op tijd.")


def _latest_codex_log(root: Path) -> bytes:
    """Return only the latest locally redacted Codex diagnostic."""
    logs = sorted((root / ".engineering" / "logs" / "codex").glob("*.log"))
    try:
        return logs[-1].read_bytes() if logs else b"Geen Codex CLI-diagnose beschikbaar."
    except OSError:
        return b"Codex CLI-diagnose is niet beschikbaar."


def _component_log(root: Path, component: str) -> bytes:
    """Return canonical SQLite logs with the file-only fallback retained in logging."""
    return stored_component_log(root, component)


def _component_log_page(root: Path, component: str, query: dict[str, list[str]]) -> dict[str, object]:
    """Validate the browser's bounded filter contract before querying SQLite."""
    def single(name: str, default: str = "") -> str:
        values = query.get(name, [])
        if len(values) > 1:
            raise ValueError("Ongeldig logfilter.")
        return values[0] if values else default

    def timestamp(name: str) -> str | None:
        value = single(name).strip()
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Ongeldig logtijdvenster.")
        return parsed.astimezone(timezone.utc).isoformat()

    try:
        page = int(single("page", "1"))
        page_size = int(single("page_size", "50"))
    except ValueError as error:
        raise ValueError("Ongeldige logpagina.") from error
    start_at = timestamp("start")
    end_at = timestamp("end")
    if start_at and end_at and end_at < start_at:
        raise ValueError("Eindtijd van het logtijdvenster ligt vóór de begintijd.")
    return stored_component_log_page(
        root,
        component,
        page=page,
        page_size=page_size,
        start_at=start_at,
        end_at=end_at,
        inclusive_end=single("inclusive_end") == "1",
        search=single("search"),
        level=single("level"),
        events=query.get("event", []),
        sort_key=single("sort", "timestamp"),
        direction=single("direction", "desc"),
    )


def _clear_component_log(root: Path, component: str) -> None:
    """Clear exactly one canonical component log."""
    clear_stored_component_log(root, component)


def _component_log_versions(root: Path) -> dict[str, str]:
    """Return SQLite revisions so browsers fetch logs only when they changed."""
    return {component: component_log_version(root, component) for component in ("inbox", "dashboard")}


def _launch_agent_health(label: str) -> dict[str, str | bool]:
    """Inspect one owned LaunchAgent process without changing its state."""
    state = LaunchdProvider().runtime_status(label)
    if state.qualified:
        return {"healthy": True, "state": "running", "detail": "LaunchAgent-proces is actief"}
    if state.detail == "launchctl unavailable":
        return {"healthy": False, "state": "unavailable", "detail": "launchctl ontbreekt"}
    return {"healthy": False, "state": "not_running", "detail": "LaunchAgent is geladen, maar heeft geen actief proces" if state.detail.endswith("no active process") else "LaunchAgent is niet geladen"}


def _inbox_watcher_health(root: Path) -> dict[str, str | bool]:
    """Add a safe startup reason when the watcher is not actually live."""
    health = _launch_agent_health(WATCHER_LABEL)
    if bool(health["healthy"]) or not storage_activation_required(root):
        return health
    return {
        **health,
        "detail": "Gecontroleerde opslagactivatie vereist voordat de Inbox-watcher kan starten.",
        "reason_code": "storage_activation_required",
    }


def _platform_health(root: Path) -> dict[str, object]:
    """Provide a read-only readiness projection for every local EP component."""
    components: dict[str, dict[str, object]] = {
        "dashboard": {
            "healthy": True,
            "state": "running",
            "detail": "HTTP-dashboard reageert",
            "version": DASHBOARD_VERSION,
            "uptime_seconds": max(0, round(time.monotonic() - DASHBOARD_STARTED_AT)),
        },
        "inbox_watcher": {
            **_inbox_watcher_health(root),
            "version": WATCHER_VERSION,
            "uptime_seconds": _component_uptime_seconds("inbox_watcher"),
        },
        "dashboard_relay": {
            **_launch_agent_health(RELAY_LABEL),
            "uptime_seconds": _component_uptime_seconds("dashboard_relay"),
        },
    }
    healthy = all(bool(component["healthy"]) for component in components.values())
    return {"health": "ok" if healthy else "degraded", "healthy": healthy, "components": components}


def _launch_agent_details(label: str) -> dict[str, object]:
    """Return the safe, owned portion of one per-user LaunchAgent contract."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    details: dict[str, object] = {
        "label": label,
        "plist_path": str(plist_path),
        "loaded": False,
        "program_arguments": [],
        "keep_alive": None,
        "run_at_load": None,
    }
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return details
    if not isinstance(payload, dict):
        return details
    arguments = payload.get("ProgramArguments")
    if isinstance(arguments, list):
        details["program_arguments"] = [str(value) for value in arguments[:8]]
    details["keep_alive"] = bool(payload.get("KeepAlive"))
    details["run_at_load"] = bool(payload.get("RunAtLoad"))
    details["loaded"] = bool(_launch_agent_health(label).get("healthy"))
    return details


def _component_processes(component: str) -> list[dict[str, int | str]]:
    """Return bounded process evidence for a known local component only."""
    patterns = {
        "dashboard": ("tools.engineering.dashboard", "dashboard.py"),
        "inbox_watcher": ("inbox_watcher",),
        "dashboard_relay": ("dashboard_supervisor",),
    }.get(component, ())
    if not patterns:
        return []
    try:
        observed = LocalProcessProvider().execute(Path.cwd(), ("ps", "-axo", "pid=,rss=,etime=,command="))
    except OSError:
        return []
    if observed.returncode:
        return []
    processes: list[dict[str, int | str]] = []
    for line in observed.stdout.splitlines():
        parts = line.strip().split(maxsplit=3)
        if len(parts) != 4 or not any(pattern in parts[3] for pattern in patterns):
            continue
        # Browser tests launch disposable dashboard servers in temporary
        # ``djconnect-dashboard-test-*`` directories.  They are not managed
        # platform components and must not inflate production health evidence.
        if "djconnect-dashboard-test-" in parts[3]:
            continue
        try:
            elapsed = _process_elapsed_seconds(parts[2])
            processes.append(
                {
                    "pid": int(parts[0]),
                    "memory_kib": int(parts[1]),
                    "uptime_seconds": elapsed,
                }
            )
        except ValueError:
            continue
    return processes


def _process_elapsed_seconds(value: str) -> int:
    """Convert portable ps etime values into bounded elapsed seconds."""
    days, separator, clock = value.partition("-")
    if not separator:
        clock = days
        day_count = 0
    else:
        day_count = int(days)
    parts = [int(part) for part in clock.split(":")]
    if len(parts) == 1:
        hours, minutes, seconds = 0, 0, parts[0]
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("Ongeldig ps etime-formaat")
    return max(0, day_count * 86_400 + hours * 3_600 + minutes * 60 + seconds)


def _component_uptime_seconds(component: str) -> int | None:
    """Return the longest observed lifetime of a local component process."""
    uptimes = [
        process.get("uptime_seconds")
        for process in _component_processes(component)
        if isinstance(process.get("uptime_seconds"), int)
    ]
    return max(uptimes) if uptimes else None


def _component_details(root: Path, component: str) -> dict[str, object]:
    """Describe one named EP component without exposing credentials or prompts."""
    health = _platform_health(root).get("components", {})
    current = health.get(component) if isinstance(health, dict) else None
    if not isinstance(current, dict):
        raise ValueError("Onbekend Engineering Platform-onderdeel.")
    result: dict[str, object] = {
        "component": component,
        "machine": socket.gethostname(),
        "git_commit": _build_commit(root),
        "healthy": bool(current.get("healthy")),
        "state": str(current.get("state", "unknown")),
        "detail": str(current.get("detail", "Geen toelichting")),
        "version": current.get("version"),
        "uptime_seconds": current.get("uptime_seconds"),
        "restart_supported": component in RESTARTABLE_COMPONENTS,
        "processes": _component_processes(component),
    }
    if label := COMPONENT_LABELS.get(component):
        result["launchd"] = _launch_agent_details(label)
    else:
        result["launchd"] = None
        result["executable_path"] = None
    return result


def _restart_component(component: str) -> None:
    """Safely ask launchd to restart one explicitly owned, restartable component."""
    if component not in RESTARTABLE_COMPONENTS:
        raise ValueError("Dit onderdeel kan niet veilig vanuit het dashboard worden herstart.")
    try:
        LaunchdProvider().restart(COMPONENT_LABELS[component])
    except OSError as error:
        raise OSError("De herstart is niet gelukt.") from error


def _choose_local_directory(root: Path) -> str | None:
    """Open the host's native directory picker after an explicit dashboard action."""
    if sys.platform != "darwin":
        raise RuntimeError("Een lokale mapkiezer is alleen op deze machine beschikbaar.")
    result = LocalProcessProvider().execute(
        root, ("osascript", "-e", "POSIX path of (choose folder)")
    )
    if result.returncode:
        if "-128" in (result.stderr or ""):
            return None
        raise RuntimeError("De lokale mapkiezer kon niet worden geopend.")
    location = result.stdout.strip()
    if not location or not Path(location).is_dir():
        raise RuntimeError("De gekozen lokale map is niet beschikbaar.")
    return location


def _restart_component_after_response(component: str, logger: logging.Logger) -> None:
    """Restart after the acknowledgement and retain only a bounded failure event."""
    try:
        _restart_component(component)
    except OSError as error:
        log_event(logger, logging.ERROR, "component_restart_failed", diagnostic=str(error))


def _restart_engineering_platform_after_main_switch(root: Path, logger: logging.Logger) -> None:
    """Reload every owned Engineering Platform process after a main switch.

    The dashboard runs the replacement last because restarting it terminates
    this process.  A newly active execution wins over freshness: it is never
    interrupted merely to reload the platform.
    """
    if _execution_active(root):
        log_event(logger, logging.WARNING, "engineering_platform_restart_skipped", diagnostic="execution_active")
        return
    failed: list[str] = []
    for component in ("inbox_watcher", "dashboard_relay", "dashboard"):
        try:
            _restart_component(component)
        except OSError:
            failed.append(component)
    log_event(
        logger,
        logging.INFO if not failed else logging.ERROR,
        "engineering_platform_restart_completed" if not failed else "engineering_platform_restart_failed",
        diagnostic="components=" + (",".join(failed) if failed else "inbox_watcher,dashboard_relay,dashboard"),
    )


def _registered_worktree_path(root: Path, worktree_path: object, branch: object | None = None) -> Path:
    """Resolve one worktree from Git's current registration, never from HTTP input."""
    if not isinstance(worktree_path, str) or not worktree_path or (branch is not None and not isinstance(branch, str)):
        raise ValueError("De gekozen worktree is ongeldig.")
    projection = _workspace_worktrees(root)
    candidates = [
        item for item in projection.get("worktrees", [])
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"] == worktree_path
        and (branch is None or item.get("branch") == branch)
    ]
    if len(candidates) != 1:
        raise RuntimeError("De gekozen worktree is niet beschikbaar voor een veilige switch.")
    target = Path(candidates[0]["path"]).resolve()
    if not target.is_absolute():
        raise RuntimeError("De gekozen worktree is niet beschikbaar voor een veilige switch.")
    return target


def _registered_worktree_switch_target(root: Path, worktree_path: object, branch: object) -> Path:
    """Return one clean, currently registered non-main worktree or fail closed."""
    if not isinstance(branch, str) or not branch:
        raise ValueError("De gekozen worktree is ongeldig.")
    target = _registered_worktree_path(root, worktree_path, branch)
    if branch == PlatformConfiguration.load(root).workspace.default_branch:
        raise RuntimeError("De gekozen worktree is niet beschikbaar voor een veilige switch.")
    if not target.is_dir() or not (target / "tools" / "engineering" / "dashboard.py").is_file() or not (target / "tools" / "engineering" / "inbox_watcher.py").is_file():
        raise RuntimeError("De gekozen worktree bevat geen complete Engineering Platform-installatie.")
    provider = GitProvider()
    try:
        status = provider.execute(target, "git", "status", "--porcelain", "--untracked-files=all")
        active = provider.execute(target, "git", "branch", "--show-current")
    except OSError as error:
        raise RuntimeError("De gekozen worktree kon niet worden gecontroleerd.") from error
    if status.returncode or active.returncode or status.stdout.strip() or active.stdout.strip() != branch:
        raise RuntimeError("De gekozen worktree moet schoon zijn en exact op de geregistreerde branch staan.")
    return target


def _worktree_switch_target_when_idle(root: Path, worktree_path: object, branch: object) -> Path:
    """Gate a worktree switch on authoritative run and Inbox state."""
    if _execution_active(root):
        raise RuntimeError("Naar een worktree schakelen kan alleen wanneer geen uitvoering actief is.")
    active_inbox = PlatformConfiguration.load(root).resolver(root).resolve_runtime_prompt_transport().inbox
    if _inbox_has_items(active_inbox):
        raise RuntimeError("Naar een worktree schakelen kan alleen wanneer de Inbox-queue leeg is.")
    return _registered_worktree_switch_target(root, worktree_path, branch)


def _activate_engineering_platform_worktree(root: Path, worktree_path: str, branch: str, logger: logging.Logger) -> None:
    """Revalidate and move the owned services to a selected clean worktree."""
    try:
        target = _worktree_switch_target_when_idle(root, worktree_path, branch)
        relay = build_relay(target)
        watcher_agent = inbox_watcher.launch_agent(target)
        relay_agent = relay_launch_agent(target, relay)
        dashboard_agent = launch_agent(target)
        launchd = LaunchdProvider()
        launchd.install(WATCHER_LABEL, watcher_agent)
        launchd.install(RELAY_LABEL, relay_agent)
        # Dashboard is deliberately last: its replacement terminates the
        # current process only after watcher and relay point at the same root.
        launchd.install(LABEL, dashboard_agent)
    except (OSError, RuntimeError, ValueError) as error:
        log_event(logger, logging.ERROR, "workspace_switch_failed", diagnostic=str(error))
        return
    log_event(logger, logging.INFO, "workspace_switch_completed", diagnostic=f"branch={branch}")


class InboxLocationChangeError(RuntimeError):
    """Raised when a new Inbox route cannot be confirmed by a fresh watcher."""


def _restart_and_verify_inbox_watcher(
    root: Path, expected_inbox: Path, *, timeout_seconds: float = 8.0,
) -> None:
    """Restart the watcher and require a fresh ready record for ``expected_inbox``."""
    requested_at = datetime.now(timezone.utc)
    _restart_component("inbox_watcher")
    deadline = time.monotonic() + timeout_seconds
    expected = str(expected_inbox.resolve())
    while time.monotonic() < deadline:
        try:
            ready = load_projection(root, WATCHER_READY_PROJECTION) or {}
            started_at, pid = ready.get("started_at"), ready.get("pid")
            started = (
                datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                if isinstance(started_at, str) else None
            )
            if (
                ready.get("inbox_path") == expected
                and isinstance(pid, int)
                and started is not None
                and started >= requested_at
            ):
                os.kill(pid, 0)
                return
        except (EngineeringStorageError, OSError, ValueError):
            pass
        time.sleep(0.1)
    raise OSError("Inbox watcher restart did not confirm the configured Inbox route.")


def _change_inbox_location(root: Path, value: object, active_inbox: Path) -> dict[str, object]:
    """Commit an Inbox route only after its replacement watcher confirms it."""
    previous = inbox_root(root)
    event = update_inbox_root(root, value)
    try:
        _restart_and_verify_inbox_watcher(root, Path(str(event["value"])) / "Inbox")
    except OSError as error:
        try:
            restore_inbox_root(root, previous)
            _restart_and_verify_inbox_watcher(root, active_inbox)
        except OSError as rollback_error:
            raise InboxLocationChangeError("watcher_restart_and_rollback_failed") from rollback_error
        raise InboxLocationChangeError("watcher_restart_failed_rolled_back") from error
    return {**event, "watcher_verified": True}


def _restore_managed_main_branch(root: Path) -> dict[str, str]:
    """Return a clean managed workspace to main, then restart its Inbox watcher."""
    provider = GitProvider()
    try:
        status = provider.execute(root, "git", "status", "--porcelain", "--untracked-files=all")
        branch = provider.execute(root, "git", "branch", "--show-current")
    except OSError as error:
        raise RuntimeError("De werkmap kon niet worden gecontroleerd.") from error
    if status.returncode or branch.returncode:
        raise RuntimeError("De werkmap kon niet veilig worden gecontroleerd.")
    if status.stdout.strip():
        raise RuntimeError("Herstel is alleen mogelijk wanneer de werkmap geen lokale wijzigingen bevat.")
    previous_branch = branch.stdout.strip()
    if previous_branch != "main":
        try:
            provider.command(root, "git", "switch", "main")
        except RuntimeError as error:
            raise RuntimeError("De werkmap kon niet veilig naar main worden teruggezet.") from error
    try:
        LaunchdProvider().restart(WATCHER_LABEL)
    except OSError as error:
        raise RuntimeError("De werkmap staat op main, maar de Inbox-watcher kon niet worden herstart.") from error
    return {"previous_branch": previous_branch, "branch": "main", "watcher": "restarted"}


def _synchronize_managed_branch_with_upstream(root: Path) -> dict[str, str]:
    """Fast-forward the configured managed branch, without overwriting work."""
    provider = GitProvider()
    expected_branch = PlatformConfiguration.load(root).workspace.default_branch
    try:
        status = provider.execute(root, "git", "status", "--porcelain", "--untracked-files=all")
        branch = provider.execute(root, "git", "branch", "--show-current")
        upstream = provider.execute(root, "git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    except OSError as error:
        raise RuntimeError("De werkmap kon niet worden gecontroleerd.") from error
    if status.returncode or branch.returncode or upstream.returncode:
        raise RuntimeError("De werkmap kon niet veilig worden gecontroleerd.")
    if status.stdout.strip():
        raise RuntimeError("Herstel is alleen mogelijk wanneer de werkmap geen lokale wijzigingen bevat.")
    if branch.stdout.strip() != expected_branch:
        raise RuntimeError("Herstel is alleen mogelijk op de verwachte branch.")
    upstream_ref = upstream.stdout.strip()
    remote, separator, _ = upstream_ref.partition("/")
    if not separator or not remote:
        raise RuntimeError("De upstream van de verwachte branch is niet beschikbaar.")
    try:
        provider.command(root, "git", "fetch", "--quiet", remote)
    except RuntimeError as error:
        raise RuntimeError("De upstream van de verwachte branch kon niet worden opgehaald.") from error
    divergence = provider.execute(root, "git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if divergence.returncode:
        raise RuntimeError("De synchronisatiestatus van de verwachte branch is niet beschikbaar.")
    try:
        behind, ahead = (int(value) for value in divergence.stdout.split())
    except ValueError as error:
        raise RuntimeError("De synchronisatiestatus van de verwachte branch is ongeldig.") from error
    if ahead:
        raise RuntimeError("De verwachte branch bevat lokale commits en kan niet veilig worden hersteld.")
    if behind:
        try:
            provider.command(root, "git", "merge", "--ff-only", "@{upstream}")
        except RuntimeError as error:
            raise RuntimeError("De verwachte branch kon niet veilig worden gesynchroniseerd.") from error
    final = provider.execute(root, "git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if final.returncode or final.stdout.strip() != "0\t0":
        raise RuntimeError("De verwachte branch is niet gesynchroniseerd met de upstream.")
    try:
        LaunchdProvider().restart(WATCHER_LABEL)
    except OSError as error:
        raise RuntimeError("De branch is gesynchroniseerd, maar de Inbox-watcher kon niet worden herstart.") from error
    return {"branch": expected_branch, "upstream": upstream_ref, "watcher": "restarted"}


def _switch_to_fast_forward_main(root: Path) -> dict[str, str]:
    """Safely switch to the managed branch only when it can fast-forward."""
    provider = GitProvider()
    expected_branch = PlatformConfiguration.load(root).workspace.default_branch
    try:
        status = provider.execute(root, "git", "status", "--porcelain", "--untracked-files=all")
        active = provider.execute(root, "git", "branch", "--show-current")
        if status.returncode or active.returncode or status.stdout.strip():
            raise RuntimeError("De werkmap moet schoon zijn voordat naar main wordt geschakeld.")
        if provider.execute(root, "git", "fetch", "--prune", "origin").returncode:
            raise RuntimeError("origin kon niet veilig worden ververst.")
        divergence = provider.execute(root, "git", "rev-list", "--left-right", "--count", f"origin/{expected_branch}...{expected_branch}")
        if divergence.returncode:
            raise RuntimeError("De synchronisatiestatus van main is niet beschikbaar.")
        behind, ahead = (int(value) for value in divergence.stdout.split())
        if ahead:
            raise RuntimeError("Lokale commits op main voorkomen een veilige fast-forward.")
        previous_branch = active.stdout.strip()
        if previous_branch != expected_branch:
            provider.command(root, "git", "switch", expected_branch)
        if behind:
            provider.command(root, "git", "merge", "--ff-only", f"origin/{expected_branch}")
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(str(error) or "Naar main schakelen is niet veilig gelukt.") from error
    # The branch action is the point at which an earlier managed-branch drift
    # can genuinely be resolved. Replace stale evidence with a fresh Level 2
    # result before the services are restarted and re-project the dashboard.
    workspace_preflight = execute_workspace_preflight(root, "Execution Mode: MANAGED")
    return {
        "previous_branch": previous_branch,
        "branch": expected_branch,
        "synchronized": "true",
        "workspace_preflight": workspace_preflight.outcome,
    }


def _workspace_git_lock(root: Path, *, now: float | None = None) -> dict[str, object]:
    """Describe the index lock without offering recovery unless it is provably stale."""
    lock_path = root / ".git" / "index.lock"
    try:
        age_seconds = max(0, int((now if now is not None else time.time()) - lock_path.stat().st_mtime))
    except OSError:
        return {"state": "free", "active": False, "stale": False}

    # lsof is the conservative ownership check: if it is unavailable or cannot
    # determine ownership, recovery remains disabled.  Never guess that a lock
    # is stale merely because it is old.
    lsof = shutil.which("lsof")
    if not lsof:
        return {"state": "active", "active": True, "stale": False, "age_seconds": age_seconds}
    try:
        ownership = LocalProcessProvider().execute(root, (lsof, "-t", str(lock_path)))
    except OSError:
        return {"state": "active", "active": True, "stale": False, "age_seconds": age_seconds}
    owner_pids = [line for line in ownership.stdout.splitlines() if line.strip().isdigit()]
    stale = ownership.returncode == 1 and not owner_pids and age_seconds >= GIT_INDEX_LOCK_STALE_SECONDS
    return {
        "state": "stale" if stale else "active",
        "active": not stale,
        "stale": stale,
        "age_seconds": age_seconds,
    }


def _recover_stale_workspace_git_lock(root: Path) -> dict[str, object]:
    """Remove only a lock that the read-only inspection proved stale."""
    lock = _workspace_git_lock(root)
    if not lock.get("stale"):
        raise RuntimeError("De Git-vergrendeling is niet aantoonbaar verouderd.")
    lock_path = root / ".git" / "index.lock"
    try:
        lock_path.unlink()
    except OSError as error:
        raise RuntimeError("De verouderde Git-vergrendeling kon niet worden verwijderd.") from error
    return {"state": "free", "recovered": True}


def _loose_local_branch_analysis(root: Path) -> list[dict[str, object]]:
    """Assess every standalone local branch without making any change.

    The dashboard must distinguish an empty inventory from an inventory with
    branches that are deliberately retained.  Only entries marked removable
    may reach the destructive cleanup endpoint.
    """
    provider = GitProvider()
    expected_branch = PlatformConfiguration.load(root).workspace.default_branch
    try:
        status = provider.execute(root, "git", "status", "--porcelain", "--untracked-files=all")
        active = provider.execute(root, "git", "branch", "--show-current")
        if status.returncode or active.returncode or status.stdout.strip() or active.stdout.strip() != expected_branch:
            raise RuntimeError("De werkmap moet schoon en op main staan.")
        if provider.execute(root, "git", "fetch", "--prune", "origin").returncode:
            raise RuntimeError("Remote-branches konden niet veilig worden ververst.")
        divergence = provider.execute(root, "git", "rev-list", "--left-right", "--count", f"origin/{expected_branch}...{expected_branch}")
        if divergence.returncode or divergence.stdout.strip() != "0\t0":
            raise RuntimeError("main moet eerst met origin worden gesynchroniseerd.")
        worktrees = provider.execute(root, "git", "worktree", "list", "--porcelain")
        if worktrees.returncode:
            raise RuntimeError("Actieve Git-worktrees konden niet veilig worden gelezen.")
        active_worktree_branches = {
            line.removeprefix("branch refs/heads/")
            for line in worktrees.stdout.splitlines()
            if line.startswith("branch refs/heads/")
        }
        branches = provider.execute(root, "git", "for-each-ref", "--format=%(refname:short)", "refs/heads")
        if branches.returncode:
            raise RuntimeError("Lokale branches konden niet veilig worden gelezen.")
        analysis: list[dict[str, object]] = []
        for branch in sorted(name for name in branches.stdout.splitlines() if name and name != expected_branch):
            if branch in active_worktree_branches:
                continue
            remote = provider.execute(root, "git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
            if remote.returncode == 0:
                analysis.append({"name": branch, "reason": "remote_branch_exists", "removable": False})
                continue
            if remote.returncode != 1:
                raise RuntimeError(f"Remote-branch van {branch} kon niet veilig worden gecontroleerd.")
            comparison = provider.execute(root, "git", "diff", "--quiet", expected_branch, branch)
            if comparison.returncode == 0:
                analysis.append({"name": branch, "reason": "remote_absent_and_matches_main", "removable": True})
            elif comparison.returncode == 1 and _branch_is_verified_merged_into_main(root, expected_branch, branch, provider):
                analysis.append({"name": branch, "reason": "remote_absent_verified_merged_pull_request", "removable": True})
            elif comparison.returncode != 1:
                raise RuntimeError(f"Branch {branch} kon niet veilig worden vergeleken.")
            else:
                analysis.append({"name": branch, "reason": "content_differs_from_main", "removable": False})
    except OSError as error:
        raise RuntimeError("Lokale branch-opruiming is niet beschikbaar.") from error
    return analysis


def _stale_local_branch_candidates(root: Path) -> list[str]:
    """Return only analysis entries that are proven safe to remove."""
    return [
        str(entry["name"])
        for entry in _loose_local_branch_analysis(root)
        if entry.get("removable") is True and isinstance(entry.get("name"), str)
    ]


def _branch_is_verified_merged_into_main(
    root: Path, expected_branch: str, branch: str, provider: GitProvider
) -> bool:
    """Accept an older local head only when a merged PR proves it reached main."""
    try:
        remote = provider.execute(root, "git", "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
        if not match:
            return False
        payload = GitHubProvider().github(
            "pr", "list", "--repo", f"{match.group(1)}/{match.group(2)}", "--state", "merged", "--head", branch,
            "--json", "number,headRefName,headRefOid,mergeCommit", "--limit", "2",
        )
        pull_requests = json.loads(payload)
        if not isinstance(pull_requests, list):
            return False
        pull_request = next((item for item in pull_requests if isinstance(item, dict) and item.get("headRefName") == branch), None)
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        merge_commit = pull_request.get("mergeCommit") if isinstance(pull_request, dict) else None
        merge_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        local_head = provider.execute(root, "git", "rev-parse", "--verify", branch)
        if not isinstance(number, int) or not isinstance(merge_oid, str) or local_head.returncode or not local_head.stdout.strip():
            return False
        merged_into_main = provider.execute(root, "git", "merge-base", "--is-ancestor", merge_oid, expected_branch)
        return (
            merged_into_main.returncode == 0
            and _github_pull_request_contains_commit(f"{match.group(1)}/{match.group(2)}", number, local_head.stdout.strip())
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False


def _github_pull_request_contains_commit(repository: str, number: int, commit_sha: str) -> bool:
    """Use GitHub's immutable PR commit record when a deleted head is not local."""
    try:
        payload = GitHubProvider().github("pr", "view", str(number), "--repo", repository, "--json", "commits")
        pull_request = json.loads(payload)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    commits = pull_request.get("commits") if isinstance(pull_request, dict) else None
    return isinstance(commits, list) and any(
        isinstance(item, dict) and isinstance(item.get("oid"), str) and item["oid"] == commit_sha
        for item in commits
    )


def _github_pull_request_for_detached_commit(
    root: Path, repository: str, commit_sha: str, expected_branch: str, provider: GitProvider,
) -> dict[str, object] | None:
    """Return immutable PR evidence for one detached commit, if GitHub can prove it."""
    try:
        payload = json.loads(GitHubProvider().github("api", f"repos/{repository}/commits/{commit_sha}/pulls"))
        if not isinstance(payload, list):
            return None
        fallback: dict[str, object] | None = None
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("number"), int) or not isinstance(item.get("html_url"), str):
                continue
            state = "MERGED" if item.get("merged_at") else str(item.get("state") or "").upper()
            merge_oid = item.get("merge_commit_sha")
            verified = False
            if state == "MERGED" and isinstance(merge_oid, str) and merge_oid:
                merged = provider.execute(root, "git", "merge-base", "--is-ancestor", merge_oid, expected_branch)
                if merged.returncode not in {0, 1}:
                    continue
                verified = merged.returncode == 0
            evidence = {"number": item["number"], "url": item["html_url"], "state": state, "verified": verified}
            if verified:
                return evidence
            if fallback is None or (state == "OPEN" and fallback.get("state") != "OPEN"):
                fallback = evidence
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return None
    return fallback


def _stale_local_branch_preview(root: Path) -> dict[str, object]:
    analysis = _loose_local_branch_analysis(root)
    removable = [str(entry["name"]) for entry in analysis if entry.get("removable") is True]
    pull_requests = {
        branch: _stale_local_branch_pull_request(root, branch)
        for branch in removable
    }
    return {
        "branches": [
            {
                "name": str(entry["name"]),
                "reason": str(entry["reason"]),
                "removable": entry.get("removable") is True,
                **({"pull_request": pull_requests[str(entry["name"])]}
                   if entry.get("removable") is True and pull_requests[str(entry["name"])] else {}),
            }
            for entry in analysis
        ],
        "removable_branches": removable,
    }


def _stale_local_branch_pull_request(root: Path, branch: str) -> dict[str, object] | None:
    """Return a merged GitHub PR for an exact former head branch, if available.

    The cleanup decision remains entirely Git-based.  GitHub metadata only
    provides an optional operator link and cannot make a branch removable.
    """
    try:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        if remote.returncode:
            return None
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip())
        if not match:
            return None
        repository = f"{match.group(1)}/{match.group(2)}"
        payload = GitHubProvider().github(
            "pr", "list", "--repo", repository, "--state", "merged", "--head", branch,
            "--json", "number,url,headRefName", "--limit", "2",
        )
        pull_requests = json.loads(payload)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    if not isinstance(pull_requests, list):
        return None
    for pull_request in pull_requests:
        if not isinstance(pull_request, dict) or pull_request.get("headRefName") != branch:
            continue
        number, url = pull_request.get("number"), pull_request.get("url")
        if isinstance(number, int) and number > 0 and isinstance(url, str) and url.startswith("https://github.com/"):
            return {"number": number, "url": url}
    return None


def _workspace_open_pull_requests(root: Path) -> list[dict[str, object]] | None:
    """Return PR context, or ``None`` when GitHub cannot authoritatively answer."""
    try:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
        if not match:
            return None
        payload = GitHubProvider().github(
            "pr", "list", "--repo", f"{match.group(1)}/{match.group(2)}", "--state", "open",
            "--json", "number,title,url,headRefOid,headRefName,isDraft,mergeStateStatus,reviewDecision,reviews,statusCheckRollup", "--limit", "20",
        )
        candidates = json.loads(payload)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    if not isinstance(candidates, list):
        return None
    result: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        number, title, url, branch = candidate.get("number"), candidate.get("title"), candidate.get("url"), candidate.get("headRefName")
        if isinstance(number, int) and number > 0 and all(isinstance(value, str) for value in (title, url, branch)) and url.startswith("https://github.com/"):
            head_sha = candidate.get("headRefOid")
            failed_checks, checks_terminal = pr_check_repair_check_summary(candidate.get("statusCheckRollup"))
            authorization_requested = _owner_authorization_requested(candidate)
            repair_state = pr_check_repair_state(root, number, head_sha)
            repair_active = repair_state in {"QUEUED", "RUNNING"} or (
                repair_state == "SUBMITTED" and not checks_terminal
            )
            repair_completed_for_head = repair_state == "SUBMITTED" and checks_terminal
            result.append({
                "number": number,
                "title": title,
                "url": url,
                "branch": branch,
                "status": _open_pull_request_status(candidate),
                "owner_approval": _owner_approval_status(candidate, match.group(1)),
                "owner_authorization_requested": authorization_requested,
                "failed_checks": failed_checks,
                # This is an explicit, one-shot operator action for a
                # human-authored same-repository PR.  Endpoint admission
                # re-reads all GitHub evidence before any provider is used.
                "check_repair_available": bool(failed_checks) and checks_terminal and not authorization_requested and not repair_active and not pr_check_repair_attempted(root, number, head_sha),
                "check_repair_state": repair_state if repair_active else None,
                "check_repair_completed_for_head": repair_completed_for_head,
            })
    return result


def _owner_approval_status(pull_request: dict[str, object], repository_owner: str) -> str:
    """Project owner approval, including GitHub's exact-SHA authorization check."""
    # ``reviewDecision`` only represents GitHub reviews.  The policy gate for
    # high-risk work is published as a legacy commit StatusContext instead, so
    # it must take precedence over the optional-review fallback below.
    checks = pull_request.get("statusCheckRollup")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            name = str(check.get("name") or check.get("context") or "").casefold()
            if name != "owner authorization":
                continue
            state = str(check.get("state") or check.get("status") or "").upper()
            conclusion = str(check.get("conclusion") or "").upper()
            if state not in {"SUCCESS", "COMPLETED"} or conclusion not in {"", "SUCCESS", "NEUTRAL", "SKIPPED"}:
                return "pending"
            # This check exists only when the HIGH_RISK owner-authorization
            # gate applied to this exact commit.  A successful result is an
            # affirmative owner decision, not the absence of a requirement.
            # Do not fall through to GitHub's optional-review fallback: the
            # owner may have authorized with the dashboard control rather
            # than submitted a conventional PR review.
            return "approved"
    reviews = pull_request.get("reviews")
    if not isinstance(reviews, list) or not repository_owner:
        return "pending"
    owner_reviews = [
        review for review in reviews
        if isinstance(review, dict) and isinstance(review.get("author"), dict)
        and str(review["author"].get("login") or "").casefold() == repository_owner.casefold()
    ]
    if not owner_reviews:
        # GitHub reports ``null`` when its branch policy does not require a
        # review.  Preserve ``pending`` for an unavailable projection, but do
        # not turn the absence of an optional owner review into a false wait.
        if "reviewDecision" in pull_request and str(pull_request.get("reviewDecision") or "").upper() != "REVIEW_REQUIRED":
            return "not_required"
        return "pending"
    latest = max(owner_reviews, key=lambda review: str(review.get("submittedAt") or ""))
    state = str(latest.get("state") or "").upper()
    if state == "APPROVED":
        return "approved"
    if state == "CHANGES_REQUESTED":
        return "changes_requested"
    return "pending"


def _owner_authorization_requested(pull_request: dict[str, object]) -> bool:
    """Whether GitHub has requested exact-SHA HIGH_RISK authorization."""
    checks = pull_request.get("statusCheckRollup")
    if not isinstance(checks, list):
        return False
    return any(
        isinstance(check, dict)
        and str(check.get("name") or check.get("context") or "").casefold() == "owner authorization"
        and str(check.get("state") or check.get("status") or "").upper() == "FAILURE"
        for check in checks
    )


class OwnerAuthorizationRequestError(RuntimeError):
    """The exact-SHA Owner Authorization workflow cannot safely be dispatched."""


def _request_owner_authorization(root: Path, pull_request_number: int) -> dict[str, object]:
    """Dispatch the canonical owner workflow for the current HIGH_RISK PR SHA.

    The browser supplies only a PR number. Repository, target branch and SHA
    are read afresh from GitHub so this endpoint cannot authorize a stale or
    caller-selected commit. The workflow remains the sole publisher of the
    ``Owner Authorization`` status.
    """
    if isinstance(pull_request_number, bool) or pull_request_number < 1:
        raise OwnerAuthorizationRequestError("owner_authorization_invalid_request")
    try:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
        if not match:
            raise OwnerAuthorizationRequestError("owner_authorization_unavailable")
        repository = f"{match.group(1)}/{match.group(2)}"
        payload = GitHubProvider().github(
            "pr", "view", str(pull_request_number), "--repo", repository,
            "--json", "number,state,headRefOid,baseRefName,statusCheckRollup",
        )
        pull_request = json.loads(payload)
    except OwnerAuthorizationRequestError:
        raise
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        raise OwnerAuthorizationRequestError("owner_authorization_unavailable") from error
    if not isinstance(pull_request, dict) or pull_request.get("number") != pull_request_number:
        raise OwnerAuthorizationRequestError("owner_authorization_unavailable")
    candidate_sha = pull_request.get("headRefOid")
    target_branch = pull_request.get("baseRefName")
    if (
        str(pull_request.get("state") or "").upper() != "OPEN"
        or not isinstance(candidate_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha)
        or not isinstance(target_branch, str)
        or not target_branch
        or not _owner_authorization_requested(pull_request)
    ):
        raise OwnerAuthorizationRequestError("owner_authorization_not_requested")
    checks = pull_request.get("statusCheckRollup")
    trusted_delivery_passed = isinstance(checks, list) and any(
        isinstance(check, dict)
        and str(check.get("name") or "") == "Trusted Delivery qualification / Qualify trusted delivery"
        and str(check.get("status") or "").upper() == "COMPLETED"
        and str(check.get("conclusion") or "").upper() == "SUCCESS"
        for check in checks
    )
    if not trusted_delivery_passed:
        raise OwnerAuthorizationRequestError("owner_authorization_qualification_pending")
    try:
        GitHubProvider().github(
            "workflow", "run", "owner-authorization.yml", "--repo", repository,
            "-f", f"repository={repository}", "-f", f"pr_number={pull_request_number}",
            "-f", f"candidate_sha={candidate_sha}", "-f", f"branch={target_branch}",
        )
    except RuntimeError as error:
        raise OwnerAuthorizationRequestError("owner_authorization_dispatch_failed") from error
    return {"queued": True, "pull_request": pull_request_number}


def _open_pull_request_status(pull_request: dict[str, object]) -> str:
    """Classify GitHub's read-only PR check projection for dashboard display.

    An unavailable or incomplete check projection deliberately stays
    ``waiting_for_checks``: the dashboard must never imply that a PR is ready
    to review or merge until GitHub has reported enough terminal evidence.
    """
    if pull_request.get("isDraft") is True:
        return "draft"
    merge_state = str(pull_request.get("mergeStateStatus") or "").upper()
    if merge_state == "BEHIND":
        return "branch_update_required"
    if merge_state in {"DIRTY", "BLOCKED"}:
        return "issues"
    review_decision = str(pull_request.get("reviewDecision") or "").upper()
    if review_decision == "CHANGES_REQUESTED":
        return "issues"
    check_rollup = pull_request.get("statusCheckRollup")
    if not isinstance(check_rollup, list):
        return "waiting_for_checks"
    failed_conclusions = {"ACTION_REQUIRED", "CANCELLED", "FAILURE", "STARTUP_FAILURE", "TIMED_OUT"}
    successful_conclusions = {"NEUTRAL", "SKIPPED", "SUCCESS"}
    conclusions: list[str] = []
    for check in check_rollup:
        if not isinstance(check, dict):
            return "waiting_for_checks"
        # Check runs expose ``status`` + ``conclusion``; legacy status
        # contexts expose their terminal result only as ``state``.
        conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in failed_conclusions:
            return "issues"
        if conclusion not in successful_conclusions or (status and status != "COMPLETED"):
            return "waiting_for_checks"
        conclusions.append(conclusion)
    if review_decision == "REVIEW_REQUIRED":
        return "ready_for_review"
    # A repository without required checks can still be ready to merge when
    # GitHub explicitly reports its merge state as clean. An absent/unknown
    # state remains fail-closed rather than presented as a false green result.
    return "ready_to_merge" if merge_state == "CLEAN" else "waiting_for_checks"


def _cleanup_stale_local_branches(root: Path, expected_branches: list[str]) -> dict[str, object]:
    """Remove exactly the stale branch set which the operator just reviewed."""
    if not expected_branches or any(not isinstance(branch, str) or not branch for branch in expected_branches):
        raise RuntimeError("De geselecteerde branches zijn ongeldig.")
    if len(expected_branches) != len(set(expected_branches)):
        raise RuntimeError("De geselecteerde branches zijn ongeldig.")
    candidates = _stale_local_branch_candidates(root)
    if sorted(expected_branches) != candidates:
        raise RuntimeError("De branchscan is gewijzigd; controleer de lijst opnieuw.")
    provider = GitProvider()
    removed: list[str] = []
    try:
        for branch in candidates:
            if provider.execute(root, "git", "branch", "-D", "--", branch).returncode:
                raise RuntimeError(f"Branch {branch} kon niet veilig worden verwijderd.")
            removed.append(branch)
    except OSError as error:
        raise RuntimeError("Lokale branch-opruiming is niet beschikbaar.") from error
    return {"removed": removed, "removed_count": len(removed)}


def _worktree_removal_analysis(root: Path) -> dict[str, object]:
    """Read and explain the fail-closed removal decision for every worktree."""
    provider = GitProvider()
    expected_branch = PlatformConfiguration.load(root).workspace.default_branch
    root = root.resolve()
    try:
        root_status = provider.execute(root, "git", "status", "--porcelain", "--untracked-files=all")
        active = provider.execute(root, "git", "branch", "--show-current")
        if provider.execute(root, "git", "fetch", "--prune", "origin").returncode:
            raise RuntimeError("Remote-branches konden niet veilig worden ververst.")
        divergence = provider.execute(root, "git", "rev-list", "--left-right", "--count", f"origin/{expected_branch}...{expected_branch}")
        observed = provider.execute(root, "git", "worktree", "list", "--porcelain")
        if observed.returncode:
            raise RuntimeError("Actieve Git-worktrees konden niet veilig worden gelezen.")
    except OSError as error:
        raise RuntimeError("Lokale worktree-opruiming is niet beschikbaar.") from error

    root_ready = (
        root_status.returncode == 0
        and active.returncode == 0
        and not root_status.stdout.strip()
        and active.stdout.strip() == expected_branch
        and divergence.returncode == 0
        and divergence.stdout.strip() == "0\t0"
    )
    root_reason = "main_ready" if root_ready else "main_not_ready"
    try:
        remote = provider.execute(root, "git", "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
        payload = GitHubProvider().github(
            "pr", "list", "--repo", f"{match.group(1)}/{match.group(2)}", "--state", "all",
            "--json", "number,url,headRefName,headRefOid,state,mergedAt,mergeCommit", "--limit", "100",
        ) if match else "[]"
        pull_requests = json.loads(payload)
        if not isinstance(pull_requests, list):
            raise ValueError
        pull_requests_by_branch = {
            item["headRefName"]: item for item in pull_requests
            if isinstance(item, dict) and isinstance(item.get("headRefName"), str)
            and isinstance(item.get("number"), int) and isinstance(item.get("url"), str)
            and isinstance(item.get("state"), str)
        }
        github_available = match is not None
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        pull_requests_by_branch, github_available = {}, False

    worktrees: list[dict[str, object]] = []
    record: dict[str, str] = {}
    for line in [*str(observed.stdout or "").splitlines(), ""]:
        if line:
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                record[key] = value
            elif key == "detached":
                record["detached"] = "true"
            continue
        path = record.get("worktree", "").strip()
        branch = record.get("branch", "").removeprefix("refs/heads/").strip()
        head = record.get("HEAD", "").strip()
        detached = record.get("detached") == "true"
        record = {}
        if not path or (not branch and not detached):
            continue
        worktree = Path(path)
        if branch == expected_branch or worktree.resolve() == root:
            worktrees.append({"path": str(worktree), "branch": branch or None, "head": head or None, "decision": "baseline", "reason": "main_baseline", "removable": False})
            continue
        status = provider.execute(worktree, "git", "status", "--porcelain", "--untracked-files=all")
        if detached:
            merged_into_main = provider.execute(root, "git", "merge-base", "--is-ancestor", head, expected_branch)
            if status.returncode not in {0, 1} or merged_into_main.returncode not in {0, 1}:
                raise RuntimeError("Een losgekoppelde worktree kon niet veilig worden gecontroleerd.")
            pull_request = _github_pull_request_for_detached_commit(
                root, f"{match.group(1)}/{match.group(2)}", head, expected_branch, provider,
            ) if github_available and match and head else None
            verified_pull_request = isinstance(pull_request, dict) and pull_request.get("verified") is True
            removable = root_ready and status.returncode == 0 and not status.stdout.strip() and (
                merged_into_main.returncode == 0 or verified_pull_request
            )
            if removable:
                reason = "safe_to_remove"
            elif status.returncode != 0 or status.stdout.strip():
                reason = "worktree_dirty"
            elif isinstance(pull_request, dict) and pull_request.get("state") == "OPEN":
                reason = "detached_head_pull_request_open"
            elif not root_ready:
                reason = root_reason
            elif not github_available:
                reason = "github_unavailable"
            else:
                reason = "detached_head_unverified"
            worktrees.append({
                "path": str(worktree), "branch": None, "head": head, "detached": True,
                "decision": "removable" if removable else "keep", "reason": reason, "removable": removable,
                **({"pull_request": {key: pull_request[key] for key in ("number", "url", "state")}} if isinstance(pull_request, dict) else {}),
            })
            continue
        remote_branch = provider.execute(root, "git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        comparison = provider.execute(root, "git", "diff", "--quiet", expected_branch, branch)
        if status.returncode not in {0, 1} or remote_branch.returncode not in {0, 1} or comparison.returncode not in {0, 1}:
            raise RuntimeError("Een lokale worktree kon niet veilig worden gecontroleerd.")
        pull_request = pull_requests_by_branch.get(branch)
        pr_state = pull_request.get("state") if isinstance(pull_request, dict) else None
        pr_head = pull_request.get("headRefOid") if isinstance(pull_request, dict) else None
        merge_commit = pull_request.get("mergeCommit") if isinstance(pull_request, dict) else None
        merge_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        squash_merged_head = False
        if pr_state == "MERGED" and isinstance(pr_head, str) and pr_head == head and isinstance(merge_oid, str):
            merged = provider.execute(root, "git", "merge-base", "--is-ancestor", merge_oid, expected_branch)
            if merged.returncode not in {0, 1}:
                raise RuntimeError("De squash-merge kon niet veilig worden gecontroleerd.")
            squash_merged_head = merged.returncode == 0
        elif pr_state == "MERGED" and isinstance(merge_oid, str) and isinstance(pull_request.get("number"), int) and match:
            merged = provider.execute(root, "git", "merge-base", "--is-ancestor", merge_oid, expected_branch)
            if merged.returncode not in {0, 1}:
                raise RuntimeError("De squash-merge kon niet veilig worden gecontroleerd.")
            squash_merged_head = merged.returncode == 0 and _github_pull_request_contains_commit(
                f"{match.group(1)}/{match.group(2)}", pull_request["number"], head,
            )
        equivalent_or_verified_squash = (
            comparison.returncode == 0 and pr_state in {"MERGED", "CLOSED"}
        ) or squash_merged_head
        removable = root_ready and status.returncode == 0 and not status.stdout.strip() and remote_branch.returncode == 1 and github_available and equivalent_or_verified_squash
        if removable:
            reason = "safe_to_remove"
        elif not root_ready:
            reason = root_reason
        elif status.returncode != 0 or status.stdout.strip():
            reason = "worktree_dirty"
        elif remote_branch.returncode == 0:
            reason = "remote_branch_present"
        elif comparison.returncode == 1 and not squash_merged_head:
            reason = "differs_from_main"
        elif not github_available:
            reason = "github_unavailable"
        elif pr_state == "OPEN":
            reason = "pull_request_open"
        else:
            reason = "pull_request_unverified"
        worktrees.append({
            "path": str(worktree), "branch": branch, "decision": "removable" if removable else "keep",
            "reason": reason, "removable": removable,
            **({"pull_request": {"number": pull_request["number"], "url": pull_request["url"], "state": pr_state}} if isinstance(pull_request, dict) else {}),
        })
    return {"available": True, "worktrees": worktrees}


def _safe_worktree_removal_candidates(root: Path) -> list[dict[str, object]]:
    """Return the freshly analysed, GitHub-verified worktrees safe to remove."""
    analysis = _worktree_removal_analysis(root)
    return [
        {key: item[key] for key in ("path", "branch", "head") if item.get(key) is not None}
        for item in analysis["worktrees"]
        if isinstance(item, dict) and item.get("removable") is True
        and isinstance(item.get("path"), str) and (isinstance(item.get("branch"), str) or isinstance(item.get("head"), str))
    ]


def _remove_safe_worktree(root: Path, worktree_path: str, branch: str | None = None, head: str | None = None) -> dict[str, object]:
    """Remove exactly one freshly verified stale worktree, never its branch."""
    if not isinstance(worktree_path, str) or not worktree_path or (not isinstance(branch, str) and not isinstance(head, str)):
        raise RuntimeError("De geselecteerde worktree is ongeldig.")
    selected = next(
        (
            candidate
            for candidate in _safe_worktree_removal_candidates(root)
            if candidate.get("path") == worktree_path and candidate.get("branch") == branch and candidate.get("head") == head
        ),
        None,
    )
    if selected is None:
        raise RuntimeError("De worktree-controle is gewijzigd; controleer de lijst opnieuw.")
    try:
        # The request path is only a selector.  Git receives the independently
        # revalidated path returned by the current worktree analysis.
        verified_path = Path(selected["path"]).resolve(strict=True)
        GitProvider().command(root, "git", "worktree", "remove", "--", str(verified_path))
    except OSError as error:
        raise RuntimeError("De worktree kon niet veilig worden verwijderd.") from error
    if isinstance(selected.get("branch"), str):
        return {"removed_worktree": str(verified_path), "branch": selected["branch"], "branch_pending_cleanup": True}
    return {"removed_worktree": str(verified_path), "head": selected.get("head")}


def _open_worktree_in_finder(root: Path, worktree_path: str) -> dict[str, str]:
    """Open only a currently registered local worktree in macOS Finder."""
    if sys.platform != "darwin" or not isinstance(worktree_path, str) or not worktree_path:
        raise RuntimeError("De lokale worktreemap kan niet veilig worden geopend.")
    try:
        # The HTTP value only selects a current Git worktree.  Finder receives
        # the independently registered path, never the request value itself.
        requested = _registered_worktree_path(root, worktree_path).resolve(strict=True)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("De geselecteerde map is geen actuele lokale worktree.") from error
    except OSError as error:
        raise RuntimeError("De lokale worktreemap kan niet veilig worden geopend.") from error
    try:
        outcome = LocalProcessProvider().execute(root, ("open", str(requested)))
    except OSError as error:
        raise RuntimeError("Finder kon de lokale worktreemap niet openen.") from error
    if outcome.returncode:
        raise RuntimeError("Finder kon de lokale worktreemap niet openen.")
    return {"opened_worktree": str(requested)}


def _approved_local_directories(root: Path) -> dict[str, Path]:
    """Map current, locally derived directory labels to Finder-safe paths."""
    candidates = {
        root,
        root / ".engineering",
        Path.home() / "Library" / "LaunchAgents",
        engineering_platform_codex_cli_prefix(),
    }
    try:
        candidates.add(PlatformConfiguration.load(root).resolver(root).resolve_runtime_prompt_transport().inbox)
    except (EngineeringStorageError, OSError, TypeError, ValueError, KeyError):
        pass
    try:
        candidates.update(
            Path(str(item["path"]))
            for item in _workspace_worktrees(root).get("worktrees", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )
    except OSError:
        pass
    approved: dict[str, Path] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir():
            # Retain both server-derived spellings. macOS commonly exposes
            # ``/tmp`` while canonical resolution yields ``/private/tmp``.
            approved[str(candidate)] = resolved
            approved[str(resolved)] = resolved
    return approved


def _open_local_directory_in_finder(root: Path, directory_path: str) -> dict[str, str]:
    """Open one current, server-approved local directory in macOS Finder."""
    if sys.platform != "darwin" or not isinstance(directory_path, str) or not directory_path:
        raise RuntimeError("De lokale map kan niet veilig worden geopend.")
    # Treat the HTTP value only as a selector.  The filesystem path passed to
    # Finder is derived from the server-approved projection, never parsed from
    # the request.
    requested = _approved_local_directories(root).get(directory_path)
    if requested is None:
        raise RuntimeError("De geselecteerde map is niet beschikbaar in dit dashboard.")
    try:
        outcome = LocalProcessProvider().execute(root, ("open", str(requested)))
    except OSError as error:
        raise RuntimeError("Finder kon de lokale map niet openen.") from error
    if outcome.returncode:
        raise RuntimeError("Finder kon de lokale map niet openen.")
    return {"opened_directory": str(requested)}


def _codex_process_metrics(root: Path) -> bytes:
    """Measure only the process group explicitly recorded by the Execution Host."""
    try:
        runner = json.loads((root / ".engineering" / "status" / "runner_process.json").read_text(encoding="utf-8"))
        live = json.loads((root / ".engineering" / "status" / "current.json").read_text(encoding="utf-8"))
        process_group = runner.get("process_group") if runner.get("run_id") == live.get("run_id") else None
        runner_pid = runner.get("pid") if runner.get("run_id") == live.get("run_id") else None
    except (OSError, json.JSONDecodeError):
        process_group, runner_pid = None, None
    if not isinstance(process_group, int) or process_group <= 0 or not isinstance(runner_pid, int) or runner_pid <= 0:
        return json.dumps({"process_count": 0, "cpu_percent": 0, "gpu_status": "Niet beschikbaar: geen actieve Execution Host-runner."}, separators=(",", ":")).encode()
    try:
        observed = LocalProcessProvider().execute(Path.cwd(), ("ps", "-axo", "pid=,pgid=,pcpu=,command="))
    except OSError:
        observed = None
    processes: list[dict[str, int | float]] = []
    owner_seen = False
    if observed and observed.returncode == 0:
        for line in observed.stdout.splitlines():
            parts = line.strip().split(maxsplit=3)
            if len(parts) != 4:
                continue
            try:
                pid, group, cpu = int(parts[0]), int(parts[1]), float(parts[2])
                owner_seen = owner_seen or (pid == runner_pid and group == process_group)
                if group == process_group:
                    processes.append({"pid": pid, "cpu_percent": cpu})
            except ValueError:
                continue
    if not owner_seen:
        processes = []
    return json.dumps(
        {
            "process_count": len(processes),
            "cpu_percent": round(sum(item["cpu_percent"] for item in processes), 1),
            "gpu_status": "Niet beschikbaar: Execution Host-verwerking draait extern.",
        },
        separators=(",", ":"),
    ).encode()


def _report_for_run(root: Path, run_id: str | None) -> bytes:
    """Return report evidence only for the exact displayed terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    try:
        indexed_report = report_for_prompt_history(root, run_id)
        if indexed_report is not None:
            return indexed_report
        reports = list((root / ".engineering" / "reports").glob(f"*_{run_id}.md"))
        report = max(reports, key=lambda path: path.stat().st_mtime) if reports else None
        return report.read_bytes() if report else b""
    except OSError:
        return b""


def _reviewer_agents_for_run(root: Path, run_id: str | None) -> bytes:
    """Project recorded specialist reviewer agents from the exact run report.

    Reviewer agents are independent, read-only advisory calls.  The dashboard
    deliberately derives this view from the immutable terminal report instead
    of inferring generic Codex sub-agents that the platform does not record.
    """
    try:
        report = _report_for_run(root, run_id).decode("utf-8")
    except UnicodeDecodeError:
        return b"[]"
    section = re.search(
        r"^## Reviewer Findings\s*$\n(?P<body>.*?)(?=^##\s|\Z)", report, re.MULTILINE | re.DOTALL
    )
    if section is None:
        return b"[]"

    records: list[dict[str, object]] = []
    for block in re.split(r"(?=^- Reviewer: )", section.group("body"), flags=re.MULTILINE):
        reviewer = re.search(r"^- Reviewer:\s*(.+)$", block, re.MULTILINE)
        if reviewer is None:
            continue

        def field(name: str) -> str | None:
            match = re.search(rf"^  - {re.escape(name)}:\s*(.+)$", block, re.MULTILINE)
            if match is None:
                return None
            return " ".join(match.group(1).split())[:180]

        accepted = field("Accepted recommendations")
        records.append(
            {
                "reviewer": " ".join(reviewer.group(1).split())[:80],
                "capability": field("Capability") or "engineering",
                "selected_because": field("Selected because") or "Niet vastgelegd.",
                "accepted_recommendations": int(accepted) if accepted and accepted.isdigit() else 0,
                "status": "Uitgevoerd",
            }
        )
        if len(records) == 12:
            break
    return json.dumps(records, separators=(",", ":")).encode()


def _report_analysis_for_run(root: Path, run_id: str | None) -> bytes:
    """Return advisory analysis only when it belongs to the displayed terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    try:
        return (root / ".engineering" / "report-analysis" / f"{run_id}.md").read_bytes()
    except OSError:
        return b""


def _report_analysis_available_for_run(root: Path, run_id: str | None) -> bool:
    """Return whether the displayed terminal run has an advisory analysis."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return False
    return (root / ".engineering" / "report-analysis" / f"{run_id}.md").is_file()


def _report_analysis_processing_status(root: Path, run_id: str | None) -> str | None:
    """Read the fixed advisory-processing state without exposing provider diagnostics."""
    analysis = _report_analysis_for_run(root, run_id).decode("utf-8", errors="replace")
    match = re.search(r"(?m)^- Status: `([a-z_]+)`$", analysis)
    return match.group(1) if match else None


def _retry_report_analysis(root: Path, run_id: object) -> bytes:
    """Regenerate only a retryable advisory analysis for its exact terminal report."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        raise ValueError("Ongeldige uitvoeringsreferentie voor AI-analyse.")
    if _report_analysis_processing_status(root, run_id) not in RETRYABLE_REPORT_ANALYSIS_STATUSES:
        raise ValueError("Deze AI-analyse hoeft niet opnieuw te worden gegenereerd.")
    report = report_path_for_prompt_history(root, run_id)
    if report is None:
        raise ValueError("Engineeringrapport is niet beschikbaar voor deze uitvoering.")
    with _REPORT_ANALYSIS_RETRY_LOCK:
        if run_id in _REPORT_ANALYSIS_RETRY_RUNS:
            raise RuntimeError("AI-analyse wordt al opnieuw gegenereerd.")
        _REPORT_ANALYSIS_RETRY_RUNS.add(run_id)
    try:
        return analyze_terminal_report(root, run_id, report).read_bytes()
    finally:
        with _REPORT_ANALYSIS_RETRY_LOCK:
            _REPORT_ANALYSIS_RETRY_RUNS.discard(run_id)


def _current_codex_log(root: Path) -> bytes:
    """Return the diagnostic for the exact run currently shown by the dashboard."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"Geen Codex CLI-diagnose beschikbaar voor de huidige uitvoering."
    try:
        return (root / ".engineering" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"Geen Codex CLI-diagnose beschikbaar voor de huidige uitvoering."


def _last_executed_codex_log(root: Path) -> bytes:
    """Return only the log bound to the latest completed or failed Inbox run."""
    try:
        run_id = json.loads((root / ".engineering" / "status" / "status.json").read_text(encoding="utf-8")).get("last_executed_run")
    except (OSError, json.JSONDecodeError):
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"Geen Codex CLI-diagnose beschikbaar voor de laatst uitgevoerde uitvoering."
    try:
        return (root / ".engineering" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"Geen Codex CLI-diagnose beschikbaar voor de laatst uitgevoerde uitvoering."


def _codex_usage(root: Path) -> bytes:
    """Return usage only when it is bound to the currently displayed run."""
    try:
        status = json.loads(_status(root))
        recorded = json.loads((root / ".engineering" / "status" / "codex_usage.json").read_text(encoding="utf-8"))
        run_id = recorded.get("run_id")
        usage = recorded.get("usage")
    except (OSError, json.JSONDecodeError):
        return b"{}"
    displayed_run = status.get("run_id") or status.get("last_executed_run")
    if run_id != displayed_run or not isinstance(usage, dict):
        return b"{}"
    allowed = {key: value for key, value in usage.items() if isinstance(key, str) and isinstance(value, (int, float, str))}
    return json.dumps(allowed, separators=(",", ":")).encode()


def _codex_usage_for_run(root: Path, run_id: str | None) -> bytes:
    """Return CLI usage only when it belongs to the named terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"{}"
    try:
        recorded = json.loads(
            (root / ".engineering" / "status" / "codex_usage.json").read_text(encoding="utf-8")
        )
        usage = recorded.get("usage")
    except (OSError, json.JSONDecodeError):
        return b"{}"
    if recorded.get("run_id") != run_id or not isinstance(usage, dict):
        return b"{}"
    allowed = {
        key: value
        for key, value in usage.items()
        if isinstance(key, str) and isinstance(value, (int, float, str))
    }
    return json.dumps(allowed, separators=(",", ":")).encode()


def _completion_commits(root: Path) -> bytes:
    """Return only recorded commit evidence for a completed displayed run."""
    try:
        status = json.loads(_status(root))
        if status.get("current_phase") != "COMPLETE":
            return b"{}"
        run_id = status.get("run_id")
        if not isinstance(run_id, str):
            return b"{}"
        checkpoint = _canonical_checkpoint(root, run_id)
    except (OSError, json.JSONDecodeError):
        return b"{}"
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    commits = {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}
    return json.dumps(commits, separators=(",", ":")).encode()


def _commits_for_run(root: Path, run_id: str | None) -> dict[str, str]:
    """Return commit evidence owned by one exact terminal execution."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return {}
    checkpoint = _canonical_checkpoint(root, run_id)
    if not checkpoint:
        return {}
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    return {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}


def _commit_timeline_for_run(root: Path, run_id: str | None) -> list[dict[str, str]]:
    """Project only strict, checkpoint-owned verified commit events.

    This is intentionally a read-only projection: old or malformed
    checkpoint values never become dashboard evidence.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return []
    checkpoint = _canonical_checkpoint(root, run_id)
    raw = checkpoint.get("commit_evidence")
    if not isinstance(raw, list):
        return []
    events: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        phase, observed_at, commit_sha, description = (
            item.get("phase"), item.get("observed_at"), item.get("commit_sha"), item.get("description"),
        )
        if not is_valid_commit_evidence_record(item):
            continue
        events.append({
            "phase": phase,
            "observed_at": observed_at,
            "commit_sha": commit_sha,
            "description": description,
        })
    return sorted(events, key=lambda item: item["observed_at"])


def _last_executed_commits(root: Path) -> bytes:
    """Return commit evidence bound to the most recent completed run only."""
    try:
        status = json.loads(_status(root))
        run_id = status.get("last_executed_run")
        if status.get("last_executed_phase") != "COMPLETE":
            return b"{}"
    except json.JSONDecodeError:
        return b"{}"
    return json.dumps(_commits_for_run(root, run_id), separators=(",", ":")).encode()


def _last_executed_agent_execution(root: Path, run_id: str | None) -> bytes:
    """Return run-bound AI timing and terminal timestamp evidence."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"{}"
    result: dict[str, float] = {}
    checkpoint = _canonical_checkpoint(root, run_id)
    seconds = checkpoint.get("agent_execution_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds <= 86_400:
        result["seconds"] = float(seconds)
    try:
        timing = execution_timing(root, run_id)
        total = timing.get("total_execution_seconds")
        finished_at = timing.get("finished_at")
    except Exception:
        total = finished_at = None
    if isinstance(total, (int, float)) and not isinstance(total, bool) and 0 <= total <= 86_400:
        result["total_seconds"] = float(total)
    if isinstance(finished_at, str):
        result["finished_at"] = finished_at
    return json.dumps(result, separators=(",", ":")).encode()


def _last_executed_runtime_metadata(root: Path, run_id: str | None) -> bytes:
    """Project only report-bound runtime provenance for the displayed run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"{}"
    report = _report_for_run(root, run_id)
    try:
        text = report.decode("utf-8")
    except UnicodeDecodeError:
        return b"{}"
    fields = {
        "runtime_provider": "Runtime Provider",
        "model": "AI Model",
        "reasoning_profile": "Reasoning Profile",
        "configuration_profile": "Configuration Profile",
        "codex_cli_version": "Codex CLI Version",
        "codex_cli_installation_path": "Codex CLI Installation Path",
    }
    metadata: dict[str, str] = {}
    for key, label in fields.items():
        match = re.search(rf"^- {re.escape(label)}: `([^`\n]{{1,120}})`$", text, re.MULTILINE)
        if match:
            metadata[key] = match.group(1)
    return json.dumps(metadata, separators=(",", ":")).encode()


def _prompt_started(root: Path) -> bytes:
    """Return the recorded Inbox start time for the run currently displayed."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str):
        return b"{}"
    for record in (root / ".engineering" / "inbox-processing").glob("*/job.json"):
        try:
            job = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("run_id") == run_id and isinstance(job.get("received_at"), str):
            return json.dumps({"started_at": job["received_at"]}, separators=(",", ":")).encode()
    return b"{}"


def _build_commit(root: Path) -> str:
    """Return the local checked-out revision for read-only dashboard identification."""
    try:
        observed = GitProvider().execute(root, "git", "rev-parse", "--short=12", "HEAD")
    except OSError:
        return "onbekend"
    return observed.stdout.strip() if observed.returncode == 0 else "onbekend"


def _tracked_file_count(root: Path) -> str:
    """Return the recursive count of files tracked by the workspace Git repository."""
    try:
        observed = GitProvider().execute(root, "git", "ls-files", "-z")
    except OSError:
        return "Niet beschikbaar"
    if observed.returncode != 0:
        return "Niet beschikbaar"
    separator = b"\0" if isinstance(observed.stdout, bytes) else "\0"
    return str(sum(1 for path in observed.stdout.split(separator) if path))


def _workspace_free_disk_space(root: Path) -> str:
    """Return free space on the volume that contains the workspace."""
    try:
        free_gigabytes = shutil.disk_usage(root).free / (1024**3)
    except OSError:
        return "Niet beschikbaar"
    return f"{free_gigabytes:.1f} GB"


def _provider_login_status(root: Path) -> dict[str, dict[str, str]]:
    """Dashboard projection of the shared token-free provider readiness check."""
    global _provider_login_active, _provider_login_started_at
    statuses = provider_readiness_status(root)
    with _provider_login_lock:
        active = _provider_login_active
        expired = time.monotonic() - _provider_login_started_at >= _PROVIDER_LOGIN_TIMEOUT_SECONDS
        if active and (expired or statuses.get(active.lower(), {}).get("state") == "READY"):
            _provider_login_active = None
            _provider_login_started_at = 0.0
    return statuses


def _start_provider_login(root: Path, provider: str) -> None:
    """Open one explicit interactive login in Terminal; no credential crosses EP."""
    commands = {
        "CODEX": (CodexCliProvider()._executable, "login", "--device-auth"),
        "GITHUB": ("gh", "auth", "login", "--hostname", "github.com", "--web"),
    }
    global _provider_login_active, _provider_login_started_at
    command = commands.get(provider)
    if command is None:
        raise ValueError("Unsupported provider login request.")
    if provider == "CODEX" and not CodexCliProvider().status().qualified:
        raise ValueError("Codex CLI is not installed.")
    if provider == "GITHUB" and shutil.which("gh") is None:
        raise ValueError("GitHub CLI is not installed.")
    if sys.platform != "darwin":
        raise ValueError("Interactive provider login is supported from the local macOS dashboard only.")
    with _provider_login_lock:
        if _provider_login_active and time.monotonic() - _provider_login_started_at < _PROVIDER_LOGIN_TIMEOUT_SECONDS:
            raise ValueError("Another provider sign-in is already in progress.")
    shell_command = "exec " + " ".join(shlex.quote(part) for part in command)
    apple_script = f'tell application "Terminal" to do script {json.dumps(shell_command)}'
    completed = LocalProcessProvider().execute(root, ("/usr/bin/osascript", "-e", apple_script))
    if completed.returncode:
        raise ValueError("Provider login window could not be opened.")
    with _provider_login_lock:
        _provider_login_active = provider
        _provider_login_started_at = time.monotonic()


def _install_provider(root: Path, provider: str) -> None:
    """Install a missing CLI only after an explicit dashboard request."""
    if not _provider_install_lock.acquire(blocking=False):
        raise ValueError("Another provider installation is already in progress.")
    try:
        if _execution_active(root):
            raise ValueError("Provider installation is unavailable while an execution is active.")
        if provider == "CODEX":
            npm = _npm_executable()
            if npm is None:
                raise ValueError("npm is required to install Codex CLI.")
            latest = LocalProcessProvider().execute(root, (npm, "view", CODEX_CLI_PACKAGE, "version", "--json"))
            version = _codex_cli_version(json.loads(latest.stdout)) if latest.returncode == 0 else None
            if version is None:
                raise ValueError("Codex CLI version could not be verified.")
            completed = LocalProcessProvider().execute(root, (npm, "install", "--global", "--prefix", str(engineering_platform_codex_cli_prefix()), f"{CODEX_CLI_PACKAGE}@{version}"))
            verification = CodexCliProvider().command("--version")
            key = "codex"
        elif provider == "GITHUB":
            brew = shutil.which("brew")
            if brew is None:
                raise ValueError("GitHub CLI installation requires Homebrew on this host.")
            completed = LocalProcessProvider().execute(root, (brew, "install", "gh"))
            verification = LocalProcessProvider().execute(root, ("gh", "--version"))
            key = "github"
        else:
            raise ValueError("Unsupported provider installation request.")
        if completed.returncode or verification.returncode or _provider_login_status(root).get(key, {}).get("state") == "UNAVAILABLE":
            raise ValueError("Provider installation could not be verified.")
    finally:
        _provider_install_lock.release()


def _logout_provider(root: Path, provider: str) -> None:
    """Remove one locally stored provider session; never expose credentials."""
    if provider == "CODEX":
        completed = CodexCliProvider().command("logout")
    elif provider == "GITHUB":
        process = LocalProcessProvider()
        account = process.execute(root, ("gh", "api", "user", "--jq", ".login"))
        username = account.stdout.strip()
        if account.returncode or not username or not re.fullmatch(r"[A-Za-z0-9-]+", username):
            raise ValueError("GitHub session cannot be safely identified for logout.")
        completed = process.execute(root, ("gh", "auth", "logout", "--hostname", "github.com", "--user", username))
    else:
        raise ValueError("Unsupported provider logout request.")
    if completed.returncode:
        raise ValueError("Provider logout did not complete.")


def _workspace_git_projection(root: Path) -> dict[str, object]:
    """Return the small, read-only Git projection shown in Workspace."""
    unavailable = "Niet beschikbaar"
    try:
        provider = GitProvider()
        branch = provider.execute(root, "git", "branch", "--show-current")
        revisions = provider.execute(root, "git", "rev-parse", "HEAD", "origin/main")
    except OSError:
        return {
            "branch": unavailable,
            "commit": unavailable,
            "origin_main_commit": unavailable,
            "origin_main_available": False,
            "main_action_available": False,
            "branch_cleanup_available": False,
        }
    branch_name = branch.stdout.strip() if branch.returncode == 0 and branch.stdout.strip() else unavailable
    revision_lines = revisions.stdout.splitlines() if revisions.returncode == 0 else []
    commit = revision_lines[0][:12] if revision_lines else unavailable
    origin_main_commit = revision_lines[1][:12] if len(revision_lines) == 2 else unavailable
    origin_main_available = len(revision_lines) == 2
    return {
        "branch": branch_name,
        "commit": commit,
        "origin_main_commit": origin_main_commit,
        "origin_main_available": origin_main_available,
        "main_action_available": origin_main_available and commit != origin_main_commit,
        "branch_cleanup_available": branch_name == "main",
    }


def _workspace_worktrees(root: Path) -> dict[str, object]:
    """Project local worktrees plus the protected main branch, read-only."""
    try:
        provider = GitProvider()
        observed = provider.execute(root, "git", "worktree", "list", "--porcelain")
    except OSError:
        return {"available": False, "worktrees": []}
    if observed.returncode != 0:
        return {"available": False, "worktrees": []}

    worktrees: list[dict[str, object]] = []
    record: dict[str, str | bool] = {}
    for line in [*str(observed.stdout or "").splitlines(), ""]:
        if line:
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                record[key] = value
            elif key == "detached":
                record[key] = True
            continue
        path = str(record.get("worktree") or "").strip()
        if path:
            reference = str(record.get("branch") or "")
            try:
                active = Path(path).resolve() == root.resolve()
            except OSError:
                # A stale worktree path is diagnostic evidence only; never
                # present it as the active checkout.
                active = False
            worktrees.append({
                "path": path,
                "branch": reference.removeprefix("refs/heads/") or None,
                "commit": str(record.get("HEAD") or "")[:12] or None,
                "detached": bool(record.get("detached")),
                "active": active,
            })
        record = {}
    # `main` is the repository's stable baseline even when it is not currently
    # checked out in a worktree.  The Workspace heading promises branches as
    # well as worktrees, so project it explicitly instead of hiding it.
    if not any(item.get("branch") == "main" for item in worktrees):
        try:
            main = provider.execute(root, "git", "rev-parse", "--verify", "refs/heads/main")
        except OSError:
            main = None
        if main is not None and main.returncode == 0 and main.stdout.strip():
            worktrees.append({
                "path": None,
                "branch": "main",
                "commit": main.stdout.strip()[:12],
                "detached": False,
                "checked_out": False,
            })
    # Stable sorting keeps Git's worktree order intact while pinning main as
    # the first, recognisable baseline entry.
    worktrees.sort(key=lambda item: item.get("branch") != "main")
    # The action itself performs the fresh, fail-closed Git verification.
    # Keep this projection read-only so periodic dashboard refreshes never
    # run Git checks inside every listed worktree.
    for item in worktrees:
        path = item.get("path")
        branch = item.get("branch")
        if isinstance(path, str) and isinstance(branch, str) and branch != "main":
            item["removable"] = True
    return {"available": True, "worktrees": worktrees}


def _engineering_database_details(root: Path) -> dict[str, str]:
    """Return read-only local SQLite identity details without creating storage."""
    database = root.resolve() / ".engineering" / "engineering.db"
    details = {
        "path": str(database),
        "size": "Niet beschikbaar",
        "schema_version": "Niet beschikbaar",
    }
    try:
        megabytes = database.stat().st_size / 1_000_000
        details["size"] = f"{megabytes:.2f}".replace(".", ",") + " MB"
    except OSError:
        return details
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM engineering_schema_migrations"
            ).fetchone()
    except (OSError, sqlite3.DatabaseError):
        return details
    if row and row[0] is not None:
        details["schema_version"] = str(row[0])
    return details


def _engineering_database_snapshot(root: Path) -> bytes | None:
    """Create a consistent SQLite backup without modifying the source database."""
    database = root.resolve() / ".engineering" / "engineering.db"
    if not database.is_file():
        return None
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="engineering-backup-", suffix=".db", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary_path) as backup:
                source.backup(backup)
        return temporary_path.read_bytes()
    except (OSError, sqlite3.DatabaseError):
        return None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _dashboard_html(
    title: str,
    build_commit: str = "onbekend",
    workspace_id: str = "onbekend",
    project_name: str = "Project",
    workspace_location: str = ".",
    workspace_free_disk_space: str = "Niet beschikbaar",
    tracked_files: str = "Niet beschikbaar",
    engineering_database_path: str = "Niet beschikbaar",
    engineering_database_size: str = "Niet beschikbaar",
    engineering_database_schema_version: str = "Niet beschikbaar",
    workspace_branch: str = "Niet beschikbaar",
    workspace_commit: str = "Niet beschikbaar",
    origin_main_commit: str = "Niet beschikbaar",
    origin_main_available: bool = False,
    workspace_open_pull_requests: list[dict[str, object]] | None = None,
    workspace_main_action_hidden: bool = True,
    workspace_branch_cleanup_hidden: bool = True,
    platform_version: str = "2.0.0",
    configuration_inbox: str = "Niet beschikbaar",
) -> bytes:
    """Render the private dashboard with a server-pushed status stream."""
    page = r"""<!doctype html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta id="dashboardThemeColor" name="theme-color" content="#15151d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta id="dashboardAppleWebAppTitle" name="apple-mobile-web-app-title" content="$TITLE">
<title>$TITLE</title>
<link rel="manifest" href="/assets/operations-console/manifest.webmanifest">
<link id="dashboardFavicon" rel="icon" type="image/png" sizes="180x180" href="/assets/operations-console/apple-touch-icon-dark.png?v=operations-console-2">
<link id="dashboardAppleTouchIcon" rel="apple-touch-icon" sizes="180x180" href="/assets/operations-console/apple-touch-icon-dark.png?v=operations-console-2">
<script>try{const state=JSON.parse(localStorage.getItem("engineering-dashboard-client-state-v1")||"{}");document.documentElement.dataset.theme=state.theme==="light"?"light":"dark"}catch{document.documentElement.dataset.theme="dark"}</script>


<link rel="stylesheet" href="/assets/dashboard.css?build=$BUILD_COMMIT">
</head>
<body data-project-id="$WORKSPACE_ID" data-project-name="$PROJECT_NAME">
<a class="skip-link" href="#engineering-dashboard-content" data-i18n="header.skip"></a>
<div id="dashboardSplash" role="status" aria-live="polite" data-testid="dashboard-splash"><div class="dashboard-splash__content"><img class="dashboard-splash__icon" src="/assets/operations-console/icon-transparent.png" alt="" aria-hidden="true" data-testid="dashboard-splash-icon"><h2 class="dashboard-splash__title" id="dashboardSplashTitle" data-i18n="dashboard.title">$TITLE</h2><span class="dashboard-splash__version" id="dashboardSplashVersion" data-platform-version="$PLATFORM_VERSION">Engineering Platform $PLATFORM_VERSION</span><span class="dashboard-splash__spinner" aria-hidden="true"></span><span class="dashboard-splash__loading" id="dashboardSplashLoading" data-i18n="dashboard.loading"></span></div></div>
<div id="copyToast" role="status" aria-live="polite" aria-atomic="true" popover="manual" hidden data-testid="copy-toast"></div>
<div id="pullRefresh" role="status" aria-live="polite" aria-hidden="true" data-testid="pull-refresh" data-i18n="refresh.pull_to_refresh"></div>
<div class="dashboard-scroll-region">
<div class="dashboard-sticky-header">
<header class="dashboard-titlebar"><div class="dashboard-titlebar__brand"><img class="dashboard-app-icon" src="/assets/operations-console/icon-transparent.png" alt="" aria-hidden="true" data-testid="dashboard-app-icon"><h1 id="dashboardTitle" data-i18n="dashboard.title">$TITLE</h1></div><div class="dashboard-titlebar__actions"><div class="dashboard-health" id="dashboardHealth"><button class="dashboard-health__button" id="dashboardHealthIndicator" type="button" aria-expanded="false" aria-controls="dashboardHealthTooltip" data-health-state="unknown" data-testid="dashboard-health-indicator"><span class="dashboard-health__dot" aria-hidden="true"></span><span class="sr-only" id="dashboardHealthAccessibleLabel"></span></button></div><section class="dashboard-health__tooltip" id="dashboardHealthTooltip" role="tooltip"><strong id="dashboardHealthTooltipTitle"></strong><ul id="dashboardHealthChecks"></ul></section><button class="page-refresh" id="pageRefresh" type="button" data-testid="page-refresh" data-i18n-title="refresh.page" data-i18n-aria-label="refresh.page"><span aria-hidden="true">↻</span></button><div class="dashboard-titlebar__options" id="dashboardTitlebarOptions"><button class="dashboard-titlebar__options-toggle" id="dashboardTitlebarOptionsToggle" type="button" aria-expanded="false" aria-controls="dashboardTitlebarOptionsContent" data-testid="titlebar-options-toggle"><span data-i18n="header.options"></span></button><div class="dashboard-titlebar__options-content" id="dashboardTitlebarOptionsContent"><label class="dashboard-locale" for="dashboardLocale"><span data-i18n="language.label"></span><select id="dashboardLocale" class="dashboard-locale__native" data-i18n-aria-label="language.label"><option value="en" data-i18n="language.en"></option><option value="nl" data-i18n="language.nl"></option><option value="de" data-i18n="language.de"></option><option value="fr" data-i18n="language.fr"></option><option value="es" data-i18n="language.es"></option></select><span class="dashboard-locale__picker"><button class="dashboard-locale__button" id="dashboardLocaleButton" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="dashboardLocaleMenu"><span id="dashboardLocaleValue"></span><span aria-hidden="true">⌄</span></button><span class="dashboard-locale__menu" id="dashboardLocaleMenu" role="listbox" hidden><button type="button" role="option" data-dashboard-locale="en"></button><button type="button" role="option" data-dashboard-locale="nl"></button><button type="button" role="option" data-dashboard-locale="de"></button><button type="button" role="option" data-dashboard-locale="fr"></button><button type="button" role="option" data-dashboard-locale="es"></button></span></span></label><button class="theme-toggle" id="themeToggle" type="button" role="switch" aria-checked="false" data-i18n-aria-label="header.enable_light" data-testid="theme-toggle"><span class="theme-toggle__label" data-i18n="header.theme"></span></button><button class="section-state-toggle" id="toggleAllSections" type="button" role="switch" aria-checked="false" data-i18n-aria-label="header.open_all" data-testid="toggle-all-sections"><span class="section-state-toggle__label" data-i18n="header.expand"></span></button><label class="auto-refresh-toggle" for="autoRefresh"><input id="autoRefresh" type="checkbox" role="switch" checked><span data-i18n="header.auto_refresh"></span></label></div></div></div></header><aside class="dashboard-status-banner dashboard-status-banner--usage-limit" id="codexUsageLimitBanner" role="alert" aria-live="assertive" hidden data-testid="codex-usage-limit-banner"><strong data-i18n="notification.codex_usage_limit.title"></strong><span data-i18n="notification.codex_usage_limit.body"></span></aside>
<aside class="dashboard-status-banner dashboard-status-banner--capacity-reserve" id="codexCapacityReserveBanner" role="alert" aria-live="assertive" hidden data-testid="codex-capacity-reserve-banner"><strong data-i18n="notification.codex_capacity_reserve.title"></strong><span id="codexCapacityReserveMessage"></span><a class="codex-capacity-reserve-banner__action" id="codexCapacityReserveAction" href="#rateLimits" data-i18n="notification.codex_capacity_reserve.action"></a></aside>
<aside class="dashboard-status-banner dashboard-status-banner--github-rate-limit" id="githubRateLimitBanner" role="alert" aria-live="assertive" hidden data-testid="github-rate-limit-banner"><strong data-i18n="notification.github_rate_limit.title"></strong><span id="githubRateLimitMessage"></span><button class="github-rate-limit-banner__refresh" id="githubRateLimitRefresh" type="button" data-i18n-aria-label="notification.github_rate_limit.refresh" data-i18n-title="notification.github_rate_limit.refresh"><span aria-hidden="true">↻</span></button></aside>
<aside class="dashboard-status-banner dashboard-status-banner--provider-readiness" id="codexProviderReadinessBanner" role="alert" aria-live="assertive" hidden data-testid="codex-provider-readiness-banner"><strong id="codexProviderReadinessTitle"></strong><span id="codexProviderReadinessMessage"></span><button class="provider-readiness-banner__action" id="codexProviderReadinessAction" type="button" hidden></button></aside>
<aside class="dashboard-status-banner dashboard-status-banner--provider-readiness" id="githubProviderReadinessBanner" role="alert" aria-live="assertive" hidden data-testid="github-provider-readiness-banner"><strong id="githubProviderReadinessTitle"></strong><span id="githubProviderReadinessMessage"></span><button class="provider-readiness-banner__action" id="githubProviderReadinessAction" type="button" hidden></button></aside>
</div>
<main class="dashboard-grid" id="engineering-dashboard-content" tabindex="-1">
<details class="inbox-queue" id="queueItems" data-testid="engineering-inbox-queue"><summary><strong data-i18n="section.inbox_queue"></strong></summary><p class="category-description" data-i18n="description.inbox_queue"></p><div class="queue-blocker" id="inboxBlocker" role="alert" hidden></div><p class="estimate-meta" id="queueSummary" data-i18n="logs.loading"></p><ol class="queue-list" id="queueList" aria-live="polite"></ol></details>
<details class="prompt-history" id="promptHistory" data-testid="engineering-prompt-history"><summary><strong data-i18n="section.prompt_history"></strong></summary><p class="category-description" data-i18n="description.prompt_history"></p><div class="log-controls"><label for="promptHistoryFilter"><span data-i18n="filter.search"></span><input id="promptHistoryFilter" type="search" maxlength="160" data-sanitize="single-line" data-i18n-placeholder="filter.search_placeholder"></label></div><p class="history-scroll-hint" id="promptHistoryScrollHint" data-i18n="history.horizontal_scroll_hint"></p><div class="log-table-wrap" aria-describedby="promptHistoryScrollHint" role="region" tabindex="0"><table class="log-table" data-i18n-aria-label="history.table_label"><thead><tr><th data-history-sort-key="run_id" data-run-suffix="true" scope="col" data-i18n="table.run_suffix"></th><th data-history-sort-key="status" scope="col" data-i18n="table.status"></th><th data-history-sort-key="title" scope="col" data-i18n="table.prompt_title"></th><th data-history-sort-key="executed_at" scope="col" data-i18n="table.executed_at"></th><th scope="col" data-i18n="table.report"></th><th id="promptHistoryAnalysisHeader" scope="col" data-i18n="table.analysis"></th><th id="promptHistoryChatHeader" scope="col" data-i18n="table.chat"></th><th scope="col" data-i18n="table.action"></th><th id="promptHistoryDetailsHeader" scope="col" data-i18n="table.details"></th></tr></thead><tbody id="promptHistoryRows"><tr><td class="log-empty" colspan="9" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="promptHistoryPagination" data-i18n-aria-label="history.table_label"></nav></details>
<details class="current-run" id="currentRun" data-i18n-aria-label="detail.execution" hidden><summary class="current-run__title"><span class="label" id="currentRunTitle" data-i18n="section.active_prompt"></span></summary><div class="current-run__grid"><div class="field"><span class="label" data-i18n="detail.prompt_title"></span><h2 id="currentPrompt" data-i18n="format.loading"></h2></div><div class="field"><span class="label" data-i18n="ui.filename"></span><pre id="currentFile" data-i18n="format.loading"></pre></div>
<div class="card" id="executionIdentity"><strong id="executionIdentityTitle" data-i18n="detail.execution"></strong><p class="field"><span class="label" data-i18n="detail.run_id"></span><span id="runId"></span></p><p class="field" id="promptStartedField"><span class="label" data-i18n="ui.prompt_started"></span><span id="promptStarted" data-i18n="format.loading"></span></p></div>
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" data-i18n-aria-label="status.unknown"></span><strong data-i18n="detail.prompt_status"></strong></div><p class="field"><span class="label" data-i18n="ui.watcher"></span><span id="watcher" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.phase"></span><span id="phase" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.current_activity"></span><span id="action" data-i18n="format.loading"></span></p><p class="field" id="providerRecovery" hidden><span class="label" data-i18n="provider_recovery.label"></span><span id="providerRecoveryValue"></span></p></div>
<div class="card execution-context" id="executionContext" hidden><strong data-i18n="ui.execution_context"></strong><p class="field"><span class="label" data-i18n="field.execution_mode"></span><span id="executionMode"></span></p><p class="field"><span class="label" data-i18n="field.repository"></span><span id="targetRepository"></span></p><div class="field"><span class="label" data-i18n="detail.target_checkout"></span><pre id="checkoutPath"></pre></div><p class="field"><span class="label" data-i18n="ui.active_branch"></span><span id="activeBranch"></span></p></div>
<div class="card" id="processMetrics" hidden><strong data-i18n="ui.local_ai_processes"></strong><p class="field"><span class="label">CPU</span><span id="codexCpu" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.process_count"></span><span id="codexProcesses" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.gpu_usage"></span><span id="codexGpu" data-i18n="format.loading"></span></p></div>
<div class="card operator-merge-wait" id="operatorMergeWait" hidden><strong id="operatorMergeWaitTitle" data-i18n="merge_wait.title"></strong><p id="operatorMergeWaitDescription"></p><p class="open-pr-status operator-merge-wait__pr-status" id="operatorMergeWaitPullRequestStatus"><span class="open-pr-status__dot" aria-hidden="true"></span><span class="open-pr-status__label"></span></p><p class="open-pr-approval" id="operatorMergeWaitOwnerApproval"></p><p class="estimate-meta" id="operatorMergeWaitLastCheck" hidden></p><div class="operator-merge-wait__actions"><a class="dashboard-action dashboard-action--primary" id="operatorMergePullRequest" target="_blank" rel="noopener noreferrer"></a><button class="dashboard-action" id="operatorMergeStatusCheck" type="button" data-i18n="merge_wait.check_status"></button><button class="dashboard-action dashboard-action--destructive" id="operatorMergeAbort" type="button" data-i18n="action.abort_execution"></button></div></div>
<div class="card operator-merge-wait" id="emergencyRecovery" hidden><strong data-i18n="emergency_recovery.title"></strong><p data-i18n="emergency_recovery.description"></p><div class="operator-merge-wait__actions"><button class="dashboard-action dashboard-action--destructive" id="emergencyRecoveryStart" type="button" data-i18n="emergency_recovery.action"></button></div></div>
<div class="card status-reconciliation-card" id="statusReconciliation" hidden><strong data-i18n="status_reconciliation.title"></strong><p data-i18n="status_reconciliation.description"></p><div class="operator-merge-wait__actions"><button class="dashboard-action dashboard-action--primary" id="statusReconciliationStart" type="button" data-i18n="status_reconciliation.action"></button></div><p id="statusReconciliationResult" role="status" aria-live="polite"></p></div>
<div class="card" id="workspaceProgress"><strong data-i18n="detail.workspace_changes"></strong><p class="field"><span id="workspaceProgressValue" data-i18n="format.loading"></span></p></div>
<div class="card" id="predecessorGate" hidden><strong data-i18n="status.blocked"></strong><p class="field"><span class="label" data-i18n="detail.run_id"></span><code id="predecessorRun"></code></p><p class="field"><span class="label" data-i18n="ui.preceding_prompt"></span><span id="predecessorPrompt"></span></p><p class="field"><span class="label" data-i18n="field.terminal_state"></span><span id="predecessorPhase"></span></p><div class="field"><span class="label" data-i18n="ui.recovery_action"></span><pre id="predecessorAction"></pre></div><button class="predecessor-retry" id="predecessorRetry" type="button" data-i18n="recovery.action"></button><p class="predecessor-retry-status" id="predecessorRetryStatus" role="status" aria-live="polite"></p></div>
<div class="card"><strong data-i18n="ui.estimated_execution_time"></strong><p class="estimate-primary" id="executionEstimate" data-i18n="estimate.not_available"></p><p class="estimate-meta" id="executionEstimateMeta" hidden></p></div>
<div class="card" id="usage" hidden><strong>Codex CLI</strong><div class="field"><span class="label" data-i18n="ui.reported_usage"></span><pre id="usageDetails"></pre></div></div>
<div class="card" id="currentDiagnostic" hidden><strong>Codex CLI</strong><pre id="currentLog" data-i18n="format.loading"></pre></div>
</div></details>
<details class="card card--resource" id="rateLimits" hidden><summary><strong data-i18n="section.remaining_usage"></strong></summary><div class="field"><span class="label" data-i18n="ui.current_ai_provider"></span><span id="rateLimitProvider" data-i18n="format.loading"></span></div><p class="rate-limit-update-status" id="codexCliUpdateStatus" role="status" aria-live="polite"></p><button class="rate-limit-reset" id="codexCliUpdate" type="button" hidden data-i18n="ui.codex_cli_update"></button><div class="field rate-limit-provider-path"><span class="label" data-i18n="ui.installation_path"></span><code id="rateLimitProviderPath" data-i18n="format.not_available"></code></div><div class="field"><span class="label" id="rateLimitLabel">Codex CLI</span><pre id="rateLimitDetails"></pre></div><button class="rate-limit-reset" id="rateLimitReset" type="button" hidden data-i18n="ui.reset_ready"></button><p class="rate-limit-reset-status" id="rateLimitResetStatus" role="status" aria-live="polite"></p></details>
<details class="platform-health" id="platformHealth" data-testid="platform-health"><summary><strong data-i18n="section.platform_components"></strong></summary><p class="category-description" data-i18n="description.platform_components"></p><div class="platform-health__components" id="platformHealthComponents" aria-live="polite"><p class="platform-health__empty" data-i18n="ui.component_health_loading"></p></div></details>
<dialog class="dashboard-modal-shell dashboard-modal-shell--component component-modal" id="componentModal" aria-labelledby="componentModalTitle"><section class="dashboard-modal-shell__panel component-modal__panel"><header class="dashboard-modal-shell__header component-modal__header"><h2 id="componentModalTitle" data-i18n="ui.component_information"></h2><button class="dashboard-modal-shell__close component-modal__close" id="componentModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><div id="componentModalContent"></div><button class="component-modal__restart" id="componentModalRestart" type="button" hidden data-i18n="ui.component_restart"></button><p class="component-modal__status" id="componentModalStatus" aria-live="polite"></p></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence telemetry-detail-modal" id="telemetryDetailModal" aria-labelledby="telemetryDetailTitle"><section class="dashboard-modal-shell__panel telemetry-detail-modal__panel"><header class="dashboard-modal-shell__header"><h2 id="telemetryDetailTitle"></h2><div class="prompt-detail-modal__actions"><button class="dashboard-action dashboard-action--download prompt-detail-download" id="telemetryDetailDownloadMarkdown" type="button" hidden>↓</button><button class="dashboard-action dashboard-action--download prompt-detail-download" id="telemetryDetailDownloadJson" type="button" hidden>{}</button><button class="dashboard-modal-shell__close" id="telemetryDetailClose" type="button" data-i18n-aria-label="sections.close">×</button></div></header><p id="telemetryDetailDescription" class="prompt-detail-modal__description"></p><div id="telemetryDetailContent" class="telemetry-detail-modal__content" aria-live="polite"></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence lifecycle-detail-modal" id="lifecycleDetailModal" aria-labelledby="lifecycleDetailTitle"><section class="dashboard-modal-shell__panel lifecycle-detail-modal__panel"><header class="dashboard-modal-shell__header"><h2 id="lifecycleDetailTitle"></h2><button class="dashboard-modal-shell__close" id="lifecycleDetailClose" type="button" data-i18n-aria-label="sections.close">×</button></header><div id="lifecycleDetailContent" class="lifecycle-detail-modal__content" aria-live="polite"></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence execution-mode-modal" id="executionModeModal" aria-labelledby="executionModeModalTitle"><section class="dashboard-modal-shell__panel execution-mode-modal__panel"><header class="dashboard-modal-shell__header execution-mode-modal__header"><h2 id="executionModeModalTitle" data-i18n="execution_mode_info.title"></h2><button class="dashboard-modal-shell__close execution-mode-modal__close" id="executionModeModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><div class="execution-mode-modal__content"><p data-i18n="execution_mode_info.intro"></p><section class="execution-mode-modal__definition"><h3 data-i18n="execution_mode_info.managed_title"></h3><p data-i18n="execution_mode_info.managed_body"></p></section><section class="execution-mode-modal__definition"><h3 data-i18n="execution_mode_info.genesis_title"></h3><p data-i18n="execution_mode_info.genesis_body"></p></section></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence confirmation-modal" id="operatorMergeWaitModal" aria-labelledby="operatorMergeWaitModalTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="operatorMergeWaitModalTitle" data-i18n="merge_wait.title"></h2><button class="dashboard-modal-shell__close" id="operatorMergeWaitModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><p id="operatorMergeWaitModalDescription"></p><p class="estimate-meta" id="operatorMergeWaitModalLastCheck" hidden></p><section class="merge-wait-context" data-i18n-aria-label="merge_wait.context_label"><p id="operatorMergeWaitModalContextIntro"></p><dl><div><dt data-i18n="merge_wait.context_run"></dt><dd id="operatorMergeWaitModalRunId"></dd></div><div><dt data-i18n="merge_wait.context_prompt"></dt><dd id="operatorMergeWaitModalPrompt"></dd></div><div><dt data-i18n="merge_wait.pull_request_status"></dt><dd><span class="open-pr-status" id="operatorMergeWaitModalPullRequestStatus"><span class="open-pr-status__dot" aria-hidden="true"></span><span class="open-pr-status__label"></span></span></dd></div><div><dt data-i18n="merge_wait.owner_approval"></dt><dd><span class="open-pr-approval" id="operatorMergeWaitModalOwnerApproval"></span></dd></div></dl></section><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action" id="operatorMergeWaitModalStatusCheck" type="button" data-i18n="merge_wait.check_status"></button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--destructive" id="operatorMergeWaitModalAbort" type="button" data-i18n="action.abort_execution"></button><a class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="operatorMergeWaitModalPullRequest" target="_blank" rel="noopener noreferrer"></a></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="confirmationModal" aria-labelledby="confirmationModalTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="confirmationModalTitle" data-i18n="ui.confirm_action"></h2><button class="dashboard-modal-shell__close confirmation-modal__close" id="confirmationModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><div id="confirmationModalText"></div><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action" id="confirmationModalCancel" type="button" data-i18n="action.cancel"></button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="confirmationModalConfirm" type="button" data-i18n="action.confirm"></button></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="dashboardErrorModal" aria-labelledby="dashboardErrorModalTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="dashboardErrorModalTitle" data-i18n="ui.action_failed"></h2><button class="dashboard-modal-shell__close confirmation-modal__close" id="dashboardErrorModalClose" type="button" data-i18n-aria-label="action.close">×</button></header><p id="dashboardErrorModalText" aria-live="assertive"></p><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action" id="dashboardErrorModalRecover" type="button" hidden></button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="dashboardErrorModalDismiss" type="button" data-i18n="action.close"></button></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="workspaceBranchCleanupResultModal" aria-labelledby="workspaceBranchCleanupResultTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="workspaceBranchCleanupResultTitle" data-i18n="workspace.branch_cleanup_result_title"></h2><button class="dashboard-modal-shell__close confirmation-modal__close" id="workspaceBranchCleanupResultClose" type="button" data-i18n-aria-label="action.close">×</button></header><div id="workspaceBranchCleanupResultContent" aria-live="polite"></div><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="workspaceBranchCleanupResultDismiss" type="button" data-i18n="action.close"></button></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="workspaceBranchMainResultModal" aria-labelledby="workspaceBranchMainResultTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="workspaceBranchMainResultTitle" data-i18n="workspace.branch_main_result_title"></h2><button class="dashboard-modal-shell__close confirmation-modal__close" id="workspaceBranchMainResultClose" type="button" data-i18n-aria-label="action.close">×</button></header><div id="workspaceBranchMainResultContent" aria-live="polite"></div><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="workspaceBranchMainResultDismiss" type="button" data-i18n="action.close"></button></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence report-view-modal" id="promptHistoryReportModal" aria-labelledby="promptHistoryReportModalTitle"><section class="dashboard-modal-shell__panel report-view-modal__panel"><header class="dashboard-modal-shell__header report-view-modal__header"><h2 class="report-view-modal__title" id="promptHistoryReportModalTitle" data-modal-glyph="report" data-i18n="history.report_title"></h2><div class="report-view-modal__actions"><button class="dashboard-action dashboard-action--primary report-analysis-retry" id="promptHistoryReportRetry" type="button" hidden data-i18n="history.retry_analysis">↻</button><button class="dashboard-action dashboard-action--download download download--glyph" id="promptHistoryReportDownload" type="button" hidden>⇩</button><button class="dashboard-action dashboard-action--copy copy copy--glyph" id="promptHistoryReportCopy" type="button" hidden>⧉</button><button class="dashboard-modal-shell__close report-view-modal__close" id="promptHistoryReportClose" type="button" data-i18n-aria-label="sections.close">×</button></div></header><article class="markdown-document report-view-modal__content" id="promptHistoryReportContent" data-i18n="history.report_loading"></article></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence prompt-detail-modal" id="promptHistoryDetailModal" aria-labelledby="promptHistoryDetailTitle"><section class="dashboard-modal-shell__panel prompt-detail-modal__panel"><header class="dashboard-modal-shell__header prompt-detail-modal__header"><h2 id="promptHistoryDetailTitle" data-i18n="history.details_loading"></h2><div class="prompt-detail-modal__actions"><button class="dashboard-action dashboard-action--download prompt-detail-download" id="promptHistoryDetailDownloadMarkdown" type="button" hidden>↓</button><button class="dashboard-action dashboard-action--download prompt-detail-download" id="promptHistoryDetailDownloadJson" type="button" hidden>{}</button><button class="dashboard-modal-shell__close prompt-detail-modal__close" id="promptHistoryDetailClose" type="button" data-i18n-aria-label="sections.close">×</button></div></header><p class="prompt-detail-modal__description" id="promptHistoryDetailDescription"></p><div class="prompt-detail-modal__content" id="promptHistoryDetailContent" data-i18n="history.details_loading"></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--chat prompt-chat-modal" id="promptHistoryChatModal" aria-labelledby="promptHistoryChatTitle"><section class="dashboard-modal-shell__panel prompt-chat-modal__panel"><header class="dashboard-modal-shell__header prompt-chat-modal__header"><h2 id="promptHistoryChatTitle" data-i18n="section.ai_conversation"></h2><div class="prompt-detail-modal__actions prompt-chat-modal__actions"><button class="dashboard-action dashboard-action--download download download--glyph" id="downloadChat" type="button" hidden>⇩</button><button class="dashboard-action dashboard-action--copy" id="copyChat" type="button" hidden data-i18n-title="chat.copy_title" data-i18n-aria-label="chat.copy_title">⧉</button><button class="dashboard-action dashboard-action--destructive" id="clearChat" type="button" hidden>⌫</button><button class="dashboard-modal-shell__close prompt-chat-modal__close" id="promptHistoryChatClose" type="button" data-i18n-aria-label="sections.close">×</button></div></header><p class="prompt-chat-modal__description" id="promptHistoryChatDescription"></p><section class="codex-chat" id="codexChat"><div class="codex-chat__details"><div class="chat-messages" id="chatMessages" aria-live="polite" data-i18n-aria-label="section.ai_conversation"></div><label class="label chat-question-label" for="chatInput" data-i18n="section.new_ai_question"></label><div class="chat-compose"><textarea id="chatInput" class="chat-input" rows="5" maxlength="2000" autocomplete="off" data-sanitize="multiline" data-i18n-placeholder="history.chat_placeholder"></textarea><button class="chat-send" id="chatSend" type="button" data-i18n-aria-label="action.confirm"><span aria-hidden="true">➤</span></button></div><div class="chat-meta"><p class="field"><span class="label" data-i18n="detail.model"></span><span id="chatModel">$CHAT_MODEL</span></p><p class="chat-status" id="chatStatus"></p></div></div></section></section></dialog>
<button id="loadComponentLogs" type="button" hidden data-i18n="logs.loading"></button>
<details class="technical-details" id="componentLogs"><summary><strong data-i18n="section.logs"></strong></summary><p class="estimate-meta" data-i18n="description.logs"></p><div class="log-controls" id="componentLogControls" hidden><label for="logFilter"><span data-i18n="filter.search"></span><input id="logFilter" type="search" maxlength="160" data-sanitize="single-line" data-i18n-placeholder="filter.search_placeholder"></label><label for="logLevelFilter"><span data-i18n="filter.level"></span><select id="logLevelFilter"><option value="" data-i18n="filter.all_levels"></option><option value="ERROR" data-i18n="filter.error"></option><option value="WARNING" data-i18n="filter.warning"></option><option value="INFO" data-i18n="filter.info"></option><option value="DEBUG" data-i18n="filter.debug"></option></select></label><label for="logTimePreset"><span data-i18n="filter.time_period"></span><select id="logTimePreset"><option value="" data-i18n="filter.all_time"></option><option value="today" data-i18n="filter.today"></option><option value="yesterday" data-i18n="filter.yesterday"></option><option value="day" data-i18n="filter.specific_day"></option><option value="range" data-i18n="filter.custom_range"></option></select></label><label for="logSpecificDate" id="logSpecificDateControl" hidden><span data-i18n="filter.specific_day"></span><input id="logSpecificDate" type="date"></label><label for="logDateFrom" id="logDateFromControl" hidden><span data-i18n="filter.from"></span><input id="logDateFrom" type="datetime-local"></label><label for="logDateTo" id="logDateToControl" hidden><span data-i18n="filter.to"></span><input id="logDateTo" type="datetime-local"></label></div><div class="technical-grid"><div class="card"><div class="log-card-header"><strong data-i18n="logs.inbox_watcher"></strong><div class="log-card-actions"><button class="dashboard-action dashboard-action--download download download--glyph component-log-download" data-component="inbox" data-testid="download-inbox-log" type="button" data-i18n-title="logs.download_inbox" data-i18n-aria-label="logs.download_inbox">⇩</button><button class="dashboard-action dashboard-action--destructive clear-component-log" data-component="inbox" data-testid="clear-inbox-log" type="button" data-i18n-title="action.clear_logs" data-i18n-aria-label="action.clear_logs">⌫</button></div></div><div class="log-table-wrap"><table class="log-table" data-i18n-aria-label="logs.inbox_watcher"><thead><tr><th data-i18n="table.number"></th><th data-i18n="table.timestamp"></th><th data-i18n="table.level"></th><th data-i18n="table.event"></th><th data-i18n="table.run_id"></th><th data-i18n="table.details"></th></tr></thead><tbody id="inboxComponentLog"><tr><td class="log-empty" colspan="6" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="inboxLogPagination" data-i18n-aria-label="logs.inbox_watcher"></nav></div><div class="card"><div class="log-card-header"><strong data-i18n="logs.status_dashboard"></strong><div class="log-card-actions"><button class="dashboard-action dashboard-action--download download download--glyph component-log-download" data-component="dashboard" data-testid="download-dashboard-log" type="button" data-i18n-title="logs.download_dashboard" data-i18n-aria-label="logs.download_dashboard">⇩</button><button class="dashboard-action dashboard-action--destructive clear-component-log" data-component="dashboard" data-testid="clear-dashboard-log" type="button" data-i18n-title="action.clear_logs" data-i18n-aria-label="action.clear_logs">⌫</button></div></div><div class="log-table-wrap"><table class="log-table" data-i18n-aria-label="logs.status_dashboard"><thead><tr><th data-i18n="table.number"></th><th data-i18n="table.timestamp"></th><th data-i18n="table.level"></th><th data-i18n="table.event"></th><th data-i18n="table.run_id"></th><th data-i18n="table.details"></th></tr></thead><tbody id="dashboardComponentLog"><tr><td class="log-empty" colspan="6" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="dashboardLogPagination" data-i18n-aria-label="logs.status_dashboard"></nav></div></div></details>
<details class="technical-details" id="technicalDetails" hidden><summary><strong data-i18n="section.technical_details"></strong></summary><p class="category-description" id="technicalDetailsDescription" data-i18n="description.technical_details"></p><p class="technical-diagnosis-summary" id="technicalHealthySummary" hidden></p><div class="technical-grid" id="technicalDiagnosisDetails">
<div class="card"><strong id="technicalRepositoryTitle" data-i18n="technical.repository"></strong><p class="field"><span class="label" id="technicalRepositoryStateLabel" data-i18n="technical.repository_status"></span><span id="repositoryState"></span></p><p class="field"><span class="label technical-repository-label" id="technicalWorkspaceStateLabel"><span data-i18n="technical.workspace_status"></span><span class="technical-info" id="technicalWorkspaceStateInfo" role="img" tabindex="0" data-i18n-title="technical.workspace_status_help" data-i18n-aria-label="technical.workspace_status_help">i</span></span><span id="workspaceState"></span></p><div class="technical-git-lock" id="technicalGitLock"><p class="field"><span class="label technical-repository-label" id="technicalGitLockLabel"><span data-i18n="technical.git_lock"></span><span class="technical-info" id="technicalGitLockInfo" role="img" tabindex="0" data-i18n-title="technical.git_lock_help" data-i18n-aria-label="technical.git_lock_help">i</span></span><span id="technicalGitLockState" data-i18n="format.loading"></span></p><p class="technical-git-lock__detail" id="technicalGitLockDetail" hidden></p><button class="queue-blocker__repair" id="technicalGitLockRecover" type="button" hidden data-i18n="technical.git_lock_recovery_action"></button><p class="technical-git-lock__status" id="technicalGitLockRecoveryStatus" role="status" aria-live="polite"></p></div></div>
<div class="card"><strong id="technicalHostPreflightTitle" data-i18n="technical.host_preflight"></strong><p class="field"><span class="label" id="technicalExecutionHostLabel" data-i18n="technical.execution_host"></span><span id="executionHostName" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalExecutionHostVersionLabel" data-i18n="technical.execution_host_version"></span><span id="executionHostVersion" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRuntimeLabel" data-i18n="technical.runtime"></span><span id="executionHostRuntime" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRuntimeVersionLabel" data-i18n="detail.codex_cli_version"></span><span id="executionHostRuntimeVersion" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRuntimePathLabel" data-i18n="detail.codex_cli_installation_path"></span><code id="executionHostRuntimePath" data-i18n="format.unavailable"></code></p><p class="field"><span class="label" id="technicalRuntimePromptTransportLabel" data-i18n="technical.runtime_prompt_transport"></span><span id="executionHostTransport" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalHostStatusLabel" data-i18n="technical.host_status"></span><span id="hostPreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalLastCheckLabel" data-i18n="technical.last_check"></span><span id="hostPreflightTimestamp" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalWorkspacePreflightStatusLabel" data-i18n="technical.workspace_status"></span><span id="workspacePreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalLastWorkspaceCheckLabel" data-i18n="technical.last_workspace_check"></span><span id="workspacePreflightTimestamp" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalCapabilityStatusLabel" data-i18n="technical.capability_status"></span><span id="capabilityPreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRecoverabilityLabel" data-i18n="technical.recoverability"></span><span id="capabilityRecoverability" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalFailureOriginLabel" data-i18n="technical.failure_origin"></span><span id="capabilityFailureOrigin" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRecommendationLabel" data-i18n="technical.recommended_action"></span><span id="capabilityRecommendation" data-i18n="format.unavailable"></span></p></div>
<div class="card" id="driftDiagnosticsCard" hidden><strong data-i18n="technical.current_drift"></strong><p class="field"><span class="label" data-i18n="technical.severity"></span><span id="driftSeverity"></span></p><p class="field"><span class="label" data-i18n="technical.affected_component"></span><span id="driftComponent"></span></p><p class="field"><span class="label" data-i18n="technical.expected_state"></span><span id="driftExpected"></span></p><p class="field"><span class="label" data-i18n="technical.observed_state"></span><span id="driftObserved"></span></p><p class="field"><span class="label" data-i18n="technical.resolution"></span><span id="driftResolution"></span></p></div>
<div class="card" id="technicalDiagnosticsCard"><strong id="technicalDiagnosticsTitle" data-i18n="technical.diagnostics"></strong><p id="diag"></p></div>
</div></details>
<details class="card card--context workspace-card" id="workspaceCard" data-testid="engineering-workspace"><summary><strong data-i18n="section.workspace"></strong></summary><p class="field"><span class="label" data-workspace-label="workspace.name" data-i18n="workspace.name"></span><span>$WORKSPACE_ID</span></p><div class="field"><span class="label" data-workspace-label="ui.workspace_location" data-i18n="ui.workspace_location"></span><pre>$WORKSPACE_LOCATION</pre></div><p class="field" id="workspaceFreeDiskSpace"><span class="label" data-workspace-label="workspace.free_disk_space" data-i18n="workspace.free_disk_space"></span><span>$WORKSPACE_FREE_DISK_SPACE</span></p><p class="field"><span class="label" data-workspace-label="detail.tracked_files" data-i18n="detail.tracked_files"></span><span>$TRACKED_FILES</span></p><section class="workspace-database-section" aria-labelledby="workspaceDatabaseHeading"><h2 id="workspaceDatabaseHeading" data-i18n="workspace.database"></h2><div class="field" id="workspaceDatabaseField"><span class="label" data-workspace-label="workspace.database_location" data-i18n="workspace.database_location"></span><pre>$ENGINEERING_DATABASE_PATH</pre></div><p class="field" id="workspaceDatabaseSize"><span class="label" data-workspace-label="workspace.database_size" data-i18n="workspace.database_size"></span><span>$ENGINEERING_DATABASE_SIZE</span></p><p class="field" id="workspaceSchemaVersion"><span class="label" data-workspace-label="workspace.schema_version" data-i18n="workspace.schema_version"></span><span>$ENGINEERING_DATABASE_SCHEMA_VERSION</span></p><label class="workspace-database-maintenance-field" for="configurationDatabaseMaintenanceInterval"><span><span data-i18n="configuration.database_maintenance_interval"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.database_maintenance_interval_help" data-i18n-aria-label="configuration.database_maintenance_interval_help">i</span></span><select id="configurationDatabaseMaintenanceInterval"><option value="60" data-i18n="configuration.minute_1"></option><option value="3600" data-i18n="configuration.hour_1"></option><option value="86400" data-i18n="configuration.day_1"></option><option value="604800" data-i18n="configuration.week_1"></option></select></label></section><p class="field"><span class="label" data-workspace-label="workspace.current_branch" data-i18n="workspace.current_branch"></span><code id="workspaceBranch">$WORKSPACE_BRANCH</code></p><p class="field"><span class="label" data-workspace-label="workspace.current_commit" data-i18n="workspace.current_commit"></span><code id="workspaceCommit">$WORKSPACE_COMMIT</code></p><p class="field" id="workspaceOriginMain" $ORIGIN_MAIN_HIDDEN><span class="label" data-workspace-label="workspace.origin_main_commit" data-i18n="workspace.origin_main_commit"></span><code id="workspaceOriginMainCommit">$ORIGIN_MAIN_COMMIT</code></p>$WORKSPACE_OPEN_PULL_REQUESTS<div class="workspace-branch-actions"><button class="workspace-branch-cleanup" id="workspaceBranchCleanup" type="button" $BRANCH_CLEANUP_HIDDEN data-i18n="workspace.branch_cleanup_scan_action"></button><button class="workspace-branch-main" id="workspaceBranchMain" type="button" $WORKSPACE_MAIN_ACTION_HIDDEN data-i18n="workspace.branch_main_action"></button></div></details>
<details class="card card--context workspace-card configuration-card" id="configuration" data-testid="dashboard-configuration"><summary><strong data-i18n="section.configuration"></strong></summary><p class="category-description" data-i18n="description.configuration"></p><div class="field configuration-field"><span class="label"><span data-i18n="configuration.inbox_location"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.inbox_location_help" data-i18n-aria-label="configuration.inbox_location_help">i</span></span><button id="configurationInboxOpen" class="configuration-inbox-open" type="button" data-i18n="configuration.inbox_location_open"></button></div><div class="configuration-controls"><label for="configurationLogRetention"><span data-i18n="configuration.log_retention"></span><select id="configurationLogRetention"><option value="30"></option><option value="60"></option><option value="90"></option><option value="120"></option><option value="180"></option><option value="360"></option></select></label><label for="configurationLogLevel"><span data-i18n="configuration.log_level"></span><select id="configurationLogLevel"><option value="INFO" data-i18n="filter.info"></option><option value="DEBUG" data-i18n="filter.debug"></option></select></label><label for="configurationInboxScanInterval"><span data-i18n="configuration.inbox_scan_interval"></span><select id="configurationInboxScanInterval"><option value="5" data-i18n="configuration.seconds_5"></option><option value="15" data-i18n="configuration.seconds_15"></option><option value="30" data-i18n="configuration.seconds_30"></option><option value="60" data-i18n="configuration.seconds_60"></option></select></label><label for="configurationOpenPrInterval"><span data-i18n="configuration.open_pr_interval"></span><select id="configurationOpenPrInterval"><option value="30" data-i18n="configuration.seconds_30"></option><option value="60" data-i18n="configuration.seconds_60"></option></select></label><label for="configurationDashboardStreamInterval"><span data-i18n="configuration.dashboard_stream_interval"></span><select id="configurationDashboardStreamInterval"><option value="1"></option><option value="2"></option><option value="3"></option><option value="4"></option><option value="5"></option><option value="6"></option><option value="7"></option><option value="8"></option><option value="9"></option><option value="10"></option></select></label><label for="configurationPlatformHealthInterval"><span data-i18n="configuration.platform_health_interval"></span><select id="configurationPlatformHealthInterval"><option value="5" data-i18n="configuration.seconds_5"></option><option value="15" data-i18n="configuration.seconds_15"></option><option value="30" data-i18n="configuration.seconds_30"></option><option value="60" data-i18n="configuration.seconds_60"></option></select></label><label for="configurationComponentDetailsInterval"><span data-i18n="configuration.component_details_interval"></span><select id="configurationComponentDetailsInterval"><option value="5" data-i18n="configuration.seconds_5"></option><option value="15" data-i18n="configuration.seconds_15"></option><option value="30" data-i18n="configuration.seconds_30"></option><option value="60" data-i18n="configuration.seconds_60"></option></select></label><p id="configurationStatus" role="status" aria-live="polite"></p></div><section class="configuration-readonly-settings" aria-labelledby="configurationReadonlySettingsTitle"><h2 id="configurationReadonlySettingsTitle" data-i18n="configuration.readonly_platform_settings"></h2><p class="field configuration-field"><span class="label"><span data-i18n="configuration.operator_merge_interval"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.operator_merge_interval_help" data-i18n-aria-label="configuration.operator_merge_interval_help">i</span></span><span data-i18n="configuration.seconds_60"></span></p><p class="field configuration-field"><span class="label"><span data-i18n="configuration.required_checks_interval"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.required_checks_interval_help" data-i18n-aria-label="configuration.required_checks_interval_help">i</span></span><span data-i18n="configuration.seconds_15"></span></p><p class="field configuration-field"><span class="label"><span data-i18n="configuration.lease_heartbeat_interval"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.lease_heartbeat_interval_help" data-i18n-aria-label="configuration.lease_heartbeat_interval_help">i</span></span><span data-i18n="configuration.seconds_15"></span></p><p class="field configuration-field"><span class="label"><span data-i18n="configuration.lease_timeout"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.lease_timeout_help" data-i18n-aria-label="configuration.lease_timeout_help">i</span></span><span data-i18n="configuration.seconds_90"></span></p><p class="field configuration-field"><span class="label"><span data-i18n="configuration.github_retry_backoff"></span><span class="configuration-info" role="img" tabindex="0" data-i18n-title="configuration.github_retry_backoff_help" data-i18n-aria-label="configuration.github_retry_backoff_help">i</span></span><span data-i18n="configuration.github_retry_backoff_value"></span></p></section></details>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence configuration-inbox-modal" id="configurationInboxModal" aria-labelledby="configurationInboxModalTitle"><section class="dashboard-modal-shell__panel"><header class="dashboard-modal-shell__header"><h2 id="configurationInboxModalTitle" data-i18n="configuration.inbox_location"></h2><button class="dashboard-modal-shell__close" id="configurationInboxModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><p data-i18n="configuration.inbox_location_modal_description"></p><div class="configuration-inbox-modal__field"><label for="configurationInboxRoot" data-i18n="configuration.inbox_location_input"></label><input id="configurationInboxRoot" type="text" autocomplete="off"><button class="dashboard-modal-shell__action configuration-inbox-modal__browse" id="configurationInboxBrowse" type="button" data-i18n="configuration.inbox_location_browse"></button></div><pre id="configurationInbox" hidden>$CONFIGURATION_INBOX</pre><p class="configuration-inbox-modal__hint" data-i18n="configuration.inbox_location_requirement"></p><p id="configurationInboxStatus" role="status" aria-live="polite"></p><div class="dashboard-modal-shell__actions"><button class="dashboard-modal-shell__action" id="configurationInboxModalCloseAction" type="button" data-i18n="action.cancel"></button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="configurationInboxSave" type="button" data-i18n="configuration.inbox_location_save"></button></div></section></dialog>
</main></div>
<footer class="footer" aria-live="polite"><span class="footer__item"><span class="label" id="platformVersionLabel" data-i18n="footer.platform_version"></span><span id="platformVersion" data-i18n="format.loading"></span></span><span class="footer__separator" aria-hidden="true">·</span><span class="footer__item" id="lastRefresh" data-i18n="format.loading"></span><span class="footer__separator" aria-hidden="true">·</span><span class="footer__item" id="updateMode" data-i18n="format.loading"></span></footer><span id="dashboardVersion" hidden></span><span id="workerVersion" hidden></span>
<script>window.DJCONNECT_DASHBOARD_BUILD="$BUILD_COMMIT";</script>
<script src="/assets/dashboard.js?build=$BUILD_COMMIT" type="module"></script>

</body>
</html>"""
    def open_pull_request_item(pull_request: dict[str, object]) -> str:
        authorization = (
            f'<button class="open-pr-owner-authorization" '
            f'data-open-pull-request-owner-authorization="{pull_request["number"]}" '
            f'type="button" data-i18n="workspace.open_pull_request.authorize_owner"></button>'
            if pull_request.get("owner_authorization_requested") is True else ""
        )
        repair = (
            f'<button class="open-pr-check-repair" '
            f'data-open-pull-request-check-repair="{pull_request["number"]}" '
            f'data-open-pull-request-failed-checks="{escape(json.dumps(pull_request.get("failed_checks", [])), quote=True)}" '
            f'type="button" data-i18n="workspace.open_pull_request.repair_failed_checks"></button>'
            if pull_request.get("check_repair_available") is True else ""
        )
        return (
            f'<li data-open-pull-request="{pull_request["number"]}"><a href="{escape(str(pull_request["url"]), quote=True)}" '
            f'target="_blank" rel="noreferrer">PR #{pull_request["number"]} — {escape(str(pull_request["title"]))}</a>'
            f'<span class="open-pr-status open-pr-status--{escape(str(pull_request.get("status", "waiting_for_checks")), quote=True)}">'
            f'<span class="open-pr-status__dot" aria-hidden="true"></span><span class="open-pr-status__label"></span></span>'
            f'{authorization}{repair}<code>{escape(str(pull_request["branch"]))}</code></li>'
        )

    pull_request_items = "".join(
        open_pull_request_item(pull_request)
        for pull_request in workspace_open_pull_requests or []
    )
    workspace_open_pull_requests_html = (
        f'<section id="workspaceOpenPullRequests" class="workspace-open-prs" aria-live="polite"><div class="workspace-open-prs__header"><strong data-i18n="workspace.open_pull_requests"></strong><button class="workspace-open-prs__refresh" id="workspaceOpenPullRequestsRefresh" type="button" data-i18n-title="workspace.open_pull_requests_refresh" data-i18n-aria-label="workspace.open_pull_requests_refresh">↻</button></div><ul>{pull_request_items}</ul></section>'
        if pull_request_items else ""
    )
    return (
        page.replace("$TITLE", escape(title))
        .replace("$BUILD_COMMIT", escape(build_commit))
        .replace("$CHAT_MODEL", escape(chat_model()))
        .replace("$WORKSPACE_ID", escape(workspace_id))
        .replace("$PROJECT_NAME", escape(project_name))
        .replace("$WORKSPACE_LOCATION", escape(workspace_location))
        .replace("$WORKSPACE_FREE_DISK_SPACE", escape(workspace_free_disk_space))
        .replace("$TRACKED_FILES", escape(tracked_files))
        .replace("$ENGINEERING_DATABASE_PATH", escape(engineering_database_path))
        .replace("$ENGINEERING_DATABASE_SIZE", escape(engineering_database_size))
        .replace("$ENGINEERING_DATABASE_SCHEMA_VERSION", escape(engineering_database_schema_version))
        .replace("$WORKSPACE_BRANCH", escape(workspace_branch))
        .replace("$WORKSPACE_COMMIT", escape(workspace_commit))
        .replace("$ORIGIN_MAIN_COMMIT", escape(origin_main_commit))
        .replace("$ORIGIN_MAIN_HIDDEN", "" if origin_main_available else "hidden")
        .replace("$WORKSPACE_OPEN_PULL_REQUESTS", workspace_open_pull_requests_html)
        .replace("$WORKSPACE_MAIN_ACTION_HIDDEN", "hidden" if workspace_main_action_hidden else "")
        .replace("$BRANCH_CLEANUP_HIDDEN", "hidden" if workspace_branch_cleanup_hidden else "")
        .replace("$PLATFORM_VERSION", escape(platform_version))
        .replace("$CONFIGURATION_INBOX", escape(configuration_inbox))
        .encode()
    )


def handler(root: Path, logger: logging.Logger | None = None):
    configuration = PlatformConfiguration.load(root)
    title = configuration.workspace.dashboard_title
    workspace_id = configuration.workspace.id
    project_name = configuration.workspace.name
    workspace_location = str(root)
    tracked_files = _tracked_file_count(root)
    platform_version = EngineeringPlatformManifest.load(
        root / "tools/engineering/ENGINEERING_PLATFORM_VERSION.json"
    ).platform_version
    logger = logger or component_logger(root, "dashboard")
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, content: bytes, content_type: str, status_code: int = 200) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(content)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or origin == f"http://{self.headers.get('Host', '')}"

        def do_POST(self) -> None:
            if not self._same_origin():
                self._send(b'{"error":"Ongeldige herkomst."}', "application/json; charset=utf-8", 403)
                return
            request_path = urlsplit(self.path).path
            analysis_retry = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/analysis-retry", request_path)
            if analysis_retry:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError("Ongeldige aanvraag voor AI-analyse.")
                    run_id = analysis_retry.group(1)
                    analysis = _retry_report_analysis(root, run_id)
                    log_event(logger, logging.INFO, "report_analysis_regenerated", run_id=run_id)
                except (OSError, RuntimeError, ValueError) as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                self._send(analysis, "text/markdown; charset=utf-8")
                return
            if request_path == "/api/provider-login/logout":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    _logout_provider(root, str(payload.get("provider", "")))
                except (OSError, ValueError, json.JSONDecodeError):
                    self._send(b'{"error":"Provider logout did not complete."}', "application/json; charset=utf-8", 400)
                    return
                self._send(b'{"logged_out":true}', "application/json; charset=utf-8")
                return
            if request_path == "/api/provider-login/repair":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {"provider", "action"}:
                        raise ValueError
                    provider, action = str(payload["provider"]), str(payload["action"])
                    if action == "login":
                        _start_provider_login(root, provider)
                    elif action == "install":
                        _install_provider(root, provider)
                    else:
                        raise ValueError
                    log_event(logger, logging.INFO, "provider_repair_requested", diagnostic=f"provider={provider}; action={action}")
                except (OSError, ValueError, json.JSONDecodeError):
                    self._send(b'{"error":"Provider repair did not start safely."}', "application/json; charset=utf-8", 400)
                    return
                self._send(b'{"started":true}', "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/telemetry/clear":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = clear_telemetry(root)
                    log_event(
                        logger,
                        logging.INFO,
                        "telemetry_cleared",
                        diagnostic=(
                            f"execution_runs={outcome['execution_runs']}; "
                            f"daily_statistics={outcome['daily_statistics']}"
                        ),
                    )
                except ValueError:
                    self._send(b'{"error":"invalid_telemetry_clear_request"}', "application/json; charset=utf-8", 400)
                    return
                except OSError as error:
                    self._send(
                        json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8",
                        503,
                    )
                    return
                self._send(json.dumps({"cleared": True, **outcome}).encode(), "application/json; charset=utf-8")
                return
            owner_authorization_match = re.fullmatch(r"/api/open-pull-requests/([1-9][0-9]*)/owner-authorization", request_path)
            if owner_authorization_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _request_owner_authorization(root, int(owner_authorization_match.group(1)))
                    log_event(
                        logger,
                        logging.INFO,
                        "owner_authorization_dispatched",
                        diagnostic=f"pull_request={outcome['pull_request']}",
                    )
                except OwnerAuthorizationRequestError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json; charset=utf-8", 409)
                    return
                except ValueError:
                    self._send(b'{"error":"owner_authorization_invalid_request"}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome).encode(), "application/json; charset=utf-8", 202)
                return
            pr_check_repair_match = re.fullmatch(r"/api/open-pull-requests/([1-9][0-9]*)/repair-failed-checks", request_path)
            if pr_check_repair_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    number = int(pr_check_repair_match.group(1))
                    if _execution_active(root):
                        raise PullRequestCheckRepairError("pr_check_repair_execution_active")
                    evidence = admit_pr_check_repair(root, number)
                    sha = str(evidence["head_sha"])
                    try:
                        LocalProcessProvider().spawn_detached(
                            root,
                            (sys.executable, "-m", "tools.engineering.pr_check_repair", "--root", str(root), "--pull-request", str(number), "--head-sha", sha),
                            os.environ.copy(),
                        )
                    except OSError:
                        mark_pr_check_repair_dispatch_failed(root, number, sha)
                        raise PullRequestCheckRepairError("pr_check_repair_dispatch_failed")
                    log_event(logger, logging.INFO, "pr_check_repair_dispatched", diagnostic=f"pull_request={number}")
                except PullRequestCheckRepairError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json; charset=utf-8", 409)
                    return
                except ValueError:
                    self._send(b'{"error":"pr_check_repair_invalid_request"}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps({"queued": True, "pull_request": number}).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path.startswith("/api/components/") and request_path.endswith("/restart"):
                component = request_path.removeprefix("/api/components/").removesuffix("/restart").rstrip("/")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError("Ongeldig herstartverzoek.")
                    if component not in RESTARTABLE_COMPONENTS:
                        raise ValueError("Dit onderdeel kan niet veilig vanuit het dashboard worden herstart.")
                    # Give the response a chance to reach the browser before the
                    # dashboard asks launchd to replace its own process.
                    Timer(0.25, _restart_component_after_response, args=(component, logger)).start()
                    log_event(
                        logger,
                        logging.INFO,
                        "component_restart_trigger_received",
                        diagnostic=f"target={component}",
                        context={
                            **component_lifecycle_context(
                                root,
                                version=DASHBOARD_VERSION,
                                launchd_label=LABEL,
                                launch_agent_path=Path.home()
                                / "Library/LaunchAgents"
                                / f"{LABEL}.plist",
                            ),
                            "target_component": component,
                        },
                    )
                except ValueError as error:
                    self._send(
                        json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8",
                        400,
                    )
                    return
                self._send(
                    json.dumps({"restarting": component}, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                    202,
                )
                return
            if request_path == "/api/rate-limit-reset":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _consume_codex_rate_limit_reset_credit()
                    log_event(
                        logger,
                        logging.INFO,
                        "ai_usage_reset_completed" if outcome == "reset" else "ai_usage_reset_not_consumed",
                        diagnostic=f"outcome={outcome}",
                    )
                    payload = {
                        "outcome": outcome,
                        "rate_limits": json.loads(_codex_rate_limits()),
                    }
                except RateLimitResetError as error:
                    log_event(logger, logging.WARNING, "ai_usage_reset_failed", diagnostic=str(error))
                    content = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
                    self._send(content, "application/json; charset=utf-8", 503)
                    return
                except (ValueError, json.JSONDecodeError):
                    self._send(b'{"error":"Ongeldig resetverzoek."}', "application/json; charset=utf-8", 400)
                    return
                status_code = 200 if outcome == "reset" else 409
                self._send(
                    json.dumps(payload, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                    status_code,
                )
                return
            if request_path == "/api/codex-cli-update":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    result = _install_codex_cli_update(root)
                    log_event(
                        logger,
                        logging.INFO,
                        "codex_cli_update_completed" if result["updated"] else "codex_cli_update_not_needed",
                        diagnostic=f"updated={result['updated']}",
                    )
                except CodexCliUpdateError as error:
                    log_event(logger, logging.WARNING, "codex_cli_update_failed", diagnostic=str(error))
                    status_code = 409 if str(error) == "codex_cli_update_execution_active" else 503
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json; charset=utf-8", status_code)
                    return
                except ValueError:
                    self._send(b'{"error":"invalid_request"}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(result).encode(), "application/json; charset=utf-8")
                return
            if request_path in {"/api/queue-recovery", "/api/predecessor-retry"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    predecessor_retry_admission_preflight(root)
                    outcome = submit_predecessor_retry(root, cloud_root(repo=root))
                    log_event(
                        logger,
                        logging.INFO,
                        "queue_recovery_triggered" if request_path == "/api/queue-recovery" else "predecessor_retry_submission_triggered",
                        run_id=outcome["blocking_run_id"],
                        diagnostic=f"retry_run_id={outcome['retry_run_id']}",
                    )
                except RetrySubmissionError as error:
                    content = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
                    self._send(content, "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError):
                    self._send(
                        b'{"error":"De Inbox-watcher verwerkt momenteel een actie. Probeer het opnieuw."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(
                    json.dumps(outcome, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                    202,
                )
                return
            if request_path == "/api/managed-branch-recovery":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _restore_managed_main_branch(root)
                    log_event(
                        logger,
                        logging.INFO,
                        "managed_branch_recovery_completed",
                        diagnostic=f"previous_branch={outcome['previous_branch']}; watcher=restarted",
                    )
                except (RuntimeError, ValueError):
                    self._send(
                        b'{"error":"De werkmap kon niet veilig naar main worden hersteld."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/managed-branch-synchronization":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _synchronize_managed_branch_with_upstream(root)
                    log_event(
                        logger,
                        logging.INFO,
                        "managed_branch_synchronization_completed",
                        diagnostic=f"branch={outcome['branch']}; upstream={outcome['upstream']}; watcher=restarted",
                    )
                except (RuntimeError, ValueError):
                    self._send(
                        b'{"error":"De verwachte branch kon niet veilig worden gesynchroniseerd."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/stale-git-lock-recovery":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _recover_stale_workspace_git_lock(root)
                    log_event(logger, logging.INFO, "stale_git_lock_recovered")
                except (RuntimeError, ValueError):
                    self._send(
                        b'{"error":"De Git-vergrendeling is niet veilig herstelbaar."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/stale-local-branch-cleanup":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    branches = payload.get("branches") if isinstance(payload, dict) and set(payload) == {"branches"} else None
                    if not isinstance(branches, list):
                        raise ValueError
                    outcome = _cleanup_stale_local_branches(root, branches)
                    log_event(logger, logging.INFO, "stale_local_branches_cleaned", diagnostic=f"removed={outcome['removed_count']}")
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(
                        b'{"error":"Lokale branches konden niet veilig worden opgeruimd."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/safe-worktree-removal":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) not in ({"worktree_path", "branch"}, {"worktree_path", "head"}):
                        raise ValueError
                    outcome = (
                        _remove_safe_worktree(root, payload["worktree_path"], payload["branch"])
                        if "branch" in payload else _remove_safe_worktree(root, payload["worktree_path"], head=payload["head"])
                    )
                    log_event(logger, logging.INFO, "safe_worktree_removed", diagnostic=f"target={outcome.get('branch') or outcome.get('head')}")
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De worktree kon niet veilig worden verwijderd."}', "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/open-worktree-folder":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"worktree_path"}:
                        raise ValueError
                    outcome = _open_worktree_in_finder(root, payload["worktree_path"])
                    log_event(logger, logging.INFO, "worktree_folder_opened", diagnostic=f"path={outcome['opened_worktree']}")
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De lokale worktreemap kon niet veilig worden geopend."}', "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/open-local-directory":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"directory_path"}:
                        raise ValueError
                    outcome = _open_local_directory_in_finder(root, payload["directory_path"])
                    log_event(logger, logging.INFO, "local_directory_opened", diagnostic=f"path={outcome['opened_directory']}")
                except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De lokale map kon niet veilig worden geopend."}', "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/worktree-removal-analysis":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _worktree_removal_analysis(root)
                    log_event(logger, logging.INFO, "worktree_removal_analysed", diagnostic=f"worktrees={len(outcome['worktrees'])}")
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"Worktree-analyse is nu niet beschikbaar."}', "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 200)
                return
            if request_path == "/api/workspace-switch-to-main":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    if _execution_active(root):
                        raise RuntimeError("Naar main schakelen en Engineering Platform herstarten kan alleen wanneer geen uitvoering actief is.")
                    outcome = _switch_to_fast_forward_main(root)
                except (OSError, RuntimeError, ValueError) as error:
                    self._send(json.dumps({"error": str(error) or "Naar main schakelen is niet veilig gelukt."}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                # The dashboard must acknowledge the operator before it is
                # replaced. The full platform reload makes the switched main
                # revision the running revision for watcher, relay and UI.
                Timer(0.25, _restart_engineering_platform_after_main_switch, args=(root, logger)).start()
                outcome["engineering_platform"] = "restart_scheduled"
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/workspace-switch-to-worktree":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"worktree_path", "branch"}:
                        raise ValueError
                    target = _worktree_switch_target_when_idle(root, payload["worktree_path"], payload["branch"])
                except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._send(json.dumps({"error": str(error) or "Naar de worktree schakelen is niet veilig gelukt."}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                # Validate again after this response: a newly claimed run or
                # Inbox item always wins over a requested service relocation.
                Timer(0.25, _activate_engineering_platform_worktree, args=(root, str(target), str(payload["branch"]), logger)).start()
                self._send(json.dumps({"branch": payload["branch"], "worktree_path": str(target), "engineering_platform": "restart_scheduled"}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/stale-local-branch-cleanup-preview":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = _stale_local_branch_preview(root)
                except (RuntimeError, ValueError):
                    self._send(
                        b'{"error":"Lokale branches konden niet veilig worden gescand."}',
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 200)
                return
            if request_path == "/api/execution-retry":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    retry_admission_preflight(root, payload["run_id"])
                    outcome = submit_execution_retry(root, cloud_root(repo=root), payload["run_id"])
                    log_event(logger, logging.INFO, "execution_retry_triggered", run_id=payload["run_id"], diagnostic=f"retry_run_id={outcome['retry_run_id']}")
                except RetrySubmissionError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De uitvoering kan nu niet veilig opnieuw worden gestart."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/status-reconciliation-preview":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = status_reconciliation_preview(root, payload["run_id"])
                except (RetrySubmissionError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._send(json.dumps({"error": str(error) or "Statusherstel is niet veilig beschikbaar."}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 200)
                return
            if request_path == "/api/status-reconciliation":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = submit_status_reconciliation(root, cloud_root(repo=root), payload["run_id"])
                    log_event(logger, logging.INFO, "status_reconciliation_requested", run_id=payload["run_id"])
                except (RetrySubmissionError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._send(json.dumps({"error": str(error) or "Statusherstel kon niet veilig worden aangevraagd."}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/execution-dismiss":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = dismiss_execution(root, payload["run_id"])
                    log_event(logger, logging.INFO, "execution_dismissed", run_id=payload["run_id"])
                except RetrySubmissionError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De uitvoering kan nu niet veilig worden bevestigd."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/execution-merge-wait-abort":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = abort_operator_merge_wait(root, payload["run_id"])
                    log_event(logger, logging.INFO, "operator_merge_wait_aborted", run_id=payload["run_id"])
                except RetrySubmissionError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De wachtende uitvoering kon niet veilig worden afgebroken."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/execution-emergency-rollback":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = execute_emergency_recovery(root, payload["run_id"])
                    log_event(logger, logging.WARNING, "execution_emergency_rollback_completed", run_id=payload["run_id"])
                except (EmergencyRecoveryError, EngineeringStorageError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._send(json.dumps({"error": str(error) or "De noodactie kon niet veilig worden uitgevoerd."}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/execution-merge-status-check":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
                    outcome = check_operator_merge_status(root, payload["run_id"])
                    log_event(logger, logging.INFO, "operator_merge_status_checked", run_id=payload["run_id"], diagnostic=str(outcome.get("reason") or outcome.get("continuation")))
                except RetrySubmissionError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De pull request-status kon niet veilig worden gecontroleerd."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202 if outcome.get("verified") else 409)
                return
            if request_path == "/api/queue-defer":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"filename"} or not isinstance(payload["filename"], str):
                        raise ValueError
                    outcome = defer_queued_prompt(root, cloud_root(repo=root), payload["filename"])
                    log_event(
                        logger,
                        logging.INFO,
                        "queue_item_deferred",
                        diagnostic=f"filename={outcome['filename']}; deferred_filename={outcome['deferred_filename']}",
                    )
                except RetrySubmissionError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 409)
                    return
                except (RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"De Inbox-opdracht kan nu niet veilig worden uitgesteld."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(outcome, ensure_ascii=False).encode(), "application/json; charset=utf-8", 202)
                return
            if request_path == "/api/audit/user-action":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 256:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = payload.get("action") if isinstance(payload, dict) else None
                    if action not in AUDITABLE_USER_ACTIONS:
                        raise ValueError
                    log_event(logger, logging.INFO, str(action))
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    self._send(b'{"error":"Ongeldige gebruikersactie."}', "application/json; charset=utf-8", 400)
                    return
                self._send(b'{"logged":true}', "application/json; charset=utf-8")
                return
            if request_path == "/api/configuration":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 256:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"key", "value", "previous"}:
                        raise ValueError
                    _validate_codex_capacity_reserve_update(root, payload["key"], payload["value"])
                    event = update_dashboard_configuration(
                        root,
                        payload["key"],
                        payload["value"],
                        expected_previous=payload["previous"],
                    )
                    if event["key"] == "log_retention_days":
                        prune_component_logs(root, int(event["value"]))
                    if event["key"] == "telemetry_retention_days":
                        prune_telemetry(root, int(event["value"]))
                    if event["key"] == "log_level":
                        logger.setLevel(str(event["value"]))
                        for log_handler in logger.handlers:
                            log_handler.setLevel(logger.level)
                    log_event(logger, logging.INFO, "dashboard_configuration_changed",
                              diagnostic=f"key={event['key']}; previous={event['previous']}; value={event['value']}")
                except CodexCapacityReserveConflict as error:
                    current = dashboard_configuration(root).get(payload.get("key"))
                    self._send(
                        json.dumps({
                            "error_code": error.code,
                            "value": current,
                            "remaining_percent": error.remaining_percent,
                        }).encode(),
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                except DashboardConfigurationConflict as error:
                    current = dashboard_configuration(root).get(payload.get("key"))
                    self._send(
                        json.dumps({"error": str(error), "value": current}).encode(),
                        "application/json; charset=utf-8",
                        409,
                    )
                    return
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"Ongeldige dashboardinstelling."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(event).encode(), "application/json; charset=utf-8")
                return
            if request_path == "/api/configuration/inbox-location":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4_096:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"inbox_root"}:
                        raise ValueError
                    if _execution_active(root):
                        raise RuntimeError("Wijzig de Inbox-locatie pas wanneer geen uitvoering actief is.")
                    active_inbox = PlatformConfiguration.load(root).resolver(root).resolve_runtime_prompt_transport().inbox
                    if _inbox_has_items(active_inbox):
                        self._send(
                            b'{"error_code":"inbox_not_empty"}',
                            "application/json; charset=utf-8",
                            409,
                        )
                        return
                    event = _change_inbox_location(root, payload["inbox_root"], active_inbox)
                    log_event(
                        logger, logging.INFO, "dashboard_configuration_changed",
                        diagnostic=f"key={event['key']}; previous={event['previous']}; value={event['value']}",
                    )
                except InboxLocationChangeError as error:
                    log_event(logger, logging.ERROR, "inbox_location_change_rolled_back", diagnostic=str(error))
                    self._send(
                        json.dumps({"error_code": "inbox_watcher_restart_failed"}).encode(),
                        "application/json; charset=utf-8", 503,
                    )
                    return
                except RuntimeError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json; charset=utf-8", 409)
                    return
                except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send(b'{"error":"Inbox-locatie kon niet veilig worden gewijzigd."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps(event).encode(), "application/json; charset=utf-8")
                return
            if request_path == "/api/configuration/inbox-location/browse":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    location = _choose_local_directory(root)
                except RuntimeError as error:
                    self._send(json.dumps({"error": str(error)}).encode(), "application/json; charset=utf-8", 409)
                    return
                except (OSError, ValueError):
                    self._send(b'{"error":"De lokale mapkiezer kon niet worden geopend."}', "application/json; charset=utf-8", 400)
                    return
                self._send(json.dumps({"cancelled": location is None, "value": location or ""}).encode(), "application/json; charset=utf-8")
                return
            if request_path.startswith("/api/logs/"):
                component = request_path.rsplit("/", 1)[-1]
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    _clear_component_log(root, component)
                    log_event(
                        logger,
                        logging.INFO,
                        "component_log_cleared",
                        diagnostic=component,
                    )
                except ValueError:
                    self._send(b'{"error":"Ongeldig logverzoek."}', "application/json; charset=utf-8", 400)
                    return
                except OSError as error:
                    content = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
                    self._send(content, "application/json; charset=utf-8", 503)
                    return
                self._send(
                    json.dumps({"cleared": component}, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                )
                return
            if request_path == "/api/codex-chat/clear":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 256:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"}:
                        raise ValueError
                    clear_codex_chat_history(root, payload["run_id"])
                except CodexChatError as error:
                    content = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
                    self._send(content, "application/json; charset=utf-8", 404)
                    return
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    self._send(b'{"error":"Ongeldig chatverzoek."}', "application/json; charset=utf-8", 400)
                    return
                log_event(logger, logging.INFO, "ai_chat_history_cleared", run_id=payload["run_id"])
                self._send(b'{"cleared":true}', "application/json; charset=utf-8")
                return
            if request_path != "/api/codex-chat":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_000:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) not in ({"message"}, {"message", "run_id"}):
                    raise ValueError
                status = json.loads(_status(root))
                answer = codex_chat_response(
                    root, status, payload["message"], payload.get("run_id")
                )
            except CodexChatError as error:
                content = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
                self._send(content, "application/json; charset=utf-8", 503)
                return
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._send(b'{"error":"Ongeldig chatverzoek."}', "application/json; charset=utf-8", 400)
                return
            log_event(logger, logging.INFO, "ai_chat_message_sent", diagnostic="[REDACTED]")
            self._send(
                json.dumps(
                    {"answer": answer, "model": chat_model(), "messages": codex_chat_history(root, payload.get("run_id") or status.get("last_executed_run"))},
                    ensure_ascii=False,
                ).encode(),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            if request.path not in {"/api/logs/inbox", "/api/logs/dashboard"}:
                log_event(logger, logging.DEBUG, "http_request", diagnostic=request.path)
            icon_assets = {
                "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                "/assets/dashboard_locales.mjs": ("dashboard_locales.mjs", "text/javascript; charset=utf-8"),
                "/assets/dashboard_status_store.mjs": ("dashboard_status_store.mjs", "text/javascript; charset=utf-8"),
                "/assets/operations-console/icon-dark.png": ("operations-console/icon-dark.png", "image/png"),
                "/assets/operations-console/icon-light.png": ("operations-console/icon-light.png", "image/png"),
                "/assets/operations-console/icon-transparent.png": ("operations-console/icon-transparent.png", "image/png"),
                "/assets/operations-console/apple-touch-icon-dark.png": (APP_ICON_DARK, "image/png"),
                "/assets/operations-console/apple-touch-icon-light.png": (APP_ICON_LIGHT, "image/png"),
                "/assets/operations-console/manifest.webmanifest": (WEB_MANIFEST, "application/manifest+json; charset=utf-8"),
                # Serve the conventional browser and iOS discovery paths too.
                # They intentionally reuse the canonical app icon rather than
                # introducing duplicate, independently-versioned icon files.
                "/favicon.ico": (APP_ICON_DARK, "image/png"),
                "/apple-touch-icon.png": (APP_ICON_DARK, "image/png"),
                "/apple-touch-icon-precomposed.png": (APP_ICON_DARK, "image/png"),
            }
            if asset := icon_assets.get(request.path):
                asset_name, content_type = asset
                try:
                    content = (ASSET_DIRECTORY / asset_name).read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                return self._send(content, content_type)
            if request.path == "/api/prompt-history":
                return self._send(_prompt_history(root), "application/json; charset=utf-8")
            if request.path.startswith("/api/prompt-history/") and request.path.endswith("/chat"):
                run_id = request.path.removeprefix("/api/prompt-history/").removesuffix("/chat").strip("/")
                try:
                    messages = codex_chat_history(root, run_id)
                except CodexChatError as error:
                    self._send(json.dumps({"error": str(error)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 404)
                    return
                return self._send(json.dumps({"messages": messages}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if request.path == "/api/engineering-database/download":
                snapshot = _engineering_database_snapshot(root)
                if snapshot is None:
                    self._send(b'{"error":"Engineering-database is niet beschikbaar."}', "application/json; charset=utf-8", 404)
                    return
                if parse_qs(request.query).get("audit") == ["download"]:
                    log_event(logger, logging.INFO, "engineering_database_downloaded")
                filename = f"engineering-database-backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.sqlite3")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(snapshot)
                return
            if request.path.startswith("/api/prompt-history/") and request.path.endswith("/details"):
                run_id = request.path.removeprefix("/api/prompt-history/").removesuffix("/details").strip("/")
                detail = _prompt_history_detail(root, run_id)
                if not detail:
                    self._send(b'{"error":"Uitvoeringsdetails zijn niet beschikbaar."}', "application/json; charset=utf-8", 404)
                    return
                return self._send(detail, "application/json; charset=utf-8")
            if request.path.startswith("/api/prompt-history/") and request.path.endswith("/report"):
                run_id = request.path.removeprefix("/api/prompt-history/").removesuffix("/report").strip("/")
                report = report_for_prompt_history(root, run_id)
                if report is None:
                    self._send(b'{"error":"Engineeringrapport is niet beschikbaar."}', "application/json; charset=utf-8", 404)
                    return
                if parse_qs(request.query).get("audit") == ["download"]:
                    log_event(logger, logging.INFO, "engineering_report_downloaded", run_id=run_id)
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="engineering-report-{run_id}.md"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(report)
                return
            if request.path.startswith("/api/prompt-history/") and request.path.endswith("/analysis"):
                run_id = request.path.removeprefix("/api/prompt-history/").removesuffix("/analysis").strip("/")
                analysis = _report_analysis_for_run(root, run_id)
                if not analysis:
                    self._send(b'{"error":"AI-analyse is niet beschikbaar."}', "application/json; charset=utf-8", 404)
                    return
                if parse_qs(request.query).get("audit") == ["download"]:
                    log_event(logger, logging.INFO, "report_analysis_downloaded", run_id=run_id)
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="ai-analysis-{run_id}.md"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(analysis)
                return
            if self.path == "/api/status":
                return self._send(_status(root), "application/json; charset=utf-8")
            if self.path == "/api/dashboard-snapshot":
                return self._send(
                    _sse_snapshot(root), "application/json; charset=utf-8"
                )
            if self.path == "/api/github-rate-limit":
                return self._send(
                    json.dumps(_github_rate_limit_status(), separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/codex-cli-update":
                return self._send(
                    json.dumps(_codex_cli_update_status(root), separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/open-pull-requests":
                pull_requests = _workspace_open_pull_requests(root)
                if pull_requests is None:
                    return self._send(
                        b'{"error":"GitHub pull-requeststatus is tijdelijk niet beschikbaar."}',
                        "application/json; charset=utf-8",
                        503,
                    )
                return self._send(
                    json.dumps({"pull_requests": pull_requests}, ensure_ascii=False, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                )
            if request.path.startswith("/api/telemetry/"):
                execution_date = request.path.removeprefix("/api/telemetry/").strip("/")
                try:
                    detail = daily_timing_detail(root, execution_date)
                except ValueError:
                    self._send(b'{"error":"Ongeldige telemetriedatum."}', "application/json; charset=utf-8", 400)
                    return
                return self._send(json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8")
            if self.path == "/api/build":
                return self._send(
                    json.dumps({"build_commit": _build_commit(root)}).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/health":
                return self._send(b'{"health":"ok"}', "application/json; charset=utf-8")
            if self.path == "/health":
                payload = _platform_health(root)
                return self._send(
                    json.dumps(payload, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                    200 if payload["healthy"] else 503,
                )
            if request.path.startswith("/api/components/") and request.path.endswith("/details"):
                component = request.path.removeprefix("/api/components/").removesuffix("/details").rstrip("/")
                try:
                    payload = _component_details(root, component)
                except ValueError as error:
                    self._send(
                        json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8",
                        404,
                    )
                    return
                return self._send(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/process-metrics":
                return self._send(_codex_process_metrics(root), "application/json; charset=utf-8")
            if self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    stream_interval = int(dashboard_configuration(root)["dashboard_stream_interval_seconds"])
                    self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
                    previous: bytes | None = None
                    for iteration in range(300):
                        snapshot = _sse_snapshot(root)
                        if snapshot != previous:
                            self.wfile.write(b"event: dashboard\ndata: " + snapshot + b"\n\n")
                            self.wfile.flush()
                            previous = snapshot
                        elif iteration and iteration % 15 == 0:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        interval = int(dashboard_configuration(root)["dashboard_stream_interval_seconds"])
                        if interval != stream_interval:
                            self.wfile.write(f"retry: {interval * 1000}\n\n".encode())
                            self.wfile.flush()
                            stream_interval = interval
                        time.sleep(stream_interval)
                except (BrokenPipeError, ConnectionResetError):
                    log_event(logger, logging.DEBUG, "sse_client_disconnected")
                return
            if self.path == "/api/report/latest":
                try:
                    reports = sorted((root / ".engineering" / "reports").glob("*.md"))
                    content = (
                        reports[-1].read_bytes() if reports else b"Geen lokaal rapport beschikbaar."
                    )
                except OSError:
                    content = b"Report is unavailable."
                return self._send(content, "text/markdown; charset=utf-8")
            if self.path == "/api/log/latest":
                return self._send(_latest_codex_log(root), "text/plain; charset=utf-8")
            if request.path in {"/api/logs/inbox", "/api/logs/dashboard"}:
                component = request.path.rsplit("/", 1)[-1]
                query = parse_qs(request.query, keep_blank_values=True)
                if query.get("format") == ["json"]:
                    try:
                        payload = _component_log_page(root, component, query)
                    except ValueError as error:
                        return self._send(
                            json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                            "application/json; charset=utf-8",
                            400,
                        )
                    return self._send(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                        "application/json; charset=utf-8",
                    )
                return self._send(
                    _component_log(root, component),
                    "text/plain; charset=utf-8",
                )
            if self.path == "/api/log/current":
                return self._send(_current_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/usage":
                return self._send(_codex_usage(root), "application/json; charset=utf-8")
            if self.path == "/api/provider-login-status":
                return self._send(
                    json.dumps({"providers": _provider_login_status(root)}, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8",
                )
            if request.path == "/api/configuration":
                return self._send(
                    json.dumps(dashboard_configuration(root)).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/commits":
                return self._send(_completion_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/prompt-started":
                return self._send(_prompt_started(root), "application/json; charset=utf-8")
            if request.path == "/":
                engineering_database = _engineering_database_details(root)
                workspace_free_disk_space = _workspace_free_disk_space(root)
                workspace_git = _workspace_git_projection(root)
                workspace_open_pull_requests = _workspace_open_pull_requests(root)
                return self._send(
                    _dashboard_html(
                        title,
                        _build_commit(root),
                        workspace_id,
                        project_name,
                        workspace_location,
                        workspace_free_disk_space,
                        tracked_files,
                        engineering_database["path"],
                        engineering_database["size"],
                        engineering_database["schema_version"],
                        str(workspace_git["branch"]),
                        str(workspace_git["commit"]),
                        str(workspace_git["origin_main_commit"]),
                        bool(workspace_git["origin_main_available"]),
                        workspace_open_pull_requests,
                        not bool(workspace_git["main_action_available"]),
                        not bool(workspace_git["branch_cleanup_available"]),
                        platform_version,
                        # The Inbox root is a durable local preference that may be
                        # changed while this server remains running.  Resolve it for
                        # each document request instead of retaining the startup
                        # fallback in the rendered page.
                        str(configuration.resolver(root).resolve_runtime_prompt_transport().inbox),
                    ),
                    "text/html; charset=utf-8",
                )
            # Browser asset misses and mistyped paths are normal HTTP noise. They
            # are returned to the caller but intentionally do not pollute the
            # operational Engineering Platform event log.
            self.send_error(404)

        def log_message(self, message: str, *_: object) -> None:
            log_event(logger, logging.DEBUG, "http_server_message", diagnostic=message)

    return DashboardHandler


def binding_addresses(provider: TailscaleProvider | None = None) -> tuple[str, ...]:
    """Keep the HTTP application on loopback; the relay owns Tailnet ingress."""
    return (LOOPBACK_ADDRESS,)


def create_servers(
    root: Path,
    port: int = 8765,
    provider: TailscaleProvider | None = None,
    logger: logging.Logger | None = None,
) -> tuple[DashboardHTTPServer, ...]:
    """Create the loopback-only HTTP listener for the dashboard."""
    request_handler = handler(root, logger)
    return tuple(
        DashboardHTTPServer((address, port), request_handler)
        for address in binding_addresses(provider)
    )


def run(root: Path, port: int = 8765, provider: TailscaleProvider | None = None) -> None:
    """Serve the read-only dashboard on loopback; the relay handles Tailnet ingress."""
    provision_workspace(root)
    logger = component_logger(root, "dashboard")
    lifecycle_context = component_lifecycle_context(
        root,
        version=DASHBOARD_VERSION,
        launchd_label=LABEL,
        launch_agent_path=Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist",
    )
    try:
        with single_instance(root, "dashboard"):
            with shutdown_signal_logging(logger, lifecycle_context):
                try:
                    servers = create_servers(root, port, provider, logger)
                except OSError as error:
                    log_event(logger, logging.ERROR, "dashboard_start_failed", diagnostic=str(error))
                    raise
                log_event(
                    logger,
                    logging.INFO,
                    "dashboard_started",
                    diagnostic="addresses="
                    + ",".join(address for address, _ in (server.server_address for server in servers)),
                    context=lifecycle_context,
                )
                try:
                    servers[0].serve_forever()
                finally:
                    log_event(
                        logger,
                        logging.INFO,
                        "dashboard_shutdown_completed",
                        context=lifecycle_context,
                    )
    except KeyboardInterrupt:
        return
    except DuplicateComponentInstanceError as error:
        log_event(logger, logging.ERROR, "duplicate_dashboard_refused", diagnostic=str(error))
        raise


def launch_agent(repo: Path) -> Path:
    """Render the only owned per-user LaunchAgent; no network policy changes.

    The dashboard is a repository module, so the LaunchAgent starts from its
    selected checkout. Python's safe-path mode deliberately ignores
    ``PYTHONPATH`` and therefore cannot be combined with module discovery
    from the neutral filesystem root. The ``--repo`` argument remains the
    sole source of operational paths.
    """
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher = (sys.executable, "-m", "tools.engineering.dashboard", "run", "--repo", str(repo))
    command = "cd " + shlex.quote(str(repo)) + " && exec " + " ".join(
        shlex.quote(value) for value in launcher
    )
    arguments = f"<string>/bin/zsh</string><string>-lc</string><string>{escape(command)}</string>"
    try:
        # The dashboard preference is durable across service regeneration.  An
        # inherited shell value is only a fallback for an unavailable store.
        log_level = str(dashboard_configuration(repo)["log_level"]).upper()
    except (EngineeringStorageError, KeyError, TypeError, ValueError):
        log_level = os.environ.get(LOG_LEVEL_ENVIRONMENT, DEFAULT_LOG_LEVEL).upper()
    if log_level not in VALID_LEVELS:
        log_level = DEFAULT_LOG_LEVEL
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{escape(str(repo))}</string><key>EnvironmentVariables</key><dict><key>{LOG_LEVEL_ENVIRONMENT}</key><string>{log_level}</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer></dict></plist>',
        encoding="utf-8",
    )
    return destination


def relay_binary(repo: Path) -> Path:
    return repo / ".engineering" / "bin" / "engineering-dashboard-relay"


def build_relay(repo: Path) -> Path:
    """Compile the repository-owned private Tailnet-to-loopback relay."""
    compiler = shutil.which("swiftc")
    if compiler is None:
        raise RuntimeError("Swift compiler ontbreekt; de private dashboardrelay kan niet starten.")
    binary = relay_binary(repo)
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    compiled = LocalProcessProvider().execute(repo, (compiler, str(repo / "tools/engineering/dashboard_supervisor.swift"), "-o", str(binary)))
    if compiled.returncode:
        raise RuntimeError("Dashboardrelay compilation failed.")
    binary.chmod(0o700)
    return binary


def relay_launch_agent(repo: Path, binary: Path) -> Path:
    destination = Path.home() / "Library/LaunchAgents" / f"{RELAY_LABEL}.plist"
    arguments = f"<string>{escape(str(binary))}</string>"
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{RELAY_LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>',
        encoding="utf-8",
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "install", "uninstall", "status", "doctor"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    relay_agent = Path.home() / "Library/LaunchAgents" / f"{RELAY_LABEL}.plist"
    if args.command == "run":
        run(repo, port=args.port)
        return 0
    if args.command == "install":
        agent = launch_agent(repo)
        LaunchdProvider().install(LABEL, agent)
        relay_agent = relay_launch_agent(repo, build_relay(repo))
        LaunchdProvider().install(RELAY_LABEL, relay_agent)
        return 0
    if args.command == "uninstall":
        LaunchdProvider().uninstall(agent)
        agent.unlink(missing_ok=True)
        LaunchdProvider().uninstall(relay_agent)
        relay_agent.unlink(missing_ok=True)
        return 0
    health = (repo / ".engineering" / "status" / "status.json").is_file()
    remote_provider = TailscaleProvider()
    remote = remote_provider.status()
    tailscale_address = remote_provider.ipv4_address()
    state = "READY" if health and agent.is_file() and relay_agent.is_file() and tailscale_address else "DEGRADED"
    action = (
        "Voer het Engineering Platform uit om een statusupdate te publiceren."
        if not health
        else "Verbind met Tailscale voordat je privétoegang tot het iPhone-dashboard gebruikt."
        if not tailscale_address
        else "Open het privédashboard via het lokale Tailscale-adres."
    )
    print(
        f"REMOTE_ENGINEERING_{state}\nprivate_remote_access={remote.detail}\n"
        f"tailscale_dashboard_address={tailscale_address or 'unavailable'}\n"
        f"Actie: {action} Er is geen netwerkconfiguratie gewijzigd."
    )
    return 0 if state == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
