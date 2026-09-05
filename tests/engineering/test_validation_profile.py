from __future__ import annotations
import unittest
from engineering_platform.validation_profile import (
    ValidationProfileResolutionError, browser_dashboard_required, classify, localization_required, phase_for_branch, profile_control_bindings,
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
        profile = classify(["src/engineering_platform/server.py"])
        self.assertEqual(profile.tier, "DASHBOARD")
        self.assertTrue(browser_dashboard_required(profile))

    def test_governed_central_core_branch_defers_only_dashboard_browser(self) -> None:
        self.assertEqual(phase_for_branch("codex/phase-p-central-core-completion"), "P_CENTRAL_CORE")
        profile = classify(["src/engineering_platform/execution_host.py"], governed_phase="P_CENTRAL_CORE")
        self.assertEqual(profile.tier, "P_CENTRAL_CORE")
        self.assertFalse(browser_dashboard_required(profile))
        self.assertEqual(profile.required_controls, ("git_diff_check", "engineering_python"))

    def test_console_and_unrelated_runtime_branches_retain_browser_requirement(self) -> None:
        self.assertIsNone(phase_for_branch("codex/phase-p-central-console"))
        self.assertTrue(browser_dashboard_required(classify(["src/engineering_platform/server.py"])))
        self.assertTrue(browser_dashboard_required(classify(["src/engineering_platform/execution_host.py"])))

    def test_console_assets_and_server_projection_require_localization_gate(self) -> None:
        self.assertTrue(localization_required(classify(["src/engineering_platform/assets/dashboard.js"])))
        self.assertTrue(localization_required(classify(["src/engineering_platform/server.py", "src/engineering_platform/storage.py"])))
        self.assertFalse(localization_required(classify(["src/engineering_platform/storage.py"])))

    def test_unknown_or_mixed_scope_fails_closed_to_full_validation(self) -> None:
        self.assertEqual(classify(["docs/engineering/a.md", "custom_components/djconnect/__init__.py"]).tier, "FULL")

    def test_structured_validation_only_profile_uses_the_registry_control_set(self) -> None:
        profile, reference = resolve_producer_profile({
            "tier": "DASHBOARD", "version": "1.0",
            "required_controls": ["git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"],
        })
        self.assertEqual(profile.required_controls, ("git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"))
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
            "required_controls": ["git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"],
        })
        with self.assertRaises(ValidationProfileResolutionError):
            producer_profile_payload("DASHBOARD ")
