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


def _digest_payload(rows: list[dict[str, object]]) -> str:
    """Return a stable digest for the complete source-to-target candidate."""
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_baseline(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"allowed_divergences": [], "allowed_additions": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("equivalence baseline must be a JSON object")
    for key in ("allowed_divergences", "allowed_additions"):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"equivalence baseline {key} must be a list")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    source_root, target_root = args.source.resolve(), args.target.resolve()
    baseline = _load_baseline(args.baseline)
    divergences = {
        item["target_path"]: item
        for item in baseline["allowed_divergences"]
    }
    additions = {
        item["target_path"]: item
        for item in baseline["allowed_additions"]
    }
    rows, failures = [], []
    source_paths, target_paths = set(), set()
    for source in sorted((source_root / "tools/engineering").rglob("*.py")):
        relative = source.relative_to(source_root / "tools/engineering")
        target = target_root / "src/engineering_platform" / relative
        source_paths.add(relative.as_posix())
        if not target.is_file():
            failures.append(f"missing target: {relative}")
            continue
        target_paths.add(relative.as_posix())
        before = source.read_text(encoding="utf-8")
        after = target.read_text(encoding="utf-8")
        source_digest, target_digest = digest(source), digest(target)
        rewrite_categories = ["namespace_import"] if before != after else []
        if normalized_source(before) != after:
            approved = divergences.get(target.relative_to(target_root).as_posix())
            if not approved or approved.get("source_digest") != source_digest or approved.get("target_digest") != target_digest:
                failures.append(f"non-mechanical divergence: {relative}")
            else:
                rewrite_categories.append(str(approved["category"]))
        rows.append({"source_path": source.relative_to(source_root).as_posix(), "target_path": target.relative_to(target_root).as_posix(), "source_digest": source_digest, "target_pre_rewrite_digest": source_digest, "target_final_digest": target_digest, "rewrite_categories": rewrite_categories})
    for target in sorted((target_root / "src/engineering_platform").rglob("*.py")):
        relative = target.relative_to(target_root / "src/engineering_platform").as_posix()
        if relative in target_paths:
            continue
        approved = additions.get((target_root / "src/engineering_platform" / relative).relative_to(target_root).as_posix())
        if not approved or approved.get("target_digest") != digest(target):
            failures.append(f"unexpected target addition: {relative}")
            continue
        rows.append({"source_path": None, "target_path": target.relative_to(target_root).as_posix(), "source_digest": None, "target_pre_rewrite_digest": None, "target_final_digest": digest(target), "rewrite_categories": [str(approved["category"])]})
    baseline_digest = _digest_payload(rows)
    expected_digest = baseline.get("candidate_baseline_digest")
    if expected_digest and expected_digest != baseline_digest:
        failures.append("candidate baseline digest mismatch")
    result = {"rows": rows, "failures": failures, "candidate_baseline_digest": baseline_digest, "source_files_accounted_for": len(source_paths), "target_files_accounted_for": len(rows), "unexpected_transformation_count": len(failures), "pass": not failures}
    if args.receipt:
        args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(rows), "failures": failures, "pass": not failures}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
