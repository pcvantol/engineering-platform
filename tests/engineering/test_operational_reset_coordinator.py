"""Qualification matrix for the subprocess-only operational reset coordinator."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from engineering_platform import server


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "qualification" / "operational_reset_coordinator.py"
FIXTURE = ROOT / "tests" / "fixtures" / "operational_reset_coordinator_fixture_cli.py"
SPEC = importlib.util.spec_from_file_location("operational_reset_coordinator", MODULE)
assert SPEC and SPEC.loader
coordinator_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator_module
SPEC.loader.exec_module(coordinator_module)


class SimulatedCoordinatorCrash(BaseException):
    """Model process death after an owning effect and before receipt update."""


class OperationalResetCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # macOS exposes /var as a compatibility symlink.  Qualification uses
        # the canonical temporary path so the receipt store can reject every
        # caller-supplied symlink component without weakening the test.
        self.root = Path(self.temporary.name).resolve()
        self.receipts = self.root / "coordinator-receipts"
        self.forge_root = self.root / "forge-data"
        self.ep_root = self.root / "ep-data"
        self.ep_backup = self.root / "ep-backups"
        self.forge_cli = self.root / "forge-reset-fixture"
        self.ep_cli = self.root / "ep-reset-fixture"
        for source, target in ((FIXTURE, self.forge_cli), (FIXTURE, self.ep_cli)):
            shutil.copy2(source, target)
            target.chmod(0o700)
        self.config = coordinator_module.CoordinationConfig.create(
            coordinator_id="joint-reset-fixture-001",
            forge_cli=self.forge_cli,
            forge_data_root=self.forge_root,
            forge_operation_id="forge-reset-fixture-001",
            ep_cli=self.ep_cli,
            ep_data_root=self.ep_root,
            ep_operation_id="ep-reset-fixture-001",
            ep_backup_root=self.ep_backup,
        )
        self.store = coordinator_module.ReceiptStore(
            self.receipts, self.config.coordinator_id,
        )
        self.coordinator = coordinator_module.OperationalResetCoordinator(
            self.store, command_timeout_seconds=10,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare_both(self) -> None:
        self.assertEqual("BOTH_PREVIEWED", self.coordinator.preview(self.config)["state"])
        self.assertEqual("BACKUPS_VERIFIED", self.coordinator.prepare()["state"])
        self.assertEqual("PLANS_REVALIDATED", self.coordinator.revalidate()["state"])

    def apply_both(self, first: str = "forge") -> None:
        second = "engineering-platform" if first == "forge" else "forge"
        expected = "FORGE_APPLIED" if first == "forge" else "EP_APPLIED"
        self.assertEqual(expected, self.coordinator.apply(first)["state"])
        self.assertEqual("BOTH_APPLIED", self.coordinator.apply(second)["state"])

    def complete_both(self, first: str = "forge") -> dict[str, object]:
        self.prepare_both()
        self.apply_both(first)
        self.assertEqual("BOTH_VERIFIED", self.coordinator.verify()["state"])
        self.assertEqual("RESUME_AUTHORIZED", self.coordinator.authorize_resume()["state"])
        self.assertEqual("RESUME_AUTHORIZED", self.coordinator.finish(first)["state"])
        second = "engineering-platform" if first == "forge" else "forge"
        return self.coordinator.finish(second)

    def owning_state(self, root: Path) -> dict[str, object]:
        return json.loads((root / "fixture-owning-state.json").read_text(encoding="utf-8"))

    def command_log(self, root: Path) -> list[dict[str, object]]:
        return [
            json.loads(line) for line in (root / "fixture-command-log.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def test_both_owning_resets_succeed_and_receipt_is_private_and_complete(self) -> None:
        result = self.complete_both()

        self.assertEqual("COMPLETE", result["state"])
        self.assertEqual(self.config.forge_operation_id, result["products"]["forge"]["operation_id"])
        self.assertEqual(
            self.config.ep_operation_id,
            result["products"]["engineering-platform"]["operation_id"],
        )
        for product in coordinator_module.PRODUCTS:
            observed = result["products"][product]
            for field in ("target_digest", "plan_digest", "backup_digest", "result_digest"):
                self.assertRegex(observed[field], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(0o700, stat.S_IMODE(self.store.directory.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.store.receipt_path.stat().st_mode))
        forge_actions = [item["action"] for item in self.command_log(self.forge_root)]
        self.assertEqual(forge_actions[-2:], ["finish", "status"])

    def test_forge_succeeds_ep_fails_and_neither_product_is_finished(self) -> None:
        self.prepare_both()
        self.coordinator.apply("forge")
        (self.ep_root / "fail-apply").touch()

        with self.assertRaises(coordinator_module.ProductCommandError):
            self.coordinator.apply("engineering-platform")

        receipt = self.store.load()
        self.assertEqual("RECONCILIATION_REQUIRED", receipt["state"])
        self.assertEqual("APPLIED", self.owning_state(self.forge_root)["state"])
        self.assertEqual("AUTHORIZED", self.owning_state(self.ep_root)["state"])
        self.assertNotIn("finish", {item["action"] for item in self.command_log(self.forge_root)})
        self.assertNotIn("finish", {item["action"] for item in self.command_log(self.ep_root)})

    def test_ep_succeeds_forge_fails_and_neither_product_is_finished(self) -> None:
        self.prepare_both()
        self.coordinator.apply("engineering-platform")
        (self.forge_root / "fail-apply").touch()

        with self.assertRaises(coordinator_module.ProductCommandError):
            self.coordinator.apply("forge")

        receipt = self.store.load()
        self.assertEqual("RECONCILIATION_REQUIRED", receipt["state"])
        self.assertEqual("BACKUP_VERIFIED", self.owning_state(self.forge_root)["state"])
        self.assertEqual("DB_APPLIED", self.owning_state(self.ep_root)["state"])
        self.assertNotIn("finish", {item["action"] for item in self.command_log(self.forge_root)})
        self.assertNotIn("finish", {item["action"] for item in self.command_log(self.ep_root)})

    def test_coordinator_crash_after_owning_apply_reconciles_same_operations(self) -> None:
        self.prepare_both()

        def crash(boundary: str) -> None:
            if boundary == "after-forge-apply":
                raise SimulatedCoordinatorCrash()

        crashing = coordinator_module.OperationalResetCoordinator(
            self.store, command_timeout_seconds=10, fault_hook=crash,
        )
        with self.assertRaises(SimulatedCoordinatorCrash):
            crashing.apply("forge")
        self.assertEqual("PLANS_REVALIDATED", self.store.load()["state"])
        self.assertTrue(self.store.load()["milestones"]["forge_apply_started"])
        self.assertEqual("APPLIED", self.owning_state(self.forge_root)["state"])

        restarted = coordinator_module.OperationalResetCoordinator(
            self.store, command_timeout_seconds=10,
        )
        self.assertEqual("FORGE_APPLIED", restarted.reconcile()["state"])
        self.assertEqual("BOTH_APPLIED", restarted.apply("engineering-platform")["state"])

    def test_resume_is_forbidden_before_revalidation_and_before_product_apply_admission(self) -> None:
        self.assertEqual("BOTH_PREVIEWED", self.coordinator.preview(self.config)["state"])
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "plan revalidation"):
            self.coordinator.resume("forge")
        self.assertEqual(["preview"], [item["action"] for item in self.command_log(self.forge_root)])

        self.assertEqual("BACKUPS_VERIFIED", self.coordinator.prepare()["state"])
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "plan revalidation"):
            self.coordinator.resume("engineering-platform")
        self.assertNotIn("resume", {item["action"] for item in self.command_log(self.ep_root)})

        self.assertEqual("PLANS_REVALIDATED", self.coordinator.revalidate()["state"])
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "apply admission"):
            self.coordinator.resume("engineering-platform")
        self.assertNotIn("resume", {item["action"] for item in self.command_log(self.ep_root)})

    def test_resume_reconciles_only_the_durably_admitted_crashed_apply(self) -> None:
        self.prepare_both()

        def crash(boundary: str) -> None:
            if boundary == "after-forge-apply":
                raise SimulatedCoordinatorCrash()

        crashing = coordinator_module.OperationalResetCoordinator(
            self.store, command_timeout_seconds=10, fault_hook=crash,
        )
        with self.assertRaises(SimulatedCoordinatorCrash):
            crashing.apply("forge")

        restarted = coordinator_module.OperationalResetCoordinator(
            self.store, command_timeout_seconds=10,
        )
        self.assertEqual("FORGE_APPLIED", restarted.resume("forge")["state"])
        self.assertIn("resume", {item["action"] for item in self.command_log(self.forge_root)})
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "apply admission"):
            restarted.resume("engineering-platform")

    def test_product_process_restart_and_status_readback_use_durable_state(self) -> None:
        self.prepare_both()
        restarted = coordinator_module.OperationalResetCoordinator(
            coordinator_module.ReceiptStore(self.receipts, self.config.coordinator_id),
            command_timeout_seconds=10,
        )

        status = restarted.status()

        self.assertEqual("PLANS_REVALIDATED", status["state"])
        self.assertEqual("BACKUP_VERIFIED", status["owning_readback"]["forge"]["state"])
        self.assertEqual("AUTHORIZED", status["owning_readback"]["engineering-platform"]["state"])
        for root in (self.forge_root, self.ep_root):
            pids = [item["pid"] for item in self.command_log(root)]
            self.assertEqual(len(pids), len(set(pids)))

    def test_bound_cli_bytes_are_rechecked_before_maintenance(self) -> None:
        self.coordinator.preview(self.config)
        with self.forge_cli.open("a", encoding="utf-8") as stream:
            stream.write("\n# changed after coordinated preview\n")

        with self.assertRaisesRegex(
            coordinator_module.CoordinatorError, "owning CLI bytes changed",
        ):
            self.coordinator.prepare()

        self.assertEqual("BOTH_PREVIEWED", self.store.load()["state"])
        for root in (self.forge_root, self.ep_root):
            self.assertEqual(["preview"], [item["action"] for item in self.command_log(root)])

    def test_blocked_preview_can_refresh_same_receipt_before_maintenance(self) -> None:
        (self.ep_root / "preview-blocked").parent.mkdir(parents=True, exist_ok=True)
        (self.ep_root / "preview-blocked").touch()
        first = self.coordinator.preview(self.config)
        first_ep_plan = first["products"]["engineering-platform"]["plan_digest"]
        self.assertEqual("BLOCKED", first["products"]["engineering-platform"]["state"])

        (self.ep_root / "preview-blocked").unlink()
        (self.ep_root / "preview-plan-version").write_text("quiesced", encoding="utf-8")
        refreshed = self.coordinator.preview(self.config)

        self.assertEqual("BOTH_PREVIEWED", refreshed["state"])
        self.assertEqual("ALLOWED", refreshed["products"]["engineering-platform"]["state"])
        self.assertNotEqual(
            first_ep_plan, refreshed["products"]["engineering-platform"]["plan_digest"],
        )
        self.assertEqual(
            ["preview", "preview"],
            [item["action"] for item in self.command_log(self.ep_root)],
        )
        self.assertIn(
            "both exact owning plans preview refreshed",
            [item["event"] for item in self.store.load()["events"]],
        )
        self.assertEqual("BACKUPS_VERIFIED", self.coordinator.prepare()["state"])

    def test_target_identity_change_is_rejected_during_revalidation(self) -> None:
        self.coordinator.preview(self.config)
        self.coordinator.prepare()
        (self.forge_root / "changed-target").touch()

        with self.assertRaisesRegex(
            coordinator_module.ProductCommandError, "REVALIDATION_CHANGED",
        ):
            self.coordinator.revalidate()

        self.assertEqual("RECONCILIATION_REQUIRED", self.store.load()["state"])

    def test_delayed_old_callbacks_are_projected_historical_without_new_work(self) -> None:
        result = self.complete_both()
        self.assertEqual("COMPLETE", result["state"])
        (self.forge_root / "delayed-old-callback").touch()
        (self.ep_root / "delayed-old-callback").touch()

        status = self.coordinator.status()

        for product in coordinator_module.PRODUCTS:
            owning = status["owning_readback"][product]
            self.assertEqual("REJECTED_HISTORICAL", owning["details"]["old_callback_projection"])
            self.assertEqual(0, owning["details"]["new_missions"])
            self.assertEqual(0, owning["details"]["new_provider_invocations"])
            self.assertEqual(0, owning["details"]["new_submissions"])

    def test_no_hidden_mission_provider_or_submission_command_is_invoked(self) -> None:
        self.complete_both(first="engineering-platform")
        allowed = {"preview", "prepare", "revalidate", "apply", "verify", "finish", "status"}
        for root in (self.forge_root, self.ep_root):
            log = self.command_log(root)
            self.assertTrue({item["action"] for item in log} <= allowed)
            state = self.coordinator.status()["owning_readback"]
            for owning in state.values():
                self.assertEqual(
                    {"missions": 0, "provider_invocations": 0, "submissions": 0},
                    owning["counts"],
                )

    def test_finish_is_forbidden_until_both_products_verify(self) -> None:
        self.prepare_both()
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "both verifications"):
            self.coordinator.authorize_resume()
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "forbidden"):
            self.coordinator.finish("forge")
        self.coordinator.apply("forge")
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "both owning resets"):
            self.coordinator.verify()
        self.coordinator.apply("engineering-platform")
        self.coordinator.verify()
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "forbidden"):
            self.coordinator.finish("forge")

    def test_finish_reproves_both_product_readiness_before_first_finish(self) -> None:
        self.prepare_both()
        self.apply_both()
        self.coordinator.verify()
        self.coordinator.authorize_resume()
        (self.ep_root / "fail-verify").touch()

        with self.assertRaises(coordinator_module.ProductCommandError):
            self.coordinator.finish("forge")

        self.assertEqual("RECONCILIATION_REQUIRED", self.store.load()["state"])
        self.assertNotIn("finish", {item["action"] for item in self.command_log(self.forge_root)})
        self.assertEqual("VERIFIED", self.owning_state(self.forge_root)["state"])
        self.assertEqual("VERIFIED", self.owning_state(self.ep_root)["state"])

    def test_sequential_finish_failure_preserves_partial_state_for_explicit_reconcile(self) -> None:
        self.prepare_both()
        self.apply_both()
        self.coordinator.verify()
        self.coordinator.authorize_resume()
        self.assertEqual("RESUME_AUTHORIZED", self.coordinator.finish("forge")["state"])
        (self.ep_root / "fail-finish").touch()

        with self.assertRaises(coordinator_module.ProductCommandError):
            self.coordinator.finish("engineering-platform")

        self.assertEqual("COMPLETED", self.owning_state(self.forge_root)["state"])
        self.assertEqual("VERIFIED", self.owning_state(self.ep_root)["state"])
        self.assertEqual("RESUME_AUTHORIZED", self.coordinator.reconcile()["state"])
        (self.ep_root / "fail-finish").unlink()
        self.assertEqual("COMPLETE", self.coordinator.finish("engineering-platform")["state"])
        forge_actions = [item["action"] for item in self.command_log(self.forge_root)]
        self.assertEqual(forge_actions.count("verify"), 2)
        self.assertGreaterEqual(forge_actions.count("status"), 3)

    def test_receipt_symlink_and_concurrent_lock_fail_closed(self) -> None:
        unsafe_root = self.root / "unsafe-receipts"
        unsafe_root.symlink_to(self.receipts, target_is_directory=True)
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "symlink"):
            coordinator_module.ReceiptStore(unsafe_root, "joint-reset-fixture-002")
        self.coordinator.preview(self.config)
        with self.store.locked():
            with self.assertRaisesRegex(coordinator_module.CoordinatorError, "another coordinator"):
                coordinator_module.OperationalResetCoordinator(
                    coordinator_module.ReceiptStore(
                        self.receipts, self.config.coordinator_id,
                    ), command_timeout_seconds=10,
                ).status()

    def test_status_rejects_a_symlink_in_an_existing_receipt_parent(self) -> None:
        self.coordinator.preview(self.config)
        unsafe_parent = self.root / "unsafe-parent"
        unsafe_parent.symlink_to(self.root, target_is_directory=True)

        def unsafe_status() -> object:
            unsafe_store = coordinator_module.ReceiptStore(
                unsafe_parent / self.receipts.name, self.config.coordinator_id,
            )
            return coordinator_module.OperationalResetCoordinator(
                unsafe_store, command_timeout_seconds=10,
            ).status()

        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "symlink component"):
            unsafe_status()

        relocated = self.root / "relocated-receipts"
        self.receipts.rename(relocated)
        self.receipts.symlink_to(relocated, target_is_directory=True)
        with self.assertRaisesRegex(coordinator_module.CoordinatorError, "symlink component"):
            self.coordinator.status()

    def test_coordinator_source_has_no_product_import_or_database_access(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertTrue({"engineering_platform", "forge", "sqlite3"}.isdisjoint(imported))
        self.assertNotIn("sqlite3.connect", source)
        self.assertIn("subprocess.run", source)

    def test_real_ep_owning_preview_smoke_uses_subprocess_boundary(self) -> None:
        real_ep_root = self.root / "real-ep-data"
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
        initialized = subprocess.run(
            [
                sys.executable, "-m", "engineering_platform.server", "init",
                "--data-root", str(real_ep_root),
            ],
            cwd=ROOT, env=environment, capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        real_ep_cli = self.root / "engineering-platform-maintenance-real"
        real_ep_cli.write_text(
            "#!/bin/sh\n"
            f"exec {sys.executable!s} -m engineering_platform.central_operational_reset \"$@\"\n",
            encoding="utf-8",
        )
        real_ep_cli.chmod(0o700)
        real_config = coordinator_module.CoordinationConfig.create(
            coordinator_id="joint-reset-real-ep-001",
            forge_cli=self.forge_cli, forge_data_root=self.forge_root,
            forge_operation_id="forge-reset-real-ep-001",
            ep_cli=real_ep_cli, ep_data_root=real_ep_root,
            ep_operation_id="ep-reset-real-ep-001", ep_backup_root=self.ep_backup,
        )
        real = coordinator_module.OperationalResetCoordinator(
            coordinator_module.ReceiptStore(self.receipts, real_config.coordinator_id),
            command_timeout_seconds=30,
            runner=lambda command, **kwargs: subprocess.run(command, env=environment, **kwargs),
        )

        result = real.preview(real_config)

        self.assertEqual("BOTH_PREVIEWED", result["state"])
        self.assertEqual(
            server.SERVER_STORE_SCHEMA_VERSION,
            result["products"]["engineering-platform"]["target"]["schema_version"],
        )


if __name__ == "__main__":
    unittest.main()
