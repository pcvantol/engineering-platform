# Local Dashboard Supervisor

The repository-owned Engineering Dashboard is the supported macOS service for
the private status page. Its per-user LaunchAgent starts the dashboard for the
configured local repository. The dashboard itself binds port `8765` only on
loopback and, when available, the workstation's explicit Tailscale IPv4
address. It does not use a wildcard, LAN or public listener.

The historical `dashboard_supervisor.swift` companion may still relay a
Tailscale connection to a loopback backend for an existing local installation,
but it is not the canonical installation path. Use the repository-owned
dashboard commands instead:

```sh
./tools/engineering/dj-engineering-dashboard doctor
./tools/engineering/dj-engineering-dashboard install
```

The dashboard never creates public listeners, Funnel configuration, ACLs,
port-forwarding, pull requests, releases or deployments. The Inbox watcher is
a separate repository-owned LaunchAgent and has no authority beyond the normal
Engineering Platform lifecycle.
