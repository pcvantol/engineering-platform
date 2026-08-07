"""Typed readiness profiles selected before an Execution Host lifecycle starts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable
from datetime import datetime, timezone


class Requirement(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ReadinessProfile:
    profile_id: str
    version: int
    execution_mode: str
    repository: Requirement
    remote: Requirement
    upstream: Requirement
    clean_worktree: Requirement
    branch: Requirement
    workspace_authorization: Requirement
    host_qualification: Requirement
    capability_qualification: Requirement
    providers: Requirement
    datastore: Requirement
    active_run_lease: Requirement
    producer_contract: Requirement
    additional_constraints: tuple[str, ...] = ()


PLATFORM_HOST = ReadinessProfile("platform_host", 1, "ANY", Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.REQUIRED, Requirement.NOT_APPLICABLE, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE)
MANAGED_REPOSITORY = ReadinessProfile("managed_repository", 1, "MANAGED", Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED)
GENESIS_TARGET = ReadinessProfile("genesis_target", 1, "GENESIS", Requirement.REQUIRED, Requirement.NOT_APPLICABLE, Requirement.NOT_APPLICABLE, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED, Requirement.REQUIRED)


@dataclass(frozen=True)
class ReadinessResult:
    profile: ReadinessProfile
    ready: bool
    diagnostic: str | None = None


@dataclass(frozen=True)
class ReadinessFacts:
    host_ready: bool
    repository_present: bool
    repository_clean: bool | None
    lease_available: bool
    remote_present: bool | None = None
    upstream_present: bool | None = None
    branch_present: bool | None = None
    workspace_authorized: bool | None = None
    capabilities_available: bool | None = None
    providers_available: bool | None = None
    datastore_healthy: bool | None = None
    producer_contract_valid: bool | None = None

    @classmethod
    def from_preflight(cls, *, host: object, workspace: object, capability: object, lease_available: bool) -> "ReadinessFacts":
        """Adapt already-observed preflight outcomes without performing any probe."""
        def field(subject: object, name: str, default: object = None) -> object:
            return subject.get(name, default) if isinstance(subject, dict) else getattr(subject, name, default)

        def outcome(subject: object, identifier: str) -> bool | None:
            for item in field(subject, "checks", ()):
                if field(item, "identifier") == identifier:
                    return field(item, "outcome") == "PASS"
            return None

        host_ready = field(host, "outcome") == "PASS"
        repository_present = outcome(workspace, "target_repository") is True
        staged, unstaged, untracked = (outcome(workspace, identifier) for identifier in ("worktree_staged", "worktree_unstaged", "worktree_untracked"))
        repository_clean = (
            outcome(workspace, "clean_worktree")
            if None in {staged, unstaged, untracked}
            else all((staged, unstaged, untracked))
        )
        capabilities_ready = field(capability, "outcome") == "PASS"
        return cls(
            host_ready,
            repository_present,
            repository_clean,
            lease_available,
            remote_present=outcome(workspace, "managed_remote"),
            upstream_present=outcome(workspace, "managed_synchronization"),
            branch_present=outcome(workspace, "managed_expected_branch")
            if outcome(workspace, "managed_expected_branch") is not None
            else outcome(workspace, "genesis_local_repository"),
            workspace_authorized=outcome(workspace, "WORKSPACE_TARGET_AUTHORIZED"),
            capabilities_available=capabilities_ready,
            providers_available=outcome(capability, "provider_support"),
            datastore_healthy=outcome(host, "telemetry_storage"),
            producer_contract_valid=outcome(capability, "producer_contract"),
        )


@dataclass(frozen=True)
class ReadinessDecision:
    passed: bool
    profile_id: str
    profile_version: int
    execution_mode: str
    failed_requirements: tuple[str, ...]
    facts: ReadinessFacts
    evaluated_at: str
    diagnostic: str | None = None


def selected_profile(execution_mode: str) -> ReadinessProfile:
    if execution_mode == "GENESIS":
        return GENESIS_TARGET
    if execution_mode == "MANAGED":
        return MANAGED_REPOSITORY
    raise ValueError(f"Unsupported execution readiness mode: {execution_mode}")


def evaluate(
    profile: ReadinessProfile,
    *,
    host_root: Path,
    target_root: Path | None,
    managed_clean: Callable[[Path], bool],
    genesis_preflight: Callable[[Path | None], str | None],
) -> ReadinessResult:
    """Evaluate only the selected profile; never mix Genesis and Managed checks."""
    if profile.profile_id == PLATFORM_HOST.profile_id:
        return ReadinessResult(profile, host_root.is_dir(), None if host_root.is_dir() else "Execution Host repository is unavailable.")
    if profile.profile_id == GENESIS_TARGET.profile_id:
        diagnostic = genesis_preflight(target_root)
        return ReadinessResult(profile, diagnostic is None, diagnostic)
    if managed_clean(host_root):
        return ReadinessResult(profile, True)
    return ReadinessResult(profile, False, "working tree is not clean; unrelated work will not be touched")


def decide(profile: ReadinessProfile, facts: ReadinessFacts) -> ReadinessDecision:
    """Fail-closed policy evaluation over facts; it never probes the environment."""
    failed: list[str] = []
    requirements = (
        ("host_qualification", profile.host_qualification, facts.host_ready),
        ("repository", profile.repository, facts.repository_present),
        ("remote", profile.remote, facts.remote_present),
        ("upstream", profile.upstream, facts.upstream_present),
        ("clean_worktree", profile.clean_worktree, facts.repository_clean),
        ("branch", profile.branch, facts.branch_present),
        ("workspace_authorization", profile.workspace_authorization, facts.workspace_authorized),
        ("capability_qualification", profile.capability_qualification, facts.capabilities_available),
        ("providers", profile.providers, facts.providers_available),
        ("datastore", profile.datastore, facts.datastore_healthy),
        ("active_run_lease", profile.active_run_lease, facts.lease_available),
        ("producer_contract", profile.producer_contract, facts.producer_contract_valid),
    )
    for identifier, requirement, observed in requirements:
        if requirement is Requirement.REQUIRED and observed is not True:
            failed.append(identifier)
    return ReadinessDecision(
        not failed, profile.profile_id, profile.version, profile.execution_mode,
        tuple(failed), facts, datetime.now(timezone.utc).isoformat(),
        None if not failed else "Readiness requirements are not satisfied: " + ", ".join(failed),
    )
