from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from engineering_platform.provider_process_identity import ProcessIdentity, capture_process_identity, verify_process_identity


class ProviderProcessIdentityTests(unittest.TestCase):
    def test_current_host_process_identity_matches_then_becomes_inactive(self) -> None:
        child = subprocess.Popen((sys.executable, "-c", "import sys; sys.stdin.read()"), stdin=subprocess.PIPE)
        try:
            # On macOS a Python child can briefly report its launcher before
            # its framework executable. Capture the stable process identity.
            time.sleep(0.05)
            identity = capture_process_identity(child.pid, os.getpgid(child.pid))
            self.assertIsNotNone(identity)
            assert identity is not None
            self.assertEqual(verify_process_identity(identity), "MATCH")
        finally:
            if child.stdin is not None:
                child.stdin.close()
            child.wait(timeout=5)
        self.assertEqual(verify_process_identity(identity), "NOT_ACTIVE")

    def test_capture_rejects_pid_reuse_group_mismatch_and_malformed_ps_evidence(self) -> None:
        self.assertIsNone(capture_process_identity(0))
        with patch("engineering_platform.provider_process_identity.os.getpgid", return_value=9):
            self.assertIsNone(capture_process_identity(3, 7))
        result = type("Result", (), {"returncode": 0, "stdout": "not enough fields"})()
        with patch("engineering_platform.provider_process_identity.os.getpgid", return_value=7), patch(
            "engineering_platform.provider_process_identity.subprocess.run", return_value=result
        ):
            self.assertIsNone(capture_process_identity(3, 7))
        expected = ProcessIdentity(3, 7, "birth-a", "/bin/a")
        with patch("engineering_platform.provider_process_identity.capture_process_identity", return_value=ProcessIdentity(3, 7, "birth-b", "/bin/a")):
            self.assertEqual(verify_process_identity(expected), "MISMATCH")
