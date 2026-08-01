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
from .agent_state import redact_diagnostic
from .platform_api import PlatformConfiguration
from .providers import LaunchdProvider
from .status_model import build, publish

LABEL = "com.djconnect.engineering-inbox"
WATCHER_VERSION = "1.0.0"
MAX_BYTES = 256_000
TERMINAL_PHASES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
LAUNCH_PATH_FALLBACK = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def cloud_root(value: str | None = None, repo: Path | None = None) -> Path:
    """Return the per-user iCloud workspace without hard-coding a username."""
    default = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/DJConnect Engineering"
    if repo is not None:
        PlatformConfiguration.load(repo)
    return Path(value or os.environ.get("DJCONNECT_ENGINEERING_INBOX") or default).expanduser()


def folders(root: Path) -> dict[str, Path]:
    result = {name: root / name for name in ("Inbox", "Running", "Reports", "Completed", "Failed")}
    for path in result.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return result


def launch_path() -> str:
    """Preserve the Codex CLI location when launchd starts the watcher."""
    codex = shutil.which("codex")
    entries = [str(Path(codex).parent)] if codex else []
    entries.extend(LAUNCH_PATH_FALLBACK)
    return ":".join(dict.fromkeys(entries))


def stable_prompt(path: Path, interval: float = 1.0) -> str | None:
    """Accept stable, bounded prompt text without relying on the filename."""
    if (
        path.name.startswith(".")
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
    if not value.strip() or "\0" in value:
        return None
    if path.suffix.lower() in {".txt", ".md", ".markdown"} or _looks_like_markdown(value):
        return value
    return None


def _looks_like_markdown(value: str) -> bool:
    """Recognize a bounded Markdown prompt when a submitted file has no useful suffix."""
    for line in value.splitlines():
        stripped = line.lstrip()
        if (
            stripped.startswith(("#", ">", "```", "- ", "* ", "+ ", "["))
            or stripped == "---"
            or (len(stripped) > 2 and stripped[0].isdigit() and stripped[1:3] in {". ", ") "})
        ):
            return True
    return False


def discover(root: Path, interval: float = 0.0) -> list[Path]:
    inbox = folders(root)["Inbox"]
    candidates: list[tuple[int, str, Path]] = []
    for path in inbox.iterdir():
        if stable_prompt(path, interval) is None:
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    return [path for _, _, path in sorted(candidates)]


def _safe_detail(value: object) -> object:
    if isinstance(value, str):
        return value[:500].replace("\n", " ")
    return value


def _runner_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Expose the bounded runner preflight reason without retaining prompt content."""
    output = completed.stderr or completed.stdout
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = lines[-1] if lines else "Runner stopped before publishing a checkpoint."
    return redact_diagnostic(detail, limit=500)


def _report_matches_terminal_phase(report: Path, phase: str | None) -> bool:
    """Allow delivery only when report prose agrees with the runner checkpoint."""
    if phase not in TERMINAL_PHASES:
        return False
    try:
        body = report.read_text(encoding="utf-8")
    except OSError:
        return False
    if f"- Terminal state: `{phase}`" not in body:
        return False
    if phase == "BLOCKED":
        return "BLOCKED — no engineering changes were executed or delivered." in body and "COMPLETE —" not in body
    if phase == "FAILED":
        return "FAILED — the engineering transaction did not complete successfully." in body and "COMPLETE —" not in body
    return "COMPLETE —" in body


def _corrected_terminal_report(run_id: str, phase: str | None, diagnostic: str | None) -> str:
    """Publish bounded, checkpoint-authoritative terminal evidence on contradiction."""
    outcome = (
        "COMPLETE — terminal checkpoint confirms completed engineering delivery."
        if phase == "COMPLETE"
        else "BLOCKED — no engineering changes were executed or delivered."
        if phase == "BLOCKED"
        else "FAILED — the engineering transaction did not complete successfully."
    )
    return "\n".join(
        (
            "# Engineering Report",
            "",
            f"- Run ID: `{run_id}`",
            f"- Terminal state: `{phase or 'FAILED'}`",
            "",
            "## Management Summary",
            outcome,
            "",
            "## Diagnostics",
            diagnostic or "The original report contradicted the terminal checkpoint.",
            "",
        )
    )


def _prompt_title(content: str, filename: str) -> str:
    """Expose only a bounded Markdown title, never the submitted prompt body."""
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return redact_diagnostic(line[2:].strip(), limit=240)
    return redact_diagnostic(filename, limit=240)


def _previous_prompt_context(root: Path) -> dict[str, object]:
    keys = (
        "submitted_filename",
        "prompt_title",
        "last_executed_filename",
        "last_executed_title",
        "last_executed_run",
        "last_executed_phase",
    )
    try:
        prior = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {key: prior[key] for key in keys if prior.get(key) is not None}


def status(root: Path, state: str, **details: object) -> None:
    """Publish bounded atomic iCloud status without prompt or command output."""
    manifest = EngineeringPlatformManifest.load(
        Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json")
    )
    context = _previous_prompt_context(root)
    context.update(
        {
            key: value
            for key, value in details.items()
            if key in {"submitted_filename", "prompt_title", "last_executed_filename", "last_executed_title", "last_executed_run", "last_executed_phase"}
            and value is not None
        }
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
        **context,
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
    phase = payload.get("phase")
    if phase in TERMINAL_PHASES:
        return False
    run_id = payload.get("run_id")
    if isinstance(run_id, str):
        checkpoint_phase, _ = _runner_result(repo, run_id)
        if checkpoint_phase in TERMINAL_PHASES:
            return False
    return True


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


def _clear_prior_codex_log(repo: Path, run_id: str) -> None:
    """A retried deterministic Inbox run must not display an older attempt's log."""
    (repo / ".djconnect" / "logs" / "codex" / f"{run_id}.log").unlink(missing_ok=True)


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
                diagnostic="Een bestaande engineeringuitvoering is nog actief.",
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
                diagnostic="Een dubbele opdracht is al geregistreerd.",
            )
            return 0
        claimed = _archive_path(areas["Running"], job_id, source)
        title = _prompt_title(content, source.name)
        status(root, "JOB_CLAIMED", queued_jobs=len(candidates) - 1, job_id=job_id, run_id=run_id, submitted_filename=source.name, prompt_title=title)
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
        _clear_prior_codex_log(repo, run_id)
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
            root, "RUNNER_STARTING", job_id=job_id, run_id=run_id, queued_jobs=len(candidates) - 1,
            submitted_filename=source.name, prompt_title=title,
        )
        completed = subprocess.run(arguments, cwd=repo, text=True, capture_output=True, check=False)
        phase, diagnostic = _runner_result(repo, run_id)
        report = _report(repo, run_id)
        delivered = None
        corrected_report = False
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
            if _report_matches_terminal_phase(report, phase):
                shutil.copy2(report, delivered)
            else:
                corrected_report = True
                delivered.write_text(
                    _corrected_terminal_report(run_id, phase, diagnostic), encoding="utf-8"
                )
        successful = completed.returncode == 0 and phase == "COMPLETE" and delivered is not None
        target = areas["Completed"] if successful else areas["Failed"]
        os.replace(claimed, _archive_path(target, job_id, source))
        final_state = (
            "JOB_COMPLETED"
            if successful
            else ("JOB_BLOCKED" if phase == "BLOCKED" else "JOB_FAILED")
        )
        reason = diagnostic or (
            _runner_failure_detail(completed)
            if completed.returncode and phase is None
            else None
        ) or (
            "Engineeringrapport kon niet worden afgeleverd."
            if completed.returncode == 0
            else "De runner stopte zonder een veilig eindrapport."
        )
        if corrected_report:
            reason = redact_diagnostic(
                "The original terminal report contradicted its checkpoint; a corrected report was delivered."
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
            submitted_filename=source.name,
            prompt_title=title,
            last_executed_filename=source.name,
            last_executed_title=title,
            last_executed_run=run_id,
            last_executed_phase=phase,
        )
        return 0 if successful else (completed.returncode or 1)


def launch_agent(repo: Path) -> Path:
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = repo / ".djconnect" / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    launcher = [sys.executable, "-m", "tools.engineering.inbox_watcher", "run", "--repo", str(repo)]
    arguments = "".join(f"<string>{value}</string>" for value in launcher)
    environment = launch_path()
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>EnvironmentVariables</key><dict><key>PATH</key><string>{environment}</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer><key>StandardOutPath</key><string>{logs / "inbox.out.log"}</string><key>StandardErrorPath</key><string>{logs / "inbox.err.log"}</string></dict></plist>',
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
        "dashboard_code": (repo / "tools/engineering/dashboard.py").is_file(),
        "handoff_index": (repo / "docs/engineering/runs/index.json").is_file(),
        "handoff_latest": (repo / "docs/engineering/runs/latest.md").is_file(),
        "dashboard_launch_agent": (
            Path.home() / "Library/LaunchAgents" / "com.djconnect.engineering-dashboard.plist"
        ).is_file(),
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
    repo = args.repo.resolve()
    root = cloud_root(args.icloud_root, repo)
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
                    diagnostic="Een andere watcher beheert de lokale Inbox-vergrendeling.",
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
        LaunchdProvider().install(LABEL, agent)
        return 0
    if args.command == "uninstall":
        LaunchdProvider().uninstall(agent)
        agent.unlink(missing_ok=True)
        return 0
    return doctor(repo, root)


if __name__ == "__main__":
    raise SystemExit(main())
