"""Codex execution evidence helpers, isolated from lifecycle coordination."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from threading import Event, Thread
from typing import Callable, Mapping

from .capability_review import ReviewerResult, ReviewerSelection, reviewer_prompt
from .codex_observability import codex_final_message as _codex_final_message, extract_codex_runtime_metadata, extract_codex_usage
from .evidence_projection import ToolProxyEnvironment
from .execution_context import additional_workspace_write_roots
from .execution_errors import CodexHandoffTimeout, CodexInvocationError, RunnerError
from .execution_timeout_policy import SPECIALIST_REVIEW
from .execution_models import AgentResult
from .platform_version import detected_codex_cli_version
from .provider_usage import churn_from_jsonl, usage_from_jsonl, usage_snapshots_from_jsonl
from .providers import CodexCliProvider
from .reviewer_evidence import ReviewerEvidence
from .storage import EngineeringStorageError, open_storage, record_artifact, verify_artifact_integrity
from .agent_state import redact_diagnostic
from .component_logging import component_logger, log_event


_LIVE_ACTION_NAME_DISALLOWED = re.compile(r"(?:https?://|[/\\\\`]|\b(?:api[_ -]?key|token|secret|password|authorization|bearer)\b)", re.IGNORECASE)
_CODEX_USAGE_LIMIT = re.compile(
    r"(?:you(?:'ve| have) hit your usage limit|purchase more credits|try again at)",
    re.IGNORECASE,
)
_QUALITY_EVIDENCE_ACTIVITIES = frozenset({"REFACTOR", "TEST_COVERAGE", "DOCUMENTATION", "VALIDATION", "NO_CHANGE_REQUIRED"})
MAX_RETAINED_VALIDATION_OUTPUT_CHARACTERS = 8_000
REVIEWER_INVOCATION_TIMEOUT_SECONDS = SPECIALIST_REVIEW.seconds
_VALIDATION_STREAM_LIMIT = MAX_RETAINED_VALIDATION_OUTPUT_CHARACTERS // 2
_UNITTEST_FAILURE = re.compile(r"^(?:FAIL|ERROR): [^(]+ \(([^)]+)\)$", re.MULTILINE)
_UNITTEST_COUNTS = re.compile(r"FAILED \((?P<details>[^)]*)\)")
_UNITTEST_COUNT = re.compile(r"\b(?P<name>failures|errors)=(?P<count>\d+)\b")
_TURN_ABORTED = re.compile(r'"type"\s*:\s*"turn_aborted"[^\n]*"reason"\s*:\s*"interrupted"', re.IGNORECASE)


def provider_turn_interruption(stdout: str, stderr: str) -> str | None:
    """Classify only provider-proven interrupted turns without inventing a result."""
    if _TURN_ABORTED.search(f"{stdout}\n{stderr}"):
        return "interrupted"
    return None


def codex_failure_disposition(
    exit_code: int, stdout: str, stderr: str
) -> tuple[str, str, str]:
    """Return the safe action and checkpoint status for a Codex CLI failure.

    A provider-side quota is not an implementation failure and, crucially, is
    not evidence that a prior pull request still awaits an operator.  Keep the
    provider wording out of durable state while retaining a specific recovery
    path for the Operations Console.
    """
    if _CODEX_USAGE_LIMIT.search(f"{stderr}\n{stdout}"):
        return (
            "resolve_codex_usage_limit",
            "codex_usage_limit_reached",
            "Codex usage limit reached. Add Codex credits or resume after the account limit resets.",
        )
    return (
        "inspect_codex_cli",
        "codex_invocation_failed",
        f"Codex CLI exited with code {exit_code}; inspect this invocation's console output.",
    )


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


def project_codex_live_action_name(event: object) -> str | None:
    """Return a short, transient Codex reasoning title when it is safe to show.

    This is intentionally separate from the persisted activity category.  The
    value is only rendered in the live status file and is removed when the run
    reaches a terminal phase; reports, history, diagnostics and the database
    never receive it.
    """
    if not isinstance(event, dict) or event.get("type") not in {"item.started", "item.updated"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    title = redact_diagnostic(text, limit=160)
    if len(title) < 4 or "[REDACTED]" in title or _LIVE_ACTION_NAME_DISALLOWED.search(title):
        return None
    return title


def project_codex_command_event(event: object) -> tuple[str, str, str, int | None] | None:
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
        exit_code = item.get("exit_code")
        return ("completed", item_id, "", exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None)
    command = item.get("command")
    if not isinstance(command, str):
        return None
    return ("started", item_id, command, None)


def redacted_cli_tail(value: str, prompt: str, *, limit: int = 1_200) -> str:
    without_prompt = value.replace(prompt, "[PROMPT_OMITTED]") if prompt else value
    return redact_diagnostic("\n".join(without_prompt.splitlines()[-60:]), limit=limit) or "(empty)"


def format_cli_failure(exit_code: int, stderr: str, stdout: str, prompt: str = "") -> str:
    return "\n".join((f"Codex CLI exit code: {exit_code}", f"stderr tail: {redacted_cli_tail(stderr, prompt)}", f"stdout tail: {redacted_cli_tail(stdout, prompt)}"))


def record_redacted_codex_cli_diagnostic(
    root: Path, run_id: str, detail: str, *, central_database: Path,
) -> None:
    """Persist a bounded CLI failure diagnostic through CENTRAL only.

    The former checkout-local ``.engineering/logs/codex`` file was a second
    durable operational log surface.  The Execution Host is owned by the
    lifecycle worker, so its diagnostic belongs to that canonical component
    identity and to the run that produced it.
    """
    logger = component_logger(root, "lifecycle_worker", central_database=central_database)
    log_event(
        logger,
        logging.WARNING,
        "codex_cli_diagnostic",
        run_id=run_id,
        diagnostic=redact_diagnostic(detail, limit=3_000),
        context={"target_component": "lifecycle_worker"},
    )


def _bounded_redacted_validation_tail(value: str | None, *, limit: int = _VALIDATION_STREAM_LIMIT) -> tuple[str | None, bool, bool]:
    if not isinstance(value, str):
        return None, False, False
    tail = value[-limit:]
    # Apply the repository's existing redactor line-by-line so traceback
    # boundaries remain useful while its secret policy remains authoritative.
    rendered = "\n".join(redact_diagnostic(line, limit=limit) for line in tail.replace("\x00", " ").splitlines())
    if len(rendered) > limit:
        rendered = rendered[-limit:]
    return rendered, len(value) > limit, rendered != tail


def validation_failure_artifact_id(command_id: str) -> str:
    return f"validation-failure-diagnostic-{command_id}"


def persist_validation_failure_diagnostic(
    root: Path, *, run_id: str, command_id: str, validation_id: str,
    control_identity: str, exit_code: int | None, stdout: str | None,
    stderr: str | None, capture_available: bool, captured_at: str | None = None,
    central_database: Path | None = None, artifact_root: Path | None = None,
) -> str:
    """Persist bounded, redacted, supplementary output for any failed control."""
    # Extract stable unittest identifiers/counts before the generic redactor
    # treats ``name=value`` failure summaries as environment assignments. Raw
    # output remains in process memory only and is never persisted wholesale.
    raw_combined = "\n".join(value for value in (stdout, stderr) if isinstance(value, str))
    identities = list(dict.fromkeys(_UNITTEST_FAILURE.findall(raw_combined)))[:20]
    details = _UNITTEST_COUNTS.search(raw_combined)
    counts = {match.group("name"): int(match.group("count")) for match in _UNITTEST_COUNT.finditer(details.group("details"))} if details else {}
    stdout_tail, stdout_truncated, stdout_redacted = _bounded_redacted_validation_tail(stdout)
    stderr_tail, stderr_truncated, stderr_redacted = _bounded_redacted_validation_tail(stderr)
    capture_is_available = capture_available and stdout_tail is not None and stderr_tail is not None
    created_at = captured_at or datetime.now(timezone.utc).isoformat()
    artifact_id = validation_failure_artifact_id(command_id)
    payload = {
        "schema": "deterministic-validation-failure-diagnostic-v1",
        "validation_id": validation_id, "command_id": command_id,
        "control_identity": control_identity, "authoritative_exit_code": exit_code,
        "captured_at": created_at,
        "capture_status": "AVAILABLE" if capture_is_available else "UNAVAILABLE",
        "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
        "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated,
        "retained_output_characters": sum(len(value or "") for value in (stdout_tail, stderr_tail)),
        "maximum_retained_output_characters": MAX_RETAINED_VALIDATION_OUTPUT_CHARACTERS,
        "truncation_strategy": "tail_per_stream",
        "redaction_applied": stdout_redacted or stderr_redacted,
        "redaction_policy": "agent_state.redact_diagnostic/v1",
        "failing_test_identities": identities,
        "failure_count": counts.get("failures"), "error_count": counts.get("errors"),
    }
    directory = ((artifact_root / "validation-failure-diagnostics") if artifact_root else
                 (root / ".engineering" / "artifacts" / "validation-failure-diagnostics"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{artifact_id}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{artifact_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    try:
        record_artifact(
            root, path, artifact_id=artifact_id,
            artifact_type="VALIDATION_FAILURE_DIAGNOSTIC",
            content_type="application/json", created_at=created_at, run_id=run_id,
            execution_id=command_id,
            central_database=central_database, artifact_root=artifact_root,
        )
    except EngineeringStorageError:
        path.unlink(missing_ok=True)
        raise
    return f"artifact:{artifact_id}"


def load_validation_failure_diagnostic(
    root: Path, artifact_reference: str, *, central_database: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, object] | None:
    """Read a bound diagnostic only after its immutable artifact verifies."""
    if not artifact_reference.startswith("artifact:"):
        return None
    artifact_id = artifact_reference.removeprefix("artifact:")
    if not verify_artifact_integrity(
        root, artifact_id, central_database=central_database, artifact_root=artifact_root,
    ):
        return None
    connection = open_storage(root) if central_database is None else sqlite3.connect(central_database.resolve(), isolation_level=None)
    try:
        row = connection.execute(
            "SELECT storage_location,artifact_type FROM execution_artifact_records WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row or row[1] != "VALIDATION_FAILURE_DIAGNOSTIC":
        return None
    try:
        authority_root = artifact_root.resolve() if artifact_root is not None else (root / ".engineering").resolve()
        payload_path = (authority_root / str(row[0])).resolve()
        payload_path.relative_to(authority_root)
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
        self.last_usage_snapshots: tuple[dict[str, int], ...] = ()
        self.last_churn: dict[str, int] = {}
        self.last_context_escalations: tuple[dict[str, object], ...] = ()
        self.last_execution_seconds: float | None = None
        self.last_runtime_metadata = self._runtime_metadata()
        # This deliberately contains only aggregate counters, plus the
        # approved EP-managed CLI prefix as invocation provenance. Command
        # text and command output are never retained in execution metadata.
        self.last_execution_metadata: dict[str, int] = {
            "modified": 0,
            "created": 0,
            "deleted": 0,
            "codex_commands_executed": 0,
        }
        self._activity_callback: Callable[[str], None] | None = None
        self._transient_action_callback: Callable[[str], None] | None = None
        self._process_callback: Callable[[dict[str, int] | None], None] | None = None
        self._runtime_metadata_callback: Callable[[dict[str, str]], None] | None = None
        self._command_callback: Callable[..., None] | None = None
        self._workspace_progress_callback: Callable[[dict[str, int]], None] | None = None
        self._handoff_deadline_callback: Callable[[], bool] | None = None

    def _runtime_metadata(self) -> dict[str, str]:
        metadata = {"runtime_provider": "codex_cli"}
        installation_path_reader = getattr(self.provider, "managed_installation_path", None)
        installation_path = installation_path_reader() if callable(installation_path_reader) else None
        if isinstance(installation_path, str) and installation_path:
            metadata["codex_cli_installation_path"] = installation_path
        return metadata

    def set_activity_callback(self, callback: Callable[[str], None] | None) -> None:
        """Set the optional local-only sink for safe live activity labels."""
        self._activity_callback = callback

    def set_transient_action_callback(self, callback: Callable[[str], None] | None) -> None:
        """Set the non-persistent sink for a safe live Codex action name."""
        self._transient_action_callback = callback

    def set_process_callback(self, callback: Callable[[dict[str, int] | None], None] | None) -> None:
        """Set the owned foreground Codex-process sink for runtime metrics."""
        self._process_callback = callback

    def set_runtime_metadata_callback(
        self, callback: Callable[[dict[str, str]], None] | None
    ) -> None:
        """Publish only explicitly reported runtime settings during a live run."""
        self._runtime_metadata_callback = callback

    def set_command_callback(self, callback: Callable[..., None] | None) -> None:
        """Set a direct JSONL command-boundary sink for execution telemetry."""
        self._command_callback = callback

    def set_workspace_progress_callback(
        self, callback: Callable[[dict[str, int]], None] | None
    ) -> None:
        """Set a bounded, filename-free workspace change counter sink."""
        self._workspace_progress_callback = callback

    def set_handoff_deadline_callback(self, callback: Callable[[], bool] | None) -> None:
        """Set a host-owned deadline check for an externally observable hand-off."""
        self._handoff_deadline_callback = callback

    def available(self) -> bool:
        return self.provider.command("--version").returncode == 0

    def version(self) -> str:
        completed = self.provider.command("--version")
        if completed.returncode:
            raise RunnerError("Codex CLI version could not be detected")
        return detected_codex_cli_version(completed.stdout)

    def review(
        self,
        root: Path,
        selection: ReviewerSelection,
        objective: str,
        evidence: ReviewerEvidence | None = None,
    ) -> ReviewerResult:
        self.last_usage = {}
        self.last_churn = {}
        self.last_context_escalations = ()
        self.last_execution_seconds = None
        self.last_runtime_metadata = self._runtime_metadata()
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
            started = time.monotonic()
            proxy = ToolProxyEnvironment()
            with proxy as environment:
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
                        reviewer_prompt(selection, objective, evidence),
                    ), environment=environment, timeout=REVIEWER_INVOCATION_TIMEOUT_SECONDS,
                )
            self.last_context_escalations = proxy.context_escalations()
            self.last_execution_seconds = round(time.monotonic() - started, 3)
            self.last_usage = extract_codex_usage(completed.stdout, completed.stderr)
            self.last_usage.update(usage_from_jsonl(completed.stdout, completed.stderr))
            self.last_usage_snapshots = usage_snapshots_from_jsonl(completed.stdout, completed.stderr)
            self.last_churn = churn_from_jsonl(completed.stdout, completed.stderr)
            self.last_runtime_metadata.update(extract_codex_runtime_metadata(
                completed.stdout, completed.stderr
            ))
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            return ReviewerResult(
                selection.reviewer,
                "Reviewer invocation failed; primary review continues.",
                failed=True,
                usage=dict(self.last_usage), runtime_metadata=dict(self.last_runtime_metadata),
                churn=dict(self.last_churn), duration_seconds=self.last_execution_seconds,
                usage_snapshots=self.last_usage_snapshots,
            )
        try:
            raw = json.loads(_codex_final_message(completed.stdout))
            return ReviewerResult(
                selection.reviewer,
                str(raw["contribution"]),
                tuple(str(value) for value in raw["recommendations"]),
                usage=dict(self.last_usage), runtime_metadata=dict(self.last_runtime_metadata),
                churn=dict(self.last_churn), duration_seconds=self.last_execution_seconds,
                usage_snapshots=self.last_usage_snapshots,
            )
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return ReviewerResult(
                selection.reviewer,
                "Reviewer returned invalid advice; primary review continues.",
                failed=True,
                usage=dict(self.last_usage), runtime_metadata=dict(self.last_runtime_metadata),
                churn=dict(self.last_churn), duration_seconds=self.last_execution_seconds,
                usage_snapshots=self.last_usage_snapshots,
            )

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.last_usage = {}
        self.last_usage_snapshots = ()
        self.last_churn = {}
        self.last_context_escalations = ()
        self.last_execution_seconds = None
        self.last_runtime_metadata = self._runtime_metadata()
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
                "quality_evidence",
                "validation_disposition",
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
                "quality_evidence": {
                    "type": "array", "maxItems": 8,
                    "items": {"type": "object", "additionalProperties": False,
                              "required": ["activity", "result"],
                              "properties": {"activity": {"type": "string", "enum": sorted(_QUALITY_EVIDENCE_ACTIVITIES)}, "result": {"type": "string", "maxLength": 240}}},
                },
                "validation_disposition": {
                    "type": "string",
                    "enum": ["product_failure", "environmental_instability"],
                },
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
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
            proxy = ToolProxyEnvironment()
            with proxy as environment:
                completed = self._run_invocation(tuple(command), root, environment)
            self.last_context_escalations = proxy.context_escalations()
            self.last_execution_seconds = round(time.monotonic() - started, 3)
            self.last_usage = extract_codex_usage(completed.stdout, completed.stderr)
            # Prefer one final explicit usage snapshot; legacy extraction stays
            # in place for compatibility with older Codex JSONL variants.
            self.last_usage.update(usage_from_jsonl(completed.stdout, completed.stderr))
            self.last_usage_snapshots = usage_snapshots_from_jsonl(completed.stdout, completed.stderr)
            self.last_churn = churn_from_jsonl(completed.stdout, completed.stderr)
            self.last_runtime_metadata.update(extract_codex_runtime_metadata(
                completed.stdout, completed.stderr
            ))
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            detail = _format_cli_failure(completed.returncode, completed.stderr, completed.stdout, prompt)
            next_action, terminal_condition, diagnostic = codex_failure_disposition(
                completed.returncode, completed.stdout, completed.stderr
            )
            interruption = provider_turn_interruption(completed.stdout, completed.stderr)
            raise CodexInvocationError(
                diagnostic,
                detail,
                next_action="NONE" if interruption else next_action,
                terminal_condition="provider_turn_interrupted" if interruption else terminal_condition,
                interruption_reason=interruption,
            )
        try:
            raw = json.loads(_codex_final_message(completed.stdout))
            result = AgentResult(**raw)
            if not isinstance(result.validation_evidence, (list, tuple)) or not isinstance(result.quality_evidence, (list, tuple)):
                raise TypeError("execution evidence must be a list")
            if result.validation_disposition not in {"product_failure", "environmental_instability"}:
                raise TypeError("validation disposition is invalid")
            if any(
                not isinstance(item, dict) or item.get("activity") not in _QUALITY_EVIDENCE_ACTIVITIES
                or not item.get("result")
                for item in result.quality_evidence
            ):
                raise TypeError("quality evidence is invalid")
            result = replace(
                result,
                validation_evidence=tuple(
                    {"command": redact_diagnostic(item.get("command", ""), limit=240), "result": redact_diagnostic(item.get("result", ""), limit=240)}
                    for item in result.validation_evidence
                    if isinstance(item, dict) and item.get("command") and item.get("result")
                ),
                quality_evidence=tuple(
                    {"activity": str(item["activity"]), "result": redact_diagnostic(str(item["result"]), limit=240)}
                    for item in result.quality_evidence
                ),
            )
            if result.diagnostic is not None:
                result = replace(result, diagnostic=redact_diagnostic(result.diagnostic))
            return result
        except (IndexError, json.JSONDecodeError, TypeError) as error:
            interruption = provider_turn_interruption(completed.stdout, completed.stderr)
            raise CodexInvocationError(
                "Provider turn interrupted before returning the required structured terminal result."
                if interruption else "Codex CLI did not return the required structured terminal result.",
                _format_cli_failure(completed.returncode, completed.stderr, completed.stdout, prompt),
                next_action="NONE" if interruption else "inspect_codex_cli",
                terminal_condition="provider_turn_interrupted" if interruption else "codex_invocation_failed",
                interruption_reason=interruption,
            ) from error

    def _run_invocation(
        self, command: tuple[str, ...], root: Path, environment: Mapping[str, str] | None = None
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
            and self._transient_action_callback is None
            and self._command_callback is None
            and self._runtime_metadata_callback is None
            and self._workspace_progress_callback is None
            and self._handoff_deadline_callback is None
        ):
            return self.provider.invoke(root, command, environment=environment)
        process = self.provider.spawn_invocation(root, command, environment=environment)
        if self._process_callback is not None:
            try:
                self._process_callback({"pid": process.pid, "process_group": os.getpgid(process.pid)})
            except OSError:
                self._process_callback(None)
        lines: list[str] = []
        last_workspace_progress: dict[str, int] | None = None
        observed_command_ids: set[str] = set()
        watchdog_stop = Event()
        handoff_timed_out = Event()

        def watchdog() -> None:
            while not watchdog_stop.wait(1):
                if self._handoff_deadline_callback is None or not self._handoff_deadline_callback():
                    continue
                handoff_timed_out.set()
                # Invocation processes start their own session. Stopping only
                # the CLI parent can leave stdout open in a child and strand
                # the runner in its read loop after the deadline.
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    process.terminate()
                return

        watchdog_thread = (
            Thread(target=watchdog, name="engineering-pr-handoff-watchdog", daemon=True)
            if self._handoff_deadline_callback is not None else None
        )
        if watchdog_thread is not None:
            watchdog_thread.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if handoff_timed_out.is_set():
                    raise CodexHandoffTimeout("Agent did not return after the host-owned PR hand-off deadline.")
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
                transient_action = project_codex_live_action_name(event)
                if transient_action is not None and self._transient_action_callback is not None:
                    self._transient_action_callback(transient_action)
                if self._command_callback is not None:
                    command_event = project_codex_command_event(event)
                    if command_event is not None:
                        if command_event[0] == "started" and command_event[1] not in observed_command_ids:
                            observed_command_ids.add(command_event[1])
                            self.last_execution_metadata["codex_commands_executed"] += 1
                            if self._workspace_progress_callback is not None:
                                self._workspace_progress_callback(dict(self.last_execution_metadata))
                        self._command_callback(*command_event)
            if handoff_timed_out.is_set():
                raise CodexHandoffTimeout("Agent did not return after the host-owned PR hand-off deadline.")
            return subprocess.CompletedProcess(command, process.wait(), "".join(lines), "")
        finally:
            watchdog_stop.set()
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=1)
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
