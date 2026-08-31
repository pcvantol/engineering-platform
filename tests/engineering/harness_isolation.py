"""Suite-wide isolation for Engineering Platform unittest processes."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


TEST_INSTALLATION_ROOT = "DJCONNECT_EP_TEST_INSTALLATION_ROOT"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def activate() -> Path:
    """Install one fresh EP authority root before Engineering tests import code."""
    configured = os.environ.get(TEST_INSTALLATION_ROOT)
    if configured:
        return Path(configured).resolve()

    installation_root = Path(tempfile.mkdtemp(prefix="djconnect-engineering-tests-"))
    os.environ["HOME"] = str(installation_root)
    os.environ["XDG_DATA_HOME"] = str(installation_root / ".local" / "share")
    os.environ[TEST_INSTALLATION_ROOT] = str(installation_root)
    atexit.register(shutil.rmtree, installation_root, ignore_errors=True)

    # Import only after HOME is isolated. An explicit test patch must not turn
    # an external authority pointer into writable test state; normal
    # per-repository legacy fixtures remain valid when no pointer exists.
    from tools.engineering import storage

    original_database_path = storage.database_path

    def isolated_database_path(root: Path) -> Path:
        pointer = storage._authority_pointer_path()
        if pointer.exists() and not _inside(pointer, installation_root):
            raise storage.EngineeringStorageError(
                "Engineering test harness rejected an authority pointer outside its isolated installation root."
            )
        resolved = original_database_path(root)
        if pointer.exists() and not _inside(resolved, installation_root):
            raise storage.EngineeringStorageError(
                "Engineering test harness rejected writable authority outside its isolated installation root."
            )
        return resolved

    storage.database_path = isolated_database_path
    return installation_root
