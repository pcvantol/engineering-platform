from __future__ import annotations

import ast
from pathlib import Path
import unittest

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
        self.assertIn("_admit_server_owned_file_inbox", server_source)
        self.assertIn("file_inbox.FileInboxService", server_source)

    def test_supported_runtime_modules_do_not_import_the_retired_watcher(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        supported = ("server.py", "dashboard.py", "parity_lifecycle_dispatcher.py", "emergency_recovery.py")
        for name in supported:
            source = (source_root / name).read_text(encoding="utf-8")
            self.assertNotIn("inbox_watcher", source, name)

    def test_retired_root_bound_human_producer_paths_are_absent(self) -> None:
        """Human files enter only through Server-owned File Inbox intake."""
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        for retired in ("workspace_inbox_api.py", "human_text_ingress.py"):
            self.assertFalse((source_root / retired).exists(), retired)
        # Forensic attribution deliberately retains historical path strings as
        # evidence; this guard covers executable ingress/runtime modules.
        for name in ("server.py", "file_inbox.py", "submission_intake.py", "submission_cli.py", "inbox_watcher.py"):
            source_path = source_root / name
            source = source_path.read_text(encoding="utf-8")
            self.assertNotIn("workspace_inbox_api", source, source_path.name)
            self.assertNotIn("human_text_ingress", source, source_path.name)
