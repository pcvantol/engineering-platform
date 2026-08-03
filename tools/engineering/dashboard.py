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
import subprocess
import sys
from threading import Lock, Timer
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from .platform_api import PlatformConfiguration
from .platform_bootstrap import provision_workspace
from .providers import TailscaleProvider
from .providers import LaunchdProvider
from .inbox_watcher import LABEL as WATCHER_LABEL
from .inbox_watcher import WATCHER_VERSION
from .inbox_watcher import RetrySubmissionError, cloud_root, dismiss_execution, submit_execution_retry, submit_predecessor_retry
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
from .platform_version import EngineeringPlatformManifest
from . import dashboard_state

LABEL = "com.djconnect.engineering-dashboard"
RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"
DASHBOARD_VERSION = "1.2.90"
DASHBOARD_STARTED_AT = time.monotonic()
ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_SVG = "engineering-status-icon.svg"
APP_ICON_TOUCH = "engineering-status-icon-180.png"
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
        payload = {"runs": prompt_history(root)}
    except Exception:
        payload = {"runs": []}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


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
            completed = subprocess.run(
                (executable, "--version"),
                text=True,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
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
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            ("codex", "app-server"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
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
                            "title": "Engineering Status",
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
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
    return json.dumps(identity, separators=(",", ":")).encode()


class RateLimitResetError(RuntimeError):
    """Raised when Codex cannot safely consume a reset credit."""


def _consume_codex_rate_limit_reset_credit() -> str:
    """Consume exactly one available Codex reset credit through its app-server API."""
    global _rate_limit_cache
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            ("codex", "app-server"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
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
                            "title": "Engineering Status",
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
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
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
    executable = shutil.which("launchctl")
    if not executable:
        return {"healthy": False, "state": "unavailable", "detail": "launchctl ontbreekt"}
    observed = subprocess.run(
        (executable, "print", f"gui/{os.getuid()}/{label}"),
        text=True,
        capture_output=True,
        check=False,
    )
    if observed.returncode:
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
        observed = subprocess.run(
            ("ps", "-axo", "pid=,rss=,etime=,command="),
            text=True,
            capture_output=True,
            check=False,
        )
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
    executable = shutil.which("launchctl")
    if not executable:
        raise OSError("launchctl ontbreekt.")
    observed = subprocess.run(
        (executable, "kickstart", "-k", f"gui/{os.getuid()}/{COMPONENT_LABELS[component]}"),
        text=True,
        capture_output=True,
        check=False,
    )
    if observed.returncode:
        raise OSError(observed.stderr.strip() or "De herstart is niet gelukt.")


def _restart_component_after_response(component: str, logger: logging.Logger) -> None:
    """Restart after the acknowledgement and retain only a bounded failure event."""
    try:
        _restart_component(component)
    except OSError as error:
        log_event(logger, logging.ERROR, "component_restart_failed", diagnostic=str(error))


def _codex_process_metrics() -> bytes:
    """Return read-only local CPU evidence for currently running Codex CLI processes."""
    try:
        observed = subprocess.run(
            ("ps", "-axo", "pid=,pcpu=,command="),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        observed = None
    processes: list[dict[str, int | float]] = []
    if observed and observed.returncode == 0:
        for line in observed.stdout.splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) != 3 or not CODEX_PROCESS.search(parts[2]):
                continue
            try:
                processes.append({"pid": int(parts[0]), "cpu_percent": float(parts[1])})
            except ValueError:
                continue
    return json.dumps(
        {
            "process_count": len(processes),
            "cpu_percent": round(sum(item["cpu_percent"] for item in processes), 1),
            "gpu_status": "Niet beschikbaar: Codex-verwerking draait extern.",
        },
        separators=(",", ":"),
    ).encode()


def _report_for_run(root: Path, run_id: str | None) -> bytes:
    """Return report evidence only for the exact displayed terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    try:
        reports = sorted((root / ".engineering" / "reports").glob(f"*_{run_id}.md"))
        return reports[-1].read_bytes() if reports else b""
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
    """Return only CLI-reported usage bound to the displayed current or last run."""
    try:
        status = json.loads(_status(root))
        recorded = json.loads((root / ".engineering" / "status" / "codex_usage.json").read_text(encoding="utf-8"))
        run_id = recorded.get("run_id")
        usage = recorded.get("usage")
    except (OSError, json.JSONDecodeError):
        return b"{}"
    if run_id not in {status.get("run_id"), status.get("last_executed_run")} or not isinstance(usage, dict):
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
        checkpoint = json.loads((root / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return b"{}"
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    commits = {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}
    return json.dumps(commits, separators=(",", ":")).encode()


def _last_executed_commits(root: Path) -> bytes:
    """Return commit evidence bound to the final last-executed run only."""
    try:
        status = json.loads(_status(root))
        run_id = status.get("last_executed_run")
        phase = status.get("last_executed_phase")
        if not isinstance(run_id, str) or phase != "COMPLETE":
            return b"{}"
        checkpoint = json.loads(
            (root / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return b"{}"
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    commits = {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}
    return json.dumps(commits, separators=(",", ":")).encode()


def _last_executed_agent_execution(root: Path, run_id: str | None) -> bytes:
    """Return run-bound AI timing and terminal timestamp evidence."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"{}"
    result: dict[str, float] = {}
    try:
        checkpoint = json.loads(
            (root / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(
                encoding="utf-8"
            )
        )
        seconds = checkpoint.get("agent_execution_seconds")
    except (OSError, json.JSONDecodeError):
        seconds = None
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
    observed = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "--short=12", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    return observed.stdout.strip() if observed.returncode == 0 else "onbekend"


def _tracked_file_count(root: Path) -> str:
    """Return the recursive count of files tracked by the workspace Git repository."""
    try:
        observed = subprocess.run(
            ("git", "-C", str(root), "ls-files", "-z"),
            capture_output=True,
            check=False,
        )
    except OSError:
        return "Niet beschikbaar"
    if observed.returncode != 0:
        return "Niet beschikbaar"
    return str(sum(1 for path in observed.stdout.split(b"\0") if path))


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
    tracked_files: str = "Niet beschikbaar",
    engineering_database_path: str = "Niet beschikbaar",
    engineering_database_size: str = "Niet beschikbaar",
    engineering_database_schema_version: str = "Niet beschikbaar",
    platform_version: str = "1.5.0",
) -> bytes:
    """Render the private dashboard with a server-pushed status stream."""
    page = r"""<!doctype html>
<html lang="nl">
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta id="dashboardThemeColor" name="theme-color" content="#15151d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="$TITLE">
<title>$TITLE</title>
<link id="dashboardFavicon" rel="icon" type="image/svg+xml" href="/assets/engineering-status-icon.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/assets/engineering-status-icon-180.png">
<script>try{const state=JSON.parse(localStorage.getItem("engineering-dashboard-client-state-v1")||"{}");document.documentElement.dataset.theme=state.theme==="light"?"light":"dark"}catch{document.documentElement.dataset.theme="dark"}</script>


<link rel="stylesheet" href="/assets/dashboard.css">
</head>
<body>
<a class="skip-link" href="#engineering-dashboard-content">Naar dashboardinhoud</a>
<div id="dashboardSplash" role="status" aria-live="polite" data-testid="dashboard-splash"><div class="dashboard-splash__content"><h2 class="dashboard-splash__title">$TITLE</h2><span class="dashboard-splash__version">Engineering Platform $PLATFORM_VERSION</span><span class="dashboard-splash__spinner" aria-hidden="true"></span><span class="dashboard-splash__loading">Gegevens laden…</span></div></div>
<div id="copyToast" role="status" aria-live="polite" aria-atomic="true" hidden data-testid="copy-toast"></div>
<div id="pullRefresh" role="status" aria-live="polite" aria-hidden="true" data-testid="pull-refresh">Trek omlaag om te vernieuwen</div>
<header class="dashboard-titlebar"><div class="dashboard-titlebar__brand"><img class="dashboard-app-icon" src="/assets/engineering-status-icon.svg" alt="" aria-hidden="true" data-testid="dashboard-app-icon"><h1>$TITLE</h1></div><div class="dashboard-titlebar__actions"><button class="theme-toggle" id="themeToggle" type="button" role="switch" aria-checked="false" aria-label="Lichte modus inschakelen" data-testid="theme-toggle"><span class="theme-toggle__label">Thema</span></button><button class="section-state-toggle" id="toggleAllSections" type="button" role="switch" aria-checked="false" aria-label="Alle secties openen" data-testid="toggle-all-sections"><span class="section-state-toggle__label">Uitklappen</span></button><label class="auto-refresh-toggle" for="autoRefresh"><input id="autoRefresh" type="checkbox" role="switch" checked><span>Automatisch vernieuwen</span></label></div></header>
<div class="dashboard-scroll-region">
<details class="card card--context workspace-card" id="workspaceCard" data-testid="engineering-workspace"><summary><strong>Workspace</strong></summary><p class="field"><span class="label">Naam</span><span>$WORKSPACE_ID</span></p><div class="field"><span class="label">Workspace locatie</span><pre>$WORKSPACE_LOCATION</pre></div><p class="field"><span class="label">Tracked files</span><span>$TRACKED_FILES</span></p><div class="field"><span class="label">Engineering-database</span><pre>$ENGINEERING_DATABASE_PATH</pre></div><p class="field"><span class="label">Databasegrootte</span><span>$ENGINEERING_DATABASE_SIZE</span></p><p class="field"><span class="label">Schema-versie</span><span>$ENGINEERING_DATABASE_SCHEMA_VERSION</span></p></details>
<main class="dashboard-grid" id="engineering-dashboard-content" tabindex="-1">
<details class="inbox-queue" id="queueItems" data-testid="engineering-inbox-queue"><summary><strong>Inbox-wachtrij</strong></summary><p class="category-description">Prompts worden uitgevoerd op volgorde van aanmaakdatum.</p><p class="estimate-meta" id="queueSummary">Wachtrij laden…</p><ol class="queue-list" id="queueList" aria-live="polite"></ol></details>
<details class="prompt-history" id="promptHistory" data-testid="engineering-prompt-history"><summary><strong>Promptgeschiedenis</strong></summary><p class="category-description">Alle terminale Engineering Platform-uitvoeringen, lokaal gecachet in de Engineering SQLite-opslag.</p><div class="log-controls"><label for="promptHistoryFilter">Zoeken<input id="promptHistoryFilter" type="search" maxlength="160" data-sanitize="single-line" placeholder="Zoek in alle velden"></label></div><div class="log-table-wrap"><table class="log-table" aria-label="Promptgeschiedenis"><thead><tr><th data-history-sort-key="status" scope="col">Status</th><th data-history-sort-key="title" scope="col">Prompttitel</th><th data-history-sort-key="executed_at" scope="col">Uitgevoerd op</th><th data-history-sort-key="git_commit" scope="col">Git-commit</th><th scope="col">Rapport</th><th scope="col">Actie</th></tr></thead><tbody id="promptHistoryRows"><tr><td class="log-empty" colspan="6">Promptgeschiedenis laden…</td></tr></tbody></table></div><nav class="log-pagination" id="promptHistoryPagination" aria-label="Paginering Promptgeschiedenis"></nav></details>
<details class="current-run" id="currentRun" aria-label="Huidige uitvoering" hidden><summary class="current-run__title"><span class="label">Actieve prompt</span><h2 id="currentPrompt">Laden…</h2><div class="field"><span class="label">Bestandsnaam</span><pre id="currentFile">Laden…</pre></div></summary><div class="current-run__grid">
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" aria-label="Status onbekend"></span><strong>Promptstatus</strong></div><p class="field"><span class="label">Watcher</span><span id="watcher">Laden…</span></p><p class="field"><span class="label">Fase</span><span id="phase">Laden…</span></p><p class="field"><span class="label">Huidige Codex-activiteit</span><span id="action">Laden…</span></p></div>
<div class="card" id="predecessorGate" hidden><strong>Wachtrij geblokkeerd</strong><p class="field"><span class="label">Blokkerende run</span><code id="predecessorRun"></code></p><p class="field"><span class="label">Voorafgaande prompt</span><span id="predecessorPrompt"></span></p><p class="field"><span class="label">Eindstatus</span><span id="predecessorPhase"></span></p><div class="field"><span class="label">Herstelactie</span><pre id="predecessorAction"></pre></div><button class="predecessor-retry" id="predecessorRetry" type="button">Resume Queue</button><p class="predecessor-retry-status" id="predecessorRetryStatus" role="status" aria-live="polite"></p></div>
<div class="card"><strong>Geschatte uitvoeringstijd</strong><p class="estimate-primary" id="executionEstimate">Nog niet beschikbaar…</p><p class="estimate-meta" id="executionEstimateMeta" hidden></p></div>
<div class="card"><strong>Uitvoering</strong><p class="field"><span class="label">Run-ID</span><span id="runId"></span></p><p class="field"><span class="label">Prompt gestart op</span><span id="promptStarted">Laden…</span></p><p class="field"><span class="label">Wachtrij</span><span id="queue"></span></p></div>
<div class="card execution-context" id="executionContext" hidden><strong>Uitvoeringscontext</strong><p class="field"><span class="label">Modus</span><span id="executionMode"></span></p><p class="field"><span class="label">Repository</span><span id="targetRepository"></span></p><div class="field"><span class="label">Lokale checkout</span><pre id="checkoutPath"></pre></div><p class="field"><span class="label">Actieve branch</span><span id="activeBranch"></span></p></div>
<div class="card" id="processMetrics" hidden><strong>Lokale Codex-processen</strong><p class="field"><span class="label">CPU-gebruik</span><span id="codexCpu">Laden…</span></p><p class="field"><span class="label">Actieve processen</span><span id="codexProcesses">Laden…</span></p><p class="field"><span class="label">GPU-gebruik</span><span id="codexGpu">Laden…</span></p></div>
<div class="card" id="usage" hidden><strong>Codex CLI-gebruik</strong><div class="field"><span class="label">Gerapporteerd verbruik</span><pre id="usageDetails"></pre></div></div>
<div class="card" id="currentDiagnostic" hidden><strong>Codex CLI-diagnose</strong><pre id="currentLog">Laden…</pre></div>
</div></details>
<details class="card card--resource" id="rateLimits" hidden><summary><strong>Resterend gebruik</strong></summary><div class="field"><span class="label">Huidige AI-provider</span><span id="rateLimitProvider">Laden…</span></div><div class="field"><span class="label" id="rateLimitLabel">Codex-gebruikslimieten</span><pre id="rateLimitDetails"></pre></div><button class="rate-limit-reset" id="rateLimitReset" type="button" hidden>Gebruik reset</button><p class="rate-limit-reset-status" id="rateLimitResetStatus" role="status" aria-live="polite"></p></details>
<section class="prompt-runs" id="promptRuns" aria-label="Promptuitvoeringen" hidden><div class="prompt-runs__cards">
<div class="last-execution last-execution-group" id="lastExecutionGroup" data-testid="last-executed-prompt-category"><article class="card card--previous last-execution-card" id="lastExecution" hidden><div class="final-status"><span id="lastIndicator" class="indicator indicator--small" aria-hidden="true"></span><span class="label">Prompt status</span><span id="lastFinalStatus"></span></div><p class="field"><span class="label">Prompttitel</span><span id="lastPrompt"></span></p><div class="field"><span class="label">Aangeleverd als</span><pre id="lastFile"></pre></div><div class="field" id="lastRuntimeProvider" hidden><span class="label">Runtimeprovider</span><span id="lastRuntimeProviderValue"></span></div><div class="field" id="lastModel" hidden><span class="label">Gebruikt model</span><span id="lastModelValue"></span></div><div class="field" id="lastReasoningProfile" hidden><span class="label">Reasoning-profiel</span><span id="lastReasoningProfileValue"></span></div><div class="field" id="lastConfigurationProfile" hidden><span class="label">Configuratieprofiel</span><span id="lastConfigurationProfileValue"></span></div><div class="field" id="lastCodexCliVersion" hidden><span class="label">Codex CLI-versie</span><span id="lastCodexCliVersionValue"></span></div><div class="field" id="lastCommits" hidden><span class="label">Git-commit</span><pre id="lastCommitDetails"></pre></div><div class="field" id="lastUsage" hidden><span class="label">Codex CLI-gebruik</span><pre id="lastUsageDetails"></pre></div><div class="field" id="lastDiagnostic" hidden><span class="label">Codex CLI-diagnose</span><pre id="lastLog">Laden…</pre></div></article><section class="card card--previous reviewer-agents" id="reviewerAgents" hidden><strong>Specialistische agentreviews</strong><p class="estimate-meta">Alleen-lezende, onafhankelijke beoordelingen. De primaire agent behield uitvoerings- en lifecycleverantwoordelijkheid.</p><div class="reviewer-agents__list" id="reviewerAgentList"></div></section><div class="card card--previous" id="commits" hidden><strong>Voltooiingscommits</strong><div class="field"><span class="label">Vastgelegd bewijs</span><pre id="completionCommits"></pre></div></div><details class="card card--previous" id="report" hidden><summary><strong>Engineeringrapport</strong></summary><button class="copy" id="copyReport" type="button" title="Kopieer rapport" aria-label="Kopieer rapport">⧉ Kopieer</button><div id="reportContent" class="markdown-document">Open dit blok om het rapport te laden.</div></details><details class="card card--previous" id="reportAnalysis" hidden><summary><strong>AI-analyse van rapport</strong></summary><div id="reportAnalysisContent" class="markdown-document">Open dit blok om de analyse te laden.</div></details></div>
</div></section>
<details class="platform-health" id="platformHealth" data-testid="platform-health"><summary><strong>Platformonderdelen</strong></summary><p class="category-description">Live gezondheidscontrole van de lokale Engineering Platform-componenten.</p><div class="platform-health__components" id="platformHealthComponents" aria-live="polite"><p class="platform-health__empty">Componentstatus laden…</p></div></details>
<dialog class="component-modal" id="componentModal" aria-labelledby="componentModalTitle"><section class="component-modal__panel"><button class="component-modal__close" id="componentModalClose" type="button" aria-label="Meer informatie sluiten">×</button><h2 id="componentModalTitle">Componentinformatie</h2><div id="componentModalContent"></div><button class="component-modal__restart" id="componentModalRestart" type="button" hidden>Component herstarten</button><p class="component-modal__status" id="componentModalStatus" aria-live="polite"></p></section></dialog>
<dialog class="confirmation-modal" id="confirmationModal" aria-labelledby="confirmationModalTitle"><section class="confirmation-modal__panel"><h2 id="confirmationModalTitle">Bevestig actie</h2><p id="confirmationModalText"></p><div class="confirmation-modal__actions"><button id="confirmationModalCancel" type="button">Annuleren</button><button id="confirmationModalConfirm" type="button">Bevestigen</button></div></section></dialog>
<dialog class="report-view-modal" id="promptHistoryReportModal" aria-labelledby="promptHistoryReportModalTitle"><section class="report-view-modal__panel"><header class="report-view-modal__header"><h2 class="report-view-modal__title" id="promptHistoryReportModalTitle">Engineeringrapport</h2><div class="report-view-modal__actions"><button class="download download--glyph" id="promptHistoryReportDownload" type="button" title="Download engineeringrapport" aria-label="Download engineeringrapport" hidden>⇩</button><button class="copy copy--glyph" id="promptHistoryReportCopy" type="button" title="Kopieer engineeringrapport" aria-label="Kopieer engineeringrapport" hidden>⧉</button><button class="report-view-modal__close" id="promptHistoryReportClose" type="button" aria-label="Engineeringrapport sluiten">×</button></div></header><article class="markdown-document report-view-modal__content" id="promptHistoryReportContent">Rapport laden…</article></section></dialog>
<button id="loadComponentLogs" type="button" hidden>Logs laden</button>
<details class="technical-details" id="componentLogs"><summary><strong>Logs</strong></summary><p class="estimate-meta">Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush.</p><div class="log-controls" id="componentLogControls" hidden><label for="logFilter">Zoeken<input id="logFilter" type="search" maxlength="160" data-sanitize="single-line" placeholder="Zoek in alle velden"></label><label for="logLevelFilter">Niveau<select id="logLevelFilter"><option value="">Alle niveaus</option><option value="ERROR">Fout</option><option value="WARNING">Waarschuwing</option><option value="INFO">Informatie</option><option value="DEBUG">Debug</option></select></label><label for="logSort">Sortering<select id="logSort"><option value="newest">Nieuwste eerst</option><option value="oldest">Oudste eerst</option><option value="level">Niveau</option><option value="event">Gebeurtenis</option></select></label></div><div class="technical-grid"><div class="card"><div class="log-card-header"><strong>Inbox-watcher</strong><div class="log-card-actions"><button class="download download--glyph component-log-download" data-component="inbox" data-testid="download-inbox-log" type="button" title="Download Inbox-watcher-log" aria-label="Download Inbox-watcher-log">⇩</button><button class="clear-component-log" data-component="inbox" data-testid="clear-inbox-log" type="button">Logs wissen</button></div></div><div class="log-table-wrap"><table class="log-table"><thead><tr><th>#</th><th>Tijdstip</th><th>Niveau</th><th>Gebeurtenis</th><th>Run-ID</th><th>Details</th></tr></thead><tbody id="inboxComponentLog"><tr><td class="log-empty" colspan="6">Nog niet geladen.</td></tr></tbody></table></div><nav class="log-pagination" id="inboxLogPagination" aria-label="Paginering Inbox-watcher"></nav></div><div class="card"><div class="log-card-header"><strong>Statusdashboard</strong><div class="log-card-actions"><button class="download download--glyph component-log-download" data-component="dashboard" data-testid="download-dashboard-log" type="button" title="Download Statusdashboard-log" aria-label="Download Statusdashboard-log">⇩</button><button class="clear-component-log" data-component="dashboard" data-testid="clear-dashboard-log" type="button">Logs wissen</button></div></div><div class="log-table-wrap"><table class="log-table"><thead><tr><th>#</th><th>Tijdstip</th><th>Niveau</th><th>Gebeurtenis</th><th>Run-ID</th><th>Details</th></tr></thead><tbody id="dashboardComponentLog"><tr><td class="log-empty" colspan="6">Nog niet geladen.</td></tr></tbody></table></div><nav class="log-pagination" id="dashboardLogPagination" aria-label="Paginering Statusdashboard"></nav></div></div></details>
<details class="card codex-chat" id="codexChat"><summary><strong>AI-gesprek</strong></summary><p class="category-description">Stel korte, alleen-lezen vragen over de laatst uitgevoerde prompt en het bijbehorende rapport. Dit start geen engineering of wijzigingen.</p><div class="codex-chat__details"><div class="chat-actions"><button class="download download--glyph" id="downloadChat" type="button" title="Download gesprek" aria-label="Download gesprek" hidden>⇩</button><button class="clear-chat" id="clearChat" type="button" title="Chat wissen" aria-label="Chat wissen" hidden>⌫</button></div><div class="chat-messages" id="chatMessages" aria-live="polite" aria-label="Gesprek met AI-assistent"></div><label class="label" for="chatInput">Nieuwe vraag aan AI-assistent</label><div class="chat-compose"><textarea id="chatInput" class="chat-input" rows="5" maxlength="2000" autocomplete="off" data-sanitize="multiline" placeholder="Bijvoorbeeld: wat zijn de belangrijkste vervolgstappen uit het laatste rapport?"></textarea><button class="chat-send" id="chatSend" type="button" title="Verstuur vraag" aria-label="Verstuur vraag"><span aria-hidden="true">➤</span></button></div><p class="field"><span class="label">Gebruikt model</span><span id="chatModel">$CHAT_MODEL</span></p><p class="chat-status" id="chatStatus"></p></div></details>
<details class="technical-details" id="technicalDetails"><summary><strong>Technische details</strong></summary><div class="technical-grid">
<div class="card"><strong>Pull requests</strong><p class="field"><span class="label">Implementatie</span><span id="implementation"></span></p><p class="field"><span class="label">Finalisatie</span><span id="finalization"></span></p></div>
<div class="card"><strong>Repository</strong><p class="field"><span class="label">Repositorystatus</span><span id="repositoryState"></span></p><p class="field"><span class="label">Werkruimtestatus</span><span id="workspaceState"></span></p></div>
<div class="card"><strong>Host Preflight</strong><p class="field"><span class="label">Execution Host</span><span id="executionHostName">Niet beschikbaar</span></p><p class="field"><span class="label">Execution Host Version</span><span id="executionHostVersion">Niet beschikbaar</span></p><p class="field"><span class="label">Runtime</span><span id="executionHostRuntime">Niet beschikbaar</span></p><p class="field"><span class="label">Runtime Prompt Transport</span><span id="executionHostTransport">Niet beschikbaar</span></p><p class="field"><span class="label">Hoststatus</span><span id="hostPreflightStatus">Niet beschikbaar</span></p><p class="field"><span class="label">Laatste controle</span><span id="hostPreflightTimestamp">Niet beschikbaar</span></p><p class="field"><span class="label">Workspacestatus</span><span id="workspacePreflightStatus">Niet beschikbaar</span></p><p class="field"><span class="label">Laatste workspacecontrole</span><span id="workspacePreflightTimestamp">Niet beschikbaar</span></p></div>
<div class="card"><strong>Diagnose</strong><p id="diag"></p></div>
</div></details>
</main></div>
<footer class="footer" aria-live="polite"><span class="label">Engineering Platform-versie</span><span id="platformVersion">Laden…</span><span aria-hidden="true">·</span><span id="lastRefresh">Laatst bijgewerkt: laden…</span><span aria-hidden="true">·</span><span id="updateMode">Serverpush: verbinden…</span></footer><span id="dashboardVersion" hidden></span><span id="workerVersion" hidden></span>
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
                    log_event(logger, logging.INFO, "ai_usage_reset_completed")
                    payload = {
                        "outcome": outcome,
                        "rate_limits": json.loads(_codex_rate_limits()),
                    }
                except RateLimitResetError as error:
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
                if not isinstance(payload, dict) or set(payload) != {"message", "history"}:
                    raise ValueError
                status = json.loads(_status(root))
                answer = codex_chat_response(root, status, payload["message"], payload["history"])
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
                "/assets/dashboard_status_store.mjs": ("dashboard_status_store.mjs", "text/javascript; charset=utf-8"),
                "/assets/engineering-status-icon.svg": (APP_ICON_SVG, "image/svg+xml; charset=utf-8"),
                "/assets/engineering-status-icon-180.png": (APP_ICON_TOUCH, "image/png"),
                # Serve the conventional browser and iOS discovery paths too.
                # They intentionally reuse the canonical app icon rather than
                # introducing duplicate, independently-versioned icon files.
                "/favicon.ico": (APP_ICON_TOUCH, "image/png"),
                "/apple-touch-icon.png": (APP_ICON_TOUCH, "image/png"),
                "/apple-touch-icon-precomposed.png": (APP_ICON_TOUCH, "image/png"),
            }
            if asset := icon_assets.get(request.path):
                asset_name, content_type = asset
                try:
                    content = (ASSET_DIRECTORY / asset_name).read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                return self._send(content, content_type)
            if request.path == "/api/report/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                if parse_qs(request.query).get("audit") == ["download"]:
                    log_event(logger, logging.INFO, "engineering_report_downloaded", run_id=run_id)
                return self._send(
                    _report_for_run(root, run_id), "text/markdown; charset=utf-8"
                )
            if request.path == "/api/report-analysis/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                if parse_qs(request.query).get("audit") == ["download"]:
                    log_event(logger, logging.INFO, "report_analysis_downloaded", run_id=run_id)
                return self._send(
                    _report_analysis_for_run(root, run_id), "text/markdown; charset=utf-8"
                )
            if request.path == "/api/prompt-history":
                return self._send(_prompt_history(root), "application/json; charset=utf-8")
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
            if request.path == "/api/usage/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                return self._send(
                    _codex_usage_for_run(root, run_id), "application/json; charset=utf-8"
                )
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
                return self._send(_codex_process_metrics(), "application/json; charset=utf-8")
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
            if self.path == "/api/log/last":
                return self._send(_last_executed_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/usage":
                return self._send(_codex_usage(root), "application/json; charset=utf-8")
            if self.path == "/api/commits":
                return self._send(_completion_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/commits/last-executed":
                return self._send(_last_executed_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/prompt-started":
                return self._send(_prompt_started(root), "application/json; charset=utf-8")
            if self.path == "/":
                engineering_database = _engineering_database_details(root)
                return self._send(
                    _dashboard_html(
                        title,
                        _build_commit(root),
                        workspace_id,
                        workspace_location,
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
    subprocess.run((compiler, str(repo / "tools/engineering/dashboard_supervisor.swift"), "-o", str(binary)), check=True)
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
