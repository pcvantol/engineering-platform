"""Read-only, graph-scoped collection of parallel Action delivery evidence.

The immutable producer graph determines the Action population. Accepted EP
submission links determine attempts; sibling Actions never become one retry
chain. All database observations are supplied through one caller-owned read
transaction, and canonical timing/usage reducers provide the measurements.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from . import parallel_action_admission, parallel_action_recovery
from .central_database import DATABASE_FILENAME
from .execution_timing import timing_summaries
from .provider_usage import provider_usage_summaries
from .telemetry_metrics import aggregate_numeric_metric, metric_coverage


COLLECTION_VERSION = "ep-parallel-action-collection/v1"
_METRICS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")
_TERMINAL = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
_ACTIVE = frozenset({"CLAIMED", "RUNNING"})


class CollectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _milliseconds(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds() * 1000)


def _interval_totals(intervals: list[tuple[datetime, datetime, str]]) -> tuple[int, int]:
    """Measure union and time with at least two distinct Actions active."""
    events: dict[datetime, list[tuple[str, int]]] = {}
    for start, end, action_id in intervals:
        events.setdefault(start, []).append((action_id, 1))
        events.setdefault(end, []).append((action_id, -1))
    active: dict[str, int] = {}
    previous: datetime | None = None
    busy = overlap = 0
    for point in sorted(events):
        if previous is not None and active:
            elapsed = _milliseconds(previous, point)
            busy += elapsed
            if len(active) > 1:
                overlap += elapsed
        for action_id, delta in events[point]:
            count = active.get(action_id, 0) + delta
            if count > 0:
                active[action_id] = count
            else:
                active.pop(action_id, None)
        previous = point
    return busy, overlap


def _metric(values: list[Mapping[str, object]], name: str, scope: str) -> dict[str, object]:
    observations = []
    for value in values:
        metrics = value.get("metrics")
        entry = metrics.get(name) if isinstance(metrics, Mapping) else None
        observations.append(entry if isinstance(entry, Mapping) else {
            "value": None, "unit": "tokens", "provenance": "UNAVAILABLE",
            **metric_coverage(expected=1, present=0, valid=0,
                              reason=f"{name} unavailable for one EP run"),
        })
    return aggregate_numeric_metric(
        observations, aggregation_level=scope, aggregation="sum", unit="tokens",
    )


def _usage_metrics(usages: list[Mapping[str, object]], scope: str) -> dict[str, object]:
    return {name: _metric(usages, name, scope) for name in _METRICS}


def _linked_attempts(connection: sqlite3.Connection, intake_ids: list[str]) -> list[sqlite3.Row]:
    if not intake_ids:
        return []
    placeholders = ",".join("?" for _ in intake_ids)
    return connection.execute(
        f"""SELECT i.intake_id,i.action_id,i.project_id,i.producer_id,i.mission_id,
                  i.target_repository_id,i.idempotency_key AS intake_idempotency_key,
                  l.submission_id,l.parent_submission_id,l.accepted_request_digest,
                  l.recorded_at AS link_recorded_at,
                  s.project_id AS submission_project_id,s.repository_id AS submission_repository_id,
                  s.producer_id AS submission_producer_id,s.mission_id AS submission_mission_id,
                  s.engineering_action_id AS submission_action_id,s.correlation_id,
                  s.idempotency_key,s.state AS submission_state,s.created_at AS submitted_at,
                  d.run_id,d.project_id AS dispatch_project_id,
                  d.repository_id AS dispatch_repository_id,
                  d.state AS dispatch_state,d.operator_resolution,
                  d.claimed_at,d.updated_at AS dispatch_updated_at,
                  p.run_id AS provenance_run_id,
                  p.project_id AS provenance_project_id,
                  p.repository_id AS provenance_repository_id,
                  p.installation_id AS provenance_installation_id,
                  m.value AS current_installation_id,
                  r.project_id AS run_project_id,r.state AS run_state,
                  r.created_at AS run_started_at,r.updated_at AS run_updated_at,
                  r.execution_mode
             FROM ep_parallel_action_intakes AS i
             JOIN ep_parallel_action_submission_links AS l ON l.intake_id=i.intake_id
             LEFT JOIN ep_submissions AS s ON s.submission_id=l.submission_id
             LEFT JOIN ep_parity_lifecycle_dispatches AS d ON d.submission_id=l.submission_id
             LEFT JOIN ep_receipt_run_provenance AS p ON p.submission_id=l.submission_id
             LEFT JOIN engineering_metadata AS m ON m.key='installation.instance_id'
             LEFT JOIN ep_execution_runs AS r ON r.run_id=d.run_id
            WHERE i.intake_id IN ({placeholders})
            ORDER BY i.action_id,l.recorded_at,l.submission_id""",
        intake_ids,
    ).fetchall()


def _verified_graph(connection: sqlite3.Connection, *, project_id: str,
                    producer_id: str, intake_id: str) -> tuple[sqlite3.Row, dict[str, object]]:
    selected = connection.execute(
        """SELECT i.graph_id,i.action_id AS selected_action_id,
                  i.mission_id AS intake_mission_id,
                  g.snapshot,g.snapshot_digest,g.mission_id,g.mission_revision,
                  g.recorded_at
             FROM ep_parallel_action_intakes AS i
             JOIN ep_parallel_action_graphs AS g ON g.graph_id=i.graph_id
            WHERE i.intake_id=? AND i.project_id=? AND i.producer_id=?
              AND g.project_id=? AND g.producer_id=?""",
        (intake_id, project_id, producer_id, project_id, producer_id),
    ).fetchone()
    if selected is None:
        raise CollectionError("PARALLEL_COLLECTION_NOT_FOUND")
    if selected["intake_mission_id"] != selected["mission_id"]:
        raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
    try:
        graph = json.loads(str(selected["snapshot"]))
        actions = graph["actions"]
        canonical_graph = {
            "contract_version": "parallel-action-graph/v1",
            "mission_id": graph["mission_id"],
            "mission_revision": graph["mission_revision"],
            "actions": [
                {"action_id": item["action_id"], "target": item["target"],
                 "dependencies": item["dependencies"]}
                for item in actions
            ],
        }
        if (not isinstance(actions, list) or not actions
                or parallel_action_admission._canonical(graph) != selected["snapshot"]
                or graph["contract_version"] != "ep-parallel-action-compat/v1"
                or graph["status"] != "COMPATIBLE"
                or graph["dispatch_authorized"] is not False
                or graph["mission_id"] != selected["mission_id"]
                or graph["mission_revision"] != selected["mission_revision"]
                or graph["producer_snapshot_digest"] != selected["snapshot_digest"]
                or "sha256:" + sha256(parallel_action_admission._canonical(
                    canonical_graph,
                ).encode()).hexdigest() != selected["snapshot_digest"]):
            raise ValueError("graph identity mismatch")
    except (ValueError, TypeError, KeyError):
        raise CollectionError("PARALLEL_COLLECTION_INVALID_SNAPSHOT") from None
    return selected, graph


def _attempt(row: sqlite3.Row) -> dict[str, object]:
    if any(row[key] is None for key in (
        "submission_project_id", "submission_repository_id", "submission_producer_id",
        "submission_mission_id", "submission_action_id",
    )):
        raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
    if (
        row["submission_project_id"] != row["project_id"]
        or row["submission_repository_id"] != row["target_repository_id"]
        or row["submission_producer_id"] != row["producer_id"]
        or row["submission_mission_id"] != row["mission_id"]
        or row["submission_action_id"] != row["action_id"]
        or (row["run_id"] is not None and (
            row["dispatch_project_id"] != row["project_id"]
            or row["dispatch_repository_id"] != row["target_repository_id"]
        ))
        or (row["run_id"] is not None and row["run_project_id"] != row["project_id"])
        or (row["run_id"] is None and row["provenance_run_id"] is not None)
        or (row["run_id"] is not None and (
            row["provenance_run_id"] != row["run_id"]
            or row["provenance_project_id"] != row["project_id"]
            or row["provenance_repository_id"] != row["target_repository_id"]
            or row["provenance_installation_id"] != row["current_installation_id"]
            or row["current_installation_id"] is None
        ))
    ):
        raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
    if row["parent_submission_id"] is None and row["idempotency_key"] != row["intake_idempotency_key"]:
        raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
    return {
        "submission_id": row["submission_id"],
        "project_id": row["submission_project_id"],
        "producer_id": row["submission_producer_id"],
        "mission_id": row["submission_mission_id"],
        "action_id": row["submission_action_id"],
        "repository_id": row["submission_repository_id"],
        "parent_submission_id": row["parent_submission_id"],
        "request_digest": row["accepted_request_digest"],
        "idempotency_key": row["idempotency_key"],
        "correlation_id": row["correlation_id"],
        "state": row["submission_state"],
        "submitted_at": row["submitted_at"],
        "run_id": row["run_id"],
        "dispatch_state": row["dispatch_state"],
        "operator_resolution": row["operator_resolution"],
        "claimed_at": row["claimed_at"],
        "dispatch_updated_at": row["dispatch_updated_at"],
        "run_state": row["run_state"],
        "run_started_at": row["run_started_at"],
        "run_updated_at": row["run_updated_at"],
        "execution_mode": row["execution_mode"],
    }


def _action_state(attempts: list[dict[str, object]], decision: Mapping[str, object] | None,
                  terminal_evidence: str) -> str:
    if any(row.get("recovery_state") == "PROVIDER_EFFECT_UNCERTAIN" for row in attempts):
        return "WAITING_RECOVERY"
    if any(row.get("recovery_state") == "CANCEL_REQUESTED" for row in attempts):
        return "CANCEL_REQUESTED"
    if any(row["dispatch_state"] in _ACTIVE for row in attempts):
        return "ACTIVE"
    if any(row["dispatch_state"] is None and row["state"] == "QUEUED" for row in attempts):
        return str(decision["state"]) if decision is not None else "QUEUED"
    if terminal_evidence == "VERIFIED":
        return "COMPLETE"
    dispatched = [row for row in attempts if row["dispatch_state"] is not None]
    if dispatched:
        latest = dispatched[-1]
        if latest["dispatch_state"] == "COMPLETE":
            return "TERMINAL_EVIDENCE_PENDING"
        return str(latest["dispatch_state"])
    return str(decision["state"]) if decision is not None else "STAGED"


def collection_readback(
    connection: sqlite3.Connection, *, data_root: Path, project_id: str,
    producer_id: str, intake_id: str, source_as_of: str,
) -> dict[str, object]:
    """Project one immutable graph and all its retained EP attempts."""
    selected, graph = _verified_graph(
        connection, project_id=project_id, producer_id=producer_id,
        intake_id=intake_id,
    )
    graph_id = str(selected["graph_id"])
    action_items = {str(item["action_id"]): item for item in graph["actions"]}
    intakes = connection.execute(
        """SELECT intake_id,graph_id,project_id,producer_id,mission_id,
                  action_id,action_revision,intent_id,intent_revision,
                  correlation_id,idempotency_key,write_scope,policy_digest,
                  concurrency_profile,target_repository_id,baseline_revision,recorded_at
             FROM ep_parallel_action_intakes WHERE graph_id=?
             ORDER BY action_id""", (graph_id,),
    ).fetchall()
    current_by_action: dict[str, sqlite3.Row] = {}
    for row in intakes:
        item = action_items.get(str(row["action_id"]))
        if (item is None or row["action_id"] in current_by_action
                or row["project_id"] != project_id
                or row["producer_id"] != producer_id
                or row["mission_id"] != selected["mission_id"]
                or row["target_repository_id"] != item["target"]["repository_id"]
                or row["baseline_revision"] != item["target"]["baseline_revision"]):
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
        current_by_action[str(row["action_id"])] = row
    by_action = dict(current_by_action)
    for action_id in action_items:
        accepted_count = connection.execute(
            """SELECT COUNT(DISTINCT i.intake_id)
                 FROM ep_parallel_action_intakes AS i
                 JOIN ep_parallel_action_submission_links AS l ON l.intake_id=i.intake_id
                WHERE i.project_id=? AND i.producer_id=? AND i.mission_id=?
                  AND i.action_id=? AND l.parent_submission_id IS NULL""",
            (project_id, producer_id, selected["mission_id"], action_id),
        ).fetchone()[0]
        if not accepted_count:
            continue
        accepted = parallel_action_admission._accepted_predecessor(
            connection, graph=graph, project_id=project_id,
            producer_id=producer_id, mission_id=str(selected["mission_id"]),
            action_id=action_id,
        )
        if accepted is None:
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
        effective = connection.execute(
            """SELECT intake_id,graph_id,project_id,producer_id,mission_id,
                      action_id,action_revision,intent_id,intent_revision,
                      correlation_id,idempotency_key,write_scope,policy_digest,
                      concurrency_profile,target_repository_id,baseline_revision,recorded_at
                 FROM ep_parallel_action_intakes WHERE intake_id=?""",
            (accepted[0],),
        ).fetchone()
        if (effective is None or effective["project_id"] != project_id
                or effective["producer_id"] != producer_id
                or effective["mission_id"] != selected["mission_id"]
                or effective["target_repository_id"] != action_items[action_id]["target"]["repository_id"]
                or effective["baseline_revision"] != action_items[action_id]["target"]["baseline_revision"]):
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
        by_action[action_id] = effective
    intake_ids = [str(row["intake_id"]) for row in by_action.values()]
    linked: dict[str, list[dict[str, object]]] = {key: [] for key in by_action}
    seen_runs: set[str] = set()
    for row in _linked_attempts(connection, intake_ids):
        attempt = _attempt(row)
        if not parallel_action_admission._stored_submission_matches_intake(
            connection, intake_id=str(row["intake_id"]),
            submission_id=str(row["submission_id"]),
        ):
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
        action_id = str(row["action_id"])
        linked[action_id].append(attempt)
        run_id = attempt["run_id"]
        if isinstance(run_id, str):
            if run_id in seen_runs:
                raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
            seen_runs.add(run_id)
    for attempts in linked.values():
        identifiers = {str(row["submission_id"]) for row in attempts}
        if any(row["parent_submission_id"] is not None
               and row["parent_submission_id"] not in identifiers for row in attempts):
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")

    run_ids = sorted(seen_runs)
    database = data_root / DATABASE_FILENAME
    timing = timing_summaries(
        data_root, run_ids, central_database=database,
        timeline_limit=None, _read_connection=connection,
    )
    usage = provider_usage_summaries(
        data_root, run_ids, central_database=database,
        invocation_limit=None, _read_connection=connection,
    )
    outcome_placeholders = ",".join("?" for _ in intake_ids)
    outcomes = {str(row["intake_id"]): row for row in connection.execute(
        f"""SELECT o.intake_id,o.submission_id,o.run_id,o.terminal_artifact_id,
                  o.terminal_digest,o.repository_revision
             FROM ep_parallel_action_outcomes AS o
            WHERE o.intake_id IN ({outcome_placeholders})""", intake_ids,
    )} if intake_ids else {}
    actions: list[dict[str, object]] = []
    provider_intervals: list[tuple[datetime, datetime, str]] = []
    run_intervals: list[tuple[datetime, datetime, str]] = []
    provider_observations = 0
    interval_issues: list[str] = []
    run_interval_issues: list[str] = []
    process_duration_values: list[int] = []
    all_usage: list[Mapping[str, object]] = []
    queue_wait_values: list[int] = []
    for action_id, item in action_items.items():
        intake = by_action.get(action_id)
        if intake is None:
            actions.append({
                "action_id": action_id, "target": item["target"],
                "dependencies": item["dependencies"], "state": "NOT_STAGED",
                "intake_id": None, "attempts": [], "runs": [],
                "terminal_evidence": "UNAVAILABLE", "decision": None,
                "usage_metrics": _usage_metrics([], "EP_ACTION"),
            })
            continue
        action_attempts = linked[action_id]
        outcome = outcomes.get(str(intake["intake_id"]))
        if outcome is not None and (outcome["submission_id"], outcome["run_id"]) not in {
            (row["submission_id"], row["run_id"]) for row in action_attempts
        }:
            raise CollectionError("PARALLEL_COLLECTION_IDENTITY_CONFLICT")
        terminal_evidence = (
            "VERIFIED" if outcome is not None and parallel_action_admission._current_terminal_matches(
                connection, str(intake["intake_id"]),
            ) else "CONFLICT" if outcome is not None else "UNAVAILABLE"
        )
        pending = not action_attempts or any(
            row["dispatch_state"] is None and row["state"] == "QUEUED"
            for row in action_attempts
        )
        decision = None
        if pending:
            if action_id == selected["selected_action_id"]:
                decision = parallel_action_admission.dependency_readback(
                    connection, intake_id=str(intake["intake_id"]),
                )
                decision = parallel_action_admission.delivery_readback(
                    connection, intake_id=str(intake["intake_id"]),
                    data_root=data_root, decision=decision,
                )
            else:
                decision = {
                    "contract_version": parallel_action_admission.READBACK_VERSION,
                    "intake_id": intake["intake_id"],
                    "state": "NOT_EVALUATED_IN_COLLECTION",
                    "admission": "NOT_GRANTED",
                    "resource_state": "NOT_EVALUATED",
                    "capacity_state": "NOT_EVALUATED",
                    "dispatch_authorized": False,
                }
        runs: list[dict[str, object]] = []
        action_usage: list[Mapping[str, object]] = []
        action_run_intervals: list[tuple[datetime, datetime, str]] = []
        for attempt in action_attempts:
            run_id = attempt["run_id"]
            if not isinstance(run_id, str):
                continue
            recovery_state = parallel_action_recovery.status(connection, run_id)
            attempt["recovery_state"] = recovery_state
            run_usage = usage.get(run_id, {})
            run_timing = timing.get(run_id, {})
            started = _utc(attempt["run_started_at"])
            completed = (
                _utc(attempt["run_updated_at"])
                if attempt["dispatch_state"] in _TERMINAL else _utc(source_as_of)
            )
            if started is None or completed is None or completed < started:
                run_interval_issues.append(f"invalid-run-interval:{run_id}")
            else:
                interval = (started, completed, action_id)
                run_intervals.append(interval)
                action_run_intervals.append(interval)
            action_usage.append(run_usage)
            all_usage.append(run_usage)
            queue_wait = run_timing.get("queue_wait_time_ms")
            if isinstance(queue_wait, int) and not isinstance(queue_wait, bool) and queue_wait >= 0:
                queue_wait_values.append(queue_wait)
            runs.append({
                "run_id": run_id, "submission_id": attempt["submission_id"],
                "state": attempt["dispatch_state"],
                "recovery_state": recovery_state,
                "run_state": attempt["run_state"],
                "operator_resolution": attempt["operator_resolution"],
                "repository_id": item["target"]["repository_id"],
                "started_at": attempt["run_started_at"],
                "updated_at": attempt["run_updated_at"],
                "telemetry_snapshot": {
                    "contract_version": run_usage.get("contract_version"),
                    "source_snapshot_reference": run_id,
                    "attempt": {"scope": "EP_RUN_ATTEMPT", "run_id": run_id,
                                "timing": run_timing, "usage": run_usage},
                },
            })
            invocations = run_usage.get("invocations")
            if not isinstance(invocations, list):
                interval_issues.append(f"provider-usage-unavailable:{run_id}")
                continue
            for invocation in invocations:
                if not isinstance(invocation, Mapping):
                    interval_issues.append(f"invalid-invocation:{run_id}")
                    continue
                provider_observations += 1
                duration = invocation.get("duration_ms")
                valid_duration = (
                    isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
                )
                if valid_duration:
                    process_duration_values.append(duration)
                start = _utc(invocation.get("started_at"))
                end = _utc(invocation.get("completed_at"))
                if start is None or end is None or end < start:
                    interval_issues.append(f"incomplete-provider-interval:{run_id}")
                elif not valid_duration:
                    interval_issues.append(f"provider-duration-unavailable:{run_id}")
                elif abs(_milliseconds(start, end) - duration) > max(1000, round(duration * 0.05)):
                    interval_issues.append(f"provider-clock-conflict:{run_id}")
                else:
                    provider_intervals.append((start, end, action_id))
        action_busy, _ = _interval_totals(action_run_intervals)
        action_first = min((row[0] for row in action_run_intervals), default=None)
        action_last = max((row[1] for row in action_run_intervals), default=None)
        action_recovery_state = next((
            str(row["recovery_state"]) for row in reversed(action_attempts)
            if row.get("recovery_state") not in (None, "NONE")
        ), "NONE")
        held = action_recovery_state in {"PROVIDER_EFFECT_UNCERTAIN", "CANCEL_REQUESTED"}
        actions.append({
            "action_id": action_id, "target": item["target"],
            "dependencies": item["dependencies"],
            "state": _action_state(action_attempts, decision, terminal_evidence),
            "intake_id": intake["intake_id"],
            "selected_graph_intake_id": (
                current_by_action[action_id]["intake_id"]
                if action_id in current_by_action else None
            ),
            "source_graph_id": intake["graph_id"],
            "action_revision": intake["action_revision"],
            "intent_id": intake["intent_id"],
            "intent_revision": intake["intent_revision"],
            "policy_digest": intake["policy_digest"],
            "concurrency_profile": intake["concurrency_profile"],
            "recorded_at": intake["recorded_at"],
            "decision": decision, "attempts": action_attempts, "runs": runs,
            "recovery_state": action_recovery_state,
            "resource_state": "HELD" if held else decision.get("resource_state") if decision else None,
            "capacity_state": "HELD" if held else decision.get("capacity_state") if decision else None,
            "terminal_evidence": terminal_evidence,
            "terminal_outcome": dict(outcome) if outcome is not None else None,
            "usage_metrics": _usage_metrics(action_usage, "EP_ACTION"),
            "execution_first_started_at": action_first.isoformat() if action_first else None,
            "execution_last_observed_at": action_last.isoformat() if action_last else None,
            "execution_elapsed_ms": _milliseconds(action_first, action_last)
            if action_first and action_last else None,
            "execution_busy_union_ms": action_busy if action_run_intervals else None,
        })
    provider_busy, provider_overlap = _interval_totals(provider_intervals)
    execution_busy, execution_overlap = _interval_totals(run_intervals)
    first_run = min((row[0] for row in run_intervals), default=None)
    last_run = max((row[1] for row in run_intervals), default=None)
    interval_coverage = (
        "UNAVAILABLE" if provider_observations == 0 else
        "CONFLICT" if any(issue.startswith("provider-clock-conflict:") for issue in interval_issues) else
        "PARTIAL" if interval_issues else "COMPLETE"
    )
    count_by_state: dict[str, int] = {}
    for action in actions:
        state = str(action["state"])
        count_by_state[state] = count_by_state.get(state, 0) + 1
    return {
        "contract_version": COLLECTION_VERSION,
        "project_id": project_id, "producer_id": producer_id,
        "mission_id": selected["mission_id"],
        "mission_revision": selected["mission_revision"],
        "graph_id": graph_id, "snapshot_digest": selected["snapshot_digest"],
        "graph_recorded_at": selected["recorded_at"],
        "source_as_of": source_as_of,
        "dispatch_authorized": False,
        "mission_acceptance": "NOT_EVALUATED_BY_EP",
        "usage_scope": "EP_OBSERVED_ONLY_FORGE_PLANNING_EXCLUDED",
        "summary": {
            "action_count": len(actions),
            "staged_action_count": len(current_by_action),
            "represented_action_count": len(by_action),
            "submission_count": sum(len(action["attempts"]) for action in actions),
            "run_count": len(run_ids), "state_counts": count_by_state,
            "provider_invocation_count": provider_observations,
            "provider_completed_interval_count": len(provider_intervals),
            "provider_busy_union_ms": provider_busy if provider_intervals else None,
            "provider_multi_action_overlap_ms": provider_overlap if provider_intervals else None,
            "actual_provider_overlap": (
                True if provider_overlap > 0 else
                False if interval_coverage == "COMPLETE" else None
            ),
            "provider_interval_coverage": interval_coverage,
            "provider_interval_issues": list(dict.fromkeys(interval_issues)),
            "provider_cumulative_process_duration_ms": sum(process_duration_values)
            if process_duration_values else None,
            "provider_duration_coverage": (
                "UNAVAILABLE" if provider_observations == 0 else
                "PARTIAL" if len(process_duration_values) < provider_observations else "COMPLETE"
            ),
            "execution_first_started_at": first_run.isoformat() if first_run else None,
            "execution_last_observed_at": last_run.isoformat() if last_run else None,
            "execution_elapsed_ms": _milliseconds(first_run, last_run)
            if first_run and last_run else None,
            "execution_busy_union_ms": execution_busy if run_intervals else None,
            "execution_multi_action_overlap_ms": execution_overlap if run_intervals else None,
            "execution_interval_coverage": (
                "UNAVAILABLE" if not run_ids else
                "PARTIAL" if run_interval_issues else "COMPLETE"
            ),
            "execution_interval_issues": list(dict.fromkeys(run_interval_issues)),
            "observed_queue_wait_ms": sum(queue_wait_values) if queue_wait_values else None,
            "queue_wait_coverage": (
                "UNAVAILABLE" if not run_ids or not queue_wait_values else
                "PARTIAL" if len(queue_wait_values) < len(run_ids) else "COMPLETE"
            ),
            "dependency_wait_ms": None, "resource_wait_ms": None,
            "capacity_wait_ms": None,
            "wait_duration_coverage": "UNAVAILABLE_WAIT_TRANSITIONS_NOT_RECORDED",
            "usage_metrics": _usage_metrics(all_usage, "EP_PARALLEL_ACTION_COLLECTION"),
        },
        "actions": actions,
    }
