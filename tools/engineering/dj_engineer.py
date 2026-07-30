"""Thin foreground orchestrator for one bounded DJConnect engineering prompt."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Protocol
import uuid

from .agent_state import StateError, StateStore, TransactionState, redact_diagnostic


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

    def invoke(self, root: Path, prompt: str) -> AgentResult: ...


class SubprocessRepositoryClient:
    def _run(self, root: Path, *args: str) -> str:
        completed = subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise RunnerError(completed.stderr.strip() or "repository command failed")
        return completed.stdout.strip()

    def inspect(self, root: Path) -> RepositoryEvidence:
        if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
            raise RunnerError("this is not a repository with canonical BOOTSTRAP.md")
        remote = self._run(root, "git", "remote", "get-url", "origin")
        repository = remote.removesuffix(".git").split(":")[-1].replace("github.com/", "")
        branch = self._run(root, "git", "branch", "--show-current")
        head_sha = self._run(root, "git", "rev-parse", "HEAD")
        clean = not self._run(root, "git", "status", "--porcelain", "--untracked-files=all")
        main_contains_head = subprocess.run(
            ("git", "merge-base", "--is-ancestor", head_sha, "main"), cwd=root, check=False
        ).returncode == 0
        return RepositoryEvidence(repository, branch, head_sha, clean, main_contains_head)

    def main_contains(self, root: Path, sha: str) -> bool:
        return subprocess.run(("git", "merge-base", "--is-ancestor", sha, "main"), cwd=root, check=False).returncode == 0


class GhCliClient:
    def pull_request(self, number: int) -> PullRequestEvidence:
        completed = subprocess.run(
            ("gh", "pr", "view", str(number), "--json", "number,state,isDraft,mergeCommit,statusCheckRollup"),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RunnerError(completed.stderr.strip() or "GitHub evidence could not be read")
        raw = json.loads(completed.stdout)
        checks = raw.get("statusCheckRollup") or []
        terminal = bool(checks) and all(item.get("status") == "COMPLETED" for item in checks)
        passed = terminal and all(item.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks)
        merge = raw.get("mergeCommit") or {}
        return PullRequestEvidence(raw["number"], raw["state"], terminal, passed, merge.get("oid"), raw["isDraft"])

    def ready(self, number: int) -> None:
        completed = subprocess.run(("gh", "pr", "ready", str(number)), text=True, capture_output=True, check=False)
        if completed.returncode and "already ready" not in completed.stderr.lower():
            raise RunnerError(completed.stderr.strip() or "pull request could not be marked ready")

    def merge(self, number: int) -> None:
        completed = subprocess.run(("gh", "pr", "merge", str(number), "--squash", "--delete-branch"), text=True, capture_output=True, check=False)
        if completed.returncode:
            raise RunnerError(completed.stderr.strip() or "pull request could not be merged")


class CodexCliClient:
    def available(self) -> bool:
        return subprocess.run(("codex", "--version"), text=True, capture_output=True, check=False).returncode == 0

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        state_directory = root / ".djconnect" / "engineering-runs"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["terminal_state", "branch", "pull_request", "terminal_condition"],
            "properties": {
                "terminal_state": {"type": "string", "enum": ["COMPLETE", "WAITING", "BLOCKED", "FAILED"]},
                "branch": {"type": ["string", "null"]},
                "pull_request": {"type": ["integer", "null"]},
                "terminal_condition": {"type": "string", "enum": ["repository_reconciled", "open_pr_checks_terminal", "external_blocked"]},
                "diagnostic": {"type": "string", "maxLength": 500},
            },
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=state_directory, delete=False) as handle:
            json.dump(schema, handle)
            schema_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ("codex", "exec", "--sandbox", "workspace-write", "-C", str(root), "--output-schema", str(schema_path), prompt),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            schema_path.unlink(missing_ok=True)
        if completed.returncode:
            detail = _format_cli_failure(completed.returncode, completed.stderr, completed.stdout)
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
                _format_cli_failure(completed.returncode, completed.stderr, completed.stdout),
            ) from error


def _format_cli_failure(exit_code: int, stderr: str, stdout: str) -> str:
    return "\n".join(
        (
            f"Codex CLI exit code: {exit_code}",
            f"stderr: {redact_diagnostic(stderr, limit=300) or '(empty)'}",
            f"stdout: {redact_diagnostic(stdout, limit=300) or '(empty)'}",
        )
    )


def assemble_prompt(prompt_path: Path, state: TransactionState | None) -> str:
    objective = prompt_path.read_text(encoding="utf-8")
    resume = "No prior transaction checkpoint exists." if state is None else json.dumps(state.to_dict(), sort_keys=True)
    return f"""You are executing one bounded DJConnect engineering transaction.
Read BOOTSTRAP.md, ENGINEERING_METHOD.md, PROMPT_INITIALIZATION.md and AGENTS.md from the actual repository before acting. Repository and GitHub evidence override this checkpoint: {resume}
Do not create merge, release, deployment, daemon, remote-control, or architecture authority beyond the supplied objective. Continue waiting for objective terminal repository evidence; pending CI and temporary failures are not completion.
Supplied bounded objective follows:\n\n{objective}\n\nReturn only one JSON object with terminal_state (COMPLETE, WAITING, BLOCKED, or FAILED), branch, pull_request, terminal_condition (repository_reconciled, open_pr_checks_terminal, or external_blocked), and optional diagnostic. The diagnostic must be a short human-readable reason without secrets, tokens, headers, environment values, prompt content, repository file content, stack traces, or raw command output."""


class EngineeringRunner:
    def __init__(self, root: Path, store: StateStore, repository: RepositoryClient, github: GitHubClient, agent: AgentClient, sleep=time.sleep) -> None:
        self.root, self.store, self.repository, self.github, self.agent, self.sleep = root, store, repository, github, agent, sleep
        self.console_detail: str | None = None

    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False, owner_authorized: bool = False) -> TransactionState:
        evidence = self.repository.inspect(self.root)
        if not evidence.clean:
            raise RunnerError("working tree is not clean; unrelated work will not be touched")
        if not self.agent.available():
            raise RunnerError("Codex CLI is not installed or invokable")
        state = self.store.load(run_id) if resume else None
        if state is not None:
            if state.repository != evidence.repository or Path(state.prompt_path) != prompt_path:
                raise RunnerError("checkpoint conflicts with current repository or prompt")
            if state.terminal:
                return state
        else:
            state = TransactionState(run_id or f"run-{uuid.uuid4().hex[:12]}", evidence.repository, str(prompt_path), "INITIALIZE", owner_authorized=owner_authorized)
        state = self._reconcile(state, evidence)
        self.store.save(state)
        if state.terminal or state.phase == "WAIT_FOR_TERMINAL_EVIDENCE":
            return self._poll(state)
        try:
            result = self.agent.invoke(self.root, assemble_prompt(prompt_path, state))
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

    def _reconcile(self, state: TransactionState, evidence: RepositoryEvidence) -> TransactionState:
        if state.branch and evidence.branch not in {"main", state.branch}:
            raise RunnerError("current branch conflicts with active transaction")
        if state.pull_request:
            return replace(state, phase="WAIT_FOR_TERMINAL_EVIDENCE", last_verified_sha=evidence.head_sha, next_action="poll_required_checks")
        return replace(state, phase="EXECUTE_AGENT", last_verified_sha=evidence.head_sha, next_action="invoke_agent")

    def _poll(self, state: TransactionState, result: AgentResult | None = None) -> TransactionState:
        if result and result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(state, result.terminal_state, "external_action_required", result.diagnostic)
        if not state.pull_request:
            if result and result.terminal_state == "COMPLETE":
                evidence = self.repository.inspect(self.root)
                if evidence.clean and evidence.main_contains_head:
                    return self._save_terminal(state, "COMPLETE", "repository_reconciled")
            return replace(state, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="obtain_repository_evidence")
        attempts = 0
        while True:
            try:
                pr = self.github.pull_request(state.pull_request)
            except RunnerError:
                attempts += 1
                if attempts >= 3:
                    return replace(state, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="retry_github_evidence")
                self.sleep(min(30, 2**attempts))
                continue
            if not pr.checks_terminal:
                self.sleep(15)
                continue
            if not pr.checks_passed:
                if state.owner_authorized:
                    return self._repair(state, "Required CI check failed. Repair only the bounded transaction defects, commit and push the repair, then return the same pull request number.")
                return self._save_terminal(state, "FAILED", "required_checks_failed", "Required CI check failed.")
            if pr.state == "MERGED":
                evidence = self.repository.inspect(self.root)
                if pr.merge_commit and self.repository.main_contains(self.root, pr.merge_commit):
                    if state.owner_authorized and state.transaction_kind == "IMPLEMENTATION":
                        return self._start_finalization(state, pr.number)
                    return self._save_terminal(state, "COMPLETE", "repository_reconciled")
            if not state.owner_authorized and state.terminal_condition == "open_pr_checks_terminal":
                return self._save_terminal(state, "COMPLETE", "open_pr_checks_terminal")
            if state.owner_authorized:
                self.github.merge(pr.number)
                self.sleep(2)
                continue
            return self._save_terminal(state, "BLOCKED", "external_merge_authorization_required", "Merge requires explicit authorization.")

    def _repair(self, state: TransactionState, objective: str) -> TransactionState:
        repair = replace(state, phase="REPAIR_AGENT", next_action="repair_bounded_validation_failure")
        self.store.save(repair)
        try:
            result = self.agent.invoke(self.root, assemble_prompt(Path(repair.prompt_path), repair) + f"\n\nRepair objective: {objective}")
        except CodexInvocationError as error:
            self.console_detail = error.console_detail
            return self._save_terminal(repair, "BLOCKED", "inspect_codex_cli", str(error))
        if result.terminal_state in {"BLOCKED", "FAILED"}:
            return self._save_terminal(repair, result.terminal_state, "external_action_required", result.diagnostic)
        if result.pull_request != repair.pull_request:
            return self._save_terminal(repair, "BLOCKED", "bounded_scope_conflict", "Repair did not preserve the bounded pull request.")
        return self._poll(replace(repair, phase="WAIT_FOR_TERMINAL_EVIDENCE", next_action="poll_required_checks"), result)

    def _start_finalization(self, state: TransactionState, implementation_pr: int) -> TransactionState:
        finalization = replace(state, phase="FINALIZE_AGENT", transaction_kind="FINALIZATION", pull_request=None, branch=None, next_action="create_finalization")
        self.store.save(finalization)
        instruction = f"\n\nThe implementation PR #{implementation_pr} is merged. Execute only its mandatory governance-only Finalization: reconcile the four rolling records and immutable Prompt History, create a draft Finalization PR, and return that PR number."
        try:
            result = self.agent.invoke(self.root, assemble_prompt(Path(finalization.prompt_path), finalization) + instruction)
        except CodexInvocationError as error:
            self.console_detail = error.console_detail
            return self._save_terminal(finalization, "BLOCKED", "inspect_codex_cli", str(error))
        if result.terminal_state in {"BLOCKED", "FAILED"} or not result.pull_request:
            return self._save_terminal(finalization, result.terminal_state if result.terminal_state in {"BLOCKED", "FAILED"} else "BLOCKED", "finalization_pr_required", result.diagnostic or "Finalization pull request was not created.")
        finalization = replace(finalization, phase="WAIT_FOR_TERMINAL_EVIDENCE", branch=result.branch, pull_request=result.pull_request, terminal_condition="repository_reconciled", next_action="poll_required_checks")
        self.store.save(finalization)
        self.github.ready(result.pull_request)
        return self._poll(finalization, result)

    def _save_terminal(self, state: TransactionState, phase: str, action: str, diagnostic: str | None = None) -> TransactionState:
        terminal = replace(state, phase=phase, terminal=True, next_action=action, diagnostic=redact_diagnostic(diagnostic) if diagnostic else None)
        self.store.save(terminal)
        return terminal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dj-engineer", description="Run one bounded DJConnect engineering transaction")
    parser.add_argument("prompt", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--owner-authorized", action="store_true", help="record and use the owner's bounded autonomous PR authorization")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd().resolve()
    prompt_path = args.prompt.resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt does not exist: {prompt_path}")
    if args.resume and not args.run_id:
        raise SystemExit("--resume requires --run-id")
    runner = EngineeringRunner(root, StateStore(root / ".djconnect" / "engineering-runs"), SubprocessRepositoryClient(), GhCliClient(), CodexCliClient())
    try:
        state = runner.run(prompt_path, args.run_id, args.resume, args.owner_authorized)
    except (RunnerError, StateError) as error:
        print(f"BLOCKED: {error}")
        return 2
    if state.phase in {"BLOCKED", "FAILED"}:
        print(_format_terminal_report(state))
        if runner.console_detail:
            print(f"\nCodex CLI details:\n{runner.console_detail}")
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
