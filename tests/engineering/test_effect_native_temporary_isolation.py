"""Actual macOS temporary-root isolation through the shared production policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from engineering_platform import effect_workspace as workspace


class NativeTemporaryIsolationTests(unittest.TestCase):
    def test_platform_policy_excludes_only_implicit_macos_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            scratch = Path(directory) / "scratch"
            root.mkdir()
            scratch.mkdir()
            for system in ("Darwin", "Linux"):
                with self.subTest(system=system), patch.object(workspace.platform, "system", return_value=system):
                    options = workspace.sandbox_options(root, scratch=scratch)
                    grants = tomllib.loads(options[1])["permissions"]["ep-effects"]["filesystem"]
                    self.assertEqual(grants[str(root.resolve())], "read")
                    self.assertEqual(grants[str(scratch.resolve())], "write")
                    self.assertEqual(grants[":minimal"], "read")
                    for implicit in ("/private/tmp", "/private/var/tmp"):
                        if system == "Darwin":
                            self.assertEqual(grants[implicit], "none")
                        else:
                            self.assertNotIn(implicit, grants)

    @unittest.skipUnless(platform.system() == "Darwin", "Actual macOS backend qualification")
    def test_native_denies_temporary_siblings_aliases_and_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="ep-native-isolation-", dir="/private/tmp") as temporary, \
                tempfile.TemporaryDirectory(prefix="ep-native-isolation-", dir="/private/var/tmp") as other:
            base = Path(temporary)
            source, scratch, sibling = (base / name for name in ("source", "scratch", "ungranted"))
            for path in (source, scratch, sibling):
                path.mkdir(mode=0o700)
            outside = Path(other)
            for path in (source, scratch, sibling, outside):
                (path / "canary").write_text("own qualification canary\n")
            (scratch / "escape").symlink_to(sibling, target_is_directory=True)
            (scratch / "other-escape").symlink_to(outside, target_is_directory=True)
            paths = (source, scratch, sibling, Path(str(sibling).replace("/private/tmp/", "/tmp/")),
                     outside, Path(str(outside).replace("/private/var/tmp/", "/var/tmp/")),
                     scratch / "escape", scratch / "other-escape")
            code = """import json,pathlib,sys,tempfile,socket
rows=[]
for raw in sys.argv[1:]:
    path=pathlib.Path(raw); row={}
    try: row['read']=path.joinpath('canary').read_text()
    except OSError as error: row['read_errno']=error.errno
    try: path.joinpath('child-created').write_text('own child canary'); row['write']='ALLOWED'
    except OSError as error: row['write_errno']=error.errno
    rows.append(row)
with tempfile.TemporaryFile() as handle:
    handle.write(b'isolated temporary workspace'); handle.seek(0)
    temporary=handle.read().decode()
try: socket.create_connection(('127.0.0.1',1),timeout=1); network='ALLOWED'
except OSError as error: network={'errno':error.errno,'type':type(error).__name__}
print(json.dumps({'rows':rows,'temporary':temporary,'network':network}))
"""
            command = workspace.sandbox_command(source, (sys.executable, "-c", code, *(str(p) for p in paths)),
                                                scratch=scratch)
            completed = subprocess.run(command, env=workspace.child_environment() | {"TMPDIR": str(scratch)},
                                       text=True, capture_output=True, timeout=30, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["rows"][0], {"read": "own qualification canary\n", "write_errno": 1})
            self.assertEqual(observed["rows"][1], {"read": "own qualification canary\n", "write": "ALLOWED"})
            for row in observed["rows"][2:]:
                self.assertEqual(row, {"read_errno": 1, "write_errno": 1})
            self.assertEqual(observed["temporary"], "isolated temporary workspace")
            self.assertEqual(observed["network"], {"errno": 1, "type": "PermissionError"})
            self.assertTrue((scratch / "child-created").is_file())
            for path in (source, sibling, outside):
                self.assertFalse((path / "child-created").exists())
                self.assertEqual((path / "canary").read_text(), "own qualification canary\n")


if __name__ == "__main__":
    unittest.main()
