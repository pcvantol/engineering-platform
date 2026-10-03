"""One PA-E2 lifecycle per OS process, isolating historical runner environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .parity_lifecycle_dispatcher import ParityLifecycleDispatcher, ParityLifecycleDispatchError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", type=Path)
    parser.add_argument("submission_id")
    args = parser.parse_args()
    os.environ["EP_PA_E2_ISOLATED_PROCESS"] = str(os.getpid())
    try:
        receipt = ParityLifecycleDispatcher(args.data_root).dispatch(args.submission_id)
    except ParityLifecycleDispatchError as error:
        print(json.dumps({"error": str(error)}))
        return 0
    print(json.dumps({"run_id": receipt.run_id, "state": receipt.state}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
