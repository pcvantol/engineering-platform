"""Fail-closed, serialized local iCloud Engineering Inbox watcher."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .platform_version import EngineeringPlatformManifest
from .status_model import build, publish

LABEL = "com.djconnect.engineering-inbox"
WATCHER_VERSION = "1.0.0"
MAX_BYTES = 256_000
TERMINAL_PHASES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


def cloud_root(value: str | None = None) -> Path:
    """Return the per-user iCloud workspace without hard-coding a username."""
    default = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/DJConnect Engineering"
    return Path(value or os.environ.get("DJCONNECT_ENGINEERING_INBOX") or default).expanduser()


def folders(root: Path) -> dict[str, Path]:
    result = {name: root / name for name in ("Inbox", "Running", "Reports", "Completed", "Failed")}
    for path in result.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return result


def stable_prompt(path: Path, interval: float = 1.0) -> str | None:
    """Accept only stable, bounded, UTF-8 text files from the direct Inbox."""
    if (
        path.name.startswith(".")
        or path.suffix.lower() not in {".txt", ".md"}
        or path.is_symlink()
        or not path.is_file()
    ):
        return None
    try:
        before = path.stat()
    except OSError:
        return None
    if not 0 < before.st_size <= MAX_BYTES:
        return None
    time.sleep(interval)
    try:
        after = path.stat()
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return value if value.strip() and "\0" not in value else None


def discover(root: Path, interval: float = 0.0) -> list[Path]:
    inbox = folders(root)["Inbox"]
    return [
        path
        for path in sorted(inbox.iterdir(), key=lambda item: (item.stat().st_mtime_ns, item.name))
        if stable_prompt(path, interval) is not None
    ]


def _safe_detail(value: object) -> object:
    if isinstance(value, str):
        return value[:500].replace("\n", " ")
    return value


def status(root: Path, state: str, **details: object) -> None:
    """Publish bounded atomic iCloud status without prompt or command output."""
    manifest = EngineeringPlatformManifest.load(
        Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json")
    )
    payload = build(
        manifest,
        watcher_state=state,
        job_id=details.get("job_id"),
        run_id=details.get("run_id"),
        queue_depth=details.get("queued_jobs", 0),
        current_phase=details.get("runner_phase"),
        current_action=details.get("current_action"),
        implementation_pr=details.get("implementation_pr"),
        finalization_pr=details.get("finalization_pr"),
        latest_report=details.get("report"),
        diagnostic=_safe_detail(details.get("diagnostic")),
        owner_authorized=state in {"RUNNER_STARTING", "JOB_CLAIMED"},
        resume_available=state in {"JOB_BLOCKED", "JOB_FAILED"},
    )
    publish(root, payload)


def _job_id(source: Path, content: str) -> tuple[str, str, str]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    job_id = f"{source.stem[:32]}-{digest[:12]}"
    return job_id, f"inbox-{digest[:16]}", digest


def _archive_path(area: Path, job_id: str, source: Path) -> Path:
    return area / f"{job_id}__{source.name}"


def _already_seen(areas: dict[str, Path], job_id: str) -> bool:
    return any(
        next(areas[name].glob(f"{job_id}__*"), None) is not None
        for name in ("Running", "Completed", "Failed")
    )


def _active_transaction(repo: Path) -> bool:
    current = repo / ".djconnect" / "status" / "current.json"
    try:
        payload = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("phase") not in TERMINAL_PHASES


@contextmanager
def _lock(repo: Path):
    """Use an exclusive local lock and recover only a proven stale PID lock."""
    path = repo / ".djconnect" / "engineering-inbox.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        try:
            owner = int(path.read_text(encoding="utf-8").strip())
            os.kill(owner, 0)
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        else:
            raise RuntimeError("another watcher instance owns the local inbox lock") from error
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _runner_result(repo: Path, run_id: str) -> tuple[str | None, str | None]:
    checkpoint = repo / ".djconnect" / "engineering-runs" / f"{run_id}.json"
    try:
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    return state.get("phase"), state.get("diagnostic")


def _report(repo: Path, run_id: str) -> Path | None:
    reports = sorted((repo / ".djconnect" / "reports").glob(f"*_{run_id}.md"))
    return reports[-1] if reports else None


def once(repo: Path, root: Path, interval: float = 1.0) -> int:
    """Process at most one stable job; all repository mutations remain runner-owned."""
    areas = folders(root)
    with _lock(repo):
        candidates = [(path, stable_prompt(path, 0.0)) for path in discover(root, interval)]
        candidates = [(path, content) for path, content in candidates if content is not None]
        if not candidates:
            status(root, "WATCHER_IDLE", queued_jobs=0)
            return 0
        if _active_transaction(repo):
            status(
                root,
                "WAITING_FOR_REPOSITORY",
                queued_jobs=len(candidates),
                diagnostic="An existing engineering transaction remains active.",
            )
            return 0
        source, content = candidates[0]
        job_id, run_id, digest = _job_id(source, content)
        if _already_seen(areas, job_id):
            status(
                root,
                "WATCHER_IDLE",
                queued_jobs=len(candidates) - 1,
                job_id=job_id,
                diagnostic="Duplicate job digest remains recorded.",
            )
            return 0
        claimed = _archive_path(areas["Running"], job_id, source)
        status(root, "JOB_CLAIMED", queued_jobs=len(candidates) - 1, job_id=job_id, run_id=run_id)
        os.replace(source, claimed)
        local = repo / ".djconnect" / "inbox-processing" / job_id
        local.mkdir(mode=0o700, parents=True, exist_ok=True)
        prompt = local / "prompt.md"
        prompt.write_text(content, encoding="utf-8")
        (local / "job.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "filename": source.name,
                    "digest": digest,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        phase, _ = _runner_result(repo, run_id)
        arguments = [
            str(repo / "tools/engineering/dj-engineer"),
            str(prompt.relative_to(repo)),
            "--owner-authorized",
            "--run-id",
            run_id,
        ]
        if phase and phase not in TERMINAL_PHASES:
            arguments.append("--resume")
        status(
            root, "RUNNER_STARTING", job_id=job_id, run_id=run_id, queued_jobs=len(candidates) - 1
        )
        completed = subprocess.run(arguments, cwd=repo, text=True, capture_output=True, check=False)
        phase, diagnostic = _runner_result(repo, run_id)
        report = _report(repo, run_id)
        delivered = None
        if report:
            status(
                root,
                "REPORT_PUBLISHING",
                job_id=job_id,
                run_id=run_id,
                queued_jobs=len(candidates) - 1,
            )
            delivered = (
                areas["Reports"]
                / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{job_id}_{run_id}.md"
            )
            shutil.copy2(report, delivered)
        successful = completed.returncode == 0 and phase == "COMPLETE" and delivered is not None
        target = areas["Completed"] if successful else areas["Failed"]
        os.replace(claimed, _archive_path(target, job_id, source))
        final_state = (
            "JOB_COMPLETED"
            if successful
            else ("JOB_BLOCKED" if phase == "BLOCKED" else "JOB_FAILED")
        )
        reason = diagnostic or (
            "Engineering report was not available for delivery."
            if completed.returncode == 0
            else "Runner ended without a safe terminal report."
        )
        status(
            root,
            final_state,
            job_id=job_id,
            run_id=run_id,
            queued_jobs=len(candidates) - 1,
            runner_phase=phase,
            report=str(delivered) if delivered else None,
            diagnostic=reason,
            resume_instruction=f"Run dj-engineer with --run-id {run_id} --resume.",
        )
        return 0 if successful else (completed.returncode or 1)


def launch_agent(repo: Path) -> Path:
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = repo / ".djconnect" / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    launcher = [sys.executable, "-m", "tools.engineering.inbox_watcher", "run", "--repo", str(repo)]
    arguments = "".join(f"<string>{value}</string>" for value in launcher)
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer><key>StandardOutPath</key><string>{logs / "inbox.out.log"}</string><key>StandardErrorPath</key><string>{logs / "inbox.err.log"}</string></dict></plist>',
        encoding="utf-8",
    )
    return destination


def doctor(repo: Path, root: Path) -> int:
    areas = folders(root)
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    checks = {
        "repository_runner": (repo / "tools/engineering/dj-engineer").is_file(),
        "inbox_writable": os.access(areas["Inbox"], os.W_OK),
        "reports_writable": os.access(areas["Reports"], os.W_OK),
        "launch_agent": agent.is_file(),
        "gitignored": ".djconnect/" in (repo / ".gitignore").read_text(encoding="utf-8"),
    }
    state = "REMOTE_ENGINEERING_READY" if all(checks.values()) else "REMOTE_ENGINEERING_DEGRADED"
    print(
        json.dumps(
            {
                "state": state,
                "watcher_version": WATCHER_VERSION,
                "inbox": str(areas["Inbox"]),
                "reports": str(areas["Reports"]),
                "checks": checks,
            },
            indent=2,
        )
    )
    return 0 if state == "REMOTE_ENGINEERING_READY" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("once", "run", "status", "install", "uninstall", "doctor")
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--icloud-root")
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args(argv)
    repo, root = args.repo.resolve(), cloud_root(args.icloud_root)
    if args.command == "once":
        return once(repo, root, 0.0)
    if args.command == "run":
        while True:
            try:
                once(repo, root, 1.0)
            except RuntimeError:
                status(
                    root,
                    "WAITING_FOR_REPOSITORY",
                    diagnostic="Another watcher owns the local inbox lock.",
                )
            time.sleep(max(5, args.interval))
    if args.command == "status":
        print(
            (root / "status.md").read_text(encoding="utf-8")
            if (root / "status.md").exists()
            else "WATCHER_IDLE"
        )
        return 0
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
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
    return doctor(repo, root)


if __name__ == "__main__":
    raise SystemExit(main())
