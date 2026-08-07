# Execution Context Card

The Engineering Status dashboard presents only the immutable, Producer-supplied
Execution Context snapshot linked to the active execution. When no snapshot
was supplied, the card explicitly shows `Not supplied by Producer` and the
execution continues normally.

Forge remains the sole owner of Execution Context generation, Mission
semantics, planning and reasoning. Engineering Platform only transports and
renders the supplied read-only snapshot; it does not derive summaries,
confidence, Mission state or execution context from prompts.

The card shows supplied Mission identity, summaries, current Intent and
Engineering Action, phase, planning confidence, iteration, runtime update,
context version, evidence/receipt references and dispatcher/queue state. The
execution phase is text-labelled as well as visually badged. Missing values are
shown as not supplied by the Producer; no value is calculated or inferred.

The engineering prompt remains available in the existing expandable execution
details. Prompt History renders Producer, Submission ID, contract/context
versions and the immutable snapshot. Engineering Reports separately expose
Producer Submission Contract, Execution Context Contract, Execution Status and
Execution Context Status. The card does not read Forge, prompt text or Forge
Runtime state.
