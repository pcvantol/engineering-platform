"""Real protocol-child fault regressions; these are not native Genesis PASS."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import genesis_native

SPEC = importlib.util.spec_from_file_location('git_host_probe', Path(__file__).resolve().parents[2] /
    'tools/qualification/genesis_git_runtime_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class GitProbeEvidenceTests(unittest.TestCase):
    def test_worker_timeout_retains_received_bytes(self):
        setup = '''import subprocess
def fail(*a,**k):raise subprocess.TimeoutExpired('fixed-own-protocol-fixture',5,output=b'original-out',stderr=b'original-error')
subprocess.run=fail
'''
        actual = subprocess.run((sys.executable, '-I', '-c', setup+probe.WORKER, 'fixed-own-protocol-fixture', str(self.root/'r')),
            cwd=self.root, capture_output=True, text=True, timeout=5, check=True)
        result = json.loads(actual.stdout)
        self.assertEqual(result['error_type'], 'TimeoutExpired')
        self.assertEqual(bytes.fromhex(result['stdout_hex']), b'original-out')
        self.assertEqual(bytes.fromhex(result['stderr_hex']), b'original-error')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='own-git-probe-log-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'a').mkdir()
        (self.root/'r').mkdir()
        self.output = self.root/'out'
        (self.output/'private').mkdir(parents=True)
        self.args = SimpleNamespace(python=Path(sys.executable), output=self.output)

    def run_protocol_child(self, code, label):
        # A fixed owning Python protocol fixture only; the actual native/macOS
        # runtime comparison remains a separate installed host qualification.
        with patch.object(genesis_native, 'runtime', return_value=Path(sys.executable)), \
             patch.object(genesis_native, 'command', return_value=(sys.executable, '-I', '-c', code)):
            return probe.one(self.args, self.root, label, 'git')

    def test_actual_stderr_overflow_keeps_original_and_cleanup_logs(self):
        code = 'import sys,json;sys.stderr.buffer.write(b"x"*2097152);sys.stderr.flush();print(json.dumps({"returncode":0}),flush=True)'
        result = self.run_protocol_child(code, 'overflow')
        self.assertEqual(result['probe_error_type'], 'RuntimeError')
        self.assertEqual(result['control_integrity'], 'FAIL')
        self.assertTrue((self.output/'private/overflow.stdout').is_file())
        saved = self.output/'private/overflow.stderr'
        self.assertEqual(saved.stat().st_size, 262144)
        record = json.loads((self.output/'private/original-controls.jsonl').read_text())
        self.assertEqual(record['probe_error_type'], 'RuntimeError')
        self.assertTrue(record['cleanup_errors'])

    def test_extra_stdout_is_retained_and_rejected(self):
        result = self.run_protocol_child(
            'import sys;sys.stdout.buffer.write(b\'{"returncode":0}\\nUNEXPECTED-TAIL\\n\');sys.stdout.flush()', 'tail')
        self.assertEqual(result['control_integrity'], 'FAIL')
        self.assertIn(b'UNEXPECTED-TAIL\n', (self.output/'private/tail.stdout').read_bytes())

    def test_stdout_sink_failure_does_not_skip_original_stderr(self):
        code = 'import sys,json;sys.stderr.buffer.write(b"original-stderr");sys.stderr.flush();print(json.dumps({"returncode":0}),flush=True)'
        original_write = Path.write_bytes
        def fail_stdout(path, data):
            if path.name == 'sink-failure.stdout': raise OSError('own exact stdout sink fault')
            return original_write(path, data)
        with patch.object(Path, 'write_bytes', fail_stdout):
            result = self.run_protocol_child(code, 'sink-failure')
        self.assertEqual((self.output/'private/sink-failure.stderr').read_bytes(), b'original-stderr')
        self.assertEqual(result['control_integrity'], 'FAIL')
        self.assertTrue(any(error['type'] == 'OSError' for error in result['cleanup_errors']))


if __name__ == '__main__': unittest.main()
