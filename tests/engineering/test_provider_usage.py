from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.provider_usage import (
    AUTHORITATIVE,
    ProviderInvocation,
    churn_from_jsonl,
    credit_estimate,
    persist_provider_invocation,
    provider_usage_summary,
    normalize_codex_model,
    speed_state,
    usage_from_jsonl,
    usage_snapshots_from_jsonl,
)
from engineering_platform.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage
from engineering_platform import server


class ProviderUsageTests(unittest.TestCase):
    def test_central_context_persists_provider_usage_without_local_database(self) -> None:
        with TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"; checkout = Path(temporary) / "checkout"; checkout.mkdir()
            server.initialize(data)
            previous = os.environ.get("EP_CENTRAL_OPERATIONAL_DATABASE")
            os.environ["EP_CENTRAL_OPERATIONAL_DATABASE"] = str(data / "engineering.db")
            try:
                persist_provider_invocation(checkout, ProviderInvocation(
                    "inbox-central-provider", 1, "codex_cli", "gpt-5.6-terra", "EXECUTE_AGENT", "agent",
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1000,
                    {"input_tokens": 1, "output_tokens": 2}, AUTHORITATIVE,
                ))
            finally:
                if previous is None: os.environ.pop("EP_CENTRAL_OPERATIONAL_DATABASE", None)
                else: os.environ["EP_CENTRAL_OPERATIONAL_DATABASE"] = previous
            self.assertFalse((checkout / ".engineering" / "engineering.db").exists())
            with sqlite3.connect(data / "engineering.db") as connection:
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM provider_invocations WHERE run_id='inbox-central-provider'"
                ).fetchone())
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _persist(
        self, ordinal: int, *, model: str = "gpt-5.6-terra", usage: dict[str, int] | None = None,
        snapshots: tuple[dict[str, int], ...] = (), role: str = "agent",
    ) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id="run-usage",
                ordinal=ordinal,
                provider="codex_cli",
                model=model,
                model_authority=AUTHORITATIVE,
                raw_provider_model=model,
                phase="PROVIDER_EXECUTION",
                role=role,
                started_at="2026-08-18T00:00:00+00:00",
                completed_at="2026-08-18T00:00:01+00:00",
                duration_ms=1000,
                usage=usage
                if usage is not None
                else {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10},
                runtime_metadata={"configuration_profile": "normal"},
                churn={"file_read_count": ordinal, "tool_output_bytes": 4},
                usage_snapshots=snapshots,
            ),
        )

    def test_persists_multiple_invocations_and_derives_cached_input_statistics(self) -> None:
        self._persist(1)
        self._persist(
            2, usage={"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 20}
        )
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertEqual(summary["provider_invocation_count"], 2)
        self.assertEqual(summary["input_tokens"], 400)
        self.assertEqual(summary["cached_input_tokens"], 125)
        self.assertEqual(summary["uncached_input_tokens"], 275)
        self.assertEqual(summary["max_input_tokens_per_invocation"], 300)
        self.assertEqual(summary["median_input_tokens_per_invocation"], 200)
        self.assertEqual(summary["usage_authority"], AUTHORITATIVE)
        self.assertEqual(summary["context_churn"]["file_read_count"], 3)
        self.assertEqual(summary["provider_invocations_by_role"], {"agent": 2})
        self.assertEqual(summary["uncached_input_by_role"], {"agent": 275})

    def test_provider_usage_is_observable_by_semantic_role(self) -> None:
        self._persist(1, role="IMPLEMENTATION")
        self._persist(2, role="REPAIR")
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertEqual(summary["provider_invocations_by_role"], {"IMPLEMENTATION": 1, "REPAIR": 1})

    def test_missing_usage_stays_unavailable(self) -> None:
        self._persist(1, usage={})
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertIsNone(summary["input_tokens"])
        self.assertEqual(summary["usage_authority"], "UNAVAILABLE")

    def test_interruption_diagnostic_persists_without_turn_usage(self) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id="run-interrupted", ordinal=1, provider="codex_cli", model=None,
                phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-08-30T00:00:00+00:00", completed_at="2026-08-30T00:00:01+00:00",
                duration_ms=None, usage={},
                churn={
                    "interruption_classification": "provider_turn_interrupted",
                    "interruption_reason": "interrupted",
                },
            ),
        )
        with open_storage(self.root) as connection:
            row = connection.execute(
                "SELECT usage_authority,churn FROM provider_invocations WHERE run_id='run-interrupted'"
            ).fetchone()
        self.assertEqual(row[0], "UNAVAILABLE")
        self.assertIn('"interruption_classification":"provider_turn_interrupted"', row[1])

    def test_versioned_rates_and_eur_are_derived(self) -> None:
        terra = credit_estimate(
            "gpt-5.6-terra",
            {
                "uncached_input_tokens": 1_000_000,
                "cached_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
        )
        sol = credit_estimate(
            "gpt-5.6-sol",
            {
                "uncached_input_tokens": 1_000_000,
                "cached_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
        )
        luna = credit_estimate(
            "gpt-5.6-luna",
            {
                "uncached_input_tokens": 1_000_000,
                "cached_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
        )
        self.assertEqual((terra["credits"], terra["eur"]), (355.0, 14.2))
        self.assertEqual((sol["credits"], sol["eur"]), (887.5, 35.5))
        self.assertEqual((luna["credits"], luna["eur"]), (35.5, 1.42))

    def test_authoritative_supported_models_produce_credits(self) -> None:
        for ordinal, model in enumerate(("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"), 1):
            with self.subTest(model=model):
                self._persist(ordinal, model=model)
                with open_storage(self.root) as connection:
                    estimate = connection.execute(
                        "SELECT estimated_credits FROM provider_invocations WHERE ordinal=?", (ordinal,)
                    ).fetchone()[0]
                self.assertIsNotNone(estimate)

    def test_normalizes_only_supported_codex_models(self) -> None:
        self.assertEqual(normalize_codex_model("GPT-5.6-Terra"), "gpt-5.6-terra")
        self.assertEqual(normalize_codex_model("gpt-5.6-sol"), "gpt-5.6-sol")
        self.assertEqual(normalize_codex_model("gpt-5.6-luna"), "gpt-5.6-luna")
        self.assertIsNone(normalize_codex_model("gpt-5.6-terra-preview"))
        self.assertIsNone(normalize_codex_model("terra"))

    def test_unknown_or_missing_model_never_produces_credits(self) -> None:
        usage = {
            "uncached_input_tokens": 1_000_000,
            "cached_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        }
        self.assertIsNone(credit_estimate(normalize_codex_model("future-model"), usage)["credits"])
        self.assertIsNone(credit_estimate(None, usage)["credits"])

    def test_persists_authoritative_raw_model_and_keeps_unknown_rate_unavailable(self) -> None:
        self._persist(1, model="gpt-5.6-terra")
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id="run-usage", ordinal=2, provider="codex_cli", model=None,
                model_authority=AUTHORITATIVE, raw_provider_model="gpt-99-future",
                phase="PROVIDER_EXECUTION", role="agent",
                started_at="2026-08-18T00:00:00+00:00",
                completed_at="2026-08-18T00:00:01+00:00", duration_ms=1000,
                usage={"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10},
            ),
        )
        with open_storage(self.root) as connection:
            rows = connection.execute(
                "SELECT model,model_authority,raw_provider_model,estimated_credits FROM provider_invocations ORDER BY ordinal"
            ).fetchall()
        self.assertEqual(
            rows[0][0:3], ("gpt-5.6-terra", AUTHORITATIVE, "gpt-5.6-terra")
        )
        self.assertEqual(rows[1][0:3], (None, AUTHORITATIVE, "gpt-99-future"))
        self.assertIsNone(rows[1][3])

    def test_incomplete_billing_usage_never_becomes_a_partial_credit_total(self) -> None:
        for usage in (
            {"output_tokens": 1_000_000},
            {"uncached_input_tokens": 1_000_000},
            {"uncached_input_tokens": 1_000_000, "output_tokens": 1_000_000},
            {"uncached_input_tokens": 1_000_000, "cached_input_tokens": 1_000_000},
        ):
            with self.subTest(usage=usage):
                estimate = credit_estimate("gpt-5.6-terra", usage)
                self.assertIsNone(estimate["credits"])
                self.assertIsNone(estimate["eur"])

    def test_speed_state_is_runtime_observed_or_unknown(self) -> None:
        self.assertEqual(speed_state({"configuration_profile": "Fast Mode"}), "FAST")
        self.assertEqual(speed_state({"speed_mode": "normal"}), "NORMAL_DEFAULT")
        self.assertEqual(speed_state({}), "UNKNOWN")
        self.assertEqual(speed_state({"note": "fast"}), "UNKNOWN")
        self.assertEqual(speed_state({"note": "default"}), "UNKNOWN")

    def test_churn_is_bounded_and_does_not_keep_command_output(self) -> None:
        line = '{"type":"item.completed","item":{"type":"command_execution","id":"c1","command":"pytest tests/test_x.py","exit_code":0,"aggregated_output":"ok"}}'
        churn = churn_from_jsonl(line)
        self.assertEqual(churn["shell_command_calls"], 1)
        self.assertEqual(churn["test_commands"], 1)
        self.assertEqual(churn["passing_test_output_bytes"], 2)
        self.assertEqual(churn["tool_loop_operations"], 1)

    def test_primary_tool_loop_fixture_reduces_redundant_operations_without_skipping_validation(self) -> None:
        def event(command: str, output: str = "") -> str:
            return json.dumps({"type": "item.completed", "item": {
                "type": "command_execution", "command": command,
                "exit_code": 0, "aggregated_output": output,
            }})

        required = (
            event("git status --short --branch", "## main"),
            event("sed -n '1,80p' src/engineering_platform/execution_host.py"),
            event("pytest tests/engineering/test_execution_host.py", "passed"),
            event("git diff --check"),
            event("git status --short --branch", "## feature"),
        )
        before = required[:2] + (event("git log --oneline --decorate", "x" * 300),) * 8 + required[2:] + (event("sed -n '1,80p' src/engineering_platform/execution_host.py"),) * 7
        after = required
        baseline = churn_from_jsonl("\n".join(before))
        optimized = churn_from_jsonl("\n".join(after))
        reduction = 1 - optimized["tool_loop_operations"] / baseline["tool_loop_operations"]
        self.assertGreaterEqual(reduction, 0.30)
        self.assertEqual(optimized["test_commands"], baseline["test_commands"])
        self.assertLess(optimized["file_read_count"], baseline["file_read_count"])
        self.assertLess(optimized["repeated_file_read_count"], baseline["repeated_file_read_count"])
        self.assertEqual(optimized["git_output_bytes"], len("## main") + len("## feature"))
        self.assertLess(optimized["git_output_bytes"], baseline["git_output_bytes"])
        self.assertLess(optimized["tool_output_bytes"], baseline["tool_output_bytes"])

    def test_comparable_fixture_shows_duplicate_logical_read_reduction(self) -> None:
        event = ('{"type":"item.completed","item":{"type":"command_execution",'
                 '"command":"sed -n \'1,120p\' src/engineering_platform/execution_host.py"}}')
        before = "\n".join((event,) * 49)
        after = "\n".join((event,) * 25)
        baseline = churn_from_jsonl(before)
        optimized = churn_from_jsonl(after)
        self.assertEqual(baseline["repeated_file_read_count"], 48)
        self.assertEqual(optimized["repeated_file_read_count"], 24)
        self.assertEqual(optimized["file_read_count"], 25)

    def test_current_codex_final_usage_is_one_cumulative_snapshot_not_context_size(self) -> None:
        output = '{"type":"turn.completed","usage":{"input_tokens":160,"cached_input_tokens":110,"output_tokens":9}}'
        snapshots = usage_snapshots_from_jsonl(output)
        self.assertEqual(snapshots, ({"input_tokens": 160, "cached_input_tokens": 110, "output_tokens": 9},))
        self.assertEqual(usage_from_jsonl(output), snapshots[0])

    def test_future_authoritative_snapshots_remain_counter_only(self) -> None:
        output = "\n".join((
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":70,"output_tokens":5}}',
            '{"type":"turn.completed","usage":{"input_tokens":160,"cached_input_tokens":110,"output_tokens":9}}',
        ))
        snapshots = usage_snapshots_from_jsonl(output)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(usage_from_jsonl(output), snapshots[-1])
        self.assertEqual(snapshots[-1]["input_tokens"] - snapshots[0]["input_tokens"], 60)
        self.assertEqual(
            usage_snapshots_from_jsonl('{"type":"item.completed","usage":{"input_tokens":999}}'),
            (),
        )

    def test_single_final_usage_snapshot_has_no_incremental_delta(self) -> None:
        self._persist(
            1,
            snapshots=({"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10},),
        )
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertEqual(summary["usage_snapshot_count"], 1)
        self.assertFalse(summary["intermediate_usage_delta_available"])
        self.assertIsNone(summary["maximum_incremental_input_tokens"])
        self.assertEqual(summary["actual_single_request_context_size"], "UNAVAILABLE")
        self.assertEqual(summary["active_context_size"], "UNAVAILABLE")

    def test_multiple_usage_snapshots_persist_only_counters_and_deltas(self) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id="run-usage", ordinal=1, provider="codex_cli", model=None,
                phase="PROVIDER_EXECUTION", role="agent", started_at="2026-08-18T00:00:00+00:00",
                completed_at="2026-08-18T00:00:01+00:00", duration_ms=1000,
                usage={"input_tokens": 160, "cached_input_tokens": 110, "output_tokens": 9},
                usage_snapshots=(
                    {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 5},
                    {"input_tokens": 160, "cached_input_tokens": 110, "output_tokens": 9},
                ),
            ),
        )
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertEqual(summary["usage_snapshot_count"], 2)
        self.assertEqual(summary["maximum_incremental_input_tokens"], 60)
        with open_storage(self.root) as connection:
            row = connection.execute(
                "SELECT input_delta,cached_input_delta,uncached_input_delta,output_delta FROM provider_usage_snapshots WHERE ordinal=2"
            ).fetchone()
        self.assertEqual(row, (60, 40, 20, 4))
        self.assertTrue(summary["intermediate_usage_delta_available"])

    def test_schema_is_migrated_transactionally(self) -> None:
        connection = open_storage(self.root)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM engineering_schema_migrations"
                ).fetchone()[0],
                ENGINEERING_STORAGE_SCHEMA_VERSION,
            )
            self.assertTrue(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_invocations'"
                ).fetchone()
            )
            self.assertTrue(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_usage_snapshots'"
                ).fetchone()
            )
        finally:
            connection.close()
