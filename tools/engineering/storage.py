"""Versioned SQLite schema contract for private Engineering Platform evidence.

This module owns only the database lifecycle. Consumers are migrated to it in a
separate, compatibility-tested change; an unrecognized database is never
silently replaced or downgraded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import re
import sqlite3


WORKSPACE_DIRECTORY = ".engineering"
DATABASE_FILENAME = "engineering.db"
ENGINEERING_STORAGE_SCHEMA_VERSION = 19
JOURNAL_MODES = frozenset({"DELETE", "MEMORY"})
LEGACY_DISMISSALS_PATH = Path(".engineering/status/execution_dismissals.json")


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


def _schema_v11(connection: sqlite3.Connection) -> None:
    """Add the canonical host records; files retain payloads and projections only."""
    for statement in """
        CREATE TABLE execution_submissions (
            submission_id TEXT PRIMARY KEY,
            producer_id TEXT NOT NULL,
            producer_type TEXT NOT NULL,
            producer_version TEXT,
            contract_version TEXT,
            prompt_content TEXT NOT NULL,
            prompt_metadata TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            original_envelope TEXT NOT NULL,
            correlation_id TEXT,
            mission_id TEXT,
            execution_run_id TEXT REFERENCES execution_runs(run_id),
            received_at TEXT NOT NULL,
            UNIQUE(producer_id, correlation_id)
        );
        CREATE TABLE execution_lifecycle_events (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES engineering_transactions(run_id) ON DELETE CASCADE,
            phase TEXT NOT NULL,
            checkpoint TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(run_id, phase, checkpoint)
        );
        CREATE INDEX execution_lifecycle_events_run_lookup
            ON execution_lifecycle_events(run_id, id DESC);
        CREATE TABLE execution_artifact_records (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            digest_algorithm TEXT NOT NULL,
            digest TEXT NOT NULL,
            content_type TEXT NOT NULL,
            run_id TEXT REFERENCES execution_runs(run_id),
            submission_id TEXT REFERENCES execution_submissions(submission_id),
            mission_id TEXT,
            execution_id TEXT,
            producer_id TEXT,
            created_at TEXT NOT NULL,
            integrity_status TEXT NOT NULL,
            storage_location TEXT NOT NULL,
            projection_status TEXT NOT NULL,
            UNIQUE(storage_location),
            UNIQUE(digest_algorithm, digest, storage_location)
        );
        CREATE TABLE execution_projections (
            projection_name TEXT PRIMARY KEY,
            classification TEXT NOT NULL CHECK(classification IN ('PROJECTION','ARTIFACT_PAYLOAD','CONFIGURATION','OBSERVABILITY','RECOVERY_EXPORT')),
            payload TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_migration_provenance (
            source_location TEXT PRIMARY KEY,
            source_digest TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            record_kind TEXT NOT NULL
        );
        """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _legacy_payload(path: Path) -> tuple[dict[str, object], str] | None:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return (payload, hashlib.sha256(raw).hexdigest()) if isinstance(payload, dict) else None


def _schema_v12(connection: sqlite3.Connection) -> None:
    """Import legacy state once, preserving it as verified migration provenance.

    The migration is deliberately one-way.  Once a record is present, normal
    runtime reads never rebuild authority from filesystem projections.
    """
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    root = database.parent.parent
    now = datetime.now(timezone.utc).isoformat()
    candidates: list[tuple[Path, str, str]] = [
        (root / ".engineering" / "status" / "status.json", "watcher_status", "PROJECTION"),
        (root / ".engineering" / "status" / "current.json", "live_status", "PROJECTION"),
    ]
    candidates.extend((path, f"transaction:{path.stem}", "CHECKPOINT") for path in (root / ".engineering" / "engineering-runs").glob("*.json"))
    for path, name, kind in candidates:
        legacy = _legacy_payload(path)
        if legacy is None:
            continue
        payload, digest = legacy
        prior = connection.execute(
            "SELECT source_digest FROM execution_migration_provenance WHERE source_location=?", (str(path),)
        ).fetchone()
        if prior and prior[0] != digest:
            raise EngineeringStorageError("Legacy projection changed during canonical datastore migration.")
        if prior:
            continue
        if kind == "CHECKPOINT":
            run_id = payload.get("run_id")
            phase = payload.get("phase")
            if not isinstance(run_id, str) or not isinstance(phase, str):
                # Keep malformed compatibility files for operator diagnosis;
                # they are never promoted into canonical authority.
                continue
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            existing = connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (run_id,)).fetchone()
            if existing and existing[0] != encoded:
                raise EngineeringStorageError("Legacy checkpoint conflicts with canonical transaction state.")
            connection.execute("INSERT OR IGNORE INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", (run_id, encoded, phase, now))
            connection.execute("INSERT OR IGNORE INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,?)", (run_id, phase, encoded, now))
        else:
            store_projection(connection, name, payload, classification="PROJECTION", updated_at=now)
        connection.execute("INSERT INTO execution_migration_provenance(source_location,source_digest,imported_at,record_kind) VALUES(?,?,?,?)", (str(path), digest, now, kind))


def _schema_v13(connection: sqlite3.Connection) -> None:
    """Import immutable terminal-report metadata once, never during dashboard reads."""
    from .prompt_history import _report_record, _safe_timestamp

    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    root = database.parent.parent
    reports = root / ".engineering" / "reports"
    if not reports.is_dir():
        return
    now = datetime.now(timezone.utc).isoformat()
    for report in reports.glob("*.md"):
        try:
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
        except OSError:
            continue
        record = _report_record(root, report)
        if record is None:
            continue
        source_location = str(report)
        prior = connection.execute(
            "SELECT source_digest FROM execution_migration_provenance WHERE source_location=?", (source_location,)
        ).fetchone()
        if prior and prior[0] != digest:
            raise EngineeringStorageError("Legacy report changed during canonical datastore migration.")
        if prior:
            continue
        run_id = record["run_id"]
        connection.execute(
            "INSERT OR IGNORE INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,retry_of,original_run_id,retry_generation,retry_timestamp,target_branch,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, record["terminal_state"], record["prompt_title"], _safe_timestamp(record["executed_at"]), record["git_commit"],
             str(report.resolve().relative_to(reports.resolve())), record["retry_of"], record["original_run_id"],
             record["retry_generation"], record["retry_timestamp"], record["target_branch"], now),
        )
        run_exists = connection.execute("SELECT 1 FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
        connection.execute(
            "INSERT OR IGNORE INTO execution_artifact_records(artifact_id,artifact_type,digest_algorithm,digest,content_type,run_id,created_at,integrity_status,storage_location,projection_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"legacy-report:{run_id}", "TERMINAL_REPORT", "sha256", digest, "text/markdown", run_id if run_exists else None,
             now, "VERIFIED", str(report.resolve().relative_to((root / ".engineering").resolve())), "AVAILABLE"),
        )
        connection.execute(
            "INSERT INTO execution_migration_provenance(source_location,source_digest,imported_at,record_kind) VALUES(?,?,?,?)",
            (source_location, digest, now, "ARTIFACT_PAYLOAD"),
        )


def _schema_v14(connection: sqlite3.Connection) -> None:
    """Add canonical, bounded ownership leases without assuming old ACTIVE runs live."""
    for statement in """
        CREATE TABLE execution_run_leases (
            lease_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES engineering_transactions(run_id) ON DELETE CASCADE,
            host_identity TEXT NOT NULL,
            host_instance_id TEXT NOT NULL,
            process_id INTEGER,
            acquired_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            lease_state TEXT NOT NULL CHECK(lease_state IN ('ACTIVE','EXPIRED','RELEASED')),
            lease_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX execution_run_one_active_lease
            ON execution_run_leases(run_id) WHERE lease_state='ACTIVE';
        CREATE INDEX execution_run_leases_expiry_lookup
            ON execution_run_leases(lease_state, expires_at);
        CREATE TABLE execution_lease_events (
            id INTEGER PRIMARY KEY,
            lease_id TEXT NOT NULL REFERENCES execution_run_leases(lease_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('LEASE_ACQUIRED','LEASE_EXPIRED','STALE_DETECTED','STALE_RECONCILED','LEASE_RELEASED')),
            outcome TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            UNIQUE(lease_id, event_type, outcome)
        );
        CREATE INDEX execution_lease_events_run_lookup
            ON execution_lease_events(run_id, id DESC);
        CREATE TABLE execution_run_reconciliations (
            run_id TEXT PRIMARY KEY REFERENCES engineering_transactions(run_id) ON DELETE CASCADE,
            outcome TEXT NOT NULL CHECK(outcome IN ('RECOVERABLE','OPERATOR_INTERVENTION_REQUIRED','TERMINAL_EVIDENCE_PRESENT','INCONSISTENT')),
            reason TEXT NOT NULL,
            reconciled_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _schema_v15(connection: sqlite3.Connection) -> None:
    """Persist typed readiness decisions as canonical run-correlated evidence."""
    connection.execute(
        "CREATE TABLE execution_readiness_evaluations ("
        "run_id TEXT PRIMARY KEY REFERENCES engineering_transactions(run_id) ON DELETE CASCADE,"
        "profile_id TEXT NOT NULL,profile_version INTEGER NOT NULL,execution_mode TEXT NOT NULL,"
        "passed INTEGER NOT NULL CHECK(passed IN (0,1)),failed_requirements TEXT NOT NULL,"
        "facts TEXT NOT NULL,evaluated_at TEXT NOT NULL,diagnostic TEXT)"
    )


def _schema_v16(connection: sqlite3.Connection) -> None:
    """Persist immutable Producer Envelope context snapshots and run linkage."""
    for statement in (
        "ALTER TABLE execution_submissions ADD COLUMN execution_context_snapshot TEXT",
        "ALTER TABLE execution_submissions ADD COLUMN execution_context_version TEXT",
        "CREATE TABLE execution_submission_links (submission_id TEXT PRIMARY KEY REFERENCES execution_submissions(submission_id),run_id TEXT NOT NULL,linked_at TEXT NOT NULL)",
        "CREATE UNIQUE INDEX execution_submission_links_run_lookup ON execution_submission_links(run_id)",
    ):
        connection.execute(statement)


def _schema_v17(connection: sqlite3.Connection) -> None:
    """Retain the declared Engineering Action provenance with the submission."""
    connection.execute("ALTER TABLE execution_submissions ADD COLUMN engineering_action_id TEXT")


def _schema_v18(connection: sqlite3.Connection) -> None:
    """Persist immutable operator handling separately from terminal outcome."""
    connection.execute(
        "CREATE TABLE execution_dismissals ("
        "run_id TEXT PRIMARY KEY REFERENCES prompt_execution_history(run_id) ON DELETE RESTRICT,"
        "terminal_state TEXT NOT NULL CHECK(terminal_state IN ('COMPLETE','BLOCKED','FAILED')) ,"
        "handling_state TEXT NOT NULL CHECK(handling_state='DISMISSED'),"
        "dismissed_at TEXT NOT NULL,dismissed_by TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TRIGGER execution_dismissals_immutable_update "
        "BEFORE UPDATE ON execution_dismissals BEGIN "
        "SELECT RAISE(ABORT, 'Execution dismissal evidence is immutable.'); END"
    )
    connection.execute(
        "CREATE TRIGGER execution_dismissals_immutable_delete "
        "BEFORE DELETE ON execution_dismissals BEGIN "
        "SELECT RAISE(ABORT, 'Execution dismissal evidence is immutable.'); END"
    )


def _schema_v19(connection: sqlite3.Connection) -> None:
    """Persist each supplied Forge governance snapshot with its submission."""
    connection.execute("ALTER TABLE execution_submissions ADD COLUMN forge_governance_handoff_snapshot TEXT")
    connection.execute("ALTER TABLE execution_submissions ADD COLUMN forge_governance_handoff_version TEXT")


def _import_legacy_execution_dismissals(root: Path, connection: sqlite3.Connection) -> None:
    """Copy valid legacy dismissal evidence into the canonical datastore.

    The former JSON audit is retained as source evidence, but never consulted
    by projections after its records have been copied to SQLite. Repeating the
    import is safe: the canonical run key makes it idempotent and permits a
    record whose history was backfilled later to be imported on a later open.
    """
    path = root / LEGACY_DISMISSALS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, list):
        return
    for record in payload:
        if not isinstance(record, dict) or record.get("dismissed") is not True:
            continue
        run_id = record.get("run_id")
        terminal_state = record.get("terminal_state")
        dismissed_at = record.get("dismissed_at")
        dismissed_by = record.get("dismissed_by")
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id)
            or terminal_state not in {"COMPLETE", "BLOCKED", "FAILED"}
            or not isinstance(dismissed_at, str)
            or not dismissed_at.strip()
            or not isinstance(dismissed_by, str)
            or not dismissed_by.strip()
        ):
            continue
        history_exists = connection.execute(
            "SELECT 1 FROM prompt_execution_history WHERE run_id=?", (run_id,)
        ).fetchone()
        if history_exists is None:
            continue
        connection.execute(
            "INSERT OR IGNORE INTO execution_dismissals(run_id,terminal_state,handling_state,dismissed_at,dismissed_by) "
            "VALUES(?,?,?,?,?)",
            (run_id, terminal_state, "DISMISSED", dismissed_at.strip(), dismissed_by.strip()),
        )


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
    11: _schema_v11,
    12: _schema_v12,
    13: _schema_v13,
    14: _schema_v14,
    15: _schema_v15,
    16: _schema_v16,
    17: _schema_v17,
    18: _schema_v18,
    19: _schema_v19,
}


def dismissal_for_run(root: Path, run_id: object) -> dict[str, object] | None:
    """Return immutable operator-handling evidence from canonical SQLite."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return None
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT terminal_state,handling_state,dismissed_at,dismissed_by "
            "FROM execution_dismissals WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "run_id": run_id,
        "terminal_state": row[0],
        "dismissed": True,
        "handling_state": row[1],
        "dismissed_at": row[2],
        "dismissed_by": row[3],
    }


def record_execution_dismissal(root: Path, *, run_id: str, terminal_state: str,
                               dismissed_at: str, dismissed_by: str) -> dict[str, object]:
    """Record one immutable dismissal after its terminal history row exists."""
    if terminal_state not in {"COMPLETE", "BLOCKED", "FAILED"}:
        raise EngineeringStorageError("Dismissal requires a terminal execution outcome.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO execution_dismissals(run_id,terminal_state,handling_state,dismissed_at,dismissed_by) "
            "VALUES(?,?,?,?,?)",
            (run_id, terminal_state, "DISMISSED", dismissed_at, dismissed_by),
        )
    except sqlite3.IntegrityError as error:
        raise EngineeringStorageError("Execution dismissal is already recorded or has no terminal history.") from error
    finally:
        connection.close()
    return {
        "run_id": run_id, "terminal_state": terminal_state, "dismissed": True,
        "handling_state": "DISMISSED", "dismissed_at": dismissed_at, "dismissed_by": dismissed_by,
    }


def _encoded_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def store_projection(connection: sqlite3.Connection, name: str, payload: dict[str, object], *, classification: str = "PROJECTION", updated_at: str | None = None) -> None:
    """Persist a bounded projection record before its optional filesystem copy."""
    if classification not in {"PROJECTION", "ARTIFACT_PAYLOAD", "CONFIGURATION", "OBSERVABILITY", "RECOVERY_EXPORT"}:
        raise EngineeringStorageError("Unknown execution projection classification.")
    encoded = _encoded_payload(payload)
    connection.execute(
        "INSERT INTO execution_projections(projection_name,classification,payload,source_digest,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(projection_name) DO UPDATE SET classification=excluded.classification,payload=excluded.payload,source_digest=excluded.source_digest,updated_at=excluded.updated_at",
        (name, classification, encoded, hashlib.sha256(encoded.encode()).hexdigest(), updated_at or datetime.now(timezone.utc).isoformat()),
    )


def load_projection(root: Path, name: str) -> dict[str, object] | None:
    """Load a canonical projection without consulting its compatibility file."""
    connection = open_storage(root)
    try:
        row = connection.execute("SELECT payload FROM execution_projections WHERE projection_name=?", (name,)).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Canonical execution projection is corrupt.") from error
    if not isinstance(payload, dict):
        raise EngineeringStorageError("Canonical execution projection is invalid.")
    return payload


def record_submission(
    root: Path,
    *,
    submission_id: str,
    producer_id: str,
    producer_type: str,
    prompt_content: str,
    prompt_metadata: dict[str, object],
    target_identity: dict[str, object],
    original_envelope: dict[str, object] | str,
    received_at: str,
    producer_version: str | None = None,
    contract_version: str | None = None,
    correlation_id: str | None = None,
    mission_id: str | None = None,
    engineering_action_id: str | None = None,
    execution_run_id: str | None = None,
    link_run_id: str | None = None,
    execution_context: dict[str, object] | None = None,
    forge_governance_handoff: dict[str, object] | None = None,
) -> None:
    """Persist the complete producer envelope before an Inbox file is consumed."""
    if not all(isinstance(value, str) and value for value in (submission_id, producer_id, producer_type, prompt_content, received_at)):
        raise EngineeringStorageError("Execution submission identity is invalid.")
    connection = open_storage(root)
    try:
        encoded_envelope = original_envelope if isinstance(original_envelope, str) else _encoded_payload(original_envelope)
        encoded_context = _encoded_payload(execution_context) if execution_context is not None else None
        encoded_handoff = _encoded_payload(forge_governance_handoff) if forge_governance_handoff is not None else None
        context_version = execution_context.get("context_version") if isinstance(execution_context, dict) else None
        handoff_version = forge_governance_handoff.get("version") if isinstance(forge_governance_handoff, dict) else None
        if context_version is not None and not isinstance(context_version, str):
            raise EngineeringStorageError("Execution Context snapshot version is invalid.")
        if handoff_version is not None and not isinstance(handoff_version, str):
            raise EngineeringStorageError("Forge Governance Handoff snapshot version is invalid.")
        connection.execute(
            "INSERT INTO execution_submissions(submission_id,producer_id,producer_type,producer_version,contract_version,prompt_content,prompt_metadata,target_identity,original_envelope,correlation_id,mission_id,execution_run_id,received_at,execution_context_snapshot,execution_context_version,engineering_action_id,forge_governance_handoff_snapshot,forge_governance_handoff_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(submission_id) DO NOTHING",
            (submission_id, producer_id, producer_type, producer_version, contract_version, prompt_content,
             _encoded_payload(prompt_metadata), _encoded_payload(target_identity), encoded_envelope,
             correlation_id, mission_id, execution_run_id, received_at, encoded_context, context_version, engineering_action_id, encoded_handoff, handoff_version),
        )
        if link_run_id:
            connection.execute(
                "INSERT INTO execution_submission_links(submission_id,run_id,linked_at) VALUES(?,?,?) ON CONFLICT(submission_id) DO NOTHING",
                (submission_id, link_run_id, received_at),
            )
    finally:
        connection.close()


def load_execution_context_snapshot(root: Path, run_id: str) -> dict[str, object] | None:
    """Read only the persisted immutable context snapshot linked to one run."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission.execution_context_snapshot FROM execution_submissions AS submission "
            "JOIN execution_submission_links AS link ON link.submission_id=submission.submission_id "
            "WHERE link.run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    try:
        snapshot = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Persisted Execution Context snapshot is corrupt.") from error
    if not isinstance(snapshot, dict):
        raise EngineeringStorageError("Persisted Execution Context snapshot is invalid.")
    return snapshot


def load_forge_governance_handoff_snapshot(root: Path, run_id: str) -> dict[str, object] | None:
    """Read the immutable Producer-supplied Forge handoff for one run."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission.forge_governance_handoff_snapshot FROM execution_submissions AS submission "
            "JOIN execution_submission_links AS link ON link.submission_id=submission.submission_id "
            "WHERE link.run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    try:
        snapshot = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Persisted Forge Governance Handoff snapshot is corrupt.") from error
    if not isinstance(snapshot, dict):
        raise EngineeringStorageError("Persisted Forge Governance Handoff snapshot is invalid.")
    return snapshot


def load_submission_for_run(root: Path, run_id: str) -> dict[str, object] | None:
    """Load immutable Producer provenance for one linked execution without prompt inspection."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission.submission_id,submission.producer_id,submission.producer_type,"
            "submission.producer_version,submission.contract_version,submission.correlation_id,"
            "submission.mission_id,submission.engineering_action_id,submission.execution_context_version,submission.execution_context_snapshot,"
            "submission.forge_governance_handoff_version,submission.forge_governance_handoff_snapshot "
            "FROM execution_submissions AS submission JOIN execution_submission_links AS link "
            "ON link.submission_id=submission.submission_id WHERE link.run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    snapshot = None
    if row[9] is not None:
        try:
            snapshot = json.loads(row[9])
        except (TypeError, json.JSONDecodeError) as error:
            raise EngineeringStorageError("Persisted Execution Context snapshot is corrupt.") from error
        if not isinstance(snapshot, dict):
            raise EngineeringStorageError("Persisted Execution Context snapshot is invalid.")
    handoff = None
    if row[11] is not None:
        try:
            handoff = json.loads(row[11])
        except (TypeError, json.JSONDecodeError) as error:
            raise EngineeringStorageError("Persisted Forge Governance Handoff snapshot is corrupt.") from error
        if not isinstance(handoff, dict):
            raise EngineeringStorageError("Persisted Forge Governance Handoff snapshot is invalid.")
    return {
        "submission_id": row[0], "producer_id": row[1], "producer_type": row[2],
        "producer_version": row[3], "contract_version": row[4], "correlation_id": row[5],
        "mission_id": row[6], "engineering_action_id": row[7], "execution_context_version": row[8], "execution_context": snapshot,
        "forge_governance_handoff_version": row[10], "forge_governance_handoff": handoff,
    }


def record_artifact(
    root: Path,
    path: Path,
    *,
    artifact_id: str,
    artifact_type: str,
    content_type: str,
    created_at: str,
    run_id: str | None = None,
    submission_id: str | None = None,
    mission_id: str | None = None,
    execution_id: str | None = None,
    producer_id: str | None = None,
    projection_status: str = "AVAILABLE",
) -> None:
    """Register immutable filesystem payload metadata in the canonical store."""
    try:
        location = str(path.resolve().relative_to((root / ".engineering").resolve()))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as error:
        raise EngineeringStorageError("Artifact payload cannot be recorded safely.") from error
    connection = open_storage(root)
    try:
        if run_id and not connection.execute("SELECT 1 FROM execution_runs WHERE run_id=?", (run_id,)).fetchone():
            run_id = None
        if submission_id and not connection.execute("SELECT 1 FROM execution_submissions WHERE submission_id=?", (submission_id,)).fetchone():
            submission_id = None
        connection.execute(
            "INSERT INTO execution_artifact_records(artifact_id,artifact_type,digest_algorithm,digest,content_type,run_id,submission_id,mission_id,execution_id,producer_id,created_at,integrity_status,storage_location,projection_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(artifact_id) DO UPDATE SET digest=excluded.digest,integrity_status=excluded.integrity_status,projection_status=excluded.projection_status",
            (artifact_id, artifact_type, "sha256", digest, content_type, run_id, submission_id, mission_id,
             execution_id, producer_id, created_at, "VERIFIED", location, projection_status),
        )
    finally:
        connection.close()


def verify_artifact_integrity(root: Path, artifact_id: str) -> bool:
    """Verify a registered payload without making the file itself authoritative."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT digest_algorithm,digest,storage_location FROM execution_artifact_records WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if not row:
            return False
        algorithm, expected, location = row
        if algorithm != "sha256" or not isinstance(location, str):
            return False
        try:
            payload = ((root / ".engineering") / location).resolve()
            payload.relative_to((root / ".engineering").resolve())
            actual = hashlib.sha256(payload.read_bytes()).hexdigest()
        except (OSError, ValueError):
            actual = ""
        connection.execute(
            "UPDATE execution_artifact_records SET integrity_status=? WHERE artifact_id=?",
            ("VERIFIED" if actual == expected else "MISMATCH", artifact_id),
        )
        return actual == expected
    finally:
        connection.close()


def regenerate_status_projections(root: Path) -> None:
    """Regenerate compatibility JSON copies from canonical state deterministically."""
    status_directory = root / ".engineering" / "status"
    status_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, filename in (("watcher_status", "status.json"), ("live_status", "current.json")):
        payload = load_projection(root, name)
        if payload is None:
            continue
        temporary = status_directory / f".{filename}.tmp"
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(status_directory / filename)


def import_legacy_projection_once(root: Path, name: str, path: Path) -> dict[str, object] | None:
    """Explicit, idempotent compatibility migration for a pre-v12 projection.

    It is only used when the canonical row is absent. Subsequent reads remain
    database-only, and a changed source fails closed rather than overwriting
    the imported operational record.
    """
    existing = load_projection(root, name)
    if existing is not None:
        return existing
    legacy = _legacy_payload(path)
    if legacy is None:
        return None
    payload, digest = legacy
    connection = open_storage(root)
    try:
        prior = connection.execute(
            "SELECT source_digest FROM execution_migration_provenance WHERE source_location=?", (str(path),)
        ).fetchone()
        if prior and prior[0] != digest:
            raise EngineeringStorageError("Legacy projection conflicts with completed canonical migration.")
        store_projection(connection, name, payload, classification="PROJECTION")
        connection.execute(
            "INSERT OR IGNORE INTO execution_migration_provenance(source_location,source_digest,imported_at,record_kind) VALUES(?,?,?,?)",
            (str(path), digest, datetime.now(timezone.utc).isoformat(), "PROJECTION"),
        )
    finally:
        connection.close()
    return payload


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
    try:
        connection = sqlite3.connect(
            path if create else f"file:{path}?mode=rw",
            timeout=10,
            isolation_level=None,
            uri=not create,
        )
    except sqlite3.DatabaseError as error:
        raise EngineeringStorageError("Engineering storage could not be opened safely.") from error
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
        _import_legacy_execution_dismissals(root, connection)
        connection.execute("COMMIT")
        path.chmod(0o600)
    except (OSError, sqlite3.DatabaseError, EngineeringStorageError) as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        connection.close()
        if isinstance(error, EngineeringStorageError):
            raise
        raise EngineeringStorageError("Engineering storage could not be opened safely.") from error
    return connection


def record_readiness_evaluation(root: Path, *, run_id: str, profile_id: str, profile_version: int,
                                execution_mode: str, passed: bool, failed_requirements: tuple[str, ...],
                                facts: dict[str, object], evaluated_at: str, diagnostic: str | None) -> None:
    """Store one deterministic readiness decision for a transaction."""
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO execution_readiness_evaluations(run_id,profile_id,profile_version,execution_mode,passed,failed_requirements,facts,evaluated_at,diagnostic) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET profile_id=excluded.profile_id,profile_version=excluded.profile_version,execution_mode=excluded.execution_mode,passed=excluded.passed,failed_requirements=excluded.failed_requirements,facts=excluded.facts,evaluated_at=excluded.evaluated_at,diagnostic=excluded.diagnostic",
            (run_id, profile_id, profile_version, execution_mode, int(passed), json.dumps(failed_requirements), json.dumps(facts, sort_keys=True), evaluated_at, diagnostic),
        )
    finally:
        connection.close()


def load_readiness_evaluation(root: Path, run_id: object) -> dict[str, object] | None:
    """Read one canonical readiness projection for dashboard/report consumers."""
    if not isinstance(run_id, str):
        return None
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT profile_id,profile_version,execution_mode,passed,failed_requirements,evaluated_at,diagnostic FROM execution_readiness_evaluations WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return {"profile_id": row[0], "profile_version": row[1], "execution_mode": row[2], "result": "PASS" if row[3] else "BLOCKED", "failed_requirements": json.loads(row[4]), "evaluated_at": row[5], "diagnostic": row[6]}
