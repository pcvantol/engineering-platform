from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class ZeroLossCapabilityAuditTests(unittest.TestCase):
    def test_catalog_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, str(root / "scripts/engineering/validate_zero_loss_capability_audit.py")],
            cwd=root,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
