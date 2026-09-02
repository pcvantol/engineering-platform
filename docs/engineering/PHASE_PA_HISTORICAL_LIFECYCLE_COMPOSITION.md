# Phase P-A historical lifecycle composition

`ParityLifecycleDispatcher` is the sole Phase-P writer from canonical CENTRAL
submissions into the preserved installed local Execution Host.  It constructs a
`ParityProjectContext`, resolves the schema-44 binding, claims one submission
in schema-45 CENTRAL state, and allocates the existing `inbox-*` run identity.

The dispatcher writes only the immutable submitted prompt beneath the
installation/project/run artifact path.  It then uses the historical watcher
storage/admission primitives and invokes `EngineeringRunner`; it does not
start the file watcher, Project Agent, local API, relay, dashboard, or any
DJConnect-owned service.

The durable `ep_parity_lifecycle_dispatches` record binds submission, project,
repository, run, and prompt artifact.  Its unique submission and run keys make
the first claim atomic; a restart resumes the same run ID.  FIFO,
predecessors, execution leases, provider handling, recovery, Managed/Genesis
flows, qualification, finalization, history, evidence, reports, and telemetry
remain entirely in their preserved historical modules.

The P-A dispatcher is deliberately an installed-local execution boundary.  It
does not grant Project Agent any execution or provider authority.  Phase S is
the future replacement of this physical invocation only.
