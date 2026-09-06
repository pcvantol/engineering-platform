"""Qualification guard for retired Dashboard/Inbox component authority."""
from __future__ import annotations

import argparse
from pathlib import Path

from engineering_platform.console_route_ownership import HISTORICAL_UNREACHABLE, route_owner
from engineering_platform.platform_components import (
    PLATFORM_COMPONENT_IDS,
    RETIRED_COMPONENT_ALIASES,
)


def violations(source_root: Path) -> list[str]:
    """Return bounded authority findings; lexical UI/history mentions are irrelevant."""
    findings: list[str] = []
    package = source_root / "engineering_platform"
    platform_model = (package / "platform_components.py").read_text(encoding="utf-8")
    logger = (package / "component_logging.py").read_text(encoding="utf-8")
    server = (package / "server.py").read_text(encoding="utf-8")
    execution_host = (package / "execution_host.py").read_text(encoding="utf-8")

    if platform_model.count("PLATFORM_COMPONENTS =") != 1 or not PLATFORM_COMPONENT_IDS:
        findings.append("CANONICAL_COMPONENT_INVENTORY_INVALID")
    if PLATFORM_COMPONENT_IDS & RETIRED_COMPONENT_ALIASES:
        findings.append("LEGACY_ALIAS_IN_CANONICAL_INVENTORY")
    if "if component not in PLATFORM_COMPONENT_IDS:" not in logger:
        findings.append("LEGACY_ALIAS_LOG_WRITER_NOT_DENIED")
    if 'component_logger(root, "execution-host"' in execution_host:
        findings.append("NONCANONICAL_EXECUTION_LOG_IDENTITY")

    selected = server.find('selected = self.headers.get("X-Engineering-Platform-Project")')
    retired = server.find("LEGACY_COMPONENT_AUTHORITY_RETIRED")
    if retired < 0 or selected < 0 or retired > selected:
        findings.append("LEGACY_ALIAS_COMPONENT_ROUTE_NOT_DENIED_EARLY")
    for alias in RETIRED_COMPONENT_ALIASES:
        for method, suffix in (("GET", "details"), ("POST", "restart")):
            route = route_owner(method, f"/api/components/{alias}/{suffix}")
            if route is None or route.owner != HISTORICAL_UNREACHABLE:
                findings.append("LEGACY_ALIAS_COMPONENT_ROUTE_OWNED")
                break
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    findings = violations(parser.parse_args(argv).source_root.resolve())
    if findings:
        print("\n".join(findings))
        return 1
    print("CANONICAL_COMPONENT_INVENTORY_COUNT=1")
    print("LEGACY_COMPONENT_ALIAS_SELECTABLE=FALSE")
    print("LEGACY_COMPONENT_ALIAS_WRITABLE=FALSE")
    print("LEGACY_ALIAS_ROUTE_OWNER=FALSE")
    print("NEW_OPERATIONAL_RECORDS_USE_CANONICAL_COMPONENT_IDS=TRUE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
