"""Bounded file/selection drift controls; modeled trust is not native proof."""
from contextlib import ExitStack
from copy import deepcopy
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import genesis_git_runtime as runtime
from engineering_platform.genesis_contract import BootstrapError


class GitRuntimeFileControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='own-git-runtime-file-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.toolchain = self.root/'toolchain'
        self.git = self.toolchain/'usr/bin/git'
        self.git.parent.mkdir(parents=True)
        self.git.write_bytes(b'fixed test bytes, NEVER executed\n')
        self.git.chmod(0o755)
        self.selection = self.root/'selection'
        self.selection.symlink_to(self.toolchain)
        stack = self.enterContext(ExitStack())
        # This fixture models only the OS trust roots. File descriptors,
        # content hashing, no-follow opens and drift remain actual filesystem IO.
        original_lstat = Path.lstat
        def root_owned_selection(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path != self.selection:
                return value
            fields = {name:getattr(value, name) for name in dir(value) if name.startswith('st_')}
            return SimpleNamespace(**{**fields, 'st_uid':0})
        stack.enter_context(patch.object(runtime.sys, 'platform', 'darwin'))
        stack.enter_context(patch.object(runtime, 'SELECTION', self.selection))
        stack.enter_context(patch.object(runtime, 'ROOTS', (self.root,)))
        stack.enter_context(patch.object(Path, 'lstat', root_owned_selection))
        # World-writable temporary ancestors are not production trust roots.
        # Their policy is exercised separately below, not silently admitted.
        stack.enter_context(patch.object(runtime, '_ancestors', return_value=[{'fixture':'modeled trusted ancestors'}]))

    def capture(self):
        return runtime.capture(operation_id='own-operation', host_id='own-host')

    def verify(self, pin, **kwargs):
        return runtime.verify(pin, operation_id='own-operation', host_id='own-host', **kwargs)

    def test_original_bytes_pin_and_same_selection_roundtrip(self):
        pin = self.capture()
        self.assertEqual(self.verify(pin), str(self.git))
        self.assertEqual(self.verify(pin, selection=True), str(self.git))
        self.assertEqual(pin['executable']['ino'], self.git.stat().st_ino)

    def test_changed_bytes_refuse_without_repinning(self):
        pin = self.capture()
        self.git.write_bytes(b'changed original executable bytes')
        for selection in (False, True):
            with self.assertRaisesRegex(BootstrapError, 'GIT_RUNTIME_CHANGED'):
                self.verify(pin, selection=selection)
        self.assertNotEqual(self.capture()['executable']['sha256'], pin['executable']['sha256'])

    def test_changed_selection_refuses_even_identical_binary_bytes(self):
        pin = self.capture()
        other = self.root/'other/usr/bin/git'
        other.parent.mkdir(parents=True)
        other.write_bytes(self.git.read_bytes()); other.chmod(0o755)
        self.selection.unlink(); self.selection.symlink_to(other.parents[2])
        with self.assertRaisesRegex(BootstrapError, 'GIT_RUNTIME_CHANGED'):
            self.verify(pin, selection=True)

    def test_invalid_or_cross_operation_pin_is_not_a_new_selection_request(self):
        pin = self.capture()
        bad = [None, {}, {**pin,'format':True}, {**pin,'format':2}, {**pin,'mode':'other'},
            {**pin,'operation_id':'another'}, {**pin,'host_id':'another'}, {**pin,'extra':0},
            {**pin,'executable':None}, {**pin,'ancestors':{}}, {**pin,'selection':None}]
        with patch.object(runtime, 'capture') as recapture, patch.object(runtime, '_binary') as binary:
            for value in bad:
                with self.subTest(value=value), self.assertRaisesRegex(BootstrapError, 'PIN_INVALID'):
                    self.verify(value, selection=True)
            recapture.assert_not_called(); binary.assert_not_called()

    def test_writeable_nonexecutable_fifo_and_hardlink_are_refused(self):
        for mode in (0o777, 0o775, 0o644):
            self.git.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(BootstrapError, 'UNTRUSTED'):
                self.capture()
        self.git.unlink(); os.mkfifo(self.git)
        with self.assertRaisesRegex(BootstrapError, 'UNTRUSTED'): self.capture()
        self.git.unlink(); self.git.write_bytes(b'never execute'); self.git.chmod(0o755)
        os.link(self.git, self.git.with_name('hardlink'))
        with self.assertRaisesRegex(BootstrapError, 'UNAVAILABLE'): self.capture()

    def test_descriptor_read_drift_is_rejected(self):
        original = runtime.selected_bytes
        def change_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.git.write_bytes(b'real content changed after bounded descriptor read')
            return result
        with patch.object(runtime, 'selected_bytes', side_effect=change_after_read):
            with self.assertRaisesRegex(BootstrapError, 'GIT_RUNTIME_CHANGED'):
                self.capture()

    def test_bad_pin_path_or_extra_binary_field_remains_red(self):
        pin = self.capture()
        for value in (3, '/outside/usr/bin/git', str(self.git.with_name('not-git'))):
            bad = deepcopy(pin); bad['executable']['path'] = value
            with self.subTest(path=value), self.assertRaises(BootstrapError): self.verify(bad)
        bad = deepcopy(pin); bad['executable']['extra'] = 0
        with self.assertRaisesRegex(BootstrapError, 'CHANGED'): self.verify(bad)

    def test_missing_os_selection_and_unsupported_platform_are_explicit(self):
        self.selection.unlink()
        with self.assertRaisesRegex(BootstrapError, 'UNAVAILABLE'): self.capture()
        with patch.object(runtime.sys, 'platform', 'other'):
            with self.assertRaisesRegex(BootstrapError, 'MODE_UNSUPPORTED'): self.capture()


class AncestorTrustControls(unittest.TestCase):
    def test_root_group_write_requires_explicit_privileged_darwin_group(self):
        for group in (20, 99, 501):
            info = SimpleNamespace(st_uid=0, st_gid=group, st_mode=stat.S_IFDIR|0o775, st_dev=1, st_ino=3)
            with self.subTest(group=group), patch.object(Path, 'lstat', return_value=info):
                with self.assertRaisesRegex(BootstrapError, 'UNTRUSTED'):
                    runtime._ancestors(Path('/Applications/fixture/git'))
        for group in (0, 80):
            info = SimpleNamespace(st_uid=0, st_gid=group, st_mode=stat.S_IFDIR|0o775, st_dev=1, st_ino=3)
            with self.subTest(group=group), patch.object(Path, 'lstat', return_value=info):
                self.assertTrue(runtime._ancestors(Path('/Applications/fixture/git')))
        # A root-owned read-only ancestor need not delegate writes to its group.
        info = SimpleNamespace(st_uid=0, st_gid=20, st_mode=stat.S_IFDIR|0o755, st_dev=1, st_ino=3)
        with patch.object(Path, 'lstat', return_value=info):
            self.assertTrue(runtime._ancestors(Path('/Applications/fixture/git')))
        # Even admin-group write is not allowed on a non-root-owned ancestor.
        info = SimpleNamespace(st_uid=501, st_gid=80, st_mode=stat.S_IFDIR|0o775, st_dev=1, st_ino=3)
        with patch.object(runtime.os, 'geteuid', return_value=501), patch.object(Path, 'lstat', return_value=info):
            with self.assertRaisesRegex(BootstrapError, 'UNTRUSTED'):
                runtime._ancestors(Path('/Applications/fixture/git'))

    def test_foreign_owner_other_writes_and_nondirectory_are_rejected(self):
        actual = runtime._ancestors
        for owner, mode in ((os.geteuid()+100000, 0o40755), (os.geteuid(), 0o40777),
                            (0, 0o40777), (0, 0o100755)):
            info = SimpleNamespace(st_uid=owner, st_gid=20, st_mode=mode, st_dev=1, st_ino=3)
            with self.subTest(owner=owner, mode=mode), patch.object(Path, 'lstat', return_value=info):
                with self.assertRaisesRegex(BootstrapError, 'UNTRUSTED'):
                    actual(Path('/Applications/fixture/git'))
        info = SimpleNamespace(st_uid=0, st_gid=80, st_mode=stat.S_IFDIR|0o775, st_dev=1, st_ino=3)
        with patch.object(Path, 'lstat', return_value=info):
            self.assertTrue(actual(Path('/Applications/fixture/git')))


class OriginalGitLogControls(unittest.TestCase):
    def test_timeout_keeps_received_bytes_and_same_primary(self):
        from engineering_platform import genesis_worker as worker
        error = subprocess.TimeoutExpired('fixed own fixture', 5, output=b'original-out', stderr=b'original-error')
        sink = io.BytesIO()
        with patch.object(worker.subprocess, 'run', side_effect=error), \
             patch.object(worker.sys, 'stderr', SimpleNamespace(buffer=sink)):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                worker._run_git(('fixed-own-fixture','--version'), 42)
        self.assertIs(caught.exception, error)
        self.assertIn(b'original-out', sink.getvalue())
        self.assertIn(b'original-error', sink.getvalue())

    def test_log_failures_attempt_both_streams_without_masking_timeout(self):
        from engineering_platform import genesis_worker as worker
        error = subprocess.TimeoutExpired('fixed own fixture', 5, output=b'out', stderr=b'err')
        class BrokenSink:
            def __init__(self): self.writes = []
            def write(self, raw):
                self.writes.append(raw)
                raise OSError('own bounded log failure')
        sink = BrokenSink()
        with patch.object(worker.subprocess, 'run', side_effect=error), \
             patch.object(worker.sys, 'stderr', SimpleNamespace(buffer=sink)):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                worker._run_git(('fixed-own-fixture','--version'), 42)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(sink.writes), 2)
        self.assertIsInstance(error.__cause__, ExceptionGroup)
        self.assertEqual(len(error.__cause__.exceptions), 2)

    def test_success_stderr_is_retained_and_oversized_output_stays_red(self):
        from engineering_platform import genesis_worker as worker
        sink = io.BytesIO()
        with patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout=b'x'*262145,stderr=b'original-success-stderr')), \
             patch.object(worker.sys, 'stderr', SimpleNamespace(buffer=sink)):
            with self.assertRaisesRegex(BootstrapError, 'GIT_OUTPUT_LIMIT'):
                worker._run_git(('fixed-own-fixture','--version'), 42)
        self.assertIn(b'original-success-stderr', sink.getvalue())
        self.assertLess(len(sink.getvalue()), 262144+65536)

    def test_nonzero_or_stdout_limit_with_log_failure_keeps_primary_and_exit_status(self):
        from engineering_platform import genesis_worker as worker
        class BrokenSink:
            def write(self, raw): raise OSError('own exact log failure')
        for code, raw, expected in ((7,b'out','LOCAL_GIT_FAILED'),(0,b'x'*262145,'GIT_OUTPUT_LIMIT')):
            with self.subTest(code=code, expected=expected), \
                 patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=code,stdout=raw,stderr=b'original-error')), \
                 patch.object(worker.sys, 'stderr', SimpleNamespace(buffer=BrokenSink())):
                with self.assertRaisesRegex(BootstrapError, expected) as caught:
                    worker._run_git(('fixed-own-fixture','--version'), 42)
                self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)
                self.assertTrue(any(f'Original Git exit={code},' in note for note in caught.exception.__notes__))

    def test_successful_stderr_overflow_is_explicitly_red(self):
        from engineering_platform import genesis_worker as worker
        sink = io.BytesIO()
        with patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout=b'git version fixture\n',stderr=b'x'*65537)), \
             patch.object(worker.sys, 'stderr', SimpleNamespace(buffer=sink)):
            with self.assertRaisesRegex(BootstrapError, 'GIT_ERROR_OUTPUT_LIMIT') as caught:
                worker._run_git(('fixed-own-fixture','--version'), 42)
        self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)
        self.assertTrue(any('stderr_bytes=65537' in note for note in caught.exception.__notes__))
        self.assertLess(len(sink.getvalue()), 65536+256)
