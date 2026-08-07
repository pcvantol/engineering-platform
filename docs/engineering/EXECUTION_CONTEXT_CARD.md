# Execution Context Card

The Engineering Status dashboard presents a Producer-supplied, canonical
Execution Context as the primary operator view whenever the live status
projection contains a validated snapshot.

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
details. Historical detail views render an Execution Context only when it is
provided with the immutable history entry. The Producer Submission Envelope
ingress and immutable snapshot persistence are a separate continuation; this
card does not read Forge, prompt text or Forge Runtime state.
