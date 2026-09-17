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
from engineering_platform.storage import open_storage, sqlite_connection
from engineering_platform.storage import EngineeringStorageError
from engineering_platform.telemetry_contract import execution_chain_summary
from engineering_platform.telemetry_export import (
    ExportSnapshotStore, detail_model, download_model, overview_model,
    serialize_json, serialize_markdown,
)
from engineering_platform.telemetry_metrics import aggregate_coverage
from engineering_platform.telemetry_metrics import VALID_SUBTOTAL, aggregate_numeric_metric, metric_coverage


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

    def test_pr_identity_retention_boundaries_and_duplicate_occurrences(self) -> None:
        for count, complete in ((249, True), (250, True), (251, False), (300, False)):
            payload = json.dumps([
                {"number": number, "repository": {"nameWithOwner": "owner/repo"}}
                for number in range(count)
            ])
            result = churn_from_jsonl(command_event(
                "item.completed", f"pr-{count}",
                "gh pr list --json number,repository", payload,
            ))
            self.assertEqual(result["historical_unique_pr_results"], count)
            self.assertEqual(len(result["historical_pr_identity_hashes"]), min(count, 250))
            self.assertEqual(result["historical_pr_identity_set_complete"], complete)
            self.assertEqual(result["historical_pr_identity_set_truncated"], not complete)
            self.assertEqual(
                result["historical_pr_identity_coverage"],
                "COMPLETE" if complete else "PARTIAL",
            )
        duplicate = json.dumps([
            {"number": 17, "repository": {"nameWithOwner": "owner/repo"}},
            {"number": 17, "repository": {"nameWithOwner": "owner/repo"}},
        ])
        repeated = churn_from_jsonl(command_event(
            "item.completed", "duplicate-pr",
            "gh pr list --json number,repository", duplicate,
        ))
        self.assertEqual(repeated["historical_pr_result_occurrences"], 2)
        self.assertEqual(repeated["historical_unique_pr_results"], 1)
        self.assertEqual(repeated["historical_pr_identity_coverage"], "COMPLETE")

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

    def test_conflicting_snapshot_is_not_promoted_by_matching_counts(self) -> None:
        persist_provider_invocation(self.root, ProviderInvocation(
            "conflict", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 5},
            invocation_id="conflicting-invocation",
            usage_snapshots=(
                {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 6},
                {"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 5},
            ),
        ))
        summary = provider_usage_summary(self.root, "conflict")
        metric = summary["metrics"]["input_tokens"]
        self.assertEqual(metric["coverage"], "CONFLICT")
        self.assertEqual(metric["present_observations"], 1)
        self.assertEqual(metric["valid_observations"], 0)
        self.assertEqual(metric["conflicting_observations"], 1)
        self.assertIsNone(metric["value"])
        self.assertIsNone(summary["cache_ratio_percent"])

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

    def test_pr_uniqueness_is_union_across_invocations_and_repositories(self) -> None:
        def pr_churn(item: str, repository: str) -> dict[str, object]:
            payload = json.dumps([{"number": 17, "repository": {"nameWithOwner": repository}}])
            return churn_from_jsonl(command_event("item.completed", item, "gh pr list --json number,repository", payload))
        for ordinal, repository in enumerate(("owner/repo", "owner/repo", "other/repo"), 1):
            persist_provider_invocation(self.root, ProviderInvocation(
                "prs", ordinal, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
                "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
                {}, invocation_id=f"pr-invocation-{ordinal}", churn=pr_churn(f"pr-{ordinal}", repository),
            ))
        summary = provider_usage_summary(self.root, "prs")
        self.assertEqual(summary["historical_pr_result_occurrences"], 3)
        self.assertEqual(summary["historical_unique_pr_results"], 2)
        self.assertEqual(summary["historical_pr_metrics_coverage"], "COMPLETE")
        self.assertEqual(summary["historical_unique_pr_coverage"], "COMPLETE")

    def test_truncated_and_legacy_pr_identity_sets_never_claim_exact_scope_uniqueness(self) -> None:
        def structured(count: int, *, offset: int = 0) -> dict[str, object]:
            payload = json.dumps([
                {"number": number, "repository": {"nameWithOwner": "owner/repo"}}
                for number in range(offset, offset + count)
            ])
            return churn_from_jsonl(command_event(
                "item.completed", f"pr-{offset}-{count}",
                "gh pr list --json number,repository", payload,
            ))

        churn_sources = (structured(200), structured(300, offset=100))
        expected_retained = len({
            identity
            for churn in churn_sources
            for identity in churn["historical_pr_identity_hashes"]
        })
        for ordinal, churn in enumerate(churn_sources, 1):
            persist_provider_invocation(self.root, ProviderInvocation(
                "bounded-prs", ordinal, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
                "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
                {}, invocation_id=f"bounded-pr-{ordinal}", churn=churn,
            ))
        bounded = provider_usage_summary(self.root, "bounded-prs")
        self.assertIsNone(bounded["historical_unique_pr_results"])
        self.assertEqual(bounded["historical_unique_pr_results_lower_bound"], expected_retained)
        self.assertEqual(bounded["historical_unique_pr_coverage"], "PARTIAL")
        self.assertEqual(bounded["historical_pr_identity_retained_count"], expected_retained)
        with open_storage(self.root) as connection:
            stored = json.loads(connection.execute(
                "SELECT churn FROM provider_invocations WHERE invocation_id='bounded-pr-2'"
            ).fetchone()[0])
        self.assertEqual(len(stored["historical_pr_identity_hashes"]), 250)
        self.assertTrue(stored["historical_pr_identity_set_truncated"])

        persist_provider_invocation(self.root, ProviderInvocation(
            "legacy-prs", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {}, invocation_id="legacy-pr", churn={
                "historical_pr_queries": 1,
                "historical_unique_pr_results": 4,
                "historical_pr_metrics_coverage": "COMPLETE",
            },
        ))
        legacy = provider_usage_summary(self.root, "legacy-prs")
        self.assertIsNone(legacy["historical_unique_pr_results"])
        self.assertEqual(legacy["historical_unique_pr_results_lower_bound"], 4)
        self.assertEqual(legacy["historical_unique_pr_coverage"], "PARTIAL")

    def test_inconsistent_complete_pr_identity_metadata_fails_closed(self) -> None:
        persist_provider_invocation(self.root, ProviderInvocation(
            "conflicting-pr-identities", 1, "codex_cli", None,
            "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
            {}, invocation_id="conflicting-pr-identity", churn={
                "historical_pr_queries": 1,
                "historical_unique_pr_results": 2,
                "historical_pr_identity_hashes": ["a" * 64],
                "historical_pr_identity_set_complete": True,
                "historical_pr_identity_set_truncated": False,
                "historical_pr_identity_retained_count": 1,
                "historical_pr_metrics_coverage": "COMPLETE",
                "historical_pr_identity_coverage": "COMPLETE",
            },
        ))
        with open_storage(self.root) as connection:
            stored = json.loads(connection.execute(
                "SELECT churn FROM provider_invocations WHERE invocation_id=?",
                ("conflicting-pr-identity",),
            ).fetchone()[0])
        self.assertFalse(stored["historical_pr_identity_set_complete"])
        self.assertEqual(stored["historical_pr_identity_coverage"], "CONFLICT")
        self.assertEqual(stored["historical_pr_unique_lower_bound"], 2)
        corrupt_stored = {
            **stored,
            "historical_pr_identity_set_complete": True,
            "historical_pr_identity_coverage": "COMPLETE",
        }
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE provider_invocations SET churn=? WHERE invocation_id=?",
                (json.dumps(corrupt_stored, sort_keys=True), "conflicting-pr-identity"),
            )
        summary = provider_usage_summary(self.root, "conflicting-pr-identities")
        self.assertIsNone(summary["historical_unique_pr_results"])
        self.assertEqual(summary["historical_unique_pr_results_lower_bound"], 2)
        self.assertEqual(summary["historical_unique_pr_coverage"], "CONFLICT")

        for identities in (
            [f"{index:064x}" for index in range(251)],
            ["A" * 64],
        ):
            corrupt_stored = {
                **stored,
                "historical_unique_pr_results": len(identities),
                "historical_pr_identity_hashes": identities,
                "historical_pr_identity_set_complete": True,
                "historical_pr_identity_set_truncated": False,
                "historical_pr_identity_retained_count": len(identities),
                "historical_pr_unique_lower_bound": len(identities),
                "historical_pr_identity_coverage": "COMPLETE",
            }
            with open_storage(self.root) as connection:
                connection.execute(
                    "UPDATE provider_invocations SET churn=? WHERE invocation_id=?",
                    (json.dumps(corrupt_stored, sort_keys=True), "conflicting-pr-identity"),
                )
            summary = provider_usage_summary(self.root, "conflicting-pr-identities")
            self.assertIsNone(summary["historical_unique_pr_results"])
            self.assertEqual(
                summary["historical_unique_pr_results_lower_bound"], len(identities),
            )
            self.assertFalse(summary["historical_pr_identity_set_complete"])
            self.assertEqual(summary["historical_unique_pr_coverage"], "CONFLICT")

    def test_churn_maximum_and_partial_coverage_keep_their_metric_semantics(self) -> None:
        for ordinal, coverage, maximum in ((1, "PARTIAL", 900), (2, "COMPLETE", 400)):
            persist_provider_invocation(self.root, ProviderInvocation(
                "churn-semantics", ordinal, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
                "2026-09-17T00:00:00+00:00", "2026-09-17T00:00:01+00:00", 1000,
                {}, invocation_id=f"churn-{ordinal}", churn={
                    "historical_pr_metrics_coverage": coverage,
                    "maximum_tool_output_bytes": maximum,
                },
            ))
        summary = provider_usage_summary(self.root, "churn-semantics")
        self.assertEqual(summary["context_churn"]["maximum_tool_output_bytes"], 900)
        self.assertEqual(summary["historical_pr_metrics_coverage"], "PARTIAL")

    def test_structured_pr_without_repository_is_not_fabricated(self) -> None:
        payload = json.dumps([{"number": 17}])
        churn = churn_from_jsonl(command_event(
            "item.completed", "pr-missing-repository", "gh pr list --json number", payload,
        ))
        self.assertEqual(churn["historical_pr_metrics_coverage"], "PARTIAL")
        self.assertEqual(churn["historical_unique_pr_results"], 0)

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

    def test_clock_difference_uses_wall_envelope_without_negative_rest(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        record_phase(self.root, "clock", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(milliseconds=10_100))
        record_phase(self.root, "clock", "PROVIDER_EXECUTION", started_at=start, completed_at=start + timedelta(milliseconds=10_100))
        with open_storage(self.root) as connection:
            connection.execute("UPDATE execution_phase_spans SET duration_ms=10000 WHERE run_id='clock' AND phase_name='TOTAL_EXECUTION'")
        summary = timing_summary(self.root, "clock")
        distribution = {row["category"]: row["duration_ms"] for row in summary["exclusive_distribution"]}
        self.assertEqual(summary["coverage"]["state"], "COMPLETE")
        self.assertEqual(summary["total_monotonic_duration_ms"], 10_000)
        self.assertEqual(summary["exclusive_envelope_duration_ms"], 10_100)
        self.assertEqual(summary["clock_difference_ms"], 100)
        self.assertEqual(distribution, {"PROVIDER_EXECUTION": 10_100})
        self.assertTrue(summary["exclusive_distribution_closes"])
        self.assertTrue(all(value >= 0 for value in distribution.values()))

    def test_opposite_clock_difference_and_small_segments_close_own_envelope(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        record_phase(self.root, "clock-opposite", "TOTAL_EXECUTION", started_at=start, completed_at=start + timedelta(seconds=10))
        with open_storage(self.root) as connection:
            connection.execute("UPDATE execution_phase_spans SET duration_ms=10100 WHERE run_id='clock-opposite' AND phase_name='TOTAL_EXECUTION'")
            for index in range(20):
                segment_start = start + timedelta(microseconds=index * 500_000)
                connection.execute(
                    """INSERT INTO execution_phase_spans(phase_id,run_id,phase_name,phase_category,parent_phase_id,attempt,ordinal,started_at,completed_at,duration_ms,outcome,metadata)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"small-{index}", "clock-opposite", "VALIDATION", "VALIDATION", None, 1, index + 2,
                     segment_start.isoformat(), (segment_start + timedelta(microseconds=500_000)).isoformat(), 500, "COMPLETE", '{"measurement_basis":"MONOTONIC"}'),
                )
        summary = timing_summary(self.root, "clock-opposite")
        self.assertEqual(summary["clock_difference_ms"], -100)
        self.assertEqual(summary["exclusive_distribution_total_ms"], 10_000)
        self.assertTrue(summary["exclusive_distribution_closes"])
        self.assertTrue(all(row["duration_ms"] >= 0 for row in summary["exclusive_distribution"]))

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
        with sqlite_connection(self.database) as connection:
            connection.execute("INSERT INTO ep_project_registrations VALUES('forge','{}','ACTIVE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:00+00:00')")
            connection.execute("INSERT INTO ep_repository_registrations VALUES('forge','forge','forge','authority','{}','2026-09-17T00:00:00+00:00','2026-09-17T00:00:00+00:00')")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, run_id: str, submission: str, started: str, ended: str, *, parent: str | None = None, resume: bool = False) -> None:
        with sqlite_connection(self.database) as connection:
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
        with sqlite_connection(self.database) as connection:
            connection.execute("INSERT INTO ep_execution_runs VALUES('a','forge','COMPLETE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:01+00:00','MANAGED')")
            connection.execute("INSERT INTO ep_execution_runs VALUES('b','forge','COMPLETE','2026-09-17T00:00:00+00:00','2026-09-17T00:00:01+00:00','MANAGED')")
            connection.execute("INSERT INTO execution_run_qualification_context VALUES('a','sub-a',0,'b',NULL,'2026-09-17T00:00:00+00:00')")
            connection.execute("INSERT INTO execution_run_qualification_context VALUES('b','sub-b',0,'a',NULL,'2026-09-17T00:00:00+00:00')")
        self.assertEqual(execution_chain_summary(self.root, "a", central_database=self.database)["coverage"], "CONFLICT")

    def test_verified_different_forge_actions_are_not_aggregated_as_one_chain(self) -> None:
        contexts = [
            {"run_id": "action-a", "fresh_submission": 1, "retry_parent_run_id": None, "resume_parent_run_id": None},
            {"run_id": "action-b", "fresh_submission": 0, "retry_parent_run_id": "action-a", "resume_parent_run_id": None},
            # Even though C has the selected Action again, it sits behind a
            # proven different-Action edge and must not re-enter the chain.
            {"run_id": "action-c", "fresh_submission": 0, "retry_parent_run_id": "action-b", "resume_parent_run_id": None},
        ]
        runs = {
            run_id: {
                "run_id": run_id, "state": "COMPLETE",
                "created_at": f"2026-09-17T00:00:{offset:02d}+00:00",
                "updated_at": f"2026-09-17T00:00:{offset + 1:02d}+00:00",
            }
            for run_id, offset in (("action-a", 0), ("action-b", 2), ("action-c", 4))
        }
        identities = {
            "action-a": {"engineering_action_id": "ACTION-A"},
            "action-b": {"engineering_action_id": "ACTION-B"},
            "action-c": {"engineering_action_id": "ACTION-A"},
        }
        def metric(run_id: str, value: int) -> dict[str, object]:
            return {
                "contract_version": "telemetry-contract@2.2", "provider_invocation_count": 1,
                "metrics": {name: {
                    "value": value, "value_semantics": VALID_SUBTOTAL, "coverage": "COMPLETE",
                    "expected_observations": 1, "present_observations": 1,
                    "valid_observations": 1, "conflicting_observations": 0,
                    "source_snapshot_reference": run_id,
                } for name in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")},
            }
        chain = execution_chain_summary(
            self.root, "action-b", central_database=self.database,
            _lineage_graph=(contexts, runs, identities),
            _usage_cache={
                "action-a": metric("action-a", 100),
                "action-b": metric("action-b", 50),
                "action-c": metric("action-c", 900),
            },
            _timing_cache={
                "action-a": {"total_wall_time_ms": 1000},
                "action-b": {"total_wall_time_ms": 1000},
                "action-c": {"total_wall_time_ms": 1000},
            },
        )
        self.assertEqual(chain["coverage"], "CONFLICT")
        self.assertEqual(chain["attempt_count"], 1)
        self.assertEqual(chain["runs"][0]["run_id"], "action-b")
        self.assertEqual(chain["usage_metrics"]["input_tokens"]["value"], 50)
        self.assertIn("conflicting-action-identity:action-a", chain["reasons"])
        self.assertIn("conflicting-action-identity:action-c", chain["reasons"])

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
        self.assertEqual(detail["contract_version"], "telemetry-contract@2.2")
        self.assertEqual(detail["summary"]["usage"]["input_tokens"]["value"], metrics["input_tokens"]["value"])
        self.assertEqual(run["input_tokens"], metrics["input_tokens"]["value"])
        self.assertEqual(detail["summary"]["provider_unique_coverage"]["average_ms"], snapshot["attempt"]["timing"]["provider_unique_coverage_ms"])
        self.assertTrue(detail["exclusive_distribution_closes"])
        self.assertFalse(detail["inclusive_shares_additive"])
        overview = server._central_console_telemetry(self.root, "forge")
        self.assertEqual(overview[0]["input_tokens"], metrics["input_tokens"]["value"])
        self.assertEqual(overview[0]["contract_version"], detail["contract_version"])

    def test_conflict_survives_run_day_and_chain_aggregation(self) -> None:
        self._run("conflict-run", "sub-conflict", "2026-09-17T11:00:00+00:00", "2026-09-17T11:00:10+00:00")
        persist_provider_invocation(self.root, ProviderInvocation(
            "conflict-run", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T11:00:00+00:00", "2026-09-17T11:00:01+00:00", 1000,
            {"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 5},
            invocation_id="conflict-chain-invocation",
            usage_snapshots=(
                {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 6},
                {"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 5},
            ),
        ), central_database=self.database)
        run = provider_usage_summary(self.root, "conflict-run", central_database=self.database)
        day = server._central_console_telemetry_detail(self.root, "forge", "2026-09-17")
        chain = execution_chain_summary(self.root, "conflict-run", central_database=self.database)
        self.assertEqual(run["metrics"]["input_tokens"]["coverage"], "CONFLICT")
        assert day is not None
        self.assertEqual(day["summary"]["usage"]["input_tokens"]["coverage"], "CONFLICT")
        self.assertEqual(chain["usage_metrics"]["input_tokens"]["coverage"], "CONFLICT")
        self.assertIsNone(chain["cache_ratio_percent"])

    def test_valid_subtotals_survive_conflict_across_run_day_chain_and_grouping(self) -> None:
        self._run("mixed-a", "sub-a", "2026-09-17T11:00:00+00:00", "2026-09-17T11:00:10+00:00")
        self._run(
            "mixed-b", "sub-b", "2026-09-17T11:00:20+00:00",
            "2026-09-17T11:00:30+00:00", parent="mixed-a",
        )
        persist_provider_invocation(self.root, ProviderInvocation(
            "mixed-a", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T11:00:00+00:00", "2026-09-17T11:00:01+00:00", 1000,
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
            invocation_id="mixed-valid",
        ), central_database=self.database)
        persist_provider_invocation(self.root, ProviderInvocation(
            "mixed-a", 2, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T11:00:01+00:00", "2026-09-17T11:00:02+00:00", 1000,
            {"input_tokens": 190, "cached_input_tokens": 90, "output_tokens": 8},
            invocation_id="mixed-conflict", usage_snapshots=(
                {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 9},
                {"input_tokens": 190, "cached_input_tokens": 90, "output_tokens": 8},
            ),
        ), central_database=self.database)
        persist_provider_invocation(self.root, ProviderInvocation(
            "mixed-b", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
            "2026-09-17T11:00:20+00:00", "2026-09-17T11:00:21+00:00", 1000,
            {"input_tokens": 50, "cached_input_tokens": 25, "output_tokens": 5},
            invocation_id="mixed-b-valid",
        ), central_database=self.database)

        run_a = provider_usage_summary(self.root, "mixed-a", central_database=self.database)
        run_b = provider_usage_summary(self.root, "mixed-b", central_database=self.database)
        metric_a = run_a["metrics"]["input_tokens"]
        metric_b = run_b["metrics"]["input_tokens"]
        self.assertEqual(metric_a["value"], 100)
        self.assertEqual(metric_a["coverage"], "CONFLICT")
        self.assertEqual(metric_a["value_semantics"], VALID_SUBTOTAL)
        direct = aggregate_numeric_metric(
            [metric_a, metric_b], aggregation_level="TEST_POPULATION", unit="tokens",
        )
        grouped = aggregate_numeric_metric([
            aggregate_numeric_metric([metric_a], aggregation_level="GROUP", unit="tokens"),
            aggregate_numeric_metric([metric_b], aggregation_level="GROUP", unit="tokens"),
        ], aggregation_level="TEST_POPULATION", unit="tokens")
        for result in (direct, grouped):
            self.assertEqual(result["value"], 150)
            self.assertEqual(result["coverage"], "CONFLICT")
            self.assertEqual(result["valid_observations"], 2)
            self.assertEqual(result["conflicting_observations"], 1)

        day = server._central_console_telemetry_detail(self.root, "forge", "2026-09-17")
        assert day is not None
        self.assertEqual(day["summary"]["usage"]["input_tokens"]["value"], 150)
        self.assertEqual(day["summary"]["usage"]["input_tokens"]["coverage"], "CONFLICT")
        self.assertEqual(day["summary"]["cache_ratio_percent"], 70.0)
        self.assertEqual(day["summary"]["cache_ratio_population"]["coverage"], "CONFLICT")
        chain = execution_chain_summary(self.root, "mixed-b", central_database=self.database)
        self.assertEqual(chain["usage_metrics"]["input_tokens"]["value"], 150)
        self.assertEqual(chain["usage_metrics"]["input_tokens"]["coverage"], "CONFLICT")
        self.assertEqual(chain["cache_ratio_percent"], 70.0)

        conflict_without_subtotal = {
            "value": 999, "coverage": "CONFLICT", "expected_observations": 1,
            "present_observations": 1, "valid_observations": 0,
            "conflicting_observations": 1,
        }
        all_conflict = aggregate_numeric_metric(
            [conflict_without_subtotal], aggregation_level="TEST", unit="tokens",
        )
        self.assertIsNone(all_conflict["value"])
        real_zero = aggregate_numeric_metric([{
            "value": 0, "value_semantics": VALID_SUBTOTAL,
            **metric_coverage(expected=2, present=2, valid=1, conflicting=1),
        }], aggregation_level="TEST", unit="tokens")
        self.assertEqual(real_zero["value"], 0)
        self.assertEqual(real_zero["coverage"], "CONFLICT")

    def test_missing_usage_projection_keeps_day_metric_incomplete(self) -> None:
        self._run("missing-usage", "sub-missing", "2026-09-17T12:00:00+00:00", "2026-09-17T12:00:10+00:00")
        detail = server._central_console_telemetry_detail(self.root, "forge", "2026-09-17")
        assert detail is not None
        self.assertNotEqual(detail["summary"]["usage"]["input_tokens"]["coverage"], "COMPLETE")

    def test_chain_export_contains_full_details_for_attempts_on_different_days(self) -> None:
        self._run("day-one", "sub-one", "2026-09-16T23:59:40+00:00", "2026-09-16T23:59:50+00:00")
        self._run(
            "day-two", "sub-two", "2026-09-17T00:00:10+00:00",
            "2026-09-17T00:00:20+00:00", parent="day-one",
        )
        for index, run_id in enumerate(("day-one", "day-two"), 1):
            started = datetime(2026, 9, 16 + index - 1, 23 if index == 1 else 0, 59 if index == 1 else 0, 40 if index == 1 else 10, tzinfo=timezone.utc)
            total = start_phase(
                self.root, run_id, "TOTAL_EXECUTION", started_at=started,
                monotonic_clock=0, central_database=self.database,
            )
            provider = start_phase(
                self.root, run_id, "PROVIDER_EXECUTION", started_at=started,
                parent_phase_id=total.phase_id, monotonic_clock=0,
                central_database=self.database,
            )
            complete_phase(
                self.root, provider, completed_at=started + timedelta(seconds=index),
                monotonic_clock=index,
            )
            complete_phase(
                self.root, total, completed_at=started + timedelta(seconds=10),
                monotonic_clock=10,
            )
            persist_provider_invocation(self.root, ProviderInvocation(
                run_id, 1, "codex_cli", f"model-{index}", "PROVIDER_EXECUTION", "IMPLEMENTATION",
                started.isoformat(), (started + timedelta(seconds=index)).isoformat(), index * 1000,
                {"input_tokens": index * 100, "cached_input_tokens": index * 50, "output_tokens": index * 10},
                model_authority=AUTHORITATIVE, invocation_id=f"invocation-{run_id}",
            ), central_database=self.database)

        with sqlite_connection(self.database) as connection:
            connection.executemany(
                """INSERT INTO provider_invocations(
                       invocation_id,run_id,ordinal,provider,phase,role,started_at,
                       completed_at,duration_ms,usage_authority,speed_state,retry_ordinal,
                       rate_table_version,churn
                   ) VALUES(?,'day-one',?,'codex_cli','PROVIDER_EXECUTION','IMPLEMENTATION',
                            '2026-09-16T23:59:40+00:00','2026-09-16T23:59:41+00:00',1,
                            'UNAVAILABLE','UNKNOWN',0,'2026-08-18','{}')""",
                [(f"invocation-day-one-{ordinal}", ordinal) for ordinal in range(2, 252)],
            )
            connection.executemany(
                """INSERT INTO execution_phase_spans(
                       phase_id,run_id,phase_name,phase_category,parent_phase_id,attempt,
                       ordinal,started_at,completed_at,duration_ms,outcome,metadata
                   ) VALUES(?,'day-one','VALIDATION','VALIDATION',NULL,1,?,
                            '2026-09-16T23:59:40+00:00','2026-09-16T23:59:40.001+00:00',1,
                            'COMPLETE','{"measurement_basis":"MONOTONIC"}')""",
                [(f"span-day-one-{ordinal}", ordinal) for ordinal in range(3, 502)],
            )

        detail = server._central_console_telemetry_detail(
            self.root, "forge", "2026-09-17", full=True,
        )
        assert detail is not None
        self.assertEqual(len(detail["runs"]), 1)
        attempts = detail["chain_attempts"]["day-two"]
        self.assertEqual([row["run_id"] for row in attempts], ["day-one", "day-two"])
        self.assertTrue(attempts[0]["outside_selected_window"])
        self.assertFalse(attempts[1]["outside_selected_window"])
        self.assertEqual(
            [row["telemetry_snapshot"]["attempt"]["usage"]["invocations"][0]["invocation_id"] for row in attempts],
            ["invocation-day-one", "invocation-day-two"],
        )
        self.assertEqual(len(attempts[0]["telemetry_snapshot"]["attempt"]["usage"]["invocations"]), 251)
        self.assertEqual(len(attempts[0]["telemetry_snapshot"]["attempt"]["timing"]["timeline"]), 501)
        self.assertTrue(all(row["telemetry_snapshot"]["attempt"]["timing"]["timeline"] for row in attempts))

        model = detail_model(
            project_id="forge", execution_date="2026-09-17", detail=detail,
            scope="EXECUTION_CHAIN", run_id="day-two", locale="nl",
        )
        parsed = json.loads(serialize_json(model))
        exported_attempts = parsed["data"]["chain_attempts"]
        self.assertEqual([row["run_id"] for row in exported_attempts], ["day-one", "day-two"])
        self.assertEqual(parsed["data"]["chain"]["usage_metrics"]["input_tokens"]["value"], 300)
        self.assertEqual(parsed["completeness"]["export"], "COMPLETE")
        markdown = serialize_markdown(model).decode("utf-8")
        self.assertIn("## Poging: `day-one`", markdown)
        self.assertIn("## Poging: `day-two`", markdown)
        self.assertIn("invocation-day-one", markdown)
        self.assertIn("invocation-day-two", markdown)
        self.assertIn("invocation-day-one-251", markdown)
        self.assertIn("span-day-one-501", markdown)

    def test_export_read_transaction_never_mixes_writer_commits_between_loaders(self) -> None:
        self._run("snapshot-run", "sub-snapshot", "2026-09-17T10:00:00+00:00", "2026-09-17T10:00:10+00:00")
        # Keep one WAL-capable connection alive. SQLite on macOS cannot open a
        # WAL database read-only after the final WAL owner has removed -shm,
        # whereas the installed CENTRAL writer remains alive in production.
        wal_keeper = sqlite3.connect(self.database)
        try:
            wal_keeper.execute("PRAGMA journal_mode=WAL")
            wal_keeper.execute("SELECT COUNT(*) FROM engineering_schema_migrations").fetchone()
            with server._telemetry_read_snapshot(self.root) as (
                read_connection, source_as_of, source_reference,
            ):
                before = server._central_console_telemetry_detail(
                    self.root, "forge", "2026-09-17", full=True,
                    _read_connection=read_connection,
                )
                persist_provider_invocation(self.root, ProviderInvocation(
                    "snapshot-run", 1, "codex_cli", None, "PROVIDER_EXECUTION", "IMPLEMENTATION",
                    "2026-09-17T10:00:00+00:00", "2026-09-17T10:00:01+00:00", 1000,
                    {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
                    invocation_id="snapshot-invocation",
                ), central_database=self.database)
                during = server._central_console_telemetry_detail(
                    self.root, "forge", "2026-09-17", full=True,
                    _read_connection=read_connection,
                )
                self.assertTrue(source_as_of.endswith("+00:00"))
                self.assertIn("central-schema:", source_reference)
        finally:
            wal_keeper.close()
        after = server._central_console_telemetry_detail(
            self.root, "forge", "2026-09-17", full=True,
        )
        assert before is not None and during is not None and after is not None
        for projection in (before, during):
            usage = projection["runs"][0]["telemetry_snapshot"]["attempt"]["usage"]
            self.assertEqual(usage.get("invocation_detail"), "UNAVAILABLE")
        usage_after = after["runs"][0]["telemetry_snapshot"]["attempt"]["usage"]
        self.assertEqual(usage_after["provider_invocation_count"], 1)
        self.assertEqual(usage_after["metrics"]["input_tokens"]["value"], 100)


class CanonicalTelemetryBatchTests(unittest.TestCase):
    def test_large_day_is_bounded_and_attempt_observations_are_batch_loaded(self) -> None:
        records = [{
            "run_id": f"run-{index:03d}", "state": "COMPLETE",
            "created_at": "2026-09-17T10:00:00+00:00",
            "updated_at": "2026-09-17T10:00:01+00:00",
        } for index in range(150)]
        snapshot = {
            "contract_version": "telemetry-contract@2.2",
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


class CanonicalTelemetryExportTests(unittest.TestCase):
    def test_retained_export_snapshot_is_bound_immutable_and_expires_explicitly(self) -> None:
        model = overview_model(
            project_id="forge", rows=[], sort_key="date", sort_direction="desc", locale="en",
            source_as_of="2026-09-17T10:00:00+00:00", source_reference="central-schema:45:data-version:4",
        )
        store = ExportSnapshotStore()
        with patch("engineering_platform.telemetry_export.monotonic", return_value=100.0):
            snapshot_id = store.retain(model, binding="project=forge")
            retained = store.read(snapshot_id, binding="project=forge")
        self.assertEqual(retained, model)
        assert retained is not None
        retained["selection"]["project_id"] = "mutated"
        with patch("engineering_platform.telemetry_export.monotonic", return_value=101.0):
            self.assertEqual(
                store.read(snapshot_id, binding="project=forge")["selection"]["project_id"],
                "forge",
            )
            self.assertIsNone(store.read(snapshot_id, binding="project=other"))
        downloaded = download_model(model)
        self.assertEqual(downloaded["snapshot_id"], snapshot_id)
        self.assertIsNotNone(downloaded["downloaded_at"])
        self.assertIsNone(model["downloaded_at"])
        with patch("engineering_platform.telemetry_export.monotonic", return_value=701.0):
            self.assertIsNone(store.read(snapshot_id, binding="project=forge"))

    def test_retained_export_snapshots_enforce_item_and_process_byte_budgets(self) -> None:
        first = overview_model(
            project_id="first", rows=[], sort_key="date", sort_direction="desc", locale="en",
        )
        second = overview_model(
            project_id="second", rows=[], sort_key="date", sort_direction="desc", locale="en",
        )
        encoded_sizes = [
            len(json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
            for model in (first, second)
        ]
        store = ExportSnapshotStore(
            max_snapshots=8,
            max_snapshot_bytes=max(encoded_sizes),
            max_retained_bytes=max(encoded_sizes) + 1,
        )
        first_id = store.retain(first, binding="first")
        second_id = store.retain(second, binding="second")
        self.assertIsNone(store.read(first_id, binding="first"))
        self.assertEqual(store.read(second_id, binding="second"), second)
        too_small = ExportSnapshotStore(
            max_snapshots=1, max_snapshot_bytes=1, max_retained_bytes=1,
        )
        with self.assertRaisesRegex(ValueError, "TELEMETRY_EXPORT_SNAPSHOT_TOO_LARGE"):
            too_small.retain(first, binding="first")

    def test_equal_observation_counts_do_not_upgrade_partial_or_conflict(self) -> None:
        partial = aggregate_coverage([{
            "coverage": "PARTIAL", "expected_observations": 1,
            "present_observations": 1, "valid_observations": 1,
            "missing_reason": "Parent relation unavailable",
        }])
        conflict = aggregate_coverage([{
            "coverage": "CONFLICT", "expected_observations": 1,
            "present_observations": 1, "valid_observations": 1,
            "missing_reason": "Clock basis conflict",
        }])
        self.assertEqual(partial["coverage"], "PARTIAL")
        self.assertEqual(conflict["coverage"], "CONFLICT")

    def test_overview_and_detail_json_markdown_share_snapshot_model(self) -> None:
        rows = [{
            "date": "2026-09-17", "prompt_count": 1, "input_tokens": None,
            "measurement_coverage": "PARTIAL", "contract_version": "telemetry-contract@2.2",
        }]
        overview = overview_model(
            project_id="forge", rows=rows, sort_key="date", sort_direction="desc", locale="nl",
        )
        overview_json = json.loads(serialize_json(overview))
        overview_markdown = serialize_markdown(overview).decode()
        self.assertEqual(overview_json["snapshot_id"], overview["snapshot_id"])
        self.assertIsNone(overview_json["data"]["overview"]["rows"][0]["input_tokens"])
        self.assertIn("Niet beschikbaar", overview_markdown)

        detail = {
            "contract_version": "telemetry-contract@2.2", "timezone": "UTC",
            "source_snapshot_references": ["run-1"], "matching_run_count": 1,
            "returned_run_count": 1, "runs_truncated": False,
            "summary": {
                "cache_ratio_percent": None,
                "usage": {"input_tokens": {"value": None, "coverage": "PARTIAL", "missing_reason": "One invocation lacks input usage"}},
            },
            "inclusive_phases": [], "exclusive_distribution": [],
            "bottlenecks": {"longest_average_phase": {"phase": "VALIDATION", "average_ms": 1250}},
            "runs": [{"run_id": "run-1", "telemetry_snapshot": {
                "contract_version": "telemetry-contract@2.2", "source_snapshot_reference": "run-1",
                "attempt": {"scope": "EP_RUN_ATTEMPT", "run_id": "run-1", "usage": {
                    "invocations": [{"invocation_id": f"inv-{index}", "input_tokens": index} for index in range(251)],
                }, "timing": {"timeline": [{"phase_id": f"span-{index}", "duration_ms": 1} for index in range(501)]}},
                "chain": {"runs": [{"run_id": "run-1"}]},
            }}],
        }
        detail["chain_attempts"] = {"run-1": list(detail["runs"])}
        exported = detail_model(
            project_id="forge", execution_date="2026-09-17", detail=detail,
            scope="UTC_DAY_DETAIL", run_id=None, locale="en",
        )
        parsed = json.loads(serialize_json(exported))
        snapshot = parsed["data"]["day_detail"]["runs"][0]["telemetry_snapshot"]["attempt"]
        self.assertEqual(len(snapshot["usage"]["invocations"]), 251)
        self.assertEqual(len(snapshot["timing"]["timeline"]), 501)
        detail_markdown = serialize_markdown(exported).decode()
        self.assertIn("Provider invocations", detail_markdown)
        self.assertIn("## Bottlenecks", detail_markdown)
        self.assertIn("## Limitations and conflicts", detail_markdown)
        self.assertIn("One invocation lacks input usage", detail_markdown)
        chain_export = detail_model(
            project_id="forge", execution_date="2026-09-17", detail=detail,
            scope="EXECUTION_CHAIN", run_id="run-1", locale="en",
        )
        self.assertEqual(chain_export["selection"]["scope"], "EXECUTION_CHAIN")
        self.assertEqual(chain_export["completeness"]["displayed_population"], 1)
        self.assertEqual(chain_export["completeness"]["full_population"], 1)

    def test_markdown_escapes_table_and_html_text_while_json_preserves_data(self) -> None:
        model = overview_model(
            project_id="forge", locale="fr", sort_key="date", sort_direction="desc",
            rows=[{
                "date": "2026-09-17", "prompt_count": 1,
                "measurement_coverage": "PARTIAL | <conflict>\nnext",
                "contract_version": "telemetry-contract@2.2",
            }],
        )
        markdown = serialize_markdown(model).decode("utf-8")
        parsed = json.loads(serialize_json(model))
        self.assertIn("PARTIAL \\| &lt;conflict&gt; next", markdown)
        self.assertNotIn("<conflict>", markdown)
        self.assertEqual(
            parsed["data"]["overview"]["rows"][0]["measurement_coverage"],
            "PARTIAL | <conflict>\nnext",
        )

    def test_chain_attempt_sections_are_localized_in_all_supported_export_locales(self) -> None:
        expected = {
            "en": "## Attempt: `run-1`",
            "nl": "## Poging: `run-1`",
            "de": "## Versuch: `run-1`",
            "fr": "## Tentative: `run-1`",
            "es": "## Intento: `run-1`",
        }
        for locale, heading in expected.items():
            with self.subTest(locale=locale):
                detail = {
                    "contract_version": "telemetry-contract@2.2", "timezone": "UTC",
                    "runs": [{"run_id": "run-1", "telemetry_snapshot": {
                        "chain": {"runs": [{"run_id": "run-1"}]},
                    }}],
                    "chain_attempts": {"run-1": [{"run_id": "run-1", "status": "COMPLETE"}]},
                }
                model = detail_model(
                    project_id="forge", execution_date="2026-09-17", detail=detail,
                    scope="EXECUTION_CHAIN", run_id="run-1", locale=locale,
                )
                self.assertIn(heading, serialize_markdown(model).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
