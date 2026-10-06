# Managed candidate adoption and first publication V1

This contract implements `MPR-ADOPT` and `MPR-PUBREC` under assignment
`L2-EP-MANAGED-PUBLICATION-RECOVERY-V1-20261006`. It concerns an existing
engineering candidate, not installation-artifact or legacy reconciliation
adoption. Finalization, protected merge and release retain their existing
authority boundaries.

## Explicit local owner entrypoint

The installed Execution Host accepts `--adopt-candidate <selection.json>` with
`--owner-authorized`, `--run-id` and the explicitly selected
`--central-database`. The selection is a typed object, never prompt text or a
producer-supplied authorization boolean. The existing local owner CLI authority
authorizes this bounded continuation. HTTP producer envelopes do not acquire
adoption authority by including similarly named fields.

The read-only command below prints an inspectable selection for the current
checkout. It does not persist an adoption or grant authority:

```sh
python -m engineering_platform.managed_adoption \
  --project-id PROJECT --repository-id REPOSITORY --run-id RUN
```

Selection V1 requires exactly these fields:

| Field | Meaning |
| --- | --- |
| `version` | `1.0` |
| `project_id`, `repository_id` | Active CENTRAL project and its BOUND local repository |
| `repository` | Exact trusted GitHub owner/repository identity |
| `branch` | Existing non-main Git branch |
| `candidate_sha`, `base_sha` | Full lowercase 40-character candidate and current protected-main commits |
| `run_id` | The exact run being continued; a different run cannot inherit its authority |
| `repair_ordinal` | Existing consumed run-wide repair count, zero for a new run |
| `validation_profile_digest` | Current candidate, ordinal, diff-derived profile and control-launcher identity |

The host rechecks the project binding, origin, clean checkout, branch, candidate,
current remote and local main, ancestry and validation profile while holding
the existing run lease. It preserves the selected branch instead of switching
to main. It enters normal required validation and independent Quality/Security
without an initial implementation-provider invocation. Neither an adopted
candidate nor a new process replenishes repair or validation allowances.

Resume uses the immutable checkpoint selection. A supplied conflicting selection,
unknown/foreign lineage, missing authority, stale base/profile, active Git
operation, dirty checkout or changed candidate fails closed. An accepted repair
continues only through the same run's durable repair audit and candidate.
Existing terminal runs are not reopened by this command.

## Host-owned first publication

After current required validation and independent Quality/Security pass on the
same candidate, the host persists a `publication_intent` in the canonical
transaction. Its immutable identity contains the run, repository, branch, base,
SHA, validation and assurance profile digests and consumed repair ordinal.

| Durable state | Permitted action |
| --- | --- |
| `PREPARED` | Exact GitHub readback; if absent, exact branch push and the one create may be attempted |
| `CREATE_UNCERTAIN` | Readback only; absence of a receipt is a durable wait, never another create |
| `RECONCILED` | Continue with the one verified PR number |

SQLite compare-and-set claims the create boundary before the external call.
Monotonic checkpoint validation rejects stale writers that try to erase,
replace or roll back an intent. Run leases exclude simultaneous host resumes.
Recovery reloads the checkpoint after acquiring that lease and never invokes
the implementation provider.

Readback includes every GitHub page and all PR states for the exact branch.
Exactly one open draft must match the target repository, head repository,
base `main`, branch and reviewed SHA. Closed, merged, non-draft, foreign,
wrong-base, wrong-SHA and ambiguous results require explicit recovery disposition.
The candidate is rechecked before and after the external boundary. A remote
branch with different bytes is not overwritten.

A crash after `PREPARED` but before the create claim can resume the one create.
A crash after `CREATE_UNCERTAIN`, even immediately before the remote call, is
intentionally unresolved until exact remote evidence exists. This sacrifices
automatic retry availability to avoid duplicate publication when acknowledgement
is unknowable. A timeout or malformed acknowledgement follows the same rule.
The ordinary protected merge flow starts only after reconciliation.

## Exact committed-source qualification

`tools/qualification/build_platform_wheel.py` is the common wheel/sdist build
entrypoint. It rejects tracked source changes, pins HEAD and its tree,
materializes that commit into an isolated directory, and verifies every blob,
mode and path against Git before building. Ignored, untracked and cached files
from the caller checkout are excluded. Unsupported tree entries fail closed.

The build verifies the selected version and expected distribution inventory,
uses the commit timestamp as `SOURCE_DATE_EPOCH`, and reports source/tree plus
wheel/sdist SHA-256. It rechecks checkout identity after building and artifact
bytes before reporting PASS. The installed qualification consumes those exact
distribution bytes outside the source checkout. Building is not publication or
release authorization.

## Qualification inventory

`test_managed_adoption.py` exercises real local Git, CENTRAL, validation and
host lifecycle with explicit external provider/GitHub adapters. It proves
candidate adoption, wrong/stale/foreign/dirty inputs, authority, profile and
run/repair lineage. `test_managed_publication.py` covers lost acknowledgement,
crashes on both sides of create, exact existing draft, repeated and concurrent
resume, remote identity conflicts and monotonic SQLite intent. Existing host
tests retain current-validation, mandatory Q/S, repair-budget and PR checks.
`test_build_platform_wheel.py` verifies isolated materialization and rejection
of dirty source. These source tests are distinct from installed qualification.
