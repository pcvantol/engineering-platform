from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from engineering_platform.agent_state import StateError, StateStore, TransactionState
from engineering_platform.execution_errors import RunnerError
from engineering_platform.execution_models import RepositoryEvidence
from engineering_platform.execution_repository import GhCliClient
from engineering_platform.managed_publication import (
    PublicationCandidate, PublicationRecovery, publish_candidate, validate_intent, valid_branch,
)


class Repository:
    def __init__(self):
        self.evidence = RepositoryEvidence("owner/repo", "codex/candidate", "a" * 40, True)
        self.pushes = 0
    def inspect(self, root): return self.evidence
    def trusted_origin_identity(self, root): return "owner/repo"
    def workspace_operation_active(self, root): return False
    def publish_candidate_branch(self, *args): self.pushes += 1


class GitHub:
    """Only the external GitHub effect is simulated; storage/service are real."""
    def __init__(self):
        self.candidates, self.creates, self.reads = [], 0, 0
        self.after_create = None
        self.read_error = False
    def publication_candidates(self, repository, branch):
        self.reads += 1
        if self.read_error: raise RunnerError("read unavailable")
        return self.candidates
    def create_draft_publication(self, *args):
        self.creates += 1
        self.candidates = [PublicationCandidate(71, "owner/repo", "owner/repo", "codex/candidate", "main", "a" * 40, "OPEN", True)]
        if self.after_create: self.after_create()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / ".engineering" / "engineering-runs")
        self.state = TransactionState(
            "mpr-publish", "owner/repo", str(self.root / "prompt.md"), "QUALITY_CONTROL_AGENT",
            branch="codex/candidate", owner_authorized=True, repair_iterations=2,
            assurance_profile={"version": "1.0", "digest": "sha256:" + "b" * 64,
                               "candidate_sha": "a" * 40, "criteria_digest": "sha256:" + "c" * 64,
                               "validation_profile_digest": "sha256:" + "d" * 64},
        )
        self.store.save(self.state)
        self.repo, self.github = Repository(), GitHub()

    def publish(self, state=None, store=None):
        return publish_candidate(state=state or self.state, store=store or self.store,
                                 root=self.root, repository=self.repo, github=self.github)

    def test_lost_ack_restart_and_repeated_resume_never_recreate(self):
        def lost_ack(): raise RunnerError("lost acknowledgement")
        self.github.after_create = lost_ack
        result, number = self.publish()
        self.assertEqual(number, 71)
        self.assertEqual(result.publication_intent["status"], "RECONCILED")
        restarted = StateStore(self.store.directory)
        for _ in range(3):
            self.publish(restarted.load(self.state.run_id), restarted)
        self.assertEqual((self.github.creates, self.repo.pushes), (1, 1))
        self.assertEqual(restarted.load(self.state.run_id).repair_iterations, 2)

    def test_crash_after_remote_accept_before_receipt_recovers(self):
        def crash(): raise SystemExit("process terminated")
        self.github.after_create = crash
        with self.assertRaises(SystemExit): self.publish()
        checkpoint = self.store.load(self.state.run_id)
        self.assertEqual(checkpoint.publication_intent["status"], "CREATE_UNCERTAIN")
        self.assertEqual(self.publish(checkpoint)[1], 71)
        self.assertEqual(self.github.creates, 1)

    def test_crash_before_create_marker_readback_allows_the_one_create(self):
        original = self.repo.publish_candidate_branch
        def crash(*args): raise SystemExit("before create")
        self.repo.publish_candidate_branch = crash
        with self.assertRaises(SystemExit): self.publish()
        checkpoint = self.store.load(self.state.run_id)
        self.assertEqual(checkpoint.publication_intent["status"], "PREPARED")
        self.repo.publish_candidate_branch = original
        self.assertEqual(self.publish(checkpoint)[1], 71)
        self.assertEqual(self.github.creates, 1)

    def test_crash_after_marker_before_remote_effect_requires_readback(self):
        def crash(*args): raise SystemExit("before remote call")
        self.github.create_draft_publication = crash
        with self.assertRaises(SystemExit): self.publish()
        for _ in range(2):
            with self.assertRaises(PublicationRecovery) as raised:
                self.publish(self.store.load(self.state.run_id))
            self.assertTrue(raised.exception.pending)
        self.assertEqual(self.github.creates, 0)

    def test_existing_exact_draft_reconciles_without_push_or_create(self):
        self.github.create_draft_publication()
        self.github.creates = 0
        self.assertEqual(self.publish()[1], 71)
        self.assertEqual((self.github.creates, self.repo.pushes), (0, 0))

    def test_wrong_or_ambiguous_remote_evidence_blocks(self):
        self.github.create_draft_publication()
        exact = self.github.candidates[0]
        self.github.creates = 0
        for changes in ({"head_sha": "e" * 40}, {"base": "other"}, {"draft": False},
                        {"state": "CLOSED"}, {"state": "MERGED"}, {"repository": "foreign/repo"},
                        {"head_repository": "fork/repo"}, {"branch": "other"}, {"number": True}):
            with self.subTest(changes=changes):
                self.github.candidates = [replace(exact, **changes)]
                state = self.store.load(self.state.run_id)
                with self.assertRaisesRegex(PublicationRecovery, "remote_identity_conflict"):
                    self.publish(state)
        self.github.candidates = [exact, exact]
        with self.assertRaisesRegex(PublicationRecovery, "ambiguous"):
            self.publish(self.store.load(self.state.run_id))
        self.assertEqual(self.github.creates, 0)

    def test_candidate_mutation_or_profile_drift_blocks(self):
        self.repo.evidence = replace(self.repo.evidence, clean=False)
        with self.assertRaisesRegex(PublicationRecovery, "candidate_changed"): self.publish()
        checkpoint = self.store.load(self.state.run_id)
        with self.assertRaisesRegex(PublicationRecovery, "identity_changed"):
            self.publish(replace(checkpoint, repair_iterations=3))
        self.assertEqual(self.github.creates, 0)

    def test_unavailable_readback_never_creates(self):
        self.github.read_error = True
        with self.assertRaises(PublicationRecovery) as raised: self.publish()
        self.assertTrue(raised.exception.pending)
        self.assertEqual(self.github.creates, 0)

    def test_concurrent_intent_claim_and_rollback_are_refused(self):
        # Both writers have the same initial observation. The SQLite compare
        # and set permits one, and a stale ordinary save cannot erase it.
        barrier = Barrier(2)
        def contender():
            barrier.wait()
            try: return self.publish()[1]
            except StateError: return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: contender(), range(2)))
        self.assertCountEqual(results, [71, "conflict"])
        self.assertEqual(self.github.creates, 1)
        with self.assertRaisesRegex(StateError, "rolled back"):
            self.store.save(self.state)

    def test_intent_schema_and_identity_cannot_be_forged(self):
        result, _ = self.publish()
        for changes in ({"version": "2"}, {"branch": "main"}, {"candidate_sha": "short"},
                        {"repair_ordinal": True}, {"pull_request": None}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_intent({**result.publication_intent, **changes})
        for value in ("main", "-head", "a..b", "a//b", "a/.hidden", "a.lock", "a/", "a.", None):
            self.assertFalse(valid_branch(value))
        altered = result.to_dict()
        altered["run_id"] = "other"
        with self.assertRaises(StateError): TransactionState.from_dict(altered)

    def test_github_adapter_reads_all_pages_and_scopes_create(self):
        calls = []
        item = {"number": 71, "state": "open", "draft": True,
                "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                "head": {"ref": "codex/candidate", "sha": "a" * 40, "repo": {"full_name": "owner/repo"}}}
        class Provider:
            def github(self, *args):
                calls.append(args)
                return json.dumps([[item], [item]]) if args[0] == "api" else "ack"
        client = GhCliClient(Provider(), "owner/repo")
        self.assertEqual(len(client.publication_candidates("owner/repo", "codex/candidate")), 2)
        self.assertIn("--paginate", calls[0])
        client.create_draft_publication("owner/repo", "codex/candidate", "main", "Title", "Body")
        self.assertEqual(calls[-1][-2:], ("--repo", "owner/repo"))
        self.assertIn("--draft", calls[-1])
        with self.assertRaises(RunnerError): client.publication_candidates("other/repo", "codex/candidate")
        with self.assertRaises(RunnerError): client.create_draft_publication("owner/repo", "main", "main", "", "")


if __name__ == "__main__":
    unittest.main()
