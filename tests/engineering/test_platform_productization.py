from __future__ import annotations

import os
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from engineering_platform.platform_api import (
    PlatformConfiguration,
    PlatformConfigurationError,
    RUNTIME_EXECUTABLE_ENVIRONMENT,
    execution_host_configuration,
    capabilities,
    provider_registry,
)
from engineering_platform.historical_dashboard_configuration import update_inbox_root
from engineering_platform.platform_version import EngineeringPlatformManifest
from engineering_platform.resources import PackageResourceError, package_path, package_text
from unittest.mock import patch
from engineering_platform.platform_bootstrap import (
    _discard_inactive_component_locks,
    _history_count,
    _link_workspace,
    _merge_databases,
    _merge_workspace,
    _worktree_roots,
    _merge_legacy_workspace,
    _validate_legacy_merge,
    migrate_legacy_workspace,
    migrate_worktree_workspace,
    provision_runtime_workspace,
    provision_workspace,
    render_template,
    validate_repository,
)
from engineering_platform.storage import open_storage


ROOT = Path(__file__).resolve().parents[2]


class PlatformProductizationTest(unittest.TestCase):
    def test_identity_and_configuration_are_canonical(self) -> None:
        configuration = PlatformConfiguration.load(ROOT)
        self.assertEqual(configuration.platform.id, "engineering-platform")
        self.assertEqual(configuration.platform.version, "2.0.0")
        self.assertEqual(configuration.workspace.id, "djconnect")
        self.assertEqual(configuration.providers["runtime"], "codex_cli")

    def test_public_api_has_all_productization_capabilities(self) -> None:
        registered = set(capabilities())
        self.assertTrue({"runner", "runtime_provider", "repository_provider", "service_manager_provider", "remote_submission_provider", "private_remote_access_provider"} <= registered)

    def test_provider_registry_is_configuration_backed(self) -> None:
        providers = provider_registry(ROOT)
        self.assertEqual(providers["repository"]["selected"], "github")
        self.assertIn("status", providers["runtime"])

    def test_execution_host_configuration_resolves_capabilities_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "managed-codex" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            managed_prefix = executable.parent.parent
            with patch("engineering_platform.platform_api.engineering_platform_codex_cli_prefix", return_value=managed_prefix):
                resolver = execution_host_configuration(root)
                self.assertEqual(resolver.resolve_runtime_prompt_transport().provider, "icloud_inbox")
                self.assertEqual(resolver.resolve_status_store(), root.resolve() / ".engineering" / "status")
                self.assertEqual(resolver.resolve_report_store(), root.resolve() / ".engineering" / "reports")
                self.assertEqual(resolver.resolve_log_store(), root.resolve() / ".engineering" / "logs")
                self.assertEqual(resolver.resolve_telemetry_store(), root.resolve() / ".engineering" / "engineering.db")
                self.assertEqual(resolver.resolve_runtime(), executable)
                identity = resolver.resolve_execution_host_identity()
                self.assertEqual((identity.name, identity.runtime, identity.runtime_prompt_transport), ("Engineering Platform", "codex_cli", "icloud_inbox"))

    def test_execution_host_ignores_retired_local_inbox_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transport = root / "transport"
            (transport / "Inbox").mkdir(parents=True)
            update_inbox_root(root, str(transport))
            with patch("engineering_platform.platform_api.Path.home", return_value=root / "home"):
                self.assertEqual(
                    execution_host_configuration(root).resolve_runtime_prompt_transport().inbox,
                    root / "home" / "Library/Mobile Documents/com~apple~CloudDocs/Engineering Platform/Inbox",
                )

    def test_execution_host_default_inbox_uses_engineering_platform_icloud_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            with patch("engineering_platform.platform_api.Path.home", return_value=home):
                inbox = execution_host_configuration(root).resolve_runtime_prompt_transport().inbox
        self.assertEqual(
            inbox,
            home / "Library/Mobile Documents/com~apple~CloudDocs/Engineering Platform/Inbox",
        )

    def test_runtime_environment_pins_the_resolved_launcher_for_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "managed-codex" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with patch("engineering_platform.platform_api.engineering_platform_codex_cli_prefix", return_value=executable.parent.parent), patch.dict(os.environ, {RUNTIME_EXECUTABLE_ENVIRONMENT: "/opt/homebrew/bin/codex"}, clear=False):
                environment = execution_host_configuration(root).runtime_environment()

        self.assertEqual(environment[RUNTIME_EXECUTABLE_ENVIRONMENT], str(executable))
        self.assertEqual(environment["PATH"].split(":")[0], str(executable.parent))

    def test_execution_host_configuration_fails_closed_for_missing_or_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".engineering"
            local.mkdir()
            (local / "engineering-platform.local.json").write_text(
                json.dumps({"providers": {"runtime": "other"}}), encoding="utf-8"
            )
            with self.assertRaises(PlatformConfigurationError):
                execution_host_configuration(root)

    def test_package_default_configuration_does_not_require_a_project_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration = PlatformConfiguration.load(root)

            self.assertEqual(configuration.platform.id, "engineering-platform")
            self.assertFalse((root / "tools" / "engineering").exists())
            self.assertFalse((root / "src" / "engineering_platform").exists())

    def test_package_default_configuration_preserves_explicit_local_workspace_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provisioning_root = root / "projects"
            (root / ".engineering").mkdir()
            (root / ".engineering" / "engineering-platform.local.json").write_text(
                json.dumps({"workspace": {"provisioning_root": str(provisioning_root)}}),
                encoding="utf-8",
            )

            configuration = PlatformConfiguration.load(root)

            self.assertEqual(configuration.workspace.provisioning_root, str(provisioning_root))

    def test_missing_package_resource_fails_without_project_or_checkout_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "engineering_platform.resources.files",
            side_effect=lambda _: (_ for _ in ()).throw(PackageResourceError("missing package resource")),
        ):
            with self.assertRaises(PackageResourceError):
                package_text("ENGINEERING_PLATFORM_CONFIG.json")

    def test_package_version_resource_is_available_without_project_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            manifest = EngineeringPlatformManifest.load(package_path("ENGINEERING_PLATFORM_VERSION.json"))

            self.assertEqual(manifest.platform_version, "2.0.0")
            self.assertFalse((root / "src" / "engineering_platform").exists())

    def test_workspace_provisioning_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "src/engineering_platform"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                (ROOT / "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            first = provision_workspace(root)
            second = provision_workspace(root)
            self.assertEqual(first, second)
            self.assertTrue(first["status"].is_dir())

    def test_linked_worktrees_share_and_merge_engineering_history(self) -> None:
        """A dashboard worktree must retain history recorded from a source worktree."""
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            runtime = Path(temporary) / "runtime"
            common = repository / ".git"
            worktree_git = common / "worktrees" / "runtime"
            common.mkdir(parents=True)
            runtime.mkdir()
            (runtime / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
            worktree_git.mkdir(parents=True)
            (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
            for root in (repository, runtime):
                target = root / "src/engineering_platform"
                target.mkdir(parents=True)
                (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                    (ROOT / "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            for root, run_id in ((repository, "source-run"), (runtime, "runtime-run")):
                with open_storage(root) as connection:
                    connection.execute(
                        "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,updated_at) VALUES(?,?,?,?,?)",
                        (run_id, "COMPLETE", run_id, "2026-08-16T08:00:00+00:00", "2026-08-16T08:00:00+00:00"),
                    )

            workspace = migrate_worktree_workspace(runtime)

            self.assertEqual(workspace, (common / "engineering-platform").resolve())
            self.assertTrue((repository / ".engineering").is_symlink())
            self.assertTrue((runtime / ".engineering").is_symlink())
            with open_storage(runtime) as connection:
                run_ids = [row[0] for row in connection.execute(
                    "SELECT run_id FROM prompt_execution_history ORDER BY run_id"
                )]
            self.assertEqual(run_ids, ["runtime-run", "source-run"])
            self.assertEqual((repository / ".engineering").resolve(), workspace)

    def test_linked_worktree_migration_refuses_live_component_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            runtime = Path(temporary) / "runtime"
            common = repository / ".git"
            worktree_git = common / "worktrees" / "runtime"
            common.mkdir(parents=True)
            runtime.mkdir()
            (runtime / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
            worktree_git.mkdir(parents=True)
            (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
            lock = runtime / ".engineering" / "locks" / "dashboard.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text(json.dumps({"component": "dashboard", "pid": os.getpid()}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "running components to stop"):
                migrate_worktree_workspace(runtime)

    def test_runtime_workspace_reuses_valid_shared_store_while_dashboard_legacy_lock_exists(self) -> None:
        """Normal components never migrate another worktree while Dashboard is open."""
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            runtime = Path(temporary) / "runtime"
            common = repository / ".git"
            worktree_git = common / "worktrees" / "runtime"
            common.mkdir(parents=True)
            runtime.mkdir()
            (runtime / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
            worktree_git.mkdir(parents=True)
            (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
            for root in (repository, runtime):
                target = root / "src/engineering_platform"
                target.mkdir(parents=True)
                (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                    (ROOT / "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            shared = common / "engineering-platform"
            shared.mkdir()
            (runtime / ".engineering").symlink_to(shared, target_is_directory=True)
            lock = repository / ".engineering" / "locks" / "dashboard.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text(json.dumps({"component": "dashboard", "pid": os.getpid()}), encoding="utf-8")

            paths = provision_runtime_workspace(runtime)

            self.assertEqual(paths["workspace"], shared.resolve())
            self.assertTrue(paths["status"].is_dir())
            with self.assertRaisesRegex(RuntimeError, "running components to stop"):
                migrate_worktree_workspace(runtime)

    def test_worktree_discovery_ignores_broken_markers_and_history_count_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            common = root / ".git"
            shared = common / "engineering-platform"
            worktrees = common / "worktrees"
            root.mkdir(parents=True)
            worktrees.mkdir(parents=True)
            broken = worktrees / "broken"
            broken.mkdir()
            valid = worktrees / "valid"
            valid.mkdir()
            runtime = Path(temporary) / "runtime"
            runtime.mkdir()
            (runtime / ".git").write_text("gitdir: marker\n", encoding="utf-8")
            (valid / "gitdir").write_text(str(runtime / ".git"), encoding="utf-8")

            with patch("engineering_platform.platform_bootstrap.shared_workspace_store", return_value=shared):
                self.assertEqual(set(_worktree_roots(root)), {root.resolve(), runtime.resolve()})

            workspace = root / ".engineering"
            workspace.mkdir()
            self.assertEqual(_history_count(workspace), 0)
            database = workspace / "engineering.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE prompt_execution_history(run_id TEXT)")
                connection.execute("INSERT INTO prompt_execution_history VALUES('one')")
            self.assertEqual(_history_count(workspace), 1)
            database.write_text("not sqlite", encoding="utf-8")
            self.assertEqual(_history_count(workspace), 0)

    def test_workspace_helpers_merge_evidence_and_reject_invalid_links_or_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "new.txt").write_text("new", encoding="utf-8")
            (source / "same.txt").write_text("same", encoding="utf-8")
            (destination / "same.txt").write_text("same", encoding="utf-8")
            (source / "nested").mkdir()
            (source / "nested" / "nested.txt").write_text("nested", encoding="utf-8")
            (destination / "nested").mkdir()

            _merge_workspace(source, destination)

            self.assertEqual((destination / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(list(source.iterdir()), [])

            worktree = root / "worktree"
            shared = root / "shared"
            worktree.mkdir()
            shared.mkdir()
            _link_workspace(worktree, shared)
            _link_workspace(worktree, shared)
            self.assertTrue((worktree / ".engineering").is_symlink())
            wrong = root / "wrong"
            wrong.mkdir()
            (wrong / ".engineering").symlink_to(root / "other", target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unexpected shared store"):
                _link_workspace(wrong, shared)

            invalid_locks = root / "invalid-locks"
            invalid_locks.mkdir()
            lock_target = root / "lock-target"
            lock_target.write_text("not a directory", encoding="utf-8")
            (invalid_locks / "locks").symlink_to(lock_target)
            with self.assertRaisesRegex(RuntimeError, "invalid component-lock"):
                _discard_inactive_component_locks(invalid_locks)

    def test_database_merge_rejects_incompatible_schemas_and_merges_simple_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.db"
            destination = root / "destination.db"
            for database in (source, destination):
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE evidence(key TEXT PRIMARY KEY, value TEXT)")
            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO evidence VALUES('source', 'preserved')")
            with sqlite3.connect(destination) as connection:
                connection.execute("INSERT INTO evidence VALUES('destination', 'current')")

            _merge_databases(source, destination)

            with sqlite3.connect(destination) as connection:
                self.assertEqual(
                    connection.execute("SELECT key, value FROM evidence ORDER BY key").fetchall(),
                    [("destination", "current"), ("source", "preserved")],
                )

            incompatible = root / "incompatible.db"
            with sqlite3.connect(incompatible) as connection:
                connection.execute("CREATE TABLE other(key TEXT PRIMARY KEY)")
            with self.assertRaisesRegex(RuntimeError, "incompatible database schemas"):
                _merge_databases(incompatible, destination)

    def test_legacy_workspace_migrates_without_losing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".djconnect"
            report = legacy / "reports" / "run.md"
            report.parent.mkdir(parents=True)
            report.write_text("evidence", encoding="utf-8")

            workspace = migrate_legacy_workspace(root)

            self.assertEqual(workspace, (root / ".engineering").resolve())
            self.assertEqual((workspace / "reports" / "run.md").read_text(encoding="utf-8"), "evidence")
            self.assertFalse(legacy.exists())

    def test_legacy_workspace_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".djconnect"
            workspace = root / ".engineering"
            legacy.mkdir()
            workspace.mkdir()
            (legacy / "status.json").write_text("legacy", encoding="utf-8")
            (workspace / "status.json").write_text("canonical", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                migrate_legacy_workspace(root)

            self.assertTrue(legacy.exists())

    def test_unknown_local_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "src/engineering_platform"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text((ROOT / "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json").read_text())
            local = root / ".engineering"
            local.mkdir()
            (local / "engineering-platform.local.json").write_text(json.dumps({"providers": {"runtime": "other"}}))
            with self.assertRaises(ValueError):
                PlatformConfiguration.load(root)

    def test_legacy_merge_keeps_identical_evidence_and_moves_unique_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy"
            destination = root / "canonical"
            source.mkdir()
            destination.mkdir()
            (source / "same.txt").write_text("same", encoding="utf-8")
            (destination / "same.txt").write_text("same", encoding="utf-8")
            (source / "nested").mkdir()
            (source / "nested" / "new.txt").write_text("new", encoding="utf-8")
            (destination / "nested").mkdir()

            _validate_legacy_merge(source, destination)
            _merge_legacy_workspace(source, destination)

            self.assertFalse((source / "same.txt").exists())
            self.assertEqual((destination / "nested" / "new.txt").read_text(encoding="utf-8"), "new")

    def test_legacy_merge_rejects_file_type_and_content_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "conflict").write_text("legacy", encoding="utf-8")
            (destination / "conflict").mkdir()
            with self.assertRaises(RuntimeError):
                _validate_legacy_merge(source, destination)

            (destination / "conflict").rmdir()
            (destination / "conflict").write_text("canonical", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _validate_legacy_merge(source, destination)

    def test_legacy_logs_and_qualification_are_archived_without_overwriting_live_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".djconnect"
            workspace = root / ".engineering"
            (legacy / "logs").mkdir(parents=True)
            (legacy / "logs" / "inbox.log").write_text("historic", encoding="utf-8")
            (legacy / "qualification").mkdir()
            (legacy / "qualification" / "result.json").write_text("historic", encoding="utf-8")
            (workspace / "logs").mkdir(parents=True)
            (workspace / "logs" / "inbox.log").write_text("live", encoding="utf-8")
            (workspace / "qualification").mkdir()
            (workspace / "qualification" / "result.json").write_text("live", encoding="utf-8")

            migrate_legacy_workspace(root)

            self.assertEqual((workspace / "logs" / "inbox.log").read_text(encoding="utf-8"), "live")
            self.assertEqual((workspace / "logs" / "legacy" / "inbox.log").read_text(encoding="utf-8"), "historic")
            self.assertEqual((workspace / "qualification" / "result.json").read_text(encoding="utf-8"), "live")
            self.assertEqual((workspace / "legacy" / "qualification" / "result.json").read_text(encoding="utf-8"), "historic")

    def test_repository_validation_and_template_rendering_are_fail_closed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                validate_repository(root)

            (root / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")
            (root / ".git").mkdir()
            target = root / "src/engineering_platform"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                (ROOT / "src/engineering_platform/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.assertEqual(validate_repository(root).platform.id, "engineering-platform")

            destination = root / ".engineering" / "workspace-config.json"
            rendered = render_template(destination, {"{{WORKSPACE_ID}}": "demo"})
            self.assertEqual(rendered, destination)
            original = destination.read_text(encoding="utf-8")
            self.assertEqual(render_template(destination, {"{{WORKSPACE_ID}}": "changed"}).read_text(encoding="utf-8"), original)
