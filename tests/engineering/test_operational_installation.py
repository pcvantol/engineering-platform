from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.operational_installation import OperationalInstallationError, package_identity, resolve, validate_health


class OperationalInstallationTests(unittest.TestCase):
    def _root(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory) / "EP Runtime With Spaces"
        root.mkdir()
        (root / "server.json").write_text(json.dumps({"version": "2.3.1"}))
        (root / "runtime-identity.json").write_text(json.dumps({"instance_id": "instance-1"}))
        interpreter = root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
        return root, interpreter

    def test_path_candidate_cannot_replace_service_selected_runtime_and_symlinks_normalize(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            alias = Path(directory) / "alias-python"
            alias.symlink_to(interpreter)
            candidate = Path(directory) / "old-path-python"
            candidate.write_text("#!/bin/sh\n"); candidate.chmod(0o755)
            installation = resolve(root, interpreter=alias, path_candidates=(candidate,))
            self.assertEqual(installation.interpreter, str(interpreter.resolve()))
            self.assertEqual(installation.path_candidates, (str(candidate.resolve()),))
            self.assertEqual(installation.data_root, str(root.resolve()))

    def test_runtime_and_health_must_belong_to_the_selected_instance(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            installation = resolve(root, interpreter=interpreter)
            validate_health(installation, {"service": "engineering-platform-server", "instance_id": "instance-1", "healthy": True})
            with self.assertRaisesRegex(OperationalInstallationError, "does not identify"):
                validate_health(installation, {"service": "engineering-platform-server", "instance_id": "wrong", "healthy": True})
            (root / "runtime.json").write_text(json.dumps({"pid": 9, "instance_id": "wrong"}))
            with self.assertRaisesRegex(OperationalInstallationError, "different instance"):
                resolve(root, interpreter=interpreter)

    def test_rejects_malformed_facts_and_wrong_health_shapes(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            (root / "server.json").write_text("[]")
            with self.assertRaisesRegex(OperationalInstallationError, "invalid"):
                resolve(root, interpreter=interpreter)
            (root / "server.json").write_text(json.dumps({"version": "2.3.1"}))
            (root / "runtime.json").write_text(json.dumps({"pid": "bad"}))
            with self.assertRaisesRegex(OperationalInstallationError, "PID"):
                resolve(root, interpreter=interpreter)
            (root / "runtime.json").unlink()
            installation = resolve(root, interpreter=interpreter)
            with self.assertRaisesRegex(OperationalInstallationError, "does not identify"):
                validate_health(installation, {"service": "other", "instance_id": "instance-1", "healthy": True})
            with self.assertRaisesRegex(OperationalInstallationError, "not healthy"):
                validate_health(installation, {"service": "engineering-platform-server", "instance_id": "instance-1", "healthy": False})
            interpreter.unlink()
            with self.assertRaisesRegex(OperationalInstallationError, "interpreter"):
                resolve(root, interpreter=interpreter)

    def test_selected_interpreter_package_identity_is_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            _, interpreter = self._root(directory)
            process = __import__("subprocess")
            good = process.CompletedProcess((), 0, json.dumps({"interpreter": str(interpreter), "version": "2.3.1", "metadata": "/metadata", "package": "/package"}), "")
            self.assertEqual(package_identity(interpreter, runner=lambda *_a, **_k: good)["version"], "2.3.1")
            for result, marker in ((process.CompletedProcess((), 1, "", ""), "does not provide"), (process.CompletedProcess((), 0, "[]", ""), "incomplete"), (process.CompletedProcess((), 0, "{}", ""), "incomplete")):
                with self.subTest(marker=marker), self.assertRaisesRegex(OperationalInstallationError, marker):
                    package_identity(interpreter, runner=lambda *_a, **_k: result)
