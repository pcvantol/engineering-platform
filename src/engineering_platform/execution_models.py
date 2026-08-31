"""Provider-neutral execution domain models shared across host components."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryEvidence:
    repository: str
    branch: str
    head_sha: str
    clean: bool
    main_contains_head: bool = False


@dataclass(frozen=True)
class PullRequestEvidence:
    number: int
    state: str
    checks_terminal: bool
    checks_passed: bool
    merge_commit: str | None = None
    is_draft: bool = False
    failed_checks: tuple[str, ...] = ()
    head_branch: str | None = None
    base_branch: str | None = None
    merge_state_status: str | None = None


@dataclass(frozen=True)
class AgentResult:
    terminal_state: str
    branch: str | None = None
    pull_request: int | None = None
    terminal_condition: str = "repository_reconciled"
    diagnostic: str | None = None
    repository_path: str | None = None
    commit_sha: str | None = None
    validation_evidence: tuple[dict[str, str], ...] = ()
    quality_evidence: tuple[dict[str, str], ...] = ()
    # A failed validation never becomes a pass. This merely tells the host
    # when a separate validation-environment recovery is warranted.
    validation_disposition: str = "product_failure"
