"""Provider invocation usage and bounded context-churn attribution.

This module deliberately stores only counters derived from provider JSONL.  It
never stores prompts, tool arguments, command output, paths, or model replies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path
import re
from statistics import median
from typing import Mapping
from uuid import uuid4

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


def usage_from_jsonl(*outputs: str) -> dict[str, int]:
    """Use the last explicit provider usage snapshot, never sum repeated snapshots."""
    result: dict[str, int] = {}
    for output in outputs:
        for line in output.splitlines():
            try:
                found = _usage_from_event(json.loads(line))
            except json.JSONDecodeError:
                continue
            if found:
                result.update(found)
    return result


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
        )
    }
    reads: set[str] = set()
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
    result["distinct_files_read"] = len(reads)
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


def persist_provider_invocation(root: Path, invocation: ProviderInvocation) -> str:
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
    churn = {
        key: _number(value)
        for key, value in (invocation.churn or {}).items()
        if _number(value) is not None
    }
    connection = open_storage(root)
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
    finally:
        connection.close()
    return identifier


def provider_usage_summary(root: Path, run_id: str) -> dict[str, object]:
    """Derive run-level totals without treating cumulative input as context size."""
    connection = open_storage(root)
    try:
        rows = connection.execute(
            "SELECT provider,model,model_authority,raw_provider_model,input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,duration_ms,estimated_credits,estimated_eur,speed_state,usage_authority,churn FROM provider_invocations WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return {"invocation_detail": UNAVAILABLE}
    inputs = [row[4] for row in rows if isinstance(row[4], int)]
    churn: dict[str, int] = {}
    for row in rows:
        try:
            values = json.loads(row[13])
        except (TypeError, json.JSONDecodeError):
            values = {}
        if isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, int):
                    churn[key] = churn.get(key, 0) + value

    def total(index: int) -> int | float | None:
        values = [row[index] for row in rows if isinstance(row[index], (int, float))]
        return sum(values) if values else None

    ordered = sorted(inputs)
    p95 = ordered[min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1)] if ordered else None
    return {
        "invocation_detail": AUTHORITATIVE,
        "provider_invocation_count": len(rows),
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
    }
