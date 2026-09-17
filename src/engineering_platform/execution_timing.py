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
from statistics import median
from time import monotonic
from typing import Mapping
import uuid

from .storage import EngineeringStorageError, open_storage
from .telemetry_metrics import TELEMETRY_CALCULATION_VERSION


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
        measurement_basis = "MONOTONIC"
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
            measurement_basis = "RECONCILED_WALL_CLOCK"
        else:
            elapsed = (monotonic() if monotonic_clock is None else monotonic_clock) - active.started_monotonic
            duration_ms = max(0, round(elapsed * 1000))
        changed = connection.execute(
            """UPDATE execution_phase_spans
                  SET completed_at=?,duration_ms=?,outcome=?,
                      metadata=json_set(metadata,'$.measurement_basis',?)
                WHERE phase_id=? AND run_id=? AND outcome='ACTIVE'""",
            (_utc(completed_at), duration_ms, outcome, measurement_basis, active.phase_id, active.run_id),
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
            "SELECT attempt.recorded_at FROM execution_submission_attempts AS attempt "
            "JOIN execution_submission_attempt_links AS link ON link.submission_id=attempt.submission_id "
            "WHERE link.run_id=?", (run_id,)
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
            """UPDATE execution_phase_spans
                  SET completed_at=?,
                      duration_ms=MAX(0,CAST((julianday(?) - julianday(started_at))*86400000 AS INTEGER)),
                      outcome=?,metadata=json_set(metadata,'$.measurement_basis','RECONCILED_WALL_CLOCK')
                WHERE run_id=? AND outcome='ACTIVE'""",
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


def phase_spans(root: Path, run_id: str, *, central_database: Path | None = None) -> list[dict[str, object]]:
    connection = _connection(root, central_database)
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


TIMING_CALCULATION_VERSION = TELEMETRY_CALCULATION_VERSION
TIMING_COMPLETE, TIMING_PARTIAL, TIMING_UNAVAILABLE, TIMING_CONFLICT = (
    "COMPLETE", "PARTIAL", "UNAVAILABLE", "CONFLICT"
)
_NO_HISTORICAL_TOTAL = object()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _union_duration(intervals: list[tuple[datetime, datetime]]) -> int:
    if not intervals:
        return 0
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return round(sum((end - start).total_seconds() * 1000 for start, end in merged))


def _duration_us(start: datetime, end: datetime) -> int:
    delta = end - start
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _rounded_category_milliseconds(values_us: Mapping[str, int]) -> dict[str, int]:
    """Round one microsecond partition without fabricating an UNASSIGNED rest."""
    if not values_us:
        return {}
    result = {key: value // 1000 for key, value in values_us.items()}
    target = round(sum(values_us.values()) / 1000)
    remainder = target - sum(result.values())
    ranked = sorted(values_us, key=lambda key: (-(values_us[key] % 1000), key))
    for key in ranked[:remainder]:
        result[key] += 1
    return result


def timing_summary(
    root: Path, run_id: str, *, central_database: Path | None = None,
    _spans: list[dict[str, object]] | None = None,
    _historical_total: object = _NO_HISTORICAL_TOTAL,
    timeline_limit: int | None = 500,
) -> dict[str, object]:
    """Return inclusive workload and an interval-derived elapsed-time partition.

    The inclusive projection retains measured span workload and may overlap.
    The exclusive projection sweeps the TOTAL_EXECUTION envelope.  A nested
    child owns its segment; simultaneous independent categories are assigned
    to ``PARALLEL_OVERLAP``; uncovered envelope time is ``UNASSIGNED``.
    """
    spans = phase_spans(root, run_id, central_database=central_database) if _spans is None else _spans
    historical_total: int | None = None
    if not spans:
        if _historical_total is _NO_HISTORICAL_TOTAL:
            connection = _connection(root, central_database)
            try:
                row = connection.execute(
                    "SELECT total_execution_seconds FROM execution_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                connection.close()
            candidate = row[0] if row else None
        else:
            candidate = _historical_total
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
            historical_total = round(float(candidate) * 1000)
        return {
            "contract_version": TIMING_CALCULATION_VERSION,
            "scope": "EP_RUN_ATTEMPT",
            "phase_telemetry_available": False,
            "historical_total_available": historical_total is not None,
            "total_wall_time_ms": historical_total,
            "coverage": {
                "state": TIMING_PARTIAL if historical_total is not None else TIMING_UNAVAILABLE,
                "reason": "Only the historical run total is retained" if historical_total is not None else "No timing observations are retained",
                "expected_observations": 1, "observed_observations": int(historical_total is not None),
            },
            "phase_durations_ms": {}, "phase_aggregates": [], "inclusive_phase_rows": [],
            "exclusive_distribution": [], "timeline": [], "longest_individual_spans": [],
            "top_phase_categories": [], "top_time_consumers": [],
        }

    completed = [
        span for span in spans
        if span.get("outcome") != "ACTIVE"
        and isinstance(span.get("duration_ms"), int)
        and int(span["duration_ms"]) >= 0
    ]
    by_id = {str(span["phase_id"]): span for span in completed}
    parent_conflicts: list[str] = []

    def ancestors(span: Mapping[str, object]) -> list[str]:
        result: list[str] = []
        parent = span.get("parent_phase_id")
        while isinstance(parent, str):
            if parent in result:
                parent_conflicts.append(f"cycle:{span['phase_id']}")
                break
            result.append(parent)
            if parent not in by_id:
                parent_conflicts.append(f"missing-parent:{span['phase_id']}")
                break
            parent = by_id[parent].get("parent_phase_id")
        return result

    ancestor_map = {str(span["phase_id"]): ancestors(span) for span in completed}

    def same_phase_ancestor(span: Mapping[str, object]) -> bool:
        return any(
            parent in by_id and by_id[parent].get("phase_name") == span.get("phase_name")
            for parent in ancestor_map[str(span["phase_id"])]
        )

    semantic = [span for span in completed if not same_phase_ancestor(span)]
    inclusive: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    phase_observations: dict[str, list[int]] = {}
    for span in semantic:
        name = str(span["phase_name"])
        if name == "TOTAL_EXECUTION":
            continue
        inclusive[name] = inclusive.get(name, 0) + int(span["duration_ms"])
        occurrences[name] = occurrences.get(name, 0) + 1
        phase_observations.setdefault(name, []).append(int(span["duration_ms"]))

    total_spans = [span for span in completed if span.get("phase_name") == "TOTAL_EXECUTION"]
    envelope: tuple[datetime, datetime] | None = None
    boundary_conflicts: list[str] = []
    total = None
    wall_clock_total = None
    exclusive_envelope_total = None
    if len(total_spans) == 1:
        start, end = _timestamp(total_spans[0].get("started_at")), _timestamp(total_spans[0].get("completed_at"))
        if start is not None and end is not None and end >= start:
            wall_duration = round((end - start).total_seconds() * 1000)
            wall_clock_total = wall_duration
            measured_duration = int(total_spans[0]["duration_ms"])
            if abs(wall_duration - measured_duration) <= max(250, round(measured_duration * .01)):
                envelope, total = (start, end), measured_duration
                exclusive_envelope_total = wall_duration
            else:
                boundary_conflicts.append("TOTAL_EXECUTION wall-clock and monotonic duration conflict")
                total = measured_duration
        else:
            boundary_conflicts.append("TOTAL_EXECUTION has missing or invalid UTC boundaries")
            total = int(total_spans[0]["duration_ms"])
    elif len(total_spans) > 1:
        boundary_conflicts.append("Multiple TOTAL_EXECUTION envelopes are not unambiguous")
        total = sum(int(span["duration_ms"]) for span in total_spans)
    else:
        boundary_conflicts.append("TOTAL_EXECUTION envelope is missing")

    interval_rows: list[tuple[datetime, datetime, dict[str, object]]] = []
    if envelope is not None:
        envelope_start, envelope_end = envelope
        for span in semantic:
            if span.get("phase_name") in {"TOTAL_EXECUTION", "QUEUE_WAIT"}:
                continue
            start, end = _timestamp(span.get("started_at")), _timestamp(span.get("completed_at"))
            if start is None or end is None or end < start:
                boundary_conflicts.append(f"Invalid boundaries for {span['phase_id']}")
                continue
            wall_duration = round((end - start).total_seconds() * 1000)
            measured_duration = int(span["duration_ms"])
            if abs(wall_duration - measured_duration) > max(250, round(measured_duration * .02)):
                boundary_conflicts.append(f"Wall-clock/monotonic conflict for {span['phase_id']}")
                continue
            clipped_start, clipped_end = max(start, envelope_start), min(end, envelope_end)
            if clipped_end > clipped_start:
                interval_rows.append((clipped_start, clipped_end, span))

    exclusive_us: dict[str, int] = {}
    if envelope is not None and total is not None and not boundary_conflicts:
        boundaries = {envelope[0], envelope[1]}
        for start, end, _ in interval_rows:
            boundaries.update((start, end))
        ordered_boundaries = sorted(boundaries)
        for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
            duration_us = _duration_us(start, end)
            if duration_us <= 0:
                continue
            active = [span for span_start, span_end, span in interval_rows if span_start < end and span_end > start]
            if not active:
                category = "UNASSIGNED"
            else:
                active_ids = {str(span["phase_id"]) for span in active}
                leaves = [
                    span for span in active
                    if not any(str(span["phase_id"]) in ancestor_map[other] for other in active_ids if other != str(span["phase_id"]))
                ]
                categories = {str(span["phase_name"]) for span in leaves}
                category = next(iter(categories)) if len(categories) == 1 else "PARALLEL_OVERLAP"
            exclusive_us[category] = exclusive_us.get(category, 0) + duration_us
    exclusive = _rounded_category_milliseconds(exclusive_us)

    interval_union_by_phase: dict[str, int] = {}
    for phase in inclusive:
        interval_union_by_phase[phase] = _union_duration([
            (start, end) for start, end, span in interval_rows if span.get("phase_name") == phase
        ])

    state = (
        TIMING_CONFLICT if boundary_conflicts
        else TIMING_PARTIAL if parent_conflicts
        else TIMING_COMPLETE if exclusive and total is not None
        else TIMING_UNAVAILABLE
    )
    reasons = list(dict.fromkeys(boundary_conflicts + parent_conflicts))
    share = lambda value: round(value * 100 / total, 3) if isinstance(total, int) and total else 0.0
    exclusive_share = lambda value: round(value * 100 / exclusive_envelope_total, 3) if isinstance(exclusive_envelope_total, int) and exclusive_envelope_total else 0.0
    inclusive_rows = [
        {
            "phase": phase, "duration_ms": duration, "share_percent": share(duration),
            "span_count": occurrences[phase], "shares_additive": False,
            "average_span_duration_ms": round(duration / occurrences[phase]),
            "median_span_duration_ms": round(median(phase_observations[phase])),
        }
        for phase, duration in sorted(inclusive.items(), key=lambda item: (-item[1], item[0]))
    ]
    exclusive_rows = [
        {"category": category, "duration_ms": duration, "share_percent": exclusive_share(duration)}
        for category, duration in sorted(exclusive.items(), key=lambda item: (-item[1], item[0]))
    ]
    def span_label(span: Mapping[str, object]) -> str:
        name = str(span["phase_name"])
        metadata = span.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("validation_kind", "operation", "reason"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    return f"{name} — {value.replace('_', ' ')[:80]}"
        attempt = span.get("attempt")
        return f"{name} — attempt {attempt}" if isinstance(attempt, int) and attempt > 1 else name

    longest_spans = [
        {
            "phase_id": span["phase_id"], "phase": span["phase_name"],
            "label": span_label(span), "duration_ms": span["duration_ms"],
            "attempt": span["attempt"], "ordinal": span["ordinal"], "outcome": span["outcome"],
        }
        for span in sorted(
            (item for item in completed if item.get("phase_name") != "TOTAL_EXECUTION"),
            key=lambda item: (-int(item["duration_ms"]), str(item["phase_name"]), int(item["ordinal"])),
        )[:3]
    ]
    highest_average = next(iter(sorted(
        inclusive_rows,
        key=lambda row: (-int(row["average_span_duration_ms"]), str(row["phase"])),
    )), None)
    provider_intervals = [(start, end) for start, end, span in interval_rows if span.get("phase_name") == "PROVIDER_EXECUTION"]
    provider_unique = _union_duration(provider_intervals)
    unassigned = exclusive.get("UNASSIGNED") if exclusive else None
    parallel = exclusive.get("PARALLEL_OVERLAP", 0) if exclusive else None
    by_phase = dict(inclusive)
    if total is not None:
        by_phase["TOTAL_EXECUTION"] = total
    queue = inclusive.get("QUEUE_WAIT", 0)
    provider = inclusive.get("PROVIDER_EXECUTION", 0)
    validation = inclusive.get("VALIDATION", 0)
    external = inclusive.get("EXTERNAL_CI_WAIT", 0)
    def processing_ancestor(span: Mapping[str, object]) -> bool:
        return any(
            parent in by_id and by_id[parent].get("phase_name") in {"PROVIDER_EXECUTION", "VALIDATION"}
            for parent in ancestor_map[str(span["phase_id"])]
        )
    processing_coverage = sum(
        min(int(span["duration_ms"]), total or int(span["duration_ms"]))
        for span in semantic
        if span.get("phase_name") in {"PROVIDER_EXECUTION", "VALIDATION"}
        and not processing_ancestor(span)
    )
    legacy_active = max(0, total - external) if isinstance(total, int) else None
    legacy_overhead = max(0, legacy_active - processing_coverage) if isinstance(legacy_active, int) else None
    timeline_rows = []
    selected_timeline = spans if timeline_limit is None else spans[:timeline_limit]
    for span in selected_timeline:
        projected = dict(span)
        metadata = span.get("metadata")
        basis = metadata.get("measurement_basis") if isinstance(metadata, Mapping) else None
        projected["measurement_basis"] = basis or (
            "RECONCILED_WALL_CLOCK" if span.get("outcome") == "STALE" else "UNKNOWN_HISTORICAL"
        )
        start, end = _timestamp(span.get("started_at")), _timestamp(span.get("completed_at"))
        projected["relative_start_ms"] = (
            round(_duration_us(envelope[0], start) / 1000)
            if envelope is not None and start is not None else None
        )
        projected["relative_end_ms"] = (
            round(_duration_us(envelope[0], end) / 1000)
            if envelope is not None and end is not None else None
        )
        timeline_rows.append(projected)
    return {
        "contract_version": TIMING_CALCULATION_VERSION,
        "scope": "EP_RUN_ATTEMPT",
        "coverage": {
            "state": state,
            "coverage": state,
            "reason": "; ".join(reasons) if reasons else None,
            "missing_reason": "; ".join(reasons) if reasons else None,
            "expected_observations": len(completed),
            "present_observations": len(completed),
            "valid_observations": max(0, len(completed) - len(boundary_conflicts)),
            "observed_observations": max(0, len(completed) - len(boundary_conflicts)),
            "missing_observations": 0,
            "conflicting_observations": len(boundary_conflicts),
        },
        "phase_telemetry_available": True, "historical_total_available": False,
        "total_wall_time_ms": total,
        "total_monotonic_duration_ms": total,
        "total_wall_clock_observed_ms": wall_clock_total,
        "exclusive_envelope_duration_ms": exclusive_envelope_total,
        "exclusive_measurement_basis": "WALL_CLOCK_INTERVAL_ENVELOPE" if envelope is not None else None,
        "clock_difference_ms": (
            wall_clock_total - total
            if isinstance(total, int) and isinstance(wall_clock_total, int) else None
        ),
        "clock_difference_diagnostic": (
            "CLOCK_DIFFERENCE_WITHIN_TOLERANCE"
            if isinstance(total, int) and isinstance(wall_clock_total, int) and total != wall_clock_total and not boundary_conflicts
            else "CLOCKS_EQUAL" if total == wall_clock_total and total is not None else
            "CLOCKS_NOT_COMPARABLE" if boundary_conflicts else None
        ),
        "boundary_reconciliation_ms": (
            total - wall_clock_total
            if isinstance(total, int) and isinstance(wall_clock_total, int) else None
        ),
        "measurement_basis": {
            "monotonic_spans": sum(
                isinstance(span.get("metadata"), Mapping)
                and span["metadata"].get("measurement_basis") == "MONOTONIC"
                for span in completed
            ),
            "reconciled_wall_clock_spans": sum(
                (
                    isinstance(span.get("metadata"), Mapping)
                    and span["metadata"].get("measurement_basis") == "RECONCILED_WALL_CLOCK"
                ) or span.get("outcome") == "STALE"
                for span in completed
            ),
            "unknown_historical_spans": sum(
                span.get("outcome") != "STALE" and (
                    not isinstance(span.get("metadata"), Mapping)
                    or not span["metadata"].get("measurement_basis")
                )
                for span in completed
            ),
        },
        "phase_durations_ms": by_phase,
        "inclusive_phase_rows": inclusive_rows, "phase_aggregates": [
            {"phase": row["phase"], "duration_ms": row["duration_ms"]} for row in inclusive_rows
        ],
        "phase_share_durations_ms": interval_union_by_phase,
        "exclusive_distribution": exclusive_rows,
        "exclusive_distribution_total_ms": sum(exclusive.values()) if exclusive else None,
        "exclusive_distribution_closes": bool(
            state == TIMING_COMPLETE and exclusive
            and sum(exclusive.values()) == exclusive_envelope_total
        ),
        "timeline": timeline_rows,
        "timeline_observation_count": len(spans),
        "timeline_limit": timeline_limit,
        "timeline_truncated": timeline_limit is not None and len(spans) > timeline_limit,
        "timeline_axis": {
            "started_at": envelope[0].isoformat() if envelope is not None else None,
            "completed_at": envelope[1].isoformat() if envelope is not None else None,
            "duration_ms": exclusive_envelope_total,
            "measurement_basis": "WALL_CLOCK_INTERVAL_ENVELOPE" if envelope is not None else None,
        },
        "provider_execution_time_ms": provider,
        "provider_cumulative_process_duration_ms": provider,
        "provider_unique_coverage_ms": provider_unique if envelope is not None else None,
        "model_inference_time_ms": None,
        "model_inference_time_coverage": TIMING_UNAVAILABLE,
        "validation_time_ms": validation, "external_wait_time_ms": external,
        "queue_wait_time_ms": queue,
        "report_generation_time_ms": inclusive.get("REPORT_GENERATION", 0),
        "evidence_persistence_time_ms": inclusive.get("EVIDENCE_PERSISTENCE", 0),
        "repository_finalization_time_ms": inclusive.get("REPOSITORY_FINALIZATION", 0),
        "unassigned_time_ms": unassigned,
        "parallel_overlap_time_ms": parallel,
        "overhead_time_ms": legacy_overhead,
        "active_ep_processing_time_ms": legacy_active,
        "provider_share_percent": share(provider_unique),
        "validation_share_percent": share(next((row["duration_ms"] for row in exclusive_rows if row["category"] == "VALIDATION"), 0)),
        "external_wait_share_percent": share(next((row["duration_ms"] for row in exclusive_rows if row["category"] == "EXTERNAL_CI_WAIT"), 0)),
        "queue_share_percent": share(queue),
        "overhead_share_percent": share(legacy_overhead or 0),
        "longest_phase": inclusive_rows[0]["phase"] if inclusive_rows else None,
        "longest_phase_duration_ms": inclusive_rows[0]["duration_ms"] if inclusive_rows else None,
        "top_phase_categories": [
            {"phase": row["phase"], "duration_ms": row["duration_ms"]} for row in inclusive_rows[:3]
        ],
        "longest_individual_spans": longest_spans,
        "top_time_consumers": [
            {"phase": row["phase"], "duration_ms": row["duration_ms"]} for row in inclusive_rows[:3]
        ],
        "bottlenecks": {
            "largest_accumulated_category": inclusive_rows[0] if inclusive_rows else None,
            "category_highest_average_span": highest_average,
            "longest_individual_span": longest_spans[0] if longest_spans else None,
        },
        "deprecated_fields": {
            "overhead_time_ms": "Compatibility alias for unassigned_time_ms; not proven waste",
            "active_ep_processing_time_ms": "Elapsed run time minus external wait; not CPU time",
            "phase_share_durations_ms": "Inclusive category durations; shares are not additive",
        },
    }


def timing_summaries(
    root: Path, run_ids: list[str], *, central_database: Path | None = None,
    timeline_limit: int | None = 500,
    _read_connection: sqlite3.Connection | None = None,
) -> dict[str, dict[str, object]]:
    """Load a bounded run population without one timing query per row."""
    identifiers = list(dict.fromkeys(value for value in run_ids if isinstance(value, str) and value))[:1000]
    if not identifiers:
        return {}
    owns_connection = _read_connection is None
    connection = _read_connection if _read_connection is not None else _connection(root, central_database)
    placeholders = ",".join("?" for _ in identifiers)
    keys = (
        "phase_id", "run_id", "phase_name", "phase_category", "parent_phase_id",
        "attempt", "ordinal", "started_at", "completed_at", "duration_ms", "outcome", "metadata",
    )
    try:
        rows = connection.execute(
            f"""SELECT phase_id,run_id,phase_name,phase_category,parent_phase_id,
                       attempt,ordinal,started_at,completed_at,duration_ms,outcome,metadata
                  FROM execution_phase_spans WHERE run_id IN ({placeholders})
                  ORDER BY run_id,ordinal""",
            identifiers,
        ).fetchall()
        historical_rows = connection.execute(
            f"SELECT run_id,total_execution_seconds FROM execution_runs WHERE run_id IN ({placeholders})",
            identifiers,
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()
    spans_by_run: dict[str, list[dict[str, object]]] = {run_id: [] for run_id in identifiers}
    for row in rows:
        item = dict(zip(keys, row, strict=True))
        try:
            item["metadata"] = json.loads(str(item["metadata"]))
        except json.JSONDecodeError:
            item["metadata"] = {}
        spans_by_run[str(item["run_id"])].append({key: value for key, value in item.items() if key != "run_id"})
    historical = {str(row[0]): row[1] for row in historical_rows}
    return {
        run_id: timing_summary(
            root, run_id, central_database=central_database,
            _spans=spans_by_run[run_id], _historical_total=historical.get(run_id),
            timeline_limit=timeline_limit,
        )
        for run_id in identifiers
    }
