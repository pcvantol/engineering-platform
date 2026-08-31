from __future__ import annotations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from engineering_platform import capability_preflight
from engineering_platform import provider_readiness
from engineering_platform.dashboard_configuration import update as update_dashboard_configuration


class CapabilityPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        target = self.root / "src/engineering_platform"
        target.mkdir(parents=True)
        source = (
            Path(__file__).resolve().parents[2]
            / "src/engineering_platform/ENGINEERING_PLATFORM_VERSION.json"
        )
        (target / "ENGINEERING_PLATFORM_VERSION.json").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (self.root / ".engineering/status").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_compatible_declaration_is_deterministic(self) -> None:
        prompt = "Execution Host Version: 2.0.0\nRunner Version: 2.0.0\nEngineering Database Schema: 6\nRequired Capabilities: capability_preflight\n"
        with patch("engineering_platform.capability_preflight.provider_readiness_failures", return_value=()):
            first = capability_preflight.execute(self.root, prompt, run_id="inbox-one")
            second = capability_preflight.execute(self.root, prompt, run_id="inbox-two")
        self.assertEqual(first.outcome, "PASS")
        self.assertEqual(first.recoverability, "RETRYABLE")
        self.assertEqual(
            [c.identifier for c in first.checks], [c.identifier for c in second.checks]
        )
        self.assertEqual(capability_preflight.latest(self.root)["run_id"], "inbox-two")

    def test_unsupported_requirement_fails_closed_with_evidence(self) -> None:
        with patch("engineering_platform.capability_preflight.provider_readiness_failures", return_value=()):
            result = capability_preflight.execute(
                self.root, "Report Format: 99\nRequired Provider Support: sqlite\n"
            )
        self.assertEqual(result.outcome, "FAIL")
        self.assertEqual(result.failure_origin, "CAPABILITY")
        self.assertEqual(result.recoverability, "RETRYABLE_AFTER_HOST_REPAIR")
        self.assertIn(
            "report_format",
            {check.identifier for check in result.checks if check.outcome == "FAIL"},
        )
        self.assertIn(
            "provider_support",
            {check.identifier for check in result.checks if check.outcome == "FAIL"},
        )

    def test_provider_readiness_blocks_managed_admission_before_execution(self) -> None:
        with patch("engineering_platform.capability_preflight.provider_readiness_failures", return_value=("CODEX", "GITHUB")):
            result = capability_preflight.execute(self.root, "Execution Mode: MANAGED\n")
        failed = {check.identifier: check for check in result.checks if check.outcome == "FAIL"}
        self.assertIn("provider_readiness", failed)
        self.assertIn("CODEX, GITHUB", failed["provider_readiness"].reason)

    def test_capacity_reserve_blocks_only_new_admission_when_remaining_capacity_is_low(self) -> None:
        update_dashboard_configuration(self.root, "codex_capacity_reserve_percent", 25)
        with patch("engineering_platform.capability_preflight.provider_readiness_failures", return_value=()), patch(
            "engineering_platform.capability_preflight.read_remaining_percent", return_value=24
        ):
            result = capability_preflight.execute(self.root, "Execution Mode: MANAGED\n")
        failed = {check.identifier: check for check in result.checks if check.outcome == "FAIL"}
        self.assertIn("codex_capacity_reserve", failed)
        self.assertIn("24% remaining", failed["codex_capacity_reserve"].reason)

    def test_capacity_reserve_allows_admission_at_the_configured_boundary(self) -> None:
        update_dashboard_configuration(self.root, "codex_capacity_reserve_percent", 25)
        with patch("engineering_platform.capability_preflight.provider_readiness_failures", return_value=()), patch(
            "engineering_platform.capability_preflight.read_remaining_percent", return_value=25
        ):
            result = capability_preflight.execute(self.root, "Execution Mode: MANAGED\n")
        self.assertEqual(result.outcome, "PASS")

    def test_github_readiness_requires_repository_access_after_authentication(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with patch("engineering_platform.provider_readiness.shutil.which", return_value="/usr/local/bin/gh"), patch(
            "engineering_platform.provider_readiness.CodexCliProvider"
        ) as codex, patch("engineering_platform.provider_readiness.LocalProcessProvider") as process:
            codex.return_value.status.return_value.qualified = True
            codex.return_value.command.return_value = completed(("codex",), 0, "", "")
            process.return_value.execute.side_effect = [
                completed(("gh", "auth"), 0, "", ""),
                completed(("gh", "api"), 1, "", "repository access denied"),
            ]
            status = provider_readiness.status(self.root)
        self.assertEqual(status["github"]["state"], "AUTH_REQUIRED")

    def test_github_readiness_uses_rest_repository_access_after_authentication(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with patch("engineering_platform.provider_readiness.shutil.which", return_value="/usr/local/bin/gh"), patch(
            "engineering_platform.provider_readiness.CodexCliProvider"
        ) as codex, patch("engineering_platform.provider_readiness.LocalProcessProvider") as process:
            codex.return_value.status.return_value.qualified = True
            codex.return_value.command.return_value = completed(("codex",), 0, "", "")
            process.return_value.execute.side_effect = [
                completed(("gh", "auth"), 0, "", ""),
                completed(("gh", "api"), 0, "pcvantol/djconnect\n", ""),
            ]

            status = provider_readiness.status(self.root)

        self.assertEqual(status["github"]["state"], "READY")
        self.assertEqual(
            process.return_value.execute.call_args_list[1].args[1],
            ("gh", "api", "repos/{owner}/{repo}", "--jq", ".full_name"),
        )

    def test_github_repository_rate_limit_is_not_presented_as_reauthentication(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with patch("engineering_platform.provider_readiness.shutil.which", return_value="/usr/local/bin/gh"), patch(
            "engineering_platform.provider_readiness.CodexCliProvider"
        ) as codex, patch("engineering_platform.provider_readiness.LocalProcessProvider") as process:
            codex.return_value.status.return_value.qualified = True
            codex.return_value.command.return_value = completed(("codex",), 0, "", "")
            process.return_value.execute.side_effect = [
                completed(("gh", "auth"), 0, "", ""),
                completed(("gh", "api"), 1, "", "API rate limit exceeded"),
            ]

            status = provider_readiness.status(self.root)

        self.assertEqual(status["github"]["state"], "CHECK_FAILED")

    def test_codex_not_logged_in_is_an_explicit_authentication_repair(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with patch("engineering_platform.provider_readiness.CodexCliProvider") as codex:
            codex.return_value.status.return_value.qualified = True
            codex.return_value.command.return_value = completed(
                ("codex", "login", "status"), 1, "Not logged in\n", ""
            )
            status = provider_readiness.status(self.root, require_github=False)
        self.assertEqual(status["codex"]["state"], "AUTH_REQUIRED")
