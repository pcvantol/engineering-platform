# Telemetriedetail

## Selectie

| Veld | Waarde |
| --- | --- |
| project_id | synthetic-project |
| scope | EP_RUN_ATTEMPT |
| date | 2026-09-17 |
| timezone | UTC |
| run_id | synthetic-run-1 |

Snapshot: `sha256:synthetic-detail-example`
Source as-of: `2026-09-17T10:00:00+00:00`
Downloaded at: `2026-09-17T10:00:05+00:00`
Contract: `telemetry-contract@2.2`
Exportschema: `telemetry-export@1.1`

## Samenvatting

| Veld | Waarde | Dekking |
| --- | ---: | --- |
| Doorlooptijd (monotoon) | 10000 ms | COMPLETE |
| Exclusieve wall-clock-envelope | 10000 ms | COMPLETE |
| Waargenomen input | 1250 tokens | COMPLETE (1/1) |
| Cached input | 1000 tokens | COMPLETE (1/1) |
| Uncached input | 250 tokens | COMPLETE (1/1) |
| Waargenomen output | 75 tokens | COMPLETE (1/1) |
| Cacheratio | 80,0% | COMPLETE (1/1) |

## Exclusieve doorlooptijdverdeling

| Categorie | Duur | Aandeel |
| --- | ---: | ---: |
| PROVIDER_EXECUTION | 6000 ms | 60,0% |
| UNASSIGNED | 4000 ms | 40,0% |

Meetbasis: `WALL_CLOCK_INTERVAL_ENVELOPE`. De verdeling sluit exact op 10000 ms.

## Providerinvocations

| Invocation | Rol | Model (herkomst) | Duur | Input | Cached | Uncached | Output | Dekking | Timingkoppeling |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| synthetic-invocation-1 | IMPLEMENTATION | observed-model (AUTHORITATIVE) | 6000 ms | 1250 | 1000 | 250 | 75 | COMPLETE | UNAVAILABLE |

## Tijdlijn

| Span | Parent | Fase | Start | Einde | Duur | Meetbasis | Uitkomst |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| provider | total | PROVIDER_EXECUTION | 0 ms | 6000 ms | 6000 ms | MONOTONIC | COMPLETE |

Ontbrekende timingkoppeling is expliciet `UNAVAILABLE`; zij wordt niet uit namen of tijdstippen afgeleid.
