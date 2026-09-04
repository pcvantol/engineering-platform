"""Thin file ingress for canonical CENTRAL submissions.

This module owns only physical delivery acknowledgement.  It does not open a
database, select work, or retain any run/lifecycle state.  An explicit project
id is required in every file; a checkout name and current directory are never
consulted for authority.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_FILE_BYTES = 131072
MAX_REASON_LENGTH = 160
HEARTBEAT_FILENAME = "file-inbox-heartbeat.json"


class FileInboxError(ValueError):
    """A bounded, terminal file-transport rejection."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _layout(root: Path) -> dict[str, Path]:
    folders = {name: root / name for name in ("incoming", "processing", "accepted", "quarantine")}
    for folder in folders.values():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folders


def _reason(value: object) -> str:
    rendered = str(value).replace("\n", " ").replace("\r", " ")
    return rendered[:MAX_REASON_LENGTH] or "MALFORMED_FILE"


def _read_envelope(path: Path) -> tuple[dict[str, object], bytes, str]:
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_FILE_BYTES or b"\0" in raw:
        raise FileInboxError("MALFORMED_FILE")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FileInboxError("MALFORMED_FILE") from error
    if not isinstance(envelope, dict) or set(envelope) != {"project_id", "submission"}:
        raise FileInboxError("MALFORMED_FILE")
    if not isinstance(envelope["project_id"], str) or not envelope["project_id"]:
        raise FileInboxError("MALFORMED_FILE")
    if not isinstance(envelope["submission"], dict):
        raise FileInboxError("MALFORMED_FILE")
    digest = hashlib.sha256(raw).hexdigest()
    return envelope, raw, digest


def _receipt_path(folder: Path, digest: str) -> Path:
    return folder / f"{digest}.receipt.json"


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _move(source: Path, destination: Path) -> None:
    if destination.exists():
        source.unlink()
    else:
        os.replace(source, destination)


def _submit(server: str, credential: str, envelope: dict[str, object], *, receipt_id: str, received_at: str) -> dict[str, object]:
    project_id = str(envelope["project_id"])
    submission = dict(envelope["submission"])
    # This physical-file digest is the delivery key.  It survives a restart
    # between CENTRAL acceptance and archive acknowledgement.
    submission["idempotency_key"] = receipt_id
    submission["transport_receipt_id"] = receipt_id
    submission["transport_received_at"] = received_at
    request = Request(
        server.rstrip("/") + f"/v1/projects/{project_id}/submissions",
        data=json.dumps(submission, sort_keys=True).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}", "EP-Submission-Transport": "FILE_INBOX"},
    )
    try:
        with urlopen(request, timeout=15) as response:  # nosec B310 -- configured CENTRAL endpoint
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code >= 500:
            raise URLError("CENTRAL_UNAVAILABLE") from error
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", "CENTRAL_REJECTED")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = "CENTRAL_REJECTED"
        raise FileInboxError(_reason(detail)) from error
    except (URLError, TimeoutError) as error:
        raise URLError("CENTRAL_UNAVAILABLE") from error
    if not isinstance(result, dict) or not isinstance(result.get("submission_id"), str):
        raise URLError("CENTRAL_UNAVAILABLE")
    return result


def process_once(root: Path, *, server: str, credential: str) -> dict[str, int]:
    """Deliver every pending file once; unavailable CENTRAL leaves it retryable."""
    folders = _layout(root)
    counts = {"accepted": 0, "quarantined": 0, "retryable": 0}
    for source in sorted(folders["incoming"].glob("*.json")):
        claimed = folders["processing"] / source.name
        _move(source, claimed)
    for claimed in sorted(folders["processing"].glob("*.json")):
        try:
            envelope, _raw, digest = _read_envelope(claimed)
            receipt_id, received_at = f"file:{digest}", _utcnow()
            receipt = _submit(server, credential, envelope, receipt_id=receipt_id, received_at=received_at)
            _write_receipt(_receipt_path(folders["accepted"], digest), {
                "transport": "FILE_INBOX", "receipt_id": receipt_id, "received_at": received_at,
                "submission_id": receipt["submission_id"], "duplicate": bool(receipt.get("duplicate")),
            })
            _move(claimed, folders["accepted"] / f"{digest}.json")
            counts["accepted"] += 1
        except FileInboxError as error:
            # A terminal parser/admission error is transport acknowledgement,
            # not a CENTRAL lifecycle mutation.
            reason = _reason(error)
            digest = hashlib.sha256(claimed.name.encode("utf-8")).hexdigest()
            _write_receipt(_receipt_path(folders["quarantine"], digest), {"transport": "FILE_INBOX", "reason": reason})
            _move(claimed, folders["quarantine"] / f"{digest}.json")
            counts["quarantined"] += 1
        except URLError:
            counts["retryable"] += 1
    return counts


def heartbeat_path(root: Path) -> Path:
    """Return the installation-owned liveness record for this adapter.

    This is deliberately a small, secret-free transport observation.  It is
    neither a queue nor a lifecycle store; CENTRAL remains the sole authority
    for accepted submissions and dispatch state.
    """
    return root / HEARTBEAT_FILENAME


def read_heartbeat(root: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(heartbeat_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class FileInboxService:
    """Installed Server-composed File Inbox adapter with a bounded heartbeat."""

    def __init__(self, root: Path, *, server: str, credential: str | None, interval_seconds: float = 2.0) -> None:
        self.root, self.server, self.credential = root, server, credential
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counts = {"accepted": 0, "quarantined": 0, "retryable": 0}
        self._recent_error: str | None = None

    def _write_heartbeat(self) -> None:
        folders = _layout(self.root)
        payload = {
            "state": "RUNNING",
            "updated_at": _utcnow(),
            "watched_location": str(self.root),
            "delivery_retry": "PENDING" if self._counts["retryable"] else "NONE",
            "quarantine_count": len(list(folders["quarantine"].glob("*.json"))),
            "recent_error": self._recent_error,
        }
        _write_receipt(heartbeat_path(self.root), payload)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.credential:
                    self._counts = process_once(self.root, server=self.server, credential=self.credential)
                    self._recent_error = None
                self._write_heartbeat()
            except (OSError, ValueError, URLError) as error:
                self._recent_error = _reason(error)
                self._write_heartbeat()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        _layout(self.root)
        self._thread = threading.Thread(target=self._run, name="engineering-platform-file-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-platform-file-inbox")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--server", required=True)
    parser.add_argument("--credential-env", default="EP_CONSUMER_TOKEN")
    args = parser.parse_args(argv)
    credential = os.environ.get(args.credential_env)
    if not credential:
        parser.error(f"{args.credential_env} is required")
    print(json.dumps(process_once(args.root, server=args.server, credential=credential), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
