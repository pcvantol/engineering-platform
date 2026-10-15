"""Fixed, bounded native-readonly worker. Never imports the inspected repository."""
from __future__ import annotations

import json
import sys
import hashlib
import errno
from pathlib import Path
from importlib.metadata import distribution

from .preproject_inspection import InspectionError, scan, validate_selection


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise InspectionError("INSPECTION_JOB_LIMIT")
        job = json.loads(raw)
        paths, content = validate_selection(job["paths"], job["content_paths"])
        result = scan(job["binding"], paths, content, isolated=True)
        metadata = distribution("engineering-platform")
        direct_url = json.loads(metadata.read_text("direct_url.json") or "{}")
        if direct_url.get("dir_info", {}).get("editable") or not Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise InspectionError("INSPECTION_WORKER_NOT_INSTALLED", 422)
        runtime = {"executable": sys.executable, "prefix": sys.prefix, "module": str(Path(__file__).resolve()),
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "version": metadata.version}
        print(json.dumps({"result": result, "runtime": runtime}, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        return 0
    except InspectionError as error:
        print(json.dumps({"error": error.code, "status": error.status}))
    except FileNotFoundError:
        print(json.dumps({"error": "INSPECTION_SOURCE_DRIFT", "status": 409}))
    except OSError as error:
        # No exception strings, private paths, file contents or target messages.
        unsupported = error.errno in {errno.ELOOP, errno.ENOTDIR}
        print(json.dumps({"error": "INSPECTION_SOURCE_UNSUPPORTED" if unsupported else "INSPECTION_SOURCE_UNAVAILABLE",
            "status": 422 if unsupported else 503}))
    except UnicodeError:
        print(json.dumps({"error": "INSPECTION_SOURCE_FORMAT_UNSUPPORTED", "status": 422}))
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"error": "INSPECTION_WORKER_JOB_INVALID", "status": 500}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
