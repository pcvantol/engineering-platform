"""Independent real OS observations do not grant effect or signal authority."""
import importlib.util
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("group_observer", Path(__file__).resolve().parents[2] /
    "tools/qualification/genesis_process_group_observer.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


class ObserverContractTests(unittest.TestCase):
    def test_libproc_failure_is_uncertain_and_ps_failure_retains_facts(self):
        def denied(*args):
            ctypes.set_errno(errno.EPERM)
            return 0
        library = SimpleNamespace(proc_pidinfo=Mock(), proc_listpids=Mock(side_effect=denied), proc_pidpath=Mock())
        with patch.object(observer.sys, "platform", "darwin"), patch.object(observer.ctypes, "CDLL", return_value=library), \
             patch.object(observer.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 2)), \
             patch.object(observer.os, "killpg", side_effect=PermissionError(errno.EPERM, "denied")):
            facts = observer.group_facts(100)
        self.assertEqual(facts["list_errno"], errno.EPERM)
        self.assertFalse(facts["inventory_complete"])
        self.assertEqual(facts["members"], [])
        self.assertEqual(facts["ps_error"], "TimeoutExpired")
        self.assertIsNone(facts["ps_members"])
        self.assertEqual(facts["signal_zero"], "UNCERTAIN")

    def test_original_failure_object_and_result_are_preserved(self):
        error = RuntimeError("original owning failure")
        def refused(group):
            raise error
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations"
            service = SimpleNamespace(_require_group_stopped=refused)
            observer.install_observer(service, path)
            with self.assertRaises(RuntimeError) as found:
                service._require_group_stopped(-1)
            self.assertIs(found.exception, error)
            event = json.loads(path.read_text())
            self.assertEqual(event["detector"], "FAIL")
            self.assertEqual(event["observation_error"], "ValueError")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            service = SimpleNamespace(_require_group_stopped=lambda group: "original result")
            observer.install_observer(service, path)
            self.assertEqual(service._require_group_stopped(1), "original result")

    def test_observer_failure_cannot_erase_primary(self):
        error = RuntimeError("primary")
        def refused(group):
            raise error
        service = SimpleNamespace(_require_group_stopped=refused)
        with tempfile.TemporaryDirectory() as temporary:
            observer.install_observer(service, Path(temporary) / "missing" / "log")
            with self.assertRaises(RuntimeError) as found:
                service._require_group_stopped(-1)
            self.assertIs(found.exception, error)
            self.assertIn("could not persist", error.__notes__[0])

    @unittest.skipUnless(sys.platform == "darwin", "Actual Darwin libproc qualification")
    def test_actual_live_own_group_then_waited_absence(self):
        child = subprocess.Popen((sys.executable, "-I", "-c", "import time;time.sleep(30)"), start_new_session=True)
        try:
            self.assertEqual(os.getpgid(child.pid), child.pid)
            facts = observer.group_facts(child.pid)
            self.assertEqual(facts["signal_zero"], "PRESENT")
            self.assertEqual(facts["ps_returncode"], 0)
            member = next(row for row in facts["members"] if row["pid"] == child.pid)
            self.assertTrue(member["identity_stable"])
            self.assertEqual(member["samples"][0]["pgid"], child.pid)
            self.assertEqual(member["samples"][0]["session"], child.pid)
        finally:
            # Direct, unreaped Popen child only; no PID/group-only kill.
            child.terminate()
            child.wait(timeout=5)
        gone = observer.group_facts(child.pid)
        self.assertEqual(gone["signal_zero"], "ABSENT_ESRCH")
        self.assertFalse(gone["members"])
        self.assertFalse(gone["ps_members"])


if __name__ == "__main__":
    unittest.main()
