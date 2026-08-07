"""Private Engineering Status dashboard with distinct queue recovery and execution retry actions."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
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
import subprocess  # Compatibility mock target; process execution is provider-owned.
import sys
from threading import Lock, Timer
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from .platform_api import PlatformConfiguration
from .platform_bootstrap import provision_workspace
from .providers import CodexCliProvider, GitProvider, LaunchdProvider, LocalProcessProvider, TailscaleProvider
from .inbox_watcher import LABEL as WATCHER_LABEL
from .inbox_watcher import WATCHER_VERSION
from .inbox_watcher import RetrySubmissionError, cloud_root, dismiss_execution, queued_retry_children, submit_execution_retry, submit_predecessor_retry
from .component_logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENVIRONMENT,
    VALID_LEVELS,
    clear_component_log as clear_stored_component_log,
    component_log as stored_component_log,
    component_log_version,
    component_lifecycle_context,
    component_logger,
    log_event,
    shutdown_signal_logging,
)
from .component_lock import DuplicateComponentInstanceError, single_instance
from .codex_chat import CodexChatError, chat_model, respond as codex_chat_response
from .telemetry import daily_statistics, execution_timing
from .prompt_history import prompt_history, report_for_prompt_history
from .recommendation_handoff import handoff_from_report
from .storage import EngineeringStorageError, open_storage
from .platform_version import EngineeringPlatformManifest
from . import dashboard_state

LABEL = "com.djconnect.engineering-dashboard"
RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"
DASHBOARD_VERSION = "1.2.90"
DASHBOARD_STARTED_AT = time.monotonic()
ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_DARK = "operations-console/apple-touch-icon-dark.png"
APP_ICON_LIGHT = "operations-console/apple-touch-icon-light.png"
WEB_MANIFEST = "operations-console/manifest.webmanifest"
LOOPBACK_ADDRESS = "127.0.0.1"
CODEX_PROCESS = re.compile(r"(?:^|\s)(?:\S*/)?codex(?:\s|$)")
RATE_LIMIT_CACHE_SECONDS = 60
_rate_limit_cache_lock = Lock()
_rate_limit_cache: tuple[float, bytes] | None = None
CODEX_IDENTITY_CACHE_SECONDS = 300
_codex_identity_cache_lock = Lock()
_codex_identity_cache: tuple[float, dict[str, str]] | None = None

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
        "prompt_history_report_copied",
        "prompt_history_report_downloaded",
        "prompt_history_analysis_copied",
        "prompt_history_analysis_downloaded",
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
    return dashboard_state.snapshot(
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
        telemetry_reader=lambda workspace: daily_statistics(workspace, days=7),
        process_metrics_reader=_codex_process_metrics,
        build_commit_reader=_build_commit,
        component_log_versions_reader=_component_log_versions,
        dashboard_version=DASHBOARD_VERSION,
        worker_version=WATCHER_VERSION,
    )


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
            "usage": usage,
            "evidence": evidence,
            "recommendation_handoff": handoff,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


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
    usage: dict[str, object] = {}
    try:
        connection = open_storage(root)
        row = connection.execute(
            "SELECT input_tokens, output_tokens, total_tokens FROM execution_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        connection.close()
        if row:
            usage = {
                label: value
                for label, value in zip(("input_tokens", "output_tokens", "total_tokens"), row)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
    except Exception:
        usage = {}
    report: str | None = None
    try:
        report = _report_for_run(root, run_id).decode("utf-8")
    except UnicodeDecodeError:
        pass
    return _project_prompt_history_detail(
        entry,
        execution=execution,
        runtime=runtime,
        reviewers=reviewers,
        commits=commits,
        usage=usage,
        report=report,
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


def _codex_provider_identity() -> dict[str, str]:
    """Return the active provider identity without exposing local paths or account data."""
    global _codex_identity_cache
    now = time.monotonic()
    with _codex_identity_cache_lock:
        if _codex_identity_cache and now - _codex_identity_cache[0] < CODEX_IDENTITY_CACHE_SECONDS:
            return dict(_codex_identity_cache[1])

    identity = {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"}
    executable = shutil.which("codex")
    if executable:
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
                            "title": "Engineering Operations Console",
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


class RateLimitResetError(RuntimeError):
    """Raised when Codex cannot safely consume a reset credit."""


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
                            "title": "Engineering Operations Console",
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


def _clear_component_log(root: Path, component: str) -> None:
    """Clear exactly one canonical component log."""
    clear_stored_component_log(root, component)


def _component_log_versions(root: Path) -> dict[str, str]:
    """Return SQLite revisions so browsers fetch logs only when they changed."""
    return {component: component_log_version(root, component) for component in ("inbox", "dashboard")}


def _launch_agent_health(label: str) -> dict[str, str | bool]:
    """Inspect one owned LaunchAgent without changing its state."""
    if not LaunchdProvider().status().qualified:
        return {"healthy": False, "state": "unavailable", "detail": "launchctl ontbreekt"}
    if not LaunchdProvider().inspect(label):
        return {"healthy": False, "state": "not_running", "detail": "LaunchAgent is niet geladen"}
    return {"healthy": True, "state": "running", "detail": "LaunchAgent is geladen"}


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
            **_launch_agent_health(WATCHER_LABEL),
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


def _restart_component_after_response(component: str, logger: logging.Logger) -> None:
    """Restart after the acknowledgement and retain only a bounded failure event."""
    try:
        _restart_component(component)
    except OSError as error:
        log_event(logger, logging.ERROR, "component_restart_failed", diagnostic=str(error))


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
    }
    metadata: dict[str, str] = {}
    for key, label in fields.items():
        match = re.search(rf"^- {re.escape(label)}: `([^`\\n]{{1,120}})`$", text, re.MULTILINE)
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


def _dashboard_html(
    title: str,
    build_commit: str = "onbekend",
    workspace_id: str = "onbekend",
    workspace_location: str = ".",
    workspace_free_disk_space: str = "Niet beschikbaar",
    tracked_files: str = "Niet beschikbaar",
    engineering_database_path: str = "Niet beschikbaar",
    engineering_database_size: str = "Niet beschikbaar",
    engineering_database_schema_version: str = "Niet beschikbaar",
    platform_version: str = "1.5.0",
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
<link rel="icon" type="image/png" sizes="180x180" media="(prefers-color-scheme: dark)" href="/assets/operations-console/apple-touch-icon-dark.png">
<link rel="icon" type="image/png" sizes="180x180" media="(prefers-color-scheme: light)" href="/assets/operations-console/apple-touch-icon-light.png">
<link id="dashboardAppleTouchIcon" rel="apple-touch-icon" sizes="180x180" href="/assets/operations-console/apple-touch-icon-dark.png">
<script>try{const state=JSON.parse(localStorage.getItem("engineering-dashboard-client-state-v1")||"{}");document.documentElement.dataset.theme=state.theme==="light"?"light":"dark"}catch{document.documentElement.dataset.theme="dark"}</script>


<link rel="stylesheet" href="/assets/dashboard.css">
</head>
<body>
<a class="skip-link" href="#engineering-dashboard-content" data-i18n="header.skip"></a>
<div id="dashboardSplash" role="status" aria-live="polite" data-testid="dashboard-splash"><div class="dashboard-splash__content"><img class="dashboard-splash__icon" src="/assets/operations-console/icon-transparent.png" alt="" aria-hidden="true" data-testid="dashboard-splash-icon"><h2 class="dashboard-splash__title" id="dashboardSplashTitle" data-i18n="dashboard.title">$TITLE</h2><span class="dashboard-splash__version" id="dashboardSplashVersion" data-platform-version="$PLATFORM_VERSION">Engineering Platform $PLATFORM_VERSION</span><span class="dashboard-splash__spinner" aria-hidden="true"></span><span class="dashboard-splash__loading" id="dashboardSplashLoading" data-i18n="dashboard.loading"></span></div></div>
<div id="copyToast" role="status" aria-live="polite" aria-atomic="true" popover="manual" hidden data-testid="copy-toast"></div>
<div id="pullRefresh" role="status" aria-live="polite" aria-hidden="true" data-testid="pull-refresh" data-i18n="refresh.pull_to_refresh"></div>
<div class="dashboard-scroll-region">
<header class="dashboard-titlebar"><div class="dashboard-titlebar__brand"><img class="dashboard-app-icon" src="/assets/operations-console/icon-transparent.png" alt="" aria-hidden="true" data-testid="dashboard-app-icon"><h1 id="dashboardTitle" data-i18n="dashboard.title">$TITLE</h1></div><div class="dashboard-titlebar__actions"><button class="page-refresh" id="pageRefresh" type="button" data-testid="page-refresh" data-i18n-title="refresh.page" data-i18n-aria-label="refresh.page"><span aria-hidden="true">↻</span></button><label class="dashboard-locale" for="dashboardLocale"><span data-i18n="language.label"></span><select id="dashboardLocale" class="dashboard-locale__native" data-i18n-aria-label="language.label"><option value="nl" data-i18n="language.nl"></option><option value="en" data-i18n="language.en"></option><option value="de" data-i18n="language.de"></option><option value="fr" data-i18n="language.fr"></option><option value="es" data-i18n="language.es"></option></select><span class="dashboard-locale__picker"><button class="dashboard-locale__button" id="dashboardLocaleButton" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="dashboardLocaleMenu"><span id="dashboardLocaleValue"></span><span aria-hidden="true">⌄</span></button><span class="dashboard-locale__menu" id="dashboardLocaleMenu" role="listbox" hidden><button type="button" role="option" data-dashboard-locale="nl"></button><button type="button" role="option" data-dashboard-locale="en"></button><button type="button" role="option" data-dashboard-locale="de"></button><button type="button" role="option" data-dashboard-locale="fr"></button><button type="button" role="option" data-dashboard-locale="es"></button></span></span></label><button class="theme-toggle" id="themeToggle" type="button" role="switch" aria-checked="false" data-i18n-aria-label="header.enable_light" data-testid="theme-toggle"><span class="theme-toggle__label" data-i18n="header.theme"></span></button><button class="section-state-toggle" id="toggleAllSections" type="button" role="switch" aria-checked="false" data-i18n-aria-label="header.open_all" data-testid="toggle-all-sections"><span class="section-state-toggle__label" data-i18n="header.expand"></span></button><label class="auto-refresh-toggle" for="autoRefresh"><input id="autoRefresh" type="checkbox" role="switch" checked><span data-i18n="header.auto_refresh"></span></label></div></header>
<main class="dashboard-grid" id="engineering-dashboard-content" tabindex="-1">
<details class="inbox-queue" id="queueItems" data-testid="engineering-inbox-queue"><summary><strong data-i18n="section.inbox_queue"></strong></summary><p class="category-description" data-i18n="description.inbox_queue"></p><div class="queue-blocker" id="inboxBlocker" role="alert" hidden></div><p class="estimate-meta" id="queueSummary" data-i18n="logs.loading"></p><ol class="queue-list" id="queueList" aria-live="polite"></ol></details>
<details class="prompt-history" id="promptHistory" data-testid="engineering-prompt-history"><summary><strong data-i18n="section.prompt_history"></strong></summary><p class="category-description" data-i18n="description.prompt_history"></p><div class="log-controls"><label for="promptHistoryFilter"><span data-i18n="filter.search"></span><input id="promptHistoryFilter" type="search" maxlength="160" data-sanitize="single-line" data-i18n-placeholder="filter.search_placeholder"></label></div><div class="log-table-wrap"><table class="log-table" data-i18n-aria-label="history.table_label"><thead><tr><th data-history-sort-key="status" scope="col" data-i18n="table.status"></th><th data-history-sort-key="title" scope="col" data-i18n="table.prompt_title"></th><th data-history-sort-key="executed_at" scope="col" data-i18n="table.executed_at"></th><th scope="col" data-i18n="table.report"></th><th id="promptHistoryAnalysisHeader" scope="col" data-i18n="table.analysis"></th><th id="promptHistoryChatHeader" scope="col" data-i18n="table.chat"></th><th scope="col" data-i18n="table.action"></th><th id="promptHistoryDetailsHeader" scope="col" data-i18n="table.details"></th></tr></thead><tbody id="promptHistoryRows"><tr><td class="log-empty" colspan="8" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="promptHistoryPagination" data-i18n-aria-label="history.table_label"></nav></details>
<details class="current-run" id="currentRun" data-i18n-aria-label="detail.execution" hidden><summary class="current-run__title"><span class="label" data-i18n="section.active_prompt"></span></summary><div class="current-run__grid"><div class="field"><span class="label" data-i18n="detail.prompt_title"></span><h2 id="currentPrompt" data-i18n="format.loading"></h2></div><div class="field"><span class="label" data-i18n="ui.filename"></span><pre id="currentFile" data-i18n="format.loading"></pre></div>
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" data-i18n-aria-label="status.unknown"></span><strong data-i18n="detail.prompt_status"></strong></div><p class="field"><span class="label" data-i18n="ui.watcher"></span><span id="watcher" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.phase"></span><span id="phase" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.current_activity"></span><span id="action" data-i18n="format.loading"></span></p></div>
<div class="card" id="predecessorGate" hidden><strong data-i18n="status.blocked"></strong><p class="field"><span class="label" data-i18n="detail.run_id"></span><code id="predecessorRun"></code></p><p class="field"><span class="label" data-i18n="ui.preceding_prompt"></span><span id="predecessorPrompt"></span></p><p class="field"><span class="label" data-i18n="field.terminal_state"></span><span id="predecessorPhase"></span></p><div class="field"><span class="label" data-i18n="ui.recovery_action"></span><pre id="predecessorAction"></pre></div><button class="predecessor-retry" id="predecessorRetry" type="button" data-i18n="action.resume_queue"></button><p class="predecessor-retry-status" id="predecessorRetryStatus" role="status" aria-live="polite"></p></div>
<div class="card"><strong data-i18n="ui.estimated_execution_time"></strong><p class="estimate-primary" id="executionEstimate" data-i18n="estimate.not_available"></p><p class="estimate-meta" id="executionEstimateMeta" hidden></p></div>
<div class="card"><strong data-i18n="detail.execution"></strong><p class="field"><span class="label" data-i18n="detail.run_id"></span><span id="runId"></span></p><p class="field"><span class="label" data-i18n="ui.prompt_started"></span><span id="promptStarted" data-i18n="format.loading"></span></p></div>
<div class="card execution-context" id="executionContext" hidden><strong data-i18n="ui.execution_context"></strong><p class="field"><span class="label" data-i18n="field.execution_mode"></span><span id="executionMode"></span></p><p class="field"><span class="label" data-i18n="field.repository"></span><span id="targetRepository"></span></p><div class="field"><span class="label" data-i18n="detail.target_checkout"></span><pre id="checkoutPath"></pre></div><p class="field"><span class="label" data-i18n="ui.active_branch"></span><span id="activeBranch"></span></p></div>
<div class="card" id="processMetrics" hidden><strong data-i18n="ui.local_ai_processes"></strong><p class="field"><span class="label">CPU</span><span id="codexCpu" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.process_count"></span><span id="codexProcesses" data-i18n="format.loading"></span></p><p class="field"><span class="label" data-i18n="ui.gpu_usage"></span><span id="codexGpu" data-i18n="format.loading"></span></p></div>
<div class="card" id="usage" hidden><strong>Codex CLI</strong><div class="field"><span class="label" data-i18n="ui.reported_usage"></span><pre id="usageDetails"></pre></div></div>
<div class="card" id="currentDiagnostic" hidden><strong>Codex CLI</strong><pre id="currentLog" data-i18n="format.loading"></pre></div>
</div></details>
<details class="card card--resource" id="rateLimits" hidden><summary><strong data-i18n="section.remaining_usage"></strong></summary><div class="field"><span class="label" data-i18n="ui.current_ai_provider"></span><span id="rateLimitProvider" data-i18n="format.loading"></span></div><div class="field"><span class="label" id="rateLimitLabel">Codex CLI</span><pre id="rateLimitDetails"></pre></div><button class="rate-limit-reset" id="rateLimitReset" type="button" hidden data-i18n="ui.reset_ready"></button><p class="rate-limit-reset-status" id="rateLimitResetStatus" role="status" aria-live="polite"></p></details>
<details class="platform-health" id="platformHealth" data-testid="platform-health"><summary><strong data-i18n="section.platform_components"></strong></summary><p class="category-description" data-i18n="description.platform_components"></p><div class="platform-health__components" id="platformHealthComponents" aria-live="polite"><p class="platform-health__empty" data-i18n="ui.component_health_loading"></p></div></details>
<dialog class="dashboard-modal-shell dashboard-modal-shell--component component-modal" id="componentModal" aria-labelledby="componentModalTitle"><section class="dashboard-modal-shell__panel component-modal__panel"><header class="dashboard-modal-shell__header component-modal__header"><h2 id="componentModalTitle" data-i18n="ui.component_information"></h2><button class="dashboard-modal-shell__close component-modal__close" id="componentModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><div id="componentModalContent"></div><button class="component-modal__restart" id="componentModalRestart" type="button" hidden data-i18n="ui.component_restart"></button><p class="component-modal__status" id="componentModalStatus" aria-live="polite"></p></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="confirmationModal" aria-labelledby="confirmationModalTitle"><section class="dashboard-modal-shell__panel confirmation-modal__panel"><header class="dashboard-modal-shell__header confirmation-modal__header"><h2 id="confirmationModalTitle" data-i18n="ui.confirm_action"></h2><button class="dashboard-modal-shell__close confirmation-modal__close" id="confirmationModalClose" type="button" data-i18n-aria-label="sections.close">×</button></header><p id="confirmationModalText"></p><div class="dashboard-modal-shell__actions confirmation-modal__actions"><button class="dashboard-modal-shell__action" id="confirmationModalCancel" type="button" data-i18n="action.cancel"></button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="confirmationModalConfirm" type="button" data-i18n="action.confirm"></button></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence report-view-modal" id="promptHistoryReportModal" aria-labelledby="promptHistoryReportModalTitle"><section class="dashboard-modal-shell__panel report-view-modal__panel"><header class="dashboard-modal-shell__header report-view-modal__header"><h2 class="report-view-modal__title" id="promptHistoryReportModalTitle" data-i18n="history.report_title"></h2><div class="report-view-modal__actions"><button class="dashboard-action dashboard-action--download download download--glyph" id="promptHistoryReportDownload" type="button" hidden>⇩</button><button class="dashboard-action dashboard-action--copy copy copy--glyph" id="promptHistoryReportCopy" type="button" hidden>⧉</button><button class="dashboard-modal-shell__close report-view-modal__close" id="promptHistoryReportClose" type="button" data-i18n-aria-label="sections.close">×</button></div></header><article class="markdown-document report-view-modal__content" id="promptHistoryReportContent" data-i18n="history.report_loading"></article></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--evidence prompt-detail-modal" id="promptHistoryDetailModal" aria-labelledby="promptHistoryDetailTitle"><section class="dashboard-modal-shell__panel prompt-detail-modal__panel"><header class="dashboard-modal-shell__header prompt-detail-modal__header"><h2 id="promptHistoryDetailTitle" data-i18n="history.details_loading"></h2><button class="dashboard-modal-shell__close prompt-detail-modal__close" id="promptHistoryDetailClose" type="button" data-i18n-aria-label="sections.close">×</button></header><p class="prompt-detail-modal__description" id="promptHistoryDetailDescription"></p><div class="prompt-detail-modal__content" id="promptHistoryDetailContent" data-i18n="history.details_loading"></div></section></dialog>
<dialog class="dashboard-modal-shell dashboard-modal-shell--chat prompt-chat-modal" id="promptHistoryChatModal" aria-labelledby="promptHistoryChatTitle"><section class="dashboard-modal-shell__panel prompt-chat-modal__panel"><header class="dashboard-modal-shell__header prompt-chat-modal__header"><h2 id="promptHistoryChatTitle" data-i18n="section.ai_conversation"></h2><button class="dashboard-modal-shell__close prompt-chat-modal__close" id="promptHistoryChatClose" type="button" data-i18n-aria-label="sections.close">×</button></header><p class="prompt-chat-modal__description" id="promptHistoryChatDescription"></p><section class="codex-chat" id="codexChat"><div class="codex-chat__details"><div class="chat-actions"><button class="dashboard-action dashboard-action--download download download--glyph" id="downloadChat" type="button" hidden>⇩</button><button class="dashboard-action dashboard-action--copy" id="copyChat" type="button" hidden data-i18n-title="chat.copy_title" data-i18n-aria-label="chat.copy_title">⧉</button><button class="dashboard-action dashboard-action--destructive" id="clearChat" type="button" hidden>⌫</button></div><div class="chat-messages" id="chatMessages" aria-live="polite" data-i18n-aria-label="section.ai_conversation"></div><label class="label chat-question-label" for="chatInput" data-i18n="section.new_ai_question"></label><div class="chat-compose"><textarea id="chatInput" class="chat-input" rows="5" maxlength="2000" autocomplete="off" data-sanitize="multiline" data-i18n-placeholder="history.chat_placeholder"></textarea><button class="chat-send" id="chatSend" type="button" data-i18n-aria-label="action.confirm"><span aria-hidden="true">➤</span></button></div><div class="chat-meta"><p class="field"><span class="label" data-i18n="detail.model"></span><span id="chatModel">$CHAT_MODEL</span></p><p class="chat-status" id="chatStatus"></p></div></div></section></section></dialog>
<button id="loadComponentLogs" type="button" hidden data-i18n="logs.loading"></button>
<details class="technical-details" id="componentLogs"><summary><strong data-i18n="section.logs"></strong></summary><p class="estimate-meta" data-i18n="description.logs"></p><div class="log-controls" id="componentLogControls" hidden><label for="logFilter"><span data-i18n="filter.search"></span><input id="logFilter" type="search" maxlength="160" data-sanitize="single-line" data-i18n-placeholder="filter.search_placeholder"></label><label for="logLevelFilter"><span data-i18n="filter.level"></span><select id="logLevelFilter"><option value="" data-i18n="filter.all_levels"></option><option value="ERROR" data-i18n="filter.error"></option><option value="WARNING" data-i18n="filter.warning"></option><option value="INFO" data-i18n="filter.info"></option><option value="DEBUG" data-i18n="filter.debug"></option></select></label></div><div class="technical-grid"><div class="card"><div class="log-card-header"><strong data-i18n="logs.inbox_watcher"></strong><div class="log-card-actions"><button class="dashboard-action dashboard-action--download download download--glyph component-log-download" data-component="inbox" data-testid="download-inbox-log" type="button" data-i18n-title="logs.download_inbox" data-i18n-aria-label="logs.download_inbox">⇩</button><button class="dashboard-action dashboard-action--destructive clear-component-log" data-component="inbox" data-testid="clear-inbox-log" type="button" data-i18n-title="action.clear_logs" data-i18n-aria-label="action.clear_logs">⌫</button></div></div><div class="log-table-wrap"><table class="log-table" data-i18n-aria-label="logs.inbox_watcher"><thead><tr><th data-i18n="table.number"></th><th data-i18n="table.timestamp"></th><th data-i18n="table.level"></th><th data-i18n="table.event"></th><th data-i18n="table.run_id"></th><th data-i18n="table.details"></th></tr></thead><tbody id="inboxComponentLog"><tr><td class="log-empty" colspan="6" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="inboxLogPagination" data-i18n-aria-label="logs.inbox_watcher"></nav></div><div class="card"><div class="log-card-header"><strong data-i18n="logs.status_dashboard"></strong><div class="log-card-actions"><button class="dashboard-action dashboard-action--download download download--glyph component-log-download" data-component="dashboard" data-testid="download-dashboard-log" type="button" data-i18n-title="logs.download_dashboard" data-i18n-aria-label="logs.download_dashboard">⇩</button><button class="dashboard-action dashboard-action--destructive clear-component-log" data-component="dashboard" data-testid="clear-dashboard-log" type="button" data-i18n-title="action.clear_logs" data-i18n-aria-label="action.clear_logs">⌫</button></div></div><div class="log-table-wrap"><table class="log-table" data-i18n-aria-label="logs.status_dashboard"><thead><tr><th data-i18n="table.number"></th><th data-i18n="table.timestamp"></th><th data-i18n="table.level"></th><th data-i18n="table.event"></th><th data-i18n="table.run_id"></th><th data-i18n="table.details"></th></tr></thead><tbody id="dashboardComponentLog"><tr><td class="log-empty" colspan="6" data-i18n="logs.loading"></td></tr></tbody></table></div><nav class="log-pagination" id="dashboardLogPagination" data-i18n-aria-label="logs.status_dashboard"></nav></div></div></details>
<details class="technical-details" id="technicalDetails"><summary><strong data-i18n="section.technical_details"></strong></summary><p class="category-description" data-i18n="description.technical_details"></p><div class="technical-grid">
<div class="card"><strong id="technicalPullRequestsTitle" data-i18n="technical.pull_requests"></strong><p class="field"><span class="label" id="technicalImplementationLabel" data-i18n="technical.implementation"></span><span id="implementation"></span></p><p class="field"><span class="label" id="technicalFinalizationLabel" data-i18n="technical.finalization"></span><span id="finalization"></span></p></div>
<div class="card"><strong id="technicalRepositoryTitle" data-i18n="technical.repository"></strong><p class="field"><span class="label" id="technicalRepositoryStateLabel" data-i18n="technical.repository_status"></span><span id="repositoryState"></span></p><p class="field"><span class="label" id="technicalWorkspaceStateLabel" data-i18n="technical.workspace_status"></span><span id="workspaceState"></span></p></div>
<div class="card"><strong id="technicalHostPreflightTitle" data-i18n="technical.host_preflight"></strong><p class="field"><span class="label" id="technicalExecutionHostLabel" data-i18n="technical.execution_host"></span><span id="executionHostName" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalExecutionHostVersionLabel" data-i18n="technical.execution_host_version"></span><span id="executionHostVersion" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRuntimeLabel" data-i18n="technical.runtime"></span><span id="executionHostRuntime" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRuntimePromptTransportLabel" data-i18n="technical.runtime_prompt_transport"></span><span id="executionHostTransport" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalHostStatusLabel" data-i18n="technical.host_status"></span><span id="hostPreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalLastCheckLabel" data-i18n="technical.last_check"></span><span id="hostPreflightTimestamp" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalWorkspacePreflightStatusLabel" data-i18n="technical.workspace_status"></span><span id="workspacePreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalLastWorkspaceCheckLabel" data-i18n="technical.last_workspace_check"></span><span id="workspacePreflightTimestamp" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalCapabilityStatusLabel" data-i18n="technical.capability_status"></span><span id="capabilityPreflightStatus" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRecoverabilityLabel" data-i18n="technical.recoverability"></span><span id="capabilityRecoverability" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalFailureOriginLabel" data-i18n="technical.failure_origin"></span><span id="capabilityFailureOrigin" data-i18n="format.unavailable"></span></p><p class="field"><span class="label" id="technicalRecommendationLabel" data-i18n="technical.recommended_action"></span><span id="capabilityRecommendation" data-i18n="format.unavailable"></span></p></div>
<div class="card" id="driftDiagnosticsCard" hidden><strong data-i18n="technical.current_drift"></strong><p class="field"><span class="label" data-i18n="technical.severity"></span><span id="driftSeverity"></span></p><p class="field"><span class="label" data-i18n="technical.affected_component"></span><span id="driftComponent"></span></p><p class="field"><span class="label" data-i18n="technical.expected_state"></span><span id="driftExpected"></span></p><p class="field"><span class="label" data-i18n="technical.observed_state"></span><span id="driftObserved"></span></p><p class="field"><span class="label" data-i18n="technical.resolution"></span><span id="driftResolution"></span></p></div>
<div class="card"><strong id="technicalDiagnosticsTitle" data-i18n="technical.diagnostics"></strong><p id="diag"></p></div>
</div></details>
<details class="card card--context workspace-card" id="workspaceCard" data-testid="engineering-workspace"><summary><strong data-i18n="section.workspace"></strong></summary><p class="field"><span class="label" data-i18n="workspace.name"></span><span>$WORKSPACE_ID</span></p><div class="field"><span class="label" data-i18n="ui.workspace_location"></span><pre>$WORKSPACE_LOCATION</pre></div><p class="field"><span class="label" data-i18n="workspace.free_disk_space"></span><span>$WORKSPACE_FREE_DISK_SPACE</span></p><p class="field"><span class="label" data-i18n="detail.tracked_files"></span><span>$TRACKED_FILES</span></p><div class="field"><span class="label" data-i18n="workspace.database"></span><pre>$ENGINEERING_DATABASE_PATH</pre></div><p class="field"><span class="label" data-i18n="workspace.database_size"></span><span>$ENGINEERING_DATABASE_SIZE</span></p><p class="field"><span class="label" data-i18n="workspace.schema_version"></span><span>$ENGINEERING_DATABASE_SCHEMA_VERSION</span></p></details>
</main></div>
<footer class="footer" aria-live="polite"><span class="footer__item"><span class="label" id="platformVersionLabel" data-i18n="footer.platform_version"></span><span id="platformVersion" data-i18n="format.loading"></span></span><span class="footer__separator" aria-hidden="true">·</span><span class="footer__item" id="lastRefresh" data-i18n="format.loading"></span><span class="footer__separator" aria-hidden="true">·</span><span class="footer__item" id="updateMode" data-i18n="format.loading"></span></footer><span id="dashboardVersion" hidden></span><span id="workerVersion" hidden></span>
<script>window.DJCONNECT_DASHBOARD_BUILD="$BUILD_COMMIT";</script>
<script src="/assets/dashboard.js" type="module"></script>

</body>
</html>"""
    return (
        page.replace("$TITLE", escape(title))
        .replace("$BUILD_COMMIT", escape(build_commit))
        .replace("$CHAT_MODEL", escape(chat_model()))
        .replace("$WORKSPACE_ID", escape(workspace_id))
        .replace("$WORKSPACE_LOCATION", escape(workspace_location))
        .replace("$WORKSPACE_FREE_DISK_SPACE", escape(workspace_free_disk_space))
        .replace("$TRACKED_FILES", escape(tracked_files))
        .replace("$ENGINEERING_DATABASE_PATH", escape(engineering_database_path))
        .replace("$ENGINEERING_DATABASE_SIZE", escape(engineering_database_size))
        .replace("$ENGINEERING_DATABASE_SCHEMA_VERSION", escape(engineering_database_schema_version))
        .replace("$PLATFORM_VERSION", escape(platform_version))
        .encode()
    )


def handler(root: Path, logger: logging.Logger | None = None):
    configuration = PlatformConfiguration.load(root)
    title = configuration.workspace.dashboard_title
    workspace_id = configuration.workspace.id
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
            if request_path in {"/api/queue-recovery", "/api/predecessor-retry"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
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
            if request_path == "/api/execution-retry":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"run_id"} or not isinstance(payload["run_id"], str):
                        raise ValueError
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
            if request_path != "/api/codex-chat":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_000:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) not in (
                    {"message", "history"},
                    {"message", "history", "run_id"},
                ):
                    raise ValueError
                status = json.loads(_status(root))
                answer = codex_chat_response(
                    root, status, payload["message"], payload["history"], payload.get("run_id")
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
                json.dumps({"answer": answer, "model": chat_model()}, ensure_ascii=False).encode(),
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
                # Legacy paths keep the first paint intact while the dashboard
                # selects the themed Operations Console icon after startup.
                "/assets/engineering-status-icon.svg": ("engineering-status-icon.svg", "image/svg+xml; charset=utf-8"),
                "/assets/engineering-status-icon-180.png": ("engineering-status-icon-180.png", "image/png"),
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
                    self.wfile.write(b"retry: 1000\n\n")
                    previous: bytes | None = None
                    for second in range(300):
                        snapshot = _sse_snapshot(root)
                        if snapshot != previous:
                            self.wfile.write(b"event: dashboard\ndata: " + snapshot + b"\n\n")
                            self.wfile.flush()
                            previous = snapshot
                        elif second and second % 15 == 0:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        time.sleep(1)
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
                return self._send(
                    _component_log(root, request.path.rsplit("/", 1)[-1]),
                    "text/plain; charset=utf-8",
                )
            if self.path == "/api/log/current":
                return self._send(_current_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/usage":
                return self._send(_codex_usage(root), "application/json; charset=utf-8")
            if self.path == "/api/commits":
                return self._send(_completion_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/prompt-started":
                return self._send(_prompt_started(root), "application/json; charset=utf-8")
            if self.path == "/":
                engineering_database = _engineering_database_details(root)
                workspace_free_disk_space = _workspace_free_disk_space(root)
                return self._send(
                    _dashboard_html(
                        title,
                        _build_commit(root),
                        workspace_id,
                        workspace_location,
                        workspace_free_disk_space,
                        tracked_files,
                        engineering_database["path"],
                        engineering_database["size"],
                        engineering_database["schema_version"],
                        platform_version,
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
    """Render the only owned per-user LaunchAgent; no network policy changes."""
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher = (sys.executable, "-m", "tools.engineering.dashboard", "run", "--repo", str(repo))
    command = f"cd {shlex.quote(str(repo))} && exec " + " ".join(shlex.quote(value) for value in launcher)
    arguments = f"<string>/bin/zsh</string><string>-lc</string><string>{escape(command)}</string>"
    log_level = os.environ.get(LOG_LEVEL_ENVIRONMENT, DEFAULT_LOG_LEVEL).upper()
    if log_level not in VALID_LEVELS:
        log_level = DEFAULT_LOG_LEVEL
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>EnvironmentVariables</key><dict><key>{LOG_LEVEL_ENVIRONMENT}</key><string>{log_level}</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer></dict></plist>',
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
