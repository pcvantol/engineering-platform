# Canonical submission ingress

```
HTTP API ─┐
CLI ──────┼──> Submission Service ──> ACCEPTED ──> ADMITTED / QUEUED ──> NOT_DISPATCHED
File ─────┘
```

`SubmissionService` is CENTRAL-owned. It validates a normalized submission,
checks that the explicit project and repository are registered together,
persists the durable submission, records a prompt-history digest and exposes a
single durable lifecycle: `ACCEPTED` → `ADMITTED` / `QUEUED` →
`NOT_DISPATCHED`. Every accepted submission records those three ordered events.
The final value is intentionally negative: CENTRAL has not started a provider,
selected an Agent, acquired a lease or invoked Codex. Those actions remain
behind a future execution protocol and Project Agent boundary.

## Consumer contract

`POST /v1/projects/{project_id}/submissions` accepts JSON with
`repository_id`, `producer` (`id`, `type`, optional `version`), `prompt`, and
optional `idempotency_key`, correlation/action identifiers, and constraints.
It uses a scoped bearer credential created for exactly one consumer/project
registration. A credential for one project is not usable for another project.
The response contains `submission_id`, project/repository identity, `state`,
`created_at`, and `admission`. HTTP acceptance is not execution success.

The installed `engineering-platform submit` command is an HTTP consumer: it
reads the prompt from `--prompt-file`, the bearer value from
`EP_CONSUMER_TOKEN` (or `--credential-env`), and never opens CENTRAL SQLite.
It identifies its adapter as `CLI` while using the same authenticated HTTP
boundary, so durable receipts distinguish HTTP and CLI without splitting their
lifecycle. `--correlation-id`, `--mission-id`, `--engineering-action-id` and a
JSON-object `--constraints-file` cover the remaining normalized request fields.

The Server-owned File Inbox child accepts structured UTF-8 JSON and human
intent `.txt`/`.md` files from its configured installation `file-inbox/`
directory. It is not an installed executable and cannot be started apart from
EP Server:

```json
{"project_id":"djconnect","submission":{"repository_id":"djconnect","producer":{"id":"file-inbox","type":"HUMAN"},"prompt":"..."}}
```

There is no default project or current-directory inference. File Inbox is an
explicit bounded internal principal (`FILE_INBOX`), not an impersonated
project consumer: it bypasses only external bearer authentication and then
calls the same canonical Submission Service as HTTP and CLI. Project and
repository registration, mode/Genesis validation, idempotency, admission and
lifecycle validation all remain mandatory. No File Inbox credential, project
token map or internal HTTP endpoint exists.

It moves one file
through `incoming/`, `processing/`, `accepted/`, or `quarantine/`. The SHA-256
of the physical file is its deterministic transport receipt/idempotency key,
so a restart after CENTRAL acceptance replays the same canonical request and
cannot create another Action. An accepted archive has a bounded receipt JSON;
malformed or authorization-rejected files have a bounded quarantine reason.
If CENTRAL is unavailable, the file remains in `processing/` for delivery
retry only. No file transport database, StateStore, queue, retry lifecycle, or
execution state exists.

`LEGACY_FILE` remains an internal, unreachable provenance helper only. It is
not an installed ingress or a supported watcher path.

## Canonical lifecycle contract

| Boundary | Durable state | Meaning | Explicitly excluded |
| --- | --- | --- | --- |
| Submission | `ACCEPTED` | Syntax and producer facts were normalized and persisted. | Provider/Agent selection, lease acquisition and process launch. |
| Admission | `ADMITTED` / `QUEUED` | CENTRAL confirmed the explicit active project and repository relationship. | Queue claiming, predecessor recovery and host preflight. |
| Execution | `NOT_DISPATCHED` | A later protocol may decide whether and how to execute. | Server-to-Codex invocation, direct workspace mutation or execution success. |

`ep_submission_events` is the durable ordered evidence. The shared service
validates that the exact event sequence exists before returning a result or an
idempotent replay. Transport is provenance, not execution authority.

## Retired watcher evidence

The historical Inbox watcher and its semantic inventory were retired after
their transport, intake and lifecycle responsibilities moved to the
Server-owned File Inbox, Submission Intake and CENTRAL Lifecycle Worker.
Historical Git evidence records the prior implementation; it is not shipped,
installed or executable in the supported product. Schema 43 is a forward
CENTRAL migration; no schema-40 database is migrated or read.

## Post-merge installation plan

1. Install the Server artifact and run its forward schema-43 initialization
   against the existing CENTRAL data root.
2. Register the intended consumer for its explicit project and issue its
   scoped credential through the operator credential workflow.
3. Start an isolated installed-package CENTRAL and perform an acceptance-only
   HTTP/CLI/file canary with a disposable project submission; confirm all three
   event boundaries end in `NOT_DISPATCHED`. Do not use the live B8 CENTRAL.
4. Start EP Server; its Server-owned File Inbox child recovers pending physical
   delivery state. B9 remains forbidden until B8C passes, B8D passes and the
   execution protocol is explicitly ready.
