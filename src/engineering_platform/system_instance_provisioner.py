"""EP-owned system-domain multi-instance provisioner boundary.

Forge Platform may select an exact artifact and instance and call this module;
it never supplies a service label, data root, interpreter, provider home,
migration command or cleanup path.  Updates are prepared, admitted, executed
and resumed by the existing ``installation_update_*`` durable state machine.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from typing import Callable, Mapping, Protocol
from urllib.error import URLError
from urllib.request import urlopen

from . import (
    installation_update_activation,
    installation_update_admission,
    installation_update_composition,
    installation_update_operation,
    installation_update_plan,
    installation_update_preparation,
    operational_installation,
    operational_installation_record,
    system_installation_topology,
    system_provider_context,
    system_server_service,
)


CONTRACT = "engineering-platform.system-provisioner/v1"
_OPERATION = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class SystemInstanceProvisionerError(ValueError):
    """The exact instance request cannot proceed without ambiguity."""


@dataclass(frozen=True)
class ReleaseRequest:
    version: str
    artifact: Path
    artifact_digest: str
    source_revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, str)
            or _VERSION.fullmatch(self.version) is None
            or not isinstance(self.artifact_digest, str)
            or _DIGEST.fullmatch(self.artifact_digest) is None
            or not isinstance(self.source_revision, str)
            or _REVISION.fullmatch(self.source_revision) is None
            or not isinstance(self.artifact, Path)
            or not self.artifact.is_absolute()
        ):
            raise SystemInstanceProvisionerError("EP release request is invalid")
        if _artifact_digest(self.artifact) != self.artifact_digest:
            raise SystemInstanceProvisionerError("EP release artifact digest mismatch")

    def identity(self) -> system_installation_topology.ReleaseIdentity:
        return system_installation_topology.ReleaseIdentity(
            self.version, self.artifact_digest, self.source_revision,
        )


@dataclass(frozen=True)
class InstanceRequest:
    operation_id: str
    instance_id: str
    display_label: str
    service_account: str
    bind_port: int
    release: ReleaseRequest

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or _OPERATION.fullmatch(self.operation_id) is None:
            raise SystemInstanceProvisionerError("EP provisioner operation ID is invalid")
        system_installation_topology.validate_instance_id(self.instance_id)
        system_installation_topology.validate_instance_label(self.display_label)
        if (
            not isinstance(self.service_account, str)
            or self.service_account == "root"
            or not 1 <= self.bind_port <= 65535
        ):
            raise SystemInstanceProvisionerError("EP instance service request is invalid")


class SystemServiceController(Protocol):
    def register(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        interpreter: Path,
    ) -> Mapping[str, object]: ...

    def quiesce(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]: ...
    def start(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]: ...
    def loaded(self, instance: system_installation_topology.SystemInstanceTopology) -> bool: ...
    def remove(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]: ...


class MacOSSystemServiceController:
    """Fixed-command root-authorized LaunchDaemon adapter."""

    def __init__(self, launch_daemons_dir: Path = system_server_service.SYSTEM_LAUNCH_DAEMONS_DIRECTORY) -> None:
        self.launch_daemons_dir = launch_daemons_dir.resolve(strict=False)

    def _paths(self, instance: system_installation_topology.SystemInstanceTopology) -> system_server_service.InstanceSystemServicePaths:
        return system_server_service.instance_default_paths(instance, self.launch_daemons_dir)

    @staticmethod
    def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("/bin/launchctl", *arguments), text=True, capture_output=True, check=False,
        )

    def register(self, instance: system_installation_topology.SystemInstanceTopology, interpreter: Path) -> Mapping[str, object]:
        if os.geteuid() != 0:
            raise SystemInstanceProvisionerError("system LaunchDaemon registration requires root")
        try:
            account = pwd.getpwnam(instance.service_account)
        except KeyError as error:
            raise SystemInstanceProvisionerError("EP instance service account is unavailable") from error
        if account.pw_uid <= 0:
            raise SystemInstanceProvisionerError("EP instance service account is invalid")
        paths = self._paths(instance)
        payload = system_server_service.instance_plist_payload(
            paths,
            system_server_service.instance_service_definition(instance, interpreter=interpreter),
        )
        paths.launch_daemons_dir.mkdir(parents=True, exist_ok=True)
        temporary = paths.plist_path.with_name(f".{paths.plist_path.name}.tmp-{os.getpid()}")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                plistlib.dump(payload, stream, fmt=plistlib.FMT_XML, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, paths.plist_path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"result": "REGISTERED", "label": instance.service_label, "plist": str(paths.plist_path)}

    def quiesce(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]:
        paths = self._paths(instance)
        result = self._launchctl("bootout", "system", str(paths.plist_path))
        if result.returncode and self.loaded(instance):
            raise SystemInstanceProvisionerError("EP instance LaunchDaemon could not be quiesced")
        return {"result": "QUIESCED", "label": instance.service_label}

    def start(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]:
        paths = self._paths(instance)
        if not self.loaded(instance):
            result = self._launchctl("bootstrap", "system", str(paths.plist_path))
            if result.returncode:
                raise SystemInstanceProvisionerError("EP instance LaunchDaemon could not be started")
        return {"result": "RUNNING", "label": instance.service_label}

    def loaded(self, instance: system_installation_topology.SystemInstanceTopology) -> bool:
        return self._launchctl("print", f"system/{instance.service_label}").returncode == 0

    def remove(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]:
        self.quiesce(instance)
        path = self._paths(instance).plist_path
        path.unlink(missing_ok=True)
        return {"result": "REMOVED", "label": instance.service_label, "plist": str(path)}


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self.descriptor)
            self.descriptor = None
            raise SystemInstanceProvisionerError("another operation owns the exact EP instance lock") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


HealthVerifier = Callable[[system_installation_topology.SystemInstanceTopology, Path], Mapping[str, object]]


class SystemInstanceProvisioner:
    def __init__(
        self,
        product_root: Path,
        *,
        controller: SystemServiceController | None = None,
        launch_daemons_dir: Path | None = None,
        health_verifier: HealthVerifier | None = None,
        account_lookup: Callable[[str], Any] = pwd.getpwnam,
    ) -> None:
        self.product = system_installation_topology.system_installation_topology(product_root)
        self.controller = controller or MacOSSystemServiceController(
            launch_daemons_dir or system_server_service.SYSTEM_LAUNCH_DAEMONS_DIRECTORY,
        )
        self.launch_daemons_dir = (
            launch_daemons_dir or system_server_service.SYSTEM_LAUNCH_DAEMONS_DIRECTORY
        ).resolve(strict=False)
        self.health_verifier = health_verifier or self._live_health
        self.account_lookup = account_lookup

    def _instance(self, *, instance_id: str, display_label: str, service_account: str) -> system_installation_topology.SystemInstanceTopology:
        return system_installation_topology.system_instance_topology(
            self.product,
            instance_id=instance_id,
            display_label=display_label,
            service_account=service_account,
        )

    def _instance_from_descriptor(self, instance_id: str) -> tuple[system_installation_topology.SystemInstanceTopology, dict[str, object]]:
        identity = system_installation_topology.validate_instance_id(instance_id)
        path = self.product.system_root / "instances" / identity / "instance.json"
        try:
            descriptor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SystemInstanceProvisionerError("EP instance descriptor is unavailable") from error
        if not isinstance(descriptor, dict) or descriptor.get("instance_id") != identity:
            raise SystemInstanceProvisionerError("EP instance descriptor is invalid")
        instance = self._instance(
            instance_id=identity,
            display_label=str(descriptor.get("display_label", "")),
            service_account=str(descriptor.get("service_account", "")),
        )
        if descriptor.get("service_label") != instance.service_label or descriptor.get("data_root") != str(instance.data_root):
            raise SystemInstanceProvisionerError("EP instance descriptor is tampered")
        return instance, descriptor

    def inventory(self) -> Mapping[str, object]:
        return system_server_service.instance_machine_inventory(
            self.product, launch_daemons_dir=self.launch_daemons_dir,
            account_lookup=self.account_lookup,
        )

    def _collision_gate(self, request: InstanceRequest, *, allow_exact_existing: bool = False) -> None:
        inventory = self.inventory()
        if inventory["state"] == "AMBIGUOUS":
            raise SystemInstanceProvisionerError("EP instance inventory is ambiguous")
        endpoint = f"http://127.0.0.1:{request.bind_port}"
        for entry in inventory["instances"]:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("instance_id") == request.instance_id:
                if (
                    entry.get("status") == "PROVIDER_BOOTSTRAP_PENDING"
                    and entry.get("display_label") == request.display_label
                    and entry.get("service_account") == request.service_account
                ):
                    continue
                selected = entry.get("selected_runtime")
                if (
                    allow_exact_existing
                    and entry.get("display_label") == request.display_label
                    and entry.get("service_account") == request.service_account
                    and entry.get("endpoint") == endpoint
                    and isinstance(selected, Mapping)
                    and all(
                        selected.get(key) == value
                        for key, value in request.release.identity().payload().items()
                    )
                ):
                    continue
                raise SystemInstanceProvisionerError("EP instance ID already exists")
            if entry.get("endpoint") == endpoint:
                raise SystemInstanceProvisionerError("EP instance endpoint collision")

    def provider_register(
        self,
        *,
        instance_id: str,
        display_label: str,
        service_account: str,
        provider: str,
        executable_sha256: str,
        version: str,
        auth_reference: str,
        auth_bootstrap_receipt: str,
    ) -> Mapping[str, object]:
        instance = self._instance(
            instance_id=instance_id, display_label=display_label,
            service_account=service_account,
        )
        bootstrap_path = instance.root / "provider-bootstrap.json"
        bootstrap = {
            "schema_version": 1,
            "instance_id": instance.instance_id,
            "display_label": instance.display_label,
            "service_account": instance.service_account,
            "service_label": instance.service_label,
            "state": "PROVIDER_BOOTSTRAP_PENDING",
        }
        if bootstrap_path.exists():
            if _read_json(bootstrap_path) != bootstrap:
                raise SystemInstanceProvisionerError("EP provider bootstrap identity conflict")
        else:
            if not _publish_json(bootstrap_path, bootstrap) and _read_json(bootstrap_path) != bootstrap:
                raise SystemInstanceProvisionerError("EP provider bootstrap identity conflict")
        return system_provider_context.record_context(
            system_provider_context.provider_context(instance, provider),
            executable_sha256=executable_sha256,
            version=version,
            auth_reference=auth_reference,
            auth_bootstrap_receipt=auth_bootstrap_receipt,
        )

    def create(self, request: InstanceRequest) -> Mapping[str, object]:
        instance = self._instance(
            instance_id=request.instance_id,
            display_label=request.display_label,
            service_account=request.service_account,
        )
        prior = self._terminal_receipt(instance.instance_id, request.operation_id, "INSTALL")
        if prior is not None:
            if prior.get("evidence", {}).get("release") != request.release.identity().payload():
                raise SystemInstanceProvisionerError("EP install receipt release identity conflict")
            _instance, descriptor = self._instance_from_descriptor(request.instance_id)
            return {"contract": CONTRACT, "result": "COMPLETE", "instance": descriptor, "receipt": prior}
        operation_request = {
            "contract": CONTRACT,
            "operation": "INSTALL",
            "operation_id": request.operation_id,
            "instance_id": request.instance_id,
            "display_label": request.display_label,
            "service_account": request.service_account,
            "bind_port": request.bind_port,
            "release": request.release.identity().payload(),
        }
        request_path = instance.operations_root / request.operation_id / "request.json"
        if request_path.exists():
            if _read_json(request_path) != operation_request:
                raise SystemInstanceProvisionerError("EP install operation identity conflict")
        else:
            if not _publish_json(request_path, operation_request) and _read_json(request_path) != operation_request:
                raise SystemInstanceProvisionerError("EP install operation identity conflict")
        self._collision_gate(request, allow_exact_existing=True)
        with _Lock(instance.lifecycle_lock):
            providers = self._provider_readback(instance)
            slot = self._install_slot(request.release)
            self._initialize_data(instance, slot.interpreter, request.bind_port)
            try:
                record = operational_installation_record.load(instance.data_root)
            except operational_installation_record.OperationalInstallationRecordNotFound:
                record = operational_installation_record.record(
                    instance.data_root,
                    installation_id=instance.instance_id,
                    version=request.release.version,
                    channel="stable",
                    artifact_digest=request.release.artifact_digest,
                    source_revision=request.release.source_revision,
                    interpreter=slot.interpreter,
                    roles={"server": instance.service_label},
                    desired_state="ACTIVE",
                    observed_state="ACTIVE",
                    verification={"result": "PENDING"},
                    cleanup={"result": "COMPLETE"},
                )
            expected_record_identity = {
                "installation_id": instance.instance_id,
                "version": request.release.version,
                "artifact_digest": request.release.artifact_digest,
                "source_revision": request.release.source_revision,
                "interpreter": str(slot.interpreter),
                "roles": {"server": instance.service_label},
            }
            if any(record.get(key) != value for key, value in expected_record_identity.items()):
                raise SystemInstanceProvisionerError("EP install operation record identity conflict")
            if record.get("verification") not in ({"result": "PENDING"}, {"result": "PASS"}):
                raise SystemInstanceProvisionerError("EP install operation verification state is invalid")
            self.controller.register(instance, slot.interpreter)
            descriptor = self._descriptor(
                instance, request.bind_port, request.release.identity(), slot.interpreter,
                providers=providers, desired_state="ACTIVE",
            )
            _atomic_json(instance.descriptor, descriptor)
            (instance.root / "provider-bootstrap.json").unlink(missing_ok=True)
            self.controller.start(instance)
            health = self.health_verifier(instance, slot.interpreter)
            if health.get("result") != "PASS":
                raise SystemInstanceProvisionerError("EP instance identity-aware health failed")
            record = operational_installation_record.replace_update_lifecycle(
                instance.data_root,
                expected_version=request.release.version,
                expected_artifact_digest=request.release.artifact_digest,
                expected_source_revision=request.release.source_revision,
                replacement={
                    **record,
                    "observed_state": "ACTIVE",
                    "verification": {"result": "PASS"},
                    "cleanup": {"result": "COMPLETE"},
                },
            )
            receipt = self._receipt(instance, request.operation_id, "INSTALL", "COMPLETE", {
                "release": request.release.identity().payload(),
                "health": dict(health),
                "providers": providers,
            })
            return {"contract": CONTRACT, "result": "COMPLETE", "instance": descriptor, "receipt": receipt}

    def status(self, instance_id: str) -> Mapping[str, object]:
        instance, descriptor = self._instance_from_descriptor(instance_id)
        record = operational_installation_record.load(instance.data_root)
        configured = system_server_service.configured_instance_service(
            instance, launch_daemons_dir=self.launch_daemons_dir,
            account_lookup=self.account_lookup,
        )
        if configured is None:
            return {"contract": CONTRACT, "instance_id": instance_id, "state": "SERVICE_ABSENT", "ready": False}
        if (
            record["installation_id"] != instance.instance_id
            or record["interpreter"] != str(configured.interpreter)
            or descriptor.get("selected_runtime") != {
                "version": record["version"],
                "artifact_digest": record["artifact_digest"],
                "source_revision": record["source_revision"],
                "interpreter": record["interpreter"],
            }
        ):
            raise SystemInstanceProvisionerError("EP instance runtime identity mismatch")
        providers = self._provider_readback(instance)
        health = self.health_verifier(instance, configured.interpreter)
        ready = health.get("result") == "PASS" and self.controller.loaded(instance)
        return {
            "contract": CONTRACT,
            "instance_id": instance.instance_id,
            "state": "READY" if ready else "NOT_READY",
            "ready": ready,
            "descriptor": descriptor,
            "installation": record,
            "providers": providers,
            "health": dict(health),
            "cold_boot_capable": ready,
        }

    def assess_update(self, instance_id: str, release: ReleaseRequest) -> Mapping[str, object]:
        instance, _descriptor = self._instance_from_descriptor(instance_id)
        record = operational_installation_record.load(instance.data_root)
        current = tuple(int(part) for part in str(record["version"]).split("."))
        target = tuple(int(part) for part in release.version.split("."))
        if target < current:
            state = "BLOCKED_DOWNGRADE"
        elif target == current:
            state = "NO_CHANGE" if (
                record["artifact_digest"] == release.artifact_digest
                and record["source_revision"] == release.source_revision
            ) else "BLOCKED_IDENTITY_MISMATCH"
        else:
            state = "UPDATE_AVAILABLE"
        return {
            "contract": CONTRACT,
            "instance_id": instance.instance_id,
            "state": state,
            "current": {
                key: record[key] for key in ("version", "artifact_digest", "source_revision", "interpreter")
            },
            "target": release.identity().payload(),
        }

    def update(
        self,
        *,
        instance_id: str,
        operation_id: str,
        release: ReleaseRequest | None,
        resume: bool = False,
    ) -> Mapping[str, object]:
        instance, descriptor = self._instance_from_descriptor(instance_id)
        with _Lock(instance.lifecycle_lock):
            prior = self._terminal_receipt(instance.instance_id, operation_id, "UPDATE")
            if prior is not None:
                return {
                    "contract": CONTRACT,
                    "result": prior["state"],
                    "instance_id": instance_id,
                    "receipt": prior,
                }
            if resume:
                plan = installation_update_operation.reopen_plan(instance.data_root, operation_id)
            else:
                if release is None:
                    raise SystemInstanceProvisionerError("EP update release identity is required")
                assessment = self.assess_update(instance_id, release)
                if assessment["state"] != "UPDATE_AVAILABLE":
                    raise SystemInstanceProvisionerError("EP instance update is not eligible")
                slot = system_installation_topology.runtime_slot(self.product, release.identity())
                plan = installation_update_plan.prepare(
                    instance.data_root,
                    operation_id=operation_id,
                    artifact=release.artifact,
                    target_version=release.version,
                    target_digest=release.artifact_digest,
                    target_source_revision=release.source_revision,
                    target_runtime_venv=slot.venv,
                )
                with _Lock(instance.artifact_lock):
                    candidate = installation_update_preparation.prepare_candidate(
                        plan, venv_builder=Path(sys.executable).absolute(),
                    )
                plan = installation_update_preparation.staged_execution_plan(plan, candidate=candidate)
                with installation_update_operation.InstallationUpdateSession(plan) as session:
                    session.bind_prepared_candidate(candidate, runner=subprocess.run)
                installation_update_admission.admit(plan)
            admission_payload = installation_update_operation.execution_admission(plan)
            if admission_payload is None:
                raise SystemInstanceProvisionerError("EP instance update lacks execution admission")
            admission = installation_update_admission.ExecutionAdmission(**admission_payload)
            outcome = installation_update_composition.execute(
                plan,
                admission=admission,
                actions=self._update_actions(instance),
            )
            record = operational_installation_record.load(instance.data_root)
            updated_descriptor = {
                **descriptor,
                "selected_runtime": {
                    key: record[key] for key in ("version", "artifact_digest", "source_revision", "interpreter")
                },
            }
            _atomic_json(instance.descriptor, updated_descriptor)
            receipt = self._receipt(instance, operation_id, "UPDATE", str(outcome["state"]), {
                "installation_update": outcome,
                "selected_runtime": updated_descriptor["selected_runtime"],
            })
            return {"contract": CONTRACT, "result": outcome["state"], "instance_id": instance_id, "receipt": receipt}

    def update_status(self, instance_id: str, operation_id: str) -> Mapping[str, object]:
        instance, _descriptor = self._instance_from_descriptor(instance_id)
        return {
            "contract": CONTRACT,
            "instance_id": instance_id,
            "installation_update": installation_update_operation.status(instance.data_root, operation_id),
        }

    def repair(self, instance_id: str, operation_id: str) -> Mapping[str, object]:
        instance, descriptor = self._instance_from_descriptor(instance_id)
        prior = self._terminal_receipt(instance.instance_id, operation_id, "REPAIR")
        if prior is not None:
            return {"contract": CONTRACT, "result": "COMPLETE", "instance_id": instance_id, "receipt": prior}
        with _Lock(instance.lifecycle_lock):
            record = operational_installation_record.load(instance.data_root)
            providers = self._provider_readback(instance)
            interpreter = Path(str(record["interpreter"]))
            identity = operational_installation.package_identity(interpreter)
            if identity["version"] != record["version"]:
                raise SystemInstanceProvisionerError("EP instance repair runtime identity mismatch")
            self.controller.register(instance, interpreter)
            self.controller.start(instance)
            health = self.health_verifier(instance, interpreter)
            receipt = self._receipt(instance, operation_id, "REPAIR", "COMPLETE", {
                "descriptor_digest": _json_digest(descriptor),
                "providers": providers,
                "health": dict(health),
            })
            return {"contract": CONTRACT, "result": "COMPLETE", "instance_id": instance_id, "receipt": receipt}

    def remove(self, instance_id: str, operation_id: str, *, confirm_instance_id: str) -> Mapping[str, object]:
        if confirm_instance_id != instance_id:
            raise SystemInstanceProvisionerError("EP instance removal confirmation mismatch")
        prior = self._terminal_receipt(instance_id, operation_id, "REMOVE")
        if prior is not None:
            return {"contract": CONTRACT, "result": "COMPLETE", "instance_id": instance_id, "receipt": prior}
        instance, descriptor = self._instance_from_descriptor(instance_id)
        with _Lock(instance.lifecycle_lock):
            service = self.controller.remove(instance)
            receipt = self._receipt(instance, operation_id, "REMOVE", "COMPLETE", {
                "removed_descriptor_digest": _json_digest(descriptor),
                "service": dict(service),
                "shared_runtime_slots_preserved": True,
            })
            expected = self.product.system_root / "instances" / instance.instance_id
            if instance.root != expected or not instance.root.is_relative_to(self.product.system_root / "instances"):
                raise SystemInstanceProvisionerError("EP instance removal target is invalid")
            shutil.rmtree(instance.root)
            return {"contract": CONTRACT, "result": "COMPLETE", "instance_id": instance_id, "receipt": receipt}

    def _provider_readback(self, instance: system_installation_topology.SystemInstanceTopology) -> dict[str, object]:
        values = {
            context.provider: system_provider_context.readback(context)
            for context in system_provider_context.provider_contexts(instance)
        }
        if values["codex"]["home"] == values["github"]["home"]:
            raise SystemInstanceProvisionerError("EP provider context collision")
        return values

    def _install_slot(self, release: ReleaseRequest) -> system_installation_topology.RuntimeSlot:
        slot = system_installation_topology.runtime_slot(self.product, release.identity())
        receipt_path = slot.root / "runtime-slot.json"
        expected = {
            "schema_version": 1,
            **release.identity().payload(),
            "venv": str(slot.venv),
        }
        with _Lock(self.product.system_root / "locks" / "artifacts.lock"):
            if receipt_path.exists():
                if _read_json(receipt_path) != expected:
                    raise SystemInstanceProvisionerError("EP shared runtime slot identity mismatch")
            else:
                if slot.root.exists():
                    raise SystemInstanceProvisionerError("EP shared runtime slot is incomplete")
                slot.root.mkdir(parents=True, mode=0o755)
                result = subprocess.run(
                    (sys.executable, "-I", "-m", "venv", str(slot.venv)),
                    text=True, capture_output=True, check=False,
                    env={"PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"},
                )
                if result.returncode:
                    raise SystemInstanceProvisionerError("EP runtime slot creation failed")
                result = subprocess.run(
                    (
                        str(slot.interpreter), "-I", "-m", "pip", "install", "--isolated",
                        "--no-deps", "--no-index", "--no-input", "--disable-pip-version-check",
                        str(release.artifact),
                    ),
                    text=True, capture_output=True, check=False,
                    env={"PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1", "PIP_CONFIG_FILE": os.devnull},
                )
                if result.returncode:
                    raise SystemInstanceProvisionerError("EP runtime slot installation failed")
                identity = operational_installation.package_identity(slot.interpreter)
                if identity["version"] != release.version:
                    raise SystemInstanceProvisionerError("EP runtime slot package identity mismatch")
                _atomic_json(receipt_path, expected)
            identity = operational_installation.package_identity(slot.interpreter)
            if identity["version"] != release.version:
                raise SystemInstanceProvisionerError("EP runtime slot package was substituted")
        return slot

    def _initialize_data(self, instance: system_installation_topology.SystemInstanceTopology, interpreter: Path, port: int) -> None:
        instance.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity_path = instance.data_root / "runtime-identity.json"
        if identity_path.exists():
            identity = _read_json(identity_path)
            if identity.get("instance_id") != instance.instance_id:
                raise SystemInstanceProvisionerError("EP instance data root belongs to another identity")
        else:
            _atomic_json(identity_path, {
                "instance_id": instance.instance_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            **system_provider_context.server_environment(instance),
        }
        result = subprocess.run(
            (
                str(interpreter), "-I", "-m", "engineering_platform.server", "init",
                "--data-root", str(instance.data_root), "--bind-host", "127.0.0.1",
                "--bind-port", str(port),
            ),
            cwd=instance.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise SystemInstanceProvisionerError("EP instance CENTRAL initialization failed")
        identity = _read_json(instance.data_root / "runtime-identity.json")
        if identity.get("instance_id") != instance.instance_id:
            raise SystemInstanceProvisionerError("EP instance initializer returned a different identity")

    def _descriptor(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        port: int,
        release: system_installation_topology.ReleaseIdentity,
        interpreter: Path,
        *,
        providers: Mapping[str, object],
        desired_state: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "instance_id": instance.instance_id,
            "display_label": instance.display_label,
            "service_label": instance.service_label,
            "service_account": instance.service_account,
            "data_root": str(instance.data_root),
            "endpoint": f"http://127.0.0.1:{port}",
            "selected_runtime": {**release.payload(), "interpreter": str(interpreter)},
            "provider_contexts": {
                name: {
                    "home": value["home"],
                    "executable": value["executable"],
                    "executable_sha256": value["executable_sha256"],
                }
                for name, value in providers.items()
            },
            "lifecycle_lock": str(instance.lifecycle_lock),
            "desired_state": desired_state,
        }

    def _configured_interpreter(self, instance: system_installation_topology.SystemInstanceTopology, _data_root: Path) -> Path | None:
        configured = system_server_service.configured_instance_service(
            instance, launch_daemons_dir=self.launch_daemons_dir,
            account_lookup=self.account_lookup,
        )
        return None if configured is None else configured.interpreter

    def _update_actions(self, instance: system_installation_topology.SystemInstanceTopology) -> installation_update_composition.InstallationUpdateOperationalActions:
        def selected(_root: Path) -> Path | None:
            return self._configured_interpreter(instance, _root)

        def inventory(plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
            current = selected(Path(plan.data_root))
            record = operational_installation_record.load(instance.data_root)
            if current is None or current != Path(str(record["interpreter"])):
                raise SystemInstanceProvisionerError("EP instance update source service mismatch")
            return {"result": "PASS", "instance_id": instance.instance_id, "service_label": instance.service_label}

        def quiesce(_plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
            return self.controller.quiesce(instance)

        def quiesce_preflight(plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
            current = selected(Path(plan.data_root))
            if current is None or not self.controller.loaded(instance):
                raise SystemInstanceProvisionerError("EP instance service is unavailable before quiescence")
            return {"result": "PASS", "instance_id": instance.instance_id, "interpreter": str(current)}

        def activate(
            plan: installation_update_plan.InstallationUpdatePlan,
            target: Path,
            source_record: Mapping[str, object] | None,
        ) -> Mapping[str, object]:
            if source_record is None:
                raise SystemInstanceProvisionerError("system update lacks exact source record")
            replacement = installation_update_activation.replacement_record(
                plan, current=source_record, interpreter=target,
            )
            self.controller.register(instance, target)
            self.controller.start(instance)
            return replacement

        def verify(_plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
            configured = selected(instance.data_root)
            if configured is None:
                raise SystemInstanceProvisionerError("updated EP instance service is unavailable")
            result = dict(self.health_verifier(instance, configured))
            if result.get("result") != "PASS":
                raise SystemInstanceProvisionerError("updated EP instance identity-aware health failed")
            return {"result": "PASS", "instance_id": instance.instance_id, "health": result}

        return installation_update_composition.InstallationUpdateOperationalActions(
            inventory=inventory,
            quiesce=quiesce,
            verify=verify,
            quiesce_preflight=quiesce_preflight,
            activate=activate,
            service_interpreter=selected,
        )

    def _live_health(self, instance: system_installation_topology.SystemInstanceTopology, interpreter: Path) -> Mapping[str, object]:
        descriptor = _read_json(instance.descriptor)
        endpoint = descriptor.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("http://127.0.0.1:"):
            raise SystemInstanceProvisionerError("EP instance endpoint is invalid")
        response: Mapping[str, object] | None = None
        for _ in range(40):
            try:
                with urlopen(endpoint + "/health", timeout=1) as stream:  # nosec B310 - loopback-only validated above
                    candidate = json.loads(stream.read())
                if isinstance(candidate, dict):
                    response = candidate
                    break
            except (OSError, URLError, ValueError, json.JSONDecodeError):
                time.sleep(0.05)
        record = operational_installation_record.load(instance.data_root)
        if (
            response is None
            or response.get("service") != "engineering-platform-server"
            or response.get("instance_id") != instance.instance_id
            or response.get("product_version") != record["version"]
            or response.get("healthy") is not True
            or Path(str(record["interpreter"])) != interpreter
        ):
            raise SystemInstanceProvisionerError("EP instance identity-aware health is unavailable")
        return {"result": "PASS", "instance_id": instance.instance_id, "product_version": record["version"]}

    def _receipt(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        operation_id: str,
        operation: str,
        state: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        if _OPERATION.fullmatch(operation_id) is None:
            raise SystemInstanceProvisionerError("EP provisioner operation ID is invalid")
        receipt = {
            "contract": CONTRACT,
            "operation_id": operation_id,
            "operation": operation,
            "state": state,
            "instance_id": instance.instance_id,
            "product_root": str(self.product.system_root),
            "evidence": dict(evidence),
        }
        receipt["receipt_sha256"] = _json_digest(receipt)
        path = self.product.system_root / "receipts" / instance.instance_id / f"{operation_id}.json"
        if path.exists():
            existing = _read_json(path)
            if existing != receipt:
                raise SystemInstanceProvisionerError("EP provisioner receipt identity conflict")
        else:
            if not _publish_json(path, receipt):
                existing = _read_json(path)
                if existing != receipt:
                    raise SystemInstanceProvisionerError("EP provisioner receipt identity conflict")
        return receipt

    def _terminal_receipt(
        self,
        instance_id: str,
        operation_id: str,
        operation: str,
    ) -> dict[str, object] | None:
        identity = system_installation_topology.validate_instance_id(instance_id)
        if _OPERATION.fullmatch(operation_id) is None:
            raise SystemInstanceProvisionerError("EP provisioner operation ID is invalid")
        path = self.product.system_root / "receipts" / identity / f"{operation_id}.json"
        if not path.exists():
            return None
        receipt = _read_json(path)
        digest = receipt.pop("receipt_sha256", None)
        expected_digest = _json_digest(receipt)
        receipt["receipt_sha256"] = digest
        if (
            digest != expected_digest
            or set(receipt) != {
                "contract", "operation_id", "operation", "state", "instance_id",
                "product_root", "evidence", "receipt_sha256",
            }
            or receipt.get("contract") != CONTRACT
            or receipt.get("operation_id") != operation_id
            or receipt.get("operation") != operation
            or receipt.get("instance_id") != identity
            or receipt.get("product_root") != str(self.product.system_root)
            or not isinstance(receipt.get("evidence"), Mapping)
            or receipt.get("state") not in {"COMPLETE", "FAILED"}
        ):
            raise SystemInstanceProvisionerError("EP provisioner receipt is invalid")
        return receipt


def _artifact_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        raise SystemInstanceProvisionerError("EP release artifact must be an exact wheel")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _json_digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemInstanceProvisionerError(f"EP provisioner record is unavailable: {path.name}") from error
    if not isinstance(value, dict):
        raise SystemInstanceProvisionerError(f"EP provisioner record is invalid: {path.name}")
    return value


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_json(path: Path, value: Mapping[str, object]) -> bool:
    """Atomically publish immutable JSON without replacing a competing writer."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.publish-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def _release(args: argparse.Namespace) -> ReleaseRequest:
    if not all((args.version, args.artifact, args.artifact_digest, args.source_revision)):
        raise SystemInstanceProvisionerError("release identity arguments are required")
    return ReleaseRequest(
        args.version,
        args.artifact.expanduser().resolve(),
        args.artifact_digest,
        args.source_revision,
    )


def _instance_request(args: argparse.Namespace) -> InstanceRequest:
    if not all((args.operation_id, args.instance_id, args.display_label, args.service_account, args.bind_port)):
        raise SystemInstanceProvisionerError("instance request arguments are required")
    return InstanceRequest(
        args.operation_id, args.instance_id, args.display_label,
        args.service_account, args.bind_port, _release(args),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engineering-platform-system-provisioner")
    parser.add_argument("command", choices=(
        "inventory", "create", "status", "update-assess", "update-execute",
        "update-resume", "update-status", "repair", "remove", "provider-register",
    ))
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--launch-daemons-dir", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--instance-id")
    parser.add_argument("--confirm-instance-id")
    parser.add_argument("--display-label")
    parser.add_argument("--service-account")
    parser.add_argument("--bind-port", type=int)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--artifact-digest")
    parser.add_argument("--version")
    parser.add_argument("--source-revision")
    parser.add_argument("--provider", choices=tuple(sorted(system_provider_context.PROVIDERS)))
    parser.add_argument("--provider-executable-digest")
    parser.add_argument("--provider-version")
    parser.add_argument("--auth-reference")
    parser.add_argument("--auth-bootstrap-receipt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        provisioner = SystemInstanceProvisioner(
            args.product_root,
            launch_daemons_dir=args.launch_daemons_dir,
        )
        if args.command == "inventory":
            result = provisioner.inventory()
        elif args.command == "create":
            result = provisioner.create(_instance_request(args))
        elif args.command == "status":
            result = provisioner.status(str(args.instance_id or ""))
        elif args.command == "update-assess":
            result = provisioner.assess_update(str(args.instance_id or ""), _release(args))
        elif args.command in {"update-execute", "update-resume"}:
            if not args.operation_id:
                raise SystemInstanceProvisionerError("update operation ID is required")
            result = provisioner.update(
                instance_id=str(args.instance_id or ""),
                operation_id=args.operation_id,
                release=None if args.command == "update-resume" else _release(args),
                resume=args.command == "update-resume",
            )
        elif args.command == "update-status":
            result = provisioner.update_status(str(args.instance_id or ""), str(args.operation_id or ""))
        elif args.command == "repair":
            result = provisioner.repair(str(args.instance_id or ""), str(args.operation_id or ""))
        elif args.command == "remove":
            result = provisioner.remove(
                str(args.instance_id or ""), str(args.operation_id or ""),
                confirm_instance_id=str(args.confirm_instance_id or ""),
            )
        else:
            required = (
                args.instance_id, args.display_label, args.service_account, args.provider,
                args.provider_executable_digest, args.provider_version,
                args.auth_reference, args.auth_bootstrap_receipt,
            )
            if not all(required):
                raise SystemInstanceProvisionerError("provider registration arguments are required")
            result = provisioner.provider_register(
                instance_id=args.instance_id,
                display_label=args.display_label,
                service_account=args.service_account,
                provider=args.provider,
                executable_sha256=args.provider_executable_digest,
                version=args.provider_version,
                auth_reference=args.auth_reference,
                auth_bootstrap_receipt=args.auth_bootstrap_receipt,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        SystemInstanceProvisionerError,
        system_installation_topology.SystemInstallationTopologyError,
        system_provider_context.SystemProviderContextError,
        system_server_service.SystemServerServiceError,
        installation_update_operation.InstallationUpdateOperationError,
        installation_update_plan.InstallationUpdatePlanError,
        installation_update_preparation.InstallationUpdatePreparationError,
        installation_update_admission.InstallationUpdateAdmissionError,
        installation_update_composition.InstallationUpdateCompositionError,
        operational_installation_record.OperationalInstallationRecordError,
        operational_installation.OperationalInstallationError,
    ) as error:
        print(json.dumps({"contract": CONTRACT, "status": "error", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
