"""Idempotent repository bootstrap and workspace provisioning API."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .platform_api import PlatformConfiguration, PlatformConfigurationError


WORKSPACE_DIRECTORY = ".engineering"
LEGACY_WORKSPACE_DIRECTORY = ".djconnect"


def _validate_legacy_merge(source: Path, destination: Path) -> None:
    """Fail closed before moving evidence into an occupied canonical workspace."""
    if source.is_symlink() or destination.is_symlink():
        raise RuntimeError("Engineering workspace migration refuses symbolic links.")
    if source.is_dir() != destination.is_dir():
        raise RuntimeError(f"Engineering workspace migration conflict: {source.name}")
    if source.is_dir():
        for child in source.iterdir():
            target = destination / child.name
            if target.exists():
                _validate_legacy_merge(child, target)
        return
    if source.read_bytes() != destination.read_bytes():
        raise RuntimeError(f"Engineering workspace migration conflict: {source.name}")


def _merge_legacy_workspace(source: Path, destination: Path) -> None:
    """Move prevalidated legacy evidence, dropping only byte-identical duplicates."""
    for child in source.iterdir():
        target = destination / child.name
        if not target.exists():
            shutil.move(str(child), str(target))
        elif child.is_dir():
            _merge_legacy_workspace(child, target)
            child.rmdir()
        else:
            child.unlink()


def _move_legacy_logs(source: Path, workspace: Path) -> None:
    """Preserve a conflicting historic log tail outside the live log files."""
    destination = workspace / "logs" / "legacy"
    if destination.exists():
        _validate_legacy_merge(source, destination)
        _merge_legacy_workspace(source, destination)
        source.rmdir()
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def _archive_legacy_evidence(source: Path, workspace: Path) -> None:
    """Keep a conflicting historic evidence category without replacing live data."""
    destination = workspace / "legacy" / source.name
    if destination.exists():
        _validate_legacy_merge(source, destination)
        _merge_legacy_workspace(source, destination)
        source.rmdir()
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def migrate_legacy_workspace(root: Path) -> Path:
    """Move `.djconnect` evidence to the sole canonical `.engineering` location."""
    root = root.resolve()
    legacy = root / LEGACY_WORKSPACE_DIRECTORY
    workspace = root / WORKSPACE_DIRECTORY
    if not legacy.exists():
        return workspace
    if legacy.is_symlink() or not legacy.is_dir():
        raise RuntimeError("Engineering workspace migration requires a local directory.")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    archived_categories = {"logs", "qualification"}
    for source in legacy.iterdir():
        if source.name in archived_categories and (workspace / source.name).exists():
            continue
        target = workspace / source.name
        if target.exists():
            _validate_legacy_merge(source, target)
    legacy_logs = legacy / "logs"
    if legacy_logs.exists() and (workspace / "logs").exists():
        _move_legacy_logs(legacy_logs, workspace)
    legacy_qualification = legacy / "qualification"
    if legacy_qualification.exists() and (workspace / "qualification").exists():
        _archive_legacy_evidence(legacy_qualification, workspace)
    _merge_legacy_workspace(legacy, workspace)
    legacy.rmdir()
    return workspace


def provision_workspace(root: Path) -> dict[str, Path]:
    """Provision only platform-owned local directories; safe to repeat."""
    workspace = migrate_legacy_workspace(root)
    PlatformConfiguration.load(root)
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
