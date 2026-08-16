from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tools.engineering.execution_timing import (
    complete_active_phase, complete_phase, phase_spans, reconcile_interrupted_phases, record_phase,
    start_phase, timing_summary,
)
from tools.engineering.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage
from tools.engineering.telemetry import ExecutionTelemetry, persist_execution


class ExecutionPhaseTimingTest(unittest.TestCase):
    def test_persists_monotonic_repeated_and_nested_spans_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = start_phase(root, "run-one", "VALIDATION", monotonic_clock=10)
            child = start_phase(root, "run-one", "VALIDATION", parent_phase_id=parent.phase_id, monotonic_clock=11)
            complete_phase(root, child, monotonic_clock=13)
            complete_phase(root, parent, monotonic_clock=15)
            again = start_phase(root, "run-one", "VALIDATION", monotonic_clock=20)
            complete_phase(root, again, monotonic_clock=24)

            spans = phase_spans(root, "run-one")
            self.assertEqual([span["duration_ms"] for span in spans], [5000, 2000, 4000])
            self.assertEqual(spans[1]["parent_phase_id"], parent.phase_id)
            self.assertEqual(timing_summary(root, "run-one")["validation_time_ms"], 9000)
            with open_storage(root) as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0], ENGINEERING_STORAGE_SCHEMA_VERSION)

    def test_queue_provider_external_and_derived_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = datetime(2026, 8, 15, tzinfo=timezone.utc)
            record_phase(root, "run-two", "QUEUE_WAIT", started_at=start, completed_at=start + timedelta(seconds=2))
            record_phase(root, "run-two", "PROVIDER_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=40))
            record_phase(root, "run-two", "VALIDATION", started_at=start, completed_at=start + timedelta(seconds=10))
            record_phase(root, "run-two", "EXTERNAL_CI_WAIT", started_at=start, completed_at=start + timedelta(seconds=20))
            record_phase(root, "run-two", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=100))
            summary = timing_summary(root, "run-two")
            self.assertEqual(summary["queue_wait_time_ms"], 2000)
            self.assertEqual(summary["provider_execution_time_ms"], 40000)
            self.assertEqual(summary["external_wait_time_ms"], 20000)
            self.assertEqual(summary["overhead_time_ms"], 30000)
            self.assertEqual(summary["overhead_share_percent"], 30.0)
            self.assertEqual(summary["longest_phase"], "PROVIDER_EXECUTION")
            self.assertEqual([item["phase"] for item in summary["top_time_consumers"]], ["PROVIDER_EXECUTION", "EXTERNAL_CI_WAIT", "VALIDATION"])

    def test_nested_validation_remains_measurable_without_reducing_overhead_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            total = start_phase(root, "run-nested", "TOTAL_EXECUTION", monotonic_clock=0)
            provider = start_phase(root, "run-nested", "PROVIDER_EXECUTION", monotonic_clock=10)
            validation = start_phase(
                root, "run-nested", "VALIDATION", parent_phase_id=provider.phase_id, monotonic_clock=20,
            )
            complete_phase(root, validation, monotonic_clock=30)
            complete_phase(root, provider, monotonic_clock=50)
            complete_phase(root, total, monotonic_clock=100)

            summary = timing_summary(root, "run-nested")
            self.assertEqual(summary["provider_execution_time_ms"], 40000)
            self.assertEqual(summary["validation_time_ms"], 10000)
            self.assertEqual(summary["overhead_time_ms"], 60000)

    def test_stale_reconciliation_preserves_completed_spans_and_closes_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = start_phase(root, "run-stale", "INITIALIZATION", monotonic_clock=1)
            complete_phase(root, complete, monotonic_clock=2)
            active = start_phase(root, "run-stale", "PROVIDER_EXECUTION", monotonic_clock=3)
            self.assertEqual(reconcile_interrupted_phases(root, "run-stale"), 1)
            spans = phase_spans(root, "run-stale")
            self.assertEqual(spans[0]["outcome"], "COMPLETE")
            self.assertEqual(spans[1]["phase_id"], active.phase_id)
            self.assertEqual(spans[1]["outcome"], "STALE")

    def test_historical_runs_have_no_fabricated_phase_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = timing_summary(Path(temporary), "legacy-run")
            self.assertFalse(summary["phase_telemetry_available"])
            self.assertEqual(summary["top_time_consumers"], [])

    def test_historical_runs_preserve_existing_total_without_phase_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = datetime(2026, 8, 15, tzinfo=timezone.utc)
            persist_execution(root, ExecutionTelemetry(
                run_id="legacy-total", arrived_at=started,
                execution_started_at=started, execution_finished_at=started + timedelta(seconds=12),
                terminal_state="COMPLETE", execution_seconds=None, input_tokens=None,
                output_tokens=None, total_tokens=None, execution_mode="MANAGED",
                workspace="djconnect", repository="pcvantol/djconnect", execution_host_version="1.0",
            ))
            summary = timing_summary(root, "legacy-total")
            self.assertFalse(summary["phase_telemetry_available"])
            self.assertTrue(summary["historical_total_available"])
            self.assertEqual(summary["total_wall_time_ms"], 12000)

    def test_cross_process_total_envelope_and_nested_critical_path_are_non_overlapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            total = start_phase(root, "run-cross-process", "TOTAL_EXECUTION", monotonic_clock=1)
            repair = start_phase(root, "run-cross-process", "REPAIR", monotonic_clock=2)
            provider = start_phase(
                root, "run-cross-process", "PROVIDER_EXECUTION",
                parent_phase_id=repair.phase_id, monotonic_clock=3,
            )
            complete_phase(root, provider, monotonic_clock=13)
            complete_phase(root, repair, monotonic_clock=17)
            self.assertTrue(complete_active_phase(root, "run-cross-process", "TOTAL_EXECUTION"))
            self.assertFalse(complete_active_phase(root, "run-cross-process", "TOTAL_EXECUTION"))

            summary = timing_summary(root, "run-cross-process")
            self.assertEqual(summary["provider_execution_time_ms"], 10000)
            self.assertEqual(summary["top_time_consumers"], [{"phase": "REPAIR", "duration_ms": 15000}])
            self.assertEqual(
                next(span for span in phase_spans(root, "run-cross-process") if span["phase_id"] == total.phase_id)["outcome"],
                "COMPLETE",
            )
