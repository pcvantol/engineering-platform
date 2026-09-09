# EP subagent source audit — isolated reproductions

Source repository: pcvantol/engineering-platform
Pinned main: `62eb6c4631cc23b9e4d2a53043216be6f20bfaae`
Analysis date: 2026-09-09

## Scope

This directory contains isolated diagnostic reproductions based on source read
through the GitHub connector. No EP repository, deployed instance, CENTRAL,
authorization, release, pull request, or runtime has been modified.
These checks do not establish full-suite qualification, installed proof, actual
production incidence, LLM accuracy, or token/cost savings.

## Run

Python 3.11 or later, standard library only:

```sh
python reproduce_context_and_churn.py
python reproduce_shared_telemetry.py
```

`reproduce_context_and_churn.py` copies the relevant function bodies from
`src/engineering_platform/provider_context.py` and
`src/engineering_platform/provider_usage.py`, with minimal standard-library
scaffolding. Docstrings/comments and the unused ContextProjection telemetry
property are omitted. It confirms that an oversized first mandatory section
can exclude later mandatory safety/acceptance sections, and that one command's
start/completion events can be counted as two commands and a repeated read.

`reproduce_shared_telemetry.py` is explicitly a reduced harness of the shared
`self.last_*` assignment/copy sequence in
`src/engineering_platform/execution_executor.py::CodexCliClient.review`.
It does not execute the original class. A controlled thread interleaving
illustrates the race enabled when `run_reviews` calls that method concurrently
on the same client instance. Reviewer identity/content remains independent;
usage, duration, and other telemetry can be attributed to the wrong reviewer.

The JSON files contain the outputs captured in this analysis session.

## Fresh-main reconciliation

A subsequent GitHub ref read resolved main to
`3161a4ea5a3e1109c1dc27deb74deaa239c875e9`. The GitHub comparison
from `62eb6c4631cc23b9e4d2a53043216be6f20bfaae` contains three
additional commits. None changes the source modules used by these diagnostic
reproductions or the reviewed execution-host/capability-review paths.
The JSON outputs intentionally retain the exact originally investigated SHA;
they are not relabelled as new qualification runs.
