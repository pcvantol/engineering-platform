from __future__ import annotations

from engineering_platform.storage import sqlite_connection
from engineering_platform.storage import record_artifact
from engineering_platform.storage import (
    record_validation_profile, record_validation_command_invocation,
    record_validation_command_terminal, record_validation_control_result,
)
from engineering_platform.execution_executor import persist_validation_result_detail

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from engineering_platform import server, submission_service, merge_delegation
from engineering_platform.agent_state import TransactionState
from engineering_platform.platform_version import CURRENT_PLATFORM_VERSION


class CanonicalSubmissionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("djconnect", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("djconnect", "djconnect", "djconnect", "authority", "{}", now, now))
            connection.execute(
                "INSERT INTO ep_local_repository_bindings(project_id,repository_id,local_root,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("djconnect", "djconnect", str(self.root / "workspace"), "BOUND", now, now),
            )
            self.credential = submission_service.issue_consumer_credential(connection, consumer_id="cli", project_id="djconnect")["credential"]
            connection.execute("INSERT INTO ep_operator_capabilities VALUES(?,?,?,?,?)", ("cli", "djconnect", "QUEUE_HOLD_RESUME", now, None))
            connection.execute("INSERT INTO ep_operator_capabilities VALUES(?,?,?,?,?)", ("cli", "djconnect", "QUEUE_DECLINE", now, None))

    def tearDown(self) -> None:
        server.stop(self.root)
        self.temporary.cleanup()

    def test_validation_result_detail_distinguishes_zero_missing_and_corrupt_output(self) -> None:
        database = self.root / server.SERVER_DATABASE_FILENAME
        with sqlite_connection(database) as connection:
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) "
                "VALUES(?,?,?,?,?,?)",
                ("run-details", "djconnect", "RUNNING", "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00+00:00", "MANAGED"),
            )
            connection.execute(
                "INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,"
                "execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,"
                "input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,"
                "execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run-details", "2026-01-01", "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 0, 1,
                 "COMPLETE", None, None, None,
                 "MANAGED", "djconnect", "djconnect", "test"),
            )
        for command_id, summary, expected in (
            ("zero-tests", "Ran 0 tests in 0.01s\n\nOK\n", 0),
            ("two-tests", "Ran 2 tests in 0.01s\n\nOK\n", 2),
            ("unknown-tests", "completed successfully", None),
        ):
            persist_validation_result_detail(
                self.root, run_id="run-details", command_id=command_id,
                validation_id="repository_suite", exit_code=0, stdout="", stderr=summary,
                capture_available=True, captured_at="2026-01-01T00:00:01+00:00",
                central_database=database, artifact_root=self.root / "artifacts",
            )
            with sqlite_connection(database) as connection:
                detail = submission_service._validation_result_detail(
                    connection, data_root=self.root, run_id="run-details",
                    validation_id="repository_suite", command_id=command_id, exit_code=0,
                )
            self.assertEqual(detail["status"], "AVAILABLE")
            self.assertEqual(detail["test_count"], expected)
            self.assertRegex(detail["output_digest"], r"^sha256:[0-9a-f]{64}$")
        target = self.root / "artifacts" / "validation-result-details" / "validation-result-detail-two-tests.json"
        target.write_text("{}", encoding="utf-8")
        with sqlite_connection(database) as connection:
            self.assertEqual(submission_service._validation_result_detail(
                connection, data_root=self.root, run_id="run-details",
                validation_id="repository_suite", command_id="two-tests", exit_code=0,
            ), {"status": "UNAVAILABLE"})
            self.assertEqual(submission_service._validation_result_detail(
                connection, data_root=self.root, run_id="other-run",
                validation_id="repository_suite", command_id="zero-tests", exit_code=0,
            ), {"status": "UNAVAILABLE"})

    def test_control_definition_digest_changes_when_argv_changes_under_same_identity(self) -> None:
        binding = {"validation_id": "repository_suite", "category": "repository",
                   "control_identity": "python3 -m unittest discover -s tests",
                   "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}
        expected = submission_service._control_definition_digest(
            profile_version="1.0", profile_reference="validation-profile-registry:FULL@1.0",
            binding=binding,
        )
        changed = {**binding, "command": [sys.executable, "-m", "unittest", "discover", "-s", "other-tests"]}
        self.assertNotEqual(expected, submission_service._control_definition_digest(
            profile_version="1.0", profile_reference="validation-profile-registry:FULL@1.0",
            binding=changed,
        ))
        logical_definition = {
            "validation_profile_version": "1.0",
            "profile_reference": "validation-profile-registry:FULL@1.0",
            "validation_id": "repository_suite", "category": "repository",
            "control_identity": "python3 -m unittest discover -s tests",
            "command_identity": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
        }
        self.assertEqual(expected, "sha256:" + hashlib.sha256(json.dumps(
            logical_definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest())

    def payload(self, key: str = "same") -> dict[str, object]:
        return {"repository_id": "djconnect", "producer": {"id": "test", "type": "HUMAN", "version": "1"}, "prompt": "Validate only; do not execute.", "idempotency_key": key}

    def forge_payload(self, key: str = "forge-receipt") -> dict[str, object]:
        payload = self.payload(key)
        payload.update({
            "producer": {"id": "forge", "type": "FORGE", "version": "2.7.2"},
            "correlation_id": "forge-correlation-" + key,
            "mission_id": "mission-" + key,
            "engineering_action_id": "action-" + key,
            "constraints": {"forge_execution": {
                "contract_version": "1.1", "host_id": "engineering-platform",
                "repository_id": "djconnect", "correlation_id": "forge-correlation-" + key,
                "mission_id": "mission-" + key, "mission_revision": "1",
                "intent_id": "intent-" + key, "intent_revision": "1", "action_id": "action-" + key,
                "runtime_prompt": {"id": "prompt-" + key, "content_digest": "sha256:" + "a" * 64},
                "retry_of_correlation_id": None, "producer_contract_version": "1.0",
                "forge_application_version": "2.7.2",
            }},
        })
        return payload

    def forge_action_context_payload(self, key: str = "forge-action-context") -> dict[str, object]:
        payload = self.forge_payload(key)
        provenance = payload["constraints"]["forge_execution"]  # type: ignore[index]
        provenance["contract_version"] = "1.2"  # type: ignore[index]
        summary = "Rotate api_key=[REDACTED] before deployment."
        summary_digest = submission_service._action_context_digest(summary)
        envelope = {
            "envelope_version": "1.0",
            "action_id": "action-" + key,
            "summary": summary,
            "summary_digest": summary_digest,
            "generator": {
                "id": "forge-redacted-action-summary",
                "model": "deterministic-template",
                "version": "1.0",
                "source_digest": "sha256:" + "b" * 64,
            },
        }
        envelope["envelope_digest"] = submission_service._action_context_digest(envelope)
        provenance["action_context_envelope"] = envelope  # type: ignore[index]
        return payload

    def forge_planning_context_payload(self, key: str = "forge-planning-context") -> dict[str, object]:
        payload = self.forge_action_context_payload(key)
        provenance = payload["constraints"]["forge_execution"]  # type: ignore[index]
        provenance["contract_version"] = "1.3"  # type: ignore[index]
        envelope = {
            "envelope_version": "1.0", "mission_id": "mission-" + key,
            "mission_revision": "1", "intent_id": "intent-" + key, "intent_revision": "1",
            "action_id": "action-" + key, "mission_title": "Bounded fixture mission",
            "business_summary": "Deliver the verified fixture outcome.",
            "engineering_summary": "Exercise the versioned Forge boundary.",
            "mission_lifecycle": "ACTIVE", "decision_evidence_reference": "architecture-review:fixture",
            "decision_evidence_reference_digest": submission_service._action_context_digest("architecture-review:fixture"),
        }
        envelope["envelope_digest"] = submission_service._action_context_digest(envelope)
        provenance["planning_context_envelope"] = envelope  # type: ignore[index]
        return payload

    def test_forge_action_context_is_validated_persisted_immutable_and_never_backfilled(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            legacy = submission_service.submit(
                connection,
                submission_service.request_from_mapping("djconnect", self.forge_payload("historical"), transport="HTTP"),
            )
            current_payload = self.forge_action_context_payload()
            request = submission_service.request_from_mapping("djconnect", current_payload, transport="HTTP")
            accepted = submission_service.submit(connection, request)
            self.assertTrue(submission_service.submit(connection, request).duplicate)
            self.assertEqual(
                connection.execute(
                    "SELECT action_id,envelope_version,generator_id,generator_model,generator_version,"
                    "source_digest,summary_digest,envelope_digest,document FROM ep_forge_action_context_envelopes "
                    "WHERE submission_id=?",
                    (accepted.submission_id,),
                ).fetchone()[:-1],
                (
                    "action-forge-action-context", "1.0", "forge-redacted-action-summary",
                    "deterministic-template", "1.0", "sha256:" + "b" * 64,
                    submission_service._action_context_digest("Rotate api_key=[REDACTED] before deployment."),
                    submission_service._action_context_digest({
                        "envelope_version": "1.0", "action_id": "action-forge-action-context",
                        "summary": "Rotate api_key=[REDACTED] before deployment.",
                        "summary_digest": submission_service._action_context_digest(
                            "Rotate api_key=[REDACTED] before deployment."
                        ),
                        "generator": {
                            "id": "forge-redacted-action-summary", "model": "deterministic-template",
                            "version": "1.0", "source_digest": "sha256:" + "b" * 64,
                        },
                    }),
                ),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_forge_action_context_envelopes WHERE submission_id=?", (legacy.submission_id,)).fetchone(),
                (0,),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE ep_forge_action_context_envelopes SET document='{}' WHERE submission_id=?",
                    (accepted.submission_id,),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM ep_forge_action_context_envelopes WHERE submission_id=?",
                    (accepted.submission_id,),
                )
        malformed = self.forge_action_context_payload("malformed")
        malformed["constraints"]["forge_execution"]["action_context_envelope"]["summary"] = "api_key=raw-secret"  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_FORGE_ACTION_CONTEXT"):
            submission_service.request_from_mapping("djconnect", malformed, transport="HTTP")

    def test_forge_v13_planning_context_is_bound_immutable_and_never_backfilled(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            legacy = submission_service.submit(
                connection,
                submission_service.request_from_mapping("djconnect", self.forge_action_context_payload("v12"), transport="HTTP"),
            )
            payload = self.forge_planning_context_payload()
            accepted = submission_service.submit(
                connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"),
            )
            stored = connection.execute(
                "SELECT mission_id,action_id,envelope_version,envelope_digest,decision_evidence_reference_digest,document "
                "FROM ep_forge_planning_context_envelopes WHERE submission_id=?", (accepted.submission_id,),
            ).fetchone()
            envelope = payload["constraints"]["forge_execution"]["planning_context_envelope"]  # type: ignore[index]
            self.assertEqual(stored[:-1], (
                "mission-forge-planning-context", "action-forge-planning-context", "1.0",
                envelope["envelope_digest"], envelope["decision_evidence_reference_digest"],
            ))
            self.assertEqual(json.loads(stored[-1]), envelope)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_forge_planning_context_envelopes WHERE submission_id=?", (legacy.submission_id,)).fetchone(),
                (0,),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE ep_forge_planning_context_envelopes SET document='{}' WHERE submission_id=?", (accepted.submission_id,),
                )
        malformed = self.forge_planning_context_payload("malformed-planning")
        malformed["constraints"]["forge_execution"]["planning_context_envelope"]["mission_title"] = "secret=plain"  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_FORGE_PLANNING_CONTEXT"):
            submission_service.request_from_mapping("djconnect", malformed, transport="HTTP")

    def test_verified_managed_noop_uses_its_explicit_run_bound_revision(self) -> None:
        revision = "a" * 40
        verified = TransactionState(
            "verified-managed-noop", "djconnect", "prompt", "COMPLETE", terminal=True,
            action_intent="MUTATING_DELIVERY", transaction_kind="IMPLEMENTATION",
            terminal_condition="repository_reconciled", last_verified_sha=revision,
            commit_evidence=({
                "phase": "EXECUTE_AGENT", "observed_at": "2026-01-01T00:00:00+00:00",
                "commit_sha": revision, "description": "managed_noop_repository_reconciled",
            },),
        )
        unproven = TransactionState(
            "unproven-managed-noop", "djconnect", "prompt", "COMPLETE", terminal=True,
            action_intent="MUTATING_DELIVERY", transaction_kind="IMPLEMENTATION",
            terminal_condition="repository_reconciled", last_verified_sha=revision,
        )

        self.assertEqual(submission_service._repository_revision(verified, "COMPLETE"), (revision, True))
        self.assertEqual(submission_service._repository_revision(unproven, "COMPLETE"), (None, False))

    def test_blocked_checkpoint_is_terminal_evidence_not_delivery_evidence(self) -> None:
        blocked = TransactionState(
            "blocked-checkpoint", "djconnect", "prompt", "BLOCKED", terminal=True,
            action_intent="MUTATING_DELIVERY", transaction_kind="IMPLEMENTATION",
            implementation_merge_commit="a" * 40,
            commit_evidence=({
                "phase": "EXECUTE_AGENT", "observed_at": "2026-01-01T00:00:00+00:00",
                "commit_sha": "a" * 40, "description": "implementation_merge_verified",
            },),
        )
        self.assertEqual(submission_service._repository_revision(blocked, "BLOCKED"), (None, False))

    def test_repository_revision_binding_requires_full_explicit_revisions(self) -> None:
        payload = self.payload("revision-binding")
        payload["constraints"] = {"repository_revision_binding": {
            "requested_revision": "a" * 40,
            "allowed_baseline_revision": "b" * 40,
        }}
        request = submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        self.assertEqual(
            request.constraints and request.constraints["repository_revision_binding"],
            {"requested_revision": "a" * 40, "allowed_baseline_revision": "b" * 40},
        )
        invalid = self.payload("bad-revision-binding")
        invalid["constraints"] = {"repository_revision_binding": {
            "requested_revision": "a" * 7,
            "allowed_baseline_revision": None,
        }}
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_REPOSITORY_REVISION_BINDING"):
            submission_service.request_from_mapping("djconnect", invalid, transport="HTTP")

        direct = submission_service.SubmissionRequest(
            project_id="djconnect", repository_id="djconnect", producer_id="test",
            producer_type="HUMAN", producer_version="1", prompt="direct admission",
            transport="HTTP", constraints=invalid["constraints"],
        )
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_REPOSITORY_REVISION_BINDING"):
                submission_service.submit(connection, direct)

    def test_retained_v13_terminal_artifact_is_recognized_without_rewrite(self) -> None:
        target = self.root / "artifacts" / "projects" / "djconnect" / "runs" / "run-old" / "terminal-evidence-v1.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({
            "artifact_type": "EP_TERMINAL_EVIDENCE", "contract_version": "1.3",
            "submission": {"id": "sub-old", "project_id": "djconnect"},
            "run": {"id": "run-old"}, "repository": {"id": "djconnect"},
        }), encoding="utf-8")
        self.assertTrue(submission_service._is_retained_v13_terminal_artifact(
            target, run_id="run-old", submission_id="sub-old", project_id="djconnect",
            repository_id="djconnect",
        ))
        self.assertFalse(submission_service._is_retained_v13_terminal_artifact(
            target, run_id="another-run", submission_id="sub-old", project_id="djconnect",
            repository_id="djconnect",
        ))

    def test_terminal_timing_is_utc_ordered_and_durable(self) -> None:
        self.assertEqual(
            submission_service._terminal_timing(
                "2026-01-01T01:00:00+01:00", "2026-01-01T00:00:01+00:00",
            ),
            {
                "execution_started_at": "2026-01-01T00:00:00+00:00",
                "execution_completed_at": "2026-01-01T00:00:01+00:00",
                "execution_duration_ms": 1000,
            },
        )
        self.assertIsNone(submission_service._terminal_timing("now", "later"))
        self.assertIsNone(submission_service._terminal_timing(
            "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:01+00:00",
        ))

    def test_versioned_forge_submission_receipt_is_durable_and_idempotent(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            request = submission_service.request_from_mapping(
                "djconnect", self.forge_payload(), transport="HTTP",
            )
            accepted = submission_service.submit(connection, request)
            self.assertIsNotNone(accepted.receipt)
            self.assertEqual(accepted.to_dict()["receipt"], accepted.receipt)
            self.assertEqual(accepted.receipt["forge_application_version"], "2.7.2")  # type: ignore[index]
            self.assertEqual(accepted.receipt["producer_contract_version"], "1.0")  # type: ignore[index]
            replay = submission_service.submit(connection, request)
            self.assertTrue(replay.duplicate)
            self.assertEqual(replay.receipt, accepted.receipt)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_forge_exchange_audit").fetchone()[0], 1,
            )
            internal_retry = submission_service.submit(
                connection,
                submission_service.request_from_mapping("djconnect", self.forge_payload("internal"), transport="HTTP"),
                audit_forge_exchange=False,
            )
            self.assertIsNone(internal_retry.receipt)
            self.assertNotIn("receipt", internal_retry.to_dict())

    def test_versioned_forge_submission_rejects_mismatched_application_provenance(self) -> None:
        payload = self.forge_payload("invalid-provenance")
        payload["constraints"]["forge_execution"]["forge_application_version"] = "2.7.1"  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_FORGE_PROVENANCE"):
            submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
    def test_delivery_validation_constraint_is_bound_to_accepted_v13_envelope(self) -> None:
        payload = self.forge_planning_context_payload("delivery-controls")
        provenance = payload["constraints"]["forge_execution"]  # type: ignore[index]
        provenance["execution_constraints"] = ["ep-delivery-control-validation:1"]  # type: ignore[index]
        request = submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        self.assertEqual(request.constraints["forge_execution"]["execution_constraints"], ["ep-delivery-control-validation:1"])
        provenance["execution_constraints"] = ["ep-delivery-control-validation:1",  # type: ignore[index]
                                               "ep-delivery-unittest:tests.test_cli.ValidCase.test_valid"]
        request = submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        self.assertEqual(len(request.constraints["forge_execution"]["execution_constraints"]), 2)
        provenance["execution_constraints"] = ["ep-delivery-control-validation:1",  # type: ignore[index]
                                               "ep-delivery-unittest:tests.test_cli;rm"]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_DELIVERY_OBSERVATION_CONTROLS"):
            submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        provenance["execution_constraints"] = ["ep-delivery-unittest:tests.test_cli.ValidCase.test_valid"]  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_DELIVERY_OBSERVATION_CONTROLS"):
            submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        provenance["execution_constraints"] = ["ep-delivery-control-validation:1", "ep-delivery-control-validation:1"]  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_FORGE_PROVENANCE"):
            submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        provenance["execution_constraints"] = "ep-delivery-control-validation:1"  # type: ignore[index]
        with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_FORGE_PROVENANCE"):
            submission_service.request_from_mapping("djconnect", payload, transport="HTTP")

    def test_merge_reservation_requires_activation_and_revocation_stops_new_submissions(self) -> None:
        delegation_id = "a" * 32
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        workspace = self.root / "workspace"
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                        "https://github.com/pcvantol/djconnect.git"], check=True)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            reserved = merge_delegation.reserve(
                connection, delegation_id=delegation_id, actor_reference="local-uid:501",
                project_id="djconnect", repository_id="djconnect",
                github_repository="pcvantol/djconnect", base_branch="main",
                roles=("IMPLEMENTATION", "FINALIZATION", "RECONCILIATION"), expires_at=expires,
            )
            self.assertFalse(reserved.permits(
                project_id="djconnect", repository_id="djconnect",
                mission_id="mission-merge-reservation", mission_revision="1",
                role="IMPLEMENTATION", base_branch="main",
            ))
            payload = self.forge_planning_context_payload("merge-reservation")
            payload["constraints"]["forge_execution"]["execution_constraints"] = [  # type: ignore[index]
                f"ep-merge-delegation:{delegation_id}",
            ]
            request = submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
            with self.assertRaisesRegex(submission_service.SubmissionError, "MERGE_DELEGATION_UNAVAILABLE"):
                submission_service.submit(connection, request)
            active = merge_delegation.activate(
                connection, delegation_id=delegation_id,
                mission_id="mission-merge-reservation", mission_revision="1",
                actor_reference="local-uid:501", github_repository="pcvantol/djconnect",
            )
            self.assertTrue(active.permits(
                project_id="djconnect", repository_id="djconnect",
                mission_id="mission-merge-reservation", mission_revision="1",
                role="IMPLEMENTATION", base_branch="main",
            ))
            submission_service.submit(connection, request)
            self.assertTrue(merge_delegation.revoke(connection, delegation_id, actor_reference="local-uid:501"))
            self.assertFalse(merge_delegation.load(connection, delegation_id).permits(
                project_id="djconnect", repository_id="djconnect",
                mission_id="mission-merge-reservation", mission_revision="1",
                role="IMPLEMENTATION", base_branch="main",
            ))
            payload = self.forge_planning_context_payload("merge-after-revoke")
            payload["constraints"]["forge_execution"]["execution_constraints"] = [  # type: ignore[index]
                f"ep-merge-delegation:{delegation_id}",
            ]
            with self.assertRaisesRegex(submission_service.SubmissionError, "MERGE_DELEGATION_UNAVAILABLE"):
                submission_service.submit(
                    connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"),
                )

    def test_authenticated_merge_grant_readback_reports_scope_and_lifecycle(self) -> None:
        delegation_id = "b" * 32
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        workspace = self.root / "workspace"
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                        "https://github.com/pcvantol/djconnect.git"], check=True)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            merge_delegation.reserve(
                connection, delegation_id=delegation_id, actor_reference="local-uid:501",
                project_id="djconnect", repository_id="djconnect",
                github_repository="pcvantol/djconnect", base_branch="main",
                roles=("IMPLEMENTATION",), expires_at=expires,
            )
        server.start(self.root)
        endpoint = f"http://127.0.0.1:{self.port}/v1/projects/djconnect/merge-delegations/{delegation_id}"
        def readback() -> dict[str, object]:
            with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
                return json.loads(response.read())
        with self.assertRaises(HTTPError) as unauthorized:
            urlopen(endpoint)  # nosec B310
        self.assertEqual(unauthorized.exception.code, 401)
        reserved = readback()
        self.assertEqual(reserved["contract_version"], "1.1")
        self.assertEqual((reserved["assurance_profile_id"], reserved["assurance_profile_revision"]), ("", ""))
        self.assertEqual(reserved["assurance_policy_digest"], "")
        self.assertEqual(reserved["status"], "RESERVED")
        self.assertEqual(reserved["github_repository"], "pcvantol/djconnect")
        self.assertEqual(reserved["mission_id"], "")
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            merge_delegation.activate(
                connection, delegation_id=delegation_id,
                mission_id="mission-readback", mission_revision="7",
                actor_reference="local-uid:501", github_repository="pcvantol/djconnect",
            )
        active = readback()
        self.assertEqual(active["status"], "ACTIVE")
        self.assertEqual((active["mission_id"], active["mission_revision"]), ("mission-readback", "7"))
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            merge_delegation.revoke(connection, delegation_id, actor_reference="local-uid:501")
        self.assertEqual(readback()["status"], "REVOKED")

    def test_authenticated_profiled_grant_readback_binds_owner_policy_digest(self) -> None:
        delegation_id = "c" * 32
        digest = "sha256:" + "a" * 64
        workspace = self.root / "workspace"
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                        "https://github.com/pcvantol/forge-mission-qualification.git"], check=True)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            merge_delegation.reserve(
                connection, delegation_id=delegation_id, actor_reference="local-uid:501",
                project_id="djconnect", repository_id="djconnect",
                github_repository="pcvantol/forge-mission-qualification", base_branch="main",
                roles=("IMPLEMENTATION",),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                assurance_profile=merge_delegation.AUTONOMOUS_ASSURANCE_PROFILE,
                assurance_policy_digest=digest,
            )
        server.start(self.root)
        endpoint = f"http://127.0.0.1:{self.port}/v1/projects/djconnect/merge-delegations/{delegation_id}"
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            payload = json.loads(response.read())
        self.assertEqual(payload["contract_version"], "1.1")
        self.assertEqual((payload["assurance_profile_id"], payload["assurance_profile_revision"],
                          payload["assurance_policy_digest"]),
                         ("qualification-autonomous-qs", "1", digest))
        self.assertEqual(payload["status"], "RESERVED")

    def test_final_reconciliation_merge_is_the_delivery_revision(self) -> None:
        final = "d" * 40
        state = TransactionState(
            "reconciled-delivery", "pcvantol/djconnect", "prompt.md", "COMPLETE",
            finalization_merge_commit="c" * 40, reconciliation_merge_commit=final,
            commit_evidence=({"commit_sha": final},), terminal=True,
        )
        self.assertEqual(submission_service._repository_revision(state, "COMPLETE"), (final, True))

    def test_service_preserves_cross_transport_idempotency_and_history(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            http = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload(), transport="HTTP"))
            cli = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload(), transport="CLI"))
            path = self.root / "inbox.json"
            path.write_text(json.dumps({"project_id": "djconnect", "submission": self.payload()}), encoding="utf-8")
            legacy = submission_service.submit_legacy_file(connection, path)
            self.assertFalse(http.duplicate)
            self.assertTrue(cli.duplicate)
            self.assertTrue(legacy.duplicate)
            self.assertEqual(http.submission_id, cli.submission_id)
            self.assertEqual(connection.execute("SELECT count(*) FROM ep_submission_prompt_history").fetchone()[0], 1)

    def test_operator_dispositions_are_audited_and_only_resumable_from_hold_states(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload("operator"), transport="HTTP"))
            held = submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="QUARANTINED", reason="Needs operator review",
            )
            self.assertEqual(held["state"], "QUARANTINED")
            readback = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submitted.submission_id)
            self.assertEqual(readback["disposition"], {"state": "QUARANTINED", "terminal": False, "execution_eligible": False, "revision": 1, "operation_id": None, "event_reference": None, "reason": "NOT_RECORDED", "actor_reference": "NOT_RECORDED", "recorded_at": None, "resolution_submission_id": None, "retry_parent_run_id": None})
            self.assertEqual(
                submission_service.producer_readback(connection, project_id="djconnect", submission_id=submitted.submission_id)["submission"]["state"],
                "QUARANTINED",
            )
            self.assertEqual(submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="DECLINED", reason="The operator rejected the quarantined submission",
            )["state"], "DECLINED")
            declined_readback = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submitted.submission_id)
            self.assertIsNone(declined_readback["run"])
            self.assertEqual(declined_readback["result"]["outcome"], "NOT_STARTED")
            self.assertTrue(declined_readback["disposition"]["terminal"])
            events = [row[0] for row in connection.execute("SELECT event_kind FROM ep_submission_events WHERE submission_id=? ORDER BY event_id", (submitted.submission_id,))]
            self.assertEqual(events[-2:], ["OPERATOR_QUEUE_QUARANTINED", "OPERATOR_QUEUE_DECLINED"])
            self.assertNotIn("OPERATOR_QUEUE_QUEUED", events[-2:])
            submitted = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload("resume"), transport="HTTP"))
            held = submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="QUARANTINED", reason="Needs operator review",
            )
            resumed = submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="QUEUED", reason="Review completed",
            )
            self.assertEqual(resumed["state"], "QUEUED")
            events = [row[0] for row in connection.execute("SELECT event_kind FROM ep_submission_events WHERE submission_id=? ORDER BY event_id", (submitted.submission_id,))]
            self.assertEqual(events[-2:], ["OPERATOR_QUEUE_QUARANTINED", "OPERATOR_QUEUE_QUEUED"])

            deferred = submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="DEFERRED", reason="Schedule later",
            )
            self.assertEqual(deferred["state"], "DEFERRED")
            self.assertEqual(submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="QUEUED", reason="Schedule resumed",
            )["state"], "QUEUED")
            self.assertEqual(submission_service.operator_queue_disposition(
                connection, project_id="djconnect", submission_id=submitted.submission_id,
                disposition="DECLINED", reason="No longer wanted",
            )["state"], "DECLINED")
            with self.assertRaisesRegex(submission_service.SubmissionError, "QUEUE_DISPOSITION_CONFLICT"):
                submission_service.operator_queue_disposition(
                    connection, project_id="djconnect", submission_id=submitted.submission_id,
                    disposition="QUEUED", reason="Must not revive a decline",
                )

    def test_claimed_submission_rejects_queue_mutation_without_audit_event(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload("claimed"), transport="HTTP"))
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", ("run-claimed", "djconnect", "CLAIMED", "now", "now", "MANAGED"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)", (submitted.submission_id, "djconnect", "djconnect", "run-claimed", "CLAIMED", "prompt", "now", "now", "NONE"))
            before = connection.execute("SELECT state,disposition_revision FROM ep_submissions WHERE submission_id=?", (submitted.submission_id,)).fetchone()
            with self.assertRaisesRegex(submission_service.SubmissionError, "QUEUE_DISPOSITION_CONFLICT"):
                submission_service.operator_queue_disposition(connection, project_id="djconnect", submission_id=submitted.submission_id, disposition="QUARANTINED", reason="Too late")
            self.assertEqual(connection.execute("SELECT state,disposition_revision FROM ep_submissions WHERE submission_id=?", (submitted.submission_id,)).fetchone(), before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submission_events WHERE submission_id=? AND event_kind LIKE 'OPERATOR_QUEUE_%'", (submitted.submission_id,)).fetchone()[0], 0)

    def test_queue_operation_id_replays_once_and_rejects_payload_collision(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload("operation"), transport="HTTP"))
            first = submission_service.operator_queue_disposition(connection, project_id="djconnect", submission_id=submitted.submission_id, disposition="QUARANTINED", reason="Investigate source", expected_state="QUEUED", expected_revision=0, operation_id="queue-operation-1", actor_reference="operator-a")
            replay = submission_service.operator_queue_disposition(connection, project_id="djconnect", submission_id=submitted.submission_id, disposition="QUARANTINED", reason="Investigate source", expected_state="QUEUED", expected_revision=0, operation_id="queue-operation-1", actor_reference="operator-a")
            self.assertEqual((first["state"], replay["state"]), ("QUARANTINED", "QUARANTINED"))
            self.assertTrue(replay["replayed"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submission_events WHERE submission_id=? AND event_kind='OPERATOR_QUEUE_QUARANTINED'", (submitted.submission_id,)).fetchone()[0], 1)
            with self.assertRaisesRegex(submission_service.SubmissionError, "OPERATION_ID_CONFLICT"):
                submission_service.operator_queue_disposition(connection, project_id="djconnect", submission_id=submitted.submission_id, disposition="DECLINED", reason="Different command", expected_state="QUARANTINED", expected_revision=1, operation_id="queue-operation-1", actor_reference="operator-a")

    def test_queue_disposition_rejects_non_string_or_control_reason_without_mutation(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", self.payload("invalid-reason"), transport="HTTP"))
            for reason in (None, {}, [], True, "\x00bad", "\n"):
                with self.assertRaisesRegex(submission_service.SubmissionError, "INVALID_QUEUE_DISPOSITION"):
                    submission_service.operator_queue_disposition(connection, project_id="djconnect", submission_id=submitted.submission_id, disposition="QUARANTINED", reason=reason)  # type: ignore[arg-type]
            self.assertEqual(connection.execute("SELECT state,disposition_revision FROM ep_submissions WHERE submission_id=?", (submitted.submission_id,)).fetchone(), ("QUEUED", 0))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submission_events WHERE submission_id=? AND event_kind LIKE 'OPERATOR_QUEUE_%'", (submitted.submission_id,)).fetchone()[0], 0)

    def test_http_auth_scope_and_acceptance(self) -> None:
        server.start(self.root)
        request = Request(f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions", data=json.dumps(self.payload("http")).encode(), headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:  # nosec B310
            accepted = json.loads(response.read())
        self.assertEqual(accepted["state"], "QUEUED")
        wrong = Request(f"http://127.0.0.1:{self.port}/v1/projects/other/submissions", data=b"{}", headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as rejected:
            urlopen(wrong)  # nosec B310
        self.assertEqual(rejected.exception.code, 401)

    def test_http_readback_accepts_only_supported_reconciled_terminal_artifact_ids(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        run_id = "run-reconciled-route"
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(
                connection,
                submission_service.request_from_mapping(
                    "djconnect", self.forge_payload("reconciled-route"), transport="HTTP",
                ),
            )
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)",
                (run_id, "djconnect", "COMPLETE", now, now, "MANAGED"),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)",
                (submitted.submission_id, "djconnect", "djconnect", run_id, "COMPLETE", "prompt", now, now, "NONE"),
            )
        artifact_id = f"terminal-evidence-reconciled:{run_id}:0123456789abcdef"
        artifact_path = (
            self.root / "artifacts" / "projects" / "djconnect" / "runs" /
            run_id / "terminal-evidence-v1-reconciled-0123456789abcdef.json"
        )
        artifact_path.parent.mkdir(parents=True)
        artifact_bytes = b'{"artifact_type":"EP_TERMINAL_EVIDENCE","contract_version":"1.4"}\n'
        artifact_path.write_bytes(artifact_bytes)
        record_artifact(
            self.root,
            artifact_path,
            artifact_id=artifact_id,
            artifact_type="EP_TERMINAL_EVIDENCE",
            content_type="application/json",
            created_at=now,
            producer_id="forge",
            ep_run_id=run_id,
            ep_submission_id=submitted.submission_id,
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
            artifact_root=self.root / "artifacts",
        )
        server.start(self.root)
        base = f"http://127.0.0.1:{self.port}/v1/projects/djconnect/artifacts/"
        headers = {"Authorization": f"Bearer {self.credential}"}
        with urlopen(Request(base + quote(artifact_id, safe=""), headers=headers)) as response:  # nosec B310
            self.assertEqual(response.read(), artifact_bytes)

        with self.assertRaises(HTTPError) as unsupported:
            urlopen(Request(  # nosec B310
                base + quote(f"terminal-evidence-migrated:{run_id}:0123456789abcdef", safe=""),
                headers=headers,
            ))
        self.assertEqual(unsupported.exception.code, 404)

        with self.assertRaises(HTTPError) as unauthenticated:
            urlopen(base + quote(artifact_id, safe=""))  # nosec B310
        self.assertEqual(unauthenticated.exception.code, 401)

    def test_console_queue_actions_change_only_the_selected_submission_and_are_audited(self) -> None:
        """Exercise the browser-facing queue action endpoint against CENTRAL."""
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(
                connection, submission_service.request_from_mapping("djconnect", self.payload("console-actions"), transport="HTTP"),
            )
        server.start(self.root)
        endpoint = f"http://127.0.0.1:{self.port}/api/queue-disposition?project=djconnect"

        current_state, current_revision = "QUEUED", 0
        def action(disposition: str, reason: str) -> dict[str, object]:
            nonlocal current_state, current_revision
            revision, expected_state = current_revision, current_state
            body = json.dumps({"contract_version": "1.0", "operation_id": f"operation-{disposition}-{revision}", "submission_id": submitted.submission_id, "expected_state": expected_state, "expected_revision": revision, "disposition": disposition, "reason": reason}).encode()
            request = Request(endpoint, data=body, method="POST", headers={
                "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", "Authorization": f"Bearer {self.credential}",
            })
            with urlopen(request) as response:  # nosec B310
                result = json.loads(response.read())
            current_state, current_revision = result["state"], result["resulting_revision"]
            return result

        self.assertEqual(action("DEFERRED", "Wait for the maintenance window")["state"], "DEFERRED")
        self.assertEqual(action("QUEUED", "Maintenance window is open")["state"], "QUEUED")
        # Quarantine is a separate operator hold and must be independently resumable.
        self.assertEqual(action("QUARANTINED", "Investigate the source envelope")["state"], "QUARANTINED")
        self.assertEqual(action("QUEUED", "Investigation completed")["state"], "QUEUED")
        self.assertEqual(action("DECLINED", "The request is no longer needed")["state"], "DECLINED")
        body = json.dumps({"contract_version": "1.0", "operation_id": "must-not-revive", "submission_id": submitted.submission_id, "expected_state": "DECLINED", "expected_revision": current_revision, "disposition": "QUEUED", "reason": "Must not revive a declined request"}).encode()
        with self.assertRaises(HTTPError) as rejected:
            urlopen(Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", "Authorization": f"Bearer {self.credential}"}))  # nosec B310
        self.assertEqual(rejected.exception.code, 409)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            events = [row[0] for row in connection.execute(
                "SELECT event_kind FROM ep_submission_events WHERE submission_id=? ORDER BY event_id", (submitted.submission_id,)
            )]
        self.assertEqual(events[-5:], [
            "OPERATOR_QUEUE_DEFERRED", "OPERATOR_QUEUE_QUEUED",
            "OPERATOR_QUEUE_QUARANTINED", "OPERATOR_QUEUE_QUEUED", "OPERATOR_QUEUE_DECLINED",
        ])

    def test_console_queue_actions_reject_cross_origin_and_unknown_project(self) -> None:
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            submitted = submission_service.submit(
                connection, submission_service.request_from_mapping("djconnect", self.payload("console-denial"), transport="HTTP"),
            )
        server.start(self.root)
        body = json.dumps({"submission_id": submitted.submission_id, "disposition": "DEFERRED", "reason": "Later"}).encode()
        cases = (
            (f"http://127.0.0.1:{self.port}/api/queue-disposition?project=djconnect", "https://untrusted.example", 403),
            (f"http://127.0.0.1:{self.port}/api/queue-disposition?project=other", f"http://127.0.0.1:{self.port}", 409),
        )
        for endpoint, origin, expected in cases:
            request = Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json", "Origin": origin})
            with self.assertRaises(HTTPError) as rejected:
                urlopen(request)  # nosec B310
            self.assertEqual(rejected.exception.code, expected)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT state FROM ep_submissions WHERE submission_id=?", (submitted.submission_id,)).fetchone()[0], "QUEUED")

    def test_authenticated_producer_readback_is_exactly_correlated_and_terminal_evidence_backed(self) -> None:
        server.start(self.root)
        with urlopen(f"http://127.0.0.1:{self.port}/v1/producer-compatibility") as response:  # nosec B310
            compatibility = json.loads(response.read())
        self.assertEqual(compatibility["contracts"], {"producer_readback": ["1.2"], "terminal_evidence": ["1.4"]})
        self.assertEqual(compatibility["producer"]["id"], "engineering-platform")
        scoped = Request(
            f"http://127.0.0.1:{self.port}/v1/producer-compatibility",
            headers={
                "Authorization": f"Bearer {self.credential}",
                "EP-Project-ID": "djconnect",
                "EP-Repository-ID": "djconnect",
            },
        )
        with urlopen(scoped) as response:  # nosec B310
            authenticated = json.loads(response.read())
        self.assertEqual(authenticated["contract_version"], "1.1")
        self.assertEqual(authenticated["contracts"]["validation_controls"], ["1.0", "1.1"])
        self.assertEqual(authenticated["contracts"]["delivery_revision_validation"], ["1.0"])
        self.assertEqual(authenticated["authentication"], {
            "consumer_id": "cli", "consumer_status": "ACTIVE",
            "project_id": "djconnect", "project_status": "ACTIVE",
            "repository_id": "djconnect", "repository_role": "authority",
            "local_repository_binding": "BOUND",
            "submission_authorization": "AUTHORIZED",
        })
        # The consumer identity is derived from the credential.  A caller
        # cannot turn an expected Forge identity into apparent EP evidence:
        # a second, valid credential in the same project is reported as its
        # own consumer even though the requested project/repository coincide.
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            other_same_scope = submission_service.issue_consumer_credential(
                connection, consumer_id="other-same-project", project_id="djconnect",
            )["credential"]
        with urlopen(Request(
            f"http://127.0.0.1:{self.port}/v1/producer-compatibility",
            headers={
                "Authorization": f"Bearer {other_same_scope}",
                "EP-Project-ID": "djconnect",
                "EP-Repository-ID": "djconnect",
            },
        )) as response:  # nosec B310
            other_authenticated = json.loads(response.read())
        self.assertEqual(
            other_authenticated["authentication"]["consumer_id"],
            "other-same-project",
        )
        payload = self.payload("readback")
        payload.update({"producer": {"id": "forge", "type": "FORGE", "version": "2.7.2"},
                        "correlation_id": "forge-correlation-1", "mission_id": "mission-1",
                        "engineering_action_id": "action-1", "constraints": {"forge_execution": {
                            "contract_version": "1.1", "host_id": "engineering-platform",
                            "repository_id": "djconnect", "correlation_id": "forge-correlation-1",
                            "mission_id": "mission-1", "mission_revision": "1", "intent_id": "intent-1",
                            "intent_revision": "1", "action_id": "action-1",
                            "runtime_prompt": {"id": "prompt-1", "content_digest": "sha256:" + "a" * 64},
                            "retry_of_correlation_id": None,
                            "producer_contract_version": "1.0",
                            "forge_application_version": "2.7.2",
                        }}})
        submit = Request(
            f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.credential}", "Content-Type": "application/json"}, method="POST",
        )
        with urlopen(submit) as response:  # nosec B310
            accepted = json.loads(response.read())
        submission_id = accepted["submission_id"]
        receipt = accepted["receipt"]
        self.assertEqual(receipt, {
            "contract_version": "1.0", "id": "ep-submission-receipt:" + submission_id,
            "event": "FORGE_SUBMISSION_ACCEPTED", "issued_at": accepted["created_at"],
            "submission_id": submission_id, "ep_instance_id": receipt["ep_instance_id"],
            "ep_application_version": CURRENT_PLATFORM_VERSION, "producer_contract_version": "1.0",
            "forge_provenance_contract_version": "1.1", "forge_application_version": "2.7.2",
            "producer_readback_contract_version": "1.2",
            "accepted_request_digest": receipt["accepted_request_digest"],
        })
        self.assertRegex(receipt["accepted_request_digest"], r"^sha256:[0-9a-f]{64}$")
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            audit = connection.execute(
                "SELECT direction,event_kind,producer_contract_version,forge_provenance_contract_version,forge_application_version,ep_application_version,receipt_id,accepted_request_digest FROM ep_forge_exchange_audit WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            self.assertEqual(audit, ("FORGE_TO_EP", "FORGE_SUBMISSION_ACCEPTED", "1.0", "1.1", "2.7.2", CURRENT_PLATFORM_VERSION, receipt["id"], receipt["accepted_request_digest"]))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM ep_forge_exchange_audit WHERE submission_id=?", (submission_id,))
            log = connection.execute(
                "SELECT payload FROM engineering_component_logs WHERE component='http_ingress' AND json_extract(payload, '$.event')='forge_submission_accepted' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(log)
            self.assertEqual(json.loads(log[0])["forge_application_version"], "2.7.2")
            self.assertEqual(json.loads(log[0])["receipt_id"], receipt["id"])
            self.assertEqual(json.loads(log[0])["ep_application_version"], CURRENT_PLATFORM_VERSION)
            receipt_log = connection.execute(
                "SELECT payload FROM engineering_component_logs WHERE component='http_ingress' AND json_extract(payload, '$.event')='forge_submission_receipt_issued' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(receipt_log)
            self.assertEqual(json.loads(receipt_log[0])["exchange_direction"], "EP_TO_FORGE")
        endpoint = f"http://127.0.0.1:{self.port}/v1/projects/djconnect/submissions/{submission_id}"
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            initial = json.loads(response.read())
        self.assertEqual(initial["contract_version"], "1.2")
        self.assertEqual(initial["correlation"], {
            "correlation_id": "forge-correlation-1", "mission_id": "mission-1", "engineering_action_id": "action-1",
        })
        self.assertIsNone(initial["run"])
        self.assertEqual(initial["result"], {"outcome": "NOT_STARTED", "terminal": False, "delivery_qualified": False})
        self.assertEqual(initial["provenance"]["status"], "PERSISTED")

        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            started = "2026-01-01T00:00:00+00:00"
            completed = "2026-01-01T00:00:01+00:00"
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", ("run-readback", "djconnect", "COMPLETE", started, completed, "MANAGED"))
            connection.execute("INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("run-readback", "2026-01-01", started, started, completed, 0, 1, "COMPLETE", None, None, None, "MANAGED", "djconnect", "djconnect", "test"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution) VALUES(?,?,?,?,?,?,?,?,?)", (submission_id, "djconnect", "djconnect", "run-readback", "COMPLETE", "/private/prompt", started, completed, "NONE"))
            profile = {"version": "validation-profile@1", "digest": "sha256:" + "b" * 64, "candidate_sha": "c" * 40}
            finding = {"id": "security-1", "fingerprint": "d" * 32, "category": "SECURITY", "criterion": "post_implementation_assurance", "observation": "Project isolation lacks a negative test.", "severity": "HIGH", "confidence": "MEDIUM", "blocking": True, "disposition": "OPEN"}
            reviews = (
                {"reviewer": "quality", "status": "PASS", "candidate_sha": "c" * 40, "profile_digest": profile["digest"], "invocation_id": "quality-1", "findings": []},
                {"reviewer": "security", "status": "FAIL", "candidate_sha": "c" * 40, "profile_digest": profile["digest"], "invocation_id": "security-1", "findings": [finding]},
            )
            checkpoint = TransactionState(
                run_id="run-readback", repository="djconnect", prompt_path="prompt",
                phase="COMPLETE", terminal=True, action_intent="VALIDATION_ONLY",
                requested_repository_revision="a" * 40,
                execution_baseline_sha="b" * 40,
                allowed_baseline_revision="b" * 40,
                implementation_head_sha="c" * 40,
                assurance_profile=profile, assurance_reviews=reviews, repair_iterations=2,
            )
            connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("run-readback", json.dumps(checkpoint.to_dict()), "COMPLETE", "now"))
            connection.execute("INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)", ("run-readback", "COMPLETE", "safe", "now", None, "/private/report", "now"))
        binding = {"validation_id": "repository_suite", "required": True,
                   "category": "repository", "control_identity": "python3 -m unittest discover -s tests",
                   "command": ["python3", "-m", "unittest", "discover", "-s", "tests"]}
        record_validation_profile(
            self.root, run_id="run-readback", selected_validation_tier="FULL",
            validation_profile_version="1.0", required_validation_controls=("repository_suite",),
            recorded_at="2026-01-01T00:00:00+00:00", control_bindings=(binding,),
            candidate_sha="c" * 40, currentness=2,
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
        )
        record_validation_command_invocation(
            self.root, run_id="run-readback", validation_id="repository_suite",
            command_id="required-control-fixture", category="repository",
            control_identity=binding["control_identity"], required_for_profile=True,
            started_at="2026-01-01T00:00:00+00:00", currentness=2,
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
        )
        record_validation_command_terminal(
            self.root, run_id="run-readback", command_id="required-control-fixture",
            completed_at="2026-01-01T00:00:01+00:00", exit_code=0,
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
        )
        record_validation_control_result(
            self.root, run_id="run-readback", validation_id="repository_suite",
            category="repository", control_identity=binding["control_identity"],
            required_for_profile=True, execution_status="EXECUTED", result="PASS",
            evidence_ref="command_terminal", observed_at="2026-01-01T00:00:01+00:00",
            currentness=2, central_database=self.root / server.SERVER_DATABASE_FILENAME,
        )
        record_validation_control_result(
            self.root, run_id="run-readback", validation_id="provider_observed_repository_suite",
            category="agent", control_identity="private optional provider command",
            required_for_profile=False, execution_status="EXECUTED", result="PASS",
            evidence_ref="provider_report", observed_at="2026-01-01T00:00:01+00:00",
            currentness=2, central_database=self.root / server.SERVER_DATABASE_FILENAME,
        )
        persist_validation_result_detail(
            self.root, run_id="run-readback", command_id="required-control-fixture",
            validation_id="repository_suite", exit_code=0,
            stdout="", stderr="Ran 2 tests in 0.01s\n\nOK\n", capture_available=True,
            captured_at="2026-01-01T00:00:01+00:00",
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
            artifact_root=self.root / "artifacts",
        )
        artifact_id = submission_service.write_terminal_evidence(self.root, repository_root=self.root, run_id="run-readback")
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            direct = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submission_id)
            self.assertIsNotNone(direct)
            stored_artifact = submission_service.producer_evidence_artifact(connection, project_id="djconnect", artifact_id=artifact_id)
            self.assertIsNotNone(stored_artifact)
            self.assertEqual(json.loads(stored_artifact or b"{}")['run']['id'], "run-readback")
            findings_artifact = submission_service.producer_evidence_artifact(connection, project_id="djconnect", artifact_id="assurance-findings:run-readback")
            self.assertEqual(json.loads(findings_artifact or b"{}")["reviews"], list(reviews))
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            terminal = json.loads(response.read())
        self.assertEqual(terminal["run"]["id"], "run-readback")
        self.assertEqual(terminal["run"]["execution_started_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(terminal["run"]["execution_completed_at"], "2026-01-01T00:00:01+00:00")
        self.assertEqual(terminal["run"]["execution_duration_ms"], 1000)
        self.assertEqual(terminal["result"], {"outcome": "COMPLETE", "terminal": True, "delivery_qualified": False})
        self.assertEqual(terminal["evidence"]["status"], "AVAILABLE")
        self.assertEqual(terminal["evidence"]["terminal_artifact"]["id"], artifact_id)
        self.assertEqual(terminal["evidence"]["repository"]["revision"], None)
        self.assertNotIn("/private", json.dumps(terminal))
        artifact_endpoint = (
            f"http://127.0.0.1:{self.port}/v1/projects/djconnect/artifacts/"
            f"{quote(artifact_id, safe='')}"
        )
        with urlopen(Request(artifact_endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            returned_artifact_bytes = response.read()
            artifact = json.loads(returned_artifact_bytes)
        self.assertEqual(returned_artifact_bytes, stored_artifact)
        self.assertEqual(artifact["submission"]["id"], submission_id)
        self.assertEqual(artifact["run"]["id"], "run-readback")
        self.assertEqual(artifact["run"]["execution_started_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(artifact["run"]["execution_completed_at"], "2026-01-01T00:00:01+00:00")
        self.assertEqual(artifact["run"]["execution_duration_ms"], 1000)
        self.assertEqual(artifact["repository"]["requested_revision"], "a" * 40)
        self.assertEqual(artifact["repository"]["execution_baseline"], "b" * 40)
        self.assertEqual(artifact["repository"]["candidate"], "c" * 40)
        self.assertEqual(artifact["repository"]["baseline_transition"], {
            "status": "ALLOWED", "from": "a" * 40, "to": "b" * 40,
            "allowed_to": "b" * 40,
        })
        self.assertEqual(artifact["delivery"], {"status": "NOT_DELIVERED", "revision": None})
        self.assertEqual(artifact["assurance"]["repair_rounds"], {"used": 2, "maximum": 3})
        self.assertEqual(artifact["assurance"]["findings"]["artifact"]["id"], "assurance-findings:run-readback")
        controls = artifact["validation_controls"]
        self.assertEqual(controls["contract_version"], "1.0")
        self.assertEqual(controls["candidate_sha"], "c" * 40)
        self.assertEqual(controls["required_validation_controls"], ["repository_suite"])
        control = controls["controls"]["repository_suite"]
        self.assertEqual(tuple(controls["controls"]), ("repository_suite",))
        self.assertNotIn("private optional provider command", returned_artifact_bytes.decode("utf-8"))
        self.assertEqual(control["evidence_authority"], "command_terminal")
        self.assertEqual(control["result_detail"]["test_count"], 2)
        logical_definition = {
            "validation_profile_version": "1.0",
            "profile_reference": "validation-profile-registry:FULL@1.0",
            "validation_id": "repository_suite", "category": "repository",
            "control_identity": binding["control_identity"],
            "command_identity": binding["command"],
        }
        expected_definition = "sha256:" + hashlib.sha256(json.dumps(
            logical_definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(control["control_definition_digest"], expected_definition)

        # A new writer must not replace a previously immutable v1.3 terminal
        # artifact merely because it can now emit a richer v1.4 shape.
        historical = dict(artifact)
        historical["contract_version"] = "1.3"
        historical["repository"] = {
            "id": "djconnect", "revision": None, "revision_required": False,
        }
        historical.pop("delivery")
        artifact_path = self.root / "artifacts" / "projects" / "djconnect" / "runs" / "run-readback" / "terminal-evidence-v1.json"
        historical_bytes = submission_service._canonical_json_bytes(historical)
        artifact_path.write_bytes(historical_bytes)
        with self.assertRaisesRegex(submission_service.SubmissionError, "TERMINAL_EVIDENCE_IMMUTABLE_CONFLICT"):
            submission_service.write_terminal_evidence(
                self.root, repository_root=self.root, run_id="run-readback",
            )
        record_artifact(
            self.root, artifact_path, artifact_id=artifact_id,
            artifact_type="EP_TERMINAL_EVIDENCE", content_type="application/json",
            created_at="2026-01-01T00:00:01+00:00", run_id="run-readback",
            submission_id=submission_id, mission_id="mission-1", producer_id="forge",
            central_database=self.root / server.SERVER_DATABASE_FILENAME,
            artifact_root=self.root / "artifacts", ep_run_id="run-readback",
            ep_submission_id=submission_id,
        )
        self.assertEqual(
            submission_service.write_terminal_evidence(
                self.root, repository_root=self.root, run_id="run-readback",
            ), artifact_id,
        )
        self.assertEqual(artifact_path.read_bytes(), historical_bytes)
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            terminal = json.loads(response.read())

        # The projection is CENTRAL state, not a process-local cache: a Server
        # restart preserves the exact submission/run/evidence correlation.
        server.stop(self.root)
        server.start(self.root)
        with urlopen(Request(endpoint, headers={"Authorization": f"Bearer {self.credential}"})) as response:  # nosec B310
            recovered = json.loads(response.read())
        self.assertEqual(recovered, terminal)

        with self.assertRaises(HTTPError) as unauthenticated:
            urlopen(endpoint)  # nosec B310
        self.assertEqual(unauthenticated.exception.code, 401)
        with self.assertRaises(HTTPError) as cross_project:
            urlopen(Request(
                f"http://127.0.0.1:{self.port}/v1/projects/other/submissions/{submission_id}",
                headers={"Authorization": f"Bearer {self.credential}"},
            ))  # nosec B310
        self.assertEqual(cross_project.exception.code, 401)
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("other", "{}", "ACTIVE", now, now))
            connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("other", "other", "other", "authority", "{}", now, now))
            other_credential = submission_service.issue_consumer_credential(connection, consumer_id="other-client", project_id="other")["credential"]
        with self.assertRaises(HTTPError) as isolated:
            urlopen(Request(
                f"http://127.0.0.1:{self.port}/v1/projects/other/submissions/{submission_id}",
                headers={"Authorization": f"Bearer {other_credential}"},
            ))  # nosec B310
        self.assertEqual(isolated.exception.code, 404)
        artifact_path.write_text("{}\n", encoding="utf-8")
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            corrupt = submission_service.producer_readback(connection, project_id="djconnect", submission_id=submission_id)
            self.assertEqual(corrupt["evidence"]["status"], "CORRUPT")  # type: ignore[index]
            self.assertFalse(corrupt["result"]["delivery_qualified"])  # type: ignore[index]
            self.assertIsNone(submission_service.producer_evidence_artifact(connection, project_id="djconnect", artifact_id=artifact_id))

    def test_terminal_assurance_requires_a_complete_current_pair_and_explicit_historical_resolution(self) -> None:
        """A missing/stale review cannot become PASS through empty aggregation."""
        candidate, digest = "c" * 40, "sha256:" + "b" * 64
        profile = {"version": "validation-profile@1", "digest": digest, "candidate_sha": candidate}
        quality = {"reviewer": "quality", "status": "PASS", "candidate_sha": candidate,
                   "profile_digest": digest, "invocation_id": "quality-current", "findings": []}
        security = {"reviewer": "security", "status": "PASS", "candidate_sha": candidate,
                    "profile_digest": digest, "invocation_id": "security-current", "findings": []}
        base = dict(run_id="assurance-current", repository="djconnect", prompt_path="prompt",
                    phase="COMPLETE", terminal=True, assurance_profile=profile)
        self.assertEqual(submission_service._current_assurance(TransactionState(**base))[0], "UNRESOLVED")
        self.assertEqual(submission_service._current_assurance(TransactionState(**base, assurance_reviews=(quality,)))[0], "UNRESOLVED")
        stale = {**security, "candidate_sha": "d" * 40}
        self.assertEqual(submission_service._current_assurance(TransactionState(**base, assurance_reviews=(quality, stale)))[0], "UNRESOLVED")

        old_finding = {"id": "old-security", "fingerprint": "e" * 32, "category": "SECURITY",
                       "criterion": "isolation", "observation": "Missing denial test", "severity": "HIGH",
                       "confidence": "HIGH", "blocking": True, "disposition": "OPEN"}
        old_security = {**security, "status": "FAIL", "invocation_id": "security-old", "findings": [old_finding]}
        unresolved = TransactionState(**base, assurance_reviews=(old_security, quality, security))
        self.assertEqual(submission_service._current_assurance(unresolved)[0], "FAIL")
        resolved = TransactionState(
            **base, assurance_reviews=(old_security, quality, security),
            assurance_resolutions=({"finding_id": "old-security", "disposition": "RESOLVED",
                                    "resolution_ref": "repair:assurance-current:1|quality-current,security-current",
                                    "candidate_sha": candidate},),
        )
        self.assertEqual(submission_service._current_assurance(resolved)[0], "PASS")

    def test_forge_provenance_is_required_and_part_of_idempotency_identity(self) -> None:
        payload = self.payload("forge-replay")
        payload.update({"producer": {"id": "forge", "type": "FORGE", "version": "1.0"},
                        "correlation_id": "corr-1", "mission_id": "mission-1", "engineering_action_id": "action-1",
                        "constraints": {"forge_execution": {"contract_version": "1.0", "host_id": "engineering-platform",
                            "repository_id": "djconnect", "correlation_id": "corr-1", "mission_id": "mission-1",
                            "mission_revision": "1", "intent_id": "intent-1", "intent_revision": "1", "action_id": "action-1",
                            "runtime_prompt": {"id": "prompt-1", "content_digest": "sha256:" + "b" * 64},
                            "retry_of_correlation_id": None}}})
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            first = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            replay = submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            self.assertTrue(replay.duplicate)
            payload["constraints"]["forge_execution"]["runtime_prompt"]["id"] = "prompt-2"  # type: ignore[index]
            with self.assertRaisesRegex(submission_service.SubmissionError, "IDEMPOTENCY_CONFLICT"):
                submission_service.submit(connection, submission_service.request_from_mapping("djconnect", payload, transport="HTTP"))
            self.assertEqual(first.submission_id, replay.submission_id)
            malformed = self.payload("missing-forge")
            malformed["producer"] = {"id": "forge", "type": "FORGE", "version": "1.0"}
            with self.assertRaisesRegex(submission_service.SubmissionError, "FORGE_PROVENANCE_REQUIRED"):
                submission_service.request_from_mapping("djconnect", malformed, transport="HTTP")

    def test_submission_rejects_malformed_and_unbound_inputs(self) -> None:
        with self.assertRaisesRegex(submission_service.SubmissionError, "MALFORMED_REQUEST"):
            submission_service.request_from_mapping("djconnect", [], transport="HTTP")
        for payload, code in (
            ({"unknown": True}, "UNKNOWN_FIELD"),
            ({"repository_id": "djconnect", "producer": {}, "prompt": "x"}, "INVALID_PRODUCER"),
            ({"repository_id": "djconnect", "producer": {"id": "x", "type": "HUMAN"}, "prompt": ""}, "INVALID_PROMPT"),
        ):
            with self.assertRaisesRegex(submission_service.SubmissionError, code):
                submission_service.request_from_mapping("djconnect", payload, transport="HTTP")
        with sqlite_connection(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            unknown_project = submission_service.SubmissionRequest("absent", "djconnect", "x", "HUMAN", None, "x", "HTTP")
            with self.assertRaisesRegex(submission_service.SubmissionError, "UNKNOWN_PROJECT"):
                submission_service.submit(connection, unknown_project)
            unknown_repository = submission_service.SubmissionRequest("djconnect", "absent", "x", "HUMAN", None, "x", "HTTP")
            with self.assertRaisesRegex(submission_service.SubmissionError, "UNKNOWN_REPOSITORY"):
                submission_service.submit(connection, unknown_repository)
