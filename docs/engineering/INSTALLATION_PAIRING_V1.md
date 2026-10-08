# Installation pairing v1 — source candidate

Installation pairing authenticates an exact Forge runtime and EP instance before
any project exists. It does not register a project, attach a checkout, grant
project access, authorize a submission, start execution or grant governance.
Workspace selects or creates a project later through separate audited authority.

EP owns installation pairing registration, one-time credential issuance,
metadata-only status, credential revocation and binding detach. These records
live in CENTRAL, under the installation owner. Stable operation identities may
not be reused with different target bytes. Lost issuance disclosure is never
replayed: status and exact revocation establish the recovery boundary.

Installation credentials have a distinct verifier domain and dedicated tables.
They cannot authenticate the project consumer routes. Conversely, project
credentials cannot authenticate installation readback. Only the bounded secure
store handoff receives an issuance disclosure; no durable record stores bearer
material. Binding and credential histories are preserved after revocation.

`GET /v1/installation-compatibility` requires an installation bearer, the exact
`EP-Instance-ID` and `Forge-Instance-ID`. It rejects project headers and queries,
returns the stored binding and explicit false project, submission, execution and
governance authority, and uses `Cache-Control: no-store`. Forge verifies both
instance identities, consumer, binding, contract and authority. Credentials are
never forwarded on redirects. Non-loopback transport requires HTTPS.

Keep three qualifications separate: component service readiness, authenticated
installation connectivity, and project execution readiness. Connectivity alone
cannot report execution-ready. No historical project-scoped operation is
reinterpreted as completion of this new contract.

This is a source candidate. It has not been released or installed, and does not
change the contract of previously published EP or Forge versions. New product
versions, protected review and actual installer qualification are required.
