# EP forensic provenance attribution

## Purpose

The Engineering Platform forensic provenance attribution tool consumes a
verified `forensic-delta.json` report and repository evidence to produce a
deterministic, evidence-only `forensic-attribution.json`. It is generic
EP-owned audit infrastructure for incident analysis, migration verification,
writer attribution and recovery planning. It moves with Engineering Platform
during Phase 3 extraction.

It neither opens nor queries a database. It cannot reconcile state, select an
authority, create an attestation, or authorize recovery.

## Input binding and usage

The input report must have exporter report version `1.0`, migration ID,
baseline and candidate fingerprints, and a valid embedded `report_digest`.
The required digest is supplied explicitly; a mismatch fails closed before
attribution. The result binds that digest, the source report version and
migration ID, plus the repository revision used to build its writer index.

```sh
python3 -m tools.engineering.central_store_migration forensic-attribution \
  --repo /path/to/djconnect \
  --report forensic-delta.json \
  --expected-report-digest <sha256> \
  --json --output forensic-attribution.json
```

`--evidence-bundle` may supply immutable, source-named component bindings when
an ingress envelope, operator receipt or Forge producer record proves facts not
represented in the safe delta fields. A binding can establish ancestry and,
only when its source explicitly proves it, writer origin and state semantics.
It never infers a writer from ancestry: a production-ancestry component can
still contain a proven test-harness write.

## Independent fields and evidence rule

Each changed row records all of the following independently:

- `ancestry_origin`: `PRODUCTION`, `TEST_HARNESS`, `OPERATOR`, `FORGE`, or
  `UNKNOWN`, describing the logical graph to which a row belongs.
- `writer_origin`: `PRODUCTION_RUNTIME`, `TEST_HARNESS`,
  `OPERATOR_CONTROL`, `FORGE_CONTROL`, `MAINTENANCE`, or `UNKNOWN`, describing
  the actor that wrote the changed row.
- `state_semantics`: immutable business state, execution evidence, control,
  configuration, mutable projection, component log, retention state,
  test-only structure, or unknown.
- `evidence_status`: exactly `PROVEN` or `UNRESOLVED`.

Evidence includes a rule ID, evidence type, source path/test and deterministic
signals. Exact committed fixture literals, registered fixture lifecycle rules,
canonical human-ingress structure, immutable envelopes, source writer APIs and
known maintenance semantics may establish proof. A timestamp, test-like name,
shared writer API, or production `run_id` alone never establishes writer
origin. Thus a production-ancestry component can correctly contain a
`TEST_HARNESS` write.

## Components, index and determinism

Rows are grouped only by a direct canonical submission, run or authority-scope
reference. A proven row never automatically proves an unrelated row. The
report contains a deterministic writer-candidate index (table, API and
production/test/operator/maintenance callers), but that index is source
evidence rather than a blanket classification rule.

Candidate-only schemas are emitted separately as `schema_findings`; they are
not invented as row deltas. The registered `backup_probe` structure is a
proven `TEST_ONLY_STRUCTURE` when it appears as a candidate-only schema.

Rows, components, evidence and count summaries are canonically sorted. The
attribution report has version `1.0` and its `report_digest` excludes itself;
the same report, repository revision and evidence bundle produce byte-stable
JSON and digest.

## Non-goals

The result is forensic evidence only. It contains no recovery approval,
rollback instruction, attestation approval or provenance guess. Unresolved
state remains unresolved for a separately governed recovery decision.
