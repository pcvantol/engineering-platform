from __future__ import annotations
import unittest
from tools.engineering.validation_profile import classify

class ValidationProfileTests(unittest.TestCase):
    def test_document_only_avoids_browser_validation(self) -> None:
        profile = classify(["docs/engineering/EXECUTION_HOST_OPERATIONS.md", "docs/development/PLAN.md"])
        self.assertEqual(profile.tier, "DOCUMENTATION")

    def test_root_engineering_documents_remain_documentation_only(self) -> None:
        profile = classify(["BOOTSTRAP.md", "ENGINEERING_METHOD.md", "PROMPT_INITIALIZATION.md"])
        self.assertEqual(profile.tier, "DOCUMENTATION")

    def test_dashboard_requires_browser_validation(self) -> None:
        self.assertEqual(classify(["tools/engineering/dashboard.py"]).tier, "DASHBOARD")

    def test_unknown_or_mixed_scope_fails_closed_to_full_validation(self) -> None:
        self.assertEqual(classify(["docs/engineering/a.md", "custom_components/djconnect/__init__.py"]).tier, "FULL")
