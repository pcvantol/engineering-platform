"""Bounded Codex CLI usage and runtime-provenance extraction."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .agent_state import redact_diagnostic


USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "remaining",
        "plan_remaining",
        "usage",
    }
)


def extract_codex_usage(*outputs: str) -> dict[str, int | float | str]:
    """Extract only explicitly reported, display-safe CLI usage fields."""
    usage: dict[str, int | float | str] = {}

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, candidate in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in USAGE_KEYS and isinstance(candidate, (int, float)) and candidate >= 0:
                    usage[normalized] = candidate
                elif normalized in {"usage", "token_usage"}:
                    collect(candidate)
                elif isinstance(candidate, dict):
                    collect(candidate)
        elif isinstance(value, list):
            for candidate in value:
                collect(candidate)

    for output in outputs:
        for line in output.splitlines():
            try:
                collect(json.loads(line))
            except json.JSONDecodeError:
                continue
    return usage


def extract_codex_runtime_metadata(*outputs: str) -> dict[str, str]:
    """Return only runtime metadata explicitly emitted by the Codex CLI."""
    aliases = {
        "model": "model",
        "model_name": "model",
        "reasoning effort": "reasoning_profile",
        "reasoning_effort": "reasoning_profile",
        "reasoning": "reasoning_profile",
        "configuration profile": "configuration_profile",
        "configuration_profile": "configuration_profile",
        "sandbox": "configuration_profile",
        "approval": "configuration_profile",
        "provider": "provider",
    }
    metadata: dict[str, str] = {"runtime_provider": "codex_cli"}
    for output in outputs:
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            normalized = aliases.get(key.casefold())
            if not normalized or not value:
                continue
            value = redact_diagnostic(value, limit=120)
            if value and value != "[REDACTED]":
                previous = metadata.get(normalized)
                metadata[normalized] = (
                    f"{previous}; {key}: {value}"
                    if previous and previous != value and normalized == "configuration_profile"
                    else value
                )
    return metadata


def codex_final_message(output: str) -> str:
    """Extract the final agent message from Codex JSONL, with legacy fallback."""
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return output.strip().splitlines()[-1] if output.strip() else ""


def write_codex_usage(root: Path, run_id: str, usage: dict[str, int | float | str]) -> None:
    """Persist cumulative, explicitly-reported CLI usage for one run only."""
    safe_usage = {
        key: value
        for key, value in usage.items()
        if key in USAGE_KEYS and isinstance(value, (int, float)) and value >= 0
    }
    if not safe_usage:
        return
    directory = root / ".engineering" / "status"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing: dict[str, int | float] = {}
    try:
        prior = json.loads((directory / "codex_usage.json").read_text(encoding="utf-8"))
        if prior.get("run_id") == run_id and isinstance(prior.get("usage"), dict):
            existing = {
                key: value
                for key, value in prior["usage"].items()
                if key in USAGE_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool)
            }
    except (OSError, json.JSONDecodeError):
        pass
    token_keys = {"input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"}
    safe_usage = {
        key: (existing.get(key, 0) + value if key in token_keys else value)
        for key, value in safe_usage.items()
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-usage.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "usage": safe_usage}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "codex_usage.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
