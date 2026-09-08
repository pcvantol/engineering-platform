# Forge Platform boundary

Engineering Platform owns the Engineering Platform Server artifact, the
Engineering Platform Project Agent artifact, EP-specific packaging, clean-store
bootstrap, and the EP service/runtime installation contract. It also owns the
future EP Local Project Agent API contract. One Project Agent is intended per
Host/OS-user context and may attach multiple repositories.

Forge Platform owns universal distribution and installer behavior across
products: role selection, artifact composition, compatibility matrices,
deployment topology, update, repair, uninstall, and their user experience.
Forge Platform consumes qualified EP artifacts; it does not own or implement
EP execution, the Project Agent, EP protocol, or EP clean-store behavior.

## Artifact publication and cross-repository composition

EP publication and Forge Platform composition are separate repository outcomes.
Forge may plan them in one cross-repository Mission and may run independent EP
and Forge Platform implementation Actions in parallel when EP execution policy
allows it.

The final Forge Platform component-manifest Action must not guess a future EP
artifact. EP must first publish the exact installable Server/Project-Agent
artifacts and expose the evidence required by the dependency, including the
artifact identity/digest, source revision and applicable qualification or
provenance references.

Conceptually:

```text
EP publish qualified Server/Agent artifacts
   -> canonical artifact evidence
   -> Forge reconciles predecessor
   -> dependent Forge Platform manifest Action becomes eligible
```

A source merge is not a substitute for publication of the artifact bytes the
installer will consume. Forge Platform owns the resulting manifest; EP owns the
artifact and its qualification evidence. Neither product reads the other's
database directly.

The Forge-owned logical dependency and EP admission-enforcement boundary is
specified in `docs/engineering/FORGE_ACTION_DEPENDENCY_ADMISSION_BOUNDARY.md`.

For the macOS Project Agent role, Forge Platform may independently invoke the
EP-owned `install`, `uninstall`, `start`, `stop`, `restart`, and `status`
primitives in the target user's context. The exact per-user paths, label and
configuration inputs are specified in
`docs/engineering/MACOS_PROJECT_AGENT_SERVICE.md`.

Historical EP documents that describe a native installer are read as the
EP-specific packaging/service-installation contract. They do not grant EP
authority over the universal Forge Platform installer. No installer or Agent
behavior is implemented by this boundary document.
