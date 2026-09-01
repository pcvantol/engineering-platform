# Project identity and attachment runtime (B8R)

**Status:** Current Engineering Platform product architecture.

## Authority model

The Canonical Project Authority Repository explicitly declares the durable
logical identity in `.engineering-platform/repository.json`: `project.id`,
`project.authority_repository_id`, `repository.id`, and `repository.role`.
EP accepts those identifiers only after validating the committed declaration.
It never derives identity from a checkout path, Git remote, repository name,
display name, host, Server installation UUID, Agent UUID, Workspace runtime
identity, or credential.

Workspace remains owner of its product state and human-facing presentation. It
may project this identity and maintain a mutable display name, but Workspace
availability is not required for an independent EP installation to clone and
attach a declared repository. This supersedes only the older extraction and
consumer-contract statements that named Workspace as the source of logical
repository project identity; those documents remain historical context for the
legacy consumer-registration flow.

## Logical versus physical topology

Schema 42 persists logical projects and repositories separately from physical
attachments. A project has exactly one authority repository; a single-repo
project declares an `authority` repository whose ID equals
`authority_repository_id`. Child declarations must name the same project and
authority. The Server rejects malformed/unsupported declarations, authority
mismatches, conflicting project declarations, and a repository ID claimed by a
different project.

An attachment records only Agent ID, repository ID, bounded checkout evidence,
availability, and timestamps. It contains no absolute checkout path. One
logical repository can be AVAILABLE on multiple Agents. An Agent's heartbeat
controls freshness; a stale or revoked Agent is UNAVAILABLE without deleting
logical topology.

## Registration boundary

`engineering-project-agent attach --repository-root ROOT` reads exactly the
B5 file from an explicitly supplied root. It does not create, edit, execute,
commit, crawl, or discover repositories. The Agent submits the validated
read-surface through the B6A credential. The Server independently parses and
validates it before one atomic, idempotent registration operation. Only paired,
authenticated, non-revoked Agents are accepted. `engineering-platform-server
topology` is a read-only, secret-free diagnostic surface.

Schema 41 did not have repository or attachment rows and is therefore
**EXTENSION_REQUIRED**. Schema 42 is the official forward-only migration:
schema-41 bootstrap structures are retained and the topology tables are added
transactionally. Fresh stores bootstrap through the same migration. Existing
schema-41 installations are upgraded when opened by this version; test stores,
not B7A CENTRAL, were used to qualify it.

## B8 DJConnect proposal (not committed here)

The later governed DJConnect PR should propose this declaration, with its
validation entrypoint selected from DJConnect's then-current canonical
repository documentation rather than guessed by EP:

```json
{
  "schema_version": "1.0",
  "project": {"id": "djconnect", "authority_repository_id": "djconnect"},
  "repository": {"id": "djconnect", "role": "authority"},
  "validation": {"kind": "command", "entrypoint": "<DJConnect canonical validation entrypoint>"}
}
```

It must contain no `server_url`, `server_uuid`, `central_id`, `agent_id`,
`host_id`, credential, credential reference, local path, or service state.

## B8 prerequisite

B8 may resume only after B8R is governed-merged and the DJConnect declaration
is separately governed-merged. B8 does not wait for Workspace to manufacture a
project ID.
