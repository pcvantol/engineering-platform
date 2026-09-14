from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from engineering_platform.legacy_installation_adoption import (
    LegacyAdoptionAuthorization, LegacyInstallationAdoptionError, adopt, inspect,
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
