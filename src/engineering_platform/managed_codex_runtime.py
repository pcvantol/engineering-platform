"""Installation-owned lifecycle for Engineering Platform's managed Codex CLI.

This preserves the historical explicit-console repair model: startup only
observes the EP-owned runtime, while a confirmed operator action provisions or
updates a version pinned from the npm registry.  It never selects a PATH
``codex`` executable.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
from threading import Lock

from .providers import CodexCliProvider, LocalProcessProvider, codex_cli_executable, engineering_platform_codex_cli_prefix


CODEX_CLI_PACKAGE = "@openai/codex"
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_VERSION_IN_OUTPUT = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?!\d)")
_install_lock = Lock()


class ManagedCodexRuntimeError(RuntimeError):
    """The explicit managed-runtime repair could not complete safely."""


def version(value: object) -> str | None:
    candidate = str(value or "").strip().removeprefix("v")
    if _VERSION.fullmatch(candidate):
        return candidate
    match = _VERSION_IN_OUTPUT.search(candidate)
    return match.group(1) if match else None


def version_key(value: str) -> tuple[int, int, int, int, str]:
    base, separator, prerelease = value.partition("-")
    major, minor, patch = (int(part) for part in base.split("."))
    return major, minor, patch, 1 if not separator else 0, prerelease


def npm_executable() -> str | None:
    return shutil.which("npm")


def inspect(root: Path) -> dict[str, object]:
    """Classify only the canonical managed launcher without network activity."""
    executable = engineering_platform_codex_cli_prefix() / "bin" / "codex"
    if not executable.is_file():
        return {"state": "MISSING", "path": str(executable), "remediation_available": npm_executable() is not None}
    if not executable.stat().st_mode & 0o111:
        return {"state": "BROKEN", "path": str(executable), "remediation_available": npm_executable() is not None}
    try:
        completed = CodexCliProvider().command("--version")
    except OSError:
        completed = None
    detected = version((completed.stdout or completed.stderr) if completed is not None and completed.returncode == 0 else None)
    if detected is None:
        return {"state": "BROKEN", "path": str(executable), "remediation_available": npm_executable() is not None}
    return {"state": "READY", "path": str(executable), "version": detected, "remediation_available": npm_executable() is not None}


def _published_version(root: Path, npm: str) -> str:
    try:
        completed = LocalProcessProvider().execute(root, (npm, "view", CODEX_CLI_PACKAGE, "version", "--json"))
        raw = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ManagedCodexRuntimeError("codex_cli_update_unavailable") from error
    candidates = raw if isinstance(raw, list) else [raw]
    versions = [candidate for item in candidates if (candidate := version(item)) is not None]
    latest = max(versions, key=version_key, default=None)
    if latest is None:
        raise ManagedCodexRuntimeError("codex_cli_update_unavailable")
    return latest


def provision(root: Path, *, allow_current: bool = True) -> dict[str, object]:
    """Install or update the exact published release into EP's owned prefix."""
    with _install_lock:
        npm = npm_executable()
        if npm is None:
            raise ManagedCodexRuntimeError("codex_cli_update_unavailable")
        before = inspect(root)
        if before["state"] == "READY" and not allow_current:
            return {"updated": False, "current_version": before["version"]}
        latest = _published_version(root, npm)
        if before.get("version") == latest:
            return {"updated": False, "current_version": latest}
        try:
            completed = LocalProcessProvider().execute(
                root,
                (npm, "install", "--global", "--prefix", str(engineering_platform_codex_cli_prefix()), f"{CODEX_CLI_PACKAGE}@{latest}"),
            )
        except OSError as error:
            raise ManagedCodexRuntimeError("codex_cli_update_failed") from error
        if completed.returncode:
            diagnostic = f"{completed.stdout}\n{completed.stderr}".casefold()
            raise ManagedCodexRuntimeError(
                "codex_cli_update_permissions_required" if "eacces" in diagnostic or "permission denied" in diagnostic else "codex_cli_update_failed"
            )
        after = inspect(root)
        if after.get("state") != "READY" or not isinstance(after.get("version"), str) or version_key(after["version"]) < version_key(latest):
            raise ManagedCodexRuntimeError("codex_cli_update_failed")
        return {"updated": True, "current_version": after["version"]}
