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

The optional legacy-file compatibility adapter accepts only UTF-8 JSON:

```json
{"project_id":"djconnect","submission":{"repository_id":"djconnect","producer":{"id":"legacy-file","type":"HUMAN"},"prompt":"..."}}
```

There is no default project or current-directory inference. Its polling,
stable-file detection, archival and quarantine behavior remain transport
concerns; once decoded it calls the same service with transport `LEGACY_FILE`.
An idempotency key only replays the same immutable normalized request; a
different request using that key is rejected with `IDEMPOTENCY_CONFLICT`.

## Canonical lifecycle contract

| Boundary | Durable state | Meaning | Explicitly excluded |
| --- | --- | --- | --- |
| Submission | `ACCEPTED` | Syntax and producer facts were normalized and persisted. | Provider/Agent selection, lease acquisition and process launch. |
| Admission | `ADMITTED` / `QUEUED` | CENTRAL confirmed the explicit active project and repository relationship. | Queue claiming, predecessor recovery and host preflight. |
| Execution | `NOT_DISPATCHED` | A later protocol may decide whether and how to execute. | Server-to-Codex invocation, direct workspace mutation or execution success. |

`ep_submission_events` is the durable ordered evidence. The shared service
validates that the exact event sequence exists before returning a result or an
idempotent replay. Transport is provenance, not execution authority.

## Historical watcher semantic inventory

The machine-checked [watcher semantic inventory](INBOX_WATCHER_SEMANTIC_INVENTORY.json)
maps all 77 top-level watcher functions exactly once: 77 classified, 0
unclassified and 0 ambiguous. It retains the six closed classifications used
in the original receipt and captures the semantics that the future execution
protocol must preserve.

The old watcher is not CENTRAL execution authority. It is a source of
transport-independent lifecycle semantics and, if enabled later, a file
transport adapter. Schema 43 is a forward CENTRAL migration; no schema-40
database is migrated or read.

## Post-merge installation plan

1. Install the Server artifact and run its forward schema-43 initialization
   against the existing CENTRAL data root.
2. Register the intended consumer for its explicit project and issue its
   scoped credential through the operator credential workflow.
3. Start an isolated installed-package CENTRAL and perform an acceptance-only
   HTTP/CLI/file canary with a disposable project submission; confirm all three
   event boundaries end in `NOT_DISPATCHED`. Do not use the live B8 CENTRAL.
4. Optionally enable the legacy file adapter with its explicit project JSON
   envelope. B9 remains forbidden until B8C passes, B8D passes and the
   execution protocol is explicitly ready.
