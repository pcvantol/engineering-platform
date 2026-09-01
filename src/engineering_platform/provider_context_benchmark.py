"""Deterministic structural benchmark for provider-context reduction.

This is deliberately a shape benchmark: it makes no production token or cost
claim and never invokes a provider.
"""

from __future__ import annotations

from .provider_context import ProviderRole, project_context


def benchmark_shape(objective: str) -> dict[str, dict[str, int]]:
    """Return comparable byte/call proxies for representative role scenarios."""
    full = len(objective.encode("utf-8"))
    return {
        "deterministic_preflight_blocker": {
            "provider_calls": 0,
            "context_bytes": 0,
            "repeated_reads": 0,
            "injected_output_bytes": 0,
        },
        "implementation": {
            "provider_calls": 1,
            "context_bytes": project_context(ProviderRole.IMPLEMENTATION, objective).telemetry["context_projected_bytes"],
            "repeated_reads": 0,
            "injected_output_bytes": 0,
        },
        "repair": {
            "provider_calls": 1,
            "context_bytes": project_context(ProviderRole.REPAIR, objective).telemetry["context_projected_bytes"],
            "repeated_reads": 0,
            "injected_output_bytes": 0,
        },
        "passive_merge_wait": {
            "provider_calls": 0,
            "context_bytes": 0,
            "repeated_reads": 0,
            "injected_output_bytes": 0,
        },
        "baseline_full_replay_bytes": {"context_bytes": full * 2},
    }
