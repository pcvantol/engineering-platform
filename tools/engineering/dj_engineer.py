"""Thin foreground orchestrator for one bounded DJConnect engineering prompt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
from typing import Protocol
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
from .platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    detected_codex_cli_version,
    validate_compatibility,
)
from .qualification import dashboard, execute_qualification, latest_qualification
from .repository_handoff import publish as publish_repository_handoff
from .status_model import build as build_canonical_status, publish as publish_canonical_status
from .platform_api import PlatformConfiguration, PlatformConfigurationError, provider_registry
from .providers import GitHubProvider, CodexCliProvider


class RunnerError(RuntimeError):
    """A fail-closed engineering-runner diagnostic."""


class CodexInvocationError(RunnerError):
    """Separates transient console detail from safe checkpoint diagnostic state."""

    def __init__(self, persistent_diagnostic: str, console_detail: str) -> None:
        super().__init__(persistent_diagnostic)
        self.console_detail = console_detail


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

    def available(self) -> bool:
        return self.provider.command("--version").returncode == 0

    def version(self) -> str:
        completed = self.provider.command("--version")
        if completed.returncode:
            raise RunnerError("Codex CLI version could not be detected")
        return detected_codex_cli_version(completed.stdout)

    def review(self, root: Path, selection: ReviewerSelection, objective: str) -> ReviewerResult:
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
        state_directory = root / ".djconnect"
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
                    "--output-schema",
                    str(schema_path),
                    reviewer_prompt(selection, objective),
                ),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            return ReviewerResult(
                selection.reviewer,
                "Reviewer invocation failed; primary review continues.",
                failed=True,
            )
        try:
            raw = json.loads(completed.stdout.strip().splitlines()[-1])
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
        state_directory = root / ".djconnect" / "engineering-runs"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["terminal_state", "branch", "pull_request", "terminal_condition"],
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
                    ],
                },
                "diagnostic": {"type": "string", "maxLength": 500},
            },
        }
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
                    "workspace-write",
                    "-C",
                    str(root),
                    "--output-schema",
                    str(schema_path),
                    prompt,
                ),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
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
            raw = json.loads(completed.stdout.strip().splitlines()[-1])
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
    directory = root / ".djconnect" / "logs" / "codex"
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
    return f"""You are executing one bounded DJConnect engineering transaction.
Read BOOTSTRAP.md, ENGINEERING_METHOD.md, PROMPT_INITIALIZATION.md and AGENTS.md from the actual repository before acting. Repository and GitHub evidence override this checkpoint: {resume}
{authority} Continue waiting for objective terminal repository evidence; pending CI and temporary failures are not completion.
Supplied bounded objective follows:\n\n{objective}\n\nReturn only one JSON object with terminal_state (COMPLETE, WAITING, BLOCKED, or FAILED), branch, pull_request, terminal_condition (repository_reconciled, open_pr_checks_terminal, or external_blocked), and optional diagnostic. The diagnostic must be a short human-readable reason without secrets, tokens, headers, environment values, prompt content, repository file content, stack traces, or raw command output."""


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

    def run(
        self,
        prompt_path: Path,
        run_id: str | None = None,
        resume: bool = False,
        owner_authorized: bool = False,
    ) -> TransactionState:
        evidence = self.repository.inspect(self.root)
        if not evidence.clean:
            raise RunnerError("working tree is not clean; unrelated work will not be touched")
        if not self.agent.available():
            raise RunnerError("Codex CLI is not installed or invokable")
        self._verify_engineering_platform()
        state = self.store.load(run_id) if resume else None
        if state is not None:
            if state.repository != evidence.repository or Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current repository or prompt")
            if state.terminal:
                return state
        else:
            state = TransactionState(
                run_id or f"run-{uuid.uuid4().hex[:12]}",
                evidence.repository,
                str(prompt_path),
                "INITIALIZE",
                owner_authorized=owner_authorized,
            )
        objective = prompt_path.read_text(encoding="utf-8")
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
        state = self._reconcile(state, evidence)
        self.store.save(state)
        if state.terminal or state.phase == "WAIT_FOR_TERMINAL_EVIDENCE":
            return self._poll(state)
        try:
            result = self.agent.invoke(
                self.root, assemble_prompt(prompt_path, state) + memory + reviewer_context
            )
        except CodexInvocationError as error:
            self.console_detail = error.console_detail
            return self._save_terminal(state, "BLOCKED", "inspect_codex_cli", str(error))
        state = replace(
            state,
            phase="WAIT_FOR_TERMINAL_EVIDENCE",
            branch=result.branch or evidence.branch,
            pull_request=result.pull_request,
            next_action="poll_required_checks",
            terminal_condition=result.terminal_condition,
        )
        self.store.save(state)
        if state.owner_authorized and state.pull_request:
            self.github.ready(state.pull_request)
        return self._poll(state, result)

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
        try:
            result = self.agent.invoke(
                self.root,
                assemble_prompt(Path(repair.prompt_path), repair)
                + f"\n\nRepair objective: {objective}",
            )
        except CodexInvocationError as error:
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
        instruction = f"\n\nThe implementation PR #{implementation_pr} is merged. Execute only its mandatory governance-only Finalization: reconcile the four rolling records and immutable Prompt History, create a draft Finalization PR, and return that PR number."
        try:
            result = self.agent.invoke(
                self.root,
                assemble_prompt(Path(finalization.prompt_path), finalization) + instruction,
            )
        except CodexInvocationError as error:
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
        prog="dj-engineer", description="Run one bounded DJConnect engineering transaction"
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
    if raw_args == ["status"]:
        return print_live_status(Path.cwd().resolve())
    if raw_args == ["qualify"]:
        report = execute_qualification(Path.cwd().resolve())
        print(dashboard(report))
        return 0 if report["qualification"] == "PASS" else 1
    args = build_parser().parse_args(raw_args)
    root = Path.cwd().resolve()
    prompt_path = args.prompt.resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt does not exist: {prompt_path}")
    if args.resume and not args.run_id:
        raise SystemExit("--resume requires --run-id")
    runner = EngineeringRunner(
        root,
        StateStore(root / ".djconnect" / "engineering-runs"),
        SubprocessRepositoryClient(),
        GhCliClient(),
        CodexCliClient(),
    )
    try:
        state = runner.run(prompt_path, args.run_id, args.resume, args.owner_authorized)
    except (RunnerError, StateError) as error:
        print(f"BLOCKED: {error}")
        return 2
    report_path, editor = (
        generate_terminal_report(
            root,
            state,
            runner.platform_manifest,
            runner.detected_codex_cli,
            runner.reviewer_records,
        )
        if state.terminal
        else (None, None)
    )
    if runner.platform_manifest:
        publish_canonical_status(
            root / ".djconnect" / "status",
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
            f"Engineering report generated:\n\n{report_path}\n\nOpened in:\n\n{editor or 'not available'}\n\nReady for review."
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


def _format_reviewer_records(records: tuple[dict[str, object], ...]) -> str:
    if not records:
        return "No specialist reviewers required."
    lines: list[str] = []
    for record in records:
        lines.extend(
            (
                f"- Reviewer: {record['reviewer']}",
                f"  - Capability: {record.get('capability', 'engineering')}",
                f"  - Selected because: {record['selected_because']}",
                f"  - Contribution: {record['contribution']}",
                f"  - Accepted recommendations: {record['accepted_recommendations']}",
                f"  - Rejected recommendations: {record['rejected_recommendations']}",
            )
        )
    return "\n".join(lines)


def generate_terminal_report(
    root: Path,
    state: TransactionState,
    manifest: EngineeringPlatformManifest | None = None,
    detected_cli: str | None = None,
    reviewer_records: tuple[dict[str, object], ...] = (),
) -> tuple[Path, str | None]:
    """Write one immutable, local-only report for a terminal transaction."""
    reports = root / ".djconnect" / "reports"
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
            f"- Detected Codex CLI Version: `{detected_cli or 'unavailable'}`",
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
            "## Product Capability Review",
            _format_reviewer_records(reviewer_records),
            "",
            "## Validation",
            "Repository validation is recorded by the runner and required GitHub Actions; inspect the linked PR evidence for durations.",
            "",
            "## Repair History",
            "No repair iterations were required."
            if not state.repair_iterations
            else f"{state.repair_iterations} bounded repair iteration(s) were recorded.",
            "",
            "## Repository Cleanup",
            state.latest_repository_evidence or "Cleanup evidence unavailable.",
            "",
            "## Sub-Agent Usage",
            "No sub-agents were required. Sub-agents are read-only advisory helpers; the primary runner retains lifecycle authority.",
            "",
            "## Management Summary",
            format_management_summary(state),
            "",
            "## Diagnostics",
            state.diagnostic or "No terminal diagnostic.",
            f"Resume: `dj-engineer {state.prompt_path} --run-id {state.run_id} --resume`",
            "",
            "## Metrics",
            f"- Repair iterations: {state.repair_iterations}",
            f"- PRs created: {sum(value is not None for value in (state.implementation_pull_request, state.finalization_pull_request))}",
            f"- Merges performed: {sum(value is not None for value in (state.implementation_merge_commit, state.finalization_merge_commit))}",
            "",
        )
    )
    path.write_text(body, encoding="utf-8")
    return path, _open_report(path)


def _open_report(path: Path) -> str | None:
    """Best-effort editor launch; failure is deliberately non-terminal."""
    editor = os.environ.get("EDITOR")
    if editor:
        return _launch_editor(tuple(editor.split()) + (str(path),), f"EDITOR={editor}")
    if platform.system() == "Darwin":
        for application, label in (
            ("Visual Studio Code", "Visual Studio Code"),
            ("Sublime Text", "Sublime Text"),
        ):
            if Path("/Applications", f"{application}.app").is_dir():
                launched = _launch_editor(("open", "-a", application, str(path)), label)
                if launched:
                    return launched
    for executable in ("code", "subl"):
        resolved = shutil.which(executable)
        if resolved:
            launched = _launch_editor((resolved, str(path)), f"PATH executable: {resolved}")
            if launched:
                return launched
    return None


def _launch_editor(command: tuple[str, ...], label: str) -> str | None:
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return label
    except OSError:
        return None


def write_live_status(root: Path, state: TransactionState, action: str) -> Path:
    """Atomically publish the advisory current transaction state."""
    directory = root / ".djconnect" / "status"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "current.json"
    payload = {
        "run_id": state.run_id,
        "phase": state.phase,
        "current_action": redact_diagnostic(action),
        "objective": state.prompt_path,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
        "repair_iteration": state.repair_iterations,
        "repository_state": "MERGED_RECONCILED" if state.phase == "COMPLETE" else "ACTIVE",
        "workspace_state": "WORKSPACE_READY" if state.phase == "COMPLETE" else "ACTIVE",
        "last_update": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 0,
        "diagnostic": state.diagnostic,
        "resume_command": f"dj-engineer {state.prompt_path} --run-id {state.run_id} --resume",
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".current.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def print_live_status(root: Path) -> int:
    path = root / ".djconnect" / "status" / "current.json"
    if not path.is_file():
        print("No active engineering status is available.")
        return 1
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("Current engineering status is unavailable.")
        return 2
    print(
        f"Run:\n{current['run_id']}\n\nCurrent Phase:\n{current['phase']}\n\nImplementation PR:\n{current['implementation_pr']}\n\nRepair Iteration:\n{current['repair_iteration']}\n\nCurrent Action:\n{current['current_action']}\n\nElapsed:\n{current['elapsed_seconds']}s"
    )
    return 0


def _memory_path(root: Path) -> Path:
    return root / ".djconnect" / "memory" / "engineering-memory.json"


def load_engineering_memory(root: Path) -> dict[str, object]:
    try:
        raw = json.loads(_memory_path(root).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def retrieve_engineering_memory(root: Path, prompt_path: Path) -> str:
    """Return safe advisory metadata; repository evidence remains authoritative."""
    try:
        entries = load_engineering_memory(root).get("transactions", [])
    except AttributeError:
        return "\n\nEngineering Memory: no prior safe transaction metadata is available."
    objective = prompt_path.stem.lower()
    relevant = [
        entry
        for entry in entries[-10:]
        if any(word in objective for word in entry.get("classification", "").split())
    ]
    return (
        "\n\nEngineering Memory (advisory only; repository evidence overrides it): "
        + json.dumps(relevant[-3:], sort_keys=True)
    )


def capture_engineering_memory(
    root: Path, state: TransactionState, reviewer_records: tuple[dict[str, object], ...] = ()
) -> None:
    """Atomically store bounded metadata, never prompts, source content or credentials."""
    path = _memory_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        raw = load_engineering_memory(root)
    except (OSError, json.JSONDecodeError):
        raw = {}
    classification = " ".join(
        part
        for part in Path(state.prompt_path).stem.lower().replace("_", "-").split("-")
        if part.isalpha()
    )[:120]
    entry = {
        "classification": classification,
        "repository": state.repository,
        "outcome": state.phase,
        "repair_iterations": state.repair_iterations,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
        "confidence": 1.0,
        "usage_count": 0,
        "last_successful_use": datetime.now(timezone.utc).isoformat(),
    }
    reviewer_index = {
        item.get("reviewer"): dict(item)
        for item in raw.get("reviewers", [])
        if isinstance(item, dict) and isinstance(item.get("reviewer"), str)
    }
    for record in reviewer_records:
        reviewer = record.get("reviewer")
        if not isinstance(reviewer, str):
            continue
        previous = reviewer_index.get(reviewer, {})
        usage = int(previous.get("usage_count", 0)) + 1
        successful = int(previous.get("successful_outcomes", 0)) + (
            0 if record.get("failed") else 1
        )
        accepted = int(previous.get("accepted_recommendations", 0)) + int(
            record.get("accepted_recommendations", 0)
        )
        recommended = (
            int(previous.get("recommendation_count", 0))
            + int(record.get("accepted_recommendations", 0))
            + int(record.get("rejected_recommendations", 0))
        )
        confidence = round(successful / usage, 2)
        reviewer_index[reviewer] = {
            "reviewer": reviewer,
            "capability": record.get("capability", "engineering"),
            "usage_count": usage,
            "successful_outcomes": successful,
            "accepted_recommendations": accepted,
            "recommendation_count": recommended,
            "recommendation_acceptance_rate": round(accepted / recommended, 2)
            if recommended
            else 0.0,
            "average_duration": 0,
            "last_successful_use": datetime.now(timezone.utc).isoformat()
            if not record.get("failed")
            else previous.get("last_successful_use"),
            "future_confidence": confidence,
        }
    reviewers = list(reviewer_index.values())[-50:]
    raw = {
        "schema_version": 2,
        "transactions": [item for item in raw.get("transactions", []) if isinstance(item, dict)][
            -49:
        ]
        + [entry],
        "reviewers": reviewers,
        "capability_metrics": {
            "most_frequently_used": max(reviewers, key=lambda item: item["usage_count"])["reviewer"]
            if reviewers
            else None,
            "highest_value": max(reviewers, key=lambda item: item["future_confidence"])["reviewer"]
            if reviewers
            else None,
            "repository_areas": sorted(
                {str(item.get("capability", "engineering")) for item in reviewers}
            ),
        },
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".memory.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
