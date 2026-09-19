from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from engineering_platform.repository_handoff import main as handoff_main, publish as publish_handoff
from engineering_platform.status_model import build, publish
from engineering_platform.platform_version import EngineeringPlatformManifest


class RemoteEngineeringTest(unittest.TestCase):
    def test_status_projections_are_atomic_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = EngineeringPlatformManifest(
                "1.4.0", "1.4.0", "2026.11", 1, 2, 2, "0.146.0", "1.0.0", 1, "1.0.0", 1, 1, 1
            )
            root = Path(temporary)
            publish(root, build(manifest, diagnostic="token=secret", queue_depth=2))
            self.assertIn("[REDACTED]", (root / "status.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "status.md").is_file())

    def test_handoff_has_deterministic_discovery_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = publish_handoff(
                root,
                run_id="remote-1",
                platform_version="1.4.0",
                implementation_pr=1,
                finalization_pr=2,
            )
            self.assertTrue(handoff.is_file())
            self.assertIn(
                "remote-1", (root / "docs/engineering/runs/index.json").read_text(encoding="utf-8")
            )
            records = [handoff.read_text(encoding="utf-8"),
                       (root / "docs/engineering/runs/latest.md").read_text(encoding="utf-8")]
            index = json.loads((root / "docs/engineering/runs/index.json").read_text(encoding="utf-8"))
            for record in records:
                self.assertIn("FINALIZATION_PR_OPEN", record)
                self.assertIn("FINALIZATION_PENDING", record)
                self.assertNotIn("MERGED_RECONCILED", record)
                self.assertNotIn("WORKSPACE_READY", record)
                self.assertNotIn("- Completed:", record)
            self.assertEqual(index["repository_state"], "FINALIZATION_PR_OPEN")
            self.assertEqual(index["workspace_state"], "FINALIZATION_PENDING")
            self.assertIn("prepared_at", index)
            self.assertNotIn("completed_at", index)

    def test_handoff_command_writes_records_to_the_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exit_code = handoff_main([
                "--root", str(root), "--run-id", "remote-2", "--platform-version", "2.0.0",
                "--implementation-pr", "3", "--finalization-pr", "4",
            ])
            self.assertEqual(exit_code, 0)
            self.assertIn("remote-2", (root / "docs/engineering/runs/latest.md").read_text(encoding="utf-8"))
