# Progress-aware provider deadlines V2

**Status:** source implemented; runtime qualification requires the normal
protected-main release and installed-product readback.

## Problem

The V1 timeout used one wall-clock limit for an entire primary provider action.
A productive action could therefore be stopped while it was still changing the
workspace, running bounded commands, or preparing its structured hand-off. The
host then had no valid `AgentResult` and discarded otherwise useful work.

## Boundaries

V2 keeps provider authority bounded with two immutable host-owned limits:

| Workflow | Maximum inactivity | Absolute maximum |
| --- | ---: | ---: |
| Specialist review | 5 minutes | 5 minutes |
| Implementation | 15 minutes | 45 minutes |
| Local repository validation | 15 minutes | 45 minutes |
| Autonomous quality control | 10 minutes | 30 minutes |
| Repair | 15 minutes | 45 minutes |
| Finalization | 15 minutes | 45 minutes |
| End reconciliation | 10 minutes | 30 minutes |

The inactivity clock advances only for concrete provider events: command
execution, file change, web search, MCP tool use, and the structured agent
message. Workspace change-count transitions also count. Reasoning text does not
advance the deadline. The absolute maximum never resets.

When either boundary expires, EP terminates only the owned provider process
group and retains the existing durable terminal-failure semantics. Dashboard
configuration remains read-only and shows both limits.

## Acceptance

- concrete provider progress extends a primary action beyond the inactivity
  boundary;
- reasoning-only output cannot extend it;
- no progress stops at the inactivity boundary;
- continuing progress still stops at the absolute maximum;
- callback state is removed after every provider attempt;
- existing process-group cleanup and terminal evidence remain authoritative.
