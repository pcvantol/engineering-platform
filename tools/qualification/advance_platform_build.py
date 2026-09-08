#!/usr/bin/env python3
"""Update the canonical Engineering Platform version for a wheel build.

The version is deliberately a checked-in, cross-surface release fact.  A build
therefore advances it once before packaging rather than letting individual
Server, Console, Runner, or wheel metadata drift independently.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_PROJECTION_PATHS = (
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "src/engineering_platform/ENGINEERING_PLATFORM_VERSION.json",
    "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json",
    "src/engineering_platform/templates/workspace-config.json",
    "src/engineering_platform/platform_version.py",
)


def _replace(path: Path, before: str, after: str) -> None:
    text = path.read_text(encoding="utf-8")
    if before not in text:
        raise RuntimeError(f"canonical version {before} is absent from {path}")
    path.write_text(text.replace(before, after), encoding="utf-8")


def _current_version(root: Path) -> str:
    root = root.resolve()
    pyproject = root / "pyproject.toml"
    match = re.search(r'^version = "([^"]+)"$', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None or _VERSION.fullmatch(match.group(1)) is None:
        raise RuntimeError("the canonical package version must be a stable X.Y.Z release")
    return match.group(1)


def set_version(root: Path, version: str) -> str:
    """Set every public EP version projection to one stable release version."""
    root = root.resolve()
    if _VERSION.fullmatch(version) is None:
        raise RuntimeError("the requested release version must be a stable X.Y.Z release")
    current = _current_version(root)
    for relative_path in VERSION_PROJECTION_PATHS:
        _replace(root / relative_path, current, version)
    return version


def advance(root: Path, *, component: str = "patch") -> str:
    """Advance one stable semantic-version component across all projections."""
    current = _current_version(root)
    major, minor, patch = (int(part) for part in current.split("."))
    if component == "patch":
        target = f"{major}.{minor}.{patch + 1}"
    elif component == "minor":
        target = f"{major}.{minor + 1}.0"
    else:
        raise RuntimeError("version component must be patch or minor")
    return set_version(root, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advance one canonical EP wheel build number")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--set-version", help="set all canonical projections to this exact stable X.Y.Z version")
    parser.add_argument("--bump", choices=("patch", "minor"), default="patch", help="semantic-version component to advance when --set-version is absent")
    args = parser.parse_args(argv)
    version = set_version(args.source_root, args.set_version) if args.set_version else advance(args.source_root, component=args.bump)
    print(f"EP_BUILD_VERSION={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
