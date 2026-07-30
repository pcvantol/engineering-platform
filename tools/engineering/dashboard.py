"""Private read-only Engineering Status dashboard; no transaction authority."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time

LABEL = "com.djconnect.engineering-dashboard"
DASHBOARD_VERSION = "1.0.0"


def _status(root: Path) -> bytes:
    try:
        return (root / ".djconnect" / "status" / "status.json").read_bytes()
    except OSError:
        return (
            b'{"watcher_state":"REMOTE_ENGINEERING_DEGRADED","diagnostic":"Status is unavailable."}'
        )


def handler(root: Path):
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
                    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>DJConnect Engineering</title><style>body{margin:0;background:#121217;color:#f7f3ee;font:16px system-ui;padding:max(20px,env(safe-area-inset-top)) 20px}.card{background:#24242d;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 4px 18px #0005}strong{color:#c7a6ff}</style><h1>DJConnect Engineering</h1><div class="card"><strong id="state">Loading</strong><p id="action"></p></div><div class="card" id="job"></div><div class="card" id="prs"></div><div class="card" id="repo"></div><div class="card" id="diag"></div><script>function r(x){state.textContent=x.watcher_state+" · "+(x.current_phase||"idle");action.textContent=x.current_action||"No active action";job.textContent="Run: "+(x.run_id||"none")+" · Queue: "+x.queue_depth;prs.textContent="Implementation: "+(x.implementation_pr||"none")+" · Finalization: "+(x.finalization_pr||"none");repo.textContent=x.repository_state+" · "+x.workspace_state;diag.textContent=x.diagnostic||"No diagnostic"}let e=new EventSource("/api/events");e.addEventListener("status",x=>r(JSON.parse(x.data)));fetch("/api/status").then(x=>x.json()).then(r)</script>'.encode(),
                    "text/html; charset=utf-8",
                )
            self.send_error(404)

        def log_message(self, *_: object) -> None:
            pass

    return DashboardHandler


def run(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    ThreadingHTTPServer((host, port), handler(root)).serve_forever()


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
        subprocess.run(
            ("launchctl", "bootout", f"gui/{os.getuid()}", str(agent)),
            check=False,
            capture_output=True,
        )
        subprocess.run(("launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)), check=False)
        return 0
    if args.command == "uninstall":
        subprocess.run(("launchctl", "bootout", f"gui/{os.getuid()}", str(agent)), check=False)
        agent.unlink(missing_ok=True)
        return 0
    health = (repo / ".djconnect" / "status" / "status.json").is_file()
    tailscale = shutil.which("tailscale") is not None
    connected = False
    if tailscale:
        observed = subprocess.run(
            ("tailscale", "status", "--json"), text=True, capture_output=True, check=False
        )
        connected = observed.returncode == 0 and '"BackendState":"Running"' in observed.stdout
    state = "READY" if health and agent.is_file() else "DEGRADED"
    print(
        f"REMOTE_ENGINEERING_{state}\ntailscale={'connected' if connected else ('installed_not_connected' if tailscale else 'not_installed')}\nAction: install/connect Tailscale for private remote Safari access; no network configuration was changed."
    )
    return 0 if state == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
