# EP parallel Action installed qualification V1 (PA-EQ)

**Status:** SOURCE_FIXED when protected PA-EQ delivery merges. **Owner:** Engineering Platform. **Version:** NO_BUMP.

PA-EQ qualifies the installed EP side of the first one-host, two-repository
profile. It consumes the protected Forge PA-F0 peer graph fixture, SHA-256
`b938388fb7a031c574407b62f07cb3ed7b12d692170dd4acf81cba5a14a3ab9c`.
Only the fixture's EP instance, project, Mission and repository baseline
identities are rebound to an isolated test installation; its A/B independence
and Q's exact A+B evidence edges are unchanged. The HTTP producer credential
is scoped to that synthetic project and each test repository has its own
explicit grant and Git origin. No production CENTRAL, provider account or
target repository is used.

`tools/qualification/pa_eq_installed_matrix.py` refuses Python other than
3.14, a source-checkout import, an editable install, a wrong package version or
a changed Forge fixture. The installed wheel is exercised through the normal
authenticated EP intake and submission HTTP routes. A and B acquire distinct
CENTRAL run IDs, repository resources and provider slots through the real
lifecycle worker/dispatcher. Two separate operating-system processes enter the
controlled provider at the same time. Host monotonic start/end markers prove
`max(start) < min(end)`; process IDs and repository roots must differ. Their
measured intervals are recorded through canonical invocation/timing APIs.
The graph-scoped collection then reports measured overlap and both run
identities. Q remains dependency-blocked. The same retained snapshot is
downloaded as JSON and Markdown, with exact collection-data parity. Usage
coverage stays incomplete because the controlled provider has no token usage;
no speedup or model efficiency is inferred.

The installed matrix also reruns focused fail-closed tests for malformed
graphs/evidence, wrong producer or target scope, exact predecessor predicates,
repository aliases, capacity and legacy serial gates, restart ownership,
uncertain cancellation, retry lineage, multi-active export and conflicting
clocks. Its machine-readable receipt maps each selected test to the EP-owned
portion of the shared PA-01..PA-26 catalogue. `PA-15-EP` covers known Git
storage/origin aliases and rejection of unqualified shared write roots; it
does not qualify an undeclared shared port, test database, signer or release
resource. Such resources must be declared and separately qualified before a
real profile may use them. `PA-20-EP` proves EP does not release an uncertain
writer; Forge Mission completion remains Forge-owned. `PA-26-EP` proves the
protected Forge fixture through installed EP HTTP and per-Action readback;
the future running Forge PA-F3 adapter is not claimed.
`PA-22-EP` proves the complete Action population and current typed wait for
the selected intake; other pending Actions in that same collection remain
`NOT_EVALUATED_IN_COLLECTION` until individually selected. The full shared
PA-22 wait-cause proof remains for Forge's integrated acceptance. The legacy
serial gate check is named `EP-LEGACY-SERIAL-GATE`, since it does not prove
PA-25's disabled-capability disposition.

Forge PA-F1/F2 planner fan-out, dynamic A-only continuation, B-independent
replanning, multi-parent result reconciliation, Mission completion and the
serial/parallel efficiency comparison remain Forge PA-FQ acceptance. Live
provider/cross-repository dogfood, independent candidate reviews for two
actual deliveries, shared non-Git services, same-repository parallel mutation,
production policy activation, Mission-3 and reset/T0 are outside this receipt.
The qualification is a controlled provider execution proof, not model
reasoning or production authorization.
