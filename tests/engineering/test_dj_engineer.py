from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.agent_state import StateError, StateStore, TransactionState, redact_diagnostic
from tools.engineering.dj_engineer import (
    AgentResult,
    CodexCliClient,
    CodexInvocationError,
    EngineeringRunner,
    PullRequestEvidence,
    RepositoryEvidence,
    RunnerError,
    _format_terminal_report,
    _open_report,
    format_management_summary,
    generate_terminal_report,
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


class LocalAgentRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("# bounded objective\n", encoding="utf-8")
        manifest = self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            '{"platform_version":"1.0.0","runner_version":"1.0.0","bootstrap_contract":"2026.07","checkpoint_format":1,"memory_format":1,"report_format":1,"minimum_codex_cli":"0.146.0"}\n',
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
