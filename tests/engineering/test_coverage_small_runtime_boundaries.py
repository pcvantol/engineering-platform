"""Public fail-closed contracts for small installed-runtime boundaries."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import provider_readiness, server_relay, validation_identity
from engineering_platform.evidence_projection import ToolProxyEnvironment


class SmallRuntimeBoundaryCoverageTests(unittest.TestCase):
    def test_validation_identity_rejects_malformed_and_controlled_transports(self) -> None:
        self.assertIsNone(validation_identity.canonical_validation_launcher(""))
        self.assertIsNone(validation_identity.canonical_validation_launcher("'unterminated"))
        self.assertFalse(validation_identity.is_canonical_dashboard_command("'unterminated"))
        self.assertFalse(validation_identity.is_canonical_dashboard_command("npm run test:engineering-dashboard ; echo unsafe"))
        self.assertFalse(validation_identity.is_canonical_dashboard_command("npm run test:engineering-dashboard --wrong"))

    def test_provider_readiness_fail_closed_for_missing_binaries_and_process_errors(self) -> None:
        failed = subprocess.CompletedProcess(("gh",), 1, "", "network timeout")
        denied = subprocess.CompletedProcess(("gh",), 1, "", "repository unavailable")
        self.assertEqual(provider_readiness._classify(None), "CHECK_FAILED")
        self.assertEqual(provider_readiness._repository_classify(None), "CHECK_FAILED")
        self.assertEqual(provider_readiness._repository_classify(failed), "CHECK_FAILED")
        self.assertEqual(provider_readiness._repository_classify(denied), "AUTH_REQUIRED")
        self.assertEqual(provider_readiness._version(None), "")
        self.assertEqual(provider_readiness._version(subprocess.CompletedProcess(("gh",), 0, "unversioned", "")), "")
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.provider_readiness.codex_cli_executable", return_value=""), patch(
            "engineering_platform.provider_readiness.shutil.which", return_value=None
        ):
            self.assertEqual(provider_readiness.runtime_details(Path(temporary)), {
                "codex": {"executable": "", "version": ""},
                "github": {"executable": "", "version": ""},
            })
            self.assertEqual(provider_readiness.status(Path(temporary))["github"]["state"], "UNAVAILABLE")

    def test_provider_readiness_treats_os_errors_as_check_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "engineering_platform.provider_readiness.CodexCliProvider"
        ) as codex, patch("engineering_platform.provider_readiness.shutil.which", return_value="/bin/gh"), patch(
            "engineering_platform.provider_readiness.LocalProcessProvider"
        ) as process:
            codex.return_value.status.return_value.qualified = True
            codex.return_value.command.side_effect = OSError("missing")
            process.return_value.execute.side_effect = OSError("missing")
            status = provider_readiness.host_status(Path(temporary))
        self.assertEqual(status["codex"]["state"], "CHECK_FAILED")
        self.assertEqual(status["github"]["state"], "CHECK_FAILED")

    def test_relay_build_fails_closed_without_compiler_or_on_compile_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("engineering_platform.server_relay.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Swift compiler is unavailable"):
                    server_relay.build_relay(root)
            with patch("engineering_platform.server_relay.shutil.which", return_value="/usr/bin/swiftc"), patch(
                "engineering_platform.server_relay.LocalProcessProvider"
            ) as process:
                process.return_value.execute.return_value = subprocess.CompletedProcess(("swiftc",), 1, "", "failed")
                with self.assertRaisesRegex(RuntimeError, "compilation failed"):
                    server_relay.build_relay(root)

    def test_evidence_proxy_fails_closed_for_missing_binary_and_invalid_escalation(self) -> None:
        with patch("engineering_platform.evidence_projection.shutil.which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "Evidence proxy could not resolve"):
                from engineering_platform.evidence_projection import proxy_main
                proxy_main("missing-tool")
        with ToolProxyEnvironment() as environment, patch.dict("os.environ", environment, clear=True), patch(
            "sys.argv", ["djconnect-context-escalate", "bad"]
        ):
            from engineering_platform.evidence_projection import context_escalation_main
            with self.assertRaises(SystemExit) as rejected:
                context_escalation_main()
        self.assertEqual(rejected.exception.code, 2)
