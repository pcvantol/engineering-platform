# EP Server 2.3.105 macOS non-root qualification

This is the bounded EP-owned evidence for LANE_2 registration revision 27,
assignment `L2-EP-NONROOT-RUNTIME-CONFORMANCE-V1-20260929`. The authoritative
operator record is [Forge #142](https://github.com/pcvantol/forge/issues/142).
The existing OI-2 lifecycle and OI-5 service/readiness nodes remain the owning
roadmap subset. None of the repository's machine-readable DAG files defines
those node IDs, so this qualification adds no parallel DAG or invented node.

## Exact released artifact

| Field | Readback |
| --- | --- |
| Release | `engineering-platform-v2.3.105`, `RELEASE_COMPLETE` |
| Release source | `ad44263f6ec87ea018cda11f053fa12521ae9d79` |
| Published wheel SHA-256 | `22dd1e49c263b55dc9eee396810a09fc43509984fe685f3c00d26289d55e8adc` |
| Release receipt SHA-256 | `0b3ebeaef44c1af7808e4bdccf88bedf555e3a94e4ba5b30e1f26fab04eda1fb` |
| Interpreter | macOS arm64, Python 3.14.7; wheel requires `>=3.14,<3.15` |
| Actual Server store schema | 72 from the released source, installed module and live API |

The registration's `EP_STORAGE_SCHEMA=68` does not match the released Server
store schema 72. The wheel and release receipt digests match exactly; the
schema discrepancy is retained as a registration correction, not applied to
the published bytes or live response.

## Native foreground matrix

The locally admitted disposable account had EUID/EGID 20000, no administrator
or remote-login group, SecureToken disabled and no APFS/FileVault cryptographic
membership. A real process under that UID loaded `engineering_platform`
from a noneditable venv outside the checkout. The process inventory showed
the macOS Python 3.14.7 app executable, and the real loopback HTTP response
reported its own instance ID, version 2.3.105 and observed exact wheel digest.
The test used a scrubbed environment with explicit HOME and PATH; macOS's
`__CF_USER_TEXT_ENCODING` injection was the only allowed extra variable.

| Case | Result and boundary |
| --- | --- |
| Product initialization and A/B | Distinct disposable identities and simultaneous real HTTP servers; logical instance separation under the same UID |
| Expected identity and release | Real HTTP 200, same expected instance, version and observed wheel digest |
| Wrong expected instance | `EP_SERVER_INSTANCE_ID_MISMATCH` and nonzero exit |
| Scoped API | A product-created disposable consumer received authenticated v1.1 identity/readback; absent bearer returned 401 and wrong project scope 403. The token stayed local and was never printed in evidence |
| Filesystem | Test UID could not write the installed package; a non-writable own data parent was rejected without global permission repair |
| Cross-UID | A synthetic 0600 file owned by another already available UID was unreadable by the test UID; this is not a second EP instance |
| Provider absence | Codex and GitHub reported `UNAVAILABLE`, with no overall provider READY |
| Normal restart | Same instance, release and scoped API identity after exact-child stop/start; provider status remained `UNAVAILABLE` |
| Cleanup | Exact child stopped; independent process and listener readback found no own server |

These are real Server/process/API observations. They do not establish a
LaunchDaemon, physical reboot, live provider login, service-account installer,
production instance or full installer acceptance. The user environment was
not borrowed. No production credential, Keychain or other lane resource was
read or changed.

## Reproduction

`tools/qualification/ep_nonroot_macos_r27.py` takes explicit absolute paths
for the account's private test home, downloaded wheel, installed noneditable
venv and a synthetic 0600 other-UID fixture. Run it with that venv's Python
`-I` after an operator-approved, local role drop and `env -i` with only
`HOME`, `PATH`, `LANG`, `LC_ALL`, `TMPDIR` and
`PYTHONDONTWRITEBYTECODE`. Pass `--account`, `--expected-uid`, `--home`,
`--wheel`, `--venv` and `--synthetic-sibling-fixture`. The harness itself
must never run as root, use a checkout import as EP authority or bypass
`Requires-Python`. It creates only disposable data in the specified home and
supervises its exact Server children.

`tools/qualification/ep_nonroot_lifecycle_macos_r27.py` uses the same
published wheel and an instance-owned product root. It writes a service plist
only inside its disposable launch stand-in directory, never in macOS system
service directories. The provider executables deliberately fail and their
synthetic records serve only to exercise preserve/restore state transitions;
they do not qualify a provider login. The controller starts a real initial
foreground Server and the product's own live health verifier checks it.

Both repository harnesses were rerun under the admitted UID/GID 20000 with
the unchanged published wheel. The foreground/API harness returned
`PASS_WITH_DECLARED_LIMITS`, including independent live process and HTTP
readback, scoped authentication, HOME isolation and exact-child stop. The
lifecycle harness returned `PASS_ADAPTED_LIFECYCLE_WITH_REAL_INITIAL_SERVER`;
its service and provider limits remain as described below.

## Lifecycle boundary

The first native lifecycle attempt under UID 20000 stopped during the
product-owned immutable runtime-slot installation, before preserve/restore.
The actual pip traceback showed `PermissionError` at `os.getcwd()` because
the released provisioner inherited its caller's inaccessible working
directory. The pending source correction runs both venv creation and wheel
installation from the account-owned runtime slot. The exact 2.3.105 wheel is
unchanged; its lifecycle rerun used an accessible disposable working directory.

That rerun executed the released wheel under UID/GID 20000 with a real initial
foreground Server and a filesystem-only service adapter. Product preserve
reported `UNINSTALLED_DATA_PRESERVED`; the server was stopped and the original
identity and data stayed intact. Both deliberately failing synthetic provider
records were rejected after preserve. Product restore reported
`RESTORED_REQUIRES_PROVIDER_REVERIFICATION`, `ready=false`, and did not load
the service or generate fresh provider authentication. This is an adapted
lifecycle PASS, not a macOS system-service or live-provider-login PASS.
