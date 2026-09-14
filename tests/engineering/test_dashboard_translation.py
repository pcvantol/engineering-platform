from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from engineering_platform import dashboard_translation


class DashboardTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_translation._cache.clear()

    def test_dynamic_evidence_is_translated_by_the_managed_codex_runtime(self) -> None:
        source = "No changed behavior or executable test surface exists."
        translated = "Er is geen gewijzigd gedrag of uitvoerbaar testoppervlak."
        event = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps({"translations": [translated]})},
        })
        completed = subprocess.CompletedProcess(("codex",), 0, event, "")
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=completed) as invoke:
            self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])
        arguments = invoke.call_args.args[1]
        self.assertIn("read-only", arguments)
        self.assertIn("--output-schema", arguments)
        self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])
        self.assertEqual(invoke.call_count, 1)

    def test_english_evidence_is_returned_without_a_provider_call(self) -> None:
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke") as invoke:
            self.assertEqual(dashboard_translation.translate("en", ["Recorded validation passed."]), ["Recorded validation passed."])
        invoke.assert_not_called()

    def test_request_is_bounded_and_rejects_unknown_locales(self) -> None:
        with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "LOCALE_INVALID"):
            dashboard_translation.translate("pt", ["text"])
        with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "REQUEST_INVALID"):
            dashboard_translation.translate("nl", ["x" * (dashboard_translation.MAX_TEXT_LENGTH + 1)])

    def test_long_evidence_and_expanded_translation_are_preserved(self) -> None:
        source = "évidence with unicode and multiple paragraphs\n\n" + ("complete source text " * 70)
        translated = "vertaalde evidence\n\n" + ("volledige vertaalde tekst " * 120)
        event = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps({"translations": [translated]})},
        })
        completed = subprocess.CompletedProcess(("codex",), 0, event, "")
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=completed):
            self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])

    def test_provider_failure_is_a_safe_display_failure(self) -> None:
        with patch(
            "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
            side_effect=OSError("runtime unavailable"),
        ):
            with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "UNAVAILABLE"):
                dashboard_translation.translate("nl", ["Recorded validation passed."])

    def test_non_object_events_and_invalid_payloads_fail_safely_without_caching(self) -> None:
        for output in ("[]", "null", "42", "true", '"event"', "{broken", "[" * 1100 + "]" * 1100, json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": "{}"},
        })):
            with self.subTest(output=output), patch(
                "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
                return_value=subprocess.CompletedProcess(("codex",), 0, output, ""),
            ):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "^DASHBOARD_TRANSLATION_OUTPUT_INVALID$"):
                    dashboard_translation.translate("nl", ["Source evidence"])
                self.assertEqual(dashboard_translation._cache, {})

    def test_output_requires_valid_cardinality_types_and_translation_lengths(self) -> None:
        for translations in ([], ["one", "two"], [None], [2], [" "], ["x" * (dashboard_translation.MAX_TRANSLATION_LENGTH + 1)]):
            event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"translations": translations})}})
            with self.subTest(translations=repr(translations)[:80]), patch(
                "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
                return_value=subprocess.CompletedProcess(("codex",), 0, event, ""),
            ):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "OUTPUT_INVALID"):
                    dashboard_translation.translate("nl", ["Source evidence"])
                self.assertEqual(dashboard_translation._cache, {})

    def test_excessively_nested_final_message_fails_safely(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "[" * 1100 + "]" * 1100}})
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=subprocess.CompletedProcess(("codex",), 0, event, "")):
            with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "OUTPUT_INVALID"):
                dashboard_translation.translate("nl", ["Source evidence"])

    def test_unrelated_non_object_events_do_not_hide_a_valid_final_message(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"translations": ["Bronbewijs"]})}})
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=subprocess.CompletedProcess(
            ("codex",), 0, "null\n" + event + "\n[]\nnot json", "",
        )):
            self.assertEqual(dashboard_translation.translate("nl", ["Source evidence"]), ["Bronbewijs"])

    def test_current_response_retains_valid_translations_during_concurrent_cache_eviction(self) -> None:
        dashboard_translation._cache[("nl", "Earlier source")] = "Eerdere bron"

        def translate_while_another_request_fills_cache(*_: object) -> tuple[str, ...]:
            with dashboard_translation._lock:
                dashboard_translation._cache.clear()
                for index in range(dashboard_translation.MAX_CACHE_ENTRIES):
                    dashboard_translation._cache[("nl", f"other-{index}")] = f"ander-{index}"
            return ("Nieuwe bron",)

        with patch.object(dashboard_translation, "_translate_missing", side_effect=translate_while_another_request_fills_cache):
            self.assertEqual(dashboard_translation.translate("nl", ["Earlier source", "New source"]), ["Eerdere bron", "Nieuwe bron"])
        self.assertEqual(len(dashboard_translation._cache), dashboard_translation.MAX_CACHE_ENTRIES)
