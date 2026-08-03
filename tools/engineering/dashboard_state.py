"""Read-only state projections used by the Engineering Status dashboard.

This module deliberately has no HTTP concerns.  The dashboard façade supplies
the small repository-specific readers, which keeps state composition testable
without giving it lifecycle or transaction authority.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from .host_preflight import latest as latest_host_preflight
from .workspace_preflight import latest as latest_workspace_preflight
from .capability_preflight import latest as latest_capability_preflight
from .platform_api import PlatformConfigurationError, execution_host_configuration


JsonReader = Callable[[Path], bytes]
RunJsonReader = Callable[[Path, str | None], bytes]


def unavailable_status() -> bytes:
    """Return the complete, safe status shape when no projection exists yet."""
    return json.dumps(
        {
            "watcher_state": "REMOTE_ENGINEERING_DEGRADED",
            "current_phase": "status niet beschikbaar",
            "current_action": "Voer het Engineering Platform uit om een statusupdate te publiceren.",
            "run_id": None,
            "queue_depth": 0,
            "queue_items": [],
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


def status(root: Path) -> bytes:
    """Project watcher and live-run state into the stable dashboard contract."""
    try:
        watcher = json.loads((root / ".engineering" / "status" / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        watcher = {}
    try:
        live = json.loads((root / ".engineering" / "status" / "current.json").read_text(encoding="utf-8"))
        projection = json.dumps(
            {
                "watcher_state": "ENGINEERING_RUN_ACTIVE",
                "current_phase": live.get("phase") or "INITIALIZE",
                "current_action": live.get("current_action") or "Engineeringuitvoering is actief.",
                "run_id": live.get("run_id"),
                "queue_depth": 0,
                "queue_items": watcher.get("queue_items", []),
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
        return (root / ".engineering" / "status" / "status.json").read_bytes()
    except OSError:
        return projection or unavailable_status()


def sse_status(root: Path) -> bytes:
    """Encode the status as a single SSE data line."""
    try:
        payload = json.loads(status(root))
    except json.JSONDecodeError:
        payload = json.loads(unavailable_status())
    return json.dumps(payload, separators=(",", ":")).encode()


def snapshot(
    root: Path,
    *,
    status_reader: JsonReader,
    unavailable_reader: Callable[[], bytes],
    prompt_started_reader: JsonReader,
    usage_reader: JsonReader,
    rate_limits_reader: Callable[[], bytes],
    usage_for_run_reader: RunJsonReader,
    completion_commits_reader: JsonReader,
    last_executed_commits_reader: JsonReader,
    reviewer_agents_reader: RunJsonReader,
    execution_reader: RunJsonReader,
    runtime_metadata_reader: RunJsonReader,
    report_analysis_available_reader: Callable[[Path, str | None], bool],
    telemetry_reader: Callable[[Path], list[dict[str, Any]]],
    process_metrics_reader: Callable[[], bytes],
    build_commit_reader: Callable[[Path], str],
    component_log_versions_reader: Callable[[Path], dict[str, str]],
    dashboard_version: str,
    worker_version: str,
) -> bytes:
    """Compose one complete read-only dashboard snapshot from small readers."""
    def read_json(reader: Callable[..., bytes], *args: object, fallback: Any) -> Any:
        try:
            return json.loads(reader(*args))
        except json.JSONDecodeError:
            return fallback

    status_payload = read_json(status_reader, root, fallback=None)
    if not isinstance(status_payload, dict):
        status_payload = read_json(unavailable_reader, fallback={})
    run_id = status_payload.get("last_executed_run")
    active = status_payload.get("watcher_state") == "ENGINEERING_RUN_ACTIVE" and isinstance(
        status_payload.get("run_id"), str
    )
    try:
        telemetry = telemetry_reader(root)
    except Exception:
        telemetry = []
    try:
        identity = execution_host_configuration(root).resolve_execution_host_identity()
        execution_host = {
            "name": identity.name,
            "version": identity.version,
            "runtime": identity.runtime,
            "runtime_prompt_transport": identity.runtime_prompt_transport,
        }
    except PlatformConfigurationError:
        execution_host = {}
    return json.dumps(
        {
            "status": status_payload,
            "build_commit": build_commit_reader(root),
            "prompt_started": read_json(prompt_started_reader, root, fallback={}),
            "usage": read_json(usage_reader, root, fallback={}),
            "rate_limits": read_json(rate_limits_reader, fallback={}),
            "last_executed_usage": read_json(usage_for_run_reader, root, run_id, fallback={}),
            "completion_commits": read_json(completion_commits_reader, root, fallback={}),
            "last_executed_commits": read_json(last_executed_commits_reader, root, fallback={}),
            "last_executed_reviewer_agents": read_json(reviewer_agents_reader, root, run_id, fallback=[]),
            "last_executed_execution": read_json(execution_reader, root, run_id, fallback={}),
            "last_executed_runtime_metadata": read_json(runtime_metadata_reader, root, run_id, fallback={}),
            "last_executed_report_analysis_available": report_analysis_available_reader(root, run_id),
            "telemetry": telemetry,
            "process_metrics": read_json(process_metrics_reader, fallback={}) if active else {},
            "component_log_versions": component_log_versions_reader(root),
            "component_versions": {"dashboard": dashboard_version, "worker": worker_version},
            "host_preflight": latest_host_preflight(root),
            "workspace_preflight": latest_workspace_preflight(root),
            "capability_preflight": latest_capability_preflight(root),
            "execution_host": execution_host,
        },
        separators=(",", ":"),
    ).encode()
