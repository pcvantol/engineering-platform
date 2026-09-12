from __future__ import annotations

from pathlib import Path
import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import local_repository_binding, parity_lifecycle_dispatcher, server, submission_service
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_timing import phase_spans
from engineering_platform.parity_lifecycle_dispatcher import (
    ParityLifecycleDispatchError,
    ParityLifecycleDispatcher,
    dismiss_operator_gate,
    retry_operator_gate,
)


class _PassingPreflight:
    timestamp = "2026-01-01T00:00:00+00:00"
    checks = ()


class _RejectedCheck:
    identifier = "host-policy"
    outcome = "FAIL"
    reason = "The configured executable is not admitted."


class _RejectedPreflight:
    timestamp = "2026-01-01T00:00:00+00:00"
    checks = (_RejectedCheck(),)


class _AcceptedCheck:
    identifier = "workspace-policy"
    outcome = "PASS"
    reason = "The configured workspace is admitted."


class _AcceptedPreflight:
    timestamp = "2026-01-01T00:00:00+00:00"
    checks = (_AcceptedCheck(),)


class _Runner:
    calls: list[tuple[Path, str | None, bool, bool]] = []

    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState:
        self.calls.append((prompt_path, run_id, resume, owner_authorized))
        return TransactionState(run_id or "inbox-missing", "fixture", str(prompt_path), "COMPLETE", terminal=True)


class _FailingRunner:
    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState:
        return TransactionState(run_id or "inbox-missing", "fixture", str(prompt_path), "FAILED", terminal=True)


class _CheckpointingRunner:
    """A deterministic preserved-runner seam for CENTRAL storage qualification."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState:
        state = TransactionState(
            run_id or "inbox-missing", "fixture", str(prompt_path), "COMPLETE", terminal=True,
        )
        StateStore(self.root / ".engineering" / "engineering-runs").save(state)
        return state


class ParityLifecycleDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name) / "central"
        server.initialize(self.data)
        self.roots: dict[str, Path] = {}
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            now = "2026-01-01T00:00:00+00:00"
            for project in ("alpha", "beta"):
                root = Path(self.temporary.name) / project
                (root / ".engineering-platform").mkdir(parents=True); self.roots[project] = root
                (root / ".engineering-platform" / "repository.json").write_text(json.dumps({
                    "schema_version": "1.0", "project": {"id": project, "authority_repository_id": project},
                    "repository": {"id": project, "role": "authority"},
                    "validation": {"kind": "command", "entrypoint": "python -m unittest"},
                    "requirements": {"host": {}, "tools": {}}, "integrations": {},
                }), encoding="utf-8")
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", (project, "{}", "ACTIVE", now, now))
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", (project, project, project, "authority", "{}", now, now))
                local_repository_binding.bind_local_repository(connection, project_id=project, repository_id=project, local_root=root, data_root=self.data)
        _Runner.calls = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dispatcher_import_does_not_load_retired_inbox_watcher_runtime(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src"
        environment = os.environ | {"PYTHONPATH": str(source_root)}
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; import engineering_platform.parity_lifecycle_dispatcher; "
                "print(json.dumps('engineering_platform.inbox_watcher' in sys.modules))",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertEqual(json.loads(completed.stdout), False)

    def _submission(self, project: str, prompt: str = "Validate only.") -> str:
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            return submission_service.submit(connection, submission_service.SubmissionRequest(
                project, project, "canary", "HUMAN", "1", prompt, "HTTP",
            )).submission_id

    def _forge_submission(self, project: str, correlation_id: str) -> str:
        constraints = {
            "forge_execution": {
                "contract_version": "1.0", "host_id": "host-alpha", "repository_id": project,
                "correlation_id": correlation_id, "mission_id": "MISSION-0006",
                "mission_revision": "5", "intent_id": "intent-0006", "intent_revision": "1",
                "action_id": "implement-and-test-durable-status-projection",
                "runtime_prompt": {"id": "prompt-0006", "content_digest": "sha256:" + "a" * 64},
                "retry_of_correlation_id": None,
            },
        }
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            return submission_service.submit(connection, submission_service.SubmissionRequest(
                project, project, "forge", "FORGE", "2.7.2", "Implement the bounded action.", "HTTP",
                correlation_id=correlation_id, mission_id="MISSION-0006",
                engineering_action_id="implement-and-test-durable-status-projection", constraints=constraints,
            )).submission_id

    def test_claims_one_submission_once_and_preserves_central_run_linkage(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            first = dispatcher.dispatch(submission)
            second = dispatcher.dispatch(submission)
        self.assertEqual(first.run_id, second.run_id)
        self.assertFalse(first.duplicate_claim)
        self.assertTrue(second.duplicate_claim)
        self.assertEqual(first.state, "COMPLETE")
        self.assertEqual(len(_Runner.calls), 2)
        self.assertFalse(_Runner.calls[0][2])
        self.assertTrue(_Runner.calls[1][2])
        self.assertTrue(_Runner.calls[0][3])
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute("SELECT project_id,repository_id,run_id,state FROM ep_parity_lifecycle_dispatches").fetchone()
            provenance = connection.execute("SELECT submission_id,run_id,project_id,repository_id,installation_id FROM ep_receipt_run_provenance").fetchone()
            installation = connection.execute("SELECT value FROM engineering_metadata WHERE key='installation.instance_id'").fetchone()
        self.assertEqual(row, ("alpha", "alpha", first.run_id, "COMPLETE"))
        self.assertEqual(provenance, (submission, first.run_id, "alpha", "alpha", installation[0]))
        self.assertTrue((self.data / "artifacts" / "projects" / "alpha" / "runs" / first.run_id / "submission.md").is_file())
        self.assertFalse((self.roots["alpha"] / ".engineering" / "engineering.db").exists())
        self.assertFalse((self.roots["alpha"] / ".engineering" / "engineering-runs").exists())

    def test_dispatch_writes_central_run_scoped_operational_decisions(self) -> None:
        """The central log records the operational lifecycle, not only service start-up."""
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            rows = connection.execute(
                "SELECT payload FROM engineering_component_logs "
                "WHERE component='lifecycle_worker' ORDER BY id"
            ).fetchall()
        entries = [json.loads(payload) for (payload,) in rows]
        entries = [entry for entry in entries if entry.get("run_id") == receipt.run_id]
        self.assertTrue({
            "lifecycle_submission_claimed",
            "lifecycle_run_state_changed",
            "lifecycle_input_materialized",
            "lifecycle_admission_decided",
            "lifecycle_runner_started",
            "lifecycle_runner_finished",
        } <= {entry["event"] for entry in entries})
        admitted = next(entry for entry in entries if entry["event"] == "lifecycle_admission_decided")
        self.assertEqual(admitted["admission_decision"], "PASS")
        self.assertEqual(admitted["submission_id"], submission)
        self.assertEqual(admitted["run_id"], receipt.run_id)

    def test_central_checkpoint_writes_its_lifecycle_phase(self) -> None:
        """Each retained-runner checkpoint is a run-scoped central log step."""
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=_CheckpointingRunner)
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            payloads = connection.execute(
                "SELECT payload FROM engineering_component_logs "
                "WHERE component='lifecycle_worker' ORDER BY id"
            ).fetchall()
        checkpoint = next(
            json.loads(payload)
            for (payload,) in payloads
            if json.loads(payload).get("run_id") == receipt.run_id
            and json.loads(payload).get("event") == "lifecycle_phase_checkpointed"
        )
        self.assertEqual(checkpoint["phase"], "COMPLETE")
        self.assertEqual(checkpoint["terminal_state"], "COMPLETE")

    def test_initialize_only_dispatch_materializes_input_on_normal_resume(self) -> None:
        """A visible pre-run dispatch remains resumable after qualification pauses it."""
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch.dict(os.environ, {"EP_QUALIFICATION_INITIALIZE_ONLY": "1"}, clear=False):
            initialized = dispatcher.dispatch(submission)
        prompt = self.data / "artifacts" / "projects" / "alpha" / "runs" / initialized.run_id / "submission.md"
        self.assertEqual(initialized.state, "RUNNING")
        self.assertFalse(prompt.exists())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            resumed = dispatcher.dispatch(submission)
        self.assertTrue(resumed.duplicate_claim)
        self.assertEqual(resumed.state, "COMPLETE")
        self.assertTrue(prompt.is_file())

    def test_context_never_crosses_project_binding(self) -> None:
        alpha, beta = self._submission("alpha"), self._submission("beta")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            alpha_receipt = dispatcher.dispatch(alpha)
            beta_receipt = dispatcher.dispatch(beta)
        self.assertNotEqual(alpha_receipt.run_id, beta_receipt.run_id)
        self.assertEqual(_Runner.calls[0][0].parents[2].resolve(), (self.data / "artifacts" / "projects" / "alpha").resolve())
        self.assertEqual(_Runner.calls[1][0].parents[2].resolve(), (self.data / "artifacts" / "projects" / "beta").resolve())

    def test_dispatcher_checkpoint_is_central_without_a_local_projection(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=_CheckpointingRunner)
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        root = self.roots["alpha"]
        self.assertFalse((root / ".engineering" / "engineering.db").exists())
        self.assertFalse((root / ".engineering" / "engineering-runs").exists())
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute(
                "SELECT phase FROM engineering_transactions WHERE run_id=?", (receipt.run_id,)
            ).fetchone()
        self.assertEqual(row, ("COMPLETE",))

    def test_explicit_central_store_never_derives_authority_from_checkout(self) -> None:
        root = self.roots["alpha"]
        store = StateStore(
            root / ".engineering" / "engineering-runs",
            central_database=(self.data / server.SERVER_DATABASE_FILENAME).resolve(),
            emit_local_projection=False,
        )
        state = TransactionState("central-checkpoint", "fixture", "central", "COMPLETE", terminal=True)
        store.save(state)
        self.assertEqual(store.load(state.run_id).phase, "COMPLETE")
        self.assertEqual(store.run_ids(), (state.run_id,))
        self.assertFalse((root / ".engineering").exists())

    def test_failed_run_blocks_later_project_submission_until_central_operator_resolution(self) -> None:
        first, later = self._submission("alpha"), self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(first)
            self.assertEqual(receipt.state, "FAILED")
            with self.assertRaisesRegex(ParityLifecycleDispatchError, "PROJECT_RUN_ALREADY_ACTIVE"):
                dispatcher._claim(later)
            dismiss_operator_gate(self.data, project_id="alpha", run_id=receipt.run_id)
            context, candidate, run_id, _prompt, duplicate = dispatcher._claim(later)
        self.assertEqual(context.project_id, "alpha")
        self.assertEqual(candidate.submission_id, later)
        self.assertFalse(duplicate)
        self.assertNotEqual(run_id, receipt.run_id)

    def test_project_scoped_operator_action_rejects_a_foreign_run(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "PROJECT_RUN_NOT_AWAITING_OPERATOR"):
            dismiss_operator_gate(self.data, project_id="beta", run_id=receipt.run_id)

    def test_retry_resolves_failed_gate_and_prioritizes_its_central_successor(self) -> None:
        first, later = self._submission("alpha"), self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(first)
        retry = retry_operator_gate(self.data, project_id="alpha", run_id=receipt.run_id)
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "PROJECT_RUN_ALREADY_ACTIVE"):
            dispatcher._claim(later)
        context, candidate, _run_id, _prompt, duplicate = dispatcher._claim(retry.submission_id)
        self.assertEqual(context.project_id, "alpha")
        self.assertEqual(candidate.submission_id, retry.submission_id)
        self.assertIn(f"Retry-Of: {receipt.run_id}", candidate.prompt)
        self.assertFalse(duplicate)

    def test_dismissed_retry_descendant_releases_the_full_fifo_chain(self) -> None:
        """A dismissed terminal retry cannot leave an older retry as a ghost blocker."""
        parent, retry, later = self._submission("alpha"), self._submission("alpha"), self._submission("alpha")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("parent-run", "alpha", "FAILED", "now", "now"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,
                    operator_resolution,resolution_submission_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (parent, "alpha", "alpha", "parent-run", "FAILED", "prompt", "now", "now", "RETRIED", retry),
            )
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("retry-run", "alpha", "FAILED", "now", "now"),
            )
            connection.execute(
                """INSERT INTO ep_parity_lifecycle_dispatches(
                    submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,
                    operator_resolution
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (retry, "alpha", "alpha", "retry-run", "FAILED", "prompt", "now", "now", "DISMISSED"),
            )
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        context, candidate, _run_id, _prompt, duplicate = dispatcher._claim(later)
        self.assertEqual(context.project_id, "alpha")
        self.assertEqual(candidate.submission_id, later)
        self.assertFalse(duplicate)

    def test_forge_retry_reuses_its_correlation_through_explicit_attempt_lineage(self) -> None:
        """A blocked Forge run can retry without changing its producer identity."""
        original = self._forge_submission("alpha", "forge-runtime-correlation-0006")
        failing = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            first = failing.dispatch(original)
        retry = retry_operator_gate(self.data, project_id="alpha", run_id=first.run_id)
        succeeding = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            second = succeeding.dispatch(retry.submission_id)
        self.assertEqual(second.state, "COMPLETE")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            attempts = connection.execute(
                "SELECT submission_id,canonical_submission_id,retry_parent_submission_id "
                "FROM execution_submission_attempts ORDER BY recorded_at,submission_id"
            ).fetchall()
            links = connection.execute(
                "SELECT submission_id,run_id FROM execution_submission_attempt_links ORDER BY submission_id"
            ).fetchall()
            lineage = connection.execute(
                "SELECT submission_id,fresh_submission,retry_parent_run_id "
                "FROM execution_run_qualification_context WHERE run_id=?", (second.run_id,)
            ).fetchone()
        self.assertIn((original, original, None), attempts)
        self.assertIn((retry.submission_id, original, original), attempts)
        self.assertIn((original, first.run_id), links)
        self.assertIn((retry.submission_id, second.run_id), links)
        self.assertEqual(lineage, (retry.submission_id, 0, first.run_id))
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            parent_readback = submission_service.producer_readback(
                connection, project_id="alpha", submission_id=original,
            )
            retry_readback = submission_service.producer_readback(
                connection, project_id="alpha", submission_id=retry.submission_id,
            )
        self.assertEqual(parent_readback["disposition"]["resolution_submission_id"], retry.submission_id)
        self.assertEqual(parent_readback["disposition"]["retry_parent_run_id"], None)
        self.assertEqual(retry_readback["disposition"]["retry_parent_run_id"], first.run_id)
        self.assertEqual(retry_readback["run"]["id"], second.run_id)
        database = self.data / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(database) as connection:
            submitted_at, claimed_at = connection.execute(
                "SELECT submission.created_at,dispatch.claimed_at FROM ep_submissions AS submission "
                "JOIN ep_parity_lifecycle_dispatches AS dispatch ON dispatch.submission_id=submission.submission_id "
                "WHERE dispatch.run_id=?", (second.run_id,),
            ).fetchone()
        queue_wait = phase_spans(self.roots["alpha"], second.run_id, central_database=database)[0]
        self.assertEqual(queue_wait["phase_name"], "QUEUE_WAIT")
        self.assertEqual(queue_wait["started_at"], submitted_at)
        self.assertEqual(queue_wait["completed_at"], claimed_at)

    def test_uncorrelated_retry_reuses_its_parent_submission_lineage(self) -> None:
        """Retained non-Forge retries remain executable after the lineage repair."""
        original = self._submission("alpha")
        failing = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            first = failing.dispatch(original)
        retry = retry_operator_gate(self.data, project_id="alpha", run_id=first.run_id)
        succeeding = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            second = succeeding.dispatch(retry.submission_id)
        self.assertEqual(second.state, "COMPLETE")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            attempt = connection.execute(
                "SELECT canonical_submission_id,retry_parent_submission_id "
                "FROM execution_submission_attempts WHERE submission_id=?", (retry.submission_id,)
            ).fetchone()
        self.assertEqual(attempt, (original, original))

    def test_historical_retry_without_an_attempt_uses_its_persisted_ancestor(self) -> None:
        """A pre-lineage retry failure does not strand its later successor."""
        original = self._forge_submission("alpha", "forge-runtime-correlation-ancestor")
        failing = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            first = failing.dispatch(original)
        historical_retry = retry_operator_gate(self.data, project_id="alpha", run_id=first.run_id)
        # Match an old retry that was claimed but failed before record_submission
        # could persist an execution_submission_attempts row.
        failed_context, _candidate, failed_run, _prompt, duplicate = failing._claim(historical_retry.submission_id)
        self.assertEqual(failed_context.project_id, "alpha")
        self.assertFalse(duplicate)
        failing._set_state(historical_retry.submission_id, failed_run, "BLOCKED")
        successor = retry_operator_gate(self.data, project_id="alpha", run_id=failed_run)
        succeeding = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            completed = succeeding.dispatch(successor.submission_id)
        self.assertEqual(completed.state, "COMPLETE")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            attempt = connection.execute(
                "SELECT canonical_submission_id,retry_parent_submission_id "
                "FROM execution_submission_attempts WHERE submission_id=?", (successor.submission_id,),
            ).fetchone()
            lineage = connection.execute(
                "SELECT fresh_submission,retry_parent_run_id FROM execution_run_qualification_context WHERE run_id=?",
                (completed.run_id,),
            ).fetchone()
        self.assertEqual(attempt, (original, original))
        self.assertEqual(lineage, (0, failed_run))

    def test_genesis_mode_is_forwarded_to_the_preserved_host_input(self) -> None:
        submission = self._submission("alpha", "Execution Mode: Genesis\nTarget repository: /tmp/target\n")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        self.assertEqual(receipt.state, "COMPLETE")
        self.assertIn("Execution Mode: Genesis", _Runner.calls[0][0].read_text(encoding="utf-8"))
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(
                connection.execute("SELECT execution_mode FROM ep_execution_runs WHERE run_id=?", (receipt.run_id,)).fetchone(),
                ("GENESIS",),
            )

    def test_terminal_dispatch_projects_the_preserved_console_history(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.generate_terminal_report") as report, \
             patch("engineering_platform.parity_lifecycle_dispatcher.record_terminal_report") as record, \
             patch("engineering_platform.parity_lifecycle_dispatcher.analyze_terminal_report") as analyze:
            report.return_value = self.data / "artifacts" / "projects" / "alpha" / "runs" / "terminal.md"
            report.return_value.parent.mkdir(parents=True, exist_ok=True)
            report.return_value.write_text("terminal", encoding="utf-8")
            receipt = dispatcher.dispatch(submission)
        self.assertEqual(receipt.state, "COMPLETE")
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], self.roots["alpha"].resolve())
        self.assertEqual(
            record.call_args.kwargs["central_database"],
            (self.data / server.SERVER_DATABASE_FILENAME).resolve(),
        )
        analyze.assert_called_once_with(self.roots["alpha"].resolve(), receipt.run_id, report.return_value)

    def test_terminal_history_reconciliation_ignores_a_retained_row_without_a_local_checkpoint(self) -> None:
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("historical-missing-checkpoint", "alpha", "COMPLETE", "now", "now"),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("historical-submission", "alpha", "alpha", "historical-missing-checkpoint", "COMPLETE", "prompt", "now", "now"),
            )

        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        dispatcher.reconcile_terminal_history()

    def test_unknown_submission_and_invalid_state_are_rejected_without_writes(self) -> None:
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "UNKNOWN_SUBMISSION"):
            dispatcher._claim("not-a-central-submission")
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "INVALID_DISPATCH_STATE"):
            dispatcher._set_state("not-a-central-submission", "not-a-run", "RETRYING")

    def test_retry_rejects_a_corrupt_original_constraint_envelope(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("UPDATE ep_submissions SET constraints=? WHERE submission_id=?", ("[]", submission))
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "RETRY_CONSTRAINTS_INVALID"):
            retry_operator_gate(self.data, project_id="alpha", run_id=receipt.run_id)
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            resolution = connection.execute(
                "SELECT operator_resolution FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (receipt.run_id,)
            ).fetchone()
        self.assertEqual(resolution, ("OPEN",))

    def test_rejected_admission_is_recorded_and_blocks_before_provider_execution(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_RejectedPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            with self.assertRaisesRegex(ParityLifecycleDispatchError, "HISTORICAL_ADMISSION_BLOCKED\\|_RejectedPreflight\\|host-policy"):
                dispatcher.dispatch(submission)
        self.assertEqual(_Runner.calls, [])
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            state = connection.execute(
                "SELECT state,operator_resolution FROM ep_parity_lifecycle_dispatches WHERE submission_id=?", (submission,)
            ).fetchone()
        self.assertEqual(state, ("BLOCKED", "OPEN"))

    def test_resolved_gate_cannot_be_retried_a_second_time(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _FailingRunner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        dismiss_operator_gate(self.data, project_id="alpha", run_id=receipt.run_id)
        with self.assertRaisesRegex(ParityLifecycleDispatchError, "PROJECT_RUN_NOT_AWAITING_OPERATOR"):
            retry_operator_gate(self.data, project_id="alpha", run_id=receipt.run_id)

    def test_historical_admission_environment_restores_preexisting_and_absent_values(self) -> None:
        schema = "ENGINEERING_PLATFORM_ADMITTED_STORAGE_SCHEMA"
        root = "ENGINEERING_PLATFORM_ADMITTED_STORAGE_ROOT"
        central = parity_lifecycle_dispatcher.CENTRAL_OPERATIONAL_DATABASE_ENVIRONMENT
        with patch.dict(os.environ, {schema: "old-schema", central: "old-central"}, clear=False):
            os.environ.pop(root, None)
            with parity_lifecycle_dispatcher._historical_admission_environment(self.roots["alpha"], self.data):
                self.assertEqual(os.environ[schema], str(parity_lifecycle_dispatcher.ENGINEERING_STORAGE_SCHEMA_VERSION))
                self.assertEqual(os.environ[root], str(self.roots["alpha"]))
            self.assertEqual(os.environ[schema], "old-schema")
            self.assertEqual(os.environ[central], "old-central")
            self.assertNotIn(root, os.environ)

    def test_passing_preflight_evidence_is_persisted_without_creating_a_failure_gate(self) -> None:
        submission = self._submission("alpha")
        dispatcher = ParityLifecycleDispatcher(self.data, runner_factory=lambda root: _Runner())
        with patch("engineering_platform.parity_lifecycle_dispatcher.execute_host_preflight", return_value=_AcceptedPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_workspace_preflight", return_value=_PassingPreflight()), \
             patch("engineering_platform.parity_lifecycle_dispatcher.execute_capability_preflight", return_value=_PassingPreflight()):
            receipt = dispatcher.dispatch(submission)
        self.assertEqual(receipt.state, "COMPLETE")
        with sqlite3.connect(self.data / server.SERVER_DATABASE_FILENAME) as connection:
            evidence = connection.execute(
                "SELECT decision,failed_gate_ids FROM execution_admission_decisions WHERE run_id=?", (receipt.run_id,)
            ).fetchone()
        self.assertEqual(evidence, ("PASS", '{"gate_ids":[]}'))
