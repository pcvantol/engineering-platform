"""Qualification guard for the canonical Console route ownership matrix."""
from __future__ import annotations

import argparse
from pathlib import Path

from engineering_platform.console_route_ownership import OWNERS, PLATFORM, ROUTE_OWNERSHIP_MATRIX, route_owners

_REPRESENTATIVE_PATHS = (
    ("GET", "/"), ("GET", "/assets/dashboard.js"), ("GET", "/health"),
    ("GET", "/api/platform-status"), ("GET", "/api/dashboard-snapshot"), ("GET", "/api/events"),
    ("GET", "/api/components/ep_server/details"), ("GET", "/api/components/platform_database/details"),
    ("GET", "/api/components/lifecycle_worker/details"), ("GET", "/api/components/operations_console/details"),
    ("GET", "/api/components/dashboard_relay/details"), ("GET", "/api/components/file_inbox_ingress/details"), ("GET", "/api/logs/all"),
    ("GET", "/api/provider-login-status"), ("POST", "/api/provider-login/repair"),
    ("GET", "/api/execution-runtime-status"), ("POST", "/api/execution-runtime/repair"),
    ("GET", "/api/host-admin/diagnostics"),
    ("GET", "/api/configuration"), ("POST", "/api/configuration"),
    ("GET", "/api/prompt-history"), ("POST", "/api/execution-retry"),
    ("GET", "/healthz"), ("POST", "/api/runtime-directory/open"),
)
_PLATFORM_DISPATCH_MARKERS = (
    'request.path == "/api/provider-login-status"', 'request.path == "/api/execution-runtime-status"',
    'request.path == "/api/execution-runtime/repair"', 'request.path == "/api/provider-login/repair"',
    'request.path == "/api/provider-login/logout"', 'request.path == "/api/configuration" and method == "do_POST"',
)


def violations(source_root: Path) -> list[str]:
    findings: list[str] = []
    for route in ROUTE_OWNERSHIP_MATRIX:
        if route.owner not in OWNERS or not route.component:
            findings.append("AMBIGUOUS_ROUTE_OWNERSHIP")
    if any(len(route_owners(method, path)) != 1 for method, path in _REPRESENTATIVE_PATHS):
        findings.append("AMBIGUOUS_ROUTE_OWNERSHIP")
    families = {"provider_login", "execution_runtime", "platform_components", "server_settings", "provider_capacity"}
    scopes: dict[str, set[str]] = {}
    for route in ROUTE_OWNERSHIP_MATRIX:
        if route.component in families:
            scopes.setdefault(route.component, set()).add(route.owner)
    if any(owners != {PLATFORM} for owners in scopes.values()):
        findings.append("COMPONENT_ROUTE_SCOPE_INCONSISTENT")
    server = (source_root / "engineering_platform" / "server.py").read_text(encoding="utf-8")
    selected_position = server.find('selected = self.headers.get("X-Engineering-Platform-Project")')
    if selected_position < 0 or any((position := server.find(marker)) < 0 or position > selected_position for marker in _PLATFORM_DISPATCH_MARKERS):
        findings.append("PLATFORM_ROUTE_PROJECT_DELEGATION")
    if any(marker in server for marker in ("dashboard.handler(", "_console_root(", "open_storage(", "StateStore(")):
        findings.append("PLATFORM_ROUTE_CHECKOUT_DEPENDENCY")
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    findings = violations(parser.parse_args(argv).source_root.resolve())
    if findings:
        print("\n".join(findings)); return 1
    print("PLATFORM_ROUTE_PROJECT_DELEGATION=0")
    print("PLATFORM_ROUTE_CHECKOUT_DEPENDENCY=0")
    print("AMBIGUOUS_ROUTE_OWNERSHIP=0")
    print("COMPONENT_ROUTE_SCOPE_CONSISTENT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
