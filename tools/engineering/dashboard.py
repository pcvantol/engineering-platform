"""Private read-only Engineering Status dashboard; no transaction authority."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import os
from pathlib import Path
import subprocess
import sys

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
            if self.path == "/":
                return self._send(
                    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>DJConnect Engineering</title><style>body{margin:0;background:#121217;color:#f7f3ee;font:16px system-ui;padding:20px}pre{white-space:pre-wrap;background:#24242d;border-radius:14px;padding:16px;color:#d9c7ff}</style><h1>DJConnect Engineering</h1><pre id="s">Loading</pre><script>fetch("/api/status").then(r=>r.json()).then(x=>s.textContent=JSON.stringify(x,null,2)).catch(()=>s.textContent="Status unavailable")</script>'.encode(),
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
    print(f"REMOTE_ENGINEERING_{'READY' if health and agent.is_file() else 'DEGRADED'}")
    return 0 if health and agent.is_file() else 1


if __name__ == "__main__":
    raise SystemExit(main())
