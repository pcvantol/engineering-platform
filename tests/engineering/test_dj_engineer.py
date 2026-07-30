from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.agent_state import StateError, StateStore, TransactionState
from tools.engineering.dj_engineer import (
    AgentResult,
    EngineeringRunner,
    PullRequestEvidence,
    RepositoryEvidence,
    RunnerError,
)


class FakeRepository:
    def __init__(self, *, clean: bool = True, branch: str = "main", contains: bool = True) -> None:
        self.evidence = RepositoryEvidence("pcvantol/djconnect", branch, "a" * 40, clean, contains)
        self.contains = contains

    def inspect(self, root: Path) -> RepositoryEvidence:
        return self.evidence

    def main_contains(self, root: Path, sha: str) -> bool:
        return self.contains


class FakeGitHub:
    def __init__(self, responses: list[PullRequestEvidence | RunnerError]) -> None:
        self.responses = responses
        self.calls = 0

    def pull_request(self, number: int) -> PullRequestEvidence:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class FakeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result, self.prompts = result, []

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        return self.result

    def available(self) -> bool:
        return True


class LocalAgentRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("# bounded objective\n", encoding="utf-8")
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
        self.store.save(TransactionState("resume-run", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", pull_request=11))
        pending = PullRequestEvidence(11, "OPEN", False, False)
        passed = PullRequestEvidence(11, "OPEN", True, True)
        github = FakeGitHub([pending, passed])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        # A non-terminal resume cannot claim completion from its stored phase.
        state = runner._reconcile(self.store.load("resume-run"), FakeRepository().inspect(self.root))
        self.assertEqual(state.phase, "WAIT_FOR_TERMINAL_EVIDENCE")
        self.assertEqual(state.next_action, "poll_required_checks")

    def test_pending_ci_is_not_completion(self) -> None:
        state = TransactionState("pending-run", "pcvantol/djconnect", str(self.prompt), "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=12, terminal_condition="open_pr_checks_terminal")
        pending = PullRequestEvidence(12, "OPEN", False, False)
        passed = PullRequestEvidence(12, "OPEN", True, True)
        github = FakeGitHub([pending, passed])
        runner = EngineeringRunner(self.root, self.store, FakeRepository(), github, FakeAgent(AgentResult("WAITING")), lambda _: None)
        result = runner._poll(state)
        self.assertEqual(github.calls, 2)
        self.assertEqual(result.phase, "COMPLETE")

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
