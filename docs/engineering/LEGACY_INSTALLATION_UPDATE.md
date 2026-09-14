# Legacy operational installation update

This maintenance route adopts one artifact-identified EP user-service runtime
for one exact update. It does not reconstruct a historical source revision and
does not create an operational installation record for the old release. The
first such record identifies only the activated, qualified target release.

## Preconditions

- The existing `com.engineeringplatform.server` LaunchAgent, its interpreter,
  instance, data root, installed package and preserved original wheel agree.
- `operational-installation.json` is truly absent. An unreadable, malformed,
  schema-invalid or dangling-symlink record is a conflict.
- The invoking local user exclusively owns the data root, which is not group-
  or world-writable.
- The target wheel digest, semantic version and 40-character source revision
  are qualification inputs for one operation ID.
- The preserved CENTRAL database remains available for retained backup and
  exact-target migration. The preserved old wheel alone is not rollback proof
  after a schema migration.

## Installed maintenance signatures

All commands are provided by the installed `engineering-platform-server`
artifact. They do not start a second EP Server.

```text
engineering-platform-server legacy-adoption-inspect \
  --data-root ROOT --preserved-wheel OLD_WHEEL

engineering-platform-server legacy-adoption-authorize \
  --data-root ROOT --preserved-wheel OLD_WHEEL \
  --operation-id OPERATION --target-version VERSION \
  --target-digest sha256:DIGEST --target-source-revision 40_HEX \
  --acknowledge-unknown-source-revision

engineering-platform-server installation-update-plan \
  --data-root ROOT --operation-id OPERATION --artifact TARGET_WHEEL \
  --target-version VERSION --target-digest sha256:DIGEST \
  --target-source-revision 40_HEX

engineering-platform-server installation-update-prepare \
  --data-root ROOT --operation-id OPERATION --artifact TARGET_WHEEL \
  --target-version VERSION --target-digest sha256:DIGEST \
  --target-source-revision 40_HEX [--venv-builder PYTHON]

engineering-platform-server installation-update-admit \
  --data-root ROOT --operation-id OPERATION

engineering-platform-server installation-update-apply \
  --data-root ROOT --operation-id OPERATION

engineering-platform-server installation-update-resume \
  --data-root ROOT --operation-id OPERATION

engineering-platform-server installation-update-status \
  --data-root ROOT --operation-id OPERATION
```

`prepare` stages the exact wheel below the operation root, creates and verifies
the non-operational candidate, and durably binds candidate plus legacy
provenance. `admit` rechecks the current owner, service selection, old wheel and
installed package under the existing installation lock before recording typed
execution evidence. `apply` and `resume` accept no new target or interpreter;
they reopen only the durable plan, candidate and admission.

The composed execution retains the normal inventory, quiesce, CENTRAL backup,
target migration, service activation, target-record creation, health identity
verification and operation-scoped cleanup steps. A retry recognizes durable
progress and the two activation acknowledgement windows: service already on
the admitted target before record creation, and exact target record already
written before the `ACTIVATED` journal event.

After durable verification, a later resume may find that operation-scoped
cleanup has already removed the staged wheel. It then reopens the digest-bound
candidate evidence and accepts only the exact target record and service for the
same operation; it does not reactivate or reconstruct legacy provenance.

## Recovery and authority boundaries

Initial quiescence requires a live `launchctl` readback that exactly matches
the admitted interpreter, label, arguments, working directory and data root.
The executor journals `QUIESCING` before it changes the persistent boot policy
or stops that service. A restart from that durable intent may accept true
service absence; an unreadable service state or a different same-label binding
still fails closed.

Activation validates the selected package version against the immutable source
plan before quiescence and validates the exact target binding again after
bootstrap. If the admitted old job races the first bootstrap after the plist
has already selected the target, a resume may unload only that exact old
binding and retry the target. It never unloads or adopts an unknown same-label
job. A target record written before the `ACTIVATED` journal event and a staged
wheel cleaned after `VERIFIED` are accepted only through their existing exact,
durable operation evidence.

The compatibility boundary remains a macOS per-user LaunchAgent. Qualification
simulates only the external service/process edges; it does not claim a
LaunchDaemon, live adoption, live schema migration, publication, rollback from
the old wheel after migration, merge authorization or owner authorization.

## PR #224 qualification handoff

The exact final review head and test head are the final PR head recorded in the
PR description. The indirection for the review head is intentional: a commit
cannot contain its own identifier. The exact full-suite test head is
`6c9a2d3301ccffe0c2b5a4f6e59ee1f91eb5a112`; any later final-head difference
is restricted to this evidence-only handoff correction.

- Release candidate: `2.3.51`.
- Wheel: `engineering_platform-2.3.51-py3-none-any.whl` (3,281,254 bytes).
- Wheel SHA-256: `5ce6040502d4f204b657e975b18804b02c0a063fe9ef2717a235f2b931eb05cd`.
- Production-wheel contents: 155 allowlisted members and no runtime
  dependencies.
- Installed full suite: 1,702 tests passed; 132 measured modules, none below
  80.20%; aggregate branch-aware coverage 84.61%.
- `installation_update_activation.py`: 81.69014084507042% branch-aware
  coverage.
- Installed new-process legacy chain: six tests passed through public
  inspection, authorization, planning, preparation, admission, apply/resume,
  status and durable reopening.
- Focused update/service chain: 119 tests passed with one expected source-only
  skip. Dashboard logic: 40 tests passed. Installed ingress, deterministic
  execution, HTTP/OpenAPI/Postman, route ownership and five-locale UI guards
  each reported `PASS`.

R224-1 is closed by the installed public chain and production composition;
R224-2 is closed by the owning not-found classification plus corrupt,
unreadable and dangling-link rejection; R224-3 is closed by byte-preserving
historical plan reopening with its original digest. The PR remains
review-ready only: publishing, merging and live activation are separate,
explicitly authorized operations.
