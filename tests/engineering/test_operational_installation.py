from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.operational_installation import OperationalInstallationError, resolve, validate_health


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
