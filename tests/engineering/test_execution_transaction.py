from __future__ import annotations

from pathlib import Path
import unittest

from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_lease import Lease
from engineering_platform.execution_transaction import ExecutionTransaction


class ExecutionTransactionTest(unittest.TestCase):
    def test_lease_must_belong_to_transaction_run(self) -> None:
        transaction = ExecutionTransaction(TransactionState("run-a", "repo", "prompt", "INITIALIZE"), Path("/repo"))
        foreign = Lease("lease", "run-b", "host", "instance", "a", "b", "c", "ACTIVE")
        with self.assertRaises(ValueError):
            transaction.with_lease(foreign)
