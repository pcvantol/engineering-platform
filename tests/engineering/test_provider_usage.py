from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.engineering.provider_usage import (
    AUTHORITATIVE,
    ProviderInvocation,
    churn_from_jsonl,
    credit_estimate,
    persist_provider_invocation,
    provider_usage_summary,
    normalize_codex_model,
    speed_state,
)
from tools.engineering.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage


class ProviderUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _persist(
        self, ordinal: int, *, model: str = "gpt-5.6-terra", usage: dict[str, int] | None = None
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
                role="agent",
                started_at="2026-08-18T00:00:00+00:00",
                completed_at="2026-08-18T00:00:01+00:00",
                duration_ms=1000,
                usage=usage
                if usage is not None
                else {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10},
                runtime_metadata={"configuration_profile": "normal"},
                churn={"file_read_count": ordinal, "tool_output_bytes": 4},
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

    def test_missing_usage_stays_unavailable(self) -> None:
        self._persist(1, usage={})
        summary = provider_usage_summary(self.root, "run-usage")
        self.assertIsNone(summary["input_tokens"])
        self.assertEqual(summary["usage_authority"], "UNAVAILABLE")

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
        finally:
            connection.close()
