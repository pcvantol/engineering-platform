#!/usr/bin/env python3
"""Deterministic integrity checks for the B8E capability catalog."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/engineering/STANDALONE_ZERO_LOSS_CAPABILITY_MATRIX.json"


def main() -> int:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    allowed = set(data["allowed_statuses"])
    capabilities = data["capabilities"]
    ids = [item["id"] for item in capabilities]
    lookup = {item["id"]: item for item in capabilities}
    errors: list[str] = []
    if len(ids) != len(set(ids)):
        errors.append("capability IDs must be unique")
    for item in capabilities:
        status = item.get("status")
        if status not in allowed:
            errors.append(f"{item['id']}: invalid disposition")
        if status == "UNRESOLVED":
            errors.append(f"{item['id']}: unresolved capability")
        if status in {"MISSING", "PARTIALLY_PRESERVED", "PRESERVED_NOT_WIRED"}:
            if not item.get("gap") or not item.get("severity") or not item.get("owner"):
                errors.append(f"{item['id']}: gap, severity and owner are required")
    for responsibility in data["historical_responsibilities"]:
        mapped = responsibility.get("capabilities", [])
        if not mapped:
            errors.append(f"{responsibility['id']}: unmapped historical responsibility")
        for capability_id in mapped:
            if capability_id not in lookup:
                errors.append(f"{responsibility['id']}: unknown capability {capability_id}")
    if errors:
        print("B8E audit validation failed:\n- " + "\n- ".join(errors))
        return 1
    print(f"B8E audit validation passed: {len(capabilities)} capabilities; {len(data['historical_responsibilities'])} responsibility groups; UNRESOLVED=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
