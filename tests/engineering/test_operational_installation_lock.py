from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.operational_installation_lock import OperationalInstallationLock, OperationalInstallationLockError


class OperationalInstallationLockTests(unittest.TestCase):
    def test_only_one_operation_can_own_the_lock_and_release_is_verified(self) -> None:
        with TemporaryDirectory() as temporary:
            owner = OperationalInstallationLock(Path(temporary) / "data root")
            contender = OperationalInstallationLock(Path(temporary) / "data root")
            owner.acquire("install-0001")
            with self.assertRaisesRegex(OperationalInstallationLockError, "another"):
                contender.acquire("install-0002")
            with self.assertRaisesRegex(OperationalInstallationLockError, "does not own"):
                owner.release("install-0002")
            owner.release("install-0001")
            contender.acquire("install-0002")
            contender.release("install-0002")

    def test_lock_rejects_invalid_or_reentrant_operation(self) -> None:
        with TemporaryDirectory() as temporary:
            lock = OperationalInstallationLock(Path(temporary))
            with self.assertRaisesRegex(OperationalInstallationLockError, "invalid"):
                lock.acquire("short")
            lock.acquire("install-0001")
            with self.assertRaisesRegex(OperationalInstallationLockError, "already owned"):
                lock.acquire("install-0001")
            lock.release("install-0001")
