# EP forensic delta exporter

## Purpose

The Engineering Platform forensic delta exporter compares a baseline SQLite
store with a candidate SQLite store and emits a deterministic, persisted-data
evidence report. It is designed for bounded recovery investigation; it does
not make a provenance verdict and cannot perform a recovery action.

## Read-only guarantee

Both inputs are opened with SQLite `mode=ro`. The exporter does not use the
Engineering Platform storage opener, migrations, checkpointing or activation
code. It fingerprints each database (and any present WAL/SHM sidecar) before
and after inspection and fails if either fingerprint differs. The optional
output path is the only file the command may write.

## Usage

```sh
python3 -m tools.engineering.central_store_migration forensic-delta \
  --baseline /path/to/legacy.db \
  --candidate /path/to/central.db \
  --migration-id 41feb31e-2e25-42c4-bca1-bbfc97dde6f4 \
  --json --output forensic-delta.json
```

`--strict` returns a non-zero status when any table has no deterministic row
identity. Default mode returns the partial report and marks those tables
`KEY_UNRESOLVED`.

## Report version 1.0

The canonical JSON contains the migration identifier, input fingerprints,
read-only verification, schema differences, summary totals, per-table deltas,
canonical graph references and a `report_digest`. JSON keys, table order, row
keys, changes and graph edges are sorted; there is deliberately no generated
timestamp. Re-running on identical inputs produces byte-identical output.

Rows use a declared primary key first, then a non-partial UNIQUE index, then a
small registered EP composite key. No row-order identity is guessed. Keyed
tables classify individual rows as `ADDED`, `REMOVED`, `MODIFIED` or unchanged;
modified evidence names only changed fields and includes safe values or
digests.

## Redaction

Sensitive columns (including credentials, tokens, passwords, bearer material,
prompts, history and verifier data) never appear as plaintext. Binary data is
represented only by type, size and SHA-256 digest. Safe identity, status,
scope and timestamp fields may appear as normalized values; other fields are
represented by a digest. This permits comparison without making a report a
credential or prompt export.

## Non-goals

Version 1 extracts persisted facts only. It does not identify a source-code
writer, infer production versus test provenance, create an attestation,
change authority, clean a database, or execute any recovery step.
