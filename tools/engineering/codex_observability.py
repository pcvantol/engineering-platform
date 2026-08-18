"""Bounded Codex CLI usage and structured runtime-provenance extraction."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

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
    """Return only structured runtime metadata explicitly emitted by Codex.

    JSONL is the invocation contract.  In particular, agent prose and terminal
    output are not a source of model, reasoning, or speed attribution.
    """
    aliases = {
        "model": "raw_provider_model",
        "model_name": "raw_provider_model",
        "reasoning_effort": "reasoning_profile",
        "reasoning_profile": "reasoning_profile",
        "speed_mode": "speed_mode",
        "speed_state": "speed_state",
        "fast_mode": "fast_mode",
    }

    def collect(container: object, metadata: dict[str, str]) -> None:
        if not isinstance(container, dict):
            return
        for key, value in container.items():
            alias = aliases.get(str(key).casefold().replace("-", "_"))
            if alias == "fast_mode" and isinstance(value, bool):
                metadata.setdefault(alias, "fast" if value else "normal")
            elif alias and isinstance(value, str) and value.strip():
                metadata.setdefault(alias, value.strip())

    metadata: dict[str, str] = {"runtime_provider": "codex_cli"}
    for output in outputs:
        for raw_line in output.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            # These are provider-owned event envelopes, not recursively walked:
            # recursive parsing could mistake an agent/tool payload for runtime
            # provenance.
            collect(event, metadata)
            collect(event.get("metadata"), metadata)
            item = event.get("item")
            collect(item, metadata)
            if isinstance(item, dict):
                collect(item.get("metadata"), metadata)
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
