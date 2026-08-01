"""Private read-only Engineering Status dashboard; no transaction authority."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
from html import escape
import json
import logging
import os
from pathlib import Path
import re
import select
import subprocess
import sys
from threading import Lock, Thread
import time
from urllib.parse import parse_qs, urlsplit
from .platform_api import PlatformConfiguration
from .platform_api import PlatformConfigurationError
from .providers import TailscaleProvider
from .providers import LaunchdProvider
from .inbox_watcher import WATCHER_VERSION, cloud_root
from .component_logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENVIRONMENT,
    VALID_LEVELS,
    component_logger,
    log_event,
)
from .codex_chat import CodexChatError, chat_model, respond as codex_chat_response

LABEL = "com.djconnect.engineering-dashboard"
DASHBOARD_VERSION = "1.2.0"
LOOPBACK_ADDRESS = "127.0.0.1"
CODEX_PROCESS = re.compile(r"(?:^|\s)(?:\S*/)?codex(?:\s|$)")
RATE_LIMIT_CACHE_SECONDS = 60
_rate_limit_cache_lock = Lock()
_rate_limit_cache: tuple[float, bytes] | None = None


class DashboardHTTPServer(ThreadingHTTPServer):
    """Private dashboard listener with safe restart behavior."""

    allow_reuse_address = True


def _unavailable_status() -> bytes:
    """Return the complete, safe status shape when no projection exists yet."""
    return json.dumps(
        {
            "watcher_state": "REMOTE_ENGINEERING_DEGRADED",
            "current_phase": "status niet beschikbaar",
            "current_action": "Voer het Engineering Platform uit om een statusupdate te publiceren.",
            "run_id": None,
            "queue_depth": 0,
            "implementation_pr": None,
            "finalization_pr": None,
            "repository_state": "UNKNOWN",
            "workspace_state": "UNKNOWN",
            "diagnostic": "Er is nog geen lokale engineeringstatus gepubliceerd.",
            "submitted_filename": None,
            "prompt_title": None,
            "last_executed_filename": None,
            "last_executed_title": None,
            "last_executed_run": None,
            "last_executed_phase": None,
            "blocking_predecessor_run": None,
            "blocking_predecessor_phase": None,
            "blocking_predecessor_filename": None,
            "blocking_predecessor_title": None,
            "predecessor_recovery_action": None,
        },
        separators=(",", ":"),
    ).encode()


def _status(root: Path) -> bytes:
    try:
        watcher = json.loads(cloud_root(repo=root).joinpath("status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, PlatformConfigurationError):
        watcher = {}
    try:
        live = json.loads((root / ".djconnect" / "status" / "current.json").read_text(encoding="utf-8"))
        projection = json.dumps(
            {
                "watcher_state": "ENGINEERING_RUN_ACTIVE",
                "current_phase": live.get("phase") or "INITIALIZE",
                "current_action": live.get("current_action") or "Engineeringuitvoering is actief.",
                "run_id": live.get("run_id"),
                "queue_depth": 0,
                "implementation_pr": live.get("implementation_pr"),
                "finalization_pr": live.get("finalization_pr"),
                "repository_state": live.get("repository_state") or "ACTIVE",
                "workspace_state": live.get("workspace_state") or "ACTIVE",
                "prompt_characters": live.get("prompt_characters"),
                "diagnostic": live.get("diagnostic"),
                "submitted_filename": watcher.get("submitted_filename"),
                "prompt_title": watcher.get("prompt_title"),
                "last_executed_filename": watcher.get("last_executed_filename"),
                "last_executed_title": watcher.get("last_executed_title"),
                "last_executed_run": watcher.get("last_executed_run"),
                "last_executed_phase": watcher.get("last_executed_phase"),
                "blocking_predecessor_run": watcher.get("blocking_predecessor_run"),
                "blocking_predecessor_phase": watcher.get("blocking_predecessor_phase"),
                "blocking_predecessor_filename": watcher.get("blocking_predecessor_filename"),
                "blocking_predecessor_title": watcher.get("blocking_predecessor_title"),
                "predecessor_recovery_action": watcher.get("predecessor_recovery_action"),
                "execution_mode": live.get("execution_mode"),
                "target_repository": live.get("target_repository"),
                "checkout_path": live.get("checkout_path"),
                "active_branch": live.get("active_branch"),
            },
            separators=(",", ":"),
        ).encode()
    except (OSError, json.JSONDecodeError):
        live, projection = None, None
    if live and live.get("phase") not in {"COMPLETE", "BLOCKED", "FAILED"}:
        return projection
    try:
        if watcher and (watcher.get("run_id") or watcher.get("last_executed_run")):
            return json.dumps(watcher, separators=(",", ":")).encode()
        return (root / ".djconnect" / "status" / "status.json").read_bytes()
    except OSError:
        return projection or _unavailable_status()


def _sse_status(root: Path) -> bytes:
    """Encode the status as a single SSE data line."""
    try:
        payload = json.loads(_status(root))
    except json.JSONDecodeError:
        payload = json.loads(_unavailable_status())
    return json.dumps(payload, separators=(",", ":")).encode()


def _sse_snapshot(root: Path) -> bytes:
    """Return the complete read-only dashboard projection for one SSE update.

    The browser receives this snapshot when it connects and only when one of
    its observable values changes.  This keeps the dashboard event-driven
    without giving the dashboard any transaction authority.
    """
    try:
        status = json.loads(_sse_status(root))
    except json.JSONDecodeError:
        status = json.loads(_unavailable_status())
    try:
        prompt_started = json.loads(_prompt_started(root))
    except json.JSONDecodeError:
        prompt_started = {}
    try:
        usage = json.loads(_codex_usage(root))
    except json.JSONDecodeError:
        usage = {}
    try:
        rate_limits = json.loads(_codex_rate_limits())
    except json.JSONDecodeError:
        rate_limits = {}
    try:
        last_executed_usage = json.loads(
            _codex_usage_for_run(root, status.get("last_executed_run"))
        )
    except json.JSONDecodeError:
        last_executed_usage = {}
    try:
        completion_commits = json.loads(_completion_commits(root))
        last_executed_commits = json.loads(_last_executed_commits(root))
    except json.JSONDecodeError:
        completion_commits = last_executed_commits = {}
    active = (
        status.get("watcher_state") == "ENGINEERING_RUN_ACTIVE"
        and isinstance(status.get("run_id"), str)
    )
    try:
        process_metrics = json.loads(_codex_process_metrics()) if active else {}
    except json.JSONDecodeError:
        process_metrics = {}
    return json.dumps(
        {
            "status": status,
            "build_commit": _build_commit(root),
            "prompt_started": prompt_started,
            "usage": usage,
            "rate_limits": rate_limits,
            "last_executed_usage": last_executed_usage,
            "completion_commits": completion_commits,
            "last_executed_commits": last_executed_commits,
            "process_metrics": process_metrics,
            "component_versions": {
                "dashboard": DASHBOARD_VERSION,
                "worker": WATCHER_VERSION,
            },
        },
        separators=(",", ":"),
    ).encode()


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


def _codex_rate_limits() -> bytes:
    """Read current Codex quota windows without persisting account or credit data."""
    global _rate_limit_cache
    now = time.monotonic()
    with _rate_limit_cache_lock:
        if _rate_limit_cache and now - _rate_limit_cache[0] < RATE_LIMIT_CACHE_SECONDS:
            return _rate_limit_cache[1]
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
            return b"{}"
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
                result = _normalize_rate_limits(response.get("result"))
                encoded = json.dumps(result, separators=(",", ":")).encode()
                with _rate_limit_cache_lock:
                    _rate_limit_cache = (time.monotonic(), encoded)
                return encoded
    except (OSError, ValueError, json.JSONDecodeError):
        return b"{}"
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
    return b"{}"


def _latest_codex_log(root: Path) -> bytes:
    """Return only the latest locally redacted Codex diagnostic."""
    logs = sorted((root / ".djconnect" / "logs" / "codex").glob("*.log"))
    try:
        return logs[-1].read_bytes() if logs else b"Geen Codex CLI-diagnose beschikbaar."
    except OSError:
        return b"Codex CLI-diagnose is niet beschikbaar."


def _component_log(root: Path, component: str) -> bytes:
    """Return a bounded tail of one known, already-redacted component log."""
    if component not in {"inbox", "dashboard"}:
        return b""
    try:
        lines = (root / ".djconnect" / "logs" / f"{component}.log").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return b"Nog geen applicatielog beschikbaar."
    tail = "\n".join(lines[-100:])[-64_000:]
    return (tail or "Nog geen applicatielog beschikbaar.").encode()


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
        reports = sorted((root / ".djconnect" / "reports").glob(f"*_{run_id}.md"))
        return reports[-1].read_bytes() if reports else b""
    except OSError:
        return b""


def _report_analysis_for_run(root: Path, run_id: str | None) -> bytes:
    """Return advisory analysis only when it belongs to the displayed terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    try:
        return (root / ".djconnect" / "report-analysis" / f"{run_id}.md").read_bytes()
    except OSError:
        return b""


def _current_codex_log(root: Path) -> bytes:
    """Return the diagnostic for the exact run currently shown by the dashboard."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"Geen Codex CLI-diagnose beschikbaar voor de huidige uitvoering."
    try:
        return (root / ".djconnect" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"Geen Codex CLI-diagnose beschikbaar voor de huidige uitvoering."


def _last_executed_codex_log(root: Path) -> bytes:
    """Return only the log bound to the latest completed or failed Inbox run."""
    try:
        run_id = json.loads((root / ".djconnect" / "status" / "status.json").read_text(encoding="utf-8")).get("last_executed_run")
    except (OSError, json.JSONDecodeError):
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"Geen Codex CLI-diagnose beschikbaar voor de laatst uitgevoerde uitvoering."
    try:
        return (root / ".djconnect" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"Geen Codex CLI-diagnose beschikbaar voor de laatst uitgevoerde uitvoering."


def _codex_usage(root: Path) -> bytes:
    """Return only CLI-reported usage bound to the displayed current or last run."""
    try:
        status = json.loads(_status(root))
        recorded = json.loads((root / ".djconnect" / "status" / "codex_usage.json").read_text(encoding="utf-8"))
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
            (root / ".djconnect" / "status" / "codex_usage.json").read_text(encoding="utf-8")
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
        checkpoint = json.loads((root / ".djconnect" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8"))
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
            (root / ".djconnect" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
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


def _prompt_started(root: Path) -> bytes:
    """Return the recorded Inbox start time for the run currently displayed."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str):
        return b"{}"
    for record in (root / ".djconnect" / "inbox-processing").glob("*/job.json"):
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


def _dashboard_html(title: str, build_commit: str = "onbekend") -> bytes:
    """Render the private dashboard with a server-pushed status stream."""
    page = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>$TITLE</title>
<style>
body{margin:0;background:#121217;color:#f7f3ee;font:16px system-ui;padding:max(18px,env(safe-area-inset-top)) 20px}.dashboard-grid{display:grid;gap:10px}h1{font-size:34px;line-height:1.1;margin:0 0 18px}.card,.technical-details{background:#24242d;border-radius:14px;padding:14px;box-shadow:0 4px 18px #0005}.card p{margin:7px 0}.current-run{background:#1d1d25;border:1px solid #3d3651;border-radius:18px;padding:14px;box-shadow:0 5px 24px #0006}.current-run__title{border-bottom:1px solid #3d3651;padding:2px 2px 13px}.current-run__title h2{font-size:22px;line-height:1.25;margin:3px 0 0}.current-run__grid{display:grid;gap:10px;margin-top:10px}.current-run .card{box-shadow:none}.prompt-runs{display:grid;gap:7px}.prompt-runs__heading{color:#b9b6c0;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.prompt-runs__cards,.last-execution{display:grid;gap:10px}.card--previous{background:#202a36;border:1px solid #37506a;box-shadow:0 4px 18px #0005}.card--previous strong,.card--previous .label{color:#8dc7ff}.field{margin:8px 0 0}.label{display:block;color:#c7a6ff;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-bottom:2px}.estimate-primary{font-size:20px;font-weight:650;margin:8px 0 0}.estimate-meta{color:#b9b6c0;font-size:12px;line-height:1.35;margin:6px 0 0}.final-status{align-items:center;display:flex;gap:7px;margin:0 0 8px}.indicator--small{height:9px;width:9px}.footer{color:#b9b6c0;font-size:12px;margin:14px 0 4px;text-align:center}.copy,.chat-send{background:#353541;color:#f7f3ee;border:1px solid #57576a;border-radius:8px;padding:6px 9px;font:13px system-ui}.copy{float:right}.technical-details{cursor:pointer}.technical-details summary{list-style:none}.technical-details summary::-webkit-details-marker{display:none}.technical-details summary::before{content:"▸ ";color:#c7a6ff}.technical-details[open] summary::before{content:"▾ "}.technical-grid{display:grid;gap:10px;margin-top:12px}.codex-chat{grid-column:1 / -1}.chat-messages{display:grid;gap:7px;max-height:260px;overflow:auto;margin:10px 0}.chat-message{padding:8px;border-radius:9px;white-space:pre-wrap}.chat-message--user{background:#353541}.chat-message--assistant{background:#202a36;border:1px solid #37506a}.chat-input{box-sizing:border-box;width:100%;min-height:60px;border:1px solid #57576a;border-radius:8px;background:#18181f;color:#f7f3ee;padding:8px;font:14px system-ui}.chat-status{color:#b9b6c0;font-size:12px}
strong{color:#c7a6ff}.status{display:flex;align-items:center;gap:8px}.indicator{width:12px;height:12px;border-radius:50%;background:#9a9aa3;box-shadow:0 0 8px #9a9aa388;flex:none}.indicator--green{background:#51d88a;box-shadow:0 0 8px #51d88a88}.indicator--yellow{background:#f4d35e;box-shadow:0 0 8px #f4d35e88}.indicator--orange{background:#ff9f43;box-shadow:0 0 8px #ff9f4388}.indicator--red{background:#ff6b6b;box-shadow:0 0 8px #ff6b6b88}.indicator--running{background:transparent;border:3px solid #ff9f43;border-right-color:transparent;box-sizing:border-box;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
pre{white-space:pre-wrap;word-break:break-word;margin:5px 0 0;font:12px ui-monospace,monospace}[hidden]{display:none}
@media (min-width:900px){body{max-width:1640px;margin:auto}.dashboard-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.current-run,.prompt-runs,.technical-details{grid-column:1 / -1}.current-run__grid{grid-template-columns:repeat(2,minmax(0,1fr))}.prompt-runs__cards{grid-template-columns:repeat(2,minmax(0,1fr))}.technical-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.technical-grid .card{box-shadow:none}}
</style>
<h1>$TITLE</h1>
<main class="dashboard-grid">
<section class="current-run" id="currentRun" aria-label="Huidige uitvoering" hidden><div class="current-run__title"><span class="label">Prompttitel</span><h2 id="currentPrompt">Laden…</h2><div class="field"><span class="label">Bestandsnaam</span><pre id="currentFile">Laden…</pre></div></div><div class="current-run__grid">
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" aria-label="Status onbekend"></span><strong>Promptstatus</strong></div><p class="field"><span class="label">Watcher</span><span id="watcher">Laden…</span></p><p class="field"><span class="label">Fase</span><span id="phase">Laden…</span></p><p class="field"><span class="label">Huidige actie</span><span id="action">Laden…</span></p></div>
<div class="card" id="predecessorGate" hidden><strong>Wachtrij geblokkeerd</strong><p class="field"><span class="label">Blokkerende run</span><code id="predecessorRun"></code></p><p class="field"><span class="label">Voorafgaande prompt</span><span id="predecessorPrompt"></span></p><p class="field"><span class="label">Eindstatus</span><span id="predecessorPhase"></span></p><div class="field"><span class="label">Herstelactie</span><pre id="predecessorAction"></pre></div></div>
<div class="card"><strong>Tijd</strong><p id="currentTime">Laden…</p><p id="lastRefresh">Laatst bijgewerkt: laden…</p><p id="updateMode">Serverpush: verbinden…</p></div>
<div class="card"><strong>Geschatte uitvoeringstijd</strong><p class="estimate-primary" id="executionEstimate">Nog niet beschikbaar…</p><p class="estimate-meta" id="executionEstimateMeta" hidden></p></div>
<div class="card"><strong>Uitvoering</strong><p class="field"><span class="label">Run-ID</span><span id="runId"></span></p><p class="field"><span class="label">Prompt gestart op</span><span id="promptStarted">Laden…</span></p><p class="field"><span class="label">Wachtrij</span><span id="queue"></span></p></div>
<div class="card execution-context" id="executionContext" hidden><strong>Uitvoeringscontext</strong><p class="field"><span class="label">Modus</span><span id="executionMode"></span></p><p class="field"><span class="label">Repository</span><span id="targetRepository"></span></p><div class="field"><span class="label">Lokale checkout</span><pre id="checkoutPath"></pre></div><p class="field"><span class="label">Actieve branch</span><span id="activeBranch"></span></p></div>
<div class="card" id="processMetrics" hidden><strong>Lokale Codex-processen</strong><p class="field"><span class="label">CPU-gebruik</span><span id="codexCpu">Laden…</span></p><p class="field"><span class="label">Actieve processen</span><span id="codexProcesses">Laden…</span></p><p class="field"><span class="label">GPU-gebruik</span><span id="codexGpu">Laden…</span></p></div>
<div class="card" id="usage" hidden><strong>Codex CLI-gebruik</strong><div class="field"><span class="label">Gerapporteerd verbruik</span><pre id="usageDetails"></pre></div></div>
<div class="card" id="currentDiagnostic" hidden><strong>Codex CLI-diagnose</strong><pre id="currentLog">Laden…</pre></div>
</div></section>
<div class="card" id="rateLimits" hidden><strong>Resterend gebruik</strong><div class="field"><span class="label">Codex-gebruikslimieten</span><pre id="rateLimitDetails"></pre></div></div>
<div class="card" id="commits" hidden><strong>Voltooiingscommits</strong><div class="field"><span class="label">Vastgelegd bewijs</span><pre id="completionCommits"></pre></div></div>
<section class="prompt-runs" id="promptRuns" aria-label="Promptuitvoeringen" hidden><div class="prompt-runs__heading">Promptuitvoeringen</div><div class="prompt-runs__cards">
<div class="last-execution" id="lastExecution" hidden><div class="card card--previous"><div class="final-status"><span id="lastIndicator" class="indicator indicator--small" aria-hidden="true"></span><strong>Laatst uitgevoerd</strong><span id="lastFinalStatus"></span></div><p class="field"><span class="label">Prompttitel</span><span id="lastPrompt"></span></p><div class="field"><span class="label">Bestandsnaam</span><pre id="lastFile"></pre></div><div class="field" id="lastCommits" hidden><span class="label">Git-commit</span><pre id="lastCommitDetails"></pre></div><div class="field" id="lastUsage" hidden><span class="label">Codex CLI-gebruik</span><pre id="lastUsageDetails"></pre></div><div class="field" id="lastDiagnostic" hidden><span class="label">Codex CLI-diagnose</span><pre id="lastLog">Laden…</pre></div></div><details class="card card--previous" id="report" hidden><summary><strong>Engineeringrapport</strong></summary><button class="copy" id="copyReport" type="button" title="Kopieer rapport" aria-label="Kopieer rapport">⧉ Kopieer</button><div class="field"><span class="label">Markdownrapport</span><pre id="reportContent">Open dit blok om het rapport te laden.</pre></div></details><details class="card card--previous" id="reportAnalysis" hidden><summary><strong>Codex-analyse van rapport</strong></summary><div class="field"><span class="label">Adviserend; repositorybewijs blijft leidend</span><pre id="reportAnalysisContent">Open dit blok om de analyse te laden.</pre></div></details></div>
</div></section>
<details class="technical-details" id="componentLogs"><summary><strong>Applicatielogs</strong></summary><p class="estimate-meta">Geredigeerde, roterende logs van watcher en dashboard. Deze worden pas opgehaald nadat je op de knop drukt.</p><button class="copy" id="loadComponentLogs" type="button">Logs laden</button><div class="technical-grid"><div class="card"><strong>Inbox-watcher</strong><pre id="inboxComponentLog">Nog niet geladen.</pre></div><div class="card"><strong>Statusdashboard</strong><pre id="dashboardComponentLog">Nog niet geladen.</pre></div></div></details>
<section class="card codex-chat" id="codexChat"><strong>Codex gesprek</strong><p class="estimate-meta">Alleen lezen · context: repository, laatst uitgevoerde prompt en rapport. Deze chat kan geen engineering starten of wijzigingen uitvoeren.</p><p class="field"><span class="label">Gebruikt model</span><span id="chatModel">$CHAT_MODEL</span></p><div class="chat-messages" id="chatMessages" aria-live="polite"></div><label class="label" for="chatInput">Vraag aan Codex</label><textarea id="chatInput" class="chat-input" maxlength="2000" placeholder="Bijvoorbeeld: wat zijn de belangrijkste vervolgstappen uit het laatste rapport?"></textarea><p><button class="chat-send" id="chatSend" type="button">Vraag stellen</button> <span class="chat-status" id="chatStatus"></span></p></section>
<details class="technical-details"><summary><strong>Technische details</strong></summary><div class="technical-grid">
<div class="card"><strong>Pull requests</strong><p class="field"><span class="label">Implementatie</span><span id="implementation"></span></p><p class="field"><span class="label">Finalisatie</span><span id="finalization"></span></p></div>
<div class="card"><strong>Repository</strong><p class="field"><span class="label">Repositorystatus</span><span id="repositoryState"></span></p><p class="field"><span class="label">Werkruimtestatus</span><span id="workspaceState"></span></p></div>
<div class="card"><strong>Diagnose</strong><p id="diag"></p></div>
</div></details>
</main>
<footer class="footer"><span class="label">Engineering Platform-versie</span><span id="platformVersion">Laden…</span> · <span class="label">Dashboard</span><span id="dashboardVersion">Laden…</span> · <span class="label">Worker</span><span id="workerVersion">Laden…</span> · <span class="label">Git-commit</span><code>$BUILD_COMMIT</code></footer>
<script>
const $=id=>document.getElementById(id),DASHBOARD_BUILD="$BUILD_COMMIT",DASHBOARD_BUILD_KEY="djconnect-engineering-dashboard-build",
formatTime=new Intl.DateTimeFormat("nl-NL",{timeZone:"Europe/Amsterdam",dateStyle:"full",timeStyle:"medium"}),
fallback={watcher_state:"REMOTE_ENGINEERING_DEGRADED",current_phase:"status niet beschikbaar",current_action:"Ververs het dashboard nadat het Engineering Platform een statusupdate heeft gepubliceerd.",queue_depth:0,repository_state:"UNKNOWN",workspace_state:"UNKNOWN",diagnostic:"Het statusverzoek kon niet worden voltooid."};
let currentLogRun,lastLogRun,lastRefresh,promptStartedAt,latestStatus;
const humanLabels={ENGINEERING_RUN_ACTIVE:"Engineering actief",WATCHER_IDLE:"Watcher wacht",REMOTE_ENGINEERING_DEGRADED:"Engineeringstatus beperkt beschikbaar",JOB_CLAIMED:"Opdracht opgepakt",RUNNER_STARTING:"Uitvoering wordt gestart",REPORT_PUBLISHING:"Rapport wordt gepubliceerd",JOB_COMPLETED:"Opdracht voltooid",JOB_BLOCKED:"Opdracht geblokkeerd",JOB_FAILED:"Opdracht mislukt",WAITING_FOR_REPOSITORY:"Wacht op repository",INITIALIZE:"Voorbereiding",EXECUTE_AGENT:"Uitvoering",REPAIR_AGENT:"Herstel",FINALIZE_AGENT:"Finalisatie",REPOSITORY_CLEANUP:"Opschoning repository",COMPLETE:"Voltooid",BLOCKED:"Geblokkeerd",FAILED:"Mislukt",invoke_agent:"Engineering uitvoeren",repository_reconciled:"Repository afgestemd",MERGED_RECONCILED:"Samengevoegd en afgestemd",WORKSPACE_READY:"Werkruimte gereed",ACTIVE:"Actief",UNKNOWN:"Onbekend","status unavailable":"status niet beschikbaar"};
const humanLabels={ENGINEERING_RUN_ACTIVE:"Engineering actief",WATCHER_IDLE:"Watcher wacht",REMOTE_ENGINEERING_DEGRADED:"Engineeringstatus beperkt beschikbaar",JOB_CLAIMED:"Opdracht opgepakt",RUNNER_STARTING:"Uitvoering wordt gestart",REPORT_PUBLISHING:"Rapport wordt gepubliceerd",JOB_COMPLETED:"Opdracht voltooid",JOB_BLOCKED:"Opdracht geblokkeerd",JOB_FAILED:"Opdracht mislukt",WAITING_FOR_REPOSITORY:"Wacht op repository",WAITING_FOR_PREDECESSOR:"Wacht op voorafgaande prompt",INITIALIZE:"Voorbereiding",EXECUTE_AGENT:"Uitvoering",REPAIR_AGENT:"Herstel",FINALIZE_AGENT:"Finalisatie",REPOSITORY_CLEANUP:"Opschoning repository",COMPLETE:"Voltooid",BLOCKED:"Geblokkeerd",FAILED:"Mislukt",invoke_agent:"Engineering uitvoeren",repository_reconciled:"Repository afgestemd",MERGED_RECONCILED:"Samengevoegd en afgestemd",WORKSPACE_READY:"Werkruimte gereed",ACTIVE:"Actief",UNKNOWN:"Onbekend","status unavailable":"status niet beschikbaar"};
const dutchDiagnostics={"Engineering report was not available for delivery.":"Engineeringrapport kon niet worden afgeleverd.","Runner ended without a safe terminal report.":"De runner stopte zonder een veilig eindrapport.","An existing engineering transaction remains active.":"Een bestaande engineeringuitvoering is nog actief.","Duplicate job digest remains recorded.":"Een dubbele opdracht is al geregistreerd.","Another watcher owns the local inbox lock.":"Een andere watcher beheert de lokale Inbox-vergrendeling.","No local engineering status has been published yet.":"Er is nog geen lokale engineeringstatus gepubliceerd.","The status request could not be completed.":"Het statusverzoek kon niet worden voltooid."};
function translate(value){return humanLabels[value]||dutchDiagnostics[value]||value}
function humanize(){for(const id of ["watcher","phase","action","repositoryState","workspaceState","diag"]){const element=$(id);element.textContent=translate(element.textContent)}}
function tone(x){const phase=x.current_phase||"",watcher=x.watcher_state||"";if(["BLOCKED","FAILED"].includes(phase)||["JOB_BLOCKED","JOB_FAILED"].includes(watcher))return "red";if(phase==="COMPLETE"||watcher==="JOB_COMPLETED")return "green";if(phase==="WAIT_FOR_TERMINAL_EVIDENCE"||["WAITING_FOR_REPOSITORY","WAITING_FOR_PREDECESSOR"].includes(watcher))return "yellow";if(["INITIALIZE","EXECUTE_AGENT","REPAIR_AGENT","FINALIZE_AGENT","REPOSITORY_CLEANUP"].includes(phase)||["RUNNER_STARTING","JOB_CLAIMED"].includes(watcher))return "orange";return "grey"}
function finalStatus(phase){if(phase==="COMPLETE")return ["green","Voltooid"];if(phase==="BLOCKED")return ["yellow","Geblokkeerd"];if(phase==="FAILED")return ["red","Mislukt"];return ["grey","Status onbekend"]}
function executionRange(x){const characters=Number(x.prompt_characters)||0;if(characters<=2000)return [6,10];if(characters<=6000)return [10,18];if(characters<=12000)return [16,26];return [24,38]}
function pluralMinutes(value){return value===1?"minuut":"minuten"}
function estimate(x){const phase=x.current_phase||"";if(phase==="INITIALIZE")return {summary:"Voorbereiding: minder dan 1 minuut",context:""};if(["EXECUTE_AGENT","REPAIR_AGENT"].includes(phase)){const [minimum,maximum]=executionRange(x);if(!promptStartedAt)return {summary:"Indicatieve totale duur: "+minimum+"–"+maximum+" minuten",context:"Gebaseerd op promptomvang en fase. Live Codex-voortgang is niet beschikbaar."};const elapsed=Math.max(0,Math.floor((Date.now()-promptStartedAt)/60000)),remainingMinimum=Math.max(1,minimum-elapsed),remainingMaximum=Math.max(remainingMinimum,maximum-elapsed);return {summary:"Indicatief resterend: "+remainingMinimum+"–"+remainingMaximum+" minuten",context:elapsed+" "+pluralMinutes(elapsed)+" verstreken · gebaseerd op promptomvang, fase en verstreken tijd. Geen live Codex-voortgang of tokenverbruik."}}if(phase==="FINALIZE_AGENT")return {summary:"Finalisatie in uitvoering",context:"De resterende tijd is pas betrouwbaar met live Codex-voortgang."};if(phase==="REPOSITORY_CLEANUP")return {summary:"Opschoning in uitvoering",context:"De resterende tijd hangt af van de lokale repository."};if(phase==="WAIT_FOR_TERMINAL_EVIDENCE")return {summary:"Wacht op externe verificatie",context:"Geen betrouwbare ETA."};if(phase==="COMPLETE")return {summary:"Voltooid",context:""};if(["BLOCKED","FAILED"].includes(phase))return {summary:"Gestopt; actie nodig",context:""};return {summary:"Nog niet beschikbaar",context:""}}
function renderEstimate(x){const value=estimate(x);$("executionEstimate").textContent=value.summary;$("executionEstimateMeta").textContent=value.context;$("executionEstimateMeta").hidden=!value.context}
function isActiveRun(x){return x.watcher_state==="ENGINEERING_RUN_ACTIVE"&&Boolean(x.run_id)}
function checkBuild(build){if(build===DASHBOARD_BUILD){sessionStorage.removeItem(DASHBOARD_BUILD_KEY);return}if(build&&DASHBOARD_BUILD!=="onbekend"&&sessionStorage.getItem(DASHBOARD_BUILD_KEY)!==build){sessionStorage.setItem(DASHBOARD_BUILD_KEY,build);location.reload()}}
function clock(){let now=Date.now();$("currentTime").textContent=formatTime.format(new Date(now));$("lastRefresh").textContent="Laatst bijgewerkt: "+(lastRefresh?formatTime.format(lastRefresh):"laden…")}
function l(id,url,run,last,container){if(run===(last?lastLogRun:currentLogRun))return;if(last)lastLogRun=run;else currentLogRun=run;$(id).textContent="Diagnose laden…";fetch(url).then(x=>x.text()).then(x=>{const available=Boolean(x)&&!x.startsWith("No Codex CLI diagnostic is available")&&!x.startsWith("Geen Codex CLI-diagnose beschikbaar");$(container).hidden=!available;if(available)$(id).textContent=x}).catch(()=>{$(container).hidden=false;$(id).textContent="Codex CLI-diagnose is niet beschikbaar."})}
function usage(x){const labels={input_tokens:"Invoertokens",cached_input_tokens:"Gecachete invoertokens",output_tokens:"Uitvoertokens",total_tokens:"Totaal tokens",cost:"Kosten",remaining:"Resterend beschikbaar",plan_remaining:"Resterend in plan",usage:"Gebruik"};let entries=Object.entries(x||{});$("usage").hidden=!entries.length;$("usageDetails").textContent=entries.map(([key,value])=>(labels[key]||key.replaceAll("_"," "))+": "+value).join("\\n")}
function rateLimits(x){const windows=Array.isArray(x?.windows)?x.windows:[],credits=Number.isInteger(x?.reset_credits)?x.reset_credits:null;$("rateLimits").hidden=!windows.length&&credits===null;let lines=windows.map(window=>{const remaining=Math.max(0,100-Number(window.used_percent||0)),reset=Number(window.resets_at);return window.label+": "+remaining+"% beschikbaar · reset "+(Number.isFinite(reset)?formatTime.format(new Date(reset*1000)):"onbekend")});if(credits!==null)lines.push("Beschikbare resets: "+credits);$("rateLimitDetails").textContent=lines.join("\\n")}
function lastUsage(x){const labels={input_tokens:"Invoertokens",cached_input_tokens:"Gecachete invoertokens",output_tokens:"Uitvoertokens",total_tokens:"Totaal tokens",cost:"Kosten",remaining:"Resterend beschikbaar",plan_remaining:"Resterend in plan",usage:"Gebruik"};let entries=Object.entries(x||{});$("lastUsage").hidden=!entries.length;$("lastUsageDetails").textContent=entries.map(([key,value])=>(labels[key]||key.replaceAll("_"," "))+": "+value).join("\\n")}
function processMetrics(active,x){$("processMetrics").hidden=!active;if(!active)return;$("codexCpu").textContent=Number(x?.cpu_percent||0).toLocaleString("nl-NL",{maximumFractionDigits:1})+"%";$("codexProcesses").textContent=x?.process_count??0;$("codexGpu").textContent=x?.gpu_status||"Niet beschikbaar"}
function commits(x){let entries=Object.entries(x||{});$("commits").hidden=!entries.length;$("completionCommits").textContent=entries.map(([label,sha])=>label+": "+sha).join("\\n")}
function lastCommits(x){let entries=Object.entries(x||{});$("lastCommits").hidden=!entries.length;$("lastCommitDetails").textContent=entries.map(([label,sha])=>label+": "+sha).join("\\n")}
function promptStarted(x){promptStartedAt=x?.started_at?Date.parse(x.started_at):undefined;$("promptStarted").textContent=promptStartedAt?formatTime.format(new Date(promptStartedAt)):"Niet beschikbaar";if(latestStatus)renderEstimate(latestStatus)}
let lastExecutedRun,reportLoaded=false,reportRequest,analysisLoaded=false,analysisRequest;function report(){if(!lastExecutedRun)return Promise.resolve();if(reportLoaded)return reportRequest;reportLoaded=true;return reportRequest=fetch("/api/report/last-executed?run_id="+encodeURIComponent(lastExecutedRun)).then(x=>x.text()).then(x=>{if(!x){$("report").hidden=true;return}$("reportContent").textContent=x}).catch(()=>{$("reportContent").textContent="Engineeringrapport is niet beschikbaar."})}function analysis(){if(!lastExecutedRun)return Promise.resolve();if(analysisLoaded)return analysisRequest;analysisLoaded=true;return analysisRequest=fetch("/api/report-analysis/last-executed?run_id="+encodeURIComponent(lastExecutedRun)).then(x=>x.text()).then(x=>{if(!x){$("reportAnalysis").hidden=true;return}$("reportAnalysisContent").textContent=x}).catch(()=>{$("reportAnalysisContent").textContent="Codex-analyse is niet beschikbaar."})}
let componentLogsLoaded=false;function loadComponentLogs(){if(componentLogsLoaded)return;$("loadComponentLogs").disabled=true;$("loadComponentLogs").textContent="Logs laden…";Promise.all([fetch("/api/logs/inbox").then(x=>x.text()),fetch("/api/logs/dashboard").then(x=>x.text())]).then(([inbox,dashboard])=>{$("inboxComponentLog").textContent=inbox;$("dashboardComponentLog").textContent=dashboard;componentLogsLoaded=true;$("loadComponentLogs").textContent="Logs geladen"}).catch(()=>{$("inboxComponentLog").textContent="Inbox-log is niet beschikbaar.";$("dashboardComponentLog").textContent="Dashboard-log is niet beschikbaar.";$("loadComponentLogs").disabled=false;$("loadComponentLogs").textContent="Opnieuw proberen"})}
let chatHistory=[];function chatMessage(role,text){let item=document.createElement("div");item.className="chat-message chat-message--"+role;item.textContent=(role==="user"?"Jij: ":"Codex: ")+text;$("chatMessages").append(item);item.scrollIntoView({block:"nearest"})}function askCodex(){let input=$("chatInput"),message=input.value.trim();if(!message||$("chatSend").disabled)return;$("chatSend").disabled=true;$("chatStatus").textContent="Codex denkt na…";chatMessage("user",message);input.value="";fetch("/api/codex-chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:message,history:chatHistory.slice(-6)})}).then(async response=>({ok:response.ok,body:await response.json()})).then(result=>{if(!result.ok)throw Error(result.body.error||"Codex Gesprek is niet beschikbaar.");let answer=result.body.answer;$("chatModel").textContent=result.body.model||$("chatModel").textContent;chatMessage("assistant",answer);chatHistory.push({role:"user",text:message},{role:"assistant",text:answer});chatHistory=chatHistory.slice(-6);$("chatStatus").textContent=""}).catch(error=>{$("chatStatus").textContent=error.message}).finally(()=>{$("chatSend").disabled=false})}
function fallbackCopy(value){const area=document.createElement("textarea");area.value=value;area.setAttribute("readonly","");area.style.cssText="position:fixed;top:0;left:0;opacity:0";document.body.append(area);area.focus();area.select();area.setSelectionRange(0,area.value.length);const copied=document.execCommand("copy");area.remove();if(!copied)throw Error("copy unavailable")}
function copyText(value){return navigator.clipboard&&window.isSecureContext?navigator.clipboard.writeText(value).catch(()=>fallbackCopy(value)):Promise.resolve().then(()=>fallbackCopy(value))}
function copyReport(){report().then(()=>copyText($("reportContent").textContent)).then(()=>{$("copyReport").textContent="Gekopieerd";setTimeout(()=>{$("copyReport").textContent="⧉ Kopieer"},1500)}).catch(()=>{$("copyReport").textContent="Kopiëren mislukt"})}
function r(x,snapshot={}){lastRefresh=new Date();clock();x=x&&typeof x==="object"?x:fallback;latestStatus=x;let active=isActiveRun(x),statusTone=tone(x),indicator=$("indicator"),previous=x.last_executed_run||null,lastStatus=finalStatus(x.last_executed_phase),components=snapshot.component_versions||{},blocked=Boolean(x.blocking_predecessor_run);if(previous!==lastExecutedRun){lastExecutedRun=previous;reportLoaded=false;reportRequest=undefined;analysisLoaded=false;analysisRequest=undefined;$("report").open=false;$("reportAnalysis").open=false;$("reportContent").textContent="Open dit blok om het rapport te laden.";$("reportAnalysisContent").textContent="Open dit blok om de analyse te laden."}$("currentRun").hidden=!active;$("promptRuns").hidden=!previous;$("lastExecution").hidden=!previous;$("report").hidden=!previous;$("reportAnalysis").hidden=!previous;$("predecessorGate").hidden=!blocked;$("predecessorRun").textContent=x.blocking_predecessor_run||"Niet beschikbaar";$("predecessorPrompt").textContent=x.blocking_predecessor_title||x.blocking_predecessor_filename||"Niet beschikbaar";$("predecessorPhase").textContent=translate(x.blocking_predecessor_phase||"Niet beschikbaar");$("predecessorAction").textContent=x.predecessor_recovery_action||"Niet beschikbaar";$("executionContext").hidden=!x.execution_mode;$("executionMode").textContent=x.execution_mode||"Niet beschikbaar";$("targetRepository").textContent=x.target_repository||"Niet beschikbaar";$("checkoutPath").textContent=x.checkout_path||"Niet beschikbaar";$("activeBranch").textContent=x.active_branch||"Niet beschikbaar";indicator.className="indicator indicator--"+statusTone+(active?" indicator--running":"");indicator.setAttribute("aria-label","Promptstatus: "+statusTone);$("lastIndicator").className="indicator indicator--small indicator--"+lastStatus[0];$("lastFinalStatus").textContent=lastStatus[1];$("watcher").textContent=translate(x.watcher_state||fallback.watcher_state);$("phase").textContent=translate(x.current_phase||"idle");$("action").textContent=translate(x.current_action||"Geen actieve actie");promptStarted(snapshot.prompt_started);renderEstimate(x);processMetrics(active,snapshot.process_metrics);$("currentPrompt").textContent=x.prompt_title||"Niet beschikbaar";$("currentFile").textContent=x.submitted_filename||"Niet beschikbaar";if(!active||x.run_id!==currentLogRun)$("currentDiagnostic").hidden=true;if(active)l("currentLog","/api/log/current",x.run_id||null,false,"currentDiagnostic");$("lastPrompt").textContent=x.last_executed_title||"Nog geen prompt uitgevoerd";$("lastFile").textContent=x.last_executed_filename||"Niet beschikbaar";$("lastDiagnostic").hidden=lastStatus[0]==="green";if(previous&&lastStatus[0]!=="green")l("lastLog","/api/log/last",previous,true,"lastDiagnostic");$("runId").textContent=x.run_id||"geen";$("queue").textContent=x.queue_depth??0;$("implementation").textContent=x.implementation_pr||"geen";$("finalization").textContent=x.finalization_pr||"geen";$("repositoryState").textContent=translate(x.repository_state||"UNKNOWN");$("workspaceState").textContent=translate(x.workspace_state||"UNKNOWN");$("diag").textContent=translate(x.diagnostic||"Geen diagnose");$("platformVersion").textContent=x.platform_version||"Niet beschikbaar";$("dashboardVersion").textContent=components.dashboard||"Niet beschikbaar";$("workerVersion").textContent=components.worker||"Niet beschikbaar";usage(snapshot.usage);rateLimits(snapshot.rate_limits);lastUsage(snapshot.last_executed_usage);commits(snapshot.completion_commits);lastCommits(snapshot.last_executed_commits)}
let chatHistory=[];function chatMessage(role,text){let item=document.createElement("div");item.className="chat-message chat-message--"+role;item.textContent=(role==="user"?"Jij: ":"Codex: ")+text;$("chatMessages").append(item);item.scrollIntoView({block:"nearest"})}function askCodex(){let input=$("chatInput"),message=input.value.trim();if(!message||$("chatSend").disabled)return;$("chatSend").disabled=true;$("chatStatus").textContent="Codex denkt na…";chatMessage("user",message);input.value="";fetch("/api/codex-chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:message,history:chatHistory.slice(-6)})}).then(async response=>({ok:response.ok,body:await response.json()})).then(result=>{if(!result.ok)throw Error(result.body.error||"Codex Gesprek is niet beschikbaar.");let answer=result.body.answer;chatMessage("assistant",answer);chatHistory.push({role:"user",text:message},{role:"assistant",text:answer});chatHistory=chatHistory.slice(-6);$("chatStatus").textContent=""}).catch(error=>{$("chatStatus").textContent=error.message}).finally(()=>{$("chatSend").disabled=false})}
let e=new EventSource("/api/events");e.addEventListener("dashboard",x=>{try{let snapshot=JSON.parse(x.data);r(snapshot.status,snapshot);humanize();checkBuild(snapshot.build_commit);$("updateMode").textContent="Serverpush: verbonden"}catch{r(fallback);humanize();$("updateMode").textContent="Serverpush: update ongeldig"}});e.onerror=()=>{$("updateMode").textContent="Serverpush: opnieuw verbinden…"};$("report").addEventListener("toggle",()=>{$("report").open&&report()});$("reportAnalysis").addEventListener("toggle",()=>{$("reportAnalysis").open&&analysis()});$("copyReport").addEventListener("click",copyReport);$("loadComponentLogs").addEventListener("click",loadComponentLogs);$("chatSend").addEventListener("click",askCodex);$("chatInput").addEventListener("keydown",event=>{if(event.key==="Enter"&&(event.metaKey||event.ctrlKey)){event.preventDefault();askCodex()}});setInterval(clock,250);clock()
</script>"""
    return (
        page.replace("$TITLE", escape(title))
        .replace("$BUILD_COMMIT", escape(build_commit))
        .replace("$CHAT_MODEL", escape(chat_model()))
        .encode()
    )


def handler(root: Path, logger: logging.Logger | None = None):
    title = PlatformConfiguration.load(root).workspace.dashboard_title
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
            if urlsplit(self.path).path != "/api/codex-chat":
                self.send_error(404)
                return
            if not self._same_origin():
                self._send(b'{"error":"Ongeldige herkomst."}', "application/json; charset=utf-8", 403)
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
            self._send(
                json.dumps({"answer": answer, "model": chat_model()}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            log_event(logger, logging.DEBUG, "http_request", diagnostic=request.path)
            if request.path == "/api/report/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                return self._send(
                    _report_for_run(root, run_id), "text/markdown; charset=utf-8"
                )
            if request.path == "/api/report-analysis/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                return self._send(
                    _report_analysis_for_run(root, run_id), "text/markdown; charset=utf-8"
                )
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
                    reports = sorted((root / ".djconnect" / "reports").glob("*.md"))
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
                return self._send(_dashboard_html(title, _build_commit(root)), "text/html; charset=utf-8")
            log_event(logger, logging.WARNING, "http_not_found", diagnostic=request.path)
            self.send_error(404)

        def log_message(self, message: str, *_: object) -> None:
            log_event(logger, logging.DEBUG, "http_server_message", diagnostic=message)

    return DashboardHandler


def binding_addresses(provider: TailscaleProvider | None = None) -> tuple[str, ...]:
    """Bind only loopback and the explicit local Tailscale address.

    The dashboard deliberately never binds a wildcard, LAN, public or Funnel
    address.  Tailnet policy remains the access boundary; this code changes no
    Tailscale configuration.
    """
    tailscale_address = (provider or TailscaleProvider()).ipv4_address()
    return (LOOPBACK_ADDRESS, *(() if tailscale_address is None else (tailscale_address,)))


def create_servers(
    root: Path,
    port: int = 8765,
    provider: TailscaleProvider | None = None,
    logger: logging.Logger | None = None,
) -> tuple[DashboardHTTPServer, ...]:
    """Create the exact private listeners for the dashboard."""
    request_handler = handler(root, logger)
    return tuple(
        DashboardHTTPServer((address, port), request_handler)
        for address in binding_addresses(provider)
    )


def run(root: Path, port: int = 8765, provider: TailscaleProvider | None = None) -> None:
    """Serve locally and, when present, over the authenticated Tailnet only."""
    logger = component_logger(root, "dashboard")
    try:
        servers = create_servers(root, port, provider, logger)
    except OSError as error:
        log_event(logger, logging.ERROR, "dashboard_start_failed", diagnostic=str(error))
        raise
    log_event(
        logger,
        logging.INFO,
        "dashboard_started",
        diagnostic="addresses=" + ",".join(address for address, _ in (server.server_address for server in servers)),
    )
    for server in servers[1:]:
        Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    finally:
        log_event(logger, logging.INFO, "dashboard_stopped")


def launch_agent(repo: Path) -> Path:
    """Render the only owned per-user LaunchAgent; no network policy changes."""
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = repo / ".djconnect" / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    arguments = "".join(
        f"<string>{value}</string>"
        for value in (
            sys.executable,
            "-m",
            "tools.engineering.dashboard",
            "run",
            "--repo",
            str(repo),
        )
    )
    log_level = os.environ.get(LOG_LEVEL_ENVIRONMENT, DEFAULT_LOG_LEVEL).upper()
    if log_level not in VALID_LEVELS:
        log_level = DEFAULT_LOG_LEVEL
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>EnvironmentVariables</key><dict><key>{LOG_LEVEL_ENVIRONMENT}</key><string>{log_level}</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer><key>StandardOutPath</key><string>{logs / "dashboard.out.log"}</string><key>StandardErrorPath</key><string>{logs / "dashboard.err.log"}</string></dict></plist>',
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
    if args.command == "run":
        run(repo, port=args.port)
        return 0
    if args.command == "install":
        agent = launch_agent(repo)
        LaunchdProvider().install(LABEL, agent)
        return 0
    if args.command == "uninstall":
        LaunchdProvider().uninstall(agent)
        agent.unlink(missing_ok=True)
        return 0
    health = (repo / ".djconnect" / "status" / "status.json").is_file()
    remote_provider = TailscaleProvider()
    remote = remote_provider.status()
    tailscale_address = remote_provider.ipv4_address()
    state = "READY" if health and agent.is_file() and tailscale_address else "DEGRADED"
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
