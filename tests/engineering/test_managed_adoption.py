"""Real Git, CENTRAL, validation and lifecycle; only external providers adapt."""
from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import server
from engineering_platform.agent_state import StateStore, StateError, TransactionState
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import AgentResult, PullRequestEvidence
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_adoption import parse_selection, profile_digest, verify_selection
from engineering_platform.managed_publication import PublicationCandidate
from engineering_platform.providers import GitProvider, process_effect_start
from engineering_platform.storage import load_validation_context, sqlite_connection
from tests.engineering.test_execution_host import FakeAgent, mandatory_review_result


class LocalGitHubTransport(GitProvider):
    """Redirect only remote Git transport to a real isolated bare repository."""
    def __init__(self, remote): self.remote = remote
    def execute(self, root, *args):
        if len(args) > 1 and args[0] == "git" and args[1] in {"fetch", "push", "ls-remote"}:
            args = ("git", "-c", f"url.{self.remote}.insteadOf=https://github.com/qualification/managed", *args[1:])
        return super().execute(root, *args)


class AdoptionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)
        self.root, self.remote, self.data = self.area / "repo", self.area / "remote.git", self.area / "central"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "qualification@example.invalid")
        self.git("config", "user.name", "MPR qualification")
        (self.root / "BOOTSTRAP.md").write_text("# Qualification\n")
        (self.root / ".gitignore").write_text(".engineering/\n__pycache__/\n")
        (self.root / "README.md").write_text("# Before\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_docs.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass Documentation(unittest.TestCase):\n"
            "    def test_heading(self):\n        self.assertTrue(Path('README.md').read_text().startswith('# '))\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(["bootstrap-topology", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo"]), 0)
            self.assertEqual(server.main(["provision-declaration", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo", "--path", str(self.root)]), 0)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        subprocess.run(("git", "init", "-q", "--bare", str(self.remote)), check=True)
        self.git("remote", "add", "origin", "https://github.com/qualification/managed")
        self.transport = LocalGitHubTransport(self.remote)
        self.transport.command(self.root, "git", "push", "-u", "origin", "main")
        self.git("switch", "-qc", "codex/existing")
        (self.root / "README.md").write_text("# Candidate\n\nExisting authored documentation.\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "existing candidate")
        self.sha = self.git("rev-parse", "HEAD")
        self.repository = SubprocessRepositoryClient(self.transport)
        self.database = self.data / server.SERVER_DATABASE_FILENAME
        with redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(["bind-repository", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo", "--path", str(self.root)]), 0)
        self.prompt = self.area / "prompt.md"
        self.prompt.write_text("Continue the selected existing documentation candidate through current validation and independent assurance.\n")
        self.store = StateStore(self.root / ".engineering/engineering-runs", central_database=self.database, emit_local_projection=False)
        self.selection = {"version": "1.0", "project_id": "project", "repository_id": "repo", "repository": "qualification/managed",
                          "branch": "codex/existing", "candidate_sha": self.sha, "base_sha": self.base, "run_id": "adopt-run",
                          "repair_ordinal": 0, "validation_profile_digest": profile_digest(self.root, self.sha, 0)}
        self.readiness = patch("engineering_platform.execution_host.provider_readiness_failures", return_value=())
        self.readiness.start()
        self.addCleanup(self.readiness.stop)

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.root, check=True, text=True, capture_output=True).stdout.strip()

    def stop_host(self, runner):
        if runner.lease_heartbeat:
            from engineering_platform.execution_lease import release
            release(self.root, runner.lease_heartbeat.stop(), central_database=self.database)
            runner.active_lease = runner.lease_heartbeat = None

    def lifecycle_adapters(self):
        fixture = self
        class Agent(FakeAgent):
            def invoke(self, root, prompt):
                self.prompts.append(prompt)
                if "Local repository validation gate" not in prompt:
                    raise SystemExit("external provider handoff")
                return AgentResult("COMPLETE", fixture.git("branch", "--show-current"), commit_sha=fixture.git("rev-parse", "HEAD"))
            def review(self, root, selected, objective, evidence=None):
                return mandatory_review_result(selected.reviewer, objective)
        class GitHub:
            def __init__(self): self.candidates, self.creates = [], 0
            def publication_candidates(self, *args): return self.candidates
            def create_draft_publication(inner, repository, branch, base, title, body):
                sha = fixture.git("rev-parse", "HEAD")
                # Only the deterministic external effect is inline; preparation
                # and transport delay remain outside canonical serialization.
                with process_effect_start():
                    inner.creates += 1
                    inner.candidates = [PublicationCandidate(71, repository, repository, branch, "main", sha, "OPEN", True)]
            def pull_request(self, number):
                candidate = self.candidates[0]
                return PullRequestEvidence(number, "OPEN", True, True, head_branch=candidate.branch, base_branch="main", head_sha=candidate.head_sha)
            def ready(self, number): pass
            def normalize_markdown_body(self, number): return False
            def find_open_pull_request(self, *args): return None
        return Agent(AgentResult("COMPLETE")), GitHub()

    def test_historical_adoption_and_publication_do_not_redirect_later_transactions(self):
        agent, github = self.lifecycle_adapters()
        runner = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        self.addCleanup(self.stop_host, runner)
        waiting = runner.run(self.prompt, run_id="adopt-run", owner_authorized=True, managed_candidate=self.selection)
        self.assertEqual(waiting.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.git("switch", "main")
        self.git("merge", "--ff-only", "codex/existing")
        self.transport.command(self.root, "git", "push", "origin", "main")
        for kind, start in (("FINALIZATION", runner._start_finalization), ("RECONCILIATION", runner._start_automatic_reconciliation)):
            with self.subTest(kind=kind):
                # The public run owns an exclusive lease before this private
                # post-merge entry. Reproduce that real contract here; the
                # passive operator wait has deliberately released its lease.
                if kind in {"FINALIZATION", "RECONCILIATION"}:
                    from engineering_platform.execution_lease import acquire, LeaseHeartbeat
                    runner.active_lease = acquire(self.root, waiting.run_id,
                        identity=runner.host_identity, instance_id=runner.host_instance_id,
                        process_id=os.getpid(), central_database=self.database)
                    runner.lease_heartbeat = LeaseHeartbeat(self.root, runner.active_lease,
                        central_database=self.database)
                    runner.lease_heartbeat.start()
                # The ordinary entry saves its checkpoint before the external
                # provider is interrupted. All host/state services remain real.
                with self.assertRaisesRegex(SystemExit, "external provider handoff"):
                    start(waiting, 71) if kind == "FINALIZATION" else start(self.store.load("adopt-run"))
                self.stop_host(runner)
                checkpoint = self.store.load("adopt-run")
                self.assertEqual(checkpoint.transaction_kind, kind)
                self.assertEqual(checkpoint.publication_intent, waiting.publication_intent)
                self.assertEqual(checkpoint.managed_candidate_adoption, self.selection)
                resumed = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
                self.addCleanup(self.stop_host, resumed)
                if kind == "FINALIZATION":
                    with self.assertRaisesRegex(SystemExit, "external provider handoff"):
                        resumed.run(self.prompt, run_id="adopt-run", resume=True)
                else:
                    result = resumed.run(self.prompt, run_id="adopt-run", resume=True)
                    # Existing reconciliation fails closed without its own
                    # finalization-merge/PR receipt, never republishes work.
                    self.assertEqual(result.next_action, "reconciliation_recovery_evidence_required")
                after = self.store.load("adopt-run")
                self.assertEqual(after.transaction_kind, kind)
                self.assertEqual(after.terminal, kind == "RECONCILIATION")
                self.assertEqual(after.publication_intent, waiting.publication_intent)
                self.assertEqual(github.creates, 1)
                self.stop_host(resumed)

    def recovered_repair_fixture(self, *, returned_pr=None):
        from engineering_platform.provider_recovery import (
            create_recovery_available, persist_recovery_agent_result, transition_recovery_state,
        )
        agent, github = self.lifecycle_adapters()
        state = TransactionState("adopt-run", "qualification/managed", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
                                 owner_authorized=True, last_verified_sha=self.sha, implementation_head_sha=self.sha)
        state = verify_selection(selection=self.selection, state=state, root=self.root, repository=self.repository,
                                 central_database=self.database, owner_authorized=True)
        self.store.save(state)
        runner = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        state, admission_error = runner._confirm_deterministic_admission(state)
        self.assertIsNone(admission_error)
        from engineering_platform.execution_lease import acquire, LeaseHeartbeat
        runner.active_lease = acquire(self.root, state.run_id, identity=runner.host_identity,
            instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=self.database)
        runner.lease_heartbeat = LeaseHeartbeat(self.root, runner.active_lease, central_database=self.database)
        runner.lease_heartbeat.start()
        self.addCleanup(self.stop_host, runner)
        with self.assertRaisesRegex(SystemExit, "external provider handoff"):
            runner._repair(state, "local validation failed. Correct the documentation heading.")
        self.stop_host(runner)
        reserved = self.store.load("adopt-run")
        self.assertEqual((reserved.repair_iterations, reserved.repair_audit[-1]["outcome"]), (1, "planned"))
        # The interrupted external provider returned its committed candidate
        # through the existing immutable result/recovery services.
        (self.root / "README.md").write_text("# Repaired candidate\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "same reserved repair")
        repaired = self.git("rev-parse", "HEAD")
        original = runner._persist_provider_invocation(reserved, phase="REPAIR", role="agent")
        self.assertIsNotNone(original)
        recovery = create_recovery_available(self.root, run_id="adopt-run", triggering_invocation_id=original,
            lifecycle_phase="REPAIR_AGENT", branch="codex/existing", worktree_identity=str(self.root), lease_id=None,
            central_database=self.database)
        reference = persist_recovery_agent_result(self.root, run_id="adopt-run", invocation_id=recovery["replacement_invocation_id"],
            result=AgentResult("COMPLETE", "codex/existing", pull_request=returned_pr, commit_sha=repaired), central_database=self.database,
            artifact_root=self.data / "artifacts")
        self.assertTrue(transition_recovery_state(self.root, run_id="adopt-run", expected="RECOVERY_AVAILABLE", target="RECOVERED",
            result="SUCCESS", result_evidence_ref=reference, central_database=self.database))
        return agent, github, reserved, repaired

    def test_recovered_adopted_repair_consumes_same_reservation_then_qualifies_new_sha(self):
        agent, github, reserved, repaired = self.recovered_repair_fixture()
        resumed = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        self.addCleanup(self.stop_host, resumed)
        after = resumed.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual(after.phase, "WAIT_FOR_OPERATOR_MERGE", after)
        self.assertEqual(after.repair_iterations, 1)
        self.assertEqual(len(after.repair_audit), 1)
        self.assertEqual(after.repair_audit[0]["repair_id"], reserved.repair_audit[0]["repair_id"])
        self.assertEqual(after.repair_audit[0]["commit_sha"], repaired)
        self.assertTrue(any(item["commit_sha"] == repaired and item["phase"] == "REPAIR_AGENT"
                            and item["description"] == "pull_request_repair_commit_verified" for item in after.commit_evidence))
        self.assertEqual(after.publication_intent["candidate_sha"], repaired)
        self.assertEqual(after.assurance_profile["candidate_sha"], repaired)
        self.assertEqual(after.managed_candidate_adoption, self.selection)
        self.assertEqual(github.creates, 1)
        self.assertEqual(len(agent.prompts), 1)  # only the interrupted repair requires reasoning

    def test_current_committed_declaration_must_still_match_the_central_binding(self):
        declaration = self.root / ".engineering-platform/repository.json"
        payload = json.loads(declaration.read_text())
        payload["project"]["id"] = "foreign-project"
        payload["repository"]["id"] = "foreign-repository"
        declaration.write_text(json.dumps(payload))
        self.git("add", str(declaration))
        self.git("commit", "-qm", "changed declaration")
        sha = self.git("rev-parse", "HEAD")
        selection = {**self.selection, "candidate_sha": sha, "validation_profile_digest": profile_digest(self.root, sha, 0)}
        state = TransactionState("adopt-run", "qualification/managed", str(self.prompt), "INITIALIZE", owner_authorized=True)
        with self.assertRaisesRegex(RuntimeError, "project binding is unavailable"):
            verify_selection(selection=selection, state=state, root=self.root, repository=self.repository,
                             central_database=self.database, owner_authorized=True)

    def unbind(self):
        from engineering_platform.local_repository_binding import unbind_local_repository
        with sqlite_connection(self.database) as connection:
            unbind_local_repository(connection, project_id="project", repository_id="repo")

    def test_unbound_prepared_publication_cannot_create(self):
        agent, github = self.lifecycle_adapters()
        runner = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        execute = self.transport.execute
        def interrupted_transport(root, *args):
            if "ls-remote" in args:
                raise SystemExit("external Git transport interrupted")
            return execute(root, *args)
        with patch.object(self.transport, "execute", side_effect=interrupted_transport):
            try:
                with self.assertRaisesRegex(SystemExit, "external Git transport"):
                    runner.run(self.prompt, run_id="adopt-run", owner_authorized=True, managed_candidate=self.selection)
            finally:
                self.stop_host(runner)
        self.assertEqual(self.store.load("adopt-run").publication_intent["status"], "PREPARED")
        self.unbind()
        resumed = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        after = resumed.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual((after.phase, after.next_action), ("BLOCKED", "managed_candidate_adoption_invalid"))
        self.assertEqual(github.creates, 0)

    def test_unbound_recovered_repair_with_pr_cannot_revalidate_or_publish(self):
        agent, github, _, _ = self.recovered_repair_fixture(returned_pr=71)
        self.unbind()
        resumed = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        self.addCleanup(self.stop_host, resumed)
        after = resumed.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual((after.phase, after.next_action), ("BLOCKED", "managed_candidate_adoption_invalid"))
        self.assertEqual((len(agent.prompts), github.creates, after.pull_request), (1, 0, None))
        self.assertFalse(after.assurance_reviews)

    def test_adopted_repair_cannot_bypass_durable_publication_with_provider_pr(self):
        agent, github, _, _ = self.recovered_repair_fixture(returned_pr=71)
        resumed = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        self.addCleanup(self.stop_host, resumed)
        after = resumed.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual((after.phase, after.next_action), ("BLOCKED", "managed_repair_publication_requires_host"))
        self.assertEqual((len(agent.prompts), github.creates, after.pull_request), (1, 0, None))

    def test_old_repair_receipt_cannot_redirect_validation_or_the_next_repair_round(self):
        agent, github, _, repaired = self.recovered_repair_fixture()
        restarted = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        self.addCleanup(self.stop_host, restarted)
        after = restarted.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual(len(agent.prompts), 1)  # no mechanical model assessment
        self.assertEqual(after.phase, "WAIT_FOR_OPERATOR_MERGE", after)
        self.assertEqual(after.publication_intent["candidate_sha"], repaired)
        self.assertEqual((after.repair_iterations, github.creates), (1, 1))
        # A later reserved round must invoke its own repair prompt, never
        # consume the completed earlier replacement merely because phase agrees.
        from engineering_platform.execution_lease import acquire, LeaseHeartbeat
        restarted.active_lease = acquire(self.root, after.run_id, identity=restarted.host_identity,
            instance_id=restarted.host_instance_id, process_id=os.getpid(), central_database=self.database)
        restarted.lease_heartbeat = LeaseHeartbeat(self.root, restarted.active_lease, central_database=self.database)
        restarted.lease_heartbeat.start()
        with self.assertRaisesRegex(SystemExit, "external provider handoff"):
            restarted._repair(after, "quality failed. Correct the new bounded finding.")
        self.assertEqual(self.store.load("adopt-run").repair_iterations, 2)
        self.assertIn("Repair objective: quality failed.", agent.prompts[-1])
        self.assertTrue(all(agent.prompts))

    def test_typed_owner_adoption_validates_reviews_and_recovers_lost_ack(self):
        selection = self.selection
        class AssuranceAgent(FakeAgent):
            def invoke(self, root, prompt):
                if "Local repository validation gate" not in prompt:
                    raise AssertionError("initial implementation must never replay")
                self.prompts.append(prompt)
                return AgentResult("COMPLETE", selection["branch"], commit_sha=selection["candidate_sha"])
            def review(self, root, selected, objective, evidence=None):
                return mandatory_review_result(selected.reviewer, objective)
        class GitHub:
            def __init__(self): self.candidates, self.creates = [], 0
            def publication_candidates(self, *args): return self.candidates
            def create_draft_publication(inner, *args):
                inner.creates += 1
                inner.candidates = [PublicationCandidate(71, "qualification/managed", "qualification/managed", selection["branch"], "main", selection["candidate_sha"], "OPEN", True)]
                raise SystemExit("accepted, crash before receipt")
            def pull_request(self, number):
                return PullRequestEvidence(number, "OPEN", True, True, head_branch=selection["branch"], base_branch="main", head_sha=selection["candidate_sha"])
            def ready(self, number): pass
            def normalize_markdown_body(self, number): return False
        agent, github = AssuranceAgent(AgentResult("COMPLETE")), GitHub()
        runner = EngineeringRunner(self.root, self.store, self.repository, github, agent, lambda _: None)
        try:
            with self.assertRaises(SystemExit):
                runner.run(self.prompt, run_id="adopt-run", owner_authorized=True, managed_candidate=self.selection)
        finally:
            if runner.lease_heartbeat:
                from engineering_platform.execution_lease import release
                release(self.root, runner.lease_heartbeat.stop(), central_database=self.database)
                runner.active_lease = runner.lease_heartbeat = None
        checkpoint = self.store.load("adopt-run")
        self.assertEqual(checkpoint.publication_intent["status"], "CREATE_UNCERTAIN")
        self.assertEqual([item["status"] for item in checkpoint.assurance_reviews], ["PASS", "PASS"])
        validation = load_validation_context(self.root, "adopt-run", central_database=self.database)
        self.assertEqual(validation["candidate_sha"], self.sha)
        self.assertTrue(all(item["result"] == "PASS" for item in validation["controls"].values()))
        restarted = EngineeringRunner(self.root, StateStore(self.store.directory, central_database=self.database, emit_local_projection=False), self.repository, github, agent, lambda _: None)
        result = restarted.run(self.prompt, run_id="adopt-run", resume=True)
        self.assertEqual(result.pull_request, 71)
        self.assertEqual(result.phase, "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(github.creates, 1)
        self.assertEqual(len(agent.prompts), 0)
        self.assertEqual(result.managed_candidate_adoption, self.selection)
        self.assertEqual((result.repair_iterations, self.git("rev-parse", "HEAD")), (0, self.sha))

    def process_restart(self, boundary):
        spec = self.area / "process-input.json"
        spec.write_text(json.dumps({"root": str(self.root), "data": str(self.data),
                                   "remote": str(self.remote), "prompt": str(self.prompt),
                                   "selection": self.selection}))
        command = (sys.executable, "-m", "tests.engineering.deterministic_delivery_process",
                   str(spec))
        interrupted = subprocess.run((*command, "start", boundary), text=True, capture_output=True, timeout=60)
        self.assertEqual(interrupted.returncode, 73, interrupted.stdout + interrupted.stderr)
        checkpoint = self.store.load("adopt-run")
        validation = load_validation_context(self.root, "adopt-run", central_database=self.database)
        self.assertEqual(validation["candidate_sha"], self.sha)
        self.assertTrue(all(item["result"] == "PASS" for item in validation["controls"].values()))
        # Preserve the actual product lease policy: a hard crash cannot release
        # its heartbeat. Wait for its real persisted expiry, without resetting
        # leases, clocks, repair consumption or deterministic decision logic.
        with sqlite_connection(self.database) as connection:
            expiry = connection.execute(
                "SELECT expires_at FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE'",
                ("adopt-run",)).fetchone()[0]
        delay = (datetime.fromisoformat(expiry) - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay + .1)
        resumed = subprocess.run((*command, "resume", boundary), text=True, capture_output=True, timeout=60)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        after = self.store.load("adopt-run")
        self.assertEqual((after.phase, after.pull_request, after.repair_iterations),
                         ("WAIT_FOR_OPERATOR_MERGE", 71, 0))
        self.assertEqual(after.publication_intent["candidate_sha"], self.sha)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.sha)
        current = load_validation_context(self.root, "adopt-run", central_database=self.database)
        self.assertEqual(current["controls"], validation["controls"])
        with sqlite_connection(self.database) as connection:
            invocations = connection.execute(
                "SELECT invocation_id,role,completed_at,churn,input_tokens,duration_ms FROM provider_invocations WHERE run_id=? ORDER BY ordinal",
                ("adopt-run",)).fetchall()
        terminal = [row for row in invocations if row[2] is not None]
        self.assertEqual([row[1] for row in terminal], ["quality", "security"])
        for row in terminal:
            binding = json.loads(row[3])
            self.assertEqual(binding["candidate_sha"], self.sha)
            self.assertEqual(binding["assurance_profile_digest"], after.assurance_profile["digest"])
            self.assertEqual(binding["canonical_invocation_id"], row[0])
            self.assertIsNone(row[4])  # unobserved usage is not measured zero
            self.assertIsNone(row[5])
            self.assertTrue(any(item[0] == row[0] + ":dispatch" and item[2] is None
                                for item in invocations))
        self.assertEqual(len(invocations), 4 if boundary == "publication" else 5)
        self.assertEqual(checkpoint.repair_iterations, after.repair_iterations)

    def test_real_process_crash_after_publication_acceptance_resumes_without_second_create(self):
        self.process_restart("publication")

    def test_real_process_crash_at_assurance_dispatch_resumes_without_implementation_or_validation_replay(self):
        self.process_restart("assurance")

    def test_selection_rejects_foreign_stale_changed_authority_and_lineage(self):
        state = TransactionState("adopt-run", "qualification/managed", str(self.prompt), "INITIALIZE", owner_authorized=True)
        def verify(selection, candidate=state, authority=True):
            return verify_selection(selection=selection, state=candidate, root=self.root, repository=self.repository,
                                    central_database=self.database, owner_authorized=authority)
        accepted = verify(self.selection)
        self.assertEqual(accepted.managed_candidate_adoption, self.selection)
        for change in ({"run_id": "other"}, {"project_id": "foreign"}, {"repository_id": "foreign"},
                       {"repository": "foreign/repo"}, {"branch": "codex/other"}, {"candidate_sha": "b" * 40},
                       {"base_sha": "c" * 40}, {"repair_ordinal": 1}, {"validation_profile_digest": "sha256:" + "d" * 64}):
            with self.subTest(change=change), self.assertRaises(RuntimeError): verify({**self.selection, **change})
        with self.assertRaises(RuntimeError): verify(self.selection, authority=False)
        with self.assertRaises(RuntimeError): verify(self.selection, replace(state, owner_authorized=False))
        with self.assertRaises(RuntimeError): verify(self.selection, replace(accepted, repair_iterations=1))
        with patch("engineering_platform.platform_admin.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(RuntimeError, "actual installation owner"): verify(self.selection)
        other = replace(state, run_id="another-run", branch="codex/existing")
        self.store.save(other)
        with self.assertRaisesRegex(RuntimeError, "another run"): verify(self.selection)
        (self.root / "README.md").write_text("changed without commit")
        with self.assertRaises(RuntimeError): verify(self.selection)
        self.assertEqual(self.git("branch", "--show-current"), "codex/existing")

    def test_schema_and_immutable_checkpoint_protect_selection(self):
        for change in ({"version": "2"}, {"candidate_sha": "short"}, {"branch": "main"},
                       {"repair_ordinal": True}, {"run_id": "invalid id"}, {"owner_authorized": True}):
            with self.subTest(change=change), self.assertRaises(ValueError): parse_selection({**self.selection, **change})
        state = TransactionState("adopt-run", "qualification/managed", str(self.prompt), "INITIALIZE", owner_authorized=True,
                                 managed_candidate_adoption=self.selection, managed_adoption_actor=f"local-uid:{os.geteuid()}")
        self.store.save(state)
        self.assertEqual(self.store.load("adopt-run").managed_candidate_adoption, self.selection)
        with self.assertRaises(StateError): self.store.save(replace(state, managed_candidate_adoption=None))

    def test_read_only_selection_command_and_same_run_repair_lineage(self):
        from engineering_platform.managed_adoption import main
        stream = io.StringIO()
        with patch("sys.argv", ["managed-adoption", "--project-id", "project", "--repository-id", "repo", "--run-id", "adopt-run"]), \
                patch("engineering_platform.execution_repository.SubprocessRepositoryClient", return_value=self.repository), \
                patch("pathlib.Path.cwd", return_value=self.root), redirect_stdout(stream):
            main()
        self.assertEqual(json.loads(stream.getvalue()), self.selection)
        self.assertEqual(self.git("status", "--porcelain"), "")
        (self.root / "README.md").write_text("# Repaired candidate\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "bounded repair")
        repaired = self.git("rev-parse", "HEAD")
        state = TransactionState("adopt-run", "qualification/managed", str(self.prompt), "LOCAL_REPOSITORY_VALIDATION",
                                 owner_authorized=True, managed_candidate_adoption=self.selection,
                                 repair_iterations=1, implementation_head_sha=repaired,
                                 repair_audit=({"iteration": "1", "commit_sha": repaired},))
        accepted = verify_selection(selection=self.selection, state=state, root=self.root, repository=self.repository,
                                    central_database=self.database, owner_authorized=True)
        self.assertEqual(accepted.repair_iterations, 1)
        self.assertEqual(accepted.managed_candidate_adoption, self.selection)


if __name__ == "__main__": unittest.main()
