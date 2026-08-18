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
from .drift_diagnostics import guidance as drift_guidance
from .platform_api import PlatformConfigurationError, execution_host_configuration
from .telemetry import comparable_duration_estimate
from .storage import EngineeringStorageError, import_legacy_projection_once, is_active_blocking_predecessor, load_execution_context_snapshot, load_forge_governance_handoff_snapshot, load_projection, load_readiness_evaluation, open_storage
from .execution_lease import liveness as lease_liveness
from .execution_lifecycle import projection as lifecycle_projection
from .agent_state import redact_diagnostic


JsonReader = Callable[[Path], bytes]
RunJsonReader = Callable[[Path, str | None], bytes]
TERMINAL_PHASES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


def _active_prompt_metadata(root: Path, run_id: object) -> tuple[str | None, str | None]:
    """Return bounded title-only prompt context for the active run.

    The watcher normally owns these fields. A restart can leave an older run
    with only its durable transaction checkpoint, so recover just the first
    Markdown H1 and filename for the operator hand-off. Never expose prompt
    body text through the status projection.
    """
    if not isinstance(run_id, str) or not run_id:
        return None, None
    try:
        connection = open_storage(root, create=False)
        try:
            row = connection.execute(
                "SELECT payload FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        checkpoint = json.loads(row[0]) if row and isinstance(row[0], str) else {}
        prompt_path = checkpoint.get("prompt_path") if isinstance(checkpoint, dict) else None
        if not isinstance(prompt_path, str) or not prompt_path:
            return None, None
        path = Path(prompt_path)
        filename = redact_diagnostic(path.name, limit=240) or None
        with path.open(encoding="utf-8") as prompt:
            for _ in range(512):
                line = prompt.readline()
                if not line:
                    break
                if line.startswith("# ") and line[2:].strip():
                    return filename, redact_diagnostic(line[2:].strip(), limit=240) or filename
        return filename, filename
    except (EngineeringStorageError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None


def _transient_live_action(root: Path, run_id: object) -> str | None:
    """Read the non-persistent current action title for its owning live run."""
    if not isinstance(run_id, str):
        return None
    try:
        current = json.loads((root / ".engineering" / "status" / "current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    action = current.get("transient_action") if current.get("run_id") == run_id else None
    return action if isinstance(action, str) and 4 <= len(action) <= 160 else None


def _terminal_checkpoint(root: Path, run_id: object) -> bool:
    """Return whether a live-status run has already reached a terminal checkpoint.

    ``current.json`` is written by the runner and can briefly outlive its terminal
    checkpoint.  It must therefore never keep a completed execution visible as
    an active dashboard prompt.
    """
    if not isinstance(run_id, str):
        return False
    try:
        connection = open_storage(root)
        try:
            row = connection.execute(
                "SELECT phase FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
    except EngineeringStorageError:
        return False
    if row:
        return row[0] in TERMINAL_PHASES
    # Narrow compatibility window for a terminal pre-v12 runner that did not
    # contain enough fields to be promoted during the one-time migration.
    try:
        checkpoint = json.loads(
            (root / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(checkpoint, dict) and checkpoint.get("phase") in TERMINAL_PHASES


def _watcher_has_terminal_run(watcher: object, run_id: object) -> bool:
    """Return whether the watcher has already closed the live run."""
    return (
        isinstance(watcher, dict)
        and isinstance(run_id, str)
        and watcher.get("last_executed_run") == run_id
        and watcher.get("last_executed_phase") in TERMINAL_PHASES
    )


def _is_operator_merge_wait(live: object, lifecycle: object) -> bool:
    """Keep the durable PR hand-off visible during internal check polling.

    ``WAIT_FOR_TERMINAL_EVIDENCE`` is an implementation detail used while an
    open implementation PR is polled.  Its lifecycle projection deliberately
    presents that interval as ``WAIT_FOR_OPERATOR_MERGE``.  The dashboard
    status must use the same presentation phase even when a short-lived live
    lease wins over the watcher projection; otherwise the operator's PR
    controls flicker away between polling updates.
    """
    if not isinstance(live, dict) or not isinstance(lifecycle, dict):
        return False
    phase = live.get("phase")
    if phase == "WAIT_FOR_OPERATOR_MERGE":
        return True
    return (
        phase == "WAIT_FOR_TERMINAL_EVIDENCE"
        and lifecycle.get("current_step") == "WAIT_FOR_OPERATOR_MERGE"
        and isinstance(live.get("pull_request"), int)
        and not isinstance(live.get("pull_request"), bool)
        and live["pull_request"] > 0
    )


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
        watcher = load_projection(root, "watcher_status")
        live = load_projection(root, "live_status")
        # One explicit compatibility migration supports upgraded hosts where
        # status files predate the canonical store. It is not a normal read
        # path once the row has been imported.
        if watcher is None:
            watcher = import_legacy_projection_once(
                root, "watcher_status", root / ".engineering" / "status" / "status.json"
            )
        if live is None:
            live = import_legacy_projection_once(
                root, "live_status", root / ".engineering" / "status" / "current.json"
            )
        watcher = watcher or {}
        # The dashboard is a read model, but it must not repeat a stale
        # compatibility projection after SQLite has recorded dismissal.  The
        # watcher persists the same reconciliation on its next publication.
        predecessor_run = watcher.get("blocking_predecessor_run")
        if (
            watcher.get("watcher_state") == "WAITING_FOR_PREDECESSOR"
            and isinstance(predecessor_run, str)
            and predecessor_run
            and not is_active_blocking_predecessor(
                root, predecessor_run, watcher.get("blocking_predecessor_phase"),
            )
        ):
            watcher = {
                **watcher,
                "watcher_state": "WATCHER_IDLE",
                "current_phase": None,
                "current_action": "Execution Host Idle",
                "blocking_predecessor_run": None,
                "blocking_predecessor_phase": None,
                "blocking_predecessor_filename": None,
                "blocking_predecessor_title": None,
                "predecessor_recovery_action": None,
            }
    except EngineeringStorageError:
        watcher = {}
        live = None
    try:
        if live is None:
            raise ValueError("No canonical live status")
        live_liveness = lease_liveness(root, live.get("run_id"))
        transient_action = _transient_live_action(root, live.get("run_id"))
        lifecycle = lifecycle_projection(root, live.get("run_id"))
        fallback_filename, fallback_prompt_title = _active_prompt_metadata(root, live.get("run_id"))
        projection = json.dumps(
            {
                "watcher_state": "ENGINEERING_RUN_ACTIVE",
                "platform_version": watcher.get("platform_version"),
                "current_phase": live.get("phase") or "INITIALIZE",
                "current_action": transient_action or live.get("current_action") or "Engineeringuitvoering is actief.",
                "run_id": live.get("run_id"),
                # The watcher owns the queue. A live runner only adds current
                # execution details, so it must not replace the queued count.
                "queue_depth": watcher.get("queue_depth", len(watcher.get("queue_items", []))),
                "queue_items": watcher.get("queue_items", []),
                "implementation_pr": live.get("implementation_pr"),
                "finalization_pr": live.get("finalization_pr"),
                "pull_request": live.get("pull_request"),
                "waiting_for_merge_since": live.get("waiting_for_merge_since"),
                "repository_state": live.get("repository_state") or "ACTIVE",
                "workspace_state": live.get("workspace_state") or "ACTIVE",
                "prompt_characters": live.get("prompt_characters"),
                "diagnostic": live.get("diagnostic"),
                "submitted_filename": watcher.get("submitted_filename") or fallback_filename,
                "prompt_title": watcher.get("prompt_title") or fallback_prompt_title,
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
                "reviewer_agents": live.get("reviewer_agents", []),
                "runtime_metadata": live.get("runtime_metadata", {}),
                "workspace_progress": live.get("workspace_progress"),
                "execution_liveness": live_liveness,
                "readiness": load_readiness_evaluation(root, live.get("run_id")),
                # The dashboard consumes only the immutable context snapshot
                # linked to this run; legacy/current prompt projections never
                # become an Execution Context source.
                "execution_context": load_execution_context_snapshot(root, str(live.get("run_id"))),
                "forge_governance_handoff": load_forge_governance_handoff_snapshot(root, str(live.get("run_id"))),
                "lifecycle": lifecycle,
            },
            separators=(",", ":"),
        ).encode()
    except (ValueError, TypeError):
        live, projection, lifecycle = None, None, None
    if (
        live
        and _is_operator_merge_wait(live, lifecycle)
        and not _terminal_checkpoint(root, live.get("run_id"))
    ):
        waiting_projection = json.loads(projection or b"{}")
        waiting_projection.update(
            {
                "watcher_state": "WAITING_FOR_OPERATOR_MERGE",
                "current_phase": "WAIT_FOR_OPERATOR_MERGE",
                "current_action": "Waiting for the operator to merge the pull request.",
                "pull_request": live.get("pull_request"),
                "waiting_for_merge_since": live.get("waiting_for_merge_since"),
            }
        )
        return json.dumps(waiting_projection, separators=(",", ":")).encode()
    if (
        live
        and live.get("phase") not in TERMINAL_PHASES
        and live_liveness.get("state") != "LIVE"
        and (
            not watcher
            or (
                watcher.get("run_id") == live.get("run_id")
                and watcher.get("watcher_state") in {"JOB_CLAIMED", "RUNNER_STARTING", "REPORT_PUBLISHING", "ENGINEERING_RUN_ACTIVE"}
            )
        )
    ):
        # Lifecycle is intentionally retained for auditability, but a stale
        # lease must never be presented as an actively running execution.
        stale_projection = json.loads(projection or b"{}")
        reconciliation = live_liveness.get("reconciliation_outcome")
        recovery = (
            "RESUME_AVAILABLE"
            if reconciliation == "RECOVERABLE"
            else "TERMINAL_EVIDENCE_RECONCILIATION"
            if reconciliation == "TERMINAL_EVIDENCE_PRESENT"
            else "OPERATOR_INTERVENTION_REQUIRED"
        )
        stale_projection.update(
            {
                "watcher_state": "ENGINEERING_RUN_STALE",
                "current_action": "Execution Host ownership is stale; no execution is currently running.",
                "recovery_action": recovery,
            }
        )
        return json.dumps(stale_projection, separators=(",", ":")).encode()
    if (
        live
        and live.get("phase") not in TERMINAL_PHASES
        and live_liveness.get("state") == "LIVE"
        and not _terminal_checkpoint(root, live.get("run_id"))
        and not _watcher_has_terminal_run(watcher, live.get("run_id"))
    ):
        # A watcher can already be idle after it has detached a runner.  A
        # confirmed live lease is authoritative for the active execution and
        # must not be hidden by that older watcher projection.
        return projection
    if watcher:
        # A queued prompt can be held before it receives its own run ID when
        # its predecessor is terminally blocked.  Keep that predecessor's
        # persisted lifecycle visible in the operational card so the operator
        # can see the exact blocking flow, without misrepresenting it as a
        # live execution of the queued prompt.
        predecessor_run = watcher.get("blocking_predecessor_run")
        if (
            watcher.get("watcher_state") == "WAITING_FOR_PREDECESSOR"
            and isinstance(predecessor_run, str)
            and predecessor_run
        ):
            watcher = dict(watcher)
            lifecycle = dict(lifecycle_projection(root, predecessor_run))
            # Recovery belongs to the predecessor detail view. The queue wait
            # card only presents its immutable lifecycle evidence.
            lifecycle["recovery"] = None
            watcher["lifecycle"] = lifecycle
        return json.dumps(watcher, separators=(",", ":")).encode()
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
    active_run_id = status_payload.get("run_id")
    active_liveness = lease_liveness(root, active_run_id)
    active = (
        status_payload.get("watcher_state") == "ENGINEERING_RUN_ACTIVE"
        and isinstance(active_run_id, str)
        and active_liveness.get("state") == "LIVE"
    )
    try:
        telemetry = telemetry_reader(root)
    except Exception:
        telemetry = []
    try:
        duration_estimate = (
            comparable_duration_estimate(
                root,
                prompt_characters=status_payload.get("prompt_characters"),
                runtime_metadata=status_payload.get("runtime_metadata"),
                run_id=active_run_id,
                current_phase=status_payload.get("current_phase"),
                execution_mode=status_payload.get("execution_mode"),
            )
            if active
            else {}
        )
    except Exception:
        duration_estimate = {}
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
    host_preflight = latest_host_preflight(root)
    workspace_preflight = latest_workspace_preflight(root)
    capability_preflight = latest_capability_preflight(root)
    current_drift = next((item for preflight in (host_preflight, workspace_preflight, capability_preflight)
                          for item in preflight.get("drift_evidence", []) if isinstance(item, dict)), None)
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
            "duration_estimate": duration_estimate,
            "process_metrics": read_json(process_metrics_reader, root, fallback={}) if active else {},
            "component_log_versions": component_log_versions_reader(root),
            "component_versions": {"dashboard": dashboard_version, "worker": worker_version},
            "host_preflight": host_preflight,
            "workspace_preflight": workspace_preflight,
            "capability_preflight": capability_preflight,
            "current_drift": current_drift or {},
            "resume_guidance": drift_guidance([current_drift] if current_drift else []),
            "execution_host": execution_host,
        },
        separators=(",", ":"),
    ).encode()
