from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engineering_platform.execution_timing import complete_phase, record_phase, start_or_resume_phase, start_phase, timing_summary
from engineering_platform import server
from engineering_platform.provider_usage import (
    AUTHORITATIVE, ProviderInvocation, churn_from_jsonl, persist_provider_invocation,
    provider_usage_summary, usage_from_jsonl, usage_snapshots_from_jsonl,
)
from engineering_platform.storage import open_storage
from engineering_platform.storage import EngineeringStorageError
from engineering_platform.telemetry_contract import execution_chain_summary


def command_event(kind: str, item_id: str | None, command: str, output: str = "", exit_code: object = 0) -> str:
    item = {"type": "command_execution", "command": command, "aggregated_output": output}
    if item_id is not None:
        item["id"] = item_id
    if kind == "item.completed":
        item["exit_code"] = exit_code
    return json.dumps({"type": kind, "item": item})


class CanonicalProviderEventTests(unittest.TestCase):
    def test_item_lifecycle_and_replayed_completion_are_counted_once(self) -> None:
        events = "\n".join((
            command_event("item.started", "tool-1", "pytest tests", ""),
            command_event("item.updated", "tool-1", "pytest tests", "partial"),
            command_event("item.completed", "tool-1", "pytest tests", "partial complete"),
            command_event("item.completed", "tool-1", "pytest tests", "partial complete"),
        ))
        result = churn_from_jsonl(events)
        self.assertEqual(result["shell_command_calls"], 1)
        self.assertEqual(result["test_commands"], 1)
        self.assertEqual(result["tool_output_bytes"], len("partial complete"))
        self.assertEqual(result["duplicate_terminal_events"], 1)
        self.assertEqual(result["event_identity_coverage"], "COMPLETE")

    def test_item_identity_is_invocation_scoped_and_missing_identity_is_partial(self) -> None:
        first = churn_from_jsonl(command_event("item.completed", "same", "git status", "one"))
        second = churn_from_jsonl(command_event("item.completed", "same", "git status", "two"))
        missing = churn_from_jsonl(command_event("item.completed", None, "git status", "three"))
        self.assertEqual(first["shell_command_calls"] + second["shell_command_calls"], 2)
        self.assertEqual(missing["event_identity_coverage"], "PARTIAL")

        missing_lifecycle = churn_from_jsonl("\n".join((
            command_event("item.started", None, "git status", ""),
            command_event("item.updated", None, "git status", "three"),
            command_event("item.completed", None, "git status", "three"),
        )))
        self.assertEqual(missing_lifecycle["shell_command_calls"], 1)

    def test_out_of_order_events_and_conflicting_terminal_are_explicit(self) -> None:
        result = churn_from_jsonl("\n".join((
            command_event("item.completed", "tool-2", "pytest tests", "failed", 1),
            command_event("item.started", "tool-2", "pytest tests"),
            command_event("item.completed", "tool-2", "pytest tests", "different", 0),
        )))
        self.assertEqual(result["shell_command_calls"], 1)
        self.assertEqual(result["conflicting_terminal_events"], 1)
        self.assertEqual(result["event_identity_coverage"], "CONFLICT")
        self.assertGreater(result["passing_test_output_bytes"], 0)

    def test_structured_pr_results_count_objects_not_json_lines(self) -> None:
        payload = json.dumps([
            {"number": number, "repository": {"nameWithOwner": "owner/repo"}, "body": "\n".join(["x"] * 100)}
            for number in (1, 2, 3)
        ], indent=2)
        events = "\n".join((
            command_event("item.completed", "pr-1", "gh pr list --json number,repository,body", payload),
            command_event("item.completed", "pr-2", "gh pr list --json number,repository,body", payload),
        ))
        result = churn_from_jsonl(events)
        self.assertEqual(result["historical_pr_result_occurrences"], 6)
        self.assertEqual(result["historical_unique_pr_results"], 3)
        self.assertEqual(result["historical_pr_results"], 6)
        self.assertEqual(result["historical_pr_metrics_coverage"], "COMPLETE")

    def test_unstructured_pr_and_read_commands_never_claim_exact_observations(self) -> None:
        result = churn_from_jsonl("\n".join((
            command_event("item.completed", "pr", "gh pr list", "row one\nrow two"),
            command_event("item.completed", "read-1", "sed -n '1,20p' secret/path"),
            command_event("item.completed", "read-2", "sed -n '1,20p' secret/path"),
        )))
        self.assertEqual(result["historical_pr_metrics_coverage"], "PARTIAL")
        self.assertEqual(result["historical_pr_results_legacy_lines"], 2)
        self.assertEqual(result["unique_read_commands"], 1)
        self.assertEqual(result["repeated_read_commands"], 1)
        self.assertEqual(result["file_read_observation_coverage"], "UNAVAILABLE")
        self.assertNotIn("secret/path", json.dumps(result))

    def test_unknown_test_status_and_cumulative_output_are_separate(self) -> None:
        result = churn_from_jsonl("\n".join((
            command_event("item.updated", "test", "pytest tests", "abc", None),
            command_event("item.completed", "test", "pytest tests", "abcdef", None),
        )))
        self.assertEqual(result["tool_output_bytes"], 6)
        self.assertEqual(result["unknown_test_commands"], 1)
        self.assertEqual(result["unknown_test_output_bytes"], 6)
        self.assertEqual(result["failed_test_diagnostic_bytes"], 0)

    def test_usage_replay_reset_and_invalid_counters(self) -> None:
        event = json.dumps({"type": "turn.completed", "id": "turn-1", "usage": {
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
        }})
        self.assertEqual(len(usage_snapshots_from_jsonl(event, event)), 1)
        reset = "\n".join((event, json.dumps({"type": "turn.completed", "id": "turn-2", "usage": {
            "input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 1,
        }})))
        self.assertEqual(usage_from_jsonl(reset), {})
        invalid = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": True, "cached_input_tokens": 200, "output_tokens": -1,
        }})
        self.assertEqual(usage_from_jsonl(invalid), {"cached_input_tokens": 200})


class CanonicalUsageAndTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_partial_usage_is_per_metric_and_unknown_model_is_preserved(self) -> None:
        for ordinal in range(1, 10):
            usage = {} if ordinal == 9 else {
                "input_tokens": 3_850_000 if ordinal == 1 else 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
            }
            persist_provider_invocation(self.root, ProviderInvocation(
                run_id="run-nine", ordinal=ordinal, provider="codex_cli", model=None,
                model_authority=AUTHORITATIVE, raw_provider_model="gpt-future-observed",
                phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-09-17T00:00:00+00:00", completed_at="2026-09-17T00:00:01+00:00",
                duration_ms=1000, usage=usage,
            ))
        summary = provider_usage_summary(self.root, "run-nine")
        self.assertEqual(summary["metrics"]["input_tokens"]["coverage"], "PARTIAL")
        self.assertEqual(summary["metrics"]["input_tokens"]["observed_observations"], 8)
        self.assertEqual(summary["invocations"][0]["model"], "gpt-future-observed")
        self.assertEqual(summary["invocations"][0]["model_provenance"], "AUTHORITATIVE")
        self.assertEqual(summary["actual_single_request_context_size"], "UNAVAILABLE")
        self.assertNotIn("context", summary["metrics"]["input_tokens"]["meaning"].casefold().replace("not current context size", ""))

    def test_cache_ratio_uses_only_compatible_pairs(self) -> None:
        persist_provider_invocation(self.root, ProviderInvocation(
            "cache", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 1},
        ))
        persist_provider_invocation(self.root, ProviderInvocation(
            "cache", 2, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {"input_tokens": 50, "cached_input_tokens": 90, "output_tokens": 1},
        ))
        summary = provider_usage_summary(self.root, "cache")
        self.assertEqual(summary["cache_ratio_percent"], 80.0)
        self.assertEqual(summary["cache_ratio_population"]["observed_observations"], 1)
        self.assertEqual(summary["metrics"]["cached_input_tokens"]["coverage"], "PARTIAL")

    def test_invocation_receipt_replay_is_idempotent_and_conflict_fails_closed(self) -> None:
        invocation = ProviderInvocation(
            "receipt", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 1},
            invocation_id="stable-receipt",
        )
        self.assertEqual(persist_provider_invocation(self.root, invocation), "stable-receipt")
        self.assertEqual(persist_provider_invocation(self.root, invocation), "stable-receipt")
        self.assertEqual(provider_usage_summary(self.root, "receipt")["provider_invocation_count"], 1)
        with self.assertRaises(EngineeringStorageError):
            persist_provider_invocation(self.root, ProviderInvocation(
                **{**invocation.__dict__, "usage": {"input_tokens": 101, "cached_input_tokens": 80, "output_tokens": 1}}
            ))

    def test_exclusive_timing_closes_and_parallel_is_visible(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        record_phase(self.root, "timing", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=10))
        parent = record_phase(self.root, "timing", "PROVIDER_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=6))
        record_phase(self.root, "timing", "VALIDATION", started_at=start + timedelta(seconds=2), completed_at=start + timedelta(seconds=4), parent_phase_id=parent)
        record_phase(self.root, "timing", "EXTERNAL_CI_WAIT", started_at=start + timedelta(seconds=5), completed_at=start + timedelta(seconds=8))
        summary = timing_summary(self.root, "timing")
        distribution = {row["category"]: row["duration_ms"] for row in summary["exclusive_distribution"]}
        self.assertEqual(summary["coverage"]["state"], "COMPLETE")
        self.assertEqual(sum(distribution.values()), 10_000)
        self.assertEqual(distribution["VALIDATION"], 2_000)
        self.assertEqual(distribution["PARALLEL_OVERLAP"], 1_000)
        self.assertEqual(distribution["UNASSIGNED"], 2_000)
        self.assertTrue(summary["exclusive_distribution_closes"])
        self.assertGreater(sum(row["share_percent"] for row in summary["inclusive_phase_rows"]), 100)

    def test_incomparable_boundaries_are_conflict_not_forced_to_close(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        record_phase(self.root, "conflict", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=10))
        with open_storage(self.root) as connection:
            connection.execute("UPDATE execution_phase_spans SET duration_ms=50000 WHERE run_id='conflict' AND phase_name='TOTAL_EXECUTION'")
        summary = timing_summary(self.root, "conflict")
        self.assertEqual(summary["coverage"]["state"], "CONFLICT")
        self.assertFalse(summary["exclusive_distribution_closes"])

    def test_missing_parent_is_partial_and_recovery_basis_is_explicit(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        record_phase(self.root, "partial", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=10))
        orphan = start_phase(
            self.root, "partial", "VALIDATION", started_at=start + timedelta(seconds=1),
            parent_phase_id="missing-parent", monotonic_clock=0,
        )
        recovered = start_or_resume_phase(self.root, "partial", "VALIDATION")
        self.assertEqual(recovered.phase_id, orphan.phase_id)
        complete_phase(self.root, recovered, completed_at=start + timedelta(seconds=4))
        summary = timing_summary(self.root, "partial")
        self.assertEqual(summary["coverage"]["state"], "PARTIAL")
        self.assertFalse(summary["exclusive_distribution_closes"])
        self.assertEqual(summary["measurement_basis"]["reconciled_wall_clock_spans"], 1)
        self.assertEqual(summary["timeline"][1]["measurement_basis"], "RECONCILED_WALL_CLOCK")


class CanonicalLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        server.initialize(self.root)
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO ep_project_registrations VALUES('forge','{}','ACTIVE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:00+00:00')")
            connection.execute("INSERT INTO ep_repository_registrations VALUES('forge','forge','forge','authority','{}','2026-09-17T00:00:00+00:00','2026-09-17T00:00:00+00:00')")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, run_id: str, submission: str, started: str, ended: str, *, parent: str | None = None, resume: bool = False) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO ep_execution_runs VALUES(?,?,?,?,?,?)", (run_id, "forge", "COMPLETE", started, ended, "MANAGED"))
            connection.execute(
                "INSERT INTO execution_run_qualification_context VALUES(?,?,?,?,?,?)",
                (run_id, submission, int(parent is None), None if resume else parent, parent if resume else None, started),
            )

    def test_original_retry_chain_aggregates_without_double_invocations(self) -> None:
        self._run("original", "sub-1", "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:10+00:00")
        self._run("retry", "sub-2", "2026-09-17T00:00:20+00:00", "2026-09-17T00:00:30+00:00", parent="original")
        for run_id in ("original", "retry"):
            persist_provider_invocation(self.root, ProviderInvocation(
                run_id, 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
                "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
                {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
                invocation_id=f"invocation-{run_id}",
            ), central_database=self.database)
        chain = execution_chain_summary(self.root, "retry", central_database=self.database)
        self.assertEqual(chain["coverage"], "COMPLETE")
        self.assertEqual(chain["attempt_count"], 2)
        self.assertEqual(chain["original_attempt_count"], 1)
        self.assertEqual(chain["retry_count"], 1)
        self.assertEqual(chain["provider_invocation_count"], 2)
        self.assertEqual(chain["usage_metrics"]["input_tokens"]["value"], 200)
        self.assertEqual(chain["inter_attempt_gap_ms"], 10_000)

    def test_missing_parent_and_cycle_never_claim_complete(self) -> None:
        self._run("missing", "sub-m", "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", parent="absent")
        self.assertEqual(execution_chain_summary(self.root, "missing", central_database=self.database)["coverage"], "PARTIAL")
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO ep_execution_runs VALUES('a','forge','COMPLETE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:01+00:00','MANAGED')")
            connection.execute("INSERT INTO ep_execution_runs VALUES('b','forge','COMPLETE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:01+00:00','MANAGED')")
            connection.execute("INSERT INTO execution_run_qualification_context VALUES('a','sub-a',0,'b',NULL,'2026-09-17T00:00:00+00:00')")
            connection.execute("INSERT INTO execution_run_qualification_context VALUES('b','sub-b',0,'a',NULL,'2026-09-17T00:00:00+00:00')")
        self.assertEqual(execution_chain_summary(self.root, "a", central_database=self.database)["coverage"], "CONFLICT")

    def test_daily_api_overview_and_embedded_snapshot_share_canonical_values(self) -> None:
        self._run("api-run", "sub-api", "2026-09-17T10:00:00+00:00", "2026-09-17T10:00:10+00:00")
        started = datetime(2026, 9, 17, 10, tzinfo=timezone.utc)
        total = start_phase(
            self.root, "api-run", "TOTAL_EXECUTION", started_at=started,
            monotonic_clock=0, central_database=self.database,
        )
        provider = start_phase(
            self.root, "api-run", "PROVIDER_EXECUTION", started_at=started,
            parent_phase_id=total.phase_id, monotonic_clock=0, central_database=self.database,
        )
        complete_phase(self.root, provider, completed_at=started + timedelta(seconds=6), monotonic_clock=6)
        complete_phase(self.root, total, completed_at=started + timedelta(seconds=10), monotonic_clock=10)
        persist_provider_invocation(self.root, ProviderInvocation(
            "api-run", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            started.isoformat(), (started + timedelta(seconds=6)).isoformat(), 6000,
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
            invocation_id="api-invocation",
        ), central_database=self.database)

        detail = server._central_console_telemetry_detail(self.root, "forge", "2026-09-17")
        self.assertIsNotNone(detail)
        assert detail is not None
        run = detail["runs"][0]
        snapshot = run["telemetry_snapshot"]
        metrics = snapshot["attempt"]["usage"]["metrics"]
        self.assertEqual(detail["contract_version"], "telemetry-contract@2.0")
        self.assertEqual(detail["summary"]["usage"]["input_tokens"]["value"], metrics["input_tokens"]["value"])
        self.assertEqual(run["input_tokens"], metrics["input_tokens"]["value"])
        self.assertEqual(detail["summary"]["provider_unique_coverage"]["average_ms"], snapshot["attempt"]["timing"]["provider_unique_coverage_ms"])
        self.assertTrue(detail["exclusive_distribution_closes"])
        self.assertFalse(detail["inclusive_shares_additive"])
        overview = server._central_console_telemetry(self.root, "forge")
        self.assertEqual(overview[0]["input_tokens"], metrics["input_tokens"]["value"])
        self.assertEqual(overview[0]["contract_version"], detail["contract_version"])


class CanonicalTelemetryBatchTests(unittest.TestCase):
    def test_large_day_is_bounded_and_attempt_observations_are_batch_loaded(self) -> None:
        records = [{
            "run_id": f"run-{index:03d}", "state": "COMPLETE",
            "created_at": "2026-09-17T10:00:00+00:00",
            "updated_at": "2026-09-17T10:00:01+00:00",
        } for index in range(150)]
        snapshot = {
            "contract_version": "telemetry-contract@2.0",
            "attempt": {"usage": {"metrics": {}, "invocations": []}, "timing": {}},
            "chain": {"coverage": "PARTIAL", "runs": []},
        }
        with patch("engineering_platform.server._central_console_run_records", return_value=records), patch(
            "engineering_platform.server.load_lineage_graph", return_value=([], {}, {}),
        ), patch(
            "engineering_platform.server.provider_usage_summaries", return_value={},
        ) as usage_batch, patch(
            "engineering_platform.server.timing_summaries", return_value={},
        ) as timing_batch, patch(
            "engineering_platform.server.run_telemetry_snapshot", return_value=snapshot,
        ) as composer:
            detail = server._central_console_telemetry_detail(Path("/bounded"), "project", "2026-09-17")
        assert detail is not None
        self.assertEqual(detail["matching_run_count"], 150)
        self.assertEqual(detail["returned_run_count"], server.MAX_TELEMETRY_DAY_RUNS)
        self.assertTrue(detail["runs_truncated"])
        self.assertEqual(usage_batch.call_count, 1)
        self.assertEqual(timing_batch.call_count, 1)
        self.assertEqual(len(usage_batch.call_args.args[1]), server.MAX_TELEMETRY_DAY_RUNS)
        self.assertEqual(composer.call_count, server.MAX_TELEMETRY_DAY_RUNS)


if __name__ == "__main__":
    unittest.main()
