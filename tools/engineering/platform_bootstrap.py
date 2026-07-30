"""Idempotent repository bootstrap and workspace provisioning API."""
from __future__ import annotations

import json
from pathlib import Path

from .platform_api import PlatformConfiguration, PlatformConfigurationError


def provision_workspace(root: Path) -> dict[str, Path]:
    """Provision only platform-owned local directories; safe to repeat."""
    PlatformConfiguration.load(root)
    workspace = root / ".djconnect"
    paths = {"workspace": workspace, "reports": workspace / "reports", "status": workspace / "status", "runs": workspace / "engineering-runs", "diagnostics": workspace / "logs"}
    for path in paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return paths


def validate_repository(root: Path) -> PlatformConfiguration:
    """Fail closed unless this repository is an explicit platform consumer."""
    if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
        raise PlatformConfigurationError("Repository bootstrap compatibility failed.")
    return PlatformConfiguration.load(root)


def render_template(destination: Path, replacements: dict[str, str]) -> Path:
    """Create a deterministic config template without overwriting consumer data."""
    if destination.exists():
        return destination
    template = Path(__file__).with_name("templates") / "workspace-config.json"
    content = template.read_text(encoding="utf-8")
    for key, value in sorted(replacements.items()):
        content = content.replace(key, value)
    json.loads(content)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content + "\n", encoding="utf-8")
    return destination
