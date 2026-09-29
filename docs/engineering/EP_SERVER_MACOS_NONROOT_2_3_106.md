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

Native foreground/API and adapted lifecycle requalification of these
**published 2.3.106 bytes** is pending. The lifecycle rerun must start from
an intentionally inaccessible, assignment-owned `0700` directory and prove
that the non-root process cannot call `os.getcwd()` there before the product
creates its runtime slot. No checkout import, `Requires-Python` bypass,
mocked health response, macOS service installation, reboot or live provider
authentication is acceptable as positive evidence.
