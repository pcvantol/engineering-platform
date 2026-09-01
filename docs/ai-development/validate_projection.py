#!/usr/bin/env python3
"""Offline validator copied into every committed contract projection."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED_IDENTITIES = {
    "AI_BOOTSTRAP_CONTRACT", "HANDOFF_CONTRACT", "PROMPT_INITIALIZATION_CONTRACT",
    "BRANCH_WORKTREE_CONTRACT", "VALIDATION_EVIDENCE_CONTRACT", "TDE_INTEGRATION_CONTRACT",
    "REPOSITORY_GOVERNANCE_CONTRACT", "PROJECTION_CONTRACT",
}

parser = argparse.ArgumentParser(description="Validate an offline AI-development projection")
parser.add_argument("--profile", required=True)
parser.add_argument("--source-commit")
parser.add_argument("--extension-identity")
args = parser.parse_args()

manifest = json.loads((ROOT / "projection-manifest.json").read_text())
body = (ROOT / "GENERATED_PROJECTION.md").read_bytes()
if manifest.get("source_repo") != "pcvantol/ai-development-contracts":
    raise SystemExit("unexpected projection authority")
if manifest.get("profile") != args.profile:
    raise SystemExit("profile mismatch")
if args.source_commit and manifest.get("source_commit") != args.source_commit:
    raise SystemExit("undeclared source revision")
if args.extension_identity and manifest.get("extension_identity") != args.extension_identity:
    raise SystemExit("extension identity mismatch")
if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")):
    raise SystemExit("invalid source revision")
contracts = {name.removesuffix(".md") for name in manifest.get("contracts", [])}
if contracts != EXPECTED_IDENTITIES or len(manifest.get("contracts", [])) != 8:
    raise SystemExit("missing or noncanonical contract identity")
for identity in contracts:
    if f"# {identity}".encode() not in body:
        raise SystemExit("missing generated contract: " + identity)
if manifest.get("projection_file_digest") != hashlib.sha256(body).hexdigest():
    raise SystemExit("projection drift")
print("offline projection validation: PASS")
