# Genesis macOS selected physical Git

Assignment `L2-GENESIS-MACOS-PROCESS-GROUP-CLOSURE-V1-20261019`;
JOIN `L2-L3-GENESIS-APPLY-HOST-QUALIFICATION-V1-20261019`.
Product allocation EP2.3.124, CENTRAL77, storage45, bootstrap contract1.0.
The earlier EP2.3.123 delivery and its evidence remain closed and unchanged.

## Supported host mode

`MACOS_OS_SELECTED_PHYSICAL_GIT_V1` uses the physical Git selected by the
existing root-owned `/var/db/xcode_select_link`. It accepts an executable under
`/Applications` or `/Library/Developer` only. Binary and ancestor ownership,
permissions, no-follow regular-file identity and bounded SHA256 reads are
checked. Root and the current trusted installation owner are supported;
non-root ancestors must not be group/world writable. No consumer supplies an
executable path, and there is no PATH or shim fallback for this macOS mode.

The trusted installation-owner/system-administrator boundary is explicit.
Before/after hashes and inode metadata are drift checks; they are not an atomic
exec binding against an administrator or the same trusted owner replacing and
restoring executable bytes during execution. Unsupported or changed host
selection is an explicit error, not permission to silently replace a pin.

The controller records the exact operation/host-bound selection in the private
signed attempt before native execution. PREFLIGHT actually runs fixed
`--version` inside the original native sandbox before target creation. Every
controller step checks the selection; every Git command checks its binary.
The worker does not read the selector through a new sandbox grant. Original
five-second Git and thirty-second step deadlines, sanitized environment,
finite filesystem capabilities, permit handshake and whole-group detector
remain in force. Linux retains its existing Git behavior and rejects a
macOS-mode pin rather than treating it as authority for a fallback command.

## Original-operation recovery

Explicit resume can add a new PREFLIGHT attempt to a signed legacy operation
which had completed native readiness before this mode existed. Earlier
attempts and completed-step history are retained; the completed prefix is not
duplicated. A failed current-mode pin remains binding and cannot be repinned.
Read/status never performs this migration or native work. COMPLETE remains the
original result. Current authority, source identity and original scopes are
required at their existing effect boundaries, including before recovery.

An interrupted Git initialization can reuse only the originally granted Git
metadata inode, literally empty and still bound to the same target and exact
documents. The held execution descriptor is checked after the last runtime
hash and immediately before Git. Foreign or unproved nonempty metadata remains
preserved and refused; no delete, adoption, reset or replacement target is
authorized. Existing proved initialized Git follows its original guard.

Private stdout/stderr and timeout bytes are retained within finite limits.
Overflow is failure, including successful Git with truncated stderr. Log I/O
failure cannot hide a primary nonzero exit, timeout or overflow; both errors
remain available. No public receipt exposes the private executable path,
credential or raw diagnostic details.

## Evidence boundaries

The original macOS14 shim chain reproduced a five-second GIT_INIT timeout with
live bash/xcodebuild members in the recorded group. The unchanged detector
correctly rejected that group. Later same-shim and reverse-order controls also
succeeded: a unique cache/warming cause is not established. Physical Git is an
explicit supported host mode, not a universal causal claim or a timeout waiver.

New candidate installed evidence on local macOS27 and hosted macOS14 includes
actual native steps, committed documents, B8R, lost response, a real Server
crash/restart, nonmutating GET, separately authorized resume and replay. These
candidate receipts retain their own source/wheel pins. The legacy installed
migration uses a current controlled WRITE_FILES crash; it does not reconstruct
the unavailable historical hosted target or demonstrate its old Git failure.
Component fault injection and modeled PID-reuse controls remain labeled as
such. Candidate evidence is not exact-final-main proof or Forge R57 acceptance.

Full owning CI, independent final reviews, protected source and separate
nonempty Finalization, NEW exact-final-main installed evidence, readback and
safe own cleanup are delivery gates. R57's writer/PR/timer/budgets remain
untouched until an actually qualified owning handoff; no automatic successor.
