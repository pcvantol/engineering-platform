"""Canonical, versioned telemetry read model shared by every projection.

This module is deliberately read-only.  EP run/attempt identity comes from
CENTRAL and Mission/Action identity is copied only from the accepted producer
submission.  No identity is inferred from names, branches, paths, or time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Mapping

from .execution_timing import timing_summary
from .provider_usage import (
    COMPLETE, CONFLICT, PARTIAL, TELEMETRY_CALCULATION_VERSION, UNAVAILABLE,
    provider_usage_summary,
)
from .telemetry_metrics import aggregate_coverage, aggregate_numeric_metric, metric_coverage
from .storage import open_storage


MAX_CHAIN_RUNS = 250
MAX_LINEAGE_GRAPH_RUNS = 5000


def _connection(root: Path, database: Path | None) -> sqlite3.Connection:
    connection = open_storage(root) if database is None else sqlite3.connect(database.resolve(), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _run_records(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ep_execution_runs" in tables:
        rows = connection.execute(
            """SELECT run_id,project_id,state,created_at,updated_at,execution_mode
                 FROM ep_execution_runs ORDER BY created_at DESC LIMIT ?""",
            (MAX_LINEAGE_GRAPH_RUNS,),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT run_id,NULL AS project_id,terminal_state AS state,
                      execution_started_at AS created_at,execution_finished_at AS updated_at,
                      execution_mode FROM execution_runs
                 ORDER BY execution_started_at DESC LIMIT ?""",
            (MAX_LINEAGE_GRAPH_RUNS,),
        ).fetchall()
    return {str(row["run_id"]): row for row in rows}


def load_lineage_graph(
    root: Path, *, central_database: Path | None = None,
) -> tuple[list[sqlite3.Row], dict[str, sqlite3.Row], dict[str, dict[str, object]]]:
    """Load the bounded explicit identity graph once for a telemetry request."""
    connection = _connection(root, central_database)
    try:
        contexts = connection.execute(
            """SELECT run_id,submission_id,fresh_submission,retry_parent_run_id,
                      resume_parent_run_id,recorded_at
                 FROM execution_run_qualification_context
                 ORDER BY recorded_at DESC LIMIT ?""",
            (MAX_LINEAGE_GRAPH_RUNS,),
        ).fetchall()
        runs = _run_records(connection)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identities: dict[str, dict[str, object]] = {}
        if {"ep_parity_lifecycle_dispatches", "ep_submissions"}.issubset(tables):
            for row in connection.execute(
                """SELECT d.run_id,s.submission_id,s.project_id,s.mission_id,
                          s.engineering_action_id,s.correlation_id,s.producer_type
                     FROM ep_parity_lifecycle_dispatches AS d
                     JOIN ep_submissions AS s ON s.submission_id=d.submission_id
                     ORDER BY s.created_at DESC LIMIT ?""",
                (MAX_LINEAGE_GRAPH_RUNS,),
            ):
                identities[str(row["run_id"])] = dict(row)
    finally:
        connection.close()
    return contexts, runs, identities


def execution_chain_summary(
    root: Path, run_id: str, *, central_database: Path | None = None,
    window_start: datetime | None = None, window_end: datetime | None = None,
    _usage_cache: dict[str, dict[str, object]] | None = None,
    _timing_cache: dict[str, dict[str, object]] | None = None,
    _lineage_graph: tuple[list[sqlite3.Row], dict[str, sqlite3.Row], dict[str, dict[str, object]]] | None = None,
) -> dict[str, object]:
    """Return the explicitly linked attempt/retry/resume component."""
    usage_cache = _usage_cache if _usage_cache is not None else {}
    timing_cache = _timing_cache if _timing_cache is not None else {}
    contexts, runs, identities = _lineage_graph or load_lineage_graph(
        root, central_database=central_database,
    )

    by_run = {str(row["run_id"]): row for row in contexts}
    issues: list[str] = []
    if run_id not in runs:
        return {
            "contract_version": TELEMETRY_CALCULATION_VERSION, "scope": "EXECUTION_CHAIN",
            "selected_run_id": run_id, "coverage": UNAVAILABLE,
            "reason": "Selected run is not retained in CENTRAL", "runs": [],
        }

    # Walk to the one explicit root. Missing and cyclic parents stay visible.
    cursor = run_id
    seen: set[str] = set()
    while cursor in by_run:
        if cursor in seen:
            issues.append(f"cycle:{cursor}")
            break
        seen.add(cursor)
        row = by_run[cursor]
        parent = row["retry_parent_run_id"] or row["resume_parent_run_id"]
        if parent is None:
            break
        if str(parent) not in runs:
            issues.append(f"missing-parent:{parent}")
            break
        cursor = str(parent)
    root_run = cursor

    children: dict[str, list[str]] = {}
    edges: set[tuple[str, str, str]] = set()
    for row in contexts:
        child = str(row["run_id"])
        if row["retry_parent_run_id"] and row["resume_parent_run_id"]:
            issues.append(f"conflicting-parents:{child}")
        relation = "RETRY" if row["retry_parent_run_id"] else "RESUME" if row["resume_parent_run_id"] else "ORIGINAL"
        parent = row["retry_parent_run_id"] or row["resume_parent_run_id"]
        if parent is None:
            continue
        edge = (str(parent), child, relation)
        if edge in edges:
            issues.append(f"duplicate-edge:{parent}:{child}")
            continue
        edges.add(edge)
        children.setdefault(str(parent), []).append(child)

    component: list[str] = []
    pending = [root_run]
    visited: set[str] = set()
    while pending and len(component) < MAX_CHAIN_RUNS:
        current = pending.pop(0)
        if current in visited:
            issues.append(f"cycle-or-duplicate:{current}")
            continue
        visited.add(current)
        component.append(current)
        pending.extend(sorted(children.get(current, ())))
    if pending:
        issues.append(f"chain-limit:{MAX_CHAIN_RUNS}")

    chain_rows: list[dict[str, object]] = []
    duplicate_invocations = 0
    metric_names = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")
    usage_sources: dict[str, list[Mapping[str, object]]] = {key: [] for key in metric_names}
    provider_invocation_count = 0
    cache_ratio_sources: list[Mapping[str, object]] = []
    cache_ratio_input = 0
    cache_ratio_cached = 0
    intervals: list[tuple[datetime, datetime]] = []
    processing_ms = 0
    mission_values: set[str] = set()
    action_values: set[str] = set()
    correlation_values: set[str] = set()
    outside_window = 0
    for member in component:
        record = runs.get(member)
        context = by_run.get(member)
        identity = identities.get(member, {})
        if record is None:
            issues.append(f"missing-run:{member}")
            continue
        start, end = _utc(record["created_at"]), _utc(record["updated_at"])
        if start is not None and end is not None and end >= start:
            intervals.append((start, end))
        else:
            issues.append(f"invalid-run-boundary:{member}")
        if window_start is not None or window_end is not None:
            if (
                (end is not None and window_start is not None and end < window_start)
                or (start is not None and window_end is not None and start >= window_end)
            ):
                outside_window += 1
        for key, target in (("mission_id", mission_values), ("engineering_action_id", action_values), ("correlation_id", correlation_values)):
            value = identity.get(key)
            if isinstance(value, str) and value:
                target.add(value)
        usage = usage_cache.get(member)
        if usage is None:
            usage = provider_usage_summary(root, member, central_database=central_database)
            usage_cache[member] = usage
        if isinstance(usage, Mapping):
            count = usage.get("provider_invocation_count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                provider_invocation_count += count
            metrics = usage.get("metrics")
            for key in metric_names:
                metric = metrics.get(key) if isinstance(metrics, Mapping) else None
                if isinstance(metric, Mapping):
                    usage_sources[key].append(metric)
                else:
                    usage_sources[key].append({
                        "value": None, "provenance": UNAVAILABLE, "unit": "tokens",
                        **metric_coverage(expected=None, present=0, valid=0,
                                          reason=f"Usage projection unavailable for {member}"),
                        "source_snapshot_reference": member,
                    })
            population = usage.get("cache_ratio_population")
            if isinstance(population, Mapping):
                cache_ratio_sources.append(population)
                if population.get("coverage") != CONFLICT:
                    if isinstance(population.get("input_tokens"), int):
                        cache_ratio_input += int(population["input_tokens"])
                    if isinstance(population.get("cached_input_tokens"), int):
                        cache_ratio_cached += int(population["cached_input_tokens"])
            else:
                cache_ratio_sources.append(metric_coverage(
                    expected=None, present=0, valid=0,
                    reason=f"Cache-ratio population unavailable for {member}",
                ))
        timing = timing_cache.get(member)
        if timing is None:
            timing = timing_summary(root, member, central_database=central_database)
            timing_cache[member] = timing
        if isinstance(timing.get("total_wall_time_ms"), int):
            processing_ms += int(timing["total_wall_time_ms"])
        relation = "ORIGINAL"
        parent = None
        if context is not None:
            if context["retry_parent_run_id"]:
                relation, parent = "RETRY", context["retry_parent_run_id"]
            elif context["resume_parent_run_id"]:
                relation, parent = "RESUME", context["resume_parent_run_id"]
            elif not context["fresh_submission"]:
                issues.append(f"inconsistent-lineage:{member}")
        else:
            issues.append(f"missing-lineage:{member}")
        chain_rows.append({
            "run_id": member, "relation": relation, "parent_run_id": parent,
            "state": record["state"], "started_at": record["created_at"], "completed_at": record["updated_at"],
            "mission_id": identity.get("mission_id"), "engineering_action_id": identity.get("engineering_action_id"),
            "provider_invocation_count": usage.get("provider_invocation_count", 0) if isinstance(usage, Mapping) else 0,
            "timing_coverage": timing.get("coverage"),
        })

    for name, values in (("mission", mission_values), ("action", action_values), ("correlation", correlation_values)):
        if len(values) > 1:
            issues.append(f"conflicting-{name}-identity")
    if duplicate_invocations:
        issues.append(f"duplicate-invocations:{duplicate_invocations}")
    ordered_intervals = sorted(intervals)
    first_start = min((item[0] for item in ordered_intervals), default=None)
    last_end = max((item[1] for item in ordered_intervals), default=None)
    elapsed_ms = round((last_end - first_start).total_seconds() * 1000) if first_start and last_end else None
    merged: list[list[datetime]] = []
    for start, end in ordered_intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    covered_ms = round(sum((end - start).total_seconds() * 1000 for start, end in merged))
    gaps_ms = elapsed_ms - covered_ms if elapsed_ms is not None else None
    root_context = by_run.get(root_run)
    if root_context is None or not bool(root_context["fresh_submission"]):
        issues.append("original-attempt-not-proven")
    coverage = CONFLICT if any(value.startswith(("cycle", "conflicting", "duplicate")) for value in issues) else PARTIAL if issues else COMPLETE
    usage_metrics = {
        key: aggregate_numeric_metric(
            values, aggregation_level="EXECUTION_CHAIN", aggregation="sum", unit="tokens",
        )
        for key, values in usage_sources.items()
    }
    cache_ratio_coverage = aggregate_coverage(cache_ratio_sources)
    cache_ratio = (
        round(cache_ratio_cached * 100 / cache_ratio_input, 3)
        if cache_ratio_input and cache_ratio_coverage["coverage"] != CONFLICT else None
    )
    return {
        "contract_version": TELEMETRY_CALCULATION_VERSION,
        "scope": "EXECUTION_CHAIN", "selected_run_id": run_id, "root_run_id": root_run,
        "coverage": coverage, "reasons": list(dict.fromkeys(issues)),
        "mission_scope_label": "EP execution within this Mission" if mission_values else None,
        "mission_id": next(iter(mission_values)) if len(mission_values) == 1 else None,
        "engineering_action_id": next(iter(action_values)) if len(action_values) == 1 else None,
        "attempt_count": len(chain_rows),
        "original_attempt_count": sum(row["relation"] == "ORIGINAL" for row in chain_rows),
        "retry_count": sum(row["relation"] == "RETRY" for row in chain_rows),
        "resume_count": sum(row["relation"] == "RESUME" for row in chain_rows),
        "first_started_at": first_start.isoformat() if first_start else None,
        "last_completed_at": last_end.isoformat() if last_end else None,
        "elapsed_ms": elapsed_ms, "processing_time_ms": processing_ms,
        "covered_elapsed_ms": covered_ms if intervals else None, "inter_attempt_gap_ms": gaps_ms,
        "provider_invocation_count": provider_invocation_count,
        "duplicate_invocation_count": duplicate_invocations,
        "usage_metrics": usage_metrics,
        "cache_ratio_percent": cache_ratio,
        "cache_ratio_population": {
            **cache_ratio_coverage,
            "input_tokens": cache_ratio_input or None,
            "cached_input_tokens": cache_ratio_cached or None,
        },
        "outside_selected_window_count": outside_window,
        "runs": sorted(chain_rows, key=lambda row: str(row.get("started_at") or "")),
    }


def run_telemetry_snapshot(
    root: Path, run_id: str, *, central_database: Path | None = None,
    window_start: datetime | None = None, window_end: datetime | None = None,
    _usage_cache: dict[str, dict[str, object]] | None = None,
    _timing_cache: dict[str, dict[str, object]] | None = None,
    _lineage_graph: tuple[list[sqlite3.Row], dict[str, sqlite3.Row], dict[str, dict[str, object]]] | None = None,
) -> dict[str, object]:
    """One snapshot consumed unchanged by API, dashboard and exports."""
    usage_cache = _usage_cache if _usage_cache is not None else {}
    timing_cache = _timing_cache if _timing_cache is not None else {}
    usage = usage_cache.get(run_id)
    if usage is None:
        usage = provider_usage_summary(root, run_id, central_database=central_database)
        usage_cache[run_id] = usage
    timing = timing_cache.get(run_id)
    if timing is None:
        timing = timing_summary(root, run_id, central_database=central_database)
        timing_cache[run_id] = timing
    return {
        "contract_version": TELEMETRY_CALCULATION_VERSION,
        "source_snapshot_reference": run_id,
        "attempt": {
            "scope": "EP_RUN_ATTEMPT", "run_id": run_id,
            "usage": usage,
            "timing": timing,
        },
        "chain": execution_chain_summary(
            root, run_id, central_database=central_database,
            window_start=window_start, window_end=window_end,
            _usage_cache=usage_cache, _timing_cache=timing_cache,
            _lineage_graph=_lineage_graph,
        ),
        "presentation_semantics": {
            "provider_invocation": "One EP-launched provider process; not a model-request count",
            "cumulative_input": "Observed cumulative invocation input; not active context size",
            "engineering_actions": "EP invocations, commands and PR observations are not Forge Engineering Actions",
            "completion": "Technical COMPLETE is distinct from qualification and autonomy acceptance",
        },
    }
