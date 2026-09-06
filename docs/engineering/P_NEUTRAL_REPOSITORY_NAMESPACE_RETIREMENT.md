# P-NEUTRAL repository namespace retirement

## Canonical active namespace

Engineering Platform is the sole active product identity in this repository:

- package and runtime: `engineering_platform`;
- service labels: `com.engineeringplatform.*`;
- environment: `ENGINEERING_PLATFORM_*`;
- producer contract: `engineering_platform.producer_submission`.

The active submission architecture remains exactly HTTP JSON, installed CLI and
File Inbox.  All three enter the Engineering Platform Server and Submission
Service before CENTRAL.  No predecessor-named Local API, LaunchAgent, service
or environment setting is current authority.

## Environment retirement map

| Retired key | Canonical key |
| --- | --- |
| `DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA` | `ENGINEERING_PLATFORM_ADMITTED_STORAGE_SCHEMA` |
| `DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT` | `ENGINEERING_PLATFORM_ADMITTED_STORAGE_ROOT` |
| `DJCONNECT_ENGINEERING_CHAT_MODEL` | `ENGINEERING_PLATFORM_CHAT_MODEL` |
| `DJCONNECT_ENGINEERING_CODEX_EXECUTABLE` | `ENGINEERING_PLATFORM_CODEX_EXECUTABLE` |
| `DJCONNECT_ENGINEERING_LOG_LEVEL` | `ENGINEERING_PLATFORM_LOG_LEVEL` |
| `DJCONNECT_ENGINEERING_PREFLIGHT_MIN_FREE_BYTES` | `ENGINEERING_PLATFORM_PREFLIGHT_MIN_FREE_BYTES` |
| `DJCONNECT_ENGINEERING_TELEMETRY_PERSISTENCE` | `ENGINEERING_PLATFORM_TELEMETRY_PERSISTENCE` |
| `DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE` | `ENGINEERING_PLATFORM_TEST_INTERRUPT_PROVIDER_ONCE` |
| `DJCONNECT_ENGINEERING_VALIDATION_RUN_ID` | `ENGINEERING_PLATFORM_VALIDATION_RUN_ID` |
| `DJCONNECT_EVIDENCE_EXPAND` | `ENGINEERING_PLATFORM_EVIDENCE_EXPAND` |
| `DJCONNECT_EVIDENCE_ORIGINAL_PATH` | `ENGINEERING_PLATFORM_EVIDENCE_ORIGINAL_PATH` |
| `DJCONNECT_CONTEXT_ESCALATION_FILE` | `ENGINEERING_PLATFORM_CONTEXT_ESCALATION_FILE` |

Retired keys are not read as compatibility fallbacks.  An ambiguous mixed
environment cannot silently establish predecessor configuration authority.

The predecessor `djconnect.producer_submission` envelope is accepted only to
ingest existing immutable producer evidence.  New EP emitters use the
canonical contract above; the predecessor contract is not an output or runtime
selection default.

## Explicit retained references

The following predecessor identifiers remain only as bounded evidence; no
current install, lifecycle, repair, health or configuration path selects them:

- `com.djconnect.engineering-dashboard-relay`: migration source for the
  already-installed relay;
- `com.djconnect.engineering-local-api`: historical Local API identity;
- `com.djconnect.engineering-dashboard` and
  `com.djconnect.engineering-inbox`: stale-lock recognition during historical
  CENTRAL-store migration, never restart targets;
- `.djconnect`: legacy-workspace migration input;
- immutable provenance, historical ADRs, extraction manifests and negative
  fixtures.

External Trusted Delivery configuration names are governed repository-security
configuration, not Engineering Platform runtime authority.  They are recorded
as external integration evidence and must not be used by product runtime code.
