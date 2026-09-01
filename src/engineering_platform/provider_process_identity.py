"""Bounded, provider-neutral OS process identity evidence.

The recovery controller never treats a PID as identity.  This adapter captures
the process birth marker and executable path without retaining arguments or
environment values that could contain prompts or credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_group: int
    start_fingerprint: str
    executable_identity: str


def _canonical_executable_identity(value: str) -> str:
    """Normalize equivalent macOS Python framework executable spellings."""
    path = Path(value).resolve()
    parts = path.parts
    try:
        framework = parts.index("Python.framework")
    except ValueError:
        return str(path)
    version_root = Path(*parts[: framework + 3])
    suffix = parts[framework + 3 :]
    if suffix == ("bin", path.name) or suffix == ("Resources", "Python.app", "Contents", "MacOS", "Python"):
        return str(version_root / "python-runtime")
    return str(path)


def capture_process_identity(pid: int, process_group: int | None = None) -> ProcessIdentity | None:
    """Capture portable `ps` birth/executable evidence for a live process."""
    if pid <= 0:
        return None
    try:
        observed_group = os.getpgid(pid)
        if process_group is not None and observed_group != process_group:
            return None
        # `lstart` is a process-birth value on macOS and common POSIX hosts;
        # `comm` is the executable identity, not command arguments.
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)),
            check=False, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip()
    if not line:
        return None
    # lstart is exactly five fields; the remainder is the executable path.
    parts = line.split(maxsplit=5)
    if len(parts) != 6:
        return None
    birth = " ".join(parts[:5])
    executable = _canonical_executable_identity(parts[5][:512])
    fingerprint = hashlib.sha256(f"{pid}|{observed_group}|{birth}".encode("utf-8")).hexdigest()
    return ProcessIdentity(pid, observed_group, fingerprint, executable)


def verify_process_identity(expected: ProcessIdentity) -> str:
    """Return MATCH, NOT_ACTIVE, or MISMATCH without trusting PID alone."""
    observed = capture_process_identity(expected.pid, expected.process_group)
    if observed is None:
        return "NOT_ACTIVE"
    if (
        observed.start_fingerprint == expected.start_fingerprint
        and observed.executable_identity == expected.executable_identity
    ):
        return "MATCH"
    return "MISMATCH"
