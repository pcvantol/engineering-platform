"""Canonical Engineering Platform execution phase timing evidence.

This module deliberately owns measurement, persistence and deterministic
projection only.  It has no Mission, Producer, retry or lifecycle authority.
Wall-clock UTC timestamps describe when a span occurred; a monotonic duration
is captured at its runtime boundary.  Repeated and nested spans stay separate
in SQLite.  Aggregates use only top-level spans, so nested work is never
double-counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic
from typing import Mapping
import uuid

from .storage import EngineeringStorageError, open_storage


PHASES = frozenset({
    "QUEUE_WAIT", "SUBMISSION_CLAIM", "INITIALIZATION", "HOST_PREFLIGHT",
    "WORKSPACE_PREFLIGHT", "CAPABILITY_PREFLIGHT", "EXECUTION_PREPARATION",
    "PROVIDER_EXECUTION", "VALIDATION", "REPAIR", "REPOSITORY_FINALIZATION",
    "PR_OR_MERGE", "FINALIZATION", "REPORT_GENERATION", "EVIDENCE_PERSISTENCE",
    "REPOSITORY_CLEANUP", "RECONCILIATION", "EXTERNAL_CI_WAIT", "TOTAL_EXECUTION",
})
TERMINAL_OUTCOMES = frozenset({"COMPLETE", "FAILED", "INTERRUPTED", "STALE"})
_MAX_METADATA_BYTES = 2048


def _utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return (value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _metadata(value: Mapping[str, object] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise EngineeringStorageError("Execution phase metadata must be a mapping.")
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > _MAX_METADATA_BYTES:
        raise EngineeringStorageError("Execution phase metadata exceeds its bounded limit.")
    return encoded


@dataclass(frozen=True)
class ActivePhase:
    run_id: str
    phase_id: str
    started_monotonic: float | None


def start_phase(root: Path, run_id: str, phase_name: str, *, category: str | None = None,
                parent_phase_id: str | None = None, attempt: int = 1,
                metadata: Mapping[str, object] | None = None, started_at: datetime | None = None,
                monotonic_clock: float | None = None) -> ActivePhase:
    """Persist a real active phase boundary and return its monotonic handle."""
    if phase_name not in PHASES or not run_id or attempt < 1:
        raise EngineeringStorageError("Execution phase identity is invalid.")
    phase_id = f"phase-{uuid.uuid4()}"
    connection = open_storage(root)
    try:
        ordinal = connection.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM execution_phase_spans WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO execution_phase_spans(phase_id,run_id,phase_name,phase_category,parent_phase_id,attempt,ordinal,started_at,outcome,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (phase_id, run_id, phase_name, category or phase_name, parent_phase_id, attempt, ordinal,
             _utc(started_at), "ACTIVE", _metadata(metadata)),
        )
    finally:
        connection.close()
    return ActivePhase(run_id, phase_id, monotonic() if monotonic_clock is None else monotonic_clock)


def complete_phase(root: Path, active: ActivePhase, *, outcome: str = "COMPLETE",
                   completed_at: datetime | None = None, monotonic_clock: float | None = None) -> None:
    """Close an active span with its directly measured monotonic duration."""
    if outcome not in TERMINAL_OUTCOMES:
        raise EngineeringStorageError("Execution phase outcome is invalid.")
    connection = open_storage(root)
    try:
        if active.started_monotonic is None:
            # A phase can outlive the runner process.  Its terminal boundary
            # is still observable, but monotonic state is intentionally not
            # fabricated across that restart.
            row = connection.execute(
                "SELECT started_at FROM execution_phase_spans WHERE phase_id=? AND run_id=? AND outcome='ACTIVE'",
                (active.phase_id, active.run_id),
            ).fetchone()
            if row is None:
                raise EngineeringStorageError("Execution phase is not active.")
            started = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            ended = completed_at or datetime.now(timezone.utc)
            duration_ms = max(0, round((ended - started).total_seconds() * 1000))
        else:
            elapsed = (monotonic() if monotonic_clock is None else monotonic_clock) - active.started_monotonic
            duration_ms = max(0, round(elapsed * 1000))
        changed = connection.execute(
            "UPDATE execution_phase_spans SET completed_at=?,duration_ms=?,outcome=? WHERE phase_id=? AND run_id=? AND outcome='ACTIVE'",
            (_utc(completed_at), duration_ms, outcome, active.phase_id, active.run_id),
        ).rowcount
        if changed != 1:
            raise EngineeringStorageError("Execution phase is not active.")
    finally:
        connection.close()


def start_or_resume_phase(root: Path, run_id: str, phase_name: str, **kwargs: object) -> ActivePhase:
    """Start one envelope or resume its observed active database boundary.

    Only lifecycle envelopes use this.  The resumed handle deliberately has no
    monotonic value, so its completion is explicitly bounded by persisted UTC
    timestamps rather than pretending a monotonic clock survived restart.
    """
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT phase_id FROM execution_phase_spans WHERE run_id=? AND phase_name=? AND outcome='ACTIVE' ORDER BY ordinal LIMIT 1",
            (run_id, phase_name),
        ).fetchone()
    finally:
        connection.close()
    if row:
        return ActivePhase(run_id, str(row[0]), None)
    return start_phase(root, run_id, phase_name, **kwargs)


def record_phase(root: Path, run_id: str, phase_name: str, *, started_at: datetime,
                 completed_at: datetime, category: str | None = None,
                 parent_phase_id: str | None = None, attempt: int = 1,
                 metadata: Mapping[str, object] | None = None, outcome: str = "COMPLETE") -> str:
    """Record a completed observed boundary when no monotonic handle survives.

    This is reserved for cross-process boundaries such as queue submission to
    claim.  In-process work must use :func:`start_phase` and
    :func:`complete_phase` instead.
    """
    active = start_phase(root, run_id, phase_name, category=category, parent_phase_id=parent_phase_id,
                         attempt=attempt, metadata=metadata, started_at=started_at, monotonic_clock=0)
    elapsed = max(0, (completed_at - started_at).total_seconds())
    complete_phase(root, active, outcome=outcome, completed_at=completed_at, monotonic_clock=elapsed)
    return active.phase_id


def record_queue_wait_from_submission(root: Path, run_id: str, *, claimed_at: datetime | None = None) -> bool:
    """Record the persisted submission-to-claim delay when it is observable."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission.received_at FROM execution_submissions AS submission JOIN execution_submission_links AS link ON link.submission_id=submission.submission_id WHERE link.run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not isinstance(row[0], str):
        return False
    try:
        submitted = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    except ValueError:
        return False
    record_phase(root, run_id, "QUEUE_WAIT", started_at=submitted, completed_at=claimed_at or datetime.now(timezone.utc), category="QUEUE")
    return True


def reconcile_interrupted_phases(root: Path, run_id: str, *, outcome: str = "STALE",
                                 completed_at: datetime | None = None) -> int:
    """Close observable abandoned work at reconciliation, never backdating it."""
    if outcome not in {"STALE", "INTERRUPTED"}:
        raise EngineeringStorageError("Interrupted phase outcome is invalid.")
    connection = open_storage(root)
    try:
        now = _utc(completed_at)
        # Monotonic state cannot survive a process restart, so a reconciled
        # duration is intentionally wall-clock bounded and explicitly STALE.
        return connection.execute(
            "UPDATE execution_phase_spans SET completed_at=?,duration_ms=MAX(0,CAST((julianday(?) - julianday(started_at))*86400000 AS INTEGER)),outcome=? WHERE run_id=? AND outcome='ACTIVE'",
            (now, now, outcome, run_id),
        ).rowcount
    finally:
        connection.close()


def complete_active_phase(root: Path, run_id: str, phase_name: str, *, outcome: str = "COMPLETE") -> bool:
    """Close the one active lifecycle envelope when its owner observes completion.

    Queue admission, the runner and the watcher are separate processes.  The
    watcher therefore owns the final total envelope boundary, after report and
    evidence persistence have completed.  This intentionally uses the stored
    UTC boundary rather than claiming a monotonic clock crosses processes.
    """
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT phase_id FROM execution_phase_spans WHERE run_id=? AND phase_name=? AND outcome='ACTIVE' ORDER BY ordinal LIMIT 1",
            (run_id, phase_name),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return False
    complete_phase(root, ActivePhase(run_id, str(row[0]), None), outcome=outcome)
    return True


def phase_spans(root: Path, run_id: str) -> list[dict[str, object]]:
    connection = open_storage(root)
    try:
        rows = connection.execute(
            "SELECT phase_id,phase_name,phase_category,parent_phase_id,attempt,ordinal,started_at,completed_at,duration_ms,outcome,metadata FROM execution_phase_spans WHERE run_id=? ORDER BY ordinal", (run_id,)
        ).fetchall()
    finally:
        connection.close()
    keys = ("phase_id", "phase_name", "phase_category", "parent_phase_id", "attempt", "ordinal", "started_at", "completed_at", "duration_ms", "outcome", "metadata")
    result = []
    for row in rows:
        item = dict(zip(keys, row, strict=True))
        item["metadata"] = json.loads(item["metadata"])
        result.append(item)
    return result


def timing_summary(root: Path, run_id: str) -> dict[str, object]:
    """Project non-double-counted run metrics from completed canonical spans."""
    spans = phase_spans(root, run_id)
    historical_total: int | None = None
    if not spans:
        # Earlier runs already have a coarse immutable execution receipt.  It
        # remains useful total-duration evidence, but never becomes invented
        # phase detail.
        connection = open_storage(root)
        try:
            row = connection.execute(
                "SELECT total_execution_seconds FROM execution_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        if row and isinstance(row[0], (int, float)) and not isinstance(row[0], bool) and row[0] >= 0:
            historical_total = round(float(row[0]) * 1000)
    completed = [span for span in spans if span["outcome"] != "ACTIVE" and isinstance(span["duration_ms"], int)]
    top_level = [span for span in completed if span["parent_phase_id"] is None]
    by_id = {str(span["phase_id"]): span for span in completed}

    def has_same_phase_ancestor(span: dict[str, object]) -> bool:
        parent = span["parent_phase_id"]
        while isinstance(parent, str) and parent in by_id:
            ancestor = by_id[parent]
            if ancestor["phase_name"] == span["phase_name"]:
                return True
            parent = ancestor["parent_phase_id"]
        return False

    semantic = [span for span in completed if not has_same_phase_ancestor(span)]
    by_phase: dict[str, int] = {}
    for span in top_level:
        by_phase[span["phase_name"]] = by_phase.get(span["phase_name"], 0) + int(span["duration_ms"])
    total = by_phase.get("TOTAL_EXECUTION")
    if total is None:
        total = historical_total if historical_total is not None else sum(
            duration for name, duration in by_phase.items() if name != "QUEUE_WAIT"
        )
    def measured(name: str) -> int:
        return sum(int(span["duration_ms"]) for span in semantic if span["phase_name"] == name)

    provider = measured("PROVIDER_EXECUTION")
    validation = measured("VALIDATION")
    external = measured("EXTERNAL_CI_WAIT")
    queue = by_phase.get("QUEUE_WAIT", 0)
    active = max(0, total - external)
    overhead = max(0, active - provider - validation)
    # Critical-path consumers use only outermost measured work.  Semantic
    # measures below intentionally include a nested provider or validation
    # span, but ranking both it and its enclosing repair/preparation span
    # would present overlapping elapsed time as separate bottlenecks.
    consumers = sorted(
        (span for span in top_level if span["phase_name"] not in {"TOTAL_EXECUTION", "QUEUE_WAIT"}),
        key=lambda item: (-int(item["duration_ms"]), int(item["ordinal"])),
    )[:3]
    share = lambda value: round(value * 100 / total, 3) if total else 0.0
    return {"phase_durations_ms": by_phase, "total_wall_time_ms": total,
            "active_ep_processing_time_ms": active, "provider_execution_time_ms": provider,
            "validation_time_ms": validation, "external_wait_time_ms": external,
            "queue_wait_time_ms": queue, "overhead_time_ms": overhead,
            "provider_share_percent": share(provider), "validation_share_percent": share(validation),
            "external_wait_share_percent": share(external), "queue_share_percent": share(queue),
            "longest_phase": consumers[0]["phase_name"] if consumers else None,
            "longest_phase_duration_ms": consumers[0]["duration_ms"] if consumers else None,
            "top_time_consumers": [{"phase": item["phase_name"], "duration_ms": item["duration_ms"]} for item in consumers],
            "phase_telemetry_available": bool(spans),
            "historical_total_available": historical_total is not None}
