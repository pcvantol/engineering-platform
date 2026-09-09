from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from engineering_platform import installation_update_preparation as update_preparation
from engineering_platform.installation_update_plan import prepare
from engineering_platform.installation_update_preparation import (
    InstallationUpdatePreparationError,
    prepare_candidate,
)
from engineering_platform.operational_installation_lock import OperationalInstallationLock
from engineering_platform.operational_installation_record import load, record


class CandidateRunner:
    """Deterministic venv/pip/package-identity boundary for preparation tests."""

    def __init__(self, *, target_version: str) -> None:
        self.target_version = target_version
        self.identity_version = target_version
        self.foreign_identity = False
        self.identity_missing_after_install = False
        self.installed = False
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    @property
    def venv_calls(self) -> list[tuple[tuple[str, ...], dict[str, object]]]:
        return [call for call in self.calls if call[0][3:4] == ("venv",)]

    @property
    def pip_calls(self) -> list[tuple[tuple[str, ...], dict[str, object]]]:
        return [call for call in self.calls if call[0][3:4] == ("pip",)]

    def __call__(self, command, **kwargs):  # type: ignore[no-untyped-def]
        invocation = tuple(command)
        self.calls.append((invocation, dict(kwargs)))
        if invocation[2:4] == ("-m", "venv"):
            candidate = Path(invocation[-1])
            if "--clear" in invocation:
                for child in candidate.iterdir() if candidate.exists() else ():
                    if child.is_dir() and not child.is_symlink():
                        for nested in sorted(child.rglob("*"), reverse=True):
                            if nested.is_file() or nested.is_symlink():
                                nested.unlink()
                            elif nested.is_dir():
                                nested.rmdir()
                        child.rmdir()
                    else:
                        child.unlink()
            interpreter = candidate / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
            interpreter.chmod(0o700)
            return subprocess.CompletedProcess(invocation, 0, "", "")
        if invocation[2:4] == ("-m", "pip"):
            candidate = Path(invocation[0]).parent.parent
            site_packages = candidate / "lib" / "python3.12" / "site-packages"
            (site_packages / "engineering_platform").mkdir(parents=True, exist_ok=True)
            (site_packages / f"engineering_platform-{self.identity_version}.dist-info").mkdir(exist_ok=True)
            self.installed = True
            return subprocess.CompletedProcess(invocation, 0, "", "")
        if invocation[1:3] == ("-I", "-c"):
            if not self.installed or self.identity_missing_after_install:
                return subprocess.CompletedProcess(invocation, 1, "", "not installed")
            candidate = Path(invocation[0]).parent.parent
            site_packages = candidate / "lib" / "python3.12" / "site-packages"
            if self.foreign_identity:
                foreign = candidate.parent / "foreign-runtime"
                package, metadata = foreign / "engineering_platform", foreign / "engineering_platform-foreign.dist-info"
                package.mkdir(parents=True, exist_ok=True)
                metadata.mkdir(exist_ok=True)
            else:
                package = site_packages / "engineering_platform"
                metadata = site_packages / f"engineering_platform-{self.identity_version}.dist-info"
            return subprocess.CompletedProcess(
                invocation,
                0,
                json.dumps({
                    "interpreter": invocation[0],
                    "version": self.identity_version,
                    "metadata": str(metadata),
                    "package": str(package),
                }),
                "",
            )
        raise AssertionError(f"unexpected candidate command: {invocation}")


class InstallationUpdatePreparationTests(unittest.TestCase):
    def _builder(self, directory: Path) -> Path:
        builder = directory / "builder python"
        builder.write_text("#!/bin/sh\n", encoding="utf-8")
        builder.chmod(0o700)
        return builder

    def _plan(self, root: Path, wheel: Path, *, operation_id: str = "update-0001"):
        current = root / "current runtime" / "bin" / "python"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("#!/bin/sh\n", encoding="utf-8")
        current.chmod(0o700)
        record(
            root,
            installation_id="installation-1",
            version="2.3.1",
            channel="stable",
            artifact_digest="sha256:" + "a" * 64,
            source_revision="b" * 40,
            interpreter=current,
            roles={"server": "com.engineeringplatform.server"},
            desired_state="ACTIVE",
            observed_state="ACTIVE",
            verification={"result": "PASS"},
            cleanup={"result": "COMPLETE"},
        )
        digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        return prepare(
            root,
            operation_id=operation_id,
            artifact=wheel,
            target_version="2.3.2",
            target_digest=digest,
            target_source_revision="c" * 40,
        )

    @staticmethod
    def _minimal_wheel(path: Path, version: str) -> None:
        """Produce an isolated valid wheel without consulting a package index."""
        distribution = f"engineering_platform-{version}.dist-info"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("engineering_platform/__init__.py", "__all__ = ()\n")
            archive.writestr(
                f"{distribution}/METADATA",
                f"Metadata-Version: 2.1\nName: engineering-platform\nVersion: {version}\n",
            )
            archive.writestr(
                f"{distribution}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{distribution}/RECORD", "")

    def test_stages_exact_wheel_and_prepares_only_an_operation_owned_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "EP Runtime"
            wheel = base / "qualified wheel.whl"
            wheel.write_bytes(b"exact qualified wheel bytes")
            plan = self._plan(root, wheel)
            before = load(root)
            runner = CandidateRunner(target_version=plan.target_version)

            prepared = prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)

            operation = root.resolve() / "operations" / plan.operation_id
            staged = Path(prepared.staged_artifact)
            self.assertEqual(staged.read_bytes(), wheel.read_bytes())
            self.assertEqual(staged.parent, operation / "download")
            self.assertEqual(Path(prepared.candidate_venv), operation / "candidate-venv")
            self.assertEqual(Path(prepared.pip_cache), operation / "pip-cache")
            self.assertEqual(Path(prepared.interpreter), operation / "candidate-venv" / "bin" / "python")
            self.assertEqual(prepared.package["version"], plan.target_version)
            self.assertEqual(prepared.payload()["artifact_digest"], plan.target_digest)
            self.assertEqual(load(root), before)
            self.assertFalse((operation / "operation.json").exists())
            self.assertEqual(len(runner.venv_calls), 1)
            self.assertEqual(len(runner.pip_calls), 1)
            pip_command, pip_kwargs = runner.pip_calls[0]
            self.assertIn("--no-deps", pip_command)
            self.assertIn("--no-index", pip_command)
            self.assertEqual(pip_command[-1], str(staged))
            self.assertEqual(pip_kwargs["env"]["PIP_CACHE_DIR"], str(operation / "pip-cache"))

    def test_real_candidate_venv_installs_the_staged_wheel_without_an_index(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "engineering_platform-2.3.2-py3-none-any.whl"
            self._minimal_wheel(wheel, "2.3.2")
            plan = self._plan(root, wheel)

            prepared = prepare_candidate(plan, venv_builder=Path(sys.executable))

            self.assertEqual(prepared.package["version"], "2.3.2")
            self.assertTrue(Path(prepared.package["package"]).is_relative_to(Path(prepared.candidate_venv)))
            self.assertTrue(Path(prepared.package["metadata"]).is_relative_to(Path(prepared.candidate_venv)))

    def test_rejects_changed_source_or_staged_wheel_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"first bytes")
            plan = self._plan(root, wheel)
            runner = CandidateRunner(target_version=plan.target_version)
            wheel.write_bytes(b"changed after planning")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "source artifact changed"):
                prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)
            self.assertFalse((root / "operations" / plan.operation_id).exists())
            self.assertEqual(runner.calls, [])

            # The durable staged wheel is also re-hashed on every resume; a
            # corrupt local cache cannot be silently installed or replaced.
            wheel.write_bytes(b"first bytes")
            plan = self._plan(base / "runtime-two", wheel, operation_id="update-0002")
            runner = CandidateRunner(target_version=plan.target_version)
            prepared = prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)
            Path(prepared.staged_artifact).write_bytes(b"tampered staged bytes")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "staged operation artifact differs"):
                prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)

    def test_idempotent_resume_reuses_the_verified_stage_and_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"immutable bytes")
            plan = self._plan(root, wheel)
            runner = CandidateRunner(target_version=plan.target_version)
            first = prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)
            venv_calls, pip_calls = len(runner.venv_calls), len(runner.pip_calls)

            # A caller's old temporary source may disappear or change after a
            # crash.  Resume is bound to EP's own verified staged bytes.
            wheel.write_bytes(b"new bytes that are not this operation")
            second = prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)

            self.assertEqual(second, first)
            self.assertEqual(len(runner.venv_calls), venv_calls)
            self.assertEqual(len(runner.pip_calls), pip_calls)

    def test_normalizes_paths_with_spaces_and_source_and_data_root_symlinks(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "EP Runtime With Spaces"
            root_link = base / "EP Runtime Alias"
            root.mkdir()
            root_link.symlink_to(root, target_is_directory=True)
            source_directory = base / "Qualified Artifacts With Spaces"
            source_directory.mkdir()
            source = source_directory / "ep target wheel.whl"
            source.write_bytes(b"exact source through symlink")
            source_link = base / "current qualified wheel.whl"
            source_link.symlink_to(source)
            plan = self._plan(root_link, source_link)
            prepared = prepare_candidate(
                plan,
                venv_builder=self._builder(base),
                runner=CandidateRunner(target_version=plan.target_version),
            )

            expected_root = root.resolve()
            for value in (prepared.operation_root, prepared.staged_artifact, prepared.candidate_venv, prepared.pip_cache):
                self.assertTrue(Path(value).resolve(strict=False).is_relative_to(expected_root))
            self.assertTrue(Path(prepared.staged_artifact).is_file())
            self.assertEqual(plan.data_root, str(expected_root))

    def test_rejects_wrong_candidate_package_or_version(self) -> None:
        for kind in ("version", "package"):
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "runtime"
                wheel = base / "candidate.whl"
                wheel.write_bytes(b"exact bytes")
                plan = self._plan(root, wheel)
                runner = CandidateRunner(target_version=plan.target_version)
                if kind == "version":
                    runner.identity_version = "2.3.3"
                    expected = "planned EP version"
                else:
                    runner.foreign_identity = True
                    expected = "escapes"
                with self.assertRaisesRegex(InstallationUpdatePreparationError, expected):
                    prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)
                self.assertEqual(len(runner.pip_calls), 1)

    def test_refuses_operation_escape_and_symlinked_operation_root(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            root = base / "runtime"
            plan = self._plan(root, wheel)
            escaped = replace(plan, operation_id="../outside")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "operation ID"):
                prepare_candidate(escaped, venv_builder=self._builder(base), runner=CandidateRunner(target_version=plan.target_version))
            self.assertFalse((base / "outside").exists())

            root = base / "runtime-two"
            plan = self._plan(root, wheel, operation_id="update-0002")
            outside = base / "outside-operation-root"
            outside.mkdir()
            (root / "operations").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "escapes"):
                prepare_candidate(plan, venv_builder=self._builder(base), runner=CandidateRunner(target_version=plan.target_version))
            self.assertEqual(list(outside.iterdir()), [])

    def test_preparation_respects_the_existing_single_operational_update_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)
            lock = OperationalInstallationLock(root)
            lock.acquire("other-update-0001")
            try:
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "another operational update"):
                    prepare_candidate(plan, venv_builder=self._builder(base), runner=CandidateRunner(target_version=plan.target_version))
            finally:
                lock.release("other-update-0001")
            self.assertFalse((root / "operations" / plan.operation_id).exists())

    def test_rejects_malformed_plan_artifact_and_operation_directory_inputs(self) -> None:
        """Private preparation boundaries reject unsafe state before mutation."""
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)

            with self.assertRaisesRegex(InstallationUpdatePreparationError, "update plan is invalid"):
                update_preparation._paths(object())
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "cleanup paths"):
                update_preparation._paths(replace(plan, cleanup_targets=()))

            artifact_alias = base / "artifact-alias.whl"
            artifact_alias.symlink_to(wheel)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "regular file"):
                update_preparation._digest(artifact_alias)
            with patch.object(Path, "open", side_effect=OSError("unreadable")):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "unreadable"):
                    update_preparation._digest(wheel)

            owned_root = root.resolve()
            not_a_directory = owned_root / "not-a-directory"
            not_a_directory.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "owned directory"):
                update_preparation._directory(not_a_directory, root=owned_root, label="test directory")

    def test_rejects_directory_creation_failures_and_post_creation_symlink_race(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            root.mkdir()
            root = root.resolve()
            denied = root / "denied"
            with patch.object(Path, "mkdir", side_effect=OSError("denied")):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "could not be created"):
                    update_preparation._directory(denied, root=root, label="test directory")

            raced = root / "raced"
            outside = base / "outside"
            outside.mkdir()
            original_mkdir = Path.mkdir

            def create_then_redirect(path: Path, *args: object, **kwargs: object) -> None:
                original_mkdir(path, *args, **kwargs)
                if path == raced:
                    path.rmdir()
                    path.symlink_to(outside, target_is_directory=True)

            with patch.object(Path, "mkdir", new=create_then_redirect):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "owned directory"):
                    update_preparation._directory(raced, root=root, label="test directory")
            self.assertTrue(raced.is_symlink())

    def test_rejects_invalid_candidate_markers_and_cleans_a_failed_marker_write(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)
            paths = update_preparation._paths(plan)
            paths.operation.mkdir(parents=True)

            paths.marker.symlink_to(wheel)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "marker is invalid"):
                update_preparation._ensure_marker(plan, paths, paths.staged_artifact)
            paths.marker.unlink()

            paths.marker.write_text("not JSON", encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "marker is unreadable"):
                update_preparation._ensure_marker(plan, paths, paths.staged_artifact)
            paths.marker.unlink()

            paths.marker.write_text(json.dumps({"different": "operation"}), encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "does not match"):
                update_preparation._ensure_marker(plan, paths, paths.staged_artifact)
            paths.marker.unlink()

            paths.candidate.mkdir()
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "exists without operation identity"):
                update_preparation._ensure_marker(plan, paths, paths.staged_artifact)
            paths.candidate.rmdir()

            with patch.object(update_preparation.os, "replace", side_effect=OSError("replace denied")):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "could not be retained"):
                    update_preparation._ensure_marker(plan, paths, paths.staged_artifact)
            self.assertFalse(paths.marker.exists())
            self.assertEqual(list(paths.operation.glob(".candidate-*")), [])

    def test_rejects_invalid_builders_runner_failures_and_incomplete_candidate_venvs(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)
            paths = update_preparation._paths(plan)
            paths.operation.mkdir(parents=True)

            with self.assertRaisesRegex(InstallationUpdatePreparationError, "absolute executable"):
                update_preparation._base_interpreter("relative-python")

            def cannot_start(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                raise OSError("not executable")

            with self.assertRaisesRegex(InstallationUpdatePreparationError, "could not be started"):
                update_preparation._run(cannot_start, ("command",), environment={}, label="test command")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "failed"):
                update_preparation._run(
                    lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "failure"),
                    ("command",),
                    environment={},
                    label="test command",
                )

            paths.candidate.write_text("not a venv", encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "not an owned directory"):
                update_preparation._create_or_recover_venv(
                    paths, builder=self._builder(base), runner=CandidateRunner(target_version=plan.target_version)
                )
            paths.candidate.unlink()
            paths.candidate.mkdir()
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "did not provide"):
                update_preparation._create_or_recover_venv(
                    paths,
                    builder=self._builder(base),
                    runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
                )

    def test_rejects_malformed_or_foreign_candidate_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)
            paths = update_preparation._paths(plan)
            package = paths.candidate / "lib" / "site-packages" / "engineering_platform"
            metadata = paths.candidate / "lib" / "site-packages" / "engineering_platform-2.3.2.dist-info"
            package.mkdir(parents=True)
            metadata.mkdir()
            identity = {
                "interpreter": str(paths.interpreter),
                "version": plan.target_version,
                "metadata": str(metadata),
                "package": str(package),
            }

            with self.assertRaisesRegex(InstallationUpdatePreparationError, "identity is invalid"):
                update_preparation._package_in_candidate(paths, {"version": plan.target_version}, plan)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "identity is invalid"):
                update_preparation._package_in_candidate(
                    paths, {**identity, "interpreter": str(base / "other-python")}, plan
                )
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "identity is invalid"):
                update_preparation._package_in_candidate(paths, {**identity, "metadata": "relative-metadata"}, plan)

    def test_detects_mid_stage_change_and_concurrent_staging_conflicts(self) -> None:
        class WrongDigest:
            def update(self, chunk: bytes) -> None:
                pass

            def hexdigest(self) -> str:
                return "f" * 64

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(base / "runtime", wheel)
            paths = update_preparation._paths(plan)
            with patch.object(
                update_preparation.installation_update_plan,
                "verify_exact_artifact",
                return_value={"path": str(wheel)},
            ), patch.object(update_preparation.hashlib, "sha256", return_value=WrongDigest()):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "changed during"):
                    update_preparation._stage(plan, paths)
            self.assertFalse(paths.staged_artifact.exists())

            plan = self._plan(base / "runtime-two", wheel, operation_id="update-0002")
            paths = update_preparation._paths(plan)

            def concurrent_exact_stage(source: Path, destination: Path) -> None:
                destination.write_bytes(wheel.read_bytes())
                raise FileExistsError

            with patch.object(update_preparation.os, "link", side_effect=concurrent_exact_stage):
                staged = update_preparation._stage(plan, paths)
            self.assertEqual(staged.read_bytes(), wheel.read_bytes())

            plan = self._plan(base / "runtime-three", wheel, operation_id="update-0003")
            paths = update_preparation._paths(plan)

            def concurrent_wrong_stage(source: Path, destination: Path) -> None:
                destination.write_bytes(b"competing wrong bytes")
                raise FileExistsError

            with patch.object(update_preparation.os, "link", side_effect=concurrent_wrong_stage):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "differs"):
                    update_preparation._stage(plan, paths)

            plan = self._plan(base / "runtime-four", wheel, operation_id="update-0004")
            paths = update_preparation._paths(plan)
            with patch.object(update_preparation.os, "link", side_effect=OSError("link failed")):
                with self.assertRaisesRegex(InstallationUpdatePreparationError, "could not be staged"):
                    update_preparation._stage(plan, paths)
            self.assertFalse(paths.staged_artifact.exists())

    def test_rejects_candidate_without_package_identity_after_exact_install(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "runtime"
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"exact bytes")
            plan = self._plan(root, wheel)
            runner = CandidateRunner(target_version=plan.target_version)
            runner.identity_missing_after_install = True

            with self.assertRaisesRegex(InstallationUpdatePreparationError, "does not provide the planned EP package"):
                prepare_candidate(plan, venv_builder=self._builder(base), runner=runner)
            self.assertEqual(len(runner.pip_calls), 1)
            lock = OperationalInstallationLock(root)
            lock.acquire("subsequent-update-0001")
            lock.release("subsequent-update-0001")


if __name__ == "__main__":
    unittest.main()
