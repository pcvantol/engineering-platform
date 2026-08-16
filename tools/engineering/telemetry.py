"""Best-effort, local Execution Host telemetry.

Telemetry is operational evidence only.  It is deliberately separate from
transaction checkpoints and cannot change an engineering outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import sqrt
from pathlib import Path
from threading import Lock, Thread, current_thread
from time import monotonic
from typing import Callable
from statistics import mean, median

from .storage import open_storage
from .producer import ProducerMetadata
from .execution_timing import timing_summary


TERMINAL_STATES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
_PENDING_WORKERS: set[Thread] = set()
_PENDING_WORKERS_LOCK = Lock()


@dataclass(frozen=True)
class ExecutionTelemetry:
    run_id: str
    arrived_at: datetime
    execution_started_at: datetime
    execution_finished_at: datetime
    terminal_state: str
    execution_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    execution_mode: str
    workspace: str
    repository: str
    execution_host_version: str
    retry_of: str | None = None
    original_run_id: str | None = None
    retry_generation: int | None = None
    retry_timestamp: str | None = None
    prompt_characters: int | None = None
    runtime_provider: str | None = None
    runtime_model: str | None = None
    reasoning_profile: str | None = None
    configuration_profile: str | None = None
    execution_metadata: dict[str, int] | None = None
    producer: ProducerMetadata = ProducerMetadata()


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _runtime_value(value: object) -> str | None:
    """Keep a bounded, display-safe runtime profile value for local aggregation."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.casefold() in {"not reported", "unavailable"}:
        return None
    return normalized[:120]


def _execution_metadata(value: object) -> str:
    if not isinstance(value, dict):
        return "{}"
    safe = {
        key: item
        for key in ("modified", "created", "deleted", "codex_commands_executed")
        for item in (value.get(key),)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000
    }
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)


def persist_execution(
    root: Path,
    telemetry: ExecutionTelemetry,
    *,
    create: bool = True,
    background: bool = False,
) -> None:
    """Persist one immutable run projection and refresh its daily aggregate."""
    if telemetry.terminal_state not in TERMINAL_STATES:
        raise ValueError("telemetry requires a terminal state")
    arrived, started, finished = map(_utc, (telemetry.arrived_at, telemetry.execution_started_at, telemetry.execution_finished_at))
    queue_wait = max(0.0, (started - arrived).total_seconds())
    total_execution_seconds = max(0.0, (finished - arrived).total_seconds())
    execution_seconds = telemetry.execution_seconds
    if execution_seconds is not None and (isinstance(execution_seconds, bool) or execution_seconds < 0):
        raise ValueError("telemetry execution duration is invalid")
    execution_date = finished.date().isoformat()
    connection = open_storage(root, create=create, journal_mode="MEMORY" if background else "DELETE")
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO execution_runs(
                run_id, execution_date, arrived_at, execution_started_at, execution_finished_at,
                queue_wait_seconds, execution_seconds, total_execution_seconds, terminal_state, input_tokens, output_tokens,
                total_tokens, execution_mode, workspace, repository, execution_host_version, retry_of,
                original_run_id, retry_generation, retry_timestamp, prompt_characters,
                runtime_provider, runtime_model, reasoning_profile, configuration_profile
                , producer_id, producer_type, producer_version, correlation_id, mission_id,
                engineering_action_id, execution_constraint_version, execution_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telemetry.run_id,
                execution_date,
                _timestamp(arrived),
                _timestamp(started),
                _timestamp(finished),
                queue_wait,
                execution_seconds,
                total_execution_seconds,
                telemetry.terminal_state,
                _integer(telemetry.input_tokens),
                _integer(telemetry.output_tokens),
                _integer(telemetry.total_tokens),
                telemetry.execution_mode,
                telemetry.workspace,
                telemetry.repository,
                telemetry.execution_host_version,
                telemetry.retry_of,
                telemetry.original_run_id,
                telemetry.retry_generation,
                telemetry.retry_timestamp,
                _integer(telemetry.prompt_characters),
                _runtime_value(telemetry.runtime_provider),
                _runtime_value(telemetry.runtime_model),
                _runtime_value(telemetry.reasoning_profile),
                _runtime_value(telemetry.configuration_profile),
                telemetry.producer.producer_id,
                telemetry.producer.producer_type,
                telemetry.producer.producer_version,
                telemetry.producer.correlation_id,
                telemetry.producer.mission_id,
                telemetry.producer.engineering_action_id,
                telemetry.producer.execution_constraint_version,
                _execution_metadata(telemetry.execution_metadata),
            ),
        )
        connection.execute(
            """INSERT OR IGNORE INTO execution_receipts(
                run_id, producer_id, producer_type, producer_version, mission_id,
                engineering_action_id, correlation_id, execution_constraint_version,
                execution_host, execution_host_version, receipt_timestamp, execution_outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                telemetry.run_id, telemetry.producer.producer_id, telemetry.producer.producer_type,
                telemetry.producer.producer_version, telemetry.producer.mission_id,
                telemetry.producer.engineering_action_id, telemetry.producer.correlation_id,
                telemetry.producer.execution_constraint_version, "Engineering Platform",
                telemetry.execution_host_version, _timestamp(finished), telemetry.terminal_state,
            ),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO daily_execution_statistics(
                execution_date, workspace, repository, execution_mode, prompt_count,
                complete_count, blocked_count, failed_count, average_execution_seconds, average_total_execution_seconds,
                average_queue_wait_seconds, input_tokens, output_tokens, total_tokens
            )
            SELECT execution_date, workspace, repository, execution_mode,
                COUNT(*),
                SUM(terminal_state = 'COMPLETE'), SUM(terminal_state = 'BLOCKED'), SUM(terminal_state = 'FAILED'),
                AVG(execution_seconds), AVG(total_execution_seconds), AVG(queue_wait_seconds),
                SUM(input_tokens), SUM(output_tokens), SUM(total_tokens)
            FROM execution_runs
            WHERE execution_date = ? AND workspace = ? AND repository = ? AND execution_mode = ?
            GROUP BY execution_date, workspace, repository, execution_mode
            """,
            (execution_date, telemetry.workspace, telemetry.repository, telemetry.execution_mode),
        )
    finally:
        connection.close()


def persist_execution_async(
    root: Path, telemetry: ExecutionTelemetry, *, on_error: Callable[[Exception], None] | None = None
) -> Thread:
    """Schedule telemetry without ever delaying or failing engineering delivery."""
    def persist() -> None:
        try:
            # The inbox watcher has already established the canonical workspace
            # before telemetry is scheduled. A delayed best-effort worker must
            # never recreate that workspace after its owner has gone away.
            persist_execution(root, telemetry, create=False, background=True)
        except Exception as error:  # Best-effort boundary; caller logs only.
            if on_error is not None:
                on_error(error)
        finally:
            with _PENDING_WORKERS_LOCK:
                _PENDING_WORKERS.discard(current_thread())

    worker = Thread(target=persist, name=f"ep-telemetry-{telemetry.run_id}", daemon=True)
    with _PENDING_WORKERS_LOCK:
        _PENDING_WORKERS.add(worker)
    worker.start()
    return worker


def wait_for_pending_telemetry(*, timeout: float = 5.0) -> None:
    """Wait for scheduled best-effort writes when a host is shutting down.

    The watcher never calls this on its normal prompt-delivery path.  It gives
    callers that own a temporary workspace a deterministic way to close it
    only after the explicitly asynchronous telemetry writer is finished.
    """
    deadline = monotonic() + max(0.0, timeout)
    while True:
        with _PENDING_WORKERS_LOCK:
            workers = tuple(_PENDING_WORKERS)
        if not workers:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        for worker in workers:
            worker.join(timeout=remaining)


def daily_statistics(root: Path, *, days: int = 7) -> list[dict[str, object]]:
    """Return generic daily aggregates, newest day first, for the private dashboard."""
    if not 1 <= days <= 31:
        raise ValueError("telemetry days must be between 1 and 31")
    connection = open_storage(root)
    try:
        rows = connection.execute(
            """
            SELECT execution_date, SUM(prompt_count), SUM(complete_count), SUM(blocked_count),
                SUM(failed_count), AVG(average_execution_seconds), AVG(average_total_execution_seconds), AVG(average_queue_wait_seconds),
                SUM(input_tokens), SUM(output_tokens), SUM(total_tokens)
            FROM daily_execution_statistics
            GROUP BY execution_date
            ORDER BY execution_date DESC
            LIMIT ?
            """,
            (days,),
        ).fetchall()
    finally:
        connection.close()
    keys = (
        "date", "prompt_count", "complete_count", "blocked_count", "failed_count",
        "average_execution_seconds", "average_total_execution_seconds", "average_queue_wait_seconds", "input_tokens",
        "output_tokens", "total_tokens",
    )
    result = [dict(zip(keys, row, strict=True)) for row in rows]
    # The compact trend deliberately exposes only two additional phase metrics.
    # Each selected UTC day is bounded by the same seven-day query limit above.
    for row in result:
        detail = daily_timing_detail(root, str(row["date"]))
        summary = detail["summary"]
        row["average_provider_execution_seconds"] = (
            float(summary["provider_execution"]["average_ms"]) / 1000
            if detail["phase_telemetry_available"] and isinstance(summary.get("provider_execution"), dict) else None
        )
        row["average_validation_seconds"] = (
            float(summary["validation"]["average_ms"]) / 1000
            if detail["phase_telemetry_available"] and isinstance(summary.get("validation"), dict) else None
        )
    return result


_DASHBOARD_PHASES = (
    "QUEUE_WAIT", "SUBMISSION_CLAIM", "INITIALIZATION", "HOST_PREFLIGHT",
    "WORKSPACE_PREFLIGHT", "CAPABILITY_PREFLIGHT", "EXECUTION_PREPARATION",
    "PROVIDER_EXECUTION", "VALIDATION", "REPAIR", "REPOSITORY_FINALIZATION",
    "PR_OR_MERGE", "FINALIZATION", "REPORT_GENERATION", "EVIDENCE_PERSISTENCE",
    "REPOSITORY_CLEANUP", "RECONCILIATION", "EXTERNAL_CI_WAIT",
)


def daily_timing_detail(root: Path, execution_date: str) -> dict[str, object]:
    """Return a bounded, read-only UTC-day timing projection for the dashboard.

    This deliberately composes ``timing_summary`` per persisted run, keeping
    the browser a renderer and preserving the canonical non-double-counting
    timing rules.  ``execution_date`` is the UTC terminal-date already used by
    the seven-day trend projection; the client only formats that stable value
    in the local-user date style.
    """
    try:
        datetime.strptime(execution_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("execution date is invalid") from None
    connection = open_storage(root)
    try:
        rows = connection.execute(
            """SELECT run_id, execution_started_at, terminal_state, total_execution_seconds,
                      queue_wait_seconds, runtime_provider, runtime_model, reasoning_profile,
                      producer_type, repository
                 FROM execution_runs WHERE execution_date=?
                 ORDER BY execution_started_at DESC LIMIT 250""",
            (execution_date,),
        ).fetchall()
    finally:
        connection.close()
    run_rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    for row in rows:
        run_id = str(row[0])
        summary = timing_summary(root, run_id)
        summaries.append(summary)
        phase_available = bool(summary.get("phase_telemetry_available"))
        total = summary.get("total_wall_time_ms") if phase_available else (
            round(float(row[3]) * 1000) if isinstance(row[3], (int, float)) else None
        )
        def measured(name: str) -> int | None:
            value = summary.get(name)
            return int(value) if phase_available and isinstance(value, int) else None
        run_rows.append({
            "run_id": run_id, "started_at": row[1], "status": row[2],
            "total_duration_ms": total, "queue_wait_ms": measured("queue_wait_time_ms"),
            "provider_duration_ms": measured("provider_execution_time_ms"),
            "validation_duration_ms": measured("validation_time_ms"),
            "external_wait_ms": measured("external_wait_time_ms"),
            "report_generation_ms": measured("report_generation_time_ms"),
            "evidence_persistence_ms": measured("evidence_persistence_time_ms"),
            "largest_phase": summary.get("longest_phase") if phase_available else None,
            "producer_type": row[8], "repository": row[9], "provider": row[5],
            "model": row[6], "reasoning_profile": row[7],
            "phase_telemetry": "RECORDED" if phase_available else "NOT_RECORDED",
        })
    def values(key: str) -> list[int]:
        return [int(item[key]) for item in summaries if isinstance(item.get(key), int)]
    def aggregate(items: list[int]) -> dict[str, int] | None:
        return {"average_ms": round(mean(items)), "median_ms": round(median(items)), "total_ms": sum(items), "runs": len(items)} if items else None
    totals = [int(row["total_duration_ms"]) for row in run_rows if isinstance(row["total_duration_ms"], int)]
    summary = {
        "executions": len(run_rows),
        "completed": sum(row["status"] == "COMPLETE" for row in run_rows),
        "blocked": sum(row["status"] == "BLOCKED" for row in run_rows),
        "failed": sum(row["status"] == "FAILED" for row in run_rows),
        "total_wall_time": aggregate(totals),
        "active_processing_time": aggregate(values("active_ep_processing_time_ms")),
        "queue_wait": aggregate(values("queue_wait_time_ms")),
        "provider_execution": aggregate(values("provider_execution_time_ms")),
        "validation": aggregate(values("validation_time_ms")),
        "external_wait": aggregate(values("external_wait_time_ms")),
        "overhead": aggregate(values("overhead_time_ms")),
        "report_generation": aggregate(values("report_generation_time_ms")),
        "evidence_persistence": aggregate(values("evidence_persistence_time_ms")),
    }
    phase_values: dict[str, list[int]] = {phase: [] for phase in _DASHBOARD_PHASES}
    for item in summaries:
        # Use the shared run-level category projection.  The dashboard must
        # not reconstruct a competing aggregate from raw spans.
        aggregates = item.get("phase_aggregates", ())
        if isinstance(aggregates, list):
            for aggregate_row in aggregates:
                if not isinstance(aggregate_row, dict):
                    continue
                phase, value = aggregate_row.get("phase"), aggregate_row.get("duration_ms")
                if isinstance(phase, str) and phase in phase_values and isinstance(value, int):
                    phase_values[phase].append(value)
    phase_rows = [dict({"phase": phase}, **aggregate(items)) for phase, items in phase_values.items() if aggregate(items)]
    total_phase_time = sum(int(item["total_ms"]) for item in phase_rows)
    consumers = sorted(phase_rows, key=lambda item: (-int(item["total_ms"]), str(item["phase"])))[:3]
    for item in consumers:
        item["share_percent"] = round(int(item["total_ms"]) * 100 / total_phase_time, 3) if total_phase_time else None
    return {
        "date": execution_date, "timezone": "UTC", "summary": summary, "phases": phase_rows,
        "bottlenecks": {"longest_average_phase": max(phase_rows, key=lambda item: int(item["average_ms"]), default=None),
                        "largest_accumulated_phase": max(phase_rows, key=lambda item: int(item["total_ms"]), default=None),
                        "top_time_consumers": consumers,
                        "shares": {key: (round(mean(values(key)) * 100 / mean(totals), 3) if values(key) and totals and mean(totals) else None) for key in ("queue_wait_time_ms", "provider_execution_time_ms", "validation_time_ms", "external_wait_time_ms", "overhead_time_ms")}},
        "runs": run_rows, "phase_telemetry_available": any(bool(item.get("phase_telemetry_available")) for item in summaries),
    }


def execution_timing(root: Path, run_id: str) -> dict[str, float | str]:
    """Return persisted timing and terminal timestamp evidence for one run."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT execution_seconds, total_execution_seconds, execution_finished_at FROM execution_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {}
    result: dict[str, float | str] = {}
    for key, value in zip(("execution_seconds", "total_execution_seconds"), row[:2], strict=True):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            result[key] = float(value)
    if isinstance(row[2], str):
        result["finished_at"] = row[2]
    return result


def comparable_duration_estimate(
    root: Path,
    *,
    prompt_characters: object,
    runtime_metadata: object,
) -> dict[str, float | int | str]:
    """Return a robust size-adjusted estimate from one exact runtime profile.

    This is intentionally advisory: it never affects scheduling or engineering
    state.  Missing or unreported profile fields yield no estimate rather than
    mixing incomparable providers, models or reasoning settings.
    """
    characters = _integer(prompt_characters)
    if not characters or not isinstance(runtime_metadata, dict):
        return {}
    signature = tuple(
        _runtime_value(runtime_metadata.get(key))
        for key in ("runtime_provider", "model", "reasoning_profile", "configuration_profile")
    )
    if any(value is None for value in signature):
        return {}
    connection = open_storage(root)
    try:
        rows = connection.execute(
            """
            SELECT execution_seconds, prompt_characters
            FROM execution_runs
            WHERE terminal_state = 'COMPLETE'
              AND runtime_provider = ? AND runtime_model = ?
              AND reasoning_profile = ? AND configuration_profile = ?
              AND execution_seconds IS NOT NULL AND prompt_characters > 0
            ORDER BY execution_finished_at DESC
            LIMIT 20
            """,
            signature,
        ).fetchall()
    finally:
        connection.close()
    # A linear character ratio turns a modestly larger prompt into an
    # implausibly long estimate even though each run has fixed startup,
    # validation and reporting work.  Use a bounded square-root factor instead:
    # it still reflects substantial input differences without letting sparse
    # historical samples dominate the operator-facing indication.
    scaled = [
        float(seconds) * min(1.6, max(0.7, sqrt(characters / int(size))))
        for seconds, size in rows
        if seconds >= 0
    ]
    if len(scaled) < 2:
        return {}
    ordered = sorted(scaled)
    # The observed spread gives an honest range while avoiding a single old
    # outlier dominating the indicator. The arithmetic mean remains available
    # as transparent diagnostic evidence for a fixed input and sample set.
    lower = ordered[max(0, (len(ordered) - 1) // 4)]
    upper = ordered[min(len(ordered) - 1, (len(ordered) - 1) * 3 // 4)]
    return {
        "sample_count": len(scaled),
        "average_seconds": round(mean(scaled), 3),
        "lower_seconds": round(min(lower, upper), 3),
        "upper_seconds": round(max(lower, upper), 3),
        "runtime_provider": signature[0],
        "model": signature[1],
    }
