"""Public, provider-neutral Engineering Platform API.

This module is the supported boundary for workspace-aware platform consumers.
Provider implementations remain private and existing command wrappers remain
compatible.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from copy import deepcopy
from pathlib import Path
from .providers import registry


class PlatformConfigurationError(ValueError):
    """Raised for invalid or unsupported platform configuration."""


@dataclass(frozen=True)
class PlatformIdentity:
    id: str
    name: str
    version: str
    generation: int
    documentation_namespace: str
    capability_registry_version: int


@dataclass(frozen=True)
class WorkspaceIdentity:
    id: str
    name: str
    repository_provider: str
    repository_owner: str
    repository_name: str
    default_branch: str
    dashboard_title: str
    provisioning_root: str | None


@dataclass(frozen=True)
class PlatformConfiguration:
    schema_version: int
    platform: PlatformIdentity
    workspace: WorkspaceIdentity
    providers: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> "PlatformConfiguration":
        path = root / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            local = root / ".engineering" / "engineering-platform.local.json"
            if local.is_file():
                override = json.loads(local.read_text(encoding="utf-8"))
                if set(override) - {"workspace"}:
                    raise PlatformConfigurationError("Local Engineering Platform configuration is incompatible.")
                raw = _merge(raw, override)
            platform, workspace = raw["platform"], raw["workspace"]
            repository, branding = workspace["repository"], workspace["branding"]
            providers = raw["providers"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise PlatformConfigurationError("Engineering Platform configuration is invalid.") from error
        expected = {"runtime", "repository", "service_manager", "remote_submission", "private_remote_access", "dashboard"}
        if set(raw) != {"schema_version", "platform", "workspace", "providers"} or raw.get("schema_version") != 1 or set(providers) != expected or not all(isinstance(v, str) and v for v in providers.values()):
            raise PlatformConfigurationError("Engineering Platform configuration is incompatible.")
        provisioning_root = workspace.get("provisioning_root")
        if provisioning_root is not None and (
            not isinstance(provisioning_root, str)
            or not provisioning_root
            or not Path(provisioning_root).is_absolute()
        ):
            raise PlatformConfigurationError("Engineering Workspace Root is invalid.")
        identity = PlatformIdentity(platform["id"], platform["name"], platform["version"], platform["generation"], platform["documentation_namespace"], platform["capability_registry_version"])
        if identity.id != "engineering-platform" or identity.version != "1.5.0" or identity.generation != 2:
            raise PlatformConfigurationError("Engineering Platform identity is incompatible.")
        return cls(1, identity, WorkspaceIdentity(workspace["id"], workspace["name"], repository["provider"], repository["owner"], repository["name"], repository["default_branch"], branding["dashboard_title"], provisioning_root), dict(providers))


def _merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def capabilities() -> tuple[str, ...]:
    """Deterministic public capability registry for the 1.5 product boundary."""
    return ("runner", "runtime_provider", "repository_provider", "service_manager_provider", "remote_submission_provider", "private_remote_access_provider", "dashboard", "qualification", "repository_handoff")


def provider_registry(root: Path) -> dict[str, object]:
    """Public provider discovery API; unknown or unavailable providers are explicit."""
    configuration = PlatformConfiguration.load(root)
    active = registry(root)
    if set(active) != {"runtime", "repository", "service_manager", "remote_submission", "private_remote_access"}:
        raise PlatformConfigurationError("Provider registry is incomplete.")
    return {kind: {"selected": configuration.providers[kind], "status": active[kind]} for kind in active}


def bootstrap_repository(root: Path) -> dict[str, object]:
    """Public idempotent consumer bootstrap; never changes product source."""
    from .platform_bootstrap import provision_workspace, validate_repository
    configuration = validate_repository(root)
    return {"platform": configuration.platform.id, "workspace": configuration.workspace.id, "provisioned": {name: str(path) for name, path in provision_workspace(root).items()}}
