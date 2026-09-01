"""Read-only Codex capacity evidence used by admission and dashboard projections.

The caller receives only a derived percentage.  Account identity, credits and
the raw app-server response remain in the Codex process boundary.
"""

from __future__ import annotations

import json
import select
import time

from .providers import CodexCliProvider


def normalize_rate_limits(payload: object) -> dict[str, object]:
    """Keep only quota-window fields that are safe to show or evaluate."""
    if not isinstance(payload, dict):
        return {}
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict):
        return {}
    windows: list[dict[str, int]] = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        used = item.get("usedPercent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        windows.append({"used_percent": max(0, min(100, round(used)))})
    return {"windows": windows} if windows else {}


def remaining_percent(rate_limits: dict[str, object]) -> float | None:
    """Return the lowest remaining quota across all safely observed windows."""
    windows = rate_limits.get("windows")
    if not isinstance(windows, list):
        return None
    remaining = [
        max(0.0, min(100.0, 100.0 - float(window["used_percent"])))
        for window in windows
        if isinstance(window, dict)
        and isinstance(window.get("used_percent"), (int, float))
        and not isinstance(window.get("used_percent"), bool)
    ]
    return min(remaining) if remaining else None


def read_remaining_percent(*, timeout_seconds: float = 5) -> float | None:
    """Ask the local Codex app-server for fresh quota evidence, without mutation."""
    provider = CodexCliProvider()
    process = None
    try:
        process = provider.app_server()
        if process.stdin is None or process.stdout is None:
            return None
        process.stdin.write(json.dumps({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "engineering_capacity_admission", "version": "2.0"}}}) + "\n")
        process.stdin.flush()
        deadline, requested = time.monotonic() + timeout_seconds, False
        while time.monotonic() < deadline:
            ready, _, _ = select.select((process.stdout,), (), (), max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == 1 and not requested:
                process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                process.stdin.write(json.dumps({"method": "account/rateLimits/read", "id": 2, "params": {}}) + "\n")
                process.stdin.flush()
                requested = True
            elif response.get("id") == 2:
                return remaining_percent(normalize_rate_limits(response.get("result")))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if process is not None:
            provider.close_app_server(process)
    return None
