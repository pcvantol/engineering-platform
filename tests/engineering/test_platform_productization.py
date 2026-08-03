from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tools.engineering.platform_api import (
    PlatformConfiguration,
    PlatformConfigurationError,
    execution_host_configuration,
    capabilities,
    provider_registry,
)
from unittest.mock import patch
from tools.engineering.platform_bootstrap import (
    _merge_legacy_workspace,
    _validate_legacy_merge,
    migrate_legacy_workspace,
    provision_workspace,
    render_template,
    validate_repository,
)


ROOT = Path(__file__).resolve().parents[2]


class PlatformProductizationTest(unittest.TestCase):
    def test_identity_and_configuration_are_canonical(self) -> None:
        configuration = PlatformConfiguration.load(ROOT)
        self.assertEqual(configuration.platform.id, "engineering-platform")
        self.assertEqual(configuration.platform.version, "1.5.0")
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
        with tempfile.TemporaryDirectory() as temporary, patch("tools.engineering.platform_api.shutil.which", return_value="/usr/local/bin/codex"):
            root = Path(temporary)
            target = root / "tools" / "engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                (ROOT / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            resolver = execution_host_configuration(root)
            self.assertEqual(resolver.resolve_runtime_prompt_transport().provider, "icloud_inbox")
            self.assertEqual(resolver.resolve_status_store(), root.resolve() / ".engineering" / "status")
            self.assertEqual(resolver.resolve_report_store(), root.resolve() / ".engineering" / "reports")
            self.assertEqual(resolver.resolve_log_store(), root.resolve() / ".engineering" / "logs")
            self.assertEqual(resolver.resolve_telemetry_store(), root.resolve() / ".engineering" / "engineering.db")
            self.assertEqual(resolver.resolve_runtime(), Path("/usr/local/bin/codex"))
            identity = resolver.resolve_execution_host_identity()
            self.assertEqual((identity.name, identity.runtime, identity.runtime_prompt_transport), ("Engineering Platform", "codex_cli", "icloud_inbox"))

    def test_execution_host_configuration_fails_closed_for_missing_or_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(PlatformConfigurationError):
                execution_host_configuration(root)
            target = root / "tools" / "engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(PlatformConfigurationError):
                execution_host_configuration(root)

    def test_workspace_provisioning_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "tools/engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                (ROOT / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            first = provision_workspace(root)
            second = provision_workspace(root)
            self.assertEqual(first, second)
            self.assertTrue(first["status"].is_dir())

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
            target = root / "tools/engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text((ROOT / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text())
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
            target = root / "tools/engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
                (ROOT / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.assertEqual(validate_repository(root).platform.id, "engineering-platform")

            destination = root / ".engineering" / "workspace-config.json"
            rendered = render_template(destination, {"{{WORKSPACE_ID}}": "demo"})
            self.assertEqual(rendered, destination)
            original = destination.read_text(encoding="utf-8")
            self.assertEqual(render_template(destination, {"{{WORKSPACE_ID}}": "changed"}).read_text(encoding="utf-8"), original)
