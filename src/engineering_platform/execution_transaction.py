"""Transaction-scoped Execution Host state; persistence remains in StateStore."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .agent_state import TransactionState
from .execution_lease import Lease


@dataclass(frozen=True)
class ExecutionTransaction:
    state: TransactionState
    target_repository: Path
    lease: Lease | None = None

    @property
    def run_id(self) -> str:
        return self.state.run_id

    @property
    def execution_mode(self) -> str:
        return self.state.execution_mode

    def with_lease(self, lease: Lease) -> "ExecutionTransaction":
        if lease.run_id != self.run_id:
            raise ValueError("lease run identity conflicts with execution transaction")
        return replace(self, lease=lease)
