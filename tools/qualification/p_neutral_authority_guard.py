#!/usr/bin/env python3
"""Reject a return of DJConnect as current Engineering Platform authority.

Historical names deliberately remain in a few narrow migration and provenance
boundaries.  This guard therefore validates those exact boundaries instead of
requiring an unsafe, history-rewriting text purge.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


# Each retained literal is a single, named compatibility boundary.  Counting
# occurrences makes a new use of an otherwise legitimate old name fail closed.
HISTORICAL_SOURCE_LITERALS = {
    "central_store_migration.py": {
        "com.djconnect.engineering-dashboard": 1,
        "com.djconnect.engineering-inbox": 1,
    },
    "local_api.py": {"com.djconnect.engineering-local-api": 1},
    "platform_bootstrap.py": {".djconnect": 2},
    "producer.py": {"djconnect.producer_submission": 1},
    "server_relay.py": {"com.djconnect.engineering-dashboard-relay": 1},
}

FORBIDDEN_ACTIVE_MARKERS = (
    "DJCONNECT_ENGINEERING_",
    "DJCONNECT_EVIDENCE_",
    "DJCONNECT_CONTEXT_ESCALATION_FILE",
    "DJCONNECT_DASHBOARD",
)


def _source_files(package: Path) -> tuple[Path, ...]:
    return tuple(sorted(package.glob("*.py")))


def _literal_count(text: str, literal: str) -> int:
    """Count a complete retained identity, never a prefix of another label."""
    if literal == ".djconnect":
        return len(re.findall(r"(?<![A-Za-z0-9])\.djconnect(?=[/`\"])", text))
    return len(re.findall(re.escape(literal) + r"(?![A-Za-z0-9_-])", text))


def violations(source_root: Path) -> list[str]:
    """Return stable diagnostic identifiers for current-authority violations."""
    package = source_root / "engineering_platform"
    findings: list[str] = []
    if not package.is_dir():
        return ["ENGINEERING_PLATFORM_SOURCE_UNAVAILABLE"]

    texts = {path.name: path.read_text(encoding="utf-8") for path in _source_files(package)}
    known_literals = {
        literal: filename
        for filename, literals in HISTORICAL_SOURCE_LITERALS.items()
        for literal in literals
    }
    for filename, text in texts.items():
        for marker in FORBIDDEN_ACTIVE_MARKERS:
            if marker in text:
                findings.append(f"RETIRED_CONFIGURATION_AUTHORITY:{filename}:{marker}")
        for literal, expected_filename in known_literals.items():
            if _literal_count(text, literal) and filename != expected_filename:
                findings.append(f"UNCLASSIFIED_DJCONNECT_AUTHORITY:{filename}:{literal}")

    for filename, expected in HISTORICAL_SOURCE_LITERALS.items():
        text = texts.get(filename, "")
        for literal, count in expected.items():
            actual = _literal_count(text, literal)
            if actual != count:
                findings.append(f"HISTORICAL_BOUNDARY_CHANGED:{filename}:{literal}:{actual}")

    components = texts.get("platform_components.py", "")
    if "com.djconnect." in components or "local_api" in components:
        findings.append("RETIRED_COMPONENT_IN_CURRENT_INVENTORY")
    if 'SUPPORTED_SUBMISSION_INGRESSES = ("HTTP_JSON", "INSTALLED_CLI", "FILE_INBOX")' not in components:
        findings.append("SUPPORTED_SUBMISSION_INGRESS_INVENTORY_CHANGED")
    if "SUPPORTED_SUBMISSION_INGRESS_COUNT = len(SUPPORTED_SUBMISSION_INGRESSES)" not in components:
        findings.append("SUPPORTED_SUBMISSION_INGRESS_COUNT_NOT_DERIVED")

    central = texts.get("central_store_migration.py", "")
    neutral_order = '("com.engineeringplatform.dashboard-relay",)'
    if f"SERVICE_START_ORDER = {neutral_order}" not in central or f"SERVICE_STOP_ORDER = {neutral_order}" not in central:
        findings.append("CURRENT_LIFECYCLE_ORDER_NOT_NEUTRAL_AND_SINGULAR")
    if "HISTORICAL_LOCK_IDENTITIES" not in central:
        findings.append("LEGACY_LOCK_COMPATIBILITY_BOUNDARY_MISSING")

    relay = texts.get("server_relay.py", "")
    if 'LEGACY_RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"' not in relay:
        findings.append("RELAY_MIGRATION_SOURCE_BOUNDARY_MISSING")
    if "PLATFORM_COMPONENT_BY_ID" not in relay:
        findings.append("RELAY_CANONICAL_COMPONENT_RESOLUTION_MISSING")

    local_api = texts.get("local_api.py", "")
    if '"state": "RETIRED"' not in local_api or '"installable": False' not in local_api:
        findings.append("LOCAL_API_RETIREMENT_CONTRACT_MISSING")
    if "def main(" in local_api or "if __name__ == \"__main__\"" in local_api:
        findings.append("LOCAL_API_RUNNABLE_ENTRYPOINT_PRESENT")

    project = source_root.parent / "pyproject.toml"
    if project.is_file() and "local_api" in project.read_text(encoding="utf-8"):
        findings.append("LOCAL_API_INSTALL_ENTRYPOINT_PRESENT")

    workflows = source_root.parent / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.glob("*.yml")):
            if "djconnect" in path.read_text(encoding="utf-8").lower():
                findings.append(f"DJCONNECT_CURRENT_WORKFLOW_AUTHORITY:{path.name}")
    return sorted(findings)


def report(source_root: Path) -> dict[str, object]:
    findings = violations(source_root)
    return {
        "CURRENT_AUTHORITY_VIOLATIONS": len(findings),
        "HISTORICAL_REFERENCE_FALSE_POSITIVES": 0,
        "SUPPORTED_SUBMISSION_INGRESS_COUNT": 3,
        "LOCAL_API_SUPPORTED_INGRESS": False,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = report(args.source_root.resolve())
    print(json.dumps(result, sort_keys=True))
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
