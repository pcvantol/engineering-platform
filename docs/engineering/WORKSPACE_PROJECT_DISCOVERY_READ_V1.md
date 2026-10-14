# Workspace project discovery — HOB-EP01

Assignment: `L2-WORKSPACE-PROJECT-DISCOVERY-READ-V1-20261012`.
JOIN: `L2-L4-HOB-EP01-VERIFIED-LINK-V1-20261012`.

**Implementation candidate, not yet a qualified handoff.** Schema75 adds one
separate security ledger. Existing B8R project/repository/attachment rows remain
the sole logical/physical topology; no new project register or identity exists.
Contract version1.0 is additive and does not widen existing credentials.

## HTTP and explicit consumer mapping

- `GET /v1/onboarding/projects?limit=25&cursor=OPAQUE`.
- `GET /v1/onboarding/projects/{project_id}/topology?expected_topology_revision=DIGEST`.

The packaged closed response schema is
`schemas/workspace-project-discovery-v1.schema.json`. `DiscoverySnapshot`
contains only permitted project IDs and their explicit canonical authority
repository IDs. `TopologySnapshot` adds that project's existing authority and
children and, if granted, its bounded physical attachment availability.
Multiple attachments do not create multiple logical repositories. No absolute
paths, remote-derived identities, agent host metadata or secrets are returned.
Unavailable/stale/revoked Agents retain their logical repository identity.

The Workspace proposal at `a6aad520bed9f1c2f7119b393435b8c739f030b3` was design
input, not an existing EP API. Its route meanings are retained. Its proposed
integer revision becomes the owner's **64-character SHA256 string** in both
`topology_revision` and `expected_topology_revision`; never truncate/cast it to
an integer. `snapshot_id` equals that digest. The owner response's explicit
`instance_id`, `producer`, `scope`, and `observed_at` map to Workspace's own
owner/scope/freshness model. Producer source/tree/wheel binding is supplied by
the eventual immutable qualified handoff; a logical snapshot digest is not a
source-commit or artifact digest. Do not synthesize source-vector evidence.

## Independent finite READ authority

Required on **both** routes:

```
Authorization: Bearer <separate discovery credential>
EP-Discovery-Contract: 1.0
EP-Instance-ID: <exact EP instance>
EP-Consumer-ID: <owner-selected Workspace consumer>
Workspace-Instance-ID: <exact Workspace runtime identity>
EP-Audience: workspace
```

All identity headers must match the actual bearer-derived grant. The audience
is `workspace`; consumer and Workspace-instance binding are separate checks.
Project scope is an explicit list of0–100 already registered active B8R
projects, never a wildcard. An explicit empty scope is healthy and grants
nothing. Identity fields are mandatory; `attachment_availability` is optional.
For an ungranted attachment field, `attachments:null` and
`attachment_visibility:NOT_GRANTED` must not be presented as no attachments.
Lifetime is finite1–86400 seconds, default900. The verifier uses a separate
domain/table; Console/admin/submission/installation-pairing bearers are not
accepted. There is no remote owner-issuance HTTP route or forwarded admin token.

Supported ordinary local installation-owner CLI:

```
engineering-platform-server discovery-grant-issue --data-root ROOT \
  --expected-instance-id EP_INSTANCE --discovery-grant-id GRANT \
  --consumer-id WORKSPACE_CONSUMER --workspace-instance-id WORKSPACE_INSTANCE \
  --discovery-project-id EXISTING_PROJECT \
  --discovery-field project_identity --discovery-field repository_identity \
  --discovery-field attachment_availability \
  --discovery-ttl-seconds 900 --operation-id ISSUE_OPERATION
engineering-platform-server discovery-grant-status --data-root ROOT \
  --expected-instance-id EP_INSTANCE --discovery-grant-id GRANT
engineering-platform-server discovery-grant-revoke --data-root ROOT \
  --expected-instance-id EP_INSTANCE --discovery-grant-id GRANT \
  --operation-id REVOKE_OPERATION
```

Repeat `--discovery-project-id` only for explicitly permitted projects. The
issue operation discloses plaintext once; replay does not mint or redisclose.
Status/revocation contain no token/verifier. Exact revocation replay is
idempotent; a different operation conflicts. These are owner ledger operations,
not project registration or runtime execution. Never publish their plaintext
output in a register or handoff; store it only in the consumer's protected
server credential channel. No real-user grants are part of qualification.

## Read boundaries, pagination and typed failures

GET bodies, duplicate/unknown parameters and unsupported contract versions are
rejected. Page size defaults25, bounded1–100. A cursor is opaque, authenticated
by a separate random server-private key stored in the grant ledger and bound
to that scope, snapshot, offset and page
size. It cannot confer rights or be reused after expiry/revocation. A topology
change between discovery/selection/detail requires rediscovery; no mixed old
identity/new topology is published. Maximum100 repositories and100 physical
attachments per selected project; overflow is a typed conflict, not silent
truncation or a healthy empty projection.

Reads use the existing noncreating/query-only CENTRAL primitive. A first
snapshot is assembled and a separate fresh snapshot rechecks authorization,
expiry, source identity and data before publication. The final read transaction
holds its shared publication fence: owner revocation cannot commit during the
protected send. Source disappearance, replacement and committed revocation
before that boundary cannot publish the old healthy snapshot. No read calls
initialize/migrate/register/attach/scan/submit or start a Mission/Action/provider.
All responses use `Cache-Control:no-store`.

The publication fence requires an existing rollback-journal store (DELETE,
TRUNCATE or PERSIST). WAL and other journal modes are rejected with422
`DISCOVERY_JOURNAL_UNSUPPORTED`; a discovery read never switches journal mode.
Source identity and finite authority are checked again after response encoding,
immediately before HTTP headers, while the protected read transaction remains
open. The check binds that publication decision; no guarantee is made against
external filesystem changes after headers have already been published.

HTTP401 means absent/invalid/expired/revoked READ authority;403 means wrong
consumer/audience/instance/project or unavailable permitted project;409 means
snapshot/cursor-topology conflict or incomplete/over-limit topology;422 means
unsupported contract/schema. Malformed query/body/cursor is400. Real source
loss/IO/unavailability is503; corruption is500. Neither is successful empty
data. Original errors remain in private server evidence; responses do not
expose paths or raw tracebacks.

Workspace must validate the closed schema, exact qualified producer and
instance, current own actor/request epoch, scope and expiry before applying a
late response. On conflict rediscover; on revoked/unavailable mark linking
unavailable. Never reconstruct identity or fall back to Console, SQL, CLI,
submission bearer, a fixture or a cached prior positive response.

## Qualification boundary

First qualify a small actual installed discovery→selected-topology chain on
own synthetic B8R registrations and finite owner-issued READ grants, including
the frozen acceptance matrix. Then existing owning tests/strict coverage,
independent Quality/Security, protected exact-head source, ordinary nonempty
Finalization, new exact-final-main installed HTTP/readback and safe own cleanup.
The immutable final source/tree/wheel/schema/issuer binding and actual results
will be supplied under the JOIN. R34 remains closed and supplies no discovery
PASS. TDE is reported separately. No L1/L3 or complete Workspace GUI prerequisite
is added, and no live EP, signing, new writer or wider HOB family is selected.
