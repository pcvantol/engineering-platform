"""Canonical telemetry export models and Markdown/JSON serializers.

Exports are read-only projections.  Both formats serialize the same model;
neither serializer recalculates telemetry metrics.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from threading import RLock
from time import monotonic
from typing import Mapping, Sequence

from .telemetry_metrics import aggregate_numeric_metric, metric_coverage


EXPORT_SCHEMA_VERSION = "telemetry-export@1.1"
SUPPORTED_LOCALES = frozenset({"en", "nl", "de", "fr", "es"})
SNAPSHOT_TTL_SECONDS = 600
MAX_RETAINED_SNAPSHOTS = 32
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


class ExportSnapshotStore:
    """Bounded in-memory readback for already projected, privacy-safe models."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._models: dict[str, tuple[float, str, dict[str, object]]] = {}

    def retain(self, model: Mapping[str, object], *, binding: str) -> str:
        snapshot_id = model.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("sha256:"):
            raise ValueError("TELEMETRY_EXPORT_SNAPSHOT_INVALID")
        encoded = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise ValueError("TELEMETRY_EXPORT_SNAPSHOT_TOO_LARGE")
        now = monotonic()
        with self._lock:
            self._expire(now)
            if len(self._models) >= MAX_RETAINED_SNAPSHOTS and snapshot_id not in self._models:
                oldest = min(self._models, key=lambda key: self._models[key][0])
                self._models.pop(oldest, None)
            self._models[snapshot_id] = (now + SNAPSHOT_TTL_SECONDS, binding, deepcopy(dict(model)))
        return snapshot_id

    def read(self, snapshot_id: str, *, binding: str) -> dict[str, object] | None:
        now = monotonic()
        with self._lock:
            self._expire(now)
            retained = self._models.get(snapshot_id)
            if retained is None or retained[1] != binding:
                return None
            return deepcopy(retained[2])

    def _expire(self, now: float) -> None:
        for key, (expires_at, _, _) in list(self._models.items()):
            if expires_at <= now:
                self._models.pop(key, None)


def download_model(model: Mapping[str, object]) -> dict[str, object]:
    """Stamp download time without changing the retained source snapshot."""
    result = deepcopy(dict(model))
    result["downloaded_at"] = datetime.now(timezone.utc).isoformat()
    return result

_LABELS = {
    "en": {"overview": "Telemetry overview", "detail": "Telemetry detail", "selection": "Selection", "summary": "Summary", "coverage": "Coverage", "runs": "Runs", "attempt": "Attempt", "inclusive": "Inclusive phase workload", "inclusive_note": "Inclusive shares may overlap and are not additive.", "exclusive": "Exclusive elapsed-time distribution", "bottlenecks": "Bottlenecks", "invocations": "Provider invocations", "timeline": "Timeline", "chain": "Execution chain", "limitations": "Limitations and conflicts", "field": "Field", "value": "Value", "unavailable": "Unavailable"},
    "nl": {"overview": "Telemetrieoverzicht", "detail": "Telemetriedetail", "selection": "Selectie", "summary": "Samenvatting", "coverage": "Dekking", "runs": "Uitvoeringen", "attempt": "Poging", "inclusive": "Inclusieve fasewerklast", "inclusive_note": "Inclusieve aandelen mogen overlappen en zijn niet optelbaar.", "exclusive": "Exclusieve doorlooptijdverdeling", "bottlenecks": "Knelpunten", "invocations": "Providerinvocations", "timeline": "Tijdlijn", "chain": "Uitvoeringsketen", "limitations": "Beperkingen en conflicten", "field": "Veld", "value": "Waarde", "unavailable": "Niet beschikbaar"},
    "de": {"overview": "Telemetrieübersicht", "detail": "Telemetriedetail", "selection": "Auswahl", "summary": "Zusammenfassung", "coverage": "Abdeckung", "runs": "Ausführungen", "attempt": "Versuch", "inclusive": "Inklusive Phasenarbeitslast", "inclusive_note": "Inklusive Anteile dürfen sich überlappen und sind nicht addierbar.", "exclusive": "Exklusive Laufzeitverteilung", "bottlenecks": "Engpässe", "invocations": "Provider-Aufrufe", "timeline": "Zeitachse", "chain": "Ausführungskette", "limitations": "Einschränkungen und Konflikte", "field": "Feld", "value": "Wert", "unavailable": "Nicht verfügbar"},
    "fr": {"overview": "Vue d’ensemble de la télémétrie", "detail": "Détail de télémétrie", "selection": "Sélection", "summary": "Résumé", "coverage": "Couverture", "runs": "Exécutions", "attempt": "Tentative", "inclusive": "Charge de phase inclusive", "inclusive_note": "Les parts inclusives peuvent se chevaucher et ne sont pas additionnables.", "exclusive": "Répartition exclusive du temps", "bottlenecks": "Goulets d’étranglement", "invocations": "Invocations fournisseur", "timeline": "Chronologie", "chain": "Chaîne d’exécution", "limitations": "Limites et conflits", "field": "Champ", "value": "Valeur", "unavailable": "Indisponible"},
    "es": {"overview": "Resumen de telemetría", "detail": "Detalle de telemetría", "selection": "Selección", "summary": "Resumen", "coverage": "Cobertura", "runs": "Ejecuciones", "attempt": "Intento", "inclusive": "Carga de fase inclusiva", "inclusive_note": "Las proporciones inclusivas pueden solaparse y no son sumables.", "exclusive": "Distribución exclusiva del tiempo", "bottlenecks": "Cuellos de botella", "invocations": "Invocaciones del proveedor", "timeline": "Cronología", "chain": "Cadena de ejecución", "limitations": "Limitaciones y conflictos", "field": "Campo", "value": "Valor", "unavailable": "No disponible"},
}


def overview_model(
    *, project_id: str, rows: Sequence[Mapping[str, object]], sort_key: str,
    sort_direction: str, locale: str, retention_days: int | None = None,
    source_as_of: str | None = None, source_reference: str | None = None,
) -> dict[str, object]:
    comparable = [row for row in rows if _sort_value(row, sort_key)[0] == 0]
    unavailable = [row for row in rows if _sort_value(row, sort_key)[0] != 0]
    ordered = sorted(
        comparable, key=lambda row: _sort_value(row, sort_key)[1],
        reverse=sort_direction == "desc",
    ) + sorted(unavailable, key=lambda row: str(row.get("date") or ""), reverse=True)
    selection = {
        "project_id": project_id, "scope": "TELEMETRY_OVERVIEW",
        "timezone": "UTC", "filters": {"retention_days": retention_days},
        "aggregation_level": "UTC_DAY", "sort": {"key": sort_key, "direction": sort_direction},
    }
    names = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")
    usage_summary: dict[str, object] = {}
    for name in names:
        metrics = []
        for row in ordered:
            usage = row.get("usage_metrics")
            metric = usage.get(name) if isinstance(usage, Mapping) else None
            metrics.append(metric if isinstance(metric, Mapping) else {
                "value": None,
                **metric_coverage(
                    expected=None, present=0, valid=0,
                    reason=f"{name} coverage is unavailable for {row.get('date', 'day')}",
                ),
            })
        usage_summary[name] = aggregate_numeric_metric(
            metrics, aggregation_level="SELECTED_OVERVIEW_POPULATION", unit="tokens",
        )
    data = {
        "summary": {
            "day_count": len(ordered),
            "run_count": sum(int(row.get("prompt_count", 0)) for row in ordered if isinstance(row.get("prompt_count"), int)),
            "completed": sum(int(row.get("complete_count", 0)) for row in ordered if isinstance(row.get("complete_count"), int)),
            "blocked": sum(int(row.get("blocked_count", 0)) for row in ordered if isinstance(row.get("blocked_count"), int)),
            "failed": sum(int(row.get("failed_count", 0)) for row in ordered if isinstance(row.get("failed_count"), int)),
            "usage": usage_summary,
        },
        "rows": [dict(row) for row in ordered], "row_count": len(ordered),
    }
    return _envelope(
        locale=locale, selection=selection, data={"overview": data},
        references=[str(row.get("date")) for row in ordered if row.get("date")],
        displayed_population=len(ordered), full_population=len(ordered), export_complete=True,
        contract_version=_contract_version(ordered),
        source_as_of=source_as_of, source_reference=source_reference,
    )


def detail_model(
    *, project_id: str, execution_date: str, detail: Mapping[str, object],
    scope: str, run_id: str | None, locale: str,
    source_as_of: str | None = None, source_reference: str | None = None,
) -> dict[str, object]:
    runs = [row for row in detail.get("runs", []) if isinstance(row, Mapping)]
    selected = next((row for row in runs if row.get("run_id") == run_id), None)
    if scope in {"EP_RUN_ATTEMPT", "EXECUTION_CHAIN"} and selected is None:
        raise ValueError("TELEMETRY_EXPORT_RUN_NOT_FOUND")
    if scope == "EP_RUN_ATTEMPT":
        data: dict[str, object] = {"attempt": _safe_run(selected or {})}
        references = [str(run_id)]
        displayed_population = full_population = 1
        export_complete = True
    elif scope == "EXECUTION_CHAIN":
        snapshot = (selected or {}).get("telemetry_snapshot", {})
        chain = snapshot.get("chain", {}) if isinstance(snapshot, Mapping) else {}
        attempts_by_selected = detail.get("chain_attempts", {})
        raw_attempts = (
            attempts_by_selected.get(str(run_id), [])
            if isinstance(attempts_by_selected, Mapping) else []
        )
        chain_attempts = [
            _safe_run(row) for row in raw_attempts if isinstance(row, Mapping)
        ] if isinstance(raw_attempts, list) else []
        data = {
            "selected_attempt_reference": str(run_id),
            "selected_attempt": _safe_run(selected or {}),
            "chain": _safe_chain(chain),
            "chain_attempts": chain_attempts,
        }
        references = [str(row.get("run_id")) for row in chain.get("runs", []) if isinstance(row, Mapping)] if isinstance(chain, Mapping) else [str(run_id)]
        chain_runs = chain.get("runs", []) if isinstance(chain, Mapping) else []
        full_population = len(chain_runs) if isinstance(chain_runs, list) else 0
        displayed_population = len(chain_attempts)
        reasons = chain.get("reasons", []) if isinstance(chain, Mapping) else []
        expected_ids = {
            str(row.get("run_id")) for row in chain_runs if isinstance(row, Mapping) and row.get("run_id")
        } if isinstance(chain_runs, list) else set()
        delivered_ids = {
            str(row.get("run_id")) for row in chain_attempts if row.get("run_id")
        }
        export_complete = expected_ids == delivered_ids and not any(
            isinstance(reason, str) and reason.startswith("chain-limit:")
            for reason in (reasons if isinstance(reasons, list) else [])
        )
    else:
        scope = "UTC_DAY_DETAIL"
        data = {"day_detail": _safe_day_detail(detail)}
        references = [str(value) for value in detail.get("source_snapshot_references", []) if isinstance(value, str)]
        matching = detail.get("matching_run_count")
        returned = detail.get("returned_run_count")
        displayed_population = int(returned) if isinstance(returned, int) else len(runs)
        full_population = int(matching) if isinstance(matching, int) else len(runs)
        export_complete = not bool(detail.get("runs_truncated"))
    selection = {
        "project_id": project_id, "scope": scope, "date": execution_date,
        "timezone": str(detail.get("timezone") or "UTC"), "run_id": run_id,
        "filters": {"selected_utc_date": execution_date},
    }
    return _envelope(
        locale=locale, selection=selection, data=data, references=references,
        displayed_population=displayed_population,
        full_population=full_population,
        export_complete=export_complete,
        contract_version=str(detail.get("contract_version") or "UNAVAILABLE"),
        source_as_of=source_as_of, source_reference=source_reference,
    )


def serialize_json(model: Mapping[str, object]) -> bytes:
    return (json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def serialize_markdown(model: Mapping[str, object]) -> bytes:
    locale = str(model.get("locale") or "en")
    labels = _LABELS.get(locale, _LABELS["en"])
    selection = model.get("selection", {})
    data = model.get("data", {})
    title = labels["overview"] if isinstance(data, Mapping) and "overview" in data else labels["detail"]
    lines = [f"# {_md(title)}", "", f"## {_md(labels['selection'])}", "", _table(
        [labels["field"], labels["value"]],
        [[key, _display(value, labels)] for key, value in selection.items()] if isinstance(selection, Mapping) else [],
    ), "", f"Snapshot: `{_md(model.get('snapshot_id'))}`  ", f"Source as-of: `{_md(model.get('source_as_of') or model.get('as_of'))}`  ", f"Downloaded at: `{_md(model.get('downloaded_at'))}`  ", f"Contract: `{_md(model.get('contract_version'))}`  ", f"Export schema: `{_md(model.get('export_schema_version'))}`", ""]
    completeness = model.get("completeness", {})
    lines += [f"## {_md(labels['coverage'])}", "", _table(
        [labels["field"], labels["value"]],
        [[key, _display(value, labels)] for key, value in completeness.items()] if isinstance(completeness, Mapping) else [],
    ), ""]
    if isinstance(data, Mapping) and isinstance(data.get("overview"), Mapping):
        summary = data["overview"].get("summary", {})
        if isinstance(summary, Mapping):
            lines += [f"## {_md(labels['summary'])}", "", _table(
                [labels["field"], labels["value"]],
                [[key, _display(value, labels)] for key, value in summary.items()],
            ), ""]
        rows = data["overview"].get("rows", [])
        columns = sorted({str(key) for row in rows if isinstance(row, Mapping) for key in row})
        lines += [f"## {_md(labels['runs'])}", "", _mapping_table(columns, rows, labels), ""]
    elif isinstance(data, Mapping):
        chain_attempts = data.get("chain_attempts")
        detail = data.get("day_detail") or data.get("attempt") or (
            {} if isinstance(chain_attempts, list) else data.get("selected_attempt")
        ) or {}
        if isinstance(detail, Mapping) and detail:
            lines += _markdown_detail(detail, labels)
        chain = data.get("chain")
        if isinstance(chain, Mapping):
            lines += [f"## {_md(labels['chain'])}", "", _table(
                [labels["field"], labels["value"]],
                [[key, _display(value, labels)] for key, value in chain.items() if key != "runs"],
            ), ""]
            chain_rows = chain.get("runs", [])
            if isinstance(chain_rows, list) and chain_rows:
                columns = sorted({str(key) for row in chain_rows if isinstance(row, Mapping) for key in row})
                lines += [_mapping_table(columns, chain_rows, labels), ""]
        if isinstance(chain_attempts, list):
            for attempt in chain_attempts:
                if not isinstance(attempt, Mapping):
                    continue
                lines += [
                    f"## {_md(labels['attempt'])}: `{_md(attempt.get('run_id'))}`", "",
                    *_markdown_detail(attempt, labels),
                ]
    limitations = _limitations(model)
    if limitations:
        lines += [f"## {_md(labels['limitations'])}", "", *[f"- {_md(reason)}" for reason in limitations], ""]
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _envelope(*, locale: str, selection: Mapping[str, object], data: Mapping[str, object], references: Sequence[str], displayed_population: int, full_population: int, export_complete: bool, contract_version: str, source_as_of: str | None = None, source_reference: str | None = None) -> dict[str, object]:
    locale = locale if locale in SUPPORTED_LOCALES else "en"
    as_of = source_as_of or datetime.now(timezone.utc).isoformat()
    core = {
        "contract_version": contract_version, "selection": selection, "data": data,
        "source_snapshot_references": list(dict.fromkeys(references)),
        "source": {
            "kind": "CENTRAL_READ_TRANSACTION",
            "as_of": as_of,
            "reference": source_reference,
        },
    }
    digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "contract_version": contract_version,
        "snapshot_id": f"sha256:{digest}", "as_of": as_of, "source_as_of": as_of,
        "downloaded_at": None, "source": core["source"], "locale": locale,
        "selection": dict(selection), "source_snapshot_references": core["source_snapshot_references"],
        "completeness": {
            "export": "COMPLETE" if export_complete and displayed_population == full_population else "PARTIAL",
            "displayed_population": displayed_population, "full_population": full_population,
            "metrics_may_be_partial": True,
        },
        "data": data,
    }


def _safe_day_detail(detail: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in detail.items()
        if key not in {"runs", "phases"}
    } | {"runs": [_safe_run(row) for row in detail.get("runs", []) if isinstance(row, Mapping)]}


def _safe_run(run: Mapping[str, object]) -> dict[str, object]:
    result = {key: value for key, value in run.items() if key != "telemetry_snapshot"}
    snapshot = run.get("telemetry_snapshot", {})
    if isinstance(snapshot, Mapping):
        attempt = snapshot.get("attempt", {})
        result["telemetry_snapshot"] = {
            "contract_version": snapshot.get("contract_version"),
            "source_snapshot_reference": snapshot.get("source_snapshot_reference"),
            "presentation_semantics": snapshot.get("presentation_semantics"),
            "attempt": _safe_attempt(attempt) if isinstance(attempt, Mapping) else {},
            "chain": _safe_chain(snapshot.get("chain", {})),
        }
    return result


def _safe_attempt(attempt: Mapping[str, object]) -> dict[str, object]:
    timing = attempt.get("timing", {})
    usage = attempt.get("usage", {})
    safe_timing = dict(timing) if isinstance(timing, Mapping) else {}
    if isinstance(safe_timing.get("timeline"), list):
        allowed = {"phase_id", "phase_name", "phase_category", "parent_phase_id", "attempt", "ordinal", "started_at", "completed_at", "duration_ms", "outcome", "measurement_basis", "relative_start_ms", "relative_end_ms"}
        safe_timing["timeline"] = [
            {key: value for key, value in row.items() if key in allowed}
            for row in safe_timing["timeline"] if isinstance(row, Mapping)
        ]
    safe_usage = dict(usage) if isinstance(usage, Mapping) else {}
    safe_usage.pop("context_churn", None)
    return {"scope": attempt.get("scope"), "run_id": attempt.get("run_id"), "timing": safe_timing, "usage": safe_usage}


def _safe_chain(chain: object) -> dict[str, object]:
    if not isinstance(chain, Mapping):
        return {}
    allowed = {
        "contract_version", "scope", "selected_run_id", "root_run_id",
        "coverage", "reason", "reasons", "mission_scope_label", "mission_id",
        "engineering_action_id", "attempt_count", "original_attempt_count",
        "retry_count", "resume_count", "first_started_at", "last_completed_at",
        "elapsed_ms", "processing_time_ms", "covered_elapsed_ms",
        "inter_attempt_gap_ms", "provider_invocation_count",
        "duplicate_invocation_count", "usage_metrics", "cache_ratio_percent",
        "cache_ratio_population", "outside_selected_window_count", "runs",
    }
    return {key: value for key, value in chain.items() if key in allowed}


def _markdown_detail(detail: Mapping[str, object], labels: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    scalar_detail = [
        [key, _display(value, labels)] for key, value in detail.items()
        if not isinstance(value, (Mapping, list))
    ]
    if scalar_detail:
        lines += [f"## {_md(labels['summary'])}", "", _table(
            [labels["field"], labels["value"]], scalar_detail,
        ), ""]
    summary = detail.get("summary")
    if isinstance(summary, Mapping):
        lines += [f"## {_md(labels['summary'])}", "", _table([labels["field"], labels["value"]], [[key, _display(value, labels)] for key, value in summary.items()]), ""]
    phases = detail.get("inclusive_phases") or detail.get("phase_aggregates")
    if isinstance(phases, list):
        lines += [f"## {_md(labels['inclusive'])}", "", labels["inclusive_note"], "", _records_table(phases, labels), ""]
    exclusive = detail.get("exclusive_distribution")
    if isinstance(exclusive, list):
        lines += [f"## {_md(labels['exclusive'])}", "", _records_table(exclusive, labels), ""]
    lines += _markdown_bottlenecks(detail.get("bottlenecks"), labels)
    snapshots = [detail.get("telemetry_snapshot")] if isinstance(detail.get("telemetry_snapshot"), Mapping) else []
    snapshots += [run.get("telemetry_snapshot") for run in detail.get("runs", []) if isinstance(run, Mapping) and isinstance(run.get("telemetry_snapshot"), Mapping)] if isinstance(detail.get("runs"), list) else []
    for snapshot in snapshots:
        attempt = snapshot.get("attempt", {}) if isinstance(snapshot, Mapping) else {}
        usage = attempt.get("usage", {}) if isinstance(attempt, Mapping) else {}
        timing = attempt.get("timing", {}) if isinstance(attempt, Mapping) else {}
        if isinstance(timing, Mapping):
            timing_summary = [
                [key, _display(value, labels)] for key, value in timing.items()
                if key not in {"timeline", "inclusive_phase_rows", "phase_aggregates", "exclusive_distribution"}
                and not isinstance(value, (Mapping, list))
            ]
            lines += [f"## {_md(labels['exclusive'])}", "", _table(
                [labels["field"], labels["value"]], timing_summary,
            ), ""]
            phase_rows = timing.get("inclusive_phase_rows") or timing.get("phase_aggregates")
            if isinstance(phase_rows, list):
                lines += [f"## {_md(labels['inclusive'])}", "", labels["inclusive_note"], "", _records_table(phase_rows, labels), ""]
            exclusive_rows = timing.get("exclusive_distribution")
            if isinstance(exclusive_rows, list):
                lines += [f"## {_md(labels['exclusive'])}", "", _records_table(exclusive_rows, labels), ""]
            lines += _markdown_bottlenecks(timing.get("bottlenecks"), labels)
        metrics = usage.get("metrics", {}) if isinstance(usage, Mapping) else {}
        if isinstance(metrics, Mapping):
            lines += [f"## {_md(labels['coverage'])}", "", _table(
                [labels["field"], labels["value"]],
                [[key, _display(value, labels)] for key, value in metrics.items()],
            ), ""]
        invocations = usage.get("invocations", []) if isinstance(usage, Mapping) else []
        timeline = timing.get("timeline", []) if isinstance(timing, Mapping) else []
        if isinstance(invocations, list):
            lines += [f"## {_md(labels['invocations'])}", "", _records_table(invocations, labels), ""]
        if isinstance(timeline, list):
            lines += [f"## {_md(labels['timeline'])}", "", _records_table(timeline, labels), ""]
    return lines


def _markdown_bottlenecks(value: object, labels: Mapping[str, str]) -> list[str]:
    if not isinstance(value, Mapping) or not value:
        return []
    rows = [[key, _display(observation, labels)] for key, observation in value.items()]
    return [
        f"## {_md(labels['bottlenecks'])}", "",
        _table([labels["field"], labels["value"]], rows), "",
    ]


def _limitations(value: object) -> list[str]:
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if key in {"missing_reason", "reason"} and isinstance(nested, str) and nested.strip():
                    found.append(nested.strip())
                elif key == "reasons" and isinstance(nested, list):
                    found.extend(reason.strip() for reason in nested if isinstance(reason, str) and reason.strip())
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return list(dict.fromkeys(found))


def _records_table(rows: Sequence[object], labels: Mapping[str, str]) -> str:
    mappings = [row for row in rows if isinstance(row, Mapping)]
    columns = sorted({str(key) for row in mappings for key in row if not isinstance(row.get(key), (dict, list))})
    return _mapping_table(columns, mappings, labels) if columns else labels["unavailable"]


def _mapping_table(columns: Sequence[str], rows: Sequence[object], labels: Mapping[str, str]) -> str:
    return _table(list(columns), [[_display(row.get(column), labels) for column in columns] for row in rows if isinstance(row, Mapping)])


def _table(headings: Sequence[object], rows: Sequence[Sequence[object]]) -> str:
    return "\n".join([
        "| " + " | ".join(_md(value) for value in headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
        *("| " + " | ".join(_md(value) for value in row) + " |" for row in rows),
    ])


def _md(value: object) -> str:
    return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _display(value: object, labels: Mapping[str, str]) -> str:
    if value is None:
        return labels["unavailable"]
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return str(value)


def _sort_value(row: Mapping[str, object], key: str) -> tuple[int, object]:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return (1, "")
    return (0, value if isinstance(value, (int, float)) else str(value))


def _contract_version(rows: Sequence[Mapping[str, object]]) -> str:
    values = {str(row.get("contract_version")) for row in rows if row.get("contract_version")}
    return next(iter(values)) if len(values) == 1 else "MIXED" if values else "UNAVAILABLE"
