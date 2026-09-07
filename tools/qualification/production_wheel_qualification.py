#!/usr/bin/env python3
"""Fail closed on unexpected contents or metadata in a production EP wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
import zipfile


_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FORBIDDEN_PARTS = {
    ".git", ".github", "__pycache__", "coverage", "docs", "fixtures",
    "node_modules", "test-results", "tests",
}


def _wheel_version(wheel: Path) -> str:
    match = re.fullmatch(r"engineering_platform-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl", wheel.name)
    if match is None:
        raise RuntimeError("production wheel name must be engineering_platform-X.Y.Z-py3-none-any.whl")
    return match.group(1)


def _inspect_members(wheel: Path, version: str) -> tuple[list[str], list[str]]:
    dist_info = f"engineering_platform-{version}.dist-info/"
    unexpected: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        members = sorted(archive.namelist())
        for member in members:
            parts = tuple(part for part in member.split("/") if part)
            if not parts or any(part in _FORBIDDEN_PARTS or part.endswith((".md", ".pyc")) for part in parts):
                unexpected.append(member)
                continue
            if member.startswith("engineering_platform/"):
                continue
            if member.startswith(dist_info) and member.removeprefix(dist_info) in {"METADATA", "WHEEL", "RECORD", "top_level.txt", "entry_points.txt", "LICENSE"}:
                continue
            unexpected.append(member)
    return members, unexpected


def qualify(root: Path, wheel: Path, expected_version: str) -> dict[str, object]:
    if _VERSION.fullmatch(expected_version) is None:
        raise RuntimeError("expected production version must be stable X.Y.Z")
    if _wheel_version(wheel) != expected_version:
        raise RuntimeError("wheel version does not match the release version")
    package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if package["version"] != expected_version:
        raise RuntimeError("source package version does not match the release version")
    if package.get("dependencies", []):
        raise RuntimeError("production dependency allowlist drift: Engineering Platform currently permits no runtime dependencies")
    members, unexpected = _inspect_members(wheel, expected_version)
    if unexpected:
        raise RuntimeError("unexpected production wheel members: " + ", ".join(unexpected))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "kind": "engineering-platform-production-wheel-qualification",
        "wheel": wheel.name,
        "version": expected_version,
        "sha256": digest,
        "runtime_dependencies": [],
        "members": members,
        "release_mode": "production",
        "result": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("ENGINEERING_PLATFORM_RELEASE_MODE") != "production":
        raise RuntimeError("production wheel qualification requires ENGINEERING_PLATFORM_RELEASE_MODE=production")
    evidence = qualify(args.source_root.resolve(), args.wheel.resolve(), args.version)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"EP_PRODUCTION_WHEEL=PASS version={args.version} sha256={evidence['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
