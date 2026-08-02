"""Thin foreground orchestrator for one bounded DJConnect engineering prompt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Mapping, Protocol
import uuid

from .agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from .capability_review import (
    ReviewerResult,
    ReviewerSelection,
    reconciled_recommendations,
    records_for_storage,
    reviewer_prompt,
    run_reviews,
    select_reviewers,
)
from .codex_observability import (
    codex_final_message as _codex_final_message,
    extract_codex_runtime_metadata,
    extract_codex_usage,
    write_codex_usage,
)
from .engineering_memory import (
    capture_engineering_memory,
    load_engineering_memory,
    retrieve_engineering_memory,
)
from .live_status import print_live_status, write_live_status
from .platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    detected_codex_cli_version,
    validate_compatibility,
)
from .qualification import dashboard, execute_qualification, latest_qualification
from .repository_handoff import publish as publish_repository_handoff
from .report_analysis import analyze as analyze_terminal_report
from .status_model import build as build_canonical_status, publish as publish_canonical_status
from .platform_api import PlatformConfiguration, PlatformConfigurationError, provider_registry
from .platform_bootstrap import migrate_legacy_workspace
from .providers import GitHubProvider, CodexCliProvider


class RunnerError(RuntimeError):
    """A fail-closed engineering-runner diagnostic."""


class CodexInvocationError(RunnerError):
    """Separates transient console detail from safe checkpoint diagnostic state."""

    def __init__(self, persistent_diagnostic: str, console_detail: str) -> None:
        super().__init__(persistent_diagnostic)
        self.console_detail = console_detail


def additional_workspace_write_roots(root: Path) -> tuple[Path, ...]:
    """Return the single configured sibling-project root, or no extra write root.

    The local setting is deliberately limited to the current repository's direct
    parent. It enables a new sibling project without giving a transaction broad
    filesystem write authority.
    """
    local = root / ".engineering" / "engineering-platform.local.json"
    if not local.is_file():
        return ()
    try:
        configured = PlatformConfiguration.load(root).workspace.provisioning_root
    except PlatformConfigurationError as error:
        raise RunnerError(str(error)) from error
    if configured is None:
        return ()
    candidate = Path(configured).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise RunnerError("Configured Engineering Workspace Root must be an existing directory, not a symlink.")
    resolved = candidate.resolve()
    if root.resolve().parent != resolved:
        raise RunnerError("Configured Engineering Workspace Root must be the current repository's direct parent directory.")
    return (resolved,)


@dataclass(frozen=True)
class RepositoryEvidence:
    repository: str
    branch: str
    head_sha: str
    clean: bool
    main_contains_head: bool = False


@dataclass(frozen=True)
class PullRequestEvidence:
    number: int
    state: str
    checks_terminal: bool
    checks_passed: bool
    merge_commit: str | None = None
    is_draft: bool = False
    failed_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResult:
    terminal_state: str
    branch: str | None = None
    pull_request: int | None = None
    terminal_condition: str = "repository_reconciled"
    diagnostic: str | None = None
    repository_path: str | None = None
    commit_sha: str | None = None


@dataclass(frozen=True)
class ExecutionContext:
    """Resolved lifecycle selection before any mode-specific readiness check."""

    execution_mode: str
    host_repository: Path
    target_repository: Path | None
    lifecycle_policy: str
    selected_preflight: str
    run_id: str | None = None


def _prompt_field_values(objective: str, label: str) -> tuple[str, ...]:
    values: list[str] = []
    lines = objective.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() != f"{label}:".casefold():
            continue
        for candidate in lines[index + 1 :]:
            value = candidate.strip()
            if value:
                values.append(value)
                break
    return tuple(values)


def resolve_execution_context(objective: str, host_repository: Path) -> ExecutionContext:
    """Resolve mode and target deterministically before lifecycle readiness."""
    modes = {
        line.split(":", 1)[1].strip().casefold()
        for line in objective.splitlines()
        if line.strip().casefold().startswith("execution mode:")
    }
    if "genesis" not in modes:
        return ExecutionContext("MANAGED", host_repository.resolve(), None, "managed", "managed_readiness")
    if modes != {"genesis"}:
        raise RunnerError("Execution Mode: Genesis conflicts with another execution mode declaration.")
    targets = _prompt_field_values(objective, "Target repository")
    if not targets:
        raise RunnerError("Genesis preflight blocked: prompt must declare one absolute Target repository path.")
    if len(set(targets)) != 1:
        raise RunnerError("Genesis preflight blocked: prompt declares conflicting Target repository paths.")
    target = Path(targets[0]).expanduser()
    if not target.is_absolute():
        raise RunnerError("Genesis preflight blocked: Target repository path must be absolute.")
    target = target.resolve()
    if target == host_repository.resolve():
        raise RunnerError("Genesis preflight blocked: Target repository cannot be the Engineering Platform host repository.")
    return ExecutionContext("GENESIS", host_repository.resolve(), target, "local_only", "genesis_git_workspace")


def execution_mode_for(objective: str) -> str:
    """Expose legacy mode detection; runner selection uses the full context resolver."""
    return (
        "GENESIS"
        if any(line.strip().casefold() == "execution mode: genesis" for line in objective.splitlines())
        else "MANAGED"
    )


def genesis_target_for(objective: str) -> Path | None:
    """Return the explicit local Genesis target, without interpreting other prompt text."""
    targets = _prompt_field_values(objective, "Target repository")
    return Path(targets[0]).expanduser() if len(set(targets)) == 1 else None


def genesis_workspace_preflight(target: Path | None) -> str | None:
    """Diagnose a non-destructive Genesis Git workspace blocker before Codex starts."""
    if target is None:
        return "Genesis preflight blocked: prompt must declare an absolute Target repository path."
    if not target.is_absolute() or not target.is_dir():
        return "Genesis preflight blocked: Target repository path is absent or not a directory."
    observed = subprocess.run(
        ("git", "-C", str(target), "rev-parse", "--git-dir"),
        text=True,
        capture_output=True,
        check=False,
    )
    if observed.returncode:
        return "Genesis preflight blocked: Target repository is not an accessible Git repository."
    git_dir = Path(observed.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (target / git_dir).resolve()
    lock = git_dir / "index.lock"
    if lock.exists():
        return f"Genesis preflight blocked: Git index lock exists at {lock}; it is not removed automatically."
    if not os.access(git_dir, os.W_OK):
        return f"Genesis preflight blocked: Git metadata directory is not writable: {git_dir}."
    status = subprocess.run(
        ("git", "-C", str(target), "status", "--porcelain", "--untracked-files=all"),
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        return "Genesis preflight blocked: Target repository status could not be inspected."
    if status.stdout.strip():
        return "Genesis preflight blocked: Target repository has tracked or untracked changes."
    return None


class RepositoryClient(Protocol):
    def inspect(self, root: Path) -> RepositoryEvidence: ...

    def main_contains(self, root: Path, sha: str) -> bool: ...


class GitHubClient(Protocol):
    def pull_request(self, number: int) -> PullRequestEvidence: ...

    def ready(self, number: int) -> None: ...

    def merge(self, number: int) -> None: ...


class AgentClient(Protocol):
    def available(self) -> bool: ...

    def version(self) -> str: ...

    def invoke(self, root: Path, prompt: str) -> AgentResult: ...


class SubprocessRepositoryClient:
    def __init__(self, provider: GitHubProvider | None = None) -> None:
        self.provider = provider or GitHubProvider()

    def _run(self, root: Path, *args: str) -> str:
        try:
            return self.provider.command(root, *args)
        except RuntimeError as error:
            raise RunnerError(str(error)) from error

    def inspect(self, root: Path) -> RepositoryEvidence:
        if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
            raise RunnerError("this is not a repository with canonical BOOTSTRAP.md")
        remote = self._run(root, "git", "remote", "get-url", "origin")
        repository = remote.removesuffix(".git").split(":")[-1].replace("github.com/", "")
        branch = self._run(root, "git", "branch", "--show-current")
        head_sha = self._run(root, "git", "rev-parse", "HEAD")
        clean = not self._run(root, "git", "status", "--porcelain", "--untracked-files=all")
        main_contains_head = (
            subprocess.run(
                ("git", "merge-base", "--is-ancestor", head_sha, "main"), cwd=root, check=False
            ).returncode
            == 0
        )
        return RepositoryEvidence(repository, branch, head_sha, clean, main_contains_head)

    def main_contains(self, root: Path, sha: str) -> bool:
        return (
            subprocess.run(
                ("git", "merge-base", "--is-ancestor", sha, "main"), cwd=root, check=False
            ).returncode
            == 0
        )

    def synchronize_main(self, root: Path) -> None:
        self._run(root, "git", "switch", "main")
        self._run(root, "git", "pull", "--ff-only")

    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str:
        self._run(root, "git", "fetch", "--prune")
        self._run(root, "git", "switch", "main")
        self._run(root, "git", "pull", "--ff-only")
        if not self.inspect(root).clean:
            raise RunnerError("Cleanup blocked: workspace is not clean.")
        removed: list[str] = []
        squash_reconciled: list[str] = []
        for branch in dict.fromkeys(branch for branch in branches if branch):
            if branch == "main":
                raise RunnerError("Cleanup blocked: transaction branch resolves to main.")
            exists = (
                subprocess.run(
                    ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                    cwd=root,
                    check=False,
                ).returncode
                == 0
            )
            if not exists:
                continue
            deletion = subprocess.run(
                ("git", "branch", "-d", branch),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            if deletion.returncode:
                # A squash merge intentionally makes the branch non-ancestral.
                # The caller reaches cleanup only after merged PR and main-containment evidence.
                force = subprocess.run(
                    ("git", "branch", "-D", branch),
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if force.returncode:
                    raise RunnerError(
                        f"Cleanup blocked: transaction branch {branch} could not be safely removed."
                    )
                squash_reconciled.append(branch)
            removed.append(branch)
        evidence = self.inspect(root)
        if evidence.branch != "main" or not evidence.clean or not evidence.main_contains_head:
            raise RunnerError(
                "Cleanup blocked: main synchronization or workspace verification failed."
            )
        squash = f"; squash-reconciled={','.join(squash_reconciled)}" if squash_reconciled else ""
        return f"fetched/pruned; main synchronized; removed={','.join(removed) or 'already-absent'}{squash}"


class GhCliClient:
    def __init__(self, provider: GitHubProvider | None = None) -> None:
        self.provider = provider or GitHubProvider()

    def pull_request(self, number: int) -> PullRequestEvidence:
        try:
            raw = json.loads(self.provider.github("pr", "view", str(number), "--json", "number,state,isDraft,mergeCommit,statusCheckRollup"))
        except RuntimeError as error:
            raise RunnerError(str(error)) from error
        checks = raw.get("statusCheckRollup") or []
        terminal = bool(checks) and all(item.get("status") == "COMPLETED" for item in checks)
        passed = terminal and all(
            item.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks
        )
        failed = tuple(
            str(item.get("name") or "unnamed check")
            for item in checks
            if item.get("status") == "COMPLETED"
            and item.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
        )
        merge = raw.get("mergeCommit") or {}
        return PullRequestEvidence(
            raw["number"], raw["state"], terminal, passed, merge.get("oid"), raw["isDraft"], failed
        )

    def ready(self, number: int) -> None:
        try:
            self.provider.github("pr", "ready", str(number))
        except RuntimeError as error:
            if "already ready" not in str(error).lower():
                raise RunnerError(str(error)) from error

    def merge(self, number: int) -> None:
        try:
            self.provider.github("pr", "merge", str(number), "--squash", "--delete-branch")
        except RuntimeError as error:
            raise RunnerError(str(error)) from error


class CodexCliClient:
    def __init__(self, provider: CodexCliProvider | None = None) -> None:
        self.provider = provider or CodexCliProvider()
        self.last_usage: dict[str, int | float | str] = {}
        self.last_execution_seconds: float | None = None
        self.last_runtime_metadata: dict[str, str] = {"runtime_provider": "codex_cli"}

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
            completed = subprocess.run(
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
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
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
                "workspace-write",
                "-C",
                str(root),
                "--json",
            ]
            for extra_root in extra_roots:
                command.extend(("--add-dir", str(extra_root)))
            command.extend(("--output-schema", str(schema_path), prompt))
            started = time.monotonic()
            completed = subprocess.run(
                tuple(command),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
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
            if result.diagnostic is not None:
                result = replace(result, diagnostic=redact_diagnostic(result.diagnostic))
            return result
        except (IndexError, json.JSONDecodeError, TypeError) as error:
            raise CodexInvocationError(
                "Codex CLI did not return the required structured terminal result.",
                _format_cli_failure(completed.returncode, completed.stderr, completed.stdout, prompt),
            ) from error


def _redacted_cli_tail(value: str, prompt: str, *, limit: int = 1_200) -> str:
    """Keep the actionable end of CLI output without retaining the prompt echo."""
    without_prompt = value.replace(prompt, "[PROMPT_OMITTED]") if prompt else value
    tail = "\n".join(without_prompt.splitlines()[-60:])
    return redact_diagnostic(tail, limit=limit) or "(empty)"


def _format_cli_failure(exit_code: int, stderr: str, stdout: str, prompt: str = "") -> str:
    return "\n".join(
        (
            f"Codex CLI exit code: {exit_code}",
            f"stderr tail: {_redacted_cli_tail(stderr, prompt)}",
            f"stdout tail: {_redacted_cli_tail(stdout, prompt)}",
        )
    )


def write_redacted_codex_cli_log(root: Path, run_id: str, detail: str) -> Path:
    """Persist bounded, redacted CLI diagnostics for local troubleshooting."""
    directory = root / ".engineering" / "logs" / "codex"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{run_id}.log"
    content = "# Redacted Codex CLI diagnostic\n\n" + redact_diagnostic(detail, limit=3_000) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def assemble_prompt(prompt_path: Path, state: TransactionState | None) -> str:
    objective = prompt_path.read_text(encoding="utf-8")
    resume = (
        "No prior transaction checkpoint exists."
        if state is None
        else json.dumps(state.to_dict(), sort_keys=True)
    )
    authority = (
        """The runner holds explicit owner authorization for this exact bounded transaction. You may create, commit and push one bounded branch and draft pull request, or repair that same pull request. The runner alone marks it ready and merges it. Do not merge, release, deploy, tag, publish, upload, change repository settings, bypass protection, or expand the objective."""
        if state and state.owner_authorized
        else "Do not create a merge, release, deployment, daemon, remote-control, or architecture authority beyond the supplied objective."
    )
    genesis = "" if not state or state.execution_mode != "GENESIS" else """
This is an explicit Genesis Mode transaction. Its target is a local-only direct child of the configured Engineering Workspace Root. Do not require, create, or contact an upstream remote; do not require origin/main; do not create a pull request. Reconcile only a clean local Git commit in that target repository. Return terminal_condition `local_commit_reconciled`, repository_path and commit_sha for a successful local commit."""
    return f"""You are executing one bounded DJConnect engineering transaction.
Read BOOTSTRAP.md, ENGINEERING_METHOD.md, PROMPT_INITIALIZATION.md and AGENTS.md from the actual repository before acting. Repository and GitHub evidence override this checkpoint: {resume}
{authority}{genesis} Continue waiting for objective terminal repository evidence; pending CI and temporary failures are not completion.
Supplied bounded objective follows:\n\n{objective}\n\nReturn only one JSON object with terminal_state (COMPLETE, WAITING, BLOCKED, or FAILED), branch, pull_request, terminal_condition (repository_reconciled, open_pr_checks_terminal, external_blocked, or local_commit_reconciled), diagnostic, repository_path, and commit_sha. Use null for fields that do not apply. The diagnostic must be a short human-readable reason without secrets, tokens, headers, environment values, prompt content, repository file content, stack traces, or raw command output."""


class EngineeringRunner:
    def __init__(
        self,
        root: Path,
        store: StateStore,
        repository: RepositoryClient,
        github: GitHubClient,
        agent: AgentClient,
        sleep=time.sleep,
        compatibility: RunnerCompatibility = RunnerCompatibility(),
    ) -> None:
        self.root, self.store, self.repository, self.github, self.agent, self.sleep = (
            root,
            store,
            repository,
            github,
            agent,
            sleep,
        )
        self.compatibility = compatibility
        self.platform_manifest: EngineeringPlatformManifest | None = None
        self.detected_codex_cli: str | None = None
        self.reviewer_records: tuple[dict[str, object], ...] = ()
        self.console_detail: str | None = None

    def _persist_agent_usage(self, run_id: str) -> None:
        usage = getattr(self.agent, "last_usage", None)
        if isinstance(usage, dict):
            write_codex_usage(self.root, run_id, usage)

    def _record_agent_execution_time(self, state: TransactionState) -> TransactionState:
        """Accumulate only measured Codex CLI invocation time for this run."""
        measured = getattr(self.agent, "last_execution_seconds", None)
        if isinstance(measured, bool) or not isinstance(measured, (int, float)):
            return state
        if not 0 <= measured <= 86_400:
            return state
        return replace(
            state,
            agent_execution_seconds=round((state.agent_execution_seconds or 0) + measured, 3),
        )

    def run(
        self,
        prompt_path: Path,
        run_id: str | None = None,
        resume: bool = False,
        owner_authorized: bool = False,
    ) -> TransactionState:
        objective = prompt_path.read_text(encoding="utf-8")
        state = self.store.load(run_id) if resume else None
        try:
            context = resolve_execution_context(objective, self.root)
        except RunnerError as error:
            evidence = self.repository.inspect(self.root)
            state = state or TransactionState(
                run_id or f"run-{uuid.uuid4().hex[:12]}",
                evidence.repository,
                str(prompt_path),
                "INITIALIZE",
                owner_authorized=owner_authorized,
                execution_mode="GENESIS"
                if any(line.strip().casefold() == "execution mode: genesis" for line in objective.splitlines())
                else "MANAGED",
            )
            return self._save_terminal(state, "BLOCKED", "execution_context_resolution", str(error))
        evidence = self.repository.inspect(self.root)
        if state is not None:
            if state.repository != evidence.repository or Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current repository or prompt")
            if state.execution_mode != context.execution_mode:
                raise RunnerError("checkpoint execution mode conflicts with the prompt")
            if (
                context.target_repository
                and state.genesis_repository_path
                and Path(state.genesis_repository_path) != context.target_repository
            ):
                raise RunnerError("checkpoint Genesis target conflicts with the prompt")
            if state.terminal:
                return state
        else:
            state = TransactionState(
                run_id or f"run-{uuid.uuid4().hex[:12]}",
                evidence.repository,
                str(prompt_path),
                "INITIALIZE",
                owner_authorized=owner_authorized,
                execution_mode=context.execution_mode,
            )
        context = replace(context, run_id=state.run_id)
        if context.execution_mode == "GENESIS":
            state = replace(state, genesis_repository_path=str(context.target_repository))
            preflight = genesis_workspace_preflight(context.target_repository)
            if preflight:
                return self._save_terminal(
                    state, "BLOCKED", "genesis_workspace_preflight", preflight
                )
            allowed = additional_workspace_write_roots(self.root)
            if context.target_repository.parent.resolve() not in allowed:
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "genesis_repository_scope",
                    "Genesis preflight blocked: Target repository is outside the configured direct-child workspace root.",
                )
            owner = self._active_genesis_transaction(context.target_repository, state.run_id)
            if owner:
                return self._save_terminal(
                    state,
                    "BLOCKED",
                    "genesis_workspace_conflict",
                    f"Genesis preflight blocked: target workspace is owned by active run {owner}.",
                )
        elif not evidence.clean:
            raise RunnerError("working tree is not clean; unrelated work will not be touched")
        if not self.agent.available():
            raise RunnerError("Codex CLI is not installed or invokable")
        self._verify_engineering_platform()
        memory = retrieve_engineering_memory(self.root, prompt_path)
        selections = select_reviewers(
            objective,
            prompt_path,
            state.transaction_kind if state else "IMPLEMENTATION",
            load_engineering_memory(self.root),
        )
        write_live_status(
            self.root,
            state
            or TransactionState(
                run_id or "pending-run", evidence.repository, str(prompt_path), "INITIALIZE"
            ),
            "Capability Selection: "
            + (
                ", ".join(item.reviewer for item in selections)
                or "No specialist reviewers required."
            ),
        )
        results = run_reviews(
            self.root, selections, objective, self.agent if hasattr(self.agent, "review") else None
        )
        self.reviewer_records = records_for_storage(selections, results)
        recommendations = reconciled_recommendations(results)
        reviewer_context = (
            ""
            if not recommendations
            else "\n\nSpecialist reviewer recommendations (advisory; primary agent must reconcile with repository evidence):\n- "
            + "\n- ".join(recommendations)
        )
        state = (
            replace(state, phase="EXECUTE_AGENT", next_action="invoke_agent")
            if context.execution_mode == "GENESIS"
            else self._reconcile(state, evidence)
        )
        self.store.save(state)
        write_live_status(self.root, state, state.next_action)
        if state.terminal or state.phase == "WAIT_FOR_TERMINAL_EVIDENCE":
            return self._poll(state)
        try:
            result = self.agent.invoke(
                self.root, assemble_prompt(prompt_path, state) + memory + reviewer_context
            )
            state = self._record_agent_execution_time(state)
            self._persist_agent_usage(state.run_id)
        except CodexInvocationError as error:
            state = self._record_agent_execution_time(state)
            self.console_detail = error.console_detail
            return self._save_terminal(state, "BLOCKED", "inspect_codex_cli", str(error))
        if state.execution_mode == "GENESIS":
            return self._reconcile_genesis_result(state, result)
        state = replace(
            state,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            branch=result.branch or evidence.branch,
            pull_request=result.pull_request,
            next_action="poll_required_checks",
            terminal_condition=result.terminal_condition,
        )
        self.store.save(state)
        write_live_status(self.root, state, state.next_action)
        if state.owner_authorized and state.pull_request:
            self.github.ready(state.pull_request)
        return self._poll(state, result)

    def _active_genesis_transaction(self, target: Path, run_id: str) -> str | None:
        """Return another active Genesis run that owns the same local workspace."""
        for checkpoint in self.store.directory.glob("*.json"):
            try:
                candidate = self.store.load(checkpoint.stem)
            except StateError:
                continue
            if (
                candidate.run_id != run_id
                and not candidate.terminal
                and candidate.execution_mode == "GENESIS"
                and candidate.genesis_repository_path
                and Path(candidate.genesis_repository_path) == target
            ):
                return candidate.run_id
        return None

    def _reconcile_genesis_result(self, state: TransactionState, result: AgentResult) -> TransactionState:
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(state, result.terminal_state, "external_action_required", result.diagnostic)
        if result.terminal_state != "COMPLETE" or result.terminal_condition != "local_commit_reconciled":
            return self._save_terminal(state, "BLOCKED", "genesis_local_commit_required", "Genesis Mode requires a reconciled local commit.")
        if not result.repository_path or not result.commit_sha:
            return self._save_terminal(state, "BLOCKED", "genesis_checkpoint_required", "Genesis Mode requires repository path and commit checkpoint evidence.")
        target = Path(result.repository_path).expanduser()
        allowed = additional_workspace_write_roots(self.root)
        if not target.is_absolute() or target.parent.resolve() not in allowed:
            return self._save_terminal(state, "BLOCKED", "genesis_repository_scope", "Genesis repository is outside the configured direct-child workspace root.")
        try:
            head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=target, text=True, capture_output=True, check=False)
            clean = subprocess.run(("git", "status", "--porcelain", "--untracked-files=all"), cwd=target, text=True, capture_output=True, check=False)
        except OSError as error:
            return self._save_terminal(state, "BLOCKED", "genesis_local_repository_required", str(error))
        actual_head = head.stdout.strip()
        workspace = "clean" if not clean.stdout.strip() else "dirty"
        if head.returncode or clean.returncode or actual_head != result.commit_sha or workspace != "clean":
            diagnostic = (
                "Genesis reconciliation failed: "
                f"reported commit={result.commit_sha or 'missing'}; "
                f"actual HEAD={actual_head or 'unavailable'}; workspace={workspace}."
            )
            return self._save_terminal(state, "BLOCKED", "genesis_reconciliation_required", diagnostic)
        reconciled = replace(state, genesis_repository_path=str(target), genesis_commit_sha=result.commit_sha, latest_repository_evidence=f"local genesis commit {result.commit_sha}")
        return self._save_terminal(reconciled, "COMPLETE", "genesis_local_commit_reconciled")

    def _verify_engineering_platform(self) -> None:
        try:
            self.detected_codex_cli = self.agent.version()
            self.platform_manifest = EngineeringPlatformManifest.load(
                self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
            )
            validate_compatibility(
                self.platform_manifest, self.compatibility, self.detected_codex_cli
            )
            configuration_path = self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json"
            if configuration_path.is_file():
                configuration = PlatformConfiguration.load(self.root)
                if configuration.platform.version != self.platform_manifest.platform_version:
                    raise EngineeringPlatformCompatibilityError("Platform identity and manifest version mismatch")
                providers = provider_registry(self.root)
                if any(not item["status"].qualified for item in providers.values()):
                    raise EngineeringPlatformCompatibilityError("Configured Engineering Platform provider is unavailable")
        except (EngineeringPlatformCompatibilityError, PlatformConfigurationError) as error:
            raise RunnerError(str(error)) from error

    def _reconcile(self, state: TransactionState, evidence: RepositoryEvidence) -> TransactionState:
        if state.branch and evidence.branch not in {"main", state.branch}:
            raise RunnerError("current branch conflicts with active transaction")
        if state.pull_request:
            return replace(
                state,
                phase="WAIT_FOR_TERMINAL_EVIDENCE",
                last_verified_sha=evidence.head_sha,
                next_action="poll_required_checks",
            )
        if (
            state.transaction_kind == "FINALIZATION"
            and state.finalization_merge_commit
            and self.repository.main_contains(self.root, state.finalization_merge_commit)
        ):
            return self._cleanup(state)
        if (
            state.transaction_kind == "IMPLEMENTATION"
            and state.implementation_merge_commit
            and self.repository.main_contains(self.root, state.implementation_merge_commit)
        ):
            if state.owner_authorized:
                return self._start_finalization(state, state.implementation_pull_request or 0)
        return replace(
            state,
            phase="EXECUTE_AGENT",
            last_verified_sha=evidence.head_sha,
            next_action="invoke_agent",
        )

    def _poll(self, state: TransactionState, result: AgentResult | None = None) -> TransactionState:
        if result and result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(
                state, result.terminal_state, "external_action_required", result.diagnostic
            )
        if not state.pull_request:
            if result and result.terminal_state == "COMPLETE":
                evidence = self.repository.inspect(self.root)
                if evidence.clean and evidence.main_contains_head:
                    return self._cleanup(state)
            return replace(
                state, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="obtain_repository_evidence"
            )
        attempts = 0
        while True:
            try:
                pr = self.github.pull_request(state.pull_request)
            except RunnerError:
                attempts += 1
                if attempts >= 3:
                    return replace(
                        state,
                        phase="WAIT_FOR_TERMINAL_EVIDENCE",
                        next_action="retry_github_evidence",
                    )
                self.sleep(min(30, 2**attempts))
                continue
            if not pr.checks_terminal:
                self.sleep(15)
                continue
            if not pr.checks_passed:
                if state.owner_authorized:
                    failed = ", ".join(pr.failed_checks) or "required CI check"
                    return self._repair(
                        state,
                        f"{failed} failed. Repair only the bounded transaction defects, commit and push the repair, then return the same pull request number.",
                    )
                return self._save_terminal(
                    state, "FAILED", "required_checks_failed", "Required CI check failed."
                )
            if pr.state == "MERGED":
                evidence = self.repository.inspect(self.root)
                if pr.merge_commit and self.repository.main_contains(self.root, pr.merge_commit):
                    state = self._record_merged_evidence(state, pr, evidence)
                    if state.owner_authorized and state.transaction_kind == "IMPLEMENTATION":
                        return self._start_finalization(state, pr.number)
                    return self._cleanup(state)
            if not state.owner_authorized and state.terminal_condition == "open_pr_checks_terminal":
                return self._save_terminal(state, "COMPLETE", "open_pr_checks_terminal")
            if state.owner_authorized:
                self.github.merge(pr.number)
                self.sleep(2)
                continue
            return self._save_terminal(
                state,
                "BLOCKED",
                "external_merge_authorization_required",
                "Merge requires explicit authorization.",
            )

    def _repair(self, state: TransactionState, objective: str) -> TransactionState:
        repair = replace(
            state,
            phase="REPAIR_AGENT",
            next_action="repair_bounded_validation_failure",
            repair_iterations=state.repair_iterations + 1,
        )
        self.store.save(repair)
        write_live_status(self.root, repair, repair.next_action)
        try:
            result = self.agent.invoke(
                self.root,
                assemble_prompt(Path(repair.prompt_path), repair)
                + f"\n\nRepair objective: {objective}",
            )
            repair = self._record_agent_execution_time(repair)
            self._persist_agent_usage(repair.run_id)
        except CodexInvocationError as error:
            repair = self._record_agent_execution_time(repair)
            self.console_detail = error.console_detail
            return self._save_terminal(repair, "BLOCKED", "inspect_codex_cli", str(error))
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(
                repair, result.terminal_state, "external_action_required", result.diagnostic
            )
        if result.pull_request != repair.pull_request:
            return self._save_terminal(
                repair,
                "BLOCKED",
                "bounded_scope_conflict",
                "Repair did not preserve the bounded pull request.",
            )
        return self._poll(
            replace(repair, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="poll_required_checks"),
            result,
        )

    def _start_finalization(
        self, state: TransactionState, implementation_pr: int
    ) -> TransactionState:
        if state.finalization_pull_request:
            return replace(
                state,
                transaction_kind="FINALIZATION",
                pull_request=state.finalization_pull_request,
                branch=state.finalization_branch,
                phase="WAIT_FOR_TERMINAL_EVIDENCE",
                next_action="poll_required_checks",
            )
        synchronize = getattr(self.repository, "synchronize_main", None)
        if callable(synchronize):
            synchronize(self.root)
        evidence = self.repository.inspect(self.root)
        if not evidence.clean or evidence.branch != "main":
            return self._save_terminal(
                state,
                "BLOCKED",
                "synchronize_main",
                "Finalization requires a clean, synchronized main checkout.",
            )
        finalization = replace(
            state,
            phase="FINALIZE_AGENT",
            transaction_kind="FINALIZATION",
            pull_request=None,
            branch=None,
            next_action="create_finalization",
            implementation_pull_request=implementation_pr or state.implementation_pull_request,
            latest_repository_evidence=_repository_summary(evidence),
        )
        self.store.save(finalization)
        write_live_status(self.root, finalization, finalization.next_action)
        instruction = f"\n\nThe implementation PR #{implementation_pr} is merged. Execute only its mandatory governance-only Finalization: reconcile the four rolling records and immutable Prompt History, create a draft Finalization PR, and return that PR number."
        try:
            result = self.agent.invoke(
                self.root,
                assemble_prompt(Path(finalization.prompt_path), finalization) + instruction,
            )
            finalization = self._record_agent_execution_time(finalization)
            self._persist_agent_usage(finalization.run_id)
        except CodexInvocationError as error:
            finalization = self._record_agent_execution_time(finalization)
            self.console_detail = error.console_detail
            return self._save_terminal(finalization, "BLOCKED", "inspect_codex_cli", str(error))
        if result.terminal_state in {"BLOCKED", "FAILED"} or not result.pull_request:
            return self._save_terminal(
                finalization,
                result.terminal_state
                if result.terminal_state in {"BLOCKED", "FAILED"}
                else "BLOCKED",
                "finalization_pr_required",
                result.diagnostic or "Finalization pull request was not created.",
            )
        finalization = replace(
            finalization,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            branch=result.branch,
            pull_request=result.pull_request,
            finalization_branch=result.branch,
            finalization_pull_request=result.pull_request,
            terminal_condition="repository_reconciled",
            next_action="poll_required_checks",
        )
        self.store.save(finalization)
        write_live_status(self.root, finalization, finalization.next_action)
        self.github.ready(result.pull_request)
        return self._poll(finalization, result)

    def _save_terminal(
        self, state: TransactionState, phase: str, action: str, diagnostic: str | None = None
    ) -> TransactionState:
        terminal = replace(
            state,
            phase=phase,
            terminal=True,
            next_action=action,
            diagnostic=redact_diagnostic(diagnostic) if diagnostic else None,
        )
        self.store.save(terminal)
        if phase == "COMPLETE":
            capture_engineering_memory(self.root, terminal, self.reviewer_records)
        write_live_status(self.root, terminal, action)
        print(f"[{terminal.phase}] {action}")
        return terminal

    def _cleanup(self, state: TransactionState) -> TransactionState:
        cleanup = replace(
            state,
            phase="REPOSITORY_CLEANUP",
            next_action="fetch_prune_and_remove_transaction_branches",
        )
        self.store.save(cleanup)
        write_live_status(self.root, cleanup, "Repository cleanup in progress")
        print("[REPOSITORY_CLEANUP] Repository cleanup in progress")
        operation = getattr(self.repository, "cleanup_transaction", None)
        if not callable(operation):
            return self._save_terminal(
                cleanup,
                "BLOCKED",
                "cleanup_unavailable",
                "Cleanup client is unavailable; resume with repository cleanup evidence.",
            )
        try:
            result = operation(
                self.root, (cleanup.implementation_branch, cleanup.finalization_branch)
            )
        except RunnerError as error:
            return self._save_terminal(
                cleanup, "BLOCKED", "repository_cleanup_required", str(error)
            )
        return self._save_terminal(
            replace(cleanup, latest_repository_evidence=redact_diagnostic(result)),
            "COMPLETE",
            "repository_cleanup_reconciled",
        )

    def _record_merged_evidence(
        self, state: TransactionState, pr: PullRequestEvidence, evidence: RepositoryEvidence
    ) -> TransactionState:
        common = {
            "last_verified_sha": evidence.head_sha,
            "latest_repository_evidence": _repository_summary(evidence),
            "latest_github_evidence": _pull_request_summary(pr),
        }
        if state.transaction_kind == "IMPLEMENTATION":
            return replace(
                state,
                implementation_branch=state.branch,
                implementation_pull_request=pr.number,
                implementation_head_sha=state.last_verified_sha,
                implementation_merge_commit=pr.merge_commit,
                **common,
            )
        return replace(
            state,
            finalization_branch=state.branch or state.finalization_branch,
            finalization_pull_request=pr.number,
            finalization_head_sha=state.last_verified_sha,
            finalization_merge_commit=pr.merge_commit,
            **common,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engineering-execution-host",
        description="Run one bounded Engineering Platform execution transaction",
    )
    parser.add_argument("prompt", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--owner-authorized",
        action="store_true",
        help="record and use the owner's bounded autonomous PR authorization",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = argv if argv is not None else __import__("sys").argv[1:]
    root = Path.cwd().resolve()
    migrate_legacy_workspace(root)
    if raw_args == ["status"]:
        return print_live_status(root)
    if raw_args == ["qualify"]:
        report = execute_qualification(root)
        print(dashboard(report))
        return 0 if report["qualification"] == "PASS" else 1
    args = build_parser().parse_args(raw_args)
    prompt_path = args.prompt.resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt does not exist: {prompt_path}")
    if args.resume and not args.run_id:
        raise SystemExit("--resume requires --run-id")
    runner = EngineeringRunner(
        root,
        StateStore(root / ".engineering" / "engineering-runs"),
        SubprocessRepositoryClient(),
        GhCliClient(),
        CodexCliClient(),
    )
    try:
        state = runner.run(prompt_path, args.run_id, args.resume, args.owner_authorized)
    except (RunnerError, StateError) as error:
        print(f"BLOCKED: {error}")
        return 2
    report_path = (
        generate_terminal_report(
            root,
            state,
            runner.platform_manifest,
            runner.detected_codex_cli,
            runner.reviewer_records,
            getattr(runner.agent, "last_runtime_metadata", None),
        )
        if state.terminal
        else None
    )
    if report_path:
        analyze_terminal_report(root, state.run_id, report_path)
    if runner.platform_manifest:
        publish_canonical_status(
            root / ".engineering" / "status",
            build_canonical_status(
                runner.platform_manifest,
                current_phase=state.phase,
                current_action=state.next_action,
                run_id=state.run_id,
                repair_iteration=state.repair_iterations,
                implementation_pr=state.implementation_pull_request,
                finalization_pr=state.finalization_pull_request,
                repository_state="MERGED_RECONCILED" if state.phase == "COMPLETE" else "ACTIVE",
                workspace_state="WORKSPACE_READY" if state.phase == "COMPLETE" else "ACTIVE",
                owner_authorized=state.owner_authorized,
                resume_available=not state.terminal,
                latest_report=str(report_path) if report_path else None,
                diagnostic=state.diagnostic,
            ),
        )
    if (
        state.phase == "COMPLETE"
        and state.finalization_pull_request
        and state.implementation_pull_request
        and runner.platform_manifest
    ):
        publish_repository_handoff(
            root,
            run_id=state.run_id,
            platform_version=runner.platform_manifest.platform_version,
            implementation_pr=state.implementation_pull_request,
            finalization_pr=state.finalization_pull_request,
        )
    if report_path:
        print(
            f"Engineering report generated:\n\n{report_path}\n\nAvailable in the Engineering Status dashboard."
        )
    if state.phase in {"BLOCKED", "FAILED"}:
        print(_format_terminal_report(state))
        if runner.console_detail:
            log_path = write_redacted_codex_cli_log(root, state.run_id, runner.console_detail)
            print(f"\nCodex CLI log: {log_path}")
            print(f"\nCodex CLI details:\n{runner.console_detail}")
    elif state.phase == "COMPLETE" and state.owner_authorized and state.finalization_merge_commit:
        print(format_management_summary(state))
    else:
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    return 0 if state.phase == "COMPLETE" else 1


def _next_action_message(action: str) -> str:
    return {
        "external_action_required": "Resolve the reported external dependency, then resume the run.",
        "external_merge_authorization_required": "Obtain the required merge authorization.",
        "required_checks_failed": "Inspect and resolve the failed required CI check.",
        "inspect_codex_cli": "Inspect the redacted Codex CLI details above, then resume after correction.",
    }.get(action, "Inspect current repository and GitHub evidence before resuming.")


def _format_terminal_report(state: TransactionState) -> str:
    return f"{state.phase}\n\nReason:\n{state.diagnostic or 'No safe diagnostic was available.'}\n\nNext action:\n{_next_action_message(state.next_action)}"


def _repository_summary(evidence: RepositoryEvidence) -> str:
    return redact_diagnostic(
        f"branch={evidence.branch}; head={evidence.head_sha}; clean={evidence.clean}; main_contains_head={evidence.main_contains_head}"
    )


def _pull_request_summary(evidence: PullRequestEvidence) -> str:
    failed = ",".join(evidence.failed_checks) or "none"
    return redact_diagnostic(
        f"pr={evidence.number}; state={evidence.state}; terminal={evidence.checks_terminal}; passed={evidence.checks_passed}; failed_checks={failed}"
    )


def format_management_summary(state: TransactionState) -> str:
    """Return a checkpoint-only completion summary without exposing prompt text."""
    return "\n".join(
        (
            "COMPLETE — IMPLEMENTATION_AND_FINALIZATION_RECONCILED",
            "Objective: bounded objective recorded at the supplied prompt path.",
            f"Implementation: branch={state.implementation_branch or state.branch}; PR={state.implementation_pull_request}; merge={state.implementation_merge_commit}.",
            f"Repair iterations: {state.repair_iterations}.",
            f"Finalization: branch={state.finalization_branch}; PR={state.finalization_pull_request}; merge={state.finalization_merge_commit}.",
            "Repository Cleanup: fetched and pruned; local main synchronized; transaction branches removed or already absent; workspace clean.",
            "Authority: owner-authorized bounded lifecycle; ready-for-review, merge and Finalization automated.",
            "No release, deployment or publication performed. Rolling Horizon unchanged.",
        )
    )


def format_terminal_management_summary(state: TransactionState) -> str:
    """Return evidence bounded by the persisted terminal checkpoint phase."""
    if state.phase == "COMPLETE":
        return format_management_summary(state)
    outcome = (
        "BLOCKED — no engineering changes were executed or delivered."
        if state.phase == "BLOCKED"
        else "FAILED — the engineering transaction did not complete successfully."
    )
    target = state.genesis_repository_path or state.repository
    codex = (
        "not started"
        if state.terminal_condition in {"genesis_workspace_preflight", "execution_context_resolution"}
        else "not confirmed by the terminal checkpoint"
    )
    return "\n".join(
        (
            outcome,
            f"Execution mode: {state.execution_mode}.",
            f"Target repository: {target}.",
            f"Terminal checkpoint: {state.phase}.",
            f"Codex execution: {codex}.",
            f"Implementation: branch={state.implementation_branch}; PR={state.implementation_pull_request}; merge={state.implementation_merge_commit}.",
            f"Finalization: branch={state.finalization_branch}; PR={state.finalization_pull_request}; merge={state.finalization_merge_commit}.",
            "No release, deployment or publication was performed.",
        )
    )


def terminal_report_matches_state(body: str, state: TransactionState) -> bool:
    """Reject report prose that conflicts with its immutable terminal checkpoint."""
    if f"- Terminal state: `{state.phase}`" not in body:
        return False
    required_sections = (
        "## Initial Repository Assessment",
        "## Engineering Outcome",
        "## Reviewer Findings",
        "## Repository Truth",
        "## Management Summary",
    )
    if any(section not in body for section in required_sections):
        return False
    if state.phase == "BLOCKED":
        return "BLOCKED — no engineering changes were executed or delivered." in body and "COMPLETE —" not in body
    if state.phase == "FAILED":
        return "FAILED — the engineering transaction did not complete successfully." in body and "COMPLETE —" not in body
    return state.phase == "COMPLETE" and "COMPLETE —" in body


def corrected_terminal_report(state: TransactionState) -> str:
    """Generate a minimal replacement when richer report assembly is inconsistent."""
    return "\n".join(
        (
            "# Engineering Report",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Terminal state: `{state.phase}`",
            "",
            "## Initial Repository Assessment",
            "Assessment evidence is unavailable. This section describes only the repository before any attempted implementation.",
            "",
            "## Engineering Outcome",
            format_terminal_management_summary(state),
            "",
            "## Reviewer Findings",
            "No reviewer findings were retained. Reviewer observations are advisory initial observations only.",
            "",
            "## Repository Truth",
            "Priority: persisted repository state, resulting commits, validation results, then reviewer observations.",
            "",
            "## Management Summary",
            format_terminal_management_summary(state),
            "",
            "## Diagnostics",
            state.diagnostic or "No terminal diagnostic.",
            "",
        )
    )


def _format_reviewer_records(records: tuple[dict[str, object], ...], phase: str) -> str:
    if not records:
        return "No specialist reviewers required. Any future reviewer observations remain advisory initial observations."
    lines: list[str] = []
    for record in records:
        lines.extend(
            (
                f"- Reviewer: {record['reviewer']}",
                f"  - Capability: {record.get('capability', 'engineering')}",
                f"  - Selected because: {record['selected_because']}",
                f"  - Initial observation: {record['contribution']}",
                f"  - Accepted recommendations: {record['accepted_recommendations']}",
                f"  - Rejected recommendations: {record['rejected_recommendations']}",
                "  - Outcome: Resolved during implementation; the final repository evidence below is authoritative."
                if phase == "COMPLETE"
                else "  - Outcome: Not a final repository statement; consult the terminal checkpoint and diagnostics.",
            )
        )
    return "\n".join(lines)


def _format_engineering_outcome(state: TransactionState) -> str:
    """Describe final delivery from checkpoint and repository evidence, never advice."""
    if state.phase != "COMPLETE":
        return "\n".join(
            (
                f"- Final checkpoint: `{state.phase}`",
                "- Completed work: no successful engineering delivery is claimed.",
                f"- Remaining limitation: {state.diagnostic or 'Terminal outcome requires follow-up.'}",
            )
        )
    return "\n".join(
        (
            "- Final checkpoint: `COMPLETE`",
            "- Completed work: implementation and any required reconciliation completed according to the persisted checkpoint.",
            f"- Resulting commits: implementation `{state.implementation_merge_commit or 'not applicable'}`; finalization `{state.finalization_merge_commit or 'not applicable'}`.",
            f"- Repository state: {state.latest_repository_evidence or 'Recorded by the terminal COMPLETE checkpoint.'}",
            "- Remaining limitations: none recorded by the terminal checkpoint.",
        )
    )


def generate_terminal_report(
    root: Path,
    state: TransactionState,
    manifest: EngineeringPlatformManifest | None = None,
    detected_cli: str | None = None,
    reviewer_records: tuple[dict[str, object], ...] = (),
    runtime_metadata: Mapping[str, str] | None = None,
) -> Path:
    """Write one immutable, local-only report for a terminal transaction."""
    reports = root / ".engineering" / "reports"
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = reports / f"{timestamp}_{state.run_id}.md"
    objective = "Objective unavailable because the prompt file is no longer local."
    try:
        objective = Path(state.prompt_path).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    manifest = manifest or EngineeringPlatformManifest.load(
        root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
    )
    qualification = latest_qualification(root)
    qualification_summary = (
        "No local Engineering Platform Qualification evidence is available."
        if qualification is None
        else f"Version: `{qualification.get('engineering_platform_version')}`\n- Latest Qualification: `{qualification.get('qualification')}`\n- Executed: `{qualification.get('executed_at')}`\n- Qualification Coverage: `{qualification.get('coverage_percent')}%`"
    )
    runtime_metadata = runtime_metadata or {"runtime_provider": "codex_cli"}
    runtime_provider = runtime_metadata.get("runtime_provider", "unavailable")
    reported_model = runtime_metadata.get("model", "not reported")
    reported_reasoning = runtime_metadata.get("reasoning_profile", "not reported")
    reported_configuration = runtime_metadata.get("configuration_profile", "not reported")
    body = "\n".join(
        (
            "# Engineering Report",
            "",
            f"- Timestamp: {timestamp}",
            f"- Run ID: `{state.run_id}`",
            f"- Repository: `{state.repository}`",
            f"- Prompt: `{state.prompt_path}`",
            f"- Terminal state: `{state.phase}`",
            f"- Objective: {objective}",
            "",
            "## Engineering Platform",
            f"- Platform Version: `{manifest.platform_version}`",
            f"- Runner Version: `{manifest.runner_version}`",
            f"- Bootstrap Contract: `{manifest.bootstrap_contract}`",
            f"- Checkpoint Format: `{manifest.checkpoint_format}`",
            f"- Memory Format: `{manifest.memory_format}`",
            f"- Report Format: `{manifest.report_format}`",
            f"- Runtime Provider: `{runtime_provider}`",
            f"- AI Model: `{reported_model}`",
            f"- Reasoning Profile: `{reported_reasoning}`",
            f"- Configuration Profile: `{reported_configuration}`",
            f"- Codex CLI Version: `{detected_cli or 'unavailable'}`",
            "",
            "## Engineering Platform Qualification",
            qualification_summary,
            "",
            "## Authorization",
            f"- Owner authorization: `{state.owner_authorized}`",
            "- Ready for Review, merge and Finalization authority remain runner-controlled.",
            "",
            "## Lifecycle Timeline",
            f"`INITIALIZE → IMPLEMENTATION → VALIDATION → REPAIR ({state.repair_iterations}) → MERGE → FINALIZATION → REPOSITORY_CLEANUP → {state.phase}`",
            "",
            "## Pull Requests",
            f"- Implementation: branch `{state.implementation_branch}`, PR `{state.implementation_pull_request}`, merge `{state.implementation_merge_commit}`",
            f"- Finalization: branch `{state.finalization_branch}`, PR `{state.finalization_pull_request}`, merge `{state.finalization_merge_commit}`",
            "",
            "## Initial Repository Assessment",
            "This assessment describes the repository before implementation. Reviewer observations are advisory and cannot describe the final repository state.",
            "",
            "## Engineering Outcome",
            _format_engineering_outcome(state),
            "",
            "## Reviewer Findings",
            "Initial observations only. They are not final repository claims.",
            _format_reviewer_records(reviewer_records, state.phase),
            "",
            "## Repository Truth",
            "Priority: persisted repository state, resulting commits, validation results, then reviewer observations.",
            "The Engineering Outcome and Management Summary above are derived from that priority order.",
            "",
            "## Validation",
            "Repository validation is recorded by the runner and required GitHub Actions; inspect the linked PR evidence for durations."
            if state.phase == "COMPLETE"
            else "No successful engineering validation or delivery is claimed for this terminal transaction.",
            "",
            "## Repair History",
            "No repair iterations were required."
            if not state.repair_iterations
            else f"{state.repair_iterations} bounded repair iteration(s) were recorded.",
            "",
            "## Repository Cleanup",
            state.latest_repository_evidence or "Cleanup evidence unavailable.",
            "",
            "## Specialist Agent Reviews",
            "Specialist review agents are read-only advisory helpers. Their initial observations are listed above; the primary runner retains lifecycle authority.",
            "",
            "## Management Summary",
            "Final repository outcome; it does not restate initial reviewer observations as current state.",
            format_terminal_management_summary(state),
            "",
            "## Diagnostics",
            state.diagnostic or "No terminal diagnostic.",
            f"Resume: `engineering-execution-host {state.prompt_path} --run-id {state.run_id} --resume`",
            "",
            "## Metrics",
            f"- Codex CLI execution time: {state.agent_execution_seconds if state.agent_execution_seconds is not None else 'not measured'} seconds",
            f"- Repair iterations: {state.repair_iterations}",
            f"- PRs created: {sum(value is not None for value in (state.implementation_pull_request, state.finalization_pull_request))}",
            f"- Merges performed: {sum(value is not None for value in (state.implementation_merge_commit, state.finalization_merge_commit))}",
            "",
        )
    )
    if not terminal_report_matches_state(body, state):
        body = corrected_terminal_report(state)
    path.write_text(body, encoding="utf-8")
    return path
