"""Real owned-process cancellation boundaries for a PA-E3 Action."""
from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from engineering_platform import parallel_action_recovery as recovery
from engineering_platform.execution_errors import CodexInvocationError, RunnerError
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.provider_process_identity import ProcessIdentity


class _SleepingProvider:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None

    def spawn_invocation(self, *_: object, **__: object) -> subprocess.Popen[str]:
        self.process = subprocess.Popen(
            (sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return self.process


class ParallelActionRecoveryClientTest(unittest.TestCase):
    def test_cancel_before_provider_launch_uses_no_repair_attempt(self) -> None:
        provider = _SleepingProvider()
        client = CodexCliClient(provider=provider)  # type: ignore[arg-type]
        client.set_cancellation_check(lambda: True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodexInvocationError) as raised:
                client._run_invocation(("codex", "exec"), Path(temporary))
        self.assertEqual(raised.exception.terminal_condition, "operator_cancellation")
        self.assertFalse(raised.exception.provider_turn_interrupted)
        self.assertIsNone(provider.process)
        self.assertTrue(client.cancellation_observed())
        self.assertTrue(client.provider_process_cleanup_confirmed())

    def test_cancel_during_provider_stops_only_owned_process_group(self) -> None:
        provider = _SleepingProvider()
        client = CodexCliClient(provider=provider)  # type: ignore[arg-type]
        requested = threading.Event()
        client.set_cancellation_check(requested.is_set)
        timer = threading.Timer(.2, requested.set)
        timer.start()
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(CodexInvocationError) as raised:
                    client._run_invocation(("codex", "exec"), Path(temporary))
        finally:
            timer.cancel()
            if provider.process is not None and provider.process.poll() is None:
                provider.process.kill()
                provider.process.wait(timeout=5)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(raised.exception.terminal_condition, "operator_cancellation")
        self.assertFalse(raised.exception.provider_turn_interrupted)
        self.assertIsNotNone(provider.process)
        self.assertIsNotNone(provider.process.poll())
        self.assertTrue(client.cancellation_observed())
        self.assertTrue(client.provider_process_cleanup_confirmed())

    def test_cancellation_status_failure_stops_owned_provider_fail_closed(self) -> None:
        provider = _SleepingProvider()
        client = CodexCliClient(provider=provider)  # type: ignore[arg-type]
        probes = 0

        def failing_probe() -> bool:
            nonlocal probes
            probes += 1
            if probes > 1:
                raise RuntimeError("central read failed")
            return False

        client.set_cancellation_check(failing_probe)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(RunnerError, "PA_E3_CANCEL_STATUS_UNAVAILABLE"):
                    client._run_invocation(("codex", "exec"), Path(temporary))
        finally:
            if provider.process is not None and provider.process.poll() is None:
                provider.process.kill()
                provider.process.wait(timeout=5)
        self.assertIsNotNone(provider.process)
        self.assertIsNotNone(provider.process.poll())
        self.assertTrue(client.provider_process_cleanup_confirmed())

    def test_exited_parent_with_live_owned_child_is_not_a_cleanup_ack(self) -> None:
        class Provider:
            process: subprocess.Popen[str] | None = None

            def spawn_invocation(self, *_: object, **__: object) -> subprocess.Popen[str]:
                self.process = subprocess.Popen(
                    (sys.executable, "-c",
                     "import subprocess,sys; subprocess.Popen((sys.executable,'-c',"
                     "'import time; time.sleep(60)'),stdout=subprocess.DEVNULL,"
                     "stderr=subprocess.DEVNULL); print('parent exited',flush=True)"),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                return self.process

        provider = Provider()
        client = CodexCliClient(provider=provider)  # type: ignore[arg-type]
        client.set_cancellation_check(lambda: False)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(RunnerError, "PA_E3_PROVIDER_EXIT_UNCONFIRMED"):
                    client._run_invocation(("codex", "exec"), Path(temporary))
        finally:
            if provider.process is not None:
                try:
                    os.killpg(provider.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class ParallelActionOwnerProofTest(unittest.TestCase):
    def test_invalid_owner_identity_fails_closed(self) -> None:
        with patch.object(recovery, "capture_process_identity", return_value=None):
            with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                        "PA_E3_PROCESS_IDENTITY_UNAVAILABLE"):
                recovery._owner("submission", "project", "repository")
        for value in ("{", "[]", '{"version":1}', '"string"'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                            "PA_E3_OWNER_EVIDENCE_INVALID"):
                    recovery._parse_owner(value, submission_id="submission",
                                          project_id="project", repository_id="repository")

    def test_process_proof_distinguishes_live_absent_reused_and_unavailable(self) -> None:
        owner = {"host": "test-host", "pid": 12345, "group": 12345,
                 "birth": "birth", "executable": "/python"}
        observed = ProcessIdentity(12345, 12345, "birth", "/python")
        reused = ProcessIdentity(12345, 12345, "other-birth", "/python")
        with patch.object(recovery.socket, "gethostname", return_value="local-host"):
            with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                        "PA_E3_OWNER_HOST_UNAVAILABLE"):
                recovery._prove_prior_process_stopped(owner)
        with patch.object(recovery.socket, "gethostname", return_value="test-host"):
            with patch.object(recovery, "verify_process_identity", return_value="MATCH"):
                with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                            "PA_E3_DISPATCH_ACTIVE"):
                    recovery._prove_prior_process_stopped(owner)
            with patch.object(recovery, "verify_process_identity", return_value="NOT_ACTIVE"):
                with patch.object(recovery.os, "kill", side_effect=ProcessLookupError):
                    recovery._prove_prior_process_stopped(owner)
                with patch.object(recovery.os, "kill", side_effect=PermissionError):
                    with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                                "PA_E3_OWNER_LIVENESS_UNAVAILABLE"):
                        recovery._prove_prior_process_stopped(owner)
                with patch.object(recovery.os, "kill", return_value=None):
                    with patch.object(recovery, "capture_process_identity", return_value=None):
                        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                                    "PA_E3_OWNER_LIVENESS_UNAVAILABLE"):
                            recovery._prove_prior_process_stopped(owner)
                    with patch.object(recovery, "capture_process_identity", return_value=observed):
                        with self.assertRaisesRegex(recovery.RecoveryFenceError,
                                                    "PA_E3_DISPATCH_ACTIVE"):
                            recovery._prove_prior_process_stopped(owner)
                    with patch.object(recovery, "capture_process_identity", return_value=reused):
                        recovery._prove_prior_process_stopped(owner)


if __name__ == "__main__":
    unittest.main()
