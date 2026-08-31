"""Compact, content-free worktree provenance for same-run recovery."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import os

from .storage import record_artifact, verify_artifact_integrity, open_storage


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(("git", *args), cwd=root, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _snapshot(
    root: Path, *, run_id: str, phase: str, transaction_baseline_sha: str | None = None,
) -> dict[str, object] | None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "HEAD")
    git_dir = _git(root, "rev-parse", "--git-dir")
    repository = _git(root, "config", "--get", "remote.origin.url")
    if None in {status, branch, head, git_dir}:
        return None
    # Engineering's own durable receipts are operational metadata, not source
    # changes owned by the provider turn.  They may be untracked in isolated
    # qualification repositories, so exclude them from the source snapshot.
    source_status = [
        line.rstrip()
        for line in str(status).splitlines()
        if ".engineering/" not in line
    ]
    normalized = "\n".join(sorted(source_status))
    counts = {"modified": 0, "added": 0, "deleted": 0}
    for line in normalized.splitlines():
        code = line[:2]
        if "D" in code:
            counts["deleted"] += 1
        elif "A" in code or code == "??":
            counts["added"] += 1
        elif code.strip():
            counts["modified"] += 1
    return {
        "kind": "RECOVERY_WORKTREE_PROVENANCE",
        "run_id": run_id,
        "phase": phase,
        "repository_identity": (repository or "local").strip()[:512],
        "worktree_identity": str((root / str(git_dir).strip()).resolve()),
        "branch": str(branch).strip(), "head_sha": str(head).strip(),
        # The execution checkpoint may already have a verified baseline.  A
        # freshly admitted run uses its current HEAD as the equivalent
        # baseline.  This is identity evidence only, never a delivery diff.
        "transaction_baseline_sha": transaction_baseline_sha or str(head).strip(),
        "status": counts,
        "status_digest": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _write(root: Path, artifact_id: str, payload: dict[str, object]) -> bool:
    directory = root / ".engineering" / "artifacts" / "worktree-provenance"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(artifact_id.encode()).hexdigest()}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".worktree-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    record_artifact(root, path, artifact_id=artifact_id, artifact_type="RECOVERY_WORKTREE_PROVENANCE",
                    content_type="application/json", created_at=str(payload["captured_at"]), run_id=str(payload["run_id"]))
    return True


def capture(
    root: Path, *, run_id: str, phase: str, stage: str,
    transaction_baseline_sha: str | None = None,
) -> bool:
    """Persist only the first bounded receipt for a stage."""
    artifact_id = f"recovery-worktree:{run_id}:{stage}"
    if _load(root, artifact_id) is not None:
        return True
    payload = _snapshot(
        root, run_id=run_id, phase=phase,
        transaction_baseline_sha=transaction_baseline_sha,
    )
    if payload is None:
        return False
    return _write(root, artifact_id, payload)


def _load(root: Path, artifact_id: str) -> dict[str, object] | None:
    if not verify_artifact_integrity(root, artifact_id):
        return None
    connection = open_storage(root)
    try:
        row = connection.execute("SELECT storage_location FROM execution_artifact_records WHERE artifact_id=?", (artifact_id,)).fetchone()
    finally:
        connection.close()
    try:
        payload = json.loads((root / ".engineering" / str(row[0])).read_text(encoding="utf-8")) if row else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def verify_recovery(
    root: Path, *, run_id: str, branch: str | None,
    transaction_baseline_sha: str | None = None,
) -> bool | None:
    """Return True/False when evidence exists, or None before capture exists."""
    baseline = _load(root, f"recovery-worktree:{run_id}:baseline")
    progress = _load(root, f"recovery-worktree:{run_id}:interrupted")
    if baseline is None and progress is None:
        return None
    expected = progress or baseline
    current = _snapshot(
        root, run_id=run_id, phase=str(expected.get("phase") or ""),
        transaction_baseline_sha=transaction_baseline_sha,
    )
    if current is None or not isinstance(expected, dict):
        return False
    if branch and current.get("branch") != branch:
        return False
    for key in ("repository_identity", "worktree_identity", "branch"):
        if current.get(key) != expected.get(key):
            return False
    if transaction_baseline_sha and expected.get("transaction_baseline_sha") != transaction_baseline_sha:
        return False
    # If the interruption receipt exists, exact status equivalence is the
    # minimal non-content proof that no external dirty changes were adopted.
    if progress is not None:
        return current.get("head_sha") == expected.get("head_sha") and current.get("status_digest") == expected.get("status_digest")
    return current.get("head_sha") == expected.get("head_sha")
