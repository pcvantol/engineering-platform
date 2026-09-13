"""Immutable, path-free Execution Host evidence for terminal artifacts.

The Execution Host owns these observations.  They are sampled at the actual
run boundary and stored in CENTRAL before a terminal artifact is published;
neither a current checkout nor a Forge record is consulted when the artifact
is later read.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from . import central_database
from .providers import GitProvider
from .storage import sqlite_connection


HOST_EXECUTION_EVIDENCE_CONTRACT_VERSION = "1.0"
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = GitProvider().execute(root, "git", *arguments)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _inventory(root: Path) -> tuple[int, str] | None:
    raw = _git(root, "ls-files", "-z")
    if raw is None:
        return None
    # Git paths cannot contain NUL.  Sorting makes the retained digest
    # independent of the index enumeration order without retaining a path.
    paths = sorted(path for path in raw.split("\0") if path)
    return len(paths), _canonical_digest(paths)


def _start_snapshot(root: Path) -> dict[str, object]:
    branch = _git(root, "branch", "--show-current")
    commit = _git(root, "rev-parse", "HEAD")
    inventory = _inventory(root)
    if commit is None or inventory is None:
        return {"status": "UNAVAILABLE"}
    commit = commit.strip()
    if _SHA.fullmatch(commit) is None:
        return {"status": "UNAVAILABLE"}
    target_branch = None if branch is None or not branch.strip() else branch.strip()
    tracked_file_count, inventory_digest = inventory
    return {
        "status": "AVAILABLE",
        "target_branch": target_branch,
        "target_commit": commit,
        "checkout_identity_digest": _canonical_digest({
            "target_branch": target_branch,
            "target_commit": commit,
            "inventory_digest": inventory_digest,
        }),
        "tracked_file_count": tracked_file_count,
        "inventory_digest": inventory_digest,
    }


def _activity(connection: sqlite3.Connection, run_id: str) -> dict[str, int] | None:
    try:
        provider = connection.execute(
            "SELECT COUNT(*) FROM provider_invocations WHERE run_id=? AND provider='codex_cli'", (run_id,),
        ).fetchone()
        validation = connection.execute(
            "SELECT COUNT(*) FROM execution_validation_command_invocations WHERE run_id=?", (run_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not provider or not validation or not isinstance(provider[0], int) or not isinstance(validation[0], int):
        return None
    return {"provider_invocations": provider[0], "host_validation_actions": validation[0]}


def _terminal_snapshot(
    root: Path, *, modified: int, created: int, deleted: int, renamed: int,
    worktree_state: str,
    connection: sqlite3.Connection, run_id: str,
) -> dict[str, object]:
    inventory = _inventory(root)
    activity = _activity(connection, run_id)
    if inventory is None or activity is None or not isinstance(worktree_state, str) or not worktree_state:
        return {
            "status": "UNAVAILABLE", "tracked_file_count": None, "inventory_digest": None,
            "worktree_state": None,
            "diff": {"modified": None, "created": None, "deleted": None, "renamed": None},
            "activity": {"provider_invocations": None, "host_validation_actions": None},
        }
    tracked_file_count, inventory_digest = inventory
    return {
        "status": "AVAILABLE", "tracked_file_count": tracked_file_count,
        "inventory_digest": inventory_digest, "worktree_state": worktree_state.upper(),
        "diff": {"modified": modified, "created": created, "deleted": deleted, "renamed": renamed},
        "activity": activity,
    }


def record_start(data_root: Path, *, run_id: str, repository_root: Path, captured_at: str) -> None:
    """Persist the one pre-provider host snapshot for a CENTRAL run."""
    document = _start_snapshot(repository_root)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO ep_execution_host_evidence(run_id,start_document,captured_at) VALUES(?,?,?)",
            (run_id, encoded, captured_at),
        )


def record_terminal(
    data_root: Path, *, run_id: str, repository_root: Path, worktree_state: str,
    modified: int, created: int, deleted: int, renamed: int, captured_at: str,
) -> None:
    """Attach the one terminal host snapshot; the SQL trigger freezes it."""
    values = (modified, created, deleted, renamed)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise ValueError("Execution Host terminal counters are invalid")
    with sqlite_connection(central_database.path(data_root)) as connection:
        document = _terminal_snapshot(
            repository_root, modified=modified, created=created, deleted=deleted, renamed=renamed,
            worktree_state=worktree_state, connection=connection, run_id=run_id,
        )
        connection.execute(
            "UPDATE ep_execution_host_evidence SET terminal_document=?,terminal_captured_at=? "
            "WHERE run_id=? AND terminal_document IS NULL",
            (json.dumps(document, sort_keys=True, separators=(",", ":")), captured_at, run_id),
        )


def terminal_projection(connection: sqlite3.Connection, *, run_id: str) -> dict[str, object]:
    """Return persisted host evidence only; historical gaps remain explicit."""
    row = connection.execute(
        "SELECT start_document,terminal_document FROM ep_execution_host_evidence WHERE run_id=?", (run_id,),
    ).fetchone()
    start: object = {"status": "NOT_RECORDED"}
    terminal: object = {"status": "NOT_RECORDED"}
    if row is not None:
        try:
            decoded_start = json.loads(str(row[0]))
            if isinstance(decoded_start, dict):
                start = decoded_start
            if row[1] is not None:
                decoded_terminal = json.loads(str(row[1]))
                if isinstance(decoded_terminal, dict):
                    terminal = decoded_terminal
        except (TypeError, ValueError, json.JSONDecodeError):
            # A corrupt record must prevent a terminal artifact from silently
            # asserting host facts.  The caller turns this into a stable 500.
            raise ValueError("Execution Host evidence is corrupt") from None
    return {
        "contract_version": HOST_EXECUTION_EVIDENCE_CONTRACT_VERSION,
        "start": start,
        "terminal": terminal,
    }
