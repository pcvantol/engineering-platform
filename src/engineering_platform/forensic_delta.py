"""Deterministic, read-only SQLite forensic delta export for Engineering Platform stores."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import unicodedata


REPORT_VERSION = "1.0"
_REFERENCE_COLUMNS = frozenset({
    "submission_id", "run_id", "execution_run_id", "invocation_id", "consumer_id", "project_id",
    "producer_id", "recovery_id", "original_id", "replacement_id", "qualification_run_id",
    "reconciliation_run_id",
})
_SAFE_VALUE_COLUMNS = frozenset({
    "artifact_id", "classification", "consumer_id", "contract_version", "created_at",
    "credential_id", "disabled_at", "execution_id", "execution_mode", "fingerprint",
    "id", "integrity_status", "invocation_id", "issued_at", "lease_id", "mission_id",
    "observed_at", "ordinal", "phase", "producer_id", "producer_type", "project_id",
    "purpose", "received_at", "repository", "run_id", "schema", "status", "submission_id",
    "updated_at", "version",
}) | _REFERENCE_COLUMNS
_SENSITIVE_WORDS = frozenset({
    "authorization", "bearer", "credential", "history", "password", "prompt", "raw_audio",
    "secret", "token", "verifier",
})
# These are only used after a table has no declared primary or total UNIQUE key.
# They are canonical EP identities, not a row-order fallback.
_CANONICAL_COMPOSITE_KEYS = {
    "local_api_consumer_registrations": ("consumer_id", "project_id"),
}


class ForensicDeltaError(RuntimeError):
    """Raised when an input cannot be safely inspected read-only."""

    code = "FORENSIC_DELTA_UNREADABLE"


@dataclass(frozen=True)
class KeyDefinition:
    columns: tuple[str, ...]
    source: str

    def report(self) -> dict[str, object]:
        return {"columns": list(self.columns), "source": self.source}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalise(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"type": "float", "value": repr(value)}
        return value
    if isinstance(value, bytes):
        return {"type": "blob", "sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, (dict, list)):
            return {"type": "json", "value": parsed}
        return text
    return {"type": type(value).__name__, "value": str(value)}


def _is_sensitive(column: str) -> bool:
    name = column.casefold()
    if name in {"credential_id", "credential_fingerprint", "fingerprint"}:
        return False
    return any(word in name for word in _SENSITIVE_WORDS)


def _safe_value(column: str, value: object) -> object:
    if _is_sensitive(column):
        return "REDACTED"
    if column.casefold() in _SAFE_VALUE_COLUMNS or column.casefold().endswith(("_at", "_status")):
        return _normalise(value)
    return {"sha256": _digest(_normalise(value))}


def _fingerprint(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ForensicDeltaError(f"database is not a regular file: {path}")
    state = path.stat()
    payload: dict[str, object] = {
        "path": str(path.resolve()), "size_bytes": state.st_size, "modified_ns": state.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file():
            payload[suffix[1:]] = {
                "size_bytes": sidecar.stat().st_size,
                "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            }
    return payload


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]


def _key_definition(connection: sqlite3.Connection, table: str) -> KeyDefinition | None:
    info = list(connection.execute(f"PRAGMA table_info({_quote(table)})"))
    column_info = {str(row[1]): row for row in info}
    primary = tuple(str(row[1]) for row in sorted(info, key=lambda row: int(row[5])) if int(row[5]) > 0)
    if primary:
        return KeyDefinition(primary, "PRIMARY_KEY")
    for index in connection.execute(f"PRAGMA index_list({_quote(table)})"):
        # seq, name, unique, origin, partial: partial unique indexes are not total row identities.
        if not int(index[2]) or (len(index) > 4 and int(index[4])):
            continue
        name = str(index[1])
        columns = tuple(str(row[2]) for row in connection.execute(f"PRAGMA index_info({_quote(name)})"))
        if columns and all(int(column_info[column][3]) for column in columns):
            return KeyDefinition(columns, "UNIQUE_INDEX")
    registered = _CANONICAL_COMPOSITE_KEYS.get(table)
    if registered and all(column in column_info for column in registered):
        return KeyDefinition(registered, "REGISTERED_COMPOSITE")
    return None


def _rows(connection: sqlite3.Connection, table: str, key: KeyDefinition) -> dict[str, dict[str, object]]:
    columns = _columns(connection, table)
    select = ", ".join(_quote(column) for column in columns)
    result: dict[str, dict[str, object]] = {}
    for values in connection.execute(f"SELECT {select} FROM {_quote(table)}"):
        row = dict(zip(columns, values, strict=True))
        key_value = [ _normalise(row[column]) for column in key.columns ]
        encoded = _canonical_json(key_value)
        if encoded in result:
            raise ForensicDeltaError(f"non-unique discovered key for {table}")
        result[encoded] = row
    return result


def _row_digest(row: dict[str, object]) -> str:
    return _digest({column: _normalise(value) for column, value in sorted(row.items())})


def _evidence(change_type: str, canonical_key: str, baseline: dict[str, object] | None,
              candidate: dict[str, object] | None) -> dict[str, object]:
    row = candidate if candidate is not None else baseline
    assert row is not None
    result: dict[str, object] = {"change_type": change_type, "canonical_key": json.loads(canonical_key)}
    if baseline is not None:
        result["row_digest_baseline"] = _row_digest(baseline)
    if candidate is not None:
        result["row_digest_candidate"] = _row_digest(candidate)
    for column in sorted(row):
        if column in _REFERENCE_COLUMNS or column.endswith("_id") or column in {"purpose", "status"} or column.endswith("_at"):
            result[column] = _safe_value(column, row[column])
    if baseline is not None and candidate is not None:
        changed = []
        for column in sorted(set(baseline) | set(candidate)):
            before, after = baseline.get(column), candidate.get(column)
            if _normalise(before) != _normalise(after):
                changed.append({"column": column, "baseline": _safe_value(column, before), "candidate": _safe_value(column, after)})
        result["changed_fields"] = changed
    return result


def _graph_edges(source: str, table: str, rows: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    edges = []
    for canonical_key, row in rows.items():
        for column in sorted(row):
            if column in _REFERENCE_COLUMNS or column.endswith("_id"):
                if row[column] is not None:
                    edges.append({"source": source, "table_name": table, "canonical_key": json.loads(canonical_key),
                                  "reference_type": column, "reference": _safe_value(column, row[column])})
    return edges


def export_forensic_delta(baseline: Path, candidate: Path, *, migration_id: str) -> dict[str, object]:
    """Compare two SQLite files without opening either through a writable API."""
    baseline, candidate = baseline.resolve(), candidate.resolve()
    before_baseline, before_candidate = _fingerprint(baseline), _fingerprint(candidate)
    table_reports: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    try:
        with closing(_readonly(baseline)) as baseline_connection, closing(_readonly(candidate)) as candidate_connection:
            baseline_tables, candidate_tables = _tables(baseline_connection), _tables(candidate_connection)
            all_tables = sorted(set(baseline_tables) | set(candidate_tables))
            for table in all_tables:
                baseline_exists, candidate_exists = table in baseline_tables, table in candidate_tables
                baseline_columns = _columns(baseline_connection, table) if baseline_exists else []
                candidate_columns = _columns(candidate_connection, table) if candidate_exists else []
                baseline_key = _key_definition(baseline_connection, table) if baseline_exists else None
                candidate_key = _key_definition(candidate_connection, table) if candidate_exists else None
                key = candidate_key if candidate_exists else baseline_key
                key_mismatch = (
                    baseline_key is not None and candidate_key is not None
                    and baseline_key.columns != candidate_key.columns
                )
                report: dict[str, object] = {
                    "table_name": table,
                    "key_status": "RESOLVED" if key and not key_mismatch else "KEY_UNRESOLVED",
                    "key_definition": key.report() if key and not key_mismatch else None,
                    "columns_baseline_only": sorted(set(baseline_columns) - set(candidate_columns)),
                    "columns_candidate_only": sorted(set(candidate_columns) - set(baseline_columns)),
                    "changes": [],
                }
                if key is None or key_mismatch:
                    report.update({"baseline_count": None, "candidate_count": None, "unchanged_count": 0,
                                   "added_count": 0, "removed_count": 0, "modified_count": 0})
                    if key_mismatch:
                        report["diagnostic"] = "KEY_DEFINITION_MISMATCH"
                    table_reports.append(report)
                    continue
                baseline_rows = _rows(baseline_connection, table, key) if baseline_exists else {}
                candidate_rows = _rows(candidate_connection, table, key) if candidate_exists else {}
                changes = []
                unchanged = added = removed = modified = 0
                for canonical_key in sorted(set(baseline_rows) | set(candidate_rows)):
                    before, after = baseline_rows.get(canonical_key), candidate_rows.get(canonical_key)
                    if before is None:
                        added += 1
                        changes.append(_evidence("ADDED", canonical_key, None, after))
                    elif after is None:
                        removed += 1
                        changes.append(_evidence("REMOVED", canonical_key, before, None))
                    elif _row_digest(before) == _row_digest(after):
                        unchanged += 1
                    else:
                        modified += 1
                        changes.append(_evidence("MODIFIED", canonical_key, before, after))
                edges.extend(_graph_edges("baseline", table, baseline_rows))
                edges.extend(_graph_edges("candidate", table, candidate_rows))
                report.update({"baseline_count": len(baseline_rows), "candidate_count": len(candidate_rows),
                               "unchanged_count": unchanged, "added_count": added, "removed_count": removed,
                               "modified_count": modified, "changes": changes})
                table_reports.append(report)
    except sqlite3.Error as error:
        raise ForensicDeltaError(f"read-only SQLite inspection failed: {error}") from error
    after_baseline, after_candidate = _fingerprint(baseline), _fingerprint(candidate)
    if before_baseline != after_baseline or before_candidate != after_candidate:
        raise ForensicDeltaError("input fingerprint changed during read-only export")
    keyed = [table for table in table_reports if table["key_status"] == "RESOLVED"]
    changed = [change for table in keyed for change in table["changes"]]
    unresolved = [table["table_name"] for table in table_reports if table["key_status"] == "KEY_UNRESOLVED"]
    report: dict[str, object] = {
        "report_version": REPORT_VERSION, "migration_id": migration_id,
        "baseline": before_baseline, "candidate": before_candidate,
        "read_only_verified": True,
        "diagnostics": {"key_unresolved_tables": unresolved},
        "schema_difference": {"tables_baseline_only": sorted(set(baseline_tables) - set(candidate_tables)),
                              "tables_candidate_only": sorted(set(candidate_tables) - set(baseline_tables))},
        "summary": {"tables_compared": len(keyed), "tables_baseline_only": len(set(baseline_tables) - set(candidate_tables)),
                    "tables_candidate_only": len(set(candidate_tables) - set(baseline_tables)),
                    "tables_key_unresolved": len(table_reports) - len(keyed),
                    "rows_added": sum(table["added_count"] for table in keyed),
                    "rows_removed": sum(table["removed_count"] for table in keyed),
                    "rows_modified": sum(table["modified_count"] for table in keyed),
                    "changed_rows_with_run_id": sum("run_id" in change or "execution_run_id" in change for change in changed),
                    "changed_rows_with_submission_id": sum("submission_id" in change for change in changed),
                    "changed_rows_with_consumer_project_scope": sum("consumer_id" in change or "project_id" in change for change in changed),
                    "graph_edge_count": len(edges)},
        "tables": table_reports, "graph_edges": sorted(edges, key=_canonical_json),
    }
    report["report_digest"] = _digest(report)
    return report


def canonical_report_json(report: dict[str, object]) -> str:
    """Render the canonical byte-stable report representation."""
    return _canonical_json(report)
