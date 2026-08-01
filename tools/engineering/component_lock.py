"""Fail-closed single-instance ownership for long-running EP components."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from typing import Iterator


class DuplicateComponentInstanceError(RuntimeError):
    """Raised when a second local process tries to own the same component."""


@contextmanager
def single_instance(repo: Path, component: str) -> Iterator[None]:
    """Hold a non-blocking, process-lifetime lock for one named component."""
    directory = repo / ".djconnect" / "locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{component}.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DuplicateComponentInstanceError(
                f"A second {component} instance was refused; the active instance retains ownership."
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"component": component, "pid": os.getpid()}))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
