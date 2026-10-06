"""FME durable recovery and integrity failures through the real public composition."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.error import HTTPError

import engineering_platform
from engineering_platform import effect_contract as contract, effect_evidence, effect_readback, effect_state
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_effect_execution as fixtures


class EffectRecoveryTests(unittest.TestCase):
    setUp = fixtures.EffectLifecycleTests.setUp
    _stop_http = fixtures.EffectLifecycleTests._stop_http
    http = fixtures.EffectLifecycleTests.http
    submit = fixtures.EffectLifecycleTests.submit

    def complete(self):
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        dispatch = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        self.assertEqual(dispatch.state, "COMPLETE")
        store = StateStore(self.root / ".engineering/engineering-runs", central_database=self.database, emit_local_projection=False)
        return submission, dispatch, store, store.load(dispatch.run_id)

    def test_invalid_compositions_rejected_before_acceptance(self):
        from engineering_platform.validation_profile import producer_profile_payload
        variants = [
            {"effect_contract": None},
            {"effect_contract": fixtures.effect(source_revision=self.revision), "validation_profile": None},
            {"effect_contract": fixtures.effect(source_revision=self.revision), "validation_profile": producer_profile_payload("FULL")},
            {"effect_contract": fixtures.effect(source_revision=self.revision), "parallel_action_intake": {}},
            {"effect_contract": fixtures.effect(source_revision=self.revision),
             "repository_revision_binding": {"requested_revision": "b" * 40, "allowed_baseline_revision": None}},
            {"effect_contract": fixtures.effect(source_revision=self.revision),
             "forge_execution": {"execution_constraints": ["ep-delivery-control-validation:1"]}},
            {"effect_contract": fixtures.effect(source_revision=self.revision),
             "forge_execution": {"execution_constraints": ["ep-merge-delegation:" + "a" * 32]}},
            {"effect_contract": fixtures.effect(source_revision=self.revision), "forge_execution": "malformed"},
        ]
        for constraints in variants:
            with self.subTest(constraints=constraints), self.assertRaises(HTTPError) as denied:
                self.http("/v1/projects/djconnect/submissions", {
                    "repository_id": "djconnect", "producer": {"id": "cli", "type": "CLI"},
                    "prompt": "Assess the bounded questions.", "constraints": constraints})
            self.assertEqual(denied.exception.code, 400); denied.exception.close()
        with self.assertRaises(HTTPError) as denied:
            self.http("/v1/projects/djconnect/submissions", {
                "repository_id": "djconnect", "producer": {"id": "foreign", "type": "CLI"},
                "prompt": "Assess the bounded questions.", "constraints": {"effect_contract": fixtures.effect(source_revision=self.revision)}})
        self.assertEqual(denied.exception.code, 403); denied.exception.close()
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM ep_submissions").fetchone()[0], 0)

    def test_current_authority_revocation_blocks_without_provider(self):
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        self.assertEqual(self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")["outcome"], "NOT_STARTED")
        with sqlite_connection(self.database) as connection:
            connection.execute("UPDATE ep_consumer_registrations SET status='DISABLED' WHERE consumer_id='cli'")
        with self.assertRaisesRegex(contract.EffectContractError, "AUTHORITY_INACTIVE"):
            ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM ep_parity_lifecycle_dispatches WHERE submission_id=?", (submission,)).fetchone()[0], "BLOCKED")
        self.assertFalse((self.prefix / "calls.jsonl").exists())

    def test_receipts_cannot_be_qualified_by_checkpoint_claims(self):
        _, dispatch, _, state = self.complete()
        directory = self.fixture.root / "artifacts/effects" / dispatch.run_id
        variants = []
        for key, value in (("command_id", "invented"), ("profile_digest", "sha256:" + "b" * 64)):
            checkpoint = deepcopy(state.effect_execution)
            checkpoint["attempts"][0]["controls"][0][key] = value
            variants.append(checkpoint)
        checkpoint = deepcopy(state.effect_execution)
        checkpoint["attempts"][0]["controls"][0].update(exit_code=7, status="FAIL")
        variants.append(checkpoint)
        checkpoint = deepcopy(state.effect_execution)
        checkpoint["attempts"][0]["reviews"][0]["subject"]["binding_digest"] = "sha256:" + "b" * 64
        variants.append(checkpoint)
        for checkpoint in variants:
            with self.subTest(), self.assertRaises(contract.EffectContractError):
                effect_evidence.verify(self.database, checkpoint, directory)
        review = directory / "review-0-quality.json"
        review.write_text("{}")
        with self.assertRaisesRegex(contract.EffectContractError, "INTEGRITY_FAILED"):
            effect_evidence.verify(self.database, state.effect_execution, directory)

    def test_readback_rejects_unbound_result_and_missing_qualification(self):
        submission, dispatch, _, state = self.complete()
        with self.assertRaisesRegex(contract.EffectContractError, "QUALIFICATION_INCOMPLETE"):
            checkpoint = deepcopy(state.effect_execution)
            checkpoint["attempts"][0]["reviews"] = []
            effect_readback.projection(self.database, replace(state, effect_execution=checkpoint))
        changed = deepcopy(state.effect_execution)
        changed["identity"]["engineering_action_id"] = "wrong-action"
        with self.assertRaisesRegex(contract.EffectContractError, "BINDING_MISMATCH"):
            effect_readback.projection(self.database, replace(state, effect_execution=changed))
        # Replacing the request digest cannot rebind historical evidence.
        changed = deepcopy(state.to_dict())
        changed["effect_execution"]["identity"]["accepted_request_digest"] = "sha256:" + "f" * 64
        with sqlite_connection(self.database) as connection:
            connection.execute("UPDATE engineering_transactions SET payload=? WHERE run_id=?", (json.dumps(changed), dispatch.run_id))
        with self.assertRaises(HTTPError) as denied:
            self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")
        self.assertEqual(denied.exception.code, 409); denied.exception.close()

    def test_artifact_catalog_and_evidence_are_immutable(self):
        _, dispatch, _, state = self.complete()
        checkpoint = state.effect_execution
        attempt = checkpoint["attempts"][0]
        path = self.fixture.root.resolve() / "artifacts/effects" / dispatch.run_id / "result-0.json"
        options = dict(artifact_id=attempt["result"]["artifact_id"], artifact_type="EP_EFFECT_RESULT",
                       fingerprint=attempt["result"]["sha256"], created_at=attempt["started_at"])
        effect_evidence.register(self.database, checkpoint, path, **options)
        with self.assertRaisesRegex(contract.EffectContractError, "IDENTITY_CONFLICT"):
            effect_evidence.register(self.database, checkpoint, path, **{**options, "artifact_type": "WRONG"})
        with sqlite_connection(self.database) as connection:
            connection.execute("UPDATE execution_artifact_records SET digest=? WHERE artifact_id=?", ("f" * 64, options["artifact_id"]))
        with self.assertRaisesRegex(contract.EffectContractError, "CATALOG_MISMATCH"):
            effect_evidence.verify(self.database, checkpoint, path.parent)

    def test_checkpoint_rejects_history_replacement_and_counter_reset(self):
        _, _, store, state = self.complete()
        checkpoint = state.effect_execution
        effect_state.validate(None)
        for key, value in (("version", "wrong"), ("manifest", {}), ("validation_bindings", [{"untrusted": "control"}]),
                           ("identity", {}), ("attempts", [None])):
            with self.subTest(key=key), self.assertRaises(contract.EffectContractError):
                effect_state.validate({**checkpoint, key: value})
        for key, value in (("exit_code", "0"), ("status", "SKIPPED"), ("started_at", "unknown"),
                           ("completed_at", "2000-01-01T00:00:00+00:00")):
            changed = deepcopy(checkpoint)
            changed["attempts"][0]["controls"][0][key] = value
            with self.subTest(key=key), self.assertRaises(contract.EffectContractError): effect_state.validate(changed)
        for mutate in (lambda item: item.update(attempts=[]),
                       lambda item: item["attempts"][0]["result"].update(sha256="f" * 64),
                       lambda item: item["attempts"][0].update(controls=[]),
                       lambda item: item["identity"].update(engineering_action_id="replacement")):
            changed = deepcopy(checkpoint); mutate(changed)
            with self.subTest(), self.assertRaises(contract.EffectContractError): effect_state.transition(checkpoint, changed)
        with self.assertRaises((ValueError, RuntimeError)):
            store.save(replace(state, effect_execution=None))

    def test_three_repairs_are_durable_and_not_reset_after_restart(self):
        # The external reviewer produces real structured blockers on all four
        # attempts; the host, mandatory review parser and budget remain real.
        source = self.provider.read_text()
        source = source.replace("'status':'REVIEWED'", "'status':'NOT_APPLICABLE'")
        # Skipping required coverage cannot approve an effect. No lifecycle or
        # budget decision is replaced by the fixture.
        self.provider.write_text(source)
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        with sqlite_connection(self.database) as connection:
            payload = json.loads(connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (receipt.run_id,)).fetchone()[0])
        self.assertEqual(payload["phase"], "FAILED", payload["diagnostic"])
        self.assertEqual(payload["repair_iterations"], 3)
        self.assertEqual(len(payload["effect_execution"]["attempts"]), 4)
        before = (self.prefix / "calls.jsonl").read_bytes()
        self.reopen(receipt.run_id)
        self.assertEqual((self.prefix / "calls.jsonl").read_bytes(), before)

    def reopen(self, run_id):
        code = """import sys
from pathlib import Path
from engineering_platform.parity_lifecycle_dispatcher import _default_runner, _historical_admission_environment
from engineering_platform.storage import sqlite_connection
import json
root,data,database,run_id=Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
with sqlite_connection(database) as connection:
 state=json.loads(connection.execute('SELECT payload FROM engineering_transactions WHERE run_id=?',(run_id,)).fetchone()[0])
with _historical_admission_environment(root,data):
 runner=_default_runner(root,central_database=database)
 result=runner.run(Path(state['prompt_path']),run_id=run_id,resume=True,owner_authorized=True)
print(result.phase)
"""
        observed = subprocess.run((sys.executable, "-c", code, str(self.root), str(self.fixture.root), str(self.database), run_id),
            env=os.environ | {"PYTHONPATH": str(Path(engineering_platform.__file__).resolve().parent.parent)},
            text=True, capture_output=True, timeout=60)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        return observed.stdout.strip()

    def test_new_process_after_acceptance_and_result_persistence(self):
        for mode in ("READ_ONLY_ASSESSMENT", "ARCHITECTURE_DESIGN_ONLY"):
            submission = self.submit(fixtures.effect(mode, source_revision=self.revision))
            code = """import sys
from pathlib import Path
from engineering_platform.agent_state import StateStore
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
original=StateStore.save
def crash_after_durable_result(self,state):
 original(self,state)
 if state.effect_execution and state.effect_execution['attempts'] and state.effect_execution['attempts'][-1]['result']:
  raise SystemExit(73)
StateStore.save=crash_after_durable_result
ParityLifecycleDispatcher(Path(sys.argv[1])).dispatch(sys.argv[2])
"""
            before = len((self.prefix / "calls.jsonl").read_text().splitlines()) if (self.prefix / "calls.jsonl").exists() else 0
            crashed = subprocess.run((sys.executable, "-c", code, str(self.fixture.root), submission),
                env=os.environ | {"PYTHONPATH": str(Path(engineering_platform.__file__).resolve().parent.parent)},
                capture_output=True, text=True, timeout=45)
            self.assertEqual(crashed.returncode, 73, crashed.stderr)
            with sqlite_connection(self.database) as connection:
                run_id = connection.execute("SELECT run_id FROM ep_parity_lifecycle_dispatches WHERE submission_id=?", (submission,)).fetchone()[0]
            self.assertEqual(len((self.prefix / "calls.jsonl").read_text().splitlines()), before + 1)
            self.assertEqual(self.reopen(run_id), "COMPLETE")
            self.assertEqual(len((self.prefix / "calls.jsonl").read_text().splitlines()), before + 3)
            self.assertEqual(ParityLifecycleDispatcher(self.fixture.root).dispatch(submission).state, "COMPLETE")
            self.assertEqual(self.reopen(run_id), "COMPLETE")
            self.assertEqual(len((self.prefix / "calls.jsonl").read_text().splitlines()), before + 3)

    def test_new_process_git_modes_after_result_and_completed_delivery(self):
        self.git_process_matrix([(mode, "result") for mode in
            ("DOCUMENTATION_ONLY", "ARCHITECTURE_DESIGN_ONLY", "BOUNDED_REPOSITORY_CHANGE")])

    def test_new_process_recovers_remote_create_without_acknowledgement(self):
        self.git_process_matrix([("DOCUMENTATION_ONLY", "publication")])

    def git_process_matrix(self, scenarios):
        from engineering_platform.effect_workspace import git
        from unittest.mock import patch
        github_cli = self.base / "gh"
        github_cli.write_text("#!/bin/sh\nexit 0\n"); github_cli.chmod(0o700)
        code = """import sys,json
from pathlib import Path
from engineering_platform.agent_state import StateStore
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_repository import SubprocessRepositoryClient,GhCliClient
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.providers import CodexCliProvider
from tests.engineering.test_effect_execution import _LocalGitTransport,_LocalGitHubTransport
data,database,bare,submission,crash=Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5]
if crash=='result':
 original=StateStore.save
 def fault(self,state):
  original(self,state)
  if state.effect_execution and state.effect_execution['attempts'] and state.effect_execution['attempts'][-1]['result']:
   raise SystemExit(73)
 StateStore.save=fault
if crash=='publication':
 original_api=_LocalGitHubTransport.github
 def lost_ack(self,*args):
  result=original_api(self,*args)
  if args[:2]==('pr','create'): raise SystemExit(73)
  return result
 _LocalGitHubTransport.github=lost_ack
def factory(root):
 return EngineeringRunner(root,StateStore(root/'.engineering/engineering-runs',central_database=database,emit_local_projection=False),
  SubprocessRepositoryClient(_LocalGitTransport(bare)),GhCliClient(_LocalGitHubTransport(bare),repository='fixture/djconnect'),CodexCliClient(CodexCliProvider()))
receipt=ParityLifecycleDispatcher(data,runner_factory=factory).dispatch(submission)
print(receipt.state)
"""
        with patch.dict(os.environ, {"EP_GITHUB_CLI_EXECUTABLE": str(github_cli)}):
            for mode, failure_boundary in scenarios:
                bare = self.base / (mode + ".git")
                git(self.base, "clone", "--bare", str(self.root), str(bare))
                target = "src/boundary.py" if mode == "BOUNDED_REPOSITORY_CHANGE" else "docs/report.md"
                submission = self.submit(fixtures.effect(mode, "GIT", source_revision=self.revision, write_paths=[target]))
                calls = self.prefix / "calls.jsonl"
                before = len(calls.read_text().splitlines()) if calls.exists() else 0
                def child(fault="none"):
                    observed = subprocess.run((sys.executable, "-c", code, str(self.fixture.root), str(self.database), str(bare), submission, fault),
                        env=os.environ | {"PYTHONPATH": os.pathsep.join((str(Path(engineering_platform.__file__).resolve().parent.parent), str(Path(__file__).resolve().parents[2])))},
                        text=True, capture_output=True, timeout=60)
                    self.assertEqual(observed.returncode, 73 if fault != "none" else 0, observed.stderr)
                    return observed.stdout.strip()
                child(failure_boundary)
                self.assertEqual(len(calls.read_text().splitlines()), before + (1 if failure_boundary == "result" else 3))
                self.assertEqual(child(), "RUNNING")
                remote = fixtures._LocalGitHubTransport(bare)
                self.assertEqual(remote.creates, 1)
                self.assertEqual(len(calls.read_text().splitlines()), before + 3)
                remote.protected_merge()
                self.assertEqual(child(), "COMPLETE")
                self.assertEqual(child(), "COMPLETE")
                self.assertEqual(fixtures._LocalGitHubTransport(bare).creates, 1)
                self.assertEqual(len(calls.read_text().splitlines()), before + 3)
                self.assertFalse((self.root / target).exists())


if __name__ == "__main__":
    unittest.main()
