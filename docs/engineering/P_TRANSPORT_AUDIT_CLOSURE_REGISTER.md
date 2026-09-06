# P-TRANSPORT audit-closure register

**Scope:** exact-head technical closure before human UI review. This register is
the normative current-state record, not a roadmap or a substitute for operator
approval.

## Exact-head qualification — 2026-09-06

The candidate was built from `833c81033bff09b908b02bea28bfda9629cded0f` on
`codex/phase-p-transport`, installed into a new isolated Python 3.11 virtual
environment, and measured only from that installed package. The same wheel was
then installed into the local EP Server runtime and the Server was restarted on
its supported loopback endpoint.

```ini
FRESH_WHEEL_EXACT_HEAD = TRUE
CLEAN_INSTALL_EXACT_HEAD = TRUE
FULL_REPOSITORY_TEST_SUITE = PASS
PRODUCTION_MODULE_COUNT = 107
PRODUCTION_MODULES_BELOW_80_20 = 0
MINIMUM_PRODUCTION_MODULE_COVERAGE = 80.28% (execution_host.py)
INSTALLED_INGRESS_MATRIX = PASS
CENTRAL_MIGRATION_QUALIFICATION = PASS
ROUTE_GUARDS = PASS
LOGGING_GUARDS = PASS
COMPONENT_ALIAS_GUARDS = PASS
CENTRAL_AUTHORITY_GUARDS = PASS
```

The installed ingress matrix exercised HTTP/API, CLI, File Inbox, Human Intent,
replay, negative ingress, storage authority and Dependabot multi-project
binding. The CENTRAL canary exercised source inventory/identity, admission
freeze, schema verification, copy verification, durable receipt, rollback and
postcondition denial after write.

## Acceptance-criteria closure

| ID | Closure status | Current exact-head evidence |
| --- | --- | --- |
| AC-01 | QUALIFIED | The canonical component model does not advertise an unsupported restart; focused and full installed-candidate tests pass. |
| AC-02 | QUALIFIED | The fresh wheel has no standalone File Inbox runtime; the Server-owned ingress matrix passes. |
| AC-03 | QUALIFIED | File Inbox readiness is distinguished from submission capability; negative readiness coverage passes. |
| AC-04 | QUALIFIED | The installed 3×2 ingress matrix passes `DEPENDABOT_MULTI_PROJECT_BINDING` and all public ingress/replay/negative cases. |
| AC-05 | QUALIFIED | `component_alias_retirement_guard.py` reports one canonical inventory; legacy aliases are neither selectable nor writable. |
| AC-06 | QUALIFIED | Route, source, installed-ingress and full-suite guards show no active direct Dashboard wrapper/configuration authority. |
| AC-07 | QUALIFIED | `logging_retirement_guard.py` reports CENTRAL as the only durable operational log authority and zero active local fallbacks/readers/writers. |
| AC-08 | QUALIFIED | The installed matrix uses isolated ports and covers invalid Genesis plus complete Human and negative ingress cases. |
| AC-09 | QUALIFIED | Immutable CENTRAL receipt-to-run provenance is covered by the installed Human Intent/replay flow and focused provenance regressions. |
| AC-10 | QUALIFIED | Full candidate and exact-head hosted browser/localization checks cover the five-locale Console contract and retired action rejections. |
| AC-11 | QUALIFIED | Supported Console/log paths use canonical markup and the retained scope adapter has no operational authority. |
| AC-12 | QUALIFIED | The actual EP Server runtime was reinstalled from this candidate, restarted healthy on loopback, and `relay-install` rebuilt the installation-owned binary. Launchd reports the canonical relay active (PID `80881`); loopback readiness remains healthy. Unsupported components retain no restart authority. |

## Final retirement audit — 2026-09-06

```ini
FINAL_RETIREMENT_AUDIT = PASS
UNCLASSIFIED_LEGACY_REFERENCES = 0
ACTIVE_LEGACY_AUTHORITY_REFERENCES = 0
```

Legacy lexical references are classified exclusively as one of:

- Console presentation and localization assets;
- immutable historical-evidence readers;
- historical archive or CENTRAL migration input;
- negative regression fixtures; or
- developer documentation.

They do not select a component, own a route or lifecycle, mutate operational
configuration, create a local operational store, dispatch a submission, or
write/read a supported local persistent log. The route, component-alias,
logging and CENTRAL authority guards enforce those boundaries.

## LR-09 isolation

```ini
LR09_SUCCESSOR_IMPLEMENTED = FALSE
LR09_LEGACY_AUTHORITY_REINTRODUCED = FALSE
LR09_ISOLATION = PASS
```

Dependabot successor work remains intentionally out of scope. It is not a
reason to retain Dashboard, Inbox, Watcher, Finder, checkout-local storage or
other historical runtime authority.

## Provider interruption scope

The durable recovery controller proves a bounded one-retry contract: one
interruption, one replacement attempt, two recorded invocations and one
preserved run identity. Its current controlled injection occurs before the
first provider invocation. It is not a post-start process interruption and is
not represented as one.

```ini
POST_START_PROVIDER_INTERRUPT_CANARY = NOT_IMPLEMENTED
POST_START_PROVIDER_INTERRUPT_CANARY_CLOSURE_BLOCKING = FALSE
RECOVERY_ATTEMPT_COUNT = 1 (controlled recovery contract)
PROVIDER_INVOCATION_COUNT = 2 (controlled recovery contract)
RUN_ID_PRESERVED = TRUE (controlled recovery contract)
```

This is a governed deferment: the current P-TRANSPORT acceptance criterion is
AC-12's Server Relay restart postcondition, which is qualified above. A
post-start provider-process interrupt must be introduced and qualified as a
separate bounded successor item; it must not be inferred from the pre-invocation
test hook.

## Governance state after technical audit

```ini
TECHNICAL_CLOSURE = PASS
HUMAN_UI_REVIEW = AWAITING_APPROVAL
OWNER_AUTHORIZATION = NOT_REQUESTED
MERGE = PROHIBITED
```

The exact-head Human UI review package must be regenerated or revalidated after
this register update, because this documentation commit creates a new candidate
head. No human approval, Owner Authorization or merge is implied by this
register.

## Legacy branch successor reconciliation

```ini
LEGACY_BRANCH_SUCCESSOR_RECONCILIATION = PASS
ACTIVE_INTEGRATION_BRANCH_COUNT = 1
WHOLESALE_LEGACY_MERGE_REQUIRED = FALSE
TARGETED_MISSING_INVARIANT_COUNT = 0
FORENSIC_BRANCHES_ARE_RUNTIME_AUTHORITY = FALSE
```

`codex/phase-p-transport` is the sole active integration branch. Historical
branches are forensic baselines or targeted invariant sources only.
