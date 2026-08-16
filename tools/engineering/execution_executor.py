"""Codex execution evidence helpers, isolated from lifecycle coordination."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .agent_state import redact_diagnostic


def project_codex_activity(event: object) -> str | None:
    """Map one JSONL event to an approved, prompt-free activity label."""
    if not isinstance(event, dict) or event.get("type") not in {"item.started", "item.updated"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    return {
        "reasoning": "Codex plant de volgende stap",
        "command_execution": "Codex voert een opdracht uit",
        "file_change": "Codex bewerkt bestanden",
        "web_search": "Codex onderzoekt referentiemateriaal",
        "mcp_tool_call": "Codex gebruikt een ontwikkeltool",
        "agent_message": "Codex formuleert het resultaat",
    }.get(item.get("type"))


def project_codex_command_event(event: object) -> tuple[str, str, str] | None:
    """Expose direct command boundaries without retaining command content.

    Codex JSONL identifies command-execution items by a stable item id.  The
    host uses this small projection only to classify known validation tools and
    record their observed start/complete boundaries.  It must not persist the
    raw command, its output, or any arguments.
    """
    if not isinstance(event, dict) or event.get("type") not in {"item.started", "item.completed"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    if event["type"] == "item.completed":
        return ("completed", item_id, "")
    command = item.get("command")
    if not isinstance(command, str):
        return None
    return ("started", item_id, command)


def redacted_cli_tail(value: str, prompt: str, *, limit: int = 1_200) -> str:
    without_prompt = value.replace(prompt, "[PROMPT_OMITTED]") if prompt else value
    return redact_diagnostic("\n".join(without_prompt.splitlines()[-60:]), limit=limit) or "(empty)"


def format_cli_failure(exit_code: int, stderr: str, stdout: str, prompt: str = "") -> str:
    return "\n".join((f"Codex CLI exit code: {exit_code}", f"stderr tail: {redacted_cli_tail(stderr, prompt)}", f"stdout tail: {redacted_cli_tail(stdout, prompt)}"))


def write_redacted_codex_cli_log(root: Path, run_id: str, detail: str) -> Path:
    directory = root / ".engineering" / "logs" / "codex"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{run_id}.log"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("# Redacted Codex CLI diagnostic\n\n" + redact_diagnostic(detail, limit=3_000) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


# Provider-backed engineering executor.
import json
import subprocess
import time
from dataclasses import replace
from typing import Callable

from .capability_review import ReviewerResult, ReviewerSelection, reviewer_prompt
from .codex_observability import codex_final_message as _codex_final_message, extract_codex_runtime_metadata, extract_codex_usage
from .execution_context import additional_workspace_write_roots
from .execution_models import AgentResult
from .platform_version import detected_codex_cli_version
from .providers import CodexCliProvider
from .execution_errors import CodexInvocationError, RunnerError

_format_cli_failure = format_cli_failure

# A managed Engineering transaction has already passed host-owned admission,
# repository synchronization and an exclusive execution lease.  It must be
# able to create its bounded branch, commit, and draft PR; `workspace-write`
# deliberately rejects Git index writes and therefore cannot complete that
# contract.  Review-only invocations remain read-only below.
MANAGED_EXECUTION_SANDBOX = "danger-full-access"

class CodexCliClient:
    def __init__(self, provider: CodexCliProvider | None = None) -> None:
        self.provider = provider or CodexCliProvider()
        self.last_usage: dict[str, int | float | str] = {}
        self.last_execution_seconds: float | None = None
        self.last_runtime_metadata: dict[str, str] = {"runtime_provider": "codex_cli"}
        # This deliberately contains only aggregate counters.  Command text,
        # paths and command output are never retained in live or terminal
        # execution metadata.
        self.last_execution_metadata: dict[str, int] = {
            "modified": 0,
            "created": 0,
            "deleted": 0,
            "codex_commands_executed": 0,
        }
        self._activity_callback: Callable[[str], None] | None = None
        self._process_callback: Callable[[dict[str, int] | None], None] | None = None
        self._runtime_metadata_callback: Callable[[dict[str, str]], None] | None = None
        self._command_callback: Callable[[str, str, str], None] | None = None
        self._workspace_progress_callback: Callable[[dict[str, int]], None] | None = None

    def set_activity_callback(self, callback: Callable[[str], None] | None) -> None:
        """Set the optional local-only sink for safe live activity labels."""
        self._activity_callback = callback

    def set_process_callback(self, callback: Callable[[dict[str, int] | None], None] | None) -> None:
        """Set the owned foreground Codex-process sink for runtime metrics."""
        self._process_callback = callback

    def set_runtime_metadata_callback(
        self, callback: Callable[[dict[str, str]], None] | None
    ) -> None:
        """Publish only explicitly reported runtime settings during a live run."""
        self._runtime_metadata_callback = callback

    def set_command_callback(self, callback: Callable[[str, str, str], None] | None) -> None:
        """Set a direct JSONL command-boundary sink for execution telemetry."""
        self._command_callback = callback

    def set_workspace_progress_callback(
        self, callback: Callable[[dict[str, int]], None] | None
    ) -> None:
        """Set a bounded, filename-free workspace change counter sink."""
        self._workspace_progress_callback = callback

    def available(self) -> bool:
        return self.provider.command("--version").returncode == 0

    def version(self) -> str:
        completed = self.provider.command("--version")
        if completed.returncode:
            raise RunnerError("Codex CLI version could not be detected")
        return detected_codex_cli_version(completed.stdout)

    def review(self, root: Path, selection: ReviewerSelection, objective: str) -> ReviewerResult:
        self.last_usage = {}
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["contribution", "recommendations"],
            "properties": {
                "contribution": {"type": "string", "maxLength": 240},
                "recommendations": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 240},
                },
            },
        }
        state_directory = root / ".engineering"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", dir=state_directory, delete=False
        ) as handle:
            json.dump(schema, handle)
            schema_path = Path(handle.name)
        try:
            completed = self.provider.invoke(
                root,
                (
                    "codex",
                    "exec",
                    "--sandbox",
                    "read-only",
                    "-C",
                    str(root),
                    "--json",
                    "--output-schema",
                    str(schema_path),
                    reviewer_prompt(selection, objective),
                ),
            )
            self.last_usage = extract_codex_usage(completed.stdout, completed.stderr)
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            return ReviewerResult(
                selection.reviewer,
                "Reviewer invocation failed; primary review continues.",
                failed=True,
            )
        try:
            raw = json.loads(_codex_final_message(completed.stdout))
            return ReviewerResult(
                selection.reviewer,
                str(raw["contribution"]),
                tuple(str(value) for value in raw["recommendations"]),
            )
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return ReviewerResult(
                selection.reviewer,
                "Reviewer returned invalid advice; primary review continues.",
                failed=True,
            )

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.last_usage = {}
        self.last_execution_seconds = None
        self.last_runtime_metadata = {"runtime_provider": "codex_cli"}
        state_directory = root / ".engineering" / "engineering-runs"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "terminal_state",
                "branch",
                "pull_request",
                "terminal_condition",
                "diagnostic",
                "repository_path",
                "commit_sha",
                "validation_evidence",
            ],
            "properties": {
                "terminal_state": {
                    "type": "string",
                    "enum": ["COMPLETE", "WAITING", "BLOCKED", "FAILED"],
                },
                "branch": {"type": ["string", "null"]},
                "pull_request": {"type": ["integer", "null"]},
                "terminal_condition": {
                    "type": "string",
                    "enum": [
                        "repository_reconciled",
                        "open_pr_checks_terminal",
                        "external_blocked",
                        "local_commit_reconciled",
                    ],
                },
                "diagnostic": {"type": "string", "maxLength": 500},
                "repository_path": {"type": ["string", "null"]},
                "commit_sha": {"type": ["string", "null"], "pattern": "^[0-9a-f]{40}$"},
                "validation_evidence": {
                    "type": "array", "maxItems": 12,
                    "items": {"type": "object", "additionalProperties": False,
                              "required": ["command", "result"],
                              "properties": {"command": {"type": "string", "maxLength": 240}, "result": {"type": "string", "maxLength": 240}}},
                },
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", dir=state_directory, delete=False
        ) as handle:
            json.dump(schema, handle)
            schema_path = Path(handle.name)
        try:
            extra_roots = additional_workspace_write_roots(root)
            command = [
                "codex",
                "exec",
                "--sandbox",
                MANAGED_EXECUTION_SANDBOX,
                "-C",
                str(root),
                "--json",
            ]
            for extra_root in extra_roots:
                command.extend(("--add-dir", str(extra_root)))
            command.extend(("--output-schema", str(schema_path), prompt))
            started = time.monotonic()
            completed = self._run_invocation(tuple(command), root)
            self.last_execution_seconds = round(time.monotonic() - started, 3)
            self.last_usage = extract_codex_usage(completed.stdout, completed.stderr)
            self.last_runtime_metadata = extract_codex_runtime_metadata(
                completed.stdout, completed.stderr
            )
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            detail = _format_cli_failure(completed.returncode, completed.stderr, completed.stdout, prompt)
            raise CodexInvocationError(
                f"Codex CLI exited with code {completed.returncode}; inspect this invocation's console output.",
                detail,
            )
        try:
            raw = json.loads(_codex_final_message(completed.stdout))
            result = AgentResult(**raw)
            if not isinstance(result.validation_evidence, (list, tuple)):
                raise TypeError("validation evidence must be a list")
            result = replace(
                result,
                validation_evidence=tuple(
                    {"command": redact_diagnostic(item.get("command", ""), limit=240), "result": redact_diagnostic(item.get("result", ""), limit=240)}
                    for item in result.validation_evidence
                    if isinstance(item, dict) and item.get("command") and item.get("result")
                ),
            )
            if result.diagnostic is not None:
                result = replace(result, diagnostic=redact_diagnostic(result.diagnostic))
            return result
        except (IndexError, json.JSONDecodeError, TypeError) as error:
            raise CodexInvocationError(
                "Codex CLI did not return the required structured terminal result.",
                _format_cli_failure(completed.returncode, completed.stderr, completed.stdout, prompt),
            ) from error

    def _run_invocation(
        self, command: tuple[str, ...], root: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run Codex, streaming only the approved activity projection when enabled."""
        self.last_execution_metadata = {
            "modified": 0,
            "created": 0,
            "deleted": 0,
            "codex_commands_executed": 0,
        }
        if (
            self._activity_callback is None
            and self._command_callback is None
            and self._runtime_metadata_callback is None
            and self._workspace_progress_callback is None
        ):
            return self.provider.invoke(root, command)
        process = self.provider.spawn(root, command)
        if self._process_callback is not None:
            try:
                self._process_callback({"pid": process.pid, "process_group": os.getpgid(process.pid)})
            except OSError:
                self._process_callback(None)
        lines: list[str] = []
        last_workspace_progress: dict[str, int] | None = None
        observed_command_ids: set[str] = set()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if self._workspace_progress_callback is not None:
                    progress = workspace_change_summary(root)
                    self.last_execution_metadata.update(progress)
                    if progress != last_workspace_progress:
                        self._workspace_progress_callback(dict(self.last_execution_metadata))
                        last_workspace_progress = progress
                observed_metadata = extract_codex_runtime_metadata(line)
                if len(observed_metadata) > 1:
                    self.last_runtime_metadata.update(observed_metadata)
                    if self._runtime_metadata_callback is not None:
                        self._runtime_metadata_callback(dict(self.last_runtime_metadata))
                try:
                    event = json.loads(line)
                    activity = project_codex_activity(event)
                except json.JSONDecodeError:
                    activity = None
                    event = None
                if activity is not None and self._activity_callback is not None:
                    self._activity_callback(activity)
                if self._command_callback is not None:
                    command_event = project_codex_command_event(event)
                    if command_event is not None:
                        if command_event[0] == "started" and command_event[1] not in observed_command_ids:
                            observed_command_ids.add(command_event[1])
                            self.last_execution_metadata["codex_commands_executed"] += 1
                            if self._workspace_progress_callback is not None:
                                self._workspace_progress_callback(dict(self.last_execution_metadata))
                        self._command_callback(*command_event)
            return subprocess.CompletedProcess(command, process.wait(), "".join(lines), "")
        finally:
            if self._process_callback is not None:
                self._process_callback(None)


def workspace_change_summary(root: Path) -> dict[str, int]:
    """Return only aggregate Git worktree changes for the live status surface."""
    try:
        completed = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return {"modified": 0, "created": 0, "deleted": 0}
    if completed.returncode:
        return {"modified": 0, "created": 0, "deleted": 0}
    summary = {"modified": 0, "created": 0, "deleted": 0}
    for line in completed.stdout.splitlines():
        status = line[:2]
        if status == "??" or "A" in status:
            summary["created"] += 1
        elif "D" in status:
            summary["deleted"] += 1
        elif status.strip():
            summary["modified"] += 1
    return summary
