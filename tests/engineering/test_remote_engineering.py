from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.repository_handoff import publish as publish_handoff
from tools.engineering.status_model import build, publish
from tools.engineering.platform_version import EngineeringPlatformManifest


class RemoteEngineeringTest(unittest.TestCase):
    def test_status_projections_are_atomic_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = EngineeringPlatformManifest(
                "1.4.0", "1.4.0", "2026.11", 1, 2, 2, "0.146.0", "1.0.0", 1, "1.0.0", 1, 1
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
