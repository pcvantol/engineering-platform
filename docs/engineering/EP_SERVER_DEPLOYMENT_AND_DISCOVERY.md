# EP Server deployment and discovery target

**Status:** Canonical deployed-runtime architecture. The system-domain
multi-instance implementation and installed-artifact qualification are defined
by [EP Server system-domain multi-instance runtime and provisioner v1](EP_SERVER_SYSTEM_MULTI_INSTANCE_V1.md).

EP Server remains the sole CENTRAL execution/admission, queue, evidence and finalization authority. It is a headless installed service with an EP-owned central runtime-storage root outside Git/source checkouts: its SQL CENTRAL store plus product-owned files, artifacts, logs, backups and cache. Its versioned HTTP ingress remains a transport adapter over interface-neutral application services. macOS lifecycle is product-owned launchd service lifecycle. This target does not broaden EP execution authority, grant Forge or Workspace SQL access, or permit either peer to bypass admission.

Every EP Server has a stable opaque instance identity. Multiple independently
owned system-domain instances may coexist on one Mac; endpoint, LaunchDaemon,
service account, CENTRAL, lifecycle lock and provider contexts are therefore
instance-scoped rather than singleton product state. LAN DNS-SD/mDNS and
configured/unicast/tailnet endpoints discover only candidates. Authenticated
pairing/binding validates EP identity and stores an explicit product-owned peer
or Agent binding; discovery does not authenticate/authorize and cannot silently
retarget a binding. EP Project Agent↔EP Server host trust remains distinct from
Forge/Workspace server-peer trust and Workspace Client user/session trust,
including on the same host.

The shared descriptor and pairing semantics are defined by Forge Platform's [instance contract](https://github.com/pcvantol/forge-platform/blob/main/docs/architecture/INSTANCE_DISCOVERY_AND_PAIRING_CONTRACT.md). EP owns its credential, Agent re-pair/rebind, CENTRAL migration and recovery semantics. Forge Platform orchestrates installation and invokes EP APIs; it does not write CENTRAL.

Forge Platform selects an exact published EP artifact and opaque instance, then
invokes the EP-owned provisioner. It does not construct a plist, runtime,
provider context, migration or cleanup plan. A real fresh-Mac cold boot and
provider-login fan-out remain joint installer acceptance; source qualification
does not claim that live-host result.
