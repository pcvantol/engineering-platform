"""Best-effort, local Execution Host telemetry.

Telemetry is operational evidence only.  It is deliberately separate from
transaction checkpoints and cannot change an engineering outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread, current_thread
from time import monotonic
from typing import Callable

from .storage import open_storage


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


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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
            INSERT OR REPLACE INTO execution_runs(
                run_id, execution_date, arrived_at, execution_started_at, execution_finished_at,
                queue_wait_seconds, execution_seconds, total_execution_seconds, terminal_state, input_tokens, output_tokens,
                total_tokens, execution_mode, workspace, repository, execution_host_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return [dict(zip(keys, row, strict=True)) for row in rows]


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
