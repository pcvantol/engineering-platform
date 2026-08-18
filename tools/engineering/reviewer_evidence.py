"""Bounded, run-scoped repository facts for independent reviewer prompts."""

from __future__ import annotations

from dataclasses import dataclass

from .execution_models import RepositoryEvidence


@dataclass(frozen=True)
class ReviewerEvidence:
    """A minimal post-synchronization fact set, valid for one reviewer wave.

    The Execution Host creates this after its repository synchronization while
    holding the run lease.  It contains facts, never command output or
    conclusions.  A caller must create a new instance after any repository,
    validation, PR, merge, finalization, or cleanup boundary.
    """

    run_id: str
    repository: str
    execution_mode: str
    branch: str
    head_sha: str
    worktree: str
    main_contains_head: bool
    freshness_boundary: str = "post_synchronization_pre_reviewer_wave"

    @classmethod
    def from_repository(
        cls, run_id: str, execution_mode: str, evidence: RepositoryEvidence
    ) -> "ReviewerEvidence":
        return cls(
            run_id=run_id,
            repository=evidence.repository,
            execution_mode=execution_mode,
            branch=evidence.branch,
            head_sha=evidence.head_sha,
            worktree="clean" if evidence.clean else "dirty",
            main_contains_head=evidence.main_contains_head,
        )

    def to_dict(self) -> dict[str, object]:
        # Keep the wire projection deliberately explicit about freshness.  The
        # data class stays flat so callers cannot accidentally substitute a
        # mutable observation for a run-stable identity field.
        return {
            "run_id": self.run_id,
            "run_stable": {
                "repository": self.repository,
                "execution_mode": self.execution_mode,
            },
            "mutable": {
                "branch": self.branch,
                "head_sha": self.head_sha,
                "worktree": self.worktree,
                "main_contains_head": self.main_contains_head,
            },
            "boundary_sensitive": {
                "freshness_boundary": self.freshness_boundary,
                "invalidated_by": (
                    "repository_mutation",
                    "validation",
                    "pull_request_mutation",
                    "merge",
                    "finalization",
                    "repository_cleanup",
                ),
            },
        }
