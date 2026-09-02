"""Canonical SQLite index for completed Engineering Platform prompt runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sqlite3

from .storage import open_storage


RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
REPORT_RUN_ID = re.compile(r"^- Run ID: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
REPORT_STATE = re.compile(r"^- Terminal state: `(COMPLETE|BLOCKED|FAILED)`$", re.MULTILINE)
REPORT_TIMESTAMP = re.compile(r"^- Timestamp: ([^\n]{1,80})$", re.MULTILINE)
REPORT_OBJECTIVE = re.compile(r"^- Objective: (.+)$", re.MULTILINE)
REPORT_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)
REPORT_INCREMENT_TITLE = re.compile(r"^# Engineering Platform Increment\s+—\s+(.+)$", re.MULTILINE)
REPORT_COMMIT = re.compile(
    r"^- (?:Target Commit|Genesis-commit|Implementation Merge Commit|Finalization Merge Commit): `?([0-9a-f]{7,64})`?$",
    re.MULTILINE | re.IGNORECASE,
)
REPORT_TARGET_BRANCH = re.compile(r"^- Target Branch: `([^`\n]{1,255})`$", re.MULTILINE)
REPORT_EXECUTION_METADATA = {
    "modified": re.compile(r"^- Files Modified: `(\d{1,9})`$", re.MULTILINE),
    "created": re.compile(r"^- Files Created: `(\d{1,9})`$", re.MULTILINE),
    "deleted": re.compile(r"^- Files Deleted: `(\d{1,9})`$", re.MULTILINE),
    "codex_commands_executed": re.compile(r"^- Codex Commands Executed: `(\d{1,9})`$", re.MULTILINE),
}
RETRY_OF = re.compile(r"^- Retry Of: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
ORIGINAL_RUN = re.compile(r"^- Original Run: `([a-z0-9][a-z0-9-]{0,63})`$", re.MULTILINE)
RETRY_GENERATION = re.compile(r"^- Retry Generation: `(\d+)`$", re.MULTILINE)
RETRY_TIMESTAMP = re.compile(r"^- Retry Timestamp: ([^\n]{1,80})$", re.MULTILINE)
TERMINAL_STATES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
RETRY_CHILD_STATES = frozenset({"QUEUED", "ACTIVE", *TERMINAL_STATES})


def _safe_run_id(value: object) -> str | None:
    return value if isinstance(value, str) and RUN_ID_PATTERN.fullmatch(value) else None


def _safe_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        timestamp = value.strip()[:80]
        return re.sub(r"T(\d{2})-(\d{2})-(\d{2})Z$", r"T\1:\2:\3Z", timestamp)
    return datetime.now(timezone.utc).isoformat()


def _relative_report(root: Path, report: Path | None, *, central_database: Path | None = None) -> str | None:
    if report is None:
        return None
    central = str(central_database) if central_database is not None else os.environ.get("EP_CENTRAL_OPERATIONAL_DATABASE")
    if central:
        try:
            return "CENTRAL:" + str(report.resolve().relative_to(Path(central).resolve().parent / "artifacts"))
        except (OSError, ValueError):
            return None
    try:
        relative = report.resolve().relative_to((root / ".engineering" / "reports").resolve())
    except (OSError, ValueError):
        return None
    return str(relative)


def _safe_checkout_path(value: object) -> str | None:
    """Keep only an absolute, bounded terminal checkout snapshot."""
    if not isinstance(value, (str, Path)):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    resolved = str(path.resolve())
    return resolved if len(resolved) <= 1_000 else None


def _safe_tracked_file_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000 else None


def _safe_target_branch(value: object) -> str | None:
    """Keep a bounded, single-line branch snapshot safe for presentation."""
    if not isinstance(value, str):
        return None
    branch = value.strip()
    if not branch or len(branch) > 255 or any(character in branch for character in "\0\r\n"):
        return None
    return branch


def safe_execution_metadata(value: object) -> dict[str, int]:
    """Keep only bounded, aggregate execution counters for presentation."""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key in ("modified", "created", "deleted", "codex_commands_executed")
        for item in (value.get(key),)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000
    }


def execution_metadata_from_terminal_report(report: Path | None) -> dict[str, int]:
    """Read only aggregate execution counters from an authoritative report."""
    if report is None:
        return {}
    try:
        content = report.read_text(encoding="utf-8")
    except OSError:
        return {}
    return safe_execution_metadata({
        key: int(match.group(1))
        for key, pattern in REPORT_EXECUTION_METADATA.items()
        if (match := pattern.search(content)) is not None
    })


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
    target_checkout_path: object = None,
    tracked_file_count: object = None,
    target_branch: object = None,
    execution_metadata: object = None,
    central_database: Path | None = None,
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
    checkout_path = _safe_checkout_path(target_checkout_path)
    tracked_files = _safe_tracked_file_count(tracked_file_count)
    branch = _safe_target_branch(target_branch)
    metadata = safe_execution_metadata(execution_metadata) or execution_metadata_from_terminal_report(report)
    if parent is None:
        original = generation = timestamp = None
    elif original is None:
        original = parent
    elif generation is None:
        generation = 1
    now = datetime.now(timezone.utc).isoformat()
    if central_database is None:
        connection = open_storage(root)
    else:
        connection = sqlite3.connect(central_database.resolve(), isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute(
            """
            INSERT INTO prompt_execution_history(
                run_id, terminal_state, prompt_title, executed_at, git_commit, report_path, retry_of,
                original_run_id, retry_generation, retry_timestamp, target_checkout_path,
                tracked_file_count, target_branch, execution_metadata, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                target_checkout_path=COALESCE(excluded.target_checkout_path, prompt_execution_history.target_checkout_path),
                tracked_file_count=COALESCE(excluded.tracked_file_count, prompt_execution_history.tracked_file_count),
                target_branch=COALESCE(excluded.target_branch, prompt_execution_history.target_branch),
                execution_metadata=CASE WHEN excluded.execution_metadata != '{}' THEN excluded.execution_metadata ELSE prompt_execution_history.execution_metadata END,
                updated_at=excluded.updated_at
            """,
            (
                safe_run_id,
                terminal_state,
                title,
                _safe_timestamp(executed_at),
                commit,
                _relative_report(root, report, central_database=central_database),
                parent,
                original,
                generation,
                timestamp,
                checkout_path,
                tracked_files,
                branch,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
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
    increment_title = REPORT_INCREMENT_TITLE.search(content)
    objective = REPORT_OBJECTIVE.search(content)
    timestamp = REPORT_TIMESTAMP.search(content)
    commit = REPORT_COMMIT.search(content)
    retry_of = RETRY_OF.search(content)
    original_run = ORIGINAL_RUN.search(content)
    retry_generation = RETRY_GENERATION.search(content)
    retry_timestamp = RETRY_TIMESTAMP.search(content)
    target_branch = REPORT_TARGET_BRANCH.search(content)
    return {
        "run_id": run.group(1),
        "terminal_state": state.group(1),
        "prompt_title": (
            increment_title.group(1).strip()
            if increment_title and increment_title.group(1).strip()
            else objective.group(1).strip()
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
        "target_branch": target_branch.group(1) if target_branch else None,
        "execution_metadata": execution_metadata_from_terminal_report(report),
    }


def submission_prompt_title(root: Path, run_id: str) -> str | None:
    """Read the submitted title without deriving it from prompt content."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission.prompt_metadata FROM execution_submissions AS submission "
            "JOIN execution_submission_links AS link ON link.submission_id=submission.submission_id "
            "WHERE link.run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str):
        return None
    try:
        metadata = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    title = metadata.get("title") if isinstance(metadata, dict) else None
    return str(title).strip()[:500] or None if isinstance(title, str) else None


def record_terminal_report(root: Path, report: Path, *, central_database: Path | None = None) -> None:
    """Project one authoritative terminal report into prompt history.

    Direct execution-host invocations do not pass through the inbox watcher.
    Keeping this projection here makes the terminal report the shared source
    for both paths and lets a later reconciled report correct an older,
    incomplete history row for the same run.
    """
    record = _report_record(root, report)
    if record is None:
        raise ValueError("terminal report cannot be projected into prompt history")
    submitted_title = submission_prompt_title(root, str(record["run_id"])) if central_database is None else None
    # A managed Inbox submission owns its title. The terminal report's
    # Objective is deliberately non-authoritative input context and must never
    # replace the title already recorded at submission, including when the
    # report supplies a non-generic placeholder for that Objective.
    if submitted_title:
        record["prompt_title"] = submitted_title
    record_prompt_execution(root, central_database=central_database, **record)


def backfill_prompt_history(root: Path) -> None:
    """Cache legacy reports and telemetry rows into the canonical history index."""
    reports = root / ".engineering" / "reports"
    connection = open_storage(root)
    try:
        indexed_run_ids = {
            row[0]
            for row in connection.execute("SELECT run_id FROM prompt_execution_history").fetchall()
            if isinstance(row[0], str)
        }
    finally:
        connection.close()
    if reports.is_dir():
        for report in reports.glob("*.md"):
            record = _report_record(root, report)
            # The stored report path is the canonical terminal projection.  A
            # legacy fallback can share a Run ID with a later, real report;
            # never let a filesystem scan replace that explicit reference.
            if record is not None and record["run_id"] not in indexed_run_ids:
                record_terminal_report(root, report)
                indexed_run_ids.add(record["run_id"])
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


def _active_retry_children(root: Path) -> list[dict[str, object]]:
    """Read active child lineage from watcher-persisted job records."""
    try:
        status = json.loads((root / ".engineering" / "status" / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    active_run_id = _safe_run_id(status.get("run_id")) if isinstance(status, dict) else None
    if active_run_id is None:
        return []
    children: list[dict[str, object]] = []
    for job in (root / ".engineering" / "inbox-processing").glob("*/job.json"):
        try:
            payload = json.loads(job.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("run_id") != active_run_id:
            continue
        retry = payload.get("retry")
        parent = _safe_run_id(retry.get("retry_of")) if isinstance(retry, dict) else None
        if parent is not None:
            children.append(
                {
                    "retry_of": parent,
                    "run_id": active_run_id,
                    "status": "ACTIVE",
                    "retry_timestamp": retry.get("retry_timestamp"),
                }
            )
    return children


def _valid_retry_children(children: object) -> list[dict[str, object]]:
    """Accept bounded persisted child projections without inventing run IDs."""
    if not isinstance(children, list):
        return []
    valid: list[dict[str, object]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        parent, run_id = _safe_run_id(child.get("retry_of")), _safe_run_id(child.get("run_id"))
        state = child.get("status")
        if parent is None or state not in RETRY_CHILD_STATES:
            continue
        if state != "QUEUED" and run_id is None:
            continue
        projection = {
            "retry_of": parent,
            "status": state,
            "retry_timestamp": _safe_timestamp(child["retry_timestamp"])
            if child.get("retry_timestamp")
            else None,
        }
        if run_id is not None:
            projection["run_id"] = run_id
        valid.append(projection)
    return valid


def prompt_history(
    root: Path, *, limit: int = 1_000, queued_retry_children: list[dict[str, object]] | None = None,
    central_database: Path | None = None,
) -> list[dict[str, object]]:
    """Return bounded, newest-first projections safe for the private dashboard."""
    bounded_limit = min(max(limit, 1), 1_000)
    chat_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    if central_database is None:
        connection = open_storage(root)
    else:
        connection = sqlite3.connect(central_database.resolve(), isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        rows = connection.execute(
            """
            SELECT history.run_id,
                CASE WHEN emergency.run_id IS NOT NULL THEN 'CANCELLED' ELSE history.terminal_state END,
                history.prompt_title, history.executed_at,
                history.git_commit, history.report_path, history.retry_of, history.original_run_id,
                history.retry_generation, history.retry_timestamp, history.target_checkout_path,
                history.tracked_file_count, history.target_branch,
                COALESCE(NULLIF(history.execution_metadata, '{}'), runs.execution_metadata, '{}'),
                COALESCE(runs.execution_mode, admission.execution_mode), runs.repository,
                COALESCE(
                    runs.total_execution_seconds,
                    ROUND((
                        SELECT MAX(span.duration_ms) / 1000.0
                        FROM execution_phase_spans AS span
                        WHERE span.run_id = history.run_id
                          AND span.phase_name = 'TOTAL_EXECUTION'
                          AND span.outcome IN ('COMPLETE', 'FAILED')
                    ), 3),
                    ROUND((
                        SELECT MAX(
                            0.0,
                            (julianday(MAX(CASE
                                WHEN json_extract(log.payload, '$.event') IN ('job_completed', 'job_failed')
                                THEN log.created_at END)) - julianday(MIN(CASE
                                WHEN json_extract(log.payload, '$.event') = 'runner_started'
                                THEN log.created_at END))) * 86400.0
                        )
                        FROM engineering_component_logs AS log
                        WHERE log.component = 'inbox'
                          AND json_extract(log.payload, '$.run_id') = history.run_id
                    ), 3)
                ) AS total_execution_seconds,
                COALESCE(submission.producer_id, runs.producer_id),
                COALESCE(submission.producer_type, runs.producer_type),
                COALESCE(submission.producer_version, runs.producer_version),
                COALESCE(submission.correlation_id, runs.correlation_id),
                COALESCE(submission.mission_id, runs.mission_id),
                COALESCE(submission.engineering_action_id, runs.engineering_action_id),
                runs.execution_constraint_version, submission.submission_id,
                submission.contract_version, submission.execution_context_version,
                submission.execution_context_snapshot, submission.forge_governance_handoff_version,
                submission.forge_governance_handoff_snapshot, dismissal.terminal_state,
                dismissal.handling_state, dismissal.dismissed_at, dismissal.dismissed_by,
                emergency.cancelled_at,
                (SELECT COUNT(*) FROM execution_chat_messages AS chat
                 WHERE chat.run_id = history.run_id AND chat.created_at >= ?)
            FROM prompt_execution_history AS history
            LEFT JOIN execution_runs AS runs ON runs.run_id = history.run_id
            LEFT JOIN execution_submission_links AS submission_link ON submission_link.run_id = history.run_id
            LEFT JOIN execution_submissions AS submission ON submission.submission_id = submission_link.submission_id
            LEFT JOIN execution_admission_decisions AS admission ON admission.run_id = history.run_id
            LEFT JOIN execution_dismissals AS dismissal ON dismissal.run_id = history.run_id
            LEFT JOIN execution_emergency_recoveries AS emergency ON emergency.run_id = history.run_id
            ORDER BY history.executed_at DESC, history.run_id DESC
            LIMIT ?
            """,
            (chat_cutoff, bounded_limit),
        ).fetchall()
    finally:
        connection.close()
    records = [
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
            "target_checkout_path": row[10],
            "tracked_file_count": row[11],
            "target_branch": row[12],
            "execution_metadata": safe_execution_metadata(json.loads(row[13])) if isinstance(row[13], str) else {},
            "execution_mode": row[14],
            "repository": row[15],
            "total_execution_seconds": row[16],
            "producer_id": row[17] or "legacy",
            "producer_type": row[18] or "HUMAN",
            "producer_version": row[19],
            "correlation_id": row[20],
            "mission_id": row[21],
            "engineering_action_id": row[22],
            "execution_constraint_version": row[23],
            "submission_id": row[24],
            "producer_submission_contract_version": row[25],
            "execution_context_version": row[26],
            "execution_context": json.loads(row[27]) if isinstance(row[27], str) else None,
            "forge_governance_handoff_version": row[28],
            "forge_governance_handoff": json.loads(row[29]) if isinstance(row[29], str) else None,
            "dismissed": row[30] is not None,
            "handling_state": row[31] or "OPEN",
            "dismissed_at": row[32],
            "dismissed_by": row[33],
            "emergency_cancelled_at": row[34],
            "chat_message_count": row[35],
        }
        for row in rows
    ]
    for record in records:
        if record["forge_governance_handoff"] is None:
            record.pop("forge_governance_handoff_version")
            record.pop("forge_governance_handoff")
        if record["emergency_cancelled_at"] is None:
            record.pop("emergency_cancelled_at")
    # Lineage is a read-only projection of persisted child evidence. It never
    # changes a terminal parent or treats retry as resume. A terminal child
    # wins over active, which wins over an unclaimed Inbox child.
    children: dict[str, dict[str, object]] = {}
    for child in [
        *_valid_retry_children(queued_retry_children),
        # CENTRAL history may not inspect a checkout-local watcher projection.
        *(_active_retry_children(root) if central_database is None else ()),
        *[record for record in records if record.get("retry_of")],
    ]:
        parent = child.get("retry_of")
        if isinstance(parent, str):
            children[parent] = child
    for record in records:
        child = children.get(record["run_id"])
        record["retry_child_run_id"] = child.get("run_id") if child else None
        record["retry_status"] = child.get("status") if child else None
        record["retry_timestamp"] = child.get("retry_timestamp") if child else record["retry_timestamp"]
        record["queued_retry_child"] = bool(child and child.get("status") == "QUEUED")
        record["active_retry_child"] = bool(child and child.get("status") == "ACTIVE")
        record["can_retry"] = record.get("status") in {"BLOCKED", "FAILED"} and child is None and not record["dismissed"]
        chain = [record["run_id"]]
        cursor = record
        while cursor.get("retry_of") and cursor["retry_of"] not in chain:
            chain.insert(0, cursor["retry_of"])
            cursor = next((item for item in records if item["run_id"] == cursor["retry_of"]), {})
        record["retry_chain"] = chain
        record["current_active_run"] = (
            child.get("run_id") if child and child.get("status") != "QUEUED" else record["run_id"]
        )
    return records


def report_path_for_prompt_history(root: Path, run_id: object) -> Path | None:
    """Resolve only the report explicitly indexed for the requested terminal run."""
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
    central = os.environ.get("EP_CENTRAL_OPERATIONAL_DATABASE")
    if central and row[0].startswith("CENTRAL:"):
        path = (Path(central).resolve().parent / "artifacts" / row[0].removeprefix("CENTRAL:")).resolve()
        try:
            path.relative_to(Path(central).resolve().parent / "artifacts")
            return path if path.is_file() else None
        except (OSError, ValueError):
            return None
    path = (root / ".engineering" / "reports" / row[0]).resolve()
    try:
        path.relative_to((root / ".engineering" / "reports").resolve())
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def report_for_prompt_history(root: Path, run_id: object) -> bytes | None:
    """Return only a report explicitly indexed for the requested terminal run."""
    path = report_path_for_prompt_history(root, run_id)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None
