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
    startup_event: str
    restart_supported: bool = False
    critical: bool = False
    lifecycle_label: str | None = None


PLATFORM_COMPONENTS = (
    PlatformComponent("ep_server", "component.ep_server", "DAEMON", "platform", "EP_SERVER_ACTIVE", "EP_SERVER_UNAVAILABLE", "EP_SERVER_ENDPOINT", "ep_server_started", False, True),
    PlatformComponent("platform_database", "component.platform_database", "STORAGE", "platform", "PLATFORM_DATABASE_HEALTHY", "PLATFORM_DATABASE_UNAVAILABLE", "PLATFORM_DATABASE_STORAGE", "central_log_store_ready", False, True),
    PlatformComponent("lifecycle_worker", "component.lifecycle_worker", "IN_PROCESS_COMPONENT", "platform", "LIFECYCLE_WORKER_ACTIVE", "LIFECYCLE_WORKER_UNAVAILABLE", "LIFECYCLE_WORKER_SERVER_HOSTED", "lifecycle_worker_started", False, True),
    PlatformComponent("operations_console", "component.operations_console", "UI_SERVICE", "access", "OPERATIONS_CONSOLE_AVAILABLE", "OPERATIONS_CONSOLE_UNAVAILABLE", "OPERATIONS_CONSOLE_SERVER_NATIVE", "operations_console_available"),
    PlatformComponent(
        "dashboard_relay", "component.dashboard_relay", "UI_SERVICE", "access",
        "DASHBOARD_RELAY_ACTIVE", "DASHBOARD_RELAY_UNAVAILABLE",
        "DASHBOARD_RELAY_SERVER_NATIVE", "dashboard_relay_available",
        restart_supported=True,
        lifecycle_label="com.djconnect.engineering-dashboard-relay",
    ),
    PlatformComponent("http_ingress", "transport.http", "TRANSPORT", "ingress", "HTTP_INGRESS_HEALTHY", "HTTP_INGRESS_DOWN", "CENTRAL_LISTENER_ENDPOINT", "http_ingress_available", False, True),
    PlatformComponent("cli_ingress", "transport.cli", "TRANSPORT", "ingress", "CLI_INGRESS_AVAILABLE", "CLI_INGRESS_DEGRADED", "CANONICAL_SUBMISSION_COMPATIBILITY", "cli_ingress_available", False, True),
    PlatformComponent("file_inbox_ingress", "transport.file", "TRANSPORT", "ingress", "FILE_INGRESS_RUNNING", "FILE_INGRESS_STOPPED", "FILE_INBOX_HEARTBEAT", "file_inbox_service_started"),
)
PLATFORM_COMPONENT_BY_ID = {component.id: component for component in PLATFORM_COMPONENTS}
PLATFORM_COMPONENT_IDS = frozenset(PLATFORM_COMPONENT_BY_ID)
# Route consumers import this value instead of duplicating a literal inventory.
PLATFORM_COMPONENT_ROUTE_PATTERN = "(?:" + "|".join(component.id for component in PLATFORM_COMPONENTS) + ")"
