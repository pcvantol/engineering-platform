"""Private Engineering Status dashboard with a bounded predecessor-retry action."""

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
from .inbox_watcher import RetrySubmissionError, cloud_root, submit_predecessor_retry
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
DASHBOARD_VERSION = "1.2.87"
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
    remote = TailscaleProvider().status()
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
        "private_remote_access": {
            "healthy": remote.qualified,
            "state": "connected" if remote.qualified else "disconnected",
            "detail": remote.detail,
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
        result["executable_path"] = (
            shutil.which("tailscale") if component == "private_remote_access" else None
        )
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
<style>
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
/* Shared product chrome: change the hue here to retheme controls, notices and status metadata. */
:root{--house-style:#f0b66a;--house-style-contrast:#fff0dc;--house-style-surface:#4a321f;--house-style-focus:#f0b66a47}
html[data-theme="light"]{--house-style-contrast:#653a13;--house-style-surface:#fff4e6}
.log-controls label:has(#logSort){display:none}.log-table th.log-sortable{cursor:pointer;user-select:none}.log-table th.log-sortable::after{color:#8dc7ff;content:attr(data-sort-indicator);font-size:11px;margin-left:6px}.log-table th.log-sortable:focus-visible{outline:2px solid #8dc7ff;outline-offset:-2px}
.current-run .card:has(#indicator){background:#2a2530;border-left:3px solid #c7a6ff}.current-run .card:has(#predecessorRun),#currentDiagnostic,.technical-details--diagnostic{background:#302a24;border-left:3px solid #f0b66a}.current-run .card:has(#queueList),.current-run .card:has(#runId),.current-run .card:has(#executionMode){background:#28263a;border-left:3px solid #a78bfa}.current-run .card:has(#currentTime),.current-run .card:has(#executionEstimate){background:#202b34;border-left:3px solid #65c5d9}#processMetrics,#usage,#rateLimits{background:#20332f;border-left:3px solid #54d6a0}#commits{background:#202a36;border-left:3px solid #8dc7ff}#codexChat{background:#292336;border-left:3px solid #d0a4ff}
body{margin:0;background:#121217;color:#f7f3ee;font:14px system-ui;padding:max(18px,env(safe-area-inset-top)) calc(28px + env(safe-area-inset-right)) max(18px,env(safe-area-inset-bottom)) calc(28px + env(safe-area-inset-left))}.dashboard-grid{display:grid;gap:10px}h1{font-size:28px;line-height:1.1;margin:0 0 18px}.card,.technical-details{background:#24242d;border-left:3px solid #c7a6ff;border-radius:14px;padding:14px;box-shadow:0 4px 18px #0005}.card p{margin:7px 0}.card--operation{background:#2a2530;border-left:3px solid #c7a6ff}.card--monitoring{background:#202b34;border-left:3px solid #65c5d9}.card--context{background:#28263a;border-left:3px solid #a78bfa}.card--resource{background:#20332f;border-left:3px solid #54d6a0}.card--evidence{background:#202a36;border-left:3px solid #8dc7ff}.card--diagnostic,.technical-details--diagnostic{background:#302a24;border-left:3px solid #f0b66a}.card--conversation{background:#292336;border-left:3px solid #d0a4ff}.current-run{background:#1d1d25;border:1px solid #3d3651;border-left:3px solid #c7a6ff;border-radius:18px;padding:14px;box-shadow:0 5px 24px #0006}.current-run__title{border-bottom:1px solid #3d3651;padding:2px 2px 13px}.current-run__title h2{font-size:18px;line-height:1.25;margin:3px 0 0}.current-run__grid{display:grid;gap:10px;margin-top:10px}.current-run .card{box-shadow:none}.prompt-runs{display:grid;gap:7px}.prompt-runs__heading{color:#b9b6c0;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.prompt-runs__cards,.last-execution{display:grid;gap:10px}.card--previous{background:#202a36;border:1px solid #37506a;border-left:3px solid #8dc7ff;box-shadow:0 4px 18px #0005}.card--previous strong,.card--previous .label{color:#8dc7ff}.field{margin:8px 0 0}.label{display:block;color:#c7a6ff;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-bottom:2px}.estimate-primary{font-size:18px;font-weight:650;margin:8px 0 0}.estimate-meta{color:#b9b6c0;font-size:12px;line-height:1.35;margin:6px 0 0}.final-status{align-items:center;display:flex;gap:7px;margin:0 0 8px}.final-status .label{margin-bottom:0}.indicator--small{height:9px;width:9px}.footer{color:#b9b6c0;font-size:12px;margin:14px 0 4px;text-align:center}.copy,.chat-send{background:#353541;color:#f7f3ee;border:1px solid #57576a;border-radius:8px;padding:6px 9px;font:13px system-ui}.copy{float:right}.technical-details{cursor:pointer}.technical-details summary{list-style:none}.technical-details summary::-webkit-details-marker{display:none}.technical-details summary::before{content:"▸ ";color:#c7a6ff;display:inline-block;font-size:20px;line-height:1;vertical-align:-2px}.technical-details[open] summary::before{content:"▾ "}.technical-grid{display:grid;gap:10px;margin-top:12px}.codex-chat{grid-column:1 / -1}.chat-messages{display:grid;gap:9px;max-height:420px;overflow:auto;margin:12px 0;padding:2px}.chat-message{border-radius:9px;font-family:"Unispace",ui-monospace,monospace;max-width:min(880px,92%);padding:9px 10px;white-space:pre-wrap}.chat-message--user{background:#353541;justify-self:end}.chat-message--assistant{background:#202a36;border:1px solid #37506a;justify-self:start}.chat-message__role{display:block;font-size:11px;font-weight:700;letter-spacing:.04em;margin-bottom:4px;text-transform:uppercase}.chat-message--user .chat-message__role{color:#c7a6ff}.chat-message--assistant .chat-message__role{color:#8dc7ff}.chat-message__body{line-height:1.4}.chat-input{box-sizing:border-box;width:100%;min-height:110px;border:1px solid #57576a;border-radius:8px;background:#18181f;color:#f7f3ee;padding:8px;font:13px "Unispace",ui-monospace,monospace}.chat-status{color:#b9b6c0;font-size:12px}
strong{color:#c7a6ff}.status{display:flex;align-items:center;gap:8px}.queue-list{display:grid;gap:7px;list-style:none;margin:10px 0 0;padding:0;padding-inline-start:0}.queue-item{align-items:start;border-top:1px solid #3d3651;display:grid;gap:6px;grid-template-columns:1.25rem minmax(0,1fr);padding-left:0;padding-top:7px}.queue-item:first-child{border-top:0;padding-top:0}.queue-item__number{color:var(--category-color);font:700 12px ui-monospace,monospace;padding-top:1px;text-align:left}.queue-item__title{display:block;font-weight:600}.queue-item__meta{color:#b9b6c0;font:12px ui-monospace,monospace;margin-top:2px}.queue-empty{color:#b9b6c0;font-size:12px}.indicator{width:12px;height:12px;border-radius:50%;background:#9a9aa3;box-shadow:0 0 8px #9a9aa388;flex:none}.indicator--green{background:#51d88a;box-shadow:0 0 8px #51d88a88}.indicator--yellow{background:#f4d35e;box-shadow:0 0 8px #f4d35e88}.indicator--orange{background:#ff9f43;box-shadow:0 0 8px #ff9f4388}.indicator--red{background:#ff6b6b;box-shadow:0 0 8px #ff6b6b88}.indicator--running{background:transparent;border:3px solid #ff6b6b;border-right-color:transparent;box-sizing:border-box;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
pre{white-space:pre-wrap;word-break:break-word;margin:5px 0 0;font:12px ui-monospace,monospace}.log-controls{align-items:end;display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.log-controls label{color:#c7a6ff;display:grid;font-size:11px;font-weight:700;gap:3px;letter-spacing:.04em;text-transform:uppercase}.log-controls input,.log-controls select{background:#18181f;border:1px solid #57576a;border-radius:7px;color:#f7f3ee;font:13px system-ui;padding:6px}.log-controls input{min-width:190px}.log-table-wrap{max-height:420px;overflow:auto;border:1px solid #3d3651;border-radius:9px;margin-top:10px}.log-table{border-collapse:collapse;font:12px ui-monospace,monospace;min-width:780px;width:100%;user-select:text}.log-table th{background:#2d2d38;color:#c7a6ff;position:sticky;text-align:left;top:0;z-index:1}.log-table th,.log-table td{border-bottom:1px solid #3d3651;padding:7px 8px;vertical-align:top}.log-table td{white-space:pre-wrap;word-break:break-word}.log-table tr:last-child td{border-bottom:0}.log-line-number{color:#92919b;text-align:right;white-space:nowrap;width:3.25rem;word-break:normal;user-select:none}.log-level{font-weight:700}.log-level--error{color:#ff8585}.log-level--warning{color:#f4d35e}.log-level--info{color:#8dc7ff}.log-level--debug{color:#b9b6c0}.log-empty{color:#b9b6c0;padding:10px}[hidden]{display:none}
#componentLogs{background:#302a24;border-left:3px solid #f0b66a}#componentLogs .card{background:#24242d;border-left:0}.technical-details:not(#componentLogs){background:#28263a;border-left:3px solid #a78bfa}.technical-details:not(#componentLogs) .card{background:#24242d;border-left:0}.last-execution-card{cursor:default}.last-execution-card summary{cursor:pointer;list-style:none}.last-execution-card summary::-webkit-details-marker{display:none}.last-execution-card summary::before{content:"▸ ";color:#8dc7ff}.last-execution-card[open] summary::before{content:"▾ "}.last-execution-card__details{border-top:1px solid #37506a;margin-top:10px;padding-top:2px}.codex-chat{cursor:default}.codex-chat summary{cursor:pointer;list-style:none}.codex-chat summary::-webkit-details-marker{display:none}.codex-chat summary::before{content:"▸ ";color:#d0a4ff}.codex-chat[open] summary::before{content:"▾ "}.codex-chat__details{border-top:1px solid #56446e;margin-top:10px;padding-top:2px}.card,.technical-details{--category-color:#c7a6ff}.card--monitoring,.current-run .card:has(#currentTime),.current-run .card:has(#executionEstimate){--category-color:#65c5d9}.card--context,.technical-details:not(#componentLogs),.current-run .card:has(#queueList),.current-run .card:has(#runId),.current-run .card:has(#executionMode){--category-color:#a78bfa}.card--resource,#processMetrics,#usage,#rateLimits{--category-color:#54d6a0}.card--evidence,.card--previous,#commits{--category-color:#8dc7ff}.card--diagnostic,.technical-details--diagnostic,#currentDiagnostic,#componentLogs,.current-run .card:has(#predecessorRun){--category-color:#f0b66a}.card--conversation,#codexChat{--category-color:#d0a4ff}.technical-details .card{--category-color:inherit}.card>strong,.technical-details>summary>strong,.card .label,.technical-details .label,.technical-details .log-controls label,.technical-details .log-table th,.technical-details summary::before{color:var(--category-color)}.technical-details .log-table th.log-sortable::after{color:var(--category-color)}.technical-details .log-table th.log-sortable:focus-visible{outline-color:var(--category-color)}.last-execution-group{--category-color:#8dc7ff;background:#202a36;border-left:3px solid #8dc7ff;border-radius:16px;box-shadow:0 4px 18px #0005;padding:14px}.last-execution-group:has(#lastExecution[hidden]){display:none}.last-execution-group__heading{color:var(--category-color);font-size:15px;margin:0 0 10px}.last-execution-group .card--previous{background:#24242d;border-left:0}.last-execution-group .card--previous strong,.last-execution-group .card--previous .label{color:var(--category-color)}
.chat-compose{align-items:stretch;display:flex;gap:8px}.chat-compose .chat-input{flex:1}.chat-compose .chat-send{align-items:center;display:flex;font-size:23px;justify-content:center;min-width:48px;padding:6px}.chat-compose .chat-send:focus-visible{outline:2px solid var(--category-color);outline-offset:2px}.chat-status{display:block;margin:7px 0 0}.card--previous summary{cursor:pointer;list-style:none}.card--previous summary::-webkit-details-marker{display:none}.card--previous summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:20px;line-height:1;vertical-align:-2px}.card--previous[open] summary::before{content:"▾ "}.last-execution-group>summary{cursor:pointer;list-style:none}.last-execution-group>summary::-webkit-details-marker{display:none}.last-execution-group>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:20px;line-height:1;vertical-align:-2px}.last-execution-group[open]>summary::before{content:"▾ "}.last-execution-group__heading{display:inline;font-size:18px;margin:0}.skip-link{background:#fff;color:#121217;left:12px;padding:10px 14px;position:fixed;top:-60px;z-index:10}.skip-link:focus{top:12px}.sr-only{height:1px;margin:-1px;overflow:hidden;padding:0;position:absolute;clip:rect(0,0,0,0);white-space:nowrap;width:1px}:where(button,input,select,textarea,summary,[role="button"],[tabindex]):focus-visible{outline:3px solid #fff;outline-offset:3px;box-shadow:0 0 0 6px #121217}button,.copy,.chat-send{min-height:44px;min-width:44px}@media (prefers-reduced-motion:reduce){*,::before,::after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}@media (forced-colors:active){:where(button,input,select,textarea,summary,[role="button"],[tabindex]):focus-visible{outline-color:Highlight;box-shadow:none}.indicator{forced-color-adjust:auto}}
.workspace-card>summary:focus-visible,#rateLimits>summary:focus-visible,.last-execution-group>summary:focus-visible,.telemetry>summary:focus-visible,.platform-health>summary:focus-visible,.technical-details>summary:focus-visible,#codexChat>summary:focus-visible,.current-run>summary:focus-visible{outline:2px solid var(--category-color);outline-offset:3px;box-shadow:0 0 0 4px color-mix(in srgb,var(--category-color) 24%,transparent)}
#componentLogs{position:relative}#componentLogs>summary{padding-right:150px}#loadComponentLogs{position:absolute;right:14px;top:14px}#componentLogs .log-card-header strong{color:var(--category-color)}#componentLogs .log-table-wrap{border-color:color-mix(in srgb,var(--category-color) 55%,#3d3651)}#componentLogs .log-table th,#componentLogs .log-table td{border-bottom-color:color-mix(in srgb,var(--category-color) 30%,#3d3651)}.technical-details>summary>strong,.codex-chat>summary>strong,.last-execution-group__heading{font-size:18px;line-height:1.25}.card>strong,.card>summary,.technical-details>summary,.last-execution-group>summary{display:block;padding-bottom:10px}.last-execution-group .card--previous{border-left:3px solid var(--category-color);box-shadow:0 10px 24px #000a,0 1px 0 #8dc7ff30}.dashboard-grid>#componentLogs,.dashboard-grid>#codexChat,.dashboard-grid>.technical-details:not(#componentLogs){margin-top:8px}.footer{align-items:baseline;display:flex;gap:6px;justify-content:center;overflow-x:auto;white-space:nowrap}.footer .label{display:inline;margin:0}.last-execution-group__heading,.technical-details>summary>strong,.codex-chat>summary>strong{font-size:18px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.last-execution-group>summary::before,.dashboard-grid>#componentLogs>summary::before,.dashboard-grid>#codexChat>summary::before,.dashboard-grid>.technical-details:not(#componentLogs)>summary::before{font-size:24px;padding-right:8px}#engineering-dashboard-content>.technical-details:not(#componentLogs){--category-color:#65c5d9;background:#202b34;border-left-color:#65c5d9}.footer .label{color:#65c5d9}#rateLimits{cursor:pointer}#rateLimits>summary{cursor:pointer;list-style:none}#rateLimits>summary::-webkit-details-marker{display:none}#rateLimits>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:24px;line-height:1;padding-right:8px;vertical-align:-2px}#rateLimits[open]>summary::before{content:"▾ "}#rateLimits>summary>strong{font-size:18px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.last-execution-group{row-gap:16px}#engineering-dashboard-content>.technical-details:not(#componentLogs) .card,#componentLogs .card,.current-run .card,.last-execution-group .card--previous{border:1px solid var(--category-color);border-left:3px solid var(--category-color)}.workspace-card{margin-bottom:16px;cursor:pointer}.workspace-card>summary{cursor:pointer;list-style:none}.workspace-card>summary::-webkit-details-marker{display:none}.workspace-card>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:24px;line-height:1;padding-right:8px;vertical-align:-2px}.workspace-card[open]>summary::before{content:"▾ "}.workspace-card>summary>strong{font-size:18px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.dashboard-grid{gap:16px}.dashboard-grid>#componentLogs,.dashboard-grid>#codexChat,.dashboard-grid>.technical-details:not(#componentLogs){margin-top:0}
.workspace-card,#rateLimits,.last-execution-group,#componentLogs,#codexChat,#engineering-dashboard-content>.technical-details:not(#componentLogs),.telemetry,.platform-health,.current-run{box-shadow:0 10px 28px rgb(15 23 42 / .18),0 3px 9px rgb(15 23 42 / .12)}
.last-execution-group__content{display:grid;gap:16px;margin-top:12px}
.reviewer-agents__list{display:grid;gap:8px;margin-top:10px}.reviewer-agent{background:#202b34;border:1px solid var(--category-color);border-left-width:1px;border-radius:8px;padding:10px}.reviewer-agent__name{color:var(--category-color);font-weight:700;margin:0 0 4px}.reviewer-agent__meta{color:#d3d0d8;font-size:13px;line-height:1.4;margin:0}.reviewer-agent__meta+.reviewer-agent__meta{margin-top:3px}
.telemetry{--category-color:#fb7185;background:#351f2a;border:1px solid var(--category-color);border-left-width:3px;border-radius:18px;box-shadow:0 5px 24px #0006;cursor:pointer;padding:14px}.telemetry>summary{border-bottom:1px solid var(--category-color);cursor:pointer;display:block;list-style:none;margin-bottom:12px;padding:2px 2px 13px}.telemetry>summary::-webkit-details-marker{display:none}.telemetry>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:24px;line-height:1;padding-right:8px;vertical-align:-2px}.telemetry[open]>summary::before{content:"▾ "}.telemetry>summary>strong{color:var(--category-color);font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.telemetry .category-description{color:#d8c3ca;font-size:14px;line-height:1.4;margin:12px 0}.telemetry-table{border-collapse:collapse;font-size:13px;width:100%}.telemetry-table th,.telemetry-table td{border-bottom:1px solid #fb718555;padding:7px;text-align:left;white-space:nowrap}.telemetry-table th{color:var(--category-color)}.telemetry-scroll{overflow-x:auto}.telemetry-empty{color:#b9b6c0;margin:0}
.platform-health{--category-color:#a3e635;background:#29331d;border:1px solid var(--category-color);border-left-width:3px;border-radius:18px;box-shadow:0 5px 24px #0006;cursor:pointer;padding:14px}.platform-health>summary{border-bottom:1px solid var(--category-color);cursor:pointer;display:block;list-style:none;margin-bottom:12px;padding:2px 2px 13px}.platform-health>summary::-webkit-details-marker{display:none}.platform-health>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:24px;line-height:1;padding-right:8px;vertical-align:-2px}.platform-health[open]>summary::before{content:"▾ "}.platform-health>summary>strong{color:var(--category-color);font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.platform-health .category-description{color:#d4ddc7;font-size:14px;line-height:1.4;margin:12px 0}.platform-health__summary{color:#d4ddc7;font-size:12px;margin-left:8px}.platform-health__components{display:grid;gap:8px}.platform-health__component{align-items:center;background:#1e2518;border:1px solid color-mix(in srgb,var(--category-color) 64%,transparent);border-radius:9px;column-gap:10px;display:grid;grid-template-columns:auto 1fr;padding:9px 10px;row-gap:3px}.platform-health__component .indicator{grid-row:span 2}.platform-health__component-name{color:var(--category-color);font-weight:700}.platform-health__component-detail{color:#d4ddc7;font-size:12px;grid-column:2;line-height:1.35}.platform-health__empty{color:#d4ddc7;margin:0}.platform-health__component[data-health="false"]{--category-color:#ff8585;background:#351f24}
#codexChat .chat-input,#codexChat .chat-send{border-color:#d0a4ff}
.component-modal__panel{color:#f7f3ee}.component-modal__close:hover{background:#a3e63526}.component-modal__close:focus-visible{box-shadow:0 0 0 4px #a3e63540;outline:2px solid #a3e635;outline-offset:3px}
#codexChat .codex-chat__details{position:relative}#codexChat .codex-chat__details>.estimate-meta{padding-right:52px}#downloadChat.download--glyph{align-items:center;background:#34283f;border:1px solid #d0a4ff;border-radius:50%;color:#eadcff;display:flex;float:none;font-size:0;height:34px;justify-content:center;min-height:34px;min-width:34px;padding:0;position:absolute;right:14px;top:0;width:34px;z-index:2}#downloadChat.download--glyph::before{content:"↓";font:700 21px/1 system-ui}#downloadChat.download--glyph::after{content:none}#downloadChat.download--glyph:hover{background:#4b3658}#downloadChat.download--glyph:focus-visible{box-shadow:0 0 0 4px #d0a4ff40;outline:2px solid #d0a4ff;outline-offset:2px}
.component-info{align-items:center;background:transparent;border:1px solid var(--category-color);border-radius:50%;color:var(--category-color);cursor:pointer;display:grid;font:700 15px/1 system-ui;height:32px;justify-items:center;padding:0;width:32px}.component-info:hover{background:color-mix(in srgb,var(--category-color) 16%,transparent)}.platform-health__component{grid-template-columns:auto 1fr auto}.platform-health__component-detail{grid-column:2}.component-modal{background:#15151de8;border:0;height:100dvh;max-height:none;max-width:none;padding:18px;width:100vw}.component-modal[open]{display:grid;place-items:center}.component-modal__panel{background:#22242c;border:2px solid #a3e635;border-radius:16px;box-shadow:0 16px 52px #000c;box-sizing:border-box;max-height:min(700px,calc(100dvh - 36px));max-width:680px;overflow:auto;padding:20px;position:relative;width:min(680px,calc(100vw - 36px))}.component-modal__close{background:transparent;border:1px solid #a3e635;border-radius:50%;color:#d8f7a5;cursor:pointer;font:20px/1 system-ui;height:34px;position:absolute;right:14px;top:14px;width:34px}.component-modal h2{color:#a3e635;margin:0 44px 14px 0}.component-modal dl{display:grid;gap:10px;margin:0}.component-modal dt{color:#a3e635;font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase}.component-modal dd{margin:3px 0 0;overflow-wrap:anywhere}.component-modal__restart{background:#253b17;border:1px solid #a3e635;border-radius:8px;color:#e6ffc0;cursor:pointer;font:14px system-ui;margin-top:18px;padding:9px 12px}.component-modal__restart:disabled{cursor:wait;opacity:.7}.component-modal__status{color:#d4ddc7;font-size:13px;margin:10px 0 0}
#chatInput:focus-visible{outline:2px solid #d0a4ff;outline-offset:2px;box-shadow:0 0 0 4px #292336}
:where(input,select,textarea):focus-visible{outline:2px solid var(--category-color);outline-offset:2px;box-shadow:0 0 0 4px color-mix(in srgb,var(--category-color) 24%,transparent)}
#reportContent{max-height:50dvh;overflow:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
.last-execution-group__heading,.technical-details>summary>strong,.codex-chat>summary>strong,#rateLimits>summary>strong,.workspace-card>summary>strong{font-size:17px}
#rateLimits>summary>strong{color:var(--category-color)}
#loadComponentLogs{background:#4a321f;border-color:#f0b66a;color:#fff0dc}
#engineering-dashboard-content>.technical-details:not(#componentLogs),#engineering-dashboard-content>.technical-details:not(#componentLogs) .card{border-left-width:1px}
.chat-message--user{background:#243648;border:1px solid #8dc7ff}.chat-message--assistant{background:#34283f;border-color:#d0a4ff}
#reportContent,#reportAnalysisContent{box-sizing:border-box;max-height:50dvh;overflow:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;background:#18181f;border:1px solid var(--category-color);border-radius:8px;padding:10px;user-select:text}.markdown-document{font-family:system-ui,sans-serif;line-height:1.45}.markdown-document p{margin:0 0 10px}.markdown-document p:last-child{margin-bottom:0}.markdown-document h3,.markdown-document h4{color:var(--category-color);margin:0 0 10px}.markdown-document ul,.markdown-document ol{margin:0 0 10px;padding-left:24px}.markdown-document code,.markdown-document pre{font-family:"Unispace",ui-monospace,monospace}.markdown-document code{background:#24242d;border-radius:4px;padding:1px 4px}.markdown-document pre{background:#24242d;border-radius:6px;margin:0 0 10px;overflow:auto;padding:8px}
.markdown-copy-wrap{position:relative}.markdown-copy-wrap .markdown-document,.markdown-copy-wrap>#reportContent,.markdown-copy-wrap>#reportAnalysisContent{padding-right:108px}.copy.copy--glyph,.download.download--glyph{align-items:center;background:#24242de6;border-color:var(--category-color);border-radius:50%;box-shadow:0 2px 8px #0008;display:flex;float:none;font-size:0;height:32px;justify-content:center;min-height:32px;min-width:32px;padding:0;position:absolute;top:10px;width:32px;z-index:2}.copy.copy--glyph{right:14px}.download.download--glyph{right:54px}.copy.copy--glyph::before{content:"⧉";font:20px/1 system-ui}.download.download--glyph::before{content:"⇩";font:20px/1 system-ui}.copy.copy--glyph:hover,.download.download--glyph:hover{background:#353541}.copy.copy--glyph:focus-visible,.download.download--glyph:focus-visible{outline-color:var(--category-color)}
.last-execution-group{row-gap:0}
.chat-compose .chat-send{background:#121217;border-color:#d0a4ff;color:#d0a4ff}
#currentRun .current-run__title>.label{display:inline;color:var(--category-color);font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.current-run__prompt-heading{align-items:center;display:flex;gap:8px;margin:8px 0 0}.current-run__prompt-heading h2{margin:0}.current-run__prompt-heading .indicator{margin:0}.current-run__category-description{color:#d6bdca;font-size:14px;line-height:1.4;margin:0 0 14px}#currentRun .current-run__grid{margin-top:0}#currentRun .card:has(#currentTime),#currentRun .card:has(#executionEstimate),#currentRun .card:has(#runId),#currentRun .card:has(#executionMode){--category-color:#f472b6;background:#241b25;border-color:#f472b6}
#report .field,#reportAnalysis .field{background:#18181f;border:1px solid var(--category-color);border-radius:8px;padding:10px}
.chat-message__body{font-family:system-ui,sans-serif}.chat-message__body p{margin:0 0 8px}.chat-message__body p:last-child{margin-bottom:0}.chat-message__body h3,.chat-message__body h4{color:#d0a4ff;margin:0 0 8px}.chat-message__body ul,.chat-message__body ol{margin:0 0 8px;padding-left:22px}.chat-message__body code,.chat-message__body pre{font-family:"Unispace",ui-monospace,monospace}.chat-message__body code{background:#18181f;border-radius:4px;padding:1px 4px}.chat-message__body pre{background:#18181f;border-radius:6px;margin:0 0 8px;overflow:auto;padding:8px}
#componentLogs>summary{padding-right:0}
.last-execution-group>summary>strong{color:#8dc7ff;text-transform:uppercase}
.chat-message--user .chat-message__role{color:#8dc7ff}
.chat-message--assistant .chat-message__role{color:#d0a4ff}
.chat-compose{display:block;position:relative}.chat-compose .chat-input{padding:8px 62px 58px 8px}.chat-compose .chat-send{background:#34283f;border-color:#d0a4ff;border-radius:8px;bottom:10px;color:#eadcff;height:44px;min-width:44px;padding:0;position:absolute;right:10px;width:44px;z-index:1}.chat-compose .chat-send:hover{background:#463653}.chat-compose .chat-send:disabled{cursor:wait;opacity:.7}
#codexChat label[for="chatInput"]{margin-bottom:10px}.chat-input{font:13px system-ui,sans-serif}
.chat-message{padding-right:46px;position:relative}.chat-message__copy{align-items:center;background:transparent;border:1px solid currentColor;border-radius:50%;color:inherit;cursor:pointer;display:flex;font:16px/1 system-ui;height:25px;justify-content:center;min-height:25px;min-width:25px;opacity:.72;padding:0;position:absolute;right:8px;top:8px;width:25px}.chat-message__copy:hover{background:#ffffff1a;opacity:1}.chat-message__copy:focus-visible{box-shadow:0 0 0 3px #d0a4ff55;outline:1px solid currentColor;outline-offset:2px}.chat-message--user .chat-message__copy{color:#8dc7ff}.chat-message--assistant .chat-message__copy{color:#d0a4ff}
.workspace-card{--category-color:#f3d36a;background:#302d20;border-left-color:#f3d36a}
#engineering-dashboard-content>.technical-details:not(#componentLogs){border-left-width:3px}
#componentLogs .card{border-left-width:1px}
.workspace-card>summary,#rateLimits>summary,.last-execution-group>summary,#componentLogs>summary,#codexChat>summary,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary{border-bottom:1px solid var(--category-color);margin-bottom:12px;padding-bottom:14px}
.category-description{color:#b9b6c0;font-size:14px;line-height:1.4;margin:0 0 14px}
.workspace-card,#rateLimits,.last-execution-group,#componentLogs,#codexChat,#engineering-dashboard-content>.technical-details:not(#componentLogs){border:1px solid var(--category-color);border-left-width:3px}
.rate-limit-reset{background:#173c31;border:1px solid #54d6a0;border-radius:8px;color:#d9fff0;font:13px system-ui;margin-top:12px;padding:8px 10px}.rate-limit-reset:disabled{cursor:wait;opacity:.7}.rate-limit-reset-status{color:#b9b6c0;font-size:12px;margin:8px 0 0}
.predecessor-retry{background:#3b281b;border:1px solid #f0b66a;border-radius:8px;color:#fff0dc;font:13px system-ui;margin-top:12px;padding:8px 10px}.predecessor-retry:hover:not(:disabled){background:#543721}.predecessor-retry:disabled{cursor:wait;opacity:.7}.predecessor-retry-status{color:#d6c5a8;font-size:12px;margin:8px 0 0}
.estimate-primary{font-size:inherit}#executionEstimateMeta{white-space:pre-line}
.workspace-card>summary>strong,#rateLimits>summary>strong,.last-execution-group>summary>strong,#componentLogs>summary>strong,#codexChat>summary>strong,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary>strong{font-size:17px;line-height:1.25}.workspace-card>summary,#rateLimits>summary,.last-execution-group>summary,#componentLogs>summary,#codexChat>summary,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary{margin-bottom:8px;padding-bottom:10px}
.workspace-card>summary>strong,#rateLimits>summary>strong,.last-execution-group>summary>strong,#componentLogs>summary>strong,#codexChat>summary>strong,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary>strong{color:var(--category-color)}
.workspace-card>summary,#rateLimits>summary,.last-execution-group>summary,#componentLogs>summary,#codexChat>summary,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary,.telemetry>summary,.platform-health>summary{box-sizing:border-box;margin-bottom:8px;min-height:35px;padding:0 0 10px}.workspace-card>summary>strong,#rateLimits>summary>strong,.last-execution-group>summary>strong,#componentLogs>summary>strong,#codexChat>summary>strong,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary>strong,.telemetry>summary>strong,.platform-health>summary>strong{font-size:17px;line-height:1.25}
.workspace-card>summary::before,#rateLimits>summary::before,.last-execution-group>summary::before,#componentLogs>summary::before,#codexChat>summary::before,#engineering-dashboard-content>.technical-details:not(#componentLogs)>summary::before,.telemetry>summary::before,.platform-health>summary::before{line-height:1;vertical-align:-2px}
#currentRun{--category-color:#f472b6;background:#321d2d;border:1px solid var(--category-color);border-left-width:3px;cursor:pointer}#currentRun>summary{cursor:pointer;list-style:none}#currentRun>summary::-webkit-details-marker{display:none}#currentRun>summary::before{color:var(--category-color);content:"▸ ";display:inline-block;font-size:24px;line-height:1;padding-right:8px;vertical-align:-2px}#currentRun[open]>summary::before{content:"▾ "}#currentRun[open]>summary{border-bottom:1px solid var(--category-color);margin-bottom:12px;padding-bottom:14px}#currentRun .card{--category-color:#f472b6;background:#241b25;border:1px solid var(--category-color);border-left-width:1px}#currentRun .current-run__grid>.card{border-left:1px solid var(--category-color)!important}#currentRun .card strong,#currentRun .label,#currentRun .queue-item__title{color:var(--category-color)}#currentRun .queue-item{border-top-color:#6e385d}#currentRun .estimate-meta{color:#d6bdca}
.log-card-header{align-items:center;display:flex;gap:12px;justify-content:space-between;margin-bottom:10px}.log-card-actions{align-items:center;display:flex;gap:8px}.log-card-actions .download.download--glyph,.clear-component-log--glyph{background:#3b281b;border-color:#f0b66a;color:#fff0dc;height:32px;min-height:32px;min-width:32px;position:static;width:32px}.log-card-actions .download.download--glyph:hover,.clear-component-log--glyph:hover{background:var(--category-color);color:#201812}.clear-component-log--glyph{border:1px solid var(--category-color);border-radius:50%;font:18px/1 system-ui;padding:0}.clear-component-log:disabled{cursor:wait;opacity:.7}
.copy.copy--glyph:hover,.download.download--glyph:hover,.prompt-history-report:hover,.report-view-modal__close:hover{background:var(--category-color);color:#17151a}
button:not(.theme-toggle):not(.section-state-toggle):hover:not(:disabled){background:var(--category-color,var(--house-style));color:#17151a}
.log-pagination{align-items:center;display:flex;gap:8px;justify-content:flex-end;margin-top:10px;min-height:32px}.log-pagination__summary{color:#d6c5a8;font-size:12px;margin-right:auto}.log-pagination button{background:#33281d;border:1px solid var(--category-color);border-radius:7px;color:#fff0dc;font:12px system-ui;min-height:32px;padding:5px 9px}.log-pagination button:disabled{cursor:not-allowed;opacity:.45}
#pullRefresh{align-items:center;background:#18181f;border:1px solid #58c8df;border-radius:999px;color:#bceefa;display:flex;font:13px system-ui;gap:8px;left:50%;opacity:0;padding:8px 13px;pointer-events:none;position:fixed;top:10px;transform:translate(-50%,-80px);transition:opacity .15s ease,transform .15s ease;z-index:20}#pullRefresh.pull-refresh--visible{opacity:1;transform:translate(-50%,0)}#pullRefresh::before{content:"↓";font-size:18px;line-height:1}@media (prefers-reduced-motion:reduce){#pullRefresh{transition:none}}
#dashboardSplash{align-items:center;background:#101015;display:flex;inset:0;justify-content:center;padding:24px;position:fixed;text-align:center;transition:opacity .25s ease,visibility .25s ease;z-index:100}#dashboardSplash[hidden]{display:none}body.dashboard-ready #dashboardSplash{opacity:0;pointer-events:none;visibility:hidden}.dashboard-splash__content{align-items:center;display:flex;flex-direction:column;gap:12px;max-width:360px}.dashboard-splash__title{color:#f7f3ee;font:700 clamp(28px,8vw,42px)/1.1 system-ui;margin:0}.dashboard-splash__version{color:#b79aff;font:600 14px/1.3 system-ui;letter-spacing:.04em;text-transform:uppercase}.dashboard-splash__loading{color:#c8c4cc;font:14px system-ui}.dashboard-splash__spinner{animation:dashboard-splash-spin .85s linear infinite;border:3px solid #332a44;border-radius:50%;border-top-color:#b79aff;height:34px;width:34px}@keyframes dashboard-splash-spin{to{transform:rotate(360deg)}}
#copyToast{background:var(--house-style-surface);border:1px solid var(--house-style);border-radius:999px;bottom:max(20px,env(safe-area-inset-bottom));box-shadow:0 8px 24px #0009;color:var(--house-style-contrast);font:600 13px system-ui;left:50%;opacity:0;padding:10px 15px;pointer-events:none;position:fixed;transform:translate(-50%,16px);transition:opacity .16s ease,transform .16s ease;z-index:110}#copyToast.copy-toast--visible{opacity:1;transform:translate(-50%,0)}
body{overflow-x:hidden}.dashboard-grid,.dashboard-grid>*{min-width:0}.telemetry,.platform-health{min-width:0}.telemetry-scroll{max-width:100%;-webkit-overflow-scrolling:touch}.platform-health__component{min-width:0}.platform-health__component-detail{overflow-wrap:anywhere}.dashboard-titlebar{align-items:center;background:#121217;box-shadow:0 10px 18px #121217;box-sizing:border-box;display:flex;gap:14px;justify-content:space-between;margin:0 0 18px;max-width:100%;padding:8px 16px 12px;position:sticky;top:max(8px,env(safe-area-inset-top));width:100%;z-index:15}.dashboard-titlebar h1{font-size:28px;line-height:1.1;margin:0}.dashboard-titlebar__actions{align-items:center;display:flex;flex:none;gap:12px}.auto-refresh-toggle{align-items:center;color:#b9b6c0;cursor:pointer;display:flex;flex:none;font-size:12px;gap:7px;white-space:nowrap}.auto-refresh-toggle input{accent-color:#54d6a0;height:18px;margin:0;width:18px}.section-state-toggle{align-items:center;background:transparent;border:0;color:#b9b6c0;cursor:pointer;display:flex;font:12px system-ui;gap:7px;margin:0;min-height:44px;min-width:44px;padding:0;position:relative}.section-state-toggle::before{background:#4a4a55;border-radius:999px;content:"";height:24px;transition:background .18s ease;width:42px}.section-state-toggle::after{background:#f7f3ee;border-radius:50%;box-shadow:0 1px 3px #0008;content:"";height:18px;left:3px;position:absolute;top:13px;transform:translateX(0);transition:transform .18s ease;width:18px}.section-state-toggle[aria-checked="true"]::before{background:#8dc7ff}.section-state-toggle[aria-checked="true"]::after{transform:translateX(18px)}.section-state-toggle:focus-visible{box-shadow:none;outline:2px solid #8dc7ff;outline-offset:3px}@media (max-width:620px){.dashboard-titlebar{align-items:flex-start}.dashboard-titlebar h1{font-size:25px}.auto-refresh-toggle span,.section-state-toggle__label{display:none}.dashboard-titlebar__actions{gap:8px}}
.dashboard-titlebar__brand{align-items:center;display:flex;gap:10px;min-width:0}.dashboard-app-icon{border-radius:9px;display:block;flex:none;height:32px;width:32px}@media (max-width:620px){.dashboard-app-icon{height:28px;width:28px}}
.inbox-queue{--category-color:#818cf8;background:#25243a;border:2px solid var(--category-color);border-radius:18px;box-shadow:0 5px 24px #0006;cursor:pointer;padding:14px}.inbox-queue>summary{border-bottom:1px solid var(--category-color);box-sizing:border-box;cursor:pointer;display:block;list-style:none;margin-bottom:8px;min-height:35px;padding:0 40px 10px 0;position:relative}.inbox-queue>summary::-webkit-details-marker{display:none}.inbox-queue>summary::before{color:var(--category-color);content:"▸ ";font-size:24px;line-height:1;margin:0;padding:0;position:absolute;right:0;top:0;vertical-align:-2px}.inbox-queue[open]>summary::before{content:"▾ "}.inbox-queue>summary>strong{color:var(--category-color);font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.inbox-queue .category-description{color:#c9c7e2;font-size:14px;line-height:1.4;margin:12px 0}.inbox-queue .estimate-meta{color:#c9c7e2}.inbox-queue .queue-item{border-top-color:#48446f}
.prompt-history{--category-color:#f29ab2;background:#36232d;border:2px solid var(--category-color);border-radius:18px;box-shadow:0 5px 24px #0006;cursor:pointer;padding:14px}.prompt-history>summary{border-bottom:1px solid var(--category-color);box-sizing:border-box;cursor:pointer;display:block;list-style:none;margin-bottom:8px;min-height:35px;padding:0 40px 10px 0;position:relative}.prompt-history>summary::-webkit-details-marker{display:none}.prompt-history>summary::before{color:var(--category-color);content:"▸ ";font-size:24px;line-height:1;margin:0;padding:0;position:absolute;right:0;top:0;vertical-align:-2px}.prompt-history[open]>summary::before{content:"▾ "}.prompt-history>summary>strong{color:var(--category-color);font-size:17px;font-weight:700;letter-spacing:.04em;line-height:1.25;text-transform:uppercase}.prompt-history .category-description{color:#dfc3ce;font-size:14px;line-height:1.4;margin:12px 0}.prompt-history .log-controls label,.prompt-history .log-table th{color:var(--category-color)}.prompt-history .log-table-wrap{border-color:color-mix(in srgb,var(--category-color) 55%,#3d3651)}.prompt-history .log-table th,.prompt-history .log-table td{border-bottom-color:color-mix(in srgb,var(--category-color) 30%,#3d3651)}.prompt-history .log-table th.log-sortable::after{color:var(--category-color)}.prompt-history .log-table th.log-sortable:focus-visible{outline:2px solid var(--category-color);outline-offset:-2px}.prompt-history .log-pagination button{background:#442b37;border-color:var(--category-color);color:#ffe9ef}.prompt-history-report{align-items:center;background:transparent;border:1px solid var(--category-color);border-radius:50%;color:var(--category-color);display:inline-flex;font:16px/1 system-ui;justify-content:center;min-height:32px;min-width:32px;padding:0}.prompt-history-report:hover{background:var(--category-color);color:#271923}.prompt-history-status{font-weight:700}.prompt-history-status--complete{color:#54d6a0}.prompt-history-status--blocked{color:#f0b66a}.prompt-history-status--failed{color:#ff718f}.report-view-modal{background:transparent;border:0;max-height:90vh;max-width:min(1000px,94vw);padding:0;width:min(1000px,94vw)}.report-view-modal::backdrop{background:#0d1018bb}.report-view-modal__panel{--category-color:#8dc7ff;background:#24242d;border:2px solid var(--category-color);border-radius:18px;box-shadow:0 16px 50px #000a;color:#f7f3ee;max-height:90vh;overflow:auto;padding:18px;position:relative}.report-view-modal__title{color:var(--category-color);font-size:20px;margin:0 80px 14px 0}.report-view-modal__actions{display:flex;gap:8px;position:absolute;right:16px;top:14px}.report-view-modal__actions .copy,.report-view-modal__actions .download{position:static}.report-view-modal__content{min-height:80px;padding:12px}.report-view-modal__close{background:#24242d;border:1px solid var(--category-color);border-radius:50%;color:var(--category-color);font:22px/1 system-ui;height:32px;min-height:32px;min-width:32px;padding:0}.report-view-modal__close:hover{background:var(--category-color);color:#17151a}
/* Keep disclosure arrows at the far edge of top-level dashboard categories. */
:where(.workspace-card,#queueItems,#rateLimits,.last-execution-group,#componentLogs,#codexChat,#engineering-dashboard-content>.technical-details:not(#componentLogs),#currentRun,.telemetry,.platform-health)>summary{padding-right:40px;position:relative}:where(.workspace-card,#queueItems,#rateLimits,.last-execution-group,#componentLogs,#codexChat,#engineering-dashboard-content>.technical-details:not(#componentLogs),#currentRun,.telemetry,.platform-health)>summary::before{margin:0;padding-right:0;position:absolute;right:0;top:0}
/* Uniform border hierarchy: main categories use 2px all around; nested cards use 1px all around. */
.card,.technical-details,.current-run,.telemetry,.platform-health,.last-execution-group{border-left-width:1px}.workspace-card,#rateLimits,.last-execution-group,#componentLogs,#codexChat,#engineering-dashboard-content>.technical-details:not(#componentLogs),#currentRun,.telemetry,.platform-health{border:2px solid var(--category-color);border-left-width:2px}#engineering-dashboard-content>.technical-details .card,.current-run .card,.last-execution-group .card--previous,.reviewer-agent,.platform-health__component{border-left-width:1px;border-width:1px}.category-icon{color:var(--category-color);display:inline-block;font:700 18px/1 system-ui;margin-right:8px;vertical-align:-1px}
/* The default remains dark; light mode deliberately restyles every surface. */
html[data-theme="light"] body{background:#f4f7fb;color:#182230}html[data-theme="light"] .dashboard-titlebar{background:#f4f7fb;box-shadow:0 10px 18px #d5deebaa}html[data-theme="light"] .dashboard-titlebar h1{color:#182230}html[data-theme="light"] .auto-refresh-toggle,html[data-theme="light"] .section-state-toggle{color:#415168}html[data-theme="light"] .card,html[data-theme="light"] .technical-details{background:#fff;box-shadow:0 5px 20px #2639551f}html[data-theme="light"] .card--operation,html[data-theme="light"] .current-run{background:#fff3fa}html[data-theme="light"] .card--monitoring,html[data-theme="light"] .platform-health__component{background:#effbfe}html[data-theme="light"] .card--context,html[data-theme="light"] .workspace-card{background:#fff9dc}html[data-theme="light"] .card--resource,html[data-theme="light"] #processMetrics,html[data-theme="light"] #usage,html[data-theme="light"] #rateLimits{background:#effbf5}html[data-theme="light"] .card--evidence,html[data-theme="light"] .last-execution-group,html[data-theme="light"] .card--previous,html[data-theme="light"] #commits{background:#f1f7ff}html[data-theme="light"] .card--diagnostic,html[data-theme="light"] .technical-details--diagnostic,html[data-theme="light"] #componentLogs{background:#fff6e9}html[data-theme="light"] .card--conversation,html[data-theme="light"] #codexChat{background:#f8f1ff}html[data-theme="light"] .telemetry{background:#fff0f4}html[data-theme="light"] .inbox-queue{background:#f1f2ff}html[data-theme="light"] .prompt-history{background:#fff1f5}html[data-theme="light"] #currentRun{background:#fff0f8}html[data-theme="light"] #engineering-dashboard-content>.technical-details:not(#componentLogs){background:#effbfe}html[data-theme="light"] .platform-health{background:#f2fae8}html[data-theme="light"] #currentRun .card,html[data-theme="light"] .technical-details .card,html[data-theme="light"] #componentLogs .card,html[data-theme="light"] .reviewer-agent{background:#fff}html[data-theme="light"] .field,html[data-theme="light"] p,html[data-theme="light"] pre,html[data-theme="light"] code{color:#182230}html[data-theme="light"] .estimate-meta,html[data-theme="light"] .category-description,html[data-theme="light"] .queue-empty,html[data-theme="light"] .queue-item__meta,html[data-theme="light"] .footer,html[data-theme="light"] .chat-status,html[data-theme="light"] .reviewer-agent__meta,html[data-theme="light"] .telemetry-empty{color:#58677b}html[data-theme="light"] .chat-input,html[data-theme="light"] .log-controls input,html[data-theme="light"] .log-controls select{background:#fff;color:#182230;border-color:#9aabc3}html[data-theme="light"] .chat-message--user{background:#eaf4ff;border:1px solid #6caee8}html[data-theme="light"] .chat-message--assistant{background:#f4edff;border-color:#b99ae5}html[data-theme="light"] .log-table-wrap{background:#fff;border-color:#aebbd0}html[data-theme="light"] .log-table th{background:#eaf0f8}html[data-theme="light"] .log-table th,html[data-theme="light"] .log-table td{border-bottom-color:#d4deeb;color:#182230}html[data-theme="light"] .log-line-number{color:#728197}html[data-theme="light"] .copy,html[data-theme="light"] .chat-send{background:#fff;color:#182230;border-color:#8596ad}html[data-theme="light"] #pullRefresh{background:#fff;color:#1c4e68}html[data-theme="light"] #dashboardSplash{background:#f4f7fb}html[data-theme="light"] .dashboard-splash__title{color:#182230}html[data-theme="light"] .skip-link{background:#182230;color:#fff}html[data-theme="light"] :where(button,input,select,textarea,summary,[role="button"],[tabindex]):focus-visible{box-shadow:0 0 0 5px #f4f7fb}
.theme-toggle{align-items:center;background:transparent;border:0;color:#b9b6c0;cursor:pointer;display:flex;font:12px system-ui;gap:7px;margin:0;min-height:44px;min-width:44px;padding:0;position:relative}.theme-toggle::before{background:#4a4a55;border-radius:999px;content:"";height:24px;transition:background .18s ease;width:42px}.theme-toggle::after{background:#f7f3ee;border-radius:50%;box-shadow:0 1px 3px #0008;content:"☀";font-size:12px;height:18px;left:3px;line-height:18px;position:absolute;text-align:center;top:13px;transform:translateX(0);transition:transform .18s ease;width:18px}.theme-toggle[aria-checked="true"]::before{background:#f0b66a}.theme-toggle[aria-checked="true"]::after{content:"☾";transform:translateX(18px)}.theme-toggle:focus-visible{box-shadow:none;outline:2px solid #8dc7ff;outline-offset:3px}html[data-theme="light"] .theme-toggle{color:#415168}html[data-theme="light"] .theme-toggle::before{background:#f0b66a}@media (max-width:620px){.theme-toggle__label{display:none}}
:where(button,input,select,textarea,summary,[role="button"],[tabindex]):focus-visible{outline:3px solid var(--category-color,#8dc7ff);outline-offset:3px;box-shadow:0 0 0 6px color-mix(in srgb,var(--category-color,#8dc7ff) 24%,transparent)}
html[data-theme="light"] .rate-limit-reset{background:#e8fff5;border-color:#259b70;color:#145a42}html[data-theme="light"] .rate-limit-reset:hover{background:#d6f8e8}html[data-theme="light"] :where(button,input,select,textarea,summary,[role="button"],[tabindex]):focus-visible{box-shadow:0 0 0 5px color-mix(in srgb,var(--category-color,#8dc7ff) 24%,transparent)}
html[data-theme="light"] .log-pagination button{background:#fff;border-color:var(--category-color);color:color-mix(in srgb,var(--category-color) 70%,#182230)}html[data-theme="light"] .log-pagination button:hover:not(:disabled){background:color-mix(in srgb,var(--category-color) 14%,#fff)}html[data-theme="light"] .log-pagination button:disabled{background:color-mix(in srgb,var(--category-color) 12%,#f4f7fb);border-color:color-mix(in srgb,var(--category-color) 48%,#d4deeb);color:color-mix(in srgb,var(--category-color) 45%,#58677b);opacity:1}
html[data-theme="light"] #technicalDetails .technical-grid>.card{background:#fff!important;color:#182230}html[data-theme="light"] #technicalDetails .technical-grid>.card :is(p,pre,code){color:#182230}
html[data-theme="light"] .platform-health__component-detail,html[data-theme="light"] .platform-health__empty,html[data-theme="light"] .platform-health__summary{color:#182230}
:is(.theme-toggle,.section-state-toggle)[aria-checked="true"]::before{background:var(--house-style)}.auto-refresh-toggle{font:12px system-ui;min-height:44px;min-width:44px}.auto-refresh-toggle input{appearance:none;background:#4a4a55;border:0;border-radius:999px;cursor:pointer;height:24px;margin:0;position:relative;transition:background .18s ease;width:42px}.auto-refresh-toggle input::after{background:#f7f3ee;border-radius:50%;box-shadow:0 1px 3px #0008;content:"";height:18px;left:3px;position:absolute;top:3px;transform:translateX(0);transition:transform .18s ease;width:18px}.auto-refresh-toggle input:checked{background:var(--house-style)}.auto-refresh-toggle input:checked::after{transform:translateX(18px)}.auto-refresh-toggle input:focus-visible{box-shadow:0 0 0 5px var(--house-style-focus);outline:3px solid var(--house-style);outline-offset:3px}html[data-theme="light"] .auto-refresh-toggle input{background:#4a4a55}html[data-theme="light"] .auto-refresh-toggle input:checked{background:var(--house-style)}
html[data-theme="light"] .component-modal{background:#d8e1edb8}html[data-theme="light"] .component-modal__panel{background:#fff;color:#182230;box-shadow:0 16px 52px #26395540}html[data-theme="light"] .component-modal dd{color:#182230}html[data-theme="light"] .component-modal__close{background:#fff;color:#3c7411}html[data-theme="light"] .component-modal__close:hover{background:#effbdc}html[data-theme="light"] .component-modal__restart{background:#effbdc;color:#244b12}html[data-theme="light"] .component-modal__restart:hover:not(:disabled){background:#def5bd}html[data-theme="light"] .component-modal__status{color:#415168}
html[data-theme="light"] #componentLogs .clear-component-log,html[data-theme="light"] #componentLogs .log-pagination button{background:#fff8ef!important;border-color:#d68b23!important;color:#643a13!important}html[data-theme="light"] #componentLogs .clear-component-log:hover:not(:disabled),html[data-theme="light"] #componentLogs .log-pagination button:hover:not(:disabled){background:var(--category-color)!important;color:#24170d!important}html[data-theme="light"] #componentLogs .clear-component-log:disabled,html[data-theme="light"] #componentLogs .log-pagination button:disabled{background:#fff3e2!important;border-color:#e0bd8f!important;color:#8f7457!important;opacity:1}
/* Report panels are their own readable document surface in both themes. */
html[data-theme="light"] #reportContent,html[data-theme="light"] #reportAnalysisContent{background:#fff;color:#182230;box-shadow:inset 0 0 0 1px #d8e5f3}html[data-theme="light"] #reportContent :is(p,li,pre,code),html[data-theme="light"] #reportAnalysisContent :is(p,li,pre,code){color:#182230}html[data-theme="light"] #reportContent :is(pre,code),html[data-theme="light"] #reportAnalysisContent :is(pre,code){background:#eef4fb}html[data-theme="light"] .markdown-copy-wrap .copy.copy--glyph,html[data-theme="light"] .markdown-copy-wrap .download.download--glyph{background:#f7fbff;border-color:var(--category-color);box-shadow:0 2px 8px #2639552e;color:#1c4e68}html[data-theme="light"] .markdown-copy-wrap .copy.copy--glyph:hover,html[data-theme="light"] .markdown-copy-wrap .download.download--glyph:hover{background:#e4f0ff}
html[data-theme="light"] #downloadChat.download--glyph{background:#fff7ff;border-color:#b99ae5;box-shadow:0 2px 8px #6e548e2e;color:#68498a}html[data-theme="light"] #downloadChat.download--glyph:hover{background:#f1e4ff}
html[data-theme="light"] .report-view-modal__panel{background:#f7fbff;color:#182230}html[data-theme="light"] .report-view-modal__content{background:#fff;color:#182230}html[data-theme="light"] .report-view-modal__close{background:#fff;color:#1c4e68}
/* Keep the download action compact and legible instead of relying on a font-dependent glyph. */
.download.download--glyph::before{content:"↓";font:700 21px/1 system-ui}.download.download--glyph::after{content:none}.markdown-copy-wrap .download.download--glyph{align-items:center;justify-content:center;line-height:1}
.footer .label{color:var(--house-style)}
/* Product controls share the house style rather than individual category colours. */
.theme-toggle[aria-checked="true"]::before,.section-state-toggle[aria-checked="true"]::before,.auto-refresh-toggle input:checked{background:var(--house-style)}.theme-toggle:focus-visible,.section-state-toggle:focus-visible{outline-color:var(--house-style);box-shadow:0 0 0 5px var(--house-style-focus)}.rate-limit-reset{background:var(--house-style-surface);border-color:var(--house-style);color:var(--house-style-contrast)}.rate-limit-reset:hover:not(:disabled){background:color-mix(in srgb,var(--house-style-surface) 78%,var(--house-style))}html[data-theme="light"] .rate-limit-reset{background:var(--house-style-surface);border-color:var(--house-style);color:var(--house-style-contrast)}html[data-theme="light"] .rate-limit-reset:hover:not(:disabled){background:color-mix(in srgb,var(--house-style-surface) 78%,var(--house-style))}
/* Component cards use symmetric vertical padding; the info action belongs at the card centre. */
.platform-health__component{align-items:start;grid-template-areas:"indicator name info" "indicator detail info";grid-template-columns:auto minmax(0,1fr) auto;padding:12px 10px;row-gap:8px}.platform-health__component .indicator{align-self:center;grid-area:indicator}.platform-health__component-name{grid-area:name}.platform-health__component-detail{grid-area:detail;margin:0}.component-info{align-self:center;grid-area:info}
.chat-input{resize:vertical}
html[data-theme="light"] .predecessor-retry{background:#fff8ef;border-color:#d68b23;color:#643a13}html[data-theme="light"] .predecessor-retry:hover:not(:disabled){background:#fff0dc}
</style>
</head>
<body>
<a class="skip-link" href="#engineering-dashboard-content">Naar dashboardinhoud</a>
<div id="dashboardSplash" role="status" aria-live="polite" data-testid="dashboard-splash"><div class="dashboard-splash__content"><h2 class="dashboard-splash__title">$TITLE</h2><span class="dashboard-splash__version">Engineering Platform $PLATFORM_VERSION</span><span class="dashboard-splash__spinner" aria-hidden="true"></span><span class="dashboard-splash__loading">Status laden…</span></div></div>
<div id="copyToast" role="status" aria-live="polite" aria-atomic="true" hidden data-testid="copy-toast"></div>
<div id="pullRefresh" role="status" aria-live="polite" aria-hidden="true" data-testid="pull-refresh">Trek omlaag om te vernieuwen</div>
<header class="dashboard-titlebar"><div class="dashboard-titlebar__brand"><img class="dashboard-app-icon" src="/assets/engineering-status-icon.svg" alt="" aria-hidden="true" data-testid="dashboard-app-icon"><h1>$TITLE</h1></div><div class="dashboard-titlebar__actions"><button class="theme-toggle" id="themeToggle" type="button" role="switch" aria-checked="false" aria-label="Lichte modus inschakelen" data-testid="theme-toggle"><span class="theme-toggle__label">Thema</span></button><button class="section-state-toggle" id="toggleAllSections" type="button" role="switch" aria-checked="false" aria-label="Alle secties openen" data-testid="toggle-all-sections"><span class="section-state-toggle__label">Uitklappen</span></button><label class="auto-refresh-toggle" for="autoRefresh"><input id="autoRefresh" type="checkbox" role="switch" checked><span>Automatisch vernieuwen</span></label></div></header>
<details class="card card--context workspace-card" id="workspaceCard" data-testid="engineering-workspace"><summary><strong>Workspace</strong></summary><p class="field"><span class="label">Naam</span><span>$WORKSPACE_ID</span></p><div class="field"><span class="label">Workspace locatie</span><pre>$WORKSPACE_LOCATION</pre></div><p class="field"><span class="label">Tracked files</span><span>$TRACKED_FILES</span></p><div class="field"><span class="label">Engineering-database</span><pre>$ENGINEERING_DATABASE_PATH</pre></div><p class="field"><span class="label">Databasegrootte</span><span>$ENGINEERING_DATABASE_SIZE</span></p><p class="field"><span class="label">Schema-versie</span><span>$ENGINEERING_DATABASE_SCHEMA_VERSION</span></p></details>
<main class="dashboard-grid" id="engineering-dashboard-content" tabindex="-1">
<details class="inbox-queue" id="queueItems" data-testid="engineering-inbox-queue"><summary><strong>Inbox-wachtrij</strong></summary><p class="category-description">Prompts worden uitgevoerd op volgorde van aanmaakdatum.</p><p class="estimate-meta" id="queueSummary">Wachtrij laden…</p><ol class="queue-list" id="queueList" aria-live="polite"></ol></details>
<details class="prompt-history" id="promptHistory" data-testid="engineering-prompt-history"><summary><strong>Promptgeschiedenis</strong></summary><p class="category-description">Alle terminale Engineering Platform-uitvoeringen, lokaal gecachet in de Engineering SQLite-opslag.</p><div class="log-controls"><label for="promptHistoryFilter">Zoeken<input id="promptHistoryFilter" type="search" maxlength="160" data-sanitize="single-line" placeholder="Zoek in alle velden"></label></div><div class="log-table-wrap"><table class="log-table" aria-label="Promptgeschiedenis"><thead><tr><th data-history-sort-key="status" scope="col">Status</th><th data-history-sort-key="title" scope="col">Prompttitel</th><th data-history-sort-key="executed_at" scope="col">Uitgevoerd op</th><th data-history-sort-key="git_commit" scope="col">Git-commit</th><th scope="col">Rapport</th></tr></thead><tbody id="promptHistoryRows"><tr><td class="log-empty" colspan="5">Promptgeschiedenis laden…</td></tr></tbody></table></div><nav class="log-pagination" id="promptHistoryPagination" aria-label="Paginering Promptgeschiedenis"></nav></details>
<details class="current-run" id="currentRun" aria-label="Huidige uitvoering" hidden><summary class="current-run__title"><span class="label">Actieve prompt</span><h2 id="currentPrompt">Laden…</h2><div class="field"><span class="label">Bestandsnaam</span><pre id="currentFile">Laden…</pre></div></summary><div class="current-run__grid">
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" aria-label="Status onbekend"></span><strong>Promptstatus</strong></div><p class="field"><span class="label">Watcher</span><span id="watcher">Laden…</span></p><p class="field"><span class="label">Fase</span><span id="phase">Laden…</span></p><p class="field"><span class="label">Huidige actie</span><span id="action">Laden…</span></p></div>
<div class="card" id="predecessorGate" hidden><strong>Wachtrij geblokkeerd</strong><p class="field"><span class="label">Blokkerende run</span><code id="predecessorRun"></code></p><p class="field"><span class="label">Voorafgaande prompt</span><span id="predecessorPrompt"></span></p><p class="field"><span class="label">Eindstatus</span><span id="predecessorPhase"></span></p><div class="field"><span class="label">Herstelactie</span><pre id="predecessorAction"></pre></div><button class="predecessor-retry" id="predecessorRetry" type="button">Opnieuw indienen</button><p class="predecessor-retry-status" id="predecessorRetryStatus" role="status" aria-live="polite"></p></div>
<div class="card"><strong>Tijd</strong><p id="currentTime">Laden…</p><p id="lastRefresh">Laatst bijgewerkt: laden…</p><p id="updateMode">Serverpush: verbinden…</p></div>
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
<dialog class="report-view-modal" id="promptHistoryReportModal" aria-labelledby="promptHistoryReportModalTitle"><section class="report-view-modal__panel"><h2 class="report-view-modal__title" id="promptHistoryReportModalTitle">Engineeringrapport</h2><div class="report-view-modal__actions"><button class="download download--glyph" id="promptHistoryReportDownload" type="button" title="Download engineeringrapport" aria-label="Download engineeringrapport" hidden>⇩</button><button class="copy copy--glyph" id="promptHistoryReportCopy" type="button" title="Kopieer engineeringrapport" aria-label="Kopieer engineeringrapport" hidden>⧉</button><button class="report-view-modal__close" id="promptHistoryReportClose" type="button" aria-label="Engineeringrapport sluiten">×</button></div><article class="markdown-document report-view-modal__content" id="promptHistoryReportContent">Rapport laden…</article></section></dialog>
<button id="loadComponentLogs" type="button" hidden>Logs laden</button>
<details class="technical-details" id="componentLogs"><summary><strong>Logs</strong></summary><p class="estimate-meta">Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush.</p><div class="log-controls" id="componentLogControls" hidden><label for="logFilter">Zoeken<input id="logFilter" type="search" maxlength="160" data-sanitize="single-line" placeholder="Zoek in alle velden"></label><label for="logLevelFilter">Niveau<select id="logLevelFilter"><option value="">Alle niveaus</option><option value="ERROR">Fout</option><option value="WARNING">Waarschuwing</option><option value="INFO">Informatie</option><option value="DEBUG">Debug</option></select></label><label for="logSort">Sortering<select id="logSort"><option value="newest">Nieuwste eerst</option><option value="oldest">Oudste eerst</option><option value="level">Niveau</option><option value="event">Gebeurtenis</option></select></label></div><div class="technical-grid"><div class="card"><div class="log-card-header"><strong>Inbox-watcher</strong><div class="log-card-actions"><button class="download download--glyph component-log-download" data-component="inbox" data-testid="download-inbox-log" type="button" title="Download Inbox-watcher-log" aria-label="Download Inbox-watcher-log">⇩</button><button class="clear-component-log" data-component="inbox" data-testid="clear-inbox-log" type="button">Logs wissen</button></div></div><div class="log-table-wrap"><table class="log-table"><thead><tr><th>#</th><th>Tijdstip</th><th>Niveau</th><th>Gebeurtenis</th><th>Run-ID</th><th>Details</th></tr></thead><tbody id="inboxComponentLog"><tr><td class="log-empty" colspan="6">Nog niet geladen.</td></tr></tbody></table></div><nav class="log-pagination" id="inboxLogPagination" aria-label="Paginering Inbox-watcher"></nav></div><div class="card"><div class="log-card-header"><strong>Statusdashboard</strong><div class="log-card-actions"><button class="download download--glyph component-log-download" data-component="dashboard" data-testid="download-dashboard-log" type="button" title="Download Statusdashboard-log" aria-label="Download Statusdashboard-log">⇩</button><button class="clear-component-log" data-component="dashboard" data-testid="clear-dashboard-log" type="button">Logs wissen</button></div></div><div class="log-table-wrap"><table class="log-table"><thead><tr><th>#</th><th>Tijdstip</th><th>Niveau</th><th>Gebeurtenis</th><th>Run-ID</th><th>Details</th></tr></thead><tbody id="dashboardComponentLog"><tr><td class="log-empty" colspan="6">Nog niet geladen.</td></tr></tbody></table></div><nav class="log-pagination" id="dashboardLogPagination" aria-label="Paginering Statusdashboard"></nav></div></div></details>
<details class="card codex-chat" id="codexChat"><summary><strong>AI-gesprek</strong></summary><p class="category-description">Stel korte, alleen-lezen vragen over de laatst uitgevoerde prompt en het bijbehorende rapport. Dit start geen engineering of wijzigingen.</p><div class="codex-chat__details"><button class="download download--glyph" id="downloadChat" type="button" title="Download gesprek" aria-label="Download gesprek" hidden>⇩</button><div class="chat-messages" id="chatMessages" aria-live="polite" aria-label="Gesprek met AI-assistent"></div><label class="label" for="chatInput">Nieuwe vraag aan AI-assistent</label><div class="chat-compose"><textarea id="chatInput" class="chat-input" rows="5" maxlength="2000" autocomplete="off" data-sanitize="multiline" placeholder="Bijvoorbeeld: wat zijn de belangrijkste vervolgstappen uit het laatste rapport?"></textarea><button class="chat-send" id="chatSend" type="button" title="Verstuur vraag" aria-label="Verstuur vraag"><span aria-hidden="true">➤</span></button></div><p class="field"><span class="label">Gebruikt model</span><span id="chatModel">$CHAT_MODEL</span></p><p class="chat-status" id="chatStatus"></p></div></details>
<details class="technical-details" id="technicalDetails"><summary><strong>Technische details</strong></summary><div class="technical-grid">
<div class="card"><strong>Pull requests</strong><p class="field"><span class="label">Implementatie</span><span id="implementation"></span></p><p class="field"><span class="label">Finalisatie</span><span id="finalization"></span></p></div>
<div class="card"><strong>Repository</strong><p class="field"><span class="label">Repositorystatus</span><span id="repositoryState"></span></p><p class="field"><span class="label">Werkruimtestatus</span><span id="workspaceState"></span></p></div>
<div class="card"><strong>Diagnose</strong><p id="diag"></p></div>
</div></details>
</main>
<footer class="footer"><span class="label">Engineering Platform-versie</span><span id="platformVersion">Laden…</span></footer><span id="dashboardVersion" hidden></span><span id="workerVersion" hidden></span>
<script>
const $=id=>document.getElementById(id),DASHBOARD_BUILD="$BUILD_COMMIT",DASHBOARD_BUILD_KEY="djconnect-engineering-dashboard-build",
formatTime=new Intl.DateTimeFormat("nl-NL",{timeZone:"Europe/Amsterdam",dateStyle:"full",timeStyle:"medium"}),
fallback={watcher_state:"REMOTE_ENGINEERING_DEGRADED",current_phase:"status niet beschikbaar",current_action:"Ververs het dashboard nadat het Engineering Platform een statusupdate heeft gepubliceerd.",queue_depth:0,repository_state:"UNKNOWN",workspace_state:"UNKNOWN",diagnostic:"Het statusverzoek kon niet worden voltooid."};
let currentLogRun,lastLogRun,lastRefresh,promptStartedAt,latestStatus;
function sanitizeFreeText(value,maximumLength,multiline=false){const normalized=String(value??"").normalize("NFC").replace(/\r\n?/g,"\n").replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g,"");return (multiline?normalized:normalized.replace(/\n+/g," ")).slice(0,maximumLength)}function sanitizeDeclaredFreeInput(element){const maximumLength=Number(element.maxLength)>0?Number(element.maxLength):160,clean=sanitizeFreeText(element.value,maximumLength,element.dataset.sanitize==="multiline");if(element.value!==clean)element.value=clean;return clean}document.querySelectorAll("input[data-sanitize],textarea[data-sanitize]").forEach(element=>element.addEventListener("input",()=>sanitizeDeclaredFreeInput(element)));
const humanLabels={ENGINEERING_RUN_ACTIVE:"Engineering actief",WATCHER_IDLE:"Watcher wacht",REMOTE_ENGINEERING_DEGRADED:"Engineeringstatus beperkt beschikbaar",JOB_CLAIMED:"Opdracht opgepakt",RUNNER_STARTING:"Uitvoering wordt gestart",REPORT_PUBLISHING:"Rapport wordt gepubliceerd",JOB_COMPLETED:"Opdracht voltooid",JOB_BLOCKED:"Opdracht geblokkeerd",JOB_FAILED:"Opdracht mislukt",WAITING_FOR_REPOSITORY:"Wacht op repository",WAITING_FOR_PREDECESSOR:"Wacht op voorafgaande prompt",INITIALIZE:"Voorbereiding",EXECUTE_AGENT:"Uitvoering",REPAIR_AGENT:"Herstel",FINALIZE_AGENT:"Finalisatie",REPOSITORY_CLEANUP:"Opschoning repository",COMPLETE:"Voltooid",BLOCKED:"Geblokkeerd",FAILED:"Mislukt",invoke_agent:"Engineering uitvoeren",repository_reconciled:"Repository afgestemd",MERGED_RECONCILED:"Samengevoegd en afgestemd",WORKSPACE_READY:"Werkruimte gereed",ACTIVE:"Actief",UNKNOWN:"Onbekend","status unavailable":"status niet beschikbaar"};
const dutchDiagnostics={"Engineering report was not available for delivery.":"Engineeringrapport kon niet worden afgeleverd.","Runner ended without a safe terminal report.":"De runner stopte zonder een veilig eindrapport.","An existing engineering transaction remains active.":"Een bestaande engineeringuitvoering is nog actief.","Duplicate job digest remains recorded.":"Een dubbele opdracht is al geregistreerd.","Another watcher owns the local inbox lock.":"Een andere watcher beheert de lokale Inbox-vergrendeling.","No local engineering status has been published yet.":"Er is nog geen lokale engineeringstatus gepubliceerd.","The status request could not be completed.":"Het statusverzoek kon niet worden voltooid."};
function translate(value){return humanLabels[value]||dutchDiagnostics[value]||value}
function humanize(){for(const id of ["watcher","phase","action","repositoryState","workspaceState","diag"]){const element=$(id);element.textContent=translate(element.textContent)}}
function tone(x){const phase=x.current_phase||"",watcher=x.watcher_state||"";if(["BLOCKED","FAILED"].includes(phase)||["JOB_BLOCKED","JOB_FAILED"].includes(watcher))return "red";if(phase==="COMPLETE"||watcher==="JOB_COMPLETED")return "green";if(phase==="WAIT_FOR_TERMINAL_EVIDENCE"||["WAITING_FOR_REPOSITORY","WAITING_FOR_PREDECESSOR"].includes(watcher))return "yellow";if(["INITIALIZE","EXECUTE_AGENT","REPAIR_AGENT","FINALIZE_AGENT","REPOSITORY_CLEANUP"].includes(phase)||["RUNNER_STARTING","JOB_CLAIMED"].includes(watcher))return "orange";return "grey"}
function finalStatus(phase){if(phase==="COMPLETE")return ["green","Voltooid"];if(phase==="BLOCKED")return ["yellow","Geblokkeerd"];if(phase==="FAILED")return ["red","Mislukt"];return ["grey","Status onbekend"]}
function executionRange(x){const characters=Number(x.prompt_characters)||0;if(characters<=2000)return [6,10];if(characters<=6000)return [10,18];if(characters<=12000)return [16,26];return [24,38]}
function pluralMinutes(value){return value===1?"minuut":"minuten"}
function estimate(x){const phase=x.current_phase||"";if(phase==="INITIALIZE")return {summary:"Voorbereiding: minder dan 1 minuut",context:""};if(["EXECUTE_AGENT","REPAIR_AGENT"].includes(phase)){const [minimum,maximum]=executionRange(x);if(!promptStartedAt)return {summary:"Indicatieve totale duur: "+minimum+"–"+maximum+" minuten",context:"Gebaseerd op promptomvang en fase. Live Codex-voortgang is niet beschikbaar."};const elapsed=Math.max(0,Math.floor((Date.now()-promptStartedAt)/60000)),remainingMinimum=Math.max(1,minimum-elapsed),remainingMaximum=Math.max(remainingMinimum,maximum-elapsed);return {summary:"Indicatief resterend: "+remainingMinimum+"–"+remainingMaximum+" minuten",context:elapsed+" "+pluralMinutes(elapsed)+" verstreken."+String.fromCharCode(10)+"gebaseerd op promptomvang, fase en verstreken tijd. Geen live Codex-voortgang of tokenverbruik."}}if(phase==="FINALIZE_AGENT")return {summary:"Finalisatie in uitvoering",context:"De resterende tijd is pas betrouwbaar met live Codex-voortgang."};if(phase==="REPOSITORY_CLEANUP")return {summary:"Opschoning in uitvoering",context:"De resterende tijd hangt af van de lokale repository."};if(phase==="WAIT_FOR_TERMINAL_EVIDENCE")return {summary:"Wacht op externe verificatie",context:"Geen betrouwbare ETA."};if(phase==="COMPLETE")return {summary:"Voltooid",context:""};if(["BLOCKED","FAILED"].includes(phase))return {summary:"Gestopt; actie nodig",context:""};return {summary:"Nog niet beschikbaar",context:""}}
function renderEstimate(x){const value=estimate(x);$("executionEstimate").textContent=value.summary;$("executionEstimateMeta").textContent=value.context;$("executionEstimateMeta").hidden=!value.context}
function isActiveRun(x){return x.watcher_state==="ENGINEERING_RUN_ACTIVE"&&Boolean(x.run_id)}
function checkBuild(build){if(build===DASHBOARD_BUILD){sessionStorage.removeItem(DASHBOARD_BUILD_KEY);return}if(build&&DASHBOARD_BUILD!=="onbekend"&&sessionStorage.getItem(DASHBOARD_BUILD_KEY)!==build){sessionStorage.setItem(DASHBOARD_BUILD_KEY,build);location.reload()}}
function clock(){let now=Date.now();$("currentTime").textContent=formatTime.format(new Date(now));$("lastRefresh").textContent="Laatst bijgewerkt: "+(lastRefresh?formatTime.format(lastRefresh):"laden…")}
function l(id,url,run,last,container){if(run===(last?lastLogRun:currentLogRun))return;if(last)lastLogRun=run;else currentLogRun=run;$(id).textContent="Diagnose laden…";fetch(url).then(x=>x.text()).then(x=>{const available=Boolean(x)&&!x.startsWith("No Codex CLI diagnostic is available")&&!x.startsWith("Geen Codex CLI-diagnose beschikbaar");$(container).hidden=false;$(id).textContent=available?x:(last?"Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze uitgevoerde prompt.":"Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze actieve prompt.")}).catch(()=>{$(container).hidden=false;$(id).textContent=last?"Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze uitgevoerde prompt.":"AI-uitvoeringsdiagnose is niet beschikbaar voor deze actieve prompt."})}
function usage(x){const labels={input_tokens:"Invoertokens",cached_input_tokens:"Gecachete invoertokens",output_tokens:"Uitvoertokens",total_tokens:"Totaal tokens",cost:"Kosten",remaining:"Resterend beschikbaar",plan_remaining:"Resterend in plan",usage:"Gebruik"};let entries=Object.entries(x||{});$("usage").hidden=!entries.length;$("usageDetails").textContent=entries.map(([key,value])=>(labels[key]||key.replaceAll("_"," "))+": "+value).join(String.fromCharCode(10))}
function rateLimits(x){const windows=Array.isArray(x?.windows)?x.windows:[],credits=Number.isInteger(x?.reset_credits)?x.reset_credits:null,provider=typeof x?.provider==="string"?x.provider:"Niet beschikbaar",version=typeof x?.provider_version==="string"?x.provider_version:"versie niet beschikbaar",button=$("rateLimitReset");$("rateLimits").hidden=!windows.length&&credits===null&&provider==="Niet beschikbaar";$("rateLimitProvider").textContent=provider+" · "+version;let lines=windows.map(window=>{const remaining=Math.max(0,100-Number(window.used_percent||0)),reset=Number(window.resets_at);return window.label+": "+remaining+"% beschikbaar · reset "+(Number.isFinite(reset)?formatTime.format(new Date(reset*1000)):"onbekend")});if(credits!==null)lines.push("Beschikbare resets: "+credits);$("rateLimitDetails").textContent=lines.join(String.fromCharCode(10));button.hidden=!(credits>0);button.disabled=false}
function consumeRateLimitReset(){const button=$("rateLimitReset"),status=$("rateLimitResetStatus");if(button.hidden||button.disabled)return;if(!window.confirm("Gebruik één beschikbare Codex-reset? Deze actie verbruikt een resetcredit."))return;button.disabled=true;status.textContent="Reset gebruiken…";fetch("/api/rate-limit-reset",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}).then(async response=>({ok:response.ok,body:await response.json()})).then(result=>{if(!result.ok)throw Error(result.body.error||"Reset kon niet worden uitgevoerd.");const messages={reset:"Reset gebruikt. De gebruikslimieten zijn bijgewerkt.",nothingToReset:"Er is op dit moment niets om te resetten.",noCredit:"Er is geen resetcredit beschikbaar.",alreadyRedeemed:"Deze resetcredit is al gebruikt."};status.textContent=messages[result.body.outcome]||"Reset verwerkt.";if(result.body.rate_limits)rateLimits(result.body.rate_limits)}).catch(error=>{status.textContent=error.message}).finally(()=>{button.disabled=false})}
function lastUsage(x){const labels={input_tokens:"Invoertokens",cached_input_tokens:"Gecachete invoertokens",output_tokens:"Uitvoertokens",total_tokens:"Totaal tokens",cost:"Kosten",remaining:"Resterend beschikbaar",plan_remaining:"Resterend in plan",usage:"Gebruik"};let entries=Object.entries(x||{});$("lastUsage").hidden=!entries.length;$("lastUsageDetails").textContent=entries.map(([key,value])=>(labels[key]||key.replaceAll("_"," "))+": "+value).join(String.fromCharCode(10))}
function lastRuntimeMetadata(metadata){const fields=[["runtime_provider","lastRuntimeProvider","lastRuntimeProviderValue"],["model","lastModel","lastModelValue"],["reasoning_profile","lastReasoningProfile","lastReasoningProfileValue"],["configuration_profile","lastConfigurationProfile","lastConfigurationProfileValue"],["codex_cli_version","lastCodexCliVersion","lastCodexCliVersionValue"]];for(const [key,fieldId,valueId] of fields){const value=metadata&&typeof metadata[key]==="string"&&metadata[key]!=="not reported"?metadata[key]:"";$(fieldId).hidden=!value;$(valueId).textContent=value}}
function processMetrics(active,x){$("processMetrics").hidden=!active;if(!active)return;$("codexCpu").textContent=Number(x?.cpu_percent||0).toLocaleString("nl-NL",{maximumFractionDigits:1})+"%";$("codexProcesses").textContent=x?.process_count??0;$("codexGpu").textContent=x?.gpu_status||"Niet beschikbaar"}
function commits(x){let entries=Object.entries(x||{});$("commits").hidden=!entries.length;$("completionCommits").textContent=entries.map(([label,sha])=>label+": "+sha).join(String.fromCharCode(10))}
function lastCommits(x){let entries=Object.entries(x||{});$("lastCommits").hidden=!entries.length;$("lastCommitDetails").textContent=entries.map(([label,sha])=>label+": "+sha).join(String.fromCharCode(10))}
function lastExecutionTime(x){let seconds=Number(x?.seconds),field=$("lastExecutionTime"),value=$("lastExecutionTimeValue");if(!field){field=document.createElement("div");value=document.createElement("span");const label=document.createElement("span");field.className="field";field.id="lastExecutionTime";value.id="lastExecutionTimeValue";label.className="label";label.textContent="Codex CLI-uitvoeringstijd";field.append(label,value);$("lastFile").closest(".field").insertAdjacentElement("afterend",field)}field.hidden=!Number.isFinite(seconds)||seconds<0;if(field.hidden)return;const hours=Math.floor(seconds/3600),minutes=Math.floor((seconds%3600)/60),remaining=Math.round(seconds%60);value.textContent=(hours?hours+" u ":"")+(minutes?minutes+" min ":"")+remaining+" sec"}
function reviewerAgents(items){const agents=Array.isArray(items)?items:[],card=$("reviewerAgents"),list=$("reviewerAgentList");card.hidden=!agents.length;list.replaceChildren();for(const agent of agents){if(!agent||typeof agent!=="object")continue;const row=document.createElement("article"),name=document.createElement("p"),capability=document.createElement("p"),reason=document.createElement("p"),recommendations=Number(agent.accepted_recommendations)||0;row.className="reviewer-agent";name.className="reviewer-agent__name";capability.className=reason.className="reviewer-agent__meta";name.textContent=String(agent.reviewer||"Specialistische review").replaceAll("_"," ");capability.textContent="Capaciteit: "+String(agent.capability||"engineering")+" · "+String(agent.status||"Uitgevoerd")+" · Gebruikte aanbevelingen: "+recommendations;reason.textContent="Geselecteerd voor: "+String(agent.selected_because||"Niet vastgelegd.");row.append(name,capability,reason);list.append(row)}}
function queueItems(x,queueDepth){const items=(Array.isArray(x)?x:[]).filter(item=>item&&typeof item==="object").sort((left,right)=>{const first=Date.parse(left.modified_at||""),second=Date.parse(right.modified_at||"");if(Number.isFinite(first)&&Number.isFinite(second)&&first!==second)return first-second;if(Number.isFinite(first)!==Number.isFinite(second))return Number.isFinite(first)?-1:1;return String(left.filename||"").localeCompare(String(right.filename||""),"nl")}),container=$("queueList"),depth=Number.isInteger(queueDepth)&&queueDepth>=0?queueDepth:items.length;$("queueSummary").textContent=depth===0?"0 prompts in de wachtrij.":depth+" "+(depth===1?"prompt":"prompts")+" in de wachtrij."+(depth>items.length?" De eerste "+items.length+" worden getoond.":"");container.replaceChildren();if(!items.length){const empty=document.createElement("li");empty.className="queue-empty";empty.textContent="Geen Inbox-prompts wachten op uitvoering.";container.append(empty);return}items.forEach((item,index)=>{const row=document.createElement("li"),number=document.createElement("span"),body=document.createElement("div"),title=document.createElement("span"),meta=document.createElement("div"),modified=Date.parse(item.modified_at||""),filename=item.filename||"Bestandsnaam niet beschikbaar";row.className="queue-item";row.setAttribute("aria-label","Positie "+(index+1)+": "+(item.title||filename));number.className="queue-item__number";number.textContent=String(index+1);title.className="queue-item__title";meta.className="queue-item__meta";title.textContent=item.title||filename;meta.textContent="Bestandsnaam: "+filename+" · gewijzigd: "+(Number.isFinite(modified)?formatTime.format(new Date(modified)):"Tijdstip niet beschikbaar");body.append(title,meta);row.append(number,body);container.append(row)})}
function promptStarted(x){promptStartedAt=x?.started_at?Date.parse(x.started_at):undefined;$("promptStarted").textContent=promptStartedAt?formatTime.format(new Date(promptStartedAt)):"Niet beschikbaar";if(latestStatus)renderEstimate(latestStatus)}
let lastExecutedRun,reportLoaded=false,reportRequest,analysisLoaded=false,analysisRequest;function renderMarkdownDocument(target,value){target.replaceChildren();renderMarkdownAnswer(target,value)}function report(){if(!lastExecutedRun)return Promise.resolve();if(reportLoaded)return reportRequest;reportLoaded=true;return reportRequest=fetch("/api/report/last-executed?run_id="+encodeURIComponent(lastExecutedRun)).then(x=>x.text()).then(x=>{if(!x){$("report").hidden=true;return}renderMarkdownDocument($("reportContent"),x)}).catch(()=>{$("reportContent").textContent="Engineeringrapport is niet beschikbaar."})}function analysis(){if(!lastExecutedRun)return Promise.resolve();if(analysisLoaded)return analysisRequest;analysisLoaded=true;return analysisRequest=fetch("/api/report-analysis/last-executed?run_id="+encodeURIComponent(lastExecutedRun)).then(x=>x.text()).then(x=>{if(!x){$("reportAnalysis").hidden=true;return}renderMarkdownDocument($("reportAnalysisContent"),x)}).catch(()=>{$("reportAnalysisContent").textContent="Codex-analyse is niet beschikbaar."})}
let componentLogsLoaded=false,componentLogEntries={inbox:[],dashboard:[]};function structuredLogEntries(text){return String(text||"").split(/\r?\n/).filter(Boolean).map((line,index)=>{try{const entry=JSON.parse(line);if(!entry||typeof entry!=="object"||Array.isArray(entry))throw Error("not an object");const known=new Set(["timestamp","level","event","run_id","component"]),details=Object.entries(entry).filter(([key])=>!known.has(key)).map(([key,value])=>key+": "+(typeof value==="string"?value:JSON.stringify(value))).join(" · ");return {line:index+1,timestamp:String(entry.timestamp||""),level:String(entry.level||"ONBEKEND").toUpperCase(),event:String(entry.event||"onbekend"),runId:entry.run_id==null?"":String(entry.run_id),details:details}}catch{return {line:index+1,timestamp:"",level:"ONGELDIGE JSON",event:"onleesbare logregel",runId:"",details:line}}})}function logTimestamp(entry){const value=Date.parse(entry.timestamp);return Number.isFinite(value)?value:0}function renderComponentLogs(){const needle=$("logFilter").value.trim().toLocaleLowerCase("nl-NL"),level=$("logLevelFilter").value,sort=$("logSort").value;for(const component of ["inbox","dashboard"]){const rows=componentLogEntries[component].filter(entry=>!level||entry.level===level).filter(entry=>!needle||Object.values(entry).join(" ").toLocaleLowerCase("nl-NL").includes(needle)).sort((left,right)=>sort==="oldest"?logTimestamp(left)-logTimestamp(right):sort==="level"?left.level.localeCompare(right.level,"nl"):sort==="event"?left.event.localeCompare(right.event,"nl"):logTimestamp(right)-logTimestamp(left)),body=$(component+"ComponentLog");body.replaceChildren();if(!rows.length){const cell=document.createElement("td"),row=document.createElement("tr");cell.className="log-empty";cell.colSpan=6;cell.textContent="Geen logregels voor deze selectie.";row.append(cell);body.append(row);continue}for(const entry of rows){const row=document.createElement("tr");for(const [name,value] of [["log-line-number",entry.line],["",entry.timestamp||"—"],["log-level log-level--"+entry.level.toLocaleLowerCase("nl-NL"),entry.level],["",entry.event],["",entry.runId||"—"],["",entry.details||"—"]]){const cell=document.createElement("td");cell.className=name;cell.textContent=value;row.append(cell)}body.append(row)}}}function loadComponentLogs(){if(componentLogsLoaded)return;$("loadComponentLogs").disabled=true;$("loadComponentLogs").textContent="Logs laden…";Promise.all([fetch("/api/logs/inbox").then(x=>x.text()),fetch("/api/logs/dashboard").then(x=>x.text())]).then(([inbox,dashboard])=>{componentLogEntries.inbox=structuredLogEntries(inbox);componentLogEntries.dashboard=structuredLogEntries(dashboard);componentLogsLoaded=true;$("componentLogControls").hidden=false;renderComponentLogs();$("loadComponentLogs").textContent="Logs geladen"}).catch(()=>{componentLogEntries.inbox=structuredLogEntries('{"level":"ERROR","event":"inbox_log_unavailable","diagnostic":"Inbox-log is niet beschikbaar."}');componentLogEntries.dashboard=structuredLogEntries('{"level":"ERROR","event":"dashboard_log_unavailable","diagnostic":"Dashboard-log is niet beschikbaar."}');$("componentLogControls").hidden=false;renderComponentLogs();$("loadComponentLogs").disabled=false;$("loadComponentLogs").textContent="Opnieuw proberen"})}
const CHAT_HISTORY_KEY="djconnect-engineering-chat-history",CHAT_CONTEXT_KEY="djconnect-engineering-chat-context",CHAT_HISTORY_LIMIT=20;function loadChatHistory(){try{const saved=JSON.parse(sessionStorage.getItem(CHAT_HISTORY_KEY)||"[]");return Array.isArray(saved)?saved.filter(entry=>entry&&["user","assistant"].includes(entry.role)&&typeof entry.text==="string").slice(-CHAT_HISTORY_LIMIT):[]}catch{return []}}let chatHistory=loadChatHistory();function persistChatHistory(){sessionStorage.setItem(CHAT_HISTORY_KEY,JSON.stringify(chatHistory))}function chatMessage(role,text){let item=document.createElement("article"),label=document.createElement("span"),body=document.createElement("div");item.className="chat-message chat-message--"+role;label.className="chat-message__role";label.textContent=role==="user"?"Jij":"Codex";body.className="chat-message__body";body.textContent=text;item.append(label,body);$("chatMessages").append(item);item.scrollIntoView({block:"nearest"})}function renderChatHistory(){const container=$("chatMessages");container.replaceChildren();chatHistory.forEach(entry=>chatMessage(entry.role,entry.text))}function reconcileChatContext(run){if(!latestStatus)return;const context=run||"none";if(sessionStorage.getItem(CHAT_CONTEXT_KEY)===context)return;chatHistory=[];sessionStorage.removeItem(CHAT_HISTORY_KEY);sessionStorage.setItem(CHAT_CONTEXT_KEY,context);renderChatHistory()}function askCodex(){let input=$("chatInput"),message=input.value.trim();if(!message||$("chatSend").disabled)return;$("chatSend").disabled=true;$("chatStatus").textContent="Codex denkt na…";chatHistory.push({role:"user",text:message});chatHistory=chatHistory.slice(-CHAT_HISTORY_LIMIT);persistChatHistory();chatMessage("user",message);input.value="";fetch("/api/codex-chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:message,history:chatHistory.slice(0,-1).slice(-6)})}).then(async response=>({ok:response.ok,body:await response.json()})).then(result=>{if(!result.ok)throw Error(result.body.error||"Codex Gesprek is niet beschikbaar.");let answer=result.body.answer;$("chatModel").textContent=result.body.model||$("chatModel").textContent;chatHistory.push({role:"assistant",text:answer});chatHistory=chatHistory.slice(-CHAT_HISTORY_LIMIT);persistChatHistory();chatMessage("assistant",answer);$("chatStatus").textContent=""}).catch(error=>{$("chatStatus").textContent=error.message}).finally(()=>{$("chatSend").disabled=false})}
function fallbackCopy(value){const area=document.createElement("textarea");area.value=value;area.setAttribute("readonly","");area.style.cssText="position:fixed;top:0;left:0;opacity:0";document.body.append(area);area.focus();area.select();area.setSelectionRange(0,area.value.length);const copied=document.execCommand("copy");area.remove();if(!copied)throw Error("copy unavailable")}
function copyText(value){const copy=navigator.clipboard&&window.isSecureContext?navigator.clipboard.writeText(value).catch(()=>fallbackCopy(value)):Promise.resolve().then(()=>fallbackCopy(value));return copy.then(()=>{showCopyToast()})}
let copyToastTimer;function showCopyToast(){const toast=$("copyToast");if(!toast)return;clearTimeout(copyToastTimer);toast.textContent="Gekopieerd naar klembord";toast.hidden=false;requestAnimationFrame(()=>toast.classList.add("copy-toast--visible"));copyToastTimer=setTimeout(()=>{toast.classList.remove("copy-toast--visible");setTimeout(()=>{toast.hidden=true},180)},2200)}
function copyReport(){report().then(()=>copyText($("reportContent").textContent)).catch(()=>{$("copyReport").textContent="Kopiëren mislukt"})}
function copyReportAnalysis(){analysis().then(()=>copyText($("reportAnalysisContent").textContent)).catch(()=>{$("copyReportAnalysis").textContent="Kopiëren mislukt"})}
function addReportAnalysisCopy(){const card=$("reportAnalysis");if(!card||$("copyReportAnalysis"))return;const button=document.createElement("button");button.className="copy";button.id="copyReportAnalysis";button.type="button";button.title="Kopieer analyse";button.setAttribute("aria-label","Kopieer analyse");button.textContent="⧉ Kopieer";button.addEventListener("click",copyReportAnalysis);card.querySelector("summary").insertAdjacentElement("afterend",button)}addReportAnalysisCopy()
function r(x,snapshot={}){lastRefresh=new Date();clock();x=x&&typeof x==="object"?x:fallback;latestStatus=x;let active=isActiveRun(x),statusTone=tone(x),indicator=$("indicator"),previous=x.last_executed_run||null,lastStatus=finalStatus(x.last_executed_phase),components=snapshot.component_versions||{},blocked=Boolean(x.blocking_predecessor_run);if(previous!==lastExecutedRun){lastExecutedRun=previous;reportLoaded=false;reportRequest=undefined;analysisLoaded=false;analysisRequest=undefined;$("report").open=false;$("reportAnalysis").open=false;$("reportContent").textContent="Open dit blok om het rapport te laden.";$("reportAnalysisContent").textContent="Open dit blok om de analyse te laden."}$("currentRun").hidden=!active;$("promptRuns").hidden=!previous;$("lastExecution").hidden=!previous;$("report").hidden=!previous;$("reportAnalysis").hidden=!previous;$("predecessorGate").hidden=!blocked;$("predecessorRun").textContent=x.blocking_predecessor_run||"Niet beschikbaar";$("predecessorPrompt").textContent=x.blocking_predecessor_title||x.blocking_predecessor_filename||"Niet beschikbaar";$("predecessorPhase").textContent=translate(x.blocking_predecessor_phase||"Niet beschikbaar");$("predecessorAction").textContent=x.predecessor_recovery_action||"Niet beschikbaar";$("executionContext").hidden=!x.execution_mode;$("executionMode").textContent=x.execution_mode||"Niet beschikbaar";$("targetRepository").textContent=x.target_repository||"Niet beschikbaar";$("checkoutPath").textContent=x.checkout_path||"Niet beschikbaar";$("activeBranch").textContent=x.active_branch||"Niet beschikbaar";indicator.className="indicator indicator--"+statusTone+(active?" indicator--running":"");indicator.setAttribute("aria-label","Promptstatus: "+statusTone);$("lastIndicator").className="indicator indicator--small indicator--"+lastStatus[0];$("lastFinalStatus").textContent=lastStatus[1];$("watcher").textContent=translate(x.watcher_state||fallback.watcher_state);$("phase").textContent=translate(x.current_phase||"idle");$("action").textContent=translate(x.current_action||"Geen actieve actie");promptStarted(snapshot.prompt_started);renderEstimate(x);processMetrics(active,snapshot.process_metrics);$("currentPrompt").textContent=x.prompt_title||"Niet beschikbaar";$("currentFile").textContent=x.submitted_filename||"Niet beschikbaar";if(!active||x.run_id!==currentLogRun)$("currentDiagnostic").hidden=true;if(active)l("currentLog","/api/log/current",x.run_id||null,false,"currentDiagnostic");$("lastPrompt").textContent=x.last_executed_title||"Nog geen prompt uitgevoerd";$("lastFile").textContent=x.last_executed_filename||"Niet beschikbaar";$("lastDiagnostic").hidden=lastStatus[0]==="green";if(previous&&lastStatus[0]!=="green")l("lastLog","/api/log/last",previous,true,"lastDiagnostic");$("runId").textContent=x.run_id||"geen";$("queue").textContent=x.queue_depth??0;queueItems(x.queue_items,x.queue_depth);$("implementation").textContent=x.implementation_pr||"geen";$("finalization").textContent=x.finalization_pr||"geen";$("repositoryState").textContent=translate(x.repository_state||"UNKNOWN");$("workspaceState").textContent=translate(x.workspace_state||"UNKNOWN");$("diag").textContent=translate(x.diagnostic||"Geen diagnose");$("platformVersion").textContent=x.platform_version||"Niet beschikbaar";$("dashboardVersion").textContent=components.dashboard||"Niet beschikbaar";$("workerVersion").textContent=components.worker||"Niet beschikbaar";usage(snapshot.usage);rateLimits(snapshot.rate_limits);lastUsage(snapshot.last_executed_usage);commits(snapshot.completion_commits);lastCommits(snapshot.last_executed_commits);reviewerAgents(snapshot.last_executed_reviewer_agents)}
const renderStatus=r;let lastExecutionCategoryRun,activePromptCategoryRun;r=(x,snapshot={})=>{renderStatus(x,snapshot);const active=x&&typeof x==="object"&&isActiveRun(x),current=$("currentRun"),previous=x&&typeof x==="object"?x.last_executed_run||null:null,group=$("lastExecutionGroup");if(active&&current&&x.run_id!==activePromptCategoryRun){activePromptCategoryRun=x.run_id;current.open=false}if(!group)return;group.hidden=!previous;if(previous!==lastExecutionCategoryRun){lastExecutionCategoryRun=previous;group.open=false}}
let e=new EventSource("/api/events");e.addEventListener("dashboard",x=>{if(!$("autoRefresh").checked)return;try{let snapshot=JSON.parse(x.data);r(snapshot.status,snapshot);humanize();checkBuild(snapshot.build_commit);$("updateMode").textContent="Serverpush: verbonden"}catch{r(fallback);humanize();$("updateMode").textContent="Serverpush: update ongeldig"}});e.onerror=()=>{$("autoRefresh").checked&&($("updateMode").textContent="Serverpush: opnieuw verbinden…")};$("report").addEventListener("toggle",()=>{$("report").open&&report()});$("reportAnalysis").addEventListener("toggle",()=>{$("reportAnalysis").open&&analysis()});$("copyReport").addEventListener("click",copyReport);$("loadComponentLogs").addEventListener("click",loadComponentLogs);for(const id of ["logFilter","logLevelFilter","logSort"])$(id).addEventListener("input",renderComponentLogs);$("chatSend").addEventListener("click",askCodex);$("chatInput").addEventListener("keydown",event=>{if(event.key==="Enter"&&(event.metaKey||event.ctrlKey)){event.preventDefault();askCodex()}});renderChatHistory();setInterval(()=>{reconcileChatContext(latestStatus?.last_executed_run);clock()},250);clock()
let logSortState={key:"timestamp",direction:"desc"};function logValue(entry,key){if(key==="line")return Number(entry.line)||0;if(key==="timestamp")return logTimestamp(entry);return String(entry[key]||"").toLocaleLowerCase("nl-NL")}function updateLogSortHeaders(){document.querySelectorAll(".log-table th[data-sort-key]").forEach(header=>{const active=header.dataset.sortKey===logSortState.key;header.dataset.sortIndicator=active?(logSortState.direction==="asc"?"↑":"↓"):"↕";header.setAttribute("aria-sort",active?(logSortState.direction==="asc"?"ascending":"descending"):"none")})}function setLogSort(key){logSortState=logSortState.key===key?{key:key,direction:logSortState.direction==="asc"?"desc":"asc"}:{key:key,direction:key==="timestamp"?"desc":"asc"};updateLogSortHeaders();renderComponentLogs()}function renderComponentLogs(){const needle=$("logFilter").value.trim().toLocaleLowerCase("nl-NL"),level=$("logLevelFilter").value;for(const component of ["inbox","dashboard"]){const rows=componentLogEntries[component].filter(entry=>!level||entry.level===level).filter(entry=>!needle||Object.values(entry).join(" ").toLocaleLowerCase("nl-NL").includes(needle)).sort((left,right)=>{const first=logValue(left,logSortState.key),second=logValue(right,logSortState.key),result=typeof first==="number"&&typeof second==="number"?first-second:String(first).localeCompare(String(second),"nl");return logSortState.direction==="asc"?result:-result}),body=$(component+"ComponentLog");body.replaceChildren();if(!rows.length){const cell=document.createElement("td"),row=document.createElement("tr");cell.className="log-empty";cell.colSpan=6;cell.textContent="Geen logregels voor deze selectie.";row.append(cell);body.append(row);continue}for(const entry of rows){const row=document.createElement("tr");for(const [name,value] of [["log-line-number",entry.line],["",entry.timestamp||"—"],["log-level log-level--"+entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ","-"),entry.level],["",entry.event],["",entry.runId||"—"],["",entry.details||"—"]]){const cell=document.createElement("td");cell.className=name;cell.textContent=value;row.append(cell)}body.append(row)}}}function configureLogSortHeaders(){const keys=["line","timestamp","level","event","runId","details"];document.querySelectorAll(".log-table").forEach(table=>table.querySelectorAll("th").forEach((header,index)=>{const key=keys[index];header.classList.add("log-sortable");header.dataset.sortKey=key;header.tabIndex=0;header.addEventListener("click",()=>setLogSort(key));header.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();setLogSort(key)}})}));updateLogSortHeaders()}function providerNeutralLabels(){const labels=[["#processMetrics>strong","Lokale AI-processen"],["#usage>strong","AI-providergebruik"],["#currentDiagnostic>strong","AI-uitvoeringsdiagnose"],["#rateLimits .label","AI-providerlimieten"],["#lastUsage .label","AI-providergebruik"],["#lastDiagnostic .label","AI-uitvoeringsdiagnose"],["#reportAnalysis summary strong","AI-analyse van rapport"],["#codexChat>strong","AI-gesprek"],["#chatMessages","Gesprek met AI-assistent"],["label[for=chatInput]","Nieuwe vraag aan AI-assistent"]];labels.forEach(([selector,text])=>{const element=document.querySelector(selector);if(element){element.textContent=text;if(selector==="#chatMessages")element.setAttribute("aria-label",text)}})}function chatMessage(role,text){let item=document.createElement("article"),label=document.createElement("span"),body=document.createElement("div");item.className="chat-message chat-message--"+role;label.className="chat-message__role";label.textContent=role==="user"?"Jij":"AI-assistent";body.className="chat-message__body";body.textContent=text;item.append(label,body);$("chatMessages").append(item);item.scrollIntoView({block:"nearest"})}configureLogSortHeaders();providerNeutralLabels();
function groupLastExecution(){const group=$("lastExecutionGroup");if(!group||group.tagName==="DETAILS")return;const category=document.createElement("details"),summary=document.createElement("summary"),title=document.createElement("strong"),content=document.createElement("div");category.id=group.id;category.className=group.className;category.dataset.testid="last-executed-prompt-category";category.hidden=group.hidden;title.textContent="Laatst uitgevoerde prompt";summary.append(title);content.className="last-execution-group__content";while(group.firstChild)content.append(group.firstChild);category.append(summary,content);group.replaceWith(category)}groupLastExecution();
function addCategoryIcons(){for(const [selector,glyph,label] of [["#workspaceCard","⌂","Werkruimte"],["#queueItems","☷","Inbox-wachtrij"],["#promptHistory","◫","Promptgeschiedenis"],["#platformHealth","◈","Platformonderdelen"],["#rateLimits","◔","Resterend gebruik"],["#executionTelemetry","▥","Execution Host-telemetrie"],["#lastExecutionGroup","◷","Laatst uitgevoerde prompt"],["#codexChat","✦","AI-gesprek"],["#technicalDetails","⚙","Technische details"],["#componentLogs","≡","Logs"],["#currentRun","▤","Actieve prompt"]]){const summary=document.querySelector(selector+">summary");if(!summary||summary.querySelector(".category-icon"))continue;const icon=document.createElement("span"),title=summary.querySelector("strong,.label");icon.className="category-icon";icon.setAttribute("aria-hidden","true");icon.textContent=glyph;icon.title=label;if(title)title.before(icon);else summary.prepend(icon)}}addCategoryIcons();
function addCategoryDescriptions(){const descriptions=[[".workspace-card","De actieve werkruimte van dit project."],["#queueItems","Prompts worden uitgevoerd op volgorde van aanmaakdatum."],["#promptHistory","Alle terminale Engineering Platform-uitvoeringen, lokaal gecachet in de Engineering SQLite-opslag."],["#rateLimits","Beschikbare gebruiksruimte en resets van de actieve AI-provider."],["#lastExecutionGroup","De meest recent uitgevoerde prompt, met bewijs, rapport en analyse."],["#componentLogs","Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush."],["#codexChat","Stel korte, alleen-lezen vragen over de laatst uitgevoerde prompt en het bijbehorende rapport. Dit start geen engineering of wijzigingen."],["#engineering-dashboard-content>.technical-details:not(#componentLogs)","Operationele details over pull requests, repository, werkruimte en diagnose."]];for(const [selector,text] of descriptions){const category=document.querySelector(selector),summary=category?.querySelector(":scope>summary");if(!category||!summary)continue;let description=category.querySelector(":scope>.category-description")||category.querySelector(":scope>.estimate-meta");if(!description){description=document.createElement("p");description.textContent=text;summary.insertAdjacentElement("afterend",description)}description.classList.add("category-description")}}addCategoryDescriptions();
function arrangeOperationalCategories(){const technical=$("technicalDetails"),telemetry=$("executionTelemetry"),health=$("platformHealth"),logs=$("componentLogs");if(!technical)return;let anchor=technical;if(telemetry){anchor.insertAdjacentElement("afterend",telemetry);anchor=telemetry}if(health){anchor.insertAdjacentElement("afterend",health);anchor=health}if(logs)anchor.insertAdjacentElement("afterend",logs)}arrangeOperationalCategories();$("rateLimitReset").addEventListener("click",consumeRateLimitReset);
function addTestIds(){const toTestId=value=>"engineering-"+value.replace(/[A-Z]/g,letter=>"-"+letter.toLowerCase());document.querySelector("main")?.setAttribute("data-testid","engineering-dashboard");document.querySelector("h1")?.setAttribute("data-testid","engineering-dashboard-title");document.querySelectorAll("[id]").forEach(element=>{if(!element.dataset.testid)element.dataset.testid=toTestId(element.id)});document.querySelectorAll(".log-table").forEach((table,index)=>table.dataset.testid="engineering-log-table-"+(index+1))}addTestIds();
function applyAccessibility(){const indicator=$("indicator"),chatStatus=$("chatStatus"),messages=$("chatMessages");indicator.setAttribute("role","status");indicator.setAttribute("aria-live","polite");indicator.setAttribute("aria-atomic","true");chatStatus.setAttribute("role","status");chatStatus.setAttribute("aria-live","polite");messages.setAttribute("role","log");messages.setAttribute("aria-relevant","additions text");document.querySelectorAll(".log-table").forEach((table,index)=>{table.setAttribute("aria-label",index===0?"Logregels van Inbox-watcher":"Logregels van Statusdashboard");table.querySelectorAll("th.log-sortable").forEach(header=>{header.setAttribute("role","button");header.setAttribute("aria-label",header.textContent.trim()+" sorteren")})});const live=document.createElement("div");live.className="sr-only";live.id="dashboardStatusAnnouncement";live.setAttribute("role","status");live.setAttribute("aria-live","polite");live.setAttribute("aria-atomic","true");document.body.append(live);let previous="";new MutationObserver(()=>{const message=indicator.getAttribute("aria-label")||"";if(message&&message!==previous){previous=message;live.textContent=message}}).observe(indicator,{attributes:true,attributeFilter:["aria-label"]})}applyAccessibility();
renderChatHistory();
function appendMarkdownInline(target,value){const pattern=/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\*[^*]+\*)/g;let offset=0;for(const token of String(value).matchAll(pattern)){target.append(document.createTextNode(value.slice(offset,token.index)));const text=token[0];if(text.startsWith("**")){const strong=document.createElement("strong");strong.textContent=text.slice(2,-2);target.append(strong)}else if(text.startsWith("`")){const code=document.createElement("code");code.textContent=text.slice(1,-1);target.append(code)}else if(text.startsWith("[")){const match=/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(text),link=document.createElement("a");link.href=match[2];link.rel="noopener noreferrer";link.target="_blank";link.textContent=match[1];target.append(link)}else{const emphasis=document.createElement("em");emphasis.textContent=text.slice(1,-1);target.append(emphasis)}offset=token.index+text.length}target.append(document.createTextNode(value.slice(offset)))}
function renderMarkdownAnswer(target,value){const newline=String.fromCharCode(10);let codeLines=null,list=null,listType="";for(const line of String(value).split(newline)){if(line.startsWith("```")){if(codeLines===null){codeLines=[]}else{const pre=document.createElement("pre"),code=document.createElement("code");code.textContent=codeLines.join(newline);pre.append(code);target.append(pre);codeLines=null}list=null;continue}if(codeLines!==null){codeLines.push(line);continue}const heading=/^(#{1,3})\s+(.+)$/.exec(line),bullet=/^[-*]\s+(.+)$/.exec(line),ordered=/^\d+\.\s+(.+)$/.exec(line);if(heading){const element=document.createElement("h"+Math.min(heading[1].length+2,4));appendMarkdownInline(element,heading[2]);target.append(element);list=null;continue}if(/^ {0,3}([-*_])\1\1+\s*$/.test(line)){target.append(document.createElement("hr"));list=null;continue}if(bullet||ordered){const type=bullet?"ul":"ol";if(!list||listType!==type){list=document.createElement(type);listType=type;target.append(list)}const item=document.createElement("li");appendMarkdownInline(item,(bullet||ordered)[1]);list.append(item);continue}list=null;if(!line.trim())continue;const paragraph=document.createElement("p");appendMarkdownInline(paragraph,line);target.append(paragraph)}if(codeLines!==null){const pre=document.createElement("pre"),code=document.createElement("code");code.textContent=codeLines.join(newline);pre.append(code);target.append(pre)}}
const plainChatMessage=chatMessage;chatMessage=(role,text)=>{if(role!=="assistant"){plainChatMessage(role,text);return}const item=document.createElement("article"),label=document.createElement("span"),body=document.createElement("div");item.className="chat-message chat-message--assistant";label.className="chat-message__role";label.textContent="AI-assistent";body.className="chat-message__body";renderMarkdownAnswer(body,text);item.append(label,body);$("chatMessages").append(item);item.scrollIntoView({block:"nearest"})};renderChatHistory();
function addChatMessageCopyButton(item,text){if(!item||item.querySelector(".chat-message__copy"))return;const button=document.createElement("button");button.className="chat-message__copy";button.type="button";button.title="Kopieer bericht";button.setAttribute("aria-label","Kopieer bericht");button.textContent="⧉";button.addEventListener("click",()=>{copyText(String(text)).then(()=>void recordUserAction("chat_message_copied")).catch(()=>{button.title="Kopiëren mislukt"})});item.append(button)}const chatMessageWithCopy=chatMessage;chatMessage=(role,text)=>{chatMessageWithCopy(role,text);addChatMessageCopyButton($("chatMessages").lastElementChild,text)};renderChatHistory();
$("chatSend").querySelector("span").textContent="↑";
let componentLogVersion="";function refreshComponentLogs(versions={}){const version=JSON.stringify(versions);if(componentLogsLoaded&&version===componentLogVersion)return;componentLogVersion=version;Promise.all([fetch("/api/logs/inbox").then(response=>response.text()),fetch("/api/logs/dashboard").then(response=>response.text())]).then(([inbox,dashboard])=>{componentLogEntries.inbox=structuredLogEntries(inbox);componentLogEntries.dashboard=structuredLogEntries(dashboard);componentLogsLoaded=true;$("componentLogControls").hidden=false;renderComponentLogs()}).catch(()=>{componentLogEntries.inbox=structuredLogEntries('{"level":"ERROR","event":"inbox_log_unavailable","diagnostic":"Inbox-log is niet beschikbaar."}');componentLogEntries.dashboard=structuredLogEntries('{"level":"ERROR","event":"dashboard_log_unavailable","diagnostic":"Dashboard-log is niet beschikbaar."}');$("componentLogControls").hidden=false;renderComponentLogs()})}function enableLiveComponentLogs(){const button=$("loadComponentLogs"),description=document.querySelector("#componentLogs .estimate-meta");button?.remove();if(description)description.textContent="Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush.";$("componentLogControls").hidden=false;refreshComponentLogs()}const renderStatusWithLiveComponentLogs=r;r=(x,snapshot={})=>{renderStatusWithLiveComponentLogs(x,snapshot);refreshComponentLogs(snapshot.component_log_versions||{})};enableLiveComponentLogs();
const healthComponentLabels={dashboard:"Statusdashboard",inbox_watcher:"Inbox-watcher",dashboard_relay:"Dashboardrelay",private_remote_access:"Privé externe toegang"};let healthRequestInFlight=false;const componentDetailsRefreshIntervalMs=5000;let activeComponentDetails=null,componentDetailsRefreshTimer=null,componentDetailsRefreshInFlight=false;function healthIndicatorClass(healthy){return healthy?"indicator indicator--green":"indicator indicator--red"}function formatComponentUptime(value){const seconds=Number(value);if(!Number.isFinite(seconds)||seconds<0)return"";const total=Math.round(seconds),days=Math.floor(total/86400),hours=Math.floor(total%86400/3600),minutes=Math.floor(total%3600/60);return days?days+"d "+hours+"u":hours?hours+"u "+minutes+"m":minutes?minutes+"m":total+"s"}function componentDetailField(list,label,value){if(value===null||value===undefined||value==="")return;const term=document.createElement("dt"),description=document.createElement("dd"),entry=document.createElement("div");term.textContent=label;description.textContent=String(value);entry.append(term,description);list.append(entry)}function componentMemory(processes){if(!Array.isArray(processes)||!processes.length)return"Geen lokaal proces gevonden";return processes.map(process=>"PID "+process.pid+": "+(Number(process.memory_kib||0)/1024).toFixed(1)+" MiB").join(" · ")}function showComponentModal(payload){const modal=$("componentModal"),content=$("componentModalContent"),title=$("componentModalTitle"),restart=$("componentModalRestart"),status=$("componentModalStatus"),launchd=payload.launchd||{},isExternalAccess=payload.component==="private_remote_access";title.textContent=healthComponentLabels[payload.component]||"Componentinformatie";content.replaceChildren();const fields=document.createElement("dl");componentDetailField(fields,"Machine",payload.machine);componentDetailField(fields,"Status",(payload.healthy?"Gezond":"Niet gezond")+" · "+(payload.detail||payload.state||"Geen toelichting"));componentDetailField(fields,"Versie",payload.version);componentDetailField(fields,"Uptime",formatComponentUptime(payload.uptime_seconds));if(!isExternalAccess)componentDetailField(fields,"Git-commit",payload.git_commit);componentDetailField(fields,"Uitvoerbaar pad",Array.isArray(launchd.program_arguments)&&launchd.program_arguments.length?launchd.program_arguments[0]:payload.executable_path);componentDetailField(fields,"Launchd-label",launchd.label);componentDetailField(fields,"LaunchAgent",launchd.plist_path);componentDetailField(fields,"Launchd-instellingen",launchd.label?(launchd.loaded?"Geladen":"Niet geladen")+" · Start bij laden: "+(launchd.run_at_load?"ja":"nee")+" · Blijf actief: "+(launchd.keep_alive?"ja":"nee"):null);if(!isExternalAccess)componentDetailField(fields,"Huidig geheugen",componentMemory(payload.processes));content.append(fields);restart.hidden=!payload.restart_supported;restart.dataset.component=payload.component;if(!modal.open){status.textContent="";modal.showModal()}}async function requestComponentDetails(component,showError=true){try{const response=await fetch("/api/components/"+encodeURIComponent(component)+"/details",{cache:"no-store"}),payload=await response.json();if(!response.ok)throw Error(payload.error||"Componentinformatie is niet beschikbaar.");showComponentModal(payload);return true}catch(error){if(showError)window.alert(error.message||"Componentinformatie is niet beschikbaar.");return false}}function stopComponentDetailsRefresh(){if(componentDetailsRefreshTimer!==null){window.clearInterval(componentDetailsRefreshTimer);componentDetailsRefreshTimer=null}activeComponentDetails=null}function startComponentDetailsRefresh(component){stopComponentDetailsRefresh();activeComponentDetails=component;componentDetailsRefreshTimer=window.setInterval(()=>void refreshOpenComponentDetails(),componentDetailsRefreshIntervalMs)}async function refreshOpenComponentDetails(){const modal=$("componentModal");if(!activeComponentDetails||!modal.open||componentDetailsRefreshInFlight)return;componentDetailsRefreshInFlight=true;try{await requestComponentDetails(activeComponentDetails,false)}finally{componentDetailsRefreshInFlight=false}}async function showComponentDetails(component){const shown=await requestComponentDetails(component);if(shown)startComponentDetailsRefresh(component)}async function restartDashboardComponent(){const restart=$("componentModalRestart"),component=restart.dataset.component;if(!component)return;if(!window.confirm("Weet je zeker dat je dit Engineering Platform-onderdeel wilt herstarten?"))return;restart.disabled=true;try{const response=await fetch("/api/components/"+encodeURIComponent(component)+"/restart",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}),payload=await response.json();if(!response.ok)throw Error(payload.error||"Herstarten is niet gelukt.");$("componentModalStatus").textContent="Herstartverzoek verzonden. De component komt zo opnieuw beschikbaar."}catch(error){$("componentModalStatus").textContent=error.message||"Herstarten is niet gelukt."}finally{restart.disabled=false}}$("componentModalClose").addEventListener("click",()=>$("componentModal").close());$("componentModal").addEventListener("click",event=>{if(event.target===$("componentModal"))$("componentModal").close()});$("componentModal").addEventListener("close",stopComponentDetailsRefresh);$("componentModalRestart").addEventListener("click",restartDashboardComponent);function renderPlatformHealth(payload){const container=$("platformHealthComponents");if(!container)return;const components=payload&&typeof payload.components==="object"?payload.components:null;container.replaceChildren();if(!components){const message=document.createElement("p");message.className="platform-health__empty";message.textContent="De live gezondheidscontrole is tijdelijk niet beschikbaar.";container.append(message);return}for(const [key,component] of Object.entries(components)){const item=document.createElement("article"),indicator=document.createElement("span"),name=document.createElement("span"),detail=document.createElement("span"),info=document.createElement("button"),componentHealthy=Boolean(component?.healthy),version=typeof component?.version==="string"?" · Versie "+component.version:"",uptime=formatComponentUptime(component?.uptime_seconds);item.className="platform-health__component";item.dataset.health=String(componentHealthy);indicator.className=healthIndicatorClass(componentHealthy);indicator.setAttribute("aria-hidden","true");name.className="platform-health__component-name";name.textContent=healthComponentLabels[key]||key;detail.className="platform-health__component-detail";detail.textContent=(componentHealthy?"Gezond":"Niet gezond")+" · "+String(component?.detail||component?.state||"Geen toelichting")+version+(uptime?" · Uptime "+uptime:"");info.className="component-info";info.type="button";info.textContent="i";info.title="Meer informatie over "+name.textContent;info.setAttribute("aria-label",info.title);info.addEventListener("click",()=>showComponentDetails(key));item.append(indicator,name,detail,info);container.append(item)}}async function refreshPlatformHealth(){if(healthRequestInFlight)return;healthRequestInFlight=true;try{const response=await fetch("/health",{cache:"no-store"}),payload=await response.json();renderPlatformHealth(payload)}catch{renderPlatformHealth(null)}finally{healthRequestInFlight=false}}refreshPlatformHealth();window.setInterval(refreshPlatformHealth,15000);
function flattenMarkdownPanels(){for(const [panelId,contentId] of [["report","reportContent"],["reportAnalysis","reportAnalysisContent"]]){const panel=$(panelId),content=$(contentId),field=content?.closest(".field");if(panel&&field&&field.parentElement===panel)field.replaceWith(content)}}flattenMarkdownPanels();
function compactCopyButton(buttonId,contentId){const button=$(buttonId),content=$(contentId);if(!button||!content)return;let wrapper=content.parentElement;if(!wrapper.classList.contains("markdown-copy-wrap")){wrapper=document.createElement("div");wrapper.className="markdown-copy-wrap";content.replaceWith(wrapper);wrapper.append(content)}button.classList.add("copy--glyph");button.textContent="⧉";wrapper.append(button)}function compactReportCopyButtons(){compactCopyButton("copyReport","reportContent");compactCopyButton("copyReportAnalysis","reportAnalysisContent")}compactReportCopyButtons();
function downloadLastExecutedDocument(endpoint,filenamePrefix){if(!lastExecutedRun)return Promise.reject(Error("Geen uitgevoerde prompt beschikbaar."));return fetch(endpoint+"?run_id="+encodeURIComponent(lastExecutedRun)).then(response=>response.ok?response.text():Promise.reject(Error("Download is niet beschikbaar."))).then(text=>{if(!text)throw Error("Download is niet beschikbaar.");const link=document.createElement("a"),url=URL.createObjectURL(new Blob([text],{type:"text/markdown;charset=utf-8"})),safeRun=String(lastExecutedRun).replace(/[^a-z0-9._-]+/gi,"-");link.href=url;link.download=filenamePrefix+"-"+safeRun+".md";link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)})}function addDownloadButton(panelId,contentId,buttonId,filenamePrefix,label){const panel=$(panelId),content=$(contentId);if(!panel||!content||$(buttonId))return;const button=document.createElement("button");button.className="download download--glyph";button.id=buttonId;button.type="button";button.title=label;button.setAttribute("aria-label",label);button.textContent="⇩";button.hidden=true;button.addEventListener("click",()=>downloadLastExecutedDocument(panelId==="report"?"/api/report/last-executed":"/api/report-analysis/last-executed",filenamePrefix).catch(()=>{button.title="Download is niet beschikbaar."}));const wrapper=content.parentElement;button.classList.add("download--glyph");wrapper.append(button)}addDownloadButton("report","reportContent","downloadReport","engineering-report","Download rapport");addDownloadButton("reportAnalysis","reportAnalysisContent","downloadReportAnalysis","ai-analyse","Download AI-analyse");const originalCopyAvailable=copyAvailable;copyAvailable=(id,available)=>{originalCopyAvailable(id,available);const downloads={copyReport:"downloadReport",copyReportAnalysis:"downloadReportAnalysis"};if(downloads[id])originalCopyAvailable(downloads[id],available)};
function placeFinalStatusIndicator(){const indicator=$("lastIndicator"),status=$("lastFinalStatus");if(indicator&&status)status.before(indicator)}placeFinalStatusIndicator();
function copyAvailable(id,available){const button=$(id);if(button)button.hidden=!available}function updateCopyAvailability(){const unavailable=value=>!value||value.startsWith("Open dit blok")||value.includes("is niet beschikbaar.")||value.startsWith("Er is geen AI-analyse");copyAvailable("copyReport",!unavailable($("reportContent")?.textContent?.trim()));copyAvailable("copyReportAnalysis",!unavailable($("reportAnalysisContent")?.textContent?.trim()))}copyAvailable("copyReport",false);copyAvailable("copyReportAnalysis",false);const reportWithCopyAvailability=report;report=()=>reportWithCopyAvailability().then(value=>{updateCopyAvailability();return value});const analysisWithCopyAvailability=analysis;analysis=()=>analysisWithCopyAvailability().then(value=>{updateCopyAvailability();return value});const renderStatusWithCopyAvailability=r;let copyAvailabilityRun,displayedAnalysisAvailable;r=(x,snapshot={})=>{renderStatusWithCopyAvailability(x,snapshot);const run=x&&typeof x==="object"?x.last_executed_run||null:null;if(Object.hasOwn(snapshot,"last_executed_report_analysis_available")){displayedAnalysisAvailable=Boolean(snapshot.last_executed_report_analysis_available);if(displayedAnalysisAvailable===false&&run){$("reportAnalysisContent").textContent="Er is geen AI-analyse beschikbaar voor deze uitgevoerde prompt.";copyAvailable("copyReportAnalysis",false)}}if(run!==copyAvailabilityRun){copyAvailabilityRun=run;copyAvailable("copyReport",false);copyAvailable("copyReportAnalysis",false)}};const analysisWithAvailability=analysis;analysis=()=>{if(displayedAnalysisAvailable===false)return Promise.resolve();return analysisWithAvailability()};
function arrangeCurrentRunCategory(){const current=$("currentRun"),summary=current?.querySelector(":scope>summary"),prompt=$("currentPrompt"),indicator=$("indicator");if(!current||!summary||!prompt||!indicator)return;let heading=summary.querySelector(".current-run__prompt-heading");if(!heading){heading=document.createElement("div");heading.className="current-run__prompt-heading";prompt.replaceWith(heading);heading.append(prompt)}heading.append(indicator);let description=current.querySelector(":scope>.current-run__category-description");if(!description){description=document.createElement("p");description.className="current-run__category-description";description.textContent="De actieve engineeringprompt, met actuele voortgang, uitvoeringstijd en uitvoeringscontext.";summary.insertAdjacentElement("afterend",description)}}arrangeCurrentRunCategory();
function durationText(seconds){if(!Number.isFinite(seconds)||seconds<0)return"—";const hours=Math.floor(seconds/3600),minutes=Math.floor((seconds%3600)/60),remaining=Math.round(seconds%60);return(hours?hours+" u ":"")+(minutes?minutes+" min ":"")+remaining+" sec"}function executionTimeField(id,label,after){let field=$(id),value=$(id+"Value");if(!field){field=document.createElement("div");value=document.createElement("span");const fieldLabel=document.createElement("span");field.className="field";field.id=id;value.id=id+"Value";fieldLabel.className="label";fieldLabel.textContent=label;field.append(fieldLabel,value);after.insertAdjacentElement("afterend",field)}return[field,value]}function lastExecutionTime(x){const agent=Number(x?.seconds),total=Number(x?.total_seconds),finishedAt=Date.parse(x?.finished_at||""),file=$("lastFile").closest(".field"),[finishedField,finishedValue]=executionTimeField("lastExecutionFinishedAt","Uitgevoerd op",file),[agentField,agentValue]=executionTimeField("lastExecutionTime","Codex CLI-uitvoeringstijd",finishedField),[totalField,totalValue]=executionTimeField("lastTotalExecutionTime","Totale doorlooptijd",agentField);finishedField.hidden=!Number.isFinite(finishedAt);agentField.hidden=!Number.isFinite(agent)||agent<0;totalField.hidden=!Number.isFinite(total)||total<0;if(!finishedField.hidden)finishedValue.textContent=formatTime.format(new Date(finishedAt));if(!agentField.hidden)agentValue.textContent=durationText(agent);if(!totalField.hidden)totalValue.textContent=durationText(total)}
function executionTelemetry(rows){let panel=$("executionTelemetry"),body=$("executionTelemetryRows");if(!panel){panel=document.createElement("details");panel.id="executionTelemetry";panel.className="telemetry";const summary=document.createElement("summary"),title=document.createElement("strong"),description=document.createElement("p"),scroll=document.createElement("div"),table=document.createElement("table"),head=document.createElement("thead"),headRow=document.createElement("tr"),tableBody=document.createElement("tbody");title.textContent="Execution Host-telemetrie";summary.append(title);description.className="category-description";description.textContent="Operationele trends van de laatste zeven dagen. Telemetrie is geen repositorybewijs.";scroll.className="telemetry-scroll";table.className="telemetry-table";table.setAttribute("aria-label","Dagelijkse Execution Host-telemetrie");for(const label of ["Dag","Prompts","Gem. AI-tijd","Gem. totaal","Gem. wachttijd","Input","Output","Totaal","Voltooid","Geblokkeerd","Mislukt"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;headRow.append(cell)}head.append(headRow);tableBody.id="executionTelemetryRows";table.append(head,tableBody);scroll.append(table);panel.append(summary,description,scroll);const rate=$("rateLimits");rate?.insertAdjacentElement("afterend",panel);body=tableBody}body.replaceChildren();for(const row of Array.isArray(rows)?rows:[]){if(!row||typeof row!=="object")continue;const line=document.createElement("tr");for(const value of [row.date,row.prompt_count,durationText(row.average_execution_seconds),durationText(row.average_total_execution_seconds),durationText(row.average_queue_wait_seconds),row.input_tokens??"—",row.output_tokens??"—",row.total_tokens??"—",row.complete_count,row.blocked_count,row.failed_count]){const cell=document.createElement("td");cell.textContent=String(value??"—");line.append(cell)}body.append(line)}if(!body.children.length){const line=document.createElement("tr"),cell=document.createElement("td");cell.colSpan=11;cell.className="telemetry-empty";cell.textContent="Nog geen voltooide Execution Host-telemetrie beschikbaar.";line.append(cell);body.append(line)}}
function telemetryDuration(seconds){if(typeof seconds!=="number"||seconds<0)return"—";const minutes=Math.floor(seconds/60),remaining=Math.round(seconds%60);return(minutes?minutes+" min ":"")+remaining+" sec"}function executionTelemetry(rows){let panel=$("executionTelemetry"),body=$("executionTelemetryRows");if(!panel){panel=document.createElement("details");panel.id="executionTelemetry";panel.className="telemetry";const summary=document.createElement("summary"),title=document.createElement("strong"),description=document.createElement("p"),scroll=document.createElement("div"),table=document.createElement("table"),head=document.createElement("thead"),headRow=document.createElement("tr"),tableBody=document.createElement("tbody");title.textContent="Execution Host-telemetrie";summary.append(title);description.className="category-description";description.textContent="Operationele trends van de laatste zeven dagen. Telemetrie is geen repositorybewijs.";scroll.className="telemetry-scroll";table.className="telemetry-table";table.setAttribute("aria-label","Dagelijkse Execution Host-telemetrie");for(const label of ["Dag","Prompts","Gem. uitvoering","Gem. wachttijd","Input","Output","Totaal","Voltooid","Geblokkeerd","Mislukt"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;headRow.append(cell)}head.append(headRow);tableBody.id="executionTelemetryRows";table.append(head,tableBody);scroll.append(table);panel.append(summary,description,scroll);const rate=$("rateLimits");rate?.insertAdjacentElement("afterend",panel);body=tableBody}body.replaceChildren();for(const row of Array.isArray(rows)?rows:[]){if(!row||typeof row!=="object")continue;const line=document.createElement("tr");for(const value of [row.date,row.prompt_count,telemetryDuration(row.average_execution_seconds),telemetryDuration(row.average_queue_wait_seconds),row.input_tokens??"—",row.output_tokens??"—",row.total_tokens??"—",row.complete_count,row.blocked_count,row.failed_count]){const cell=document.createElement("td");cell.textContent=String(value??"—");line.append(cell)}body.append(line)}if(!body.children.length){const line=document.createElement("tr"),cell=document.createElement("td");cell.colSpan=10;cell.className="telemetry-empty";cell.textContent="Nog geen voltooide Execution Host-telemetrie beschikbaar.";line.append(cell);body.append(line)}}
const renderTelemetryInOrder=executionTelemetry;executionTelemetry=rows=>{renderTelemetryInOrder(rows);arrangeOperationalCategories();addCategoryIcons()};function updateFavicon(){$("dashboardFavicon").href="/assets/engineering-status-icon.svg"}const renderStatusWithFavicon=r;r=(x,snapshot={})=>{renderStatusWithFavicon(x,snapshot);updateFavicon();executionTelemetry(snapshot.telemetry)};updateFavicon();
const renderStatusWithExecutionEvidence=r;r=(x,snapshot={})=>{renderStatusWithExecutionEvidence(x,snapshot);lastExecutionTime(snapshot.last_executed_execution);lastRuntimeMetadata(snapshot.last_executed_runtime_metadata);reviewerAgents(snapshot.last_executed_reviewer_agents)};
const independentLogSortStates={inbox:{key:"timestamp",direction:"desc"},dashboard:{key:"timestamp",direction:"desc"}};function logComponentForTable(table){return table.querySelector("#inboxComponentLog")?"inbox":"dashboard"}function updateIndependentLogSortHeaders(){document.querySelectorAll(".log-table").forEach(table=>{const state=independentLogSortStates[logComponentForTable(table)];table.querySelectorAll("th[data-sort-key]").forEach(header=>{const active=header.dataset.sortKey===state.key;header.dataset.sortIndicator=active?(state.direction==="asc"?"↑":"↓"):"↕";header.setAttribute("aria-sort",active?(state.direction==="asc"?"ascending":"descending"):"none")})})}function renderComponentLogs(){const needle=$("logFilter").value.trim().toLocaleLowerCase("nl-NL"),level=$("logLevelFilter").value;for(const component of ["inbox","dashboard"]){const state=independentLogSortStates[component],rows=componentLogEntries[component].filter(entry=>!level||entry.level===level).filter(entry=>!needle||Object.values(entry).join(" ").toLocaleLowerCase("nl-NL").includes(needle)).sort((left,right)=>{const first=logValue(left,state.key),second=logValue(right,state.key),result=typeof first==="number"&&typeof second==="number"?first-second:String(first).localeCompare(String(second),"nl");return state.direction==="asc"?result:-result}),body=$(component+"ComponentLog");body.replaceChildren();if(!rows.length){const cell=document.createElement("td"),row=document.createElement("tr");cell.className="log-empty";cell.colSpan=6;cell.textContent="Geen logregels voor deze selectie.";row.append(cell);body.append(row);continue}for(const entry of rows){const row=document.createElement("tr");for(const [name,value] of [["log-line-number",entry.line],["",entry.timestamp||"—"],["log-level log-level--"+entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ","-"),entry.level],["",entry.event],["",entry.runId||"—"],["",entry.details||"—"]]){const cell=document.createElement("td");cell.className=name;cell.textContent=value;row.append(cell)}body.append(row)}}updateIndependentLogSortHeaders()}function setIndependentLogSort(component,key){const state=independentLogSortStates[component];independentLogSortStates[component]=state.key===key?{key:key,direction:state.direction==="asc"?"desc":"asc"}:{key:key,direction:key==="timestamp"?"desc":"asc"};renderComponentLogs()}document.querySelectorAll(".log-table").forEach(table=>{const component=logComponentForTable(table);table.querySelectorAll("th[data-sort-key]").forEach(header=>{const key=header.dataset.sortKey;header.addEventListener("click",()=>setIndependentLogSort(component,key));header.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();setIndependentLogSort(component,key)}})})});updateIndependentLogSortHeaders();
const LOG_PAGE_SIZE=50,independentLogPageStates={inbox:1,dashboard:1};function filteredComponentLogEntries(component){const needle=$("logFilter").value.trim().toLocaleLowerCase("nl-NL"),level=$("logLevelFilter").value,state=independentLogSortStates[component];return componentLogEntries[component].filter(entry=>!level||entry.level===level).filter(entry=>!needle||Object.values(entry).join(" ").toLocaleLowerCase("nl-NL").includes(needle)).sort((left,right)=>{const first=logValue(left,state.key),second=logValue(right,state.key),result=typeof first==="number"&&typeof second==="number"?first-second:String(first).localeCompare(String(second),"nl");return state.direction==="asc"?result:-result})}function renderLogPagination(component,total,pageCount){const navigation=$(component+"LogPagination");navigation.replaceChildren();const summary=document.createElement("span"),previous=document.createElement("button"),next=document.createElement("button"),page=Math.min(Math.max(1,independentLogPageStates[component]),pageCount||1);independentLogPageStates[component]=page;summary.className="log-pagination__summary";summary.textContent=total?"Pagina "+page+" van "+pageCount+" · "+total+" regels":"Geen logregels";previous.type=next.type="button";previous.textContent="Vorige";next.textContent="Volgende";previous.disabled=page<=1;next.disabled=page>=pageCount;previous.addEventListener("click",()=>{independentLogPageStates[component]=page-1;renderComponentLogs()});next.addEventListener("click",()=>{independentLogPageStates[component]=page+1;renderComponentLogs()});navigation.append(summary,previous,next)}function renderPaginatedComponentLogs(){for(const component of ["inbox","dashboard"]){const rows=filteredComponentLogEntries(component),body=$(component+"ComponentLog"),pageCount=Math.max(1,Math.ceil(rows.length/LOG_PAGE_SIZE)),page=Math.min(Math.max(1,independentLogPageStates[component]),pageCount),visible=rows.slice((page-1)*LOG_PAGE_SIZE,page*LOG_PAGE_SIZE);independentLogPageStates[component]=page;body.replaceChildren();if(!visible.length){const cell=document.createElement("td"),row=document.createElement("tr");cell.className="log-empty";cell.colSpan=6;cell.textContent="Geen logregels voor deze selectie.";row.append(cell);body.append(row)}else for(const entry of visible){const row=document.createElement("tr");for(const [name,value] of [["log-line-number",entry.line],["",entry.timestamp||"—"],["log-level log-level--"+entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ","-"),entry.level],["",entry.event],["",entry.runId||"—"],["",entry.details||"—"]]){const cell=document.createElement("td");cell.className=name;cell.textContent=value;row.append(cell)}body.append(row)}renderLogPagination(component,rows.length,pageCount)}updateIndependentLogSortHeaders()}const setIndependentLogSortWithPagination=setIndependentLogSort;setIndependentLogSort=(component,key)=>{independentLogPageStates[component]=1;setIndependentLogSortWithPagination(component,key);renderPaginatedComponentLogs()};const renderComponentLogsWithPagination=renderComponentLogs;renderComponentLogs=()=>renderPaginatedComponentLogs();$("logFilter").addEventListener("input",()=>{independentLogPageStates.inbox=independentLogPageStates.dashboard=1;renderComponentLogs()});$("logLevelFilter").addEventListener("change",()=>{independentLogPageStates.inbox=independentLogPageStates.dashboard=1;renderComponentLogs()});renderComponentLogs();
async function clearComponentLog(component,button){const name=component==="inbox"?"Inbox-watcher":"Statusdashboard";if(!window.confirm("Weet je zeker dat je de applicatielogs van "+name+" wilt wissen? Dit kan niet ongedaan worden gemaakt."))return;button.disabled=true;try{const response=await fetch("/api/logs/"+encodeURIComponent(component),{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});if(!response.ok){const payload=await response.json().catch(()=>({}));throw Error(payload.error||"Logs wissen is niet gelukt.")}componentLogEntries[component]=structuredLogEntries(await fetch("/api/logs/"+encodeURIComponent(component)).then(response=>response.text()));componentLogVersion="";renderComponentLogs()}catch(error){window.alert(error.message||"Logs wissen is niet gelukt.")}finally{button.disabled=false}}
document.querySelectorAll(".clear-component-log").forEach(button=>button.addEventListener("click",()=>clearComponentLog(button.dataset.component,button)));
function downloadComponentLog(component){const names={inbox:"inbox-watcher",dashboard:"statusdashboard"},name=names[component];if(!name)return Promise.reject(Error("Onbekend logonderdeel."));return fetch("/api/logs/"+encodeURIComponent(component),{cache:"no-store"}).then(response=>response.ok?response.text():Promise.reject(Error("Logdownload is niet beschikbaar."))).then(text=>{const stamp=new Date().toISOString().replace(/[:.]/g,"-"),link=document.createElement("a"),url=URL.createObjectURL(new Blob([text],{type:"application/x-ndjson;charset=utf-8"}));link.href=url;link.download=name+"-log-"+stamp+".ndjson";link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0);return recordUserAction("component_log_downloaded")})}
document.querySelectorAll(".component-log-download").forEach(button=>button.addEventListener("click",()=>downloadComponentLog(button.dataset.component).catch(()=>{button.title="Logdownload is niet beschikbaar."})));
document.querySelectorAll(".clear-component-log").forEach(button=>{button.classList.add("clear-component-log--glyph");button.textContent="♲";button.title="Wis log";button.setAttribute("aria-label","Wis log")});
let pullRefreshStart=null,pullRefreshDistance=0;const pullRefresh=$("pullRefresh");function updatePullRefresh(distance){pullRefreshDistance=Math.max(0,Math.min(distance,112));const ready=pullRefreshDistance>=72;pullRefresh.classList.toggle("pull-refresh--visible",pullRefreshDistance>8);pullRefresh.textContent=ready?"Laat los om te vernieuwen":"Trek omlaag om te vernieuwen";pullRefresh.setAttribute("aria-hidden",String(pullRefreshDistance<=8))}function startPullRefresh(event){if(window.scrollY>0||event.touches.length!==1)return;const target=event.target;if(target instanceof Element&&target.closest("input,textarea,select,button,[contenteditable=true]"))return;pullRefreshStart=event.touches[0].clientY;pullRefreshDistance=0}function movePullRefresh(event){if(pullRefreshStart===null||event.touches.length!==1)return;const distance=event.touches[0].clientY-pullRefreshStart;if(distance<=0){updatePullRefresh(0);return}event.preventDefault();updatePullRefresh(distance)}function endPullRefresh(){const refresh=pullRefreshDistance>=72;pullRefreshStart=null;updatePullRefresh(0);if(refresh){pullRefresh.textContent="Dashboard vernieuwen…";pullRefresh.classList.add("pull-refresh--visible");pullRefresh.setAttribute("aria-hidden","false");window.location.reload()}}document.addEventListener("touchstart",startPullRefresh,{passive:true});document.addEventListener("touchmove",movePullRefresh,{passive:false});document.addEventListener("touchend",endPullRefresh,{passive:true});document.addEventListener("touchcancel",endPullRefresh,{passive:true});
function hideDashboardSplash(){const splash=$("dashboardSplash");if(!splash||document.body.classList.contains("dashboard-ready"))return;document.body.classList.add("dashboard-ready");splash.setAttribute("aria-hidden","true");setTimeout(()=>{splash.hidden=true},260)}const renderStatusWithSplash=r;r=(x,snapshot={})=>{renderStatusWithSplash(x,snapshot);hideDashboardSplash()};setTimeout(hideDashboardSplash,8000);
const PROMPT_HISTORY_PAGE_SIZE=25;let promptHistoryEntries=[],promptHistoryPage=1,promptHistorySort={key:"executed_at",direction:"desc"};function promptHistoryValue(entry,key){const value=entry?.[key];if(key==="executed_at"){const parsed=Date.parse(String(value||""));return Number.isFinite(parsed)?parsed:0}return String(value||"")}function filteredPromptHistory(){const needle=$("promptHistoryFilter").value.trim().toLocaleLowerCase("nl-NL");return promptHistoryEntries.filter(entry=>!needle||Object.values(entry).join(" ").toLocaleLowerCase("nl-NL").includes(needle)).sort((left,right)=>{const first=promptHistoryValue(left,promptHistorySort.key),second=promptHistoryValue(right,promptHistorySort.key),result=typeof first==="number"&&typeof second==="number"?first-second:String(first).localeCompare(String(second),"nl");return promptHistorySort.direction==="asc"?result:-result})}function promptHistoryStatus(value){return {COMPLETE:"Voltooid",BLOCKED:"Geblokkeerd",FAILED:"Mislukt"}[value]||"Onbekend"}function updatePromptHistoryHeaders(){document.querySelectorAll("#promptHistory th[data-history-sort-key]").forEach(header=>{const active=header.dataset.historySortKey===promptHistorySort.key;header.classList.add("log-sortable");header.tabIndex=0;header.setAttribute("role","button");header.setAttribute("aria-sort",active?(promptHistorySort.direction==="asc"?"ascending":"descending"):"none");header.dataset.sortIndicator=active?(promptHistorySort.direction==="asc"?"↑":"↓"):"↕"})}function renderPromptHistory(){const rows=filteredPromptHistory(),body=$("promptHistoryRows"),navigation=$("promptHistoryPagination"),pageCount=Math.max(1,Math.ceil(rows.length/PROMPT_HISTORY_PAGE_SIZE));promptHistoryPage=Math.min(Math.max(1,promptHistoryPage),pageCount);body.replaceChildren();const visible=rows.slice((promptHistoryPage-1)*PROMPT_HISTORY_PAGE_SIZE,promptHistoryPage*PROMPT_HISTORY_PAGE_SIZE);if(!visible.length){const row=document.createElement("tr"),cell=document.createElement("td");cell.className="log-empty";cell.colSpan=5;cell.textContent="Geen prompts in de geschiedenis voor deze selectie.";row.append(cell);body.append(row)}else for(const entry of visible){const row=document.createElement("tr"),status=document.createElement("td"),title=document.createElement("td"),executed=document.createElement("td"),commit=document.createElement("td"),report=document.createElement("td"),timestamp=Date.parse(String(entry.executed_at||""));status.className="prompt-history-status prompt-history-status--"+String(entry.status||"").toLocaleLowerCase("nl-NL");status.textContent=promptHistoryStatus(entry.status);title.textContent=String(entry.title||entry.run_id||"Prompttitel niet beschikbaar");executed.textContent=Number.isFinite(timestamp)?formatTime.format(new Date(timestamp)):String(entry.executed_at||"Tijdstip niet beschikbaar");commit.textContent=entry.git_commit||"—";if(entry.report_available&&entry.run_id){const link=document.createElement("a");link.className="prompt-history-report";link.href="/api/prompt-history/"+encodeURIComponent(entry.run_id)+"/report";link.download="engineering-report-"+entry.run_id+".md";link.title="Download engineeringrapport";link.setAttribute("aria-label","Download engineeringrapport voor "+title.textContent);link.textContent="⇩";report.append(link)}else report.textContent="—";row.append(status,title,executed,commit,report);body.append(row)}navigation.replaceChildren();const summary=document.createElement("span"),previous=document.createElement("button"),next=document.createElement("button");summary.className="log-pagination__summary";summary.textContent=rows.length?"Pagina "+promptHistoryPage+" van "+pageCount+" · "+rows.length+" prompts":"Geen prompts";previous.type=next.type="button";previous.textContent="Vorige";next.textContent="Volgende";previous.disabled=promptHistoryPage<=1;next.disabled=promptHistoryPage>=pageCount;previous.addEventListener("click",()=>{promptHistoryPage--;renderPromptHistory()});next.addEventListener("click",()=>{promptHistoryPage++;renderPromptHistory()});navigation.append(summary,previous,next);updatePromptHistoryHeaders()}function refreshPromptHistory(){return fetch("/api/prompt-history").then(response=>response.ok?response.json():Promise.reject()).then(payload=>{promptHistoryEntries=Array.isArray(payload?.runs)?payload.runs:[];renderPromptHistory()}).catch(()=>{promptHistoryEntries=[];renderPromptHistory()})}$("promptHistoryFilter").addEventListener("input",()=>{promptHistoryPage=1;renderPromptHistory()});document.querySelectorAll("#promptHistory th[data-history-sort-key]").forEach(header=>{const sort=()=>{const key=header.dataset.historySortKey;if(promptHistorySort.key===key)promptHistorySort.direction=promptHistorySort.direction==="asc"?"desc":"asc";else promptHistorySort={key:key,direction:"asc"};promptHistoryPage=1;renderPromptHistory()};header.addEventListener("click",sort);header.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();sort()}})});refreshPromptHistory();
const parseStructuredLogEntries=structuredLogEntries;structuredLogEntries=text=>{const normalized=String(text??"").trim();return !normalized||normalized==="Nog geen applicatielog beschikbaar."?[]:parseStructuredLogEntries(normalized)};renderPaginatedComponentLogs=()=>{for(const component of ["inbox","dashboard"]){const entries=componentLogEntries[component],rows=filteredComponentLogEntries(component),body=$(component+"ComponentLog"),pageCount=Math.max(1,Math.ceil(rows.length/LOG_PAGE_SIZE)),page=Math.min(Math.max(1,independentLogPageStates[component]),pageCount),visible=rows.slice((page-1)*LOG_PAGE_SIZE,page*LOG_PAGE_SIZE);independentLogPageStates[component]=page;body.replaceChildren();if(!visible.length){const cell=document.createElement("td"),row=document.createElement("tr");cell.className="log-empty";cell.colSpan=6;cell.textContent=entries.length?"Geen logregels voor deze selectie.":"Nog geen applicatielog beschikbaar.";row.append(cell);body.append(row)}else for(const entry of visible){const row=document.createElement("tr");for(const [name,value] of [["log-line-number",entry.line],["",entry.timestamp||"—"],["log-level log-level--"+entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ","-"),entry.level],["",entry.event],["",entry.runId||"—"],["",entry.details||"—"]]){const cell=document.createElement("td");cell.className=name;cell.textContent=value;row.append(cell)}body.append(row)}renderLogPagination(component,rows.length,pageCount)}updateIndependentLogSortHeaders()};renderComponentLogs();
const DASHBOARD_CLIENT_STATE_KEY="engineering-dashboard-client-state-v1";function loadDashboardClientState(){try{const stored=JSON.parse(localStorage.getItem(DASHBOARD_CLIENT_STATE_KEY)||"{}");return stored&&typeof stored==="object"?stored:{}}catch{return{}}}const dashboardClientState=loadDashboardClientState();function saveDashboardClientState(){try{localStorage.setItem(DASHBOARD_CLIENT_STATE_KEY,JSON.stringify(dashboardClientState))}catch{}}function restoreDashboardDetails(root=document){const details=dashboardClientState.details||{};root.querySelectorAll?.("details[id]").forEach(element=>{if(Object.hasOwn(details,element.id))element.open=Boolean(details[element.id])})}const autoRefreshToggle=$("autoRefresh"),allSectionsToggle=$("toggleAllSections"),dashboardCategoryIds=["workspaceCard","queueItems","currentRun","rateLimits","executionTelemetry","lastExecutionGroup","platformHealth","codexChat","technicalDetails","componentLogs"];function visibleDashboardCategories(){return dashboardCategoryIds.map(id=>$(id)).filter(element=>element instanceof HTMLDetailsElement&&!element.hidden&&!element.closest("[hidden]"))}function updateAllSectionsToggle(){const categories=visibleDashboardCategories(),allOpen=categories.length>0&&categories.every(category=>category.open);allSectionsToggle.setAttribute("aria-checked",String(allOpen));allSectionsToggle.setAttribute("aria-label",allOpen?"Alle secties sluiten":"Alle secties openen");allSectionsToggle.title=allOpen?"Alles sluiten":"Alles openen"}function setAllSections(open){const details={...(dashboardClientState.details||{})};for(const category of visibleDashboardCategories()){category.open=open;details[category.id]=open}dashboardClientState.details=details;saveDashboardClientState();updateAllSectionsToggle()}allSectionsToggle.addEventListener("click",()=>setAllSections(allSectionsToggle.getAttribute("aria-checked")!=="true"));autoRefreshToggle.checked=dashboardClientState.autoRefresh!==false;autoRefreshToggle.addEventListener("change",()=>{dashboardClientState.autoRefresh=autoRefreshToggle.checked;saveDashboardClientState();$("updateMode").textContent=autoRefreshToggle.checked?"Serverpush: verbonden":"Automatisch vernieuwen is uit"});document.addEventListener("toggle",event=>{const element=event.target;if(element instanceof HTMLDetailsElement&&element.id){dashboardClientState.details={...(dashboardClientState.details||{}),[element.id]:element.open};saveDashboardClientState();updateAllSectionsToggle()}},true);for(const component of ["inbox","dashboard"]){const saved=dashboardClientState.logSorts?.[component];if(saved&&["line","timestamp","level","event","runId","details"].includes(saved.key)&&["asc","desc"].includes(saved.direction))independentLogSortStates[component]=saved}document.addEventListener("click",event=>{if(event.target.closest(".log-table th[data-sort-key]"))setTimeout(()=>{dashboardClientState.logSorts=structuredClone(independentLogSortStates);saveDashboardClientState()},0)});restoreDashboardDetails();new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)if(node instanceof Element)restoreDashboardDetails(node);updateAllSectionsToggle()}).observe($("engineering-dashboard-content"),{childList:true,subtree:true});const renderStatusWithSectionToggle=r;r=(x,snapshot={})=>{renderStatusWithSectionToggle(x,snapshot);updateAllSectionsToggle()};updateAllSectionsToggle();updateIndependentLogSortHeaders();
function chatHistoryMarkdown(){const entries=chatHistory.map(entry=>"## "+(entry.role==="user"?"Jij":"AI-assistent")+"\n\n"+entry.text.trim()).filter(Boolean);return["# AI-gesprek","","Model: "+$("chatModel").textContent.trim(),"",...entries].join("\n\n")}function updateChatDownloadAvailability(){const button=$("downloadChat");if(button)button.hidden=chatHistory.length===0}function downloadChatHistory(){if(!chatHistory.length)return;const url=URL.createObjectURL(new Blob([chatHistoryMarkdown()],{type:"text/markdown;charset=utf-8"})),link=document.createElement("a");link.href=url;link.download="ai-gesprek-"+new Date().toISOString().replace(/[:.]/g,"-")+".md";link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)}const renderChatHistoryWithDownload=renderChatHistory;renderChatHistory=()=>{renderChatHistoryWithDownload();updateChatDownloadAvailability()};const chatMessageWithDownload=chatMessage;chatMessage=(role,text)=>{chatMessageWithDownload(role,text);updateChatDownloadAvailability()};$("downloadChat").addEventListener("click",downloadChatHistory);updateChatDownloadAvailability();
const promptHistoryCategory=$("promptHistory");if(promptHistoryCategory&&Object.hasOwn(dashboardClientState.details||{},"promptHistory"))promptHistoryCategory.open=Boolean(dashboardClientState.details.promptHistory);dashboardCategoryIds.splice(2,0,"promptHistory");updateAllSectionsToggle();
const themeToggle=$("themeToggle"),themeColor=$("dashboardThemeColor");function applyDashboardTheme(theme){const light=theme==="light";document.documentElement.dataset.theme=light?"light":"dark";themeColor.content=light?"#f4f7fb":"#15151d";themeToggle.setAttribute("aria-checked",String(light));themeToggle.setAttribute("aria-label",light?"Donkere modus inschakelen":"Lichte modus inschakelen");themeToggle.title=light?"Donkere modus":"Lichte modus"}applyDashboardTheme(dashboardClientState.theme==="light"?"light":"dark");themeToggle.addEventListener("click",()=>{dashboardClientState.theme=document.documentElement.dataset.theme==="light"?"dark":"light";saveDashboardClientState();applyDashboardTheme(dashboardClientState.theme)});
function applyThemeModeAttributes(root=document.body){const theme=document.documentElement.dataset.theme==="light"?"light":"dark",elements=[];if(root instanceof Element)elements.push(root);if(root?.querySelectorAll)elements.push(...root.querySelectorAll("*"));for(const element of elements)if(!["SCRIPT","STYLE"].includes(element.tagName))element.dataset.themeMode=theme}const applyDashboardThemeWithElementAttributes=applyDashboardTheme;applyDashboardTheme=theme=>{applyDashboardThemeWithElementAttributes(theme);applyThemeModeAttributes()};applyThemeModeAttributes();new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)if(node instanceof Element)applyThemeModeAttributes(node)}).observe(document.body,{childList:true,subtree:true});
$("rateLimitProvider")?.previousElementSibling?.replaceChildren("Huidige AI-provider");
let latestPlatformHealthPayload=null;const restartingPlatformComponents=new Set();const renderPlatformHealthWithRestartState=renderPlatformHealth;renderPlatformHealth=payload=>{latestPlatformHealthPayload=payload;const components=payload&&typeof payload.components==="object"?Object.fromEntries(Object.entries(payload.components).map(([key,component])=>restartingPlatformComponents.has(key)?[key,{...component,healthy:false,state:"restarting",detail:"Herstart wordt uitgevoerd"}]:[key,component])):null;return renderPlatformHealthWithRestartState(components?{...payload,components}:payload)};async function confirmComponentRestart(component){for(let attempt=0;attempt<5;attempt++){await new Promise(resolve=>setTimeout(resolve,1250));try{const response=await fetch("/health",{cache:"no-store"}),payload=await response.json();if(response.ok&&payload?.components?.[component]?.healthy){restartingPlatformComponents.delete(component);renderPlatformHealth(payload);$("componentModalStatus").textContent="Component is opnieuw beschikbaar.";return}renderPlatformHealth(payload)}catch{}}$("componentModalStatus").textContent="De component komt nog niet gezond terug; controle loopt door."}const legacyRestartControl=$("componentModalRestart"),restartControl=legacyRestartControl.cloneNode(true);legacyRestartControl.replaceWith(restartControl);restartControl.addEventListener("click",async()=>{const component=restartControl.dataset.component;if(!component)return;if(!window.confirm("Weet je zeker dat je dit Engineering Platform-onderdeel wilt herstarten?"))return;restartControl.disabled=true;restartingPlatformComponents.add(component);renderPlatformHealth(latestPlatformHealthPayload);try{const response=await fetch("/api/components/"+encodeURIComponent(component)+"/restart",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}),payload=await response.json();if(!response.ok)throw Error(payload.error||"Herstarten is niet gelukt.");$("componentModalStatus").textContent="Herstartverzoek verzonden. De component wordt opnieuw gecontroleerd.";void confirmComponentRestart(component)}catch(error){restartingPlatformComponents.delete(component);renderPlatformHealth(latestPlatformHealthPayload);$("componentModalStatus").textContent=error.message||"Herstarten is niet gelukt."}finally{restartControl.disabled=false}});const renderStatusWithHealthInvalidation=r;r=(x,snapshot={})=>{renderStatusWithHealthInvalidation(x,snapshot);void refreshPlatformHealth()};
const formatComponentUptimeForMeasuredValues=formatComponentUptime;formatComponentUptime=value=>{const seconds=Number(value);return Number.isFinite(seconds)&&seconds>0?formatComponentUptimeForMeasuredValues(value):""};
function recordUserAction(action){return fetch("/api/audit/user-action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})}).catch(()=>undefined)}
function downloadLastExecutedDocument(endpoint,filenamePrefix){if(!lastExecutedRun)return Promise.reject(Error("Geen uitgevoerde prompt beschikbaar."));const separator=endpoint.includes("?")?"&":"?";return fetch(endpoint+separator+"run_id="+encodeURIComponent(lastExecutedRun)+"&audit=download").then(response=>response.ok?response.text():Promise.reject(Error("Download is niet beschikbaar."))).then(text=>{if(!text)throw Error("Download is niet beschikbaar.");const link=document.createElement("a"),url=URL.createObjectURL(new Blob([text],{type:"text/markdown;charset=utf-8"})),safeRun=String(lastExecutedRun).replace(/[^a-z0-9._-]+/gi,"-");link.href=url;link.download=filenamePrefix+"-"+safeRun+".md";link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)})}
$("downloadChat")?.addEventListener("click",()=>void recordUserAction("chat_downloaded"));$("copyReport")?.addEventListener("click",()=>void recordUserAction("report_copied"));$("copyReportAnalysis")?.addEventListener("click",()=>void recordUserAction("report_analysis_copied"));
let promptHistoryReportText="",promptHistoryReportRun="";function promptHistoryReportFilename(){return "engineering-report-"+String(promptHistoryReportRun||"unknown").replace(/[^a-z0-9._-]+/gi,"-")+".md"}function closePromptHistoryReport(){const modal=$("promptHistoryReportModal");if(modal.open)modal.close()}function downloadPromptHistoryReport(){if(!promptHistoryReportText)return;const url=URL.createObjectURL(new Blob([promptHistoryReportText],{type:"text/markdown;charset=utf-8"})),link=document.createElement("a");link.href=url;link.download=promptHistoryReportFilename();link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0);void recordUserAction("prompt_history_report_downloaded")}function openPromptHistoryReport(runId,title){const modal=$("promptHistoryReportModal"),content=$("promptHistoryReportContent");promptHistoryReportRun=String(runId||"");promptHistoryReportText="";$("promptHistoryReportModalTitle").textContent=title||"Engineeringrapport";$("promptHistoryReportCopy").hidden=true;$("promptHistoryReportDownload").hidden=true;content.replaceChildren();content.textContent="Rapport laden…";if(!modal.open)modal.showModal();fetch("/api/prompt-history/"+encodeURIComponent(promptHistoryReportRun)+"/report",{cache:"no-store"}).then(response=>response.ok?response.text():Promise.reject(Error("Rapport is niet beschikbaar."))).then(text=>{if(!text)throw Error("Rapport is niet beschikbaar.");promptHistoryReportText=text;renderMarkdownDocument(content,text);$("promptHistoryReportCopy").hidden=false;$("promptHistoryReportDownload").hidden=false}).catch(()=>{content.textContent="Engineeringrapport is niet beschikbaar voor deze prompt."})}$("promptHistoryReportClose").addEventListener("click",closePromptHistoryReport);$("promptHistoryReportModal").addEventListener("click",event=>{if(event.target===$("promptHistoryReportModal"))closePromptHistoryReport()});$("promptHistoryReportCopy").addEventListener("click",()=>{if(promptHistoryReportText)copyText(promptHistoryReportText).then(()=>void recordUserAction("prompt_history_report_copied"))});$("promptHistoryReportDownload").addEventListener("click",downloadPromptHistoryReport);const renderPromptHistoryWithReportView=renderPromptHistory;renderPromptHistory=()=>{renderPromptHistoryWithReportView();document.querySelectorAll("#promptHistoryRows a.prompt-history-report").forEach(link=>{const button=document.createElement("button");button.className="prompt-history-report";button.type="button";button.title="Bekijk engineeringrapport";button.setAttribute("aria-label","Bekijk engineeringrapport voor "+(link.getAttribute("aria-label")||"deze prompt").replace("Download engineeringrapport voor ",""));button.textContent="◉";button.addEventListener("click",()=>openPromptHistoryReport(link.href.split("/report")[0].split("/").pop(),button.getAttribute("aria-label").replace("Bekijk engineeringrapport voor ","")));link.replaceWith(button)})};
const renderStatusWithRetry=r;r=(x,snapshot={})=>{renderStatusWithRetry(x,snapshot);const blocked=Boolean(x&&x.blocking_predecessor_run),button=$("predecessorRetry"),status=$("predecessorRetryStatus");button.hidden=!blocked;button.disabled=isActiveRun(x||{});if(!blocked)status.textContent=""};
function submitPredecessorRetry(){const button=$("predecessorRetry"),status=$("predecessorRetryStatus"),run=latestStatus?.blocking_predecessor_run;if(!run||button.disabled)return;if(!window.confirm("Deze actie dient de geblokkeerde prompt opnieuw in. De Inbox-watcher voert hem daarna als eerstvolgende prompt uit. Doorgaan?"))return;button.disabled=true;status.textContent="Herindiening wordt klaargezet…";fetch("/api/predecessor-retry",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}).then(async response=>({ok:response.ok,body:await response.json()})).then(result=>{if(!result.ok)throw Error(result.body.error||"Herindiening kon niet worden gestart.");status.textContent="Herindiening staat in de Inbox en wordt door de watcher opgepakt."}).catch(error=>{status.textContent=error.message||"Herindiening kon niet worden gestart."}).finally(()=>{button.disabled=false})}
$("predecessorRetry").addEventListener("click",submitPredecessorRetry);
</script>
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
            if request_path == "/api/predecessor-retry":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length != 2 or self.rfile.read(length) != b"{}":
                        raise ValueError
                    outcome = submit_predecessor_retry(root, cloud_root(repo=root))
                    log_event(
                        logger,
                        logging.INFO,
                        "predecessor_retry_submission_triggered",
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
