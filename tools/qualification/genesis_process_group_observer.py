"""Private, read-only Darwin facts after the unchanged Genesis group check.

This is qualification instrumentation, not an alternative admission detector.
It never signals a process or converts an uncertain result to success.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class BsdInfo(ctypes.Structure):
    # Darwin sys/proc_info.h, PROC_PIDTBSDINFO; no argv/environment is read.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
    ] + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def group_facts(group: int) -> dict:
    """Two birth-guarded libproc reads and an independent bounded ps snapshot."""
    if sys.platform != "darwin" or group <= 0:
        raise ValueError("Darwin positive process group required")
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    lib.proc_pidpath.restype = ctypes.c_int
    buffer = (ctypes.c_int * 4096)()
    ctypes.set_errno(0)
    size = lib.proc_listpids(2, group, buffer, ctypes.sizeof(buffer))  # PROC_PGRP_ONLY
    list_errno = ctypes.get_errno()
    result = {"list_bytes": size, "list_errno": list_errno,
        "capacity_bytes": ctypes.sizeof(buffer), "members": []}
    # Zero is also libproc's error return. Never label it a complete empty
    # inventory; ESRCH from signal zero remains a separate observation.
    result["inventory_complete"] = (list_errno == 0 and 0 < size < ctypes.sizeof(buffer)
        and size % ctypes.sizeof(ctypes.c_int) == 0)
    for pid in buffer[:max(0, min(size, ctypes.sizeof(buffer))) // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        samples = []
        for _ in range(2):
            info = BsdInfo()
            ctypes.set_errno(0)
            count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if count != ctypes.sizeof(info):
                samples.append({"bytes": count, "errno": ctypes.get_errno()})
                continue
            try:
                session = os.getsid(pid)
            except OSError as error:
                session = {"errno": error.errno}
            executable_buffer = ctypes.create_string_buffer(4096)
            path_bytes = lib.proc_pidpath(pid, executable_buffer, len(executable_buffer))
            executable = executable_buffer.value if path_bytes > 0 else b""
            leaf = Path(os.fsdecode(executable)).name if executable else ""
            kind = leaf if leaf in {"git", "xcrun", "sandbox-exec", "Python", "python3.14", "codex", "sh"} else "other"
            samples.append({"pid": info.pid, "ppid": info.ppid, "pgid": info.pgid,
                "status": info.status, "start": [info.start_sec, info.start_usec],
                "session": session, "open_files": info.nfiles,
                "executable_kind": kind,
                "executable_sha256": hashlib.sha256(executable).hexdigest() if executable else None})
        stable = all("start" in sample for sample in samples) and all(
            samples[0][key] == samples[1][key] for key in ("pid", "pgid", "start", "executable_sha256"))
        result["members"].append({"pid": pid, "samples": samples, "identity_stable": stable})
    try:
        completed = subprocess.run(("/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="),
            capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        result["ps_error"] = type(error).__name__
        result["ps_members"] = None
    else:
        result["ps_returncode"] = completed.returncode
        result["ps_members"] = [line.split() for line in completed.stdout.splitlines()
            if len(line.split()) == 4 and line.split()[2] == str(group)]
    try:
        os.killpg(group, 0)  # Observation only; never signals or grants authority.
        result["signal_zero"] = "PRESENT"
    except OSError as error:
        result["signal_zero"] = "ABSENT_ESRCH" if error.errno == errno.ESRCH else "UNCERTAIN"
        result["signal_zero_errno"] = error.errno
    return result


def append_private(path: Path, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def install_observer(service, path: Path) -> None:
    original = service._require_group_stopped

    def observed(group, *args, **kwargs):
        before = time.monotonic_ns()
        try:
            value = original(group, *args, **kwargs)
        except BaseException as primary:
            record = {"group": group, "at_monotonic_ns": before, "detector": "FAIL",
                "error_type": type(primary).__name__, "error_code": getattr(primary, "code", None)}
            try:
                record["facts"] = group_facts(group)
            except Exception as error:
                record["observation_error"] = type(error).__name__
            try:
                append_private(path, record)
            except Exception as error:
                primary.add_note("Owning read-only observation could not persist: " + type(error).__name__)
            raise
        else:
            append_private(path, {"group": group, "at_monotonic_ns": before, "detector": "PASS_ESRCH"})
            return value

    service._require_group_stopped = observed
