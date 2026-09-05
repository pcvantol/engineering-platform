from __future__ import annotations

import ast
from pathlib import Path
import unittest

from engineering_platform.platform_components import PLATFORM_COMPONENT_IDS

class TransportAuthorityGuardTest(unittest.TestCase):
    """Source-level canaries for the supported ingress authority boundary."""

    def test_supported_adapters_have_no_local_operational_authority(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        for name in ("submission_cli.py", "file_inbox.py"):
            source = (source_root / name).read_text(encoding="utf-8")
            self.assertNotIn("sqlite3", source, name)
            self.assertNotIn("StateStore", source, name)
            self.assertNotIn("EngineeringRunner", source, name)
            self.assertNotIn("inbox_watcher", source, name)
        file_tree = ast.parse((source_root / "file_inbox.py").read_text(encoding="utf-8"))
        imported = {node.names[0].name for node in ast.walk(file_tree) if isinstance(node, ast.Import) and node.names}
        self.assertNotIn("sqlite3", imported)

    def test_supported_transport_paths_converge_at_canonical_submission_service(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        server_source = (source_root / "server.py").read_text(encoding="utf-8")
        self.assertIn("submission_service.request_from_mapping", server_source)
        self.assertIn("submission_service.submit(connection, request)", server_source)
        self.assertIn("_admit_server_owned_file_inbox", server_source)
        self.assertIn("file_inbox.FileInboxService", server_source)

    def test_supported_runtime_modules_do_not_import_the_retired_watcher(self) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        supported = ("server.py", "dashboard.py", "parity_lifecycle_dispatcher.py", "emergency_recovery.py")
        for name in supported:
            source = (source_root / name).read_text(encoding="utf-8")
            self.assertNotIn("inbox_watcher", source, name)

    def test_supported_component_log_writers_use_canonical_component_ids(self) -> None:
        """A writer cannot silently recreate a retired Inbox/Dashboard stream."""
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        supported = (
            "dashboard.py",
            "host_preflight.py",
            "file_inbox.py",
            "dependabot_producer.py",
            "server.py",
        )
        for name in supported:
            tree = ast.parse((source_root / name).read_text(encoding="utf-8"))
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Name) or call.func.id != "component_logger":
                    continue
                if len(call.args) < 2 or not isinstance(call.args[1], ast.Constant):
                    continue
                component = call.args[1].value
                if isinstance(component, str):
                    self.assertIn(component, PLATFORM_COMPONENT_IDS, f"{name}: {component}")

    def test_retired_root_bound_human_producer_paths_are_absent(self) -> None:
        """Human files enter only through Server-owned File Inbox intake."""
        source_root = Path(__file__).resolve().parents[2] / "src" / "engineering_platform"
        for retired in ("workspace_inbox_api.py", "human_text_ingress.py"):
            self.assertFalse((source_root / retired).exists(), retired)
        # Forensic attribution deliberately retains historical path strings as
        # evidence; this guard covers executable ingress/runtime modules.
        self.assertFalse((source_root / "inbox_watcher.py").exists())
        for name in ("server.py", "file_inbox.py", "submission_intake.py", "submission_cli.py"):
            source_path = source_root / name
            source = source_path.read_text(encoding="utf-8")
            self.assertNotIn("workspace_inbox_api", source, source_path.name)
            self.assertNotIn("human_text_ingress", source, source_path.name)

    def test_installed_qualification_guards_retired_runtime_entrypoints(self) -> None:
        """The installed-wheel gate must reject, not merely hide, old daemons."""
        root = Path(__file__).resolve().parents[2]
        qualification = (root / "tools" / "qualification" / "p_transport_installed_ingress_matrix.py").read_text(encoding="utf-8")
        self.assertIn("installed_runtime_topology", qualification)
        for retired in (
            "engineering-platform-file-inbox",
            "engineering-platform-dashboard",
            "engineering-inbox-watcher",
            "engineering_platform/inbox_watcher.py",
        ):
            self.assertIn(retired, qualification)

    def test_browser_fixture_uses_the_server_boundary_and_no_local_finder_route(self) -> None:
        """Dashboard browser evidence must not revive the retired direct listener."""
        root = Path(__file__).resolve().parents[2]
        fixture = (root / "tests" / "engineering" / "dashboard.spec.mjs").read_text(encoding="utf-8")
        self.assertIn("from engineering_platform.server import _HealthHandler, initialize", fixture)
        self.assertNotIn("engineering_platform.dashboard import DashboardHTTPServer, handler", fixture)
        dashboard = (root / "src" / "engineering_platform" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn('fetch("/api/open-local-directory"', dashboard)
        self.assertNotIn('fetch("/api/runtime-directory/open"', dashboard)

    def test_direct_dashboard_module_has_no_runtime_entrypoint(self) -> None:
        """The historical wrapper cannot become an installed listener again."""
        root = Path(__file__).resolve().parents[2]
        dashboard = (root / "src" / "engineering_platform" / "dashboard.py").read_text(encoding="utf-8")
        packaging = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('if __name__ == "__main__"', dashboard)
        self.assertNotIn("engineering_platform.dashboard:main", packaging)

    def test_retired_local_branch_cleanup_is_absent_from_supported_console(self) -> None:
        """The Console must not own destructive checkout-local branch cleanup."""
        root = Path(__file__).resolve().parents[2]
        dashboard = (root / "src" / "engineering_platform" / "dashboard.py").read_text(encoding="utf-8")
        asset = (root / "src" / "engineering_platform" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        stylesheet = (root / "src" / "engineering_platform" / "assets" / "dashboard.css").read_text(encoding="utf-8")
        for source in (dashboard, asset, stylesheet):
            self.assertNotIn("stale-local-branch-cleanup", source)
            self.assertNotIn("workspaceBranchCleanup", source)
            self.assertNotIn("workspace-branch-cleanup", source)
