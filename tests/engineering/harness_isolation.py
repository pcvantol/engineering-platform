"""Suite-wide isolation for Engineering Platform unittest processes."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile


TEST_INSTALLATION_ROOT = "DJCONNECT_EP_TEST_INSTALLATION_ROOT"
_ORIGINAL_DATABASE_PATH = None
_SUITE_INSTALLATION_ROOT: Path | None = None


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
        installation_root = Path(configured).resolve()
    else:
        installation_root = Path(tempfile.mkdtemp(prefix="djconnect-engineering-tests-"))
        os.environ["HOME"] = str(installation_root)
        os.environ["XDG_DATA_HOME"] = str(installation_root / ".local" / "share")
        os.environ[TEST_INSTALLATION_ROOT] = str(installation_root)
        atexit.register(shutil.rmtree, installation_root, ignore_errors=True)

    # Import only after HOME is isolated. An explicit test patch must not turn
    # an external authority pointer into writable test state; normal
    # per-repository legacy fixtures remain valid when no pointer exists.
    from engineering_platform import storage

    global _ORIGINAL_DATABASE_PATH, _SUITE_INSTALLATION_ROOT
    _SUITE_INSTALLATION_ROOT = installation_root
    if _ORIGINAL_DATABASE_PATH is not None:
        return installation_root
    _ORIGINAL_DATABASE_PATH = storage.database_path

    def isolated_database_path(root: Path) -> Path:
        # Some execution-host tests intentionally scrub child environments.
        # Their fallback is the captured suite root, never the real user home.
        active_root = Path(os.environ.get(TEST_INSTALLATION_ROOT, str(_SUITE_INSTALLATION_ROOT))).resolve()
        pointer = storage._authority_pointer_path()
        if pointer.exists() and not _inside(pointer, active_root):
            raise storage.EngineeringStorageError(
                "Engineering test harness rejected an authority pointer outside its isolated installation root."
            )
        resolved = _ORIGINAL_DATABASE_PATH(root)
        if pointer.exists() and not _inside(resolved, active_root):
            raise storage.EngineeringStorageError(
                "Engineering test harness rejected writable authority outside its isolated installation root."
            )
        return resolved

    storage.database_path = isolated_database_path
    return installation_root


@contextmanager
def scoped_installation_root():
    """Temporarily make a nested EP authority root active for one test."""
    outer = {name: os.environ.get(name) for name in ("HOME", "XDG_DATA_HOME", TEST_INSTALLATION_ROOT)}
    inner = Path(tempfile.mkdtemp(prefix="djconnect-engineering-test-override-"))
    os.environ["HOME"] = str(inner / "home")
    os.environ["XDG_DATA_HOME"] = str(inner / ".local" / "share")
    os.environ[TEST_INSTALLATION_ROOT] = str(inner)
    try:
        yield inner
    finally:
        for name, value in outer.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(inner, ignore_errors=True)
