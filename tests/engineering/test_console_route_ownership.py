from pathlib import Path
import tempfile
import unittest

from engineering_platform.console_route_ownership import HISTORICAL_UNREACHABLE, HOST_ADMIN, PLATFORM, PROJECT, ROUTE_OWNERSHIP_MATRIX, route_owner
from tools.qualification import console_route_ownership_guard as guard

SOURCE_ROOT = Path(__file__).parents[2] / "src"


class ConsoleRouteOwnershipTest(unittest.TestCase):
    def test_matrix_is_closed_and_unambiguous(self) -> None:
        self.assertTrue(ROUTE_OWNERSHIP_MATRIX)
        self.assertTrue(all(route.owner in {PLATFORM, PROJECT, HOST_ADMIN, HISTORICAL_UNREACHABLE, "TRANSPORT_INTERNAL"} and route.component for route in ROUTE_OWNERSHIP_MATRIX))

    def test_host_admin_diagnostic_has_its_own_non_project_owner(self) -> None:
        self.assertEqual(route_owner("GET", "/api/host-admin/diagnostics").owner, HOST_ADMIN)

    def test_platform_routes_keep_owner_when_project_is_selected(self) -> None:
        for method, path in (("GET", "/api/provider-login-status"), ("POST", "/api/provider-login/repair"), ("GET", "/api/execution-runtime-status"), ("POST", "/api/execution-runtime/repair"), ("GET", "/api/components/file_inbox_ingress/details"), ("POST", "/api/components/dashboard_relay/restart"), ("GET", "/api/logs/all"), ("GET", "/api/configuration")):
            self.assertEqual(route_owner(method, path).owner, PLATFORM, path)

    def test_retired_component_log_routes_cannot_become_project_routes(self) -> None:
        for method, path in (("GET", "/api/logs/inbox"), ("POST", "/api/logs/dashboard")):
            self.assertEqual(route_owner(method, path).owner, HISTORICAL_UNREACHABLE, path)

    def test_qualification_guard_passes_for_installed_console_source(self) -> None:
        self.assertEqual(guard.violations(SOURCE_ROOT), [])

    def test_guard_detects_project_dispatch_before_platform_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src" / "engineering_platform"; source.mkdir(parents=True)
            (source / "server.py").write_text('selected = self.headers.get("X-Engineering-Platform-Project")\n', encoding="utf-8")
            self.assertIn("PLATFORM_ROUTE_PROJECT_DELEGATION", guard.violations(source.parent))
