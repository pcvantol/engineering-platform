# Human Intent File Inbox contract

The EP Server owns File Inbox.  It transports a file to canonical Submission
Intake; Intake normalizes explicit metadata; CENTRAL admits; lifecycle runs.
Neither File Inbox nor Intake owns a queue, Action, run, database or checkout
selection.

## Supported files

`*.json` remains the structured canonical submission envelope.  `*.md` and
`*.txt` are human intent files, UTF-8 and at most 128 KiB.  They require this
deterministic metadata block:

```text
---
project: forge
repository: forge-repository
mode: MANAGED
---
Repair the delivery contract.
```

Genesis also requires an explicit absolute target; it is never inferred from a
folder, filename, CWD, Git remote or Console selection.

```text
---
project: forge
repository: forge-repository
mode: GENESIS
target: /absolute/authorized/target
---
Create the target capability.
```

The original body is retained as canonical prompt intent.  Intake records
normalization `submission-intake-v1`; no AI/provider is used.  A physical
digest supplies the File Inbox idempotency identity.  Invalid metadata,
encoding or intent is quarantined; temporary CENTRAL unavailability remains a
transport retry; execution failure belongs only to lifecycle repair.

## Compatibility map

| Historical capability | Canonical replacement | Evidence |
| --- | --- | --- |
| iCloud plain prompt delivery | Human Intent Submission Intake | `FILE_HUMAN_MANAGED` |
| Explicit Genesis prompt target | Human Intent Genesis metadata | `FILE_HUMAN_GENESIS` |
| Physical replay protection | File digest idempotency | File Inbox replay qualification |

The historical watcher implementation is not a supported parser or execution
authority.  This replacement preserves the product capability before later
legacy retirement work.
