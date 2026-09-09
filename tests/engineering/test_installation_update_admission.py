from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.installation_update_admission import (
    InstallationUpdateAdmissionError,
    admit,
)
from engineering_platform.installation_update_operation import InstallationUpdateSession, status
from engineering_platform.installation_update_plan import prepare
from engineering_platform.installation_update_preparation import prepare_candidate, staged_execution_plan
from engineering_platform.operational_installation_lock import OperationalInstallationLock
from engineering_platform.operational_installation_record import FILENAME, record


class CandidateRunner:
    """Deterministic venv/package boundary; no real installer is exercised."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.installed = False

    def __call__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(command)
        if command[2:4] == ("-m", "venv"):
            interpreter = Path(command[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!\n", encoding="utf-8")
            interpreter.chmod(0o700)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2:4] == ("-m", "pip"):
            candidate = Path(command[0]).parent.parent
            site = candidate / "lib" / "python3.12" / "site-packages"
            (site / "engineering_platform").mkdir(parents=True, exist_ok=True)
            (site / f"engineering_platform-{self.version}.dist-info").mkdir(exist_ok=True)
            self.installed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ("-I", "-c"):
            if not self.installed:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            candidate = Path(command[0]).parent.parent
            site = candidate / "lib" / "python3.12" / "site-packages"
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "interpreter": command[0], "version": self.version,
                "metadata": str(site / f"engineering_platform-{self.version}.dist-info"),
                "package": str(site / "engineering_platform"),
            }), "")
        raise AssertionError(command)


class InstallationUpdateAdmissionTests(unittest.TestCase):
    def _prepared(self, base: Path):
        root = base / "EP data root with spaces"
        current = root / "current" / "bin" / "python"
        current.parent.mkdir(parents=True); current.write_text("#!\n", encoding="utf-8"); current.chmod(0o700)
        record(root, installation_id="installation-1", version="2.3.1", channel="stable",
               artifact_digest="sha256:" + "a" * 64, source_revision="b" * 40,
               interpreter=current, roles={"server": "com.engineeringplatform.server"},
               desired_state="ACTIVE", observed_state="ACTIVE", verification={"result": "PASS"},
               cleanup={"result": "COMPLETE"})
        wheel = base / "caller owned source wheel.whl"; wheel.write_bytes(b"exact bytes")
        source = prepare(root, operation_id="update-0001", artifact=wheel, target_version="2.3.2",
                         target_digest="sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
                         target_source_revision="c" * 40)
        builder = base / "builder python"; builder.write_text("#!\n", encoding="utf-8"); builder.chmod(0o700)
        runner = CandidateRunner(source.target_version)
        candidate = prepare_candidate(source, venv_builder=builder, runner=runner)
        plan = staged_execution_plan(source, candidate=candidate, runner=runner)
        with InstallationUpdateSession(plan) as session:
            session.bind_prepared_candidate(candidate, runner=runner)
        return root, plan, candidate, runner, wheel

    def test_admits_only_bound_staged_candidate_and_recovers_without_source_wheel(self) -> None:
        with TemporaryDirectory() as temporary:
            root, plan, candidate, runner, wheel = self._prepared(Path(temporary))
            wheel.unlink()
            first = admit(plan, runner=runner)
            second = admit(plan, runner=runner)  # reboot/retry: only stage/marker/candidate are read
            self.assertEqual(first, second)
            self.assertEqual(first.prepared_candidate, candidate.payload())
            journal = status(root, plan.operation_id)
            self.assertEqual(journal["state"], "PREPARED")
            self.assertEqual(journal["execution_admission"], first.payload())
            self.assertEqual(Path(plan.artifact), Path(candidate.staged_artifact))

    def test_rejects_concurrent_session_before_candidate_or_service_selection(self) -> None:
        with TemporaryDirectory() as temporary:
            root, plan, _candidate, runner, _wheel = self._prepared(Path(temporary))
            lock = OperationalInstallationLock(root); lock.acquire("contender-0002")
            try:
                with self.assertRaisesRegex(InstallationUpdateAdmissionError, "operation is invalid"):
                    admit(plan, runner=runner)
            finally:
                lock.release("contender-0002")

    def test_rejects_changed_registered_provenance_and_candidate_becoming_selected(self) -> None:
        for change in ("source", "roles", "selected-candidate"):
            with self.subTest(change=change), TemporaryDirectory() as temporary:
                root, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
                first = admit(plan, runner=runner)
                payload = json.loads((root / FILENAME).read_text())
                if change == "source":
                    payload["source_revision"] = "d" * 40
                elif change == "roles":
                    payload["roles"] = {"server": "different-service"}
                else:
                    payload["interpreter"] = candidate.interpreter
                (root / FILENAME).write_text(json.dumps(payload), encoding="utf-8")
                expected = "already selected" if change == "selected-candidate" else "changed after prepared candidate binding"
                with self.assertRaisesRegex(InstallationUpdateAdmissionError, expected):
                    admit(plan, runner=runner)
                self.assertEqual(status(root, plan.operation_id)["execution_admission"], first.payload())

    def test_refuses_same_version_and_digest_record_change_before_first_admission(self) -> None:
        with TemporaryDirectory() as temporary:
            root, plan, _candidate, runner, _wheel = self._prepared(Path(temporary))
            # The plan has only version/digest, so this deliberately preserves
            # those values while changing service/source provenance after OI-4b
            # binding and before the first OI-4c admission.
            payload = json.loads((root / FILENAME).read_text())
            payload["source_revision"] = "d" * 40
            payload["roles"] = {"server": "replacement-service"}
            (root / FILENAME).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdateAdmissionError, "changed after prepared candidate binding"):
                admit(plan, runner=runner)
            observed = status(root, plan.operation_id)
            self.assertIsNone(observed["execution_admission"])
            self.assertIsNotNone(observed["prepared_record_provenance"])

    def test_legacy_candidate_binding_requires_explicit_provenance_rebinding(self) -> None:
        with TemporaryDirectory() as temporary:
            root, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            journal = root / "operations" / plan.operation_id / "operation.json"
            legacy = json.loads(journal.read_text())
            legacy["schema_version"] = 2
            legacy.pop("prepared_record_provenance")
            legacy.pop("prepared_record_provenance_digest")
            journal.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(InstallationUpdateAdmissionError, "lacks immutable record provenance"):
                admit(plan, runner=runner)
            with InstallationUpdateSession(plan) as session:
                session.bind_prepared_candidate(candidate, runner=runner)
            self.assertEqual(admit(plan, runner=runner).prepared_candidate, candidate.payload())

    def test_rejects_marker_stage_launcher_binding_and_unsafe_path_tampering(self) -> None:
        for change in ("marker", "stage", "launcher", "binding", "candidate-symlink"):
            with self.subTest(change=change), TemporaryDirectory() as temporary:
                root, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
                operation = root / "operations" / plan.operation_id
                if change == "marker":
                    (operation / "candidate-runtime.json").write_text("{}", encoding="utf-8")
                elif change == "stage":
                    Path(candidate.staged_artifact).write_bytes(b"changed")
                elif change == "launcher":
                    runner.version = "2.3.3"
                elif change == "binding":
                    journal = operation / "operation.json"; payload = json.loads(journal.read_text())
                    payload["prepared_candidate"]["interpreter"] = str(Path(temporary) / "foreign" / "python")
                    encoded = json.dumps(payload["prepared_candidate"], sort_keys=True, separators=(",", ":")).encode()
                    payload["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
                    journal.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    candidate_root = Path(candidate.candidate_venv); retained = Path(temporary) / "retained"
                    candidate_root.rename(retained); candidate_root.symlink_to(retained, target_is_directory=True)
                with self.assertRaises(InstallationUpdateAdmissionError):
                    admit(plan, runner=runner)

    def test_rejects_unbound_source_plan_and_candidate_plan_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            root, plan, candidate, runner, _wheel = self._prepared(Path(temporary))
            # A plan that names a caller wheel cannot reopen this bound journal.
            foreign = plan.__class__(**{**plan.payload(), "artifact": str(Path(temporary) / "foreign.whl")})
            with self.assertRaisesRegex(InstallationUpdateAdmissionError, "operation is invalid"):
                admit(foreign, runner=runner)
            journal = root / "operations" / plan.operation_id / "operation.json"
            payload = json.loads(journal.read_text())
            payload["prepared_candidate"]["operation_id"] = "other-update-0002"
            encoded = json.dumps(payload["prepared_candidate"], sort_keys=True, separators=(",", ":")).encode()
            payload["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
            journal.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(InstallationUpdateAdmissionError):
                admit(plan, runner=runner)
