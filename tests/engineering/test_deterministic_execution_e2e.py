"""Regression tests for the installed deterministic execution qualification."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


def _qualification_module() -> object:
    path = Path(__file__).parents[2] / "tools" / "qualification" / "p_deterministic_execution_e2e.py"
    specification = importlib.util.spec_from_file_location("deterministic_execution_e2e", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("qualification module is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DeterministicExecutionE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _qualification_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "central"
        self.data.mkdir()
        self.configuration = self.data / "server.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_isolated_runtime_is_executable_and_becomes_server_runtime_authority(self) -> None:
        self.configuration.write_text(json.dumps({
            "version": 3,
            "bind_host": "127.0.0.1",
            "bind_port": 8765,
            "managed_codex_cli_prefix": "/unavailable/production-runtime",
            "product_version": "2.3.2",
        }), encoding="utf-8")

        executable = self.module.configure_deterministic_runtime(self.data, self.root)

        self.assertEqual(
            subprocess.run((str(executable), "--version"), check=True, text=True, capture_output=True).stdout.strip(),
            f"codex {self.module.QUALIFICATION_RUNTIME_VERSION}",
        )
        configuration = json.loads(self.configuration.read_text(encoding="utf-8"))
        self.assertEqual(configuration["managed_codex_cli_prefix"], str(executable.parent.parent.resolve()))
        self.assertEqual(executable, self.root / "managed-codex-cli" / "bin" / "codex")

    def test_isolated_runtime_rejects_an_unexpected_server_configuration_shape(self) -> None:
        self.configuration.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "QUALIFICATION_SERVER_CONFIGURATION_INVALID"):
            self.module.configure_deterministic_runtime(self.data, self.root)

    def test_managed_fixture_has_a_real_suite_without_dirtying_its_candidate(self) -> None:
        repository = self.root / "managed"
        self.module.create_repository(repository)

        completed = subprocess.run(
            ("python3", "-m", "unittest", "discover"), cwd=repository,
            check=False, text=True, capture_output=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            subprocess.run(
                ("git", "-C", str(repository), "status", "--porcelain"),
                check=True, text=True, capture_output=True,
            ).stdout,
            "",
        )


if __name__ == "__main__":
    unittest.main()
