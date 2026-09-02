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
import sqlite3
from time import monotonic
from typing import Mapping
import uuid

from .storage import EngineeringStorageError, open_storage


PHASES = frozenset({
    "QUEUE_WAIT", "SUBMISSION_CLAIM", "INITIALIZATION", "HOST_PREFLIGHT",
    "WORKSPACE_PREFLIGHT", "CAPABILITY_PREFLIGHT", "DETERMINISTIC_ADMISSION", "CAPABILITY_REVIEW", "EXECUTION_PREPARATION",
    "PROVIDER_EXECUTION", "VALIDATION", "QUALITY_CONTROL", "REPAIR", "REPOSITORY_FINALIZATION",
    "PR_OR_MERGE", "FINALIZATION", "REPORT_GENERATION", "EVIDENCE_PERSISTENCE",
    "REPOSITORY_CLEANUP", "RECONCILIATION", "EXTERNAL_CI_WAIT", "TOTAL_EXECUTION",
})
TERMINAL_OUTCOMES = frozenset({"COMPLETE", "FAILED", "INTERRUPTED", "STALE"})
_MAX_METADATA_BYTES = 2048


def _connection(root: Path, central_database: Path | None = None) -> sqlite3.Connection:
    """Open timing storage from an explicit CENTRAL binding when supplied."""
    if central_database is None:
        return open_storage(root)
    database = central_database.resolve()
    if not database.is_file():
        raise EngineeringStorageError("CENTRAL timing database is unavailable.")
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


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
    central_database: Path | None = None


def start_phase(root: Path, run_id: str, phase_name: str, *, category: str | None = None,
                parent_phase_id: str | None = None, attempt: int = 1,
                metadata: Mapping[str, object] | None = None, started_at: datetime | None = None,
                monotonic_clock: float | None = None, central_database: Path | None = None) -> ActivePhase:
    """Persist a real active phase boundary and return its monotonic handle."""
    if phase_name not in PHASES or not run_id or attempt < 1:
        raise EngineeringStorageError("Execution phase identity is invalid.")
    phase_id = f"phase-{uuid.uuid4()}"
    connection = _connection(root, central_database)
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
    return ActivePhase(run_id, phase_id, monotonic() if monotonic_clock is None else monotonic_clock, central_database)


def complete_phase(root: Path, active: ActivePhase, *, outcome: str = "COMPLETE",
                   completed_at: datetime | None = None, monotonic_clock: float | None = None) -> None:
    """Close an active span with its directly measured monotonic duration."""
    if outcome not in TERMINAL_OUTCOMES:
        raise EngineeringStorageError("Execution phase outcome is invalid.")
    connection = _connection(root, active.central_database)
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
    central_database = kwargs.get("central_database")
    if central_database is not None and not isinstance(central_database, Path):
        raise EngineeringStorageError("CENTRAL timing database is invalid.")
    connection = _connection(root, central_database)
    try:
        row = connection.execute(
            "SELECT phase_id FROM execution_phase_spans WHERE run_id=? AND phase_name=? AND outcome='ACTIVE' ORDER BY ordinal LIMIT 1",
            (run_id, phase_name),
        ).fetchone()
    finally:
        connection.close()
    if row:
        return ActivePhase(run_id, str(row[0]), None, central_database)
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
                                 completed_at: datetime | None = None,
                                 central_database: Path | None = None) -> int:
    """Close observable abandoned work at reconciliation, never backdating it."""
    if outcome not in {"STALE", "INTERRUPTED"}:
        raise EngineeringStorageError("Interrupted phase outcome is invalid.")
    connection = _connection(root, central_database)
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


def complete_active_phase(
    root: Path, run_id: str, phase_name: str, *, outcome: str = "COMPLETE",
    central_database: Path | None = None,
) -> bool:
    """Close the one active lifecycle envelope when its owner observes completion.

    Queue admission, the runner and the watcher are separate processes.  The
    watcher therefore owns the final total envelope boundary, after report and
    evidence persistence have completed.  This intentionally uses the stored
    UTC boundary rather than claiming a monotonic clock crosses processes.
    """
    connection = _connection(root, central_database)
    try:
        row = connection.execute(
            "SELECT phase_id FROM execution_phase_spans WHERE run_id=? AND phase_name=? AND outcome='ACTIVE' ORDER BY ordinal LIMIT 1",
            (run_id, phase_name),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return False
    complete_phase(root, ActivePhase(run_id, str(row[0]), None, central_database), outcome=outcome)
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
    """Return the one canonical timing read model for a completed run.

    ``phase_aggregates`` and ``longest_individual_spans`` intentionally answer
    different questions.  Aggregates suppress only a same-category ancestor,
    so a category is represented once without inventing a critical path.
    Individual spans retain every observed occurrence (apart from the total
    envelope) and are ranked independently.  Consumers must use these fields
    rather than deriving their own bottleneck order.
    """
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

    def timestamp(span: dict[str, object], key: str) -> datetime | None:
        value = span.get(key)
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    total_envelopes = [
        (timestamp(span, "started_at"), timestamp(span, "completed_at"))
        for span in top_level
        if span["phase_name"] == "TOTAL_EXECUTION"
    ]
    total_envelopes = [
        (started, completed) for started, completed in total_envelopes
        if started is not None and completed is not None and completed >= started
    ]
    if total_envelopes:
        observed_total = round(sum((completed - started).total_seconds() * 1000 for started, completed in total_envelopes))
        # Unit and recovery paths can preserve monotonic durations while their
        # wall-clock timestamps are only boundary markers. Do not turn such
        # non-comparable timestamps into invented overlap evidence.
        if abs(observed_total - total) > 5_000:
            total_envelopes = []

    def envelope_overlap(span: dict[str, object]) -> int:
        """Return the measurable overlap with TOTAL_EXECUTION, never raw stale tail time."""
        duration = int(span["duration_ms"])
        if not total_envelopes:
            return min(duration, total)
        started, completed = timestamp(span, "started_at"), timestamp(span, "completed_at")
        if started is None or completed is None or completed < started:
            return min(duration, total)
        overlap = sum(
            max(0.0, (min(completed, envelope_end) - max(started, envelope_start)).total_seconds())
            for envelope_start, envelope_end in total_envelopes
        )
        return min(duration, total, round(overlap * 1000))

    def measured(name: str) -> int:
        return sum(int(span["duration_ms"]) for span in semantic if span["phase_name"] == name)

    provider = measured("PROVIDER_EXECUTION")
    validation = measured("VALIDATION")
    external = measured("EXTERNAL_CI_WAIT")
    queue = by_phase.get("QUEUE_WAIT", 0)
    report_generation = measured("REPORT_GENERATION")
    evidence_persistence = measured("EVIDENCE_PERSISTENCE")
    repository_finalization = measured("REPOSITORY_FINALIZATION")
    active = max(0, total - external)

    def has_processing_ancestor(span: dict[str, object]) -> bool:
        """Whether this work is already covered by an enclosing work span.

        Provider command-boundary validation is deliberately nested under the
        provider process.  It remains independently measurable, but cannot
        also consume a second portion of overhead.  This ancestry rule keeps
        the accounting partition deterministic even where the persisted UTC
        timestamps have the normal small clock-resolution differences from
        monotonic duration measurement.
        """
        parent = span["parent_phase_id"]
        while isinstance(parent, str) and parent in by_id:
            if by_id[parent]["phase_name"] in {"PROVIDER_EXECUTION", "VALIDATION"}:
                return True
            parent = by_id[parent]["parent_phase_id"]
        return False

    processing_coverage = sum(
        envelope_overlap(span)
        for span in semantic
        if span["phase_name"] in {"PROVIDER_EXECUTION", "VALIDATION"}
        and not has_processing_ancestor(span)
    )
    overhead = max(0, active - processing_coverage)
    # Category aggregates use the same semantic selection as the named
    # metrics.  The deterministic category-name tie break keeps reports, API
    # projections and dashboard detail identical.
    aggregate_by_phase: dict[str, int] = {}
    share_by_phase: dict[str, int] = {}
    for span in semantic:
        name = str(span["phase_name"])
        if name != "TOTAL_EXECUTION":
            aggregate_by_phase[name] = aggregate_by_phase.get(name, 0) + int(span["duration_ms"])
            share_by_phase[name] = min(total, share_by_phase.get(name, 0) + envelope_overlap(span))
    phase_aggregates = [
        {"phase": phase, "duration_ms": duration}
        for phase, duration in sorted(aggregate_by_phase.items(), key=lambda item: (-item[1], item[0]))
    ]

    def span_label(span: dict[str, object]) -> str:
        """Give repeated spans bounded, typed context without prompt content."""
        name = str(span["phase_name"])
        metadata = span.get("metadata")
        context: str | None = None
        if isinstance(metadata, dict):
            for key in ("validation_kind", "operation", "reason"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    context = value.replace("_", " ")[:80]
                    break
            if context is None and isinstance(metadata.get("iteration"), int):
                context = f"iteration {metadata['iteration']}"
        attempt = span.get("attempt")
        if context:
            return f"{name} — {context}"
        if isinstance(attempt, int) and attempt > 1:
            return f"{name} — attempt {attempt}"
        return name

    longest_individual_spans = [
        {
            "phase_id": item["phase_id"], "phase": item["phase_name"], "label": span_label(item),
            "duration_ms": item["duration_ms"], "attempt": item["attempt"],
            "ordinal": item["ordinal"], "outcome": item["outcome"],
        }
        for item in sorted(
            (span for span in completed if span["phase_name"] != "TOTAL_EXECUTION"),
            key=lambda item: (-int(item["duration_ms"]), str(item["phase_name"]), int(item["ordinal"])),
        )
    ]
    def share(value: int) -> float:
        return round(value * 100 / total, 3) if total else 0.0
    return {"phase_durations_ms": by_phase, "total_wall_time_ms": total,
            "occurred_phases": tuple(sorted({str(span["phase_name"]) for span in completed})),
            "active_ep_processing_time_ms": active, "provider_execution_time_ms": provider,
            "validation_time_ms": validation, "external_wait_time_ms": external,
            "queue_wait_time_ms": queue, "report_generation_time_ms": report_generation,
            "evidence_persistence_time_ms": evidence_persistence,
            "repository_finalization_time_ms": repository_finalization, "overhead_time_ms": overhead,
            "provider_share_percent": share(share_by_phase.get("PROVIDER_EXECUTION", 0)), "validation_share_percent": share(share_by_phase.get("VALIDATION", 0)),
            "external_wait_share_percent": share(share_by_phase.get("EXTERNAL_CI_WAIT", 0)), "queue_share_percent": share(share_by_phase.get("QUEUE_WAIT", 0)),
            "overhead_share_percent": share(overhead),
            "longest_phase": phase_aggregates[0]["phase"] if phase_aggregates else None,
            "longest_phase_duration_ms": phase_aggregates[0]["duration_ms"] if phase_aggregates else None,
            "phase_aggregates": phase_aggregates,
            "phase_share_durations_ms": share_by_phase,
            "top_phase_categories": phase_aggregates[:3],
            "longest_individual_spans": longest_individual_spans[:3],
            # Compatibility alias for pre-reconciliation API clients.  It is
            # intentionally category-only and no longer mixes individual spans.
            "top_time_consumers": phase_aggregates[:3],
            "phase_telemetry_available": bool(spans),
            "historical_total_available": historical_total is not None}
