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
preserved bindings and all external ingest routes. A delayed Inbox/import file
therefore leaves the operation in maintenance instead of being admitted after
the fence is released.

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
