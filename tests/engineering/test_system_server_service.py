"""Focused behavioural coverage for the EP system-service evidence foundation."""
from __future__ import annotations

from pathlib import Path
import os
import plistlib
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import system_server_service as service


class SystemServerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "EP Runtime With Spaces"
        self.root.mkdir()
        self.launch_daemons = base / "LaunchDaemons"
        self.launch_daemons.mkdir()
        self.shared_agents = base / "Shared LaunchAgents"
        self.shared_agents.mkdir()
        self.launcher = self._interpreter("Operational Venv/bin/python")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _interpreter(self, relative: str) -> Path:
        launcher = Path(self.temporary.name) / relative
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the venv launcher identity rather than resolving it to the
        # shared base Python.
        launcher.symlink_to(Path(sys.executable))
        (launcher.parent.parent / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        return launcher.parent.resolve() / launcher.name

    @staticmethod
    def _account(name: str) -> SimpleNamespace:
        if name != "ep-server":
            raise KeyError(name)
        return SimpleNamespace(pw_uid=os.getuid())

    def _paths(self) -> service.SystemServicePaths:
        return service.default_paths(self.root, self.launch_daemons)

    def _write_canonical_system_plist(self) -> service.SystemServerService:
        paths = self._paths()
        definition = service.service_definition(self.root, interpreter=self.launcher, service_account="ep-server")
        paths.plist_path.write_bytes(plistlib.dumps(service.plist_payload(paths, definition), fmt=plistlib.FMT_XML))
        return definition

    def test_payload_binds_a_non_root_account_and_exact_absolute_venv_launcher(self) -> None:
        paths = self._paths()
        definition = service.service_definition(self.root, interpreter=self.launcher, service_account="ep-server")

        payload = service.plist_payload(paths, definition)

        self.assertEqual(payload["Label"], service.LABEL)
        self.assertEqual(payload["UserName"], "ep-server")
        self.assertEqual(payload["ProgramArguments"], [
            str(self.launcher), "-m", "engineering_platform.server", "serve", "--data-root", str(self.root.resolve()),
        ])
        self.assertNotEqual(payload["ProgramArguments"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(payload["EnvironmentVariables"]["EP_SERVER_DATA_ROOT"], str(self.root.resolve()))
        self.assertEqual(payload["StandardOutPath"], "/dev/null")
        with self.assertRaisesRegex(service.SystemServerServiceError, "must not run as root"):
            service.plist_payload(
                paths,
                service.SystemServerService(paths.data_root, Path("/bin/sh"), "root"),
            )
        with self.assertRaisesRegex(service.SystemServerServiceError, "venv bin/python"):
            service.plist_payload(
                paths,
                service.SystemServerService(paths.data_root, Path("/bin/sh"), "ep-server"),
            )

    def test_definition_rejects_root_relative_and_non_executable_identity(self) -> None:
        with self.assertRaisesRegex(service.SystemServerServiceError, "must not run as root"):
            service.service_definition(self.root, interpreter=self.launcher, service_account="root")
        with self.assertRaisesRegex(service.SystemServerServiceError, "data root must be absolute"):
            service.service_definition("relative-root", interpreter=self.launcher, service_account="ep-server")
        with self.assertRaisesRegex(service.SystemServerServiceError, "interpreter must be absolute"):
            service.service_definition(self.root, interpreter="relative-python", service_account="ep-server")
        with self.assertRaisesRegex(service.SystemServerServiceError, "not an executable"):
            service.service_definition(self.root, interpreter=Path(self.temporary.name) / "missing", service_account="ep-server")
        with self.assertRaisesRegex(service.SystemServerServiceError, "venv bin/python"):
            service.service_definition(self.root, interpreter=Path("/bin/sh"), service_account="ep-server")
        with self.assertRaisesRegex(service.SystemServerServiceError, "LaunchDaemon directory must be absolute"):
            service.default_paths(self.root, "relative-daemons")
        with self.assertRaisesRegex(service.SystemServerServiceError, "LaunchDaemon directory must be absolute"):
            service.default_paths(self.root, "")
        with self.assertRaisesRegex(service.SystemServerServiceError, "shared LaunchAgent directory must be absolute"):
            service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir="",
                account_lookup=self._account,
            )
        self.assertEqual(
            service.default_paths(self.root, Path("/tmp")).launch_daemons_dir,
            Path("/tmp").resolve(),
        )

    def test_configured_service_reads_only_one_strict_system_plist(self) -> None:
        definition = self._write_canonical_system_plist()

        observed = service.configured_service(
            self.root, launch_daemons_dir=self.launch_daemons, account_lookup=self._account,
        )

        self.assertEqual(observed, definition)
        self.assertEqual(
            service.configured_interpreter(
                self.root, launch_daemons_dir=self.launch_daemons, account_lookup=self._account,
            ), self.launcher,
        )
        empty_launch_daemons = Path(self.temporary.name) / "Empty LaunchDaemons"
        empty_launch_daemons.mkdir()
        self.assertIsNone(
            service.configured_service(
                Path(self.temporary.name) / "other-root",
                launch_daemons_dir=empty_launch_daemons, account_lookup=self._account,
            ),
        )

    def test_configured_service_rejects_tampered_or_symlinked_plist(self) -> None:
        self._write_canonical_system_plist()
        paths = self._paths()
        with paths.plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
        payload["EnvironmentVariables"]["PATH"] = "/attacker/bin"
        paths.plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
        with self.assertRaisesRegex(service.SystemServerServiceError, "unexpected environment"):
            service.configured_service(
                self.root, launch_daemons_dir=self.launch_daemons, account_lookup=self._account,
            )
        paths.plist_path.unlink()
        paths.plist_path.symlink_to(Path(self.temporary.name) / "foreign.plist")
        with self.assertRaisesRegex(service.SystemServerServiceError, "symlink"):
            service.configured_service(
                self.root, launch_daemons_dir=self.launch_daemons, account_lookup=self._account,
            )

    def test_configured_service_rejects_missing_service_account_and_different_root(self) -> None:
        self._write_canonical_system_plist()
        with self.assertRaisesRegex(service.SystemServerServiceError, "account is unavailable"):
            service.configured_service(self.root, launch_daemons_dir=self.launch_daemons)
        with self._paths().plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
        payload["ProgramArguments"][-1] = str(Path(self.temporary.name) / "different-root")
        self._paths().plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
        with self.assertRaisesRegex(service.SystemServerServiceError, "expected runtime"):
            service.configured_service(
                self.root, launch_daemons_dir=self.launch_daemons, account_lookup=self._account,
            )

    def test_inventory_reports_shared_and_user_legacy_services_as_conflicts_without_machine_claim(self) -> None:
        self._write_canonical_system_plist()
        home = Path(self.temporary.name) / "Other User"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        old_runtime = self._interpreter("Old PlatformIO/bin/python")
        legacy = {
            "Label": service.LABEL,
            "ProgramArguments": [
                str(old_runtime), "-m", "engineering_platform.server", "serve", "--data-root", str(self.root),
            ],
        }
        (agents / f"{service.LABEL}.plist").write_bytes(plistlib.dumps(legacy, fmt=plistlib.FMT_XML))
        shared = {**legacy, "Label": "com.engineeringplatform.server.legacy"}
        (self.shared_agents / "com.engineeringplatform.server.legacy.plist").write_bytes(
            plistlib.dumps(shared, fmt=plistlib.FMT_XML),
        )

        inventory = service.machine_scope_inventory(
            self.root, launch_daemons_dir=self.launch_daemons,
            shared_launch_agents_dir=self.shared_agents, user_homes=(home,),
            account_lookup=self._account, effective_uid=0,
        )

        self.assertEqual(inventory["coverage"], "SYSTEM_SHARED_AND_DECLARED_USER_SERVICE_SURFACES")
        self.assertEqual(inventory["scope"]["status"], "INCOMPLETE")
        self.assertIn("ACCOUNT_HOME_DISCOVERY_NOT_AUTHORITY", inventory["scope"]["limitations"])
        self.assertFalse(inventory["single_operational_installation"])
        self.assertEqual(inventory["single_operational_installation_status"], "CONFLICTS_DETECTED")
        statuses = {(entry["domain"], entry["status"]) for entry in inventory["entries"]}
        self.assertIn(("SYSTEM", "SELECTED_SYSTEM_SERVICE"), statuses)
        self.assertIn(("SHARED_USER", "CONFLICTING_USER_SERVICE"), statuses)
        self.assertIn(("USER", "CONFLICTING_USER_SERVICE"), statuses)

    def test_inventory_makes_symlinked_or_malformed_surfaces_explicit(self) -> None:
        home = Path(self.temporary.name) / "A User"
        library = home / "Library"
        library.mkdir(parents=True)
        (library / "LaunchAgents").symlink_to(Path(self.temporary.name) / "foreign")
        malformed = self.shared_agents / "com.engineeringplatform.server.plist"
        malformed.write_text("not a plist", encoding="utf-8")

        inventory = service.machine_scope_inventory(
            self.root, launch_daemons_dir=self.launch_daemons,
            shared_launch_agents_dir=self.shared_agents, user_homes=(home,),
            account_lookup=self._account, effective_uid=501,
        )

        self.assertIn("UNPRIVILEGED_CALLER", inventory["scope"]["limitations"])
        self.assertIn("INACCESSIBLE_SERVICE_SURFACES", inventory["scope"]["limitations"])
        self.assertEqual(inventory["inaccessible_locations"][0]["domain"], "USER")
        self.assertIn(
            {"plist": str(malformed.resolve()), "domain": "SHARED_USER", "status": "INVALID_OWNED_SERVICE_REFERENCE"},
            inventory["entries"],
        )

    def test_inventory_rejects_malformed_xml_and_nonregular_owned_records_without_hanging(self) -> None:
        malformed_xml = self.shared_agents / "com.engineeringplatform.server-malformed.plist"
        malformed_xml.write_text("<?xml version='1.0'?><plist><dict>", encoding="utf-8")
        fifo = self.shared_agents / "com.engineeringplatform.server-fifo.plist"
        os.mkfifo(fifo)

        inventory = service.machine_scope_inventory(
            self.root,
            launch_daemons_dir=self.launch_daemons,
            shared_launch_agents_dir=self.shared_agents,
            account_lookup=self._account,
            effective_uid=0,
        )

        invalid = {(entry["plist"], entry["status"]) for entry in inventory["entries"]}
        self.assertIn((str(malformed_xml.resolve()), "INVALID_OWNED_SERVICE_REFERENCE"), invalid)
        self.assertIn((str(fifo.resolve()), "INVALID_OWNED_SERVICE_REFERENCE"), invalid)

    def test_inventory_makes_directory_enumeration_failure_explicit(self) -> None:
        with patch("engineering_platform.system_server_service.os.scandir", side_effect=PermissionError):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        self.assertIn("INACCESSIBLE_SERVICE_SURFACES", inventory["scope"]["limitations"])
        self.assertEqual(
            {entry["domain"] for entry in inventory["inaccessible_locations"]},
            {"SYSTEM", "SHARED_USER"},
        )

    def test_inventory_records_a_raced_owned_plist_explicitly(self) -> None:
        self._write_canonical_system_plist()
        with patch(
            "engineering_platform.system_server_service._read_plist_snapshot_from_descriptor",
            side_effect=FileNotFoundError,
        ):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        self.assertIn(
            {
                "plist": str(self._paths().plist_path.resolve()),
                "domain": "SYSTEM",
                "status": "INACCESSIBLE_OR_RACED_OWNED_SERVICE_REFERENCE",
            },
            inventory["entries"],
        )
        self.assertFalse(inventory["single_operational_installation"])

    def test_inventory_marks_a_replaced_directory_surface_as_raced(self) -> None:
        old_runtime = self._interpreter("Old Operational Venv/bin/python")
        replacement_runtime = self._interpreter("Replacement Operational Venv/bin/python")
        candidate = self.shared_agents / "com.engineeringplatform.server.legacy.plist"
        legacy = {
            "Label": "com.engineeringplatform.server.legacy",
            "ProgramArguments": [
                str(old_runtime), "-m", "engineering_platform.server", "serve", "--data-root", str(self.root),
            ],
        }
        candidate.write_bytes(plistlib.dumps(legacy, fmt=plistlib.FMT_XML))
        moved_directory = Path(self.temporary.name) / "Enumerated Shared LaunchAgents"
        original_reader = service._read_plist_snapshot_from_descriptor
        replaced = False

        def replace_path_after_enumeration(descriptor: int, filename: str):
            nonlocal replaced
            if filename == candidate.name and not replaced:
                replaced = True
                self.shared_agents.rename(moved_directory)
                self.shared_agents.mkdir()
                replacement = {**legacy, "ProgramArguments": [
                    str(replacement_runtime), "-m", "engineering_platform.server", "serve", "--data-root", str(self.root),
                ]}
                (self.shared_agents / filename).write_bytes(plistlib.dumps(replacement, fmt=plistlib.FMT_XML))
            return original_reader(descriptor, filename)

        with patch(
            "engineering_platform.system_server_service._read_plist_snapshot_from_descriptor",
            side_effect=replace_path_after_enumeration,
        ):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        self.assertIn(
            {
                "path": str(self.shared_agents.resolve()),
                "domain": "SHARED_USER",
                "status": "INACCESSIBLE_OR_RACED_SERVICE_SURFACE",
            },
            inventory["entries"],
        )
        self.assertFalse(any(entry.get("interpreter") == str(old_runtime) for entry in inventory["entries"]))
        shared_surface = next(entry for entry in inventory["inspected_locations"] if entry["domain"] == "SHARED_USER")
        self.assertEqual(shared_surface["state"], "RACED")

    def test_inventory_never_selects_a_system_plist_that_changed_after_surface_scan(self) -> None:
        self._write_canonical_system_plist()
        paths = self._paths()
        original_scan = service._scan_service_directory
        changed = False

        def scan_then_tamper(directory: Path, *, domain: str):
            nonlocal changed
            result = original_scan(directory, domain=domain)
            if domain == "SYSTEM" and not changed:
                changed = True
                with paths.plist_path.open("rb") as stream:
                    payload = plistlib.load(stream)
                payload["EnvironmentVariables"]["PATH"] = "/attacker/bin"
                paths.plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
            return result

        with patch(
            "engineering_platform.system_server_service._scan_service_directory",
            side_effect=scan_then_tamper,
        ):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        system_entry = next(entry for entry in inventory["entries"] if entry["domain"] == "SYSTEM")
        self.assertEqual(system_entry["status"], "CONFLICTING_SYSTEM_SERVICE")
        self.assertIsNone(inventory["selected_interpreter"])

    def test_inventory_requires_the_same_service_account_for_selected_identity(self) -> None:
        definition = self._write_canonical_system_plist()
        changed_account = service.SystemServerService(
            definition.data_root,
            definition.interpreter,
            "different-nonroot-account",
        )

        with patch(
            "engineering_platform.system_server_service.configured_service",
            return_value=changed_account,
        ):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        system_entry = next(entry for entry in inventory["entries"] if entry["domain"] == "SYSTEM")
        self.assertEqual(system_entry["status"], "CONFLICTING_SYSTEM_SERVICE")

    def test_inventory_reports_directory_open_failure_without_a_cli_crash(self) -> None:
        with patch(
            "engineering_platform.system_server_service._open_directory_fd",
            side_effect=PermissionError,
        ):
            inventory = service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

        self.assertIn("INACCESSIBLE_SERVICE_SURFACES", inventory["scope"]["limitations"])
        self.assertIsNone(inventory["selected_interpreter"])

    def test_inventory_keeps_absent_declared_home_distinct_from_inaccessible_and_rejects_relative_home(self) -> None:
        absent_home = Path(self.temporary.name) / "No Agent Home"
        inventory = service.machine_scope_inventory(
            self.root, launch_daemons_dir=self.launch_daemons,
            shared_launch_agents_dir=self.shared_agents, user_homes=(absent_home,),
            account_lookup=self._account, effective_uid=0,
        )
        self.assertEqual(inventory["inaccessible_locations"], [])
        self.assertIn(
            {"path": str(absent_home.resolve() / "Library" / "LaunchAgents"), "domain": "USER", "state": "ABSENT"},
            inventory["inspected_locations"],
        )
        with self.assertRaisesRegex(service.SystemServerServiceError, "declared user home must be absolute"):
            service.machine_scope_inventory(
                self.root, launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents, user_homes=("relative-home",),
                account_lookup=self._account, effective_uid=0,
            )
        with self.assertRaisesRegex(service.SystemServerServiceError, "declared user home is unavailable"):
            service.machine_scope_inventory(
                self.root,
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                user_homes=("~this-user-does-not-exist-ep-inventory",),
                account_lookup=self._account,
                effective_uid=0,
            )
        with self.assertRaisesRegex(service.SystemServerServiceError, "data root is unavailable"):
            service.machine_scope_inventory(
                "/tmp/\0ep-inventory",
                launch_daemons_dir=self.launch_daemons,
                shared_launch_agents_dir=self.shared_agents,
                account_lookup=self._account,
                effective_uid=0,
            )

    def test_foundation_exports_no_direct_daemon_mutation_api(self) -> None:
        for name in ("install", "uninstall", "replace_runtime", "write_plist"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(service, name))
