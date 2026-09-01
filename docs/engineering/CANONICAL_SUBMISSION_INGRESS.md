# Canonical submission ingress

```
HTTP API ─┐
CLI ──────┼──> Submission Service ──> admission ──> queue ──> execution
File ─────┘
```

`SubmissionService` is CENTRAL-owned. It validates a normalized submission,
checks that the explicit project and repository are registered together,
persists the durable submission, records a prompt-history digest and exposes a
`QUEUED` admission result. It never starts a provider, selects an Agent, or
acquires an execution lease. Those remain later CENTRAL execution lifecycle
work.

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

The optional legacy-file compatibility adapter accepts only UTF-8 JSON:

```json
{"project_id":"djconnect","submission":{"repository_id":"djconnect","producer":{"id":"legacy-file","type":"HUMAN"},"prompt":"..."}}
```

There is no default project or current-directory inference. Its polling,
stable-file detection, archival and quarantine behavior remain transport
concerns; once decoded it calls the same service.

## Migration receipt / watcher inventory

| Historical responsibility | Classification | B8D destination |
| --- | --- | --- |
| `cloud_root`, `folders`, `local_folders`, `stable_prompt`, `discover`, `_move`, archive paths, launchd loop | TRANSPORT_SPECIFIC | legacy-file adapter only / retired polling runtime |
| `parse_producer_submission`, `_persisted_producer_for_run`, `record_submission` | CANONICAL_SUBMISSION_CORE | `SubmissionRequest` and `SubmissionService.submit` |
| `_admit_queue_candidate`, host/workspace/capability preflight | ADMISSION_CORE | retained execution-admission lifecycle; submission admission only resolves CENTRAL project/repository |
| queue predecessors, retries, leases, detached runner, provider launch | EXECUTION_ORCHESTRATION | retained downstream; no adapter owns it |
| status projections, prompt history, telemetry, terminal reports | OBSERVABILITY | submission event/history projection plus retained downstream evidence |
| retry/archive migration and legacy markdown producer parsing | LEGACY_COMPATIBILITY | legacy JSON adapter; historical markdown remains historical compatibility |

UNRESOLVED: 0. The old watcher is not execution authority. It is a source of
transport-independent semantics and, if enabled later, a file transport
adapter. Schema 43 is a forward CENTRAL migration; no schema-40 database is
migrated or read.

## Post-merge installation plan

1. Install the Server artifact and run its forward schema-43 initialization
   against the existing CENTRAL data root.
2. Register the intended consumer for its explicit project and issue its
   scoped credential through the operator credential workflow.
3. Start CENTRAL and perform an acceptance-only HTTP/CLI canary with a
   disposable project submission; do not dispatch execution.
4. Optionally enable the legacy file adapter with its explicit project JSON
   envelope. B9 can use CLI or HTTP after governance approval.
