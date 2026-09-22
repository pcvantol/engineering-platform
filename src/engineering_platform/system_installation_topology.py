"""Pure topology and runtime-slot planning for the EP system provisioner.

The system-domain EP provisioner uses one machine-scoped directory tree
before it can safely create a venv, acquire a lock, retain recovery material or
change the Server LaunchDaemon.  This module defines that tree and the exact,
digest-pinned target slot without creating any directory, reading an installed
runtime, acquiring a lock, or invoking a service command.

It is deliberately not an installer.  In particular, an operation-scoped
candidate is never a valid selected runtime here: the mutator must build a
target venv directly in its final slot because a venv must not be moved after
its entry points have been created.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Mapping

from . import system_server_service


COMPONENT = "engineering-platform-server"
SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# Operation directories must be portable to the usual case-insensitive macOS
# volume.  Mixed-case identifiers could otherwise name the same recovery or
# staging directory while remaining lexically distinct in durable evidence.
_OPERATION = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SLOT_ID = re.compile(r"^sha256-[0-9a-f]{64}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_ACCOUNT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_INSTANCE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_PYTHON = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_MODES = frozenset({"INSTALL", "UPDATE"})
_RECOVERY_RETENTION = "REQUIRED_UNTIL_EXPLICIT_RECOVERY_DISPOSITION"


class SystemInstallationTopologyError(ValueError):
    """The EP provisioner has no closed, safe path topology."""


def validate_instance_id(value: object) -> str:
    """Return one stable opaque instance identity safe on macOS filesystems.

    Instance identifiers are deliberately not host names, display labels or
    service labels.  Lower-case portable spelling also prevents two lexical
    identities from aliasing on the usual case-insensitive system volume.
    """
    if not isinstance(value, str) or _INSTANCE_ID.fullmatch(value) is None:
        raise SystemInstallationTopologyError("EP Server instance ID is invalid")
    return value


def validate_instance_label(value: object) -> str:
    """Validate display-only metadata without turning it into authority."""
    if not isinstance(value, str) or _INSTANCE_LABEL.fullmatch(value) is None:
        raise SystemInstallationTopologyError("EP Server instance label is invalid")
    return value


def instance_service_label(instance_id: str) -> str:
    """Derive a collision-resistant launchd label from the opaque identity."""
    identity = validate_instance_id(instance_id)
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"com.engineeringplatform.server.instance-{suffix}"


def instance_service_account(instance_id: str) -> str:
    """Derive the default non-root account for one Server instance."""
    identity = validate_instance_id(instance_id)
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"_ep_{suffix}"


def _directory(value: str | Path, *, label: str) -> Path:
    """Normalize an absolute directory spelling without creating it.

    ``resolve(strict=False)`` deliberately canonicalizes aliases such as
    ``/tmp`` and ``/private/tmp``.  A later privileged writer must still use
    descriptor-anchored, no-follow mutation; this planning boundary is not a
    filesystem ownership or race proof.
    """
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise SystemInstallationTopologyError(f"{label} must be absolute")
        normalized = raw.resolve(strict=False)
    except SystemInstallationTopologyError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemInstallationTopologyError(f"{label} is unavailable") from error
    if normalized == Path("/"):
        raise SystemInstallationTopologyError(
            f"{label} must not be the filesystem root"
        )
    return normalized


def _launcher(value: str | Path, *, label: str) -> Path:
    """Normalize a venv launcher while retaining its final symlink spelling."""
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise SystemInstallationTopologyError(f"{label} must be absolute")
        if raw.parent.name != "bin" or _PYTHON.fullmatch(raw.name) is None:
            raise SystemInstallationTopologyError(
                f"{label} must be a venv bin/python launcher"
            )
        return raw.parent.resolve(strict=False) / raw.name
    except SystemInstallationTopologyError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemInstallationTopologyError(f"{label} is unavailable") from error


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _require_mapping(
    value: object, *, fields: frozenset[str], label: str
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SystemInstallationTopologyError(f"{label} is invalid")
    return dict(value)


@dataclass(frozen=True)
class ReleaseIdentity:
    """One exact, published EP artifact identity for a final runtime slot."""

    version: str
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
        ):
            raise SystemInstallationTopologyError("release identity is invalid")

    def payload(self) -> dict[str, str]:
        return {
            "version": self.version,
            "artifact_digest": self.artifact_digest,
            "source_revision": self.source_revision,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ReleaseIdentity":
        payload = _require_mapping(
            value,
            fields=frozenset({"version", "artifact_digest", "source_revision"}),
            label="release identity",
        )
        return cls(
            version=payload["version"] if isinstance(payload["version"], str) else "",
            artifact_digest=payload["artifact_digest"]
            if isinstance(payload["artifact_digest"], str)
            else "",
            source_revision=payload["source_revision"]
            if isinstance(payload["source_revision"], str)
            else "",
        )


@dataclass(frozen=True)
class SystemInstallationTopology:
    """One declared machine-level EP product tree, without filesystem effects."""

    system_root: Path
    data_root: Path
    runtime_root: Path
    operations_root: Path
    recovery_root: Path
    machine_lock: Path
    service_label: str
    service_account: str

    def __post_init__(self) -> None:
        root = _directory(self.system_root, label="EP system installation root")
        if (
            root != self.system_root
            or self.data_root != root / "data"
            or self.runtime_root != root / "runtimes"
            or self.operations_root != root / "operations"
            or self.recovery_root != root / "recovery"
            or self.machine_lock != root / "locks" / f"{COMPONENT}.lock"
            or self.service_label != system_server_service.LABEL
            or not isinstance(self.service_account, str)
            or self.service_account == "root"
            or _ACCOUNT.fullmatch(self.service_account) is None
        ):
            raise SystemInstallationTopologyError(
                "EP system installation topology is invalid"
            )
        roots = (
            self.data_root,
            self.runtime_root,
            self.operations_root,
            self.recovery_root,
            self.machine_lock.parent,
        )
        if len(set(roots)) != len(roots) or any(
            not _inside(root, path) for path in roots
        ):
            raise SystemInstallationTopologyError(
                "EP system installation topology is invalid"
            )

    def payload(self) -> dict[str, str]:
        return {
            "system_root": str(self.system_root),
            "data_root": str(self.data_root),
            "runtime_root": str(self.runtime_root),
            "operations_root": str(self.operations_root),
            "recovery_root": str(self.recovery_root),
            "machine_lock": str(self.machine_lock),
            "service_label": self.service_label,
            "service_account": self.service_account,
        }

    @classmethod
    def from_payload(cls, value: object) -> "SystemInstallationTopology":
        payload = _require_mapping(
            value,
            fields=frozenset(
                {
                    "system_root",
                    "data_root",
                    "runtime_root",
                    "operations_root",
                    "recovery_root",
                    "machine_lock",
                    "service_label",
                    "service_account",
                }
            ),
            label="system installation topology",
        )
        root = payload["system_root"]
        account = payload["service_account"]
        if not isinstance(root, str) or not isinstance(account, str):
            raise SystemInstallationTopologyError(
                "system installation topology is invalid"
            )
        topology = system_installation_topology(root, service_account=account)
        if topology.payload() != payload:
            raise SystemInstallationTopologyError(
                "system installation topology is invalid"
            )
        return topology


def system_installation_topology(
    system_root: str | Path,
    *,
    service_account: str = system_server_service.DEFAULT_SERVICE_ACCOUNT,
) -> SystemInstallationTopology:
    """Derive the closed EP system tree from an explicit product root.

    The root is intentionally an explicit provisioner input.  This pure
    planning function does not bless an existing user root as machine-owned,
    create the account, or establish filesystem ownership.
    """
    root = _directory(system_root, label="EP system installation root")
    if (
        not isinstance(service_account, str)
        or service_account == "root"
        or _ACCOUNT.fullmatch(service_account) is None
    ):
        raise SystemInstallationTopologyError("EP system service account is invalid")
    topology = SystemInstallationTopology(
        system_root=root,
        data_root=root / "data",
        runtime_root=root / "runtimes",
        operations_root=root / "operations",
        recovery_root=root / "recovery",
        machine_lock=root / "locks" / f"{COMPONENT}.lock",
        service_label=system_server_service.LABEL,
        service_account=service_account,
    )
    return topology


@dataclass(frozen=True)
class SystemInstanceTopology:
    """All mutable roots owned by exactly one system-domain Server instance.

    ``product`` owns immutable digest slots and the artifact coordination lock.
    Everything below ``root`` is instance-owned and may be quiesced, migrated,
    repaired or removed without changing another instance.
    """

    product: SystemInstallationTopology
    instance_id: str
    display_label: str
    root: Path
    data_root: Path
    operations_root: Path
    recovery_root: Path
    backups_root: Path
    cache_root: Path
    logs_root: Path
    providers_root: Path
    lifecycle_lock: Path
    descriptor: Path
    service_label: str
    service_account: str

    def __post_init__(self) -> None:
        if not isinstance(self.product, SystemInstallationTopology):
            raise SystemInstallationTopologyError("EP Server instance topology is invalid")
        identity = validate_instance_id(self.instance_id)
        validate_instance_label(self.display_label)
        expected_root = self.product.system_root / "instances" / identity
        expected = {
            "root": expected_root,
            "data_root": expected_root / "data",
            "operations_root": expected_root / "operations",
            "recovery_root": expected_root / "recovery",
            "backups_root": expected_root / "backups",
            "cache_root": expected_root / "cache",
            "logs_root": expected_root / "logs",
            "providers_root": expected_root / "providers",
            "lifecycle_lock": expected_root / "locks" / "lifecycle.lock",
            "descriptor": expected_root / "instance.json",
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise SystemInstallationTopologyError("EP Server instance topology is invalid")
        if self.service_label != instance_service_label(identity):
            raise SystemInstallationTopologyError("EP Server instance service label is invalid")
        if (
            not isinstance(self.service_account, str)
            or self.service_account == "root"
            or _ACCOUNT.fullmatch(self.service_account) is None
        ):
            raise SystemInstallationTopologyError("EP Server instance service account is invalid")
        owned = (
            self.data_root,
            self.operations_root,
            self.recovery_root,
            self.backups_root,
            self.cache_root,
            self.logs_root,
            self.providers_root,
            self.lifecycle_lock.parent,
        )
        if len(set(owned)) != len(owned) or any(not _inside(expected_root, path) for path in owned):
            raise SystemInstallationTopologyError("EP Server instance topology is invalid")
        if _inside(expected_root, self.product.runtime_root):
            raise SystemInstallationTopologyError("EP Server shared runtime topology is invalid")

    @property
    def artifact_lock(self) -> Path:
        """The only product-wide lock; it coordinates immutable slot bytes."""
        return self.product.system_root / "locks" / "artifacts.lock"

    def operation_lock(self, operation_id: str) -> Path:
        operation, _staging, _recovery = _instance_operation_paths(self, operation_id)
        return operation / "operation.lock"

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "instance_id": self.instance_id,
            "display_label": self.display_label,
            "product_root": str(self.product.system_root),
            "instance_root": str(self.root),
            "data_root": str(self.data_root),
            "operations_root": str(self.operations_root),
            "recovery_root": str(self.recovery_root),
            "backups_root": str(self.backups_root),
            "cache_root": str(self.cache_root),
            "logs_root": str(self.logs_root),
            "providers_root": str(self.providers_root),
            "lifecycle_lock": str(self.lifecycle_lock),
            "artifact_lock": str(self.artifact_lock),
            "descriptor": str(self.descriptor),
            "service_label": self.service_label,
            "service_account": self.service_account,
        }


def system_instance_topology(
    product: SystemInstallationTopology,
    *,
    instance_id: str,
    display_label: str,
    service_account: str | None = None,
) -> SystemInstanceTopology:
    """Derive one closed instance tree without creating or inspecting it."""
    if not isinstance(product, SystemInstallationTopology):
        raise SystemInstallationTopologyError("EP Server product topology is invalid")
    identity = validate_instance_id(instance_id)
    validate_instance_label(display_label)
    root = product.system_root / "instances" / identity
    return SystemInstanceTopology(
        product=product,
        instance_id=identity,
        display_label=display_label,
        root=root,
        data_root=root / "data",
        operations_root=root / "operations",
        recovery_root=root / "recovery",
        backups_root=root / "backups",
        cache_root=root / "cache",
        logs_root=root / "logs",
        providers_root=root / "providers",
        lifecycle_lock=root / "locks" / "lifecycle.lock",
        descriptor=root / "instance.json",
        service_label=instance_service_label(identity),
        service_account=service_account or instance_service_account(identity),
    )


def _instance_operation_paths(
    topology: SystemInstanceTopology, operation_id: str,
) -> tuple[Path, tuple[Path, ...], Path]:
    if not isinstance(topology, SystemInstanceTopology):
        raise SystemInstallationTopologyError("EP Server instance topology is invalid")
    if not isinstance(operation_id, str) or _OPERATION.fullmatch(operation_id) is None:
        raise SystemInstallationTopologyError("provisioning operation ID is invalid")
    operation = topology.operations_root / operation_id
    staging = tuple(operation / name for name in ("build", "download", "pip-cache"))
    recovery = topology.recovery_root / operation_id
    if (
        not _inside(topology.operations_root, operation)
        or not _inside(topology.recovery_root, recovery)
        or any(not _inside(operation, path) for path in staging)
    ):
        raise SystemInstallationTopologyError("provisioning operation topology is invalid")
    return operation, staging, recovery


@dataclass(frozen=True)
class InstanceProvisioningTransition:
    """Pure, exact-instance install/update plan using shared runtime bytes."""

    mode: str
    operation_id: str
    instance: SystemInstanceTopology
    target: RuntimeSlot
    current_interpreter: Path | None
    operation_root: Path
    staging_targets: tuple[Path, ...]
    recovery_root: Path

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise SystemInstallationTopologyError("instance provisioning transition is invalid")
        if not isinstance(self.instance, SystemInstanceTopology):
            raise SystemInstallationTopologyError("instance provisioning transition is invalid")
        if self.target.topology != self.instance.product:
            raise SystemInstallationTopologyError("instance provisioning transition is invalid")
        operation, staging, recovery = _instance_operation_paths(self.instance, self.operation_id)
        if (self.operation_root, self.staging_targets, self.recovery_root) != (operation, staging, recovery):
            raise SystemInstallationTopologyError("instance provisioning transition is invalid")
        if (self.mode == "INSTALL") != (self.current_interpreter is None):
            raise SystemInstallationTopologyError("instance provisioning transition is invalid")
        if self.current_interpreter is not None:
            current = _current_runtime(self.instance.product, self.current_interpreter)
            if current != self.current_interpreter or _inside(self.target.root, current):
                raise SystemInstallationTopologyError("instance provisioning transition is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "component": COMPONENT,
            "mode": self.mode,
            "operation_id": self.operation_id,
            "instance": self.instance.payload(),
            "target": self.target.payload(),
            "current_interpreter": None if self.current_interpreter is None else str(self.current_interpreter),
            "operation": {
                "root": str(self.operation_root),
                "lock": str(self.instance.operation_lock(self.operation_id)),
                "staging_targets": [str(path) for path in self.staging_targets],
            },
            "recovery": {"root": str(self.recovery_root), "retention": _RECOVERY_RETENTION},
            "activation": {
                "instance_id": self.instance.instance_id,
                "service_label": self.instance.service_label,
                "interpreter": str(self.target.interpreter),
                "data_root": str(self.instance.data_root),
                "state": "NOT_EXECUTED",
            },
            "coordination": {
                "instance_lifecycle_lock": str(self.instance.lifecycle_lock),
                "shared_artifact_lock": str(self.instance.artifact_lock),
                "other_instances_mutable_state": "OUT_OF_SCOPE",
            },
        }


def plan_instance_install(
    instance: SystemInstanceTopology, *, operation_id: str, release: ReleaseIdentity,
) -> InstanceProvisioningTransition:
    target = runtime_slot(instance.product, release)
    operation, staging, recovery = _instance_operation_paths(instance, operation_id)
    return InstanceProvisioningTransition(
        "INSTALL", operation_id, instance, target, None, operation, staging, recovery,
    )


def plan_instance_update(
    instance: SystemInstanceTopology,
    *,
    operation_id: str,
    release: ReleaseIdentity,
    current_interpreter: str | Path,
) -> InstanceProvisioningTransition:
    target = runtime_slot(instance.product, release)
    current = _current_runtime(instance.product, current_interpreter)
    operation, staging, recovery = _instance_operation_paths(instance, operation_id)
    return InstanceProvisioningTransition(
        "UPDATE", operation_id, instance, target, current, operation, staging, recovery,
    )


@dataclass(frozen=True)
class RuntimeSlot:
    """A final, immutable destination for exactly one artifact digest."""

    topology: SystemInstallationTopology
    release: ReleaseIdentity
    slot_id: str
    root: Path
    venv: Path
    interpreter: Path

    def __post_init__(self) -> None:
        """Reject a detached or forged slot before it can carry evidence.

        ``RuntimeSlot`` is public because it appears in the durable transition
        evidence.  It must therefore be closed on its own, rather than relying
        on a later ``ProvisioningTransition`` to notice a foreign root.
        """
        if not isinstance(self.topology, SystemInstallationTopology) or not isinstance(
            self.release, ReleaseIdentity
        ):
            raise SystemInstallationTopologyError("runtime slot is invalid")
        expected_slot_id = "sha256-" + self.release.artifact_digest.removeprefix(
            "sha256:"
        )
        expected_root = self.topology.runtime_root / "slots" / expected_slot_id
        expected_venv = expected_root / "venv"
        expected_interpreter = expected_venv / "bin" / "python"
        if (
            self.slot_id != expected_slot_id
            or self.root != expected_root
            or self.venv != expected_venv
            or self.interpreter != expected_interpreter
            or not _inside(self.topology.runtime_root, self.root)
            or _inside(self.topology.operations_root, self.root)
            or _inside(self.topology.recovery_root, self.root)
            or _launcher(self.interpreter, label="EP target interpreter")
            != self.interpreter
        ):
            raise SystemInstallationTopologyError("runtime slot is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "release": self.release.payload(),
            "slot_id": self.slot_id,
            "root": str(self.root),
            "venv": str(self.venv),
            "interpreter": str(self.interpreter),
        }


def runtime_slot(
    topology: SystemInstallationTopology, release: ReleaseIdentity
) -> RuntimeSlot:
    """Return the deterministic final venv location for exact artifact bytes."""
    if not isinstance(topology, SystemInstallationTopology) or not isinstance(
        release, ReleaseIdentity
    ):
        raise SystemInstallationTopologyError("runtime slot inputs are invalid")
    slot_id = "sha256-" + release.artifact_digest.removeprefix("sha256:")
    root = topology.runtime_root / "slots" / slot_id
    venv = root / "venv"
    interpreter = venv / "bin" / "python"
    return RuntimeSlot(topology, release, slot_id, root, venv, interpreter)


@dataclass(frozen=True)
class ProvisioningTransition:
    """Closed, non-mutating intent for one future EP install or update."""

    mode: str
    operation_id: str
    topology: SystemInstallationTopology
    target: RuntimeSlot
    current_interpreter: Path | None
    operation_root: Path
    staging_targets: tuple[Path, ...]
    recovery_root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, str)
            or self.mode not in _MODES
            or not isinstance(self.operation_id, str)
        ):
            raise SystemInstallationTopologyError("provisioning transition is invalid")
        if not isinstance(self.topology, SystemInstallationTopology) or not isinstance(
            self.target, RuntimeSlot
        ):
            raise SystemInstallationTopologyError("provisioning transition is invalid")
        expected_target = runtime_slot(self.topology, self.target.release)
        if self.target != expected_target:
            raise SystemInstallationTopologyError("provisioning transition is invalid")
        operation, staging, recovery = _operation_paths(
            self.topology, self.operation_id
        )
        if (
            self.operation_root != operation
            or self.staging_targets != staging
            or self.recovery_root != recovery
        ):
            raise SystemInstallationTopologyError("provisioning transition is invalid")
        if (self.mode == "INSTALL") != (self.current_interpreter is None):
            raise SystemInstallationTopologyError("provisioning transition is invalid")
        if self.current_interpreter is not None:
            current = _current_runtime(self.topology, self.current_interpreter)
            if current != self.current_interpreter or _inside(
                self.target.root, current
            ):
                raise SystemInstallationTopologyError(
                    "provisioning transition is invalid"
                )

    def payload(self) -> dict[str, object]:
        protected = [
            str(self.topology.data_root),
            str(self.topology.runtime_root),
            str(self.target.root),
            str(self.recovery_root),
        ]
        if self.current_interpreter is not None:
            protected.append(str(self.current_interpreter))
        return {
            "schema_version": SCHEMA_VERSION,
            "component": COMPONENT,
            "mode": self.mode,
            "operation_id": self.operation_id,
            "topology": self.topology.payload(),
            "target": self.target.payload(),
            "current_interpreter": None
            if self.current_interpreter is None
            else str(self.current_interpreter),
            "operation": {
                "root": str(self.operation_root),
                "staging_targets": [str(path) for path in self.staging_targets],
            },
            "recovery": {
                "root": str(self.recovery_root),
                "retention": _RECOVERY_RETENTION,
            },
            "activation": {
                "service_label": self.topology.service_label,
                "interpreter": str(self.target.interpreter),
                "state": "NOT_EXECUTED",
            },
            "cleanup": {
                "ephemeral_targets": [str(path) for path in self.staging_targets],
                "protected_targets": protected,
            },
        }


def _operation_paths(
    topology: SystemInstallationTopology, operation_id: str
) -> tuple[Path, tuple[Path, ...], Path]:
    if not isinstance(operation_id, str) or _OPERATION.fullmatch(operation_id) is None:
        raise SystemInstallationTopologyError("provisioning operation ID is invalid")
    operation = topology.operations_root / operation_id
    staging = tuple(operation / name for name in ("build", "download", "pip-cache"))
    recovery = topology.recovery_root / operation_id
    if (
        not _inside(topology.operations_root, operation)
        or not _inside(topology.recovery_root, recovery)
        or any(not _inside(operation, path) for path in staging)
    ):
        raise SystemInstallationTopologyError(
            "provisioning operation topology is invalid"
        )
    return operation, staging, recovery


def _current_runtime(topology: SystemInstallationTopology, value: str | Path) -> Path:
    current = _launcher(value, label="current EP runtime")
    protected_roots = (
        topology.data_root,
        topology.operations_root,
        topology.recovery_root,
    )
    if any(_inside(root, current) for root in protected_roots):
        raise SystemInstallationTopologyError(
            "current EP runtime is not an eligible operational runtime"
        )
    if _inside(topology.system_root, current):
        # A legacy runtime may legitimately live outside this new system tree.
        # Within it, however, only an already-finalized, canonical slot may be
        # current evidence.  Do not let a lock, logs, or an unclassified
        # product subtree masquerade as a selected interpreter.
        try:
            relative = current.relative_to(topology.runtime_root)
        except ValueError:
            raise SystemInstallationTopologyError(
                "current EP runtime is not an eligible operational runtime"
            ) from None
        if (
            len(relative.parts) != 5
            or relative.parts[0] != "slots"
            or _SLOT_ID.fullmatch(relative.parts[1]) is None
            or relative.parts[2:] != ("venv", "bin", "python")
        ):
            raise SystemInstallationTopologyError(
                "current EP runtime is not an eligible operational runtime"
            )
    return current


def _transition(
    topology: SystemInstallationTopology,
    *,
    mode: str,
    operation_id: str,
    release: ReleaseIdentity,
    current_interpreter: str | Path | None,
) -> ProvisioningTransition:
    if not isinstance(topology, SystemInstallationTopology) or not isinstance(
        release, ReleaseIdentity
    ):
        raise SystemInstallationTopologyError(
            "provisioning transition inputs are invalid"
        )
    if mode not in _MODES:
        raise SystemInstallationTopologyError("provisioning transition mode is invalid")
    if (mode == "INSTALL") != (current_interpreter is None):
        raise SystemInstallationTopologyError(
            "provisioning transition current runtime is invalid"
        )
    current = (
        None
        if current_interpreter is None
        else _current_runtime(topology, current_interpreter)
    )
    target = runtime_slot(topology, release)
    if current is not None and _inside(target.root, current):
        raise SystemInstallationTopologyError("target EP runtime is already selected")
    operation, staging, recovery = _operation_paths(topology, operation_id)
    protected = (topology.data_root, topology.runtime_root, target.root, recovery)
    if any(any(_inside(path, item) for path in protected) for item in staging):
        raise SystemInstallationTopologyError(
            "provisioning cleanup topology is invalid"
        )
    return ProvisioningTransition(
        mode, operation_id, topology, target, current, operation, staging, recovery
    )


def plan_install(
    topology: SystemInstallationTopology,
    *,
    operation_id: str,
    release: ReleaseIdentity,
) -> ProvisioningTransition:
    """Plan a clean install without creating its final runtime slot."""
    return _transition(
        topology,
        mode="INSTALL",
        operation_id=operation_id,
        release=release,
        current_interpreter=None,
    )


def plan_update(
    topology: SystemInstallationTopology,
    *,
    operation_id: str,
    release: ReleaseIdentity,
    current_interpreter: str | Path,
) -> ProvisioningTransition:
    """Plan an update from one eligible existing runtime without changing it."""
    return _transition(
        topology,
        mode="UPDATE",
        operation_id=operation_id,
        release=release,
        current_interpreter=current_interpreter,
    )


def transition_from_payload(value: object) -> ProvisioningTransition:
    """Reconstruct only the canonical, untampered transition payload shape."""
    payload = _require_mapping(
        value,
        fields=frozenset(
            {
                "schema_version",
                "component",
                "mode",
                "operation_id",
                "topology",
                "target",
                "current_interpreter",
                "operation",
                "recovery",
                "activation",
                "cleanup",
            }
        ),
        label="provisioning transition",
    )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["component"] != COMPONENT
    ):
        raise SystemInstallationTopologyError("provisioning transition is invalid")
    topology = SystemInstallationTopology.from_payload(payload["topology"])
    target_payload = _require_mapping(
        payload["target"],
        fields=frozenset({"release", "slot_id", "root", "venv", "interpreter"}),
        label="runtime slot",
    )
    release = ReleaseIdentity.from_payload(target_payload["release"])
    mode, operation_id, current = (
        payload["mode"],
        payload["operation_id"],
        payload["current_interpreter"],
    )
    if not isinstance(mode, str) or not isinstance(operation_id, str):
        raise SystemInstallationTopologyError("provisioning transition is invalid")
    if mode == "INSTALL":
        transition = plan_install(topology, operation_id=operation_id, release=release)
    elif mode == "UPDATE" and isinstance(current, str):
        transition = plan_update(
            topology,
            operation_id=operation_id,
            release=release,
            current_interpreter=current,
        )
    else:
        raise SystemInstallationTopologyError("provisioning transition is invalid")
    if transition.payload() != payload:
        raise SystemInstallationTopologyError("provisioning transition is invalid")
    return transition
