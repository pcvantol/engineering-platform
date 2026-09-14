from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from engineering_platform.legacy_installation_adoption import (
    LegacyAdoptionAuthorization, LegacyInstallationAdoptionError, LegacyInstallationObservation,
    _digest, _wheel_record, adopt, inspect, local_owner_authority,
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

    def test_wheel_parser_rejects_ambiguous_invalid_and_unhashed_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertRaisesRegex(LegacyInstallationAdoptionError, "wheel")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "wheel"):
                _digest(root / "not-a-wheel.txt")
            for name, metadata, record, expected in (
                ("ambiguous.whl", None, None, "ambiguous"),
                ("wrong-name.whl", "Name: another-package\nVersion: 2.3.34\n", "engineering_platform/x.py,sha256=a,1\n", "does not identify"),
                ("no-hashes.whl", "Name: engineering-platform\nVersion: 2.3.34\n", "engineering_platform/x.py,,1\n", "no EP package hashes"),
            ):
                path = root / name
                with zipfile.ZipFile(path, "w") as archive:
                    if metadata is not None:
                        archive.writestr("x.dist-info/METADATA", metadata)
                        archive.writestr("x.dist-info/RECORD", record)
                with self.assertRaisesRegex(LegacyInstallationAdoptionError, expected):
                    _wheel_record(path)
            invalid = root / "invalid.whl"; invalid.write_text("not zip")
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "invalid"):
                _wheel_record(invalid)

    def test_inspection_refuses_wrong_service_package_failure_and_version_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; package = root / "engineering_platform"; package.mkdir(parents=True)
            (package / "__init__.py").write_text("pass\n")
            interpreter = root / "python"; interpreter.write_text(""); interpreter.chmod(0o700)
            other = root / "other"; other.write_text(""); other.chmod(0o700)
            wheel = Path(temporary) / "original.whl"; self._wheel(wheel)
            installation = OperationalInstallation(str(interpreter), str(root), "instance-1", "2.3.34", None, ())
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "selected service"):
                inspect(installation=installation, service_label="", service_interpreter=interpreter, preserved_wheel=wheel)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "does not use"):
                inspect(installation=installation, service_label="service", service_interpreter=other, preserved_wheel=wheel)
            def unavailable(*_args, **_kwargs):
                class Result: returncode = 1; stdout = ""
                return Result()
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "does not provide"):
                inspect(installation=installation, service_label="service", service_interpreter=interpreter, preserved_wheel=wheel, runner=unavailable)
            def wrong_version(*_args, **_kwargs):
                class Result:
                    returncode = 0
                    stdout = '{"interpreter":"%s","version":"2.3.33","metadata":"x","package":"%s"}' % (interpreter, package)
                return Result()
            unconfigured = OperationalInstallation(str(interpreter), str(root), "instance-1", None, None, ())
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "version differs"):
                inspect(installation=unconfigured, service_label="service", service_interpreter=interpreter, preserved_wheel=wheel, runner=wrong_version)

    def test_authorization_is_exact_owner_bound_and_record_is_conflict_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"; root.mkdir(mode=0o700)
            observed = LegacyInstallationObservation("instance-1", str(root), "service", str(root / "python"), str(root / "package"),
                "2.3.34", "sha256:" + "a" * 64, str(root / "old.whl"))
            good = LegacyAdoptionAuthorization("instance-1", str(root), "service", str(root / "python"), "sha256:" + "a" * 64,
                "2.3.36", "sha256:" + "b" * 64, "c" * 40, "operation-1", True)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "explicitly acknowledge"):
                local_owner_authority(LegacyAdoptionAuthorization(**{**good.payload(), "acknowledge_unknown_source_revision": False}), observed)
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "does not bind"):
                local_owner_authority(LegacyAdoptionAuthorization(**{**good.payload(), "instance_id": "other"}), observed)
            with patch("engineering_platform.legacy_installation_adoption.os.geteuid", return_value=-1):
                with self.assertRaisesRegex(LegacyInstallationAdoptionError, "exclusively own"):
                    local_owner_authority(good, observed)
            saved = adopt(observation=observed, authorization=good, authorizer=lambda *_args: None)
            self.assertEqual(adopt(observation=observed, authorization=good, authorizer=lambda *_args: None), saved)
            conflicting = LegacyAdoptionAuthorization(**{**good.payload(), "operation_id": "operation-2"})
            with self.assertRaisesRegex(LegacyInstallationAdoptionError, "different identity"):
                adopt(observation=observed, authorization=conflicting, authorizer=lambda *_args: None)
