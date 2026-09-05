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
        self.assertEqual(row, ("alpha", "alpha", first.run_id, "COMPLETE"))
        self.assertTrue((self.data / "artifacts" / "projects" / "alpha" / "runs" / first.run_id / "submission.md").is_file())
        self.assertFalse((self.roots["alpha"] / ".engineering" / "engineering.db").exists())
        self.assertFalse((self.roots["alpha"] / ".engineering" / "engineering-runs").exists())

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
        schema = "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA"
        root = "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT"
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
