from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from engineering_platform import legacy_installation_adoption as adoption
from engineering_platform.legacy_installation_adoption import (
    LegacyAdoptionAuthorization, LegacyInstallationAdoptionError, LegacyInstallationObservation, adopt, inspect,
)
from engineering_platform.operational_installation import OperationalInstallation


class LegacyInstallationAdoptionTests(unittest.TestCase):
    def _wheel(self, path: Path, *, content: bytes = b"pass\n") -> str:
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("engineering_platform/__init__.py", content)
            archive.writestr("engineering_platform-2.3.34.dist-info/METADATA", "Name: engineering-platform\nVersion: 2.3.34\n")
            archive.writestr("engineering_platform-2.3.34.dist-info/RECORD", f"engineering_platform/__init__.py,sha256={encoded},{len(content)}\n")
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def test_artifact_identified_legacy_has_unknown_source_and_requires_bound_owner_adoption(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; package = root / "site" / "engineering_platform"; package.mkdir(parents=True)
            code = b"pass\n"; (package / "__init__.py").write_bytes(code)
            interpreter = root / "venv" / "bin" / "python"; interpreter.parent.mkdir(parents=True); interpreter.write_text(""); interpreter.chmod(0o700)
            wheel = Path(temporary) / "original.whl"; digest = self._wheel(wheel, content=code)
            installation = OperationalInstallation(str(interpreter), str(root), "instance-1", "2.3.34", None, ())
            def runner(*_args, **_kwargs):
                class Result:
                    returncode = 0
                    stdout = ('{"interpreter":"%s","version":"2.3.34","metadata":"%s",'
                              '"package":"%s"}' % (interpreter, root / "site" / "engineering_platform-2.3.34.dist-info", package))
                return Result()
            observed = inspect(installation=installation, service_label="com.engineeringplatform.server",
                               service_interpreter=interpreter, preserved_wheel=wheel, runner=runner)
            self.assertIsNone(observed.source_revision); self.assertEqual(observed.artifact_digest, digest)
            authorization = LegacyAdoptionAuthorization("instance-1", str(root), "com.engineeringplatform.server", str(interpreter), digest,
                "2.3.35", "sha256:" + "b" * 64, "c" * 40, "legacy-update-0001", True)
            saved = adopt(observation=observed, authorization=authorization)
            self.assertEqual(saved["kind"], "OBSERVED_LEGACY_ARTIFACT")
            self.assertFalse((root / "operational-installation.json").exists())

    def test_modified_installed_code_and_unbound_authorization_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; package = root / "site" / "engineering_platform"; package.mkdir(parents=True)
            (package / "__init__.py").write_bytes(b"modified\n")
            interpreter = root / "python"; interpreter.write_text(""); interpreter.chmod(0o700)
            wheel = Path(temporary) / "original.whl"; self._wheel(wheel)
            installation = OperationalInstallation(str(interpreter), str(root), "instance-1", "2.3.34", None, ())
            def runner(*_args, **_kwargs):
                class Result:
                    returncode = 0; stdout = '{"interpreter":"%s","version":"2.3.34","metadata":"x","package":"%s"}' % (interpreter, package)
                return Result()
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "differs"):
                inspect(installation=installation, service_label="service", service_interpreter=interpreter, preserved_wheel=wheel, runner=runner)

    def _observation(self, root: Path) -> LegacyInstallationObservation:
        root = root.resolve()
        return LegacyInstallationObservation(
            "instance-1", str(root), "com.engineeringplatform.server", str(root / "python"),
            str(root / "site" / "engineering_platform"), "2.3.34", "sha256:" + "a" * 64,
            str(root / "original.whl"),
        )

    def _authorization(self, observation: LegacyInstallationObservation, **overrides: object) -> LegacyAdoptionAuthorization:
        values: dict[str, object] = {
            "instance_id": observation.instance_id, "data_root": observation.data_root,
            "service_label": observation.service_label, "interpreter": observation.interpreter,
            "old_artifact_digest": observation.artifact_digest, "target_version": "2.3.35",
            "target_artifact_digest": "sha256:" + "b" * 64, "target_source_revision": "c" * 40,
            "operation_id": "legacy-update-0001", "acknowledge_unknown_source_revision": True,
        }
        values.update(overrides)
        return LegacyAdoptionAuthorization(**values)  # type: ignore[arg-type]

    def test_wheel_validation_rejects_non_wheels_and_ambiguous_or_unhashed_archives(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "wheel"):
                adoption._digest(root / "missing.whl")
            bad = root / "bad.whl"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("one.dist-info/METADATA", "Name: engineering-platform\nVersion: 2.3.34\n")
                archive.writestr("two.dist-info/METADATA", "Name: engineering-platform\nVersion: 2.3.34\n")
                archive.writestr("one.dist-info/RECORD", "engineering_platform/__init__.py,,0\n")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "ambiguous"):
                adoption._wheel_record(bad)
            unhashed = root / "unhashed.whl"
            with zipfile.ZipFile(unhashed, "w") as archive:
                archive.writestr("engineering-platform-2.3.34.dist-info/METADATA", "Name: engineering-platform\nVersion: 2.3.34\n")
                archive.writestr("engineering-platform-2.3.34.dist-info/RECORD", "engineering_platform/__init__.py,,0\n")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "no EP package hashes"):
                adoption._wheel_record(unhashed)

    def test_package_matching_rejects_extra_and_changed_files(self) -> None:
        with TemporaryDirectory() as temporary:
            package = Path(temporary) / "engineering_platform"; package.mkdir()
            (package / "__init__.py").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "differs"):
                adoption._matches_wheel_package(package, {"engineering_platform/__init__.py": "expected"})
            (package / "extra.py").write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "differs"):
                adoption._matches_wheel_package(package, {})

    def test_inspection_rejects_invalid_service_and_mismatched_artifact_version(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; root.mkdir()
            observation = self._observation(root)
            installation = OperationalInstallation(observation.interpreter, str(root), "instance-1", "2.3.34", None, ())
            wheel = Path(temporary) / "original.whl"; self._wheel(wheel)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "service is invalid"):
                inspect(installation=installation, service_label="", service_interpreter=observation.interpreter, preserved_wheel=wheel)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "observed interpreter"):
                inspect(installation=installation, service_label="service", service_interpreter=root / "other", preserved_wheel=wheel)
            identity = {"version": "2.3.35", "package": observation.package}
            with patch.object(adoption.operational_installation, "package_identity", return_value=identity), patch.object(adoption.operational_installation, "validate_package_identity"):
                with self.assertRaisesRegex(LegacyInstallationAdoptionError, "version differs"):
                    inspect(installation=installation, service_label="service", service_interpreter=observation.interpreter, preserved_wheel=wheel)

    def test_authorization_and_local_owner_checks_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); observation = self._observation(root)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "explicitly acknowledge"):
                adoption._validate_authorization(self._authorization(observation, acknowledge_unknown_source_revision=False), observation)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "identity is invalid"):
                adoption._validate_authorization(self._authorization(observation, target_version="not-a-version"), observation)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "does not bind"):
                adoption._validate_authorization(self._authorization(observation, instance_id="other"), observation)
            os.chmod(root, 0o777)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "exclusively own"):
                adoption.local_owner_authority(self._authorization(observation), observation)

    def test_adoption_is_idempotent_and_rejects_conflicting_or_unreadable_records(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); observation = self._observation(root); authorization = self._authorization(observation)
            authorizer = lambda *_args: None
            saved = adopt(observation=observation, authorization=authorization, authorizer=authorizer)
            self.assertEqual(adopt(observation=observation, authorization=authorization, authorizer=authorizer), saved)
            path = root / adoption.FILENAME
            path.write_text(json.dumps({"different": True}), encoding="utf-8")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "different identity"):
                adopt(observation=observation, authorization=authorization, authorizer=authorizer)
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "unreadable"):
                adopt(observation=observation, authorization=authorization, authorizer=authorizer)
