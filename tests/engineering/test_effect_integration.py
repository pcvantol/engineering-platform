"""Review regressions across the real bounded-effect integration boundaries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from engineering_platform import effect_workspace as workspace, merge_delegation, submission_service
from engineering_platform.agent_state import StateStore
from engineering_platform.effect_contract import EffectContractError
from engineering_platform.execution_errors import RunnerError
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_repository import SubprocessRepositoryClient, GhCliClient
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
from engineering_platform.platform_version import RunnerCompatibility
from engineering_platform.providers import CodexCliProvider
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_effect_execution as fixtures


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class EffectIntegrationTests(unittest.TestCase):
    setUp = fixtures.EffectLifecycleTests.setUp
    _stop_http = fixtures.EffectLifecycleTests._stop_http
    http = fixtures.EffectLifecycleTests.http
    submit = fixtures.EffectLifecycleTests.submit

    def state(self, run_id):
        return StateStore(self.root / ".engineering/engineering-runs", central_database=self.database,
                          emit_local_projection=False).load(run_id)

    def dispatcher(self, transport_type=fixtures._LocalGitHubTransport, compatibility=None):
        bare = self.base / "external.git"
        workspace.git(self.base, "clone", "--bare", str(self.root), str(bare))
        remote = transport_type(bare)
        def factory(root):
            runner = EngineeringRunner(root, StateStore(root / ".engineering/engineering-runs",
                central_database=self.database, emit_local_projection=False),
                SubprocessRepositoryClient(fixtures._LocalGitTransport(bare)),
                GhCliClient(remote, repository="fixture/djconnect"), CodexCliClient(CodexCliProvider()))
            if compatibility is not None:
                runner.compatibility = compatibility
            return runner
        cli = self.base / "gh"
        cli.write_text("#!/bin/sh\nexit 0\n"); cli.chmod(0o700)
        environment = patch.dict(os.environ, {"EP_GITHUB_CLI_EXECUTABLE": str(cli)})
        environment.start(); self.addCleanup(environment.stop)
        return ParityLifecycleDispatcher(self.fixture.root, runner_factory=factory), remote

    def test_readonly_observations_preserve_index_and_never_invoke_fsmonitor(self):
        source = self.root / "docs/design.md"
        metadata = source.stat()
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000))
        marker = self.base / "fsmonitor-was-invoked"
        hook = self.base / "fsmonitor"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n"); hook.chmod(0o700)
        workspace.git(self.root, "config", "core.fsmonitor", str(hook))
        before = files(self.root)
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        self.assertEqual(receipt.state, "COMPLETE")
        self.assertEqual([path for path, data in files(self.root).items() if before.get(path) != data], [])
        self.assertFalse(marker.exists())

    def test_replacement_objects_cannot_rebind_snapshot_or_qualified_report(self):
        original = (self.root / "docs/design.md").read_bytes()
        blob = workspace.git(self.root, "rev-parse", self.revision + ":docs/design.md").decode().strip()
        replacement = workspace.git(self.root, "hash-object", "-w", "--stdin", input_bytes=b"# Forged source\n").decode().strip()
        workspace.git(self.root, "replace", blob, replacement)
        before = files(self.root)
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(self.submit(fixtures.effect(source_revision=self.revision)))
        self.assertEqual(receipt.state, "COMPLETE")
        source = self.fixture.root / "artifacts/effects" / receipt.run_id / "source/docs/design.md"
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(files(self.root), before)
        workspace.git(self.root, "replace", "-d", blob)
        bad_docs = workspace.git(self.root, "mktree", input_bytes=f"100644 blob {replacement}\tdesign.md\n".encode()).decode().strip()
        real_docs = workspace.git(self.root, "rev-parse", self.revision + ":docs").decode().strip()
        bad_root = workspace.git(self.root, "mktree", input_bytes=f"040000 tree {bad_docs}\tdocs\n".encode()).decode().strip()
        bad_commit = workspace.git(self.root, "commit-tree", bad_root, input_bytes=b"Forged tree\n").decode().strip()
        for kind, old, new in (("tree", real_docs, bad_docs), ("commit", self.revision, bad_commit)):
            workspace.git(self.root, "replace", old, new)
            destination = self.base / kind
            workspace.snapshot(self.root, destination, fixtures.effect(source_revision=self.revision))
            self.assertEqual((destination / "docs/design.md").read_bytes(), original)
            workspace.git(self.root, "replace", "-d", old)

    def test_missing_promised_blob_is_not_fetched_or_written_to_target(self):
        workspace.git(self.root, "config", "uploadpack.allowFilter", "true")
        partial = self.base / "partial"
        workspace.git(self.base, "clone", "--filter=blob:none", "--no-checkout", self.root.as_uri(), str(partial))
        workspace.git(partial, "read-tree", "HEAD")
        for relative in ("BOOTSTRAP.md", "docs/design.md", "tests/test_fixture.py", ".engineering-platform/repository.json"):
            workspace.write_file(partial, relative, (self.root / relative).read_bytes())
        before = files(partial)
        self.assertFalse(workspace.git(partial, "status", "--porcelain"))
        with self.assertRaisesRegex(EffectContractError, "GIT_OBSERVATION_FAILED"):
            workspace.snapshot(partial, self.base / "partial-source", fixtures.effect(source_revision=self.revision))
        self.assertEqual(files(partial), before)

    def test_oversized_committed_blob_is_rejected_before_reading_its_body(self):
        workspace.write_file(self.root, "docs/large.md", b"x" * 1_048_577)
        workspace.git(self.root, "add", "."); workspace.git(self.root, "commit", "-m", "Oversized source")
        revision = workspace.git(self.root, "rev-parse", "HEAD").decode().strip()
        blob = workspace.git(self.root, "rev-parse", revision + ":docs/large.md").decode().strip()
        # Observe real Git I/O; never replace its response or the limit decision.
        with patch.object(workspace, "git", wraps=workspace.git) as observed:
            with self.assertRaisesRegex(EffectContractError, "SOURCE_LIMIT"):
                workspace.snapshot(self.root, self.base / "oversized", fixtures.effect(source_revision=revision))
        self.assertIn((self.root, "cat-file", "-s", blob), [call.args for call in observed.call_args_list])
        self.assertNotIn((self.root, "cat-file", "blob", blob), [call.args for call in observed.call_args_list])

    def test_incompatible_platform_is_refused_before_state_or_provider(self):
        dispatcher, _ = self.dispatcher(compatibility=RunnerCompatibility(storage_schemas=frozenset({2})))
        with self.assertRaises(RunnerError):
            dispatcher.dispatch(self.submit(fixtures.effect(source_revision=self.revision)))
        self.assertFalse((self.prefix / "calls.jsonl").exists())
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM engineering_transactions").fetchone()[0], 0)

    def test_nonblocking_findings_remain_visible_without_consuming_repairs(self):
        source = self.provider.read_text().replace("'findings':[]", "'findings':[{'id':'style-1','category':'STYLE',"
            "'criterion':'advisory','observation':'The document could use a shorter heading.',"
            "'severity':'LOW','confidence':'HIGH','evidence_ref':request['subject']['subject_digest']}]")
        self.provider.write_text(source)
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        self.assertEqual(receipt.state, "COMPLETE")
        report = self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")
        fixtures.validate_public_schema("effect-result-v1.1", report)
        self.assertEqual(report["repair_rounds"]["used"], 0)
        self.assertTrue(report["effect_qualified"])
        for review in report["assurance_reviews"]:
            self.assertFalse(review["findings"][0]["blocking"])
            self.assertEqual(review["findings"][0]["disposition"], "NON_BLOCKING")

    def test_delegated_protected_merge_only_fetches_into_owned_delivery(self):
        dispatcher, remote = self.dispatcher(_DelegatedGitHub)
        remote.base_revision = self.revision
        delegation = "a" * 32
        payload = self.fixture.forge_planning_context_payload("fme-delegation")
        payload["constraints"]["effect_contract"] = fixtures.effect("DOCUMENTATION_ONLY", "GIT",
            source_revision=self.revision, write_paths=["docs/report.md"])
        payload["constraints"]["forge_execution"]["execution_constraints"] = ["ep-merge-delegation:" + delegation]
        with sqlite_connection(self.database) as connection:
            credential = submission_service.issue_consumer_credential(connection, consumer_id="forge", project_id="djconnect")["credential"]
            merge_delegation.reserve(connection, delegation_id=delegation, actor_reference="local-uid:501",
                project_id="djconnect", repository_id="djconnect", github_repository="fixture/djconnect",
                base_branch="main", roles=("IMPLEMENTATION",), expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
            merge_delegation.activate(connection, delegation_id=delegation, mission_id=payload["mission_id"],
                mission_revision="1", actor_reference="local-uid:501", github_repository="fixture/djconnect")
        submission = self.http("/v1/projects/djconnect/submissions", payload, credential=credential)["submission_id"]
        before = files(self.root)
        receipt = dispatcher.dispatch(submission)
        state = self.state(receipt.run_id)
        self.assertEqual(receipt.state, "COMPLETE", state.diagnostic)
        self.assertEqual(state.delegated_merge_actor_reference, "local-uid:501")
        self.assertEqual(remote.creates, 1)
        self.assertEqual(files(self.root), before)

    def test_hosted_failure_repairs_and_requalifies_the_same_pr(self):
        dispatcher, remote = self.dispatcher(_RepairGitHub)
        submission = self.submit(fixtures.effect("DOCUMENTATION_ONLY", "GIT", source_revision=self.revision,
                                                write_paths=["docs/report.md"]))
        before = files(self.root)
        receipt = dispatcher.dispatch(submission)
        state = self.state(receipt.run_id)
        self.assertEqual(state.phase, "WAIT_FOR_OPERATOR_MERGE", state.diagnostic)
        self.assertEqual(state.repair_iterations, 1)
        self.assertEqual(state.repair_audit[0]["origin"], "hosted")
        self.assertEqual(state.repair_audit[0]["outcome"], "submitted_for_recheck")
        self.assertEqual(state.repair_audit[0]["commit_sha"], state.effect_execution["attempts"][-1]["candidate_sha"])
        self.assertEqual(remote.creates, 1)
        self.assertEqual(len(state.effect_execution["attempts"]), 2)
        self.assertEqual(len((self.prefix / "calls.jsonl").read_text().splitlines()), 6)
        self.assertEqual(state.publication_intent["candidate_sha"], remote.first_head)
        self.assertNotEqual(remote.head, remote.first_head)
        remote.protected_merge()
        self.assertEqual(dispatcher.dispatch(submission).state, "COMPLETE")
        self.assertEqual(files(self.root), before)

    def test_owning_environment_command_and_script_run_with_disposable_outputs(self):
        declaration = self.root / ".engineering-platform/repository.json"
        entrypoints = (("command", "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"),
                       ("script", "bash scripts/validate.sh"))
        workspace.write_file(self.root, "src/input_module.py", b"BOUNDARY = 'operator-owned'\n")
        workspace.write_file(self.root, "tests/test_fixture.py", b"import pathlib,unittest,input_module\nclass Boundary(unittest.TestCase):\n def test_input(self):\n  self.assertEqual(input_module.BOUNDARY, 'operator-owned')\n  pathlib.Path('test-generated-output').write_text('disposable output')\n")
        workspace.write_file(self.root, "scripts/validate.sh", b"set -eu\npython3 -m compileall -q src tests\nPYTHONPATH=src python3 -m unittest discover -s tests -v\ngit diff --check\n")
        # Each owning declaration runs against a separate real candidate. A
        # completed first run is followed by a fresh accepted source identity.
        for kind, command in entrypoints:
            with self.subTest(kind=kind):
                raw = json.loads(declaration.read_text()); raw["validation"] = {"kind": kind, "entrypoint": command}
                declaration.write_text(json.dumps(raw))
                workspace.git(self.root, "add", "."); workspace.git(self.root, "commit", "-m", kind)
                self.revision = workspace.git(self.root, "rev-parse", "HEAD").decode().strip()
                bare = self.base / "external.git"
                if bare.exists():
                    import shutil
                    shutil.rmtree(bare)
                dispatcher, remote = self.dispatcher()
                effects = fixtures.effect("BOUNDED_REPOSITORY_CHANGE", "GIT", source_revision=self.revision,
                                          write_paths=["src/boundary.py"])
                submission = self.http("/v1/projects/djconnect/submissions", {"repository_id":"djconnect",
                    "producer":{"id":"cli","type":"CLI"}, "prompt":"Implement the bounded operator-owned source.",
                    "constraints":{"effect_contract":effects}})["submission_id"]
                before = files(self.root)
                receipt = dispatcher.dispatch(submission)
                state = self.state(receipt.run_id)
                self.assertEqual(state.phase, "WAIT_FOR_OPERATOR_MERGE", (state.diagnostic,
                    [(item["validation_id"], item["exit_code"]) for item in state.effect_execution["attempts"][-1]["controls"]]))
                controls = state.effect_execution["attempts"][-1]["controls"]
                self.assertEqual(controls[-1]["validation_id"], "repository_json")
                self.assertEqual(controls[-1]["status"], "PASS")
                delivery = self.fixture.root / "artifacts/effects" / receipt.run_id / "delivery-0"
                self.assertFalse((delivery / "test-generated-output").exists())
                self.assertFalse(list(delivery.rglob("__pycache__")))
                remote.protected_merge()
                self.assertEqual(dispatcher.dispatch(submission).state, "COMPLETE")
                self.assertEqual(files(self.root), before)

    def test_validator_mutating_its_inputs_cannot_claim_pass(self):
        declaration = self.root / ".engineering-platform/repository.json"
        raw = json.loads(declaration.read_text())
        raw["validation"] = {"kind": "command", "entrypoint":
            "python -c \"from pathlib import Path; Path('docs/design.md').write_text('Changed validation input')\""}
        declaration.write_text(json.dumps(raw))
        workspace.git(self.root, "add", "."); workspace.git(self.root, "commit", "-m", "Mutating validator fixture")
        self.revision = workspace.git(self.root, "rev-parse", "HEAD").decode().strip()
        dispatcher, remote = self.dispatcher()
        before = files(self.root)
        receipt = dispatcher.dispatch(self.submit(fixtures.effect("BOUNDED_REPOSITORY_CHANGE", "GIT",
            source_revision=self.revision, write_paths=["src/boundary.py"])))
        state = self.state(receipt.run_id)
        self.assertEqual(state.phase, "FAILED")
        self.assertEqual(state.repair_iterations, 3)
        self.assertEqual(remote.creates, 0)
        for attempt in state.effect_execution["attempts"]:
            self.assertEqual(attempt["controls"][-1]["exit_code"], 125)
            self.assertEqual(attempt["controls"][-1]["status"], "FAIL")
        self.assertEqual(files(self.root), before)
        self.assertFalse(list((self.fixture.root / "artifacts/effects" / receipt.run_id).glob("validation-scratch-*")))


class _RepairGitHub(fixtures._LocalGitHubTransport):
    def __init__(self, bare):
        super().__init__(bare)
        self.first_head = getattr(self, "first_head", None)

    def persist(self):
        super().persist()
        value = json.loads(self.ledger.read_text())
        value["first_head"] = self.first_head
        self.ledger.write_text(json.dumps(value))

    def github(self, *args):
        if args[:2] == ("pr", "view"):
            self.head = workspace.git(self.bare, "rev-parse", self.branch).decode().strip()
            self.first_head = self.first_head or self.head
            self.persist()
            result = json.loads(super().github(*args))
            if self.head == self.first_head:
                result["statusCheckRollup"][0]["conclusion"] = "FAILURE"
            return json.dumps(result)
        return super().github(*args)


class _DelegatedGitHub(fixtures._LocalGitHubTransport):
    def github(self, *args):
        if args[:2] == ("pr", "view"):
            value = json.loads(super().github(*args)); value["baseRefOid"] = self.base_revision
            return json.dumps(value)
        if args[0] == "api" and "PUT" in args:
            self.protected_merge(); return json.dumps({"merged": True, "sha": self.head})
        if args[0] == "api":
            endpoint = args[1]
            if endpoint.endswith("/protection/required_status_checks"):
                return json.dumps({"contexts": ["required"], "checks": [], "strict": True})
            if endpoint.endswith("/protection/required_pull_request_reviews"):
                return json.dumps({"required_approving_review_count": 1})
            if endpoint.endswith("/reviews?per_page=100"):
                return json.dumps([{"user": {"login": "independent"}, "state": "APPROVED", "commit_id": self.head}])
            if endpoint.endswith("/pulls/17"):
                return json.dumps({"number": 17, "state": "open", "draft": False, "head": {"sha": self.head},
                                   "base": {"ref": "main"}, "user": {"login": "producer"}})
        return super().github(*args)
