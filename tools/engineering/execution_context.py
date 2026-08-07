"""Typed execution selection resolved before lifecycle admission."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

from .execution_errors import RunnerError
from .platform_api import PlatformConfiguration, PlatformConfigurationError
from .providers import GitProvider


@dataclass(frozen=True)
class ExecutionContext:
    execution_mode: str
    host_repository: Path
    target_repository: Path | None
    lifecycle_policy: str
    selected_preflight: str
    run_id: str | None = None


def _prompt_field_values(objective: str, label: str) -> tuple[str, ...]:
    values: list[str] = []
    lines = objective.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() != f"{label}:".casefold():
            continue
        for candidate in lines[index + 1 :]:
            value = candidate.strip()
            if value:
                values.append(value)
                break
    return tuple(values)


def resolve_execution_context(objective: str, host_repository: Path) -> ExecutionContext:
    """Resolve the mode and selected target before lifecycle admission."""
    modes = {line.split(":", 1)[1].strip().casefold() for line in objective.splitlines() if line.strip().casefold().startswith("execution mode:")}
    if "genesis" not in modes:
        return ExecutionContext("MANAGED", host_repository.resolve(), None, "managed", "managed_readiness")
    if modes != {"genesis"}:
        raise RunnerError("Execution Mode: Genesis conflicts with another execution mode declaration.")
    targets = _prompt_field_values(objective, "Target repository")
    if not targets:
        raise RunnerError("Genesis preflight blocked: prompt must declare one absolute Target repository path.")
    if len(set(targets)) != 1:
        raise RunnerError("Genesis preflight blocked: prompt declares conflicting Target repository paths.")
    target = Path(targets[0]).expanduser()
    if not target.is_absolute():
        raise RunnerError("Genesis preflight blocked: Target repository path must be absolute.")
    target = target.resolve()
    if target == host_repository.resolve():
        raise RunnerError("Genesis preflight blocked: Target repository cannot be the Engineering Platform host repository.")
    return ExecutionContext("GENESIS", host_repository.resolve(), target, "local_only", "genesis_git_workspace")


def execution_mode_for(objective: str) -> str:
    return "GENESIS" if any(line.strip().casefold() == "execution mode: genesis" for line in objective.splitlines()) else "MANAGED"


def genesis_target_for(objective: str) -> Path | None:
    targets = _prompt_field_values(objective, "Target repository")
    return Path(targets[0]).expanduser() if len(set(targets)) == 1 else None


def genesis_workspace_preflight(target: Path | None) -> str | None:
    """Return a bounded, non-destructive Genesis workspace diagnostic."""
    if target is None:
        return "Genesis preflight blocked: prompt must declare an absolute Target repository path."
    if not target.is_absolute() or not target.is_dir():
        return "Genesis preflight blocked: Target repository path is absent or not a directory."
    observed = GitProvider().execute(target, "git", "rev-parse", "--git-dir")
    if observed.returncode:
        return "Genesis preflight blocked: Target repository is not an accessible Git repository."
    git_dir = Path(observed.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (target / git_dir).resolve()
    if (git_dir / "index.lock").exists():
        return f"Genesis preflight blocked: Git index lock exists at {git_dir / 'index.lock'}; it is not removed automatically."
    if not os.access(git_dir, os.W_OK):
        return f"Genesis preflight blocked: Git metadata directory is not writable: {git_dir}."
    status = GitProvider().execute(target, "git", "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        return "Genesis preflight blocked: Target repository status could not be inspected."
    return "Genesis preflight blocked: Target repository has tracked or untracked changes." if status.stdout.strip() else None


def additional_workspace_write_roots(root: Path) -> tuple[Path, ...]:
    """Resolve only configured, non-symlink Genesis write roots."""
    if not (root / ".engineering" / "engineering-platform.local.json").is_file():
        return ()
    try:
        policy = PlatformConfiguration.load(root).resolve_repository_authorization_policy()
    except PlatformConfigurationError as error:
        raise RunnerError(str(error)) from error
    roots: list[Path] = []
    for configured in policy.allowed_roots:
        candidate = Path(configured.path).expanduser()
        if candidate.is_symlink() or not candidate.is_dir() or candidate == Path(candidate.anchor):
            raise RunnerError("Configured Engineering Workspace Root must be an existing non-root directory, not a symlink.")
        roots.append(candidate.resolve())
    for configured in policy.allowed_repositories:
        candidate = Path(configured).expanduser()
        if candidate.is_symlink() or not candidate.is_dir():
            raise RunnerError("Configured Engineering Repository Allow-List entry must be an existing directory, not a symlink.")
        roots.append(candidate.resolve().parent)
    return tuple(dict.fromkeys(roots))


def target_repository_authorization(root: Path, target: Path) -> str | None:
    try:
        authorization = PlatformConfiguration.load(root).authorize_target_repository(target, "GENESIS")
    except PlatformConfigurationError as error:
        return f"Genesis preflight blocked: {error}"
    if authorization.authorized:
        return None
    return f"Genesis preflight blocked: WORKSPACE_TARGET_AUTHORIZED: {authorization.reason} Recovery: {authorization.recovery}"
