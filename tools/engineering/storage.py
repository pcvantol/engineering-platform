"""Versioned SQLite schema contract for private Engineering Platform evidence.

This module owns only the database lifecycle. Consumers are migrated to it in a
separate, compatibility-tested change; an unrecognized database is never
silently replaced or downgraded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


WORKSPACE_DIRECTORY = ".engineering"
DATABASE_FILENAME = "engineering.db"
ENGINEERING_STORAGE_SCHEMA_VERSION = 10
JOURNAL_MODES = frozenset({"DELETE", "MEMORY"})


class EngineeringStorageError(RuntimeError):
    """Raised when the local Engineering evidence database is unsafe to use."""


Migration = Callable[[sqlite3.Connection], None]


def _schema_v1(connection: sqlite3.Connection) -> None:
    """Create the initial normalized local-evidence schema."""
    for statement in """
        CREATE TABLE IF NOT EXISTS engineering_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS engineering_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_status (
            name TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_transactions (
            run_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            phase TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_artifacts (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            run_id TEXT,
            name TEXT NOT NULL,
            content BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(category, run_id, name)
        );
        CREATE INDEX IF NOT EXISTS engineering_artifacts_lookup
            ON engineering_artifacts(category, run_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS engineering_component_logs (
            id INTEGER PRIMARY KEY,
            component TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS engineering_component_logs_lookup
            ON engineering_component_logs(component, id DESC);
        """.split(";"):
        if statement.strip():
            connection.execute(statement)
    legacy_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    copies = (
        ("ep_status", "engineering_status", "name,payload,updated_at"),
        ("ep_transactions", "engineering_transactions", "run_id,payload,phase,updated_at"),
        ("ep_artifacts", "engineering_artifacts", "id,category,run_id,name,content,created_at"),
        ("ep_component_logs", "engineering_component_logs", "id,component,payload,created_at"),
    )
    for source, destination, columns in copies:
        if source in legacy_tables:
            connection.execute(f"INSERT OR IGNORE INTO {destination}({columns}) SELECT {columns} FROM {source}")


def _schema_v2(connection: sqlite3.Connection) -> None:
    """Create generic, local-only Execution Host telemetry evidence."""
    for statement in """
        CREATE TABLE IF NOT EXISTS execution_runs (
            run_id TEXT PRIMARY KEY,
            execution_date TEXT NOT NULL,
            arrived_at TEXT NOT NULL,
            execution_started_at TEXT NOT NULL,
            execution_finished_at TEXT NOT NULL,
            queue_wait_seconds REAL NOT NULL,
            execution_seconds REAL,
            terminal_state TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            execution_mode TEXT NOT NULL,
            workspace TEXT NOT NULL,
            repository TEXT NOT NULL,
            execution_host_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS execution_runs_daily_lookup
            ON execution_runs(execution_date, terminal_state);
        CREATE TABLE IF NOT EXISTS daily_execution_statistics (
            execution_date TEXT NOT NULL,
            workspace TEXT NOT NULL,
            repository TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            prompt_count INTEGER NOT NULL,
            complete_count INTEGER NOT NULL,
            blocked_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            average_execution_seconds REAL,
            average_queue_wait_seconds REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            PRIMARY KEY(execution_date, workspace, repository, execution_mode)
        );
        CREATE INDEX IF NOT EXISTS daily_execution_statistics_date_lookup
            ON daily_execution_statistics(execution_date);
        """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _schema_v3(connection: sqlite3.Connection) -> None:
    """Add total Execution Host elapsed time without changing run authority."""
    connection.execute("ALTER TABLE execution_runs ADD COLUMN total_execution_seconds REAL")
    connection.execute(
        "ALTER TABLE daily_execution_statistics ADD COLUMN average_total_execution_seconds REAL"
    )


def _schema_v4(connection: sqlite3.Connection) -> None:
    """Migrate the previous redacted component-log files into SQLite once."""
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    logs = database.parent / "logs"
    for component in ("inbox", "dashboard"):
        existing = connection.execute(
            "SELECT COUNT(*) FROM engineering_component_logs WHERE component=?", (component,)
        ).fetchone()[0]
        if existing:
            continue
        files = [logs / f"{component}.log.{index}" for index in range(3, 0, -1)]
        files.append(logs / f"{component}.log")
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                created_at = payload.get("timestamp")
                if not isinstance(created_at, str) or not created_at:
                    created_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    (component, json.dumps(payload, separators=(",", ":"), sort_keys=True), created_at),
                )


def _schema_v5(connection: sqlite3.Connection) -> None:
    """Create the canonical, private index of terminal prompt executions."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_execution_history (
            run_id TEXT PRIMARY KEY,
            terminal_state TEXT NOT NULL,
            prompt_title TEXT NOT NULL,
            executed_at TEXT NOT NULL,
            git_commit TEXT,
            report_path TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS prompt_execution_history_executed_lookup "
        "ON prompt_execution_history(executed_at DESC, run_id DESC)"
    )


def _schema_v6(connection: sqlite3.Connection) -> None:
    """Persist immutable retry lineage separately from the original run."""
    for statement in (
        "ALTER TABLE prompt_execution_history ADD COLUMN retry_of TEXT",
        "ALTER TABLE prompt_execution_history ADD COLUMN original_run_id TEXT",
        "ALTER TABLE prompt_execution_history ADD COLUMN retry_generation INTEGER",
        "ALTER TABLE prompt_execution_history ADD COLUMN retry_timestamp TEXT",
        "ALTER TABLE execution_runs ADD COLUMN retry_of TEXT",
        "ALTER TABLE execution_runs ADD COLUMN original_run_id TEXT",
        "ALTER TABLE execution_runs ADD COLUMN retry_generation INTEGER",
        "ALTER TABLE execution_runs ADD COLUMN retry_timestamp TEXT",
    ):
        connection.execute(statement)


def _schema_v7(connection: sqlite3.Connection) -> None:
    """Record the safe runtime signature needed for comparable duration estimates."""
    for statement in (
        "ALTER TABLE execution_runs ADD COLUMN prompt_characters INTEGER",
        "ALTER TABLE execution_runs ADD COLUMN runtime_provider TEXT",
        "ALTER TABLE execution_runs ADD COLUMN runtime_model TEXT",
        "ALTER TABLE execution_runs ADD COLUMN reasoning_profile TEXT",
        "ALTER TABLE execution_runs ADD COLUMN configuration_profile TEXT",
    ):
        connection.execute(statement)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS execution_runs_duration_profile_lookup "
        "ON execution_runs(terminal_state, runtime_provider, runtime_model, "
        "reasoning_profile, configuration_profile)"
    )
    # Existing immutable reports already contain safe runtime provenance and
    # the submitted objective.  Extract only the profile and its character
    # count so the new estimate can learn from prior local evidence without
    # copying prompt content into telemetry.
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    reports_root = database.parent / "reports"
    rows = connection.execute(
        """
        SELECT runs.run_id, history.report_path
        FROM execution_runs AS runs
        JOIN prompt_execution_history AS history ON history.run_id = runs.run_id
        WHERE history.report_path IS NOT NULL
        """
    ).fetchall()
    labels = {
        "runtime_provider": "Runtime Provider",
        "runtime_model": "AI Model",
        "reasoning_profile": "Reasoning Profile",
        "configuration_profile": "Configuration Profile",
    }
    for run_id, relative_path in rows:
        if not isinstance(run_id, str) or not isinstance(relative_path, str):
            continue
        try:
            report = (reports_root / relative_path).resolve()
            report.relative_to(reports_root.resolve())
            text = report.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        values: dict[str, str | None] = {}
        for key, label in labels.items():
            match = re.search(rf"^- {re.escape(label)}: `([^`\n]{{1,120}})`$", text, re.MULTILINE)
            value = match.group(1).strip() if match else ""
            values[key] = value if value and value.casefold() not in {"not reported", "unavailable"} else None
        objective = re.search(
            r"^- Objective: (.*?)(?=\n\n## Execution Target Identity)", text, re.MULTILINE | re.DOTALL
        )
        prompt_characters = len(objective.group(1)) if objective else None
        connection.execute(
            """
            UPDATE execution_runs SET prompt_characters=?, runtime_provider=?, runtime_model=?,
                reasoning_profile=?, configuration_profile=? WHERE run_id=?
            """,
            (
                prompt_characters,
                values["runtime_provider"],
                values["runtime_model"],
                values["reasoning_profile"],
                values["configuration_profile"],
                run_id,
            ),
        )


def _schema_v8(connection: sqlite3.Connection) -> None:
    """Preserve target-workspace facts with each terminal execution."""
    for statement in (
        "ALTER TABLE prompt_execution_history ADD COLUMN target_checkout_path TEXT",
        "ALTER TABLE prompt_execution_history ADD COLUMN tracked_file_count INTEGER",
    ):
        connection.execute(statement)


def _schema_v9(connection: sqlite3.Connection) -> None:
    """Persist producer-neutral provenance and immutable execution receipts."""
    for statement in (
        "ALTER TABLE execution_runs ADD COLUMN producer_id TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE execution_runs ADD COLUMN producer_type TEXT NOT NULL DEFAULT 'HUMAN'",
        "ALTER TABLE execution_runs ADD COLUMN producer_version TEXT",
        "ALTER TABLE execution_runs ADD COLUMN correlation_id TEXT",
        "ALTER TABLE execution_runs ADD COLUMN mission_id TEXT",
        "ALTER TABLE execution_runs ADD COLUMN engineering_action_id TEXT",
        "ALTER TABLE execution_runs ADD COLUMN execution_constraint_version TEXT",
        "CREATE INDEX IF NOT EXISTS execution_runs_producer_lookup ON execution_runs(producer_type, producer_id)",
        """CREATE TABLE IF NOT EXISTS execution_receipts (
            run_id TEXT PRIMARY KEY,
            producer_id TEXT NOT NULL,
            producer_type TEXT NOT NULL,
            producer_version TEXT,
            mission_id TEXT,
            engineering_action_id TEXT,
            correlation_id TEXT,
            execution_constraint_version TEXT,
            execution_host TEXT NOT NULL,
            execution_host_version TEXT NOT NULL,
            receipt_timestamp TEXT NOT NULL,
            execution_outcome TEXT NOT NULL
        )""",
    ):
        connection.execute(statement)


def _schema_v10(connection: sqlite3.Connection) -> None:
    """Preserve the target branch observed when terminal evidence is written."""
    connection.execute("ALTER TABLE prompt_execution_history ADD COLUMN target_branch TEXT")


MIGRATIONS: dict[int, Migration] = {
    1: _schema_v1,
    2: _schema_v2,
    3: _schema_v3,
    4: _schema_v4,
    5: _schema_v5,
    6: _schema_v6,
    7: _schema_v7,
    8: _schema_v8,
    9: _schema_v9,
    10: _schema_v10,
}


def database_path(root: Path) -> Path:
    """Return the only persistent EP evidence path for a repository."""
    return root.resolve() / WORKSPACE_DIRECTORY / DATABASE_FILENAME


def _schema_version(connection: sqlite3.Connection) -> int:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "engineering_schema_migrations" not in tables:
        if "ep_metadata" in tables:
            return 0
        if tables:
            raise EngineeringStorageError("Engineering storage has no recognized schema history.")
        return 0
    versions = [int(row[0]) for row in connection.execute("SELECT version FROM engineering_schema_migrations")]
    return max(versions, default=0)


def open_storage(
    root: Path, *, create: bool = True, journal_mode: str = "DELETE"
) -> sqlite3.Connection:
    """Open, upgrade and validate the private SQLite evidence database.

    Schema upgrades run in one immediate transaction. SQLite rollback-journal
    mode intentionally avoids persistent WAL sidecars in `.engineering`.
    Background best-effort writers may request an in-memory journal so their
    temporary transaction files cannot race workspace cleanup.
    """
    if journal_mode not in JOURNAL_MODES:
        raise ValueError("Unsupported SQLite journal mode.")
    path = database_path(root)
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise EngineeringStorageError("Engineering storage is unavailable.")
    # Best-effort consumers (telemetry) open in read/write-existing mode.  This
    # prevents a delayed worker from recreating a database while a workspace is
    # being removed.
    connection = sqlite3.connect(
        path if create else f"file:{path}?mode=rw",
        timeout=10,
        isolation_level=None,
        uri=not create,
    )
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        current = _schema_version(connection)
        if current > ENGINEERING_STORAGE_SCHEMA_VERSION:
            raise EngineeringStorageError(
                "Engineering storage schema is newer than this Engineering Platform supports."
            )
        connection.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, ENGINEERING_STORAGE_SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise EngineeringStorageError(f"Engineering storage migration {version} is unavailable.")
            migration(connection)
            connection.execute(
                "INSERT INTO engineering_schema_migrations(version) VALUES(?)", (version,)
            )
        connection.execute("COMMIT")
        path.chmod(0o600)
        return connection
    except (OSError, sqlite3.DatabaseError, EngineeringStorageError) as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        connection.close()
        if isinstance(error, EngineeringStorageError):
            raise
        raise EngineeringStorageError("Engineering storage could not be opened safely.") from error
