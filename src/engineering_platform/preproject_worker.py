"""Fixed, bounded native-readonly worker. Never imports the inspected repository."""
from __future__ import annotations

import json
import sys

from .preproject_inspection import InspectionError, scan, validate_selection


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise InspectionError("INSPECTION_JOB_LIMIT")
        job = json.loads(raw)
        paths, content = validate_selection(job["paths"], job["content_paths"])
        result = scan(job["binding"], paths, content)
        print(json.dumps({"result": result}, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        return 0
    except InspectionError as error:
        print(json.dumps({"error": error.code, "status": error.status}))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        # No exception strings, private paths, file contents or target messages.
        print(json.dumps({"error": "INSPECTION_SOURCE_UNAVAILABLE_OR_UNSUPPORTED", "status": 503}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
