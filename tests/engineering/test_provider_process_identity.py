from __future__ import annotations

import os
import subprocess
import sys
import unittest

from tools.engineering.provider_process_identity import capture_process_identity, verify_process_identity


class ProviderProcessIdentityTests(unittest.TestCase):
    def test_current_host_process_identity_matches_then_becomes_inactive(self) -> None:
        child = subprocess.Popen((sys.executable, "-c", "import sys; sys.stdin.read()"), stdin=subprocess.PIPE)
        try:
            identity = capture_process_identity(child.pid, os.getpgid(child.pid))
            self.assertIsNotNone(identity)
            assert identity is not None
            self.assertEqual(verify_process_identity(identity), "MATCH")
        finally:
            if child.stdin is not None:
                child.stdin.close()
            child.wait(timeout=5)
        self.assertEqual(verify_process_identity(identity), "NOT_ACTIVE")
