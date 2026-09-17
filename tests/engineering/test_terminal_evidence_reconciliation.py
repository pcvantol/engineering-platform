from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from engineering_platform import server, submission_service
from engineering_platform.agent_state import TransactionState
from engineering_platform.storage import sqlite_connection
from engineering_platform.terminal_evidence_reconciliation import (
    TerminalEvidenceReconciliationError,
    reconcile_terminal_evidence,
)


class TerminalEvidenceReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        server.initialize(self.root)
        self.run_id = "run-terminal-projection"
        self.baseline = "a" * 40
        self.candidate = "b" * 40
        self.delivery = "c" * 40
        now = "2026-01-01T00:00:00+00:00"
        completed = "2026-01-01T00:00:01+00:00"
        profile = {
            "version": "validation-profile@1",
            "digest": "sha256:" + "d" * 64,
            "candidate_sha": self.candidate,
        }
        reviews = tuple({
            "reviewer": role,
            "status": "PASS",
            "candidate_sha": self.candidate,
            "profile_digest": profile["digest"],
            "invocation_id": role + "-1",
            "findings": [],
        } for role in ("quality", "security"))
        checkpoint = TransactionState(
            self.run_id, "repo", "prompt", "COMPLETE", terminal=True,
            action_intent="MUTATING_DELIVERY", transaction_kind="RECONCILIATION",
            requested_repository_revision="9" * 40,
            execution_baseline_sha=self.baseline,
            allowed_baseline_revision=self.baseline,
            # Reproduce the retired merge writer: it projected the prior
            # checkout instead of retaining the assurance-bound candidate.
            implementation_head_sha=self.baseline,
            implementation_merge_commit="8" * 40,
            finalization_merge_commit=self.delivery,
            assurance_profile=profile,
            assurance_reviews=reviews,
            commit_evidence=({
                "phase": "WAIT_FOR_OPERATOR_MERGE", "observed_at": completed,
                "commit_sha": self.delivery, "description": "finalization_merge_verified",
            },),
        )
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute(
                "INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                ("project", "{}", "ACTIVE", now, now),
            )
            connection.execute(
                "INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)",
                ("repo", "project", "repo", "authority", "{}", now, now),
            )
            accepted = submission_service.submit(
                connection,
                submission_service.request_from_mapping("project", {
                    "repository_id": "repo",
                    "producer": {"id": "forge", "type": "FORGE", "version": "2.7.21"},
                    "prompt": "Bound terminal projection fixture.",
                    "correlation_id": "correlation-terminal-projection",
                    "mission_id": "mission-terminal-projection",
                    "engineering_action_id": "action-terminal-projection",
                    "constraints": {"forge_execution": {
                        "contract_version": "1.1",
                        "host_id": "engineering-platform",
                        "repository_id": "repo",
                        "correlation_id": "correlation-terminal-projection",
                        "mission_id": "mission-terminal-projection",
                        "mission_revision": "1",
                        "intent_id": "intent-terminal-projection",
                        "intent_revision": "1",
                        "action_id": "action-terminal-projection",
                        "runtime_prompt": {
                            "id": "prompt-terminal-projection",
                            "content_digest": "sha256:" + "f" * 64,
                        },
                        "retry_of_correlation_id": None,
                        "producer_contract_version": "1.0",
                        "forge_application_version": "2.7.21",
                    }},
                }, transport="HTTP"),
            )
            self.submission_id = accepted.submission_id
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)",
                (self.run_id, "project", "COMPLETE", now, completed, "MANAGED"),
            )
            connection.execute(
                "INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, "2026-01-01", now, now, completed, 0, 1, "COMPLETE", None, None, None, "MANAGED", "project", "repo", "test"),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)",
                (self.submission_id, "project", "repo", self.run_id, "COMPLETE", "/private/prompt", now, completed, "NONE"),
            )
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                (self.run_id, json.dumps(checkpoint.to_dict()), "COMPLETE", completed),
            )
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, "COMPLETE", "safe", completed, None, "/private/report", completed),
            )
        self.source_id = submission_service.write_terminal_evidence(
            self.root, repository_root=self.root, run_id=self.run_id,
        )
        self.source_path = (
            self.root / "artifacts" / "projects" / "project" / "runs" /
            self.run_id / "terminal-evidence-v1.json"
        )
        self.source_bytes = self.source_path.read_bytes()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reconciliation_preserves_source_and_selects_corrected_projection(self) -> None:
        operation = reconcile_terminal_evidence(
            self.root, run_id=self.run_id,
            operation_id="terminal-evidence-repair-001",
        )
        replay = reconcile_terminal_evidence(
            self.root, run_id=self.run_id,
            operation_id="terminal-evidence-repair-001",
        )

        self.assertEqual(operation, replay)
        self.assertEqual(operation["state"], "SUCCEEDED")
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            statuses = dict(connection.execute(
                "SELECT artifact_id,projection_status FROM execution_artifact_records WHERE ep_run_id=? AND artifact_type='EP_TERMINAL_EVIDENCE'",
                (self.run_id,),
            ))
            readback = submission_service.producer_readback(
                connection, project_id="project", submission_id=self.submission_id,
            )
            replacement = submission_service.producer_evidence_artifact(
                connection, project_id="project",
                artifact_id=str(operation["replacement_artifact_id"]),
            )
        self.assertEqual(statuses[self.source_id], "SUPERSEDED")
        self.assertEqual(statuses[str(operation["replacement_artifact_id"])], "AVAILABLE")
        self.assertEqual(
            readback["evidence"]["terminal_artifact"]["id"],
            operation["replacement_artifact_id"],
        )
        self.assertEqual(json.loads(replacement or b"{}")['repository']['candidate'], self.candidate)

    def test_second_operation_and_nonmatching_projection_are_refused(self) -> None:
        reconcile_terminal_evidence(
            self.root, run_id=self.run_id,
            operation_id="terminal-evidence-repair-001",
        )
        with self.assertRaisesRegex(
            TerminalEvidenceReconciliationError, "RUN_ALREADY_RECONCILED",
        ):
            reconcile_terminal_evidence(
                self.root, run_id=self.run_id,
                operation_id="terminal-evidence-repair-002",
            )

    def test_immutable_operation_receipt_rejects_update_and_delete(self) -> None:
        reconcile_terminal_evidence(
            self.root, run_id=self.run_id,
            operation_id="terminal-evidence-repair-001",
        )
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute(
                    "UPDATE ep_terminal_evidence_reconciliation_operations SET reason_code=reason_code"
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute("DELETE FROM ep_terminal_evidence_reconciliation_operations")


if __name__ == "__main__":
    unittest.main()
