#!/usr/bin/env python3
"""Subprocess-only owning CLI fixture for coordinator fault qualification."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _argument(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def main() -> int:
    product = "forge" if "forge" in Path(sys.argv[0]).name else "engineering-platform"
    if product == "forge":
        action = sys.argv[sys.argv.index("reset") + 1]
    else:
        action = sys.argv[1]
    root_value = _argument("--data-root")
    if root_value is None:
        raise SystemExit("fixture requires --data-root")
    root = Path(root_value)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = root / "fixture-owning-state.json"
    log_path = root / "fixture-command-log.jsonl"
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "action": action, "pid": os.getpid(), "product": product,
        }, sort_keys=True) + "\n")
    operation_id = _argument("--operation-id")
    if (root / f"fail-{action}").exists():
        print(json.dumps({"error": f"INJECTED_{action.upper()}_FAILURE"}, sort_keys=True))
        return 2
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"state": "ALLOWED", "operation_id": None}
    if action == "prepare":
        state = {
            "state": "BACKUP_VERIFIED" if product == "forge" else "AUTHORIZED",
            "operation_id": operation_id,
        }
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    elif action == "apply":
        state["state"] = "APPLIED" if product == "forge" else "DB_APPLIED"
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    elif action in {"verify", "resume"}:
        state["state"] = "VERIFIED"
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    elif action == "finish":
        state["state"] = "COMPLETED"
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    plan_digest = _digest(product + ":plan")
    backup_digest = _digest(product + ":backup")
    verification_digest = _digest(product + ":verification")
    target = {
        "instance_id": product + "-fixture-instance" + (
            "-changed" if (root / "changed-target").exists() else ""
        ),
        "database_path": str(root / ("forge.db" if product == "forge" else "epdata.sqlite")),
        "database_identity": _digest(product + ":database"),
        "schema_version": 38 if product == "forge" else 68,
    }
    details: dict[str, object] = {
        "request_digest": _digest(product + ":request"),
        "old_callback_projection": (
            "REJECTED_HISTORICAL" if (root / "delayed-old-callback").exists() else "NOT_OBSERVED"
        ),
        "new_missions": 0,
        "new_provider_invocations": 0,
        "new_submissions": 0,
    }
    if state["state"] in {"VERIFIED", "COMPLETED"} and product == "forge":
        details["verification_digest"] = verification_digest
    envelope = {
        "contract_version": "operational-reset-v1",
        "product": product,
        "command": action,
        "operation_id": operation_id or state.get("operation_id"),
        "state": state["state"],
        "allowed": True,
        "target": target,
        "profile": (
            "forge-operational-history-v1" if product == "forge"
            else "EP_CENTRAL_OPERATIONAL_HISTORY_V1"
        ),
        "dataset_generation": 1 if state["state"] in {
            "APPLIED", "DB_APPLIED", "VERIFIED", "COMPLETED",
        } else 0,
        "plan_digest": plan_digest,
        "relevant_revision_digest": _digest(product + ":source"),
        "backup": (
            None if state["state"] == "ALLOWED" else {
                "manifest": str(root / "fixture-backup" / "manifest.json"),
                "digest": backup_digest,
                "verified": True,
            }
        ),
        "counts": {"missions": 0, "provider_invocations": 0, "submissions": 0},
        "blockers": [],
        "integrity": {"quick_check": "ok", "foreign_key_errors": 0},
        "preserved_bindings_digest": _digest(product + ":bindings"),
        "details": details,
    }
    print(json.dumps(envelope, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
