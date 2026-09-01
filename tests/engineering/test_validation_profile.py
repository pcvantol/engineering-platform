from __future__ import annotations
import unittest
from engineering_platform.validation_profile import (
    ValidationProfileResolutionError, classify, profile_control_bindings,
    producer_profile_payload, resolve_producer_profile,
)

class ValidationProfileTests(unittest.TestCase):
    def test_document_only_avoids_browser_validation(self) -> None:
        profile = classify(["docs/engineering/EXECUTION_HOST_OPERATIONS.md", "docs/development/PLAN.md"])
        self.assertEqual(profile.tier, "DOCUMENTATION")

    def test_root_engineering_documents_remain_documentation_only(self) -> None:
        profile = classify(["BOOTSTRAP.md", "ENGINEERING_METHOD.md", "PROMPT_INITIALIZATION.md"])
        self.assertEqual(profile.tier, "DOCUMENTATION")

    def test_dashboard_requires_browser_validation(self) -> None:
        self.assertEqual(classify(["src/engineering_platform/dashboard.py"]).tier, "DASHBOARD")

    def test_unknown_or_mixed_scope_fails_closed_to_full_validation(self) -> None:
        self.assertEqual(classify(["docs/engineering/a.md", "custom_components/djconnect/__init__.py"]).tier, "FULL")

    def test_structured_validation_only_profile_uses_the_registry_control_set(self) -> None:
        profile, reference = resolve_producer_profile({
            "tier": "DASHBOARD", "version": "1.0",
            "required_controls": ["git_diff_check", "engineering_python", "dashboard_browser"],
        })
        self.assertEqual(profile.required_controls, ("git_diff_check", "engineering_python", "dashboard_browser"))
        self.assertEqual(reference, "validation-profile-registry:DASHBOARD@1.0")
        self.assertEqual(profile_control_bindings(profile)[-1]["command"], ["npm", "run", "test:engineering-dashboard"])

    def test_missing_or_substituted_profile_is_fail_closed(self) -> None:
        with self.assertRaises(ValidationProfileResolutionError):
            resolve_producer_profile(None)
        with self.assertRaises(ValidationProfileResolutionError):
            resolve_producer_profile({"tier": "DASHBOARD", "version": "1.0", "required_controls": ["dashboard_browser"]})

    def test_producer_payload_is_derived_only_from_the_canonical_registry(self) -> None:
        self.assertEqual(producer_profile_payload("DASHBOARD"), {
            "tier": "DASHBOARD", "version": "1.0",
            "required_controls": ["git_diff_check", "engineering_python", "dashboard_browser"],
        })
        with self.assertRaises(ValidationProfileResolutionError):
            producer_profile_payload("DASHBOARD ")
