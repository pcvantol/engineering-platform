from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_errors import RunnerError
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.providers import GitProvider
from engineering_platform.reconciliation_adoption import (
    ROLLING_RECORDS,
    adopt_legacy_direct_reconciliation,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.ready_calls: list[int] = []

    def create_or_recover_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False,
    ) -> PullRequestEvidence:
        self.calls.append((branch, base, draft))
        return PullRequestEvidence(
            71, "OPEN", False, False, is_draft=draft,
            head_branch=branch, base_branch=base,
        )

    def ready(self, number: int) -> None:
        self.ready_calls.append(number)


class ReconciliationAdoptionTest(unittest.TestCase):
    @staticmethod
    def _git(root: Path, *args: str) -> str:
        completed = subprocess.run(
            ("git", *args), cwd=root, check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    def _fixture(self, temporary: str, *, extra_change: bool = False) -> tuple[Path, str, str]:
        base = Path(temporary)
        remote, seed, checkout = base / "remote.git", base / "seed", base / "checkout"
        remote.mkdir()
        self._git(remote, "init", "--bare")
        seed.mkdir()
        self._git(seed, "init", "-b", "main")
        self._git(seed, "config", "user.name", "Test")
        self._git(seed, "config", "user.email", "test@example.invalid")
        for path in ROLLING_RECORDS:
            (seed / path).write_text(f"baseline {path}\n", encoding="utf-8")
        self._git(seed, "add", *sorted(ROLLING_RECORDS))
        self._git(seed, "commit", "-m", "baseline")
        self._git(seed, "remote", "add", "origin", str(remote))
        self._git(seed, "push", "-u", "origin", "main")
        self._git(base, "clone", str(remote), str(checkout))
        self._git(checkout, "switch", "main")
        self._git(checkout, "config", "user.name", "Test")
        self._git(checkout, "config", "user.email", "test@example.invalid")
        baseline = self._git(checkout, "rev-parse", "HEAD")
        for path in ROLLING_RECORDS:
            (checkout / path).write_text(f"reconciled {path}\n", encoding="utf-8")
        changed = sorted(ROLLING_RECORDS)
        if extra_change:
            (checkout / "runtime.py").write_text("unsafe = True\n", encoding="utf-8")
            changed.append("runtime.py")
        self._git(checkout, "add", *changed)
        self._git(checkout, "commit", "-m", "reconcile rolling records")
        return checkout, baseline, self._git(checkout, "rev-parse", "HEAD")

    @staticmethod
    def _state(root: Path, baseline: str) -> TransactionState:
        return TransactionState(
            "legacy-reconciliation", "pcvantol/forge", str(root / "prompt.md"),
            "BLOCKED", owner_authorized=True, transaction_kind="RECONCILIATION",
            finalization_merge_commit=baseline, terminal=True,
        )

    def test_exact_legacy_commit_is_moved_to_one_draft_pr_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline, candidate = self._fixture(temporary)
            github = FakeGitHub()
            state = self._state(root, baseline)

            first = adopt_legacy_direct_reconciliation(
                root, state, provider=GitProvider(), github=github, candidate=candidate,
            )
            second = adopt_legacy_direct_reconciliation(
                root, state, provider=GitProvider(), github=github, candidate=candidate,
            )

            branch = "codex/reconcile-legacy-reconciliation"
            self.assertEqual(first.number, second.number)
            self.assertEqual(github.calls, [(branch, "main", True), (branch, "main", True)])
            self.assertEqual(github.ready_calls, [71, 71])
            self.assertEqual(self._git(root, "rev-parse", "main"), baseline)
            self.assertEqual(self._git(root, "rev-parse", branch), candidate)
            self.assertEqual(self._git(root, "rev-parse", f"origin/{branch}"), candidate)

    def test_candidate_with_non_rolling_change_is_rejected_before_branch_or_push(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline, candidate = self._fixture(temporary, extra_change=True)
            github = FakeGitHub()

            with self.assertRaisesRegex(RunnerError, "exact four-record update"):
                adopt_legacy_direct_reconciliation(
                    root, self._state(root, baseline), provider=GitProvider(),
                    github=github, candidate=candidate,
                )

            self.assertEqual(self._git(root, "branch", "--show-current"), "main")
            self.assertEqual(github.calls, [])

    def test_nonterminal_run_cannot_enter_legacy_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline, candidate = self._fixture(temporary)
            state = TransactionState(
                "legacy-reconciliation", "pcvantol/forge", str(root / "prompt.md"),
                "RECONCILE_AGENT", owner_authorized=True,
                transaction_kind="RECONCILIATION", finalization_merge_commit=baseline,
            )
            with self.assertRaisesRegex(RunnerError, "not an owner-authorized blocked"):
                adopt_legacy_direct_reconciliation(
                    root, state, provider=GitProvider(), github=FakeGitHub(),
                    candidate=candidate,
                )


if __name__ == "__main__":
    unittest.main()
