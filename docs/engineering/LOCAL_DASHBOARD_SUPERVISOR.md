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

## Local status, reports and logs

The dashboard reads only canonical local Engineering Platform state. It does
not need iCloud Drive to render a current or completed run:

- `.djconnect/status/status.json` supplies bounded watcher status;
- `.djconnect/reports/` supplies the Engineering Report and its advisory
  analysis for the matching terminal run; and
- `.djconnect/logs/dashboard.log` and `.djconnect/logs/inbox.log` supply
  bounded, redacted component-log tails after the maintainer explicitly chooses
  **Applicatielogs → Logs laden**.

iCloud Drive is solely an Inbox transport source for the separate watcher.
The dashboard does not read iCloud reports, status or archived prompts.

The page receives status changes through server-sent events. A browser refresh
remains safe, but periodic polling is not the source of truth. The dashboard
also shows the Engineering Platform, watcher, dashboard and build-commit
versions so a maintainer can distinguish a stale local service from a stale
browser page.

## Private read-only Codex advice

**Codex gesprek** is available only through this same private listener. Its
bounded context is the repository identity, the latest terminal prompt and
the matching local Engineering Report. It starts an ephemeral, read-only Codex
CLI process and cannot inspect or submit Inbox files, modify a repository,
create or merge pull requests, or trigger release, deployment or publication.
Any requested implementation must be submitted as a new Engineering prompt.
