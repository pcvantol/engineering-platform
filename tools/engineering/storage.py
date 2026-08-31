"""Versioned SQLite schema contract for private Engineering Platform evidence.

This module owns only the database lifecycle. Consumers are migrated to it in a
separate, compatibility-tested change; an unrecognized database is never
silently replaced or downgraded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import argparse
import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3


WORKSPACE_DIRECTORY = ".engineering"
DATABASE_FILENAME = "engineering.db"
ENGINEERING_STORAGE_SCHEMA_VERSION = 40
JOURNAL_MODES = frozenset({"DELETE", "MEMORY"})
LEGACY_DISMISSALS_PATH = Path(".engineering/status/execution_dismissals.json")
ADMITTED_STORAGE_SCHEMA_ENVIRONMENT = "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA"
ADMITTED_STORAGE_ROOT_ENVIRONMENT = "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT"


class EngineeringStorageError(RuntimeError):
    """Raised when the local Engineering evidence database is unsafe to use."""


def _admitted_migration_ceiling(root: Path) -> int | None:
    """Return the watcher-admitted schema only for its canonical workspace."""
    admitted_root = os.environ.get(ADMITTED_STORAGE_ROOT_ENVIRONMENT)
    admitted_schema = os.environ.get(ADMITTED_STORAGE_SCHEMA_ENVIRONMENT)
    if not admitted_root and not admitted_schema:
        return None
    if not admitted_root or not admitted_schema:
        raise EngineeringStorageError("Engineering storage admission context is incomplete.")
    try:
        ceiling = int(admitted_schema)
    except ValueError as error:
        raise EngineeringStorageError("Engineering storage admission schema is invalid.") from error
    if ceiling < 1:
        raise EngineeringStorageError("Engineering storage admission schema is invalid.")
    try:
        applies = root.resolve() == Path(admitted_root).resolve()
    except OSError as error:
        raise EngineeringStorageError("Engineering storage admission root is invalid.") from error
    return ceiling if applies else None


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
        prompt_size = re.search(r"^- Submitted Prompt Characters: `(\d+)`$", text, re.MULTILINE)
        if prompt_size is not None:
            prompt_characters = int(prompt_size.group(1))
        else:
            # Compatibility for immutable reports written before submitted
            # prompt text was removed from the report header.
            objective = re.search(
                r"^- Objective: (.*?)(?=\n\n## )", text, re.MULTILINE | re.DOTALL
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
    workspace = database.parent
    now = datetime.now(timezone.utc).isoformat()
    candidates: list[tuple[Path, str, str]] = [
        (workspace / "status" / "status.json", "watcher_status", "PROJECTION"),
        (workspace / "status" / "current.json", "live_status", "PROJECTION"),
    ]
    candidates.extend((path, f"transaction:{path.stem}", "CHECKPOINT") for path in (workspace / "engineering-runs").glob("*.json"))
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
    workspace = database.parent
    reports = workspace / "reports"
    if not reports.is_dir():
        return
    now = datetime.now(timezone.utc).isoformat()
    for report in reports.glob("*.md"):
        try:
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
        except OSError:
            continue
        record = _report_record(workspace.parent, report)
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
             now, "VERIFIED", str(report.resolve().relative_to(reports.resolve())), "AVAILABLE"),
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


def _schema_v20(connection: sqlite3.Connection) -> None:
    """Persist immutable, run-scoped execution phase spans.

    Existing executions intentionally receive no synthetic phase records.  A
    span is started and completed at an observed runtime boundary; durations
    are measured by the caller's monotonic clock and timestamps remain UTC.
    """
    for statement in """
        CREATE TABLE execution_phase_spans (
            phase_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            phase_name TEXT NOT NULL,
            phase_category TEXT NOT NULL,
            parent_phase_id TEXT,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
            ordinal INTEGER NOT NULL DEFAULT 1 CHECK(ordinal >= 1),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            outcome TEXT NOT NULL CHECK(outcome IN ('ACTIVE','COMPLETE','FAILED','INTERRUPTED','STALE')),
            metadata TEXT NOT NULL DEFAULT '{}',
            CHECK((completed_at IS NULL AND duration_ms IS NULL AND outcome='ACTIVE')
                  OR (completed_at IS NOT NULL AND duration_ms IS NOT NULL AND outcome != 'ACTIVE')),
            UNIQUE(run_id, ordinal)
        );
        CREATE INDEX execution_phase_spans_run_lookup
            ON execution_phase_spans(run_id, ordinal);
        CREATE INDEX execution_phase_spans_parent_lookup
            ON execution_phase_spans(run_id, parent_phase_id, ordinal);
        """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _schema_v21(connection: sqlite3.Connection) -> None:
    """Persist safe aggregate execution counters with terminal evidence."""
    for statement in (
        "ALTER TABLE prompt_execution_history ADD COLUMN execution_metadata TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE execution_runs ADD COLUMN execution_metadata TEXT NOT NULL DEFAULT '{}'",
    ):
        connection.execute(statement)


def _schema_v22(connection: sqlite3.Connection) -> None:
    """Append immutable provider-invocation evidence and bounded churn counters."""
    connection.execute(
        """CREATE TABLE provider_invocations (
            invocation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            provider TEXT NOT NULL, model TEXT, phase TEXT NOT NULL, role TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT, duration_ms INTEGER,
            input_tokens INTEGER, cached_input_tokens INTEGER, uncached_input_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
            usage_authority TEXT NOT NULL CHECK(usage_authority IN ('AUTHORITATIVE','DERIVED','UNAVAILABLE')),
            speed_state TEXT NOT NULL CHECK(speed_state IN ('FAST','NORMAL_DEFAULT','OTHER','UNKNOWN')),
            retry_ordinal INTEGER NOT NULL DEFAULT 0, estimated_credits REAL, estimated_eur REAL,
            rate_table_version TEXT NOT NULL, churn TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id, ordinal)
        )"""
    )
    connection.execute("CREATE INDEX provider_invocations_run_lookup ON provider_invocations(run_id, ordinal)")


def _schema_v23(connection: sqlite3.Connection) -> None:
    """Add explicit model provenance without modifying historical evidence."""
    connection.execute(
        "ALTER TABLE provider_invocations ADD COLUMN model_authority TEXT NOT NULL DEFAULT 'UNAVAILABLE' "
        "CHECK(model_authority IN ('AUTHORITATIVE','DERIVED','UNAVAILABLE'))"
    )
    connection.execute("ALTER TABLE provider_invocations ADD COLUMN raw_provider_model TEXT")


def _schema_v24(connection: sqlite3.Connection) -> None:
    """Keep bounded, counter-only Codex usage snapshots per invocation."""
    connection.execute(
        """CREATE TABLE provider_usage_snapshots (
            invocation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            input_tokens INTEGER, cached_input_tokens INTEGER, uncached_input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_tokens INTEGER, total_tokens INTEGER,
            input_delta INTEGER, cached_input_delta INTEGER, uncached_input_delta INTEGER,
            output_delta INTEGER,
            PRIMARY KEY(invocation_id, ordinal),
            FOREIGN KEY(invocation_id) REFERENCES provider_invocations(invocation_id)
        )"""
    )


def _schema_v25(connection: sqlite3.Connection) -> None:
    """Repair early v24 snapshot tables missing uncached-token counters.

    Some live databases recorded migration 24 while carrying the initial
    provider-usage table shape.  Keep this migration additive and inspect the
    physical table so both those databases and clean v24 installations migrate
    safely.
    """
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(provider_usage_snapshots)")
    }
    for name in ("uncached_input_tokens", "uncached_input_delta"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE provider_usage_snapshots ADD COLUMN {name} INTEGER"
            )


def _schema_v26(connection: sqlite3.Connection) -> None:
    """Append bounded Managed-autonomy evidence without lifecycle authority."""
    for statement in """
        CREATE TABLE IF NOT EXISTS managed_autonomy_actions (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, action TEXT NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('AUTONOMOUS_EP_ACTION','EXPECTED_OPERATOR_GATE','EXTERNAL_PLATFORM_EVENT','UNPLANNED_MANUAL_INTERVENTION','UNKNOWN_AUTHORITY')),
            actor TEXT NOT NULL, evidence_ref TEXT NOT NULL, observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS managed_autonomy_actions_run_lookup ON managed_autonomy_actions(run_id,id);
        CREATE TABLE IF NOT EXISTS managed_governance_gates (
            run_id TEXT NOT NULL, gate_type TEXT NOT NULL CHECK(gate_type IN ('IMPLEMENTATION_MERGE_APPROVAL','FINALIZATION_MERGE_APPROVAL')),
            gate_authority TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('NOT_REQUIRED','WAITING','SATISFIED','UNAVAILABLE')),
            requested_at TEXT NOT NULL, resolved_at TEXT, resolution_actor TEXT, related_pr INTEGER, phase TEXT NOT NULL,
            PRIMARY KEY(run_id,gate_type)
        );
        CREATE TABLE IF NOT EXISTS managed_validation_observations (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, control TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('PASS','FAIL','NOT_EXECUTED','NOT_APPLICABLE','UNAVAILABLE','WAITING')),
            required INTEGER NOT NULL CHECK(required IN (0,1)), currentness INTEGER NOT NULL, observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS managed_validation_observations_run_lookup ON managed_validation_observations(run_id,control,id);
    """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _schema_v27(connection: sqlite3.Connection) -> None:
    """Append terminal PR-check observations used by Managed report projections."""
    for statement in """
        CREATE TABLE IF NOT EXISTS managed_pr_check_observations (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, pr_number INTEGER NOT NULL,
            pr_role TEXT NOT NULL CHECK(pr_role IN ('IMPLEMENTATION','FINALIZATION')),
            pr_state TEXT NOT NULL, merge_state TEXT NOT NULL,
            merge_commit TEXT, required_checks_state TEXT NOT NULL
                CHECK(required_checks_state IN ('PASS','FAIL','WAITING','UNAVAILABLE')),
            evidence_ref TEXT NOT NULL, observed_at TEXT NOT NULL, currentness INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS managed_pr_check_observations_run_lookup
            ON managed_pr_check_observations(run_id,pr_role,currentness,id);
    """.split(";"):
        if statement.strip():
            connection.execute(statement)


def _schema_v28(connection: sqlite3.Connection) -> None:
    """Keep immutable evidence for an operator-approved emergency rollback."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_emergency_recoveries ("
        "run_id TEXT PRIMARY KEY REFERENCES prompt_execution_history(run_id) ON DELETE RESTRICT,"
        "cancelled_at TEXT NOT NULL,rolled_back INTEGER NOT NULL CHECK(rolled_back IN (0,1)),"
        "removed_branch TEXT,recorded_by TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS execution_emergency_recoveries_immutable_update "
        "BEFORE UPDATE ON execution_emergency_recoveries BEGIN "
        "SELECT RAISE(ABORT, 'Emergency recovery evidence is immutable.'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS execution_emergency_recoveries_immutable_delete "
        "BEFORE DELETE ON execution_emergency_recoveries BEGIN "
        "SELECT RAISE(ABORT, 'Emergency recovery evidence is immutable.'); END"
    )


def _schema_v29(connection: sqlite3.Connection) -> None:
    """Retain append-only Dependabot Inbox-admission evidence before execution."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS dependabot_admission_events ("
        "id INTEGER PRIMARY KEY,repository TEXT NOT NULL,pull_request INTEGER NOT NULL,"
        "head_sha TEXT NOT NULL,head_branch TEXT NOT NULL,submission_id TEXT NOT NULL,"
        "event_type TEXT NOT NULL CHECK(event_type IN ('ENQUEUED')),observed_at TEXT NOT NULL,"
        "UNIQUE(repository,pull_request,event_type))"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS dependabot_admission_events_immutable_update "
        "BEFORE UPDATE ON dependabot_admission_events BEGIN "
        "SELECT RAISE(ABORT, 'Dependabot admission evidence is immutable.'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS dependabot_admission_events_immutable_delete "
        "BEFORE DELETE ON dependabot_admission_events BEGIN "
        "SELECT RAISE(ABORT, 'Dependabot admission evidence is immutable.'); END"
    )


def _schema_v30(connection: sqlite3.Connection) -> None:
    """Durably stage terminal telemetry before its rebuildable projection.

    The outbox is deliberately independent of lifecycle authority.  A terminal
    checkpoint remains authoritative; this table only makes its operational
    telemetry projection recoverable after a watcher or process loss.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS terminal_telemetry_outbox ("
        "run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL "
        "CHECK(source IN ('LIVE_TERMINAL','RECOVERY','BACKFILL')),"
        "state TEXT NOT NULL CHECK(state IN ('PENDING','PROCESSED','FAILED_RETRYABLE')) "
        "DEFAULT 'PENDING', attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,"
        "created_at TEXT NOT NULL, processed_at TEXT)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS terminal_telemetry_outbox_pending_lookup "
        "ON terminal_telemetry_outbox(state,created_at,run_id)"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS terminal_telemetry_outbox_payload_immutable "
        "BEFORE UPDATE OF run_id,payload,source,created_at ON terminal_telemetry_outbox BEGIN "
        "SELECT RAISE(ABORT, 'Terminal telemetry intent is immutable.'); END"
    )


def _schema_v31(connection: sqlite3.Connection) -> None:
    """Keep immutable, operator-invoked PR-evidence backfill decisions.

    A backfill can amend a legacy checkpoint only after live GitHub evidence
    has been verified.  This table retains both applied and deliberately
    skipped decisions, so historical state never gains an unexplained PR.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_pr_evidence_backfills ("
        "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES engineering_transactions(run_id) ON DELETE RESTRICT,"
        "pr_role TEXT NOT NULL CHECK(pr_role IN ('IMPLEMENTATION','FINALIZATION')),"
        "outcome TEXT NOT NULL CHECK(outcome IN ('APPLIED','SKIPPED')),"
        "reason TEXT NOT NULL,pr_number INTEGER,expected_branch TEXT,expected_merge_commit TEXT,"
        "observed_at TEXT NOT NULL,actor TEXT NOT NULL,"
        "CHECK((outcome='APPLIED' AND pr_number IS NOT NULL) OR outcome='SKIPPED')"
        ")"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS execution_pr_evidence_backfills_run_lookup "
        "ON execution_pr_evidence_backfills(run_id,id)"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS execution_pr_evidence_backfills_immutable_update "
        "BEFORE UPDATE ON execution_pr_evidence_backfills BEGIN "
        "SELECT RAISE(ABORT, 'Pull-request evidence backfill audit is immutable.'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS execution_pr_evidence_backfills_immutable_delete "
        "BEFORE DELETE ON execution_pr_evidence_backfills BEGIN "
        "SELECT RAISE(ABORT, 'Pull-request evidence backfill audit is immutable.'); END"
    )


def _schema_v32(connection: sqlite3.Connection) -> None:
    """Persist immutable, provider-free admission decisions for every candidate run."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_admission_decisions ("
        "run_id TEXT PRIMARY KEY,submission_id TEXT NOT NULL REFERENCES execution_submissions(submission_id),"
        "execution_mode TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('PASS','FAIL')),"
        "failed_gate_ids TEXT NOT NULL,evidence TEXT NOT NULL,observed_at TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS execution_admission_decisions_immutable_{operation.casefold()} "
            f"BEFORE {operation} ON execution_admission_decisions BEGIN "
            "SELECT RAISE(ABORT, 'Admission decision evidence is immutable.'); END"
        )


def _schema_v33(connection: sqlite3.Connection) -> None:
    """Persist immutable qualification lineage and resolved validation policy.

    This intentionally adds no historical rows.  A legacy run therefore stays
    unavailable instead of receiving invented fresh-submission or validation
    evidence during schema activation.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_run_qualification_context ("
        "run_id TEXT PRIMARY KEY,submission_id TEXT NOT NULL,"
        "fresh_submission INTEGER NOT NULL CHECK(fresh_submission IN (0,1)),"
        "retry_parent_run_id TEXT,resume_parent_run_id TEXT,recorded_at TEXT NOT NULL,"
        "CHECK(NOT (retry_parent_run_id IS NOT NULL AND resume_parent_run_id IS NOT NULL)),"
        "CHECK((fresh_submission=1 AND retry_parent_run_id IS NULL AND resume_parent_run_id IS NULL) "
        "OR (fresh_submission=0 AND (retry_parent_run_id IS NOT NULL OR resume_parent_run_id IS NOT NULL)))"
        ")"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_validation_profiles ("
        "run_id TEXT PRIMARY KEY,selected_validation_tier TEXT NOT NULL,"
        "validation_profile_version TEXT NOT NULL,required_validation_controls TEXT NOT NULL,"
        "recorded_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_validation_control_results ("
        "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,validation_id TEXT NOT NULL,"
        "category TEXT NOT NULL,control_identity TEXT NOT NULL,required_for_profile INTEGER NOT NULL "
        "CHECK(required_for_profile IN (0,1)),execution_status TEXT NOT NULL,"
        "result TEXT NOT NULL,evidence_ref TEXT NOT NULL,observed_at TEXT NOT NULL,currentness INTEGER NOT NULL,"
        "UNIQUE(run_id,validation_id,currentness))"
    )
    for table in (
        "execution_run_qualification_context", "execution_validation_profiles",
        "execution_validation_control_results",
    ):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} "
                f"BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} evidence is immutable.'); END"
        )


def _schema_v34(connection: sqlite3.Connection) -> None:
    """Store bounded, redacted, run-scoped advisory chat transcripts."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_chat_messages ("
        "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES prompt_execution_history(run_id),"
        "role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,"
        "model TEXT,created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS execution_chat_messages_run_created "
        "ON execution_chat_messages(run_id,id)"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS execution_chat_messages_immutable_update "
        "BEFORE UPDATE ON execution_chat_messages BEGIN "
        "SELECT RAISE(ABORT, 'Chat transcript messages are immutable.'); END"
    )


def _schema_v35(connection: sqlite3.Connection) -> None:
    """Persist immutable command start and terminal evidence as one lineage."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_validation_command_invocations ("
        "run_id TEXT NOT NULL,validation_id TEXT NOT NULL,command_id TEXT NOT NULL,"
        "category TEXT NOT NULL,control_identity TEXT NOT NULL,required_for_profile INTEGER NOT NULL,"
        "started_at TEXT NOT NULL,currentness INTEGER NOT NULL,evidence_ref TEXT NOT NULL,"
        "PRIMARY KEY(run_id,command_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_validation_command_terminals ("
        "run_id TEXT NOT NULL,command_id TEXT NOT NULL,completed_at TEXT NOT NULL,"
        "duration_ms INTEGER,exit_code INTEGER,result TEXT NOT NULL,evidence_ref TEXT NOT NULL,"
        "PRIMARY KEY(run_id,command_id),"
        "FOREIGN KEY(run_id,command_id) REFERENCES execution_validation_command_invocations(run_id,command_id))"
    )
    for table in ("execution_validation_command_invocations", "execution_validation_command_terminals"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} "
                f"BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} evidence is immutable.'); END"
            )


def _schema_v36(connection: sqlite3.Connection) -> None:
    """Persist one immutable, run-bound qualification snapshot prospectively.

    The snapshot is deliberately a bounded JSON document because its fields
    are a versioned projection of already-authoritative run evidence, not a
    second relational evidence engine.  No legacy row is created here.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_run_qualification_snapshots ("
        "run_id TEXT PRIMARY KEY,snapshot_version INTEGER NOT NULL,"
        "snapshot_id TEXT NOT NULL UNIQUE,required_control_snapshot_ref TEXT NOT NULL,"
        "terminal_checkpoint_ref TEXT NOT NULL,payload TEXT NOT NULL,persisted_at TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS execution_run_qualification_snapshots_immutable_{operation.casefold()} "
            f"BEFORE {operation} ON execution_run_qualification_snapshots BEGIN "
            "SELECT RAISE(ABORT, 'Run qualification snapshot is immutable.'); END"
        )


def _schema_v37(connection: sqlite3.Connection) -> None:
    """Persist the forward-only canonical execution activity summary."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_activity_summaries ("
        "run_id TEXT PRIMARY KEY,"
        "summary_version INTEGER NOT NULL,payload TEXT NOT NULL,persisted_at TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS execution_activity_summaries_immutable_{operation.casefold()} "
            f"BEFORE {operation} ON execution_activity_summaries BEGIN "
            "SELECT RAISE(ABORT, 'Execution activity summary is immutable.'); END"
        )
    # A historical test/store can retain migration 37 while replaying its
    # early v24 shape; preserve v25's additive repair without rewriting rows.
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(provider_usage_snapshots)")}
    for name in ("uncached_input_tokens", "uncached_input_delta"):
        if name not in columns:
            connection.execute(f"ALTER TABLE provider_usage_snapshots ADD COLUMN {name} INTEGER")


def _schema_v38(connection: sqlite3.Connection) -> None:
    """Persist one bounded provider-interruption recovery and its process receipt.

    Provider invocation telemetry remains immutable and is appended only when
    an invocation reaches its terminal evidence boundary.  These two tables
    record the preceding launch lifecycle needed to recover safely after the
    Execution Host itself disappears.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_recovery_attempts ("
        "run_id TEXT PRIMARY KEY REFERENCES engineering_transactions(run_id) ON DELETE CASCADE,"
        "recovery_ordinal INTEGER NOT NULL CHECK(recovery_ordinal=1),"
        "maximum_attempts INTEGER NOT NULL CHECK(maximum_attempts=1),"
        "triggering_invocation_id TEXT NOT NULL, replacement_invocation_id TEXT NOT NULL UNIQUE,"
        "lifecycle_phase TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ("
        "'RECOVERY_AVAILABLE','RECOVERY_STARTING','RECOVERY_IN_PROGRESS','RECOVERED',"
        "'EXHAUSTED','PRECHECK_FAILED','AMBIGUOUS')) ,"
        "requested_at TEXT NOT NULL, provider_session_id TEXT UNIQUE, launch_claimed_at TEXT, process_receipt_id TEXT,"
        "process_pid INTEGER, process_group INTEGER, provider_confirmed_active_at TEXT,"
        "completed_at TEXT, result TEXT, result_evidence_ref TEXT, branch TEXT,"
        "worktree_identity TEXT, lease_id TEXT, fault_injection_consumed_at TEXT, diagnostic_code TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_invocation_receipts ("
        "receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES provider_recovery_attempts(run_id) ON DELETE CASCADE,"
        "invocation_id TEXT NOT NULL, launch_state TEXT NOT NULL CHECK(launch_state IN ('CLAIMED','PROCESS_STARTED','TERMINAL')),"
        "provider_session_id TEXT, process_pid INTEGER, process_group INTEGER, process_start_fingerprint TEXT,"
        "process_executable_identity TEXT, started_at TEXT NOT NULL, completed_at TEXT,"
        "outcome TEXT, result_evidence_ref TEXT, UNIQUE(run_id, invocation_id, launch_state))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS provider_recovery_state_lookup ON provider_recovery_attempts(state,requested_at)")
    for table in ("provider_invocation_receipts",):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} evidence is immutable.'); END"
            )


def _schema_v39(connection: sqlite3.Connection) -> None:
    """Add verifier-only Local Consumer API credential authority metadata."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS local_api_credentials ("
        "credential_id TEXT PRIMARY KEY CHECK(length(credential_id) BETWEEN 1 AND 128),"
        "consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128),"
        "project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),"
        "verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32),"
        "fingerprint BLOB NOT NULL UNIQUE CHECK(length(fingerprint)=32),"
        "issued_at TEXT NOT NULL,expires_at TEXT,revoked_at TEXT,"
        "replaced_by_credential_id TEXT REFERENCES local_api_credentials(credential_id))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS local_api_credentials_scope_lookup "
        "ON local_api_credentials(consumer_id,project_id,revoked_at)"
    )


def _schema_v40(connection: sqlite3.Connection) -> None:
    """Add the non-secret Local Consumer API registration authority."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS local_api_consumer_registrations ("
        "consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128),"
        "project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),"
        "status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')),"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,disabled_at TEXT,revoked_at TEXT,"
        "audit_metadata TEXT NOT NULL DEFAULT '{}',"
        "PRIMARY KEY(consumer_id,project_id))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS local_api_consumer_registrations_status_lookup "
        "ON local_api_consumer_registrations(consumer_id,project_id,status)"
    )


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
    20: _schema_v20,
    21: _schema_v21,
    22: _schema_v22,
    23: _schema_v23,
    24: _schema_v24,
    25: _schema_v25,
    26: _schema_v26,
    27: _schema_v27,
    28: _schema_v28,
    29: _schema_v29,
    30: _schema_v30,
    31: _schema_v31,
    32: _schema_v32,
    33: _schema_v33,
    34: _schema_v34,
    35: _schema_v35,
    36: _schema_v36,
    37: _schema_v37,
    38: _schema_v38,
    39: _schema_v39,
    40: _schema_v40,
}


AI_CAPACITY_HISTORY_BUCKET_HOURS = 2


def record_ai_capacity_bi_hourly(
    root: Path, *, provider: str, remaining_percent: float, observed_at: datetime | None = None
) -> None:
    """Keep the lowest remaining capacity in each two-hour UTC bucket.

    This intentionally stores only a percentage and a provider label: no
    account, credit, or request detail is retained. The bounded series uses
    existing observability projection storage, so it does not alter the
    Execution Host's active schema contract.
    """
    normalized_provider = provider.strip()[:120]
    if not normalized_provider:
        return
    try:
        remaining = float(remaining_percent)
    except (TypeError, ValueError):
        return
    if not 0 <= remaining <= 100:
        return
    timestamp = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket = timestamp.replace(
        hour=timestamp.hour - (timestamp.hour % AI_CAPACITY_HISTORY_BUCKET_HOURS),
        minute=0,
        second=0,
        microsecond=0,
    )
    connection = open_storage(root, journal_mode="MEMORY")
    try:
        row = connection.execute(
            "SELECT payload FROM execution_projections WHERE projection_name='ai_capacity_hourly'"
        ).fetchone()
        try:
            payload = json.loads(row[0]) if row else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        providers = payload.get("providers") if isinstance(payload, dict) else None
        providers = providers if isinstance(providers, dict) else {}
        samples = providers.get(normalized_provider)
        samples = samples if isinstance(samples, dict) else {}
        bucket_key = bucket.isoformat()
        existing = samples.get(bucket_key)
        if isinstance(existing, (int, float)) and not isinstance(existing, bool):
            remaining = min(remaining, float(existing))
            if remaining == float(existing):
                return
        samples[bucket_key] = remaining
        cutoff = bucket.timestamp() - 7 * 24 * 3600
        providers[normalized_provider] = {
            key: value
            for key, value in samples.items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (_capacity_timestamp(key) or 0) >= cutoff
        }
        store_projection(
            connection,
            "ai_capacity_hourly",
            {"providers": providers, "updated_at": timestamp.isoformat()},
            classification="OBSERVABILITY",
        )
    finally:
        connection.close()


def ai_capacity_history(root: Path, *, provider: str, hours: int = 168) -> list[dict[str, object]]:
    """Return the rolling two-hour local capacity series for a provider."""
    normalized_provider = provider.strip()[:120]
    if not normalized_provider or hours < 1:
        return []
    cutoff = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    earliest = cutoff.timestamp() - (hours - 1) * 3600
    connection = open_storage(root, journal_mode="MEMORY")
    try:
        row = connection.execute(
            "SELECT payload FROM execution_projections WHERE projection_name='ai_capacity_hourly'"
        ).fetchone()
    finally:
        connection.close()
    try:
        payload = json.loads(row[0]) if row else {}
    except (TypeError, json.JSONDecodeError):
        return []
    providers = payload.get("providers") if isinstance(payload, dict) else None
    samples = providers.get(normalized_provider) if isinstance(providers, dict) else None
    if not isinstance(samples, dict):
        return []
    return [
        {"at": key, "remaining_percent": float(value)}
        for key, value in sorted(samples.items())
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= float(value) <= 100
        and (_capacity_timestamp(key) or 0) >= earliest
        and int((_capacity_timestamp(key) or 0) // 3600) % AI_CAPACITY_HISTORY_BUCKET_HOURS == 0
    ]


def _capacity_timestamp(value: object) -> float | None:
    """Parse a bounded UTC capacity bucket without accepting malformed data."""
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return timestamp.astimezone(timezone.utc).timestamp() if timestamp.tzinfo else None


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


def is_active_blocking_predecessor(root: Path, run_id: object, terminal_state: object) -> bool:
    """Return whether canonical evidence still requires predecessor resolution.

    A terminal ``BLOCKED`` or ``FAILED`` run fails closed unless its immutable
    dismissal record agrees with that terminal evidence.  Dismissal is
    operator handling, not a lifecycle rewrite: callers must retain the
    original terminal state in history while removing only its active queue
    gate.
    """
    if terminal_state not in {"BLOCKED", "FAILED"}:
        return False
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return False
    dismissal = dismissal_for_run(root, run_id)
    if dismissal is None:
        # Older records without canonical operator evidence deliberately stay
        # blocking; absence must never be interpreted as a dismissal.
        return True
    return not (
        dismissal.get("dismissed") is True
        and dismissal.get("handling_state") == "DISMISSED"
        and dismissal.get("terminal_state") == terminal_state
    )


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


def record_emergency_recovery(root: Path, *, run_id: str, cancelled_at: str,
                              rolled_back: bool, removed_branch: str | None) -> None:
    """Append one immutable operator-authorized emergency recovery record."""
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise EngineeringStorageError("Emergency recovery requires a valid Inbox run.")
    if not isinstance(cancelled_at, str) or not cancelled_at.strip():
        raise EngineeringStorageError("Emergency recovery requires a timestamp.")
    if removed_branch is not None and (not isinstance(removed_branch, str) or not removed_branch.startswith("codex/")):
        raise EngineeringStorageError("Emergency recovery branch evidence is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO execution_emergency_recoveries(run_id,cancelled_at,rolled_back,removed_branch,recorded_by) VALUES(?,?,?,?,?)",
            (run_id, cancelled_at.strip(), int(rolled_back), removed_branch, "dashboard_emergency_recovery"),
        )
    except sqlite3.IntegrityError as error:
        raise EngineeringStorageError("Emergency recovery is already recorded or has no terminal history.") from error
    finally:
        connection.close()


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
            existing = connection.execute(
                "SELECT run_id FROM execution_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if existing is not None and existing[0] != link_run_id:
                raise EngineeringStorageError("Execution submission is already bound to a different run.")
            connection.execute(
                "INSERT INTO execution_submission_links(submission_id,run_id,linked_at) VALUES(?,?,?) ON CONFLICT(submission_id) DO NOTHING",
                (submission_id, link_run_id, received_at),
            )
            current = connection.execute(
                "SELECT execution_run_id FROM execution_submissions WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if current is None:
                raise EngineeringStorageError("Execution submission was not persisted.")
            if current[0] not in (None, link_run_id):
                raise EngineeringStorageError("Execution submission is already bound to a different run.")
            # The link is the crash-safe admission-time binding. The FK-backed
            # column is filled as soon as its execution-run row exists.
            if current[0] is None and connection.execute(
                "SELECT 1 FROM execution_runs WHERE run_id=?", (link_run_id,)
            ).fetchone():
                connection.execute(
                    "UPDATE execution_submissions SET execution_run_id=? WHERE submission_id=? AND execution_run_id IS NULL",
                    (link_run_id, submission_id),
                )
    finally:
        connection.close()


def record_admission_decision(
    root: Path,
    *,
    run_id: str,
    submission_id: str,
    execution_mode: str,
    decision: str,
    failed_gate_ids: tuple[str, ...],
    evidence: tuple[dict[str, object], ...],
    observed_at: str,
) -> None:
    """Persist one bounded admission outcome before any provider-backed work.

    Admission evidence intentionally contains only gate identifiers, expected and
    observed states, and timestamps.  Prompt content, provider output and
    diagnostics remain outside this projection.
    """
    if decision not in {"PASS", "FAIL"} or not run_id or not submission_id:
        raise EngineeringStorageError("Admission decision identity is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_admission_decisions(run_id,submission_id,execution_mode,decision,failed_gate_ids,evidence,observed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                submission_id,
                execution_mode,
                decision,
                _encoded_payload({"gate_ids": list(failed_gate_ids)}),
                _encoded_payload({"gates": list(evidence)}),
                observed_at,
            ),
        )
    finally:
        connection.close()


def load_admission_decision(root: Path, run_id: str) -> dict[str, object] | None:
    """Load one structured provider-free admission decision for rendering."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission_id,execution_mode,decision,failed_gate_ids,evidence,observed_at "
            "FROM execution_admission_decisions WHERE run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    try:
        failed = json.loads(row[3])
        evidence = json.loads(row[4])
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Admission decision evidence is corrupt.") from error
    if not isinstance(failed, dict) or not isinstance(evidence, dict):
        raise EngineeringStorageError("Admission decision evidence is invalid.")
    return {
        "run_id": run_id,
        "submission_id": row[0],
        "execution_mode": row[1],
        "decision": row[2],
        "failed_gate_ids": failed.get("gate_ids", []),
        "gates": evidence.get("gates", []),
        "observed_at": row[5],
    }


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


def load_run_lineage(root: Path, run_id: str) -> dict[str, object] | None:
    """Load explicit qualification lineage; legacy rows deliberately remain unavailable."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT submission_id,fresh_submission,retry_parent_run_id,resume_parent_run_id,recorded_at "
            "FROM execution_run_qualification_context WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "submission_id": row[0], "fresh_submission": bool(row[1]),
        "retry_parent": row[2], "resume_parent": row[3], "recorded_at": row[4],
    }


def record_run_qualification_context(
    root: Path, *, run_id: str, submission_id: str, fresh_submission: bool,
    retry_parent_run_id: str | None, resume_parent_run_id: str | None,
    recorded_at: str,
) -> None:
    """Persist one immutable lineage statement before provider work starts."""
    if not all(isinstance(value, str) and value for value in (run_id, submission_id, recorded_at)):
        raise EngineeringStorageError("Qualification lineage identity is invalid.")
    if retry_parent_run_id is not None and resume_parent_run_id is not None:
        raise EngineeringStorageError("Qualification lineage cannot have dual parents.")
    if fresh_submission != (retry_parent_run_id is None and resume_parent_run_id is None):
        raise EngineeringStorageError("Qualification lineage fresh state is inconsistent.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_run_qualification_context("
            "run_id,submission_id,fresh_submission,retry_parent_run_id,resume_parent_run_id,recorded_at) VALUES(?,?,?,?,?,?)",
            (run_id, submission_id, int(fresh_submission), retry_parent_run_id, resume_parent_run_id, recorded_at),
        )
    finally:
        connection.close()


def record_validation_profile(
    root: Path, *, run_id: str, selected_validation_tier: str, validation_profile_version: str,
    required_validation_controls: tuple[str, ...], recorded_at: str,
    profile_reference: str | None = None, profile_selection_source: str | None = None,
    control_bindings: tuple[dict[str, object], ...] | None = None,
) -> None:
    """Persist the exact mandatory controls before their execution evidence."""
    if not run_id or not selected_validation_tier or not validation_profile_version or not recorded_at:
        raise EngineeringStorageError("Validation profile identity is invalid.")
    if not required_validation_controls or any(not control for control in required_validation_controls):
        raise EngineeringStorageError("Validation profile controls are invalid.")
    if control_bindings is None:
        # Compatibility for existing callers while new lifecycle paths persist
        # their binding explicitly before scheduling anything.
        from .validation_profile import control_binding
        resolved = tuple(control_binding(control) for control in required_validation_controls)
        control_bindings = tuple(
            binding if binding is not None else {
                "validation_id": control, "required": True, "category": "unavailable",
                "control_identity": control, "command": [],
            }
            for control, binding in zip(required_validation_controls, resolved, strict=True)
        )
    binding_ids = tuple(binding.get("validation_id") for binding in control_bindings if isinstance(binding, dict))
    if binding_ids != required_validation_controls:
        raise EngineeringStorageError("Validation profile bindings are invalid.")
    payload = {
        "validation_ids": list(required_validation_controls),
        "profile_reference": profile_reference or f"validation-profile-registry:{selected_validation_tier}@{validation_profile_version}",
        "profile_selection_source": profile_selection_source or "registry",
        "control_bindings": list(control_bindings),
    }
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_validation_profiles(run_id,selected_validation_tier,validation_profile_version,required_validation_controls,recorded_at) VALUES(?,?,?,?,?)",
            (run_id, selected_validation_tier, validation_profile_version, _encoded_payload(payload), recorded_at),
        )
    finally:
        connection.close()


def record_validation_control_result(
    root: Path, *, run_id: str, validation_id: str, category: str, control_identity: str,
    required_for_profile: bool, execution_status: str, result: str, evidence_ref: str,
    observed_at: str, currentness: int,
) -> None:
    """Append one machine-readable validation observation for its resolved profile."""
    if not all(isinstance(value, str) and value for value in (run_id, validation_id, category, control_identity, execution_status, result, evidence_ref, observed_at)) or currentness < 0:
        raise EngineeringStorageError("Validation control result is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_validation_control_results("
            "run_id,validation_id,category,control_identity,required_for_profile,execution_status,result,evidence_ref,observed_at,currentness) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, validation_id, category, control_identity, int(required_for_profile), execution_status, result, evidence_ref, observed_at, currentness),
        )
    finally:
        connection.close()


def record_validation_command_invocation(
    root: Path, *, run_id: str, validation_id: str, command_id: str, category: str,
    control_identity: str, required_for_profile: bool, started_at: str, currentness: int,
) -> None:
    """Record authoritative command start before subprocess completion is known."""
    values = (run_id, validation_id, command_id, category, control_identity, started_at)
    if not all(isinstance(value, str) and value for value in values) or currentness < 0:
        raise EngineeringStorageError("Validation command invocation is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_validation_command_invocations("
            "run_id,validation_id,command_id,category,control_identity,required_for_profile,started_at,currentness,evidence_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, validation_id, command_id, category, control_identity, int(required_for_profile), started_at, currentness, "command_invocation"),
        )
    finally:
        connection.close()


def record_validation_command_terminal(
    root: Path, *, run_id: str, command_id: str, completed_at: str, exit_code: int | None,
    evidence_ref: str = "command_terminal",
) -> None:
    """Close a previously recorded command with its observed terminal outcome."""
    if not all(isinstance(value, str) and value for value in (run_id, command_id, completed_at, evidence_ref)):
        raise EngineeringStorageError("Validation command terminal evidence is invalid.")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise EngineeringStorageError("Validation command exit code is invalid.")
    result = "PASS" if exit_code == 0 else "FAIL" if exit_code is not None else "UNAVAILABLE"
    connection = open_storage(root)
    try:
        started = connection.execute(
            "SELECT started_at FROM execution_validation_command_invocations WHERE run_id=? AND command_id=?",
            (run_id, command_id),
        ).fetchone()
        duration_ms = None
        if started and isinstance(started[0], str):
            try:
                duration_ms = max(0, round((datetime.fromisoformat(completed_at) - datetime.fromisoformat(started[0])).total_seconds() * 1000))
            except ValueError:
                pass
        connection.execute(
            "INSERT OR IGNORE INTO execution_validation_command_terminals("
            "run_id,command_id,completed_at,duration_ms,exit_code,result,evidence_ref) VALUES(?,?,?,?,?,?,?)",
            (run_id, command_id, completed_at, duration_ms, exit_code, result, evidence_ref),
        )
    finally:
        connection.close()


def load_validation_context(root: Path, run_id: str) -> dict[str, object] | None:
    """Return the resolved profile and current control evidence without inference."""
    connection = open_storage(root)
    try:
        profile = connection.execute(
            "SELECT selected_validation_tier,validation_profile_version,required_validation_controls,recorded_at "
            "FROM execution_validation_profiles WHERE run_id=?", (run_id,)
        ).fetchone()
        rows = connection.execute(
            "SELECT validation_id,category,control_identity,required_for_profile,execution_status,result,evidence_ref,observed_at,currentness "
            "FROM execution_validation_control_results WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        command_rows = connection.execute(
            "SELECT inv.validation_id,inv.command_id,inv.category,inv.control_identity,inv.required_for_profile,"
            "inv.started_at,inv.currentness,term.completed_at,term.duration_ms,term.exit_code,term.result,term.evidence_ref "
            "FROM execution_validation_command_invocations inv "
            "LEFT JOIN execution_validation_command_terminals term "
            "ON term.run_id=inv.run_id AND term.command_id=inv.command_id "
            "WHERE inv.run_id=? ORDER BY inv.started_at", (run_id,)
        ).fetchall()
        diagnostic_artifacts = {
            row[0]: row[1] for row in connection.execute(
                "SELECT artifact_id,execution_id FROM execution_artifact_records "
                "WHERE artifact_type='VALIDATION_FAILURE_DIAGNOSTIC' AND projection_status='AVAILABLE'"
            ).fetchall()
        }
    finally:
        connection.close()
    if profile is None:
        return None
    try:
        payload = json.loads(profile[2])
        required = payload.get("validation_ids", [])
    except (TypeError, json.JSONDecodeError, AttributeError) as error:
        raise EngineeringStorageError("Validation profile controls are corrupt.") from error
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise EngineeringStorageError("Validation profile controls are invalid.")
    bindings = payload.get("control_bindings", [])
    if not isinstance(bindings, list) or any(not isinstance(binding, dict) for binding in bindings):
        raise EngineeringStorageError("Validation profile bindings are invalid.")
    if bindings and tuple(binding.get("validation_id") for binding in bindings) != tuple(required):
        raise EngineeringStorageError("Validation profile bindings are invalid.")
    controls: dict[str, dict[str, object]] = {}
    for row in rows:
        validation_id, category, identity, is_required, status, result, evidence_ref, observed_at, currentness = row
        current = controls.get(validation_id)
        if current is None or int(currentness) > int(current["currentness"]):
            controls[validation_id] = {"validation_id": validation_id, "category": category, "control_identity": identity,
                "required_for_profile": bool(is_required), "execution_status": status, "result": result,
                "evidence_ref": evidence_ref, "observed_at": observed_at, "currentness": currentness}
        elif int(currentness) == int(current["currentness"]) and result != current["result"]:
            controls[validation_id] = {**current, "result": "UNRESOLVED", "conflict": True}
    for row in command_rows:
        validation_id, command_id, category, identity, is_required, started_at, currentness, completed_at, duration_ms, exit_code, result, terminal_ref = row
        diagnostic_artifact_id = f"validation-failure-diagnostic-{command_id}"
        diagnostic_execution_id = diagnostic_artifacts.get(diagnostic_artifact_id)
        controls[validation_id] = {
            "validation_id": validation_id, "category": category, "control_identity": identity,
            "required_for_profile": bool(is_required), "execution_status": "EXECUTED",
            "result": result or "UNAVAILABLE", "evidence_ref": terminal_ref if completed_at else "command_invocation",
            "observed_at": completed_at or started_at, "currentness": currentness,
            "started_at": started_at, "ended_at": completed_at, "duration_ms": duration_ms, "exit_code": exit_code,
            # The artifact id is deterministically derived from the immutable
            # command receipt and the writer persists that command as the
            # artifact execution identity.  Older unbound artifacts remain
            # historical only; projections never backfill their association.
            "diagnostic_evidence_ref": f"artifact:{diagnostic_artifact_id}" if diagnostic_execution_id == command_id else "UNAVAILABLE",
        }
    return {"selected_validation_tier": profile[0], "validation_profile_version": profile[1],
            "profile_reference": payload.get("profile_reference", "UNAVAILABLE"),
            "profile_selection_source": payload.get("profile_selection_source", "UNAVAILABLE"),
            "required_validation_controls": tuple(required), "control_bindings": tuple(bindings),
            "recorded_at": profile[3], "controls": controls}


def record_run_qualification_snapshot(root: Path, snapshot: dict[str, object]) -> dict[str, object]:
    """Store the first authoritative qualification projection for one run.

    This is intentionally insert-only: terminal reporting, receipts and the
    dashboard all consume this same snapshot after it has been persisted.
    """
    run_id = snapshot.get("run_id")
    snapshot_id = snapshot.get("qualification_snapshot_id")
    required_ref = snapshot.get("required_control_snapshot_ref")
    terminal_ref = snapshot.get("terminal_checkpoint_ref")
    persisted_at = snapshot.get("persisted_at")
    if not all(isinstance(value, str) and value for value in (run_id, snapshot_id, required_ref, terminal_ref, persisted_at)):
        raise EngineeringStorageError("Run qualification snapshot identity is invalid.")
    if snapshot.get("required_validation_state") not in {"PASS", "FAIL", "UNRESOLVED"}:
        raise EngineeringStorageError("Run qualification snapshot validation state is invalid.")
    if snapshot.get("cleanup_outcome") not in {"COMPLETED", "NOT_REQUIRED", "FAILED", "UNAVAILABLE"}:
        raise EngineeringStorageError("Run qualification snapshot cleanup outcome is invalid.")
    if snapshot.get("run_qualification") not in {"QUALIFIED", "NOT_QUALIFIED"}:
        raise EngineeringStorageError("Run qualification snapshot outcome is invalid.")
    if snapshot.get("run_qualification") == "QUALIFIED" and snapshot.get("terminal_execution_state") != "COMPLETE":
        raise EngineeringStorageError("A non-complete terminal run cannot be qualified.")
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO execution_run_qualification_snapshots("
            "run_id,snapshot_version,snapshot_id,required_control_snapshot_ref,terminal_checkpoint_ref,payload,persisted_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, 1, snapshot_id, required_ref, terminal_ref, payload, persisted_at),
        )
    finally:
        connection.close()
    stored = load_run_qualification_snapshot(root, run_id)
    if stored is None:
        raise EngineeringStorageError("Run qualification snapshot was not persisted.")
    return stored


def load_run_qualification_snapshot(root: Path, run_id: str) -> dict[str, object] | None:
    """Load a persisted snapshot without deriving or backfilling legacy runs."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT payload FROM execution_run_qualification_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        # A supported historical database can legitimately predate v36.
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Run qualification snapshot is corrupt.") from error
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise EngineeringStorageError("Run qualification snapshot is invalid.")
    return payload


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
    # A transient Codex action title belongs only to the current local status
    # file.  It must never be promoted into canonical storage during a legacy
    # projection import.
    if name == "live_status":
        payload.pop("transient_action", None)
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


def storage_activation_required(root: Path) -> bool:
    """Read whether an existing shared store is behind this source's schema.

    This deliberately opens SQLite read-only and never invokes a migration. It
    is used only to explain why a persistent component cannot start.
    """
    path = database_path(root)
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _schema_version(connection) < ENGINEERING_STORAGE_SCHEMA_VERSION
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, EngineeringStorageError):
        return False


def _assert_controlled_schema_activation(root: Path, path: Path) -> None:
    """Refuse an upgrade while any EP execution or durable component is live.

    This is intentionally independent of the execution-host environment. A
    provider child can otherwise escape its admission environment and import
    newer source against the shared database directly.
    """
    if path.is_file():
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "execution_run_leases" in tables:
                    active = connection.execute(
                        "SELECT 1 FROM execution_run_leases WHERE lease_state='ACTIVE' AND expires_at>=? LIMIT 1",
                        (datetime.now(timezone.utc).isoformat(),),
                    ).fetchone()
                    if active is not None:
                        raise EngineeringStorageError(
                            "Engineering storage activation requires no active execution lease."
                        )
                if "engineering_transactions" in tables:
                    active_transaction = connection.execute(
                        "SELECT 1 FROM engineering_transactions "
                        "WHERE phase NOT IN ('COMPLETE','BLOCKED','FAILED') LIMIT 1"
                    ).fetchone()
                    if active_transaction is not None:
                        raise EngineeringStorageError(
                            "Engineering storage activation requires no non-terminal execution."
                        )
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            raise EngineeringStorageError("Engineering storage activation cannot inspect the shared database.") from error
    locks = root.resolve() / WORKSPACE_DIRECTORY / "locks"
    for component in ("inbox-watcher", "dashboard"):
        lock = locks / f"{component}.lock"
        if not lock.exists():
            continue
        try:
            with lock.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise EngineeringStorageError(
                        f"Engineering storage activation requires {component} to stop first."
                    ) from error
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise EngineeringStorageError("Engineering storage activation cannot verify component ownership.") from error


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
    root: Path, *, create: bool = True, journal_mode: str = "DELETE", allow_schema_upgrade: bool = False
) -> sqlite3.Connection:
    """Open, upgrade and validate the private SQLite evidence database.

    New private stores are initialized transactionally. An existing shared
    store never upgrades implicitly: version changes require
    :func:`activate_storage_schema` after all persistent EP components have
    stopped. SQLite rollback-journal mode intentionally avoids persistent WAL
    sidecars in `.engineering`. Background best-effort writers may request an
    in-memory journal so their temporary transaction files cannot race
    workspace cleanup.
    """
    if journal_mode not in JOURNAL_MODES:
        raise ValueError("Unsupported SQLite journal mode.")
    path = database_path(root)
    new_store = not path.exists()
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
        admitted_ceiling = _admitted_migration_ceiling(root)
        if (
            admitted_ceiling is not None
            and current < ENGINEERING_STORAGE_SCHEMA_VERSION
            and ENGINEERING_STORAGE_SCHEMA_VERSION > admitted_ceiling
        ):
            raise EngineeringStorageError(
                "Engineering storage migration is deferred until the Execution Host is upgraded."
            )
        if current < ENGINEERING_STORAGE_SCHEMA_VERSION and not (new_store or allow_schema_upgrade):
            raise EngineeringStorageError(
                "Engineering storage migration requires controlled post-merge activation."
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


def activate_storage_schema(root: Path) -> sqlite3.Connection:
    """Upgrade a shared EP database only at a controlled post-merge boundary."""
    root = root.resolve()
    path = database_path(root)
    _assert_controlled_schema_activation(root, path)
    return open_storage(root, allow_schema_upgrade=True)


def main(argv: list[str] | None = None) -> int:
    """Run the narrow, operator-owned shared-storage activation command."""
    parser = argparse.ArgumentParser(prog="engineering-storage")
    parser.add_argument("command", choices=("activate",))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        with activate_storage_schema(args.repo.resolve()) as connection:
            version = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0]
    except EngineeringStorageError as error:
        print(f"BLOCKED: {error}")
        return 2
    print(f"ACTIVATED: engineering storage schema {version}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
