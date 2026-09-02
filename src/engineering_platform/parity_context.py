"""Scoped CENTRAL compatibility boundary for Phase-P parity consumers.

This module only adapts durable CENTRAL state to the input shapes used by the
preserved lifecycle and Console.  It neither schedules nor executes a run.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Literal

from .execution_context import execution_mode_for
from .local_repository_binding import resolve_execution_repository


class ParityContextError(ValueError):
    """A stable, fail-closed context construction error."""


ExecutionMode = Literal["MANAGED", "GENESIS"]


@dataclass(frozen=True)
class ParityProjectContext:
    installation_id: str
    data_root: Path
    project_id: str
    repository_id: str
    authority_repository_id: str
    local_repository_root: Path | None


@dataclass(frozen=True)
class HistoricalCandidate:
    """Direct, transport-neutral equivalent of the watcher input envelope."""

    context: ParityProjectContext
    submission_id: str
    prompt: str
    prompt_digest: str
    producer_id: str
    producer_type: str
    producer_version: str | None
    transport: str
    correlation_id: str | None
    mission_id: str | None
    engineering_action_id: str | None
    constraints: dict[str, object]
    execution_mode: ExecutionMode

    def producer_envelope(self) -> str:
        """Return the existing validated Producer Submission Envelope shape."""
        producer: dict[str, object] = {"id": self.producer_id, "type": self.producer_type}
        for field, attribute in (("version", "producer_version"), ("correlation_id", "correlation_id"),
                                 ("mission_id", "mission_id"), ("engineering_action_id", "engineering_action_id")):
            value = getattr(self, attribute)
            if value is not None:
                producer[field] = value
        return json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": self.submission_id},
            "producer": producer,
            "prompt": {"text": self.prompt},
        }, sort_keys=True)


class ParityProjectStore:
    """Explicit project-bound CENTRAL reader for future P-A/P-B composition."""

    def __init__(self, connection: sqlite3.Connection, context: ParityProjectContext) -> None:
        self.connection, self.context = connection, context

    def queued_submissions(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT submission_id,repository_id,state,admission,created_at,prompt_digest "
            "FROM ep_submissions WHERE project_id=? ORDER BY created_at,submission_id",
            (self.context.project_id,),
        ).fetchall()
        return [{"submission_id": str(row[0]), "repository_id": str(row[1]), "state": str(row[2]),
                 "admission": str(row[3]), "created_at": str(row[4]), "prompt_digest": str(row[5])}
                for row in rows]

    def dashboard_projection(self) -> dict[str, object]:
        """A deliberately small project-scoped P-B data boundary, not a UI."""
        runs = self.connection.execute(
            "SELECT run_id,state,created_at,updated_at FROM ep_execution_runs WHERE project_id=? ORDER BY created_at DESC",
            (self.context.project_id,),
        ).fetchall()
        return {
            "project_id": self.context.project_id,
            "repository_id": self.context.repository_id,
            "queue": self.queued_submissions(),
            "runs": [{"run_id": str(row[0]), "state": str(row[1]), "created_at": str(row[2]), "updated_at": str(row[3])} for row in runs],
            "local_execution_available": self.context.local_repository_root is not None,
        }

    def validate_action_scope(self, *, repository_id: str, run_id: str | None = None) -> None:
        if repository_id != self.context.repository_id:
            raise ParityContextError("PROJECT_REPOSITORY_MISMATCH")
        if run_id is not None and self.connection.execute(
            "SELECT 1 FROM ep_execution_runs WHERE run_id=? AND project_id=?", (run_id, self.context.project_id)
        ).fetchone() is None:
            raise ParityContextError("RUN_OUTSIDE_PROJECT_SCOPE")


def _context_rows(connection: sqlite3.Connection, project_id: str, repository_id: str) -> tuple[str, str]:
    installation = connection.execute("SELECT instance_id FROM ep_installations").fetchone()
    project = connection.execute("SELECT status,attachment_contract FROM ep_project_registrations WHERE project_id=?", (project_id,)).fetchone()
    repository = connection.execute(
        "SELECT project_id,authority_repository_id FROM ep_repository_registrations WHERE repository_id=?", (repository_id,)
    ).fetchone()
    if installation is None or project is None or project[0] != "ACTIVE":
        raise ParityContextError("UNKNOWN_PROJECT")
    if repository is None:
        raise ParityContextError("UNKNOWN_REPOSITORY")
    if str(repository[0]) != project_id:
        raise ParityContextError("PROJECT_REPOSITORY_MISMATCH")
    authority = str(repository[1])
    if connection.execute(
        "SELECT 1 FROM ep_repository_registrations WHERE repository_id=? AND project_id=? AND role='authority'",
        (authority, project_id),
    ).fetchone() is None:
        raise ParityContextError("AUTHORITY_REPOSITORY_INVALID")
    return str(installation[0]), authority


def project_context(
    connection: sqlite3.Connection, *, data_root: Path, project_id: str, repository_id: str,
    require_local_root: bool = True,
) -> ParityProjectContext:
    """The sole CENTRAL-to-parity construction path; no ambient project state."""
    installation_id, authority = _context_rows(connection, project_id, repository_id)
    binding = resolve_execution_repository(
        connection, project_id=project_id, repository_id=repository_id, data_root=data_root
    ) if require_local_root else None
    return ParityProjectContext(
        installation_id, data_root.resolve(), project_id, repository_id, authority,
        binding.local_root if binding is not None else None,
    )


def historical_candidate(
    connection: sqlite3.Connection, *, context: ParityProjectContext, submission_id: str,
) -> HistoricalCandidate:
    """Adapt one canonical submission without allocating a run or dispatching it."""
    row = connection.execute(
        "SELECT project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,"
        "constraints,correlation_id,mission_id,engineering_action_id,state,admission "
        "FROM ep_submissions WHERE submission_id=?", (submission_id,),
    ).fetchone()
    if row is None:
        raise ParityContextError("UNKNOWN_SUBMISSION")
    if str(row[0]) != context.project_id or str(row[1]) != context.repository_id:
        raise ParityContextError("SUBMISSION_OUTSIDE_CONTEXT")
    if tuple(map(str, row[12:14])) != ("QUEUED", "ADMITTED"):
        raise ParityContextError("SUBMISSION_NOT_DISPATCHABLE")
    try:
        constraints = json.loads(str(row[8]))
    except json.JSONDecodeError as error:
        raise ParityContextError("SUBMISSION_CONSTRAINTS_INVALID") from error
    if not isinstance(constraints, dict):
        raise ParityContextError("SUBMISSION_CONSTRAINTS_INVALID")
    mode = execution_mode_for(str(row[6]))
    return HistoricalCandidate(
        context, submission_id, str(row[6]), str(row[7]), str(row[2]), str(row[3]),
        str(row[4]) if row[4] is not None else None, str(row[5]),
        str(row[9]) if row[9] is not None else None, str(row[10]) if row[10] is not None else None,
        str(row[11]) if row[11] is not None else None, constraints, mode,
    )
