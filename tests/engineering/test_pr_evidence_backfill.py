"""Regression coverage for fail-closed historical PR-evidence recovery."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.pr_evidence_backfill import _candidate_decision, _current_repository, _origin_main_contains, backfill
from engineering_platform.storage import open_storage


class FakeGitHub:
    def __init__(self, evidence: PullRequestEvidence | None = None, *, fails: bool = False) -> None:
        self.evidence, self.fails = evidence, fails
        self.branches: list[str] = []

    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None:
        self.branches.append(branch)
        if self.fails:
            raise RuntimeError("offline")
        return self.evidence


class PullRequestEvidenceBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / ".engineering" / "engineering-runs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _evidence(number: int = 945, *, branch: str = "codex/finalize", commit: str = "b" * 40) -> PullRequestEvidence:
        return PullRequestEvidence(number, "MERGED", True, True, commit, False, (), branch, "main", "CLEAN")

    def _state(self, **changes: object) -> TransactionState:
        state = TransactionState(
            "inbox-backfill", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True,
            implementation_branch="codex/implementation", implementation_pull_request=944,
            implementation_merge_commit="a" * 40, finalization_branch="codex/finalize",
            finalization_merge_commit="b" * 40,
        )
        return replace(state, **changes)

    def test_dry_run_never_mutates_a_checkpoint(self) -> None:
        self.store.save(self._state())
        report = backfill(self.root, github=FakeGitHub(self._evidence()), repository="pcvantol/djconnect", main_contains=lambda _: True)
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["applied"], 1)
        self.assertIsNone(self.store.load("inbox-backfill").finalization_pull_request)
        with open_storage(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_pr_evidence_backfills").fetchone()[0], 0)

    def test_apply_atomically_links_only_exact_finalization_evidence(self) -> None:
        self.store.save(self._state())
        report = backfill(self.root, apply=True, github=FakeGitHub(self._evidence()), repository="pcvantol/djconnect", main_contains=lambda _: True)
        self.assertEqual(report["applied"], 1)
        recovered = self.store.load("inbox-backfill")
        self.assertEqual(recovered.finalization_pull_request, 945)
        with open_storage(self.root) as connection:
            row = connection.execute(
                "SELECT pr_role,outcome,reason,pr_number FROM execution_pr_evidence_backfills WHERE run_id=?",
                ("inbox-backfill",),
            ).fetchall()
        self.assertIn(("FINALIZATION", "APPLIED", "exact_github_branch_and_merge_evidence", 945), row)

    def test_mismatched_branch_or_merge_commit_is_skipped_and_audited(self) -> None:
        self.store.save(self._state())
        report = backfill(
            self.root, apply=True,
            github=FakeGitHub(self._evidence(branch="codex/other", commit="c" * 40)), repository="pcvantol/djconnect", main_contains=lambda _: True,
        )
        self.assertEqual(report["applied"], 0)
        self.assertIsNone(self.store.load("inbox-backfill").finalization_pull_request)
        finalization = next(item for item in report["decisions"] if item["role"] == "finalization")
        self.assertEqual(finalization["reason"], "github_evidence_does_not_exactly_match_checkpoint")
        with open_storage(self.root) as connection:
            outcome = connection.execute(
                "SELECT outcome FROM execution_pr_evidence_backfills WHERE run_id=? AND pr_role='FINALIZATION'",
                ("inbox-backfill",),
            ).fetchone()[0]
        self.assertEqual(outcome, "SKIPPED")

    def test_incomplete_or_nonterminal_checkpoints_are_never_connected(self) -> None:
        self.store.save(self._state(terminal=False, phase="FINALIZE_AGENT"))
        report = backfill(self.root, apply=True, github=FakeGitHub(self._evidence()), repository="pcvantol/djconnect", main_contains=lambda _: True)
        finalization = next(item for item in report["decisions"] if item["role"] == "finalization")
        self.assertEqual(finalization["reason"], "run_not_terminal")
        self.assertIsNone(self.store.load("inbox-backfill").finalization_pull_request)

    def test_github_failure_is_reported_without_a_mutation(self) -> None:
        self.store.save(self._state())
        report = backfill(self.root, apply=True, github=FakeGitHub(fails=True), repository="pcvantol/djconnect", main_contains=lambda _: True)
        finalization = next(item for item in report["decisions"] if item["role"] == "finalization")
        self.assertEqual(finalization["reason"], "github_evidence_unavailable")
        self.assertIsNone(self.store.load("inbox-backfill").finalization_pull_request)

    def test_missing_current_main_evidence_is_skipped_without_a_mutation(self) -> None:
        self.store.save(self._state())
        report = backfill(
            self.root, apply=True, github=FakeGitHub(self._evidence()),
            repository="pcvantol/djconnect", main_contains=lambda _: None,
        )
        finalization = next(item for item in report["decisions"] if item["role"] == "finalization")
        self.assertEqual(finalization["reason"], "repository_merge_evidence_unavailable")
        self.assertIsNone(self.store.load("inbox-backfill").finalization_pull_request)

    def test_candidate_recovery_rejects_every_missing_or_wrong_authority_fact(self) -> None:
        """Historical repair needs an exact terminal managed checkpoint and origin evidence."""
        cases = (
            (self._state(execution_mode="CLI"), "pcvantol/djconnect", "not_managed_execution"),
            (self._state(), "other/repository", "repository_evidence_unavailable_or_mismatched"),
            (self._state(finalization_pull_request=9), "pcvantol/djconnect", "pull_request_already_recorded"),
            (self._state(finalization_branch=None), "pcvantol/djconnect", "checkpoint_evidence_incomplete"),
        )
        for state, repository, reason in cases:
            decision = _candidate_decision(state, "FINALIZATION", FakeGitHub(self._evidence()), repository, lambda _: True)
            self.assertEqual(decision.reason, reason)
        self.assertEqual(
            _candidate_decision(self._state(), "FINALIZATION", FakeGitHub(None), "pcvantol/djconnect", lambda _: True).reason,
            "pull_request_not_found_for_checkpoint_branch",
        )
        self.assertEqual(
            _candidate_decision(self._state(), "FINALIZATION", FakeGitHub(self._evidence()), "pcvantol/djconnect", lambda _: False).reason,
            "merge_commit_not_in_origin_main",
        )

    def test_repository_origin_and_main_check_are_read_only_and_normalized(self) -> None:
        completed = type("Result", (), {"returncode": 0, "stdout": "git@github.com:owner/repo.git\n"})()
        from unittest.mock import patch
        with patch("engineering_platform.pr_evidence_backfill.GitProvider") as provider:
            provider.return_value.execute.return_value = completed
            self.assertEqual(_current_repository(self.root), "owner/repo")
            provider.return_value.execute.side_effect = [completed, type("Result", (), {"returncode": 1, "stdout": ""})()]
            self.assertFalse(_origin_main_contains(self.root, "a" * 40))
