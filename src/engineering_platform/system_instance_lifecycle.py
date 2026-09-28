"""Product-owned preserve/purge/restore lifecycle for EP Server instances."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping, Protocol

from . import operational_installation_record
from . import system_installation_topology
from . import system_provider_context


CONTRACT = "engineering-platform.system-instance-lifecycle/v1"
_PROVIDER_PRESERVED = "PRESERVED_REQUIRES_REVERIFICATION"
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SystemInstanceLifecycleError(RuntimeError):
    pass


class ServiceController(Protocol):
    def register(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        interpreter: Path,
    ) -> Mapping[str, object]: ...
    def loaded(self, instance: system_installation_topology.SystemInstanceTopology) -> bool: ...
    def remove(self, instance: system_installation_topology.SystemInstanceTopology) -> Mapping[str, object]: ...


RuntimeInstaller = Callable[
    [object], system_installation_topology.RuntimeSlot
]
ServiceInterpreter = Callable[
    [system_installation_topology.SystemInstanceTopology], Path | None
]


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self.fd)
            self.fd = None
            raise SystemInstanceLifecycleError("another operation owns the exact EP instance lifecycle") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _json_digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemInstanceLifecycleError(f"EP lifecycle evidence is unavailable: {path.name}") from error
    if not isinstance(value, dict):
        raise SystemInstanceLifecycleError(f"EP lifecycle evidence is invalid: {path.name}")
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


def _regular_digest(path: Path) -> str:
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise SystemInstanceLifecycleError(f"EP preserved instance contains unsafe entry: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            payload = stream.read(1024 * 1024)
            if not payload:
                break
            digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _tree_digest(root: Path) -> str:
    try:
        details = root.lstat()
    except OSError as error:
        raise SystemInstanceLifecycleError("EP preserved instance root is unavailable") from error
    if root.is_symlink() or not stat.S_ISDIR(details.st_mode):
        raise SystemInstanceLifecycleError("EP preserved instance root is unsafe")
    entries: list[dict[str, object]] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in sorted(names):
            path = parent / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SystemInstanceLifecycleError(f"EP preserved instance contains unsafe entry: {path}")
            entries.append({
                "path": str(path.relative_to(root)),
                "kind": "DIRECTORY",
                "mode": stat.S_IMODE(info.st_mode),
            })
        for name in sorted(files):
            path = parent / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SystemInstanceLifecycleError(f"EP preserved instance contains unsafe entry: {path}")
            entries.append({
                "path": str(path.relative_to(root)),
                "kind": "FILE",
                "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size,
                "digest": _regular_digest(path),
            })
    return "sha256:" + hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _mark_provider_auth_preserved(
    instance: system_installation_topology.SystemInstanceTopology,
) -> list[str]:
    providers: list[str] = []
    for context in system_provider_context.provider_contexts(instance):
        if context.auth_receipt.is_symlink() or context.descriptor.is_symlink():
            raise SystemInstanceLifecycleError("EP provider lifecycle evidence contains a symbolic link")
        auth = _read_json(context.auth_receipt)
        if (
            auth.get("provider") != context.provider
            or auth.get("instance_id") != instance.instance_id
            or auth.get("state") not in {"READY", _PROVIDER_PRESERVED}
        ):
            raise SystemInstanceLifecycleError("EP provider authentication evidence cannot be preserved")
        auth["state"] = _PROVIDER_PRESERVED
        _atomic_json(context.auth_receipt, auth)
        providers.append(context.provider)
    return sorted(providers)


class SystemInstanceLifecycle:
    def __init__(
        self,
        product: system_installation_topology.SystemInstallationTopology,
        controller: ServiceController,
        service_interpreter: ServiceInterpreter,
    ) -> None:
        self.product = product
        self.controller = controller
        self.service_interpreter = service_interpreter

    def _root(self, instance_id: str) -> Path:
        identity = system_installation_topology.validate_instance_id(instance_id)
        return self.product.system_root / "instance-lifecycle-v1" / identity

    def _operation_root(self, instance_id: str, operation_id: str) -> Path:
        if _OPERATION.fullmatch(operation_id) is None:
            raise SystemInstanceLifecycleError("EP lifecycle operation ID is invalid")
        return self._root(instance_id) / "operations" / operation_id

    def _state(
        self,
        instance_id: str,
        operation_id: str,
        operation: str,
        request: Mapping[str, object],
    ) -> tuple[Path, dict[str, object]]:
        root = self._operation_root(instance_id, operation_id)
        path = root / "state.json"
        if path.exists():
            state = _read_json(path)
            if (
                state.get("contract") != CONTRACT
                or state.get("operation") != operation
                or state.get("request") != dict(request)
                or state.get("request_digest") != _json_digest(dict(request))
            ):
                raise SystemInstanceLifecycleError("EP lifecycle operation identity conflict")
            return root, state
        state = {
            "contract": CONTRACT,
            "operation": operation,
            "operation_id": operation_id,
            "instance_id": instance_id,
            "request": dict(request),
            "request_digest": _json_digest(dict(request)),
            "phase": "PREPARED",
        }
        _atomic_json(path, state)
        return root, state

    def _save(self, root: Path, state: Mapping[str, object], phase: str, **evidence: object) -> dict[str, object]:
        updated = {**state, **evidence, "phase": phase}
        _atomic_json(root / "state.json", updated)
        return updated

    def _receipt(
        self,
        root: Path,
        state: Mapping[str, object],
        evidence: Mapping[str, object],
    ) -> dict[str, object]:
        path = root / "receipt.json"
        receipt = {
            "contract": CONTRACT,
            "operation": state["operation"],
            "operation_id": state["operation_id"],
            "instance_id": state["instance_id"],
            "request_digest": state["request_digest"],
            "state": "COMPLETE",
            "evidence": dict(evidence),
        }
        receipt["receipt_sha256"] = _json_digest(receipt)
        if path.exists():
            existing = _read_json(path)
            digest = existing.get("receipt_sha256")
            unsigned = {key: value for key, value in existing.items() if key != "receipt_sha256"}
            if (
                digest != _json_digest(unsigned)
                or existing.get("contract") != CONTRACT
                or existing.get("operation") != state["operation"]
                or existing.get("request_digest") != state["request_digest"]
            ):
                raise SystemInstanceLifecycleError("EP lifecycle terminal receipt conflict")
            receipt = existing
        else:
            _atomic_json(path, receipt)
        self._save(root, state, "COMPLETE", receipt_sha256=receipt["receipt_sha256"])
        return receipt

    def _terminal(self, root: Path, state: Mapping[str, object]) -> dict[str, object]:
        receipt = _read_json(root / "receipt.json")
        digest = receipt.get("receipt_sha256")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        if (
            digest != _json_digest(unsigned)
            or receipt.get("contract") != CONTRACT
            or receipt.get("operation") != state.get("operation")
            or receipt.get("request_digest") != state.get("request_digest")
            or state.get("receipt_sha256") != digest
        ):
            raise SystemInstanceLifecycleError("EP lifecycle terminal receipt is invalid")
        return receipt

    def _preserve_receipt(self, instance_id: str, preserve_operation_id: str) -> dict[str, object]:
        root = self._operation_root(instance_id, preserve_operation_id)
        receipt = _read_json(root / "receipt.json")
        digest = receipt.get("receipt_sha256")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        evidence = receipt.get("evidence")
        if (
            digest != _json_digest(unsigned)
            or receipt.get("contract") != CONTRACT
            or receipt.get("operation") != "PRESERVE"
            or receipt.get("instance_id") != instance_id
            or not isinstance(evidence, Mapping)
            or evidence.get("lifecycle_state") != "UNINSTALLED_DATA_PRESERVED"
            or evidence.get("restorable") is not True
        ):
            raise SystemInstanceLifecycleError("EP preserve evidence does not authorize restore")
        return receipt

    def preserve(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        descriptor: Mapping[str, object],
        operation_id: str,
        *,
        confirm_instance_id: str,
    ) -> Mapping[str, object]:
        if confirm_instance_id != instance.instance_id:
            raise SystemInstanceLifecycleError("EP preserve confirmation mismatch")
        if (self._root(instance.instance_id) / "purged.json").exists():
            raise SystemInstanceLifecycleError("EP instance identity was permanently purged")
        request = {
            "instance_id": instance.instance_id,
            "descriptor_digest": _json_digest(dict(descriptor)),
            "selected_runtime": descriptor.get("selected_runtime"),
        }
        root, state = self._state(instance.instance_id, operation_id, "PRESERVE", request)
        if state.get("phase") == "COMPLETE":
            receipt = self._terminal(root, state)
            if _tree_digest(instance.root) != receipt["evidence"]["mutable_instance_data_digest"]:
                raise SystemInstanceLifecycleError("EP preserved instance changed after terminal preserve")
            return receipt
        with _Lock(instance.lifecycle_lock):
            self.controller.remove(instance)
            if self.controller.loaded(instance) or self.service_interpreter(instance) is not None:
                raise SystemInstanceLifecycleError("EP preserved service was not fully removed")
            providers = _mark_provider_auth_preserved(instance)
            tree_digest = _tree_digest(instance.root)
            state = self._save(
                root,
                state,
                "VERIFIED",
                mutable_instance_data_digest=tree_digest,
                providers=providers,
            )
            return self._receipt(root, state, {
                "lifecycle_state": "UNINSTALLED_DATA_PRESERVED",
                "instance_identity": "PRESERVED",
                "mutable_instance_data": "PRESERVED",
                "mutable_instance_data_digest": tree_digest,
                "restorable": True,
                "service_state": "REMOVED_OR_INACTIVE",
                "shared_immutable_runtime_slots": "PRESERVED",
                "provider_auth_state": _PROVIDER_PRESERVED,
                "selected_runtime": descriptor.get("selected_runtime"),
            })

    def restore(
        self,
        instance: system_installation_topology.SystemInstanceTopology,
        descriptor: Mapping[str, object],
        operation_id: str,
        preserve_operation_id: str,
        release: object,
        runtime_installer: RuntimeInstaller,
    ) -> Mapping[str, object]:
        if (self._root(instance.instance_id) / "purged.json").exists():
            raise SystemInstanceLifecycleError("EP purged instance identity cannot be restored")
        preserved = self._preserve_receipt(instance.instance_id, preserve_operation_id)
        preserved_evidence = preserved["evidence"]
        release_identity = release.identity().payload()
        selected = descriptor.get("selected_runtime")
        if not isinstance(selected, Mapping) or any(
            selected.get(key) != value for key, value in release_identity.items()
        ):
            raise SystemInstanceLifecycleError("EP restore release does not match preserved runtime identity")
        request = {
            "instance_id": instance.instance_id,
            "preserve_operation_id": preserve_operation_id,
            "preserve_receipt_sha256": preserved["receipt_sha256"],
            "descriptor_digest": _json_digest(dict(descriptor)),
            "release": release_identity,
        }
        root, state = self._state(instance.instance_id, operation_id, "RESTORE", request)
        if state.get("phase") == "COMPLETE":
            return self._terminal(root, state)
        with _Lock(instance.lifecycle_lock):
            if self.controller.loaded(instance):
                raise SystemInstanceLifecycleError("EP preserved service unexpectedly became active")
            current_digest = _tree_digest(instance.root)
            if current_digest != preserved_evidence.get("mutable_instance_data_digest"):
                raise SystemInstanceLifecycleError("EP preserved instance data no longer matches restore evidence")
            slot = runtime_installer(release)
            if str(slot.interpreter) != selected.get("interpreter"):
                raise SystemInstanceLifecycleError("EP restored runtime slot does not match preserved interpreter")
            state = self._save(
                root,
                state,
                "VERIFIED",
                mutable_instance_data_digest=current_digest,
                expected_service_interpreter=str(slot.interpreter),
            )
            configured = self.service_interpreter(instance)
            if configured is None:
                service = self.controller.register(instance, slot.interpreter)
            elif state.get("phase") == "VERIFIED" and configured == slot.interpreter:
                service = {
                    "result": "REGISTERED",
                    "label": instance.service_label,
                    "replayed": True,
                }
            else:
                raise SystemInstanceLifecycleError("EP restore found a foreign service definition")
            return self._receipt(root, state, {
                "lifecycle_state": "RESTORED_REQUIRES_PROVIDER_REVERIFICATION",
                "instance_identity": "PRESERVED",
                "mutable_instance_data": "PRESERVED",
                "mutable_instance_data_digest": current_digest,
                "restored_from_preserve_operation": preserve_operation_id,
                "restored": True,
                "restorable": False,
                "service_state": "REGISTERED_INACTIVE",
                "shared_immutable_runtime_slots": "PRESERVED",
                "provider_auth_state": _PROVIDER_PRESERVED,
                "ready": False,
                "release": release_identity,
            })

    def record_purge(
        self,
        instance_id: str,
        operation_id: str,
        remove_receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        request = {
            "instance_id": instance_id,
            "legacy_remove_receipt_sha256": remove_receipt.get("receipt_sha256"),
        }
        root, state = self._state(instance_id, operation_id, "PURGE", request)
        if state.get("phase") == "COMPLETE":
            return self._terminal(root, state)
        instance_root = self.product.system_root / "instances" / instance_id
        if instance_root.exists() or instance_root.is_symlink():
            raise SystemInstanceLifecycleError("EP destructive remove did not purge instance root")
        tombstone = {
            "contract": CONTRACT,
            "instance_id": instance_id,
            "lifecycle_state": "PURGED",
            "restorable": False,
            "purge_operation_id": operation_id,
            "legacy_remove_receipt_sha256": remove_receipt.get("receipt_sha256"),
        }
        tombstone["tombstone_sha256"] = _json_digest(tombstone)
        tombstone_path = self._root(instance_id) / "purged.json"
        if tombstone_path.exists():
            if _read_json(tombstone_path) != tombstone:
                raise SystemInstanceLifecycleError("EP purge tombstone conflicts with prior evidence")
        else:
            _atomic_json(tombstone_path, tombstone)
        return self._receipt(root, state, {
            "lifecycle_state": "PURGED",
            "instance_identity": "RETIRED",
            "mutable_instance_data": "REMOVED",
            "restorable": False,
            "service_state": "REMOVED",
            "shared_immutable_runtime_slots": "PRESERVED",
            "provider_auth_state": "REMOVED_WITH_INSTANCE_DATA",
            "legacy_remove_receipt_sha256": remove_receipt.get("receipt_sha256"),
            "purge_tombstone_sha256": tombstone["tombstone_sha256"],
        })

    def status(self, instance_id: str, operation_id: str) -> Mapping[str, object]:
        root = self._operation_root(instance_id, operation_id)
        state = _read_json(root / "state.json")
        result: dict[str, object] = {
            "contract": CONTRACT,
            "operation": state.get("operation"),
            "operation_id": operation_id,
            "instance_id": instance_id,
            "phase": state.get("phase"),
            "state": "IN_PROGRESS",
        }
        if state.get("phase") == "COMPLETE":
            receipt = self._terminal(root, state)
            evidence = receipt["evidence"]
            result.update({
                "state": "COMPLETE",
                "lifecycle_state": evidence.get("lifecycle_state"),
                "restorable": evidence.get("restorable"),
                "receipt_sha256": receipt.get("receipt_sha256"),
            })
        return result
