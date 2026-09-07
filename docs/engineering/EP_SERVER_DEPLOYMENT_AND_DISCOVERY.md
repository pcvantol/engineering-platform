# EP Server deployment and discovery target

**Status:** Canonical target architecture; implementation and qualification remain separately governed.

EP Server remains the sole CENTRAL execution/admission, queue, evidence and finalization authority. It is a headless installed service with an EP-owned central runtime-storage root outside Git/source checkouts: its SQL CENTRAL store plus product-owned files, artifacts, logs, backups and cache. Its versioned HTTP ingress remains a transport adapter over interface-neutral application services. macOS lifecycle is product-owned launchd service lifecycle. This target does not broaden EP execution authority, grant Forge or Workspace SQL access, or permit either peer to bypass admission.

EP Server has a stable opaque instance identity. LAN DNS-SD/mDNS and configured/unicast/tailnet endpoints discover only candidates. Authenticated pairing/binding validates EP identity and stores an explicit product-owned peer or Agent binding; discovery does not authenticate/authorize and cannot silently retarget a binding. EP Project Agent↔EP Server host trust remains distinct from Forge/Workspace server-peer trust and Workspace Client user/session trust, including on the same host.

The shared descriptor and pairing semantics are defined by Forge Platform's [instance contract](https://github.com/pcvantol/forge-platform/blob/main/docs/architecture/INSTANCE_DISCOVERY_AND_PAIRING_CONTRACT.md). EP owns its credential, Agent re-pair/rebind, CENTRAL migration and recovery semantics. Forge Platform orchestrates installation and invokes EP APIs; it does not write CENTRAL.

The first Forge→EP→Forge canary needs the existing installed EP Server, authenticated versioned consumer HTTP ingress, stable/pinned Forge peer identity and durable restart-safe evidence. General LAN discovery, all topology profiles and installer productization are not predecessors to the canary.

