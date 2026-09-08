#!/usr/bin/env python3
"""Plan or apply an explicit, field-aware EP version operation.

Normal builds only read the version already committed in ``pyproject.toml``.
This helper is the separate source mutation boundary; it has no publication or
qualification authority and its per-file atomic replacements are not claimed to
be a multi-file transaction.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib


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


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _current_version(root: Path) -> str:
    root = root.resolve()
    try:
        value = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError("the canonical package version is unreadable") from error
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise RuntimeError("the canonical package version must be a stable X.Y.Z release")
    return value


def _json_projection(path: Path, current: str, target: str, field: tuple[str, ...]) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        item: object = payload
        for key in field[:-1]:
            if not isinstance(item, dict): raise RuntimeError(f"invalid object projection {path}")
            item = item[key]
        if not isinstance(item, dict) or item.get(field[-1]) != current:
            raise RuntimeError(f"canonical version projection drift in {path}")
        item[field[-1]] = target
        return json.dumps(payload, indent=2) + "\n"
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid canonical version projection {path}") from error


def _python_projection(path: Path, current: str, target: str) -> str:
    text = path.read_text(encoding="utf-8")
    old = f'CURRENT_PLATFORM_VERSION = "{current}"'
    if text.count(old) != 1:
        raise RuntimeError(f"canonical runtime version projection drift in {path}")
    return text.replace(old, f'CURRENT_PLATFORM_VERSION = "{target}"', 1)


def _toml_projection(path: Path, current: str, target: str) -> str:
    text = path.read_text(encoding="utf-8")
    old = f'version = "{current}"'
    if text.count(old) != 1:
        raise RuntimeError(f"canonical package version projection drift in {path}")
    return text.replace(old, f'version = "{target}"', 1)


def _prepared_writes(root: Path, current: str, target: str) -> dict[Path, str]:
    paths = {relative: root / relative for relative in VERSION_PROJECTION_PATHS}
    # Read and validate every projection before touching any source file.
    return {
        paths["pyproject.toml"]: _toml_projection(paths["pyproject.toml"], current, target),
        paths["package.json"]: _json_projection(paths["package.json"], current, target, ("version",)),
        paths["package-lock.json"]: _json_projection(paths["package-lock.json"], current, target, ("version",)),
        paths["src/engineering_platform/ENGINEERING_PLATFORM_VERSION.json"]: _json_projection(paths["src/engineering_platform/ENGINEERING_PLATFORM_VERSION.json"], current, target, ("platform_version",)),
        paths["src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json"]: _json_projection(paths["src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json"], current, target, ("platform", "version")),
        paths["src/engineering_platform/templates/workspace-config.json"]: _json_projection(paths["src/engineering_platform/templates/workspace-config.json"], current, target, ("platform", "version")),
        paths["src/engineering_platform/platform_version.py"]: _python_projection(paths["src/engineering_platform/platform_version.py"], current, target),
    }


def set_version(root: Path, version: str, *, expected_version: str | None = None) -> str:
    """Set every public EP version projection to one stable release version."""
    root = root.resolve()
    if _VERSION.fullmatch(version) is None:
        raise RuntimeError("the requested release version must be a stable X.Y.Z release")
    current = _current_version(root)
    if expected_version is not None and expected_version != current:
        raise RuntimeError(f"stale version operation: expected {expected_version}, found {current}")
    writes = _prepared_writes(root, current, version)
    if version == current:
        return version
    for path, text in writes.items():
        _atomic_write(path, text)
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
    parser = argparse.ArgumentParser(description="Apply an explicit canonical EP version operation")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--set-version", help="set all canonical projections to this exact stable X.Y.Z version")
    parser.add_argument("--bump", choices=("patch", "minor"), help="semantic-version component to advance")
    parser.add_argument("--expected-version")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        if args.bump or args.set_version: parser.error("--check cannot change a version")
        version = _current_version(args.source_root)
    elif (args.bump is None) == (args.set_version is None):
        parser.error("provide exactly one of --bump or --set-version")
    else:
        version = set_version(args.source_root, args.set_version, expected_version=args.expected_version) if args.set_version else advance(args.source_root, component=args.bump)
    print(f"EP_BUILD_VERSION={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
