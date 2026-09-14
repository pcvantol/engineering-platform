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
