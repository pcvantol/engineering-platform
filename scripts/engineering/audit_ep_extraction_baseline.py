#!/usr/bin/env python3
"""Deterministically prove the EP 2.x extraction manifest against repository truth."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys


CLASSIFICATIONS = {
    "EP_PRODUCT_SOURCE", "EP_TEST", "EP_DOCUMENTATION", "EP_WORKFLOW",
    "EP_RELEASE_ASSET", "CONSUMER_ADAPTER", "DJCONNECT_RETAINED",
    "GENERATED_LOCAL_ONLY", "EXCLUDED",
}
BASELINE_FIELDS = {
    "source_repository", "extraction_baseline_commit", "baseline_generated_at",
    "engineering_platform_version", "storage_schema_version", "consumer_contract_version",
    "bootstrap_contract_version", "operations_console_version",
}
SEMANTIC_MANIFEST_FIELDS = ("manifest_version", "classifications", "path_rules")
RULE_FIELDS = {"path", "classification", "reason_code", "ownership", "extraction_target", "dependency_notes"}

# These roots are deliberately owned by the audit, rather than inferred from
# manifest entries. They were derived from EP runtime, test, documentation,
# host, workflow and Operations Console entry points. A new file changes the
# frozen digest, so it cannot silently enter the extraction candidate set.
CANDIDATE_ROOTS = (
    "tools/engineering", "tests/engineering", "docs/engineering", "onboarding",
    "scripts/runner", ".github/workflows",
)
CANDIDATE_FILES = (
    "scripts/engineering/audit_ep_extraction_baseline.py",
    "docs/development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md",
    "docs/development/ENGINEERING_PLATFORM_CONSUMER_CONTRACT.md",
    "docs/development/ENGINEERING_PLATFORM_MIGRATION_REPORT.md",
    "docs/adr/0019-engineering-platform-central-installation-store.md",
)
IGNORED_NAMES = {".DS_Store", "__pycache__"}


def is_safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    windows_path = PureWindowsPath(value)
    return not path.is_absolute() and not windows_path.is_absolute() and ".." not in path.parts


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest(root: Path) -> dict:
    return json.loads((root / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text(encoding="utf-8"))


def candidate_universe(root: Path) -> list[str]:
    """Discover Phase-0 candidates independently of the manifest rules."""
    candidates: set[str] = set()
    for relative_root in CANDIDATE_ROOTS:
        base = root / relative_root
        if base.exists():
            for item in base.rglob("*"):
                if item.is_file() and not any(part in IGNORED_NAMES for part in item.parts):
                    candidates.add(item.relative_to(root).as_posix())
    for relative_file in CANDIDATE_FILES:
        if (root / relative_file).is_file():
            candidates.add(relative_file)
    return sorted(candidates)


def universe_digest(candidates: list[str]) -> str:
    return hashlib.sha256("\n".join(candidates).encode("utf-8")).hexdigest()


def manifest_semantic_digest(manifest: dict) -> str:
    """Hash the classification control separately from repository discovery."""
    control = {field: manifest.get(field) for field in SEMANTIC_MANIFEST_FIELDS}
    encoded = json.dumps(control, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def path_matches(path: str, rule_path: str) -> bool:
    return path == rule_path or path.startswith(f"{rule_path}/")


def effective_rule(path: str, rules: list[dict]) -> tuple[dict | None, list[dict]]:
    """Resolve by most-specific path; equal specificity is deliberately ambiguous."""
    matches = [rule for rule in rules if path_matches(path, rule.get("path", ""))]
    if not matches:
        return None, []
    longest = max(len(rule["path"]) for rule in matches)
    winners = [rule for rule in matches if len(rule["path"]) == longest]
    return (winners[0] if len(winners) == 1 else None), winners


def validate(manifest: dict, root: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("manifest_version") != 2:
        errors.append("manifest_version must be 2")
    if manifest.get("manifest_semantic_digest") != manifest_semantic_digest(manifest):
        errors.append("manifest semantic drift")
    baseline = manifest.get("baseline")
    if not isinstance(baseline, dict) or set(baseline) != BASELINE_FIELDS:
        errors.append("baseline does not have the required fields")
    elif not all(isinstance(baseline[field], (str, int)) and baseline[field] != "" for field in BASELINE_FIELDS):
        errors.append("baseline has an invalid value")
    if set(manifest.get("classifications", [])) != CLASSIFICATIONS:
        errors.append("manifest classifications do not match the closed vocabulary")
    rules = manifest.get("path_rules", [])
    if not isinstance(rules, list) or not rules:
        return errors + ["path_rules must be a non-empty list"]
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        if set(rule) != RULE_FIELDS:
            errors.append(f"rule {index} does not have the required fields")
            continue
        path = rule["path"]
        if not is_safe_relative_path(path):
            errors.append(f"rule {index} has an unsafe path")
        elif path in seen:
            errors.append(f"duplicate canonical path: {path}")
        else:
            seen.add(path)
            if not (root / path).exists():
                errors.append(f"missing required classified path: {path}")
        if rule["classification"] not in CLASSIFICATIONS:
            errors.append(f"rule {index} has an invalid classification")
        if not isinstance(rule["reason_code"], str) or not rule["reason_code"]:
            errors.append(f"rule {index} has an invalid reason code")
        if not isinstance(rule["ownership"], str) or not rule["ownership"]:
            errors.append(f"rule {index} has an invalid ownership")
        target = rule["extraction_target"]
        requires_target = rule["classification"] in CLASSIFICATIONS - {"DJCONNECT_RETAINED", "GENERATED_LOCAL_ONLY", "EXCLUDED"}
        if requires_target and (not isinstance(target, str) or not target):
            errors.append(f"rule {index} requires an extraction target")
        if not requires_target and target is not None:
            errors.append(f"rule {index} must not define an extraction target")
        if not isinstance(rule["dependency_notes"], str) or not rule["dependency_notes"]:
            errors.append(f"rule {index} has invalid dependency notes")

    candidates = candidate_universe(root)
    actual_digest = universe_digest(candidates)
    if manifest.get("candidate_universe_digest") != actual_digest:
        errors.append(f"candidate universe drift: expected {manifest.get('candidate_universe_digest')}, actual {actual_digest}")
    unclassified, ambiguous = [], []
    for path in candidates:
        winner, winners = effective_rule(path, rules)
        if winner is None:
            (ambiguous if winners else unclassified).append(path)
    if unclassified:
        errors.append(f"unclassified candidates ({len(unclassified)}): {', '.join(unclassified[:8])}")
    if ambiguous:
        errors.append(f"ambiguous candidates ({len(ambiguous)}): {', '.join(ambiguous[:8])}")
    return errors


def projection(manifest: dict, root: Path) -> dict:
    candidates = candidate_universe(root)
    rules = manifest["path_rules"]
    effective = [effective_rule(path, rules)[0] for path in candidates]
    classifications = {name: sum(rule is not None and rule["classification"] == name for rule in effective) for name in sorted(CLASSIFICATIONS)}
    operations = [path for path in candidates if path.startswith("tools/engineering/assets/") or path in {"tools/engineering/dashboard.py", "tools/engineering/dashboard_state.py", "tools/engineering/dashboard_configuration.py", "tools/engineering/live_status.py", "tests/engineering/dashboard.spec.mjs", "tests/engineering/dashboard_status_store.test.mjs"}]
    product_python = [path for path, rule in zip(candidates, effective) if rule and rule["classification"] == "EP_PRODUCT_SOURCE" and path.endswith(".py")]
    imports: list[str] = []
    for relative in product_python:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.append(node.module)
    standard = set(sys.stdlib_module_names)
    home_assistant = [name for name in imports if name == "homeassistant" or name.startswith("homeassistant.")]
    djconnect = [name for name in imports if name == "djconnect" or name.startswith("djconnect.")]
    repository_local = [name for name in imports if name.split(".")[0] in {"custom_components", "onboarding", "scripts"}]
    ep_internal = [name for name in imports if name == "tools" or name.startswith("tools.engineering")]
    known = standard | {name.split(".")[0] for name in home_assistant + djconnect + repository_local + ep_internal}
    unknown = [name for name in imports if name.split(".")[0] not in known]
    return {
        "baseline": manifest["baseline"], "candidate_universe_count": len(candidates),
        "candidate_universe_digest": universe_digest(candidates),
        "manifest_semantic_digest": manifest["manifest_semantic_digest"],
        "classified_exactly_once": sum(rule is not None for rule in effective),
        "unclassified": sum(rule is None and not effective_rule(path, rules)[1] for path, rule in zip(candidates, effective)),
        "ambiguous": sum(rule is None and bool(effective_rule(path, rules)[1]) for path, rule in zip(candidates, effective)),
        "classifications": classifications,
        "operations_console": {"candidate_paths": len(operations), "classified_exactly_once": sum(effective_rule(path, rules)[0] is not None for path in operations)},
        "import_audit": {
            "ep_product_source_files": classifications["EP_PRODUCT_SOURCE"],
            "python_source_files_inspected": len(product_python), "imports_classified": len(imports),
            "unknown_imports": len(unknown), "djconnect_runtime_imports": len(djconnect),
            "home_assistant_runtime_imports": len(home_assistant), "repository_local_support_imports": len(repository_local),
            "extraction_blocking_imports": len(djconnect) + len(home_assistant) + len(repository_local),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--projection", action="store_true")
    args = parser.parse_args(argv)
    root = repository_root()
    manifest = load_manifest(root)
    errors = validate(manifest, root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if args.projection:
        print(json.dumps(projection(manifest, root), indent=2, sort_keys=True))
    elif args.check or not args.projection:
        print("EP extraction baseline manifest: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
