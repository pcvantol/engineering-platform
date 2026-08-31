# Engineering Platform Repository Bootstrap

**Status:** Canonical standalone repository onboarding

Run repository synchronization from the intended Engineering Platform checkout
before qualification or implementation work:

```sh
git switch main
git fetch origin main
git merge --ff-only origin/main
```

The standalone package, its tests, and its qualification evidence are the
authority for Engineering Platform development. Runtime defaults and version
metadata are resolved from the installed `engineering_platform` package; an
arbitrary consumer project is not an Engineering Platform checkout.

For the extraction provenance, source-to-target reconciliation, and the
history-preserved source context, see
[`docs/provenance/PHASE_3_INCREMENT_1_EXTRACTION_RECEIPT.md`](docs/provenance/PHASE_3_INCREMENT_1_EXTRACTION_RECEIPT.md).

For local host operation and qualification, see
[`docs/development/LOCAL_AGENT_RUNNER.md`](docs/development/LOCAL_AGENT_RUNNER.md)
and [`docs/engineering/ENGINEERING_QUALIFICATION.md`](docs/engineering/ENGINEERING_QUALIFICATION.md).
