"""Minimal external product helper fixture for the EP delivery contract."""
from __future__ import annotations

import json
from pathlib import Path
import sys


root = Path(sys.argv[1])
request = json.loads(sys.stdin.read())
(root / "product-version.json").write_text(
    json.dumps({"product": request["product_id"], "version": request["determined_target_version"]}) + "\n",
    encoding="utf-8",
)
receipt = {
    "schema_version": 1,
    "operation_id": request["operation_id"],
    "product": request["product_id"],
    "component_id": request["component_id"],
    "repository_id": request["repository_id"],
    "policy_revision": request["policy_revision"],
    "policy_digest": request["policy_digest"],
    "source_event_set": request["source_event_set"],
    "source_event_policy": request["source_event_policy"],
    "release_class": request["release_class"],
    "release_rationale": request["release_rationale"],
    "expected_source_revision": request["expected_source_revision"],
    "expected_target_branch_revision": request["expected_target_branch_revision"],
    "expected_version": request["expected_version"],
    "requested_change": request["requested_change"],
    "determined_target_version": request["determined_target_version"],
    "allowed_projection_paths": request["allowed_projection_paths"],
    "authorization_reference": request["authorization_reference"],
    "delivery_mode": request["delivery_mode"],
}
directory = root / ".version-operations"
directory.mkdir(exist_ok=True)
(directory / f"{request['operation_id']}.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
