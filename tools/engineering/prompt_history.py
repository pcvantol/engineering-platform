"""Canonical SQLite index for completed Engineering Platform prompt runs."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from pathlib import Path

from .storage import open_storage


RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
REPORT_RUN_ID = re.compile(r"^- Run ID: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
REPORT_STATE = re.compile(r"^- Terminal state: `(COMPLETE|BLOCKED|FAILED)`$", re.MULTILINE)
REPORT_TIMESTAMP = re.compile(r"^- Timestamp: ([^\n]{1,80})$", re.MULTILINE)
REPORT_OBJECTIVE = re.compile(r"^- Objective: (.+)$", re.MULTILINE)
REPORT_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)
REPORT_COMMIT = re.compile(
    r"^- (?:Target Commit|Genesis-commit|Implementation Merge Commit|Finalization Merge Commit): `?([0-9a-f]{7,64})`?$",
    re.MULTILINE | re.IGNORECASE,
)
RETRY_OF = re.compile(r"^- Retry Of: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
ORIGINAL_RUN = re.compile(r"^- Original Run: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
RETRY_GENERATION = re.compile(r"^- Retry Generation: `(\d+)`$", re.MULTILINE)
RETRY_TIMESTAMP = re.compile(r"^- Retry Timestamp: ([^\n]{1,80})$", re.MULTILINE)
TERMINAL_STATES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


def _safe_run_id(value: object) -> str | None:
    return value if isinstance(value, str) and RUN_ID_PATTERN.fullmatch(value) else None


def _safe_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        timestamp = value.strip()[:80]
        return re.sub(r"T(\d{2})-(\d{2})-(\d{2})Z$", r"T\1:\2:\3Z", timestamp)
    return datetime.now(timezone.utc).isoformat()


def _relative_report(root: Path, report: Path | None) -> str | None:
    if report is None:
        return None
    try:
        relative = report.resolve().relative_to((root / ".engineering" / "reports").resolve())
    except (OSError, ValueError):
        return None
    return str(relative)


def record_prompt_execution(
    root: Path,
    *,
    run_id: object,
    terminal_state: object,
    prompt_title: object,
    executed_at: object,
    report: Path | None = None,
    git_commit: object = None,
    retry_of: object = None,
    original_run_id: object = None,
    retry_generation: object = None,
    retry_timestamp: object = None,
) -> None:
    """Upsert a terminal prompt projection without changing execution authority."""
    safe_run_id = _safe_run_id(run_id)
    if safe_run_id is None or terminal_state not in TERMINAL_STATES:
        raise ValueError("prompt history requires a terminal Engineering Platform run")
    title = str(prompt_title or safe_run_id).strip()[:500] or safe_run_id
    commit = git_commit if isinstance(git_commit, str) and re.fullmatch(r"[0-9a-f]{7,64}", git_commit) else None
    parent = _safe_run_id(retry_of)
    original = _safe_run_id(original_run_id)
    generation = retry_generation if isinstance(retry_generation, int) and retry_generation >= 1 else None
    timestamp = _safe_timestamp(retry_timestamp) if parent and retry_timestamp else None
    if parent is None:
        original = generation = timestamp = None
    elif original is None:
        original = parent
    elif generation is None:
        generation = 1
    now = datetime.now(timezone.utc).isoformat()
    connection = open_storage(root)
    try:
        connection.execute(
            """
            INSERT INTO prompt_execution_history(
                run_id, terminal_state, prompt_title, executed_at, git_commit, report_path, retry_of,
                original_run_id, retry_generation, retry_timestamp, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                terminal_state=excluded.terminal_state,
                prompt_title=excluded.prompt_title,
                executed_at=excluded.executed_at,
                git_commit=COALESCE(excluded.git_commit, prompt_execution_history.git_commit),
                report_path=COALESCE(excluded.report_path, prompt_execution_history.report_path),
                retry_of=COALESCE(excluded.retry_of, prompt_execution_history.retry_of),
                original_run_id=COALESCE(excluded.original_run_id, prompt_execution_history.original_run_id),
                retry_generation=COALESCE(excluded.retry_generation, prompt_execution_history.retry_generation),
                retry_timestamp=COALESCE(excluded.retry_timestamp, prompt_execution_history.retry_timestamp),
                updated_at=excluded.updated_at
            """,
            (
                safe_run_id,
                terminal_state,
                title,
                _safe_timestamp(executed_at),
                commit,
                _relative_report(root, report),
                parent,
                original,
                generation,
                timestamp,
                now,
            ),
        )
    finally:
        connection.close()


def _report_record(root: Path, report: Path) -> dict[str, object] | None:
    try:
        content = report.read_text(encoding="utf-8")
    except OSError:
        return None
    run = REPORT_RUN_ID.search(content)
    state = REPORT_STATE.search(content)
    if run is None or state is None:
        return None
    title = REPORT_TITLE.search(content)
    objective = REPORT_OBJECTIVE.search(content)
    timestamp = REPORT_TIMESTAMP.search(content)
    commit = REPORT_COMMIT.search(content)
    retry_of = RETRY_OF.search(content)
    original_run = ORIGINAL_RUN.search(content)
    retry_generation = RETRY_GENERATION.search(content)
    retry_timestamp = RETRY_TIMESTAMP.search(content)
    return {
        "run_id": run.group(1),
        "terminal_state": state.group(1),
        "prompt_title": (
            objective.group(1).strip()
            if objective and objective.group(1).strip()
            else title.group(1).strip()
            if title
            else run.group(1)
        ),
        "executed_at": timestamp.group(1)
        if timestamp
        else datetime.fromtimestamp(report.stat().st_mtime, timezone.utc).isoformat(),
        "report": report,
        "git_commit": commit.group(1) if commit else None,
        "retry_of": retry_of.group(1) if retry_of else None,
        "original_run_id": original_run.group(1) if original_run else None,
        "retry_generation": int(retry_generation.group(1)) if retry_generation else None,
        "retry_timestamp": retry_timestamp.group(1) if retry_timestamp else None,
    }


def backfill_prompt_history(root: Path) -> None:
    """Cache legacy reports and telemetry rows into the canonical history index."""
    reports = root / ".engineering" / "reports"
    if reports.is_dir():
        for report in reports.glob("*.md"):
            record = _report_record(root, report)
            if record is not None:
                record_prompt_execution(root, **record)
    connection = open_storage(root)
    try:
        rows = connection.execute(
            """
            SELECT run_id, terminal_state, execution_finished_at
            FROM execution_runs
            WHERE run_id NOT IN (SELECT run_id FROM prompt_execution_history)
            """
        ).fetchall()
    finally:
        connection.close()
    for run_id, terminal_state, finished_at in rows:
        record_prompt_execution(
            root,
            run_id=run_id,
            terminal_state=terminal_state,
            prompt_title=run_id,
            executed_at=finished_at,
        )


def prompt_history(root: Path, *, limit: int = 1_000) -> list[dict[str, object]]:
    """Return bounded, newest-first projections safe for the private dashboard."""
    bounded_limit = min(max(limit, 1), 1_000)
    backfill_prompt_history(root)
    connection = open_storage(root)
    try:
        rows = connection.execute(
            """
            SELECT history.run_id, history.terminal_state, history.prompt_title, history.executed_at,
                history.git_commit, history.report_path, history.retry_of, history.original_run_id,
                history.retry_generation, history.retry_timestamp, runs.execution_mode, runs.repository
            FROM prompt_execution_history AS history
            LEFT JOIN execution_runs AS runs ON runs.run_id = history.run_id
            ORDER BY history.executed_at DESC, history.run_id DESC
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "run_id": row[0],
            "status": row[1],
            "title": row[2],
            "executed_at": row[3],
            "git_commit": row[4],
            "report_available": bool(row[5]),
            "retry_of": row[6],
            "original_run_id": row[7],
            "retry_generation": row[8],
            "retry_timestamp": row[9],
            "execution_mode": row[10],
            "repository": row[11],
        }
        for row in rows
    ]


def report_for_prompt_history(root: Path, run_id: object) -> bytes | None:
    """Return only a report explicitly indexed for the requested terminal run."""
    safe_run_id = _safe_run_id(run_id)
    if safe_run_id is None:
        return None
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT report_path FROM prompt_execution_history WHERE run_id=?", (safe_run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not isinstance(row[0], str):
        return None
    path = (root / ".engineering" / "reports" / row[0]).resolve()
    try:
        path.relative_to((root / ".engineering" / "reports").resolve())
        return path.read_bytes()
    except (OSError, ValueError):
        return None
