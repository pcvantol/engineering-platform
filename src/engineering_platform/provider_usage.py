"""Provider invocation usage and bounded provider-input attribution.

This module deliberately stores only counters derived from provider JSONL.  It
never stores prompts, tool arguments, command output, paths, or model replies.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import ceil
from pathlib import Path
import sqlite3
import re
from statistics import median
from typing import Callable, Mapping
from uuid import uuid4

from .agent_state import redact_diagnostic
from .storage import EngineeringStorageError, open_storage


RATE_TABLE_VERSION = "2026-08-18"
EUR_PER_CREDIT = 0.04
RATE_TABLE = {
    "gpt-5.6-sol": {"uncached_input": 125.0, "cached_input": 12.5, "output": 750.0},
    "gpt-5.6-terra": {"uncached_input": 50.0, "cached_input": 5.0, "output": 300.0},
    "gpt-5.6-luna": {"uncached_input": 5.0, "cached_input": 0.5, "output": 30.0},
}
AUTHORITATIVE, DERIVED, UNAVAILABLE = "AUTHORITATIVE", "DERIVED", "UNAVAILABLE"
COMPLETE, PARTIAL, CONFLICT = "COMPLETE", "PARTIAL", "CONFLICT"
TELEMETRY_CALCULATION_VERSION = "telemetry-contract@2.0"
_SPEED_STATES = frozenset({"FAST", "NORMAL_DEFAULT", "OTHER", "UNKNOWN"})
_SAFE_CHURN_TEXT_FIELDS = frozenset({
    "interruption_classification",
    "interruption_reason",
    "usage_state",
    "context_scope_policy",
    "context_scope_initial",
    "context_scope_effective",
    "context_escalation_reasons",
    "context_escalation_boundaries",
    "context_escalation_diagnostic",
    "event_identity_coverage",
    "historical_pr_metrics_coverage",
    "file_read_observation_coverage",
    "tool_output_coverage",
})
_MODEL_NORMALIZATION = {
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
}


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def speed_state(metadata: Mapping[str, object] | None) -> str:
    """Return only a runtime-observed speed state; UI preferences are irrelevant."""
    known_fields = {
        "speed_state",
        "speed_mode",
        "execution_speed",
        "codex_speed_mode",
        "fast_mode",
        "configuration_profile",
        "codex_configuration_profile",
    }
    for key, value in (metadata or {}).items():
        normalized_key = str(key).casefold().replace("-", "_")
        if normalized_key not in known_fields or not isinstance(value, str):
            continue
        normalized_value = value.casefold().strip()
        if normalized_key == "fast_mode":
            if normalized_value in {"true", "fast", "enabled"}:
                return "FAST"
            if normalized_value in {"false", "normal", "disabled"}:
                return "NORMAL_DEFAULT"
        if re.search(r"\bfast(?:\s+mode)?\b", normalized_value):
            return "FAST"
        if re.search(r"\b(?:normal|default)\b", normalized_value):
            return "NORMAL_DEFAULT"
        return "OTHER"
    return "UNKNOWN"


def normalize_codex_model(raw_model: object) -> str | None:
    """Map only explicitly supported Codex runtime model identifiers."""
    if not isinstance(raw_model, str):
        return None
    return _MODEL_NORMALIZATION.get(raw_model.casefold().strip())


def _usage_from_event(event: object) -> dict[str, int]:
    usage: dict[str, int] = {}
    if not isinstance(event, dict):
        return usage

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = key.casefold().replace("-", "_")
                if normalized in {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                }:
                    number = _number(item)
                    if number is not None:
                        usage[normalized] = number
                elif normalized in {"usage", "token_usage"} or isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(event)
    input_tokens = usage.get("input_tokens")
    cached_tokens = usage.get("cached_input_tokens")
    if input_tokens is not None and cached_tokens is not None and cached_tokens > input_tokens:
        # Preserve the independently valid input observation, but never derive
        # uncached input or a cache ratio from an impossible pair.
        usage.pop("cached_input_tokens", None)
    return usage


def usage_snapshots_from_jsonl(*outputs: str) -> tuple[dict[str, int], ...]:
    """Return only actual final-turn counter snapshots, without conversation content.

    Codex CLI 0.154.0 exposes usage on ``turn.completed``.  The counters are
    provider-execution cumulative counters, not an active-context measurement.
    """
    snapshots: list[dict[str, int]] = []
    seen: set[str] = set()
    for output in outputs:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                found = _usage_from_event(event.get("usage"))
                identity = event.get("id") or event.get("event_id")
                fingerprint = json.dumps(
                    {"identity": identity, "usage": found}, sort_keys=True, separators=(",", ":")
                )
                if found and fingerprint not in seen:
                    seen.add(fingerprint)
                    snapshots.append(found)
    return tuple(snapshots)


def usage_from_jsonl(*outputs: str) -> dict[str, int]:
    """Use one compatible cumulative final snapshot; never sum snapshots.

    A decreasing counter is a reset, a different scope, or conflicting replay.
    Without an explicit scope identifier none of those can safely be converted
    to spend, so the invocation total remains unavailable.
    """
    snapshots = usage_snapshots_from_jsonl(*outputs)
    previous: dict[str, int] | None = None
    for snapshot in snapshots:
        if previous is not None and any(
            key in previous and key in snapshot and snapshot[key] < previous[key]
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
        ):
            return {}
        previous = snapshot
    return dict(snapshots[-1]) if snapshots else {}


def _structured_pr_id(value: object) -> str | None:
    """Return an opaque stable PR identity from structured GitHub output."""
    if not isinstance(value, Mapping):
        return None
    number = _number(value.get("number"))
    repository = value.get("repository")
    if isinstance(repository, Mapping):
        repository = repository.get("nameWithOwner") or repository.get("name_with_owner")
    if not isinstance(repository, str):
        url = value.get("url")
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url) if isinstance(url, str) else None
        if match:
            repository, number = match.group(1), int(match.group(2))
    if number is None:
        return None
    repository = repository.casefold().strip() if isinstance(repository, str) and repository.strip() else "current-repository"
    return hashlib.sha256(f"{repository}#{number}".encode()).hexdigest()


def _structured_pr_results(raw: object) -> tuple[int, set[str]] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    values = payload if isinstance(payload, list) else [payload]
    identities = {_structured_pr_id(value) for value in values}
    identities.discard(None)
    if len(identities) != len(values):
        return None
    return len(values), {str(value) for value in identities}


def churn_from_jsonl(*outputs: str) -> dict[str, int | str]:
    """Measure bounded churn after invocation/item lifecycle deduplication.

    Codex item ``started``/``updated``/``completed`` records describe one item,
    not separate tool executions.  This reducer keeps command/output content in
    memory only long enough to derive counters; persisted evidence is numeric
    plus the small coverage labels explicitly allowed above.
    """
    result = {
        key: 0
        for key in (
            "file_read_count",
            "distinct_files_read",
            "repeated_file_read_count",
            "glob_search_calls",
            "grep_calls",
            "shell_command_calls",
            "test_commands",
            "tool_output_bytes",
            "maximum_tool_output_bytes",
            "passing_test_output_bytes",
            "failed_test_diagnostic_bytes",
            "unknown_test_output_bytes",
            "unknown_test_commands",
            "git_output_bytes",
            "github_output_bytes",
            "historical_commit_queries",
            "historical_commit_results",
            "historical_pr_queries",
            "historical_pr_results",
            "historical_pr_result_occurrences",
            "historical_unique_pr_results",
            "historical_pr_details_fetched",
            "historical_pr_unstructured_queries",
            "historical_pr_results_legacy_lines",
            "historical_context_bytes",
            "tool_loop_operations",
            "unique_read_commands",
            "repeated_read_commands",
            "duplicate_terminal_events",
            "conflicting_terminal_events",
            "missing_item_identity_events",
        )
    }
    reads: set[str] = set()
    pr_identities: set[str] = set()
    historical_observed = False
    items: dict[str, dict[str, object]] = {}
    seen_events: set[str] = set()
    missing_identity = False
    sequence = 0
    for output in outputs:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            event_type = event.get("type")
            if event_type not in {"item.started", "item.updated", "item.completed"}:
                continue
            sequence += 1
            fingerprint = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
            if fingerprint in seen_events:
                if event_type == "item.completed":
                    result["duplicate_terminal_events"] += 1
                continue
            seen_events.add(fingerprint)
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                missing_identity = True
                result["missing_item_identity_events"] += 1
                # Merge lifecycle updates for the same opaque command only as
                # a lower-bound observation. Two truly distinct executions
                # could be indistinguishable without an item ID, hence PARTIAL.
                command_identity = item.get("command")
                key = "missing:" + hashlib.sha256(
                    str(command_identity if isinstance(command_identity, str) else sequence).encode()
                ).hexdigest()
            else:
                key = item_id
            observation = items.setdefault(key, {"events": set(), "terminal_fingerprints": set()})
            observation["events"].add(str(event_type))  # type: ignore[union-attr]
            command = item.get("command")
            if isinstance(command, str):
                observation["command"] = command
            raw = item.get("aggregated_output", item.get("output", ""))
            if isinstance(raw, str) and len(raw.encode("utf-8")) >= int(observation.get("output_size", 0)):
                observation["output"] = raw
                observation["output_size"] = len(raw.encode("utf-8"))
            if event_type == "item.completed":
                terminal = json.dumps(
                    {"exit_code": item.get("exit_code"), "output": raw},
                    sort_keys=True, separators=(",", ":"), default=str,
                )
                terminals = observation["terminal_fingerprints"]
                if terminals and terminal not in terminals:  # type: ignore[operator]
                    result["conflicting_terminal_events"] += 1
                terminals.add(terminal)  # type: ignore[union-attr]
                observation["completed"] = True
                exit_code = item.get("exit_code")
                observation["exit_code"] = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None

    for observation in items.values():
        command = observation.get("command")
        if not isinstance(command, str):
            continue
        normalized = command.casefold()
        result["shell_command_calls"] += 1
        # One normalized item is one observed tool-loop operation. This is a
        # derived churn counter, not token attribution.
        result["tool_loop_operations"] += 1
        is_test = bool(re.search(r"\b(?:pytest|unittest|tox|nox|playwright)\b", normalized))
        if is_test:
            result["test_commands"] += 1
        if re.search(r"\b(?:rg|grep)\b", normalized):
            result["grep_calls"] += 1
        if re.search(r"\b(?:find|rg\s+--files|glob)\b", normalized):
            result["glob_search_calls"] += 1
        if re.search(r"\b(?:cat|sed|head|tail|less|awk)\b", normalized):
            # A command may read several paths but retaining them would be sensitive.
            fingerprint = hashlib.sha256(re.sub(r"\s+", " ", command.strip()).encode()).hexdigest()
            result["file_read_count"] += 1
            if fingerprint in reads:
                result["repeated_file_read_count"] += 1
                result["repeated_read_commands"] += 1
            reads.add(fingerprint)
        raw = observation.get("output", "")
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
        result["tool_output_bytes"] += size
        result["maximum_tool_output_bytes"] = max(result["maximum_tool_output_bytes"], size)
        if is_test:
            if observation.get("exit_code") == 0:
                result["passing_test_output_bytes"] += size
            elif isinstance(observation.get("exit_code"), int):
                result["failed_test_diagnostic_bytes"] += size
            else:
                result["unknown_test_commands"] += 1
                result["unknown_test_output_bytes"] += size
        if re.search(r"\b(?:git)\b", normalized):
            result["git_output_bytes"] += size
        if re.search(r"\b(?:gh)\b", normalized):
            result["github_output_bytes"] += size
        if re.search(r"\bgit\s+(?:log|blame)\b", normalized):
            historical_observed = True
            result["historical_commit_queries"] += 1
            result["historical_commit_results"] += len(raw.splitlines()) if isinstance(raw, str) else 0
            result["historical_context_bytes"] += size
        if re.search(r"\bgh\s+(?:pr\s+list|search\s+prs)\b", normalized):
            historical_observed = True
            result["historical_pr_queries"] += 1
            structured = _structured_pr_results(raw)
            if structured is None:
                result["historical_pr_unstructured_queries"] += 1
                legacy_lines = len(raw.splitlines()) if isinstance(raw, str) else 0
                result["historical_pr_results_legacy_lines"] += legacy_lines
                # Compatibility only. New consumers use the explicitly
                # named structured counters and coverage below.
                result["historical_pr_results"] += legacy_lines
            else:
                occurrences, identities = structured
                result["historical_pr_result_occurrences"] += occurrences
                result["historical_pr_results"] += occurrences
                pr_identities.update(identities)
            result["historical_context_bytes"] += size
        if re.search(r"\bgh\s+pr\s+(?:view|diff)\b", normalized):
            result["historical_pr_details_fetched"] += 1
    result["distinct_files_read"] = len(reads)
    result["unique_read_commands"] = len(reads)
    result["historical_unique_pr_results"] = len(pr_identities)
    result["event_identity_coverage"] = (
        CONFLICT if result["conflicting_terminal_events"] else PARTIAL if missing_identity else COMPLETE
    )
    result["file_read_observation_coverage"] = UNAVAILABLE
    result["tool_output_coverage"] = (
        CONFLICT if result["conflicting_terminal_events"] else PARTIAL if missing_identity else COMPLETE
    )
    if result["historical_pr_queries"]:
        result["historical_pr_metrics_coverage"] = (
            PARTIAL if result["historical_pr_unstructured_queries"] else COMPLETE
        )
    if not historical_observed:
        for key in (
            "historical_commit_queries", "historical_commit_results", "historical_pr_queries",
            "historical_pr_results", "historical_context_bytes",
        ):
            result.pop(key)
    return result


def credit_estimate(model: object, usage: Mapping[str, object]) -> dict[str, float | str | None]:
    key = str(model or "").casefold().strip()
    rates = RATE_TABLE.get(key)
    if rates is None:
        return {"rate_table_version": RATE_TABLE_VERSION, "credits": None, "eur": None}
    uncached = _number(usage.get("uncached_input_tokens"))
    cached = _number(usage.get("cached_input_tokens"))
    output = _number(usage.get("output_tokens"))
    if any(value is None for value in (uncached, cached, output)):
        return {"rate_table_version": RATE_TABLE_VERSION, "credits": None, "eur": None}
    credits = (
        int(uncached) * rates["uncached_input"]
        + int(cached) * rates["cached_input"]
        + int(output) * rates["output"]
    ) / 1_000_000
    return {
        "rate_table_version": RATE_TABLE_VERSION,
        "credits": round(credits, 8),
        "eur": round(credits * EUR_PER_CREDIT, 8),
    }


@dataclass(frozen=True)
class ProviderInvocation:
    run_id: str
    ordinal: int
    provider: str
    model: str | None
    phase: str
    role: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    usage: Mapping[str, object]
    model_authority: str = UNAVAILABLE
    raw_provider_model: str | None = None
    runtime_metadata: Mapping[str, object] | None = None
    retry_ordinal: int = 0
    churn: Mapping[str, object] | None = None
    invocation_id: str | None = None
    usage_snapshots: tuple[Mapping[str, object], ...] = ()


def persist_provider_invocation(root: Path, invocation: ProviderInvocation, *, central_database: Path | None = None) -> str:
    """Append one immutable provider invocation; unknowns remain NULL, never zero."""
    usage = dict(invocation.usage)
    input_tokens = _number(usage.get("input_tokens"))
    cached = _number(usage.get("cached_input_tokens"))
    if input_tokens is not None and cached is not None and cached > input_tokens:
        cached = None
    uncached = (
        input_tokens - cached
        if input_tokens is not None and cached is not None and cached <= input_tokens
        else None
    )
    output = _number(usage.get("output_tokens"))
    reasoning = _number(usage.get("reasoning_tokens"))
    total = _number(usage.get("total_tokens"))
    authority = (
        AUTHORITATIVE
        if any(value is not None for value in (input_tokens, cached, output, reasoning, total))
        else UNAVAILABLE
    )
    model_authority = (
        invocation.model_authority
        if invocation.model_authority in {AUTHORITATIVE, DERIVED, UNAVAILABLE}
        else UNAVAILABLE
    )
    model = invocation.model if model_authority != UNAVAILABLE else None
    estimate = credit_estimate(
        model,
        {"uncached_input_tokens": uncached, "cached_input_tokens": cached, "output_tokens": output},
    )
    identifier = (
        invocation.invocation_id or f"{invocation.run_id}-{invocation.ordinal}-{uuid4().hex[:12]}"
    )
    # Invocation churn is normally numeric aggregation.  A provider turn that
    # never returns an AgentResult additionally needs one small, deterministic
    # diagnostic to let the watcher recover the same terminal outcome after a
    # host interruption.  Keep this allow-list deliberately narrow: arbitrary
    # provider output is never retained here.
    churn: dict[str, int | str] = {}
    for key, value in (invocation.churn or {}).items():
        number = _number(value)
        if number is not None:
            churn[key] = number
        elif key in _SAFE_CHURN_TEXT_FIELDS and isinstance(value, str):
            compact = redact_diagnostic(value, limit=120)
            if compact:
                churn[key] = compact
    snapshots = tuple(
        {
            key: _number(snapshot.get(key))
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
        }
        for snapshot in invocation.usage_snapshots
        if isinstance(snapshot, Mapping)
    )
    if central_database is None:
        connection = open_storage(root)
    else:
        database = central_database.resolve()
        if not database.is_file():
            raise RuntimeError("CENTRAL provider-usage database is unavailable")
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        stored_churn = json.dumps(churn, sort_keys=True, separators=(",", ":"))
        values = (
            identifier, invocation.run_id, invocation.ordinal, invocation.provider, model,
            model_authority, invocation.raw_provider_model, invocation.phase, invocation.role,
            invocation.started_at, invocation.completed_at, invocation.duration_ms, input_tokens,
            cached, uncached, output, reasoning, total, authority,
            speed_state(invocation.runtime_metadata), invocation.retry_ordinal,
            estimate["credits"], estimate["eur"], RATE_TABLE_VERSION, stored_churn,
        )
        existing = connection.execute(
            """SELECT invocation_id,run_id,ordinal,provider,model,model_authority,
                      raw_provider_model,phase,role,started_at,completed_at,duration_ms,
                      input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,
                      reasoning_tokens,total_tokens,usage_authority,speed_state,retry_ordinal,
                      estimated_credits,estimated_eur,rate_table_version,churn
                 FROM provider_invocations
                WHERE invocation_id=? OR (run_id=? AND ordinal=?) LIMIT 1""",
            (identifier, invocation.run_id, invocation.ordinal),
        ).fetchone()
        if existing is not None:
            if tuple(existing)[1:] == values[1:]:
                return str(existing[0])
            raise EngineeringStorageError("Conflicting provider invocation replay.")
        connection.execute(
            """INSERT INTO provider_invocations(
                invocation_id,run_id,ordinal,provider,model,model_authority,raw_provider_model,phase,role,started_at,completed_at,duration_ms,
                input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,reasoning_tokens,total_tokens,
                usage_authority,speed_state,retry_ordinal,estimated_credits,estimated_eur,rate_table_version,churn
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        previous: dict[str, int | None] | None = None
        for ordinal, snapshot in enumerate(snapshots, 1):
            input_tokens = snapshot["input_tokens"]
            cached_input_tokens = snapshot["cached_input_tokens"]
            if input_tokens is not None and cached_input_tokens is not None and cached_input_tokens > input_tokens:
                cached_input_tokens = None
                snapshot["cached_input_tokens"] = None
            snapshot["uncached_input_tokens"] = (
                input_tokens - cached_input_tokens
                if input_tokens is not None and cached_input_tokens is not None
                and cached_input_tokens <= input_tokens
                else None
            )
            def delta(key: str) -> int | None:
                if previous is None or snapshot[key] is None or previous[key] is None:
                    return None
                return snapshot[key] - previous[key] if snapshot[key] >= previous[key] else None
            connection.execute(
                """INSERT OR IGNORE INTO provider_usage_snapshots(
                    invocation_id,ordinal,input_tokens,cached_input_tokens,uncached_input_tokens,
                    output_tokens,reasoning_tokens,total_tokens,input_delta,cached_input_delta,
                    uncached_input_delta,output_delta
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, ordinal, snapshot["input_tokens"], snapshot["cached_input_tokens"],
                 snapshot["uncached_input_tokens"], snapshot["output_tokens"], snapshot["reasoning_tokens"],
                 snapshot["total_tokens"], delta("input_tokens"), delta("cached_input_tokens"),
                 delta("uncached_input_tokens"), delta("output_tokens")),
            )
            previous = snapshot
    finally:
        connection.close()
    return identifier


def provider_usage_summary(
    root: Path, run_id: str, *, central_database: Path | None = None,
    _rows: list[sqlite3.Row] | None = None,
    _snapshot_rows: list[sqlite3.Row] | None = None,
) -> dict[str, object]:
    """Derive the canonical run-level usage projection.

    Values are observed sums.  Their completeness is carried independently per
    field, so an authoritative provider counter can still be partial for the
    run population.  Legacy scalar fields remain for compatibility only.
    """
    if _rows is None or _snapshot_rows is None:
        if central_database is None:
            connection = open_storage(root)
        else:
            database = central_database.resolve()
            if not database.is_file():
                raise RuntimeError("CENTRAL provider-usage database is unavailable")
            connection = sqlite3.connect(database, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
            """SELECT invocation_id,ordinal,provider,model,model_authority,raw_provider_model,
                      phase,role,started_at,completed_at,duration_ms,input_tokens,
                      cached_input_tokens,uncached_input_tokens,output_tokens,
                      reasoning_tokens,total_tokens,estimated_credits,estimated_eur,
                      speed_state,usage_authority,churn,retry_ordinal
                 FROM provider_invocations WHERE run_id=? ORDER BY ordinal""",
            (run_id,),
            ).fetchall()
            snapshot_rows = connection.execute(
            """SELECT invocation_id,ordinal,input_tokens,cached_input_tokens,
                      uncached_input_tokens,output_tokens,input_delta,cached_input_delta,
                      uncached_input_delta,output_delta
                 FROM provider_usage_snapshots
                WHERE invocation_id IN (
                    SELECT invocation_id FROM provider_invocations WHERE run_id=?
                ) ORDER BY invocation_id,ordinal""",
            (run_id,),
            ).fetchall()
        finally:
            connection.close()
    else:
        rows, snapshot_rows = _rows, _snapshot_rows
    if not rows:
        return {"invocation_detail": UNAVAILABLE}
    inputs = [row["input_tokens"] for row in rows if isinstance(row["input_tokens"], int)]
    snapshot_conflicts: dict[str, set[str]] = {}
    previous_snapshot: dict[str, sqlite3.Row] = {}
    for snapshot in snapshot_rows:
        invocation_id = str(snapshot["invocation_id"])
        previous = previous_snapshot.get(invocation_id)
        if previous is not None:
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                before, current = previous[key], snapshot[key]
                if isinstance(before, int) and isinstance(current, int) and current < before:
                    snapshot_conflicts.setdefault(invocation_id, set()).add(key)
                    if key in {"input_tokens", "cached_input_tokens"}:
                        snapshot_conflicts[invocation_id].add("uncached_input_tokens")
        previous_snapshot[invocation_id] = snapshot
    churn: dict[str, int | str] = {}
    for row in rows:
        try:
            values = json.loads(row["churn"])
        except (TypeError, json.JSONDecodeError):
            values = {}
        if isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, int):
                    previous = churn.get(key, 0)
                    churn[key] = (previous if isinstance(previous, int) else 0) + value
                elif key in _SAFE_CHURN_TEXT_FIELDS and isinstance(value, str):
                    # Scope is invocation evidence, not an aggregate.  The
                    # last invocation is the effective run projection.
                    churn[key] = value

    def total(key: str) -> int | float | None:
        values = [row[key] for row in rows if isinstance(row[key], (int, float))]
        return sum(values) if values else None

    def coverage(key: str, *, compatible: Callable[[sqlite3.Row], bool] | None = None) -> dict[str, object]:
        observed = sum(
            isinstance(row[key], (int, float)) and not isinstance(row[key], bool)
            and (compatible(row) if compatible is not None else True)
            for row in rows
        )
        conflicts = sum(key in values for values in snapshot_conflicts.values())
        state = CONFLICT if conflicts else COMPLETE if observed == len(rows) else PARTIAL if observed else UNAVAILABLE
        return {
            "coverage": state,
            "expected_observations": len(rows),
            "observed_observations": observed,
            "missing_reason": (
                f"Non-monotone {key} snapshots for {conflicts} invocation(s)" if conflicts
                else None if state == COMPLETE
                else f"{key} observed for {observed} of {len(rows)} invocations"
            ),
        }

    def metric(
        key: str, value: object, *, meaning: str, unit: str, provenance: str = AUTHORITATIVE,
        compatible: Callable[[sqlite3.Row], bool] | None = None,
    ) -> dict[str, object]:
        return {
            "value": value,
            "meaning": meaning,
            "unit": unit,
            "aggregation_level": "EP_RUN_ATTEMPT",
            "provenance": provenance,
            **coverage(key, compatible=compatible),
            "calculation_version": TELEMETRY_CALCULATION_VERSION,
            "source_snapshot_reference": run_id,
        }

    ordered = sorted(inputs)
    p95 = ordered[min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1)] if ordered else None
    input_deltas = [row["input_delta"] for row in snapshot_rows if isinstance(row["input_delta"], int)]
    calls_by_role: dict[str, int] = {}
    uncached_input_by_role: dict[str, int] = {}
    for row in rows:
        role = row["role"] if isinstance(row["role"], str) and row["role"] else "UNSPECIFIED"
        calls_by_role[role] = calls_by_role.get(role, 0) + 1
        if isinstance(row["uncached_input_tokens"], int):
            uncached_input_by_role[role] = uncached_input_by_role.get(role, 0) + row["uncached_input_tokens"]
    observed_history = any(key in churn for key in (
        "historical_commit_queries", "historical_pr_queries", "historical_context_bytes"
    ))
    compatible_cache_rows = [
        row for row in rows
        if isinstance(row["input_tokens"], int)
        and isinstance(row["cached_input_tokens"], int)
        and row["cached_input_tokens"] <= row["input_tokens"]
    ]
    compatible_input = sum(row["input_tokens"] for row in compatible_cache_rows)
    compatible_cached = sum(row["cached_input_tokens"] for row in compatible_cache_rows)
    cache_ratio = round(compatible_cached * 100 / compatible_input, 3) if compatible_input else None
    cost_complete = all(
        isinstance(row["estimated_credits"], (int, float))
        and isinstance(row["estimated_eur"], (int, float))
        for row in rows
    )
    usage_metrics = {
        "input_tokens": metric(
            "input_tokens", total("input_tokens"),
            meaning="Observed cumulative provider-invocation input; not current context size",
            unit="tokens",
        ),
        "cached_input_tokens": metric(
            "cached_input_tokens", total("cached_input_tokens"),
            meaning="Cached portion of compatible observed input",
            unit="tokens",
        ),
        "uncached_input_tokens": metric(
            "uncached_input_tokens", total("uncached_input_tokens"),
            meaning="Input minus cached input only where both counters are compatible",
            unit="tokens", provenance=DERIVED,
        ),
        "output_tokens": metric(
            "output_tokens", total("output_tokens"),
            meaning="Observed cumulative provider-invocation output",
            unit="tokens",
        ),
        "duration_ms": metric(
            "duration_ms", total("duration_ms"),
            meaning="Cumulative provider process duration; not model inference time",
            unit="milliseconds",
        ),
    }
    invocation_rows = []
    for row in rows:
        missing = [
            key for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            if not isinstance(row[key], int)
        ]
        raw_model = row["raw_provider_model"] if isinstance(row["raw_provider_model"], str) else None
        normalized_model = row["model"] if isinstance(row["model"], str) else None
        conflicts = snapshot_conflicts.get(str(row["invocation_id"]), set())
        observed_model = raw_model or normalized_model
        invocation_rows.append({
            "invocation_id": row["invocation_id"], "ordinal": row["ordinal"],
            "provider": row["provider"], "phase": row["phase"], "role": row["role"],
            "state": "COMPLETE" if row["completed_at"] else "INCOMPLETE",
            "started_at": row["started_at"], "completed_at": row["completed_at"],
            "duration_ms": row["duration_ms"], "retry_ordinal": row["retry_ordinal"],
            "retry_relation": None,
            "model": observed_model,
            "normalized_model": normalized_model,
            "model_provenance": row["model_authority"] if observed_model else UNAVAILABLE,
            "configured_model": normalized_model if row["model_authority"] == DERIVED else None,
            "configured_model_provenance": "CONFIGURED" if row["model_authority"] == DERIVED else UNAVAILABLE,
            "input_tokens": row["input_tokens"], "cached_input_tokens": row["cached_input_tokens"],
            "uncached_input_tokens": row["uncached_input_tokens"], "output_tokens": row["output_tokens"],
            "usage_coverage": CONFLICT if conflicts else COMPLETE if not missing else PARTIAL if len(missing) < 3 else UNAVAILABLE,
            "missing_usage_fields": missing,
            "conflicting_usage_fields": sorted(conflicts),
        })
    exact_pr_coverage = churn.get("historical_pr_metrics_coverage", UNAVAILABLE)
    exact_file_coverage = churn.get("file_read_observation_coverage", UNAVAILABLE)
    result = {
        "contract_version": TELEMETRY_CALCULATION_VERSION,
        "scope": "EP_RUN_ATTEMPT",
        "invocation_detail": AUTHORITATIVE,
        "provider_invocation_count": len(rows),
        "invocations": invocation_rows[:250],
        "invocation_observation_count": len(invocation_rows),
        "invocation_table_limit": 250,
        "invocation_table_truncated": len(invocation_rows) > 250,
        "metrics": usage_metrics,
        "provider_invocations_by_role": calls_by_role,
        "uncached_input_by_role": uncached_input_by_role or None,
        # Compatibility scalars. Coverage-aware consumers use ``metrics``.
        "input_tokens": total("input_tokens"),
        "cached_input_tokens": total("cached_input_tokens"),
        "uncached_input_tokens": total("uncached_input_tokens"),
        "output_tokens": total("output_tokens"),
        "total_provider_execution_ms": total("duration_ms"),
        "cache_ratio_percent": cache_ratio,
        "cache_ratio_population": {
            "coverage": COMPLETE if len(compatible_cache_rows) == len(rows) else PARTIAL if compatible_cache_rows else UNAVAILABLE,
            "expected_observations": len(rows), "observed_observations": len(compatible_cache_rows),
            "input_tokens": compatible_input or None, "cached_input_tokens": compatible_cached or None,
        },
        "max_input_tokens_per_invocation": max(inputs) if inputs else None,
        "median_input_tokens_per_invocation": median(inputs) if inputs else None,
        "p95_input_tokens_per_invocation": p95,
        "estimated_credits": total("estimated_credits") if cost_complete else None,
        "estimated_eur": total("estimated_eur") if cost_complete else None,
        "cost_coverage": COMPLETE if cost_complete else UNAVAILABLE,
        "cost_kind": "ESTIMATE" if cost_complete else UNAVAILABLE,
        "rate_table_version": RATE_TABLE_VERSION,
        "speed_state": next((row["speed_state"] for row in rows if row["speed_state"] != "UNKNOWN"), "UNKNOWN"),
        "usage_authority": AUTHORITATIVE
        if any(row["usage_authority"] == AUTHORITATIVE for row in rows)
        else UNAVAILABLE,
        "context_churn": churn,
        "historical_context_metrics_authority": AUTHORITATIVE if observed_history else UNAVAILABLE,
        "historical_commit_queries": churn.get("historical_commit_queries") if observed_history else None,
        "historical_commit_results": churn.get("historical_commit_results") if observed_history else None,
        "historical_pr_queries": churn.get("historical_pr_queries") if observed_history else None,
        "historical_pr_results": churn.get("historical_pr_results") if observed_history else None,
        "historical_pr_result_occurrences": churn.get("historical_pr_result_occurrences") if exact_pr_coverage != UNAVAILABLE else None,
        "historical_unique_pr_results": churn.get("historical_unique_pr_results") if exact_pr_coverage != UNAVAILABLE else None,
        "historical_pr_details_fetched": churn.get("historical_pr_details_fetched") if exact_pr_coverage != UNAVAILABLE else None,
        "historical_pr_metrics_coverage": exact_pr_coverage,
        "legacy_historical_pr_output_lines": churn.get("historical_pr_results_legacy_lines") or (
            churn.get("historical_pr_results") if observed_history and exact_pr_coverage == UNAVAILABLE else None
        ),
        "file_read_observations": None,
        "file_read_observation_coverage": exact_file_coverage,
        "unique_read_commands": churn.get("unique_read_commands"),
        "repeated_read_commands": churn.get("repeated_read_commands"),
        "historical_context_bytes": churn.get("historical_context_bytes") if observed_history else None,
        "usage_snapshot_count": len(snapshot_rows) or None,
        "intermediate_usage_delta_available": bool(input_deltas),
        "maximum_incremental_input_tokens": max(input_deltas) if input_deltas else None,
        "actual_single_request_context_size": UNAVAILABLE,
        "active_context_size": UNAVAILABLE,
        "deprecated_fields": {
            "historical_pr_results": "Legacy output-line/result compatibility field; not PRs inspected",
            "file_read_count": "Derived read-command count; not exact file observations",
            "repeated_file_read_count": "Derived repeated command count; not exact repeated file reads",
            "estimated_credits": "Estimate only; never billing or subscription budget",
        },
    }
    return result


def provider_usage_summaries(
    root: Path, run_ids: list[str], *, central_database: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Load a bounded run population in two queries, then reuse the canonical reducer."""
    identifiers = list(dict.fromkeys(value for value in run_ids if isinstance(value, str) and value))[:1000]
    if not identifiers:
        return {}
    if central_database is None:
        connection = open_storage(root)
    else:
        database = central_database.resolve()
        if not database.is_file():
            raise RuntimeError("CENTRAL provider-usage database is unavailable")
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in identifiers)
    try:
        rows = connection.execute(
            f"""SELECT invocation_id,run_id,ordinal,provider,model,model_authority,
                       raw_provider_model,phase,role,started_at,completed_at,duration_ms,
                       input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,
                       reasoning_tokens,total_tokens,estimated_credits,estimated_eur,
                       speed_state,usage_authority,churn,retry_ordinal
                  FROM provider_invocations WHERE run_id IN ({placeholders})
                  ORDER BY run_id,ordinal""",
            identifiers,
        ).fetchall()
        snapshot_rows = connection.execute(
            f"""SELECT p.run_id,s.invocation_id,s.ordinal,s.input_tokens,s.cached_input_tokens,
                       s.uncached_input_tokens,s.output_tokens,s.input_delta,s.cached_input_delta,
                       s.uncached_input_delta,s.output_delta
                  FROM provider_usage_snapshots AS s
                  JOIN provider_invocations AS p ON p.invocation_id=s.invocation_id
                 WHERE p.run_id IN ({placeholders}) ORDER BY p.run_id,s.invocation_id,s.ordinal""",
            identifiers,
        ).fetchall()
    finally:
        connection.close()
    rows_by_run: dict[str, list[sqlite3.Row]] = {run_id: [] for run_id in identifiers}
    snapshots_by_run: dict[str, list[sqlite3.Row]] = {run_id: [] for run_id in identifiers}
    for row in rows:
        rows_by_run[str(row["run_id"])].append(row)
    for row in snapshot_rows:
        snapshots_by_run[str(row["run_id"])].append(row)
    return {
        run_id: provider_usage_summary(
            root, run_id, central_database=central_database,
            _rows=rows_by_run[run_id], _snapshot_rows=snapshots_by_run[run_id],
        )
        for run_id in identifiers
    }
