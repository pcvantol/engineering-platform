"""Bounded, non-authoritative Host Admin observations for an EP installation.

This adapter deliberately receives only the Server installation data root.  It
does not accept a browser path, project id, checkout, CWD or Git remote, and
it cannot admit work, mutate CENTRAL or execute a command.  Mutating host
administration needs a separate, explicitly audited contract.
"""
from __future__ import annotations

from pathlib import Path
import shutil

from . import managed_codex_runtime


def installation_root(data_root: Path) -> Path:
    """Return the one explicit host context available to Host Admin."""
    return data_root.resolve()


def diagnostics(data_root: Path) -> dict[str, object]:
    """Project a small, secret-free installation health observation.

    The values are derived observations only.  They are not a source for
    project, queue, run, retry, execution or component truth.
    """
    root = installation_root(data_root)
    usage = shutil.disk_usage(root)
    runtime = managed_codex_runtime.inspect(root)
    state = runtime.get("state") if isinstance(runtime, dict) else "UNKNOWN"
    return {
        "scope": "HOST_ADMIN",
        "root_kind": "EP_SERVER_INSTALLATION",
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
        "managed_codex_runtime": {"state": state},
        "mutations_supported": False,
        "project_authority": False,
        "execution_authority": False,
        "queue_authority": False,
    }
