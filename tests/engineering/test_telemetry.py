from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from tools.engineering.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage
from tools.engineering.telemetry import (
    ExecutionTelemetry,
    clear_telemetry,
    daily_statistics,
    daily_timing_detail,
    execution_timing,
    comparable_duration_estimate,
    persist_execution,
    persist_execution_async,
    prune_telemetry,
    queue_terminal_telemetry,
    materialize_pending_terminal_telemetry,
    recover_missing_terminal_telemetry,
    recover_terminal_telemetry,
    wait_for_pending_telemetry,
)
from tools.engineering.producer import ProducerMetadata
from tools.engineering.execution_timing import record_phase


class ExecutionHostTelemetryTest(unittest.TestCase):
    def _record(self, run_id: str, state: str, started: datetime) -> ExecutionTelemetry:
        return ExecutionTelemetry(
            run_id=run_id,
            arrived_at=started - timedelta(seconds=12),
            execution_started_at=started,
            execution_finished_at=started + timedelta(seconds=90),
            terminal_state=state,
            execution_seconds=75.0,
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
            execution_mode="MANAGED",
            workspace="djconnect",
            repository="pcvantol/djconnect",
            execution_host_version="2.0.0",
            prompt_characters=1_000,
            runtime_provider="codex_cli",
            runtime_model="gpt-5.6-terra",
            reasoning_profile="medium",
            configuration_profile="workspace-write",
        )

    def test_persists_generic_execution_runs_and_daily_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
            persist_execution(root, self._record("run-complete", "COMPLETE", started))
            persist_execution(root, self._record("run-blocked", "BLOCKED", started + timedelta(hours=1)))

            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0], 2)
            self.assertEqual(
                daily_statistics(root),
                [
                    {
                        "date": "2026-08-01",
                        "prompt_count": 2,
                        "complete_count": 1,
                        "blocked_count": 1,
                        "failed_count": 0,
                        "average_execution_seconds": 75.0,
                        "average_total_execution_seconds": 102.0,
                        "average_queue_wait_seconds": 12.0,
                        "input_tokens": 240,
                        "output_tokens": 60,
                        "total_tokens": 300,
                        "average_provider_execution_seconds": None,
                        "average_validation_seconds": None,
                    }
                ],
            )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT total_execution_seconds FROM execution_runs WHERE run_id = 'run-complete'"
                    ).fetchone()[0],
                    102.0,
                )
            self.assertEqual(
                execution_timing(root, "run-complete"),
                {
                    "execution_seconds": 75.0,
                    "total_execution_seconds": 102.0,
                    "finished_at": "2026-08-01T10:01:30+00:00",
                },
            )

    def test_durable_terminal_outbox_is_idempotent_across_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = self._record("run-durable", "COMPLETE", datetime(2026, 8, 25, 10, tzinfo=timezone.utc))
            # Establish storage, then simulate a process loss after the intent
            # commit and before projection materialization.
            persist_execution(root, self._record("run-bootstrap", "COMPLETE", datetime(2026, 8, 24, 10, tzinfo=timezone.utc)))
            self.assertTrue(queue_terminal_telemetry(root, telemetry))
            self.assertFalse(queue_terminal_telemetry(root, telemetry))
            self.assertEqual(materialize_pending_terminal_telemetry(root), {"processed": 1, "failed": 0, "pending": 1})
            self.assertEqual(materialize_pending_terminal_telemetry(root), {"processed": 0, "failed": 0, "pending": 0})
            with open_storage(root) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_runs WHERE run_id='run-durable'").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT state,source FROM terminal_telemetry_outbox WHERE run_id='run-durable'").fetchone(), ("PROCESSED", "LIVE_TERMINAL"))
            row = next(item for item in daily_statistics(root) if item["date"] == "2026-08-25")
            self.assertEqual(row["prompt_count"], 1)

    def test_recovery_uses_structured_terminal_evidence_and_canonical_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "inbox-104eb3087bf34afa914358155fc77073"
            persist_execution(root, self._record("run-bootstrap", "COMPLETE", datetime(2026, 8, 24, 10, tzinfo=timezone.utc)))
            with open_storage(root) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                        (run_id, '{"repository":"pcvantol/djconnect","execution_mode":"MANAGED","agent_execution_seconds":1443.574}', "COMPLETE", "2026-08-25T20:04:22Z"),
                    )
                    connection.execute(
                        "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,execution_metadata,updated_at) VALUES(?,?,?,?,?,?)",
                        (run_id, "COMPLETE", "EP baseline", "2026-08-25T20:04:22Z", '{"codex_commands_executed":16}', "2026-08-25T20:04:22Z"),
                    )
            record_phase(root, run_id, "QUEUE_WAIT", started_at=datetime(2026, 8, 25, 19, 31, 17, tzinfo=timezone.utc), completed_at=datetime(2026, 8, 25, 19, 31, 18, tzinfo=timezone.utc))
            record_phase(root, run_id, "TOTAL_EXECUTION", started_at=datetime(2026, 8, 25, 19, 31, 18, tzinfo=timezone.utc), completed_at=datetime(2026, 8, 25, 20, 2, 44, tzinfo=timezone.utc))
            self.assertEqual(recover_terminal_telemetry(root, run_id), "recovered")
            self.assertEqual(recover_terminal_telemetry(root, run_id), "already_materialized")
            with open_storage(root) as connection:
                self.assertEqual(connection.execute("SELECT execution_date,terminal_state FROM execution_runs WHERE run_id=?", (run_id,)).fetchone(), ("2026-08-25", "COMPLETE"))
                self.assertEqual(connection.execute("SELECT source,state FROM terminal_telemetry_outbox WHERE run_id=?", (run_id,)).fetchone(), ("RECOVERY", "PROCESSED"))
            self.assertEqual(next(item for item in daily_statistics(root) if item["date"] == "2026-08-25")["prompt_count"], 1)

    def test_automatic_recovery_fails_closed_when_terminal_timing_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persist_execution(root, self._record("run-bootstrap", "COMPLETE", datetime.now(timezone.utc)))
            with open_storage(root) as connection:
                with connection:
                    connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("run-missing-time", '{"repository":"pcvantol/djconnect","execution_mode":"MANAGED"}', "COMPLETE", "2026-08-25T20:04:22Z"))
                    connection.execute("INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,execution_metadata,updated_at) VALUES(?,?,?,?,?,?)", ("run-missing-time", "COMPLETE", "Missing", "2026-08-25T20:04:22Z", "{}", "2026-08-25T20:04:22Z"))
            self.assertEqual(recover_missing_terminal_telemetry(root), {"recovered": 0, "failed": 1, "candidates": 1})
            with open_storage(root) as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM execution_runs WHERE run_id='run-missing-time'").fetchone())

    def test_daily_statistics_keeps_phase_detail_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persist_execution(root, self._record("run-trend", "COMPLETE", datetime(2026, 8, 1, 10, tzinfo=timezone.utc)))
            with patch("tools.engineering.telemetry.daily_timing_detail", side_effect=AssertionError("detail query")):
                rows = daily_statistics(root)
        self.assertEqual(rows[0]["average_provider_execution_seconds"], None)
        self.assertEqual(rows[0]["average_validation_seconds"], None)

    def test_daily_statistics_allows_the_configurable_dashboard_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(daily_statistics(root, days=360), [])
            with self.assertRaises(ValueError):
                daily_statistics(root, days=361)

    def test_prune_telemetry_removes_only_expired_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = datetime.now(timezone.utc) - timedelta(days=91)
            recent = datetime.now(timezone.utc) - timedelta(days=1)
            persist_execution(root, self._record("run-old", "COMPLETE", old))
            persist_execution(root, self._record("run-recent", "COMPLETE", recent))

            self.assertEqual(prune_telemetry(root, 90), {"daily_statistics": 1, "execution_runs": 1})
            with open_storage(root) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM daily_execution_statistics").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0], 2)

    def test_clear_telemetry_preserves_execution_receipts_and_timing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
            persist_execution(root, self._record("run-clear", "COMPLETE", started))
            record_phase(root, "run-clear", "PROVIDER_EXECUTION", started_at=started, completed_at=started + timedelta(seconds=30))

            self.assertEqual(clear_telemetry(root), {"daily_statistics": 1, "execution_runs": 1})
            self.assertEqual(daily_statistics(root), [])
            with open_storage(root) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_phase_spans").fetchone()[0], 1)

    def test_persists_only_aggregate_execution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = ExecutionTelemetry(
                **{
                    **self._record("run-metadata", "COMPLETE", datetime.now(timezone.utc)).__dict__,
                    "execution_metadata": {
                        "modified": 3, "created": 2, "deleted": 1,
                        "codex_commands_executed": 17, "command": "must-not-persist",
                    },
                }
            )
            persist_execution(root, telemetry)

            with open_storage(root) as connection:
                stored = connection.execute(
                    "SELECT execution_metadata FROM execution_runs WHERE run_id='run-metadata'"
                ).fetchone()[0]
            self.assertEqual(
                stored,
                '{"codex_commands_executed":17,"created":2,"deleted":1,"modified":3}',
            )

    def test_daily_timing_detail_projects_canonical_phase_spans_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 15, 10, tzinfo=timezone.utc)
            persist_execution(root, self._record("run-phase", "COMPLETE", started))
            record_phase(root, "run-phase", "QUEUE_WAIT", started_at=started - timedelta(seconds=12), completed_at=started)
            record_phase(root, "run-phase", "PROVIDER_EXECUTION", started_at=started, completed_at=started + timedelta(seconds=40))
            record_phase(root, "run-phase", "VALIDATION", started_at=started + timedelta(seconds=40), completed_at=started + timedelta(seconds=50))
            record_phase(root, "run-phase", "EXTERNAL_CI_WAIT", started_at=started + timedelta(seconds=50), completed_at=started + timedelta(seconds=70))
            record_phase(root, "run-phase", "TOTAL_EXECUTION", started_at=started - timedelta(seconds=12), completed_at=started + timedelta(seconds=90))
            detail = daily_timing_detail(root, "2026-08-15")
        self.assertEqual(detail["summary"]["provider_execution"]["average_ms"], 40_000)
        self.assertEqual(detail["summary"]["validation"]["average_ms"], 10_000)
        self.assertEqual(detail["summary"]["queue_wait"]["average_ms"], 12_000)
        self.assertEqual(detail["summary"]["external_wait"]["average_ms"], 20_000)
        self.assertEqual(detail["summary"]["overhead"]["average_ms"], 32_000)
        self.assertEqual(detail["runs"][0]["model"], "gpt-5.6-terra")
        self.assertEqual(detail["bottlenecks"]["top_time_consumers"][0]["phase"], "PROVIDER_EXECUTION")
        self.assertEqual(detail["bottlenecks"]["top_time_consumers"][0]["share_percent"], 39.216)
        self.assertEqual(detail["bottlenecks"]["shares"]["provider_execution"], 39)

    def test_daily_timing_detail_caps_a_phase_share_to_total_execution_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 15, 10, tzinfo=timezone.utc)
            persist_execution(root, self._record("run-stale-share", "FAILED", started))
            record_phase(root, "run-stale-share", "TOTAL_EXECUTION", started_at=started,
                         completed_at=started + timedelta(seconds=100), outcome="FAILED")
            record_phase(root, "run-stale-share", "PROVIDER_EXECUTION", started_at=started + timedelta(seconds=50),
                         completed_at=started + timedelta(seconds=160), outcome="STALE")
            detail = daily_timing_detail(root, "2026-08-15")
        provider = next(item for item in detail["phases"] if item["phase"] == "PROVIDER_EXECUTION")
        self.assertEqual(provider["total_ms"], 110_000)
        self.assertEqual(provider["share_percent"], 50.0)
    def test_async_telemetry_failure_is_isolated_from_engineering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observed = Event()
            errors: list[Exception] = []

            def failure(_: Path, __: ExecutionTelemetry, **___: object) -> None:
                raise RuntimeError("storage unavailable")

            with patch("tools.engineering.telemetry.persist_execution", side_effect=failure):
                worker = persist_execution_async(
                    Path(temporary),
                    self._record("run-failed-telemetry", "FAILED", datetime.now(timezone.utc)),
                    on_error=lambda error: (errors.append(error), observed.set()),
                )
                worker.join(timeout=2)

            self.assertTrue(observed.is_set())
            self.assertEqual(str(errors[0]), "storage unavailable")

    def test_persists_producer_metadata_and_an_immutable_execution_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = ExecutionTelemetry(
                **{
                    **self._record("run-forge", "COMPLETE", datetime.now(timezone.utc)).__dict__,
                    "producer": ProducerMetadata(
                        producer_id="forge", producer_type="FORGE", producer_version="2.0",
                        correlation_id="corr-42", mission_id="MISSION-0003",
                        engineering_action_id="EA-0042", execution_constraint_version="1.0",
                    ),
                }
            )
            persist_execution(root, telemetry)
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT producer_id, producer_type, mission_id, engineering_action_id, correlation_id "
                        "FROM execution_runs WHERE run_id='run-forge'"
                    ).fetchone(),
                    ("forge", "FORGE", "MISSION-0003", "EA-0042", "corr-42"),
                )
                receipt = connection.execute(
                    "SELECT producer_id, producer_type, execution_host, execution_outcome "
                    "FROM execution_receipts WHERE run_id='run-forge'"
                ).fetchone()
                self.assertEqual(receipt, ("forge", "FORGE", "Engineering Platform", "COMPLETE"))
            persist_execution(
                root,
                ExecutionTelemetry(
                    **{**telemetry.__dict__, "producer": ProducerMetadata(producer_id="changed", producer_type="EXTERNAL")}
                ),
            )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT producer_id FROM execution_runs WHERE run_id='run-forge'").fetchone()[0],
                    "forge",
                )
                self.assertEqual(
                    connection.execute("SELECT producer_id FROM execution_receipts WHERE run_id='run-forge'").fetchone()[0],
                    "forge",
                )

    def test_duration_estimate_uses_only_complete_runs_with_the_exact_runtime_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
            first = self._record("run-one", "COMPLETE", started)
            second = self._record("run-two", "COMPLETE", started + timedelta(hours=1))
            incompatible = self._record("run-three", "COMPLETE", started + timedelta(hours=2))
            incompatible = ExecutionTelemetry(
                **{**incompatible.__dict__, "runtime_model": "gpt-5.6-sol"}
            )
            persist_execution(root, first)
            persist_execution(root, second)
            persist_execution(root, incompatible)

            estimate = comparable_duration_estimate(
                root,
                prompt_characters=2_000,
                runtime_metadata={
                    "runtime_provider": "codex_cli",
                    "model": "gpt-5.6-terra",
                    "reasoning_profile": "medium",
                    "configuration_profile": "workspace-write",
                },
            )

            self.assertEqual(estimate["sample_count"], 2)
            self.assertEqual(estimate["average_seconds"], 106.066)
            self.assertEqual(estimate["lower_seconds"], 106.066)
            self.assertEqual(estimate["upper_seconds"], 106.066)
            self.assertEqual(
                comparable_duration_estimate(
                    root,
                    prompt_characters=2_000,
                    runtime_metadata={"runtime_provider": "codex_cli", "model": "not reported"},
                ),
                {},
            )

    def test_duration_estimate_uses_comparable_phase_timings_for_remaining_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
            for run_id, provider_seconds in (("phase-one", 60), ("phase-two", 70), ("phase-three", 80), ("phase-four", 100)):
                persist_execution(root, self._record(run_id, "COMPLETE", started))
                record_phase(root, run_id, "PROVIDER_EXECUTION", started_at=started, completed_at=started + timedelta(seconds=provider_seconds))
                record_phase(root, run_id, "FINALIZATION", started_at=started + timedelta(seconds=provider_seconds), completed_at=started + timedelta(seconds=provider_seconds + 20))
                record_phase(root, run_id, "REPOSITORY_CLEANUP", started_at=started + timedelta(seconds=provider_seconds + 20), completed_at=started + timedelta(seconds=provider_seconds + 30))

            estimate = comparable_duration_estimate(
                root,
                prompt_characters=1_000,
                runtime_metadata={
                    "runtime_provider": "codex_cli",
                    "model": "gpt-5.6-terra",
                    "reasoning_profile": "medium",
                    "configuration_profile": "workspace-write",
                },
                current_phase="EXECUTE_AGENT",
                execution_mode="MANAGED",
            )

            self.assertTrue(estimate["phase_aware"])
            self.assertEqual(estimate["phase_sample_count"], 4)
            self.assertEqual(estimate["remaining_lower_seconds"], 90.0)
            self.assertEqual(estimate["remaining_upper_seconds"], 110.0)

    def test_async_telemetry_never_recreates_a_removed_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            persist_execution(root, self._record("run-existing", "COMPLETE", datetime.now(timezone.utc)))
            shutil.rmtree(root)

            worker = persist_execution_async(
                root,
                self._record("run-removed", "COMPLETE", datetime.now(timezone.utc)),
            )
            worker.join(timeout=2)

            self.assertFalse(root.exists())

    def test_pending_telemetry_can_be_drained_before_workspace_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persist_execution(root, self._record("run-existing", "COMPLETE", datetime.now(timezone.utc)))
            worker = persist_execution_async(
                root,
                self._record("run-drained", "COMPLETE", datetime.now(timezone.utc)),
            )
            wait_for_pending_telemetry()
            self.assertFalse(worker.is_alive())
            self.assertTrue(execution_timing(root, "run-drained"))
