from __future__ import annotations

import json
import io
import os
from contextlib import redirect_stdout
from hashlib import sha256
import http.server
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import parallel_action_admission as admission
from engineering_platform import parallel_action_recovery as recovery
from engineering_platform import parity_lifecycle_dispatcher as lifecycle_dispatcher
from engineering_platform import server, submission_service
from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_errors import CodexInvocationError, RunnerError
from engineering_platform.lifecycle_worker import LifecycleWorker
from engineering_platform.parity_lifecycle_dispatcher import (
    ParityLifecycleDispatcher, ParityLifecycleDispatchError, dismiss_operator_gate,
    retry_operator_gate,
)
from engineering_platform.storage import sqlite_connection


FIXTURE = Path(__file__).parents[1] / "fixtures" / "forge-parallel-action-peer-graph-v1.json"
NOW = "2026-10-03T00:00:00+00:00"


class _PaE3CheckpointRunner:
    def __init__(self, repository_root: Path, data_root: Path, *,
                 cancel: bool = False, observe_cancel: bool = False,
                 cleanup_confirmed: bool = True, crash: bool = False) -> None:
        self.root = repository_root
        self.data_root = data_root
        self.cancel = cancel
        self.observe_cancel = observe_cancel
        self.cleanup_confirmed = cleanup_confirmed
        self.crash = crash
        self.agent = self
        self.check = lambda: False

    def set_cancellation_check(self, callback: object) -> None:
        self.check = callback

    def cancellation_observed(self) -> bool:
        return self.cancel and self.observe_cancel

    def provider_process_cleanup_confirmed(self) -> bool:
        return self.cleanup_confirmed

    def run(self, prompt_path: Path, *, run_id: str, **_: object) -> TransactionState:
        if self.cancel:
            recovery.request_cancel(self.data_root, project_id="project-test", run_id=run_id)
            assert self.check()
        if self.crash:
            raise RuntimeError("controlled dispatcher crash after runner entry")
        return TransactionState(run_id, self.root.name, str(prompt_path),
                                "RUNNING", terminal=False)


class ParallelActionAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        self.identity = server.initialize(self.root)
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        self.graph = json.loads(FIXTURE.read_text())
        self.graph["mission_id"] = "mission-test"
        self.repository_roots: dict[str, Path] = {}
        revisions: dict[str, str] = {}
        for repository_id in ("repository-a", "repository-b"):
            repository_root = Path(self.temporary.name) / repository_id
            repository_root.mkdir()
            declaration = repository_root / ".engineering-platform" / "repository.json"
            declaration.parent.mkdir()
            declaration.write_text(json.dumps({
                "schema_version": "1.0",
                "project": {"id": "project-test",
                            "authority_repository_id": "repository-a"},
                "repository": {"id": repository_id,
                               "role": "authority" if repository_id == "repository-a" else "child"},
                "validation": {"kind": "none", "entrypoint": None,
                               "description": None},
                "requirements": {"host": {}, "tools": {}},
                "integrations": {},
            }))
            (repository_root / ".gitignore").write_text("installed-*.zip\n")
            subprocess.run(["git", "init", "-q", "-b", "main", str(repository_root)], check=True)
            subprocess.run(["git", "-C", str(repository_root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository_root), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
                           check=True)
            revisions[repository_id] = subprocess.run(
                ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.repository_roots[repository_id] = repository_root
        for item in self.graph["actions"]:
            item["target"]["ep_instance_id"] = self.identity.instance_id
            item["target"]["project_id"] = "project-test"
            item["target"]["baseline_revision"] = revisions[item["target"]["repository_id"]]
        with sqlite_connection(self.database) as connection:
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                               ("project-test", "{}", "ACTIVE", NOW, NOW))
            for repository_id in ("repository-a", "repository-b"):
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)",
                                   (repository_id, "project-test", "repository-a",
                                    "authority" if repository_id == "repository-a" else "child",
                                    "{}", NOW, NOW))
                connection.execute(
                    "INSERT INTO ep_local_repository_bindings VALUES(?,?,?,?,?,?)",
                    ("project-test", repository_id, str(self.repository_roots[repository_id]),
                     "BOUND", NOW, NOW),
                )
            self.credential = submission_service.issue_consumer_credential(
                connection, consumer_id="forge", project_id="project-test")["credential"]
            for repository_id in ("repository-a", "repository-b"):
                connection.execute(
                    "INSERT INTO ep_parallel_action_repository_grants VALUES(?,?,?,?,?,?,?,?)",
                    ("forge", "project-test", repository_id, "ACTIVE", "test-owner",
                     "fixture grant", NOW, NOW),
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stage(self, connection: sqlite3.Connection, action_id: str, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "project_id": "project-test", "consumer_id": "forge",
            "producer_id": "forge", "action_id": action_id, "action_revision": "1",
            "intent_id": "intent-test", "intent_revision": "1",
            "correlation_id": "corr-" + action_id,
            "idempotency_key": "key-" + action_id,
            "write_scope": "repository-only",
            "policy_digest": admission.SUPPORTED_POLICY_DIGEST,
        }
        values.update(changes)
        return admission.stage_action(connection, json.dumps(self.graph).encode(), **values)

    def request(self, action_id: str, staged: dict[str, object], *,
                policy_digest: str = admission.SUPPORTED_POLICY_DIGEST) -> submission_service.SubmissionRequest:
        target = next(item["target"] for item in self.graph["actions"]
                      if item["action_id"] == action_id)
        repository_id = target["repository_id"]
        correlation_id = "corr-" + action_id
        return submission_service.SubmissionRequest(
            project_id="project-test", repository_id=repository_id,
            producer_id="forge", producer_type="FORGE", producer_version="2.7.2",
            prompt="Synthetic bounded Action.", transport="HTTP",
            idempotency_key="key-" + action_id, correlation_id=correlation_id,
            mission_id="mission-test", engineering_action_id=action_id,
            constraints={
                "forge_execution": {
                    "contract_version": "1.1", "host_id": "engineering-platform",
                    "repository_id": repository_id, "correlation_id": correlation_id,
                    "mission_id": "mission-test", "mission_revision": "1",
                    "intent_id": "intent-test", "intent_revision": "1",
                    "action_id": action_id,
                    "runtime_prompt": {"id": "prompt-" + action_id,
                                       "content_digest": "sha256:" + "f" * 64},
                    "retry_of_correlation_id": None,
                    "producer_contract_version": "1.0",
                    "forge_application_version": "2.7.2",
                },
                "repository_revision_binding": {
                    "requested_revision": target["baseline_revision"],
                    "allowed_baseline_revision": None,
                },
                "parallel_action_intake": {
                    "contract_version": "ep-parallel-action-intake/v1",
                    "intake_id": staged["intake_id"],
                    "snapshot_digest": staged["snapshot_digest"],
                    "action_revision": "1", "write_scope": "repository-only",
                    "policy_digest": policy_digest,
                    "concurrency_profile": "DIFFERENT_REPOSITORIES_V1",
                },
            },
        )

    def pa_e3_submissions(self) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        for repository_id, root in self.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
        with sqlite_connection(self.database) as connection:
            staged = {action: self.stage(
                connection, action, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
            ) for action in ("ACTION-A", "ACTION-B")}
            submissions = {action: submission_service.submit(
                connection, self.request(
                    action, staged[action], policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
                ), authenticated_consumer_id="forge",
            ).submission_id for action in ("ACTION-A", "ACTION-B")}
        return staged, submissions

    def pa_e3_child(self, submission_id: str, boundary: str) -> tuple[int, dict[str, object]]:
        driver = Path(__file__).parents[1] / "fixtures" / "pa_e3_recovery_driver.py"
        environment = dict(os.environ)
        completed = subprocess.run(
            (sys.executable, str(driver), str(self.root), submission_id, boundary),
            env=environment, capture_output=True, text=True, timeout=20,
        )
        self.assertTrue(completed.stdout, completed.stderr)
        return completed.returncode, json.loads(completed.stdout.splitlines()[-1])

    def _fake_canonical_readback(self, action_id: str, submission_id: str,
                                 digest: str, repository_id: str) -> dict[str, object]:
        return {
            "producer": {"id": "forge", "type": "FORGE"},
            "correlation": {"engineering_action_id": action_id,
                            "correlation_id": "corr-" + action_id,
                            "mission_id": "mission-test"},
            "submission": {"id": submission_id, "project_id": "project-test",
                           "repository_id": repository_id},
            "provenance": {"forge_execution": {"mission_revision": "1"}},
            "run": {"id": "run-" + action_id, "terminal": True},
            "result": {"outcome": "COMPLETE", "terminal": True,
                       "delivery_qualified": True},
            "evidence": {"status": "AVAILABLE",
                         "terminal_artifact": {"id": "terminal-" + action_id,
                                               "digest": digest},
                         "repository": {"id": repository_id,
                                        "revision": "c" * 40}},
        }

    def _canonical_fixture_rows(self, connection: sqlite3.Connection,
                                submission_id: str, action_id: str,
                                repository_id: str, digest: str) -> None:
        connection.execute(
            "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)",
            ("run-" + action_id, "project-test", "COMPLETE", NOW, NOW, "MANAGED"),
        )
        connection.execute(
            """INSERT INTO ep_parity_lifecycle_dispatches(
                submission_id,project_id,repository_id,run_id,state,prompt_path,
                claimed_at,updated_at,operator_resolution)
                VALUES(?,?,?,?,?,?,?,?,?)""",
            (submission_id, "project-test", repository_id, "run-" + action_id,
             "COMPLETE", "/synthetic/prompt", NOW, NOW, "NONE"),
        )
        connection.execute(
            """INSERT INTO execution_artifact_records(
                artifact_id,artifact_type,digest_algorithm,digest,content_type,
                ep_run_id,ep_submission_id,created_at,integrity_status,
                storage_location,projection_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("terminal-" + action_id, "EP_TERMINAL_EVIDENCE", "sha256",
             digest[7:], "application/json", "run-" + action_id,
             submission_id, NOW, "VERIFIED",
             action_id + ".json", "AVAILABLE"),
        )

    def _qualified_package(self, connection: sqlite3.Connection, intake_id: str,
                           action_id: str, submission_id: str,
                           payload: bytes, *,
                           owner_qualify: bool = True,
                           suffix: str = "",
                           terminal_receipt: tuple[str, str] | None = None,
                           ) -> tuple[str, str]:
        artifact_id = "package-" + action_id + suffix
        qualification_id = "qualification-" + action_id + suffix
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir(exist_ok=True)
        (artifact_root / (action_id + suffix + ".zip")).write_bytes(payload)
        connection.execute(
            """INSERT INTO execution_artifact_records(
                artifact_id,artifact_type,digest_algorithm,digest,content_type,
                ep_run_id,ep_submission_id,created_at,integrity_status,
                storage_location,projection_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (artifact_id, "EP_DELIVERABLE_PACKAGE", "sha256", sha256(payload).hexdigest(),
             "application/zip", "run-" + action_id, submission_id,
             NOW, "VERIFIED", action_id + suffix + ".zip", "AVAILABLE"),
        )
        outcome = connection.execute(
            "SELECT terminal_artifact_id,terminal_digest FROM ep_parallel_action_outcomes WHERE intake_id=?",
            (intake_id,),
        ).fetchone()
        if terminal_receipt is not None:
            outcome = terminal_receipt
        repository_id = "repository-a" if action_id == "ACTION-A" else "repository-b"
        qualification = {
            "contract_version": "ep-artifact-qualification/v1",
            "artifact_id": artifact_id,
            "content_digest": "sha256:" + sha256(payload).hexdigest(),
            "project_id": "project-test", "repository_id": repository_id,
            "submission_id": submission_id, "run_id": "run-" + action_id,
            "terminal_artifact_id": outcome[0], "terminal_digest": outcome[1],
            "status": "PASS",
            "controls": {"package_integrity": "PASS", "installed_readback": "PASS"},
        }
        qualification_bytes = json.dumps(qualification, sort_keys=True,
                                         separators=(",", ":")).encode()
        (artifact_root / (action_id + suffix + "-qualification.json")).write_bytes(qualification_bytes)
        connection.execute(
            """INSERT INTO execution_artifact_records(
                artifact_id,artifact_type,digest_algorithm,digest,content_type,
                ep_run_id,ep_submission_id,created_at,integrity_status,
                storage_location,projection_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (qualification_id, "EP_ARTIFACT_QUALIFICATION", "sha256",
             sha256(qualification_bytes).hexdigest(), "application/json",
             "run-" + action_id, submission_id, NOW, "VERIFIED",
             action_id + suffix + "-qualification.json", "AVAILABLE"),
        )
        installed_relative_path = "installed-" + action_id + suffix + ".zip"
        (self.repository_roots[repository_id] / installed_relative_path).write_bytes(payload)
        if owner_qualify:
            with patch("engineering_platform.platform_admin.require_installation_owner",
                       return_value="installation-owner"):
                admission.record_qualification_receipt(
                    connection, data_root=self.root, intake_id=intake_id,
                    artifact_id=artifact_id,
                    qualification_artifact_id=qualification_id,
                    installed_relative_path=installed_relative_path,
                    reason="verified installed package bytes",
                )
        return artifact_id, qualification_id

    def test_immutable_graph_replay_and_wait_states(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            b = self.stage(connection, "ACTION-B")
            q = self.stage(connection, "ACTION-Q")
            self.assertFalse(a["dispatch_authorized"])
            self.assertTrue(self.stage(connection, "ACTION-A")["replayed"])
            self.assertEqual(admission.dependency_readback(connection, intake_id=a["intake_id"])["state"],
                             "DEPENDENCY_ELIGIBLE")
            self.assertEqual(admission.dependency_readback(connection, intake_id=b["intake_id"])["state"],
                             "DEPENDENCY_ELIGIBLE")
            wait = admission.dependency_readback(connection, intake_id=q["intake_id"])
            self.assertEqual(wait["state"], "WAITING_DEPENDENCY")
            self.assertEqual(wait["blockers"], ["ACTION-A", "ACTION-B"])
            self.assertFalse(wait["dispatch_authorized"])
            self.assertEqual(wait["resource_state"], "NOT_EVALUATED")
            self.assertEqual(wait["capacity_state"], "NOT_EVALUATED")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM ep_parallel_action_graphs")
            with self.assertRaises(admission.ParallelAdmissionError) as changed:
                self.stage(connection, "ACTION-A", correlation_id="changed-correlation")
            self.assertEqual(changed.exception.code, "IDEMPOTENCY_CONFLICT")
        with sqlite_connection(self.database) as reopened:
            self.assertEqual(admission.dependency_readback(reopened, intake_id=q["intake_id"])["blockers"],
                             ["ACTION-A", "ACTION-B"])

    def test_exact_predecessor_evidence_and_wrong_scope(self) -> None:
        packages = {"ACTION-A": b"qualified-package-A", "ACTION-B": b"qualified-package-B"}
        for edge in self.graph["actions"][2]["dependencies"]:
            edge["required_evidence"]["content_digest"] = (
                "sha256:" + sha256(packages[edge["predecessor_action_id"]]).hexdigest()
            )
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            b = self.stage(connection, "ACTION-B")
            q = self.stage(connection, "ACTION-Q")
            a_digest = "sha256:" + "a" * 64
            b_digest = "sha256:" + "b" * 64
            a_submission = submission_service.submit(
                connection, self.request("ACTION-A", a), authenticated_consumer_id="forge").submission_id
            b_submission = submission_service.submit(
                connection, self.request("ACTION-B", b), authenticated_consumer_id="forge").submission_id
            self._canonical_fixture_rows(
                connection, a_submission, "ACTION-A", "repository-a", a_digest)
            self._canonical_fixture_rows(
                connection, b_submission, "ACTION-B", "repository-b", b_digest)
            with self.assertRaises(admission.ParallelAdmissionError):
                admission.attest_terminal_outcome(connection, intake_id=a["intake_id"],
                                                  submission_id=a_submission)
            wrong = self._fake_canonical_readback("ACTION-A", a_submission, a_digest, "repository-b")
            with patch.object(admission, "producer_readback", return_value=wrong):
                with self.assertRaises(admission.ParallelAdmissionError) as mismatch:
                    admission.attest_terminal_outcome(connection, intake_id=a["intake_id"],
                                                      submission_id=a_submission)
            self.assertEqual(mismatch.exception.code, "PREDECESSOR_SCOPE_MISMATCH")
            correct = self._fake_canonical_readback("ACTION-A", a_submission, a_digest, "repository-a")
            missing_revision = json.loads(json.dumps(correct))
            missing_revision["evidence"]["repository"]["revision"] = None
            with patch.object(admission, "producer_readback", return_value=missing_revision):
                with self.assertRaises(admission.ParallelAdmissionError) as missing:
                    admission.attest_terminal_outcome(
                        connection, intake_id=a["intake_id"],
                        submission_id=a_submission)
            self.assertEqual(missing.exception.code, "PREDECESSOR_SCOPE_MISMATCH")
            with patch.object(admission, "producer_readback", return_value=correct):
                outcome = admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"], submission_id=a_submission)
                self.assertTrue(admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"],
                    submission_id=a_submission)["replayed"])
            self.assertFalse(outcome["replayed"])
            b_readback = self._fake_canonical_readback("ACTION-B", b_submission, b_digest, "repository-b")
            readbacks = {a_submission: correct, b_submission: b_readback}
            with patch.object(admission, "producer_readback",
                              side_effect=lambda _connection, *, project_id, submission_id,
                              contract_version: readbacks[submission_id]):
                self.assertEqual(admission.dependency_readback(connection, intake_id=q["intake_id"])["blockers"],
                                 ["ACTION-A", "ACTION-B"])
                package_a, qualification_a = self._qualified_package(
                    connection, a["intake_id"], "ACTION-A", a_submission,
                    packages["ACTION-A"], owner_qualify=False)
                qualification_path = self.root / "artifacts" / "ACTION-A-qualification.json"
                original_qualification = qualification_path.read_bytes()
                qualification_path.write_bytes(b'{"status":"PASS"}')
                with self.assertRaises(admission.ParallelAdmissionError) as unsupported:
                    admission.attest_qualified_artifact(
                        connection, intake_id=a["intake_id"], artifact_id=package_a,
                        qualification_artifact_id=qualification_a)
                self.assertEqual(unsupported.exception.code, "ARTIFACT_QUALIFICATION_UNAVAILABLE")
                qualification_path.write_bytes(original_qualification)
                with self.assertRaises(admission.ParallelAdmissionError) as untrusted:
                    admission.attest_qualified_artifact(
                        connection, intake_id=a["intake_id"], artifact_id=package_a,
                        qualification_artifact_id=qualification_a)
                self.assertEqual(untrusted.exception.code, "ARTIFACT_QUALIFICATION_UNAVAILABLE")
                with patch("engineering_platform.platform_admin.require_installation_owner",
                           side_effect=PermissionError("PLATFORM_ADMIN_FORBIDDEN")):
                    with self.assertRaises(PermissionError):
                        admission.record_qualification_receipt(
                            connection, data_root=self.root, intake_id=a["intake_id"],
                            artifact_id=package_a,
                            qualification_artifact_id=qualification_a,
                            installed_relative_path="installed-ACTION-A.zip",
                            reason="unauthorized assertion",
                        )
                with patch("engineering_platform.platform_admin.require_installation_owner",
                           return_value="installation-owner"):
                    admission.record_qualification_receipt(
                        connection, data_root=self.root, intake_id=a["intake_id"],
                        artifact_id=package_a,
                        qualification_artifact_id=qualification_a,
                        installed_relative_path="installed-ACTION-A.zip",
                        reason="verified installed package bytes",
                    )
                self.assertGreaterEqual(admission.reconcile_predecessors(
                    connection, intake_id=q["intake_id"]), 1)
                self.assertEqual(admission.dependency_readback(connection, intake_id=q["intake_id"])["blockers"],
                                 ["ACTION-B"])
                replacement_id = "replacement-ACTION-A"
                replacement_digest = "d" * 64
                connection.execute(
                    """INSERT INTO execution_artifact_records(
                        artifact_id,artifact_type,digest_algorithm,digest,content_type,
                        ep_run_id,ep_submission_id,created_at,integrity_status,
                        storage_location,projection_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (replacement_id, "EP_TERMINAL_EVIDENCE", "sha256",
                     replacement_digest, "application/json", "run-ACTION-A",
                     a_submission, NOW, "VERIFIED", "replacement-a.json", "CANDIDATE"),
                )
                connection.execute(
                    "UPDATE execution_artifact_records SET projection_status='SUPERSEDED' "
                    "WHERE artifact_id='terminal-ACTION-A'",
                )
                connection.execute(
                    "UPDATE execution_artifact_records SET projection_status='AVAILABLE' "
                    "WHERE artifact_id=?", (replacement_id,),
                )
                correct["evidence"]["terminal_artifact"] = {
                    "id": replacement_id, "digest": "sha256:" + replacement_digest}
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"],
                    ["ACTION-A", "ACTION-B"])
                connection.execute(
                    """INSERT INTO ep_terminal_evidence_reconciliation_operations(
                        operation_id,run_id,project_id,source_artifact_id,source_digest,
                        replacement_artifact_id,replacement_digest,reason_code,recorded_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("reconcile-a-0001", "run-ACTION-A", "project-test",
                     "terminal-ACTION-A", a_digest[7:], replacement_id,
                     replacement_digest, "MERGE_CANDIDATE_PROJECTION_V1", NOW),
                )
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"],
                    ["ACTION-A", "ACTION-B"])
                self._qualified_package(
                    connection, a["intake_id"], "ACTION-A", a_submission,
                    packages["ACTION-A"], suffix="-reconciled",
                    terminal_receipt=(replacement_id, "sha256:" + replacement_digest))
                admission.reconcile_predecessors(connection, intake_id=q["intake_id"])
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"], ["ACTION-B"])
                admission.attest_terminal_outcome(connection, intake_id=b["intake_id"],
                                                  submission_id=b_submission)
                self.assertEqual(admission.dependency_readback(connection, intake_id=q["intake_id"])["blockers"],
                                 ["ACTION-B"])
                package_b, qualification_b = self._qualified_package(
                    connection, b["intake_id"], "ACTION-B", b_submission,
                    packages["ACTION-B"])
                self.assertEqual(admission.reconcile_predecessors(
                    connection, intake_id=q["intake_id"]), 1)
                eligible = admission.dependency_readback(connection, intake_id=q["intake_id"])
                self.assertEqual(eligible["state"], "DEPENDENCY_ELIGIBLE")
                self.assertFalse(eligible["dispatch_authorized"])
                accepted_q = submission_service.submit(
                    connection, self.request("ACTION-Q", q), authenticated_consumer_id="forge")
                self.assertEqual(accepted_q.state, "QUEUED")
                b_qualification = self.root / "artifacts" / "ACTION-B-qualification.json"
                original_b_qualification = b_qualification.read_bytes()
                b_qualification.write_bytes(b'{"status":"PASS"}')
                self.assertEqual(admission.dependency_readback(connection, intake_id=q["intake_id"])["blockers"],
                                 ["ACTION-B"])
                b_qualification.write_bytes(original_b_qualification)
                installed_b = self.repository_roots["repository-b"] / "installed-ACTION-B.zip"
                installed_b.write_bytes(b"not the qualified installation")
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"], ["ACTION-B"])
                installed_b.write_bytes(packages["ACTION-B"])
                (self.root / "artifacts" / "ACTION-B.zip").write_bytes(b"tampered")
                self.assertEqual(admission.dependency_readback(connection, intake_id=q["intake_id"])["blockers"],
                                 ["ACTION-B"])
        with patch.object(admission, "producer_readback",
                          side_effect=lambda _connection, *, project_id, submission_id,
                          contract_version: readbacks[submission_id]):
            self.assertNotIn(accepted_q.submission_id,
                             LifecycleWorker(self.root).eligible_submission_ids())
            with self.assertRaises(ParityLifecycleDispatchError) as stopped:
                ParityLifecycleDispatcher(self.root)._claim(accepted_q.submission_id)
            self.assertEqual(str(stopped.exception), "WAITING_DEPENDENCY")
        with sqlite_connection(self.database) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (accepted_q.submission_id,),
            ).fetchone())

    def test_graph_change_and_consumer_scope_fail_closed(self) -> None:
        with sqlite_connection(self.database) as connection:
            self.stage(connection, "ACTION-A")
            self.graph["actions"][2]["dependencies"].pop()
            with self.assertRaises(admission.ParallelAdmissionError) as changed:
                self.stage(connection, "ACTION-B")
            self.assertEqual(changed.exception.code, "GRAPH_REVISION_CONFLICT")
            with self.assertRaises(admission.ParallelAdmissionError) as missing_consumer:
                self.stage(connection, "ACTION-B", consumer_id="unknown")
            self.assertEqual(missing_consumer.exception.code, "PRODUCER_PRINCIPAL_MISMATCH")

    def test_repository_revision_edge_requires_exact_live_predecessor(self) -> None:
        edge = next(item for item in self.graph["actions"][2]["dependencies"]
                    if item["predecessor_action_id"] == "ACTION-A")
        edge["required_evidence"]["kind"] = "REPOSITORY_REVISION"
        edge["required_evidence"]["content_digest"] = (
            "sha256:" + sha256(("c" * 40).encode()).hexdigest())
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            q = self.stage(connection, "ACTION-Q")
            submission_id = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge").submission_id
            terminal_digest = "sha256:" + "a" * 64
            self._canonical_fixture_rows(
                connection, submission_id, "ACTION-A", "repository-a",
                terminal_digest)
            readback = self._fake_canonical_readback(
                "ACTION-A", submission_id, terminal_digest, "repository-a")
            with patch.object(admission, "producer_readback", return_value=readback):
                admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"], submission_id=submission_id)
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"], ["ACTION-B"])
                readback["evidence"]["repository"]["revision"] = "d" * 40
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"],
                    ["ACTION-A", "ACTION-B"])
                readback["evidence"]["repository"]["revision"] = "c" * 40
                readback["evidence"]["repository"]["id"] = "repository-b"
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q["intake_id"])["blockers"],
                    ["ACTION-A", "ACTION-B"])
                readback["evidence"]["repository"]["id"] = "repository-a"
                self.graph["mission_revision"] = 2
                edge["required_evidence"]["content_digest"] = (
                    "sha256:" + sha256(("d" * 40).encode()).hexdigest())
                q2 = self.stage(
                    connection, "ACTION-Q", action_revision="2",
                    idempotency_key="key-ACTION-Q-revision-2")
                self.assertEqual(admission.dependency_readback(
                    connection, intake_id=q2["intake_id"])["blockers"],
                    ["ACTION-A", "ACTION-B"])

    def test_explicit_target_grant_and_staged_producer_type_are_required(self) -> None:
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='REVOKED' "
                "WHERE repository_id='repository-a'",
            )
            with self.assertRaises(admission.ParallelAdmissionError) as denied:
                self.stage(connection, "ACTION-A")
            self.assertEqual(denied.exception.code, "SCOPE_UNAVAILABLE")
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='ACTIVE' "
                "WHERE repository_id='repository-a'",
            )
            a = self.stage(connection, "ACTION-A")
            q = self.stage(connection, "ACTION-Q")
            downgraded = self.request("ACTION-A", a)
            downgraded = submission_service.SubmissionRequest(
                **{**downgraded.__dict__, "producer_type": "OTHER"})
            with self.assertRaises(submission_service.SubmissionError) as rejected:
                submission_service.submit(connection, downgraded,
                                          authenticated_consumer_id="forge")
            self.assertEqual(rejected.exception.code, "PARALLEL_PRODUCER_TYPE_REQUIRED")
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='REVOKED' "
                "WHERE repository_id='repository-a'",
            )
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_SCOPE")
            scope_wait = admission.dependency_readback(
                connection, intake_id=q["intake_id"])
            self.assertEqual(scope_wait["state"], "WAITING_SCOPE")
            self.assertEqual(scope_wait["blockers"],
                             ["TARGET_SCOPE_UNAVAILABLE", "ACTION-A", "ACTION-B"])
            with self.assertRaises(submission_service.SubmissionError) as revoked:
                submission_service.submit(connection, self.request("ACTION-A", a),
                                          authenticated_consumer_id="forge")
            self.assertEqual(revoked.exception.code, "WAITING_SCOPE")

    def test_repository_grant_change_requires_installation_owner(self) -> None:
        with sqlite_connection(self.database) as connection:
            arguments = {
                "data_root": self.root, "consumer_id": "forge",
                "project_id": "project-test", "repository_id": "repository-a",
                "reason": "reviewed target", "active": False,
            }
            with patch("engineering_platform.platform_admin.require_installation_owner",
                       side_effect=PermissionError("PLATFORM_ADMIN_FORBIDDEN")):
                with self.assertRaises(PermissionError):
                    admission.set_repository_grant(connection, **arguments)
            self.assertEqual(connection.execute(
                "SELECT status FROM ep_parallel_action_repository_grants "
                "WHERE consumer_id='forge' AND repository_id='repository-a'",
            ).fetchone(), ("ACTIVE",))
            with patch("engineering_platform.platform_admin.require_installation_owner",
                       return_value="installation-owner"):
                changed = admission.set_repository_grant(connection, **arguments)
            self.assertEqual(changed["status"], "REVOKED")
            with self.assertRaises(admission.ParallelAdmissionError) as denied:
                self.stage(connection, "ACTION-A")
            self.assertEqual(denied.exception.code, "SCOPE_UNAVAILABLE")

    def test_owner_cli_grant_routes_require_scope_and_record_revocation(self) -> None:
        base = ("--data-root", str(self.root), "--consumer-id", "forge",
                "--project-id", "project-test", "--repository-id", "repository-a",
                "--reason", "owner decision")
        with redirect_stdout(io.StringIO()) as missing:
            self.assertEqual(server.main(("grant-parallel-action-repository",
                                          "--data-root", str(self.root))), 2)
        self.assertIn("--consumer-id", missing.getvalue())
        with patch("engineering_platform.platform_admin.require_installation_owner",
                   return_value="installation-owner"):
            with redirect_stdout(io.StringIO()) as revoked:
                self.assertEqual(server.main(("revoke-parallel-action-repository", *base)), 0)
            self.assertEqual(json.loads(revoked.getvalue())["status"], "REVOKED")
            with redirect_stdout(io.StringIO()) as granted:
                self.assertEqual(server.main(("grant-parallel-action-repository", *base)), 0)
            self.assertEqual(json.loads(granted.getvalue())["status"], "ACTIVE")

    def test_owner_cli_qualifies_exact_installed_package(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            submission_id = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge").submission_id
            terminal_digest = "sha256:" + "a" * 64
            self._canonical_fixture_rows(
                connection, submission_id, "ACTION-A", "repository-a",
                terminal_digest)
            readback = self._fake_canonical_readback(
                "ACTION-A", submission_id, terminal_digest, "repository-a")
            with patch.object(admission, "producer_readback", return_value=readback):
                admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"], submission_id=submission_id)
                package_id, qualification_id = self._qualified_package(
                    connection, a["intake_id"], "ACTION-A", submission_id,
                    b"installed exact package", owner_qualify=False)
        with redirect_stdout(io.StringIO()) as missing:
            self.assertEqual(server.main(("qualify-parallel-action-artifact",
                                          "--data-root", str(self.root))), 2)
        self.assertIn("--intake-id", missing.getvalue())
        with (patch.object(admission, "producer_readback", return_value=readback),
              patch("engineering_platform.platform_admin.require_installation_owner",
                    return_value="installation-owner"),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(server.main((
                "qualify-parallel-action-artifact", "--data-root", str(self.root),
                "--intake-id", a["intake_id"], "--artifact-id", package_id,
                "--qualification-artifact-id", qualification_id,
                "--installed-relative-path", "installed-ACTION-A.zip",
                "--reason", "owner verified exact bytes",
            )), 0)
        self.assertFalse(json.loads(output.getvalue())["replayed"])

    def test_owner_can_revoke_grant_while_consumer_disabled(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='DISABLED' "
                "WHERE consumer_id='forge' AND project_id='project-test'")
            with patch("engineering_platform.platform_admin.require_installation_owner",
                       return_value="installation-owner"):
                revoked = admission.set_repository_grant(
                    connection, data_root=self.root, consumer_id="forge",
                    project_id="project-test", repository_id="repository-a",
                    reason="owner revoked dormant access", active=False)
            self.assertEqual(revoked["status"], "REVOKED")
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='ACTIVE' "
                "WHERE consumer_id='forge' AND project_id='project-test'")
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_SCOPE")

    def test_staging_rejects_invalid_authority_and_policy_inputs(self) -> None:
        with sqlite_connection(self.database) as connection:
            for changes, expected in (
                ({"correlation_id": "bad/correlation"}, "INVALID_IDENTITY"),
                ({"producer_id": "another-producer"}, "PRODUCER_PRINCIPAL_MISMATCH"),
                ({"concurrency_profile": "UNBOUNDED"}, "UNSUPPORTED_CONCURRENCY_PROFILE"),
                ({"write_scope": "project-wide"}, "UNSUPPORTED_PARALLEL_POLICY"),
            ):
                with self.subTest(changes=changes):
                    with self.assertRaises(admission.ParallelAdmissionError) as rejected:
                        self.stage(connection, "ACTION-A", **changes)
                    self.assertEqual(rejected.exception.code, expected)
            with patch("engineering_platform.platform_admin.require_installation_owner",
                       return_value="installation-owner"):
                with self.assertRaises(admission.ParallelAdmissionError) as unknown_scope:
                    admission.set_repository_grant(
                        connection, data_root=self.root, consumer_id="forge",
                        project_id="project-test", repository_id="unknown-repository",
                        reason="reviewed target", active=True,
                    )
                self.assertEqual(unknown_scope.exception.code, "SCOPE_UNAVAILABLE")
                with self.assertRaises(admission.ParallelAdmissionError) as bad_reason:
                    admission.set_repository_grant(
                        connection, data_root=self.root, consumer_id="forge",
                        project_id="project-test", repository_id="repository-a",
                        reason="", active=True,
                    )
                self.assertEqual(bad_reason.exception.code, "INVALID_GRANT_REASON")

    def test_graph_requires_current_grants_for_every_target_before_persistence(self) -> None:
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='REVOKED' "
                "WHERE repository_id='repository-b'")
            with self.assertRaises(admission.ParallelAdmissionError) as rejected:
                self.stage(connection, "ACTION-A")
            self.assertEqual(rejected.exception.code, "SCOPE_UNAVAILABLE")
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_parallel_action_graphs").fetchone())
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='ACTIVE' "
                "WHERE repository_id='repository-b'")
            self.assertFalse(self.stage(connection, "ACTION-A")["replayed"])

    def test_graph_uses_two_targets_in_project_with_more_than_256_repositories(self) -> None:
        with sqlite_connection(self.database) as connection:
            connection.executemany(
                "INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)",
                [(f"extra-{index:03d}", "project-test", "repository-a", "child",
                  "{}", NOW, NOW) for index in range(255)],
            )
            self.assertFalse(self.stage(connection, "ACTION-A")["replayed"])

    def test_missing_link_cannot_turn_queued_action_into_serial_work(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            connection.execute(
                "DROP TRIGGER ep_parallel_action_submission_links_immutable_delete")
            connection.execute(
                "DELETE FROM ep_parallel_action_submission_links WHERE submission_id=?",
                (accepted.submission_id,),
            )
        self.assertNotIn(accepted.submission_id,
                         LifecycleWorker(self.root).eligible_submission_ids())
        with self.assertRaises(ParityLifecycleDispatchError) as refused:
            ParityLifecycleDispatcher(self.root)._claim(accepted.submission_id)
        self.assertEqual(str(refused.exception), "PARALLEL_SUBMISSION_UNLINKED")

    def test_missing_link_and_intake_cannot_turn_marked_action_serial(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge")
            connection.execute(
                "DROP TRIGGER ep_parallel_action_submission_links_immutable_delete")
            connection.execute(
                "DELETE FROM ep_parallel_action_submission_links WHERE submission_id=?",
                (accepted.submission_id,))
            connection.execute("DROP TRIGGER ep_parallel_action_intakes_immutable_delete")
            connection.execute(
                "DELETE FROM ep_parallel_action_intakes WHERE intake_id=?",
                (a["intake_id"],))
        self.assertNotIn(accepted.submission_id,
                         LifecycleWorker(self.root).eligible_submission_ids())
        with self.assertRaises(ParityLifecycleDispatchError) as refused:
            ParityLifecycleDispatcher(self.root)._claim(accepted.submission_id)
        self.assertEqual(str(refused.exception), "PARALLEL_SUBMISSION_UNLINKED")

    def test_claimed_action_resumes_after_checkpointed_repository_progress(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            baseline = self.graph["actions"][0]["target"]["baseline_revision"]
            run_id = "run-continuation-a"
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, "project-test", "RUNNING", NOW, NOW, "MANAGED"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,
                    claimed_at,updated_at,operator_resolution)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (accepted.submission_id, "project-test", "repository-a", run_id,
                 "RUNNING", "/synthetic/continuation", NOW, NOW, "NONE"),
            )
            connection.execute(
                "INSERT INTO ep_receipt_run_provenance VALUES(?,?,?,?,?,?)",
                (accepted.submission_id, run_id, "project-test", "repository-a",
                 self.identity.instance_id, NOW),
            )
        root = self.repository_roots["repository-a"]
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin",
             "https://github.com/example-org/checkpoint-target.git"], check=True)
        (root / "progress.txt").write_text("checkpointed progress")
        subprocess.run(["git", "-C", str(root), "add", "progress.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "progress"],
                       check=True)
        progressed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        checkpoint = TransactionState(
            run_id, "example-org/checkpoint-target", "/synthetic/continuation", "EXECUTE_AGENT",
            requested_repository_revision=baseline,
            execution_baseline_sha=baseline, last_verified_sha=progressed,
        )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(admission.linked_submission_decision(
                connection, submission_id=accepted.submission_id,
                continuation_run_id=run_id)["state"], "WAITING_BASELINE")
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                (run_id, json.dumps(checkpoint.to_dict()), "EXECUTE_AGENT", NOW),
            )
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_BASELINE")
            self.assertEqual(admission.linked_submission_decision(
                connection, submission_id=accepted.submission_id,
                continuation_run_id=run_id)["state"], "DEPENDENCY_ELIGIBLE")
        self.assertIn(accepted.submission_id,
                      LifecycleWorker(self.root).eligible_submission_ids())
        context, _candidate, claimed_run, _prompt, duplicate = (
            ParityLifecycleDispatcher(self.root)._claim(accepted.submission_id))
        self.assertEqual((context.repository_id, claimed_run, duplicate),
                         ("repository-a", run_id, True))

    def test_new_graph_reuses_exact_accepted_predecessor_and_supersedes_old_intake(self) -> None:
        with sqlite_connection(self.database) as connection:
            original_graph = json.loads(json.dumps(self.graph))
            a = self.stage(connection, "ACTION-A")
            old_q = self.stage(connection, "ACTION-Q")
            accepted_a = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            self.graph["mission_revision"] = 2
            self.graph["actions"][2]["dependencies"][0]["required_evidence"][
                "content_digest"] = "sha256:" + "e" * 64
            new_q = self.stage(connection, "ACTION-Q", action_revision="2",
                               idempotency_key="key-Q-revision-2")
            revision_two_graph = json.loads(json.dumps(self.graph))
            self.graph = original_graph
            self.assertEqual(self.stage(connection, "ACTION-A")["intake_id"], a["intake_id"])
            changed_replay = json.loads(json.dumps(original_graph))
            changed_replay["actions"][0]["target"]["baseline_revision"] = "c" * 40
            self.graph = changed_replay
            with self.assertRaises(admission.ParallelAdmissionError) as changed_key:
                self.stage(connection, "ACTION-A")
            self.assertEqual(changed_key.exception.code, "IDEMPOTENCY_CONFLICT")
            self.graph = revision_two_graph
            duplicate_a = self.stage(connection, "ACTION-A", action_revision="2",
                                     idempotency_key="key-A-revision-2")
            duplicate_request = self.request("ACTION-A", duplicate_a)
            duplicate_constraints = json.loads(json.dumps(duplicate_request.constraints))
            duplicate_constraints["forge_execution"]["mission_revision"] = "2"
            duplicate_constraints["parallel_action_intake"]["action_revision"] = "2"
            duplicate_request = submission_service.SubmissionRequest(**{
                **duplicate_request.__dict__, "idempotency_key": "key-A-revision-2",
                "constraints": duplicate_constraints,
            })
            with self.assertRaises(submission_service.SubmissionError) as duplicate:
                submission_service.submit(connection, duplicate_request,
                                          authenticated_consumer_id="forge")
            self.assertEqual(duplicate.exception.code, "ACTION_ALREADY_SUBMITTED")
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=old_q["intake_id"])["state"], "GRAPH_SUPERSEDED")
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=new_q["intake_id"])["blockers"],
                ["ACTION-A", "ACTION-B"])
            old_request = self.request("ACTION-Q", old_q)
            with self.assertRaises(submission_service.SubmissionError) as stale:
                submission_service.submit(connection, old_request,
                                          authenticated_consumer_id="forge")
            self.assertEqual(stale.exception.code, "GRAPH_SUPERSEDED")
            self.assertEqual(connection.execute(
                "SELECT intake_id FROM ep_parallel_action_submission_links WHERE submission_id=?",
                (accepted_a.submission_id,),
            ).fetchone(), (a["intake_id"],))
            self.graph["mission_revision"] = 3
            self.graph["actions"][0]["target"]["baseline_revision"] = "c" * 40
            with self.assertRaises(admission.ParallelAdmissionError) as changed:
                self.stage(connection, "ACTION-Q", action_revision="3",
                           idempotency_key="key-Q-revision-3")
            self.assertEqual(changed.exception.code, "ACCEPTED_ACTION_CHANGED")

    def test_failed_action_retry_keeps_one_intake_and_requires_operator_lineage(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            failed_run = "run-failed-a"
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) "
                "VALUES(?,?,?,?,?,?)",
                (failed_run, "project-test", "FAILED", NOW, NOW, "MANAGED"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,
                    claimed_at,updated_at,operator_resolution)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (accepted.submission_id, "project-test", "repository-a", failed_run,
                 "FAILED", "/synthetic/failed", NOW, NOW, "OPEN"),
            )
            retry = self.request("ACTION-A", a)
            retry_constraints = json.loads(json.dumps(retry.constraints))
            retry_constraints["repository_revision_binding"]["allowed_baseline_revision"] = (
                retry_constraints["repository_revision_binding"]["requested_revision"])
            retry = submission_service.SubmissionRequest(**{
                **retry.__dict__, "prompt": f"Retry-Of: {failed_run}\n\n{retry.prompt}",
                "idempotency_key": None, "constraints": retry_constraints,
            })
            with self.assertRaises(submission_service.SubmissionError) as bypass:
                submission_service.submit(connection, retry)
            self.assertEqual(bypass.exception.code, "PARALLEL_PRINCIPAL_REQUIRED")
            altered = submission_service.SubmissionRequest(
                **{**retry.__dict__, "prompt": retry.prompt + " changed"})
            with self.assertRaises(submission_service.SubmissionError) as mismatch:
                submission_service.submit(
                    connection, altered, audit_forge_exchange=False,
                    operator_retry_parent_submission_id=accepted.submission_id,
                )
            self.assertEqual(mismatch.exception.code, "PARALLEL_RETRY_MISMATCH")
            successor = submission_service.submit(
                connection, retry, audit_forge_exchange=False,
                operator_retry_parent_submission_id=accepted.submission_id,
            )
            connection.execute(
                """UPDATE ep_parity_lifecycle_dispatches
                    SET operator_resolution='RETRIED',resolution_submission_id=?
                    WHERE submission_id=?""",
                (successor.submission_id, accepted.submission_id),
            )
            self.assertEqual(connection.execute(
                "SELECT intake_id,parent_submission_id FROM ep_parallel_action_submission_links "
                "WHERE submission_id=?", (successor.submission_id,),
            ).fetchone(), (a["intake_id"], accepted.submission_id))
            self.assertEqual(admission.linked_submission_decision(
                connection, submission_id=successor.submission_id)["state"],
                "DEPENDENCY_ELIGIBLE")
            digest = "sha256:" + "a" * 64
            self._canonical_fixture_rows(
                connection, successor.submission_id, "ACTION-A", "repository-a", digest)
            readback = self._fake_canonical_readback(
                "ACTION-A", successor.submission_id, digest, "repository-a")
            with patch.object(admission, "producer_readback", return_value=readback):
                outcome = admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"],
                    submission_id=successor.submission_id)
                self.assertEqual(outcome["submission_id"], successor.submission_id)
                self.assertTrue(admission.attest_terminal_outcome(
                    connection, intake_id=a["intake_id"],
                    submission_id=successor.submission_id)["replayed"])
            connection.execute(
                "UPDATE ep_parallel_action_repository_grants SET status='REVOKED' "
                "WHERE repository_id='repository-a'",
            )
            self.assertEqual(admission.linked_submission_decision(
                connection, submission_id=successor.submission_id)["state"],
                "WAITING_SCOPE")

    def test_operator_retry_gate_creates_linked_successor(self) -> None:
        failed_run = "run-failed-operator"
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) "
                "VALUES(?,?,?,?,?,?)",
                (failed_run, "project-test", "FAILED", NOW, NOW, "MANAGED"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,
                    claimed_at,updated_at,operator_resolution)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (accepted.submission_id, "project-test", "repository-a", failed_run,
                 "FAILED", "/synthetic/failed", NOW, NOW, "OPEN"),
            )
            constraints = json.loads(json.dumps(self.request("ACTION-A", a).constraints))
            constraints["repository_revision_binding"]["allowed_baseline_revision"] = (
                constraints["repository_revision_binding"]["requested_revision"])
        with patch("engineering_platform.parity_lifecycle_dispatcher._retry_constraints_for_current_main",
                   return_value=constraints):
            successor = retry_operator_gate(
                self.root, project_id="project-test", run_id=failed_run,
            )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT intake_id,parent_submission_id FROM ep_parallel_action_submission_links "
                "WHERE submission_id=?", (successor.submission_id,),
            ).fetchone(), (a["intake_id"], accepted.submission_id))
            self.assertEqual(connection.execute(
                "SELECT operator_resolution,resolution_submission_id "
                "FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (accepted.submission_id,),
            ).fetchone(), ("RETRIED", successor.submission_id))

    def test_operator_retry_accepts_exact_remote_pin_ahead_of_clean_local_main(self) -> None:
        root = self.repository_roots["repository-a"]
        subprocess.run(["git", "-C", str(root), "branch", "-M", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin",
             "https://github.com/example-org/retry-target.git"], check=True)
        original = self.graph["actions"][0]["target"]["baseline_revision"]
        (root / "future.txt").write_text("remote protected main")
        subprocess.run(["git", "-C", str(root), "add", "future.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "future"],
                       check=True)
        protected = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", protected],
            check=True)
        subprocess.run(["git", "-C", str(root), "reset", "--hard", original],
                       check=True, capture_output=True)
        failed_run = "run-retry-behind"
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a),
                authenticated_consumer_id="forge",
            )
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) "
                "VALUES(?,?,?,?,?,?)",
                (failed_run, "project-test", "FAILED", NOW, NOW, "MANAGED"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,
                    claimed_at,updated_at,operator_resolution)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (accepted.submission_id, "project-test", "repository-a", failed_run,
                 "FAILED", "/synthetic/failed", NOW, NOW, "OPEN"),
            )
            constraints = json.loads(json.dumps(self.request("ACTION-A", a).constraints))
            constraints["repository_revision_binding"]["allowed_baseline_revision"] = protected
        with patch("engineering_platform.parity_lifecycle_dispatcher._retry_constraints_for_current_main",
                   return_value=constraints):
            successor = retry_operator_gate(
                self.root, project_id="project-test", run_id=failed_run,
            )
        with sqlite_connection(self.database) as connection:
            decision = admission.linked_submission_decision(
                connection, submission_id=successor.submission_id)
            self.assertEqual(decision["state"], "DEPENDENCY_ELIGIBLE")
            self.assertEqual(decision["target_baseline_state"], "PENDING_HOST_SYNC")
        _context, _candidate, _run_id, _prompt, duplicate = (
            ParityLifecycleDispatcher(self.root)._claim(successor.submission_id))
        self.assertFalse(duplicate)

    def test_action_revision_is_unambiguous_within_and_across_graph_revisions(self) -> None:
        with sqlite_connection(self.database) as connection:
            original = self.stage(connection, "ACTION-A")
            with self.assertRaises(admission.ParallelAdmissionError) as same_graph:
                self.stage(connection, "ACTION-A", action_revision="2",
                           idempotency_key="new-key")
            self.assertEqual(same_graph.exception.code, "ACTION_REVISION_CONFLICT")
            self.graph["mission_revision"] = 2
            self.graph["actions"][0]["target"]["baseline_revision"] = "c" * 40
            with self.assertRaises(admission.ParallelAdmissionError) as changed_graph:
                self.stage(connection, "ACTION-A", idempotency_key="new-key")
            self.assertEqual(changed_graph.exception.code, "ACTION_REVISION_CONFLICT")
            restaged = self.stage(connection, "ACTION-A", action_revision="2",
                                  idempotency_key="new-key")
            self.assertNotEqual(restaged["intake_id"], original["intake_id"])

    def test_canonical_submission_gates_independent_actions_and_blocks_join(self) -> None:
        with sqlite_connection(self.database) as connection:
            a, b, q = (self.stage(connection, action_id)
                       for action_id in ("ACTION-A", "ACTION-B", "ACTION-Q"))
            accepted_a = submission_service.submit(
                connection, self.request("ACTION-A", a), authenticated_consumer_id="forge")
            accepted_b = submission_service.submit(
                connection, self.request("ACTION-B", b), authenticated_consumer_id="forge")
            self.assertEqual(accepted_a.state, "QUEUED")
            self.assertEqual(accepted_b.state, "QUEUED")
            self.assertTrue(submission_service.submit(
                connection, self.request("ACTION-A", a), authenticated_consumer_id="forge").duplicate)
            linked = connection.execute(
                "SELECT intake_id FROM ep_parallel_action_submission_links WHERE submission_id=?",
                (accepted_b.submission_id,),
            ).fetchone()
            self.assertEqual(linked, (b["intake_id"],))
            with self.assertRaises(submission_service.SubmissionError) as waiting:
                submission_service.submit(
                    connection, self.request("ACTION-Q", q), authenticated_consumer_id="forge")
            self.assertEqual(waiting.exception.code, "WAITING_DEPENDENCY")
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_submissions WHERE engineering_action_id='ACTION-Q'"
            ).fetchone())
            altered = self.request("ACTION-Q", q)
            constraints = dict(altered.constraints or {})
            constraints.pop("parallel_action_intake")
            altered = submission_service.SubmissionRequest(
                **{**altered.__dict__, "constraints": constraints})
            with self.assertRaises(submission_service.SubmissionError) as mismatch:
                submission_service.submit(connection, altered, authenticated_consumer_id="forge")
            self.assertEqual(mismatch.exception.code, "PARALLEL_INTAKE_MISMATCH")

    def test_authenticated_http_submission_of_join_waits_for_evidence(self) -> None:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        httpd.data_root = self.root
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            intake_endpoint = (f"http://127.0.0.1:{httpd.server_port}"
                               "/v1/projects/project-test/parallel-action-intakes")
            intake_headers = {
                "Authorization": f"Bearer {self.credential}",
                "Content-Type": "application/json",
                "EP-Action-ID": "ACTION-Q", "EP-Action-Revision": "1",
                "EP-Intent-ID": "intent-test", "EP-Intent-Revision": "1",
                "EP-Correlation-ID": "corr-ACTION-Q",
                "Idempotency-Key": "key-ACTION-Q",
                "EP-Write-Scope": "repository-only",
                "EP-Policy-Digest": admission.SUPPORTED_POLICY_DIGEST,
                "EP-Concurrency-Profile": "DIFFERENT_REPOSITORIES_V1",
            }
            intake_request = Request(
                intake_endpoint, data=json.dumps(self.graph).encode(), method="POST",
                headers=intake_headers,
            )
            unauthenticated_intake = Request(
                intake_endpoint, data=json.dumps(self.graph).encode(), method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as denied_intake:
                urlopen(unauthenticated_intake)  # nosec B310 - loopback test server
            self.assertEqual(denied_intake.exception.code, 401)
            denied_intake.exception.close()
            with urlopen(intake_request) as response:  # nosec B310 - loopback test server
                self.assertEqual(response.status, 201)
                q = json.load(response)
            with urlopen(intake_request) as response:  # nosec B310 - idempotent replay
                self.assertEqual(response.status, 200)
                self.assertTrue(json.load(response)["replayed"])
            readback_url = intake_endpoint + "/" + str(q["intake_id"])
            readback_request = Request(
                readback_url, headers={"Authorization": f"Bearer {self.credential}"},
            )
            with urlopen(readback_request) as response:  # nosec B310 - loopback test server
                decision = json.load(response)
                self.assertEqual(decision["state"], "WAITING_DEPENDENCY")
                self.assertEqual(decision["resource_state"], "NOT_EVALUATED")
                self.assertFalse(decision["dispatch_authorized"])
            with self.assertRaises(HTTPError) as unauthenticated:
                urlopen(readback_url)  # nosec B310 - loopback test server
            self.assertEqual(unauthenticated.exception.code, 401)
            unauthenticated.exception.close()
            request = self.request("ACTION-Q", q)
            payload = {
                "repository_id": request.repository_id,
                "producer": {"id": request.producer_id, "type": request.producer_type,
                             "version": request.producer_version},
                "prompt": request.prompt, "idempotency_key": request.idempotency_key,
                "correlation_id": request.correlation_id, "mission_id": request.mission_id,
                "engineering_action_id": request.engineering_action_id,
                "constraints": request.constraints,
            }
            endpoint = f"http://127.0.0.1:{httpd.server_port}/v1/projects/project-test/submissions"
            submitted = Request(
                endpoint, data=json.dumps(payload).encode(), method="POST",
                headers={"Authorization": f"Bearer {self.credential}",
                         "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as blocked:
                urlopen(submitted)  # nosec B310 - loopback test server
            self.assertEqual(blocked.exception.code, 409)
            self.assertIn("WAITING_DEPENDENCY", blocked.exception.read().decode())
            blocked.exception.close()
            with sqlite_connection(self.database) as connection:
                connection.execute(
                    "UPDATE ep_parallel_action_repository_grants SET status='REVOKED' "
                    "WHERE repository_id='repository-a'",
                )
            alias = json.loads(json.dumps(payload))
            alias["producer"]["id"] = "forged-alias"
            alias["constraints"].pop("parallel_action_intake")
            alias_request = Request(
                endpoint, data=json.dumps(alias).encode(), method="POST",
                headers={"Authorization": f"Bearer {self.credential}",
                         "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as rejected_alias:
                urlopen(alias_request)  # nosec B310 - loopback test server
            self.assertEqual(rejected_alias.exception.code, 403)
            self.assertIn("PRODUCER_PRINCIPAL_MISMATCH",
                          rejected_alias.exception.read().decode())
            rejected_alias.exception.close()
        finally:
            httpd.shutdown()
            worker.join(timeout=5)
            httpd.server_close()
        with sqlite_connection(self.database) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_submissions WHERE engineering_action_id='ACTION-Q'"
            ).fetchone())

    def test_parallel_marker_requires_exact_graph_and_authenticated_producer(self) -> None:
        fake = {"intake_id": "0" * 64, "snapshot_digest": "sha256:" + "0" * 64}
        with sqlite_connection(self.database) as connection:
            with self.assertRaises(submission_service.SubmissionError) as missing_graph:
                submission_service.submit(
                    connection, self.request("ACTION-Q", fake),
                    authenticated_consumer_id="forge",
                )
            self.assertEqual(missing_graph.exception.code, "PARALLEL_GRAPH_REQUIRED")
            q = self.stage(connection, "ACTION-Q")
            forged = self.request("ACTION-Q", q)
            forged = submission_service.SubmissionRequest(
                **{**forged.__dict__, "producer_id": "different-producer"})
            with self.assertRaises(submission_service.SubmissionError) as wrong_producer:
                submission_service.submit(
                    connection, forged, authenticated_consumer_id="different-producer")
            self.assertEqual(wrong_producer.exception.code, "PARALLEL_GRAPH_REQUIRED")
            with self.assertRaises(submission_service.SubmissionError) as no_principal:
                submission_service.submit(connection, self.request("ACTION-Q", q))
            self.assertEqual(no_principal.exception.code, "PARALLEL_PRINCIPAL_REQUIRED")

    def test_graph_cannot_activate_after_unlinked_join_was_admitted(self) -> None:
        fake = {"intake_id": "0" * 64, "snapshot_digest": "sha256:" + "0" * 64}
        request = self.request("ACTION-Q", fake)
        constraints = dict(request.constraints or {})
        constraints.pop("parallel_action_intake")
        serial = submission_service.SubmissionRequest(
            **{**request.__dict__, "constraints": constraints})
        with sqlite_connection(self.database) as connection:
            accepted = submission_service.submit(
                connection, serial, authenticated_consumer_id="forge")
            self.assertEqual(accepted.state, "QUEUED")
            with self.assertRaises(admission.ParallelAdmissionError) as late:
                self.stage(connection, "ACTION-A")
            self.assertEqual(late.exception.code, "GRAPH_ALREADY_DISPATCHED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_parallel_action_graphs"
            ).fetchone(), (0,))

    def test_pre_staging_forge_alias_cannot_queue_unlinked_action(self) -> None:
        fake = {"intake_id": "0" * 64, "snapshot_digest": "sha256:" + "0" * 64}
        request = self.request("ACTION-A", fake)
        constraints = dict(request.constraints or {})
        constraints.pop("parallel_action_intake")
        aliased = submission_service.SubmissionRequest(
            **{**request.__dict__, "producer_id": "forged-alias", "constraints": constraints})
        with sqlite_connection(self.database) as connection:
            with self.assertRaises(submission_service.SubmissionError) as rejected:
                submission_service.submit(
                    connection, aliased, authenticated_consumer_id="forge")
            self.assertEqual(rejected.exception.code, "PRODUCER_PRINCIPAL_MISMATCH")
            wrong_type = submission_service.SubmissionRequest(
                **{**aliased.__dict__, "producer_type": "LEGACY"})
            with self.assertRaises(submission_service.SubmissionError) as type_escape:
                submission_service.submit(
                    connection, wrong_type, authenticated_consumer_id="forge")
            self.assertEqual(type_escape.exception.code, "PRODUCER_PRINCIPAL_MISMATCH")
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_submissions WHERE engineering_action_id='ACTION-A'"
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_parallel_action_graphs").fetchone())
            self.assertFalse(self.stage(connection, "ACTION-B")["replayed"])

    def test_second_consumer_cannot_bypass_staged_action_at_http_or_claim(self) -> None:
        with sqlite_connection(self.database) as connection:
            second_credential = submission_service.issue_consumer_credential(
                connection, consumer_id="second-consumer", project_id="project-test",
            )["credential"]
            q = self.stage(connection, "ACTION-Q")
        request = self.request("ACTION-Q", q)
        constraints = dict(request.constraints or {})
        constraints.pop("parallel_action_intake")
        foreign = submission_service.SubmissionRequest(
            **{**request.__dict__, "producer_id": "second-consumer",
               "constraints": constraints})
        payload = {
            "repository_id": foreign.repository_id,
            "producer": {"id": foreign.producer_id, "type": foreign.producer_type,
                         "version": foreign.producer_version},
            "prompt": foreign.prompt, "idempotency_key": foreign.idempotency_key,
            "correlation_id": foreign.correlation_id, "mission_id": foreign.mission_id,
            "engineering_action_id": foreign.engineering_action_id,
            "constraints": foreign.constraints,
        }
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        httpd.data_root = self.root
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = (f"http://127.0.0.1:{httpd.server_port}"
                        "/v1/projects/project-test/submissions")
            http_request = Request(
                endpoint, data=json.dumps(payload).encode(), method="POST",
                headers={"Authorization": f"Bearer {second_credential}",
                         "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as rejected:
                urlopen(http_request)  # nosec B310 - loopback test server
            self.assertEqual(rejected.exception.code, 409)
            self.assertIn("PARALLEL_GRAPH_REQUIRED",
                          rejected.exception.read().decode())
            rejected.exception.close()
        finally:
            httpd.shutdown()
            worker.join(timeout=5)
            httpd.server_close()
        with sqlite_connection(self.database) as connection:
            with patch.object(admission, "validate_for_submission", return_value=None):
                unlinked = submission_service.submit(
                    connection, foreign,
                    authenticated_consumer_id="second-consumer")
        self.assertNotIn(unlinked.submission_id,
                         LifecycleWorker(self.root).eligible_submission_ids())
        with self.assertRaises(ParityLifecycleDispatchError) as refused:
            ParityLifecycleDispatcher(self.root)._claim(unlinked.submission_id)
        self.assertEqual(str(refused.exception), "PARALLEL_SUBMISSION_UNLINKED")

    def test_pre_enrollment_alias_history_blocks_later_graph(self) -> None:
        fake = {"intake_id": "0" * 64, "snapshot_digest": "sha256:" + "0" * 64}
        request = self.request("ACTION-A", fake)
        constraints = dict(request.constraints or {})
        constraints.pop("parallel_action_intake")
        aliased = submission_service.SubmissionRequest(
            **{**request.__dict__, "producer_id": "legacy-alias", "constraints": constraints})
        with sqlite_connection(self.database) as connection:
            grants = connection.execute(
                "SELECT * FROM ep_parallel_action_repository_grants").fetchall()
            connection.execute("DELETE FROM ep_parallel_action_repository_grants")
            queued = submission_service.submit(
                connection, aliased, authenticated_consumer_id="forge")
            self.assertEqual(queued.state, "QUEUED")
            connection.executemany(
                "INSERT INTO ep_parallel_action_repository_grants VALUES(?,?,?,?,?,?,?,?)",
                grants)
            with self.assertRaises(admission.ParallelAdmissionError) as rejected:
                self.stage(connection, "ACTION-B")
            self.assertEqual(rejected.exception.code, "GRAPH_ALREADY_DISPATCHED")
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ep_parallel_action_graphs").fetchone())

    def test_scope_baseline_and_policy_hold_before_queue(self) -> None:
        with sqlite_connection(self.database) as connection:
            with self.assertRaises(admission.ParallelAdmissionError) as policy:
                self.stage(connection, "ACTION-A", policy_digest="sha256:" + "a" * 64)
            self.assertEqual(policy.exception.code, "UNSUPPORTED_PARALLEL_POLICY")
            a = self.stage(connection, "ACTION-A")
            connection.execute(
                "UPDATE ep_local_repository_bindings SET state='UNBOUND' WHERE repository_id='repository-a'")
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_SCOPE")
            with self.assertRaises(submission_service.SubmissionError) as unbound:
                submission_service.submit(connection, self.request("ACTION-A", a),
                                          authenticated_consumer_id="forge")
            self.assertEqual(unbound.exception.code, "WAITING_SCOPE")
            connection.execute(
                "UPDATE ep_local_repository_bindings SET state='BOUND' WHERE repository_id='repository-a'")
        repository_root = self.repository_roots["repository-a"]
        (repository_root / "changed.txt").write_text("new baseline")
        subprocess.run(["git", "-C", str(repository_root), "add", "changed.txt"], check=True)
        subprocess.run(["git", "-C", str(repository_root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "changed"],
                       check=True)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_BASELINE")
            with self.assertRaises(submission_service.SubmissionError) as drift:
                submission_service.submit(connection, self.request("ACTION-A", a),
                                          authenticated_consumer_id="forge")
            self.assertEqual(drift.exception.code, "WAITING_BASELINE")

    def test_baseline_requires_clean_main_at_exact_head(self) -> None:
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"],
                "DEPENDENCY_ELIGIBLE")
        root = self.repository_roots["repository-a"]
        untracked = root / "untracked-change.txt"
        untracked.write_text("dirty")
        with sqlite_connection(self.database) as connection:
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_BASELINE")
        untracked.unlink()
        subprocess.run(["git", "-C", str(root), "checkout", "-qb", "feature"], check=True)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"], "WAITING_BASELINE")
        subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(admission.dependency_readback(
                connection, intake_id=a["intake_id"])["state"],
                "DEPENDENCY_ELIGIBLE")

    def test_corrupt_stored_graph_fails_closed_at_readback(self) -> None:
        with sqlite_connection(self.database) as connection:
            q = self.stage(connection, "ACTION-Q")
            connection.execute("DROP TRIGGER ep_parallel_action_graphs_immutable_update")
            connection.execute("UPDATE ep_parallel_action_graphs SET snapshot='{}'")
            decision = admission.dependency_readback(connection, intake_id=q["intake_id"])
            self.assertEqual(decision["state"], "INVALID_SNAPSHOT")
            self.assertFalse(decision["dispatch_authorized"])

    def test_corrupt_staged_snapshot_cannot_hide_action_from_other_consumer(self) -> None:
        with sqlite_connection(self.database) as connection:
            q = self.stage(connection, "ACTION-Q")
            graph_id, snapshot = connection.execute(
                "SELECT graph_id,snapshot FROM ep_parallel_action_graphs"
            ).fetchone()
            corrupted = json.loads(snapshot)
            corrupted["actions"] = [item for item in corrupted["actions"]
                                    if item["action_id"] != "ACTION-Q"]
            connection.execute("DROP TRIGGER ep_parallel_action_graphs_immutable_update")
            connection.execute(
                "UPDATE ep_parallel_action_graphs SET snapshot=? WHERE graph_id=?",
                (json.dumps(corrupted, sort_keys=True, separators=(",", ":")), graph_id),
            )
            request = self.request("ACTION-Q", q)
            constraints = dict(request.constraints or {})
            constraints.pop("parallel_action_intake")
            foreign = submission_service.SubmissionRequest(
                **{**request.__dict__, "producer_id": "second-consumer",
                   "constraints": constraints})
            with self.assertRaises(submission_service.SubmissionError) as refused:
                submission_service.submit(
                    connection, foreign,
                    authenticated_consumer_id="second-consumer")
            self.assertEqual(refused.exception.code, "INVALID_SNAPSHOT")

    def test_real_terminal_finalizer_attests_linked_predecessor(self) -> None:
        started, completed = "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"
        with sqlite_connection(self.database) as connection:
            a = self.stage(connection, "ACTION-A")
            accepted = submission_service.submit(
                connection, self.request("ACTION-A", a), authenticated_consumer_id="forge")
            revision = self.graph["actions"][0]["target"]["baseline_revision"]
            run_id = "run-actual-a"
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)",
                (run_id, "project-test", "COMPLETE", started, completed, "MANAGED"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,
                    claimed_at,updated_at,operator_resolution)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (accepted.submission_id, "project-test", "repository-a", run_id,
                 "COMPLETE", "/synthetic/prompt", started, completed, "NONE"),
            )
            checkpoint = TransactionState(
                run_id, "repository-a", "prompt", "COMPLETE", terminal=True,
                action_intent="MUTATING_DELIVERY", transaction_kind="IMPLEMENTATION",
                requested_repository_revision=revision,
                execution_baseline_sha=revision,
                implementation_head_sha=revision,
                implementation_merge_commit=revision,
                commit_evidence=({"phase": "WAIT_FOR_OPERATOR_MERGE",
                                  "observed_at": completed, "commit_sha": revision,
                                  "description": "implementation_merge_verified"},),
            )
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                (run_id, json.dumps(checkpoint.to_dict()), "COMPLETE", completed),
            )
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, "COMPLETE", "synthetic", completed, revision,
                 "/synthetic/report", completed),
            )
        terminal_artifact_id = submission_service.write_terminal_evidence(
            self.root, repository_root=self.repository_roots["repository-a"],
            run_id="run-actual-a",
        )
        with sqlite_connection(self.database) as connection:
            outcome = connection.execute(
                "SELECT terminal_artifact_id FROM ep_parallel_action_outcomes WHERE intake_id=?",
                (a["intake_id"],),
            ).fetchone()
            self.assertIsNone(outcome)
            self.assertEqual(admission.reconcile_predecessors(
                connection, intake_id=a["intake_id"]), 0)
            admission.attest_terminal_outcome(
                connection, intake_id=a["intake_id"],
                submission_id=accepted.submission_id,
            )
            outcome = connection.execute(
                "SELECT terminal_artifact_id FROM ep_parallel_action_outcomes WHERE intake_id=?",
                (a["intake_id"],),
            ).fetchone()
            self.assertEqual(outcome, (terminal_artifact_id,))
            readback = submission_service.producer_readback(
                connection, project_id="project-test", submission_id=accepted.submission_id,
                contract_version="1.3",
            )
            self.assertTrue(readback["result"]["delivery_qualified"])

    def test_pa_e2_claims_two_repositories_and_fences_duplicate_and_dependency(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        for repository_id, root in self.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
        with sqlite_connection(self.database) as connection:
            staged = {action: self.stage(
                connection, action, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
            ) for action in ("ACTION-A", "ACTION-B", "ACTION-Q")}
            submissions = {action: submission_service.submit(
                connection, self.request(
                    action, staged[action], policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
                ), authenticated_consumer_id="forge",
            ).submission_id for action in ("ACTION-A", "ACTION-B")}
            blocked_q = admission.dependency_readback(connection, intake_id=staged["ACTION-Q"]["intake_id"])
            self.assertEqual(blocked_q["state"], "WAITING_DEPENDENCY")
        worker = LifecycleWorker(self.root, dispatcher_factory=lambda: None)
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("held-resource", "project-test", "BLOCKED", NOW, NOW),
            )
            for resource in delivery.repository_resources(self.repository_roots["repository-a"]):
                connection.execute(
                    "INSERT INTO ep_execution_leases VALUES(?,?,?,?,?,NULL)",
                    (resource, "held-resource", "pa-e2:repository", NOW,
                     "9999-12-31T23:59:59+00:00"),
                )
        self.assertEqual(worker.eligible_submission_ids(), [submissions["ACTION-B"]])
        with sqlite_connection(self.database) as connection:
            delivery.release_resources(connection, "held-resource")
        self.assertEqual(set(worker.eligible_submission_ids()), set(submissions.values()))
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": "inherited-parent"}):
            with self.assertRaisesRegex(ParityLifecycleDispatchError, "PA_E2_ISOLATED_PROCESS_REQUIRED"):
                dispatcher._claim(submissions["ACTION-A"])
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            first = dispatcher._claim(submissions["ACTION-A"])
            second = dispatcher._claim(submissions["ACTION-B"])
            duplicate = dispatcher._claim(submissions["ACTION-A"])
        self.assertNotEqual(first[2], second[2])
        self.assertEqual(duplicate[2], first[2])
        self.assertTrue(duplicate[4])
        subprocess.run(("git", "-C", str(self.repository_roots["repository-a"]),
                        "remote", "set-url", "origin", "https://github.com/fixture/changed.git"), check=True)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            with self.assertRaisesRegex(ParityLifecycleDispatchError, "REPOSITORY_RESOURCE_CHANGED"):
                dispatcher._claim(submissions["ACTION-A"])
        subprocess.run(("git", "-C", str(self.repository_roots["repository-a"]),
                        "remote", "set-url", "origin", "https://github.com/fixture/repository-a.git"), check=True)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE lease_id LIKE 'pa-e2:slot:%' "
                "AND released_at IS NULL").fetchone()[0], 2)
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")
        dispatcher._set_state(submissions["ACTION-A"], first[2], "COMPLETE")
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE lease_id LIKE 'pa-e2:slot:%' "
                "AND released_at IS NULL").fetchone()[0], 1)

    def test_pa_e2_origin_alias_waits_and_readback_distinguishes_capacity(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        for repository_id, root in self.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
            subprocess.run(("git", "-C", str(root), "remote", "set-url", "--push",
                            "origin", "https://github.com/fixture/shared.git"), check=True)
        with sqlite_connection(self.database) as connection:
            staged = {action: self.stage(
                connection, action, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
            ) for action in ("ACTION-A", "ACTION-B")}
            submissions = {action: submission_service.submit(
                connection, self.request(
                    action, staged[action], policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
                ), authenticated_consumer_id="forge",
            ).submission_id for action in ("ACTION-A", "ACTION-B")}
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            first = dispatcher._claim(submissions["ACTION-A"])
            with self.assertRaisesRegex(ParityLifecycleDispatchError, "WAITING_RESOURCE"):
                dispatcher._claim(submissions["ACTION-B"])
        with sqlite_connection(self.database) as connection:
            decision = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-B"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-B"]["intake_id"])),
            )
            self.assertEqual((decision["state"], decision["resource_state"]),
                             ("WAITING_RESOURCE", "HELD"))
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("other-provider", "project-test", "RUNNING", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO ep_execution_leases VALUES(?,?,?,?,?,NULL)",
                ("pa-e2:slot:1", "other-provider", "pa-e2:provider", NOW,
                 "9999-12-31T23:59:59+00:00"),
            )
            combined = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-B"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-B"]["intake_id"])),
            )
            self.assertEqual((combined["state"], combined["resource_state"],
                              combined["capacity_state"]),
                             ("WAITING_RESOURCE", "HELD", "FULL"))
            delivery.release_capacity(connection, "other-provider")
        dispatcher._set_state(submissions["ACTION-A"], first[2], "BLOCKED")
        with sqlite_connection(self.database) as connection:
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-b",
            ).state, "WAITING_RESOURCE")
        dismiss_operator_gate(self.root, project_id="project-test", run_id=first[2])
        with sqlite_connection(self.database) as connection:
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-b",
            ).state, "READY")
            # The same available resource can independently be held by a
            # full provider pool; projection must name capacity, not resource.
            for index in range(2):
                run_id = f"capacity-fixture-{index}"
                connection.execute(
                    "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (run_id, "project-test", "RUNNING", NOW, NOW),
                )
                connection.execute(
                    """INSERT INTO ep_execution_leases VALUES(?,?,?,?,?,NULL)
                       ON CONFLICT(lease_id) DO UPDATE SET run_id=excluded.run_id,
                       released_at=NULL""",
                    (f"pa-e2:slot:{index}", run_id, "pa-e2:provider", NOW,
                     "9999-12-31T23:59:59+00:00"),
                )
            decision = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-B"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-B"]["intake_id"])),
            )
            self.assertEqual((decision["state"], decision["capacity_state"]),
                             ("WAITING_CAPACITY", "FULL"))
            delivery.release_capacity(connection, "capacity-fixture-0")
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-b",
            ).state, "READY")

    def test_pa_e2_worker_and_provider_intervals_overlap_in_separate_processes(self) -> None:
        for repository_id, root in self.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
        with sqlite_connection(self.database) as connection:
            staged = {action: self.stage(
                connection, action, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
            ) for action in ("ACTION-A", "ACTION-B")}
            submissions = {action: submission_service.submit(
                connection, self.request(
                    action, staged[action], policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
                ), authenticated_consumer_id="forge",
            ).submission_id for action in ("ACTION-A", "ACTION-B")}
        worker = LifecycleWorker(self.root)
        with patch.dict(os.environ, {"EP_QUALIFICATION_INITIALIZE_ONLY": "1"}):
            self.assertTrue(worker.run_once())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with sqlite_connection(self.database) as connection:
                    rows = connection.execute(
                        "SELECT submission_id,run_id FROM ep_parity_lifecycle_dispatches "
                        "WHERE submission_id IN (?,?) AND state='RUNNING'",
                        tuple(submissions.values()),
                    ).fetchall()
                if len(rows) == 2 and worker.diagnostics().dispatched == 2:
                    break
                time.sleep(.02)
            self.assertEqual(len(rows), 2)
            self.assertEqual(worker.diagnostics().dispatched, 2)
        self.assertEqual(len({row[1] for row in rows}), 2)
        rendezvous = Path(self.temporary.name) / "provider-overlap"
        rendezvous.mkdir()
        env = {**os.environ, "EP_QUALIFICATION_PA_E2_OVERLAP_DIR": str(rendezvous)}
        driver = Path(__file__).parents[1] / "fixtures" / "pa_e2_provider_driver.py"
        processes = [subprocess.Popen(
            (sys.executable, str(driver), str(self.root), submissions[action]),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for action in ("ACTION-A", "ACTION-B")]
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if len(list(rendezvous.glob("*.provider-started"))) == 2:
                    break
                if any(process.poll() is not None for process in processes):
                    break
                time.sleep(.02)
            markers = [json.loads(path.read_text()) for path in rendezvous.glob("*.provider-started")]
            self.assertEqual(len(markers), 2)
            self.assertEqual(len({marker["pid"] for marker in markers}), 2)
            self.assertEqual(len({marker["root"] for marker in markers}), 2)
            self.assertTrue(all(process.poll() is None for process in processes))
            (rendezvous / "release").write_text("go", encoding="utf-8")
            outputs = [process.communicate(timeout=20) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0, 0], outputs)
            self.assertEqual({json.loads(output[0])["run_id"] for output in outputs},
                             {row[1] for row in rows})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_pa_e2_resource_identity_rejects_unqualified_origin_and_git_failure(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        self.assertEqual(delivery._origin_identity("git@github.com:Owner/Repo.git"),
                         "github.com/owner/repo")
        self.assertEqual(delivery._origin_identity("ssh://git@github.com/Owner/Repo"),
                         "github.com/owner/repo")
        with self.assertRaisesRegex(delivery.DeliveryScopeError, "REPOSITORY_ORIGIN_UNQUALIFIED"):
            delivery._origin_identity("https://example.com/owner/repo.git")
        with self.assertRaisesRegex(delivery.DeliveryScopeError, "REPOSITORY_ORIGIN_UNQUALIFIED"):
            delivery._origin_identity("https://github.com/owner")
        with self.assertRaisesRegex(delivery.DeliveryScopeError, "REPOSITORY_RESOURCE_UNAVAILABLE"):
            delivery._git(self.root, "rev-parse", "--git-common-dir")
        with patch("engineering_platform.parallel_action_delivery.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git", 5)):
            with self.assertRaisesRegex(delivery.DeliveryScopeError, "REPOSITORY_RESOURCE_UNAVAILABLE"):
                delivery._git(self.repository_roots["repository-a"], "rev-parse", "--git-common-dir")
        with patch("engineering_platform.parallel_action_delivery.additional_workspace_write_roots",
                   return_value=(self.root,)):
            with self.assertRaisesRegex(delivery.DeliveryScopeError, "PA_E2_SHARED_WRITE_SCOPE_UNQUALIFIED"):
                delivery.repository_resources(self.repository_roots["repository-a"])

    def test_pa_e2_waits_behind_active_legacy_project_gate(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        for repository_id, root in self.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
        with sqlite_connection(self.database) as connection:
            staged = self.stage(connection, "ACTION-A",
                                policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST)
            submission = submission_service.submit(
                connection, self.request(
                    "ACTION-A", staged, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
                ), authenticated_consumer_id="forge",
            ).submission_id
            legacy = submission_service.submit(connection, submission_service.SubmissionRequest(
                "project-test", "repository-b", "operator", "HUMAN", None,
                "Legacy independent task.", "HTTP",
            )).submission_id
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("legacy-run", "project-test", "RUNNING", NOW, NOW),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches
                   (submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (legacy, "project-test", "repository-b", "legacy-run", "RUNNING", "prompt", NOW, NOW),
            )
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")
        self.assertNotIn(submission, LifecycleWorker(self.root).eligible_submission_ids())

    def test_pa_e2_child_process_protocol_reports_success_and_claim_wait(self) -> None:
        from engineering_platform import pa_e2_dispatch
        with patch.dict(os.environ, {}, clear=False), patch.object(sys, "argv", [
            "pa_e2_dispatch", str(self.root), "submission-test",
        ]), patch.object(pa_e2_dispatch, "ParityLifecycleDispatcher") as dispatcher:
            dispatcher.return_value.dispatch.return_value = SimpleNamespace(run_id="run-test", state="RUNNING")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(pa_e2_dispatch.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"run_id": "run-test", "state": "RUNNING"})
            self.assertEqual(os.environ["EP_PA_E2_ISOLATED_PROCESS"], str(os.getpid()))
            dispatcher.return_value.dispatch.side_effect = ParityLifecycleDispatchError("WAITING_RESOURCE")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(pa_e2_dispatch.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"error": "WAITING_RESOURCE"})

    def test_pa_e3_new_process_restart_requires_a_returned_checkpoint(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        staged, submissions = self.pa_e3_submissions()
        first_code, first = self.pa_e3_child(submissions["ACTION-A"], "claimed")
        self.assertEqual(first_code, 0, first)
        second_code, second = self.pa_e3_child(submissions["ACTION-A"], "claimed")
        self.assertEqual(second_code, 0, second)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertTrue(second["duplicate"])
        returned_code, returned = self.pa_e3_child(submissions["ACTION-A"], "returned")
        self.assertEqual(returned_code, 0, returned)
        next_code, next_attempt = self.pa_e3_child(submissions["ACTION-A"], "entered")
        self.assertEqual(next_code, 0, next_attempt)
        self.assertEqual(next_attempt["run_id"], first["run_id"])
        denied_code, denied = self.pa_e3_child(submissions["ACTION-A"], "claimed")
        self.assertEqual((denied_code, denied["error"]),
                         (2, "PA_E3_PROVIDER_EFFECT_UNCERTAIN"))
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, str(first["run_id"])),
                             "PROVIDER_EFFECT_UNCERTAIN")
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")
            decision = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-A"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-A"]["intake_id"])),
                continuation_run_id=str(first["run_id"]),
            )
            self.assertEqual((decision["state"], decision["recovery_state"]),
                             ("WAITING_RECOVERY", "PROVIDER_EFFECT_UNCERTAIN"))
        projected = next(record for record in server._central_console_run_records(
            self.root, "project-test") if record["run_id"] == first["run_id"])
        self.assertEqual(projected["recovery_state"], "PROVIDER_EFFECT_UNCERTAIN")
        self.assertFalse(projected["can_cancel"])
        with self.assertRaisesRegex(ParityLifecycleDispatchError,
                                    "PA_E3_PROVIDER_EFFECT_UNCERTAIN"):
            dismiss_operator_gate(self.root, project_id="project-test",
                                  run_id=str(first["run_id"]))
        worker = LifecycleWorker(self.root)
        self.assertNotIn(submissions["ACTION-A"], worker.eligible_submission_ids())
        self.assertIn(submissions["ACTION-B"], worker.eligible_submission_ids())
        sibling_code, sibling = self.pa_e3_child(submissions["ACTION-B"], "claimed")
        self.assertEqual(sibling_code, 0, sibling)
        self.assertNotEqual(sibling["run_id"], first["run_id"])
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE lease_id LIKE 'pa-e2:slot:%' "
                "AND released_at IS NULL",
            ).fetchone()[0], 2)

    def test_pa_e3_exact_cancel_before_runner_releases_only_that_action(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        with self.assertRaisesRegex(recovery.RecoveryFenceError, "PA_E3_DISPATCH_ACTIVE"):
            recovery.acquire(
                self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
                project_id=context.project_id, repository_id=context.repository_id,
            )
        with self.assertRaisesRegex(recovery.RecoveryFenceError, "PA_E3_CANCEL_TARGET_UNAVAILABLE"):
            recovery.request_cancel(self.root, project_id="wrong-project", run_id=run_id)
        self.assertEqual(recovery.request_cancel(
            self.root, project_id="project-test", run_id=run_id,
        )["state"], "CANCEL_REQUESTED")
        self.assertEqual(recovery.request_cancel(
            self.root, project_id="project-test", run_id=run_id,
        )["state"], "CANCEL_REQUESTED")
        self.assertEqual(recovery.runner_entry(
            self.root, run_id=run_id, attempt_id=attempt,
        ), "CANCEL_REQUESTED")
        recovery.acknowledge_pre_runner_cancel(
            self.root, run_id=run_id,
            submission_id=submissions["ACTION-A"], attempt_id=attempt,
        )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_ACKNOWLEDGED")
            decision = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-A"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-A"]["intake_id"])),
                continuation_run_id=run_id,
            )
            self.assertEqual((decision["state"], decision["resource_state"],
                              decision["capacity_state"]),
                             ("CANCEL_ACKNOWLEDGED", "AVAILABLE", "AVAILABLE"))
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "READY")
            self.assertEqual(connection.execute(
                "SELECT state,operator_resolution FROM ep_parity_lifecycle_dispatches "
                "WHERE run_id=?", (run_id,),
            ).fetchone(), ("FAILED", "DISMISSED"))
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            with self.assertRaisesRegex(ParityLifecycleDispatchError,
                                        "PA_E3_DISPATCH_ALREADY_TERMINAL"):
                dispatcher._claim(submissions["ACTION-A"])

    def test_pa_e3_cancel_after_returned_checkpoint_keeps_prior_effects_held(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        _staged, submissions = self.pa_e3_submissions()
        code, returned = self.pa_e3_child(submissions["ACTION-A"], "returned")
        self.assertEqual(code, 0, returned)
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        self.assertEqual(recovery.runner_entry(
            self.root, run_id=run_id, attempt_id=attempt,
        ), "CANCEL_REQUESTED")
        recovery.acknowledge_pre_runner_cancel(
            self.root, run_id=run_id,
            submission_id=submissions["ACTION-A"], attempt_id=attempt,
        )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_ACKNOWLEDGED")
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")
            self.assertEqual(connection.execute(
                "SELECT operator_resolution FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
                (run_id,),
            ).fetchone(), ("OPEN",))

    def test_pa_e3_pending_cancel_skips_preflight_on_checkpoint_resume(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        code, returned = self.pa_e3_child(submissions["ACTION-A"], "returned")
        self.assertEqual(code, 0, returned)
        run_id = str(returned["run_id"])
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda _root: (_ for _ in ()).throw(
                AssertionError("cancelled checkpoint must not create a runner")),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input",
            side_effect=AssertionError("cancelled checkpoint must not run preflight"),
        ):
            receipt = dispatcher.dispatch(submissions["ACTION-A"])
        self.assertEqual((receipt.run_id, receipt.state), (run_id, "FAILED"))
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_ACKNOWLEDGED")
            self.assertEqual(connection.execute(
                "SELECT operator_resolution FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
                (run_id,),
            ).fetchone(), ("OPEN",))

    def test_pa_e3_cancel_after_runner_entry_preserves_repository_hold(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        self.assertEqual(recovery.runner_entry(
            self.root, run_id=run_id, attempt_id=attempt,
        ), "RUNNER_ENTERED")
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        dispatcher._set_state(submissions["ACTION-A"], run_id, "BLOCKED")
        recovery.acknowledge_stopped_provider(
            self.root, run_id=run_id,
            submission_id=submissions["ACTION-A"], attempt_id=attempt,
        )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_ACKNOWLEDGED")
            decision = admission.delivery_readback(
                connection, intake_id=str(staged["ACTION-A"]["intake_id"]),
                data_root=self.root,
                decision=admission.dependency_readback(
                    connection, intake_id=str(staged["ACTION-A"]["intake_id"])),
                continuation_run_id=run_id,
            )
            self.assertEqual((decision["state"], decision["resource_state"],
                              decision["capacity_state"]),
                             ("CANCEL_ACKNOWLEDGED", "HELD", "AVAILABLE"))
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")
            self.assertEqual(connection.execute(
                "SELECT state,operator_resolution FROM ep_parity_lifecycle_dispatches "
                "WHERE run_id=?", (run_id,),
            ).fetchone(), ("BLOCKED", "OPEN"))
        dismiss_operator_gate(self.root, project_id="project-test", run_id=run_id)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "READY")

    def test_pa_e3_console_cancel_route_is_project_scoped_and_audited(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        code, claimed = self.pa_e3_child(submissions["ACTION-A"], "claimed")
        self.assertEqual(code, 0, claimed)
        run_id = str(claimed["run_id"])
        projected = next(record for record in server._central_console_run_records(
            self.root, "project-test") if record["run_id"] == run_id)
        self.assertTrue(projected["can_cancel"])
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        httpd.data_root = self.root
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_port}/api/execution-cancel"

            def request(project: str, payload: dict[str, str], *, origin: str | None = None) -> Request:
                headers = {"Content-Type": "application/json"}
                if origin is not None:
                    headers["Origin"] = origin
                return Request(
                    base + "?project=" + project, data=json.dumps(payload).encode(),
                    method="POST", headers=headers,
                )

            with self.assertRaises(HTTPError) as foreign:
                urlopen(request("foreign-project", {"run_id": run_id}))  # nosec B310 - loopback
            self.assertEqual(foreign.exception.code, 409)
            foreign.exception.close()
            with self.assertRaises(HTTPError) as origin_denied:
                urlopen(request("project-test", {"run_id": run_id}, origin="https://invalid.example"))  # nosec B310 - loopback
            self.assertEqual(origin_denied.exception.code, 403)
            origin_denied.exception.close()
            with urlopen(request("project-test", {"run_id": run_id})) as response:  # nosec B310 - loopback
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["state"], "CANCEL_REQUESTED")
        finally:
            httpd.shutdown()
            worker.join(timeout=5)
            httpd.server_close()
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_REQUESTED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM engineering_component_logs "
                "WHERE component='operations_console' "
                "AND json_extract(payload, '$.run_id')=? "
                "AND json_extract(payload, '$.audit_action')='execution_cancel_requested'",
                (run_id,),
            ).fetchone()[0], 1)

    def test_pa_e3_console_mutation_rejects_dns_rebinding_host(self) -> None:
        port = 8765
        for host in ("127.0.0.1:8765", "localhost:8765", "100.100.10.1:8765"):
            with self.subTest(host=host):
                self.assertTrue(server._execution_console_origin(
                    {"Host": host, "Origin": "http://" + host}, port,
                ))
        for host in ("attacker.example:8765", "192.168.1.10:8765",
                     "127.0.0.1:8766", "user@127.0.0.1:8765", ""):
            with self.subTest(host=host):
                self.assertFalse(server._execution_console_origin(
                    {"Host": host, "Origin": "http://" + host}, port,
                ))
        self.assertFalse(server._execution_console_origin(
            {"Host": "127.0.0.1:8765", "Origin": "http://attacker.example:8765"}, port,
        ))

    def test_pa_e3_cancel_audit_write_failure_rolls_back_intent(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        code, claimed = self.pa_e3_child(submissions["ACTION-A"], "claimed")
        self.assertEqual(code, 0, claimed)
        run_id = str(claimed["run_id"])
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "CREATE TRIGGER reject_cancel_audit BEFORE INSERT ON engineering_component_logs "
                "WHEN NEW.component='operations_console' "
                "BEGIN SELECT RAISE(ABORT, 'audit write blocked'); END"
            )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "audit write blocked"):
            recovery.request_cancel(
                self.root, project_id="project-test", run_id=run_id,
                audit_actor="DASHBOARD_USER",
            )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "NONE")

    def test_pa_e3_fence_rejects_invalid_target_entry_and_checkpoint(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_DISPATCH_TARGET_INVALID"):
            recovery.acquire(
                self.root, run_id="run-nonexistent", submission_id=submissions["ACTION-A"],
                project_id="project-test", repository_id="repository-a",
            )
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_DISPATCH_FENCE_CONFLICT"):
            recovery.runner_entry(self.root, run_id=run_id, attempt_id="wrong-attempt")
        with patch.object(recovery, "capture_process_identity", return_value=None):
            with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                        "PA_E3_PROCESS_IDENTITY_UNAVAILABLE"):
                recovery.runner_entry(self.root, run_id=run_id, attempt_id=attempt)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_CHECKPOINT_INVALID"):
            recovery.complete_attempt(self.root, run_id=run_id, attempt_id=attempt,
                                      checkpoint_phase="COMPLETE")
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_RUNNER_ENTRY_MISSING"):
            recovery.complete_attempt(self.root, run_id=run_id, attempt_id=attempt,
                                      checkpoint_phase="WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(recovery.runner_entry(
            self.root, run_id=run_id, attempt_id=attempt,
        ), "RUNNER_ENTERED")
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_RUNNER_ALREADY_ENTERED"):
            recovery.runner_entry(self.root, run_id=run_id, attempt_id=attempt)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_DISPATCH_FENCE_CONFLICT"):
            recovery.complete_attempt(self.root, run_id=run_id, attempt_id="wrong-attempt",
                                      checkpoint_phase="WAIT_FOR_OPERATOR_MERGE")
        recovery.complete_attempt(self.root, run_id=run_id, attempt_id=attempt,
                                  checkpoint_phase="WAIT_FOR_OPERATOR_MERGE")
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_DISPATCH_TARGET_INVALID"):
            recovery.acquire(
                self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
                project_id="wrong-project", repository_id="repository-a",
            )
        next_attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        self.assertNotEqual(attempt, next_attempt)
        recovery.release_attempt(self.root, run_id=run_id, attempt_id=next_attempt)

    def test_pa_e3_uncertain_and_cancel_pending_block_operator_release(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        with sqlite_connection(self.database) as connection:
            with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                        "PA_E3_CANCEL_ACK_PENDING"):
                recovery.require_resolved(connection, run_id)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_CANCEL_NOT_REQUESTED"):
            recovery.acknowledge_pre_runner_cancel(
                self.root, run_id="unknown-run", submission_id=submissions["ACTION-A"],
                attempt_id=attempt,
            )
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_DISPATCH_FENCE_CONFLICT"):
            recovery.acknowledge_pre_runner_cancel(
                self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
                attempt_id="wrong-attempt",
            )
        recovery.mark_uncertain(self.root, run_id=run_id)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id),
                             "PROVIDER_EFFECT_UNCERTAIN")
            with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                        "PA_E3_PROVIDER_EFFECT_UNCERTAIN"):
                recovery.require_resolved(connection, run_id)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_PROVIDER_EFFECT_UNCERTAIN"):
            recovery.runner_entry(self.root, run_id=run_id, attempt_id=attempt)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_PROVIDER_EFFECT_UNCERTAIN"):
            recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        recovery.release_attempt(self.root, run_id=run_id, attempt_id=attempt)
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_PROVIDER_EFFECT_UNCERTAIN"):
            recovery.acquire(
                self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
                project_id=context.project_id, repository_id=context.repository_id,
            )

    def test_pa_e3_terminal_cancel_is_too_late_without_retroactive_ack(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submissions["ACTION-A"])
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submissions["ACTION-A"],
            project_id=context.project_id, repository_id=context.repository_id,
        )
        recovery.close_terminal_cancel(self.root, run_id=run_id)
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        recovery.close_terminal_cancel(self.root, run_id=run_id)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_REQUESTED")
        dispatcher._set_state(submissions["ACTION-A"], run_id, "COMPLETE")
        recovery.close_terminal_cancel(self.root, run_id=run_id)
        recovery.close_terminal_cancel(self.root, run_id=run_id)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_TOO_LATE")
        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                    "PA_E3_CANCEL_NOT_REQUESTED"):
            recovery.acknowledge_stopped_provider(
                self.root, run_id=run_id,
                submission_id=submissions["ACTION-A"], attempt_id=attempt,
            )

    def test_pa_e3_dispatch_acknowledges_cancel_before_runner_entry(self) -> None:
        _staged, submissions = self.pa_e3_submissions()

        class NoProviderRunner:
            def __init__(self) -> None:
                self.agent = self
                self.called = False

            def set_cancellation_check(self, callback: object) -> None:
                self.callback = callback

            def run(self, *_: object, **__: object) -> None:
                self.called = True
                raise AssertionError("provider must not run after pre-entry cancel")

        runner = NoProviderRunner()
        dispatcher = ParityLifecycleDispatcher(self.root, runner_factory=lambda _root: runner)

        def cancel_during_preflight(_root: Path, _candidate: object,
                                    run_id: str, _prompt: Path) -> None:
            recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)

        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", side_effect=cancel_during_preflight,
        ):
            receipt = dispatcher.dispatch(submissions["ACTION-A"])
        self.assertEqual(receipt.state, "FAILED")
        self.assertFalse(runner.called)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, receipt.run_id), "CANCEL_ACKNOWLEDGED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:%' AND released_at IS NULL",
                (receipt.run_id,),
            ).fetchone()[0], 0)

    def test_pa_e3_dispatch_acknowledges_owned_provider_stop(self) -> None:
        from engineering_platform import parallel_action_delivery as delivery
        _staged, submissions = self.pa_e3_submissions()

        class CancelledAgent:
            def __init__(self) -> None:
                self.check = lambda: False
                self.observed = False

            def set_cancellation_check(self, callback: object) -> None:
                self.check = callback

            def cancellation_observed(self) -> bool:
                return self.observed

            def provider_process_cleanup_confirmed(self) -> bool:
                return True

        class CancelledRunner:
            def __init__(self, agent: CancelledAgent) -> None:
                self.agent = agent

            def run(self, _prompt: Path, *, run_id: str, **_: object) -> None:
                recovery.request_cancel(self_root, project_id="project-test", run_id=run_id)
                self.agent.observed = bool(self.agent.check())
                raise CodexInvocationError(
                    "operator cancellation", "owned provider stopped",
                    next_action="NONE", terminal_condition="operator_cancellation",
                )

        self_root = self.root
        agent = CancelledAgent()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda _root: CancelledRunner(agent),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ):
            with self.assertRaises(CodexInvocationError):
                dispatcher.dispatch(submissions["ACTION-A"])
        self.assertTrue(agent.observed)
        with sqlite_connection(self.database) as connection:
            row = connection.execute(
                "SELECT run_id,state,operator_resolution FROM ep_parity_lifecycle_dispatches "
                "WHERE submission_id=?", (submissions["ACTION-A"],),
            ).fetchone()
            self.assertEqual((row[1], row[2]), ("BLOCKED", "OPEN"))
            self.assertEqual(recovery.status(connection, str(row[0])), "CANCEL_ACKNOWLEDGED")
            self.assertEqual(delivery.gate(
                connection, data_root=self.root, project_id="project-test",
                repository_id="repository-a",
            ).state, "WAITING_RESOURCE")

    def test_pa_e3_dispatch_initialize_only_and_active_owner_exclusion(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {
            "EP_PA_E2_ISOLATED_PROCESS": str(os.getpid()),
            "EP_QUALIFICATION_INITIALIZE_ONLY": "1",
        }):
            initialized = dispatcher.dispatch(submissions["ACTION-A"])
        self.assertEqual(initialized.state, "RUNNING")
        attempt = recovery.acquire(
            self.root, run_id=initialized.run_id,
            submission_id=submissions["ACTION-A"],
            project_id="project-test", repository_id="repository-a",
        )
        try:
            with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
                with self.assertRaisesRegex(ParityLifecycleDispatchError,
                                            "PA_E3_DISPATCH_ACTIVE"):
                    dispatcher.dispatch(submissions["ACTION-A"])
        finally:
            recovery.release_attempt(self.root, run_id=initialized.run_id,
                                     attempt_id=attempt)

    def test_pa_e3_dispatch_returns_a_resumable_child_free_checkpoint(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda root: _PaE3CheckpointRunner(root, self.root),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ), patch.object(
            dispatcher, "_release_pa_e2_capacity",
            side_effect=AssertionError("no release may follow a returned checkpoint"),
        ):
            first = dispatcher.dispatch(submissions["ACTION-A"])
            second = dispatcher.dispatch(submissions["ACTION-A"])
        self.assertEqual((first.state, second.state), ("RUNNING", "RUNNING"))
        self.assertEqual(first.run_id, second.run_id)
        self.assertTrue(second.duplicate_claim)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e3:returned:%'",
                (first.run_id,),
            ).fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (first.run_id,),
            ).fetchone()[0], 0)

    def test_pa_e3_dispatch_nonterminal_cancel_requires_stop_ack(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda root: _PaE3CheckpointRunner(
                root, self.root, cancel=True, observe_cancel=True,
            ),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ):
            receipt = dispatcher.dispatch(submissions["ACTION-A"])
        self.assertEqual(receipt.state, "BLOCKED")
        with sqlite_connection(self.database) as connection:
            self.assertEqual(recovery.status(connection, receipt.run_id),
                             "CANCEL_ACKNOWLEDGED")
            self.assertEqual(connection.execute(
                "SELECT operator_resolution FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
                (receipt.run_id,),
            ).fetchone(), ("OPEN",))

    def test_pa_e3_cancel_ack_failure_keeps_capacity_and_repository_held(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda root: _PaE3CheckpointRunner(
                root, self.root, cancel=True, observe_cancel=True,
            ),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ), patch.object(
            recovery, "acknowledge_stopped_provider_in_transaction",
            side_effect=RuntimeError("ack write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "ack write failed"):
                dispatcher.dispatch(submissions["ACTION-A"])
        with sqlite_connection(self.database) as connection:
            run_id = str(connection.execute(
                "SELECT run_id FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (submissions["ACTION-A"],),
            ).fetchone()[0])
            self.assertEqual(recovery.status(connection, run_id), "PROVIDER_EFFECT_UNCERTAIN")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 1)
            self.assertGreaterEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:resource:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 1)

    def test_pa_e3_terminal_state_and_cancel_ack_commit_together(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        submission_id = submissions["ACTION-A"]
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submission_id)
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submission_id,
            project_id=context.project_id, repository_id=context.repository_id,
        )
        recovery.runner_entry(self.root, run_id=run_id, attempt_id=attempt)
        recovery.request_cancel(self.root, project_id="project-test", run_id=run_id)
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_pa_e3_terminal_release BEFORE UPDATE ON ep_execution_leases "
                "WHEN OLD.lease_id LIKE 'pa-e2:slot:%' AND NEW.released_at IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'slot release blocked'); END"
            )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "slot release blocked"):
            dispatcher._set_state(
                submission_id, run_id, "BLOCKED",
                pa_e3_attempt_id=attempt, pa_e3_ack_stopped=True,
            )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()[0], "CLAIMED")
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_REQUESTED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 1)
            connection.execute("DROP TRIGGER fail_pa_e3_terminal_release")
        dispatcher._set_state(
            submission_id, run_id, "BLOCKED",
            pa_e3_attempt_id=attempt, pa_e3_ack_stopped=True,
        )
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()[0], "BLOCKED")
            self.assertEqual(recovery.status(connection, run_id), "CANCEL_ACKNOWLEDGED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 0)

    def test_pa_e3_complete_and_resource_release_commit_together(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        submission_id = submissions["ACTION-A"]
        dispatcher = ParityLifecycleDispatcher(self.root)
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}):
            context, _candidate, run_id, _prompt, _duplicate = dispatcher._claim(submission_id)
        attempt = recovery.acquire(
            self.root, run_id=run_id, submission_id=submission_id,
            project_id=context.project_id, repository_id=context.repository_id,
        )
        recovery.runner_entry(self.root, run_id=run_id, attempt_id=attempt)
        with sqlite_connection(self.database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_pa_e3_resource_release BEFORE UPDATE ON ep_execution_leases "
                "WHEN OLD.lease_id LIKE 'pa-e2:resource:%' AND NEW.released_at IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'resource release blocked'); END"
            )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "resource release blocked"):
            dispatcher._set_state(submission_id, run_id, "COMPLETE", pa_e3_attempt_id=attempt)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()[0], "CLAIMED")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 1)
            connection.execute("DROP TRIGGER fail_pa_e3_resource_release")
        with patch.object(lifecycle_dispatcher, "log_event",
                          side_effect=RuntimeError("log unavailable")):
            dispatcher._set_state(submission_id, run_id, "COMPLETE", pa_e3_attempt_id=attempt)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()[0], "COMPLETE")
            self.assertNotEqual(recovery.status(connection, run_id),
                                "PROVIDER_EFFECT_UNCERTAIN")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 0)

    def test_pa_e3_dispatch_retains_uncertain_hold_for_unconfirmed_provider(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda root: _PaE3CheckpointRunner(
                root, self.root, cleanup_confirmed=False,
            ),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ):
            with self.assertRaisesRegex(RunnerError, "PA_E3_PROVIDER_EXIT_UNCONFIRMED"):
                dispatcher.dispatch(submissions["ACTION-A"])
        with sqlite_connection(self.database) as connection:
            run_id = str(connection.execute(
                "SELECT run_id FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (submissions["ACTION-A"],),
            ).fetchone()[0])
            self.assertEqual(recovery.status(connection, run_id),
                             "PROVIDER_EFFECT_UNCERTAIN")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_execution_leases WHERE run_id=? "
                "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL",
                (run_id,),
            ).fetchone()[0], 1)

    def test_pa_e3_dispatch_unexpected_runner_crash_stays_uncertain(self) -> None:
        _staged, submissions = self.pa_e3_submissions()
        dispatcher = ParityLifecycleDispatcher(
            self.root, runner_factory=lambda root: _PaE3CheckpointRunner(
                root, self.root, crash=True,
            ),
        )
        with patch.dict(os.environ, {"EP_PA_E2_ISOLATED_PROCESS": str(os.getpid())}), patch.object(
            dispatcher, "_persist_historical_input", return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "controlled dispatcher crash"):
                dispatcher.dispatch(submissions["ACTION-A"])
        with sqlite_connection(self.database) as connection:
            run_id = str(connection.execute(
                "SELECT run_id FROM ep_parity_lifecycle_dispatches WHERE submission_id=?",
                (submissions["ACTION-A"],),
            ).fetchone()[0])
            self.assertEqual(recovery.status(connection, run_id),
                             "PROVIDER_EFFECT_UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
