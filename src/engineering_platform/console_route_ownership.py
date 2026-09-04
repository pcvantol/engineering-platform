"""Canonical ownership for Console and Console-adjacent HTTP routes.

Ownership is an authority decision, not a presentation decision. A selected
project may tailor a PLATFORM projection, but never its authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

PLATFORM = "PLATFORM"
PROJECT = "PROJECT"
TRANSPORT_INTERNAL = "TRANSPORT_INTERNAL"
HISTORICAL_UNREACHABLE = "HISTORICAL_UNREACHABLE"
OWNERS = frozenset({PLATFORM, PROJECT, TRANSPORT_INTERNAL, HISTORICAL_UNREACHABLE})


@dataclass(frozen=True)
class ConsoleRoute:
    methods: tuple[str, ...]
    pattern: str
    owner: str
    component: str
    description: str
    selected_project_presentation_only: bool = False

    def matches(self, method: str, path: str) -> bool:
        return method.upper() in self.methods and re.fullmatch(self.pattern, path) is not None


# Closed list: each addition must select an owner and component family.
ROUTE_OWNERSHIP_MATRIX: tuple[ConsoleRoute, ...] = (
    ConsoleRoute(("GET",), r"/", PLATFORM, "console_shell", "Console shell", True),
    ConsoleRoute(("GET",), r"/(?:favicon\.ico|apple-touch-icon(?:-precomposed)?\.png)", PLATFORM, "console_shell", "Console icons"),
    ConsoleRoute(("GET",), r"/assets/[A-Za-z0-9_./-]+", PLATFORM, "console_shell", "Installed Console assets"),
    ConsoleRoute(("GET",), r"/health", PLATFORM, "platform_components", "Platform Components health", True),
    ConsoleRoute(("GET",), r"/api/platform-status", PLATFORM, "platform_components", "Platform Components projection", True),
    ConsoleRoute(("GET",), r"/api/(?:dashboard-snapshot|status|events)", PLATFORM, "platform_components", "Platform status projection or stream", True),
    ConsoleRoute(("GET",), r"/api/components/(?:ep_server|platform_database|lifecycle_worker|operations_console|dashboard_relay|http_ingress|cli_ingress|file_inbox_ingress)/details", PLATFORM, "platform_components", "Component detail popout", True),
    ConsoleRoute(("GET",), r"/api/(?:logs/dashboard|logs/inbox)", PLATFORM, "platform_components", "Platform component logs", True),
    ConsoleRoute(("GET",), r"/api/provider-login-status", PLATFORM, "provider_login", "Provider readiness", True),
    ConsoleRoute(("POST",), r"/api/provider-login/(?:repair|logout)", PLATFORM, "provider_login", "Provider login action", True),
    ConsoleRoute(("GET",), r"/api/execution-runtime-status", PLATFORM, "execution_runtime", "Execution runtime readiness", True),
    ConsoleRoute(("POST",), r"/api/execution-runtime/repair", PLATFORM, "execution_runtime", "Execution runtime repair", True),
    ConsoleRoute(("GET",), r"/api/provider-capacity", PLATFORM, "provider_capacity", "Provider capacity", True),
    ConsoleRoute(("GET", "POST"), r"/api/provider-capacity/configuration", PLATFORM, "provider_capacity", "Provider capacity settings", True),
    ConsoleRoute(("GET",), r"/api/github-rate-limit", PLATFORM, "provider_capacity", "Provider rate-limit diagnostics", True),
    ConsoleRoute(("GET", "POST"), r"/api/configuration", PLATFORM, "server_settings", "Server settings", True),
    ConsoleRoute(("GET",), r"/api/(?:process-metrics|usage)", PLATFORM, "platform_components", "Platform diagnostics", True),
    ConsoleRoute(("GET",), r"/api/central-database/download", PLATFORM, "server_settings", "Central database backup", True),
    ConsoleRoute(("POST",), r"/api/central-database/open-directory", PLATFORM, "server_settings", "Central database action", True),
    ConsoleRoute(("GET", "POST"), r"/api/central-database/configuration", PLATFORM, "server_settings", "Central database maintenance settings", True),
    ConsoleRoute(("GET",), r"/v1/operations/projects", PLATFORM, "operations", "Operations project listing"),
    ConsoleRoute(("GET",), r"/api/prompt-history", PROJECT, "project_history", "Project run history"),
    ConsoleRoute(("GET",), r"/api/prompt-history/[a-z0-9][a-z0-9-]{0,63}/(?:report|chat|details)", PROJECT, "project_history", "Project run detail"),
    ConsoleRoute(("GET",), r"/api/telemetry/[0-9]{4}-[0-9]{2}-[0-9]{2}", PROJECT, "project_history", "Project telemetry detail"),
    ConsoleRoute(("POST",), r"/api/execution-(?:dismiss|retry)", PROJECT, "project_execution", "Project execution action"),
    ConsoleRoute(("POST",), r"/api/dashboard-translate", PROJECT, "project_console", "Project Console translation"),
    ConsoleRoute(("GET",), r"/diagnostics/topology", TRANSPORT_INTERNAL, "transport", "Transport topology diagnostic"),
    ConsoleRoute(("GET",), r"/(?:healthz|readyz)", TRANSPORT_INTERNAL, "transport", "Transport probe"),
    ConsoleRoute(("POST",), r"/v1/projects/[A-Za-z0-9._-]+/submissions", TRANSPORT_INTERNAL, "transport", "Authenticated submission ingress"),
    ConsoleRoute(("POST",), r"/v1/agent/(?:pair|register|heartbeat|attachment)", TRANSPORT_INTERNAL, "transport", "Agent transport control"),
    ConsoleRoute(("POST",), r"/api/runtime-directory/open", HISTORICAL_UNREACHABLE, "historical_unreachable", "Retired checkout runtime action"),
)


def route_owners(method: str, path: str) -> tuple[ConsoleRoute, ...]:
    return tuple(route for route in ROUTE_OWNERSHIP_MATRIX if route.matches(method, path))


def route_owner(method: str, path: str) -> ConsoleRoute | None:
    matches = route_owners(method, path)
    if len(matches) > 1:
        raise RuntimeError(f"AMBIGUOUS_ROUTE_OWNERSHIP: {method} {path}")
    return matches[0] if matches else None
