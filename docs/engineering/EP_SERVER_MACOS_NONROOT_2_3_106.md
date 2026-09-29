# EP Server 2.3.106 published-wheel macOS non-root follow-up

This is the release follow-up for LANE_2 registration revision 27 and
assignment `L2-EP-NONROOT-RUNTIME-CONFORMANCE-V1-20260929`. The owning
operator record is [Forge #142](https://github.com/pcvantol/forge/issues/142).
The [2.3.105 qualification](EP_SERVER_MACOS_NONROOT_2_3_105.md) established
the original native foreground/API and adapted lifecycle boundary. This page
binds the corrected immutable release to its own registry and native evidence.

## Published artifact identity

| Field | Readback |
| --- | --- |
| Source | Protected main `7b99b578153ae5d72372a09db194306b49ec9f9c` (EP PR #325) |
| Release | `engineering-platform-v2.3.106`, `RELEASE_COMPLETE` |
| Release-complete receipt SHA-256 | `8ac24fcc41fbf41d8e30efbba1578a95599f50f1cd665d72fc96fe402d260d05` |
| PyPI wheel SHA-256 | `9d25a53d75b61d43d665d9f8290a968dc3e63d12d2037eae8ef31ee810eb6694` |
| PyPI sdist SHA-256 | `df74342e729f5f98378c40ffc35cb12b3ad53d0d4de0c602e260ff697c880058` |
| Runtime | macOS arm64, Python 3.14.7; wheel requires `>=3.14,<3.15` |

The exact-main [production release workflow](https://github.com/pcvantol/engineering-platform/actions/runs/36577447405)
completed production-wheel, dashboard, API, security, coverage, PyPI
publication, registry readback and cleanup jobs. Its terminal receipt records
`RELEASE_COMPLETE`, matching wheel/sdist registry digests and completed
cleanup. An independent direct PyPI wheel download matched the wheel digest
above, and a new noneditable Python 3.14 venv outside the checkout reported
installed version 2.3.106 from site-packages.

## Corrected boundary

The released 2.3.105 provisioner passed an inaccessible caller working
directory to its venv/pip children. Under a real non-root UID, pip failed at
`os.getcwd()` with `PermissionError` before any preserve/restore transition.
The 2.3.106 source binds both child working directories to the account-owned
runtime slot. The published wheel contains both `cwd=slot.root` call sites.

## Native published-wheel requalification

Both local rerunners used the exact PyPI wheel digest above in a new
noneditable Python 3.14.7 venv outside the checkout. The foreground/API runner
(SHA-256 `073ff8bfd8b38c52d6140bde782027a28e5c29ad6ba0dd6098b41ffe84bb1b9e`)
returned `PASS_WITH_DECLARED_LIMITS` under actual UID/GID 20000. Live process
inventory showed that UID/GID and the macOS Python 3.14 executable running
`-I -m engineering_platform.server serve`; real HTTP 200 health returned the
expected disposable instance, version 2.3.106 and `OBSERVED` exact PyPI wheel
digest. The installed package resolved from site-packages rather than source.

The product-created scoped API consumer authenticated exact instance and
consumer readback. Missing bearer, wrong scope and wrong expected instance
were rejected. Two disposable live instances retained distinct identities;
normal exact-child stop/start retained the instance, release and scoped API
identity. Missing Codex/GitHub evidence stayed `UNAVAILABLE`, not `READY`.
The test UID could not mutate the installed package or a synthetic file owned
by another UID; a nonwritable own data parent was rejected. Product config
selected the test account's private HOME, not the interactive user's HOME or
PATH. The cross-UID case used a synthetic `0600` fixture, not another EP
Server. The exact test Server children were stopped.

The adapted lifecycle runner (SHA-256
`2c31ba689e73b4e1866bb7bbc8972e7c34cf41c05ec030b5f5d022f299299f93`)
started from an assignment-owned directory with mode `0700` that was
inaccessible to UID 20000. Before product work it required `os.getcwd()` to
raise `PermissionError`; an accessible inherited cwd would have failed the
test. Despite that negative precondition, the published wheel installed the
exact runtime slot and started a real initial foreground Server. Product
preserve returned `UNINSTALLED_DATA_PRESERVED`, retained identity/data and
stopped the child. Both deliberately failing synthetic provider readbacks
were rejected. Restore returned
`RESTORED_REQUIRES_PROVIDER_REVERIFICATION`, `ready=false` and service not
loaded, with no automatic provider-auth promotion. Its result was
`PASS_ADAPTED_LIFECYCLE_WITH_REAL_INITIAL_SERVER` under UID/GID 20000.

The service adapter wrote only to the disposable testroot; it did not install
a LaunchDaemon or LaunchAgent. No physical reboot or live provider login was
performed. These results do not establish full installer acceptance. An
independent post-run process/listener check found no test-UID process or
EP/Python TCP listener; the tokenless test account was retained safely for
the bounded assignment cleanup/readback.
