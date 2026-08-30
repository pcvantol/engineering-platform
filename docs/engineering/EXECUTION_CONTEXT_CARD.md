# Execution Context Card

The Engineering Status dashboard presents only the immutable, Producer-supplied
Execution Context snapshot linked to the active execution. When no snapshot
was supplied, the card explicitly says `Not supplied by Producer` and the
execution continues normally.

Forge remains the sole owner of Execution Context generation, Mission
semantics, planning and reasoning. Engineering Platform only transports and
renders the supplied read-only snapshot; it does not derive summaries,
confidence, Mission state or execution context from prompts.

The active card keeps its presentation compact:

- It always shows the host-owned execution identity that is available, such as
  execution mode, repository, checkout and current active branch.
- It shows producer-supplied structured fields such as action intent,
  validation profile, required controls and context version when present.
- It shows Mission, planning and governance fields only when the Producer
  supplied those exact values. Engineering Platform never fills them from
  prompt prose or derives them from runtime activity.
- If a valid structured HUMAN submission contains no additional Mission or
  planning fields, the card displays one short explanation instead of a long
  list of empty rows.

`action_intent` is not the same field as Producer `current_intent`; the former
is the submission execution contract, while the latter is optional planning
context. Likewise, Producer `execution_phase` is not the live Engineering
Platform lifecycle phase shown elsewhere in the dashboard.

The engineering prompt remains available in the existing expandable execution
details. Prompt History renders Producer, Submission ID, contract/context
versions and the immutable snapshot as labelled fields and readable control
lists, never as a raw JSON blob. Its historical branch value is labelled
**Target branch** because it is immutable run evidence; an active local branch
would be misleading after the run has ended. Both the active card and the
historical detail modal use the same information control to explain Managed and
Genesis execution modes. Engineering Reports separately expose Producer
Submission Contract, Execution Context Contract, Execution Status and Execution
Context Status. The card does not read Forge, prompt text or Forge Runtime state.
