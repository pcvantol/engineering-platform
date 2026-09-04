"""The sole installed-Server inventory of supported Platform Components."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformComponent:
    """Stable product, health and logging identity for one Server component."""

    id: str
    name_key: str
    kind: str
    group: str
    active_status: str
    inactive_status: str
    detail_code: str
    restart_supported: bool = False
    critical: bool = False


PLATFORM_COMPONENTS = (
    PlatformComponent("ep_server", "component.ep_server", "DAEMON", "platform", "EP_SERVER_ACTIVE", "EP_SERVER_UNAVAILABLE", "EP_SERVER_ENDPOINT", True, True),
    PlatformComponent("platform_database", "component.platform_database", "STORAGE", "platform", "PLATFORM_DATABASE_HEALTHY", "PLATFORM_DATABASE_UNAVAILABLE", "PLATFORM_DATABASE_STORAGE", False, True),
    PlatformComponent("lifecycle_worker", "component.lifecycle_worker", "IN_PROCESS_COMPONENT", "platform", "LIFECYCLE_WORKER_ACTIVE", "LIFECYCLE_WORKER_UNAVAILABLE", "LIFECYCLE_WORKER_SERVER_HOSTED", False, True),
    PlatformComponent("operations_console", "component.operations_console", "UI_SERVICE", "access", "OPERATIONS_CONSOLE_AVAILABLE", "OPERATIONS_CONSOLE_UNAVAILABLE", "OPERATIONS_CONSOLE_SERVER_NATIVE"),
    PlatformComponent("dashboard_relay", "component.dashboard_relay", "UI_SERVICE", "access", "DASHBOARD_RELAY_ACTIVE", "DASHBOARD_RELAY_UNAVAILABLE", "DASHBOARD_RELAY_SERVER_NATIVE", True),
    PlatformComponent("http_ingress", "transport.http", "TRANSPORT", "ingress", "HTTP_INGRESS_HEALTHY", "HTTP_INGRESS_DOWN", "CENTRAL_LISTENER_ENDPOINT", False, True),
    PlatformComponent("cli_ingress", "transport.cli", "TRANSPORT", "ingress", "CLI_INGRESS_AVAILABLE", "CLI_INGRESS_DEGRADED", "CANONICAL_SUBMISSION_COMPATIBILITY", False, True),
    PlatformComponent("file_inbox_ingress", "transport.file", "TRANSPORT", "ingress", "FILE_INGRESS_RUNNING", "FILE_INGRESS_STOPPED", "FILE_INBOX_HEARTBEAT", True),
)
PLATFORM_COMPONENT_BY_ID = {component.id: component for component in PLATFORM_COMPONENTS}
PLATFORM_COMPONENT_IDS = frozenset(PLATFORM_COMPONENT_BY_ID)
# These records predate the model. They remain readable, but never appear as
# selectable identities or writer targets.
LEGACY_COMPONENT_ALIASES = {
    "dashboard": "operations_console",
    "inbox": "file_inbox_ingress",
    "execution-host": "lifecycle_worker",
}
