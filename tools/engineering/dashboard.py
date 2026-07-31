"""Private read-only Engineering Status dashboard; no transaction authority."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import sys
from threading import Thread
import time
from .platform_api import PlatformConfiguration
from .providers import TailscaleProvider
from .providers import LaunchdProvider

LABEL = "com.djconnect.engineering-dashboard"
DASHBOARD_VERSION = "1.1.0"
LOOPBACK_ADDRESS = "127.0.0.1"


class DashboardHTTPServer(ThreadingHTTPServer):
    """Private dashboard listener with safe restart behavior."""

    allow_reuse_address = True


def _unavailable_status() -> bytes:
    """Return the complete, safe status shape when no projection exists yet."""
    return json.dumps(
        {
            "watcher_state": "REMOTE_ENGINEERING_DEGRADED",
            "current_phase": "status unavailable",
            "current_action": "Run Engineering Platform to publish a status update.",
            "run_id": None,
            "queue_depth": 0,
            "implementation_pr": None,
            "finalization_pr": None,
            "repository_state": "UNKNOWN",
            "workspace_state": "UNKNOWN",
            "diagnostic": "No local engineering status has been published yet.",
        },
        separators=(",", ":"),
    ).encode()


def _status(root: Path) -> bytes:
    try:
        return (root / ".djconnect" / "status" / "status.json").read_bytes()
    except OSError:
        return _unavailable_status()


def handler(root: Path):
    title = PlatformConfiguration.load(root).workspace.dashboard_title
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, content: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'"
            )
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            if self.path == "/api/status":
                return self._send(_status(root), "application/json; charset=utf-8")
            if self.path == "/api/health":
                return self._send(b'{"health":"ok"}', "application/json; charset=utf-8")
            if self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for _ in range(60):
                        self.wfile.write(b"event: status\ndata: " + _status(root) + b"\n\n")
                        self.wfile.flush()
                        time.sleep(5)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/api/report/latest":
                try:
                    reports = sorted((root / ".djconnect" / "reports").glob("*.md"))
                    content = (
                        reports[-1].read_bytes() if reports else b"No local report is available."
                    )
                except OSError:
                    content = b"Report is unavailable."
                return self._send(content, "text/markdown; charset=utf-8")
            if self.path == "/":
                return self._send(
                    f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{title}</title><style>body{{margin:0;background:#121217;color:#f7f3ee;font:16px system-ui;padding:max(20px,env(safe-area-inset-top)) 20px}}.card{{background:#24242d;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 4px 18px #0005}}strong{{color:#c7a6ff}}</style><h1>{title}</h1><div class="card"><strong id="state">Loading status…</strong><p id="action"></p></div><div class="card" id="job"></div><div class="card" id="prs"></div><div class="card" id="repo"></div><div class="card" id="diag"></div><script>const $=id=>document.getElementById(id),fallback={{watcher_state:"REMOTE_ENGINEERING_DEGRADED",current_phase:"status unavailable",current_action:"Refresh the dashboard after the Engineering Platform publishes status.",queue_depth:0,repository_state:"UNKNOWN",workspace_state:"UNKNOWN",diagnostic:"The status request could not be completed."}};function r(x){{x=x&&typeof x==="object"?x:fallback;$("state").textContent=(x.watcher_state||fallback.watcher_state)+" · "+(x.current_phase||"idle");$("action").textContent=x.current_action||"No active action";$("job").textContent="Run: "+(x.run_id||"none")+" · Queue: "+(x.queue_depth??0);$("prs").textContent="Implementation: "+(x.implementation_pr||"none")+" · Finalization: "+(x.finalization_pr||"none");$("repo").textContent=(x.repository_state||"UNKNOWN")+" · "+(x.workspace_state||"UNKNOWN");$("diag").textContent=x.diagnostic||"No diagnostic"}}let e=new EventSource("/api/events");e.addEventListener("status",x=>{{try{{r(JSON.parse(x.data))}}catch{{r(fallback)}}}});fetch("/api/status").then(x=>{{if(!x.ok)throw Error("status unavailable");return x.json()}}).then(r).catch(()=>r(fallback))</script>'.encode(),
                    "text/html; charset=utf-8",
                )
            self.send_error(404)

        def log_message(self, *_: object) -> None:
            pass

    return DashboardHandler


def binding_addresses(provider: TailscaleProvider | None = None) -> tuple[str, ...]:
    """Bind only loopback and the explicit local Tailscale address.

    The dashboard deliberately never binds a wildcard, LAN, public or Funnel
    address.  Tailnet policy remains the access boundary; this code changes no
    Tailscale configuration.
    """
    tailscale_address = (provider or TailscaleProvider()).ipv4_address()
    return (LOOPBACK_ADDRESS, *(() if tailscale_address is None else (tailscale_address,)))


def create_servers(
    root: Path, port: int = 8765, provider: TailscaleProvider | None = None
) -> tuple[DashboardHTTPServer, ...]:
    """Create the exact private listeners for the dashboard."""
    request_handler = handler(root)
    return tuple(
        DashboardHTTPServer((address, port), request_handler)
        for address in binding_addresses(provider)
    )


def run(root: Path, port: int = 8765, provider: TailscaleProvider | None = None) -> None:
    """Serve locally and, when present, over the authenticated Tailnet only."""
    servers = create_servers(root, port, provider)
    for server in servers[1:]:
        Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()


def launch_agent(repo: Path) -> Path:
    """Render the only owned per-user LaunchAgent; no network policy changes."""
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = repo / ".djconnect" / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    arguments = "".join(
        f"<string>{value}</string>"
        for value in (
            sys.executable,
            "-m",
            "tools.engineering.dashboard",
            "run",
            "--repo",
            str(repo),
        )
    )
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer><key>StandardOutPath</key><string>{logs / "dashboard.out.log"}</string><key>StandardErrorPath</key><string>{logs / "dashboard.err.log"}</string></dict></plist>',
        encoding="utf-8",
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "install", "uninstall", "status", "doctor"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.command == "run":
        run(repo, port=args.port)
        return 0
    if args.command == "install":
        agent = launch_agent(repo)
        LaunchdProvider().install(LABEL, agent)
        return 0
    if args.command == "uninstall":
        LaunchdProvider().uninstall(agent)
        agent.unlink(missing_ok=True)
        return 0
    health = (repo / ".djconnect" / "status" / "status.json").is_file()
    remote_provider = TailscaleProvider()
    remote = remote_provider.status()
    tailscale_address = remote_provider.ipv4_address()
    state = "READY" if health and agent.is_file() and tailscale_address else "DEGRADED"
    action = (
        "Run Engineering Platform to publish a status update."
        if not health
        else "Connect Tailscale before using private iPhone dashboard access."
        if not tailscale_address
        else "Open the private dashboard through the local Tailscale address."
    )
    print(
        f"REMOTE_ENGINEERING_{state}\nprivate_remote_access={remote.detail}\n"
        f"tailscale_dashboard_address={tailscale_address or 'unavailable'}\n"
        f"Action: {action} No network configuration was changed."
    )
    return 0 if state == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
