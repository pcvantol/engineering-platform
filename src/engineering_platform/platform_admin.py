"""Installation-owner authorization for local Server administration."""
from __future__ import annotations

from pathlib import Path
import os


def require_installation_owner(data_root: Path) -> str:
    """Authorize only the owner of the private Server data root."""
    owner = data_root.resolve().stat().st_uid
    if os.geteuid() != owner:
        raise PermissionError("PLATFORM_ADMIN_FORBIDDEN")
    return str(owner)
