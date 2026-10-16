# Genesis local bootstrap V1 — selected first slice

Owner L2: `L2-GENESIS-LOCAL-BOOTSTRAP-V1-20261016`; JOIN `L2-L3-GENESIS-LOCAL-BOOTSTRAP-V1-20261016`.
Status: implemented local candidate contract; NOT yet delivered or fully qualified. Base EP2.3.122/bc441c0. Own product allocation EP2.3.123/CENTRAL77, subject to canonical consistency checks. Closed predecessors and L4's qualified progresspin remain unchanged.

## Common meaning and transport

Only CREATE_GENESIS_LOCAL / GENESIS / LOCAL_POSIX / portable-docs/1.0.0 / DOCUMENT_ONLY / NO_PRODUCT_CODE, one NEW authority repository, before-state ABSENT. Existing/unborn/adopt/child/remote/Managed/promotion are typed refusals. Proposed identities come from original exact B8R declaration, never target name; reserve conflict-free before filesystem effects, canonically register only after original committed declaration/tree inspection.

Candidate routes (not historical generic inboxes):
- POST `/v1/bootstrap/genesis/operations`: admit original request and execute bounded steps.
- GET `/v1/bootstrap/genesis/operations/{operation_id}`: original scoped readback only; never resume.
- POST `/v1/bootstrap/genesis/operations/{operation_id}/resume`: explicit same-original-operation recovery under current original authority; no new target/commit/attachment.

Closed request: `contract_version=1.0`, `operation_id`, opaque `target_ref`, `effect_plan_binding`, exact original `bundle`. `effect_plan_binding` contains `plan_id`, integer `plan_revision`, `plan_digest`, `decision_id`, `decision_digest`, `proposal_id`, `proposal_digest`, `source_instance_id`, `source_actor_id`, `context_id`. The exact Forge plan is owned by L3; EP consumes these immutable references and independently admitted original bundle, not a second plan renderer or assumed Forge approval. EP-owner qualification uses explicitly synthetic references, never Peter's approval. Original forwarded actor is provenance, not authenticated EP actor.

`bundle` retains the R50 object (`contract_version=pb-render/1.0`, `kind=RENDERED_PROPOSAL_EXAMPLE`, finite `files` map of exact sha256/utf8 payloads). No new mode/identity fields inserted into the original declaration. Every payload must match its digest and manifest, modes0644. Manifest excludes itself and computes digest without `manifest_digest`. Digests of JSON objects use sorted compact UTF8 (ensure_ascii=False/allow_nan=False) plus LF, matching owning R50 canonical bytes. No clock/random/rerender on replay.

Headers: separate finite `Authorization: Bearer` with purpose CREATE_GENESIS_LOCAL; `EP-Bootstrap-Contract:1.0`, `EP-Instance-ID`, `EP-Host-ID`, `EP-Consumer-ID`, `EP-Principal`, `EP-Audience:genesis-bootstrap`. All exact scope values are bound by explicit private installation-owner issue, not free header authority. Existing read/inspection/discovery/pairing/submission credentials are not fallback. No credential in Client, argv, artifacts, logs or public evidence.

Owner initialize/issue/status/revoke and credential-stdin read/apply/resume CLI parity call the same services. Owner scope binds actual instance/host/consumer/principal, same operation/request/plan/proposal/decision/bundle/manifest/input/validator digests, proposed IDs, placement/target refs and finite effects/limits/expiry. Trusted private placement maps target_ref to one literal direct-child name under a no-follow owner-allowed root. HTTP never accepts root/path/validator command. Independently trusted validator bytes must match externally qualified R50 pin; no trust from in-bundle self-check alone. Product requests carry no arbitrary command.

## Exact original interoperability fixture

Pinned Forge source `cd483fd9b17a96d0deac0f0264ab6fc32c75e778`, owning R50 bundle `examples/project-bootstrap/authority-bundle.json`.
- Bundle canonical SHA256 `0c2c0fb41a68793be0960bb4289d7590060c8092ebb976852e1c4674b0f66b1d`.
- Manifest `d03a573b7752838720a1c0509950a4b92e36a098e97c5f5cc3c13c40ad01556a`; input `f5660a96893b9aec6b87fad6d66c9ac08d42e8b0d77d937893e87ada6753aaf7`.
- Renderer source `a7a16db4d735d9411ff3a3c7997d8aff82be2d4b6f2059aa56bf250b7e104536`; validator `3ce4eca67c659d65ea2df70439d7f61bc8e4eb16b3d4b64b397d7fc3bdc16ae2`.
-37 files /127700 payload bytes, existing fixed declaration: project sample-product, authority sample-core, repository sample-core/authority.
First actual isolated direct validator probe returned VALIDATED_PROPOSAL/37 and NOT_APPLICABLE_NO_PRODUCT_CODE; this is document validation, not installed bootstrap/Git/B8R proof.

## Effect and recovery boundary

Persist exact admitted intent and reserve operation/target/proposed IDs before effects. Reuse native host sandbox, original local Git and B8R topology/local-attachment primitives. Existing execution_run_leases require a real engineering transaction and cannot be populated with a fictitious Mission/run; bootstrap resource ownership belongs to its explicit unique operation reservation with OS-process ownership, not a new scheduler or fake run.

All state and security rows are in owning CENTRAL77 with migration/reset/retention fences. Noncreating runtime access; missing/corrupt initialized stores fail explicitly. Private original stdout/stderr/exit/timeout evidence stays outside public receipts. No business action/model/Mission/queue/remote/network/signing/dependency installation.

Steps: RESERVED → CREATE_TARGET → WRITE_EXACT_FILES → TRUSTED_VALIDATE → LOCAL_GIT_COMMIT → VERIFY_COMMITTED_DECLARATION → REGISTER_AND_ATTACH → COMPLETE. Per-step durable intent and actual identities, fresh authority/expiry and source/target checks at each effect boundary, no network wait under authority lock. Same operation+same bytes gives same record; changed bytes conflict. Current grant revocation forbids new effects, not historical erasure or rollback. GET never repairs.

No-follow descriptor-based writes, bounded paths/UTF8/modes/digests and case/Unicode/file-directory collision rejection; guard parent/target physical identity, foreign creation, nested/host repositories and existing attachment bindings. Exclusive create, no overwrite/adoption after a race. Known own partial effects may be reconciled from original durable identities; a crash between filesystem creation and durable ownership evidence remains EFFECT_UNCERTAIN, never adopted silently. Original local commit/ref/tree is inspected before continuation, never another commit or reset/force/history rewrite. No automatic directory compensation/deletion.

Safe closed receipt identifies original operation/request/plan/decision/proposal/bundle/manifest, proposed→reserved→committed→registered IDs distinctly, actual step outcomes, commit/tree/B8R/attachment references and bounded AS_OF. PARTIAL/EFFECT_UNCERTAIN is not COMPLETE. Readiness/Forge Mission acceptance NOT_OWNED_BY_EP. Remote governance NOT_APPLICABLE_GENESIS and product coverage NOT_APPLICABLE_NO_PRODUCT_CODE. No human businessapproval implied.

Small actual noneditable installed ownergrant→HTTP→37 exact files→real trusted sandbox validator→real local Git commit→B8R→identical replay precedes full qualification. Main refusals and separate actual OS-crash recovery at creation/commit/registration, lost acknowledgements, no duplicate effects and real fault-restoration detector must pass before full waves. Full mandatory owning regression/coverage, independent Quality/Security, required profileCI/OwnerAuth, protected implementation and own nonempty reviewed/protected Finalization, NEW exact-final-main HTTP/Git/B8R/restart proof and safe proof-preserving own cleanup remain required. No GUI/installer/general peerACK gate or automatic next feature.

## Current implementation constraints (not delivery qualification)

The owning EP installation-owner provisioning step prepares one private empty
staging-directory capability before runtime admission. Its exact inode and
parent identity are bound in the finite signed grant. CREATE_TARGET publishes
that capability with Darwin renameatx_np(RENAME_EXCL), using held descriptors;
existing targets, changed capabilities and cross-filesystem placement refuse.
There is no mkdir-and-adopt, copy, delete or overwrite fallback. Unknown owner
preparation leftovers remain private evidence and are not adopted on recovery.
Native rights remain finite to the selected staging capability and target.

Git blob/tree plumbing consumes the original admitted bytes with --no-filters;
it does not evaluate target clean filters or use git add. The original local
root commit is deterministic for the original accepted operation. Actual full
commit verification binds a finite metadata inode/content snapshot, checked
before and after the existing B8R helpers in the same CENTRAL transaction with
fresh authority. Any late drift rolls back those registration writes.

These source repairs do not establish installed/full scoped DoD PASS. Original
failed candidates, outputs and independent review reports remain preserved.

The native permission compiler has a real finite placement capacity. Private
bootstrap storage uses the short owning `g/a` anchor; the public contract never
accepts that host path. The same finite traversal policy excludes whole sibling
subtrees, not only their directory names, and explicitly retains the selected
installed runtime when it shares an ancestor. Ambiguous selected branch-name
prefixes and unsupported profile capacity refuse before physical effects.
The prepared inode is a permitted child of its owning anchor, not a separate
protected writable sandbox root. All other anchor children are denied. These
constraints require actual native qualification at the selected placement;
no parent-subtree fallback is used. Prior prototype layouts remain preserved
and explicitly incompatible with new initialization, never silently recreated.

## Current bounded Git and process implementation

Owner provisioning prepares two distinct private empty inode capabilities: the
new target and its later Git metadata directory. Both are signed into the
original finite grant before admission. The second is published with the same
no-replace primitive only after real document validation. No mkdir/open gap may
claim foreign Git metadata. Existing/unknown targets remain explicit refusals.

Fixed blob-only fast-import commands consume exact admitted bytes, with no
commit/ref/command instructions from documents. Original blob IDs are checked.
A single bounded mktree batch verifies precomputed canonical tree IDs; read-tree
initializes only the index, never the worktree. Batch cat-file proves original
committed blobs; index-versus-commit is checked independently. No target filter,
hook, auto maintenance, remote effect, reset or product-code execution occurs.
The original 30-second step deadline is retained; serial per-blob Git calls are
not allowed to justify extending it.

Live stderr is drained into a hard finite retained byte budget during upload,
protocol reads and process completion. Protocol/tail bytes are retained. Whole
process-group closure and durable private logs precede STEP_COMPLETED. Primary
and cleanup/evidence exceptions remain separate actual exception objects with
private detailed readback; any unconfirmed closure/persistence remains failure.
Final independent review and installed qualification are still required.
