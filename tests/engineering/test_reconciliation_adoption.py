from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_errors import RunnerError
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.providers import GitProvider
from engineering_platform.reconciliation_adoption import (
    ROLLING_RECORDS,
    _command,
    _verified_candidate,
    adopt_legacy_direct_reconciliation,
    main,
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

    def test_recorded_candidate_is_authoritative_and_explicit_fallback_is_required(self) -> None:
        state = TransactionState(
            "legacy-reconciliation", "pcvantol/forge", "prompt.md", "BLOCKED",
            commit_evidence=({
                "description": "end_reconciliation_commit_verified",
                "commit_sha": "1" * 40,
            },),
        )
        self.assertEqual(_verified_candidate(state, None), "1" * 40)
        with self.assertRaisesRegex(RunnerError, "conflicts with recorded"):
            _verified_candidate(state, "2" * 40)
        with self.assertRaisesRegex(RunnerError, "requires an explicit candidate"):
            _verified_candidate(replace(state, commit_evidence=()), None)

    def test_command_and_checkout_guards_fail_closed(self) -> None:
        class BrokenProvider:
            def command(self, _root: Path, *_args: str) -> str:
                raise RuntimeError("git unavailable")

        with self.assertRaisesRegex(RunnerError, "git unavailable"):
            _command(BrokenProvider(), Path("."), "git", "status")  # type: ignore[arg-type]

        for case in ("identity", "branch", "dirty"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root, baseline, candidate = self._fixture(temporary)
                if case == "branch":
                    self._git(root, "switch", "-c", "unrelated")
                elif case == "dirty":
                    (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
                expected = {
                    "identity": "candidate identity is invalid",
                    "branch": "requires main or its deterministic branch",
                    "dirty": "requires a clean checkout",
                }[case]
                with self.assertRaisesRegex(RunnerError, expected):
                    adopt_legacy_direct_reconciliation(
                        root, self._state(root, baseline), provider=GitProvider(),
                        github=FakeGitHub(), candidate="invalid" if case == "identity" else candidate,
                    )

    def test_existing_branch_and_returned_pull_request_must_match_guarded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline, candidate = self._fixture(temporary)
            branch = "codex/reconcile-legacy-reconciliation"
            self._git(root, "branch", branch, baseline)
            with self.assertRaisesRegex(RunnerError, "branch points to another commit"):
                adopt_legacy_direct_reconciliation(
                    root, self._state(root, baseline), provider=GitProvider(),
                    github=FakeGitHub(), candidate=candidate,
                )

        class WrongGitHub(FakeGitHub):
            def create_or_recover_pull_request(
                self, branch: str, base: str, title: str, body: str, *, draft: bool = False,
            ) -> PullRequestEvidence:
                return PullRequestEvidence(
                    72, "OPEN", False, False, is_draft=draft,
                    head_branch=branch, base_branch="release",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root, baseline, candidate = self._fixture(temporary)
            with self.assertRaisesRegex(RunnerError, "does not match its guarded identity"):
                adopt_legacy_direct_reconciliation(
                    root, self._state(root, baseline), provider=GitProvider(),
                    github=WrongGitHub(), candidate=candidate,
                )

    def test_cli_wires_central_state_to_secret_free_result(self) -> None:
        state = TransactionState(
            "legacy-reconciliation", "pcvantol/forge", "prompt.md", "BLOCKED",
            owner_authorized=True, transaction_kind="RECONCILIATION",
            finalization_merge_commit="1" * 40, terminal=True,
        )
        pull_request = PullRequestEvidence(
            73, "OPEN", False, False,
            head_branch="codex/reconcile-legacy-reconciliation", base_branch="main",
        )
        with patch("engineering_platform.reconciliation_adoption.StateStore") as store, patch(
            "engineering_platform.reconciliation_adoption._command",
            return_value="https://github.com/pcvantol/forge.git",
        ), patch(
            "engineering_platform.reconciliation_adoption.adopt_legacy_direct_reconciliation",
            return_value=pull_request,
        ) as adopt, patch("builtins.print") as output:
            store.return_value.load.return_value = state
            result = main([
                "--repo", "/tmp/forge", "--run-id", "legacy-reconciliation",
                "--central-database", "/tmp/epdata.sqlite", "--candidate", "2" * 40,
            ])

        self.assertEqual(result, 0)
        self.assertEqual(adopt.call_args.kwargs["candidate"], "2" * 40)
        self.assertEqual(json.loads(output.call_args.args[0]), {
            "base": "main",
            "branch": "codex/reconcile-legacy-reconciliation",
            "pull_request": 73,
            "run_id": "legacy-reconciliation",
            "state": "OPEN",
        })


if __name__ == "__main__":
    unittest.main()
