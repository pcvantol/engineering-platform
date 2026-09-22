"""Instance-owned provider runtime and cold-boot readback contracts.

This module never performs an interactive login and never copies credential
files.  A provider-supported bootstrap may populate one exact instance context
and then commit the non-secret authentication receipt defined here.  Server
startup and the Forge Platform provisioner can subsequently prove that the
same executable, home/config root, instance and receipt are still selected.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

from .system_installation_topology import SystemInstanceTopology, validate_instance_id


PROVIDERS = frozenset({"codex", "github"})
SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTH_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")


class SystemProviderContextError(ValueError):
    """An instance provider context is ambiguous, substituted or unavailable."""


@dataclass(frozen=True)
class ProviderContext:
    provider: str
    instance_id: str
    root: Path
    installation_root: Path
    executable: Path
    home: Path
    auth_receipt: Path
    descriptor: Path

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise SystemProviderContextError("unsupported EP Server provider context")
        validate_instance_id(self.instance_id)
        name = "codex" if self.provider == "codex" else "gh"
        expected = {
            "installation_root": self.root / "runtime",
            "executable": self.root / "runtime" / "bin" / name,
            "home": self.root / ("home" if self.provider == "codex" else "config"),
            "auth_receipt": self.root / "auth-state.json",
            "descriptor": self.root / "context.json",
        }
        if any(getattr(self, field) != path for field, path in expected.items()):
            raise SystemProviderContextError("EP Server provider context topology is invalid")

    def payload(self) -> dict[str, str | int]:
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": self.provider,
            "instance_id": self.instance_id,
            "root": str(self.root),
            "installation_root": str(self.installation_root),
            "executable": str(self.executable),
            "home": str(self.home),
            "auth_receipt": str(self.auth_receipt),
        }


def provider_context(instance: SystemInstanceTopology, provider: str) -> ProviderContext:
    if not isinstance(instance, SystemInstanceTopology) or provider not in PROVIDERS:
        raise SystemProviderContextError("unsupported EP Server provider context")
    root = instance.providers_root / provider
    name = "codex" if provider == "codex" else "gh"
    return ProviderContext(
        provider=provider,
        instance_id=instance.instance_id,
        root=root,
        installation_root=root / "runtime",
        executable=root / "runtime" / "bin" / name,
        home=root / ("home" if provider == "codex" else "config"),
        auth_receipt=root / "auth-state.json",
        descriptor=root / "context.json",
    )


def provider_contexts(instance: SystemInstanceTopology) -> tuple[ProviderContext, ProviderContext]:
    return provider_context(instance, "codex"), provider_context(instance, "github")


def _object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemProviderContextError(f"EP Server provider {label} is unavailable") from error
    if not isinstance(value, dict):
        raise SystemProviderContextError(f"EP Server provider {label} is invalid")
    return value


def executable_digest(path: Path) -> str:
    """Hash one exact regular executable without following a replacement link."""
    try:
        details = path.lstat()
        if path.is_symlink() or not path.is_file() or not details.st_mode & 0o111:
            raise SystemProviderContextError("EP Server provider executable is unavailable")
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except SystemProviderContextError:
        raise
    except OSError as error:
        raise SystemProviderContextError("EP Server provider executable is unavailable") from error


def record_context(
    context: ProviderContext,
    *,
    executable_sha256: str,
    version: str,
    auth_reference: str,
    auth_bootstrap_receipt: str,
) -> dict[str, object]:
    """Commit non-secret provider readiness after supported external bootstrap.

    The executable must already be installed at the instance-owned path.  This
    function neither accepts a source credential path nor serializes provider
    output, tokens, cookies or keychain material.
    """
    if not isinstance(context, ProviderContext):
        raise SystemProviderContextError("EP Server provider context is invalid")
    if not isinstance(executable_sha256, str) or _DIGEST.fullmatch(executable_sha256) is None:
        raise SystemProviderContextError("EP Server provider executable digest is invalid")
    if executable_digest(context.executable) != executable_sha256:
        raise SystemProviderContextError("EP Server provider executable digest mismatch")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise SystemProviderContextError("EP Server provider version is invalid")
    for value in (auth_reference, auth_bootstrap_receipt):
        if not isinstance(value, str) or _AUTH_REFERENCE.fullmatch(value) is None:
            raise SystemProviderContextError("EP Server provider authentication reference is invalid")
    context.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    context.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = {
        **context.payload(),
        "executable_sha256": executable_sha256,
        "version": version,
        "credential_scope": "COMPONENT_INSTANCE",
        "cold_boot_required": True,
    }
    auth = {
        "schema_version": SCHEMA_VERSION,
        "provider": context.provider,
        "instance_id": context.instance_id,
        "state": "READY",
        "auth_reference": auth_reference,
        "bootstrap_receipt": auth_bootstrap_receipt,
        "executable_sha256": executable_sha256,
    }
    _atomic_json(context.descriptor, descriptor)
    _atomic_json(context.auth_receipt, auth)
    return readback(context)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def readback(context: ProviderContext, *, authentication_required: bool = True) -> dict[str, object]:
    """Prove executable and non-secret authentication identity independently."""
    descriptor = _object(context.descriptor, "descriptor")
    expected_fields = set(context.payload()) | {
        "executable_sha256", "version", "credential_scope", "cold_boot_required",
    }
    if set(descriptor) != expected_fields or any(
        descriptor.get(field) != value for field, value in context.payload().items()
    ):
        raise SystemProviderContextError("EP Server provider descriptor is invalid")
    digest = executable_digest(context.executable)
    if (
        descriptor.get("executable_sha256") != digest
        or descriptor.get("credential_scope") != "COMPONENT_INSTANCE"
        or descriptor.get("cold_boot_required") is not True
    ):
        raise SystemProviderContextError("EP Server provider executable was substituted")
    auth_state = "NOT_REQUIRED"
    auth_reference: str | None = None
    if authentication_required:
        auth = _object(context.auth_receipt, "authentication receipt")
        if (
            set(auth) != {
                "schema_version", "provider", "instance_id", "state", "auth_reference",
                "bootstrap_receipt", "executable_sha256",
            }
            or auth.get("schema_version") != SCHEMA_VERSION
            or auth.get("provider") != context.provider
            or auth.get("instance_id") != context.instance_id
            or auth.get("state") != "READY"
            or auth.get("executable_sha256") != digest
            or not isinstance(auth.get("auth_reference"), str)
            or _AUTH_REFERENCE.fullmatch(str(auth["auth_reference"])) is None
            or not isinstance(auth.get("bootstrap_receipt"), str)
            or _AUTH_REFERENCE.fullmatch(str(auth["bootstrap_receipt"])) is None
        ):
            raise SystemProviderContextError("EP Server provider authentication receipt is invalid")
        auth_state = "READY"
        auth_reference = str(auth["auth_reference"])
    return {
        "provider": context.provider,
        "instance_id": context.instance_id,
        "state": "READY",
        "executable": str(context.executable),
        "executable_sha256": digest,
        "version": descriptor["version"],
        "home": str(context.home),
        "credential_scope": "COMPONENT_INSTANCE",
        "authentication": {"state": auth_state, "reference": auth_reference},
        "cold_boot_ready": True,
    }


def server_environment(instance: SystemInstanceTopology) -> dict[str, str]:
    """Return only explicit provider selections for this instance."""
    codex, github = provider_contexts(instance)
    return {
        "EP_MANAGED_CODEX_CLI_PREFIX": str(codex.installation_root),
        "CODEX_HOME": str(codex.home),
        "EP_GITHUB_CLI_EXECUTABLE": str(github.executable),
        "GH_CONFIG_DIR": str(github.home),
    }


def assert_distinct(left: SystemInstanceTopology, right: SystemInstanceTopology) -> None:
    """Reject every mutable provider or instance collision between two targets."""
    if left.instance_id == right.instance_id:
        raise SystemProviderContextError("duplicate EP Server instance ID")
    left_paths = {
        left.root, left.data_root, left.operations_root, left.recovery_root,
        left.lifecycle_lock, *(item.root for item in provider_contexts(left)),
        *(item.home for item in provider_contexts(left)),
    }
    right_paths = {
        right.root, right.data_root, right.operations_root, right.recovery_root,
        right.lifecycle_lock, *(item.root for item in provider_contexts(right)),
        *(item.home for item in provider_contexts(right)),
    }
    if left.service_label == right.service_label or left.service_account == right.service_account:
        raise SystemProviderContextError("EP Server service identity collision")
    if left_paths & right_paths:
        raise SystemProviderContextError("EP Server mutable provider topology collision")
