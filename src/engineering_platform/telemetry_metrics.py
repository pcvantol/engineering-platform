"""Shared metric coverage and aggregation semantics for telemetry projections.

The helpers in this module deliberately know nothing about UI presentation.
They preserve the difference between a value being present and that value
being valid evidence.  Every higher-level projection (run, day and chain)
uses the same state transition rules.
"""

from __future__ import annotations

from typing import Iterable, Mapping


COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"
CONFLICT = "CONFLICT"
COVERAGE_STATES = frozenset({COMPLETE, PARTIAL, UNAVAILABLE, CONFLICT})
TELEMETRY_CALCULATION_VERSION = "telemetry-contract@2.2"
VALID_SUBTOTAL = "VALID_OBSERVATIONS_SUBTOTAL"


def metric_coverage(
    *,
    expected: int | None,
    present: int,
    valid: int,
    conflicting: int = 0,
    reason: str | None = None,
) -> dict[str, object]:
    """Return one explainable coverage record without inventing population.

    ``observed_observations`` remains as a compatibility alias for valid
    observations.  New consumers use the explicit present/valid/conflicting
    fields.
    """
    if expected is not None and expected < 0:
        expected = None
    present = max(0, int(present))
    valid = max(0, min(int(valid), present))
    conflicting = max(0, min(int(conflicting), present))
    missing = max(0, expected - present) if expected is not None else None
    if conflicting:
        state = CONFLICT
    elif expected is None:
        state = PARTIAL if present else UNAVAILABLE
    elif expected == 0:
        # A known empty population is complete; this is distinct from an
        # unknown expected count represented by ``None``.
        state = COMPLETE if present == 0 else CONFLICT
    elif valid == expected and present == expected:
        state = COMPLETE
    elif present or valid:
        state = PARTIAL
    else:
        state = UNAVAILABLE
    if reason is None and state != COMPLETE:
        if conflicting:
            reason = f"{conflicting} conflicting observation(s)"
        elif expected is None:
            reason = "Expected observation population is unknown"
        else:
            reason = f"{valid} valid of {expected} expected observation(s)"
    return {
        "coverage": state,
        "expected_observations": expected,
        "present_observations": present,
        "valid_observations": valid,
        "observed_observations": valid,
        "missing_observations": missing,
        "conflicting_observations": conflicting,
        "missing_reason": reason,
    }


def aggregate_coverage(metrics: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Merge source coverage while retaining conflicts and unknown totals."""
    rows = list(metrics)
    if not rows:
        return metric_coverage(expected=None, present=0, valid=0)
    expected_known = all(
        isinstance(row.get("expected_observations"), int)
        and not isinstance(row.get("expected_observations"), bool)
        for row in rows
    )
    expected = sum(int(row["expected_observations"]) for row in rows) if expected_known else None
    present = sum(_count(row, "present_observations", fallback="observed_observations") for row in rows)
    valid = sum(_valid_count(row) for row in rows)
    conflicting = sum(_conflict_count(row) for row in rows)
    reasons = [
        str(row.get("missing_reason"))
        for row in rows
        if row.get("coverage") != COMPLETE and row.get("missing_reason")
    ]
    source_states = {row.get("coverage") for row in rows}
    if CONFLICT in source_states and conflicting == 0:
        conflicting = 1
        present = max(present, 1)
    result = metric_coverage(
        expected=expected, present=present, valid=valid, conflicting=conflicting,
        reason="; ".join(dict.fromkeys(reasons)) or None,
    )
    # Counts explain population, but they cannot upgrade an upstream semantic
    # state. A timing projection can have all numeric durations present while
    # remaining PARTIAL because its parent relation is missing, for example.
    if result["coverage"] != CONFLICT and ({PARTIAL, UNAVAILABLE} & source_states):
        result["coverage"] = PARTIAL if present or valid or COMPLETE in source_states or PARTIAL in source_states else UNAVAILABLE
        result["missing_reason"] = (
            "; ".join(dict.fromkeys(reasons))
            or "One or more source projections are partial or unavailable"
        )
    return result


def aggregate_numeric_metric(
    metrics: Iterable[Mapping[str, object]], *, aggregation_level: str,
    aggregation: str = "sum", meaning: str | None = None,
    unit: str | None = None, provenance: str | None = None,
) -> dict[str, object]:
    """Aggregate valid metric values according to their declared meaning."""
    rows = list(metrics)
    coverage = aggregate_coverage(rows)
    values = [
        row.get("value") for row in rows
        if (
            row.get("coverage") != CONFLICT
            or row.get("value_semantics") == VALID_SUBTOTAL
        )
        and isinstance(row.get("value"), (int, float))
        and not isinstance(row.get("value"), bool)
    ]
    value: int | float | None
    if not values:
        value = None
    elif aggregation == "max":
        value = max(values)
    elif aggregation == "sum":
        value = sum(values)
    else:
        raise ValueError(f"Unsupported telemetry aggregation: {aggregation}")
    return {
        "value": value,
        "meaning": meaning or next((str(row["meaning"]) for row in rows if row.get("meaning")), None),
        "unit": unit or next((str(row["unit"]) for row in rows if row.get("unit")), None),
        "aggregation": aggregation,
        "aggregation_level": aggregation_level,
        "provenance": provenance or _merged_provenance(rows),
        # The value is the subtotal over observations that remain valid for
        # this metric. Coverage remains independent and can still be PARTIAL
        # or CONFLICT. Higher-level reducers may retain only values carrying
        # this marker when a source population is conflicted.
        "value_semantics": VALID_SUBTOTAL if value is not None else None,
        **coverage,
        "calculation_version": TELEMETRY_CALCULATION_VERSION,
        "source_snapshot_references": sorted({
            str(reference)
            for row in rows
            for reference in _references(row)
            if reference
        }),
    }


def _count(row: Mapping[str, object], key: str, *, fallback: str) -> int:
    value = row.get(key, row.get(fallback, 0))
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _valid_count(row: Mapping[str, object]) -> int:
    value = row.get("valid_observations")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    observed = _count(row, "observed_observations", fallback="observed_observations")
    return 0 if row.get("coverage") == CONFLICT else observed


def _conflict_count(row: Mapping[str, object]) -> int:
    value = row.get("conflicting_observations")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 1 if row.get("coverage") == CONFLICT else 0


def _merged_provenance(rows: list[Mapping[str, object]]) -> str:
    values = {str(row.get("provenance")) for row in rows if row.get("provenance")}
    return next(iter(values)) if len(values) == 1 else "MIXED" if values else "UNAVAILABLE"


def _references(row: Mapping[str, object]) -> list[object]:
    plural = row.get("source_snapshot_references")
    if isinstance(plural, list):
        return plural
    reference = row.get("source_snapshot_reference")
    return [reference] if reference is not None else []
