# EP CENTRAL operational reset V1

**Owning product:** Engineering Platform.  **Profile:**
`EP_CENTRAL_OPERATIONAL_HISTORY_V1`.  **Server schema:** 68.  **Candidate
release:** 2.3.81.

This is a bounded local maintenance route for removing EP operational history
while retaining installation identity, project and repository attachment,
consumer authority, credentials, security audit and configuration. It is not a
factory reset, a generic database administration surface, or a Forge Mission.
It never opens Forge storage and has no remote HTTP route or Console button.

The installed command is `engineering-platform-maintenance`. It is separate
from `engineering-platform-server` because a stopped Server is a precondition,
and separate from the submission CLI because maintenance authorization is the
local installation owner's authority rather than a consumer bearer. Every
mutating command acquires the existing `operational-installation.lock` and is
also fenced by durable CENTRAL maintenance state.

## State and authorization contract

`preview` opens the selected `epdata.sqlite` with SQLite URI `mode=ro`. It does
not call `server.initialize`, create a maintenance row, migrate schema, run a
Mission, issue a grant, checkpoint WAL, vacuum, or write an artifact. A schema
other than 68 is a reported blocker rather than an implicit upgrade.

The plan binds the exact instance ID, resolved data/database paths, filesystem
database identity, schema, profile, dataset generation, complete logical data
revision, preserved-binding digest, table/record classification, external
file inventory, integrity findings and effect set. Its canonical SHA-256
digest is the approval identity. `prepare` re-reads that plan while acquiring
the installation lock, derives the real local actor as `uid:<uid>:<account>`,
persists `PREPARING`, and thereby activates database triggers that reject
normal INSERT/UPDATE/DELETE entry points after any process restart.

The durable states are:

```text
PREPARING -> AUTHORIZED -> ARTIFACTS_ARCHIVING -> ARTIFACTS_ARCHIVED
          -> DB_APPLIED -> VERIFIED -> COMPLETED
```

`FAILED` is deliberately still writer-blocking. Only `finish` may enter
`COMPLETED`, and only from `VERIFIED`. A changed operation request or plan
digest is rejected; there is no `--force`. Known foreign-key findings may be
approved only by repeating every exact `fk:<child>:<rowid>:<parent>:<index>`
identity emitted by that plan. Findings touching preserved or unknown state
remain blockers.

`apply` accepts only `AUTHORIZED`, `ARTIFACTS_ARCHIVING` or
`ARTIFACTS_ARCHIVED`; its safe idempotent readback states are `DB_APPLIED` and
`VERIFIED`. It rejects `ABORTED`, `COMPLETED`, `FAILED` and every unknown state.
Every transition is compare-and-set and is independently enforced by a
maintenance-owner state trigger. Reset-operation bindings and tombstones are
immutable to ordinary CENTRAL connections; a connection-local owning
capability is present only on the maintenance service's connections. Preview
requires the complete schema-derived normal-writer trigger set plus every
maintenance-table guard before it can be allowed.

Before the first external effect, `abort` may move `PREPARING` or `AUTHORIZED`
to terminal `ABORTED` and release the fence. It is rejected after artifact
archiving or database apply starts. A failed-backup resume accepts only the
originally authorized backup-root binding; changing that binding requires
aborting safely and preparing a newly previewed operation.

The CLI emits `operational-reset-v1` JSON with stable top-level product,
command, operation, state, allowed, target, profile, generation, plan/revision,
backup, counts, blockers, integrity and preserved-binding fields. Protected
credential verifiers and row contents never appear in that receipt.
Argument, storage and unexpected command failures use the same top-level
shape and a stable `error_code`; exception text, raw verifier material and
operator-selected paths are not copied into error receipts.

## Schema-owned classification

The implementation enumerates every schema-68 application table; an extra or
missing application table blocks prepare. SQLite indexes, triggers and views
are separately inventoried with definition digests and a preserve effect;
SQLite-internal objects are not classified as application data to purge.

`INSTALLATION_AND_CONFIGURATION` is preserved:

- `engineering_schema_migrations`, `ep_installations`,
  `ep_project_registrations`, `ep_repository_registrations`,
  `ep_agent_registrations`, `ep_agent_repository_attachments`,
  `ep_local_repository_bindings`, `ep_external_producer_bindings`, and
  `execution_migration_provenance`;
- configuration records in `engineering_metadata` and records in
  `execution_projections` whose explicit classification is `CONFIGURATION`.

`SECURITY_AND_AUTHORITY_LEDGER` is preserved:

- `ep_agent_pairing_codes`, `ep_consumer_credentials`,
  `ep_consumer_registrations`,
  `ep_consumer_credential_recovery_operations`, `ep_control_provenance`,
  `ep_external_producer_binding_audit`, and `ep_operator_capabilities`.

This preserves verifier bytes in their owning database but never copies a
Keychain item, bearer, provider login or credential into a receipt or portable
export. Revocations, recovery decisions, operator capabilities and external
producer bindings are not reset into reusable authority.

`OPERATIONAL_HISTORY` is purged from the active dataset:

- CENTRAL submissions/events/prompt indexes, execution runs/leases,
  lifecycle dispatches, queue dispositions, receipt provenance, Forge
  exchange/action/planning envelopes, execution-host/technical/reconciliation
  evidence;
- retained-host transactions, submissions/attempts/links, leases/events,
  prompt history/chat, receipts/artifacts, admission/readiness/reconciliation,
  phase timing, validation/qualification, governance/assurance, provider
  invocation/recovery, Dependabot admission and terminal telemetry outbox;
- legacy `engineering_artifacts` and every other table enumerated in
  `OPERATIONAL_HISTORY` in `central_operational_reset.py`.

`DERIVED_CACHE_OR_PROJECTION` is purged:

- daily/execution activity and provider-usage telemetry, component logs,
  `engineering_status`, non-configuration `execution_projections`, and the
  two operational metadata keys
  `central_database.maintenance_last_attempt_at` and
  `ep.provider_capacity_history.v1`.

`MAINTENANCE_AUDIT` is retained and excluded from the relevant-source revision:

- `ep_operational_reset_operations`, `ep_operational_dataset_state`, and
  `ep_operational_identity_tombstones`.

Tombstones retain one-way identities for deleted submissions, runs, provider
invocations and used idempotency keys. The original prompt is not retained in
the tombstone. Reusing the same idempotency request is rejected as
`IDEMPOTENCY_RETIRED`; using different bytes with that key is
`IDEMPOTENCY_CONFLICT`. Dataset generation advances once for one applied
operation; installation/runtime identity does not change. Integer SQLite
rowids are internal storage identities, not external run/submission
namespaces. External textual identities are random/immutable and tombstoned.

## External storage

The active `artifacts/`, `file-inbox/` and
`runtime/central-data-imports/` trees are hashed into the plan and protected
backup, moved beneath
`operational-reset-archive/<operation-id>/`, and recreated empty. This prevents
an Inbox watcher or artifact lookup from immediately re-projecting the old
dataset. A symlink or non-regular entry fails closed.

The exact known installation/configuration/runtime/recovery names are
preserved. In particular, `central.sqlite` and the bounded
`epdata.sqlite.pre-X.Y.Z.backup` form are
`FORENSIC_OR_RECOVERY`; `.forge-ep-consumer-recovery.lock` and the installation
lock are `SYSTEM_RUNTIME_CONTROL`. `server.json`, `runtime-identity.json`,
`runtime/`, update `operations/`, `migration/`, `recovery/` and existing
`backups/` stay outside the purge. A new top-level name is
`UNKNOWN_OR_UNSUPPORTED` and blocks prepare. Source repositories, canonical
documents, Git worktrees, user workspaces and other installations are never
targets.

Known preserved directories are also inventoried recursively. Only exact
product-owned runtime records, installer journal/staging shapes, migration
receipts, legacy backups and reset-archive layouts receive a preserve
classification. An arbitrary file hidden inside `runtime/`, `operations/`,
`recovery/`, `migration/`, `backups/` or a reset archive is unsupported and
blocks prepare without being moved or deleted. A pending CENTRAL import is an
explicit active-ingest blocker.

## Protected backup and recovery

`prepare` requires an operator-selected backup root outside the active data
root. It rejects links, unsafe permissions, insufficient free space and
unwritable destinations. SQLite's online backup API captures the complete
database, including committed WAL content, into `central.sqlite`; no raw main
file copy is used. The private operation directory and files are mode 0700 and
0600. Its manifest binds source instance/schema, operation/plan/revision,
database hash and row counts, exact included external files/hashes, integrity
and known FK findings, and excluded secret stores/runtime/source trees.

Verification reopens the backup in an isolated read-only connection, runs
`integrity_check`, compares every table count and re-hashes every protected
file. Known purge-local FK defects may remain faithfully present in the backup;
that is recorded, not called semantically clean.

Backup publication is staged in a new private sibling and atomically renamed
to the operation-ID destination only after full restore verification. Nested
source or destination symlinks and pre-existing destination bytes are never
followed or overwritten. A complete destination left by a crash is reusable
only when its operation, plan, database and every file verify exactly; partial
staging remains preserved for diagnosis while `resume` creates a fresh staging
snapshot for the same durable operation. The stored manifest-file digest is
rechecked by `apply`, `verify`, readback and finalization, not merely recomputed
from whichever manifest happens to be present.

After an interruption, use `status` and `resume` with the same operation ID
and plan digest. A partial external move is reconciled by exact planned hashes.
The database purge and generation change are one `BEGIN IMMEDIATE` transaction;
immutable-evidence and maintenance-block triggers are removed and recreated
inside that uncommitted transaction, so another connection never observes an
unfenced write window. A pre-commit crash leaves no partial DB purge. A
post-commit crash resumes at verification. Forward reconciliation is the
default.

`finish` does not trust an earlier `VERIFIED` receipt by itself. While the
installation lock and durable writer fence are still active it re-verifies the
protected backup, quick/FK checks, operational emptiness, dataset generation,
preserved bindings and all external ingest routes. It then durably binds an
operation- and generation-specific finish boundary and atomically renames the
complete active `artifacts/`, `file-inbox/` and
`runtime/central-data-imports/` roots into that protected boundary before it
creates empty roots for the new generation. A file that arrives after the last
empty scan through the old path is therefore preserved behind the boundary;
a writer holding an old directory descriptor also remains isolated in the
renamed inode. Unknown files are never deleted. The boundary manifest, file
hashes and digest are verified before `COMPLETED` and on later verification.

The boundary-path binding is persisted while the state remains `VERIFIED`
before the first filesystem rename. A crash during the three renames therefore
keeps the durable writer fence active. Re-running `finish` for the same
operation recognizes already-rotated roots, rotates only the remaining roots,
recovers its exact marker staging file and completes forward. A pre-existing
unbound boundary or a changed marker fails closed.

Do not automatically copy the backup over live CENTRAL: an old database could
undo later revocations, consumed authority or external effects. An exceptional
restore is a separately authorized, offline operation: keep writers stopped,
copy the protected backup to an isolated test root, verify manifest/hash/counts
and schema, establish how later security effects will be preserved, then use
the owning recovery/import route. This reset command deliberately has no
generic rollback switch.

## Chat-integrity defect and migration

Schema 67 declared
`execution_chat_messages.run_id -> prompt_execution_history.run_id`, while the
CENTRAL Console deliberately authorized a terminal chat through
`ep_parity_lifecycle_dispatches`/`ep_execution_runs` even when the legacy
prompt-history writer had not produced a row. The direct CENTRAL Console path
therefore wrote a schema-declared orphan whenever this supported no-prompt-index
state occurred. That owning defect shape is reproducible and could create the
observed shape.

Schema 68 changes the declared chat parent to canonical `ep_execution_runs`
and makes the owning Console writer enable and read back `foreign_keys=ON` on
the exact write connection, lock the write transaction, and verify the
canonical run before insert.
Reset-owned connections enforce foreign keys throughout maintenance. Migration
copies chat rows unchanged and refuses any row without that canonical run; it
creates no fictitious prompt parent and deletes no chat row. Correct chat
evidence whose legacy prompt index is absent is therefore preserved. The
currently observed production rows were confirmed read-only to have canonical
run and lifecycle parents, but their exact historical writer/provenance was
not retained.
Consequently:

```text
REPRODUCIBLE_OWNING_DEFECT = CONFIRMED
EXACT_HISTORICAL_ROW_CAUSE = ROOT_CAUSE_UNCONFIRMED
ORPHAN_PREVENTION = CANONICAL_PARENT_PLUS_FOREIGN_KEYS
ORPHAN_RESET_HANDLING = PREVIEW_CLASSIFY_EXACTLY; NO AUTOMATIC ROW DELETION
```

## Installed command recipe

Preview and status are non-destructive:

```bash
engineering-platform-maintenance preview \
  --data-root "/exact/Engineering Platform Server/data"

engineering-platform-maintenance status \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>"
```

The later clean-CENTRAL operation is explicitly destructive from `apply`
onward. Do not run these commands during installation or as part of an
ordinary Server start:

```bash
# 1. Stop/verify every writer, save PREVIEW_PLAN_DIGEST and every exact
#    review_required FK identity from the preview.
engineering-platform-maintenance prepare \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST" \
  --backup-root "/exact/protected/recovery/root" \
  --allow-operational-fk "fk:<child>:<rowid>:<parent>:<index>"

# 2. DESTRUCTIVE: archive active artifacts/Inbox and purge the approved DB set.
engineering-platform-maintenance apply \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST"

# 3. Prove physical/referential integrity, operational emptiness, backup and bindings.
engineering-platform-maintenance verify \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST"

# 4. Only after the coordinated Forge+EP verification authorizes normal writers.
engineering-platform-maintenance finish \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST"
```

If interrupted, run `status`, then:

```bash
engineering-platform-maintenance resume \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST" \
  --backup-root "/exact/protected/recovery/root"
```

`--backup-root` is needed during resume only if the durable state is still
`PREPARING`. The command never rotates credentials, changes historical
acceptance, creates a submission/run/Mission, or resumes writers by itself.

To safely end preparation before any reset effect has begun:

```bash
engineering-platform-maintenance abort \
  --data-root "/exact/Engineering Platform Server/data" \
  --operation-id "central-clean-<coordinator-reference>" \
  --plan-digest "$PREVIEW_PLAN_DIGEST"
```

## Joint qualification coordinator

`tools/qualification/operational_reset_coordinator.py` is the bounded operator
harness for the later coordinated Forge and EP maintenance window. It is not a
third product service and it does not open either database. It records only a
secret-free joint progress receipt and invokes the two installed, owning CLIs
as subprocesses. Its receipt directory is mode `0700`, its receipt and lock are
mode `0600`, updates are atomic and fsynced, and symlinks or a concurrent
coordinator process are rejected.

Choose a new joint reference and two distinct product operation IDs. Bind the
exact installed CLI files, target roots and protected EP backup root during the
initial preview:

```bash
COORDINATOR_ID="central-clean-coordinator-<reference>"
COORDINATOR_RECEIPTS="/exact/protected/coordinator-receipts"
FORGE_OPERATION_ID="forge-clean-<reference>"
EP_OPERATION_ID="ep-clean-<reference>"

python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" \
  --coordinator-id "$COORDINATOR_ID" \
  preview \
  --forge-cli "/exact/installed/bin/forge" \
  --forge-data-root "/exact/Forge/data" \
  --forge-operation-id "$FORGE_OPERATION_ID" \
  --ep-cli "/exact/installed/bin/engineering-platform-maintenance" \
  --ep-data-root "/exact/Engineering Platform Server/data" \
  --ep-operation-id "$EP_OPERATION_ID" \
  --ep-backup-root "/exact/protected/ep-recovery"
```

`preview` is read-only for both product datasets. If either preview reports an
explicitly reviewable operational foreign-key finding, retain that receipt as
read-only evidence, choose a new coordinator reference before any `prepare`,
and repeat the initial preview with the exact finding identity bound as
`--forge-fk-acknowledgement <identity>` or
`--ep-fk-acknowledgement <identity>`. The acknowledgement is used only by the
later owning `prepare`; it is not a general integrity bypass.

The remaining commands load the already-bound configuration from the receipt:

```bash
# MUTATING: both products enter durable maintenance and make verified backups.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" prepare

# Read-only under both writer fences: prove both exact plans are still current.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" revalidate

# DESTRUCTIVE: invoke one owning reset at a time. Order is an operator choice.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" \
  apply --product forge
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" \
  apply --product engineering-platform

# Read-only proofs. Authorization is local to the joint receipt.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" verify
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" authorize-resume

# MUTATING: owning products may leave maintenance only after both verified.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" \
  finish --product forge
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" \
  finish --product engineering-platform
```

These destructive commands are qualified only against isolated fixture stores
in this delivery; do not run them against CENTRAL as part of installation. The
joint receipt reaches `COMPLETE` only after both owning verifications, explicit
resume authorization and both owning finishes.

Immediately before every individual `finish`, the harness calls both owning
`verify` commands again and requires each product to report `VERIFIED` or
`COMPLETED`. This is a two-phase pre-finish readiness check, not a distributed
transaction: the owning finishes remain sequential. If the first product has
already reached `COMPLETED` and the second finish fails, the receipt records
`RECONCILIATION_REQUIRED`; reconciliation truthfully returns the pair
`COMPLETED`/`VERIFIED`, keeps the unfinished product fenced, and requires an
explicit retry of that same second finish. It never rolls the first product
back or automatically resumes the second.

After any interruption or owning failure, inspect without creating a new
operation and reconcile the same product operation IDs:

```bash
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" status
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" reconcile

# Use only when the receipt proves PLANS_REVALIDATED and durably records that
# this exact product's apply was admitted before the process was interrupted.
python3 tools/qualification/operational_reset_coordinator.py \
  --receipt-root "$COORDINATOR_RECEIPTS" --coordinator-id "$COORDINATOR_ID" \
  resume --product engineering-platform
```

Once an owning apply may have started, every command failure is
`RECONCILIATION_REQUIRED`: both products stay in maintenance, there is no
automatic finish and no automatic destructive rollback. The harness never
calls a peer CLI from product code, imports Forge or EP, creates a Mission,
provider invocation or submission, or treats delayed callbacks as new work.
`resume` is fail-closed from `BOTH_PREVIEWED`, `BACKUPS_VERIFIED`, and from
`PLANS_REVALIDATED` until the selected product has its durable apply-admission
milestone. A subprocess crash after apply can be reconciled because that
milestone was fsynced before the owning command ran.

The repository document `missions/MISSION-0003.md`, a future test label and an
operational Forge Mission are distinct namespaces. EP reset readback names its
own runtime run/submission namespace and does not make a Mission-ID allocation
decision.

## Delivery status

At source-candidate creation this slice is `IMPLEMENTED` and locally fixture
qualified, while protected PR review, hosted full gates, release publication,
artifact-byte qualification and installed live preview remain separate
evidence. Installation may activate schema 68 and the chat relationship repair
but must not automatically prepare/apply a reset or delete historical rows.
