# Engineering Report Evidence Contract

Engineering Reports are local, derived evidence for a terminal Engineering
Platform transaction. They do not alter lifecycle, reviewer selection,
qualification or repository authority.

## Report interpretation

Each report separates three viewpoints:

1. **Initial Repository Assessment** describes the repository before any
   attempted implementation.
2. **Reviewer Findings** preserves read-only reviewer observations as initial,
   advisory input. These findings are never a statement of final repository
   state.
3. **Engineering Outcome** and **Management Summary** describe the terminal
   repository outcome.

When a transaction reaches `COMPLETE`, an initial finding about a missing
capability is labelled as resolved during implementation unless the terminal
checkpoint records a remaining limitation.

## Repository truth

The report resolves evidence in this order:

1. persisted repository state and terminal checkpoint;
2. resulting commits;
3. validation evidence; and
4. advisory reviewer observations.

Reviewer findings cannot override repository evidence. `BLOCKED` and `FAILED`
reports never claim successful implementation or delivery.
