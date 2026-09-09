from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.operational_installation import OperationalInstallationError, inventory, package_identity, record_status, resolve, validate_health, validate_package_identity, validate_registered_package_identity
from engineering_platform.operational_installation_record import OperationalInstallationRecordError, load, record, replace_for_update


class OperationalInstallationTests(unittest.TestCase):
    def _root(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory) / "EP Runtime With Spaces"
        root.mkdir()
        (root / "server.json").write_text(json.dumps({"version": 2, "product_version": "2.3.1"}))
        (root / "runtime-identity.json").write_text(json.dumps({"instance_id": "instance-1"}))
        interpreter = root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
        return root, interpreter

    def test_path_candidate_cannot_replace_service_selected_runtime_and_symlinks_normalize(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            alias = Path(directory) / "alias-python"
            alias.symlink_to(interpreter)
            candidate = Path(directory) / "old-path-python"
            candidate.write_text("#!/bin/sh\n"); candidate.chmod(0o755)
            installation = resolve(root, interpreter=alias, path_candidates=(candidate,))
            self.assertEqual(installation.interpreter, str(interpreter.resolve()))
            self.assertEqual(installation.path_candidates, (str(candidate.resolve()),))
            self.assertEqual(installation.data_root, str(root.resolve()))

    def test_runtime_and_health_must_belong_to_the_selected_instance(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            installation = resolve(root, interpreter=interpreter)
            validate_health(installation, {"service": "engineering-platform-server", "instance_id": "instance-1", "healthy": True})
            with self.assertRaisesRegex(OperationalInstallationError, "does not identify"):
                validate_health(installation, {"service": "engineering-platform-server", "instance_id": "wrong", "healthy": True})
            (root / "runtime.json").write_text(json.dumps({"pid": 9, "instance_id": "wrong"}))
            with self.assertRaisesRegex(OperationalInstallationError, "different instance"):
                resolve(root, interpreter=interpreter)

    def test_record_status_is_explicit_and_must_bind_the_selected_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            installation = resolve(root, interpreter=interpreter)
            self.assertEqual(record_status(installation)["state"], "UNREGISTERED")
            record(root, installation_id="instance-1", version="2.3.1", channel="stable",
                   artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40,
                   interpreter=interpreter, roles={"server": "com.engineeringplatform.server"},
                   desired_state="ACTIVE", observed_state="ACTIVE",
                   verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
            status = record_status(installation)
            self.assertEqual((status["state"], status["artifact_digest"]), ("REGISTERED", "sha256:" + "a" * 64))
            (root / "server.json").write_text(json.dumps({"version": 2, "product_version": "2.3.2"}))
            with self.assertRaisesRegex(OperationalInstallationError, "does not match"):
                record_status(resolve(root, interpreter=interpreter))

    def test_update_record_replacement_requires_exact_current_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            original = record(root, installation_id="instance-1", version="2.3.1", channel="stable",
                              artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40,
                              interpreter=interpreter, roles={"server": "com.engineeringplatform.server"},
                              desired_state="ACTIVE", observed_state="ACTIVE",
                              verification={"result": "PASS"}, cleanup={"result": "COMPLETE"})
            replacement = {**original, "version": "2.3.2", "artifact_digest": "sha256:" + "c" * 64,
                           "source_revision": "d" * 40, "verification": {"result": "PASS", "operation": "update-0001"}}
            self.assertEqual(replace_for_update(root, expected_version="2.3.1",
                                                 expected_artifact_digest="sha256:" + "a" * 64,
                                                 replacement=replacement)["version"], "2.3.2")
            self.assertEqual(load(root)["artifact_digest"], "sha256:" + "c" * 64)
            self.assertEqual(replace_for_update(root, expected_version="2.3.2",
                                                 expected_artifact_digest="sha256:" + "c" * 64,
                                                 replacement=replacement), replacement)
            with self.assertRaisesRegex(OperationalInstallationRecordError, "changed before"):
                replace_for_update(root, expected_version="2.3.1", expected_artifact_digest="sha256:" + "a" * 64,
                                   replacement=replacement)
            with self.assertRaisesRegex(OperationalInstallationRecordError, "identity cannot change"):
                replace_for_update(root, expected_version="2.3.2", expected_artifact_digest="sha256:" + "c" * 64,
                                   replacement={**replacement, "installation_id": "other"})

    def test_rejects_malformed_facts_and_wrong_health_shapes(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            (root / "server.json").write_text("[]")
            with self.assertRaisesRegex(OperationalInstallationError, "invalid"):
                resolve(root, interpreter=interpreter)
            (root / "server.json").write_text(json.dumps({"version": 2, "product_version": "2.3.1"}))
            (root / "runtime.json").write_text(json.dumps({"pid": "bad"}))
            with self.assertRaisesRegex(OperationalInstallationError, "PID"):
                resolve(root, interpreter=interpreter)
            (root / "runtime.json").unlink()
            installation = resolve(root, interpreter=interpreter)
            with self.assertRaisesRegex(OperationalInstallationError, "does not identify"):
                validate_health(installation, {"service": "other", "instance_id": "instance-1", "healthy": True})
            with self.assertRaisesRegex(OperationalInstallationError, "not healthy"):
                validate_health(installation, {"service": "engineering-platform-server", "instance_id": "instance-1", "healthy": False})
            interpreter.unlink()
            with self.assertRaisesRegex(OperationalInstallationError, "interpreter"):
                resolve(root, interpreter=interpreter)

    def test_schema_revision_is_never_interpreted_as_a_product_version(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            (root / "server.json").write_text(json.dumps({"version": 56}))
            installation = resolve(root, interpreter=interpreter)
            self.assertIsNone(installation.configured_version)
            validate_package_identity(installation, {"interpreter": str(interpreter), "version": "2.3.1", "metadata": "/metadata", "package": "/package"})

    def test_selected_interpreter_package_identity_is_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            _, interpreter = self._root(directory)
            process = __import__("subprocess")
            good = process.CompletedProcess((), 0, json.dumps({"interpreter": str(interpreter), "version": "2.3.1", "metadata": "/metadata", "package": "/package"}), "")
            self.assertEqual(package_identity(interpreter, runner=lambda *_a, **_k: good)["version"], "2.3.1")
            for result, marker in ((process.CompletedProcess((), 1, "", ""), "does not provide"), (process.CompletedProcess((), 0, "[]", ""), "incomplete"), (process.CompletedProcess((), 0, "{}", ""), "incomplete")):
                with self.subTest(marker=marker), self.assertRaisesRegex(OperationalInstallationError, marker):
                    package_identity(interpreter, runner=lambda *_a, **_k: result)

    def test_package_identity_must_match_the_selected_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            installation = resolve(root, interpreter=interpreter)
            identity = {"interpreter": str(interpreter), "version": "2.3.1", "metadata": "/metadata", "package": "/package"}
            validate_package_identity(installation, identity)
            with self.assertRaisesRegex(OperationalInstallationError, "version does not match"):
                validate_package_identity(installation, {**identity, "version": "2.3.0"})
            other = Path(directory) / "other-python"; other.write_text("#!/bin/sh\n"); other.chmod(0o755)
            with self.assertRaisesRegex(OperationalInstallationError, "different interpreter"):
                validate_package_identity(installation, {**identity, "interpreter": str(other)})

    def test_registered_record_must_match_selected_package_without_configured_product_version(self) -> None:
        identity = {"interpreter": "/runtime/python", "version": "2.3.0", "metadata": "/metadata", "package": "/package"}
        with self.assertRaisesRegex(OperationalInstallationError, "registered installation version"):
            validate_registered_package_identity({"state": "REGISTERED", "version": "2.3.1"}, identity)
        validate_registered_package_identity({"state": "REGISTERED", "version": "2.3.0"}, identity)
        validate_registered_package_identity({"state": "UNREGISTERED"}, identity)

    def test_inventory_exposes_old_path_package_and_conflicting_service_without_selecting_it(self) -> None:
        with TemporaryDirectory() as directory:
            root, interpreter = self._root(directory)
            old = Path(directory) / "old EP venv" / "bin" / "python"; old.parent.mkdir(parents=True); old.write_text("#!/bin/sh\n"); old.chmod(0o755)
            installation = resolve(root, interpreter=interpreter)
            process = __import__("subprocess")
            def runner(args, **_kwargs):
                selected = str(Path(args[0]).resolve())
                version = "2.3.1" if selected == str(interpreter.resolve()) else "2.3.0"
                return process.CompletedProcess(args, 0, json.dumps({"interpreter": selected, "version": version, "metadata": "/metadata", "package": "/package"}), "")
            observed = inventory(installation, candidates=(old,), service_references={"com.example.legacy": old}, runner=runner)
            self.assertEqual(observed["coverage"], "EXPLICIT_PATHS_ONLY")
            self.assertFalse(observed["single_operational_installation"])
            self.assertEqual(observed["conflicting_service_references"][0]["identity"]["version"], "2.3.0")
