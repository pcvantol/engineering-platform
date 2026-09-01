# Engineering Platform development extension

Engineering Platform retains source/package architecture, phase status,
standalone validation, store/schema architecture, installer/release planning,
and the rule that no source checkout becomes runtime authority.

Engineering Platform owns the EP Server artifact, EP Project Agent artifact,
EP-specific packaging, clean-store/bootstrap behavior, service/runtime
installation contract, and the future EP Local Project Agent API contract. One
EP Project Agent may serve multiple locally attached repositories for one
Host/OS-user context. This is an authority boundary only; it does not implement
the Agent or its protocol.

Forge Platform owns universal installer UX, cross-product role selection,
artifact composition, compatibility declarations, update/repair/uninstall UX,
and deployment topology. EP-specific packaging and service installation do not
make EP the universal installer authority.
