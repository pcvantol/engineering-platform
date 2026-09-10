# P-NEUTRAL Phase 0 audit

This record begins P-NEUTRAL as an inventory-and-classification increment. It
does not rename, move, install, uninstall, start, stop, load, unload, or
otherwise mutate host or repository runtime artifacts.

## Independent scopes

The repository inventory is independently complete when every current-worktree
DJConnect reference has a terminal classification. The host audit is separate.
Its first stage emits only a frozen, exact discovery manifest. Discovery does
not inspect candidate contents, enumerate the environment, query Keychain, or
query process/service state. It cannot establish a host residual count,
authority classification, platform identity, or host-audit completion.

The final host audit may begin only after the discovery manifest passes
validation. It must enter `HOST_INSPECTION_AUTHORIZED`, then
`HOST_INSPECTION_RUNNING`; `HOST_INSPECTION_PASS` is deliberately not a state.
A passing inspection atomically yields:

```
HOST_INSPECTION_STATUS = PASS
P_NEUTRAL_HOST_AUDIT = COMPLETE
HOST_AUDIT_STATE = P_NEUTRAL_HOST_AUDIT_COMPLETE
```

The only permitted state progression is:

```
INITIAL -> HOST_DISCOVERY_RUNNING -> HOST_DISCOVERY_PASS
-> HOST_INSPECTION_AUTHORIZED -> HOST_INSPECTION_RUNNING
-> P_NEUTRAL_HOST_AUDIT_COMPLETE
```

Failure states are `HOST_DISCOVERY_BLOCKED` and `HOST_INSPECTION_BLOCKED`.

## Produced evidence

- `p-neutral-repository-inventory.jsonl` contains one record for every file in
  the frozen authority revision containing `DJConnect`, with a terminal
  repository classification and disposition.
- `p-neutral-host-discovery.jsonl` contains only exact candidates admitted to
  the future host inspection. Its IDs are deterministic SHA-256 values.
- `scripts/engineering/audit_p_neutral.py validate` validates manifest shape,
  uniqueness, repository classifications, and the absence of inspected-output
  representation in this discovery-only increment.

The discovery manifest freezes the exact Application Support, runtime, cache,
and log roots listed in the manifest for a future Stage 2. It separately
freezes the exact LaunchAgent, CLI, and environment-key identities listed in
the manifest. No broad host
root, broad service namespace, or broad environment enumeration is authorized.

## Derived naming evidence

The authority revision derives `Engineering Platform` as product name,
`engineering_platform` as package prefix, `com.engineeringplatform` as the
neutral service prefix, `ENGINEERING_PLATFORM` as the neutral configuration
and environment prefix, and
`~/Library/Application Support/Engineering Platform` as the installation-root
prefix. Active legacy `com.djconnect.engineering-*` services and
`DJCONNECT_ENGINEERING_*` keys remain repository remediation findings; they do
not alter the derived neutral target.

## Stage 2 completion

The authorized bounded inspection completed every frozen candidate. All five
admitted filesystem roots were inspected to bounded metadata depth without
following symlinks; the five exact LaunchAgent labels, five exact CLI names,
and fifteen exact environment keys were queried individually. No Keychain,
environment dump, service/process enumeration, or host mutation occurred.

One actual blocking residual was observed:
`com.djconnect.engineering-dashboard-relay` is loaded. Its exact loaded label
has lifecycle authority and conflicts with the derived neutral service prefix.
The host audit is complete—not partially passed—and the evidence therefore
concludes that DJConnect remains a platform identity inside this frozen scope.
The resulting remediation backlog is complete in
`p-neutral-execution-backlog.jsonl` and includes this finding; host remediation
remains deferred.
