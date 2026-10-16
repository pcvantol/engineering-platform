"""Actual stopped-child cleanup and independent primary-evidence retention."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('genesis_http_qualification',
    Path(__file__).resolve().parents[2] / 'tools/qualification/genesis_local_bootstrap_http.py')
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class QualificationCleanupTests(unittest.TestCase):
    def test_exited_parent_does_not_hide_actual_live_own_group(self):
        from engineering_platform.provider_process_identity import capture_process_identity, verify_process_identity
        p = subprocess.Popen([sys.executable, '-I', '-c',
            'import os,time;pid=os.fork();\nif pid==0: time.sleep(60)\nelse: print(pid,flush=True)'],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        child = int(p.stdout.readline()); identity = capture_process_identity(child, p.pid)
        self.assertIsNotNone(identity)
        def cleanup():
            if verify_process_identity(identity) == 'MATCH': os.kill(child, signal.SIGKILL)
            p.wait(timeout=5); p.stdout.close()
        self.addCleanup(cleanup)
        p.wait(timeout=5)
        case = qualification.Case.__new__(qualification.Case)
        case.children = [p]; case.record = {'processes': [{'pid': p.pid}]}
        with self.assertRaisesRegex(RuntimeError, 'group active or uncertain'): case.stop(p)
        self.assertNotIn('reaped', case.record['processes'][0])

    def test_stopped_actual_own_server_is_resumed_stopped_and_reaped(self):
        p = subprocess.Popen([sys.executable, '-I', '-c',
            'import os,signal,time;os.kill(os.getpid(),signal.SIGSTOP);time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait(timeout=5)))
        until = time.monotonic()+5
        while time.monotonic() < until:
            state = subprocess.check_output(['ps', '-o', 'stat=', '-p', str(p.pid)], text=True)
            if 'T' in state: break
            time.sleep(.02)
        else: self.fail('Own actual child did not pause')
        case = qualification.Case.__new__(qualification.Case)
        case.children = [p]; case.record = {'processes': [{'pid': p.pid}]}
        case.stop(p)
        self.assertIsNotNone(p.poll())
        self.assertTrue(case.record['processes'][0]['reaped'])

    def test_cleanup_failure_retains_primary_and_attempts_every_owned_resource(self):
        with tempfile.TemporaryDirectory() as d:
            case = qualification.Case.__new__(qualification.Case)
            case.children = [object(), object()]
            case.stop = Mock(side_effect=[RuntimeError('actual first stop failed'), None])
            case.logs = [Mock(), Mock()]
            case.logs[0].close.side_effect = OSError('actual first close failed')
            case.evidence = Path(d); case.port = 1
            case.record = {'state': 'FAILED', 'failure': 'original actual primary failure'}
            with patch.object(qualification.socket, 'socket') as socket:
                socket.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaisesRegex(ExceptionGroup, 'cleanup incomplete') as found: case.finish()
            self.assertEqual(case.stop.call_count, 2)
            for log in case.logs: log.close.assert_called_once()
            receipt = json.loads((Path(d)/'receipt.json').read_bytes())
            self.assertEqual(receipt['failure'], 'original actual primary failure')
            self.assertEqual(len(receipt['cleanup_errors']), 2)
            self.assertEqual(receipt['state'], 'FAILED')
            self.assertEqual([str(e) for e in found.exception.exceptions], ['actual first stop failed', 'actual first close failed'])
            stop_error = RuntimeError('original stop error')
            receipt_error = OSError('original receipt error')
            case.stop.side_effect = [stop_error, None]
            case.logs = []
            with patch.object(qualification.socket, 'socket') as socket, patch.object(qualification, 'write_private', side_effect=receipt_error):
                socket.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaises(ExceptionGroup) as all_errors: case.finish()
            self.assertEqual(all_errors.exception.exceptions, (stop_error, receipt_error))
            case.record['state'] = 'PASS'; case.stop.side_effect = None
            with patch.object(qualification.socket, 'socket') as socket, patch.object(qualification, 'write_private', side_effect=receipt_error):
                socket.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaises(ExceptionGroup) as receipt_only: case.finish()
            self.assertEqual(case.record['state'], 'FAILED')
            self.assertEqual(receipt_only.exception.exceptions, (receipt_error,))
