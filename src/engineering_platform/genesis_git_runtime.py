"""Operation-bound physical Git for the trusted macOS installation owner.

Apple's /usr/bin/git is a tool-selection shim which can start Xcode helpers.
This mode executes the existing OS-selected Git inside the original sandbox.
It is not protection against an administrator or the same owner replacing
installed executables during exec. No arbitrary consumer path or PATH fallback
is accepted. Selection drift is an error, never permission to replace a pin.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import time

from .genesis_contract import BootstrapError, canonical
from .preproject_inspection import directory, selected_bytes, InspectionError

SELECTION = Path('/var/db/xcode_select_link')
ROOTS = (Path('/Applications'), Path('/Library/Developer'))
POLICY = 'MACOS_OS_SELECTED_PHYSICAL_GIT_V1'
FIELDS = {'format', 'mode', 'operation_id', 'host_id', 'selection', 'executable', 'ancestors'}


def _metadata(info):
    return {name: getattr(info, 'st_'+name) for name in
        ('dev', 'ino', 'uid', 'gid', 'mode', 'size', 'mtime_ns', 'ctime_ns')}


def _ancestors(path):
    rows = []
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & (0o002 if info.st_uid == 0 else 0o022)):
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_UNTRUSTED', 422)
        rows.append({'path': str(parent), **{key: getattr(info, 'st_'+key)
            for key in ('dev', 'ino', 'uid', 'gid', 'mode')}})
    return rows


def _binary(path):
    if (not path.is_absolute() or '..' in path.parts or path.name != 'git'
            or not any(path.is_relative_to(root) for root in ROOTS)):
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_UNTRUSTED', 422)
    ancestors = _ancestors(path)
    with directory(path.parent) as parent:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022 or not before.st_mode & 0o111):
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_UNTRUSTED', 422)
        _, sha256 = selected_bytes(parent, path.name, maximum=32*1024*1024,
            deadline=time.monotonic()+2)
        after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if _metadata(before) != _metadata(after) or ancestors != _ancestors(path):
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_CHANGED', 409)
    return {'path': str(path), 'sha256': sha256, **_metadata(before)}, ancestors


def capture(*, operation_id: str, host_id: str) -> dict:
    """Bounded host reads only; no Git, selector subprocess or target access."""
    if sys.platform != 'darwin':
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_MODE_UNSUPPORTED', 422)
    try:
        before = SELECTION.lstat()
        if before.st_uid != 0 or not stat.S_ISLNK(before.st_mode):
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_UNTRUSTED', 422)
        link = str(SELECTION.readlink())
        path = (SELECTION/'usr/bin/git').resolve(strict=True)
        executable, ancestors = _binary(path)
        if _metadata(before) != _metadata(SELECTION.lstat()) or link != str(SELECTION.readlink()):
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_CHANGED', 409)
        return {'format': 1, 'mode': POLICY, 'operation_id': operation_id, 'host_id': host_id,
            'selection': {'path': str(SELECTION), 'link': link, **_metadata(before)},
            'executable': executable, 'ancestors': ancestors}
    except (OSError, InspectionError) as error:
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_UNAVAILABLE', 422) from error


def verify(pin, *, operation_id: str, host_id: str, selection: bool = False) -> str:
    """The worker checks its binary; the controller also checks OS selection."""
    if (not isinstance(pin, dict) or set(pin) != FIELDS or type(pin['format']) is not int
            or pin['format'] != 1 or pin['mode'] != POLICY
            or pin['operation_id'] != operation_id or pin['host_id'] != host_id
            or not isinstance(pin['selection'], dict) or not isinstance(pin['executable'], dict)
            or not isinstance(pin['ancestors'], list)):
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_PIN_INVALID', 409)
    try:
        path = Path(pin['executable']['path'])
        if selection:
            observed = capture(operation_id=operation_id, host_id=host_id)
            match = canonical(observed) == canonical(pin)
        else:
            executable, ancestors = _binary(path)
            match = (canonical(executable) == canonical(pin['executable'])
                and canonical(ancestors) == canonical(pin['ancestors']))
        if not match:
            raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_CHANGED', 409)
    except BootstrapError:
        raise
    except (OSError, InspectionError, KeyError, TypeError, ValueError) as error:
        raise BootstrapError('BOOTSTRAP_GIT_RUNTIME_PIN_INVALID', 409) from error
    return str(path)
