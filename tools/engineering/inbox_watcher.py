"""Fail-closed, serialized local iCloud Engineering Inbox watcher."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from .platform_version import EngineeringPlatformManifest
from .agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from .platform_api import PlatformConfigurationError, RUNTIME_EXECUTABLE_ENVIRONMENT, execution_host_configuration
from .platform_bootstrap import provision_workspace
from .providers import GitProvider, LaunchdProvider, LocalProcessProvider
from .status_model import build, publish
from .component_logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENVIRONMENT,
    VALID_LEVELS,
    component_lifecycle_context,
    component_logger,
    log_event,
    shutdown_signal_logging,
)
from .component_lock import DuplicateComponentInstanceError, single_instance
from .telemetry import ExecutionTelemetry, persist_execution_async
from .prompt_history import backfill_prompt_history, execution_metadata_from_terminal_report, record_prompt_execution
from .host_preflight import execute as execute_host_preflight
from .workspace_preflight import execute as execute_workspace_preflight
from .capability_preflight import execute as execute_capability_preflight
from .producer import ProducerSubmissionError, parse_producer_metadata, parse_producer_submission
from .drift_diagnostics import summary as drift_summary
from .storage import ENGINEERING_STORAGE_SCHEMA_VERSION, EngineeringStorageError, dismissal_for_run, is_active_blocking_predecessor, load_projection, open_storage, record_artifact, record_execution_dismissal, record_submission
from .execution_lease import liveness as lease_liveness, reconcile_stale
from .execution_timing import complete_active_phase, complete_phase, record_queue_wait_from_submission, start_or_resume_phase, start_phase
from .status_reconciliation import is_stale_rolling_status_block

LABEL = "com.djconnect.engineering-inbox"
WATCHER_VERSION = "1.1.5"
MAX_BYTES = 256_000
TERMINAL_PHASES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
OPERATOR_MERGE_WAIT_PHASE = "WAIT_FOR_OPERATOR_MERGE"
BACKGROUND_RUN_ID_ENVIRONMENT = "DJCONNECT_ENGINEERING_BACKGROUND_RUN_ID"
BACKGROUND_JOB_ID_ENVIRONMENT = "DJCONNECT_ENGINEERING_BACKGROUND_JOB_ID"
BLOCKING_PREDECESSOR_PHASES = frozenset({"BLOCKED", "FAILED"})
RETRY_OF_PATTERN = re.compile(r"(?mi)^retry[ _-]of\s*:\s*(inbox-[a-z0-9-]{6,64})\s*$")
STATUS_RECONCILIATION_OF_PATTERN = re.compile(
    r"(?mi)^status[ _-]reconciliation[ _-]of\s*:\s*(inbox-[a-z0-9-]{6,64})\s*$"
)
ORIGINAL_RUN_ID_PATTERN = re.compile(r"(?mi)^original[ _-]run[ _-]id\s*:\s*(inbox-[a-z0-9-]{6,64})\s*$")
RETRY_GENERATION_PATTERN = re.compile(r"(?mi)^retry[ _-]generation\s*:\s*(\d+)\s*$")
RETRY_TIMESTAMP_PATTERN = re.compile(r"(?mi)^retry[ _-]timestamp\s*:\s*([^\n]{1,80})\s*$")
LAUNCH_PATH_FALLBACK = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
RUNNER_START_GRACE_SECONDS = 90
OPERATOR_MERGE_POLL_SECONDS = 60


class RetrySubmissionError(ValueError):
    """Raised when a fail-closed predecessor cannot be safely resubmitted."""


def cloud_root(value: str | None = None, repo: Path | None = None) -> Path:
    """Compatibility wrapper; transport location is resolved by the host resolver."""
    if repo is None:
        raise PlatformConfigurationError("Execution Host repository is required to resolve Runtime Prompt transport.")
    if value is not None:
        return Path(value).expanduser()
    return execution_host_configuration(repo).resolve_runtime_prompt_transport().inbox.parent


def folders(root: Path) -> dict[str, Path]:
    """Return the sole iCloud transport folder; no state is stored in iCloud."""
    result = {"Inbox": root / "Inbox"}
    for path in result.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return result


def local_folders(repo: Path) -> dict[str, Path]:
    """Return canonical local prompt archives owned by Engineering Platform."""
    result = {name: repo / ".engineering" / "inbox" / name for name in ("Running", "Completed", "Failed")}
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
    if path.suffix.lower() in {".txt", ".md", ".markdown", ".json"} or _looks_like_markdown(value):
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


def defer_queued_prompt(repo: Path, root: Path, filename: str) -> dict[str, str]:
    """Move one still-waiting Inbox prompt into the reversible deferred area."""
    candidate = Path(filename)
    if not filename or candidate.name != filename or filename in {".", ".."}:
        raise RetrySubmissionError("De gekozen Inbox-opdracht is ongeldig.")
    inbox = folders(root)["Inbox"]
    source = inbox / filename
    deferred = inbox / "_deferred"
    with _lock(repo):
        content = stable_prompt(source, 0.0)
        if content is None:
            raise RetrySubmissionError("De gekozen Inbox-opdracht wacht niet meer op uitvoering.")
        deferred.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = deferred / source.name
        if destination.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = deferred / f"{source.stem}-{timestamp}-{uuid.uuid4().hex[:8]}{source.suffix}"
        try:
            os.replace(source, destination)
        except OSError as error:
            raise RetrySubmissionError("De Inbox-opdracht kon niet veilig worden uitgesteld.") from error
        _publish_active_queue(repo, _scan_queue(root, 0.0))
    return {
        "filename": redact_diagnostic(filename, limit=240),
        "deferred_filename": redact_diagnostic(destination.name, limit=240),
        "deferred_at": datetime.now(timezone.utc).isoformat(),
    }


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


def _preflight_failure_detail(result: object) -> str:
    """Return the actionable, redacted reasons from a failed admission check."""
    failed: list[str] = []
    for check in getattr(result, "checks", ()):
        if getattr(check, "outcome", None) != "FAIL":
            continue
        identifier = str(getattr(check, "identifier", "preflight_check"))
        reason = str(getattr(check, "reason", "Preflight check failed."))
        recovery = str(getattr(check, "recovery", "Resolve the preflight issue before retrying."))
        failed.append(f"{identifier}: {reason} Required action: {recovery}")
    detail = " | ".join(failed) or "Preflight failed without a specific recorded reason."
    return redact_diagnostic(detail, limit=500)


def _telemetry_values(repo: Path, run_id: str) -> tuple[float | None, dict[str, int | None], str]:
    """Read only local, run-bound evidence for best-effort telemetry."""
    execution_seconds: float | None = None
    repository = repo.name
    try:
        state = json.loads((repo / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8"))
        value = state.get("agent_execution_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            execution_seconds = float(value)
        if isinstance(state.get("repository"), str):
            repository = state["repository"]
    except (OSError, json.JSONDecodeError):
        pass
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    try:
        stored = json.loads((repo / ".engineering" / "status" / "codex_usage.json").read_text(encoding="utf-8"))
        raw = stored.get("usage") if stored.get("run_id") == run_id else {}
        if isinstance(raw, dict):
            for key in usage:
                value = raw.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = value
    except (OSError, json.JSONDecodeError):
        pass
    return execution_seconds, usage, repository


def _report_runtime_metadata(report: Path | None) -> dict[str, str]:
    """Read the bounded runtime signature from the immutable terminal report."""
    if report is None:
        return {}
    try:
        text = report.read_text(encoding="utf-8")
    except OSError:
        return {}
    labels = {
        "runtime_provider": "Runtime Provider",
        "runtime_model": "AI Model",
        "reasoning_profile": "Reasoning Profile",
        "configuration_profile": "Configuration Profile",
    }
    result: dict[str, str] = {}
    for key, label in labels.items():
        match = re.search(rf"^- {re.escape(label)}: `([^`\\n]{{1,120}})`$", text, re.MULTILINE)
        if match:
            result[key] = match.group(1)
    return result


def _terminal_git_commit(repo: Path, run_id: str) -> str | None:
    """Read the strongest local commit evidence without changing terminal state."""
    try:
        checkpoint = json.loads(
            (repo / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("genesis_commit_sha", "implementation_merge_commit", "finalization_merge_commit"):
        value = checkpoint.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,64}", value):
            return value
    return None


def _terminal_workspace_snapshot(repo: Path, run_id: str) -> tuple[str | None, int | None, str | None]:
    """Capture target-checkout facts exactly when a run becomes terminal."""
    try:
        checkpoint = json.loads(
            (repo / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return None, None, None
    execution_mode = checkpoint.get("execution_mode")
    if execution_mode not in {"MANAGED", "GENESIS"}:
        return None, None, None
    checkout = repo
    if execution_mode == "GENESIS":
        candidate = checkpoint.get("genesis_repository_path")
        if not isinstance(candidate, str) or not Path(candidate).is_absolute():
            return None, None, None
        checkout = Path(candidate).expanduser()
    try:
        observed = GitProvider().execute(checkout, "git", "ls-files", "-z")
        branch = GitProvider().execute(checkout, "git", "branch", "--show-current")
    except OSError:
        return str(checkout.resolve()), None, None
    separators = b"\0" if isinstance(observed.stdout, bytes) else "\0"
    count = (
        sum(1 for item in observed.stdout.split(separators) if item)
        if observed.returncode == 0
        else None
    )
    branch_name = (
        branch.stdout.strip()
        if branch.returncode == 0 and isinstance(branch.stdout, str)
        else None
    )
    return str(checkout.resolve()), count, branch_name or None


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


def _queue_items(candidates: list[tuple[Path, str]], claimed: Path | None = None) -> list[dict[str, str]]:
    """Project bounded, title-only Inbox evidence for the private status page."""
    items: list[dict[str, str]] = []
    for path, content in candidates:
        if path == claimed:
            continue
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            continue
        items.append(
            {
                "filename": redact_diagnostic(path.name, limit=240),
                "title": _prompt_title(content, path.name),
                "modified_at": modified_at,
            }
        )
        if len(items) == 25:
            break
    return items


def _previous_prompt_context(repo: Path) -> dict[str, object]:
    keys = (
        "submitted_filename",
        "prompt_title",
        "last_executed_filename",
        "last_executed_title",
        "last_executed_run",
        "last_executed_phase",
        "blocking_predecessor_run",
        "blocking_predecessor_phase",
        "blocking_predecessor_filename",
        "blocking_predecessor_title",
        "predecessor_recovery_action",
    )
    try:
        prior = load_projection(repo, "watcher_status") or {}
    except EngineeringStorageError:
        return {}
    return {key: prior[key] for key in keys if prior.get(key) is not None}


def status(repo: Path, state: str, **details: object) -> None:
    """Publish bounded atomic local status without prompt or command output."""
    manifest = EngineeringPlatformManifest.load(
        Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json")
    )
    context = _previous_prompt_context(repo)
    retained = {
        "submitted_filename", "prompt_title", "last_executed_filename", "last_executed_title",
        "last_executed_run", "last_executed_phase", "blocking_predecessor_run",
        "blocking_predecessor_phase", "blocking_predecessor_filename",
        "blocking_predecessor_title", "predecessor_recovery_action",
    }
    context.update({key: value for key, value in details.items() if key in retained and value is not None})
    context.update({key: None for key in retained if key in details and details[key] is None})
    # ``blocking_predecessor_*`` is a derived operational projection.  Never
    # retain stale fields after canonical dismissal evidence makes the run
    # historical-only; this preserves its BLOCKED history and report.
    if not is_active_blocking_predecessor(
        repo, context.get("blocking_predecessor_run"), context.get("blocking_predecessor_phase"),
    ):
        context.update({
            "blocking_predecessor_run": None,
            "blocking_predecessor_phase": None,
            "blocking_predecessor_filename": None,
            "blocking_predecessor_title": None,
            "predecessor_recovery_action": None,
        })
    payload = build(
        manifest,
        watcher_state=state,
        job_id=details.get("job_id"),
        run_id=details.get("run_id"),
        runner_pid=details.get("runner_pid"),
        queue_depth=details.get("queued_jobs", 0),
        queue_items=details.get("queue_items", []),
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
    publish(repo / ".engineering" / "status", payload)


def _publish_active_queue(repo: Path, candidates: list[tuple[Path, str]]) -> None:
    """Refresh queue evidence without replacing a detached runner's status.

    The polling watcher may observe new Inbox files while the admitted runner
    is executing.  That observation is read-only: it must retain the runner's
    run identity and phase rather than publish a competing watcher state.
    """
    try:
        existing = load_projection(repo, "watcher_status") or {}
    except EngineeringStorageError:
        existing = {}
    manifest = EngineeringPlatformManifest.load(
        Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json")
    )
    payload = build(
        manifest,
        **{
            **existing,
            "queue_depth": len(candidates),
            "queue_items": _queue_items(candidates),
        },
    )
    publish(repo / ".engineering" / "status", payload)


def _job_id(source: Path, content: str) -> tuple[str, str, str]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    job_id = f"{source.stem[:32]}-{digest[:12]}"
    return job_id, f"inbox-{digest[:16]}", digest


def _allocate_run_id() -> str:
    """Allocate an execution identity only after admission has passed.

    A content digest remains the job fingerprint.  It must never identify an
    execution because an operator may deliberately submit identical content
    again after host repair or a completed prior run.
    """
    return f"inbox-{uuid.uuid4().hex}"


def _archive_path(area: Path, job_id: str, source: Path) -> Path:
    return area / f"{job_id}__{source.name}"


def _already_seen(areas: dict[str, Path], job_id: str) -> bool:
    return any(
        next(areas[name].glob(f"{job_id}__*"), None) is not None
        for name in ("Running", "Completed", "Failed")
    )


def _retry_of(content: str) -> str | None:
    """Return the explicit predecessor run named by a corrected Inbox retry."""
    match = RETRY_OF_PATTERN.search(content)
    return match.group(1) if match else None


def retry_metadata(content: str) -> dict[str, object]:
    """Read only the bounded retry lineage headers from a submitted prompt."""
    parent = _retry_of(content)
    if parent is None:
        return {"retry_of": None, "original_run_id": None, "retry_generation": None, "retry_timestamp": None}
    original = ORIGINAL_RUN_ID_PATTERN.search(content)
    generation = RETRY_GENERATION_PATTERN.search(content)
    timestamp = RETRY_TIMESTAMP_PATTERN.search(content)
    return {
        "retry_of": parent,
        "original_run_id": original.group(1) if original else parent,
        "retry_generation": int(generation.group(1)) if generation else 1,
        "retry_timestamp": timestamp.group(1).strip() if timestamp else None,
    }


def queued_retry_children(root: Path) -> list[dict[str, object]]:
    """Project queued retry lineage without inventing an execution identity."""
    children: list[dict[str, object]] = []
    for path in discover(root, 0.0):
        content = stable_prompt(path, 0.0)
        if content is None:
            continue
        lineage = retry_metadata(content)
        parent = lineage["retry_of"]
        if not isinstance(parent, str):
            continue
        children.append(
            {
                "retry_of": parent,
                "status": "QUEUED",
                "retry_timestamp": lineage["retry_timestamp"],
            }
        )
    return children


def _blocking_predecessor(root: Path) -> dict[str, str] | None:
    """Return terminal predecessor evidence that must fail closed for the queue."""
    prior = _previous_prompt_context(root)
    phase = prior.get("last_executed_phase")
    run_id = prior.get("last_executed_run")
    if not is_active_blocking_predecessor(root, run_id, phase):
        return None
    title = prior.get("last_executed_title")
    filename = prior.get("last_executed_filename")
    return {
        "run_id": run_id,
        "phase": str(phase),
        "title": str(title) if title else "Onbekende prompt",
        "filename": str(filename) if filename else "Onbekend bestand",
    }


def _predecessor_recovery_action(run_id: str) -> str:
    return (
        "Herstel de geblokkeerde prompt of dien die bewust opnieuw in met een eigen regel "
        f"`Retry-Of: {run_id}`. De wachtrij blijft gepauzeerd totdat deze herindiening voltooid is."
    )


def _archived_prompt_for_run(repo: Path, run_id: str) -> tuple[Path, str] | None:
    """Find the immutable local failed prompt that produced ``run_id``."""
    for job in (repo / ".engineering" / "inbox-processing").glob("*/job.json"):
        try:
            payload = json.loads(job.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("run_id") == run_id:
            prompt = job.parent / "prompt.md"
            content = stable_prompt(prompt, 0.0)
            if content is not None:
                return prompt, content
    for path in sorted(local_folders(repo)["Failed"].iterdir()):
        content = stable_prompt(path, 0.0)
        if content is not None and _job_id(path, content)[1] == run_id:
            return path, content
    return None


def _terminal_phase_for_run(repo: Path, run_id: str) -> str | None:
    phase, _ = _runner_result(repo, run_id)
    if phase in TERMINAL_PHASES:
        return phase
    try:
        from .prompt_history import prompt_history
        for record in prompt_history(repo):
            if record.get("run_id") == run_id:
                return record.get("status") if record.get("status") in TERMINAL_PHASES else None
    except Exception:
        pass
    predecessor = _blocking_predecessor(repo)
    if predecessor and predecessor["run_id"] == run_id:
        return predecessor["phase"]
    return None


def retry_admission_preflight(repo: Path, run_id: str) -> None:
    """Fail a dashboard retry before it enters the Inbox when admission fails.

    The watcher repeats these checks when it claims the new prompt.  This
    early pass is solely operator feedback: it prevents a known-bad retry from
    creating confusing queue lineage while preserving the original retry
    action for a later attempt.
    """
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise RetrySubmissionError("De opgegeven run-ID is ongeldig.")
    if dismissal_for_run(repo, run_id):
        raise RetrySubmissionError("Deze uitvoering is al afgesloten; opnieuw proberen is niet beschikbaar.")
    archived = _archived_prompt_for_run(repo, run_id)
    if archived is None:
        raise RetrySubmissionError("De oorspronkelijke terminale prompt is lokaal niet beschikbaar.")
    _, content = archived
    results = (
        execute_host_preflight(repo, run_id=run_id),
        execute_workspace_preflight(repo, content, run_id=run_id),
        execute_capability_preflight(repo, content, run_id=run_id),
    )
    failures = [
        check
        for result in results
        for check in result.checks
        if check.outcome == "FAIL"
    ]
    if not failures:
        return
    primary = failures[0]
    raise RetrySubmissionError(
        f"Preflight mislukt: {primary.reason} Herstel: {primary.recovery}"
    )


def predecessor_retry_admission_preflight(repo: Path) -> str:
    """Validate the blocking predecessor before queue recovery submits it."""
    predecessor = _blocking_predecessor(repo)
    if predecessor is None:
        raise RetrySubmissionError("Er is geen geblokkeerde voorafgaande prompt om de wachtrij te hervatten.")
    retry_admission_preflight(repo, predecessor["run_id"])
    return predecessor["run_id"]


def dismiss_execution(repo: Path, run_id: str, *, dismissed_by: str = "dashboard_operator") -> dict[str, object]:
    """Acknowledge one terminal execution without changing engineering evidence."""
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise RetrySubmissionError("De opgegeven run-ID is ongeldig.")
    with _lock(repo):
        status_path = repo / ".engineering" / "status" / "status.json"
        try:
            current = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RetrySubmissionError("Er is geen actieve terminale uitvoering om te bevestigen.") from error
        if current.get("watcher_state") == "ENGINEERING_RUN_ACTIVE" or current.get("run_id"):
            raise RetrySubmissionError("Een actieve uitvoering kan niet worden bevestigd.")
        phase = _terminal_phase_for_run(repo, run_id)
        if phase not in TERMINAL_PHASES:
            raise RetrySubmissionError("Alleen een terminale uitvoering kan worden bevestigd.")
        if dismissal_for_run(repo, run_id):
            raise RetrySubmissionError("Deze uitvoering is al afgesloten.")
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = open_storage(repo)
        try:
            history_exists = connection.execute(
                "SELECT 1 FROM prompt_execution_history WHERE run_id=?", (run_id,)
            ).fetchone() is not None
        finally:
            connection.close()
        if not history_exists:
            record_prompt_execution(
                repo, run_id=run_id, terminal_state=phase,
                prompt_title=current.get("last_executed_title") or run_id, executed_at=timestamp,
            )
        try:
            record = record_execution_dismissal(
                repo, run_id=run_id, terminal_state=phase, dismissed_at=timestamp, dismissed_by=dismissed_by,
            )
        except EngineeringStorageError as error:
            raise RetrySubmissionError("De dismissal-audit is niet veilig beschikbaar.") from error
        # Dismissing an older terminal record must not erase the watcher
        # context of a newer terminal execution.
        if current.get("last_executed_run") == run_id:
            status(
                repo,
                "WATCHER_IDLE",
                queued_jobs=current.get("queue_depth", 0),
                queue_items=current.get("queue_items", []),
                last_executed_filename=None,
                last_executed_title=None,
                last_executed_run=None,
                last_executed_phase=None,
                current_action="Execution Host Idle",
            )
        return record


def submit_execution_retry(repo: Path, root: Path, run_id: str, *, queue_recovery: bool = False) -> dict[str, object]:
    """Create one explicitly requested new execution for a retryable terminal run."""
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise RetrySubmissionError("De opgegeven run-ID is ongeldig.")
    with _lock(repo):
        if dismissal_for_run(repo, run_id):
            raise RetrySubmissionError("Deze uitvoering is al afgesloten; opnieuw proberen is niet beschikbaar.")
        terminal_phase = _terminal_phase_for_run(repo, run_id)
        if terminal_phase not in BLOCKING_PREDECESSOR_PHASES:
            raise RetrySubmissionError("Alleen een terminal geblokkeerde of mislukte uitvoering kan opnieuw worden uitgevoerd.")
        candidates = [(path, stable_prompt(path, 0.0)) for path in discover(root, 0.0)]
        if any(content is not None and _retry_of(content) == run_id for _, content in candidates):
            raise RetrySubmissionError("Een uitvoering opnieuw proberen staat al in de wachtrij.")
        # A completed or active child is immutable lineage evidence too; a
        # historical terminal run must never mint a second retry execution.
        from .prompt_history import prompt_history
        if any(record.get("retry_of") == run_id for record in prompt_history(repo)):
            raise RetrySubmissionError("Voor deze uitvoering bestaat al een Retry Execution.")
        archived = _archived_prompt_for_run(repo, run_id)
        if archived is None:
            raise RetrySubmissionError("De oorspronkelijke terminale prompt is lokaal niet beschikbaar.")
        source, content = archived
        prior = retry_metadata(content)
        original = str(prior["original_run_id"] or run_id)
        generation = int(prior["retry_generation"] or 0) + 1
        timestamp = datetime.now(timezone.utc).isoformat()
        operation = "Queue recovery replacement" if queue_recovery else "Explicit execution retry"
        retry_content = (
            f"Retry-Of: {run_id}\nOriginal-Run-ID: {original}\nRetry-Generation: {generation}\n"
            f"Retry-Timestamp: {timestamp}\n<!-- {operation}: {uuid.uuid4().hex} -->\n\n{content}"
        )
        inbox = folders(root)["Inbox"]
        suffix = source.suffix.lower() if source.suffix.lower() in {".md", ".markdown", ".txt"} else ".md"
        filename = f"retry-{run_id}-{uuid.uuid4().hex[:8]}{suffix}"
        destination, temporary = inbox / filename, inbox / f".{filename}.tmp"
        try:
            temporary.write_text(retry_content, encoding="utf-8")
            os.replace(temporary, destination)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RetrySubmissionError("De nieuwe uitvoering kon niet veilig in de Inbox worden geplaatst.") from error
        _, retry_run_id, _ = _job_id(destination, retry_content)
        return {"retry_of": run_id, "original_run_id": original, "retry_generation": generation,
                "retry_timestamp": timestamp, "filename": filename, "retry_run_id": retry_run_id}


def status_reconciliation_preview(repo: Path, run_id: str) -> dict[str, str]:
    """Prove one blocked run needs a governance-only status reconciliation."""
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise RetrySubmissionError("De opgegeven run-ID is ongeldig.")
    try:
        state = StateStore(repo / ".engineering" / "engineering-runs").load(run_id)
    except StateError as error:
        raise RetrySubmissionError("De geblokkeerde uitvoering is niet beschikbaar.") from error
    if not is_stale_rolling_status_block(state):
        raise RetrySubmissionError("Deze uitvoering komt niet in aanmerking voor veilig statusherstel.")
    return {"run_id": run_id, "reason": "merged_status_records_stale"}


def _is_verified_status_reconciliation(
    repo: Path, content: str, predecessor_run_id: str
) -> bool:
    """Allow only the narrowly proved reconciliation past its own predecessor gate.

    The marker alone is deliberately insufficient: an Inbox author must not be
    able to bypass the predecessor gate by adding a lookalike header.  The
    referenced run must still satisfy the same immutable, governance-only
    status-drift proof used by the dashboard action.
    """
    marker = STATUS_RECONCILIATION_OF_PATTERN.search(content)
    if marker is None or marker.group(1) != predecessor_run_id:
        return False
    try:
        status_reconciliation_preview(repo, predecessor_run_id)
    except RetrySubmissionError:
        return False
    return True


def submit_status_reconciliation(repo: Path, root: Path, run_id: str) -> dict[str, str]:
    """Queue exactly one dedicated Finalization prompt after a verified preview."""
    preview = status_reconciliation_preview(repo, run_id)
    with _lock(repo):
        marker = f"Status-Reconciliation-Of: {run_id}"
        if any(content is not None and marker in content for _, content in ((path, stable_prompt(path, 0.0)) for path in discover(root, 0.0))):
            raise RetrySubmissionError("Een statusherstel staat al in de wachtrij.")
        request_id = uuid.uuid4().hex
        content = (
            f"{marker}\nStatus-Reconciliation-Request: {request_id}\n\n"
            "# Engineering Platform — Reconcile merged status records\n\n"
            "Required Engineering Platform: >= 1.5.0\n\n"
            "Execute only the dedicated governance-only Finalization reconciliation for the "
            "verified merged predecessor of the referenced blocked run. Reconcile the four "
            "rolling records required by PROMPT_INITIALIZATION.md with current main. Do not "
            "rewrite Prompt History or change product, execution, validation, retry, merge, "
            "or lifecycle semantics. Create one reviewable Finalization pull request.\n"
        )
        inbox = folders(root)["Inbox"]
        filename = f"status-reconciliation-{run_id}-{uuid.uuid4().hex[:8]}.md"
        destination, temporary = inbox / filename, inbox / f".{filename}.tmp"
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, destination)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RetrySubmissionError("De Finalization-herstelopdracht kon niet veilig in de Inbox worden geplaatst.") from error
    return {**preview, "filename": filename}


def submit_predecessor_retry(repo: Path, root: Path) -> dict[str, str]:
    """Explicitly resubmit the current blocking prompt through the Inbox transport.

    The watcher remains the only owner of claiming, sequencing and execution.
    A unique, inert marker prevents an accidental duplicate retry from reusing
    an already recorded deterministic run identity.
    """
    predecessor = _blocking_predecessor(repo)
    if predecessor is None:
        raise RetrySubmissionError("Er is geen geblokkeerde voorafgaande prompt om de wachtrij te hervatten.")
    queued = [(path, stable_prompt(path, 0.0)) for path in discover(root, 0.0)]
    if not any(content is not None and _retry_of(content) != predecessor["run_id"] for _, content in queued):
        raise RetrySubmissionError("Queue recovery is alleen beschikbaar wanneer afhankelijke Inbox-werk wacht.")
    outcome = submit_execution_retry(repo, root, predecessor["run_id"], queue_recovery=True)
    return {"blocking_run_id": predecessor["run_id"], **outcome}  # type: ignore[return-value]

def _move(source: Path, destination: Path) -> None:
    """Move a prompt out of iCloud, allowing the expected cross-device boundary."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))


def _active_transaction(repo: Path) -> bool:
    """Return whether an admitted runner is still demonstrably alive.

    A detached child may be terminated between its admission projection and its
    first checkpoint.  That must not leave the watcher in ``RUNNER_STARTING``
    indefinitely and block every later Inbox item.
    """
    try:
        payload = load_projection(repo, "live_status") or {}
    except EngineeringStorageError:
        payload = {}
    phase = payload.get("phase")
    if phase in TERMINAL_PHASES:
        return False
    run_id = payload.get("run_id")
    if isinstance(run_id, str):
        checkpoint_phase, _ = _runner_result(repo, run_id)
        if checkpoint_phase in TERMINAL_PHASES:
            return False
        # Waiting for an approved PR merge is intentionally durable and does
        # not have a live process lease.  It still owns the queue until the
        # merge is observed or an operator explicitly aborts it.
        if checkpoint_phase == OPERATOR_MERGE_WAIT_PHASE:
            return True
        # A transaction lifecycle checkpoint is not liveness evidence. Once
        # the Execution Host has persisted the transaction, only its canonical
        # lease may keep later Inbox work gated. This prevents a crashed host
        # with an old ACTIVE checkpoint from blocking the queue indefinitely.
        if checkpoint_phase is not None:
            return lease_liveness(repo, run_id).get("state") == "LIVE"
        # The runner can stop after publishing its terminal watcher result but
        # before replacing current.json. The watcher result is authoritative
        # for that same Run ID, so it must not hold later Inbox work hostage.
        try:
            watcher = load_projection(repo, "watcher_status") or {}
        except EngineeringStorageError:
            watcher = {}
        if (
            watcher.get("last_executed_run") == run_id
            and watcher.get("last_executed_phase") in TERMINAL_PHASES
        ):
            return False
        if (
            checkpoint_phase is None
            and watcher.get("watcher_state") == "RUNNER_STARTING"
            and watcher.get("run_id") == run_id
        ):
            return _detached_runner_is_alive(watcher)
        return True
    # The detached runner is admitted before it has written current.json.
    # Status is therefore the authoritative short-lived admission record.
    try:
        watcher = load_projection(repo, "watcher_status") or {}
    except EngineeringStorageError:
        return False
    watcher_run_id = watcher.get("run_id")
    if watcher.get("watcher_state") not in {"JOB_CLAIMED", "RUNNER_STARTING", "REPORT_PUBLISHING"}:
        return False
    if not isinstance(watcher_run_id, str):
        return False
    checkpoint_phase, _ = _runner_result(repo, watcher_run_id)
    if checkpoint_phase in TERMINAL_PHASES:
        return False
    return _detached_runner_is_alive(watcher)


def _detached_runner_is_alive(watcher: dict[str, object]) -> bool:
    """Confirm a detached runner PID, reaping an exited child when possible."""
    pid = watcher.get("runner_pid")
    if isinstance(pid, int) and pid > 0:
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # The PID can belong to a pre-existing watcher process. It is not
            # ours to reap, so retain the non-mutating liveness check below.
            reaped_pid = 0
        if reaped_pid == pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    observed = watcher.get("last_update")
    if not isinstance(observed, str):
        return False
    try:
        started = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is None:
        return False
    return (datetime.now(timezone.utc) - started).total_seconds() <= RUNNER_START_GRACE_SECONDS


@contextmanager
def _lock(repo: Path):
    """Use an exclusive local lock and recover only a proven stale PID lock."""
    # A detached runner has already been admitted by the polling watcher.  It
    # must not own the watcher lock for its full engineering lifetime, or the
    # watcher cannot keep discovering later Inbox prompts.
    if os.environ.get(BACKGROUND_RUN_ID_ENVIRONMENT):
        yield
        return
    path = repo / ".engineering" / "engineering-inbox.lock"
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
    try:
        connection = open_storage(repo)
        try:
            row = connection.execute(
                "SELECT phase,payload FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
    except EngineeringStorageError:
        return None, None
    if not row:
        # Compatibility import for a pre-datastore runner that completed
        # between watcher releases. New runners always create the row first.
        try:
            legacy = json.loads(
                (repo / ".engineering" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None, None
        return legacy.get("phase"), legacy.get("diagnostic") if isinstance(legacy, dict) else None
    try:
        payload = json.loads(row[1])
    except (TypeError, json.JSONDecodeError):
        return None, None
    return row[0], payload.get("diagnostic") if isinstance(payload, dict) else None


def _operator_merge_wait(repo: Path) -> TransactionState | None:
    """Return the one durable operator merge hand-off, if present."""
    try:
        connection = open_storage(repo)
        try:
            row = connection.execute(
                "SELECT payload FROM engineering_transactions WHERE phase=? ORDER BY updated_at DESC LIMIT 1",
                (OPERATOR_MERGE_WAIT_PHASE,),
            ).fetchone()
        finally:
            connection.close()
        return TransactionState.from_dict(json.loads(row[0])) if row else None
    except (EngineeringStorageError, TypeError, json.JSONDecodeError, ValueError):
        return None


def _operator_merge_poll_due(repo: Path, run_id: str) -> bool:
    """Rate-limit GitHub reconciliation without tying it to a browser session."""
    try:
        watcher = load_projection(repo, "watcher_status") or {}
        observed = watcher.get("last_update") if watcher.get("run_id") == run_id else None
        when = datetime.fromisoformat(observed.replace("Z", "+00:00")) if isinstance(observed, str) else None
    except (EngineeringStorageError, ValueError):
        return True
    return when is None or (datetime.now(timezone.utc) - when).total_seconds() >= OPERATOR_MERGE_POLL_SECONDS


def _publish_operator_merge_wait(repo: Path, state: TransactionState, *, queue_items: list[dict[str, object]] | None = None, queue_depth: int = 0, job_id: str | None = None, filename: str | None = None, title: str | None = None) -> None:
    """Project a PR hand-off as active operational state, never as failure."""
    if job_id is None or filename is None or title is None:
        try:
            prior = load_projection(repo, "watcher_status") or {}
        except EngineeringStorageError:
            prior = {}
        job_id = job_id or (prior.get("job_id") if isinstance(prior.get("job_id"), str) else None)
        filename = filename or (prior.get("submitted_filename") if isinstance(prior.get("submitted_filename"), str) else None)
        title = title or (prior.get("prompt_title") if isinstance(prior.get("prompt_title"), str) else None)
    status(
        repo,
        "WAITING_FOR_OPERATOR_MERGE",
        job_id=job_id,
        run_id=state.run_id,
        queued_jobs=queue_depth,
        queue_items=queue_items or [],
        runner_phase=state.phase,
        current_action="Wacht op de operator om de pull request te mergen.",
        implementation_pr=state.implementation_pull_request,
        finalization_pr=state.finalization_pull_request,
        submitted_filename=filename,
        prompt_title=title,
    )


def _publish_resumed_merge_transition(
    repo: Path,
    state: TransactionState,
    *,
    queue_items: list[dict[str, object]],
    queue_depth: int,
) -> None:
    """Replace a resolved merge wait with its newly resumed lifecycle state.

    A resumed Execution Host can move the same run into finalization before it
    returns to the watcher. Re-publishing the previous merge wait afterwards
    makes the Operations Console reopen an obsolete PR modal on refresh.
    """
    try:
        prior = load_projection(repo, "watcher_status") or {}
    except EngineeringStorageError:
        prior = {}
    status(
        repo,
        "ENGINEERING_RUN_ACTIVE",
        job_id=prior.get("job_id") if isinstance(prior.get("job_id"), str) else None,
        run_id=state.run_id,
        queued_jobs=queue_depth,
        queue_items=queue_items,
        runner_phase=state.phase,
        current_action=state.next_action,
        implementation_pr=state.implementation_pull_request,
        finalization_pr=state.finalization_pull_request,
        submitted_filename=prior.get("submitted_filename") if isinstance(prior.get("submitted_filename"), str) else None,
        prompt_title=prior.get("prompt_title") if isinstance(prior.get("prompt_title"), str) else None,
    )


def abort_operator_merge_wait(repo: Path, run_id: str, *, dismissed_by: str = "dashboard_operator") -> dict[str, object]:
    """Explicitly stop a durable PR hand-off without claiming a technical failure."""
    if not re.fullmatch(r"inbox-[a-z0-9-]{6,64}", run_id):
        raise RetrySubmissionError("De opgegeven run-ID is ongeldig.")
    with _lock(repo):
        state = _operator_merge_wait(repo)
        if state is None or state.run_id != run_id:
            raise RetrySubmissionError("Deze uitvoering wacht niet op een pull request-merge.")
        aborted = replace(
            state,
            phase="FAILED",
            terminal=True,
            next_action="operator_cancelled_merge_wait",
            terminal_condition="operator_cancelled",
            diagnostic="De operator heeft deze uitvoering gestopt terwijl de pull request op merge wachtte.",
        )
        StateStore(repo / ".engineering" / "engineering-runs").save(aborted)
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            watcher = load_projection(repo, "watcher_status") or {}
        except EngineeringStorageError:
            watcher = {}
        job_id = watcher.get("job_id") if isinstance(watcher.get("job_id"), str) else None
        running = local_folders(repo)["Running"]
        source = next(running.glob(f"{job_id}__*"), None) if job_id else None
        if source is not None:
            original = Path(source.name.removeprefix(f"{job_id}__"))
            _move(source, _archive_path(local_folders(repo)["Failed"], job_id or run_id, original))
        try:
            record_prompt_execution(
                repo,
                run_id=run_id,
                terminal_state="FAILED",
                prompt_title=Path(state.prompt_path).stem,
                executed_at=timestamp,
            )
            record = record_execution_dismissal(
                repo, run_id=run_id, terminal_state="FAILED", dismissed_at=timestamp,
                dismissed_by=dismissed_by,
            )
        except EngineeringStorageError as error:
            raise RetrySubmissionError("De afsluiting kon niet veilig worden vastgelegd.") from error
        complete_active_phase(repo, run_id, "TOTAL_EXECUTION", outcome="FAILED")
        status(
            repo,
            "JOB_FAILED",
            job_id=job_id,
            run_id=run_id,
            queued_jobs=0,
            queue_items=[],
            runner_phase="FAILED",
            diagnostic=aborted.diagnostic,
            last_executed_filename=source.name if source is not None else None,
            last_executed_title=Path(state.prompt_path).stem,
            last_executed_run=run_id,
            last_executed_phase="FAILED",
        )
        return record


def _finalize_operator_merge_wait(repo: Path, state: TransactionState) -> None:
    """Archive a reconciled waiting run after its resumed host becomes terminal."""
    try:
        watcher = load_projection(repo, "watcher_status") or {}
    except EngineeringStorageError:
        watcher = {}
    job_id = watcher.get("job_id") if isinstance(watcher.get("job_id"), str) else None
    running = local_folders(repo)["Running"]
    source = next(running.glob(f"{job_id}__*"), None) if job_id else None
    if source is not None:
        target = local_folders(repo)["Completed" if state.phase == "COMPLETE" else "Failed"]
        original = Path(source.name.removeprefix(f"{job_id}__"))
        _move(source, _archive_path(target, job_id or state.run_id, original))
    report = _report(repo, state.run_id)
    title = watcher.get("prompt_title") if isinstance(watcher.get("prompt_title"), str) else Path(state.prompt_path).stem
    try:
        record_prompt_execution(
            repo,
            run_id=state.run_id,
            terminal_state=state.phase,
            prompt_title=title,
            executed_at=datetime.now(timezone.utc),
            report=report,
            git_commit=_terminal_git_commit(repo, state.run_id),
        )
        if report is not None:
            record_artifact(
                repo, report, artifact_id=f"report:{state.run_id}", artifact_type="TERMINAL_REPORT",
                content_type="text/markdown", created_at=datetime.now(timezone.utc).isoformat(), run_id=state.run_id,
            )
    except EngineeringStorageError:
        pass
    complete_active_phase(repo, state.run_id, "TOTAL_EXECUTION", outcome="COMPLETE" if state.phase == "COMPLETE" else "FAILED")
    status(
        repo,
        "JOB_COMPLETED" if state.phase == "COMPLETE" else "JOB_FAILED",
        job_id=job_id,
        run_id=state.run_id,
        queued_jobs=0,
        queue_items=[],
        runner_phase=state.phase,
        report=str(report) if report else None,
        diagnostic=state.diagnostic,
        last_executed_filename=source.name if source else None,
        last_executed_title=title,
        last_executed_run=state.run_id,
        last_executed_phase=state.phase,
    )


def _report(repo: Path, run_id: str) -> Path | None:
    reports = list((repo / ".engineering" / "reports").glob(f"*_{run_id}.md"))
    return max(reports, key=lambda path: path.stat().st_mtime) if reports else None


def _clear_prior_codex_log(repo: Path, run_id: str) -> None:
    """A retried deterministic Inbox run must not display an older attempt's log."""
    (repo / ".engineering" / "logs" / "codex" / f"{run_id}.log").unlink(missing_ok=True)


QueueCandidate = tuple[Path, str]


@dataclass(frozen=True)
class QueueAdmission:
    """The single candidate permitted to advance during one watcher cycle."""

    source: Path | None
    content: str | None
    exit_code: int = 0


def _scan_queue(root: Path, interval: float) -> list[QueueCandidate]:
    """Return stable Inbox prompts in their canonical execution order."""
    return [
        (path, content)
        for path in discover(root, interval)
        if (content := stable_prompt(path, 0.0)) is not None
    ]


def _admit_queue_candidate(
    repo: Path,
    candidates: list[QueueCandidate],
    *,
    child_run_id: str | None,
    child_job_id: str | None,
    logger: logging.Logger,
) -> QueueAdmission:
    """Choose the one permitted candidate, preserving queue-recovery ordering."""
    if child_job_id:
        admitted = [
            (candidate, prompt)
            for candidate, prompt in candidates
            if _job_id(candidate, prompt)[0] == child_job_id
        ]
        if admitted:
            source, content = admitted[0]
            return QueueAdmission(source, content)
        status(
            repo,
            "JOB_FAILED",
            run_id=child_run_id,
            queued_jobs=len(candidates),
            queue_items=_queue_items(candidates),
            diagnostic="De toegelaten Inbox-opdracht is niet meer beschikbaar voor de losgestarte runner.",
        )
        log_event(logger, logging.ERROR, "detached_runner_job_missing", run_id=child_run_id)
        return QueueAdmission(None, None, 1)

    predecessor = _blocking_predecessor(repo)
    if predecessor is None:
        source, content = candidates[0]
        return QueueAdmission(source, content)

    retries = [
        (candidate, prompt)
        for candidate, prompt in candidates
        if _retry_of(prompt) == predecessor["run_id"]
    ]
    if retries:
        source, content = retries[0]
        return QueueAdmission(source, content)

    reconciliations = [
        (candidate, prompt)
        for candidate, prompt in candidates
        if _is_verified_status_reconciliation(repo, prompt, predecessor["run_id"])
    ]
    if reconciliations:
        source, content = reconciliations[0]
        return QueueAdmission(source, content)

    status(
        repo,
        "WAITING_FOR_PREDECESSOR",
        queued_jobs=len(candidates),
        queue_items=_queue_items(candidates),
        runner_phase="WAITING_FOR_PREDECESSOR",
        current_action="Wachtrij gepauzeerd tot de voorafgaande prompt is hersteld.",
        diagnostic=(
            f"Voorafgaande prompt {predecessor['run_id']} eindigde als "
            f"{predecessor['phase']}; geen volgende prompt is geclaimd."
        ),
        blocking_predecessor_run=predecessor["run_id"],
        blocking_predecessor_phase=predecessor["phase"],
        blocking_predecessor_filename=predecessor["filename"],
        blocking_predecessor_title=predecessor["title"],
        predecessor_recovery_action=_predecessor_recovery_action(predecessor["run_id"]),
    )
    return QueueAdmission(None, None)


def _detach_runner(
    repo: Path,
    root: Path,
    candidates: list[QueueCandidate],
    source: Path,
    content: str,
    job_id: str,
    run_id: str,
    logger: logging.Logger,
) -> int:
    """Publish admission and start one independent runner without blocking scans."""
    status(
        repo,
        "RUNNER_STARTING",
        queued_jobs=len(candidates) - 1,
        queue_items=_queue_items(candidates, source),
        job_id=job_id,
        run_id=run_id,
        submitted_filename=source.name,
        prompt_title=_prompt_title(content, source.name),
        current_action="De Engineering-runner is los gestart; de watcher blijft de Inbox volgen.",
    )
    environment = dict(os.environ)
    environment.update(execution_host_configuration(repo).runtime_environment())
    environment[BACKGROUND_RUN_ID_ENVIRONMENT] = run_id
    environment[BACKGROUND_JOB_ID_ENVIRONMENT] = job_id
    try:
        process = LocalProcessProvider().spawn_detached(
            repo,
            [
                sys.executable,
                "-m",
                "tools.engineering.inbox_watcher",
                "once",
                "--repo",
                str(repo),
                "--icloud-root",
                str(root),
            ],
            environment,
        )
    except OSError as error:
        status(
            repo,
            "JOB_FAILED",
            queued_jobs=len(candidates),
            queue_items=_queue_items(candidates),
            job_id=job_id,
            run_id=run_id,
            diagnostic=f"De los gestarte Engineering-runner kon niet starten: {error}",
        )
        return 1
    runner_pid = getattr(process, "pid", None)
    status(
        repo,
        "RUNNER_STARTING",
        queued_jobs=len(candidates) - 1,
        queue_items=_queue_items(candidates, source),
        job_id=job_id,
        run_id=run_id,
        runner_pid=runner_pid if isinstance(runner_pid, int) and runner_pid > 0 else None,
        submitted_filename=source.name,
        prompt_title=_prompt_title(content, source.name),
        current_action="De Engineering-runner is los gestart; de watcher blijft de Inbox volgen.",
    )
    log_event(logger, logging.INFO, "runner_detached", run_id=run_id)
    return 0


def _execute_runner_command(
    repo: Path,
    prompt: Path,
    run_id: str,
) -> tuple[datetime, subprocess.CompletedProcess[str]]:
    """Run the admitted host command and return only process lifecycle evidence."""
    phase, _ = _runner_result(repo, run_id)
    _clear_prior_codex_log(repo, run_id)
    arguments = [
        str(repo / "tools/engineering/engineering-execution-host"),
        str(prompt.relative_to(repo)),
        "--owner-authorized",
        "--run-id",
        run_id,
        "--admitted-storage-schema",
        str(ENGINEERING_STORAGE_SCHEMA_VERSION),
    ]
    # This marker is admitted only after the immutable predecessor proof.  It
    # must also select the Finalization transaction in the Execution Host;
    # otherwise the host defaults to an implementation pipeline.
    if STATUS_RECONCILIATION_OF_PATTERN.search(prompt.read_text(encoding="utf-8")):
        arguments.extend(("--transaction-kind", "FINALIZATION"))
    if phase and phase not in TERMINAL_PHASES:
        arguments.append("--resume")
    execution_started_at = datetime.now(timezone.utc)
    completed = LocalProcessProvider().execute(repo, arguments)
    return execution_started_at, completed


def once(repo: Path, root: Path, interval: float = 1.0, *, background: bool = False) -> int:
    """Process at most one stable job; all repository mutations remain runner-owned."""
    logger = component_logger(repo, "inbox")
    areas = local_folders(repo)
    with _lock(repo):
        # A terminal report can be written by a runner that lost storage access
        # before it could publish its history projection.  Reconcile those
        # immutable reports before presenting or admitting the next job.  The
        # operation is idempotent and never changes an existing projection.
        try:
            backfill_prompt_history(repo)
        except EngineeringStorageError as error:
            status(
                repo,
                "JOB_FAILED",
                queued_jobs=0,
                queue_items=[],
                diagnostic="De canonieke Execution Host-opslag is niet beschikbaar.",
            )
            log_event(logger, logging.ERROR, "prompt_history_reconciliation_failed", diagnostic=str(error))
            return 1
        reconciled = reconcile_stale(repo)
        if reconciled:
            log_event(logger, logging.WARNING, "active_run_lease_reconciled", diagnostic=f"reconciled_runs={len(reconciled)}")
        candidates = _scan_queue(root, interval)
        log_event(logger, logging.DEBUG, "inbox_scan", diagnostic=f"eligible_jobs={len(candidates)}")
        waiting_merge = _operator_merge_wait(repo)
        if waiting_merge is not None:
            if _operator_merge_poll_due(repo, waiting_merge.run_id):
                prompt = Path(waiting_merge.prompt_path)
                if prompt.is_file():
                    _execute_runner_command(repo, prompt, waiting_merge.run_id)
                    try:
                        waiting_merge = StateStore(repo / ".engineering" / "engineering-runs").load(waiting_merge.run_id)
                    except StateError:
                        waiting_merge = None
                    if waiting_merge is not None and waiting_merge.terminal:
                        _finalize_operator_merge_wait(repo, waiting_merge)
                        return 0 if waiting_merge.phase == "COMPLETE" else 1
                    if (
                        waiting_merge is not None
                        and waiting_merge.phase != OPERATOR_MERGE_WAIT_PHASE
                    ):
                        _publish_resumed_merge_transition(
                            repo,
                            waiting_merge,
                            queue_items=_queue_items(candidates),
                            queue_depth=len(candidates),
                        )
                        return 0
                _publish_operator_merge_wait(
                    repo, waiting_merge, queue_items=_queue_items(candidates), queue_depth=len(candidates),
                )
            # The previous projection remains authoritative between bounded
            # reconciliation polls.  Do not rewrite its timestamp each cycle,
            # otherwise the poll would never become due.
            return 0
        child_run_id = os.environ.get(BACKGROUND_RUN_ID_ENVIRONMENT)
        child_job_id = os.environ.get(BACKGROUND_JOB_ID_ENVIRONMENT)
        if _active_transaction(repo) and not child_run_id:
            _publish_active_queue(repo, candidates)
            log_event(logger, logging.DEBUG, "active_transaction_queue_refreshed", diagnostic=f"eligible_jobs={len(candidates)}")
            return 0
        if not candidates:
            log_event(logger, logging.DEBUG, "watcher_idle")
            status(repo, "WATCHER_IDLE", queued_jobs=0, queue_items=[])
            return 0
        admission = _admit_queue_candidate(
            repo,
            candidates,
            child_run_id=child_run_id,
            child_job_id=child_job_id,
            logger=logger,
        )
        if admission.source is None or admission.content is None:
            return admission.exit_code
        source, raw_submission = admission.source, admission.content
        try:
            submission = parse_producer_submission(raw_submission)
        except ProducerSubmissionError as error:
            status(
                repo, "INVALID_PRODUCER_SUBMISSION", queued_jobs=len(candidates),
                queue_items=_queue_items(candidates), run_id=None,
                current_action="Producer Submission Envelope kon niet veilig worden geclaimd.",
                diagnostic="Producer Submission Envelope is ongeldig.",
            )
            log_event(logger, logging.ERROR, "producer_submission_invalid", diagnostic=str(error))
            return 1
        content = submission.prompt
        job_id, legacy_run_id, digest = _job_id(source, raw_submission)
        # Allocate the execution identity before admission so observed
        # preflight work is attributable to the candidate that was actually
        # considered.  A rejected candidate is completed with FAILED timing;
        # it is never retroactively assigned to a later submission.
        run_id = child_run_id or _allocate_run_id()
        # Persist the eligibility boundary before any admission preflight.
        # Source mtimes are filesystem transport details, not authoritative
        # queue evidence.  This timestamp is the first observed point at
        # which the watcher accepted the submission as eligible to claim.
        eligible_at = datetime.now(timezone.utc)
        title = _prompt_title(content, source.name)
        producer = submission.producer
        try:
            record_submission(
                repo,
                submission_id=submission.submission_id or job_id,
                producer_id=producer.producer_id,
                producer_type=producer.producer_type,
                producer_version=producer.producer_version,
                contract_version=submission.contract_version or producer.execution_constraint_version,
                prompt_content=content,
                prompt_metadata={"filename": source.name, "digest": digest, "title": title},
                target_identity={"repository": repo.name, "path": str(repo.resolve())},
                original_envelope=(
                    raw_submission if not submission.is_legacy
                    else {"transport": "inbox", "filename": source.name, "content": raw_submission}
                ),
                correlation_id=producer.correlation_id,
                mission_id=producer.mission_id,
                engineering_action_id=producer.engineering_action_id,
                link_run_id=run_id,
                execution_context=submission.execution_context,
                forge_governance_handoff=submission.forge_governance_handoff,
                received_at=eligible_at.isoformat(),
            )
        except EngineeringStorageError as error:
            status(repo, "JOB_FAILED", queued_jobs=len(candidates), queue_items=_queue_items(candidates), diagnostic="De canonieke Execution Host-opslag is niet beschikbaar.")
            log_event(logger, logging.ERROR, "submission_persist_failed", run_id=run_id, diagnostic=str(error))
            return 1
        host_preflight_phase = start_phase(repo, run_id, "HOST_PREFLIGHT", category="ADMISSION")
        try:
            preflight = execute_host_preflight(repo, run_id=run_id)
        except Exception:
            complete_phase(repo, host_preflight_phase, outcome="FAILED")
            raise
        complete_phase(repo, host_preflight_phase, outcome="COMPLETE" if preflight.outcome != "FAIL" else "FAILED")
        if preflight.outcome == "FAIL":
            status(
                repo,
                "HOST_PREFLIGHT_FAILED",
                queued_jobs=len(candidates),
                queue_items=_queue_items(candidates),
                run_id=None,
                current_action="Execution Host preflight blokkeert het claimen van Inbox-werk.",
                diagnostic=drift_summary(preflight.drift_evidence),
            )
            log_event(
                logger,
                logging.ERROR,
                "host_preflight_failed",
                run_id=legacy_run_id,
                diagnostic=_preflight_failure_detail(preflight),
            )
            return 1
        workspace_preflight_phase = start_phase(repo, run_id, "WORKSPACE_PREFLIGHT", category="ADMISSION")
        try:
            workspace_preflight = execute_workspace_preflight(repo, content, run_id=run_id)
        except Exception:
            complete_phase(repo, workspace_preflight_phase, outcome="FAILED")
            raise
        complete_phase(repo, workspace_preflight_phase, outcome="COMPLETE" if workspace_preflight.outcome != "FAIL" else "FAILED")
        if workspace_preflight.outcome == "FAIL":
            status(
                repo,
                "WORKSPACE_PREFLIGHT_FAILED",
                queued_jobs=len(candidates),
                queue_items=_queue_items(candidates),
                run_id=None,
                current_action="Workspace preflight blokkeert het claimen van Inbox-werk.",
                diagnostic=drift_summary(workspace_preflight.drift_evidence),
            )
            log_event(
                logger,
                logging.ERROR,
                "workspace_preflight_failed",
                run_id=legacy_run_id,
                diagnostic=_preflight_failure_detail(workspace_preflight),
            )
            return 1
        capability_preflight_phase = start_phase(repo, run_id, "CAPABILITY_PREFLIGHT", category="ADMISSION")
        try:
            capability_preflight = execute_capability_preflight(repo, content, run_id=run_id)
        except Exception:
            complete_phase(repo, capability_preflight_phase, outcome="FAILED")
            raise
        complete_phase(repo, capability_preflight_phase, outcome="COMPLETE" if capability_preflight.outcome != "FAIL" else "FAILED")
        if capability_preflight.outcome == "FAIL":
            status(repo, "CAPABILITY_PREFLIGHT_FAILED", queued_jobs=len(candidates), queue_items=_queue_items(candidates), run_id=None,
                   current_action="Capability Preflight blokkeert het claimen van Inbox-werk.",
                   diagnostic=drift_summary(capability_preflight.drift_evidence))
            log_event(
                logger,
                logging.ERROR,
                "capability_preflight_failed",
                run_id=legacy_run_id,
                diagnostic=_preflight_failure_detail(capability_preflight),
            )
            return 1
        if _already_seen(areas, job_id):
            status(
                repo,
                "WATCHER_IDLE",
                queued_jobs=len(candidates) - 1,
                queue_items=_queue_items(candidates, source),
                job_id=job_id,
                diagnostic="Een dubbele opdracht is al geregistreerd.",
            )
            log_event(logger, logging.WARNING, "duplicate_job_ignored", run_id=legacy_run_id)
            return 0
        if background and not child_run_id:
            return _detach_runner(repo, root, candidates, source, content, job_id, run_id, logger)
        claimed = _archive_path(areas["Running"], job_id, source)
        status(repo, "JOB_CLAIMED", queued_jobs=len(candidates) - 1, queue_items=_queue_items(candidates, source), job_id=job_id, run_id=run_id, submitted_filename=source.name, prompt_title=title,
               blocking_predecessor_run=None, blocking_predecessor_phase=None, blocking_predecessor_filename=None,
               blocking_predecessor_title=None, predecessor_recovery_action=None)
        # The queue boundary ends at the observed claim, rather than after
        # runner initialization or readiness work.  This keeps queue delay
        # distinct from active Execution Host processing.
        claimed_at = datetime.now(timezone.utc)
        record_queue_wait_from_submission(repo, run_id, claimed_at=claimed_at)
        start_or_resume_phase(
            repo, run_id, "TOTAL_EXECUTION", category="EXECUTION", started_at=claimed_at,
        )
        claim = start_phase(repo, run_id, "SUBMISSION_CLAIM", category="ADMISSION", started_at=claimed_at)
        log_event(logger, logging.INFO, "job_claimed", run_id=run_id)
        _move(source, claimed)
        local = repo / ".engineering" / "inbox-processing" / job_id
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
                    "retry": retry_metadata(content),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        complete_phase(repo, claim)
        status(
            repo, "RUNNER_STARTING", job_id=job_id, run_id=run_id, queued_jobs=len(candidates) - 1, queue_items=_queue_items(candidates, source),
            runner_pid=os.getpid(), submitted_filename=source.name, prompt_title=title,
        )
        log_event(logger, logging.INFO, "runner_started", run_id=run_id)
        execution_started_at, completed = _execute_runner_command(repo, prompt, run_id)
        phase, diagnostic = _runner_result(repo, run_id)
        if phase == OPERATOR_MERGE_WAIT_PHASE:
            try:
                waiting_state = StateStore(repo / ".engineering" / "engineering-runs").load(run_id)
            except StateError:
                waiting_state = None
            if waiting_state is not None:
                _publish_operator_merge_wait(
                    repo,
                    waiting_state,
                    queue_items=_queue_items(candidates, source),
                    queue_depth=len(candidates) - 1,
                    job_id=job_id,
                    filename=source.name,
                    title=title,
                )
                log_event(logger, logging.INFO, "operator_merge_wait_started", run_id=run_id)
                return 0
        terminal_phase = phase if phase in TERMINAL_PHASES else "FAILED"
        reason = diagnostic or (
            _runner_failure_detail(completed)
            if completed.returncode and phase is None
            else None
        ) or (
            "Engineeringrapport kon niet worden afgeleverd."
            if completed.returncode == 0
            else "De runner stopte zonder een veilig eindrapport."
        )
        report = _report(repo, run_id)
        delivered = None
        corrected_report = False
        if report and _report_matches_terminal_phase(report, terminal_phase):
            status(
                repo, "REPORT_PUBLISHING", job_id=job_id, run_id=run_id, queued_jobs=len(candidates) - 1, queue_items=_queue_items(candidates, source),
            )
            delivered = report
        else:
            corrected_report = True
            delivered = repo / ".engineering" / "reports" / f"corrected_{run_id}.md"
            delivered.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            delivered.write_text(
                _corrected_terminal_report(run_id, terminal_phase, reason), encoding="utf-8"
            )
            log_event(logger, logging.WARNING, "terminal_report_corrected", run_id=run_id)
        successful = completed.returncode == 0 and terminal_phase == "COMPLETE" and delivered is not None
        target = areas["Completed"] if successful else areas["Failed"]
        _move(claimed, _archive_path(target, job_id, source))
        final_state = (
            "JOB_COMPLETED"
            if successful
            else ("JOB_BLOCKED" if terminal_phase == "BLOCKED" else "JOB_FAILED")
        )
        if corrected_report:
            reason = redact_diagnostic(
                "Een checkpoint-conform eindrapport is afgeleverd voor deze uitvoering."
            )
        status(
            repo,
            final_state,
            job_id=job_id,
            run_id=run_id,
            queued_jobs=len(candidates) - 1,
            queue_items=_queue_items(candidates, source),
            runner_phase=terminal_phase,
            report=str(delivered) if delivered else None,
            diagnostic=reason,
            resume_instruction=f"Run engineering-execution-host with --run-id {run_id} --resume.",
            submitted_filename=source.name,
            prompt_title=title,
            last_executed_filename=source.name,
            last_executed_title=title,
            last_executed_run=run_id,
            last_executed_phase=terminal_phase,
        )
        log_event(
            logger,
            logging.INFO if successful else logging.ERROR,
            "job_completed" if successful else "job_failed",
            run_id=run_id,
            diagnostic=reason,
        )
        evidence_phase = start_phase(repo, run_id, "EVIDENCE_PERSISTENCE")
        try:
            target_checkout_path, tracked_file_count, target_branch = _terminal_workspace_snapshot(repo, run_id)
            execution_metadata = execution_metadata_from_terminal_report(delivered)
            record_prompt_execution(
                repo,
                run_id=run_id,
                terminal_state=terminal_phase,
                prompt_title=title,
                executed_at=datetime.now(timezone.utc),
                report=delivered,
                git_commit=_terminal_git_commit(repo, run_id),
                target_checkout_path=target_checkout_path,
                tracked_file_count=tracked_file_count,
                target_branch=target_branch,
                execution_metadata=execution_metadata,
                **retry_metadata(content),
            )
            record_artifact(
                repo,
                delivered,
                artifact_id=f"report:{run_id}",
                artifact_type="TERMINAL_REPORT",
                content_type="text/markdown",
                created_at=datetime.now(timezone.utc).isoformat(),
                run_id=run_id,
                mission_id=parse_producer_metadata(content).mission_id,
                producer_id=parse_producer_metadata(content).producer_id,
            )
        except Exception as error:
            log_event(
                logger,
                logging.WARNING,
                "prompt_history_persist_failed",
                run_id=run_id,
                diagnostic=str(error),
            )
        try:
            execution_seconds, usage, repository = _telemetry_values(repo, run_id)
            lineage = retry_metadata(content)
            runtime_metadata = _report_runtime_metadata(delivered)
            producer = parse_producer_metadata(content)
            persist_execution_async(
                repo,
                ExecutionTelemetry(
                    run_id=run_id,
                    arrived_at=eligible_at,
                    execution_started_at=execution_started_at,
                    execution_finished_at=datetime.now(timezone.utc),
                    terminal_state=terminal_phase,
                    execution_seconds=execution_seconds,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    total_tokens=usage["total_tokens"],
                    execution_mode="GENESIS" if "Execution Mode: Genesis" in content else "MANAGED",
                    workspace=repo.name,
                    repository=repository,
                    execution_host_version=EngineeringPlatformManifest.load(
                        Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json")
                    ).platform_version,
                    retry_of=lineage["retry_of"],
                    original_run_id=lineage["original_run_id"],
                    retry_generation=lineage["retry_generation"],
                    retry_timestamp=lineage["retry_timestamp"],
                    prompt_characters=len(content),
                    execution_metadata=execution_metadata_from_terminal_report(delivered),
                    producer=producer,
                    **runtime_metadata,
                ),
                on_error=lambda error: log_event(
                    logger,
                    logging.WARNING,
                    "telemetry_persist_failed",
                    run_id=run_id,
                    diagnostic=str(error),
                ),
            )
        except Exception as error:
            log_event(
                logger,
                logging.WARNING,
                "telemetry_schedule_failed",
                run_id=run_id,
                diagnostic=str(error),
            )
        complete_phase(repo, evidence_phase)
        complete_active_phase(repo, run_id, "TOTAL_EXECUTION", outcome="COMPLETE" if successful else "FAILED")
        return 0 if successful else (completed.returncode or 1)


def launch_agent(repo: Path) -> Path:
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher = [sys.executable, "-m", "tools.engineering.inbox_watcher", "run", "--repo", str(repo)]
    command = f"cd {shlex.quote(str(repo))} && exec " + " ".join(shlex.quote(value) for value in launcher)
    arguments = f"<string>/bin/zsh</string><string>-lc</string><string>{escape(command)}</string>"
    runtime_environment = execution_host_configuration(repo).runtime_environment()
    environment = runtime_environment["PATH"]
    runtime_executable = runtime_environment[RUNTIME_EXECUTABLE_ENVIRONMENT]
    log_level = os.environ.get(LOG_LEVEL_ENVIRONMENT, DEFAULT_LOG_LEVEL).upper()
    if log_level not in VALID_LEVELS:
        log_level = DEFAULT_LOG_LEVEL
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>EnvironmentVariables</key><dict><key>PATH</key><string>{environment}</string><key>{RUNTIME_EXECUTABLE_ENVIRONMENT}</key><string>{runtime_executable}</string><key>{LOG_LEVEL_ENVIRONMENT}</key><string>{log_level}</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer></dict></plist>',
        encoding="utf-8",
    )
    return destination


def doctor(repo: Path, root: Path) -> int:
    transport = folders(root)
    areas = local_folders(repo)
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    checks = {
        "repository_runner": (repo / "tools/engineering/engineering-execution-host").is_file(),
        "inbox_writable": os.access(transport["Inbox"], os.W_OK),
        "local_archives_writable": os.access(areas["Completed"], os.W_OK),
        "launch_agent": agent.is_file(),
        "gitignored": ".engineering/" in (repo / ".gitignore").read_text(encoding="utf-8"),
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
                "inbox": str(transport["Inbox"]),
                "local_archives": str(areas["Completed"]),
                "checks": checks,
            },
            indent=2,
        )
    )
    return 0 if state == "REMOTE_ENGINEERING_READY" else 1


def migrate_icloud_archives(repo: Path, root: Path) -> dict[str, int]:
    """Move legacy iCloud archives into EP storage, leaving only Inbox behind."""
    local = local_folders(repo)
    targets = {
        "Running": local["Running"],
        "Completed": local["Completed"],
        "Failed": local["Failed"],
        "Reports": repo / ".engineering" / "reports",
    }
    moved = deleted = 0
    for name, target in targets.items():
        source_directory = root / name
        if not source_directory.is_dir():
            continue
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for source in source_directory.iterdir():
            if source.is_symlink() or not source.is_file():
                continue
            destination = target / source.name
            if destination.exists():
                source.unlink()
                deleted += 1
            else:
                _move(source, destination)
                moved += 1
        source_directory.rmdir()
    for name in ("status.json", "status.md"):
        source = root / name
        destination = repo / ".engineering" / "status" / name
        if not source.is_file() or source.is_symlink():
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            source.unlink()
            deleted += 1
        else:
            _move(source, destination)
            moved += 1
    return {"moved": moved, "deleted_duplicates": deleted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("once", "run", "status", "install", "uninstall", "doctor", "migrate-icloud-archives")
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--icloud-root")
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    provision_workspace(repo)
    root = cloud_root(args.icloud_root, repo)
    if args.command == "once":
        return once(repo, root, 0.0, background=True)
    if args.command == "run":
        logger = component_logger(repo, "inbox")
        lifecycle_context = component_lifecycle_context(
            repo,
            version=WATCHER_VERSION,
            launchd_label=LABEL,
            launch_agent_path=Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist",
        )
        try:
            with single_instance(repo, "inbox-watcher"):
                with shutdown_signal_logging(logger, lifecycle_context):
                    log_event(logger, logging.INFO, "watcher_started", context=lifecycle_context)
                    try:
                        while True:
                            try:
                                once(repo, root, 1.0, background=True)
                            except RuntimeError as error:
                                # An older detached runner may still own the
                                # pre-fix lock.  Its lock must not prevent this
                                # watcher from publishing a read-only Inbox
                                # snapshot while that run completes.
                                candidates = [
                                    (path, content)
                                    for path in discover(root, 0.0)
                                    if (content := stable_prompt(path, 0.0)) is not None
                                ]
                                if _active_transaction(repo):
                                    _publish_active_queue(repo, candidates)
                                    log_event(
                                        logger,
                                        logging.DEBUG,
                                        "active_transaction_queue_refreshed",
                                        diagnostic=f"eligible_jobs={len(candidates)}",
                                    )
                                else:
                                    status(
                                        repo,
                                        "WAITING_FOR_REPOSITORY",
                                        diagnostic="Een andere watcher beheert de lokale Inbox-vergrendeling.",
                                    )
                                    log_event(logger, logging.ERROR, "watcher_cycle_failed", diagnostic=str(error))
                            time.sleep(max(5, args.interval))
                    finally:
                        log_event(
                            logger,
                            logging.INFO,
                            "watcher_shutdown_completed",
                            context=lifecycle_context,
                        )
        except KeyboardInterrupt:
            return 0
        except DuplicateComponentInstanceError as error:
            log_event(logger, logging.ERROR, "duplicate_watcher_refused", diagnostic=str(error))
            return 1
    if args.command == "status":
        print(
            (repo / ".engineering" / "status" / "status.md").read_text(encoding="utf-8")
            if (repo / ".engineering" / "status" / "status.md").exists()
            else "WATCHER_IDLE"
        )
        return 0
    if args.command == "migrate-icloud-archives":
        print(json.dumps(migrate_icloud_archives(repo, root), sort_keys=True))
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
