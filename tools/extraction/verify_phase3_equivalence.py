#!/usr/bin/env python3
"""Validate the bounded mechanical Phase-3 source-to-target translation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_source(text: str) -> str:
    return (
        text.replace("tools.engineering", "engineering_platform")
        .replace("tools/engineering", "src/engineering_platform")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    source_root, target_root = args.source.resolve(), args.target.resolve()
    rows, failures = [], []
    for source in sorted((source_root / "tools/engineering").rglob("*.py")):
        relative = source.relative_to(source_root / "tools/engineering")
        target = target_root / "src/engineering_platform" / relative
        if not target.is_file():
            failures.append(f"missing target: {relative}")
            continue
        before = source.read_text(encoding="utf-8")
        after = target.read_text(encoding="utf-8")
        if normalized_source(before) != after:
            failures.append(f"non-mechanical divergence: {relative}")
        rows.append({"source_path": source.relative_to(source_root).as_posix(), "target_path": target.relative_to(target_root).as_posix(), "source_digest": digest(source), "target_pre_rewrite_digest": digest(source), "target_final_digest": digest(target), "rewrite_categories": ["namespace_import"] if before != after else []})
    result = {"rows": rows, "failures": failures, "pass": not failures}
    if args.receipt:
        args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(rows), "failures": failures, "pass": not failures}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
