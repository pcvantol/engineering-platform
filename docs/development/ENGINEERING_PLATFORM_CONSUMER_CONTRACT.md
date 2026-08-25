# Engineering Platform 2.x consumer contract

## Purpose

This contract lets DJConnect, Forge and Workspace consume an installed,
pinned Engineering Platform wheel without carrying EP source code or owning EP
execution data.

## Project registration

Before a consumer submits an Engineering Action, it registers one active
Workspace project with the local EP installation. Registration contains:

- `project_id`: immutable, opaque and canonical in Workspace;
- `project_name`: current human-friendly Workspace label, required for normal
  dashboard presentation but never used as identity;
- the validated local repository/workspace path;
- the project-specific writable Inbox root;
- optional consumer display metadata.

`project_id` is mandatory on every consumer-to-EP operation. Paths, repository
names and display names are not identities and cannot substitute for it.

### Registration projection

EP stores a single current registration record per `project_id`, including at
least `project_id`, `project_name`, repository/workspace path, Inbox root and
the last registration update time. `project_name` is a mutable label supplied
by Workspace. Registering the same canonical ID with a new name is an atomic
label update, not a new project and not a migration of execution data.

The dashboard's project selector renders `project_name` as its primary text.
It may expose `project_id` as secondary diagnostic information, but it must
not make the technical ID the normal label. If a legacy registration has no
name, EP uses a clearly marked temporary fallback until Workspace refreshes
that registration; it must never infer a name from a repository path.

## Ownership and isolation

EP keeps one installation-wide SQLite database. All EP execution data carries
`project_id` and is queried, queued, leased and displayed within that project
scope. Each project has an independent Inbox route and queue; an execution for
one project can never consume another project's prompt.

Project names are deliberately not copied into project-scoped lifecycle,
receipt, report, telemetry or Prompt History rows. Their stable relation is
always the canonical `project_id`; selector and dashboard labels are resolved
from the current registration so a Workspace rename is immediately reflected
without rewriting historical evidence.

Workspace keeps its own planning state and canonical project registry.
Forge remains the owner of planning and Runtime Prompts. EP remains the owner
of execution lifecycle, telemetry, evidence, dashboard, Inbox and Prompt
History. The physical Inbox transport and Workspace API route remain parallel
ways to admit a prompt for the same registered project.

### Settings scope

The registered Inbox root, Inbox scan interval and open-pull-request check
interval are project settings. They belong to the selected project's queue,
never to installation-wide EP configuration. Log retention, log level,
platform-health refresh and component-detail refresh remain installation-wide
settings. Fixed lifecycle, lease and retry safeguards remain EP runtime
defaults rather than consumer-editable project settings.

## Upgrade and compatibility

The local upgrade runs before the installed EP process becomes the writer:

1. create a recoverable backup of legacy EP state;
2. verify consumer and wheel compatibility;
3. register the legacy workspace as one canonical project;
4. migrate and backfill project-scoped EP records in place into the central
   installation store;
5. update launchd to the installed EP commands;
6. validate that only the installed EP writer is active.

The consumer pins the immutable EP 2.x wheel version. It must fail closed when
the requested contract version or canonical Workspace `project_id` is absent.
