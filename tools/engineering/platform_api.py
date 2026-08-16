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
import os
import shutil
import sys
from .providers import registry


class PlatformConfigurationError(ValueError):
    """Raised for invalid or unsupported platform configuration."""


# launchd deliberately starts with a minimal environment.  Preserve the
# resolved launcher rather than depending on a later child process finding a
# different PATH.  The value is local host configuration, not product data.
RUNTIME_EXECUTABLE_ENVIRONMENT = "DJCONNECT_ENGINEERING_CODEX_EXECUTABLE"
RUNTIME_PATH_FALLBACK = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def shared_workspace_store(root: Path) -> Path:
    """Resolve the private Engineering store shared by every Git worktree.

    A linked worktree has a ``.git`` *file* that points at its private Git
    directory.  Git's ``commondir`` file then identifies the repository-wide
    Git directory.  Keeping local Engineering evidence immediately below that
    common directory makes it independent of the currently checked-out
    worktree while remaining private and repository-scoped.

    Non-Git callers deliberately retain the old local layout.  This keeps
    isolated test fixtures and explicit non-repository tooling self-contained.
    """
    root = root.resolve()
    git_marker = root / ".git"
    if git_marker.is_dir():
        common = git_marker.resolve()
    elif git_marker.is_file():
        try:
            line = git_marker.read_text(encoding="utf-8").strip()
            prefix = "gitdir: "
            if not line.startswith(prefix):
                return root / ".engineering"
            git_dir = Path(line[len(prefix):].strip())
            if not git_dir.is_absolute():
                git_dir = git_marker.parent / git_dir
            common_marker = git_dir.resolve() / "commondir"
            if not common_marker.is_file():
                return root / ".engineering"
            common_path = common_marker.read_text(encoding="utf-8").strip()
            common = Path(common_path)
            if not common.is_absolute():
                common = common_marker.parent / common
            common = common.resolve()
        except OSError:
            return root / ".engineering"
    else:
        return root / ".engineering"
    return common / "engineering-platform"


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
class AllowedWorkspaceRoot:
    """A trusted directory which may contain engineering repositories."""

    path: str
    repository_scope: str


@dataclass(frozen=True)
class RepositoryAuthorizationPolicy:
    """Fail-closed repository admission policy owned by host configuration."""

    allowed_roots: tuple[AllowedWorkspaceRoot, ...]
    allowed_repositories: tuple[str, ...]
    denied_repositories: tuple[str, ...]
    symlink_policy: str
    case_sensitivity: str
    legacy: bool = False


@dataclass(frozen=True)
class RepositoryAuthorization:
    authorized: bool
    canonical_target: str | None
    matched: str | None
    scope: str | None
    reason: str
    recovery: str


@dataclass(frozen=True)
class RuntimePromptTransport:
    """Provider-neutral runtime-prompt transport; paths stay resolver-owned."""

    provider: str
    inbox: Path


@dataclass(frozen=True)
class ExecutionHostIdentity:
    name: str
    version: str
    runtime: str
    runtime_prompt_transport: str


class ExecutionHostConfigurationResolver:
    """The sole host-specific configuration and location resolution boundary."""

    def __init__(self, root: Path, configuration: "PlatformConfiguration") -> None:
        self._root = root.resolve()
        self._configuration = configuration

    def resolve_runtime_prompt_transport(self) -> RuntimePromptTransport:
        provider = self._configuration.providers["remote_submission"]
        if provider != "icloud_inbox":
            raise PlatformConfigurationError("Configured Runtime Prompt transport is unsupported.")
        override = os.environ.get("DJCONNECT_ENGINEERING_INBOX")
        inbox_root = Path(override).expanduser() if override else Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/DJConnect Engineering"
        return RuntimePromptTransport(provider, inbox_root / "Inbox")

    def resolve_workspace_store(self) -> Path:
        return shared_workspace_store(self._root)

    def resolve_status_store(self) -> Path:
        return self.resolve_workspace_store() / "status"

    def resolve_report_store(self) -> Path:
        return self.resolve_workspace_store() / "reports"

    def resolve_log_store(self) -> Path:
        return self.resolve_workspace_store() / "logs"

    def resolve_telemetry_store(self) -> Path:
        return self.resolve_workspace_store() / "engineering.db"

    def resolve_runtime(self) -> Path:
        if self._configuration.providers["runtime"] != "codex_cli":
            raise PlatformConfigurationError("Configured Execution Host runtime is unsupported.")
        configured = os.environ.get(RUNTIME_EXECUTABLE_ENVIRONMENT)
        if configured:
            executable = Path(configured).expanduser()
            if executable.is_file() and os.access(executable, os.X_OK):
                return executable
            raise PlatformConfigurationError("Configured Execution Host runtime is unavailable.")
        executable = shutil.which("codex")
        if not executable:
            raise PlatformConfigurationError("Configured Execution Host runtime is unavailable.")
        # Do not resolve the Homebrew launcher symlink.  Its directory is the
        # one that must be retained for launchd's PATH and Node resolution.
        return Path(executable)

    def runtime_environment(self) -> dict[str, str]:
        """Return a child-safe environment pinned to the admitted CLI launcher."""
        executable = self.resolve_runtime()
        entries = [str(executable.parent), *RUNTIME_PATH_FALLBACK]
        entries.extend(os.environ.get("PATH", "").split(":"))
        return {
            RUNTIME_EXECUTABLE_ENVIRONMENT: str(executable),
            "PATH": ":".join(dict.fromkeys(entry for entry in entries if entry)),
        }

    def resolve_execution_host_identity(self) -> ExecutionHostIdentity:
        return ExecutionHostIdentity(
            self._configuration.platform.name,
            self._configuration.platform.version,
            self._configuration.providers["runtime"],
            self._configuration.providers["remote_submission"],
        )


@dataclass(frozen=True)
class PlatformConfiguration:
    schema_version: int
    platform: PlatformIdentity
    workspace: WorkspaceIdentity
    providers: dict[str, str]
    repository_authorization: RepositoryAuthorizationPolicy

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
        if set(raw) != {"schema_version", "platform", "workspace", "providers"} or raw.get("schema_version") not in {1, 2} or set(providers) != expected or not all(isinstance(v, str) and v for v in providers.values()):
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
        authorization = _authorization_policy(workspace, provisioning_root)
        return cls(int(raw["schema_version"]), identity, WorkspaceIdentity(workspace["id"], workspace["name"], repository["provider"], repository["owner"], repository["name"], repository["default_branch"], branding["dashboard_title"], provisioning_root), dict(providers), authorization)

    def resolve_allowed_workspace_roots(self) -> tuple[AllowedWorkspaceRoot, ...]:
        return self.repository_authorization.allowed_roots

    def resolve_repository_authorization_policy(self) -> RepositoryAuthorizationPolicy:
        return self.repository_authorization

    def resolver(self, root: Path) -> ExecutionHostConfigurationResolver:
        return ExecutionHostConfigurationResolver(root, self)

    def authorize_target_repository(self, path: Path, execution_mode: str) -> RepositoryAuthorization:
        """Authorize one existing target without broadening host filesystem access."""
        policy = self.repository_authorization
        raw = path.expanduser()
        if not raw.is_absolute():
            return _denied(None, "Target repository path must be absolute.", "Resubmit with an absolute repository path.")
        if ".." in raw.parts:
            return _denied(None, "Target repository path contains traversal.", "Resubmit with a canonical repository path without '..'.")
        if policy.symlink_policy == "reject" and _has_untrusted_symlink_component(raw, policy):
            return _denied(None, "Target repository contains a symlink and the policy rejects symlinks.", "Use a non-symlink repository path or change trusted host configuration.")
        try:
            target = raw.resolve(strict=True)
        except OSError:
            return _denied(None, "Target repository cannot be resolved canonically.", "Correct the target path and retry.")
        if not target.is_dir():
            return _denied(str(target), "Target repository is not a directory.", "Select an existing repository directory.")
        if execution_mode == "MANAGED":
            return RepositoryAuthorization(True, str(target), "managed-host", "managed", "Managed target is the configured execution host.", "No action required.")
        denied = _matching_path(target, policy.denied_repositories, policy.case_sensitivity)
        if denied:
            return _denied(str(target), "Target repository is explicitly denied by trusted host configuration.", "Remove the deny-list entry only when the target is approved.")
        allowed = _matching_path(target, policy.allowed_repositories, policy.case_sensitivity)
        if allowed:
            return RepositoryAuthorization(True, str(target), f"allow-list:{allowed.name}", "explicit_allow_list", "Target repository is explicitly authorized.", "No action required.")
        for root in policy.allowed_roots:
            try:
                canonical_root = _canonical_root(root.path)
            except OSError:
                continue
            if _under_scope(target, canonical_root, root.repository_scope, policy.case_sensitivity):
                return RepositoryAuthorization(True, str(target), f"root:{canonical_root.name}", root.repository_scope, "Target repository matches an authorized workspace root.", "No action required.")
        return _denied(str(target), "Target repository does not match an authorized workspace root or allow-list entry.", "Add an approved root or explicit repository entry in trusted host configuration, then retry.")


def _authorization_policy(workspace: dict[str, object], provisioning_root: str | None) -> RepositoryAuthorizationPolicy:
    raw = workspace.get("workspace_authorization")
    if raw is None:
        roots = () if provisioning_root is None else (AllowedWorkspaceRoot(provisioning_root, "direct_children"),)
        return RepositoryAuthorizationPolicy(roots, (), (), "reject", "host", legacy=True)
    if not isinstance(raw, dict) or set(raw) - {"allowed_roots", "allowed_repositories", "denied_repositories", "symlink_policy", "case_sensitivity"}:
        raise PlatformConfigurationError("Engineering Workspace Authorization is invalid.")
    roots_raw = raw.get("allowed_roots", [])
    allow_raw = raw.get("allowed_repositories", [])
    deny_raw = raw.get("denied_repositories", [])
    if not isinstance(roots_raw, list) or not isinstance(allow_raw, list) or not isinstance(deny_raw, list):
        raise PlatformConfigurationError("Engineering Workspace Authorization is invalid.")
    roots: list[AllowedWorkspaceRoot] = []
    for entry in roots_raw:
        if not isinstance(entry, dict) or set(entry) != {"path", "repository_scope"}:
            raise PlatformConfigurationError("Engineering Workspace Authorization root is invalid.")
        path, scope = entry["path"], entry["repository_scope"]
        if not isinstance(path, str) or not Path(path).is_absolute() or scope not in {"direct_children", "descendants"}:
            raise PlatformConfigurationError("Engineering Workspace Authorization root is invalid.")
        roots.append(AllowedWorkspaceRoot(path, scope))
    if not all(isinstance(path, str) and Path(path).is_absolute() for path in (*allow_raw, *deny_raw)):
        raise PlatformConfigurationError("Engineering Workspace Authorization repository list is invalid.")
    symlink_policy = raw.get("symlink_policy", "reject")
    case_sensitivity = raw.get("case_sensitivity", "host")
    if symlink_policy not in {"reject", "canonicalize_within_root"} or case_sensitivity not in {"host", "sensitive"}:
        raise PlatformConfigurationError("Engineering Workspace Authorization is invalid.")
    return RepositoryAuthorizationPolicy(tuple(roots), tuple(allow_raw), tuple(deny_raw), symlink_policy, case_sensitivity)


def _canonical_root(value: str) -> Path:
    root = Path(value)
    if root.is_symlink() or not root.is_dir() or root == Path(root.anchor):
        raise OSError("invalid workspace root")
    return root.resolve(strict=True)


def _has_untrusted_symlink_component(path: Path, policy: RepositoryAuthorizationPolicy) -> bool:
    """Reject symlinks below a configured path, not host-owned mount aliases.

    macOS commonly exposes temporary directories through `/var`, itself a
    system symlink. A trusted configured root may therefore contain such a
    host alias. Only components below that exact configured root are untrusted.
    """
    configured_paths = [Path(root.path).expanduser() for root in policy.allowed_roots]
    configured_paths.extend(Path(value).expanduser() for value in policy.allowed_repositories)
    configured_paths.extend(Path(value).expanduser() for value in policy.denied_repositories)
    for configured in configured_paths:
        try:
            relative = path.relative_to(configured)
        except ValueError:
            continue
        current = configured
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
    return False


def _matching_path(target: Path, configured: tuple[str, ...], case_sensitivity: str) -> Path | None:
    for value in configured:
        try:
            candidate = Path(value).resolve(strict=True)
        except OSError:
            continue
        if _same_path(candidate, target, case_sensitivity):
            return candidate
    return None


def _same_path(left: Path, right: Path, case_sensitivity: str) -> bool:
    return _path_parts(left, case_sensitivity) == _path_parts(right, case_sensitivity)


def _under_scope(target: Path, root: Path, scope: str, case_sensitivity: str) -> bool:
    target_parts = _path_parts(target, case_sensitivity)
    root_parts = _path_parts(root, case_sensitivity)
    if len(target_parts) <= len(root_parts) or target_parts[:len(root_parts)] != root_parts:
        return False
    depth = len(target_parts) - len(root_parts)
    return scope == "descendants" or depth == 1


def _path_parts(path: Path, case_sensitivity: str) -> tuple[str, ...]:
    parts = path.parts
    # APFS normally has case-insensitive semantics; explicit `sensitive` keeps
    # exact component matching for hosts and volumes that require it.
    if case_sensitivity == "host" and sys.platform == "darwin":
        return tuple(part.casefold() for part in parts)
    return parts


def _denied(target: str | None, reason: str, recovery: str) -> RepositoryAuthorization:
    return RepositoryAuthorization(False, target, None, None, reason, recovery)


def resolve_allowed_workspace_roots(root: Path) -> tuple[AllowedWorkspaceRoot, ...]:
    return PlatformConfiguration.load(root).resolve_allowed_workspace_roots()


def resolve_repository_authorization_policy(root: Path) -> RepositoryAuthorizationPolicy:
    return PlatformConfiguration.load(root).resolve_repository_authorization_policy()


def authorize_target_repository(root: Path, path: Path, execution_mode: str) -> RepositoryAuthorization:
    return PlatformConfiguration.load(root).authorize_target_repository(path, execution_mode)


def execution_host_configuration(root: Path) -> ExecutionHostConfigurationResolver:
    """Return the canonical resolver; consumers must not derive host paths."""
    return PlatformConfiguration.load(root).resolver(root)


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
