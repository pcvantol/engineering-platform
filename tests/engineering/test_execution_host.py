from __future__ import annotations

from pathlib import Path
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from engineering_platform.agent_state import StateError, StateStore, TransactionState, is_valid_commit_evidence_record, redact_diagnostic, verified_commit_evidence_record
from engineering_platform.storage import (
    ENGINEERING_STORAGE_SCHEMA_VERSION,
    load_projection,
    load_validation_context,
    record_validation_command_invocation,
    record_validation_command_terminal,
    record_validation_control_result,
    record_validation_profile,
)
from engineering_platform.execution_errors import CodexHandoffTimeout, CodexInvocationError
from engineering_platform.execution_reporting import _target_repository_name
from engineering_platform import execution_host
from engineering_platform.execution_host import (
    AgentResult,
    CodexCliClient,
    CodexInvocationError,
    EngineeringRunner,
    GhCliClient,
    PullRequestEvidence,
    RepositoryEvidence,
    RunnerError,
    SubprocessRepositoryClient,
    _format_terminal_report,
    _format_cli_failure,
    additional_workspace_write_roots,
    build_parser,
    extract_codex_runtime_metadata,
    extract_codex_usage,
    execution_mode_for,
    resolve_execution_context,
    genesis_workspace_preflight,
    format_management_summary,
    terminal_report_matches_state,
    report_consistency_errors,
    collect_terminal_evidence,
    assemble_prompt,
    generate_terminal_report,
    project_codex_activity,
    project_codex_live_action_name,
    write_redacted_codex_cli_log,
    write_codex_usage,
    write_live_status,
)
from engineering_platform.platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    validate_compatibility,
)
from engineering_platform.capability_review import (
    ReviewerResult,
    ReviewerSelection,
    reconciled_recommendations,
    records_for_storage,
    run_reviews,
    select_reviewers,
    reviewer_prompt,
)
from engineering_platform.reviewer_evidence import ReviewerEvidence
from engineering_platform.investigation_ledger import InvocationInvestigationLedger
from engineering_platform.qualification import SCENARIOS, dashboard, execute_qualification, latest_qualification
from engineering_platform.providers import CodexCliProvider, DeterministicValidationExecutor, DeterministicValidationResult
from engineering_platform.execution_executor import (
    MAX_RETAINED_VALIDATION_OUTPUT_CHARACTERS,
    load_validation_failure_diagnostic,
    persist_validation_failure_diagnostic,
    validation_failure_artifact_id,
    workspace_change_summary,
)
from engineering_platform.execution_lease import acquire as acquire_lease, history as lease_history, liveness as lease_liveness, release as release_lease
from engineering_platform.execution_timing import complete_phase, phase_spans, start_phase
from engineering_platform.provider_usage import ProviderInvocation, persist_provider_invocation
from engineering_platform.storage import open_storage


class FakeRepository:
    def __init__(self, *, clean: bool = True, branch: str = "main", contains: bool = True) -> None:
        self.evidence = RepositoryEvidence("pcvantol/djconnect", branch, "a" * 40, clean, contains)
        self.contains = contains
        self.cleanup_calls: list[tuple[str | None, ...]] = []
        self.cleanup_error: RunnerError | None = None
        self.refresh_main_reference_calls: list[Path] = []
        self.refresh_main_reference_error: RunnerError | None = None
        self.synchronize_calls: list[Path] = []
        self.synchronize_error: RunnerError | None = None

    def inspect(self, root: Path) -> RepositoryEvidence:
        return self.evidence

    def main_contains(self, root: Path, sha: str) -> bool:
        return self.contains

    def refresh_main_reference(self, root: Path) -> None:
        self.refresh_main_reference_calls.append(root)
        if self.refresh_main_reference_error:
            raise self.refresh_main_reference_error

    def remote_main_contains(self, root: Path, sha: str) -> bool:
        return self.contains

    def synchronize_main(self, root: Path) -> None:
        self.synchronize_calls.append(root)
        if self.synchronize_error:
            raise self.synchronize_error

    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str:
        self.cleanup_calls.append(branches)
        if self.cleanup_error:
            raise self.cleanup_error
        self.evidence = RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True)
        return "fetched/pruned; main synchronized; removed=already-absent"


class FakeGitHub:
    def __init__(self, responses: list[PullRequestEvidence | RunnerError], *, branch_response: PullRequestEvidence | None = None) -> None:
        self.responses = responses
        self.branch_response = branch_response
        self.calls = 0
        self.branch_calls: list[str] = []
        self.ready_calls: list[int] = []
        self.merge_calls: list[int] = []
        self.markdown_normalization_calls: list[int] = []

    def pull_request(self, number: int) -> PullRequestEvidence:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response

    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None:
        self.branch_calls.append(branch)
        return self.branch_response

    def ready(self, number: int) -> None:
        self.ready_calls.append(number)

    def normalize_markdown_body(self, number: int) -> bool:
        self.markdown_normalization_calls.append(number)
        return False

    def merge(self, number: int) -> None:
        self.merge_calls.append(number)


class FakeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result, self.prompts, self.roots = result, [], []

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.roots.append(root)
        self.prompts.append(prompt)
        return self.result

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "0.146.0"


class ReviewCapableFakeAgent(FakeAgent):
    def __init__(self, result: AgentResult) -> None:
        super().__init__(result)
        self.reviewer_evidence: list[object] = []

    def review(
        self, _: Path, selection: object, __: str, evidence: object = None
    ) -> ReviewerResult:
        self.reviewer_evidence.append(evidence)
        return ReviewerResult(
            getattr(selection, "reviewer"),
            "Reviewer completed the bounded check.",
            ("DISTINCTIVE_REVIEWER_RECOMMENDATION",),
        )


class SequencedFakeAgent(FakeAgent):
    def __init__(self, results: list[AgentResult]) -> None:
        super().__init__(results[0])
        self.results = results

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        return self.results.pop(0)


class LiveStatusFakeAgent(FakeAgent):
    def __init__(self, result: AgentResult) -> None:
        super().__init__(result)
        self.live_phase: str | None = None
        self.live_action: str | None = None
        self.activity_action: str | None = None
        self.activity_callback: object | None = None

    def set_activity_callback(self, callback: object) -> None:
        self.activity_callback = callback

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        payload = json.loads((root / ".engineering" / "status" / "current.json").read_text())
        self.live_phase = payload["phase"]
        self.live_action = payload["current_action"]
        if callable(self.activity_callback):
            self.activity_callback("Codex bewerkt bestanden")
            self.activity_action = json.loads(
                (root / ".engineering" / "status" / "current.json").read_text()
            )["current_action"]
        return super().invoke(root, prompt)


class CommandTimingFakeAgent(FakeAgent):
    def __init__(self, result: AgentResult) -> None:
        super().__init__(result)
        self.command_callback: object | None = None

    def set_command_callback(self, callback: object) -> None:
        self.command_callback = callback

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        if callable(self.command_callback):
            self.command_callback("started", "command-1", "python -m pytest tests/engineering")
            self.command_callback("completed", "command-1", "python -m pytest tests/engineering")
        return super().invoke(root, prompt)


class DeadlineFakeAgent(FakeAgent):
    def __init__(self) -> None:
        super().__init__(AgentResult("WAITING"))
        self.deadline_callback: object | None = None

    def set_handoff_deadline_callback(self, callback: object) -> None:
        self.deadline_callback = callback

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        raise CodexHandoffTimeout("test finalization hand-off timeout")


class LeaseAwareDeadlineAgent(DeadlineFakeAgent):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.observed_lease: dict[str, object] | None = None

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.observed_lease = lease_liveness(root, self.run_id)
        return super().invoke(root, prompt)


class FakeReviewer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.evidence: list[object] = []

    def review(
        self, root: Path, selection: object, objective: str, evidence: object = None
    ) -> ReviewerResult:
        reviewer = getattr(selection, "reviewer")
        self.calls.append(reviewer)
        self.evidence.append(evidence)
        if self.fail:
            raise RuntimeError("reviewer unavailable")
        return ReviewerResult(reviewer, "Bounded review complete.", ("Use canonical wording.",))


class ClientContractTest(unittest.TestCase):
    @patch("engineering_platform.execution_host.subprocess.run")
    def test_repository_main_containment_uses_git_ancestry_evidence(self, run: object) -> None:
        client = SubprocessRepositoryClient()
        run.return_value = subprocess.CompletedProcess(("git",), 0)
        self.assertTrue(client.main_contains(Path("/repository"), "a" * 40))
        run.return_value = subprocess.CompletedProcess(("git",), 1)
        self.assertFalse(client.main_contains(Path("/repository"), "a" * 40))

    def test_repository_synchronization_uses_only_main_fast_forward_commands(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                return ""

        provider = Provider()
        SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))
        self.assertEqual(
            provider.calls,
            [
                ("git", "switch", "main"),
                ("git", "fetch", "origin", "main"),
                ("git", "merge", "--ff-only", "origin/main"),
            ],
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        completed = subprocess.run(("git", *args), cwd=root, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise AssertionError(completed.stderr or completed.stdout)
        return completed.stdout.strip()

    def _managed_sync_fixture(self, temporary: str) -> tuple[Path, Path]:
        root = Path(temporary)
        origin, seed, checkout = root / "origin.git", root / "seed", root / "checkout"
        self._git(root, "init", "--bare", str(origin))
        seed.mkdir()
        self._git(seed, "init")
        self._git(seed, "config", "user.email", "tests@example.invalid")
        self._git(seed, "config", "user.name", "Engineering tests")
        self._git(seed, "checkout", "-b", "main")
        (seed / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(seed, "add", "base.txt")
        self._git(seed, "commit", "-m", "base")
        self._git(seed, "remote", "add", "origin", str(origin))
        self._git(seed, "push", "-u", "origin", "main")
        self._git(root, "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(root, "clone", str(origin), str(checkout))
        self._git(checkout, "config", "user.email", "tests@example.invalid")
        self._git(checkout, "config", "user.name", "Engineering tests")
        return seed, checkout

    def test_repository_synchronization_ignores_multiple_merge_targets_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            seed, checkout = self._managed_sync_fixture(temporary)
            self._git(seed, "checkout", "-b", "other")
            (seed / "other.txt").write_text("other\n", encoding="utf-8")
            self._git(seed, "add", "other.txt")
            self._git(seed, "commit", "-m", "other")
            self._git(seed, "push", "-u", "origin", "other")
            self._git(seed, "checkout", "main")
            (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
            self._git(seed, "add", "remote.txt")
            self._git(seed, "commit", "-m", "remote main")
            self._git(seed, "push", "origin", "main")
            self._git(checkout, "config", "--add", "branch.main.merge", "refs/heads/other")

            implicit_pull = subprocess.run(
                ("git", "pull", "--ff-only"), cwd=checkout, text=True, capture_output=True, check=False
            )
            self.assertNotEqual(implicit_pull.returncode, 0)
            self.assertIn("multiple branches", (implicit_pull.stderr + implicit_pull.stdout).lower())
            self._git(checkout, "update-ref", "-d", "refs/remotes/origin/other")

            client = SubprocessRepositoryClient()
            client.synchronize_main(checkout)
            synchronized_head = self._git(checkout, "rev-parse", "HEAD")
            self.assertEqual(synchronized_head, self._git(seed, "rev-parse", "main"))
            self.assertNotEqual(
                subprocess.run(
                    ("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/other"), cwd=checkout
                ).returncode,
                0,
            )
            client.synchronize_main(checkout)
            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), synchronized_head)

    def test_repository_synchronization_ignores_a_misleading_configured_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            seed, checkout = self._managed_sync_fixture(temporary)
            self._git(seed, "checkout", "-b", "other")
            (seed / "other.txt").write_text("other\n", encoding="utf-8")
            self._git(seed, "add", "other.txt")
            self._git(seed, "commit", "-m", "other")
            self._git(seed, "push", "-u", "origin", "other")
            self._git(seed, "checkout", "main")
            (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
            self._git(seed, "add", "remote.txt")
            self._git(seed, "commit", "-m", "remote main")
            self._git(seed, "push", "origin", "main")
            origin_url = self._git(checkout, "remote", "get-url", "origin")
            self._git(checkout, "remote", "add", "misleading", origin_url)
            self._git(checkout, "config", "branch.main.remote", "misleading")
            self._git(checkout, "config", "branch.main.merge", "refs/heads/other")

            SubprocessRepositoryClient().synchronize_main(checkout)

            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), self._git(seed, "rev-parse", "main"))

    def test_resumed_host_reuses_persisted_implementation_pr_after_two_restarts(self) -> None:
        """A departed host resumes the one persisted implementation PR only."""
        class HostDisappeared(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            _, checkout = self._managed_sync_fixture(temporary)
            prompt = checkout / "prompt.md"
            prompt.write_text("# bounded objective\n", encoding="utf-8")
            manifest = checkout / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"platform_version":"2.0.0","runner_version":"2.0.0",'
                '"bootstrap_contract":"2026.12","checkpoint_format":1,'
                '"memory_format":2,"report_format":2,"minimum_codex_cli":"0.146.0",'
                '"watcher_version":"2.0.0","inbox_protocol":1,"dashboard_version":"2.0.0",'
                '"handoff_protocol":1,"status_model":1,"storage_schema":29}\n',
                encoding="utf-8",
            )
            store = StateStore(checkout / ".engineering" / "engineering-runs")
            commit = "b" * 40
            branch = "codex/reuse-implementation-pr"
            repository = FakeRepository()

            class DeliveryAgent(FakeAgent):
                def __init__(self) -> None:
                    super().__init__(AgentResult("COMPLETE"))
                    self.pr_create_calls = 0

                def invoke(self, root: Path, prompt_text: str) -> AgentResult:
                    self.roots.append(root)
                    self.prompts.append(prompt_text)
                    if "Local repository validation gate" in prompt_text:
                        self.pr_create_calls += 1
                        return AgentResult("COMPLETE", branch, 701, commit_sha=commit)
                    if "Mandatory autonomous refactor" in prompt_text:
                        return AgentResult("COMPLETE", branch, 701, commit_sha=commit)
                    repository.evidence = RepositoryEvidence(
                        "pcvantol/djconnect", branch, commit, True
                    )
                    return AgentResult("COMPLETE", branch, commit_sha=commit)

            agent = DeliveryAgent()
            github = FakeGitHub([
                PullRequestEvidence(
                    701, "OPEN", True, True, head_branch=branch, base_branch="main"
                )
            ])
            first_host = EngineeringRunner(
                checkout, store, repository, github, agent, lambda _: None
            )

            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(first_host, "_poll", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    first_host.run(prompt, run_id="implementation-pr-restart", owner_authorized=True)

            persisted = store.load("implementation-pr-restart")
            self.assertEqual(persisted.run_id, "implementation-pr-restart")
            self.assertTrue(
                any(item["commit_sha"] == commit for item in persisted.commit_evidence)
            )
            self.assertEqual(persisted.pull_request, 701)
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(github.ready_calls, [701])
            self.assertNotIn("Retry-Of:", prompt.read_text(encoding="utf-8"))

            assert first_host.lease_heartbeat is not None
            assert first_host.active_lease is not None
            release_lease(checkout, first_host.lease_heartbeat.stop())
            first_host.active_lease = None
            first_host.lease_heartbeat = None

            resumed_store = StateStore(checkout / ".engineering" / "engineering-runs")
            resumed_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            resumed_host = EngineeringRunner(
                checkout, resumed_store, repository, github, resumed_agent, lambda _: None
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ):
                resumed = resumed_host.run(
                    prompt, run_id="implementation-pr-restart", resume=True
                )

            self.assertEqual(resumed.run_id, persisted.run_id)
            self.assertEqual(resumed.commit_evidence, persisted.commit_evidence)
            self.assertEqual(resumed.pull_request, persisted.pull_request)
            self.assertEqual(resumed.phase, "WAIT_FOR_OPERATOR_MERGE")
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(resumed_agent.prompts, [])
            self.assertEqual(
                [path.stem for path in resumed_store.directory.glob("*.json")],
                ["implementation-pr-restart"],
            )

            second_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            second_host = EngineeringRunner(
                checkout,
                StateStore(checkout / ".engineering" / "engineering-runs"),
                repository,
                github,
                second_agent,
                lambda _: None,
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ):
                second_restart = second_host.run(
                    prompt, run_id="implementation-pr-restart", resume=True
                )

            self.assertEqual(second_restart.run_id, persisted.run_id)
            self.assertEqual(second_restart.commit_evidence, persisted.commit_evidence)
            self.assertEqual(second_restart.pull_request, persisted.pull_request)
            self.assertEqual(second_restart.phase, "WAIT_FOR_OPERATOR_MERGE")
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(github.ready_calls, [701])
            self.assertEqual(second_agent.prompts, [])

    def test_resumed_host_observes_merged_implementation_pr_once_before_finalization(self) -> None:
        """A merged implementation hand-off enters Finalization once after restart."""
        class HostDisappeared(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            _, checkout = self._managed_sync_fixture(temporary)
            prompt = checkout / "prompt.md"
            prompt.write_text("# bounded objective\n", encoding="utf-8")
            manifest = checkout / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"platform_version":"2.0.0","runner_version":"2.0.0",'
                '"bootstrap_contract":"2026.12","checkpoint_format":1,'
                '"memory_format":2,"report_format":2,"minimum_codex_cli":"0.146.0",'
                '"watcher_version":"2.0.0","inbox_protocol":1,"dashboard_version":"2.0.0",'
                '"handoff_protocol":1,"status_model":1,"storage_schema":29}\n',
                encoding="utf-8",
            )
            run_id, commit, implementation_pr = "implementation-merge-restart", "b" * 40, 701
            state = TransactionState(
                run_id, "pcvantol/djconnect", str(prompt), "WAIT_FOR_OPERATOR_MERGE",
                branch="codex/reuse-implementation-pr", pull_request=implementation_pr,
                owner_authorized=True, last_verified_sha=commit,
                waiting_for_merge_since="2026-08-30T00:00:00+00:00",
                commit_evidence=(verified_commit_evidence_record(
                    phase="LOCAL_REPOSITORY_VALIDATION", observed_at="2026-08-30T00:00:00+00:00",
                    commit_sha=commit, description="local_repository_validation_commit_verified",
                ),),
            )
            store = StateStore(checkout / ".engineering" / "engineering-runs")
            store.save(state)
            repository = FakeRepository(contains=True)
            github = FakeGitHub([
                PullRequestEvidence(
                    implementation_pr, "MERGED", True, True, commit,
                    head_branch=state.branch, base_branch="main",
                )
            ])
            delivery_create_calls = 1
            first_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            first_host = EngineeringRunner(
                checkout, store, repository, github, first_agent, lambda _: None
            )

            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(first_host, "_invoke_agent_with_timing", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    first_host.run(prompt, run_id=run_id, resume=True)

            finalization_entry = store.load(run_id)
            self.assertEqual(finalization_entry.run_id, run_id)
            self.assertEqual(finalization_entry.implementation_pull_request, implementation_pr)
            self.assertEqual(finalization_entry.implementation_merge_commit, commit)
            self.assertEqual(finalization_entry.phase, "FINALIZE_AGENT")
            self.assertEqual(finalization_entry.transaction_kind, "FINALIZATION")
            self.assertEqual(delivery_create_calls, 1)
            self.assertEqual(github.calls, 1)
            self.assertEqual(github.merge_calls, [])
            self.assertEqual(first_agent.prompts, [])
            self.assertNotIn("Retry-Of:", prompt.read_text(encoding="utf-8"))
            self.assertEqual(
                [path.stem for path in store.directory.glob("*.json")], [run_id]
            )

            second_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            second_host = EngineeringRunner(
                checkout,
                StateStore(checkout / ".engineering" / "engineering-runs"),
                repository,
                github,
                second_agent,
                lambda _: None,
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(second_host, "_invoke_agent_with_timing", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    second_host.run(prompt, run_id=run_id, resume=True)
            second_restart = StateStore(
                checkout / ".engineering" / "engineering-runs"
            ).load(run_id)

            self.assertEqual(second_restart.run_id, run_id)
            self.assertEqual(second_restart.implementation_pull_request, implementation_pr)
            self.assertEqual(second_restart.implementation_merge_commit, commit)
            self.assertEqual(second_restart.phase, "FINALIZE_AGENT")
            self.assertEqual(delivery_create_calls, 1)
            self.assertEqual(github.calls, 1)
            self.assertEqual(github.merge_calls, [])
            self.assertEqual(second_agent.prompts, [])
            self.assertEqual(
                sum(
                    span["phase_name"] == "REPOSITORY_FINALIZATION"
                    for span in phase_spans(checkout, run_id)
                ),
                1,
            )

    def test_resumed_host_reuses_persisted_finalization_pr_after_two_restarts(self) -> None:
        """A durable Finalization PR hand-off never creates a replacement."""
        class HostDisappeared(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            _, checkout = self._managed_sync_fixture(temporary)
            prompt = checkout / "prompt.md"
            prompt.write_text("# bounded objective\n", encoding="utf-8")
            manifest = checkout / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"platform_version":"2.0.0","runner_version":"2.0.0",'
                '"bootstrap_contract":"2026.12","checkpoint_format":1,'
                '"memory_format":2,"report_format":2,"minimum_codex_cli":"0.146.0",'
                '"watcher_version":"2.0.0","inbox_protocol":1,"dashboard_version":"2.0.0",'
                '"handoff_protocol":1,"status_model":1,"storage_schema":29}\n',
                encoding="utf-8",
            )
            run_id, implementation_pr, finalization_pr = "finalization-pr-restart", 701, 702
            implementation_commit, finalization_commit = "b" * 40, "c" * 40
            implementation_branch = "codex/reuse-implementation-pr"
            finalization_branch = f"codex/finalize-{run_id}"
            state = TransactionState(
                run_id, "pcvantol/djconnect", str(prompt), "FINALIZE_AGENT",
                branch=finalization_branch, owner_authorized=True,
                transaction_kind="FINALIZATION", implementation_branch=implementation_branch,
                implementation_pull_request=implementation_pr,
                implementation_merge_commit=implementation_commit,
                finalization_branch=finalization_branch,
            )
            store = StateStore(checkout / ".engineering" / "engineering-runs")
            repository = FakeRepository(contains=True)

            class FinalizationDeliveryAgent(FakeAgent):
                def __init__(self) -> None:
                    super().__init__(AgentResult("COMPLETE"))
                    self.pr_create_calls = 0

                def invoke(self, root: Path, prompt_text: str) -> AgentResult:
                    self.roots.append(root)
                    self.prompts.append(prompt_text)
                    self.pr_create_calls += 1
                    repository.evidence = RepositoryEvidence(
                        "pcvantol/djconnect", finalization_branch, finalization_commit, True
                    )
                    return AgentResult(
                        "COMPLETE", finalization_branch, finalization_pr,
                        commit_sha=finalization_commit,
                    )

            agent = FinalizationDeliveryAgent()
            github = FakeGitHub([
                PullRequestEvidence(
                    finalization_pr, "OPEN", True, True,
                    head_branch=finalization_branch, base_branch="main",
                )
            ])
            delivery_host = EngineeringRunner(
                checkout, store, repository, github, agent, lambda _: None
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(delivery_host, "_poll", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    delivery_host._start_finalization(state, implementation_pr)

            persisted = store.load(run_id)
            self.assertEqual(persisted.run_id, run_id)
            self.assertEqual(persisted.finalization_pull_request, finalization_pr)
            self.assertEqual(persisted.pull_request, finalization_pr)
            self.assertTrue(
                any(item["commit_sha"] == finalization_commit for item in persisted.commit_evidence)
            )
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(agent.prompts.count(agent.prompts[0]), 1)
            self.assertNotIn("Retry-Of:", prompt.read_text(encoding="utf-8"))

            first_restart_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            first_restart = EngineeringRunner(
                checkout,
                StateStore(checkout / ".engineering" / "engineering-runs"),
                repository,
                github,
                first_restart_agent,
                lambda _: None,
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ):
                first_resumed = first_restart.run(prompt, run_id=run_id, resume=True)

            self.assertEqual(first_resumed.run_id, run_id)
            self.assertEqual(first_resumed.finalization_pull_request, finalization_pr)
            self.assertEqual(first_resumed.pull_request, finalization_pr)
            self.assertEqual(first_resumed.commit_evidence, persisted.commit_evidence)
            self.assertEqual(first_resumed.phase, "WAIT_FOR_OPERATOR_MERGE")
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(first_restart_agent.prompts, [])

            second_restart_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            second_restart = EngineeringRunner(
                checkout,
                StateStore(checkout / ".engineering" / "engineering-runs"),
                repository,
                github,
                second_restart_agent,
                lambda _: None,
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ):
                second_resumed = second_restart.run(prompt, run_id=run_id, resume=True)

            self.assertEqual(second_resumed.run_id, run_id)
            self.assertEqual(second_resumed.finalization_pull_request, finalization_pr)
            self.assertEqual(second_resumed.pull_request, finalization_pr)
            self.assertEqual(second_resumed.commit_evidence, persisted.commit_evidence)
            self.assertEqual(second_resumed.phase, "WAIT_FOR_OPERATOR_MERGE")
            self.assertEqual(agent.pr_create_calls, 1)
            self.assertEqual(first_restart_agent.prompts, [])
            self.assertEqual(second_restart_agent.prompts, [])
            self.assertEqual(github.merge_calls, [])
            self.assertEqual(github.calls, 3)
            self.assertEqual(
                [path.stem for path in store.directory.glob("*.json")], [run_id]
            )

    def test_resumed_host_observes_merged_finalization_pr_once_before_reconciliation(self) -> None:
        """A merged Finalization PR enters reconciliation without replaying delivery."""
        class HostDisappeared(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            _, checkout = self._managed_sync_fixture(temporary)
            prompt = checkout / "prompt.md"
            prompt.write_text("# bounded objective\n", encoding="utf-8")
            manifest = checkout / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"platform_version":"2.0.0","runner_version":"2.0.0",'
                '"bootstrap_contract":"2026.12","checkpoint_format":1,'
                '"memory_format":2,"report_format":2,"minimum_codex_cli":"0.146.0",'
                '"watcher_version":"2.0.0","inbox_protocol":1,"dashboard_version":"2.0.0",'
                '"handoff_protocol":1,"status_model":1,"storage_schema":29}\n',
                encoding="utf-8",
            )
            run_id, implementation_pr, finalization_pr = "finalization-merge-restart", 701, 702
            implementation_commit, finalization_commit, merge_commit = "b" * 40, "c" * 40, "d" * 40
            finalization_branch = "codex/finalize-finalization-merge-restart"
            state = TransactionState(
                run_id, "pcvantol/djconnect", str(prompt), "WAIT_FOR_OPERATOR_MERGE",
                branch=finalization_branch, pull_request=finalization_pr,
                owner_authorized=True, transaction_kind="FINALIZATION",
                implementation_branch="codex/reuse-implementation-pr",
                implementation_pull_request=implementation_pr,
                implementation_merge_commit=implementation_commit,
                finalization_branch=finalization_branch,
                finalization_pull_request=finalization_pr,
                finalization_head_sha=finalization_commit,
                waiting_for_merge_since="2026-08-30T00:00:00+00:00",
                commit_evidence=(verified_commit_evidence_record(
                    phase="FINALIZE_AGENT", observed_at="2026-08-30T00:00:00+00:00",
                    commit_sha=finalization_commit, description="finalization_commit_verified",
                ),),
            )
            store = StateStore(checkout / ".engineering" / "engineering-runs")
            store.save(state)
            repository = FakeRepository(contains=True)
            github = FakeGitHub([
                PullRequestEvidence(
                    finalization_pr, "MERGED", True, True, merge_commit,
                    head_branch=finalization_branch, base_branch="main",
                )
            ])
            finalization_pr_create_calls = 1
            first_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            first_host = EngineeringRunner(
                checkout, store, repository, github, first_agent, lambda _: None
            )

            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(first_host, "_invoke_agent_with_timing", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    first_host.run(prompt, run_id=run_id, resume=True)

            reconciliation_entry = store.load(run_id)
            self.assertEqual(reconciliation_entry.run_id, run_id)
            self.assertEqual(reconciliation_entry.transaction_kind, "RECONCILIATION")
            self.assertEqual(reconciliation_entry.phase, "RECONCILE_AGENT")
            self.assertEqual(reconciliation_entry.finalization_pull_request, finalization_pr)
            self.assertTrue(
                any(item["commit_sha"] == finalization_commit for item in reconciliation_entry.commit_evidence)
            )
            self.assertTrue(
                any(item["commit_sha"] == merge_commit for item in reconciliation_entry.commit_evidence)
            )
            self.assertEqual(reconciliation_entry.finalization_merge_commit, merge_commit)
            self.assertEqual(finalization_pr_create_calls, 1)
            self.assertEqual(github.calls, 1)
            self.assertEqual(github.merge_calls, [])
            self.assertEqual(first_agent.prompts, [])
            self.assertNotIn("Retry-Of:", prompt.read_text(encoding="utf-8"))

            second_agent = FakeAgent(AgentResult("BLOCKED", diagnostic="must not be invoked"))
            second_host = EngineeringRunner(
                checkout,
                StateStore(checkout / ".engineering" / "engineering-runs"),
                repository,
                github,
                second_agent,
                lambda _: None,
            )
            with patch(
                "engineering_platform.execution_host.provider_readiness_failures", return_value=()
            ), patch.object(second_host, "_invoke_agent_with_timing", side_effect=HostDisappeared):
                with self.assertRaises(HostDisappeared):
                    second_host.run(prompt, run_id=run_id, resume=True)
            second_restart = StateStore(
                checkout / ".engineering" / "engineering-runs"
            ).load(run_id)

            self.assertEqual(second_restart.run_id, run_id)
            self.assertEqual(second_restart.transaction_kind, "RECONCILIATION")
            self.assertEqual(second_restart.phase, "RECONCILE_AGENT")
            self.assertEqual(second_restart.finalization_pull_request, finalization_pr)
            self.assertEqual(second_restart.commit_evidence, reconciliation_entry.commit_evidence)
            self.assertEqual(second_restart.finalization_merge_commit, merge_commit)
            self.assertEqual(finalization_pr_create_calls, 1)
            self.assertEqual(github.calls, 1)
            self.assertEqual(github.merge_calls, [])
            self.assertEqual(second_agent.prompts, [])
            self.assertEqual(
                [path.stem for path in store.directory.glob("*.json")], [run_id]
            )
            with open_storage(checkout) as connection:
                reconciliation_transitions = connection.execute(
                    "SELECT COUNT(*) FROM managed_autonomy_actions "
                    "WHERE run_id=? AND action='AUTOMATIC_RECONCILIATION'",
                    (run_id,),
                ).fetchone()[0]
            self.assertEqual(reconciliation_transitions, 1)

    def test_repository_synchronization_fails_closed_when_main_diverges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            seed, checkout = self._managed_sync_fixture(temporary)
            (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
            self._git(seed, "add", "remote.txt")
            self._git(seed, "commit", "-m", "remote main")
            self._git(seed, "push", "origin", "main")
            (checkout / "local.txt").write_text("local\n", encoding="utf-8")
            self._git(checkout, "add", "local.txt")
            self._git(checkout, "commit", "-m", "local main")
            local_head = self._git(checkout, "rev-parse", "HEAD")

            with self.assertRaisesRegex(
                RunnerError,
                rf"target_branch=main authoritative_ref=origin/main local_sha={local_head} .*fast_forward_state=diverged",
            ):
                SubprocessRepositoryClient().synchronize_main(checkout)

            self.assertEqual(self._git(checkout, "rev-parse", "HEAD"), local_head)

    def test_repository_main_reference_refresh_does_not_change_the_checkout(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                return ""

        provider = Provider()
        SubprocessRepositoryClient(provider).refresh_main_reference(Path("/repository"))
        self.assertEqual(provider.calls, [("git", "fetch", "origin", "main")])

    def test_repository_remote_main_containment_uses_refreshed_remote_reference(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def execute(self, _: Path, *args: str) -> subprocess.CompletedProcess[str]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 0)

        provider = Provider()
        self.assertTrue(
            SubprocessRepositoryClient(provider).remote_main_contains(Path("/repository"), "a" * 40)
        )
        self.assertEqual(
            provider.calls,
            [("git", "merge-base", "--is-ancestor", "a" * 40, "origin/main")],
        )

    @patch("engineering_platform.execution_repository.time.sleep")
    def test_repository_synchronization_retries_only_a_transient_index_lock_conflict(self, sleep: object) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []
                self.failures = 1

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("fatal: Unable to create '.git/index.lock': File exists.")
                return ""

        provider = Provider()
        SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))

        self.assertEqual(
            provider.calls,
            [
                ("git", "switch", "main"),
                ("git", "switch", "main"),
                ("git", "fetch", "origin", "main"),
                ("git", "merge", "--ff-only", "origin/main"),
            ],
        )
        sleep.assert_called_once_with(0.5)

    def test_repository_synchronization_stops_after_an_authoritative_fetch_failure(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                if args == ("git", "fetch", "origin", "main"):
                    raise RuntimeError("fatal: origin unavailable")
                return ""

        provider = Provider()
        with self.assertRaisesRegex(RunnerError, "origin unavailable"):
            SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))
        self.assertEqual(provider.calls, [("git", "switch", "main"), ("git", "fetch", "origin", "main")])

    def test_repository_synchronization_does_not_fallback_after_fast_forward_failure(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                if args == ("git", "merge", "--ff-only", "origin/main"):
                    raise RuntimeError("fatal: Not possible to fast-forward")
                return ""

        provider = Provider()
        with self.assertRaisesRegex(RunnerError, "fast-forward"):
            SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))
        self.assertEqual(
            provider.calls,
            [
                ("git", "switch", "main"),
                ("git", "fetch", "origin", "main"),
                ("git", "merge", "--ff-only", "origin/main"),
            ],
        )

    @patch("engineering_platform.execution_repository.time.sleep")
    def test_repository_synchronization_does_not_retry_a_git_permission_failure(self, sleep: object) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                raise RuntimeError("fatal: Unable to create '.git/index.lock': Permission denied")

        provider = Provider()
        with self.assertRaisesRegex(RunnerError, "Permission denied"):
            SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))

        self.assertEqual(provider.calls, [("git", "switch", "main")])
        sleep.assert_not_called()

    @patch("engineering_platform.execution_repository.time.sleep")
    def test_repository_synchronization_stops_after_bounded_lock_retries(self, sleep: object) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, _: Path, *args: str) -> str:
                self.calls.append(args)
                raise RuntimeError("fatal: Unable to create '.git/index.lock': File exists.")

        provider = Provider()
        with self.assertRaisesRegex(RunnerError, "index.lock.*File exists"):
            SubprocessRepositoryClient(provider).synchronize_main(Path("/repository"))

        self.assertEqual(provider.calls, [("git", "switch", "main")] * 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_repository_client_rejects_non_platform_roots_and_provider_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RunnerError, "canonical BOOTSTRAP"):
                SubprocessRepositoryClient().inspect(Path(temporary))

        class FailingProvider:
            def command(self, _: Path, *args: str) -> str:
                raise RuntimeError("git unavailable")

        with self.assertRaisesRegex(RunnerError, "git unavailable"):
            SubprocessRepositoryClient(FailingProvider()).synchronize_main(Path("/repository"))

    def test_github_client_translates_provider_failures_and_allows_already_ready(self) -> None:
        class Provider:
            def github(self, *_: str) -> str:
                raise RuntimeError("already ready for review")

        GhCliClient(Provider()).ready(42)

        class FailingProvider:
            def github(self, *_: str) -> str:
                raise RuntimeError("service unavailable")

        with self.assertRaisesRegex(RunnerError, "service unavailable"):
            GhCliClient(FailingProvider()).merge(42)

    def test_github_client_normalizes_only_a_fully_escaped_markdown_body(self) -> None:
        class Provider:
            def __init__(self, body: str) -> None:
                self.body, self.calls = body, []
            def github(self, *args: str) -> str:
                self.calls.append(args)
                if args[0:2] == ("pr", "view"):
                    return json.dumps({"body": self.body})
                return ""

        provider = Provider("## Summary\\n- first\\n- second")
        self.assertTrue(GhCliClient(provider).normalize_markdown_body(42))
        self.assertEqual(provider.calls[-1], ("pr", "edit", "42", "--body", "## Summary\n- first\n- second"))
        real_markdown = Provider("## Summary\n\n- code: `\\n`")
        self.assertFalse(GhCliClient(real_markdown).normalize_markdown_body(42))
        self.assertEqual(len(real_markdown.calls), 1)

    def test_codex_client_availability_and_version_fail_closed(self) -> None:
        class Provider:
            def __init__(self, code: int, stdout: str = "") -> None:
                self.code, self.stdout = code, stdout

            def command(self, *_: str) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(("codex",), self.code, self.stdout, "")

        self.assertFalse(CodexCliClient(Provider(1)).available())
        with self.assertRaisesRegex(RunnerError, "version could not be detected"):
            CodexCliClient(Provider(1)).version()
        self.assertEqual(CodexCliClient(Provider(0, "codex-cli 0.146.0")).version(), "0.146.0")

    def test_codex_client_records_only_the_managed_cli_installation_path(self) -> None:
        with patch(
            "engineering_platform.execution_executor.CodexCliProvider.managed_installation_path",
            return_value="/managed/engineering-platform/codex-cli",
        ):
            self.assertEqual(
                CodexCliClient().last_runtime_metadata,
                {
                    "runtime_provider": "codex_cli",
                    "codex_cli_installation_path": "/managed/engineering-platform/codex-cli",
                },
            )

    def test_repository_client_inspects_and_translates_provider_failures(self) -> None:
        class Provider:
            def command(self, root: Path, *args: str) -> str:
                values = {
                    ("git", "remote", "get-url", "origin"): "git@github.com:pcvantol/djconnect.git",
                    ("git", "branch", "--show-current"): "main",
                    ("git", "rev-parse", "HEAD"): "a" * 40,
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
                }
                return values[args]

            def execute(self, _: Path, *args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.execution_host.subprocess.run") as run:
            root = Path(temporary)
            (root / "BOOTSTRAP.md").write_text("contract", encoding="utf-8")
            (root / ".git").mkdir()
            run.return_value = subprocess.CompletedProcess(("git",), 0)
            evidence = SubprocessRepositoryClient(Provider()).inspect(root)
            self.assertEqual(evidence.repository, "pcvantol/djconnect")
            self.assertTrue(evidence.clean)
            self.assertTrue(evidence.main_contains_head)

        class FailingProvider:
            def command(self, _: Path, *args: str) -> str:
                raise RuntimeError("provider failed")

        with self.assertRaisesRegex(RunnerError, "provider failed"):
            SubprocessRepositoryClient(FailingProvider())._run(Path("/tmp"), "git", "status")

    def test_repository_client_normalizes_https_github_origin(self) -> None:
        class Provider:
            def command(self, root: Path, *args: str) -> str:
                values = {
                    ("git", "remote", "get-url", "origin"): "https://github.com/pcvantol/ep-pa1q-qualification.git",
                    ("git", "branch", "--show-current"): "main",
                    ("git", "rev-parse", "HEAD"): "a" * 40,
                    ("git", "status", "--porcelain", "--untracked-files=all"): "",
                }
                return values[args]

            def execute(self, _: Path, *args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "BOOTSTRAP.md").write_text("contract", encoding="utf-8")
            (root / ".git").mkdir()
            evidence = SubprocessRepositoryClient(Provider()).inspect(root)

        self.assertEqual(evidence.repository, "pcvantol/ep-pa1q-qualification")

    def test_terminal_reporting_normalizes_https_github_origin(self) -> None:
        with patch(
            "engineering_platform.execution_reporting._git_output",
            return_value="https://github.com/pcvantol/ep-pa1q-qualification.git",
        ):
            repository = _target_repository_name(Path("/qualification/repo"), "fallback/repository")

        self.assertEqual(repository, "pcvantol/ep-pa1q-qualification")

    def test_github_client_interprets_checks_and_control_commands(self) -> None:
        class Provider:
            def __init__(self, response: str) -> None:
                self.response = response
                self.calls: list[tuple[str, ...]] = []

            def github(self, *args: str) -> str:
                self.calls.append(args)
                return self.response

        response = json.dumps(
            {
                "number": 7,
                "state": "OPEN",
                "isDraft": False,
                "mergeCommit": {"oid": "b" * 40},
                "headRefName": "engineering/example",
                "baseRefName": "main",
                "statusCheckRollup": [
                    {"name": "green", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "bad", "status": "COMPLETED", "conclusion": "FAILURE"},
                    {"name": None, "status": None, "conclusion": None},
                ],
            }
        )
        provider = Provider(response)
        client = GhCliClient(provider)
        evidence = client.pull_request(7)
        self.assertTrue(evidence.checks_terminal)
        self.assertFalse(evidence.checks_passed)
        self.assertEqual(evidence.failed_checks, ("bad",))
        self.assertEqual(evidence.head_branch, "engineering/example")
        self.assertEqual(evidence.base_branch, "main")
        client.ready(7)
        client.merge(7)
        self.assertIn(("pr", "ready", "7"), provider.calls)
        self.assertIn(("pr", "merge", "7", "--squash", "--delete-branch"), provider.calls)

    def test_github_client_ignores_empty_check_rollup_entries(self) -> None:
        class Provider:
            def github(self, *_: str) -> str:
                return json.dumps(
                    {
                        "number": 8,
                        "state": "MERGED",
                        "isDraft": False,
                        "mergeCommit": {"oid": "c" * 40},
                        "statusCheckRollup": [
                            {"name": "green", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"name": None, "status": None, "conclusion": None},
                        ],
                    }
                )

        evidence = GhCliClient(Provider()).pull_request(8)
        self.assertTrue(evidence.checks_terminal)
        self.assertTrue(evidence.checks_passed)
        self.assertEqual(evidence.failed_checks, ())

    @patch("engineering_platform.execution_host.subprocess.run")
    def test_codex_client_handles_valid_review_and_invoke_results(self, run: object) -> None:
        review_message = json.dumps(
            {"contribution": "reviewed", "recommendations": ["keep scope"]}
        )
        agent_message = json.dumps(
            {
                "terminal_state": "COMPLETE",
                "branch": "codex/test",
                "pull_request": 12,
                "terminal_condition": "repository_reconciled",
                "diagnostic": "safe",
                "repository_path": "/tmp/repository",
                "commit_sha": "c" * 40,
            }
        )
        review_output = "\n".join((
            json.dumps({"type": "turn.started", "metadata": {"model": "gpt-5.6-terra"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": review_message}}),
        ))
        run.side_effect = [
            subprocess.CompletedProcess(("codex",), 0, review_output, ""),
            subprocess.CompletedProcess(("codex",), 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": agent_message}}), ""),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = CodexCliClient(CodexCliProvider())
            review = client.review(root, __import__("engineering_platform.capability_review", fromlist=["ReviewerSelection"]).ReviewerSelection("validation", "scope", 1), "objective")
            result = client.invoke(root, "objective")
        self.assertFalse(review.failed)
        self.assertEqual(review.recommendations, ("keep scope",))
        self.assertEqual(review.runtime_metadata["raw_provider_model"], "gpt-5.6-terra")
        self.assertEqual(review.usage["input_tokens"], 100)
        self.assertEqual(result.pull_request, 12)

    @patch("engineering_platform.execution_host.time.monotonic", side_effect=(10.0, 12.75))
    @patch("engineering_platform.execution_host.subprocess.run")
    def test_codex_client_records_measured_invocation_time(
        self, run: object, _: object
    ) -> None:
        agent_message = json.dumps(
            {
                "terminal_state": "COMPLETE",
                "branch": "codex/test",
                "pull_request": 12,
                "terminal_condition": "repository_reconciled",
                "diagnostic": "safe",
                "repository_path": "/tmp/repository",
                "commit_sha": "c" * 40,
            }
        )
        run.return_value = subprocess.CompletedProcess(
            ("codex",),
            0,
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": agent_message}}),
            "",
        )
        with tempfile.TemporaryDirectory() as temporary:
            client = CodexCliClient()
            client.invoke(Path(temporary), "objective")
        self.assertEqual(client.last_execution_seconds, 2.75)


    @patch("engineering_platform.execution_host.subprocess.run")
    def test_codex_client_keeps_bounded_diagnostics_on_failures(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(("codex",), 1, "prompt body", "token=secret\nfailed")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodexInvocationError) as raised:
                CodexCliClient().invoke(Path(temporary), "prompt body")
        self.assertIn("code 1", str(raised.exception))
        self.assertNotIn("secret", raised.exception.console_detail)

    @patch("engineering_platform.execution_host.subprocess.run")
    def test_codex_client_classifies_a_usage_limit_without_persisting_provider_copy(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            ("codex",),
            1,
            "You've hit your usage limit. Visit the account usage page to purchase more credits or try again at tomorrow.",
            "",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodexInvocationError) as raised:
                CodexCliClient().invoke(Path(temporary), "objective")
        self.assertEqual(raised.exception.next_action, "resolve_codex_usage_limit")
        self.assertEqual(raised.exception.terminal_condition, "codex_usage_limit_reached")
        self.assertEqual(
            str(raised.exception),
            "Codex usage limit reached. Add Codex credits or resume after the account limit resets.",
        )
        self.assertNotIn("purchase more credits", str(raised.exception))

    @patch("engineering_platform.execution_host.subprocess.run")
    def test_codex_client_classifies_interrupted_turn_without_agent_result(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            ("codex",), 1, '{"type":"turn_aborted","reason":"interrupted"}\n', ""
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodexInvocationError) as raised:
                CodexCliClient().invoke(Path(temporary), "objective")
        self.assertTrue(raised.exception.provider_turn_interrupted)
        self.assertEqual(raised.exception.terminal_condition, "provider_turn_interrupted")
        self.assertEqual(raised.exception.next_action, "NONE")

    @patch("engineering_platform.execution_host.generate_terminal_report", return_value=None)
    @patch("engineering_platform.execution_host.EngineeringRunner")
    def test_main_publishes_complete_runner_result(self, runner_type: object, _: object) -> None:
        state = TransactionState(
            run_id="run-main",
            repository="pcvantol/djconnect",
            prompt_path="prompt.md",
            phase="COMPLETE",
            next_action="repository_reconciled",
            terminal=True,
        )
        runner_type.return_value.run.return_value = state
        runner_type.return_value.platform_manifest = None
        runner_type.return_value.console_detail = None
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.execution_host.Path.cwd", return_value=Path(temporary)):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            central = Path(temporary) / "engineering.db"
            central.touch()
            with sqlite3.connect(central) as connection:
                connection.execute("CREATE TABLE execution_phase_spans(phase_id TEXT,run_id TEXT,phase_name TEXT,phase_category TEXT,parent_phase_id TEXT,attempt INTEGER,ordinal INTEGER,started_at TEXT,completed_at TEXT,duration_ms INTEGER,outcome TEXT,metadata TEXT)")
            self.assertEqual(__import__("engineering_platform.execution_host", fromlist=["main"]).main([str(prompt), "--central-database", str(central)]), 0)

    @patch("engineering_platform.execution_host.EngineeringRunner")
    def test_main_reports_blocked_runner_and_writes_redacted_console_log(self, runner_type: object) -> None:
        runner_type.return_value.run.side_effect = RunnerError("blocked preflight")
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.execution_host.Path.cwd", return_value=Path(temporary)):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            central = Path(temporary) / "engineering.db"
            central.touch()
            self.assertEqual(__import__("engineering_platform.execution_host", fromlist=["main"]).main([str(prompt), "--central-database", str(central)]), 2)

    def test_main_rejects_unbound_operational_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.execution_host.Path.cwd", return_value=Path(temporary)):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "CENTRAL_OPERATIONAL_DATABASE_REQUIRED"):
                __import__("engineering_platform.execution_host", fromlist=["main"]).main([str(prompt)])

    @patch.object(SubprocessRepositoryClient, "inspect")
    @patch("engineering_platform.execution_host.subprocess.run")
    def test_repository_cleanup_handles_absent_and_squash_merged_branches(
        self, run: object, inspect: object
    ) -> None:
        class Provider:
            def command(self, _: Path, *args: str) -> str:
                return ""

            def execute(self, _: Path, *args: str) -> subprocess.CompletedProcess[str]:
                return next(run.side_effect)

        clean = RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True)
        inspect.return_value = clean
        run.side_effect = [
            subprocess.CompletedProcess(("git",), 0),  # existing branch
            subprocess.CompletedProcess(("git",), 1, "", "not ancestral"),
            subprocess.CompletedProcess(("git",), 0),  # forced squash cleanup
            subprocess.CompletedProcess(("git",), 1),  # absent branch
        ]
        detail = SubprocessRepositoryClient(Provider()).cleanup_transaction(
            Path("/repository"), ("codex/transaction", "codex/absent", "codex/transaction")
        )
        self.assertIn("removed=codex/transaction", detail)
        self.assertIn("squash-reconciled=codex/transaction", detail)

    def test_repository_cleanup_rejects_main_and_dirty_workspaces(self) -> None:
        class Provider:
            def command(self, _: Path, *args: str) -> str:
                return ""

        client = SubprocessRepositoryClient(Provider())
        with patch.object(client, "inspect", return_value=RepositoryEvidence("repo", "main", "a" * 40, False, True)):
            with self.assertRaisesRegex(RunnerError, "not clean"):
                client.cleanup_transaction(Path("/repository"), ())
        with patch.object(client, "inspect", return_value=RepositoryEvidence("repo", "main", "a" * 40, True, True)):
            with self.assertRaisesRegex(RunnerError, "resolves to main"):
                client.cleanup_transaction(Path("/repository"), ("main",))

    @patch("engineering_platform.execution_host.subprocess.run")
    def test_live_status_and_status_command_cover_missing_invalid_and_valid_files(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(("git",), 0, "main\n", "")
        state = TransactionState(
            run_id="run-status",
            repository="pcvantol/djconnect",
            prompt_path="/missing-prompt.md",
            phase="INITIALIZE",
        )
        module = __import__("engineering_platform.execution_host", fromlist=["print_live_status"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(module.print_live_status(root), 1)
            path = module.write_live_status(root, state, "starting")
            self.assertTrue(path.is_file())
            self.assertEqual(module.print_live_status(root), 0)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(module.print_live_status(root), 0)

    def test_engineering_memory_is_bounded_advisory_metadata(self) -> None:
        module = __import__("engineering_platform.execution_host", fromlist=["capture_engineering_memory"])
        state = TransactionState(
            run_id="run-memory",
            repository="pcvantol/djconnect",
            prompt_path="/tmp/documentation-validation.md",
            phase="COMPLETE",
            terminal=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(module.load_engineering_memory(root), {})
            self.assertIn("[]", module.retrieve_engineering_memory(root, Path("documentation.md")))
            module.capture_engineering_memory(
                root,
                state,
                ({"reviewer": "validation", "accepted_recommendations": 1, "failed": False},),
            )
            memory = module.load_engineering_memory(root)
            self.assertEqual(memory["transactions"][-1]["outcome"], "COMPLETE")
            self.assertIn("validation", memory["reviewers"][0]["reviewer"])
            self.assertIn("documentation", module.retrieve_engineering_memory(root, Path("documentation-next.md")))

    def test_cli_helpers_are_bounded(self) -> None:
        module = __import__("engineering_platform.execution_host", fromlist=["_codex_final_message"])
        usage = extract_codex_usage(
            '{"usage":[{"input-tokens":12},{"nested":{"output_tokens":3}}]}\nnot-json'
        )
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(module._codex_final_message("plain final message"), "plain final message")

    def test_usage_and_execution_context_helpers_fail_closed(self) -> None:
        module = __import__("engineering_platform.execution_host", fromlist=["write_codex_usage"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module.write_codex_usage(root, "run-usage", {"unknown": 1, "input_tokens": -1})
            self.assertFalse((root / ".engineering/status/codex_usage.json").exists())
            module.write_codex_usage(root, "run-usage", {"input_tokens": 2})
            self.assertEqual(
                json.loads((root / ".engineering/status/codex_usage.json").read_text(encoding="utf-8"))["usage"],
                {"input_tokens": 2},
            )
            (root / ".engineering/status/codex_usage.json").write_text("not-json", encoding="utf-8")
            module.write_codex_usage(root, "run-usage", {"output_tokens": 3})
            self.assertEqual(
                json.loads((root / ".engineering/status/codex_usage.json").read_text(encoding="utf-8"))["usage"],
                {"output_tokens": 3},
            )
            module.write_codex_usage(root, "run-usage", {"input_tokens": 2})
            self.assertEqual(
                json.loads((root / ".engineering/status/codex_usage.json").read_text(encoding="utf-8"))["usage"],
                {"input_tokens": 2},
            )
        self.assertEqual(execution_mode_for("Execution Mode: Genesis"), "GENESIS")
        self.assertEqual(execution_mode_for("no declaration"), "MANAGED")
        self.assertIsNotNone(genesis_workspace_preflight(None))


_INHERITED_RUNNER_ENVIRONMENT = (
    "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA",
    "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT",
    "DJCONNECT_ENGINEERING_VALIDATION_RUN_ID",
    "DJCONNECT_ENGINEERING_BACKGROUND_RUN_ID",
    "DJCONNECT_ENGINEERING_BACKGROUND_JOB_ID",
)


class LocalAgentRunnerTest(unittest.TestCase):
    def test_execution_host_exposes_the_generic_command_name(self) -> None:
        self.assertEqual(build_parser().prog, "engineering-execution-host")

    def test_execution_host_accepts_watcher_admitted_storage_schema(self) -> None:
        arguments = build_parser().parse_args(
            ["prompt.md", "--admitted-storage-schema", "18"]
        )
        self.assertEqual(arguments.admitted_storage_schema, 18)

    @patch("engineering_platform.execution_host.generate_terminal_report", return_value=None)
    @patch("engineering_platform.execution_host.EngineeringRunner")
    def test_main_propagates_watcher_schema_admission_to_child_processes(self, runner_type: object, _: object) -> None:
        state = TransactionState("run-admission", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True)
        runner_type.return_value.run.return_value = state
        runner_type.return_value.platform_manifest = None
        runner_type.return_value.console_detail = None
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), patch(
            "engineering_platform.execution_host.Path.cwd", return_value=Path(temporary)
        ):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            central = Path(temporary) / "engineering.db"
            central.touch()
            with sqlite3.connect(central) as connection:
                connection.execute("CREATE TABLE execution_phase_spans(phase_id TEXT,run_id TEXT,phase_name TEXT,phase_category TEXT,parent_phase_id TEXT,attempt INTEGER,ordinal INTEGER,started_at TEXT,completed_at TEXT,duration_ms INTEGER,outcome TEXT,metadata TEXT)")
            self.assertEqual(
                __import__("engineering_platform.execution_host", fromlist=["main"]).main(
                    [str(prompt), "--admitted-storage-schema", "18", "--central-database", str(central)]
                ),
                0,
            )
            self.assertEqual(os.environ["DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA"], "18")
            self.assertEqual(
                Path(os.environ["DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT"]).resolve(),
                Path(temporary).resolve(),
            )

    def setUp(self) -> None:
        # Required-control subprocesses inherit these runner-only values.  Unit
        # fixtures own fresh storage and must not accidentally enter a real
        # watcher-admitted or validation-child lifecycle.
        self.inherited_runner_environment = {
            key: os.environ.pop(key, None) for key in _INHERITED_RUNNER_ENVIRONMENT
        }
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("# bounded objective\n", encoding="utf-8")
        manifest = self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            '{"platform_version":"2.0.0","runner_version":"2.0.0","bootstrap_contract":"2026.12","checkpoint_format":1,"memory_format":2,"report_format":2,"minimum_codex_cli":"0.146.0","watcher_version":"2.0.0","inbox_protocol":1,"dashboard_version":"2.0.0","handoff_protocol":1,"status_model":1,"storage_schema":29}\n',
            encoding="utf-8",
        )
        self.store = StateStore(self.root / ".engineering" / "engineering-runs")
        self.provider_readiness = patch(
            "engineering_platform.execution_host.provider_readiness_failures", return_value=()
        )
        self.provider_readiness.start()

    def tearDown(self) -> None:
        self.provider_readiness.stop()
        self.temporary.cleanup()
        for key, value in self.inherited_runner_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_new_run_initializes_and_records_canonical_prompt(self) -> None:
        quality_evidence = ({"activity": "TEST_COVERAGE", "result": "Added focused regression coverage."},)
        agent = FakeAgent(AgentResult("COMPLETE", quality_evidence=quality_evidence))
        repository = FakeRepository()
        runner = EngineeringRunner(self.root, self.store, repository, FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="new-run")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertTrue(self.store.path_for("new-run").is_file())
        self.assertIn("Read BOOTSTRAP.md", agent.prompts[0])
        self.assertIn("# bounded objective", agent.prompts[0])
        self.assertIn("Execution Host has already synchronized `main`", agent.prompts[0])
        self.assertIn("host, workspace and capability preflights passed", agent.prompts[0])
        self.assertIn("do not rerun the development-host bootstrap", agent.prompts[0])
        self.assertIn(f"The only repository checkout for this transaction is `{self.root.resolve()}`", agent.prompts[0])
        self.assertIn("producer provenance only", agent.prompts[0])
        self.assertEqual(agent.roots, [self.root, self.root])
        self.assertIn("Mandatory autonomous refactor and quality-control stage", agent.prompts[1])
        self.assertIn("Assess test coverage for every changed behavior", agent.prompts[1])
        self.assertIn("Assess the applicable operator, contract, and implementation documentation", agent.prompts[1])
        self.assertIn("In quality_evidence, record only work actually performed", agent.prompts[1])
        self.assertEqual(state.quality_evidence, quality_evidence)
        self.assertEqual(repository.synchronize_calls, [self.root])

    def test_provider_recovery_preflight_rejects_every_ambiguous_restart_condition(self) -> None:
        repository = FakeRepository(branch="recovery")
        runner = EngineeringRunner(self.root, self.store, repository, FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("recovery-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", branch="recovery")
        with patch("engineering_platform.execution_host.dismissal_for_run", return_value=False), \
             patch("engineering_platform.execution_host.verify_worktree_recovery", return_value=True):
            self.assertEqual(runner._provider_recovery_preflight(state), "PRECHECK_FAILED")
            runner.active_lease = SimpleNamespace(run_id=state.run_id)
            self.assertIsNone(runner._provider_recovery_preflight(state))
            with patch.object(repository, "inspect", side_effect=RuntimeError("unavailable")):
                self.assertEqual(runner._provider_recovery_preflight(state), "PRECHECK_FAILED")
            repository.evidence = RepositoryEvidence("pcvantol/djconnect", "other", "a" * 40, True, True)
            self.assertEqual(runner._provider_recovery_preflight(state), "PRECHECK_FAILED")
            repository.evidence = RepositoryEvidence("pcvantol/djconnect", "recovery", "a" * 40, True, True)
            with patch("engineering_platform.execution_host.verify_worktree_recovery", return_value=False):
                self.assertEqual(runner._provider_recovery_preflight(state), "PRECHECK_FAILED")
            process = self.root / ".engineering" / "status" / "runner_process.json"
            process.parent.mkdir(parents=True)
            process.write_text(json.dumps({"run_id": state.run_id, "pid": 42}), encoding="utf-8")
            with patch("engineering_platform.execution_host.os.kill"):
                self.assertEqual(runner._provider_recovery_preflight(state), "PRECHECK_FAILED")
            with patch("engineering_platform.execution_host.os.kill", side_effect=ProcessLookupError):
                self.assertIsNone(runner._provider_recovery_preflight(state))
            self.assertFalse(process.exists())
            process.write_text("{", encoding="utf-8")
            self.assertIsNone(runner._provider_recovery_preflight(state))
        with patch("engineering_platform.execution_host.dismissal_for_run", return_value=True):
            self.assertEqual(runner._provider_recovery_preflight(state), "CANCELLED")
        terminal = TransactionState("terminal-recovery", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        self.assertEqual(runner._provider_recovery_preflight(terminal), "CANCELLED")

    def test_optional_phase_telemetry_and_agent_timing_never_change_run_authority(self) -> None:
        with patch("engineering_platform.execution_host._complete_phase") as complete:
            execution_host.complete_phase(self.root, None)
            complete.assert_not_called()
        with patch("engineering_platform.execution_host._complete_phase", side_effect=execution_host.EngineeringStorageError("offline")):
            execution_host.complete_phase(self.root, SimpleNamespace())
        agent = FakeAgent(AgentResult("WAITING"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = TransactionState("timing-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        for measured in (True, "unknown", -1, 86_401):
            agent.last_execution_seconds = measured
            self.assertEqual(runner._record_agent_execution_time(state), state)
        agent.last_execution_seconds = 1.2345
        self.assertEqual(runner._record_agent_execution_time(state).agent_execution_seconds, 1.234)

    def test_optional_phase_wrappers_degrade_only_telemetry_storage_failures(self) -> None:
        with patch("engineering_platform.execution_host._start_phase", return_value=SimpleNamespace()) as start:
            self.assertIsNotNone(execution_host.start_phase(self.root, "phase-run", "VALIDATION"))
            start.assert_called_once()
        with patch("engineering_platform.execution_host._start_or_resume_phase", side_effect=execution_host.EngineeringStorageError("offline")):
            self.assertIsNone(execution_host.start_or_resume_phase(self.root, "phase-run", "VALIDATION"))
        with patch("engineering_platform.execution_host._complete_active_phase", side_effect=execution_host.EngineeringStorageError("offline")):
            self.assertFalse(execution_host.complete_active_phase(self.root, "phase-run", "VALIDATION"))

    def test_repair_plans_and_environmental_validation_require_explicit_durable_evidence(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("repair-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", repair_iterations=1)
        self.assertIsNone(runner._repair_plan(state))
        stale = state.__class__(**{**state.__dict__, "repair_audit": ({"iteration": "0", "outcome": "planned"},)})
        self.assertIsNone(runner._repair_plan(stale))
        planned = state.__class__(**{**state.__dict__, "repair_audit": ({"iteration": "1", "outcome": "planned", "failed_checks": "suite", "proposed_action": "repair"},)})
        self.assertEqual(runner._repair_plan(planned)["failed_checks"], "suite")
        self.assertFalse(runner._is_environmental_validation_instability(AgentResult("FAILED")))
        self.assertFalse(runner._is_environmental_validation_instability(AgentResult("FAILED", validation_disposition="environmental_instability", validation_evidence=({"result": "passed"},))))
        self.assertTrue(runner._is_environmental_validation_instability(AgentResult("FAILED", validation_disposition="environmental_instability", validation_evidence=({"result": "passed once; timed out once"},))))

    def test_validation_failure_and_commit_evidence_helpers_refuse_unverified_inputs(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        self.assertFalse(runner._has_failed_validation_evidence(AgentResult("FAILED")))
        self.assertFalse(runner._has_failed_validation_evidence(AgentResult("FAILED", validation_evidence=("not-a-record",))))
        self.assertTrue(runner._has_failed_validation_evidence(AgentResult("FAILED", validation_evidence=({"result": "timed out"},))))
        self.assertEqual(runner._append_verified_commit_evidence(TransactionState("commit-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT"), phase="EXECUTE_AGENT", commit_sha="not-a-sha", description="bad" ).commit_evidence, ())
        self.assertEqual(runner._validation_kind("python -m unittest discover"), "tests")
        self.assertEqual(runner._validation_kind("echo harmless"), None)

    def test_validation_classification_and_verified_commit_records_are_bounded(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        classifications = {
            "markdown-link-check": "documentation_contract",
            "ruff check": "static_analysis",
            "semgrep scan": "security",
            "git diff --check": "format_or_diff",
            "playwright test": "browser_e2e",
        }
        for command, expected in classifications.items():
            self.assertEqual(runner._validation_kind(command), expected)
        self.assertEqual(runner._validation_id("playwright test", "browser_e2e"), "validation_browser_e2e")
        self.assertEqual(runner._validation_id("npm run test:engineering-dashboard", "browser_e2e"), "dashboard_browser")
        state = TransactionState("verified-commit", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        sha = "a" * 40
        recorded = runner._append_verified_commit_evidence(state, phase="EXECUTE_AGENT", commit_sha=sha, description="implementation_agent_commit_verified")
        self.assertEqual(len(recorded.commit_evidence), 1)
        self.assertEqual(runner._append_verified_commit_evidence(recorded, phase="EXECUTE_AGENT", commit_sha=sha, description="implementation_agent_commit_verified"), recorded)
        audit = runner._audit_record(iteration=1, failed_checks="suite", proposed_action="repair", result=None, outcome="planned", empty_summary="none")
        self.assertEqual(audit["agent_summary"], "none")

    def test_provider_readiness_blocks_and_restores_the_original_action_without_provider_work(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("readiness-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", next_action="invoke_agent")
        with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=("CODEX", "GITHUB")):
            blocked = runner._provider_readiness_gate(state, require_codex=True, require_github=True)
        self.assertEqual(blocked.next_action, "provider_auth_repair_required")
        self.assertEqual(blocked.auth_recovery_providers, ("CODEX", "GITHUB"))
        self.assertEqual(self.store.load(state.run_id).next_action, "provider_auth_repair_required")
        with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=()):
            restored = runner._provider_readiness_gate(blocked, require_codex=True, require_github=True)
        self.assertEqual(restored.next_action, "invoke_agent")
        self.assertIsNone(restored.auth_recovery_phase)

    def test_deterministic_admission_reuses_pass_and_fails_closed_without_watcher_evidence(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        admitted = TransactionState("admission-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", admission_decision="PASS", admission_completed_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(runner._confirm_deterministic_admission(admitted), (admitted, None))
        pending = TransactionState("watcher-admission", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        environment = {
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA": "40",
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT": str(self.root),
        }
        with patch.dict(os.environ, environment, clear=False), \
             patch("engineering_platform.execution_host.load_admission_decision", side_effect=execution_host.EngineeringStorageError("offline")):
            blocked, reason = runner._confirm_deterministic_admission(pending)
        self.assertEqual(blocked.admission_decision, "BLOCKED")
        self.assertIn("not a persisted PASS", str(reason))
        watcher_pass = {"run_id": pending.run_id, "submission_id": "submission-1", "decision": "PASS", "execution_mode": "MANAGED"}
        with patch.dict(os.environ, environment, clear=False), \
             patch("engineering_platform.execution_host.load_admission_decision", return_value=watcher_pass):
            accepted, reason = runner._confirm_deterministic_admission(pending)
        self.assertIsNone(reason)
        self.assertEqual(accepted.admission_evidence_source, "WATCHER")

    def test_host_auxiliary_evidence_paths_preserve_lifecycle_authority(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("auxiliary-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        with patch.object(self.store, "save") as save:
            runner._project_durable_recovery(state, {"state": "UNKNOWN"})
            save.assert_not_called()
            runner._project_durable_recovery(state, {"state": "RECOVERED", "triggering_invocation_id": "old", "replacement_invocation_id": "new"})
            self.assertEqual(save.call_args.args[0].provider_recovery_attempts[0]["result"], "RECOVERED")
        runner._dispatch_guard_enforced = True
        with self.assertRaisesRegex(RunnerError, "deterministic admission"):
            runner._require_provider_dispatch_admission(state)
        runner._dispatch_guard_enforced = False
        runner._require_provider_dispatch_admission(state)
        with patch("engineering_platform.execution_host.write_codex_usage") as usage:
            runner.agent.last_usage = "invalid"
            runner._persist_agent_usage(state.run_id)
            usage.assert_not_called()
            runner.agent.last_usage = {"input_tokens": 3}
            runner._persist_agent_usage(state.run_id)
            usage.assert_called_once()
        self.assertEqual(runner._validation_summary_status("not applicable"), "NOT_APPLICABLE")
        self.assertEqual(runner._validation_summary_status("not recorded"), "UNAVAILABLE")
        self.assertEqual(runner._validation_summary_status("ERROR: failure"), "FAIL")
        self.assertEqual(runner._validation_summary_status("passed with no whitespace errors"), "PASS")
        self.assertEqual(runner._validation_summary_status("ambiguous"), "UNAVAILABLE")

    def test_reviewer_progress_ignores_unknown_events_and_projects_only_safe_counts(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("review-progress", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        selection = ReviewerSelection("validation", "bounded", 1.0)
        runner.reviewer_runtime = [{"reviewer": "validation", "status": "queued"}]
        with patch("engineering_platform.execution_host.write_live_status") as status:
            runner._publish_reviewer_progress(state, selection, "unknown")
            status.assert_not_called()
            runner._publish_reviewer_progress(state, selection, "started")
            runner._publish_reviewer_progress(state, selection, "completed", ReviewerResult("validation", "done", churn={"tool_loop_operations": True}))
        projected = runner.reviewer_runtime[0]
        self.assertEqual(projected["status"], "completed")
        self.assertEqual(projected["codex_commands_executed"], 0)

    def test_heartbeat_and_required_validation_profile_fail_closed_before_provider_work(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        runner.lease_heartbeat = SimpleNamespace(error=OSError("lost"), lease=None)
        with self.assertRaisesRegex(RunnerError, "heartbeat was lost"):
            runner._heartbeat()
        runner.lease_heartbeat = SimpleNamespace(error=None, lease=None)
        runner.active_lease = SimpleNamespace(run_id="lease-run")
        replacement = SimpleNamespace(run_id="lease-run")
        with patch("engineering_platform.execution_host.heartbeat_lease", return_value=replacement):
            runner._heartbeat()
        self.assertIs(runner.active_lease, replacement)
        self.assertIs(runner.lease_heartbeat.lease, replacement)
        state = TransactionState("profile-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        with patch.object(runner, "_save_terminal", side_effect=lambda *_args: _args[0]) as terminal:
            with patch("engineering_platform.execution_host.load_validation_context", return_value=None):
                self.assertIs(runner._execute_required_validation_controls(state), state)
            with patch("engineering_platform.execution_host.load_validation_context", return_value={"required_validation_controls": (), "control_bindings": ()}):
                runner._execute_required_validation_controls(state)
            with patch("engineering_platform.execution_host.load_validation_context", return_value={"required_validation_controls": ("suite",), "control_bindings": ()}):
                runner._execute_required_validation_controls(state)
            with patch("engineering_platform.execution_host.load_validation_context", return_value={"required_validation_controls": ("suite",), "control_bindings": ({"validation_id": "other", "command": ["check"]},)}):
                runner._execute_required_validation_controls(state)
            invalid_command = {"validation_id": "suite", "category": "test", "control_identity": "check", "command": "not-a-command-list"}
            with patch("engineering_platform.execution_host.load_validation_context", return_value={"required_validation_controls": ("suite",), "control_bindings": (invalid_command,)}), \
                 patch.object(runner, "_managed_action"):
                runner._execute_required_validation_controls(state)
        self.assertEqual(terminal.call_count, 5)
        unavailable_control = {"validation_id": "suite", "category": "test", "control_identity": "unavailable", "command": []}
        with patch("engineering_platform.execution_host.load_validation_context", return_value={"required_validation_controls": ("suite",), "control_bindings": (unavailable_control,)}), \
             patch.object(runner.store, "save"), patch.object(runner, "_managed_action"), \
             patch("engineering_platform.execution_host.write_live_status"), \
             patch("engineering_platform.execution_host.record_validation_control_result") as recorded:
            result = runner._execute_required_validation_controls(state)
        self.assertEqual(result.phase, "LOCAL_REPOSITORY_VALIDATION")
        self.assertEqual(recorded.call_args.kwargs["execution_status"], "NOT_EXECUTED")
        self.assertEqual(recorded.call_args.kwargs["result"], "UNAVAILABLE")

    def test_reported_provider_commits_require_matching_clean_repository_evidence(self) -> None:
        sha = "b" * 40
        repository = FakeRepository(branch="feature/verified")
        repository.evidence = RepositoryEvidence("pcvantol/djconnect", "feature/verified", sha, True, True)
        runner = EngineeringRunner(self.root, self.store, repository, FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        state = TransactionState("reported-commit", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", branch="feature/verified")
        result = AgentResult("COMPLETE", branch="feature/verified", commit_sha=sha)
        verified = runner._record_verified_result_commit(state, result, phase="EXECUTE_AGENT", description="implementation_agent_commit_verified")
        self.assertEqual(verified.commit_evidence[0]["commit_sha"], sha)
        repository.evidence = RepositoryEvidence("pcvantol/djconnect", "feature/verified", sha, False, True)
        self.assertEqual(runner._record_verified_result_commit(state, result, phase="EXECUTE_AGENT", description="implementation_agent_commit_verified"), state)

    def test_watcher_missing_admission_blocks_before_reviewer_or_agent_dispatch(self) -> None:
        agent = ReviewCapableFakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        with patch.dict(os.environ, {
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA": str(ENGINEERING_STORAGE_SCHEMA_VERSION),
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT": str(self.root),
        }, clear=False), patch(
            "engineering_platform.execution_host.load_admission_decision", return_value=None
        ):
            state = runner.run(self.prompt, run_id="missing-watcher-admission")

        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.next_action, "deterministic_admission")
        self.assertEqual(state.admission_decision, "BLOCKED")
        self.assertEqual(agent.prompts, [])
        self.assertEqual(agent.reviewer_evidence, [])
        self.assertFalse(any(span["phase_name"] == "PROVIDER_EXECUTION" for span in phase_spans(self.root, state.run_id)))

    def test_watcher_pass_admission_precedes_reviewer_dispatch_and_is_checkpointed(self) -> None:
        agent = ReviewCapableFakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        admission = {
            "run_id": "passed-watcher-admission",
            "submission_id": "submission-1",
            "execution_mode": "MANAGED",
            "decision": "PASS",
            "failed_gate_ids": [],
            "gates": [],
            "observed_at": "2026-08-29T07:00:00+00:00",
        }
        with patch.dict(os.environ, {
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA": str(ENGINEERING_STORAGE_SCHEMA_VERSION),
            "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT": str(self.root),
        }, clear=False), patch(
            "engineering_platform.execution_host.load_admission_decision", return_value=admission
        ):
            state = runner.run(self.prompt, run_id="passed-watcher-admission")

        self.assertEqual(state.admission_decision, "PASS")
        self.assertEqual(state.admission_evidence_source, "WATCHER")
        self.assertTrue(state.admission_completed_at)
        self.assertTrue(agent.reviewer_evidence)
        phase_names = [span["phase_name"] for span in phase_spans(self.root, state.run_id)]
        self.assertLess(phase_names.index("DETERMINISTIC_ADMISSION"), phase_names.index("CAPABILITY_REVIEW"))

    def test_all_non_pass_admission_states_refuse_direct_provider_invocation(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        runner._dispatch_guard_enforced = True
        for decision in ("NOT_STARTED", "FAIL", "BLOCKED", "UNAVAILABLE"):
            with self.subTest(decision=decision):
                state = TransactionState(
                    f"admission-{decision.casefold()}", "pcvantol/djconnect", str(self.prompt),
                    "EXECUTE_AGENT", admission_decision=decision,
                )
                with self.assertRaisesRegex(RunnerError, "deterministic admission"):
                    runner._invoke_agent_with_timing(state, "objective")
        self.assertEqual(agent.prompts, [])

    def test_reviewer_recommendations_do_not_enter_the_primary_prompt(self) -> None:
        self.prompt.write_text("# validation regression objective\n", encoding="utf-8")
        agent = ReviewCapableFakeAgent(AgentResult("COMPLETE"))

        EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None
        ).run(self.prompt, run_id="reviewer-isolation-run")

        self.assertTrue(agent.reviewer_evidence)
        for evidence in agent.reviewer_evidence:
            self.assertIsInstance(evidence, ReviewerEvidence)
            self.assertEqual(evidence.run_id, "reviewer-isolation-run")
            self.assertEqual(evidence.head_sha, "a" * 40)
        primary_prompt = agent.prompts[0]
        self.assertIn('"run_id": "reviewer-isolation-run"', primary_prompt)
        self.assertIn('"head_sha": "' + "a" * 40 + '"', primary_prompt)
        self.assertNotIn("DISTINCTIVE_REVIEWER_RECOMMENDATION", primary_prompt)

    def test_managed_run_keeps_a_producer_target_from_overriding_host_checkout(self) -> None:
        producer_checkout = self.root / "producer-checkout"
        self.prompt.write_text(
            f"# bounded objective\n\nTarget repository:\n\n{producer_checkout}\n",
            encoding="utf-8",
        )
        agent = FakeAgent(AgentResult("COMPLETE"))

        EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None
        ).run(self.prompt, run_id="managed-target-boundary")

        self.assertEqual(agent.roots, [self.root, self.root])
        self.assertIn(
            f"The only repository checkout for this transaction is `{self.root.resolve()}`",
            agent.prompts[0],
        )
        self.assertIn(
            "A `Target repository` value within the supplied objective is producer provenance only",
            agent.prompts[0],
        )

    def test_runtime_validation_commands_are_timed_at_direct_boundaries(self) -> None:
        agent = CommandTimingFakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None
        )
        state = TransactionState("validation-boundary-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        runner._invoke_agent_with_timing(state, "objective")
        spans = phase_spans(self.root, state.run_id)
        validation = next(span for span in spans if span["phase_name"] == "VALIDATION")
        self.assertEqual(validation["outcome"], "COMPLETE")
        self.assertEqual(validation["metadata"], {
            "validation_kind": "tests",
            "validation_id": "validation_tests",
            "command_id": "command-1",
        })
        self.assertIsNotNone(validation["parent_phase_id"])

    def test_browser_validation_invocation_persists_its_canonical_identity_at_start(self) -> None:
        class BrowserCommandAgent(CommandTimingFakeAgent):
            def invoke(self, root: Path, prompt: str) -> AgentResult:
                if callable(self.command_callback):
                    self.command_callback("started", "dashboard-command", "npm run test:engineering-dashboard")
                    self.command_callback("completed", "dashboard-command", "")
                return FakeAgent.invoke(self, root, prompt)

        agent = BrowserCommandAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = TransactionState("dashboard-boundary-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        runner._invoke_agent_with_timing(state, "objective")

        validation = next(span for span in phase_spans(self.root, state.run_id) if span["phase_name"] == "VALIDATION")
        self.assertEqual(validation["metadata"], {
            "validation_kind": "browser_e2e",
            "validation_id": "dashboard_browser",
            "command_id": "dashboard-command",
        })
        self.assertEqual(validation["outcome"], "COMPLETE")

    def test_dashboard_browser_identity_requires_the_structural_npm_launcher(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        cases = (
            ("npm run test:engineering-dashboard", "dashboard_browser"),
            ("npm run test:engineering-dashboard -- --reporter=dot", "dashboard_browser"),
            ("/bin/zsh -lc 'npm run test:engineering-dashboard'", "dashboard_browser"),
            ("python3 -m unittest tests.engineering.test_dashboard_browser_validation", "validation_tests"),
            ("pgrep -f 'playwright dashboard_browser_validation'", "validation_browser_e2e"),
            ("echo npm run test:engineering-dashboard", "validation_browser_e2e"),
            ("npm run test:engineering-dashboard && pgrep -f playwright", "validation_browser_e2e"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                kind = runner._validation_kind(command)
                self.assertIsNotNone(kind)
                self.assertEqual(runner._validation_id(command, kind), expected)

    def test_browser_command_terminal_evidence_preserves_invocation_on_pass_and_failure(self) -> None:
        class BrowserCommandAgent(CommandTimingFakeAgent):
            def __init__(self, exit_code: int | None) -> None:
                super().__init__(AgentResult("COMPLETE"))
                self.exit_code = exit_code
            def invoke(self, root: Path, prompt: str) -> AgentResult:
                if callable(self.command_callback):
                    self.command_callback("started", "dashboard-command", "npm run test:engineering-dashboard")
                    self.command_callback("completed", "dashboard-command", "", self.exit_code)
                return FakeAgent.invoke(self, root, prompt)

        for label, exit_code, expected in (("pass", 0, "PASS"), ("fail", 23, "FAIL"), ("unavailable", None, "UNAVAILABLE")):
            with self.subTest(label=label):
                run_id = f"dashboard-terminal-{label}"
                record_validation_profile(self.root, run_id=run_id, selected_validation_tier="DASHBOARD", validation_profile_version="1.0", required_validation_controls=("dashboard_browser",), recorded_at="2026-08-29T00:00:00+00:00")
                runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), BrowserCommandAgent(exit_code), lambda _: None)
                runner._invoke_agent_with_timing(TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT"), "objective")
                control = load_validation_context(self.root, run_id)["controls"]["dashboard_browser"]
                self.assertEqual(control["execution_status"], "EXECUTED")
                self.assertEqual(control["result"], expected)
                self.assertEqual(control["exit_code"], exit_code)
                self.assertEqual(control["control_identity"], "npm run test:engineering-dashboard")

    def test_dashboard_control_is_not_overwritten_by_browser_process_inspection(self) -> None:
        class CanonicalThenInspectionAgent(CommandTimingFakeAgent):
            def invoke(self, root: Path, prompt: str) -> AgentResult:
                if callable(self.command_callback):
                    self.command_callback("started", "dashboard", "npm run test:engineering-dashboard")
                    self.command_callback("completed", "dashboard", "", 0)
                    self.command_callback("started", "inspection", "ps -axo command | rg 'playwright test dashboard.spec'")
                    self.command_callback("completed", "inspection", "", 0)
                return FakeAgent.invoke(self, root, prompt)

        run_id = "dashboard-canonical-lineage"
        record_validation_profile(self.root, run_id=run_id, selected_validation_tier="DASHBOARD", validation_profile_version="1.0", required_validation_controls=("dashboard_browser",), recorded_at="2026-08-29T00:00:00+00:00")
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), CanonicalThenInspectionAgent(AgentResult("COMPLETE")), lambda _: None)
        runner._invoke_agent_with_timing(TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT"), "objective")
        controls = load_validation_context(self.root, run_id)["controls"]
        self.assertEqual(controls["dashboard_browser"]["result"], "PASS")
        self.assertEqual(controls["dashboard_browser"]["exit_code"], 0)
        self.assertEqual(controls["dashboard_browser"]["control_identity"], "npm run test:engineering-dashboard")
        self.assertIn("validation_browser_e2e", controls)

    def test_browser_validation_result_uses_the_canonical_dashboard_control(self) -> None:
        state = TransactionState("dashboard-control-run", "pcvantol/djconnect", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION")
        record_validation_profile(
            self.root, run_id=state.run_id, selected_validation_tier="DASHBOARD",
            validation_profile_version="1.0", required_validation_controls=("dashboard_browser",),
            recorded_at="2026-08-28T00:00:00+00:00",
        )
        result = AgentResult(
            "WAITING",
            validation_evidence=({"command": "npm run test:engineering-dashboard", "result": "completed; detailed counts unavailable"},),
        )
        EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(result), lambda _: None)._record_validation_evidence(state, result)

        context = load_validation_context(self.root, state.run_id)
        self.assertIsNotNone(context)
        self.assertEqual(context["controls"]["dashboard_browser"], {
            "validation_id": "dashboard_browser",
            "category": "agent",
            "control_identity": "npm run test:engineering-dashboard",
            "required_for_profile": True,
            "execution_status": "EXECUTED",
            "result": "UNAVAILABLE",
            "evidence_ref": "agent_result",
            "observed_at": context["controls"]["dashboard_browser"]["observed_at"],
            "currentness": 0,
        })

    def test_validation_evidence_keeps_no_errors_summaries_passing(self) -> None:
        summaries = (
            "Passed; committed documentation diff has no whitespace errors.",
            "No errors detected.",
            "Validation passed with no errors.",
            "git diff --check passed; no whitespace errors.",
        )
        for index, summary in enumerate(summaries, start=1):
            with self.subTest(summary=summary):
                state = TransactionState(
                    f"validation-no-errors-{index}", "pcvantol/djconnect", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
                )
                record_validation_profile(
                    self.root, run_id=state.run_id, selected_validation_tier="DOCUMENTATION",
                    validation_profile_version="1.0", required_validation_controls=("git_diff_check",),
                    recorded_at="2026-08-30T00:00:00+00:00",
                )
                result = AgentResult("COMPLETE", validation_evidence=({
                    "command": "git diff --check", "result": summary,
                },))
                EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(result), lambda _: None)._record_validation_evidence(state, result)
                self.assertEqual(load_validation_context(self.root, state.run_id)["controls"]["git_diff_check"]["result"], "PASS")

    def test_validation_evidence_keeps_explicit_failures_fail_closed(self) -> None:
        summaries = (
            "ERROR: git diff --check failed",
            "Validation failed",
            "Whitespace error detected",
        )
        for index, summary in enumerate(summaries, start=1):
            with self.subTest(summary=summary):
                state = TransactionState(
                    f"validation-real-failure-{index}", "pcvantol/djconnect", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
                )
                record_validation_profile(
                    self.root, run_id=state.run_id, selected_validation_tier="DOCUMENTATION",
                    validation_profile_version="1.0", required_validation_controls=("git_diff_check",),
                    recorded_at="2026-08-30T00:00:00+00:00",
                )
                result = AgentResult("COMPLETE", validation_evidence=({
                    "command": "git diff --check", "result": summary,
                },))
                EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(result), lambda _: None)._record_validation_evidence(state, result)
                self.assertEqual(load_validation_context(self.root, state.run_id)["controls"]["git_diff_check"]["result"], "FAIL")

    def test_combined_documentation_validation_no_errors_does_not_create_false_failure(self) -> None:
        state = TransactionState(
            "documentation-no-errors", "pcvantol/djconnect", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
        )
        record_validation_profile(
            self.root, run_id=state.run_id, selected_validation_tier="DOCUMENTATION",
            validation_profile_version="1.0", required_validation_controls=("documentation_contract",),
            recorded_at="2026-08-30T00:00:00+00:00",
        )
        result = AgentResult("COMPLETE", validation_evidence=({
            "command": "git diff --check && python3 -m unittest tests.engineering.test_engineering_operational_documentation",
            "result": "Passed: no whitespace errors; 14 tests passed.",
        },))
        EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(result), lambda _: None)._record_validation_evidence(state, result)
        context = load_validation_context(self.root, state.run_id)
        self.assertEqual(context["controls"]["documentation_contract"]["result"], "PASS")

    def test_validation_only_controls_are_pending_until_their_execution_stage(self) -> None:
        run_id = "validation-only-pending"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD",
            validation_profile_version="1.0", required_validation_controls=("dashboard_browser",),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        context = load_validation_context(self.root, run_id)
        self.assertEqual(context["required_validation_controls"], ("dashboard_browser",))
        self.assertEqual(context["controls"], {})

    def test_validation_only_binds_the_structured_profile_before_control_execution(self) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        state = runner._bind_validation_only_profile(
            TransactionState("bound-validation-only", "pcvantol/djconnect", str(self.prompt), "CAPABILITY_REVIEW", action_intent="VALIDATION_ONLY"),
            {"validation_profile": {
                "tier": "DASHBOARD", "version": "1.0",
                "required_controls": [
                    "git_diff_check", "engineering_python", "console_route_ownership",
                    "ui_localization", "dashboard_browser",
                ],
            }},
        )
        self.assertFalse(state.terminal)
        context = load_validation_context(self.root, state.run_id)
        self.assertEqual(context["profile_reference"], "validation-profile-registry:DASHBOARD@1.0")
        self.assertEqual(context["profile_selection_source"], "producer_execution_context")
        self.assertEqual(
            context["required_validation_controls"],
            ("git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"),
        )
        self.assertEqual(context["control_bindings"][-1]["command"], ["npm", "run", "test:engineering-dashboard"])
        self.assertEqual(context["controls"], {})
        # The immutable run record prevents a later selection from rewriting
        # the controls that the executor and qualification will observe.
        record_validation_profile(
            self.root, run_id=state.run_id, selected_validation_tier="FULL", validation_profile_version="1.0",
            required_validation_controls=("repository_suite",), recorded_at="2026-08-29T00:01:00+00:00",
        )
        self.assertEqual(load_validation_context(self.root, state.run_id)["selected_validation_tier"], "DASHBOARD")

    def test_missing_validation_only_profile_blocks_without_control_or_provider_work(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner._bind_validation_only_profile(
            TransactionState("missing-validation-profile", "pcvantol/djconnect", str(self.prompt), "CAPABILITY_REVIEW", action_intent="VALIDATION_ONLY"),
            {"context_version": "1.0", "action_intent": "VALIDATION_ONLY"},
        )
        self.assertTrue(state.terminal)
        self.assertEqual(state.next_action, "validation_profile_resolution")
        self.assertIsNone(load_validation_context(self.root, state.run_id))
        self.assertEqual(agent.prompts, [])

    @patch.object(EngineeringRunner, "_run_required_validation_command")
    def test_validation_only_executor_uses_the_persisted_control_binding(self, run: object) -> None:
        run.return_value = 0  # type: ignore[attr-defined]
        run_id = "persisted-binding-execution"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD", validation_profile_version="1.0",
            required_validation_controls=("dashboard_browser",),
            profile_reference="fixture:dashboard", profile_selection_source="fixture",
            control_bindings=({"validation_id": "dashboard_browser", "required": True, "category": "browser", "control_identity": "fixture launcher", "command": ["fixture", "dashboard"]},),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        runner._execute_required_validation_controls(TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY"))
        run.assert_called_once_with(("fixture", "dashboard"))  # type: ignore[attr-defined]

    @patch.object(EngineeringRunner, "_run_required_validation_command")
    def test_validation_only_executes_required_control_before_qualification(self, run: object) -> None:
        run.return_value = 0  # type: ignore[attr-defined]
        run_id = "validation-only-execution"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD",
            validation_profile_version="1.0", required_validation_controls=("dashboard_browser",),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner._execute_required_validation_controls(
            TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
        )
        context = load_validation_context(self.root, run_id)
        control = context["controls"]["dashboard_browser"]
        self.assertFalse(state.terminal)
        self.assertEqual(state.phase, "LOCAL_REPOSITORY_VALIDATION")
        self.assertEqual(control["execution_status"], "EXECUTED")
        self.assertEqual(control["result"], "PASS")
        self.assertEqual(control["control_identity"], "npm run test:engineering-dashboard")
        self.assertEqual(agent.roots, [])
        run.assert_called_once_with(("npm", "run", "test:engineering-dashboard"))  # type: ignore[attr-defined]

    @patch.object(EngineeringRunner, "_run_required_validation_command")
    def test_validation_only_executor_persists_every_required_control_result(self, run: object) -> None:
        run.side_effect = (0, 7, None)  # type: ignore[attr-defined]
        run_id = "all-required-control-results"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD",
            validation_profile_version="1.0",
            required_validation_controls=("git_diff_check", "engineering_python", "dashboard_browser"),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        runner._execute_required_validation_controls(
            TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
        )
        context = load_validation_context(self.root, run_id)
        self.assertEqual(
            tuple(context["controls"][control]["result"] for control in context["required_validation_controls"]),
            ("PASS", "FAIL", "UNAVAILABLE"),
        )
        connection = open_storage(self.root)
        try:
            rows = connection.execute(
                "SELECT validation_id,execution_status,result,evidence_ref FROM execution_validation_control_results WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [
            ("git_diff_check", "EXECUTED", "PASS", "command_terminal"),
            ("engineering_python", "EXECUTED", "FAIL", "command_terminal"),
            ("dashboard_browser", "EXECUTED", "UNAVAILABLE", "command_terminal"),
        ])

    @patch.object(EngineeringRunner, "_run_required_validation_command", return_value=0)
    def test_validation_only_report_projects_each_persisted_required_control(self, _: object) -> None:
        run_id = "persisted-profile-report-projection"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD",
            validation_profile_version="1.0",
            required_validation_controls=("git_diff_check", "engineering_python", "dashboard_browser"),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        runner._execute_required_validation_controls(
            TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
        )
        body = generate_terminal_report(
            self.root, TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        ).read_text(encoding="utf-8")
        for validation_id in ("git_diff_check", "engineering_python", "dashboard_browser"):
            self.assertIn(f"Required control {validation_id}: `PASS` — `PERSISTED_PROFILE`", body)
            self.assertIn(f"Validation ID: `{validation_id}`", body)
        self.assertEqual(body.count("Execution inclusion: `AVAILABLE`."), 3)

    @patch.object(EngineeringRunner, "_run_required_validation_command")
    def test_validation_only_all_required_pass_is_pass_and_failure_states_remain_authoritative(self, run: object) -> None:
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        cases = (
            ("all-pass", ("git_diff_check", "engineering_python"), (0, 0), ("PASS", "PASS")),
            ("one-fail", ("git_diff_check", "engineering_python"), (0, 9), ("PASS", "FAIL")),
            ("unavailable", ("dashboard_browser",), (None,), ("UNAVAILABLE",)),
        )
        for run_id, controls, exits, expected in cases:
            with self.subTest(run_id=run_id):
                record_validation_profile(
                    self.root, run_id=run_id, selected_validation_tier="RUNTIME",
                    validation_profile_version="1.0", required_validation_controls=controls,
                    recorded_at="2026-08-29T00:00:00+00:00",
                )
                effects = list(exits)
                run.side_effect = effects  # type: ignore[attr-defined]
                state = runner._execute_required_validation_controls(
                    TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
                )
                context = load_validation_context(self.root, run_id)
                self.assertFalse(state.terminal)
                self.assertEqual(
                    tuple(context["controls"][control]["result"] for control in controls), expected,
                )

    def test_validation_only_unmapped_control_is_terminally_not_executed_and_non_pass(self) -> None:
        run_id = "validation-only-no-launcher"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="CUSTOM",
            validation_profile_version="1.0", required_validation_controls=("unmapped_control",),
            recorded_at="2026-08-29T00:00:00+00:00",
        )
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        runner._execute_required_validation_controls(
            TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
        )
        control = load_validation_context(self.root, run_id)["controls"]["unmapped_control"]
        self.assertEqual(control["execution_status"], "NOT_EXECUTED")
        self.assertEqual(control["result"], "UNAVAILABLE")

    def test_managed_synchronization_blocks_before_agent_when_host_cannot_sync(self) -> None:
        repository = FakeRepository()
        repository.synchronize_error = RunnerError("fatal: Unable to create '.git/index.lock': Permission denied")
        agent = FakeAgent(AgentResult("COMPLETE"))

        state = EngineeringRunner(
            self.root, self.store, repository, FakeGitHub([]), agent, lambda _: None
        ).run(self.prompt, run_id="sync-blocked-run")

        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.next_action, "repository_synchronization")
        self.assertIn("Permission denied", state.diagnostic or "")
        self.assertEqual(repository.synchronize_calls, [self.root])
        self.assertEqual(agent.prompts, [])

    def test_unexpected_managed_branch_blocks_before_provider_dispatch(self) -> None:
        repository = FakeRepository(branch="codex/unexpected-feature")
        agent = ReviewCapableFakeAgent(AgentResult("COMPLETE"))

        state = EngineeringRunner(
            self.root, self.store, repository, FakeGitHub([]), agent, lambda _: None
        ).run(self.prompt, run_id="managed-branch-blocked")

        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.next_action, "managed_target_baseline")
        self.assertIn("expected clean main", state.diagnostic or "")
        self.assertEqual(repository.synchronize_calls, [self.root])
        self.assertEqual(agent.prompts, [])
        self.assertEqual(agent.reviewer_evidence, [])
        with open_storage(self.root) as connection:
            invocations = connection.execute(
                "SELECT COUNT(*) FROM provider_invocations WHERE run_id=?", (state.run_id,)
            ).fetchone()[0]
        self.assertEqual(invocations, 0)
        self.assertFalse(any(span["phase_name"] == "PROVIDER_EXECUTION" for span in phase_spans(self.root, state.run_id)))

    def test_execute_agent_phase_is_published_before_agent_invocation(self) -> None:
        agent = LiveStatusFakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        runner.run(self.prompt, run_id="live-phase-run")
        self.assertEqual(agent.live_phase, "QUALITY_CONTROL_AGENT")
        self.assertEqual(agent.live_action, "autonomous_refactor_and_quality_control")
        self.assertEqual(agent.activity_action, "Codex bewerkt bestanden")

    def test_autonomous_quality_control_cannot_replace_the_implementation_pr(self) -> None:
        agent = SequencedFakeAgent([
            AgentResult("COMPLETE", "codex/implementation", 701),
            AgentResult("COMPLETE", "codex/implementation", 702),
        ])
        state = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None
        ).run(self.prompt, run_id="quality-scope-run")

        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.next_action, "autonomous_quality_control_scope")

    def test_local_repository_validation_iterates_before_creating_the_implementation_pr(self) -> None:
        agent = SequencedFakeAgent([
            AgentResult(
                "WAITING", "codex/implementation", diagnostic="Canonical tests still fail.",
                validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},),
            ),
            AgentResult(
                "COMPLETE", "codex/implementation", 701, diagnostic="Canonical tests passed.",
                validation_evidence=({"command": "python -m unittest tests.engineering", "result": "passed"},),
            ),
        ])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = TransactionState(
            "local-validation-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            branch="codex/implementation", owner_authorized=True,
        )
        validated, result = runner._run_local_repository_validation(
            state, AgentResult("COMPLETE", "codex/implementation")
        )
        self.assertEqual(validated.phase, "LOCAL_REPOSITORY_VALIDATION")
        self.assertEqual(validated.local_validation_iterations, 2)
        self.assertEqual([item["outcome"] for item in validated.local_validation_audit], ["validation_failed", "validated"])
        self.assertEqual(validated.local_validation_audit[0]["proposed_action"], "FULL: full required repository suite")
        self.assertEqual(validated.validation_evidence, ({"command": "python -m unittest tests.engineering", "result": "passed"},))
        self.assertEqual(result.pull_request, 701)
        self.assertIn("iteration 1 of 3", agent.prompts[0])
        self.assertIn("Create one draft implementation pull request only after", agent.prompts[0])

    def test_verified_implementation_validation_failure_enters_local_repair_route(self) -> None:
        sha = "b" * 40
        initial = AgentResult(
            "FAILED",
            "codex/implementation",
            diagnostic="The broader local suite failed after the implementation commit.",
            commit_sha=sha,
            validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},),
        )
        repository = FakeRepository(branch="codex/implementation")
        repository.evidence = RepositoryEvidence("pcvantol/djconnect", "codex/implementation", sha, True)
        agent = SequencedFakeAgent([
            AgentResult("WAITING", "codex/implementation", diagnostic="Still failing.", validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},)),
            AgentResult("COMPLETE", "codex/implementation", 701, diagnostic="Passed.", validation_evidence=({"command": "python -m unittest tests.engineering", "result": "passed"},)),
        ])
        runner = EngineeringRunner(self.root, self.store, repository, FakeGitHub([]), agent, lambda _: None)
        state = TransactionState(
            "implementation-validation-failure", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            branch="codex/implementation", owner_authorized=True,
        )
        verified = runner._record_verified_result_commit(
            state, initial, phase="EXECUTE_AGENT", description="implementation_agent_commit_verified"
        )

        self.assertTrue(runner._is_recoverable_implementation_validation_failure(verified, initial))
        validated, result = runner._run_local_repository_validation(verified, initial)

        self.assertFalse(validated.terminal)
        self.assertEqual(validated.local_validation_iterations, 2)
        self.assertEqual([item["outcome"] for item in validated.local_validation_audit], ["validation_failed", "validated"])
        self.assertEqual(result.pull_request, 701)

    def test_runner_routes_verified_failed_implementation_to_local_validation_before_pr(self) -> None:
        sha = "d" * 40
        repository = FakeRepository()

        class CommitThenValidateAgent(SequencedFakeAgent):
            def invoke(self, root: Path, prompt: str) -> AgentResult:
                result = super().invoke(root, prompt)
                if len(self.prompts) == 1:
                    repository.evidence = RepositoryEvidence(
                        "pcvantol/djconnect", "codex/implementation", sha, True
                    )
                return result

        agent = CommitThenValidateAgent([
            AgentResult(
                "FAILED", "codex/implementation", commit_sha=sha,
                diagnostic="Broader local suite failed after the implementation commit.",
                validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},),
            ),
            AgentResult(
                "COMPLETE", "codex/implementation", 701,
                validation_evidence=({"command": "python -m unittest tests.engineering", "result": "passed"},),
            ),
            AgentResult("COMPLETE", "codex/implementation", 701),
        ])
        github = FakeGitHub([
            PullRequestEvidence(701, "OPEN", True, True, head_branch="codex/implementation", base_branch="main"),
        ])

        state = EngineeringRunner(self.root, self.store, repository, github, agent, lambda _: None).run(
            self.prompt, run_id="failed-implementation-routes-locally", owner_authorized=True
        )

        self.assertTrue(state.commit_evidence, state.diagnostic)
        self.assertEqual(state.phase, "WAIT_FOR_OPERATOR_MERGE", state.diagnostic)
        self.assertFalse(state.terminal)
        self.assertEqual(state.local_validation_iterations, 1)
        self.assertEqual(state.local_validation_audit[0]["outcome"], "validated")
        self.assertEqual(len(agent.prompts), 3)
        self.assertIn("Local repository validation gate — iteration 1 of 3", agent.prompts[1])

    def test_unverified_or_external_implementation_failure_never_starts_local_repair(self) -> None:
        sha = "c" * 40
        result = AgentResult(
            "FAILED", "codex/implementation", commit_sha=sha,
            terminal_condition="external_blocked",
            validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},),
        )
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(result), lambda _: None)
        state = TransactionState(
            "external-validation-failure", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            branch="codex/implementation", owner_authorized=True,
        )

        self.assertFalse(runner._is_recoverable_implementation_validation_failure(state, result))

    def test_failed_local_validation_uses_all_three_bounded_attempts(self) -> None:
        failures = [
            AgentResult(
                "FAILED", "codex/implementation", diagnostic="Required local suite failed.",
                validation_evidence=({"command": "python -m unittest tests.engineering", "result": "failed"},),
            )
            for _ in range(3)
        ]
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), SequencedFakeAgent(failures), lambda _: None
        )
        state = TransactionState(
            "local-validation-limit", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            branch="codex/implementation", owner_authorized=True,
        )

        blocked, _ = runner._run_local_repository_validation(
            state, AgentResult("COMPLETE", "codex/implementation")
        )

        self.assertTrue(blocked.terminal)
        self.assertEqual(blocked.next_action, "local_validation_attempt_limit_reached")
        self.assertEqual(blocked.local_validation_iterations, 3)
        self.assertEqual(len(blocked.local_validation_audit), 3)
        self.assertEqual({item["outcome"] for item in blocked.local_validation_audit}, {"validation_failed"})

    def test_local_repository_validation_separates_proven_environment_instability(self) -> None:
        agent = SequencedFakeAgent([
            AgentResult(
                "WAITING", "codex/implementation",
                diagnostic="The required browser suite timed out, but its isolated rerun passed without a code change.",
                validation_evidence=(
                    {"command": "npm run test:engineering-dashboard", "result": "failed: browser timeout"},
                    {"command": "focused browser rerun", "result": "passed without a code change"},
                ),
                validation_disposition="environmental_instability",
            ),
        ])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = TransactionState(
            "validation-instability-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            branch="codex/implementation", owner_authorized=True,
        )

        blocked, result = runner._run_local_repository_validation(
            state, AgentResult("COMPLETE", "codex/implementation")
        )

        self.assertEqual(result.pull_request, None)
        self.assertEqual(blocked.phase, "BLOCKED")
        self.assertEqual(blocked.next_action, "validation_infrastructure_recovery_required")
        self.assertEqual(blocked.local_validation_iterations, 1)
        self.assertIn("separate validation-infrastructure recovery item", blocked.diagnostic)

    def test_runtime_failure_replaces_a_stale_operator_merge_terminal_condition(self) -> None:
        class UsageLimitedAgent:
            def available(self) -> bool:
                return True

            def version(self) -> str:
                return "0.146.0"

            def invoke(self, _: Path, __: str) -> AgentResult:
                raise CodexInvocationError(
                    "Codex usage limit reached. Add Codex credits or resume after the account limit resets.",
                    "redacted provider detail",
                    next_action="resolve_codex_usage_limit",
                    terminal_condition="codex_usage_limit_reached",
                )

        stale = TransactionState(
            "usage-limit-run",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_OPERATOR_MERGE",
            next_action="await_operator_pr_merge",
            terminal_condition="operator_merge_required",
        )
        self.store.save(stale)
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), UsageLimitedAgent(), lambda _: None
        )
        result = runner.run(self.prompt, run_id=stale.run_id, resume=True)
        self.assertEqual(result.phase, "BLOCKED")
        self.assertTrue(result.terminal)
        self.assertEqual(result.next_action, "resolve_codex_usage_limit")
        self.assertEqual(result.terminal_condition, "codex_usage_limit_reached")
        self.assertNotEqual(result.terminal_condition, "operator_merge_required")

    def test_provider_interruption_terminalizes_without_a_follow_up_action(self) -> None:
        state = TransactionState(
            "interrupted-provider-run", "pcvantol/djconnect", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
            next_action="run_local_repository_validation",
        )
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None
        )
        error = CodexInvocationError(
            "Provider turn interrupted before returning the required structured terminal result.",
            "redacted provider detail",
            next_action="NONE",
            terminal_condition="provider_turn_interrupted",
            interruption_reason="interrupted",
        )
        terminal = runner._terminalize_provider_invocation_error(state, error)
        self.assertEqual(terminal.phase, "FAILED")
        self.assertTrue(terminal.terminal)
        self.assertEqual(terminal.next_action, "NONE")
        self.assertEqual(terminal.terminal_condition, "provider_turn_interrupted")
        self.assertEqual(self.store.load(state.run_id), terminal)

    def test_host_shutdown_during_provider_turn_persists_interruption_evidence(self) -> None:
        class InterruptedAgent(FakeAgent):
            def invoke(self, _: Path, __: str) -> AgentResult:
                raise KeyboardInterrupt

        state = TransactionState(
            "signal-interrupted-provider-run", "pcvantol/djconnect", str(self.prompt),
            "LOCAL_REPOSITORY_VALIDATION", next_action="run_local_repository_validation",
        )
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]),
            InterruptedAgent(AgentResult("COMPLETE")), lambda _: None,
        )

        with self.assertRaises(CodexInvocationError) as raised:
            runner._invoke_agent_with_timing(state, "objective", local_validation=True)

        self.assertTrue(raised.exception.provider_turn_interrupted)
        self.assertEqual(raised.exception.next_action, "NONE")
        with open_storage(self.root) as connection:
            churn = json.loads(connection.execute(
                "SELECT churn FROM provider_invocations WHERE run_id=?", (state.run_id,)
            ).fetchone()[0])
        self.assertEqual(churn["interruption_classification"], "provider_turn_interrupted")
        self.assertEqual(churn["interruption_reason"], "host_shutdown_during_provider_turn")
        self.assertTrue(all(span["outcome"] == "INTERRUPTED" for span in phase_spans(self.root, state.run_id)))

    def test_codex_activity_projection_is_fixed_and_never_echoes_event_content(self) -> None:
        self.assertEqual(
            project_codex_activity(
                {"type": "item.started", "item": {"type": "reasoning", "text": "private reasoning"}}
            ),
            "Codex plant de volgende stap",
        )
        self.assertEqual(
            project_codex_activity(
                {"type": "item.updated", "item": {"type": "file_change", "changes": [{"path": "secret.md"}]}}
            ),
            "Codex bewerkt bestanden",
        )
        self.assertIsNone(project_codex_activity({"type": "item.completed", "item": {"type": "agent_message"}}))
        self.assertIsNone(project_codex_activity({"type": "item.started", "item": {"type": "unknown", "prompt": "secret"}}))

    def test_codex_live_action_name_is_bounded_and_rejects_sensitive_or_path_content(self) -> None:
        self.assertEqual(
            project_codex_live_action_name(
                {"type": "item.updated", "item": {"type": "reasoning", "text": "Integrating runtime resolution"}}
            ),
            "Integrating runtime resolution",
        )
        self.assertIsNone(project_codex_live_action_name(
            {"type": "item.started", "item": {"type": "reasoning", "text": "Read /Users/example/.env"}}
        ))
        self.assertIsNone(project_codex_live_action_name(
            {"type": "item.started", "item": {"type": "reasoning", "text": "token=private-value"}}
        ))

    @patch("engineering_platform.execution_host.os.getpgid", return_value=4321)
    @patch("engineering_platform.execution_host.subprocess.Popen")
    def test_codex_client_streams_only_safe_activity_labels(self, popen: object, _: object) -> None:
        class Process:
            pid = 1234
            stdout = iter(
                (
                    '{"type":"item.started","item":{"type":"reasoning","text":"secret reasoning"}}\n',
                    '{"type":"item.started","item":{"type":"command_execution","command":"cat secrets.txt"}}\n',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"secret final"}}\n',
                )
            )

            def wait(self) -> int:
                return 0

        popen.return_value = Process()
        observed: list[str] = []
        client = CodexCliClient()
        client.set_activity_callback(observed.append)

        result = client._run_invocation(("codex", "exec", "--json"), self.root)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(observed, ["Codex plant de volgende stap", "Codex voert een opdracht uit"])
        self.assertNotIn("secret", " ".join(observed))

    @patch("engineering_platform.execution_host.os.getpgid", return_value=4321)
    @patch("engineering_platform.execution_host.subprocess.Popen")
    def test_codex_client_streams_a_safe_transient_action_name_separately(self, popen: object, _: object) -> None:
        class Process:
            pid = 1234
            stdout = iter((
                '{"type":"item.updated","item":{"type":"reasoning","text":"Integrating runtime resolution"}}\n',
                '{"type":"item.started","item":{"type":"command_execution","command":"git status"}}\n',
            ))

            def wait(self) -> int:
                return 0

        popen.return_value = Process()
        activity: list[str] = []
        transient: list[str] = []
        client = CodexCliClient()
        client.set_activity_callback(activity.append)
        client.set_transient_action_callback(transient.append)

        client._run_invocation(("codex", "exec", "--json"), self.root)

        self.assertEqual(activity, ["Codex plant de volgende stap", "Codex voert een opdracht uit"])
        self.assertEqual(transient, ["Integrating runtime resolution"])

    @patch("engineering_platform.execution_host.os.getpgid", return_value=4321)
    @patch("engineering_platform.execution_host.subprocess.Popen")
    def test_codex_client_stops_at_a_host_owned_handoff_deadline(self, popen: object, _: object) -> None:
        class Process:
            pid = 1234
            terminated = False

            class SlowOutput:
                emitted = False

                def __iter__(self) -> "Process.SlowOutput":
                    return self

                def __next__(self) -> str:
                    if self.emitted:
                        raise StopIteration
                    time.sleep(1.1)
                    self.emitted = True
                    return '{"type":"item.started","item":{"type":"reasoning"}}\n'

            stdout = SlowOutput()

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                return 0

        process = Process()
        popen.return_value = process
        client = CodexCliClient()
        client.set_activity_callback(lambda _: None)
        client.set_handoff_deadline_callback(lambda: True)

        with patch("engineering_platform.execution_executor.os.killpg") as killpg:
            with self.assertRaises(CodexHandoffTimeout):
                client._run_invocation(("codex", "exec", "--json"), self.root)

        self.assertEqual(killpg.call_args.args, (4321, signal.SIGTERM))
        self.assertFalse(process.terminated)

    def test_live_status_retains_only_completed_reviewers_after_capability_review(self) -> None:
        state = TransactionState(
            "genesis-context",
            "pcvantol/djconnect",
            str(self.prompt),
            "CAPABILITY_REVIEW",
            execution_mode="GENESIS",
            genesis_repository_path=str(self.root),
        )
        reviewers = [{"reviewer": "validation", "capability": "engineering", "status": "running"}]
        write_live_status(self.root, state, "invoke_agent", reviewers)
        payload = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(payload["execution_mode"], "GENESIS")
        self.assertEqual(payload["target_repository"], self.root.name)
        self.assertEqual(payload["checkout_path"], str(self.root))
        self.assertEqual(payload["reviewer_agents"], reviewers)
        write_live_status(self.root, state, "Codex voert een opdracht uit")
        preserved = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(preserved["reviewer_agents"], reviewers)
        finalization = TransactionState(
            "genesis-context",
            "pcvantol/djconnect",
            str(self.prompt),
            "FINALIZE_AGENT",
            execution_mode="GENESIS",
            genesis_repository_path=str(self.root),
        )
        write_live_status(self.root, finalization, "create_finalization", reviewers)
        cleared = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(cleared["reviewer_agents"], [])
        completed = [{"reviewer": "validation", "capability": "engineering", "status": "completed"}]
        write_live_status(self.root, state, "review completed", completed)
        write_live_status(self.root, finalization, "create_finalization")
        historical = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(historical["reviewer_agents"], completed)

    def test_live_action_name_is_filesystem_only_and_clears_when_terminal(self) -> None:
        state = TransactionState("live-action", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        write_live_status(self.root, state, "Codex plant de volgende stap", transient_action="Integrating runtime resolution")
        status_file = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(status_file["transient_action"], "Integrating runtime resolution")
        self.assertNotIn("transient_action", load_projection(self.root, "live_status"))

        terminal = TransactionState("live-action", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        write_live_status(self.root, terminal, "Uitvoering voltooid")
        self.assertNotIn("transient_action", json.loads((self.root / ".engineering" / "status" / "current.json").read_text()))

    def test_live_status_preserves_safe_workspace_progress(self) -> None:
        state = TransactionState("workspace-progress", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        write_live_status(
            self.root,
            state,
            "Codex bewerkt bestanden",
            workspace_progress={"modified": 3, "created": 2, "deleted": 1},
        )
        payload = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertEqual(payload["workspace_progress"], {
            "modified": 3, "created": 2, "deleted": 1, "codex_commands_executed": 0,
        })
        self.assertNotIn("path", json.dumps(payload["workspace_progress"]))
        self.assertTrue(payload["live_worktree_snapshot"]["volatile"])
        self.assertEqual(payload["live_worktree_snapshot"]["kind"], "LIVE_WORKTREE_SNAPSHOT")
        self.assertEqual(payload["cumulative_activity"]["overall_activity_total"], 0)

        terminal = TransactionState("workspace-progress", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        write_live_status(self.root, terminal, "completed")
        completed = json.loads((self.root / ".engineering" / "status" / "current.json").read_text())
        self.assertIsNone(completed["live_worktree_snapshot"])

    def test_workspace_change_summary_counts_only_change_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            (root / "modified.txt").write_text("before", encoding="utf-8")
            (root / "deleted.txt").write_text("delete", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"),
                cwd=root,
                check=True,
            )
            (root / "modified.txt").write_text("after", encoding="utf-8")
            (root / "deleted.txt").unlink()
            (root / "created.txt").write_text("new", encoding="utf-8")
            self.assertEqual(workspace_change_summary(root), {"modified": 1, "created": 1, "deleted": 1})

    @patch("engineering_platform.execution_executor.workspace_change_summary")
    @patch("engineering_platform.execution_host.subprocess.Popen")
    def test_codex_client_publishes_only_changed_aggregate_workspace_progress(
        self, popen: object, summary: object
    ) -> None:
        class Process:
            pid = 1234
            stdout = iter((
                '{"type":"item.started","item":{"type":"reasoning"}}\n',
                '{"type":"item.updated","item":{"type":"file_change"}}\n',
            ))

            def wait(self) -> int:
                return 0

        popen.return_value = Process()
        summary.side_effect = (
            {"modified": 0, "created": 0, "deleted": 0},
            {"modified": 1, "created": 2, "deleted": 0},
        )
        observed: list[dict[str, int]] = []
        client = CodexCliClient()
        client.set_workspace_progress_callback(observed.append)

        client._run_invocation(("codex", "exec", "--json"), self.root)

        self.assertEqual(observed, [
            {"modified": 0, "created": 0, "deleted": 0, "codex_commands_executed": 0},
            {"modified": 1, "created": 2, "deleted": 0, "codex_commands_executed": 0},
        ])

    @patch("engineering_platform.execution_executor.workspace_change_summary")
    @patch("engineering_platform.execution_host.subprocess.Popen")
    def test_codex_client_counts_only_distinct_started_commands(self, popen: object, summary: object) -> None:
        class Process:
            pid = 1234
            stdout = iter((
                '{"type":"item.started","item":{"type":"command_execution","id":"command-1","command":"pytest"}}\n',
                '{"type":"item.completed","item":{"type":"command_execution","id":"command-1"}}\n',
                '{"type":"item.started","item":{"type":"command_execution","id":"command-2","command":"git status"}}\n',
            ))

            def wait(self) -> int:
                return 0

        popen.return_value = Process()
        summary.return_value = {"modified": 1, "created": 0, "deleted": 0}
        client = CodexCliClient()
        client.set_command_callback(lambda *_: None)
        client.set_workspace_progress_callback(lambda _: None)

        client._run_invocation(("codex", "exec", "--json"), self.root)

        self.assertEqual(client.last_execution_metadata, {
            "modified": 1, "created": 0, "deleted": 0, "codex_commands_executed": 2,
        })

    def test_genesis_mode_requires_an_explicit_execution_mode_declaration(self) -> None:
        self.assertEqual(execution_mode_for("Introduce Genesis Mode documentation."), "MANAGED")
        self.assertEqual(execution_mode_for("Execution Mode: Genesis"), "GENESIS")

    def test_genesis_context_rejects_relative_and_host_targets(self) -> None:
        with self.assertRaisesRegex(RunnerError, "must be absolute"):
            resolve_execution_context(
                "Execution Mode: Genesis\n\nTarget repository:\n\nrelative/project\n",
                self.root,
            )

    def test_genesis_context_accepts_inline_target_declaration(self) -> None:
        context = resolve_execution_context(
            "Execution Mode: Genesis\nTarget repository: /tmp/qualified-genesis\n",
            self.root,
        )
        self.assertEqual(context.execution_mode, "GENESIS")
        self.assertEqual(context.target_repository, Path("/tmp/qualified-genesis").resolve())
        with self.assertRaisesRegex(RunnerError, "cannot be the Engineering Platform host"):
            resolve_execution_context(
                f"Execution Mode: Genesis\n\nTarget repository:\n\n{self.root}\n",
                self.root,
            )

    def test_genesis_preflight_rejects_non_git_directory(self) -> None:
        target = self.root / "not-a-git-repository"
        target.mkdir()
        self.assertIn("not an accessible Git repository", genesis_workspace_preflight(target) or "")

    def test_additional_workspace_roots_are_absent_without_local_configuration(self) -> None:
        self.assertEqual(additional_workspace_write_roots(self.root), ())

    def test_additional_workspace_roots_reject_invalid_and_filesystem_root_configuration(self) -> None:
        local = self.root / ".engineering"
        local.mkdir()
        (local / "engineering-platform.local.json").write_text(
            json.dumps({"workspace": {"workspace_authorization": {"allowed_roots": [{"path": str(self.root / "missing"), "repository_scope": "direct_children"}], "allowed_repositories": [], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RunnerError, "existing non-root directory"):
            additional_workspace_write_roots(self.root)

        external = Path(self.root.anchor).resolve()
        (local / "engineering-platform.local.json").write_text(
            json.dumps({"workspace": {"workspace_authorization": {"allowed_roots": [{"path": str(external), "repository_scope": "direct_children"}], "allowed_repositories": [], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RunnerError, "non-root"):
            additional_workspace_write_roots(self.root)

    def test_genesis_mode_reconciles_a_clean_local_commit_without_remote_or_pr(self) -> None:
        target = self.root.parent / f"genesis-{self.root.name}"
        target.mkdir()
        subprocess.run(("git", "init", "--initial-branch=main", str(target)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(target), "config", "user.email", "genesis@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(target), "config", "user.name", "Genesis Test"), check=True)
        (target / "README.md").write_text("# Genesis\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(target), "add", "README.md"), check=True)
        subprocess.run(("git", "-C", str(target), "commit", "-m", "Initialize"), check=True, capture_output=True)
        commit = subprocess.run(("git", "-C", str(target), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        self.prompt.write_text(
            f"# New workspace\n\nExecution Mode: Genesis\n\nTarget repository:\n\n{target}\n",
            encoding="utf-8",
        )
        agent = FakeAgent(AgentResult("COMPLETE", terminal_condition="local_commit_reconciled", repository_path=str(target), commit_sha=commit))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        with patch("engineering_platform.execution_host.additional_workspace_write_roots", return_value=(target.parent.resolve(),)), patch("engineering_platform.execution_host.target_repository_authorization", return_value=None):
            state = runner.run(self.prompt, run_id="genesis-run")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertEqual(state.execution_mode, "GENESIS")
        self.assertEqual(state.genesis_repository_path, str(target))
        self.assertEqual(state.genesis_commit_sha, commit)
        self.assertIsNone(state.pull_request)

    def test_genesis_selects_its_target_before_managed_cleanliness_checks(self) -> None:
        target = self.root.parent / f"genesis-clean-{self.root.name}"
        target.mkdir()
        subprocess.run(("git", "init", "--initial-branch=main", str(target)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(target), "config", "user.email", "genesis@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(target), "config", "user.name", "Genesis Test"), check=True)
        (target / "README.md").write_text("# Genesis\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(target), "add", "README.md"), check=True)
        subprocess.run(("git", "-C", str(target), "commit", "-m", "Initialize"), check=True, capture_output=True)
        commit = subprocess.run(("git", "-C", str(target), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        self.prompt.write_text(f"Execution Mode: Genesis\n\nTarget repository:\n\n{target}\n", encoding="utf-8")
        agent = FakeAgent(AgentResult("COMPLETE", terminal_condition="local_commit_reconciled", repository_path=str(target), commit_sha=commit))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(clean=False), FakeGitHub([]), agent, lambda _: None)
        with patch("engineering_platform.execution_host.additional_workspace_write_roots", return_value=(target.parent.resolve(),)), patch("engineering_platform.execution_host.target_repository_authorization", return_value=None):
            state = runner.run(self.prompt, run_id="genesis-before-managed")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertEqual(state.genesis_repository_path, str(target))
        self.assertEqual(len(agent.prompts), 1)

    def test_genesis_without_target_blocks_without_falling_back_to_managed(self) -> None:
        self.prompt.write_text("Execution Mode: Genesis\n", encoding="utf-8")
        agent = FakeAgent(AgentResult("COMPLETE"))
        state = EngineeringRunner(self.root, self.store, FakeRepository(clean=False), FakeGitHub([]), agent, lambda _: None).run(self.prompt, run_id="genesis-missing-target")
        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.execution_mode, "GENESIS")
        self.assertIn("Target repository", state.diagnostic or "")
        self.assertEqual(agent.prompts, [])

    def test_conflicting_genesis_targets_fail_closed(self) -> None:
        with self.assertRaisesRegex(RunnerError, "conflicting Target"):
            resolve_execution_context(
                "Execution Mode: Genesis\n\nTarget repository:\n\n/tmp/one\n\nTarget repository:\n\n/tmp/two\n",
                self.root,
            )

    def test_conflicting_genesis_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(RunnerError, "conflicts"):
            resolve_execution_context(
                "Execution Mode: Genesis\nExecution Mode: Managed\n\nTarget repository:\n\n/tmp/forge\n",
                self.root,
            )

    def test_dirty_genesis_target_uses_genesis_diagnostic(self) -> None:
        target = self.root.parent / f"genesis-dirty-{self.root.name}"
        target.mkdir()
        subprocess.run(("git", "init", "--initial-branch=main", str(target)), check=True, capture_output=True)
        (target / "untracked.md").write_text("dirty\n", encoding="utf-8")
        self.prompt.write_text(f"Execution Mode: Genesis\n\nTarget repository:\n\n{target}\n", encoding="utf-8")
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(clean=False), FakeGitHub([]), agent, lambda _: None)
        with patch("engineering_platform.execution_host.additional_workspace_write_roots", return_value=(target.parent.resolve(),)):
            state = runner.run(self.prompt, run_id="genesis-dirty-target")
        self.assertEqual(state.phase, "BLOCKED")
        self.assertIn("Genesis preflight blocked", state.diagnostic or "")
        self.assertNotIn("working tree is not clean", state.diagnostic or "")
        self.assertEqual(agent.prompts, [])

    def test_rejects_malformed_state(self) -> None:
        path = self.store.path_for("bad-run")
        path.parent.mkdir(parents=True)
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(StateError):
            self.store.load("bad-run")
        with self.assertRaises(StateError):
            TransactionState.from_dict({"schema_version": 1, "run_id": "bad-run", "repository": "pcvantol/djconnect", "prompt_path": str(self.prompt), "phase": "INITIALIZE", "branch": None, "pull_request": None, "last_verified_sha": None, "next_action": "invoke_agent", "terminal_condition": "repository_reconciled", "terminal": "false"})

    def test_state_persistence_is_atomic_and_owner_only(self) -> None:
        state = TransactionState("atomic-run", "pcvantol/djconnect", str(self.prompt), "INITIALIZE")
        path = self.store.save(state)
        self.assertEqual(self.store.load("atomic-run"), state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_completed_history_is_bounded_without_removing_malformed_state(self) -> None:
        for number in range(11):
            self.store.save(TransactionState(f"done-{number}", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True))
        malformed = self.store.path_for("malformed-run")
        malformed.write_text("not-json", encoding="utf-8")
        self.store.save(TransactionState("done-11", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True))
        self.assertEqual(len([path for path in self.store.directory.glob("done-*.json")]), 10)
        self.assertTrue(malformed.exists())

    def test_repository_mismatch_fails_closed_on_resume(self) -> None:
        self.store.save(TransactionState("resume-run", "other/repository", str(self.prompt), "EXECUTE_AGENT"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None)
        with self.assertRaisesRegex(RunnerError, "conflicts"):
            runner.run(self.prompt, run_id="resume-run", resume=True)

    def test_resume_rejects_a_dismissed_execution_without_invoking_the_agent(self) -> None:
        from engineering_platform.prompt_history import record_prompt_execution
        from engineering_platform.storage import record_execution_dismissal

        run_id = "dismissed-resume-run"
        self.store.save(TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT"))
        record_prompt_execution(
            self.root,
            run_id=run_id,
            terminal_state="BLOCKED",
            prompt_title="Dismissed blocked execution",
            executed_at="2026-08-08T10:00:00+00:00",
        )
        record_execution_dismissal(
            self.root,
            run_id=run_id,
            terminal_state="BLOCKED",
            dismissed_at="2026-08-08T10:01:00+00:00",
            dismissed_by="test_operator",
        )
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)

        with self.assertRaisesRegex(RunnerError, "already been dismissed"):
            runner.run(self.prompt, run_id=run_id, resume=True)

        self.assertEqual(agent.prompts, [])
        self.assertEqual(self.store.load(run_id).phase, "EXECUTE_AGENT")

    def test_resume_recomputes_waiting_phase_from_pr_evidence(self) -> None:
        self.store.save(TransactionState("resume-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", pull_request=11, diagnostic="Prior waiting diagnostic."))
        pending = PullRequestEvidence(11, "OPEN", False, False)
        passed = PullRequestEvidence(11, "OPEN", True, True)
        github = FakeGitHub([pending, passed])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        # A non-terminal resume cannot claim completion from its stored phase.
        state = runner._reconcile(self.store.load("resume-run"), FakeRepository().inspect(self.root))
        self.assertEqual(state.phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertEqual(state.next_action, "poll_required_checks")

    def test_blocked_diagnostic_is_persisted(self) -> None:
        agent = FakeAgent(AgentResult("BLOCKED", diagnostic="Repository not synchronized."))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="blocked-run")
        self.assertEqual(state.phase, "BLOCKED")
        self.assertEqual(state.diagnostic, "Repository not synchronized.")
        self.assertEqual(self.store.load("blocked-run").diagnostic, state.diagnostic)

    def test_failed_diagnostic_is_persisted(self) -> None:
        agent = FakeAgent(AgentResult("FAILED", diagnostic="Engineering policy requires approval."))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="failed-run")
        self.assertEqual(state.phase, "FAILED")
        self.assertEqual(state.diagnostic, "Engineering policy requires approval.")

    def test_terminal_console_report_includes_reason_and_next_action(self) -> None:
        state = TransactionState("report-run", "pcvantol/djconnect", str(self.prompt), "BLOCKED", next_action="external_merge_authorization_required", diagnostic="Merge requires explicit authorization.", terminal=True)
        report = _format_terminal_report(state)
        self.assertIn("Reason:\nMerge requires explicit authorization.", report)
        self.assertIn("Next action:\nObtain the required merge authorization.", report)

    def test_complete_omits_diagnostic(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE", diagnostic="This must not be retained."))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="complete-run")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertIsNone(state.diagnostic)

    def test_cli_failure_exposes_only_redacted_console_detail(self) -> None:
        completed = __import__("subprocess").CompletedProcess(("codex",), 7, "ACCESS_TOKEN=stdout-secret", "Bearer stderr-secret")
        with patch("engineering_platform.execution_host.subprocess.run", return_value=completed):
            with self.assertRaises(CodexInvocationError) as raised:
                CodexCliClient().invoke(self.root, "test")
        self.assertIn("code 7", str(raised.exception))
        self.assertNotIn("stdout-secret", raised.exception.console_detail)
        self.assertNotIn("stderr-secret", raised.exception.console_detail)
        self.assertIn("[REDACTED]", raised.exception.console_detail)

    def test_cli_usage_extracts_only_reported_numeric_fields(self) -> None:
        usage = extract_codex_usage(
            '{"usage":{"input_tokens":120,"output_tokens":30,"cost":0.04,"ignored":"text"}}\n'
        )

        self.assertEqual(usage, {"input_tokens": 120, "output_tokens": 30, "cost": 0.04})

    def test_cli_json_usage_and_final_message_are_recorded_together(self) -> None:
        captured: list[str] = []
        output = "\n".join(
            (
                '{"type":"thread.started","thread_id":"run-1"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"{\\\"terminal_state\\\":\\\"COMPLETE\\\",\\\"branch\\\":null,\\\"pull_request\\\":null,\\\"terminal_condition\\\":\\\"repository_reconciled\\\",\\\"diagnostic\\\":null,\\\"repository_path\\\":null,\\\"commit_sha\\\":null}"}}',
                '{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":30,"total_tokens":150}}',
            )
        )

        def invoke_with_json(command: tuple[str, ...], **_: object) -> object:
            captured.extend(command)
            return __import__("subprocess").CompletedProcess(command, 0, output, "")

        client = CodexCliClient()
        with patch("engineering_platform.execution_host.subprocess.run", side_effect=invoke_with_json):
            result = client.invoke(self.root, "test")

        self.assertIn("--json", captured)
        self.assertEqual(
            captured[captured.index("--sandbox") + 1],
            "danger-full-access",
        )
        self.assertEqual(result.terminal_state, "COMPLETE")
        self.assertEqual(
            client.last_usage,
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )
        self.assertEqual(client.last_usage_snapshots, ({"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},))

    def test_genesis_workspace_preflight_requires_accessible_target(self) -> None:
        issue = genesis_workspace_preflight(Path("/definitely/absent/forge"))

        self.assertIn("Target repository path is absent", issue or "")

    def test_cli_usage_is_written_only_when_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_codex_usage(root, "inbox-usage", {"total_tokens": 150})
            payload = json.loads((root / ".engineering" / "status" / "codex_usage.json").read_text())
            self.assertEqual(payload, {"run_id": "inbox-usage", "usage": {"total_tokens": 150}})

    def test_cli_output_schema_requires_every_declared_property(self) -> None:
        captured: dict[str, object] = {}

        def invoke_with_schema(command: tuple[str, ...], **_: object) -> object:
            schema_path = Path(command[command.index("--output-schema") + 1])
            captured.update(__import__("json").loads(schema_path.read_text(encoding="utf-8")))
            return __import__("subprocess").CompletedProcess(
                command,
                0,
                '{"terminal_state":"COMPLETE","branch":null,"pull_request":null,"terminal_condition":"repository_reconciled","diagnostic":"","repository_path":null,"commit_sha":null}\n',
                "",
            )

        with patch("engineering_platform.execution_host.subprocess.run", side_effect=invoke_with_schema):
            CodexCliClient().invoke(self.root, "test")

        self.assertEqual(set(captured["properties"]), set(captured["required"]))

    def test_cli_output_schema_never_creates_repository_local_state_directory(self) -> None:
        """Provider protocol scratch belongs in temporary storage, not a checkout."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def invoke_with_schema(command: tuple[str, ...], **_: object) -> object:
                schema_path = Path(command[command.index("--output-schema") + 1])
                self.assertTrue(schema_path.is_file())
                self.assertFalse(schema_path.is_relative_to(root))
                return __import__("subprocess").CompletedProcess(
                    command,
                    0,
                    '{"terminal_state":"COMPLETE","branch":null,"pull_request":null,'
                    '"terminal_condition":"repository_reconciled","diagnostic":"",'
                    '"repository_path":null,"commit_sha":null}\n',
                    "",
                )

            with patch("engineering_platform.execution_host.subprocess.run", side_effect=invoke_with_schema):
                CodexCliClient().invoke(root, "test")

            self.assertFalse((root / ".engineering" / "engineering-runs").exists())

    def test_cli_adds_configured_sibling_project_root(self) -> None:
        local = self.root / ".engineering"
        local.mkdir(exist_ok=True)
        workspace_root = self.root.parent.resolve()
        (local / "engineering-platform.local.json").write_text(
            __import__("json").dumps({"workspace": {"workspace_authorization": {"allowed_roots": [{"path": str(workspace_root), "repository_scope": "direct_children"}], "allowed_repositories": [], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}),
            encoding="utf-8",
        )
        captured: list[str] = []

        def invoke_with_workspace_root(command: tuple[str, ...], **_: object) -> object:
            captured.extend(command)
            return __import__("subprocess").CompletedProcess(
                command,
                0,
                '{"terminal_state":"COMPLETE","branch":null,"pull_request":null,"terminal_condition":"repository_reconciled","diagnostic":"","repository_path":null,"commit_sha":null}\n',
                "",
            )

        with patch("engineering_platform.execution_host.subprocess.run", side_effect=invoke_with_workspace_root):
            CodexCliClient().invoke(self.root, "test")

        self.assertEqual(captured[captured.index("--add-dir") + 1], str(workspace_root))

    def test_cli_failure_log_omits_prompt_and_keeps_error_tail(self) -> None:
        detail = _format_cli_failure(
            1,
            "header\nuser\nconfidential prompt body\nERROR: actionable failure",
            "",
            "confidential prompt body",
        )
        self.assertNotIn("confidential prompt body", detail)
        self.assertIn("[PROMPT_OMITTED]", detail)
        self.assertIn("ERROR: actionable failure", detail)

    def test_codex_cli_log_is_private_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_redacted_codex_cli_log(Path(temporary), "cli-run", "Bearer private-token")
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("private-token", content)
            self.assertIn("[REDACTED]", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sensitive_diagnostic_is_redacted_before_persistence(self) -> None:
        agent = FakeAgent(AgentResult("BLOCKED", diagnostic="authorization=top-secret API_KEY=also-secret"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="redacted-run")
        self.assertEqual(state.diagnostic, "[REDACTED] [REDACTED]")
        self.assertEqual(redact_diagnostic("Bearer private-token"), "[REDACTED]")

    def test_green_open_pr_waits_for_operator_merge(self) -> None:
        state = TransactionState("pending-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=12, terminal_condition="open_pr_checks_terminal")
        pending = PullRequestEvidence(12, "OPEN", False, False)
        passed = PullRequestEvidence(12, "OPEN", True, True)
        github = FakeGitHub([pending, passed])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._poll(state)
        self.assertEqual(github.calls, 2)
        self.assertEqual(result.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(result.next_action, "await_operator_pr_merge")
        self.assertFalse(result.terminal)
        self.assertEqual(self.store.load("pending-run").phase, "WAIT_FOR_OPERATOR_MERGE")

    def test_pr_wait_blocks_durably_on_github_without_requiring_codex(self) -> None:
        state = TransactionState(
            "github-auth-wait", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=12,
        )
        github = FakeGitHub([PullRequestEvidence(12, "OPEN", True, True)])
        with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=("GITHUB",)):
            result = EngineeringRunner(
                self.root, self.store, FakeRepository(), github,
                FakeAgent(AgentResult("WAITING")), lambda _: None,
            )._poll(state)
        self.assertEqual(result.phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertFalse(result.terminal)
        self.assertEqual(result.next_action, "provider_auth_repair_required")
        self.assertEqual(result.auth_recovery_phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertEqual(result.auth_recovery_providers, ("GITHUB",))
        self.assertEqual(github.calls, 0)

    def test_agent_readiness_block_preserves_original_phase_without_invocation(self) -> None:
        state = TransactionState("codex-auth-run", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT")
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=("CODEX", "GITHUB")):
            with self.assertRaisesRegex(Exception, "Provider readiness"):
                runner._invoke_agent_with_timing(state, "bounded work")
        blocked = self.store.load(state.run_id)
        self.assertEqual(blocked.phase, "FINALIZE_AGENT")
        self.assertEqual(blocked.next_action, "provider_auth_repair_required")
        self.assertEqual(blocked.auth_recovery_providers, ("CODEX", "GITHUB"))
        self.assertEqual(agent.prompts, [])

    def test_operator_merge_wait_resume_only_polls_the_pull_request(self) -> None:
        state = TransactionState(
            "lightweight-merge-wait", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_OPERATOR_MERGE", branch="codex/waiting", pull_request=12,
            owner_authorized=True, waiting_for_merge_since="2026-08-17T07:00:00+00:00",
        )
        self.store.save(state)
        repository = FakeRepository()
        github = FakeGitHub([PullRequestEvidence(12, "OPEN", True, True)])
        agent = FakeAgent(AgentResult("WAITING"))

        result = EngineeringRunner(
            self.root, self.store, repository, github, agent, lambda _: None
        ).run(self.prompt, run_id=state.run_id, resume=True)

        self.assertEqual(result.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(github.calls, 1)
        self.assertEqual(repository.synchronize_calls, [])
        self.assertEqual(agent.prompts, [])
        self.assertFalse(any(
            span["phase_name"] == "EXECUTION_PREPARATION"
            for span in phase_spans(self.root, state.run_id)
        ))

    def test_resume_reacquires_lease_before_bounded_repair_and_timeout_blocks(self) -> None:
        state = TransactionState(
            "resume-repair-lease", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_OPERATOR_MERGE", branch="codex/resume-repair", pull_request=12,
            owner_authorized=True, transaction_kind="FINALIZATION",
            admission_decision="PASS", admission_completed_at="2026-08-17T07:00:00+00:00",
            admission_evidence_source="RUNNER",
        )
        self.store.save(state)
        agent = LeaseAwareDeadlineAgent(state.run_id)
        result = EngineeringRunner(
            self.root,
            self.store,
            FakeRepository(),
            FakeGitHub([PullRequestEvidence(12, "OPEN", True, False, failed_checks=("browser-dashboard",))]),
            agent,
            lambda _: None,
        ).run(self.prompt, run_id=state.run_id, resume=True)

        self.assertEqual(agent.observed_lease and agent.observed_lease.get("state"), "LIVE")
        self.assertIsNone(agent.deadline_callback)
        self.assertTrue(result.terminal)
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.next_action, "repair_agent_timeout")
        self.assertEqual(result.terminal_condition, "repair_agent_timeout")
        self.assertEqual(lease_history(self.root, state.run_id).get("lease_state"), "RELEASED")

    def test_merged_pull_request_refreshes_main_reference_before_containment_check(self) -> None:
        state = TransactionState(
            "merged-main-refresh", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_OPERATOR_MERGE", branch="codex/merged", pull_request=12,
            transaction_kind="FINALIZATION", owner_authorized=True,
        )
        repository = FakeRepository(contains=True)

        result = EngineeringRunner(
            self.root,
            self.store,
            repository,
            FakeGitHub([PullRequestEvidence(12, "MERGED", True, True, "b" * 40)]),
            FakeAgent(AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)),
            lambda _: None,
        )._poll(state)

        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(repository.refresh_main_reference_calls, [self.root])
        self.assertEqual(repository.synchronize_calls, [self.root])

    def test_agent_cannot_reuse_main_or_an_unbranched_pr_as_transaction_evidence(self) -> None:
        state = TransactionState(
            "invalid-pr-evidence",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="main",
            pull_request=791,
            owner_authorized=True,
        )
        result = EngineeringRunner(
            self.root,
            self.store,
            FakeRepository(),
            FakeGitHub([]),
            FakeAgent(AgentResult("COMPLETE", "main", 791)),
            lambda _: None,
        )._poll(state, AgentResult("COMPLETE", "main", 791))
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.next_action, "invalid_pull_request_evidence")
        self.assertIn("current main branch", result.diagnostic or "")

    def test_agent_cannot_adopt_an_already_merged_pull_request_as_new_evidence(self) -> None:
        state = TransactionState(
            "historical-pr-evidence",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="codex/historical",
            pull_request=790,
            owner_authorized=True,
        )
        github = FakeGitHub([PullRequestEvidence(790, "MERGED", True, True, "b" * 40)])
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), github,
            FakeAgent(AgentResult("WAITING")), lambda _: None,
        )

        result = runner._reject_historical_agent_pull_request(state)

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.next_action, "historical_pull_request_evidence")
        self.assertIn("already merged PR #790", result.diagnostic or "")
        self.assertEqual(github.ready_calls, [])

    def test_retry_lineage_reconciles_its_own_merged_pull_request(self) -> None:
        self.prompt.write_text("Retry-Of: inbox-original\n# Retry", encoding="utf-8")
        state = TransactionState(
            "lineage-pr-evidence",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="engineering/execution-phase-telemetry",
            pull_request=819,
        )
        github = FakeGitHub([
            PullRequestEvidence(
                819, "MERGED", True, True, "b" * 40, False, (),
                "engineering/execution-phase-telemetry", "main",
            ),
        ])
        repository = FakeRepository(contains=True)
        runner = EngineeringRunner(
            self.root, self.store, repository, github,
            FakeAgent(AgentResult("WAITING")), lambda _: None,
        )

        result = runner._reject_historical_agent_pull_request(state)

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(result.implementation_pull_request, 819)
        self.assertEqual(result.implementation_merge_commit, "b" * 40)
        self.assertEqual(repository.cleanup_calls, [("engineering/execution-phase-telemetry", None)])

    def test_retry_lineage_rejects_a_merged_pull_request_from_another_branch(self) -> None:
        self.prompt.write_text("Retry-Of: inbox-original\n# Retry", encoding="utf-8")
        state = TransactionState(
            "lineage-pr-mismatch",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="engineering/current-work",
            pull_request=819,
        )
        github = FakeGitHub([
            PullRequestEvidence(
                819, "MERGED", True, True, "b" * 40, False, (),
                "engineering/other-work", "main",
            ),
        ])
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(contains=True), github,
            FakeAgent(AgentResult("WAITING")), lambda _: None,
        )

        result = runner._reject_historical_agent_pull_request(state)

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.next_action, "historical_pull_request_evidence")

    def test_owner_authorization_does_not_merge_green_finalization_pr(self) -> None:
        state = TransactionState("authorized-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=14, transaction_kind="FINALIZATION", owner_authorized=True)
        github = FakeGitHub([PullRequestEvidence(14, "OPEN", True, True), PullRequestEvidence(14, "MERGED", True, True, "b" * 40)])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._poll(state)
        self.assertEqual(github.merge_calls, [])
        self.assertEqual(result.phase, "WAIT_FOR_OPERATOR_MERGE")

    def test_merged_operator_handoff_resumes_on_a_later_poll(self) -> None:
        state = TransactionState(
            "later-merge", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_OPERATOR_MERGE", branch="codex/implementation", pull_request=21,
            owner_authorized=True, waiting_for_merge_since="2026-08-15T09:00:00+00:00",
        )
        github = FakeGitHub([
            PullRequestEvidence(21, "MERGED", True, True, "b" * 40),
            PullRequestEvidence(22, "OPEN", True, True),
            PullRequestEvidence(22, "MERGED", True, True, "c" * 40),
        ])
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), github,
            SequencedFakeAgent([AgentResult("WAITING", "codex/final", 22), AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)]), lambda _: None,
        )

        result = runner._poll(state)

        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(result.implementation_merge_commit, "b" * 40)
        self.assertEqual(result.finalization_merge_commit, "c" * 40)

    def test_merged_implementation_starts_and_reconciles_finalization(self) -> None:
        implementation = PullRequestEvidence(21, "MERGED", True, True, "b" * 40)
        final_open = PullRequestEvidence(22, "OPEN", True, True)
        final_merged = PullRequestEvidence(22, "MERGED", True, True, "c" * 40)
        agent = SequencedFakeAgent([AgentResult("WAITING", "codex/final", 22), AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)])
        state = TransactionState("full-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/implementation", pull_request=21, owner_authorized=True)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([implementation, final_open, final_merged]), agent, lambda _: None)
        result = runner._poll(state)
        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(result.implementation_pull_request, 21)
        self.assertEqual(result.finalization_pull_request, 22)
        self.assertEqual(result.finalization_merge_commit, "c" * 40)
        self.assertIn("mandatory governance-only Finalization", agent.prompts[0])
        self.assertIn("engineering_platform.repository_handoff", agent.prompts[0])
        self.assertIn("handoff records to that same Finalization branch", agent.prompts[0])

    def test_owner_authorized_merged_lifecycle_reconciles_and_cleans_up(self) -> None:
        """A verified two-PR happy path completes without a runner merge call."""
        implementation = PullRequestEvidence(21, "MERGED", True, True, "b" * 40)
        final_open = PullRequestEvidence(22, "OPEN", True, True)
        final_merged = PullRequestEvidence(22, "MERGED", True, True, "c" * 40)
        repository = FakeRepository()
        github = FakeGitHub([implementation, final_open, final_merged])
        state = TransactionState(
            "autonomous-happy-path", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/implementation",
            pull_request=21, owner_authorized=True,
        )
        result = EngineeringRunner(
            self.root, self.store, repository, github,
            SequencedFakeAgent([AgentResult("WAITING", "codex/final", 22), AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)]), lambda _: None,
        )._poll(state)

        self.assertEqual(result.phase, "COMPLETE")
        self.assertTrue(result.terminal)
        self.assertEqual(result.implementation_merge_commit, "b" * 40)
        self.assertEqual(result.finalization_merge_commit, "c" * 40)
        self.assertEqual(repository.cleanup_calls, [("codex/implementation", "codex/final")])
        self.assertEqual(github.merge_calls, [])

    def test_merged_finalization_returned_by_agent_is_reconciled_without_ready(self) -> None:
        implementation = PullRequestEvidence(21, "MERGED", True, True, "b" * 40)
        final_merged = PullRequestEvidence(22, "MERGED", True, True, "c" * 40)
        agent = SequencedFakeAgent([AgentResult("WAITING", "codex/final", 22), AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)])
        state = TransactionState(
            "already-finalized", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/implementation",
            pull_request=21, owner_authorized=True,
        )
        github = FakeGitHub([implementation, final_merged])
        result = EngineeringRunner(
            self.root, self.store, FakeRepository(), github, agent, lambda _: None,
        )._poll(state)

        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(result.finalization_pull_request, 22)
        self.assertEqual(result.finalization_merge_commit, "c" * 40)
        self.assertEqual(github.ready_calls, [])

    def test_finalization_checkpoint_prevents_duplicate_generation(self) -> None:
        state = TransactionState("no-duplicate", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT", owner_authorized=True, transaction_kind="FINALIZATION", finalization_branch="codex/final", finalization_pull_request=23)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([PullRequestEvidence(23, "OPEN", False, False)]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._start_finalization(state, 21)
        self.assertEqual(result.pull_request, 23)
        self.assertEqual(runner.agent.prompts, [])

    def test_finalization_recovery_persists_existing_pr_without_invoking_agent(self) -> None:
        state = TransactionState(
            "recover-finalization", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT",
            owner_authorized=True, transaction_kind="FINALIZATION",
            implementation_pull_request=21, implementation_merge_commit="b" * 40,
            finalization_branch="codex/finalize-recover-finalization",
        )
        candidate = PullRequestEvidence(
            22, "OPEN", True, True, None, False, (),
            "codex/finalize-recover-finalization", "main",
        )
        github = FakeGitHub([candidate], branch_response=candidate)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)

        recovered = runner._recover_finalization_pull_request(state, FakeRepository().inspect(self.root))

        self.assertEqual(recovered.finalization_pull_request, 22)
        self.assertEqual(recovered.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(github.branch_calls, ["codex/finalize-recover-finalization"])
        self.assertEqual(runner.agent.prompts, [])

    def test_finalization_recovery_fails_closed_when_existing_pr_does_not_match_branch(self) -> None:
        state = TransactionState(
            "reject-finalization", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT",
            owner_authorized=True, transaction_kind="FINALIZATION",
            implementation_pull_request=21, implementation_merge_commit="b" * 40,
            finalization_branch="codex/finalize-reject-finalization",
        )
        candidate = PullRequestEvidence(22, "OPEN", True, True, None, False, (), "codex/other", "main")
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([], branch_response=candidate),
            FakeAgent(AgentResult("WAITING")), lambda _: None,
        )

        blocked = runner._recover_finalization_pull_request(state, FakeRepository().inspect(self.root))

        self.assertEqual(blocked.phase, "BLOCKED")
        self.assertEqual(blocked.next_action, "finalization_recovery_evidence_invalid")
        self.assertEqual(runner.agent.prompts, [])

    def test_repair_records_iterations_and_failed_check_name(self) -> None:
        state = TransactionState("repair-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/repair", pull_request=24, owner_authorized=True)
        github = FakeGitHub([PullRequestEvidence(24, "OPEN", True, False, failed_checks=("Ruff",))])
        agent = SequencedFakeAgent([AgentResult("BLOCKED", "codex/repair", 24, diagnostic="External review required.")])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, agent, lambda _: None)
        repaired = runner._poll(state)
        self.assertEqual(repaired.repair_iterations, 1)
        self.assertIn("Ruff failed", agent.prompts[0])
        self.assertEqual(len(repaired.repair_audit), 1)
        self.assertEqual(repaired.repair_audit[0]["failed_checks"], "Ruff")
        self.assertEqual(repaired.repair_audit[0]["outcome"], "agent_failed")
        self.assertEqual(repaired.repair_audit[0]["agent_summary"], "External review required.")

    def test_verified_phase_commit_evidence_requires_exact_clean_branch_and_sha(self) -> None:
        repository = FakeRepository(branch="codex/verified-commit")
        repository.evidence = RepositoryEvidence(
            "pcvantol/djconnect", "codex/verified-commit", "b" * 40, True, False
        )
        runner = EngineeringRunner(
            self.root, self.store, repository, FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None,
        )
        state = TransactionState(
            "commit-evidence", "pcvantol/djconnect", str(self.prompt), "REPAIR_AGENT", branch="codex/verified-commit"
        )
        recorded = runner._record_verified_result_commit(
            state,
            AgentResult("WAITING", branch="codex/verified-commit", commit_sha="b" * 40),
            phase="REPAIR_AGENT",
            description="pull_request_repair_commit_verified",
        )
        self.assertEqual(len(recorded.commit_evidence), 1)
        self.assertEqual(recorded.commit_evidence[0]["commit_sha"], "b" * 40)
        self.assertTrue(is_valid_commit_evidence_record(recorded.commit_evidence[0]))
        self.assertFalse(is_valid_commit_evidence_record({
            **recorded.commit_evidence[0], "description": "unverified arbitrary text",
        }))
        self.store.save(recorded)
        self.assertEqual(self.store.load("commit-evidence").commit_evidence, recorded.commit_evidence)

        repository.evidence = RepositoryEvidence(
            "pcvantol/djconnect", "codex/verified-commit", "c" * 40, True, False
        )
        rejected = runner._record_verified_result_commit(
            recorded,
            AgentResult("WAITING", branch="codex/verified-commit", commit_sha="b" * 40),
            phase="QUALITY_CONTROL_AGENT",
            description="quality_control_commit_verified",
        )
        self.assertEqual(len(rejected.commit_evidence), 1)

    def test_repair_timeout_is_a_valid_durable_audit_outcome(self) -> None:
        state = TransactionState(
            "repair-timeout", "pcvantol/djconnect", str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/repair-timeout",
            pull_request=24, owner_authorized=True,
        )
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), DeadlineFakeAgent(), lambda _: None,
        )

        blocked = runner._repair(state, "Ruff failed. Repair only the bounded transaction defects.")

        self.assertEqual(blocked.phase, "BLOCKED")
        self.assertEqual(blocked.next_action, "repair_agent_timeout")
        self.assertEqual(blocked.repair_audit[0]["outcome"], "agent_timed_out")
        self.assertEqual(self.store.load("repair-timeout").repair_audit[0]["outcome"], "agent_timed_out")

    def test_implementation_timeout_becomes_a_durable_provider_failure(self) -> None:
        state = TransactionState(
            "implementation-timeout", "pcvantol/djconnect", str(self.prompt),
            "EXECUTE_AGENT", branch="codex/implementation-timeout", owner_authorized=True,
        )
        agent = DeadlineFakeAgent()
        runner = EngineeringRunner(
            self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None,
        )

        with self.assertRaises(CodexInvocationError) as raised:
            runner._invoke_agent_with_timing(state, "bounded implementation")

        self.assertEqual(raised.exception.terminal_condition, "provider_invocation_timeout")
        self.assertIn("15-minute", str(raised.exception))
        self.assertIsNone(agent.deadline_callback)

    def test_finalization_pr_behind_main_enters_same_bounded_repair_loop(self) -> None:
        state = TransactionState(
            "behind-finalization", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="codex/finalize-behind-finalization", pull_request=949,
            finalization_branch="codex/finalize-behind-finalization", finalization_pull_request=949,
            transaction_kind="FINALIZATION", owner_authorized=True,
        )
        behind = PullRequestEvidence(949, "OPEN", True, True, head_branch=state.branch, base_branch="main", merge_state_status="BEHIND")
        agent = SequencedFakeAgent([AgentResult("BLOCKED", state.branch, 949, diagnostic="Repair needs owner input.")])
        recovered = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([behind]), agent, lambda _: None)._poll(state)
        self.assertEqual(recovered.repair_iterations, 1)
        self.assertIn("behind or cannot merge cleanly", agent.prompts[0])
        self.assertEqual(recovered.repair_audit[0]["failed_checks"], "pull request is behind or cannot merge cleanly with main")

    def test_finalization_handoff_timeout_recovers_existing_pr_without_another_agent(self) -> None:
        branch = "codex/finalize-timeout-recovery"
        state = TransactionState(
            "timeout-recovery", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT",
            implementation_pull_request=44, implementation_merge_commit="b" * 40,
            owner_authorized=True,
        )
        candidate = PullRequestEvidence(
            45, "OPEN", True, True, head_branch=branch, base_branch="main",
        )
        github = FakeGitHub([candidate], branch_response=candidate)
        agent = DeadlineFakeAgent()
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, agent, lambda _: None)

        recovered = runner._start_finalization(state, 44)

        self.assertEqual(recovered.finalization_pull_request, 45)
        self.assertEqual(recovered.pull_request, 45)
        self.assertEqual(github.branch_calls, [branch])
        self.assertIsNone(agent.deadline_callback)

    def test_repair_stops_after_three_failed_required_check_repairs(self) -> None:
        state = TransactionState(
            "repair-limit-run",
            "pcvantol/djconnect",
            str(self.prompt),
            "WAIT_FOR_TERMINAL_EVIDENCE",
            branch="codex/repair-limit",
            pull_request=25,
            owner_authorized=True,
            repair_iterations=3,
        )
        github = FakeGitHub([
            PullRequestEvidence(25, "OPEN", True, False, failed_checks=("browser-dashboard",)),
        ])
        agent = FakeAgent(AgentResult("COMPLETE", "codex/repair-limit", 25))
        stopped = EngineeringRunner(self.root, self.store, FakeRepository(), github, agent, lambda _: None)._poll(state)

        self.assertEqual(stopped.phase, "BLOCKED")
        self.assertTrue(stopped.terminal)
        self.assertEqual(stopped.next_action, "repair_attempt_limit_reached")
        self.assertEqual(stopped.terminal_condition, "repair_attempt_limit_reached")
        self.assertEqual(stopped.repair_iterations, 3)
        self.assertIn("browser-dashboard", stopped.diagnostic or "")
        self.assertEqual(agent.prompts, [])

    def test_completion_summary_contains_lifecycle_evidence(self) -> None:
        state = TransactionState("summary-run", "pcvantol/djconnect", str(self.prompt), "COMPLETE", owner_authorized=True, implementation_pull_request=21, implementation_merge_commit="b" * 40, finalization_pull_request=22, finalization_merge_commit="c" * 40, terminal=True)
        summary = format_management_summary(state)
        self.assertIn("IMPLEMENTATION_AND_FINALIZATION_RECONCILED", summary)
        self.assertIn("PR=21", summary)
        self.assertIn("PR=22", summary)
        self.assertIn("Repository Cleanup", summary)

    def test_cleanup_removes_only_transaction_branches_after_finalization(self) -> None:
        repository = FakeRepository()
        state = TransactionState("cleanup-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", transaction_kind="FINALIZATION", owner_authorized=True, implementation_branch="codex/implementation", finalization_branch="codex/final", finalization_merge_commit="c" * 40, pull_request=25)
        github = FakeGitHub([PullRequestEvidence(25, "MERGED", True, True, "c" * 40)])
        result = EngineeringRunner(self.root, self.store, repository, github, FakeAgent(AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha="a" * 40)), lambda _: None)._poll(state)
        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(repository.cleanup_calls, [("codex/implementation", "codex/final")])

    def test_cleanup_failure_is_blocked_and_resumable(self) -> None:
        repository = FakeRepository()
        repository.cleanup_error = RunnerError("Cleanup blocked: transaction branch codex/final has unmerged commits.")
        state = TransactionState("cleanup-blocked", "pcvantol/djconnect", str(self.prompt), "REPOSITORY_CLEANUP", transaction_kind="FINALIZATION", finalization_merge_commit="c" * 40)
        result = EngineeringRunner(self.root, self.store, repository, FakeGitHub([]), FakeAgent(AgentResult("WAITING")), lambda _: None)._cleanup(state)
        self.assertEqual(result.phase, "BLOCKED")
        self.assertIn("unmerged commits", result.diagnostic or "")

    def test_transient_polling_failure_preserves_non_terminal_state(self) -> None:
        state = TransactionState("retry-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=13)
        github = FakeGitHub([RunnerError("temporary GitHub outage")] * 3)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        self.store.save(state)
        runner.active_lease = acquire_lease(
            self.root, state.run_id, identity="test-host", instance_id="test-instance"
        )
        result = runner._poll(state)
        self.assertEqual(result.phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertFalse(result.terminal)
        self.assertEqual(result.next_action, "retry_github_evidence")
        self.assertEqual(lease_liveness(self.root, state.run_id)["lease_state"], "RELEASED")

    def test_dirty_workspace_has_no_agent_or_destructive_action(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(clean=False), FakeGitHub([]), agent, lambda _: None)
        with self.assertRaisesRegex(RunnerError, "not clean"):
            runner.run(self.prompt, run_id="dirty-run")
        self.assertEqual(agent.prompts, [])

    def test_engineering_platform_accepts_newer_compatible_runner(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")
        validate_compatibility(manifest, RunnerCompatibility(runner_version="2.1.0"), "0.146.0")

    def test_default_runner_compatibility_accepts_the_repository_manifest(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
        )
        validate_compatibility(manifest, RunnerCompatibility(), "0.146.0")

    def test_current_storage_schema_is_admitted_for_retry_children(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
        )
        self.assertEqual(manifest.storage_schema, ENGINEERING_STORAGE_SCHEMA_VERSION)
        validate_compatibility(
            manifest, RunnerCompatibility(storage_schemas=frozenset({ENGINEERING_STORAGE_SCHEMA_VERSION})), "0.146.0"
        )

    def test_incompatible_admitted_storage_schema_is_rejected_before_state_is_saved(self) -> None:
        compatibility = RunnerCompatibility(
            platform_version="2.0.0",
            runner_version="2.0.0",
            bootstrap_contract="2026.12",
            checkpoint_formats=frozenset({1}),
            memory_formats=frozenset({2}),
            report_formats=frozenset({2}),
            storage_schemas=frozenset({2}),
        )
        runner = EngineeringRunner(
            self.root,
            self.store,
            FakeRepository(),
            FakeGitHub([]),
            FakeAgent(AgentResult("COMPLETE")),
            lambda _: None,
            compatibility=compatibility,
        )

        with self.assertRaisesRegex(RunnerError, "Engineering storage schema mismatch"):
            runner.run(self.prompt, run_id="schema-rollout")

        self.assertFalse((self.root / ".engineering" / "engineering.db").exists())

    def test_engineering_platform_rejects_incompatible_platform_version(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")
        with self.assertRaisesRegex(EngineeringPlatformCompatibilityError, "Engineering Platform mismatch"):
            validate_compatibility(manifest, RunnerCompatibility(platform_version="0.9.0"), "0.146.0")

    def test_engineering_platform_rejects_bootstrap_checkpoint_memory_and_report_mismatches(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")
        cases = (
            (RunnerCompatibility(bootstrap_contract="2026.06"), "Bootstrap contract mismatch"),
            (RunnerCompatibility(checkpoint_formats=frozenset({2})), "Checkpoint format mismatch"),
            (RunnerCompatibility(memory_formats=frozenset({1})), "Engineering Memory format mismatch"),
            (RunnerCompatibility(report_formats=frozenset({1})), "Report format mismatch"),
            (RunnerCompatibility(storage_schemas=frozenset({2})), "Engineering storage schema mismatch"),
        )
        for compatibility, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(EngineeringPlatformCompatibilityError, diagnostic):
                validate_compatibility(manifest, compatibility, "0.146.0")

    def test_engineering_platform_rejects_older_runner(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")
        with self.assertRaisesRegex(EngineeringPlatformCompatibilityError, "Runner version mismatch"):
            validate_compatibility(manifest, RunnerCompatibility(runner_version="0.9.0"), "0.146.0")

    def test_terminal_report_records_engineering_platform(self) -> None:
        state = TransactionState("platform-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        report = generate_terminal_report(
            self.root,
            state,
            EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"),
            "0.146.0",
            runtime_metadata={
                "runtime_provider": "codex_cli",
                "model": "gpt-5.6-terra",
                "reasoning_profile": "medium",
                "configuration_profile": "workspace-write",
                "codex_cli_installation_path": "/managed/engineering-platform/codex-cli",
            },
        )
        body = report.read_text(encoding="utf-8")
        self.assertIn("Platform Version: `2.0.0`", body)
        self.assertIn("Runtime Provider: `codex_cli`", body)
        self.assertIn("AI Model: `gpt-5.6-terra`", body)
        self.assertIn("Reasoning Profile: `medium`", body)
        self.assertIn("Configuration Profile: `workspace-write`", body)
        self.assertIn("Codex CLI Version: `0.146.0`", body)
        self.assertIn("Codex CLI Installation Path: `/managed/engineering-platform/codex-cli`", body)

    def test_terminal_report_does_not_render_submitted_expected_results_as_summary(self) -> None:
        self.prompt.write_text(
            "DASHBOARD VALIDATION PROOF\n\n"
            "engineering_python: PASS\n"
            "Required Validation State: PASS\n"
            "Run Qualification: PASS / QUALIFIED\n",
            encoding="utf-8",
        )
        state = TransactionState(
            "non-authoritative-prompt", "pcvantol/djconnect", str(self.prompt), "BLOCKED", terminal=True,
        )

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")

        self.assertIn(
            "- Objective: Submitted runtime prompt retained at the supplied prompt path; non-authoritative input.",
            body,
        )
        self.assertIn(
            f"- Submitted Prompt Characters: `{len(self.prompt.read_text(encoding='utf-8').strip())}`",
            body,
        )
        self.assertIn("- Terminal state: `BLOCKED`", body)
        self.assertNotIn("engineering_python: PASS", body)
        self.assertNotIn("Required Validation State: PASS", body)
        self.assertNotIn("Run Qualification: PASS / QUALIFIED", body)

    def test_terminal_report_includes_bounded_local_validation_audit(self) -> None:
        audit = ({
            "iteration": "1",
            "observed_at": "2026-08-28T10:00:00+00:00",
            "failed_checks": "Required local suite",
            "proposed_action": "Repair the bounded test failure.",
            "agent_summary": "Repaired the local test fixture.",
            "commit_sha": "a" * 40,
            "outcome": "validated",
        },)
        state = TransactionState(
            "local-validation-report",
            "pcvantol/djconnect",
            str(self.prompt),
            "BLOCKED",
            terminal=True,
            local_validation_iterations=1,
            local_validation_audit=audit,
        )

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")

        self.assertIn("## Local Repository Validation History", body)
        self.assertIn("### Local validation iteration 1", body)
        self.assertIn("Required local suite", body)
        self.assertIn("Repair the bounded test failure.", body)

    def test_terminal_report_labels_cumulative_input_without_calling_it_context(self) -> None:
        state = TransactionState("provider-usage-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=state.run_id,
                ordinal=1,
                provider="codex_cli",
                model="gpt-5.6-terra",
                phase="PROVIDER_EXECUTION",
                role="agent",
                started_at="2026-08-18T12:00:00Z",
                completed_at="2026-08-18T12:00:01Z",
                duration_ms=1000,
                usage={"input_tokens": 400, "cached_input_tokens": 100, "output_tokens": 20},
                churn={
                    "context_scope_policy": "provider-context-v1",
                    "context_scope_initial": "NORMAL",
                    "context_scope_effective": "NORMAL",
                    "context_escalation_count": 0,
                },
            ),
        )

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")

        self.assertIn("- Run Cumulative Input Tokens: `400`", body)
        self.assertIn("- Maximum Provider Invocation Cumulative Input: `400`", body)
        self.assertIn("- Actual Single-Request Context Size: `UNAVAILABLE`", body)
        self.assertIn("- Active Context Size: `UNAVAILABLE`", body)
        self.assertIn("## Provider Context Scope", body)
        self.assertIn("- Policy: `provider-context-v1`", body)
        self.assertIn("- Initial Scope: `NORMAL`", body)
        self.assertIn("- Context Escalations: `0`", body)
        self.assertIn("- Historical PRs Inspected: `UNAVAILABLE`", body)
        self.assertNotRegex(
            body,
            r"(?mi)^-\s*.*(?:context size|active context|request context).*:\s*`400`",
        )

    def test_terminal_report_omits_timing_categories_that_did_not_occur(self) -> None:
        phase = start_phase(self.root, "timing-report", "HOST_PREFLIGHT", monotonic_clock=0)
        complete_phase(self.root, phase, monotonic_clock=1)
        state = TransactionState("timing-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Execution Phase Timing", body)
        self.assertNotIn("- Provider execution:", body)
        self.assertNotIn("- Validation:", body)
        self.assertNotIn("- External wait:", body)
        self.assertNotIn("- Queue wait:", body)

    def test_terminal_report_projects_producer_contract_without_forge_implementation(self) -> None:
        self.prompt.write_text(
            "Producer ID: forge\nProducer Type: FORGE\nProducer Version: 2.0\n"
            "Producer Correlation ID: corr-42\nMission ID: MISSION-0003\n"
            "Engineering Action ID: EA-0042\nExecution Constraint Version: 1.0\n",
            encoding="utf-8",
        )
        state = TransactionState("producer-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Producer", body)
        self.assertIn("- Producer Type: `FORGE`", body)
        self.assertIn("- Mission ID: `MISSION-0003`", body)
        self.assertIn("- Engineering Action ID: `EA-0042`", body)
        self.assertIn("- Execution lifecycle: `COMPLETE`", body)
        self.assertIn("- Execution liveness:", body)
        self.assertIn("- Recovery action:", body)

    def test_terminal_report_projects_persisted_forge_governance_handoff_without_governance_mutation(self) -> None:
        from engineering_platform.storage import record_submission
        record_submission(
            self.root, submission_id="submission-handoff", producer_id="forge", producer_type="FORGE",
            prompt_content="bounded", prompt_metadata={}, target_identity={}, original_envelope={},
            received_at="2026-08-15T00:00:00+00:00", link_run_id="forge-handoff",
            forge_governance_handoff={"version": "1.0", "recommendation_set": {"id": "set-1", "count": 2},
                "selected_recommendation": {"recommendation_id": "REC-1", "title": "Mission Aurora", "rank": 1, "lifecycle_status": "RECOMMENDED", "confidence": "0.91", "business_value": "High", "dependencies": ["DEC-7"]},
                "alternatives": [{"recommendation_id": "REC-2", "title": "Mission Borealis", "rank": 2, "lifecycle_status": "PROPOSED", "confidence": "0.7"}],
                "decision_evidence": {"id": "DEC-7", "type": "RANKING", "timestamp": "2026-08-15T00:00:00Z"},
                "governance": {"business_approval_state": "PENDING"}},
        )
        state = TransactionState("forge-handoff", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Forge Governance Handoff", body)
        self.assertIn("- Recommendation Set ID: `set-1`", body)
        self.assertIn("- Selected Recommendation ID: `REC-1`", body)
        self.assertIn("- Recommendation ID: `REC-2`; Rank: 2", body)
        self.assertNotIn("NOT PERFORMED BY ENGINEERING PLATFORM", body)

    def test_retry_report_records_immutable_execution_lineage(self) -> None:
        self.prompt.write_text(
            "Retry-Of: inbox-original\nOriginal-Run-ID: inbox-original\nRetry-Generation: 1\n"
            "Retry-Timestamp: 2026-08-03T12:00:00+00:00\n# Retry objective\n",
            encoding="utf-8",
        )
        state = TransactionState("inbox-retry", "pcvantol/djconnect", str(self.prompt), "BLOCKED", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Retry Relationship", body)
        self.assertIn("- Retry Of: `inbox-original`", body)
        self.assertIn("- Original Run: `inbox-original`", body)
        self.assertIn("- Retry Generation: `1`", body)
        self.assertIn("- Current Run: `inbox-retry`", body)

    def test_runtime_metadata_uses_only_cli_reported_values(self) -> None:
        metadata = extract_codex_runtime_metadata(
            '{"type":"turn.started","model":"gpt-5.6-terra","reasoning_effort":"medium","fast_mode":true}\n',
            "model: gpt-5.6-sol\n",
        )
        self.assertEqual(metadata["runtime_provider"], "codex_cli")
        self.assertEqual(metadata["raw_provider_model"], "gpt-5.6-terra")
        self.assertEqual(metadata["reasoning_profile"], "medium")
        self.assertEqual(metadata["fast_mode"], "fast")

    def test_runtime_metadata_accepts_only_structured_known_model_events(self) -> None:
        for model in ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"):
            with self.subTest(model=model):
                metadata = extract_codex_runtime_metadata(
                    json.dumps({"type": "turn.completed", "metadata": {"model": model}})
                )
                self.assertEqual(metadata["raw_provider_model"], model)
        self.assertEqual(
            extract_codex_runtime_metadata('{"type":"item.completed","item":{"type":"agent_message","text":"model: gpt-5.6-terra"}}'),
            {"runtime_provider": "codex_cli"},
        )

    def test_successful_report_prioritizes_final_repository_outcome(self) -> None:
        state = TransactionState(
            "outcome-report",
            "pcvantol/djconnect",
            str(self.prompt),
            "COMPLETE",
            implementation_merge_commit="a" * 40,
            latest_repository_evidence="branch=main; clean=True",
            terminal=True,
        )
        records = (
            {
                "reviewer": "documentation",
                "selected_because": "documentation-oriented objective",
                "contribution": "The capability does not yet exist.",
                "accepted_recommendations": 1,
                "rejected_recommendations": 0,
                "failed": False,
            },
        )
        report = generate_terminal_report(
            self.root,
            state,
            EngineeringPlatformManifest.load(
                self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
            ),
            "0.146.0",
            records,
        )
        body = report.read_text(encoding="utf-8")
        self.assertIn("## Initial Repository Assessment", body)
        self.assertIn("## Engineering Outcome", body)
        self.assertIn("## Reviewer Findings", body)
        self.assertIn("## Repository Truth", body)
        self.assertIn("Initial observation: The capability does not yet exist.", body)
        self.assertIn("Resolved by: implementation evidence", body)
        self.assertIn("Resulting commits: implementation `" + "a" * 40, body)
        self.assertIn("Repository state: branch=main; clean=True", body)
        self.assertTrue(terminal_report_matches_state(body, state))

    def test_complete_managed_report_exposes_target_identity_and_evidence_bundle(self) -> None:
        subprocess.run(("git", "init", "-b", "main", str(self.root)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.email", "report@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.name", "Report Test"), check=True)
        (self.root / "modified.txt").write_text("before\n", encoding="utf-8")
        (self.root / "removed.txt").write_text("remove\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "baseline"), check=True, capture_output=True)
        (self.root / "modified.txt").write_text("after\n", encoding="utf-8")
        (self.root / "added.txt").write_text("added\n", encoding="utf-8")
        (self.root / "removed.txt").unlink()
        subprocess.run(("git", "-C", str(self.root), "add", "-A"), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "implementation"), check=True, capture_output=True)
        commit = subprocess.check_output(("git", "-C", str(self.root), "rev-parse", "HEAD"), text=True).strip()
        state = TransactionState(
            "managed-evidence", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
            implementation_merge_commit=commit, terminal=True,
        )
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Execution Target Identity", body)
        self.assertIn("Execution Host Repository: `pcvantol/djconnect`", body)
        self.assertIn(f"Target Commit: `{commit}`", body)
        self.assertIn("## Evidence Bundle", body)
        self.assertIn("File added: `added.txt`", body)
        self.assertIn("File modified: `modified.txt`", body)
        self.assertIn("File removed: `removed.txt`", body)
        self.assertIn("git diff --check result: PASS", body)

    def test_complete_report_renders_structured_validation_evidence(self) -> None:
        state = TransactionState(
            "validation-evidence", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
            validation_evidence=({"command": "python -m unittest tests.engineering", "result": "passed (12 tests)"},),
            terminal=True,
        )
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("Executed test: `python -m unittest tests.engineering`", body)
        self.assertIn("Result: passed (12 tests)", body)

    def test_complete_report_projects_validation_controls_and_unambiguous_timing(self) -> None:
        state = TransactionState(
            "validation-controls", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
            validation_evidence=(
                {"command": "ruff check tools", "result": "passed"},
                {"command": "semgrep scan", "result": "not applicable"},
            ),
            terminal=True,
        )
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Validation Control Results", body)
        self.assertIn("Ruff: `PASS` — `LOCAL`", body)
        self.assertIn("CodeQL: `NOT_EXECUTED` — `GITHUB_CI`", body)
        self.assertIn("Semgrep: `NOT_APPLICABLE` — `GITHUB_CI`", body)
        self.assertIn("Transaction Baseline Availability:", body)
        self.assertIn("Execution Duration (legacy): Provider Execution Time.", body)

    def test_complete_report_keeps_an_executed_dashboard_control_out_of_not_executed(self) -> None:
        run_id = "dashboard-execution-evidence"
        record_validation_profile(
            self.root, run_id=run_id, selected_validation_tier="DASHBOARD", validation_profile_version="1.0",
            required_validation_controls=("dashboard_browser",), recorded_at="2026-08-28T00:00:00+00:00",
        )
        record_validation_control_result(
            self.root, run_id=run_id, validation_id="dashboard_browser", category="agent",
            control_identity="npm run test:engineering-dashboard", required_for_profile=True,
            execution_status="EXECUTED", result="UNAVAILABLE", evidence_ref="agent_result",
            observed_at="2026-08-28T00:00:01+00:00", currentness=0,
        )
        state = TransactionState(run_id, "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("Required control dashboard_browser: `UNAVAILABLE` — `PERSISTED_PROFILE`", body)
        self.assertIn("Execution status: `EXECUTED`.", body)
        self.assertIn("Execution inclusion: `AVAILABLE`.", body)

    def test_complete_report_does_not_project_focused_or_diagnostic_commands_as_dashboard(self) -> None:
        for command in (
            "python3 -m unittest tests.engineering.test_dashboard_browser_validation",
            "pgrep -f 'playwright dashboard_browser_validation'",
        ):
            with self.subTest(command=command):
                state = TransactionState(
                    f"dashboard-unrelated-{len(command)}", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
                    validation_evidence=({"command": command, "result": "passed"},), terminal=True,
                )
                body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
                self.assertIn("Dashboard/browser tests: `NOT_EXECUTED` — `LOCAL`", body)
                self.assertIn("Execution inclusion: `UNAVAILABLE`.", body)

    def test_engineering_evidence_2_report_is_self_validating_and_traceable(self) -> None:
        self.prompt.write_text(
            "# Engineering Evidence 2.0\n\n## Deliverable\n\n- Produce a self-validating report.\n\n"
            "## Tests\n\n- Add report regression coverage.\n\n## Validation\n\n- PASS when report consistency validation succeeds.\n",
            encoding="utf-8",
        )
        state = TransactionState(
            "evidence-2", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
            branch="codex/evidence-2", implementation_branch="codex/evidence-2",
            validation_evidence=({"command": "python -m unittest tests.engineering", "result": "passed"},),
            agent_execution_seconds=12.5, terminal=True,
        )
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        for section in (
            "## Component Inventory",
            "## Deliverable Projection",
            "## Qualification Projection",
            "## Runtime Projection",
            "## Execution Receipt Projection",
            "## Decision Evidence Projection",
            "## Statistics Projection",
            "## Deliverable Answer",
            "## Commit Strategy",
            "## Branch Traceability",
            "## Requirement Traceability",
            "## Validation Traceability",
            "## Execution Statistics",
            "## Engineering Evidence Summary",
        ):
            self.assertIn(section, body)
        self.assertIn("YES / PASS / GO", body)
        self.assertIn("Requirement: Produce a self-validating report.", body)
        self.assertIn("Runtime evidence: run `evidence-2`; execution mode `MANAGED`.", body)
        self.assertIn("Execution Status: `COMPLETE`", body)
        self.assertIn("Receipt ID: `evidence-2`", body)
        self.assertIn("### Mission Statistics", body)
        self.assertIn("Executed Validation Command: `Documentation validation`", body)
        self.assertIn('"deliverable_answer": "YES / PASS / GO', body)

    def test_component_inventory_is_derived_from_implementation_evidence(self) -> None:
        subprocess.run(("git", "init", "-b", "main", str(self.root)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.email", "report@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.name", "Report Test"), check=True)
        implementation = self.root / "src" / "engineering_platform" / "execution_host.py"
        implementation.parent.mkdir(parents=True, exist_ok=True)
        implementation.write_text("before\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "baseline"), check=True, capture_output=True)
        implementation.write_text("after\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "."), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "report component"), check=True, capture_output=True)
        commit = subprocess.check_output(("git", "-C", str(self.root), "rev-parse", "HEAD"), text=True).strip()
        state = TransactionState("component-inventory", "pcvantol/djconnect", str(self.prompt), "COMPLETE", implementation_merge_commit=commit, terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("Component: `Engineering Report Generator`", body)
        self.assertIn("Repository file: `src/engineering_platform/execution_host.py`", body)

    def test_report_consistency_validation_rejects_missing_evidence_2_sections(self) -> None:
        state = TransactionState("inconsistent-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        errors = report_consistency_errors(
            "# Engineering Report\n", state, collect_terminal_evidence(self.root, state), "PASS"
        )
        self.assertIn("missing required section: ## Component Inventory", errors)
        self.assertIn("missing required section: ## Deliverable Projection", errors)
        self.assertIn("missing required section: ## Qualification Projection", errors)
        self.assertIn("missing required section: ## Statistics Projection", errors)
        self.assertIn("explicit deliverable answer is missing", errors)

    def test_report_consistency_rejects_contradictory_fresh_submission(self) -> None:
        state = TransactionState("lineage-conflict", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        bundle = collect_terminal_evidence(self.root, state)
        body = "\n".join((
            "## Component Inventory", "## Deliverable Projection", "## Qualification Projection",
            "## Runtime Projection", "## Execution Receipt Projection", "## Decision Evidence Projection",
            "## Statistics Projection", "## Commit Strategy", "## Branch Traceability", "## Requirement Traceability",
            "## Validation Traceability", "## Execution Statistics", "## Engineering Evidence Summary",
            "## Evidence Bundle", f"{bundle.target_commit}", "- Fresh Submission: `YES`",
            "- Retry Parent: `inbox-parent`", "- Resume Parent: `NONE`",
        ))
        self.assertIn("fresh submission conflicts with retry or resume parent", report_consistency_errors(body, state, bundle, ""))

    def test_complete_genesis_report_keeps_host_and_target_identities_distinct(self) -> None:
        target = self.root / "genesis-report-target"
        target.mkdir()
        subprocess.run(("git", "init", "-b", "main", str(target)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(target), "config", "user.email", "report@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(target), "config", "user.name", "Report Test"), check=True)
        subprocess.run(("git", "-C", str(target), "remote", "add", "origin", "git@github.com:pcvantol/forge.git"), check=True)
        (target / "README.md").write_text("# target\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(target), "add", "."), check=True)
        subprocess.run(("git", "-C", str(target), "commit", "-m", "genesis"), check=True, capture_output=True)
        commit = subprocess.check_output(("git", "-C", str(target), "rev-parse", "HEAD"), text=True).strip()
        self.prompt.write_text("Reconciliation required\n", encoding="utf-8")
        state = TransactionState(
            "genesis-evidence", "pcvantol/djconnect", str(self.prompt), "COMPLETE",
            execution_mode="GENESIS", genesis_repository_path=str(target), genesis_commit_sha=commit,
            terminal=True,
        )
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("Execution Host Repository: `pcvantol/djconnect`", body)
        self.assertIn("Target Repository: `pcvantol/forge`", body)
        self.assertIn(f"Target Commit: `{commit}`", body)
        self.assertIn("## Reconciliation Evidence", body)
        self.assertIn("Initial classification:", body)
        self.assertIn("Final classification: `COMPLETE`", body)

    def test_assessment_only_report_does_not_claim_delivery(self) -> None:
        state = TransactionState(
            "assessment-report",
            "pcvantol/djconnect",
            str(self.prompt),
            "BLOCKED",
            diagnostic="Repository preflight requires attention.",
            terminal=True,
        )
        report = generate_terminal_report(self.root, state)
        body = report.read_text(encoding="utf-8")
        self.assertIn("## Initial Repository Assessment", body)
        self.assertIn("Completed work: no successful engineering delivery is claimed.", body)
        self.assertIn("BLOCKED — no engineering changes were executed or delivered.", body)
        self.assertTrue(terminal_report_matches_state(body, state))

    def test_blocked_post_merge_report_does_not_discard_verified_implementation_evidence(self) -> None:
        state = TransactionState(
            "post-merge-blocked",
            "pcvantol/djconnect",
            str(self.prompt),
            "BLOCKED",
            implementation_pull_request=908,
            implementation_merge_commit="a" * 40,
            diagnostic="Finalization requires a clean, synchronized main checkout.",
            terminal=True,
        )

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")

        self.assertIn(
            "BLOCKED — implementation merge was verified, but Finalization and end reconciliation did not complete.",
            body,
        )
        self.assertIn(
            "Completed work: implementation merge was verified; this is not a complete delivery.",
            body,
        )
        self.assertNotIn("BLOCKED — no engineering changes were executed or delivered.", body)
        self.assertTrue(terminal_report_matches_state(body, state))

    def test_post_merge_workspace_drift_waits_for_safe_finalization_retry(self) -> None:
        repository = FakeRepository(clean=False, branch="feature/other")
        runner = EngineeringRunner(
            self.root, self.store, repository, FakeGitHub([]), FakeAgent(AgentResult("COMPLETE")), lambda _: None
        )
        state = TransactionState(
            "post-merge-sync-wait", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_OPERATOR_MERGE",
            owner_authorized=True, implementation_pull_request=908,
            implementation_merge_commit="a" * 40,
        )

        result = runner._start_finalization(state, 908)

        self.assertEqual(result.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.assertFalse(result.terminal)
        self.assertEqual(result.next_action, "await_clean_synchronized_main")
        self.assertEqual(result.terminal_condition, "post_merge_workspace_sync_required")

    def test_blocked_report_projects_structured_development_host_drift(self) -> None:
        status = self.root / ".engineering" / "status"
        status.mkdir(parents=True, exist_ok=True)
        (status / "host_preflight.json").write_text(json.dumps({
            "run_id": None,
            "drift_evidence": [{
                "drift_id": "drift-report", "category": "Runtime Database", "severity": "BLOCKING",
                "expected_value": "telemetry_storage: PASS", "observed_value": "Telemetry SQLite storage is unavailable.",
                "resolution_recommendation": "Restore local SQLite evidence storage before accepting work.",
                "affected_component": "telemetry_storage", "affected_repository": "/workspace",
                "affected_runtime": "Engineering Platform",
            }],
        }), encoding="utf-8")
        state = TransactionState("drift-report", "pcvantol/djconnect", str(self.prompt), "BLOCKED", terminal=True)
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        self.assertIn("## Development Host Drift Diagnostics", body)
        self.assertIn("Expected State: telemetry_storage: PASS", body)
        self.assertIn("Observed State: Telemetry SQLite storage is unavailable.", body)
        self.assertIn("Recommended Resolution / Required Action", body)
        self.assertIn("resume is not appropriate", body)

    def test_blocked_and_failed_reports_match_the_terminal_checkpoint(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")
        for phase, expected in (
            ("BLOCKED", "BLOCKED — no engineering changes were executed or delivered."),
            ("FAILED", "FAILED — the engineering transaction did not complete successfully."),
        ):
            with self.subTest(phase=phase):
                state = TransactionState(f"{phase.lower()}-report", "pcvantol/djconnect", str(self.prompt), phase, diagnostic="Bounded diagnostic.", terminal=True)
                report = generate_terminal_report(self.root, state, manifest, "0.146.0")
                body = report.read_text(encoding="utf-8")
            self.assertIn(expected, body)
            self.assertNotIn("COMPLETE —", body)
            self.assertIn("## Engineering Outcome", body)
            self.assertIn("## Reviewer Findings", body)
            self.assertTrue(terminal_report_matches_state(body, state))

    def test_capability_selection_covers_documentation_validation_governance_and_finalization(self) -> None:
        selections = select_reviewers("Update governance documentation and validation diagnostics.", self.prompt, "FINALIZATION", {})
        self.assertEqual(tuple(item.reviewer for item in selections), ("repository_governance", "validation", "documentation", "finalization"))

    def test_capability_selection_uses_memory_confidence_and_allows_no_reviewer(self) -> None:
        memory = {"reviewers": [{"reviewer": "documentation", "future_confidence": 0.4}]}
        documented = select_reviewers("documentation", self.prompt, "IMPLEMENTATION", memory)
        self.assertEqual(documented[0].confidence, 0.9)
        self.assertEqual(select_reviewers("binary objective", Path("objective.txt"), "IMPLEMENTATION", {}), ())

    def test_parallel_reviews_are_advisory_and_reconcile_conflicts(self) -> None:
        selections = select_reviewers("governance documentation validation", self.prompt, "IMPLEMENTATION", {})
        reviewer = FakeReviewer()
        results = run_reviews(self.root, selections, "objective", reviewer)
        self.assertEqual(len(results), 3)
        self.assertEqual(reconciled_recommendations(results), ("Use canonical wording.",))
        records = records_for_storage(selections, results)
        self.assertEqual(records[0]["accepted_recommendations"], 1)
        self.assertEqual(records[0]["codex_commands_executed"], 0)

    def test_reviewer_record_keeps_its_own_safe_command_count(self) -> None:
        selection = select_reviewers("documentation", self.prompt, "IMPLEMENTATION", {})
        result = ReviewerResult(
            "documentation", "Review complete.", churn={"tool_loop_operations": 4}
        )

        records = records_for_storage(selection, (result,))

        self.assertEqual(records[0]["codex_commands_executed"], 4)

    def test_reviewer_prompt_reuses_bounded_run_scoped_facts_without_conclusions(self) -> None:
        selection = select_reviewers("validation", self.prompt, "IMPLEMENTATION", {})[0]
        evidence = ReviewerEvidence.from_repository(
            "inbox-context", "MANAGED",
            RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True),
        )
        prompt = json.loads(reviewer_prompt(selection, "objective", evidence))
        self.assertEqual(prompt["run_scoped_repository_evidence"], {
            "run_id": "inbox-context",
            "run_stable": {"repository": "pcvantol/djconnect", "execution_mode": "MANAGED"},
            "mutable": {"branch": "main", "head_sha": "a" * 40, "worktree": "clean", "main_contains_head": True},
            "boundary_sensitive": {
                "freshness_boundary": "post_synchronization_pre_reviewer_wave",
                "invalidated_by": ["repository_mutation", "validation", "pull_request_mutation", "merge", "finalization", "repository_cleanup"],
            },
        })
        self.assertIn("do not rediscover branch", prompt["evidence_instructions"])
        self.assertNotIn("recommendation", prompt["run_scoped_repository_evidence"])
        self.assertIn("one reviewer invocation", prompt["invocation_read_reuse"])
        self.assertIn("another reviewer", prompt["invocation_read_reuse"])
        self.assertIn("whenever freshness is uncertain", prompt["invocation_read_reuse"])

    def test_parallel_reviewers_share_facts_but_not_reasoning(self) -> None:
        selections = select_reviewers("governance documentation validation", self.prompt, "IMPLEMENTATION", {})
        evidence = ReviewerEvidence.from_repository(
            "inbox-context", "MANAGED",
            RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True),
        )
        reviewer = FakeReviewer()
        results = run_reviews(self.root, selections, "objective", reviewer, evidence=evidence)
        self.assertEqual(len(results), len(selections))
        self.assertEqual(reviewer.evidence, [evidence] * len(selections))

    def test_primary_prompt_receives_the_same_expiring_repository_projection(self) -> None:
        state = TransactionState("inbox-context", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        evidence = ReviewerEvidence.from_repository(
            state.run_id, state.execution_mode,
            RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True),
        )
        prompt = assemble_prompt(self.prompt, state, reviewer_evidence=evidence)
        self.assertIn('"freshness_boundary": "post_synchronization_pre_reviewer_wave"', prompt)
        self.assertIn("instead of repeating Git/GitHub discovery", prompt)
        self.assertIn("Invocation-scoped source-read reuse", prompt)
        self.assertIn("after you edit it", prompt)
        self.assertIn("Do not create a persistent source cache", prompt)
        self.assertIn("Shell reads are not host-intercepted", prompt)
        self.assertIn("Primary Invocation Investigation Ledger", prompt)
        self.assertIn('"persistence": "none"', prompt)
        self.assertIn("Prefer exact branch/HEAD/status", prompt)
        self.assertIn("Reviewer advice and primary conclusions are never ledger", prompt)

    def test_provider_prompt_receives_current_delta_first_context_scope_policy(self) -> None:
        state = TransactionState("context-scope", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT")
        prompt = assemble_prompt(self.prompt, state)
        self.assertIn("Provider Context Scope: NORMAL; Policy: provider-context-v1", prompt)
        self.assertIn("merge-base delta against canonical base", prompt)
        self.assertIn("Do not enumerate historical pull requests", prompt)

    def test_managed_finalization_prompt_returns_after_pr_handoff_without_polling(self) -> None:
        state = TransactionState(
            "finalization-handoff", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT",
            branch="codex/finalize-finalization-handoff", finalization_branch="codex/finalize-finalization-handoff",
            transaction_kind="FINALIZATION", owner_authorized=True,
        )
        prompt = assemble_prompt(self.prompt, state, managed_target=self.root)
        self.assertIn("PR hand-off boundary", prompt)
        self.assertIn("Return the required JSON object immediately", prompt)
        self.assertIn("Do not\n  poll or wait for GitHub checks", prompt)
        self.assertIn("Never create a replacement pull request", prompt)
        self.assertNotIn("Continue waiting for objective terminal repository evidence", prompt)
        self.assertEqual(prompt.count("Supplied bounded objective follows:"), 1)

    def test_managed_repair_prompt_returns_same_pr_to_host(self) -> None:
        state = TransactionState(
            "finalization-repair-handoff", "pcvantol/djconnect", str(self.prompt), "REPAIR_AGENT",
            branch="codex/finalize-finalization-repair-handoff", pull_request=949,
            finalization_branch="codex/finalize-finalization-repair-handoff", finalization_pull_request=949,
            transaction_kind="FINALIZATION", owner_authorized=True, repair_iterations=1,
        )
        prompt = assemble_prompt(self.prompt, state, managed_target=self.root)
        self.assertIn("preserve the exact checkpointed pull-request number", prompt)
        self.assertIn("at most three bounded repairs", prompt)

    def test_primary_investigation_ledger_reuses_only_current_facts(self) -> None:
        ledger = InvocationInvestigationLedger().record(
            "repository_identity", "repository_status", "source_inspection", "test_surface"
        )
        self.assertTrue(ledger.reusable("source_inspection"))
        self.assertTrue(ledger.reusable("test_surface"))
        invalidated = ledger.invalidate("repository_mutation")
        self.assertTrue(invalidated.reusable("repository_identity"))
        self.assertFalse(invalidated.reusable("repository_status"))
        self.assertFalse(invalidated.reusable("source_inspection"))
        self.assertFalse(invalidated.reusable("test_surface"))

    def test_primary_investigation_ledger_fails_closed_for_uncertain_freshness(self) -> None:
        ledger = InvocationInvestigationLedger().record(
            "repository_identity", "validation_surface", "finalization_state", "reconciliation_state"
        ).invalidate("freshness_uncertain")
        self.assertTrue(ledger.reusable("repository_identity"))
        self.assertFalse(ledger.reusable("validation_surface"))
        self.assertFalse(ledger.reusable("finalization_state"))
        self.assertFalse(ledger.reusable("reconciliation_state"))

    def test_primary_investigation_ledger_is_identifier_only_and_not_cross_run_memory(self) -> None:
        first_invocation = InvocationInvestigationLedger().record("source_inspection")
        projection = first_invocation.to_prompt_dict()
        self.assertEqual(projection["scope"], "one_primary_provider_invocation")
        self.assertEqual(projection["persistence"], "none")
        self.assertEqual(projection["completed_fact_ids"], ["source_inspection"])
        serialized = json.dumps(projection)
        self.assertNotIn("path", serialized)
        self.assertNotIn("output", serialized)
        self.assertNotIn("prompt", serialized)
        self.assertFalse(InvocationInvestigationLedger().reusable("source_inspection"))

    def test_reviewer_progress_reports_started_and_terminal_states(self) -> None:
        selections = select_reviewers("documentation validation", self.prompt, "IMPLEMENTATION", {})
        progress: list[tuple[str, str, bool | None]] = []

        results = run_reviews(
            self.root,
            selections,
            "objective",
            FakeReviewer(),
            progress=lambda selection, event, result: progress.append(
                (selection.reviewer, event, None if result is None else result.failed)
            ),
        )

        self.assertEqual(len(results), len(selections))
        for selection in selections:
            self.assertIn((selection.reviewer, "started", None), progress)
            self.assertIn((selection.reviewer, "completed", False), progress)

    def test_reviewer_failure_never_blocks_selection(self) -> None:
        selections = select_reviewers("documentation", self.prompt, "IMPLEMENTATION", {})
        results = run_reviews(self.root, selections, "objective", FakeReviewer(fail=True))
        self.assertTrue(results[0].failed)
        self.assertEqual(reconciled_recommendations(results), ())

    def test_terminal_report_records_selected_reviewers(self) -> None:
        state = TransactionState("review-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        records = ({"reviewer": "documentation", "selected_because": "documentation-oriented objective", "contribution": "Navigation checked.", "accepted_recommendations": 3, "rejected_recommendations": 1, "failed": False},)
        report = generate_terminal_report(self.root, state, EngineeringPlatformManifest.load(self.root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"), "0.146.0", records)
        self.assertIn("Reviewer: documentation", report.read_text(encoding="utf-8"))

    def test_product_capability_reviewers_are_selected_from_repository_evidence(self) -> None:
        cases = (
            ("apps/apple/View.swift", "apple_platform"),
            ("djconnect-windows MAUI", "windows_platform"),
            ("custom_components/djconnect config flow", "home_assistant_integration"),
            ("djconnect-esp32 ESPHome firmware yaml", "esphome_firmware"),
            ("djconnect-pi display lifecycle", "pi_renderer"),
            ("VibeCast browser receiver transport", "universal_receiver"),
            ("djconnect-website static site", "website"),
            ("djconnect-api REST API contract", "api"),
        )
        for objective, reviewer in cases:
            with self.subTest(reviewer=reviewer):
                selected = select_reviewers(objective, Path("objective.txt"), "IMPLEMENTATION", {})
                self.assertIn(reviewer, tuple(item.reviewer for item in selected))

    def test_cross_capability_selection_preserves_product_scope_and_generic_review(self) -> None:
        selected = select_reviewers("apps/apple/ integrates with djconnect-api REST API contract and validation", Path("objective.md"), "IMPLEMENTATION", {})
        reviewers = {item.reviewer: item for item in selected}
        self.assertEqual(reviewers["apple_platform"].capability, "apple_platform")
        self.assertEqual(reviewers["api"].capability, "api")
        self.assertIn("validation", reviewers)
        self.assertIn("documentation", reviewers)

    def test_engineering_qualification_registers_and_executes_all_scenarios(self) -> None:
        report = execute_qualification(self.root, {scenario.capability: True for scenario in SCENARIOS})
        self.assertEqual(report["qualification"], "PASS")
        self.assertEqual(len(report["scenarios"]), len(SCENARIOS))
        self.assertEqual(report["coverage_percent"], 100.0)
        self.assertEqual(latest_qualification(self.root)["qualification"], "PASS")
        self.assertIn(f"Scenarios: {len(SCENARIOS)} / {len(SCENARIOS)}", dashboard(report))

    def test_engineering_qualification_reports_scenario_failure_and_coverage(self) -> None:
        checks = {scenario.capability: True for scenario in SCENARIOS}
        checks["Repair Loop"] = False
        report = execute_qualification(self.root, checks)
        self.assertEqual(report["qualification"], "FAIL")
        self.assertEqual(report["failures"], 1)
        self.assertLess(report["coverage_percent"], 100.0)


class ValidationFailureDiagnosticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = "validation-diagnostic-run"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_failure_diagnostic_is_bounded_redacted_and_bound_to_command(self) -> None:
        output = "x" * 5_000 + "\nFAIL: test_retry (tests.engineering.test_retry.TestRetry.test_retry)\nAssertionError: Bearer private-token\nFAILED (failures=1)\n"
        reference = persist_validation_failure_diagnostic(
            self.root, run_id=self.run_id, command_id="command-1", validation_id="suite",
            control_identity="python3 -m unittest discover", exit_code=1,
            stdout="", stderr=output, capture_available=True,
        )
        payload = load_validation_failure_diagnostic(self.root, reference)
        self.assertEqual(reference, "artifact:" + validation_failure_artifact_id("command-1"))
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["validation_id"], "suite")
        self.assertEqual(payload["command_id"], "command-1")
        self.assertEqual(payload["failing_test_identities"], ["tests.engineering.test_retry.TestRetry.test_retry"])
        self.assertTrue(payload["stderr_truncated"])
        self.assertLessEqual(payload["retained_output_characters"], MAX_RETAINED_VALIDATION_OUTPUT_CHARACTERS)
        self.assertNotIn("private-token", payload["stderr_tail"])
        self.assertIn("AssertionError", payload["stderr_tail"])
        with open_storage(self.root) as connection:
            artifact = connection.execute(
                "SELECT run_id,execution_id FROM execution_artifact_records WHERE artifact_id=?",
                (validation_failure_artifact_id("command-1"),),
            ).fetchone()
        self.assertEqual(artifact, (None, "command-1"))
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="CUSTOM", validation_profile_version="1.0",
            required_validation_controls=("suite",), recorded_at="2026-08-29T00:00:00+00:00",
            control_bindings=({"validation_id": "suite", "required": True, "category": "test", "control_identity": "python3 -m unittest discover", "command": ["python3"]},),
        )
        record_validation_command_invocation(
            self.root, run_id=self.run_id, validation_id="suite", command_id="command-1", category="test",
            control_identity="python3 -m unittest discover", required_for_profile=True,
            started_at="2026-08-29T00:00:00+00:00", currentness=0,
        )
        record_validation_command_terminal(
            self.root, run_id=self.run_id, command_id="command-1", completed_at="2026-08-29T00:00:01+00:00", exit_code=1,
        )
        report = generate_terminal_report(
            self.root, TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True),
            EngineeringPlatformManifest.load(Path(__file__).resolve().parents[2] / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"),
        ).read_text(encoding="utf-8")
        self.assertIn("Failure Diagnostic Evidence: `" + reference + "`.", report)
        self.assertIn("tests.engineering.test_retry.TestRetry.test_retry", report)
        self.assertIn("Authoritative Exit Code: `1`.", report)

    def test_failure_diagnostic_extracts_multiple_unittest_identities(self) -> None:
        reference = persist_validation_failure_diagnostic(
            self.root, run_id=self.run_id, command_id="command-2", validation_id="suite",
            control_identity="python", exit_code=1, stdout="", stderr=(
                "ERROR: test_a (pkg.TestA.test_a)\nFAIL: test_b (pkg.TestB.test_b)\nFAILED (failures=1, errors=1)"
            ), capture_available=True,
        )
        payload = load_validation_failure_diagnostic(self.root, reference)
        self.assertEqual(payload["failing_test_identities"], ["pkg.TestA.test_a", "pkg.TestB.test_b"])
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(payload["error_count"], 1)

    def test_generic_executor_keeps_success_output_ephemeral_and_failure_authoritative(self) -> None:
        class Process:
            def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
                self.completed = completed
                self.environment: dict[str, str] | None = None

            def execute(
                self, root: Path, arguments: tuple[str, ...], *, environment: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                self.environment = environment
                return self.completed

        process = Process(subprocess.CompletedProcess(("check",), 0, "ok", ""))
        success = DeterministicValidationExecutor(process).run(self.root, ("check",))
        self.assertEqual(success.exit_code, 0)
        self.assertTrue(success.diagnostic_capture_available)
        self.assertIsNotNone(process.environment)
        self.assertTrue(str(process.environment["PATH"]).startswith(str(Path(sys.executable).resolve().parent)))
        self.assertFalse((self.root / ".engineering" / "artifacts").exists())
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="CUSTOM", validation_profile_version="1.0",
            required_validation_controls=("arbitrary_control",), recorded_at="2026-08-29T00:00:00+00:00",
            control_bindings=({"validation_id": "arbitrary_control", "required": True, "category": "lint", "control_identity": "arbitrary check", "command": ["arbitrary"]},),
        )
        runner = EngineeringRunner(self.root, StateStore(self.root / ".engineering" / "state.json"), None, None, None, lambda _: None)
        runner._run_required_validation_command = lambda _: DeterministicValidationResult(  # type: ignore[method-assign]
            exit_code=7, stdout="", stderr="permission denied", diagnostic_capture_available=True,
        )
        runner._execute_required_validation_controls(
            TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", action_intent="VALIDATION_ONLY")
        )
        control = load_validation_context(self.root, self.run_id)["controls"]["arbitrary_control"]
        self.assertEqual(control["result"], "FAIL")
        self.assertTrue(str(control["diagnostic_evidence_ref"]).startswith("artifact:validation-failure-diagnostic-required-control-"))
        payload = load_validation_failure_diagnostic(self.root, control["diagnostic_evidence_ref"])
        self.assertEqual(payload["capture_status"], "AVAILABLE")
        self.assertEqual(payload["failing_test_identities"], [])

    def test_unavailable_capture_preserves_terminal_unavailable_and_projects_report(self) -> None:
        command_id = "command-3"
        reference = persist_validation_failure_diagnostic(
            self.root, run_id=self.run_id, command_id=command_id, validation_id="suite",
            control_identity="generic deterministic command", exit_code=None, stdout=None, stderr=None,
            capture_available=False,
        )
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="CUSTOM", validation_profile_version="1.0",
            required_validation_controls=("suite",), recorded_at="2026-08-29T00:00:00+00:00",
            control_bindings=({"validation_id": "suite", "required": True, "category": "test", "control_identity": "generic deterministic command", "command": ["generic"]},),
        )
        record_validation_command_invocation(
            self.root, run_id=self.run_id, validation_id="suite", command_id=command_id, category="test",
            control_identity="generic deterministic command", required_for_profile=True,
            started_at="2026-08-29T00:00:00+00:00", currentness=0,
        )
        record_validation_command_terminal(
            self.root, run_id=self.run_id, command_id=command_id, completed_at="2026-08-29T00:00:01+00:00", exit_code=None,
        )
        control = load_validation_context(self.root, self.run_id)["controls"]["suite"]
        self.assertEqual(control["result"], "UNAVAILABLE")
        self.assertEqual(control["diagnostic_evidence_ref"], reference)
        report = generate_terminal_report(
            self.root, TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True),
            EngineeringPlatformManifest.load(Path(__file__).resolve().parents[2] / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"),
        ).read_text(encoding="utf-8")
        self.assertIn("Failure Diagnostic Evidence: `" + reference + "`.", report)
        self.assertIn("Failure Diagnostic Capture: `UNAVAILABLE`", report)

    def test_unavailable_diagnostic_capture_does_not_downgrade_a_nonzero_fail(self) -> None:
        command_id = "command-4"
        reference = persist_validation_failure_diagnostic(
            self.root, run_id=self.run_id, command_id=command_id, validation_id="suite",
            control_identity="generic deterministic command", exit_code=1, stdout=None, stderr=None,
            capture_available=False,
        )
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="CUSTOM", validation_profile_version="1.0",
            required_validation_controls=("suite",), recorded_at="2026-08-29T00:00:00+00:00",
            control_bindings=({"validation_id": "suite", "required": True, "category": "test", "control_identity": "generic deterministic command", "command": ["generic"]},),
        )
        record_validation_command_invocation(
            self.root, run_id=self.run_id, validation_id="suite", command_id=command_id, category="test",
            control_identity="generic deterministic command", required_for_profile=True,
            started_at="2026-08-29T00:00:00+00:00", currentness=0,
        )
        record_validation_command_terminal(
            self.root, run_id=self.run_id, command_id=command_id, completed_at="2026-08-29T00:00:01+00:00", exit_code=1,
        )
        control = load_validation_context(self.root, self.run_id)["controls"]["suite"]
        self.assertEqual(control["result"], "FAIL")
        self.assertEqual(control["diagnostic_evidence_ref"], reference)

    def test_unbound_historical_diagnostic_is_not_backfilled_into_a_control(self) -> None:
        command_id = "command-5"
        persist_validation_failure_diagnostic(
            self.root, run_id=self.run_id, command_id=command_id, validation_id="suite",
            control_identity="generic deterministic command", exit_code=1, stdout="", stderr="failed",
            capture_available=True,
        )
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE execution_artifact_records SET execution_id=NULL WHERE artifact_id=?",
                (validation_failure_artifact_id(command_id),),
            )
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="CUSTOM", validation_profile_version="1.0",
            required_validation_controls=("suite",), recorded_at="2026-08-29T00:00:00+00:00",
            control_bindings=({"validation_id": "suite", "required": True, "category": "test", "control_identity": "generic deterministic command", "command": ["generic"]},),
        )
        record_validation_command_invocation(
            self.root, run_id=self.run_id, validation_id="suite", command_id=command_id, category="test",
            control_identity="generic deterministic command", required_for_profile=True,
            started_at="2026-08-29T00:00:00+00:00", currentness=0,
        )
        record_validation_command_terminal(
            self.root, run_id=self.run_id, command_id=command_id, completed_at="2026-08-29T00:00:01+00:00", exit_code=1,
        )
        control = load_validation_context(self.root, self.run_id)["controls"]["suite"]
        self.assertEqual(control["result"], "FAIL")
        self.assertEqual(control["diagnostic_evidence_ref"], "UNAVAILABLE")
