from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from engineering_platform import development_profile, server


class DevelopmentProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "development data root with spaces"
        self.venv = self.base / "development venv with spaces"
        self.interpreter = self.venv / "bin" / "python"
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.symlink_to(Path(sys.executable))
        (self.venv / "pyvenv.cfg").write_text("home = test-only\n", encoding="utf-8")

    def tearDown(self) -> None:
        try:
            server.stop(self.root)
        except server.ServerConfigurationError:
            pass
        self.temporary.cleanup()

    def _arguments(
        self,
        command: str,
        root: Path | None = None,
        *,
        port: int = 8876,
        credential_reference: str = "development:keychain/test-profile",
        venv: Path | None = None,
    ) -> tuple[str, ...]:
        return (
            command,
            "--runtime-profile", "development",
            "--data-root", str(root or self.root),
            "--bind-port", str(port),
            "--development-venv", str(venv or self.venv),
            "--development-credential-reference", credential_reference,
        )

    def _main(self, arguments: tuple[str, ...]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with patch("engineering_platform.server.sys.executable", str(self.interpreter)), redirect_stdout(output):
            result = server.main(arguments)
        return result, json.loads(output.getvalue())

    def _initialize_profile(self) -> None:
        result, payload = self._main(self._arguments("init"))
        self.assertEqual(result, 0, payload)

    def test_explicit_development_init_creates_isolated_identity_and_storage(self) -> None:
        self._initialize_profile()

        marker = json.loads((self.root / development_profile.FILENAME).read_text(encoding="utf-8"))
        profile = server.status(self.root)["runtime_profile"]
        self.assertEqual(marker["profile"], "DEVELOPMENT")
        self.assertEqual(marker["data_root"], str(self.root.resolve()))
        self.assertEqual(marker["interpreter"], str(self.interpreter.absolute()))
        self.assertEqual(marker["bind_port"], 8876)
        self.assertTrue((self.root / server.SERVER_DATABASE_FILENAME).is_file())
        self.assertTrue((self.root / server.SERVER_IDENTITY_FILENAME).is_file())
        self.assertTrue((self.root / development_profile.LOG_DIRECTORY).is_dir())
        self.assertTrue((self.root / development_profile.CACHE_DIRECTORY).is_dir())
        self.assertEqual(profile["kind"], "DEVELOPMENT")
        self.assertEqual(profile["bind_port"], 8876)
        self.assertEqual(profile["credential_reference"], "CONFIGURED")
        self.assertNotIn("keychain/test-profile", json.dumps(profile))

    def test_development_process_rejects_production_data_root_through_a_symlink(self) -> None:
        operational = self.base / "operational data root"
        alias = self.base / "production root alias"
        operational.mkdir()
        alias.symlink_to(operational, target_is_directory=True)

        with patch("engineering_platform.server.platform_default_data_root", return_value=operational):
            result, payload = self._main(self._arguments("init", alias))

        self.assertEqual(result, 2)
        self.assertIn("operational data root", str(payload["error"]))
        self.assertFalse((operational / server.SERVER_CONFIGURATION_FILENAME).exists())
        self.assertFalse((operational / development_profile.FILENAME).exists())

    def test_existing_development_root_cannot_silently_run_as_operational(self) -> None:
        self._initialize_profile()

        output = io.StringIO()
        with redirect_stdout(output):
            result = server.main(("status", "--data-root", str(self.root)))

        self.assertEqual(result, 2)
        self.assertIn("--runtime-profile development", output.getvalue())

    def test_development_runtime_rejects_production_service_labels(self) -> None:
        self._initialize_profile()

        result, payload = self._main(self._arguments("service-install"))

        self.assertEqual(result, 2)
        self.assertIn("com.engineeringplatform.server", str(payload["error"]))
        self.assertFalse((self.root / "runtime" / "server-launchagent.err.log").exists())

    def test_development_runtime_rejects_inherited_operational_credentials_and_issuance(self) -> None:
        self._initialize_profile()
        with patch.dict(os.environ, {"EP_CONSUMER_TOKEN": "operational-token"}, clear=False):
            result, payload = self._main(self._arguments("status"))
        self.assertEqual(result, 2)
        self.assertIn("operational credentials", str(payload["error"]))

        result, payload = self._main(self._arguments("issue-consumer-credential") + (
            "--consumer-id", "development-consumer", "--project-id", "development-project",
        ))
        self.assertEqual(result, 2)
        self.assertIn("cannot issue", str(payload["error"]))

    def test_development_runtime_rejects_a_known_operational_interpreter(self) -> None:
        operational = self.base / "operational root"
        with patch("engineering_platform.server.platform_default_data_root", return_value=operational), patch(
            "engineering_platform.server.system_server_service.configured_service",
            return_value=server.system_server_service.SystemServerService(
                operational.resolve(), self.interpreter, "ep-server",
            ),
        ):
            result, payload = self._main(self._arguments("init"))

        self.assertEqual(result, 2)
        self.assertIn("operational interpreter", str(payload["error"]))
        self.assertFalse((self.root / development_profile.FILENAME).exists())

    def test_development_runtime_does_not_treat_a_legacy_launchagent_as_operational_authority(self) -> None:
        operational = self.base / "operational root"
        with patch("engineering_platform.server.platform_default_data_root", return_value=operational), patch(
            "engineering_platform.server.system_server_service.configured_service", return_value=None,
        ), patch(
            "engineering_platform.server.server_service.configured_interpreter", return_value=self.interpreter,
        ) as legacy:
            result, payload = self._main(self._arguments("init"))

        self.assertEqual(result, 0, payload)
        legacy.assert_not_called()

    def test_development_child_arguments_preserve_the_explicit_profile(self) -> None:
        self._initialize_profile()
        profile = development_profile.require(
            data_root=self.root,
            bind_port=8876,
            development_venv=self.venv,
            credential_reference="development:keychain/test-profile",
            interpreter=self.interpreter,
            operational_data_roots=(self.base / "operational",),
        )

        self.assertEqual(profile.server_arguments(), (
            "--runtime-profile", "development",
            "--development-venv", str(self.venv.absolute()),
            "--development-credential-reference", "development:keychain/test-profile",
            "--bind-port", "8876",
        ))

    def test_start_propagates_the_profile_to_its_child_process(self) -> None:
        self._initialize_profile()
        profile = development_profile.require(
            data_root=self.root,
            bind_port=8876,
            development_venv=self.venv,
            credential_reference="development:keychain/test-profile",
            interpreter=self.interpreter,
            operational_data_roots=(self.base / "operational",),
        )
        child = SimpleNamespace(pid=912_345)
        with patch("engineering_platform.server.status", side_effect=(
            {"running": False}, {"running": True},
        )), patch("engineering_platform.server.subprocess.Popen", return_value=child) as popen, patch(
            "engineering_platform.server.time.sleep",
        ):
            result = server.start(self.root, development=profile)
        server._CHILDREN.pop(child.pid, None)

        self.assertTrue(result["running"])
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[-8:], list(profile.server_arguments()))

    def test_profile_requires_a_non_operational_port_and_development_scoped_reference(self) -> None:
        result, payload = self._main(self._arguments("init", port=8765))
        self.assertEqual(result, 2)
        self.assertIn("operational bind port", str(payload["error"]))

        result, payload = self._main(self._arguments(
            "init", credential_reference="production:keychain/operational",
        ))
        self.assertEqual(result, 2)
        self.assertIn("development-scoped reference", str(payload["error"]))
        self.assertFalse((self.root / development_profile.FILENAME).exists())

    def test_profile_is_idempotent_but_refuses_a_changed_runtime_identity(self) -> None:
        self._initialize_profile()
        result, payload = self._main(self._arguments("init"))
        self.assertEqual(result, 0, payload)

        result, payload = self._main(self._arguments(
            "status", credential_reference="development:keychain/changed-profile",
        ))
        self.assertEqual(result, 2)
        self.assertIn("does not match", str(payload["error"]))

    def test_profile_rejects_nested_operational_and_unmarked_existing_roots(self) -> None:
        operational = self.base / "operational root"
        nested = operational / "development child"
        with patch("engineering_platform.server.platform_default_data_root", return_value=operational):
            result, payload = self._main(self._arguments("init", nested))
        self.assertEqual(result, 2)
        self.assertIn("operational data root", str(payload["error"]))

        self.root.mkdir()
        (self.root / server.SERVER_CONFIGURATION_FILENAME).write_text("{}", encoding="utf-8")
        result, payload = self._main(self._arguments("init"))
        self.assertEqual(result, 2)
        self.assertIn("not an empty isolated root", str(payload["error"]))

    def test_profile_uses_its_explicit_root_over_an_inherited_default_and_rejects_a_malformed_marker(self) -> None:
        operational = self.base / "inherited operational root"
        with patch.dict(os.environ, {"EP_SERVER_DATA_ROOT": str(operational)}, clear=False):
            result, payload = self._main(self._arguments("init"))
        self.assertEqual(result, 0, payload)
        self.assertTrue((self.root / development_profile.FILENAME).is_file())
        self.assertFalse(operational.exists())

        (self.root / development_profile.FILENAME).write_text("{}", encoding="utf-8")
        result, payload = self._main(self._arguments("status"))
        self.assertEqual(result, 2)
        self.assertIn("development profile is invalid", str(payload["error"]))
