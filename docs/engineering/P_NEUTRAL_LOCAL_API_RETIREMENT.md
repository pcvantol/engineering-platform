# P-NEUTRAL Local Consumer API retirement

## Current product boundary

The supported Engineering Platform submission ingresses are exactly:

1. HTTP JSON to Engineering Platform Server;
2. the installed CLI; and
3. the Server-owned File Inbox.

All three normalize through Engineering Platform Server, then Submission
Service, then CENTRAL.  The historical Local Consumer API is not a supported
submission ingress and has no direct path to CENTRAL.

## Reference classification

| Surface | Classification | Current authority |
| --- | --- | --- |
| `local_api.py` contract server | TEST_ONLY / historical compatibility | No |
| `com.djconnect.engineering-local-api` | HISTORICAL_ONLY | No |
| `com.engineeringplatform.local-api` | HISTORICAL_ONLY; never a replacement service | No |
| Local API LaunchAgent generation and service CLI | REMOVE | No |
| CENTRAL migration service ordering | REMOVE | No |
| `local_api_credentials` tables and verifier | CURRENT_AUTHORITY: Server HTTP consumer authentication | Yes, Server-owned |
| credential-table forensic and migration evidence | HISTORICAL_ONLY / MIGRATION_ONLY | No service authority |

The credential table names are retained for schema compatibility and current
Server authentication.  This increment does not drop or rewrite them.

## P-INSTALLER-V1 handoff

`P_INSTALLER_V1_PROFILE = EP_SERVER_ONLY`.

It must include the Engineering Platform Server, CENTRAL, HTTP JSON, installed
CLI, File Inbox, Server-owned lifecycle and Console/relay where applicable,
health/readiness, and install/repair/upgrade/uninstall receipts.  It must
exclude the Local API service, Forge Runtime, Workspace, and Project Agent
productization.
