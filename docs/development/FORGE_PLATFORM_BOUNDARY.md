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

Historical EP documents that describe a native installer are read as the
EP-specific packaging/service-installation contract. They do not grant EP
authority over a future universal Forge Platform installer. No installer or
Agent behavior is implemented by this boundary document.
