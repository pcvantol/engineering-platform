"""Typed, read-only terminal evidence shared by reporting and lifecycle coordination."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalEvidenceBundle:
    target_workspace: str
    target_repository: str
    target_branch: str
    target_commit: str
    worktree_state: str
    changed_files: tuple[str, ...]
    files_added: tuple[str, ...]
    files_modified: tuple[str, ...]
    files_removed: tuple[str, ...]
    diff_check: str
    transaction_baseline: str
    resulting_commit: str | None
    lease: dict[str, object]
    readiness: dict[str, object] | None
