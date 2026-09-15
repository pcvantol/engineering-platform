from pathlib import Path
import shutil
import tempfile
import unittest

from engineering_platform.console_route_ownership import HISTORICAL_UNREACHABLE, HOST_ADMIN, PLATFORM, PROJECT, ROUTE_OWNERSHIP_MATRIX, route_owner
from engineering_platform.platform_components import RETIRED_COMPONENT_ALIASES
from tools.qualification import console_route_ownership_guard as guard

SOURCE_ROOT = Path(__file__).parents[2] / "src"


class ConsoleRouteOwnershipTest(unittest.TestCase):
    def test_matrix_is_closed_and_unambiguous(self) -> None:
        self.assertTrue(ROUTE_OWNERSHIP_MATRIX)
        self.assertTrue(all(route.owner in {PLATFORM, PROJECT, HOST_ADMIN, HISTORICAL_UNREACHABLE, "TRANSPORT_INTERNAL"} and route.component for route in ROUTE_OWNERSHIP_MATRIX))

    def test_host_admin_diagnostic_has_its_own_non_project_owner(self) -> None:
        self.assertEqual(route_owner("GET", "/api/host-admin/diagnostics").owner, HOST_ADMIN)

    def test_platform_routes_keep_owner_when_project_is_selected(self) -> None:
        for method, path in (("GET", "/api/health"), ("GET", "/api/provider-login-status"), ("POST", "/api/provider-login/repair"), ("GET", "/api/execution-runtime-status"), ("POST", "/api/execution-runtime/repair"), ("GET", "/api/components/file_inbox_ingress/details"), ("POST", "/api/components/dashboard_relay/restart"), ("GET", "/api/logs/all"), ("GET", "/api/configuration")):
            self.assertEqual(route_owner(method, path).owner, PLATFORM, path)

    def test_central_advisory_analysis_routes_are_project_scoped(self) -> None:
        for method, path in (
            ("GET", "/api/prompt-history/run-a/analysis"),
            ("POST", "/api/prompt-history/run-a/analysis-retry"),
            ("POST", "/api/codex-chat"),
            ("POST", "/api/codex-chat/clear"),
            ("GET", "/api/execution-diagnostic/current"),
        ):
            self.assertEqual(route_owner(method, path).owner, PROJECT, path)

    def test_retired_component_log_routes_cannot_become_project_routes(self) -> None:
        for method, path in (("GET", "/api/logs/inbox"), ("POST", "/api/logs/dashboard")):
            self.assertEqual(route_owner(method, path).owner, HISTORICAL_UNREACHABLE, path)

    def test_retired_component_aliases_have_no_supported_route_owner(self) -> None:
        for alias in RETIRED_COMPONENT_ALIASES:
            for method, suffix in (("GET", "details"), ("POST", "restart")):
                self.assertEqual(
                    route_owner(method, f"/api/components/{alias}/{suffix}").owner,
                    HISTORICAL_UNREACHABLE,
                    f"{method} {alias}",
                )

    def test_retired_checkout_actions_are_explicitly_unreachable(self) -> None:
        for method, path in (
            ("GET", "/api/codex-cli-update"),
            ("POST", "/api/codex-cli-update"),
            ("POST", "/api/rate-limit-reset"),
            ("POST", "/api/telemetry/clear"),
            ("POST", "/api/status-reconciliation"),
            ("POST", "/api/execution-emergency-rollback"),
            ("POST", "/api/execution-merge-wait-abort"),
            ("POST", "/api/managed-branch-recovery"),
            ("POST", "/api/stale-git-lock-recovery"),
            ("POST", "/api/workspace-switch-to-main"),
            ("POST", "/api/configuration/file-inbox/relocate"),
            ("POST", "/api/open-pull-requests/123/repair-failed-checks"),
        ):
            self.assertEqual(route_owner(method, path).owner, HISTORICAL_UNREACHABLE, path)

    def test_complete_central_data_transfer_family_is_platform_owned(self) -> None:
        for path in (
            "/api/central-data/relocate",
            "/api/central-data/relocate/browse",
            "/api/central-data/relocate/discard",
            "/api/central-data/import",
        ):
            self.assertEqual(route_owner("POST", path).owner, PLATFORM, path)

    def test_dashboard_mutation_endpoints_all_have_a_central_owner(self) -> None:
        """Every browser action is either owned or deliberately retired."""
        routes = (
            ("POST", "/api/audit/user-action", PLATFORM),
            ("GET", "/api/central-data/export", PLATFORM),
            ("POST", "/api/central-data/relocate/browse", PLATFORM),
            ("POST", "/api/central-data/relocate/discard", PLATFORM),
            ("POST", "/api/central-data/relocate", PLATFORM),
            ("POST", "/api/central-data/import", PLATFORM),
            ("POST", "/api/logs/all", PLATFORM),
            ("POST", "/api/components/dashboard_relay/restart", PLATFORM),
            ("POST", "/api/provider-login/repair", PLATFORM),
            ("POST", "/api/provider-login/logout", PLATFORM),
            ("POST", "/api/execution-runtime/repair", PLATFORM),
            ("POST", "/api/provider-capacity/configuration", PLATFORM),
            ("POST", "/api/configuration", PLATFORM),
            ("POST", "/api/central-database/configuration", PLATFORM),
            ("POST", "/api/execution-dismiss", PROJECT),
            ("POST", "/api/execution-retry", PROJECT),
            ("POST", "/api/queue-disposition", PROJECT),
            ("POST", "/api/codex-chat", PROJECT),
            ("POST", "/api/codex-chat/clear", PROJECT),
            ("POST", "/api/prompt-history/run-a/analysis-retry", PROJECT),
            ("POST", "/api/dashboard-translate", PROJECT),
        )
        for method, path, owner in routes:
            self.assertEqual(route_owner(method, path).owner, owner, path)

    def test_qualification_guard_passes_for_installed_console_source(self) -> None:
        self.assertEqual(guard.violations(SOURCE_ROOT), [])

    def test_guard_detects_project_dispatch_before_platform_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src" / "engineering_platform"; source.mkdir(parents=True)
            (source / "server.py").write_text('selected = self.headers.get("X-Engineering-Platform-Project")\n', encoding="utf-8")
            self.assertIn("PLATFORM_ROUTE_PROJECT_DELEGATION", guard.violations(source.parent))

    def test_guard_requires_imported_translation_module_and_its_real_route(self) -> None:
        for missing in ("import", "asset", "route"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "engineering_platform"
                assets = source / "assets"
                assets.mkdir(parents=True)
                shutil.copyfile(SOURCE_ROOT / "engineering_platform/server.py", source / "server.py")
                dashboard = (SOURCE_ROOT / "engineering_platform/assets/dashboard.js").read_text()
                translation = (SOURCE_ROOT / "engineering_platform/assets/dashboard_translation.mjs").read_text()
                if missing == "import":
                    dashboard = dashboard.replace('import { createDynamicEvidenceLocalizer } from "./dashboard_translation.mjs";', "")
                if missing == "route":
                    translation = translation.replace("/api/dashboard-translate", "/api/wrong-route")
                (assets / "dashboard.js").write_text(dashboard)
                if missing != "asset":
                    (assets / "dashboard_translation.mjs").write_text(translation)
                self.assertIn("DASHBOARD_FETCH_CONTRACT_INCOMPLETE", guard.violations(source.parent))
