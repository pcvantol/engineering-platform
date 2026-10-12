"""Real own-process lifecycle controls plus explicitly injected denial faults."""
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests.engineering.dashboard_run_evidence_fixture import OwnedReadWorker, StoredConsoleCanary, CodexCliProvider


class DashboardWorkerProcessCleanupTests(unittest.TestCase):
    def own_worker(self):
        process = subprocess.Popen(
            (sys.executable, "-u", "-c",
             'import sys,os; print("ready",flush=True); sys.stdin.readline(); os._exit(0)'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.assertEqual(process.stdout.readline(), "ready\n")
        self.assertEqual(os.getpgid(process.pid), process.pid)
        worker = OwnedReadWorker.__new__(OwnedReadWorker)
        worker.process = process
        worker.process_events = []
        worker.group_ended = False
        self.addCleanup(self.finish_actual_child, process)
        return worker

    def finish_actual_child(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def test_real_live_group_is_signalled_reaped_and_verified(self):
        worker = self.own_worker()
        worker.end_process()
        self.assertEqual(worker.process.returncode, -signal.SIGTERM)
        self.assertTrue(worker.group_ended)

    def test_actual_exit_after_real_poll_is_verified_before_classifying_signal_error(self):
        worker = self.own_worker()
        process = worker.process
        actual_poll = process.poll
        released = False

        def exit_after_poll():
            nonlocal released
            observed = actual_poll()
            if observed is None and not released:
                released = True
                process.stdin.write("exit\n")
                process.stdin.flush()
                self.assertEqual(process.stdout.read(), "")
            # Return the actual result from the poll before the controlled
            # scheduling interleaving, never a fabricated process state/PID.
            return observed

        with patch.object(process, "poll", exit_after_poll):
            worker.end_process()
        self.assertTrue(released)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(worker.group_ended)
        for event in worker.process_events:
            self.assertTrue(event["actual_child_reaped"])
            self.assertTrue(event["whole_group_ended"])
        if sys.platform == "darwin":
            self.assertTrue(worker.process_events)
            self.assertEqual(worker.process_events[0]["error"], "PermissionError")

    def test_injected_denial_of_still_live_actual_group_stays_red(self):
        worker = self.own_worker()
        # This negative transport fault is explicitly synthetic; the actual
        # native signal/exit-race controls above are not replaced by it.
        with patch("tests.engineering.dashboard_run_evidence_fixture.os.killpg",
                   side_effect=PermissionError(1, "controlled own-group denial")):
            with self.assertRaises(PermissionError):
                worker.end_process()
        self.assertIsNone(worker.process.poll())
        self.assertFalse(worker.group_ended)
        self.assertNotIn("whole_group_ended", worker.process_events[0])

    def test_unavailable_group_census_cannot_authorize_cleanup(self):
        worker = self.own_worker()
        process = worker.process
        actual_poll = process.poll
        released = False

        def exit_after_poll():
            nonlocal released
            observed = actual_poll()
            if observed is None and not released:
                released = True
                process.stdin.write("exit\n"); process.stdin.flush()
                self.assertEqual(process.stdout.read(), "")
            return observed

        with patch.object(process, "poll", exit_after_poll), patch(
                "tests.engineering.dashboard_run_evidence_fixture.os.killpg",
                side_effect=PermissionError(1, "controlled signal denial")), patch(
                "tests.engineering.dashboard_run_evidence_fixture.subprocess.check_output",
                side_effect=OSError("controlled census unavailable")):
            with self.assertRaises(PermissionError):
                worker.end_process()
        self.assertEqual(process.returncode, 0)
        self.assertFalse(worker.group_ended)

    def test_reaped_parent_with_live_actual_descendant_does_not_clear_denial(self):
        script = ('import sys,os,subprocess; '
                  'p=subprocess.Popen([sys.executable,"-c","import signal; signal.pause()"],'
                  'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                  'print(p.pid,flush=True); sys.stdin.readline(); os._exit(0)')
        process = subprocess.Popen((sys.executable, "-u", "-c", script),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        descendant = int(process.stdout.readline())
        self.assertEqual(os.getpgid(process.pid), process.pid)
        self.assertEqual(os.getpgid(descendant), process.pid)
        worker = OwnedReadWorker.__new__(OwnedReadWorker)
        worker.process = process; worker.process_events = []; worker.group_ended = False
        try:
            process.stdin.write("exit\n"); process.stdin.flush()
            self.assertEqual(process.stdout.read(), "")
            self.assertEqual(process.wait(timeout=2), 0)
            with patch("tests.engineering.dashboard_run_evidence_fixture.os.killpg",
                       side_effect=PermissionError(1, "controlled descendant-group denial")):
                with self.assertRaises(PermissionError):
                    worker.end_process()
            self.assertFalse(worker.group_ended)
            self.assertTrue(worker.process_events[0]["actual_child_reaped"])
            self.assertFalse(worker.process_events[0]["whole_group_ended"])
        finally:
            # Only this Popen-created group, whose actual descendant was
            # observed above; never a guessed or manufactured process ID.
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
            deadline = time.monotonic() + 2
            while True:
                rows = subprocess.check_output(("ps", "-axo", "pgid=,stat="), text=True)
                live = any(parts[0] == str(process.pid) and not parts[1].startswith("Z")
                           for line in rows.splitlines() if len(parts := line.split()) >= 2)
                if not live:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_partial_constructor_keeps_owner_guards_and_both_failures(self):
        canary = StoredConsoleCanary()
        original = CodexCliProvider.app_server
        worker = None
        try:
            with patch.object(OwnedReadWorker, "end_process",
                              side_effect=OSError("controlled missing cleanup proof")):
                with self.assertRaises(ExceptionGroup) as failed:
                    canary.start_listener(bind_port=-1)
                worker = canary.listener
                self.assertIsNotNone(worker)
                self.assertIsInstance(failed.exception.exceptions[0], AssertionError)
                self.assertIsInstance(failed.exception.exceptions[1], OSError)
                self.assertIsInstance(worker.startup_error, AssertionError)
                self.assertIsInstance(worker.cleanup_error, OSError)
                with self.assertRaises(OSError):
                    canary.close()
                self.assertTrue(canary.fixture.area.exists())
                self.assertFalse(worker.group_ended)
                # Constructor failure cannot lose the actual owner reference
                # or prematurely restore the canary's provider guards.
                self.assertIs(CodexCliProvider.app_server, original)
        finally:
            if worker:
                worker.end_process()
                self.assertTrue(worker.group_ended)
                canary.listener = None
            canary.close()
        self.assertFalse(canary.fixture.area.exists())
