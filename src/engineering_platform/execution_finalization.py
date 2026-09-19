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
        post_cleanup_validation: Callable[[TransactionState], TransactionState] | None = None,
    ) -> TransactionState:
        cleanup = replace(state, phase="REPOSITORY_CLEANUP", next_action="fetch_prune_and_remove_transaction_branches")
        store.save(cleanup)
        write_live_status(root, cleanup, "Repository cleanup in progress")
        operation = getattr(repository, "cleanup_transaction", None)
        if not callable(operation):
            return save_terminal(cleanup, "BLOCKED", "cleanup_unavailable", "Cleanup client is unavailable; resume with repository cleanup evidence.")
        try:
            branches = (cleanup.implementation_branch, cleanup.finalization_branch)
            if cleanup.transaction_kind == "RECONCILIATION":
                branches += (cleanup.branch,)
            result = operation(root, branches)
        except RunnerError as error:
            return save_terminal(cleanup, "BLOCKED", "repository_cleanup_required", str(error))
        reconciled = replace(
                cleanup,
                latest_repository_evidence=redact_diagnostic(result),
                terminal_condition=(
                    "local_commit_reconciled"
                    if cleanup.execution_mode == "GENESIS"
                    else "repository_reconciled"
                ),
            )
        if post_cleanup_validation is not None:
            reconciled = post_cleanup_validation(reconciled)
            if reconciled.terminal:
                return reconciled
        return save_terminal(
            reconciled,
            "COMPLETE",
            "repository_cleanup_reconciled",
            None,
        )
