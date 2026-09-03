"""Provider invocation usage and bounded provider-input attribution.

This module deliberately stores only counters derived from provider JSONL.  It
never stores prompts, tool arguments, command output, paths, or model replies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path
import sqlite3
import re
from statistics import median
from typing import Mapping
from uuid import uuid4

from .agent_state import redact_diagnostic
from .storage import open_storage


RATE_TABLE_VERSION = "2026-08-18"
EUR_PER_CREDIT = 0.04
RATE_TABLE = {
    "gpt-5.6-sol": {"uncached_input": 125.0, "cached_input": 12.5, "output": 750.0},
    "gpt-5.6-terra": {"uncached_input": 50.0, "cached_input": 5.0, "output": 300.0},
    "gpt-5.6-luna": {"uncached_input": 5.0, "cached_input": 0.5, "output": 30.0},
}
AUTHORITATIVE, DERIVED, UNAVAILABLE = "AUTHORITATIVE", "DERIVED", "UNAVAILABLE"
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
    return usage


def usage_snapshots_from_jsonl(*outputs: str) -> tuple[dict[str, int], ...]:
    """Return only actual final-turn counter snapshots, without conversation content.

    Codex CLI 0.147.0 exposes usage on ``turn.completed``.  The counters are
    provider-execution cumulative counters, not an active-context measurement.
    """
    snapshots: list[dict[str, int]] = []
    for output in outputs:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                found = _usage_from_event(event.get("usage"))
                if found:
                    snapshots.append(found)
    return tuple(snapshots)


def usage_from_jsonl(*outputs: str) -> dict[str, int]:
    """Use the last final-turn usage snapshot; never sum repeated snapshots."""
    snapshots = usage_snapshots_from_jsonl(*outputs)
    return dict(snapshots[-1]) if snapshots else {}


def churn_from_jsonl(*outputs: str) -> dict[str, int]:
    """Measure deterministic, content-free churn indicators from JSONL events."""
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
            "git_output_bytes",
            "github_output_bytes",
            "historical_commit_queries",
            "historical_commit_results",
            "historical_pr_queries",
            "historical_pr_results",
            "historical_context_bytes",
            "tool_loop_operations",
        )
    }
    reads: set[str] = set()
    historical_observed = False
    for output in outputs:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            command = item.get("command")
            if not isinstance(command, str):
                continue
            normalized = command.casefold()
            result["shell_command_calls"] += 1
            # One command-execution event is one observed tool-loop operation.
            # This is a derived churn counter, not token attribution and does
            # not require a storage-schema column.
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
                fingerprint = re.sub(r"\s+", " ", command.strip())[:512]
                result["file_read_count"] += 1
                if fingerprint in reads:
                    result["repeated_file_read_count"] += 1
                reads.add(fingerprint)
            raw = item.get("aggregated_output", item.get("output", ""))
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
            result["tool_output_bytes"] += size
            result["maximum_tool_output_bytes"] = max(result["maximum_tool_output_bytes"], size)
            if is_test:
                if item.get("exit_code") == 0:
                    result["passing_test_output_bytes"] += size
                else:
                    result["failed_test_diagnostic_bytes"] += size
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
                result["historical_pr_results"] += len(raw.splitlines()) if isinstance(raw, str) else 0
                result["historical_context_bytes"] += size
    result["distinct_files_read"] = len(reads)
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
        connection.execute(
            """INSERT OR IGNORE INTO provider_invocations(
                invocation_id,run_id,ordinal,provider,model,model_authority,raw_provider_model,phase,role,started_at,completed_at,duration_ms,
                input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,reasoning_tokens,total_tokens,
                usage_authority,speed_state,retry_ordinal,estimated_credits,estimated_eur,rate_table_version,churn
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                invocation.run_id,
                invocation.ordinal,
                invocation.provider,
                model,
                model_authority,
                invocation.raw_provider_model,
                invocation.phase,
                invocation.role,
                invocation.started_at,
                invocation.completed_at,
                invocation.duration_ms,
                input_tokens,
                cached,
                uncached,
                output,
                reasoning,
                total,
                authority,
                speed_state(invocation.runtime_metadata),
                invocation.retry_ordinal,
                estimate["credits"],
                estimate["eur"],
                RATE_TABLE_VERSION,
                json.dumps(churn, sort_keys=True, separators=(",", ":")),
            ),
        )
        previous: dict[str, int | None] | None = None
        for ordinal, snapshot in enumerate(snapshots, 1):
            input_tokens = snapshot["input_tokens"]
            cached_input_tokens = snapshot["cached_input_tokens"]
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


def provider_usage_summary(root: Path, run_id: str, *, central_database: Path | None = None) -> dict[str, object]:
    """Derive run-level totals without treating cumulative input as context size."""
    if central_database is None:
        connection = open_storage(root)
    else:
        database = central_database.resolve()
        if not database.is_file():
            raise RuntimeError("CENTRAL provider-usage database is unavailable")
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        rows = connection.execute(
            "SELECT provider,model,model_authority,raw_provider_model,input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,duration_ms,estimated_credits,estimated_eur,speed_state,usage_authority,churn,role FROM provider_invocations WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            "SELECT input_delta,cached_input_delta,uncached_input_delta,output_delta FROM provider_usage_snapshots WHERE invocation_id IN (SELECT invocation_id FROM provider_invocations WHERE run_id=?)",
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return {"invocation_detail": UNAVAILABLE}
    inputs = [row[4] for row in rows if isinstance(row[4], int)]
    churn: dict[str, int | str] = {}
    for row in rows:
        try:
            values = json.loads(row[13])
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

    def total(index: int) -> int | float | None:
        values = [row[index] for row in rows if isinstance(row[index], (int, float))]
        return sum(values) if values else None

    ordered = sorted(inputs)
    p95 = ordered[min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1)] if ordered else None
    input_deltas = [row[0] for row in snapshot_rows if isinstance(row[0], int)]
    calls_by_role: dict[str, int] = {}
    uncached_input_by_role: dict[str, int] = {}
    for row in rows:
        role = row[14] if isinstance(row[14], str) and row[14] else "UNSPECIFIED"
        calls_by_role[role] = calls_by_role.get(role, 0) + 1
        if isinstance(row[6], int):
            uncached_input_by_role[role] = uncached_input_by_role.get(role, 0) + row[6]
    observed_history = any(key in churn for key in (
        "historical_commit_queries", "historical_pr_queries", "historical_context_bytes"
    ))
    return {
        "invocation_detail": AUTHORITATIVE,
        "provider_invocation_count": len(rows),
        "provider_invocations_by_role": calls_by_role,
        "uncached_input_by_role": uncached_input_by_role or None,
        "input_tokens": total(4),
        "cached_input_tokens": total(5),
        "uncached_input_tokens": total(6),
        "output_tokens": total(7),
        "total_provider_execution_ms": total(8),
        "max_input_tokens_per_invocation": max(inputs) if inputs else None,
        "median_input_tokens_per_invocation": median(inputs) if inputs else None,
        "p95_input_tokens_per_invocation": p95,
        "estimated_credits": total(9),
        "estimated_eur": total(10),
        "rate_table_version": RATE_TABLE_VERSION,
        "speed_state": next((row[11] for row in rows if row[11] != "UNKNOWN"), "UNKNOWN"),
        "usage_authority": AUTHORITATIVE
        if any(row[12] == AUTHORITATIVE for row in rows)
        else UNAVAILABLE,
        "context_churn": churn,
        "historical_context_metrics_authority": AUTHORITATIVE if observed_history else UNAVAILABLE,
        "historical_commit_queries": churn.get("historical_commit_queries") if observed_history else None,
        "historical_commit_results": churn.get("historical_commit_results") if observed_history else None,
        "historical_pr_queries": churn.get("historical_pr_queries") if observed_history else None,
        "historical_pr_results": churn.get("historical_pr_results") if observed_history else None,
        "historical_context_bytes": churn.get("historical_context_bytes") if observed_history else None,
        "usage_snapshot_count": len(snapshot_rows) or None,
        "intermediate_usage_delta_available": bool(input_deltas),
        "maximum_incremental_input_tokens": max(input_deltas) if input_deltas else None,
        "actual_single_request_context_size": UNAVAILABLE,
        "active_context_size": UNAVAILABLE,
    }
