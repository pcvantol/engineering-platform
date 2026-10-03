"""A separate dispatcher process for deterministic PA-E3 restart boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from engineering_platform import parallel_action_recovery
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher


def main() -> int:
    data_root, submission_id, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    os.environ["EP_PA_E2_ISOLATED_PROCESS"] = str(os.getpid())
    dispatcher = ParityLifecycleDispatcher(data_root)
    try:
        context, _candidate, run_id, _prompt, duplicate = dispatcher._claim(submission_id)
        attempt = parallel_action_recovery.acquire(
            data_root, run_id=run_id, submission_id=submission_id,
            project_id=context.project_id, repository_id=context.repository_id,
        )
        if boundary == "entered":
            parallel_action_recovery.runner_entry(
                data_root, run_id=run_id, attempt_id=attempt,
            )
        elif boundary == "returned":
            parallel_action_recovery.runner_entry(
                data_root, run_id=run_id, attempt_id=attempt,
            )
            parallel_action_recovery.complete_attempt(
                data_root, run_id=run_id, attempt_id=attempt,
                checkpoint_phase="WAIT_FOR_OPERATOR_MERGE",
            )
        print(json.dumps({"run_id": run_id, "attempt_id": attempt,
                          "duplicate": duplicate, "boundary": boundary}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
