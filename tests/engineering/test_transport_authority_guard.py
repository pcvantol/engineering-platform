from __future__ import annotations

import ast
from pathlib import Path
import unittest

from engineering_platform import inbox_watcher


class TransportAuthorityGuardTest(unittest.TestCase):
    """Source-level canaries for the supported ingress authority boundary."""

    def test_supported_adapters_have_no_local_operational_authority(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        for name in ("submission_cli.py", "file_inbox.py"):
            source = (source_root / name).read_text(encoding="utf-8")
            self.assertNotIn("sqlite3", source, name)
            self.assertNotIn("StateStore", source, name)
            self.assertNotIn("EngineeringRunner", source, name)
            self.assertNotIn("inbox_watcher", source, name)
        file_tree = ast.parse((source_root / "file_inbox.py").read_text(encoding="utf-8"))
        imported = {node.names[0].name for node in ast.walk(file_tree) if isinstance(node, ast.Import) and node.names}
        self.assertNotIn("sqlite3", imported)

    def test_supported_transport_paths_converge_at_canonical_submission_service(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        server_source = (source_root / "server.py").read_text(encoding="utf-8")
        self.assertIn("submission_service.request_from_mapping", server_source)
        self.assertIn("submission_service.submit(connection, request)", server_source)
        self.assertEqual(inbox_watcher.RETIRED_OPERATIONAL_COMMANDS, frozenset({"once", "run", "install"}))
