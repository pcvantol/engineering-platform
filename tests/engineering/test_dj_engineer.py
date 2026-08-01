from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from tools.engineering.dj_engineer import (
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
    _open_report,
    extract_codex_usage,
    execution_mode_for,
    resolve_execution_context,
    genesis_workspace_preflight,
    format_management_summary,
    terminal_report_matches_state,
    generate_terminal_report,
    write_redacted_codex_cli_log,
    write_codex_usage,
    write_live_status,
)
from tools.engineering.platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    validate_compatibility,
)
from tools.engineering.capability_review import (
    ReviewerResult,
    reconciled_recommendations,
    records_for_storage,
    run_reviews,
    select_reviewers,
)
from tools.engineering.qualification import SCENARIOS, dashboard, execute_qualification, latest_qualification
from tools.engineering.providers import CodexCliProvider


class FakeRepository:
    def __init__(self, *, clean: bool = True, branch: str = "main", contains: bool = True) -> None:
        self.evidence = RepositoryEvidence("pcvantol/djconnect", branch, "a" * 40, clean, contains)
        self.contains = contains
        self.cleanup_calls: list[tuple[str | None, ...]] = []
        self.cleanup_error: RunnerError | None = None

    def inspect(self, root: Path) -> RepositoryEvidence:
        return self.evidence

    def main_contains(self, root: Path, sha: str) -> bool:
        return self.contains

    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str:
        self.cleanup_calls.append(branches)
        if self.cleanup_error:
            raise self.cleanup_error
        self.evidence = RepositoryEvidence("pcvantol/djconnect", "main", "a" * 40, True, True)
        return "fetched/pruned; main synchronized; removed=already-absent"


class FakeGitHub:
    def __init__(self, responses: list[PullRequestEvidence | RunnerError]) -> None:
        self.responses = responses
        self.calls = 0
        self.ready_calls: list[int] = []
        self.merge_calls: list[int] = []

    def pull_request(self, number: int) -> PullRequestEvidence:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response

    def ready(self, number: int) -> None:
        self.ready_calls.append(number)

    def merge(self, number: int) -> None:
        self.merge_calls.append(number)


class FakeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result, self.prompts = result, []

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        return self.result

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "0.146.0"


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

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        payload = json.loads((root / ".djconnect" / "status" / "current.json").read_text())
        self.live_phase = payload["phase"]
        self.live_action = payload["current_action"]
        return super().invoke(root, prompt)


class FakeReviewer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def review(self, root: Path, selection: object, objective: str) -> ReviewerResult:
        reviewer = getattr(selection, "reviewer")
        self.calls.append(reviewer)
        if self.fail:
            raise RuntimeError("reviewer unavailable")
        return ReviewerResult(reviewer, "Bounded review complete.", ("Use canonical wording.",))


class ClientContractTest(unittest.TestCase):
    @patch("tools.engineering.dj_engineer.subprocess.run")
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
            [("git", "switch", "main"), ("git", "pull", "--ff-only")],
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

        with tempfile.TemporaryDirectory() as temporary, patch("tools.engineering.dj_engineer.subprocess.run") as run:
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
                "statusCheckRollup": [
                    {"name": "green", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "bad", "status": "COMPLETED", "conclusion": "FAILURE"},
                ],
            }
        )
        provider = Provider(response)
        client = GhCliClient(provider)
        evidence = client.pull_request(7)
        self.assertTrue(evidence.checks_terminal)
        self.assertFalse(evidence.checks_passed)
        self.assertEqual(evidence.failed_checks, ("bad",))
        client.ready(7)
        client.merge(7)
        self.assertIn(("pr", "ready", "7"), provider.calls)
        self.assertIn(("pr", "merge", "7", "--squash", "--delete-branch"), provider.calls)

    @patch("tools.engineering.dj_engineer.subprocess.run")
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
        run.side_effect = [
            subprocess.CompletedProcess(("codex",), 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": review_message}}), ""),
            subprocess.CompletedProcess(("codex",), 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": agent_message}}), ""),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = CodexCliClient(CodexCliProvider())
            review = client.review(root, __import__("tools.engineering.capability_review", fromlist=["ReviewerSelection"]).ReviewerSelection("validation", "scope", 1), "objective")
            result = client.invoke(root, "objective")
        self.assertFalse(review.failed)
        self.assertEqual(review.recommendations, ("keep scope",))
        self.assertEqual(result.pull_request, 12)

    @patch("tools.engineering.dj_engineer.subprocess.run")
    def test_codex_client_keeps_bounded_diagnostics_on_failures(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(("codex",), 1, "prompt body", "token=secret\nfailed")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodexInvocationError) as raised:
                CodexCliClient().invoke(Path(temporary), "prompt body")
        self.assertIn("code 1", str(raised.exception))
        self.assertNotIn("secret", raised.exception.console_detail)

    @patch("tools.engineering.dj_engineer.generate_terminal_report", return_value=(None, None))
    @patch("tools.engineering.dj_engineer.EngineeringRunner")
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
        with tempfile.TemporaryDirectory() as temporary, patch("tools.engineering.dj_engineer.Path.cwd", return_value=Path(temporary)):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            self.assertEqual(__import__("tools.engineering.dj_engineer", fromlist=["main"]).main([str(prompt)]), 0)

    @patch("tools.engineering.dj_engineer.EngineeringRunner")
    def test_main_reports_blocked_runner_and_writes_redacted_console_log(self, runner_type: object) -> None:
        runner_type.return_value.run.side_effect = RunnerError("blocked preflight")
        with tempfile.TemporaryDirectory() as temporary, patch("tools.engineering.dj_engineer.Path.cwd", return_value=Path(temporary)):
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("# objective", encoding="utf-8")
            self.assertEqual(__import__("tools.engineering.dj_engineer", fromlist=["main"]).main([str(prompt)]), 2)

    @patch.object(SubprocessRepositoryClient, "inspect")
    @patch("tools.engineering.dj_engineer.subprocess.run")
    def test_repository_cleanup_handles_absent_and_squash_merged_branches(
        self, run: object, inspect: object
    ) -> None:
        class Provider:
            def command(self, _: Path, *args: str) -> str:
                return ""

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

    @patch("tools.engineering.dj_engineer.subprocess.run")
    def test_live_status_and_status_command_cover_missing_invalid_and_valid_files(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(("git",), 0, "main\n", "")
        state = TransactionState(
            run_id="run-status",
            repository="pcvantol/djconnect",
            prompt_path="/missing-prompt.md",
            phase="INITIALIZE",
        )
        module = __import__("tools.engineering.dj_engineer", fromlist=["print_live_status"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(module.print_live_status(root), 1)
            path = module.write_live_status(root, state, "starting")
            self.assertTrue(path.is_file())
            self.assertEqual(module.print_live_status(root), 0)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(module.print_live_status(root), 2)

    def test_engineering_memory_is_bounded_advisory_metadata(self) -> None:
        module = __import__("tools.engineering.dj_engineer", fromlist=["capture_engineering_memory"])
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

    @patch("tools.engineering.dj_engineer.subprocess.Popen", side_effect=OSError)
    @patch("tools.engineering.dj_engineer.shutil.which", return_value="/usr/local/bin/code")
    @patch("tools.engineering.dj_engineer.platform.system", return_value="Linux")
    def test_cli_helpers_and_editor_fallbacks_are_bounded(
        self, _: object, __: object, ___: object
    ) -> None:
        module = __import__("tools.engineering.dj_engineer", fromlist=["_codex_final_message"])
        usage = extract_codex_usage(
            '{"usage":[{"input-tokens":12},{"nested":{"output_tokens":3}}]}\nnot-json'
        )
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(module._codex_final_message("plain final message"), "plain final message")
        self.assertIsNone(module._open_report(Path("/tmp/report.md")))

    def test_usage_and_execution_context_helpers_fail_closed(self) -> None:
        module = __import__("tools.engineering.dj_engineer", fromlist=["write_codex_usage"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module.write_codex_usage(root, "run-usage", {"unknown": 1, "input_tokens": -1})
            self.assertFalse((root / ".djconnect/status/codex_usage.json").exists())
            module.write_codex_usage(root, "run-usage", {"input_tokens": 2})
            self.assertEqual(
                json.loads((root / ".djconnect/status/codex_usage.json").read_text(encoding="utf-8"))["usage"],
                {"input_tokens": 2},
            )
        self.assertEqual(execution_mode_for("Execution Mode: Genesis"), "GENESIS")
        self.assertEqual(execution_mode_for("no declaration"), "MANAGED")
        self.assertIsNotNone(genesis_workspace_preflight(None))


class LocalAgentRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("# bounded objective\n", encoding="utf-8")
        manifest = self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            '{"platform_version":"1.0.0","runner_version":"1.0.0","bootstrap_contract":"2026.07","checkpoint_format":1,"memory_format":1,"report_format":1,"minimum_codex_cli":"0.146.0","watcher_version":"1.0.0","inbox_protocol":1,"dashboard_version":"1.0.0","handoff_protocol":1,"status_model":1}\n',
            encoding="utf-8",
        )
        self.store = StateStore(self.root / ".djconnect" / "engineering-runs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_run_initializes_and_records_canonical_prompt(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="new-run")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertTrue(self.store.path_for("new-run").is_file())
        self.assertIn("Read BOOTSTRAP.md", agent.prompts[0])
        self.assertIn("# bounded objective", agent.prompts[0])

    def test_execute_agent_phase_is_published_before_agent_invocation(self) -> None:
        agent = LiveStatusFakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        runner.run(self.prompt, run_id="live-phase-run")
        self.assertEqual(agent.live_phase, "EXECUTE_AGENT")
        self.assertEqual(agent.live_action, "invoke_agent")

    def test_live_status_records_execution_context(self) -> None:
        state = TransactionState(
            "genesis-context",
            "pcvantol/djconnect",
            str(self.prompt),
            "EXECUTE_AGENT",
            execution_mode="GENESIS",
            genesis_repository_path=str(self.root),
        )
        write_live_status(self.root, state, "invoke_agent")
        payload = json.loads((self.root / ".djconnect" / "status" / "current.json").read_text())
        self.assertEqual(payload["execution_mode"], "GENESIS")
        self.assertEqual(payload["target_repository"], self.root.name)
        self.assertEqual(payload["checkout_path"], str(self.root))

    def test_genesis_mode_requires_an_explicit_execution_mode_declaration(self) -> None:
        self.assertEqual(execution_mode_for("Introduce Genesis Mode documentation."), "MANAGED")
        self.assertEqual(execution_mode_for("Execution Mode: Genesis"), "GENESIS")

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
        with patch("tools.engineering.dj_engineer.additional_workspace_write_roots", return_value=(target.parent.resolve(),)):
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
        with patch("tools.engineering.dj_engineer.additional_workspace_write_roots", return_value=(target.parent.resolve(),)):
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
        with patch("tools.engineering.dj_engineer.additional_workspace_write_roots", return_value=(target.parent.resolve(),)):
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
        with patch("tools.engineering.dj_engineer.subprocess.run", return_value=completed):
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
        with patch("tools.engineering.dj_engineer.subprocess.run", side_effect=invoke_with_json):
            result = client.invoke(self.root, "test")

        self.assertIn("--json", captured)
        self.assertEqual(result.terminal_state, "COMPLETE")
        self.assertEqual(
            client.last_usage,
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )

    def test_genesis_workspace_preflight_requires_accessible_target(self) -> None:
        issue = genesis_workspace_preflight(Path("/definitely/absent/forge"))

        self.assertIn("Target repository path is absent", issue or "")

    def test_cli_usage_is_written_only_when_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_codex_usage(root, "inbox-usage", {"total_tokens": 150})
            payload = json.loads((root / ".djconnect" / "status" / "codex_usage.json").read_text())
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

        with patch("tools.engineering.dj_engineer.subprocess.run", side_effect=invoke_with_schema):
            CodexCliClient().invoke(self.root, "test")

        self.assertEqual(set(captured["properties"]), set(captured["required"]))

    def test_cli_adds_configured_sibling_project_root(self) -> None:
        source = Path(__file__).resolve().parents[2] / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json"
        configuration = self.root / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json"
        configuration.parent.mkdir(parents=True, exist_ok=True)
        configuration.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        local = self.root / ".djconnect"
        local.mkdir(exist_ok=True)
        workspace_root = self.root.parent.resolve()
        (local / "engineering-platform.local.json").write_text(
            __import__("json").dumps({"workspace": {"provisioning_root": str(workspace_root)}}),
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

        with patch("tools.engineering.dj_engineer.subprocess.run", side_effect=invoke_with_workspace_root):
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

    def test_editor_env_has_deterministic_precedence(self) -> None:
        with patch.dict("os.environ", {"EDITOR": "/opt/editor"}, clear=True), patch("tools.engineering.dj_engineer.subprocess.Popen") as launch:
            self.assertEqual(_open_report(self.prompt), "EDITOR=/opt/editor")
        self.assertEqual(launch.call_args.args[0], ("/opt/editor", str(self.prompt)))

    def test_path_code_is_not_misidentified_as_vs_code(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("tools.engineering.dj_engineer.platform.system", return_value="Linux"), patch("tools.engineering.dj_engineer.shutil.which", side_effect=["/usr/local/bin/code", None]), patch("tools.engineering.dj_engineer.subprocess.Popen"):
            self.assertEqual(_open_report(self.prompt), "PATH executable: /usr/local/bin/code")

    def test_sensitive_diagnostic_is_redacted_before_persistence(self) -> None:
        agent = FakeAgent(AgentResult("BLOCKED", diagnostic="authorization=top-secret API_KEY=also-secret"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([]), agent, lambda _: None)
        state = runner.run(self.prompt, run_id="redacted-run")
        self.assertEqual(state.diagnostic, "[REDACTED] [REDACTED]")
        self.assertEqual(redact_diagnostic("Bearer private-token"), "[REDACTED]")

    def test_pending_ci_is_not_completion(self) -> None:
        state = TransactionState("pending-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=12, terminal_condition="open_pr_checks_terminal")
        pending = PullRequestEvidence(12, "OPEN", False, False)
        passed = PullRequestEvidence(12, "OPEN", True, True)
        github = FakeGitHub([pending, passed])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._poll(state)
        self.assertEqual(github.calls, 2)
        self.assertEqual(result.phase, "COMPLETE")

    def test_owner_authorization_merges_green_finalization_pr(self) -> None:
        state = TransactionState("authorized-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=14, transaction_kind="FINALIZATION", owner_authorized=True)
        github = FakeGitHub([PullRequestEvidence(14, "OPEN", True, True), PullRequestEvidence(14, "MERGED", True, True, "b" * 40)])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._poll(state)
        self.assertEqual(github.merge_calls, [14])
        self.assertEqual(result.phase, "COMPLETE")

    def test_merged_implementation_starts_and_reconciles_finalization(self) -> None:
        implementation = PullRequestEvidence(21, "MERGED", True, True, "b" * 40)
        final_open = PullRequestEvidence(22, "OPEN", True, True)
        final_merged = PullRequestEvidence(22, "MERGED", True, True, "c" * 40)
        agent = SequencedFakeAgent([AgentResult("WAITING", "codex/final", 22)])
        state = TransactionState("full-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/implementation", pull_request=21, owner_authorized=True)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([implementation, final_open, final_merged]), agent, lambda _: None)
        result = runner._poll(state)
        self.assertEqual(result.phase, "COMPLETE")
        self.assertEqual(result.implementation_pull_request, 21)
        self.assertEqual(result.finalization_pull_request, 22)
        self.assertEqual(result.finalization_merge_commit, "c" * 40)
        self.assertIn("mandatory governance-only Finalization", agent.prompts[0])

    def test_finalization_checkpoint_prevents_duplicate_generation(self) -> None:
        state = TransactionState("no-duplicate", "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT", owner_authorized=True, transaction_kind="FINALIZATION", finalization_branch="codex/final", finalization_pull_request=23)
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), FakeGitHub([PullRequestEvidence(23, "OPEN", False, False)]), FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._start_finalization(state, 21)
        self.assertEqual(result.pull_request, 23)
        self.assertEqual(runner.agent.prompts, [])

    def test_repair_records_iterations_and_failed_check_name(self) -> None:
        state = TransactionState("repair-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", branch="codex/repair", pull_request=24, owner_authorized=True)
        github = FakeGitHub([PullRequestEvidence(24, "OPEN", True, False, failed_checks=("Ruff",))])
        agent = SequencedFakeAgent([AgentResult("BLOCKED", "codex/repair", 24, diagnostic="External review required.")])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, agent, lambda _: None)
        repaired = runner._poll(state)
        self.assertEqual(repaired.repair_iterations, 1)
        self.assertIn("Ruff failed", agent.prompts[0])

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
        result = EngineeringRunner(self.root, self.store, repository, github, FakeAgent(AgentResult("WAITING")), lambda _: None)._poll(state)
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
        result = runner._poll(state)
        self.assertEqual(result.phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertFalse(result.terminal)
        self.assertEqual(result.next_action, "retry_github_evidence")

    def test_dirty_workspace_has_no_agent_or_destructive_action(self) -> None:
        agent = FakeAgent(AgentResult("COMPLETE"))
        runner = EngineeringRunner(self.root, self.store, FakeRepository(clean=False), FakeGitHub([]), agent, lambda _: None)
        with self.assertRaisesRegex(RunnerError, "not clean"):
            runner.run(self.prompt, run_id="dirty-run")
        self.assertEqual(agent.prompts, [])

    def test_engineering_platform_accepts_newer_compatible_runner(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json")
        validate_compatibility(manifest, RunnerCompatibility(runner_version="1.1.0", bootstrap_contract="2026.08", checkpoint_formats=frozenset({1, 2}), memory_formats=frozenset({1}), report_formats=frozenset({1})), "0.146.0")

    def test_engineering_platform_rejects_incompatible_platform_version(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json")
        with self.assertRaisesRegex(EngineeringPlatformCompatibilityError, "Engineering Platform mismatch"):
            validate_compatibility(manifest, RunnerCompatibility(platform_version="0.9.0"), "0.146.0")

    def test_engineering_platform_rejects_bootstrap_checkpoint_memory_and_report_mismatches(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json")
        cases = (
            (RunnerCompatibility(bootstrap_contract="2026.06"), "Bootstrap contract mismatch"),
            (RunnerCompatibility(checkpoint_formats=frozenset({2})), "Checkpoint format mismatch"),
            (RunnerCompatibility(memory_formats=frozenset({2})), "Engineering Memory format mismatch"),
            (RunnerCompatibility(report_formats=frozenset({2})), "Report format mismatch"),
        )
        for compatibility, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(EngineeringPlatformCompatibilityError, diagnostic):
                validate_compatibility(manifest, compatibility, "0.146.0")

    def test_engineering_platform_rejects_older_runner(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json")
        with self.assertRaisesRegex(EngineeringPlatformCompatibilityError, "Runner version mismatch"):
            validate_compatibility(manifest, RunnerCompatibility(runner_version="0.9.0"), "0.146.0")

    def test_terminal_report_records_engineering_platform(self) -> None:
        state = TransactionState("platform-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        with patch("tools.engineering.dj_engineer._open_report", return_value=None):
            report, _ = generate_terminal_report(self.root, state, EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"), "0.146.0")
        body = report.read_text(encoding="utf-8")
        self.assertIn("Platform Version: `1.0.0`", body)
        self.assertIn("Detected Codex CLI Version: `0.146.0`", body)

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
        with patch("tools.engineering.dj_engineer._open_report", return_value=None):
            report, _ = generate_terminal_report(
                self.root,
                state,
                EngineeringPlatformManifest.load(
                    self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
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
        self.assertIn("Resolved during implementation", body)
        self.assertIn("Resulting commits: implementation `" + "a" * 40, body)
        self.assertIn("Repository state: branch=main; clean=True", body)
        self.assertTrue(terminal_report_matches_state(body, state))

    def test_assessment_only_report_does_not_claim_delivery(self) -> None:
        state = TransactionState(
            "assessment-report",
            "pcvantol/djconnect",
            str(self.prompt),
            "BLOCKED",
            diagnostic="Repository preflight requires attention.",
            terminal=True,
        )
        with patch("tools.engineering.dj_engineer._open_report", return_value=None):
            report, _ = generate_terminal_report(self.root, state)
        body = report.read_text(encoding="utf-8")
        self.assertIn("## Initial Repository Assessment", body)
        self.assertIn("Completed work: no successful engineering delivery is claimed.", body)
        self.assertIn("BLOCKED — no engineering changes were executed or delivered.", body)
        self.assertTrue(terminal_report_matches_state(body, state))

    def test_blocked_and_failed_reports_match_the_terminal_checkpoint(self) -> None:
        manifest = EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json")
        for phase, expected in (
            ("BLOCKED", "BLOCKED — no engineering changes were executed or delivered."),
            ("FAILED", "FAILED — the engineering transaction did not complete successfully."),
        ):
            with self.subTest(phase=phase), patch("tools.engineering.dj_engineer._open_report", return_value=None):
                state = TransactionState(f"{phase.lower()}-report", "pcvantol/djconnect", str(self.prompt), phase, diagnostic="Bounded diagnostic.", terminal=True)
                report, _ = generate_terminal_report(self.root, state, manifest, "0.146.0")
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

    def test_reviewer_failure_never_blocks_selection(self) -> None:
        selections = select_reviewers("documentation", self.prompt, "IMPLEMENTATION", {})
        results = run_reviews(self.root, selections, "objective", FakeReviewer(fail=True))
        self.assertTrue(results[0].failed)
        self.assertEqual(reconciled_recommendations(results), ())

    def test_terminal_report_records_selected_reviewers(self) -> None:
        state = TransactionState("review-report", "pcvantol/djconnect", str(self.prompt), "COMPLETE", terminal=True)
        records = ({"reviewer": "documentation", "selected_because": "documentation-oriented objective", "contribution": "Navigation checked.", "accepted_recommendations": 3, "rejected_recommendations": 1, "failed": False},)
        with patch("tools.engineering.dj_engineer._open_report", return_value=None):
            report, _ = generate_terminal_report(self.root, state, EngineeringPlatformManifest.load(self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"), "0.146.0", records)
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
