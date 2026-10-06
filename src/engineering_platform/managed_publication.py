"""Durable first-draft publication for an already qualified Managed candidate.

The CREATE_UNCERTAIN checkpoint is a one-way boundary: absence of a remote
receipt after that point never authorizes another create. Only exact readback
can resolve it. GitHub and Git are the external adapters; this module owns the
decision and the SQLite checkpoint owns its identity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING

from .execution_errors import RunnerError

if TYPE_CHECKING:
    from pathlib import Path
    from .agent_state import StateStore, TransactionState
    from .execution_repository import GitHubClient, RepositoryClient


_IDENTITY = {"version", "run_id", "repository", "branch", "base", "candidate_sha",
             "validation_profile_digest", "assurance_profile_digest", "repair_ordinal"}
_STATES = {"PREPARED": 0, "CREATE_UNCERTAIN": 1, "RECONCILED": 2}


@dataclass(frozen=True)
class PublicationCandidate:
    number: int
    repository: str
    head_repository: str
    branch: str
    base: str
    head_sha: str
    state: str
    draft: bool


class PublicationRecovery(RunnerError):
    def __init__(self, state: TransactionState, reason: str, *, pending: bool = False) -> None:
        super().__init__(reason)
        self.state, self.pending = state, pending


def validate_intent(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _IDENTITY | {"status", "pull_request"}:
        raise ValueError("publication intent fields are invalid")
    if (value["version"] != "1.0" or value["status"] not in _STATES
            or value["base"] != "main"
            or not isinstance(value["repair_ordinal"], int) or isinstance(value["repair_ordinal"], bool)
            or not 0 <= value["repair_ordinal"] <= 3
            or not isinstance(value["run_id"], str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["run_id"]) is None
            or not isinstance(value["repository"], str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]) is None
            or not valid_branch(value["branch"])
            or not isinstance(value["candidate_sha"], str) or re.fullmatch(r"[0-9a-f]{40}", value["candidate_sha"]) is None
            or any(not isinstance(value[key], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value[key]) is None
                   for key in ("validation_profile_digest", "assurance_profile_digest"))):
        raise ValueError("publication intent identity is invalid")
    number = value["pull_request"]
    if ((value["status"] == "RECONCILED" and (type(number) is not int or number < 1))
            or (value["status"] != "RECONCILED" and number is not None)):
        raise ValueError("publication receipt is invalid")


def valid_branch(value: object) -> bool:
    return (isinstance(value, str) and value != "main" and len(value) <= 240
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is not None
            and not any(part in value for part in ("..", "//", "@{"))
            and not any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
            and not value.endswith(("/", ".")))


def validate_transition(previous: object, current: object) -> None:
    """Reject identity changes or rollback even from a stale checkpoint writer."""
    if current is not None:
        validate_intent(current)
    if previous is None:
        return
    validate_intent(previous)
    if (current is None or any(previous[key] != current[key] for key in _IDENTITY)
            or _STATES[current["status"]] < _STATES[previous["status"]]
            or (previous["status"] == "RECONCILED" and current != previous)):
        raise ValueError("publication intent cannot be replaced or rolled back")


def publish_candidate(*, state: TransactionState, store: StateStore, root: Path,
                      repository: RepositoryClient, github: GitHubClient) -> tuple[TransactionState, int]:
    """Recover or issue the sole create permitted by the durable intent."""
    profile = state.assurance_profile or {}
    identity = {"version": "1.0", "run_id": state.run_id, "repository": state.repository,
                "branch": state.branch, "base": "main", "candidate_sha": profile.get("candidate_sha"),
                "validation_profile_digest": profile.get("validation_profile_digest"),
                "assurance_profile_digest": profile.get("digest"), "repair_ordinal": state.repair_iterations}
    prepared = {**identity, "status": "PREPARED", "pull_request": None}
    try:
        validate_intent(prepared)
    except ValueError as error:
        raise PublicationRecovery(state, "publication_identity_invalid") from error
    if state.publication_intent is not None:
        if any(state.publication_intent[key] != value for key, value in identity.items()):
            raise PublicationRecovery(state, "publication_identity_changed")
    else:
        state = replace(state, publication_intent=prepared, next_action="read_publication_receipt")
        store.save(state, expected_publication_intent=None)

    def exact_workspace() -> None:
        observed = repository.inspect(root)
        if (not observed.clean or observed.repository != identity["repository"]
                or observed.branch != identity["branch"] or observed.head_sha != identity["candidate_sha"]
                or repository.trusted_origin_identity(root) != identity["repository"]
                or repository.workspace_operation_active(root)):
            raise PublicationRecovery(state, "publication_candidate_changed")

    def readback() -> list[PublicationCandidate]:
        try:
            return github.publication_candidates(state.repository, str(state.branch))
        except (RunnerError, RuntimeError, ValueError) as error:
            raise PublicationRecovery(state, "publication_readback_unavailable", pending=True) from error

    exact_workspace()
    candidates = readback()
    if not candidates and state.publication_intent["status"] == "PREPARED":
        # Push is also exact and compare-and-set: a foreign remote branch is
        # never overwritten. Repeat after a pre-create crash is harmless.
        try:
            repository.publish_candidate_branch(root, state.repository, str(state.branch), str(identity["candidate_sha"]))
        except RunnerError as error:
            raise PublicationRecovery(state, "publication_branch_conflict") from error
        exact_workspace()
        previous = state.publication_intent
        state = replace(state, publication_intent={**previous, "status": "CREATE_UNCERTAIN"})
        store.save(state, expected_publication_intent=previous)
        try:
            github.create_draft_publication(
                state.repository, str(state.branch), "main",
                f"Managed delivery: {state.branch}",
                f"Managed run `{state.run_id}`.\n\nCandidate `{identity['candidate_sha']}` passed current required validation and independent Quality and Security assurance.",
            )
        except (RunnerError, RuntimeError):
            # A transport error is indistinguishable from an accepted create
            # with lost acknowledgement. Readback is the only authority.
            pass
        candidates = readback()
    if not candidates:
        raise PublicationRecovery(state, "publication_receipt_unresolved", pending=True)
    if len(candidates) != 1:
        raise PublicationRecovery(state, "publication_ambiguous")
    candidate = candidates[0]
    if (candidate.repository != identity["repository"] or candidate.head_repository != identity["repository"]
            or candidate.branch != identity["branch"] or candidate.base != identity["base"]
            or candidate.head_sha != identity["candidate_sha"] or candidate.state != "OPEN"
            or candidate.draft is not True or type(candidate.number) is not int or candidate.number < 1):
        raise PublicationRecovery(state, "publication_remote_identity_conflict")
    exact_workspace()
    previous = state.publication_intent
    state = replace(state, publication_intent={**previous, "status": "RECONCILED", "pull_request": candidate.number})
    store.save(state, expected_publication_intent=previous)
    return state, candidate.number
