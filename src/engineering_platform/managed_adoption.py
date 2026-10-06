"""Typed local-owner admission of an existing Managed engineering candidate."""
from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .execution_errors import RunnerError
from .local_repository_binding import LocalRepositoryBindingError, resolve_local_repository_binding
from .managed_publication import valid_branch
from .platform_admin import require_installation_owner
from .validation_profile import (
    VALIDATION_PROFILE_VERSION, changed_paths, classify, profile_control_bindings,
    validation_profile_identity,
)


_FIELDS = {"version", "project_id", "repository_id", "repository", "branch", "candidate_sha",
           "base_sha", "run_id", "repair_ordinal", "validation_profile_digest"}


def parse_selection(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Managed adoption selection fields are invalid.")
    if (value["version"] != "1.0" or not valid_branch(value["branch"])
            or type(value["repair_ordinal"]) is not int or not 0 <= value["repair_ordinal"] <= 3
            or any(not isinstance(value[key], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value[key]) is None
                   for key in ("project_id", "repository_id"))
            or not isinstance(value["repository"], str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]) is None
            or not isinstance(value["run_id"], str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["run_id"]) is None
            or any(not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{40}", value[key]) is None
                   for key in ("candidate_sha", "base_sha"))
            or not isinstance(value["validation_profile_digest"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["validation_profile_digest"]) is None):
        raise ValueError("Managed adoption selection identity is invalid.")
    return dict(value)


def profile_digest(root: Path, candidate_sha: str, repair_ordinal: int) -> str:
    """Use the same candidate/profile serializer as required local validation."""
    profile = classify(changed_paths(root, "main"))
    _, digest = validation_profile_identity(
        candidate_sha=candidate_sha, currentness=repair_ordinal, selected_validation_tier=profile.tier,
        validation_profile_version=VALIDATION_PROFILE_VERSION,
        profile_reference=f"validation-profile-registry:{profile.tier}@{VALIDATION_PROFILE_VERSION}",
        profile_selection_source="diff_classification", required_validation_controls=profile.required_controls,
        control_bindings=profile_control_bindings(profile, repository_root=root),
    )
    return digest


def verify_selection(*, selection: object, state, root: Path, repository, central_database: Path | None,
                     owner_authorized: bool):
    """Require actual owner CLI authority, exact local project and run lineage.

    A producer-supplied boolean or prompt cannot select this path. The normal
    execution-host entrypoint supplies the local owner flag separately, and
    resumes retain the same immutable selection and the existing run budget.
    """
    try:
        selected = parse_selection(selection)
    except ValueError as error:
        raise RunnerError(str(error)) from error
    previous = state.managed_candidate_adoption
    if (not owner_authorized or not state.owner_authorized or state.execution_mode != "MANAGED"
            or state.action_intent != "MUTATING_DELIVERY" or state.transaction_kind != "IMPLEMENTATION"
            or state.terminal or state.pull_request is not None or state.publication_intent is not None
            or selected["run_id"] != state.run_id or selected["repository"] != state.repository
            or (previous is not None and selected != previous)
            or (previous is None and selected["repair_ordinal"] != state.repair_iterations)):
        raise RunnerError("Managed adoption authority or run lineage is invalid.")
    if central_database is None or not central_database.is_file():
        raise RunnerError("Managed adoption requires the selected CENTRAL project binding.")
    try:
        actor = "local-uid:" + require_installation_owner(central_database.parent)
    except (OSError, PermissionError) as error:
        raise RunnerError("Managed adoption requires the actual installation owner.") from error
    if state.managed_adoption_actor not in {None, actor}:
        raise RunnerError("Managed adoption owner differs from the durable authority.")
    try:
        with closing(sqlite3.connect(f"file:{central_database}?mode=ro", uri=True)) as connection:
            binding = resolve_local_repository_binding(
                connection, project_id=str(selected["project_id"]), repository_id=str(selected["repository_id"]),
                data_root=central_database.parent,
            )
            active = connection.execute(
                "SELECT 1 FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'",
                (selected["project_id"],),
            ).fetchone()
            foreign = connection.execute(
                "SELECT t.run_id FROM engineering_transactions t WHERE t.run_id<>? "
                "AND json_extract(t.payload,'$.repository')=? AND ("
                "json_extract(t.payload,'$.branch')=? OR json_extract(t.payload,'$.implementation_branch')=? "
                "OR EXISTS(SELECT 1 FROM execution_run_leases l WHERE l.run_id=t.run_id AND l.lease_state='ACTIVE')) LIMIT 1",
                (state.run_id, selected["repository"], selected["branch"], selected["branch"]),
            ).fetchone()
    except (sqlite3.Error, LocalRepositoryBindingError) as error:
        raise RunnerError("Managed adoption project binding is unavailable.") from error
    if active is None or binding.local_root != root.resolve():
        raise RunnerError("Managed adoption project or repository binding differs from the selected checkout.")
    if foreign is not None:
        raise RunnerError("Managed adoption candidate belongs to another run or active writer.")
    observed = repository.inspect(root)
    expected_sha = selected["candidate_sha"]
    if previous is not None and state.repair_iterations != selected["repair_ordinal"]:
        if (state.repair_iterations < selected["repair_ordinal"] or state.repair_iterations > 3
                or not state.repair_audit or state.repair_audit[-1].get("iteration") != str(state.repair_iterations)
                or state.repair_audit[-1].get("commit_sha") != state.implementation_head_sha):
            raise RunnerError("Managed adoption repair lineage is unresolved.")
        expected_sha = state.implementation_head_sha
    if (not observed.clean or observed.repository != selected["repository"]
            or observed.branch != selected["branch"] or observed.head_sha != expected_sha
            or repository.trusted_origin_identity(root) != selected["repository"]
            or repository.workspace_operation_active(root)
            or repository.protected_main_revision(root) != selected["base_sha"]
            or repository.local_main_revision(root) != selected["base_sha"]
            or not repository.revision_is_ancestor(root, str(selected["base_sha"]), observed.head_sha)):
        raise RunnerError("Managed adoption candidate is stale, foreign, changed or dirty.")
    if expected_sha == selected["candidate_sha"] and profile_digest(root, observed.head_sha, state.repair_iterations) != selected["validation_profile_digest"]:
        raise RunnerError("Managed adoption validation profile differs from the current candidate.")
    return replace(state, managed_candidate_adoption=selected, managed_adoption_actor=actor, branch=observed.branch,
                   implementation_branch=observed.branch, execution_baseline_sha=str(selected["base_sha"]))


def main() -> None:
    """Print an inspectable selection; this read-only command grants no authority."""
    import argparse
    import json
    from .execution_repository import SubprocessRepositoryClient
    parser = argparse.ArgumentParser(description="Inspect a Managed candidate selection for explicit owner adoption")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repair-ordinal", type=int, default=0)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    repository = SubprocessRepositoryClient()
    observed = repository.inspect(root)
    selected = parse_selection({
        "version": "1.0", "project_id": args.project_id, "repository_id": args.repository_id,
        "repository": observed.repository, "branch": observed.branch, "candidate_sha": observed.head_sha,
        "base_sha": repository.protected_main_revision(root), "run_id": args.run_id,
        "repair_ordinal": args.repair_ordinal,
        "validation_profile_digest": profile_digest(root, observed.head_sha, args.repair_ordinal),
    })
    print(json.dumps(selected, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
