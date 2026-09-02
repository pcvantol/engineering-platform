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
            dashboard_translation.translate("nl", ["x" * 241])

    def test_provider_failure_is_a_safe_display_failure(self) -> None:
        with patch(
            "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
            side_effect=OSError("runtime unavailable"),
        ):
            with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "UNAVAILABLE"):
                dashboard_translation.translate("nl", ["Recorded validation passed."])
