# Project / repository attachment contract

**Status:** B5 declarative foundation.  This is the repository-local
attachment contract for a future EP Project Agent and EP Server.  It is not a
transport, authentication, registration, workspace, installer, or execution
contract.

## Canonical repository-local declaration

Every attached repository declares exactly one JSON file at:

```
<repository-root>/.engineering-platform/repository.json
```

The versioned schema is packaged at
`engineering_platform/schemas/repository-attachment.schema.json`; its stable
identity is `https://engineering-platform.dev/schemas/repository-attachment/1.0`.
The matching EP reader is `engineering_platform.repository_attachment`.
Unknown schema versions and unknown fields fail closed.  The reader only reads
this exact path in the supplied repository root; it does not crawl parent,
sibling, workspace, or host directories.

## Shape and authority

```json
{
  "schema_version": "1.0",
  "project": {"id": "acme-mobile", "authority_repository_id": "acme-mobile-app"},
  "repository": {"id": "acme-mobile-framework", "role": "child"},
  "validation": {"kind": "command", "entrypoint": "xcodebuild test"},
  "requirements": {"host": {"os": "macos"}, "tools": {"xcode": "16.x"}},
  "integrations": {"issue_tracker": {"project_key": "MOBILE"}}
}
```

`project.id` and `repository.id` are stable lowercase identifiers, not local
paths, Git remotes, hostnames, database keys, or workspace IDs.  A project has
one `authority_repository_id`.  Its declaration uses role `authority` and its
repository ID must equal that authority ID.  Every other repository uses role
`child` and references the same project and authority repository ID.

Consequently, a single-repository project uses the same identifier for its
authority and local execution repository.  A multi-repository project is
formed by independently attached authority and child declarations that share
the project and authority identities.  B5 deliberately does not discover,
store, or reconcile those peer declarations.

`validation` is a local validation boundary declaration.  Its `entrypoint` is
opaque metadata in B5: reading it must never execute it.  `kind: "none"` is
the explicit boundary for a static repository with no automated local
validation.  `requirements.host` and `requirements.tools` are optional
capability constraints, not host identity or placement.  `integrations` is
namespaced optional metadata only; it must contain no credentials, bearer
tokens, absolute paths, remotes, or executable payloads.

## Topology boundary

The logical project topology is carried by stable project/repository IDs and
the authority-repository relationship.  Physical topology (which host has a
checkout, where it lives, and whether a tool is installed) is discovered and
attested later by the Agent/host layer.  It is not durable attachment topology.

Workspace state is a consumer projection and cannot be the topology authority.
Likewise, one Agent's local state cannot be the sole topology authority.  A
future Server may retain registered attachment declarations after authenticated
registration, but B5 creates no Server rows, no Agent state, and no registration
mechanism.

## B6 convergence handoff

| Topic | B5 handoff |
| --- | --- |
| Config/schema identity | `.engineering-platform/repository.json`; schema `https://engineering-platform.dev/schemas/repository-attachment/1.0`; schema version `1.0`. |
| Repository/project identifiers | `project.id`, `project.authority_repository_id`, `repository.id`, and `repository.role` (`authority` or `child`). |
| Expected Agent read surface | Read and structurally validate the exact config path, then expose `project`, `repository`, `validation`, optional `requirements`, and optional `integrations`; do not execute the entrypoint. |
| Expected Server registration payload | The four logical identifiers plus schema version, validation declaration, optional requirements/integrations, and later separately-attested physical checkout/host evidence.  A Server must reject mismatched authority/role declarations and duplicate/conflicting repository identities. |
| Unresolved transport/auth | Agent-to-Server endpoint, credentials/attestation, registration authorization, Server durability and conflict resolution, revocation, peer-topology reconciliation, and how an approved execution layer invokes the declared entrypoint. |

Fixtures cover .NET, Swift, Python, Node/web, embedded, static, and an
illustrative future DJConnect authority declaration.  The DJConnect fixture is
test-only and does not mutate DJConnect or establish a registration.
