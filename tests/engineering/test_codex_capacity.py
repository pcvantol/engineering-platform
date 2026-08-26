from __future__ import annotations

import unittest

from tools.engineering.codex_capacity import normalize_rate_limits, remaining_percent


class CodexCapacityTest(unittest.TestCase):
    def test_remaining_capacity_uses_the_lowest_safe_quota_window(self) -> None:
        limits = normalize_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 72},
                    "secondary": {"usedPercent": 81},
                },
                "account": {"email": "must-not-leak@example.invalid"},
            }
        )
        self.assertEqual(remaining_percent(limits), 19)

    def test_missing_or_invalid_capacity_is_not_treated_as_available(self) -> None:
        self.assertEqual(normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": True}}}), {})
        self.assertIsNone(remaining_percent({}))
