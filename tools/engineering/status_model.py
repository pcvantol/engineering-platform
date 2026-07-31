"""Canonical, bounded engineering-status model and atomic projections."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .agent_state import redact_diagnostic
from .platform_version import EngineeringPlatformManifest

SCHEMA_VERSION = 1


def build(manifest: EngineeringPlatformManifest, **values: object) -> dict[str, object]:
    """Build the sole status payload; callers cannot publish arbitrary content."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "platform_version": manifest.platform_version,
        "watcher_version": manifest.watcher_version,
        "dashboard_version": manifest.dashboard_version,
        "inbox_protocol": manifest.inbox_protocol,
        "watcher_state": "WATCHER_IDLE",
        "current_phase": None,
        "current_action": None,
        "run_id": None,
        "job_id": None,
        "submitted_filename": None,
        "received_timestamp": None,
        "elapsed_seconds": 0,
        "queue_depth": 0,
        "repair_iteration": 0,
        "implementation_pr": None,
        "finalization_pr": None,
        "implementation_state": None,
        "finalization_state": None,
        "validation_summary": None,
        "repository_state": "MERGED_RECONCILED",
        "workspace_state": "WORKSPACE_READY",
        "owner_authorized": False,
        "resume_available": False,
        "latest_report": None,
        "latest_completed_run": None,
        "diagnostic": None,
        "last_update": datetime.now(timezone.utc).isoformat(),
    }
    payload.update({key: value for key, value in values.items() if key in payload})
    payload["diagnostic"] = (
        redact_diagnostic(str(payload["diagnostic"])) if payload["diagnostic"] else None
    )
    return payload


def publish(root: Path, payload: dict[str, object]) -> None:
    """Atomically publish synchronized JSON and compact iPhone-readable Markdown."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    markdown = "\n".join(
        (
            "# DJConnect Engineering",
            "",
            "## Current State",
            f"`{payload['watcher_state']}` — `{payload['current_phase'] or 'idle'}`",
            "",
            "## Active Job",
            f"Run: `{payload['run_id'] or 'none'}`  ",
            f"Queue: `{payload['queue_depth']}`",
            "",
            "## Pull Requests",
            f"Implementation: `{payload['implementation_pr'] or 'none'}`  ",
            f"Finalization: `{payload['finalization_pr'] or 'none'}`",
            "",
            "## Repository",
            f"Repository: `{payload['repository_state']}`  ",
            f"Workspace: `{payload['workspace_state']}`",
            "",
            "## Authorization",
            f"Owner-authorized: `{payload['owner_authorized']}`; release and deployment authority: `absent`",
            "",
            "## Latest Report",
            f"`{payload['latest_report'] or 'none'}`",
            "",
            "## Diagnostic",
            str(payload["diagnostic"] or "none"),
            "",
        )
    )
    for name, content in (
        ("status.json", json.dumps(payload, indent=2, sort_keys=True) + "\n"),
        ("status.md", markdown),
    ):
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / name)
