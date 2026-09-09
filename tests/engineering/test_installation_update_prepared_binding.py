from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_operation import (
    InstallationUpdateOperationError,
    InstallationUpdateSession,
    create,
    recover_prepared_candidate,
    status,
)
from engineering_platform.installation_update_plan import prepare
from engineering_platform.installation_update_preparation import (
    InstallationUpdatePreparationError,
    prepare_candidate,
    staged_execution_plan,
    verify_prepared_candidate,
)
from engineering_platform.operational_installation_record import record


class CandidateRunner:
    """Small deterministic boundary for candidate readback and construction."""

    def __init__(self, target_version: str) -> None:
        self.target_version = target_version
        self.identity_version = target_version
        self.installed = False

    def __call__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
        invocation = tuple(command)
        if invocation[2:4] == ("-m", "venv"):
            interpreter = Path(invocation[-1]) / "bin" / "python"
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
            if not self.installed:
                return subprocess.CompletedProcess(invocation, 1, "", "not installed")
            candidate = Path(invocation[0]).parent.parent
            site_packages = candidate / "lib" / "python3.12" / "site-packages"
            return subprocess.CompletedProcess(
                invocation,
                0,
                json.dumps({
                    "interpreter": invocation[0],
                    "version": self.identity_version,
                    "metadata": str(site_packages / f"engineering_platform-{self.identity_version}.dist-info"),
                    "package": str(site_packages / "engineering_platform"),
                }),
                "",
            )
        raise AssertionError(f"unexpected candidate command: {invocation}")


class PreparedCandidateBindingTests(unittest.TestCase):
    @staticmethod
    def _builder(base: Path) -> Path:
        builder = base / "builder python"
        builder.write_text("#!/bin/sh\n", encoding="utf-8")
        builder.chmod(0o700)
        return builder

    @staticmethod
    def _plan(root: Path, wheel: Path, *, operation_id: str = "update-0001"):
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
        return prepare(
            root,
            operation_id=operation_id,
            artifact=wheel,
            target_version="2.3.2",
            target_digest="sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            target_source_revision="c" * 40,
        )

    def _prepared(self, base: Path, *, operation_id: str = "update-0001", rebind: bool = True):
        root = base / "EP Runtime With Spaces"
        wheel = base / "exact candidate wheel.whl"
        wheel.write_bytes(b"exact immutable artifact")
        source_plan = self._plan(root, wheel, operation_id=operation_id)
        runner = CandidateRunner(source_plan.target_version)
        candidate = prepare_candidate(source_plan, venv_builder=self._builder(base), runner=runner)
        execution_plan = staged_execution_plan(source_plan, candidate=candidate, runner=runner) if rebind else None
        return source_plan, execution_plan, candidate, runner, wheel

    @staticmethod
    def _bind(plan, candidate, runner) -> None:  # type: ignore[no-untyped-def]
        with InstallationUpdateSession(plan) as session:
            session.bind_prepared_candidate(candidate, runner=runner)

    def test_rebinds_to_stage_and_recovers_after_source_disappears_and_reboot(self) -> None:
        with TemporaryDirectory() as temporary:
            source_plan, _initial_execution_plan, candidate, runner, wheel = self._prepared(
                Path(temporary), rebind=False,
            )
            wheel.unlink()

            # Resume can perform the rebinding after a crash between staging
            # and journal creation; it never consults the gone source path.
            execution_plan = staged_execution_plan(source_plan, candidate=candidate, runner=runner)

            self.assertEqual(Path(execution_plan.artifact), Path(candidate.staged_artifact))
            self.assertTrue(Path(execution_plan.artifact).is_file())
            self.assertEqual(verify_prepared_candidate(source_plan, candidate=candidate, runner=runner), candidate)
            self._bind(execution_plan, candidate, runner)

            # A fresh session represents crash/reboot recovery.  It uses the
            # staged execution plan only; the caller's temporary wheel is gone.
            with InstallationUpdateSession(execution_plan) as resumed:
                self.assertEqual(resumed.recover_prepared_candidate(runner=runner), candidate)
            self.assertEqual(recover_prepared_candidate(execution_plan, runner=runner), candidate)
            # The old plan cannot reopen the rebound journal and thereby make
            # its vanished caller-owned artifact authoritative again.
            with self.assertRaisesRegex(InstallationUpdateOperationError, "exact plan"):
                recover_prepared_candidate(source_plan, runner=runner)
            observed = status(Path(execution_plan.data_root), execution_plan.operation_id)
            self.assertEqual(observed["prepared_candidate"], candidate.payload())
            self.assertTrue(str(observed["prepared_candidate_digest"]).startswith("sha256:"))

    def test_rejects_tampered_stage_marker_candidate_and_persisted_binding(self) -> None:
        for kind in ("stage", "marker", "missing-marker", "candidate", "binding", "journal-operation"):
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                _source, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
                self._bind(plan, candidate, runner)
                operation = Path(plan.data_root) / "operations" / plan.operation_id
                if kind == "stage":
                    Path(candidate.staged_artifact).write_bytes(b"changed bytes")
                    with self.assertRaisesRegex(InstallationUpdatePreparationError, "staged operation artifact"):
                        verify_prepared_candidate(plan, candidate=candidate, runner=runner)
                elif kind == "marker":
                    (operation / "candidate-runtime.json").write_text('{"tampered":true}', encoding="utf-8")
                    with self.assertRaisesRegex(InstallationUpdatePreparationError, "does not match"):
                        verify_prepared_candidate(plan, candidate=candidate, runner=runner)
                elif kind == "missing-marker":
                    marker = operation / "candidate-runtime.json"
                    marker.unlink()
                    with self.assertRaisesRegex(InstallationUpdatePreparationError, "marker is invalid"):
                        verify_prepared_candidate(plan, candidate=candidate, runner=runner)
                    self.assertFalse(marker.exists())
                elif kind == "candidate":
                    runner.identity_version = "2.3.3"
                    with self.assertRaisesRegex(InstallationUpdatePreparationError, "planned EP version"):
                        verify_prepared_candidate(plan, candidate=candidate, runner=runner)
                elif kind == "binding":
                    journal = operation / "operation.json"
                    payload = json.loads(journal.read_text(encoding="utf-8"))
                    payload["prepared_candidate"]["interpreter"] = str(Path(temporary) / "foreign-python")
                    canonical = json.dumps(payload["prepared_candidate"], sort_keys=True, separators=(",", ":")).encode("utf-8")
                    payload["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
                    journal.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(InstallationUpdateOperationError, "invalid"):
                        recover_prepared_candidate(plan, runner=runner)
                else:
                    journal = operation / "operation.json"
                    payload = json.loads(journal.read_text(encoding="utf-8"))
                    payload["operation_id"] = "other-update-0002"
                    journal.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(InstallationUpdateOperationError, "exact plan"):
                        recover_prepared_candidate(plan, runner=runner)

    def test_refuses_source_plan_and_mismatched_candidate_binding(self) -> None:
        with TemporaryDirectory() as temporary:
            source_plan, execution_plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            with InstallationUpdateSession(source_plan) as session:
                with self.assertRaisesRegex(InstallationUpdateOperationError, "staged execution plan"):
                    session.bind_prepared_candidate(candidate, runner=runner)
            with self.assertRaisesRegex(InstallationUpdateOperationError, "exact plan"):
                create(execution_plan)

        with TemporaryDirectory() as temporary:
            source_plan, _execution_plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            for label, foreign in (
                ("installation", replace(candidate, installation_id="other-installation")),
                ("operation", replace(candidate, operation_id="other-update-0002")),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(InstallationUpdatePreparationError, "does not match"):
                    staged_execution_plan(source_plan, candidate=foreign, runner=runner)

    def test_binding_requires_prepared_state_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            _source, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            with InstallationUpdateSession(plan) as session:
                first = session.bind_prepared_candidate(candidate, runner=runner)
                self.assertEqual(first["prepared_candidate"], candidate.payload())
                self.assertEqual(session.bind_prepared_candidate(candidate, runner=runner), first)
                session.advance("INVENTORIED", {"inventory": "PASS"})
                with self.assertRaisesRegex(InstallationUpdateOperationError, "prepared operation state"):
                    session.bind_prepared_candidate(candidate, runner=runner)

    def test_upgrades_a_legacy_unbound_journal_when_binding_a_recovered_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            _source, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            create(plan)
            journal = Path(plan.data_root) / "operations" / plan.operation_id / "operation.json"
            legacy = json.loads(journal.read_text(encoding="utf-8"))
            legacy["schema_version"] = 1
            legacy.pop("prepared_candidate")
            legacy.pop("prepared_candidate_digest")
            journal.write_text(json.dumps(legacy), encoding="utf-8")

            with self.assertRaisesRegex(InstallationUpdateOperationError, "no prepared candidate"):
                recover_prepared_candidate(plan, runner=runner)
            with InstallationUpdateSession(plan) as session:
                session.bind_prepared_candidate(candidate, runner=runner)
                self.assertEqual(session.recover_prepared_candidate(runner=runner), candidate)
            upgraded = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["schema_version"], 2)
            self.assertEqual(upgraded["prepared_candidate"], candidate.payload())

    def test_rejects_symlinked_stage_and_candidate_escape(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            _source, plan, candidate, runner, _wheel = self._prepared(base)
            staged = Path(candidate.staged_artifact)
            retained = base / "retained exact wheel.whl"
            staged.rename(retained)
            staged.symlink_to(retained)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "escapes"):
                verify_prepared_candidate(plan, candidate=candidate, runner=runner)

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            _source, plan, candidate, runner, _wheel = self._prepared(base)
            candidate_root = Path(candidate.candidate_venv)
            retained = base / "retained candidate"
            candidate_root.rename(retained)
            outside = base / "outside"
            outside.mkdir()
            candidate_root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "escapes"):
                verify_prepared_candidate(plan, candidate=candidate, runner=runner)

        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            _source, plan, candidate, runner, _wheel = self._prepared(base)
            # ``bin`` is EP-owned; only the final venv launcher may point to
            # the base interpreter.  A redirected launcher directory must
            # not turn recovery into an external-runtime selection.
            launcher_directory = Path(candidate.interpreter).parent
            retained = base / "retained candidate bin"
            launcher_directory.rename(retained)
            outside = base / "outside candidate bin"
            outside.mkdir()
            launcher_directory.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(InstallationUpdatePreparationError, "unavailable"):
                verify_prepared_candidate(plan, candidate=candidate, runner=runner)

    def test_session_binding_and_recovery_require_existing_lock_ownership(self) -> None:
        with TemporaryDirectory() as temporary:
            _source, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            session = InstallationUpdateSession(plan)
            with self.assertRaisesRegex(InstallationUpdateOperationError, "does not own"):
                session.bind_prepared_candidate(candidate, runner=runner)
            with self.assertRaisesRegex(InstallationUpdateOperationError, "does not own"):
                session.recover_prepared_candidate(runner=runner)


if __name__ == "__main__":
    unittest.main()
