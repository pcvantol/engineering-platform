"""One crash-safe lock for EP operational installation lifecycle work."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re


_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


class OperationalInstallationLockError(ValueError):
    """An installer does not safely own the single operational lifecycle lock."""


class OperationalInstallationLock:
    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root).expanduser().resolve() / "operational-installation.lock"
        self._descriptor: int | None = None
        self._operation_id: str | None = None

    def acquire(self, operation_id: str) -> None:
        if _OPERATION.fullmatch(operation_id) is None:
            raise OperationalInstallationLockError("operational installation operation ID is invalid")
        if self._descriptor is not None:
            raise OperationalInstallationLockError("operational installation lock is already owned")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise OperationalInstallationLockError("another operational installation operation owns the lock") from error
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (operation_id + "\n").encode("utf-8"))
            os.fsync(descriptor)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN); os.close(descriptor)
            raise
        self._descriptor, self._operation_id = descriptor, operation_id

    def release(self, operation_id: str) -> None:
        if self._descriptor is None or self._operation_id != operation_id:
            raise OperationalInstallationLockError("operational installation operation does not own the lock")
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor, self._operation_id = None, None
