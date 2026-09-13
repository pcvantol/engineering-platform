"""Qualification guard for the canonical Console route ownership matrix."""
from __future__ import annotations

import argparse
from pathlib import Path

from engineering_platform.console_route_ownership import OWNERS, PLATFORM, ROUTE_OWNERSHIP_MATRIX, route_owners

_DASHBOARD_REQUEST_CONTRACTS = (
    ("GET", "/"), ("GET", "/assets/dashboard.js"), ("GET", "/health"),
    ("GET", "/api/platform-status"), ("GET", "/api/dashboard-snapshot"), ("GET", "/api/events"),
    ("GET", "/api/components/ep_server/details"), ("GET", "/api/components/platform_database/details"),
    ("GET", "/api/components/lifecycle_worker/details"), ("GET", "/api/components/operations_console/details"),
    ("GET", "/api/components/dashboard_relay/details"), ("GET", "/api/components/file_inbox_ingress/details"), ("GET", "/api/logs/all"),
    ("GET", "/api/provider-login-status"), ("POST", "/api/provider-login/repair"),
    ("GET", "/api/execution-runtime-status"), ("POST", "/api/execution-runtime/repair"),
    ("GET", "/api/host-admin/diagnostics"),
    ("GET", "/api/configuration"), ("POST", "/api/configuration"),
    ("GET", "/api/prompt-history"), ("GET", "/api/prompt-history/example-run/analysis"),
    ("POST", "/api/prompt-history/example-run/analysis-retry"), ("POST", "/api/execution-retry"),
    ("GET", "/api/prompt-history/example-run/report"),
    ("GET", "/api/prompt-history/example-run/chat"),
    ("GET", "/api/prompt-history/example-run/details"),
    ("GET", "/api/execution-diagnostic/current"),
    ("GET", "/api/telemetry/2026-01-01"),
    ("GET", "/api/provider-capacity"), ("GET", "/api/github-rate-limit"),
    ("POST", "/api/dashboard-translate"), ("POST", "/api/audit/user-action"),
    ("POST", "/api/queue-disposition"), ("POST", "/api/codex-chat"),
    ("POST", "/api/codex-chat/clear"), ("POST", "/api/telemetry/clear"),
    ("POST", "/api/rate-limit-reset"), ("POST", "/api/queue-defer"),
    ("POST", "/api/status-reconciliation-preview"), ("POST", "/api/status-reconciliation"),
    ("POST", "/api/execution-emergency-rollback"),
    ("GET", "/api/open-pull-requests"),
    ("POST", "/api/open-pull-requests/1/owner-authorization"),
    ("POST", "/api/open-pull-requests/1/repair-failed-checks"),
    ("POST", "/api/execution-dismiss"), ("POST", "/api/execution-retry"), ("POST", "/api/managed-branch-recovery"),
    ("POST", "/api/stale-git-lock-recovery"), ("POST", "/api/workspace-switch-to-main"),
    ("POST", "/api/workspace-switch-to-worktree"),
    ("POST", "/api/execution-merge-wait-abort"), ("POST", "/api/execution-merge-status-check"),
    ("POST", "/api/managed-branch-synchronization"),
    ("GET", "/api/provider-login-status"), ("GET", "/api/execution-runtime-status"),
    ("POST", "/api/provider-login/repair"), ("POST", "/api/provider-login/logout"),
    ("POST", "/api/execution-runtime/repair"), ("GET", "/api/configuration"),
    ("POST", "/api/configuration"), ("POST", "/api/provider-capacity/configuration"),
    ("GET", "/api/logs/all"), ("POST", "/api/logs/all"),
    ("GET", "/api/components/ep_server/details"), ("POST", "/api/components/ep_server/restart"),
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
    if any(len(route_owners(method, path)) != 1 for method, path in _DASHBOARD_REQUEST_CONTRACTS):
        findings.append("AMBIGUOUS_ROUTE_OWNERSHIP")
    families = {"provider_login", "execution_runtime", "platform_components", "server_settings", "provider_capacity"}
    scopes: dict[str, set[str]] = {}
    for route in ROUTE_OWNERSHIP_MATRIX:
        if route.component in families:
            scopes.setdefault(route.component, set()).add(route.owner)
    if any(owners != {PLATFORM} for owners in scopes.values()):
        findings.append("COMPONENT_ROUTE_SCOPE_INCONSISTENT")
    server = (source_root / "engineering_platform" / "server.py").read_text(encoding="utf-8")
    dashboard_path = source_root / "engineering_platform" / "assets" / "dashboard.js"
    dashboard = dashboard_path.read_text(encoding="utf-8") if dashboard_path.exists() else None
    # The list above is deliberately the dashboard's complete HTTP surface,
    # including explicitly refused legacy clicks.  These anchors make a newly
    # added fetch fail qualification until it has both an owner and a test.
    required_anchors = (
        "/api/provider-capacity", "/api/codex-cli-update", "/api/rate-limit-reset",
        "/api/queue-disposition", "/api/queue-defer", "/api/codex-chat",
        "/api/prompt-history/", "/api/github-rate-limit", "/api/dashboard-translate",
        "/api/status-reconciliation", "/api/execution-emergency-rollback",
        "/api/open-pull-requests", "/api/dashboard-snapshot", "/api/events",
        "/api/logs/", "/api/components/", "/api/telemetry/", "/api/provider-login",
        "/api/execution-runtime", "/api/configuration", "/api/audit/user-action",
        "/api/execution-dismiss", "/api/managed-branch", "/api/stale-git-lock",
        "/api/workspace-switch", "/api/execution-merge", "/health",
    )
    if dashboard is not None and any(anchor not in dashboard for anchor in required_anchors):
        findings.append("DASHBOARD_FETCH_CONTRACT_INCOMPLETE")
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
