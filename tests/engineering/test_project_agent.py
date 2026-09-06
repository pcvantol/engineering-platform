from __future__ import annotations

import json
import io
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engineering_platform import project_agent


class ProjectAgentTests(unittest.TestCase):
    def test_identity_is_stable_for_one_host_user_context(self) -> None:
        host = project_agent.HostIdentity("host", "user", "Darwin", "arm64")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            first = project_agent.load_or_create_identity(host, path)
            second = project_agent.load_or_create_identity(host, path)
        self.assertEqual(first, second)
        self.assertEqual(first.identity_format, project_agent.IDENTITY_FORMAT)

    def test_identity_file_is_private(self) -> None:
        host = project_agent.HostIdentity("host", "user", "Darwin", "arm64")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            project_agent.load_or_create_identity(host, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @patch("engineering_platform.project_agent.subprocess.run")
    def test_inventory_supports_zero_one_and_many_explicit_roots(self, run: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").mkdir()
            (root / "two").mkdir()
            run.return_value = __import__("subprocess").CompletedProcess(("git",), 0, "true\n", "")
            self.assertEqual(project_agent.inventory_repositories(()), ())
            inventory = project_agent.inventory_repositories((root / "one", root / "two"))
        self.assertEqual(len(inventory), 2)
        self.assertTrue(all(item.is_git_repository for item in inventory))

    @patch("engineering_platform.project_agent.shutil.which", return_value="/usr/bin/git")
    @patch("engineering_platform.project_agent.subprocess.run")
    def test_discovery_only_reports_safe_local_tool_facts(self, run: object, _: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(("git", "--version"), 0, "git version 2.45\n", "")
        capability = project_agent.discover_tool("git")
        self.assertEqual(capability.name, "git")
        self.assertTrue(capability.available)
        self.assertEqual(capability.version, "git version 2.45")

    @patch("engineering_platform.project_agent.observe")
    def test_cli_emits_a_serializable_snapshot(self, observe: object) -> None:
        host = project_agent.HostIdentity("host", "user", "Linux", "x86_64")
        identity = project_agent.AgentIdentity("agent", project_agent.IDENTITY_FORMAT, host.context_key, "2026-01-01T00:00:00+00:00")
        observe.return_value = project_agent.AgentSnapshot(identity, project_agent.CapabilitySnapshot(host, project_agent.ToolCapability("git", False, None, None), (), ()), (), "2026-01-01T00:00:00+00:00")
        with patch("builtins.print") as printed:
            self.assertEqual(project_agent.main([]), 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["identity"]["agent_id"], "agent")
        self.assertEqual(payload["capabilities"]["host"]["context_key"], host.context_key)

    def test_service_cli_dispatches_only_to_installed_service_owner(self) -> None:
        output = io.StringIO()
        status = {"state": "RUNNING"}
        with patch("engineering_platform.project_agent_service.install", return_value={"state": "INSTALLED"}) as install, patch(
            "engineering_platform.project_agent_service.uninstall"
        ) as uninstall, patch("engineering_platform.project_agent_service.start") as start, patch(
            "engineering_platform.project_agent_service.stop"
        ) as stop, patch("engineering_platform.project_agent_service.status", return_value=status), patch(
            "engineering_platform.project_agent_service.run", return_value=7
        ) as run, redirect_stdout(output):
            for command in ("install", "uninstall", "start", "stop", "restart", "status"):
                self.assertEqual(project_agent.service_main([command]), 0)
            self.assertEqual(project_agent.service_main(["service", "run", "--config", "/tmp/agent.json"]), 7)
        install.assert_called_once()
        uninstall.assert_called_once(); self.assertGreaterEqual(start.call_count, 2); self.assertGreaterEqual(stop.call_count, 2)
        run.assert_called_once_with(Path("/tmp/agent.json"))
        self.assertIn('"state": "RUNNING"', output.getvalue())

    def test_agent_configuration_and_loopback_transport_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = root / "configuration.json"; invalid.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration is invalid"):
                project_agent._configuration(invalid)
            with self.assertRaisesRegex(ValueError, "non-loopback"):
                project_agent._post("https://example.invalid", "/v1/agent/pair", {})
            with patch("engineering_platform.project_agent.urlopen", side_effect=OSError("offline")):
                with self.assertRaisesRegex(ValueError, "rejected or unavailable"):
                    project_agent._post("http://127.0.0.1:8765", "/v1/agent/pair", {})
            with patch("engineering_platform.project_agent.Path.home", return_value=root), patch(
                "engineering_platform.project_agent.sys.platform", "darwin"
            ):
                self.assertIn("Application Support", str(project_agent.default_identity_path()))

    def test_agent_cli_delegates_supported_pair_register_heartbeat_and_attachment(self) -> None:
        identity = Path("/tmp/identity.json"); configuration = Path("/tmp/configuration.json")
        with patch("engineering_platform.project_agent.pair", return_value={"paired": "true"}) as pair, patch(
            "engineering_platform.project_agent.register", return_value={"state": "REGISTERED"}
        ) as register, patch("engineering_platform.project_agent.heartbeat", return_value={"state": "ONLINE"}) as heartbeat, patch(
            "engineering_platform.project_agent.attach", return_value={"state": "ATTACHED"}
        ) as attach, patch("builtins.print"):
            self.assertEqual(project_agent.main(["pair", "--server-endpoint", "http://127.0.0.1:8765", "--pairing-code", "code", "--identity-path", str(identity), "--configuration-path", str(configuration)]), 0)
            self.assertEqual(project_agent.main(["register", "--configuration-path", str(configuration)]), 0)
            self.assertEqual(project_agent.main(["heartbeat", "--configuration-path", str(configuration)]), 0)
            self.assertEqual(project_agent.main(["attach", "--repository-root", "/tmp/repository", "--configuration-path", str(configuration)]), 0)
        pair.assert_called_once(); register.assert_called_once(); heartbeat.assert_called_once(); attach.assert_called_once()


if __name__ == "__main__":
    unittest.main()
