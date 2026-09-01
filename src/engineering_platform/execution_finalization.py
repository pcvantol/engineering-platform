"""Bounded post-execution cleanup coordination for the Execution Host."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol

from .agent_state import StateStore, TransactionState, redact_diagnostic
from .execution_errors import RunnerError
from .live_status import write_live_status


class CleanupRepository(Protocol):
    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str: ...


class FinalizationCoordinator:
    """Coordinates cleanup facts; the lifecycle owner supplies terminal transition."""

    def cleanup(
        self,
        *,
        root: Path,
        store: StateStore,
        repository: object,
        state: TransactionState,
        save_terminal: Callable[[TransactionState, str, str, str | None], TransactionState],
    ) -> TransactionState:
        cleanup = replace(state, phase="REPOSITORY_CLEANUP", next_action="fetch_prune_and_remove_transaction_branches")
        store.save(cleanup)
        write_live_status(root, cleanup, "Repository cleanup in progress")
        operation = getattr(repository, "cleanup_transaction", None)
        if not callable(operation):
            return save_terminal(cleanup, "BLOCKED", "cleanup_unavailable", "Cleanup client is unavailable; resume with repository cleanup evidence.")
        try:
            result = operation(root, (cleanup.implementation_branch, cleanup.finalization_branch))
        except RunnerError as error:
            return save_terminal(cleanup, "BLOCKED", "repository_cleanup_required", str(error))
        return save_terminal(
            replace(cleanup, latest_repository_evidence=redact_diagnostic(result)),
            "COMPLETE",
            "repository_cleanup_reconciled",
            None,
        )
