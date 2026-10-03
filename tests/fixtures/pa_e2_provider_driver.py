"""Controlled provider seam for a two-process PA-E2 lifecycle qualification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from engineering_platform.agent_state import TransactionState
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
from engineering_platform.qualification_runtime import DeterministicQualificationAgent


class ControlledProvider(DeterministicQualificationAgent):
    @staticmethod
    def _github_write_target(root: Path) -> bool:
        # This local overlap fixture must stay offline even if the parent
        # qualification environment has an external-write flow armed.
        return False

    def invoke(self, root: Path, prompt: str):
        rendezvous = Path(os.environ["EP_QUALIFICATION_PA_E2_OVERLAP_DIR"])
        rendezvous.mkdir(parents=True, exist_ok=True)
        (rendezvous / (root.name + ".provider-started")).write_text(
            json.dumps({"pid": os.getpid(), "root": str(root),
                        "wall_ns": time.time_ns(),
                        "monotonic_ns": time.monotonic_ns()}), encoding="utf-8",
        )
        deadline = time.monotonic() + 15
        while not (rendezvous / "release").is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("PA_E2_PROVIDER_OVERLAP_TIMED_OUT")
            time.sleep(.02)
        result = super().invoke(root, prompt)
        (rendezvous / (root.name + ".provider-ended")).write_text(
            json.dumps({"pid": os.getpid(), "root": str(root),
                        "wall_ns": time.time_ns(),
                        "monotonic_ns": time.monotonic_ns()}), encoding="utf-8",
        )
        return result


class ProviderRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.agent = self

    def provider_process_cleanup_confirmed(self) -> bool:
        # This deterministic provider runs synchronously in this process.
        return True

    def run(self, prompt_path: Path, run_id: str | None = None, resume: bool = False,
            owner_authorized: bool = False, transaction_kind: str = "IMPLEMENTATION") -> TransactionState:
        if os.environ.get("ENGINEERING_PLATFORM_ADMITTED_STORAGE_ROOT") != str(self.root):
            raise RuntimeError("PA_E2_STORAGE_CONTEXT_CROSSED")
        ControlledProvider().invoke(self.root, "Synthetic bounded Action.")
        return TransactionState(run_id or "missing", self.root.name, str(prompt_path),
                                "RUNNING", terminal=False)


def main() -> None:
    data_root, submission_id = Path(sys.argv[1]), sys.argv[2]
    os.environ["EP_PA_E2_ISOLATED_PROCESS"] = str(os.getpid())
    dispatcher = ParityLifecycleDispatcher(data_root, runner_factory=ProviderRunner)
    with patch.object(ParityLifecycleDispatcher, "_persist_historical_input", return_value=None):
        receipt = dispatcher.dispatch(submission_id)
    print(json.dumps({"run_id": receipt.run_id, "state": receipt.state}))


if __name__ == "__main__":
    main()
