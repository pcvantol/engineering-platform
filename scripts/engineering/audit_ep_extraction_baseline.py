#!/usr/bin/env python3
"""Read-only, deterministic validation and drift projection for the EP 2.x baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PureWindowsPath
import sys


REQUIRED = {"path", "classification", "reason_code", "ownership", "extraction_target", "dependency_notes"}


def is_safe_relative_path(value: object) -> bool:
    """Accept only portable, repository-relative manifest paths."""
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    windows_path = PureWindowsPath(value)
    return not path.is_absolute() and not windows_path.is_absolute() and ".." not in path.parts


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest(root: Path) -> dict:
    path = root / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate(manifest: dict, root: Path) -> list[str]:
    errors: list[str] = []
    allowed = set(manifest.get("classifications", []))
    entries = manifest.get("paths", [])
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if set(item) != REQUIRED:
            errors.append(f"entry {index} does not have the required fields")
            continue
        path = item["path"]
        if not is_safe_relative_path(path):
            errors.append(f"entry {index} has an unsafe path")
        elif path in seen:
            errors.append(f"duplicate path: {path}")
        else:
            seen.add(path)
            if not (root / path).exists():
                errors.append(f"missing path: {path}")
        if item["classification"] not in allowed:
            errors.append(f"entry {index} has an unknown classification")
        if not isinstance(item["reason_code"], str) or not item["reason_code"]:
            errors.append(f"entry {index} has an invalid reason code")
        for field in ("ownership", "dependency_notes"):
            if not isinstance(item[field], str) or not item[field]:
                errors.append(f"entry {index} has an invalid {field}")
    return errors


def projection(manifest: dict) -> dict:
    paths = manifest["paths"]
    return {
        "manifest_version": manifest["manifest_version"],
        "baseline": manifest["baseline"],
        "entry_count": len(paths),
        "classifications": {
            name: sum(item["classification"] == name for item in paths)
            for name in sorted(manifest["classifications"])
        },
        "paths": [item["path"] for item in sorted(paths, key=lambda item: item["path"])],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the checked-in manifest")
    parser.add_argument("--projection", action="store_true", help="emit deterministic inventory JSON")
    args = parser.parse_args(argv)
    root = repository_root()
    manifest = load_manifest(root)
    errors = validate(manifest, root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if args.projection:
        print(json.dumps(projection(manifest), indent=2, sort_keys=True))
    elif args.check:
        print("EP extraction baseline manifest: valid")
    else:
        parser.error("choose --check or --projection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
