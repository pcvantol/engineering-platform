"""Durable, provider-free intake for Forge's bounded parallel Action graph.

This is an EP-owned pre-queue boundary. A compatible graph is never a worker
grant: a canonical submission may be created only after this module reports
dependency eligibility and the later resource/capacity gates run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import json
import re
import sqlite3
import subprocess
from typing import Any

from .local_repository_binding import (
    LocalRepositoryBindingError, _next_updated_at, resolve_execution_repository,
)
from .parallel_action_compat import (
    CompatibilityError, CompatibilityScope, assess_parallel_action_graph,
)
from .submission_service import (
    SubmissionError, SubmissionRequest, _accepted_request_digest,
    _forge_provenance, producer_readback,
)


READBACK_VERSION = "ep-parallel-action-admission/v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_POLICY_BYTES = (
    b'{"concurrency_profile":"DIFFERENT_REPOSITORIES_V1",'
    b'"execution":"SERIAL_UNTIL_PA_E2",'
    b'"write_scope":"repository-only"}'
)
SUPPORTED_POLICY_DIGEST = "sha256:" + sha256(_POLICY_BYTES).hexdigest()
_PA_E2_POLICY_BYTES = (
    b'{"concurrency_profile":"DIFFERENT_REPOSITORIES_V1",'
    b'"execution":"BOUNDED_PARALLEL_PA_E2",'
    b'"provider_child_limit":1,'
    b'"write_scope":"repository-only"}'
)
SUPPORTED_PA_E2_POLICY_DIGEST = "sha256:" + sha256(_PA_E2_POLICY_BYTES).hexdigest()


class ParallelAdmissionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ParallelAdmissionError("INVALID_IDENTITY")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _request_digest(request: SubmissionRequest) -> str:
    return _accepted_request_digest(
        repository_id=request.repository_id, producer_id=request.producer_id,
        producer_type=request.producer_type, producer_version=request.producer_version,
        prompt_digest=sha256(request.prompt.encode()).hexdigest(),
        constraints=request.constraints or {}, correlation_id=request.correlation_id,
        mission_id=request.mission_id, engineering_action_id=request.engineering_action_id,
    )


def _action_contract(graph: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    action = next((item for item in graph["actions"]
                   if item["action_id"] == action_id), None)
    if action is None:
        return None
    return {"target": action["target"], "dependencies": action["dependencies"]}


def _staged_graph_claims_action(
    connection: sqlite3.Connection, *, project_id: str, mission_id: str | None,
    action_id: str | None = None, action_ids: frozenset[str] | None = None,
    exclude_producer_id: str | None = None,
) -> bool:
    """Reject a legacy alias for any Action named by a staged graph."""
    wanted = action_ids or (frozenset((action_id,)) if action_id is not None else frozenset())
    if mission_id is None or not wanted:
        return False
    rows = connection.execute(
        """SELECT producer_id,snapshot_digest,snapshot FROM ep_parallel_action_graphs
            WHERE project_id=? AND mission_id=?""",
        (project_id, mission_id),
    ).fetchall()
    for producer_id, digest, snapshot in rows:
        if exclude_producer_id is not None and producer_id == exclude_producer_id:
            continue
        try:
            graph = json.loads(str(snapshot))
            actions = graph["actions"]
            canonical_graph = {
                "contract_version": "parallel-action-graph/v1",
                "mission_id": graph["mission_id"],
                "mission_revision": graph["mission_revision"],
                "actions": [
                    {"action_id": item["action_id"], "target": item["target"],
                     "dependencies": item["dependencies"]}
                    for item in actions
                ],
            }
            if (not isinstance(actions, list) or graph["mission_id"] != mission_id
                    or graph["status"] != "COMPATIBLE"
                    or graph["dispatch_authorized"] is not False
                    or graph["producer_snapshot_digest"] != digest
                    or _canonical(graph) != snapshot
                    or "sha256:" + sha256(_canonical(canonical_graph).encode()).hexdigest()
                       != digest):
                raise ValueError("invalid actions")
            if any(item["action_id"] in wanted for item in actions):
                return True
        except (ValueError, TypeError, KeyError):
            raise ParallelAdmissionError("INVALID_SNAPSHOT") from None
    return False


def _accepted_predecessor(
    connection: sqlite3.Connection, *, graph: dict[str, Any],
    project_id: str, producer_id: str, mission_id: str, action_id: str,
) -> tuple[str, str, str | None, str] | None:
    """Resolve the exact accepted Action across immutable graph revisions."""
    expected = _action_contract(graph, action_id)
    if expected is None:
        return None
    rows = connection.execute(
        """SELECT i.intake_id,g.snapshot,o.repository_revision,i.target_repository_id,
                  g.snapshot_digest,i.baseline_revision
             FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
             JOIN ep_parallel_action_submission_links l ON l.intake_id=i.intake_id
             LEFT JOIN ep_parallel_action_outcomes o ON o.intake_id=i.intake_id
            WHERE i.project_id=? AND i.producer_id=? AND i.mission_id=?
              AND i.action_id=?
            ORDER BY g.mission_revision DESC""",
        (project_id, producer_id, mission_id, action_id),
    ).fetchall()
    matches = []
    for intake_id, snapshot, revision, repository_id, digest, baseline in rows:
        try:
            previous = json.loads(str(snapshot))
            canonical_graph = {
                "contract_version": "parallel-action-graph/v1",
                "mission_id": previous["mission_id"],
                "mission_revision": previous["mission_revision"],
                "actions": [
                    {"action_id": item["action_id"], "target": item["target"],
                     "dependencies": item["dependencies"]}
                    for item in previous["actions"]
                ],
            }
            prior_contract = _action_contract(previous, action_id)
            if (_canonical(previous) == snapshot
                    and previous["producer_snapshot_digest"] == digest
                    and "sha256:" + sha256(_canonical(canonical_graph).encode()).hexdigest() == digest
                    and prior_contract is not None
                    and prior_contract["target"]["repository_id"] == repository_id
                    and prior_contract["target"]["baseline_revision"] == baseline
                    and prior_contract == expected):
                matches.append((str(intake_id), str(snapshot), revision,
                                str(repository_id)))
        except (ValueError, KeyError, TypeError):
            continue
    # Multiple accepted Action identities for one dependency are ambiguous.
    return matches[0] if len({item[0] for item in matches}) == 1 else None


def _immediate_transaction(function: Any) -> Any:
    """Serialize graph activation with canonical Forge submission reads."""
    @wraps(function)
    def guarded(connection: sqlite3.Connection, *args: Any, **kwargs: Any) -> Any:
        if connection.in_transaction:
            return function(connection, *args, **kwargs)
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            return function(connection, *args, **kwargs)
    return guarded


def install_schema(connection: sqlite3.Connection) -> None:
    """Install append-only Action intent and canonical outcome link tables."""
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_repository_grants (
        consumer_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(consumer_id,project_id,repository_id),
        FOREIGN KEY(consumer_id,project_id)
            REFERENCES ep_consumer_registrations(consumer_id,project_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_graphs (
        graph_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        producer_id TEXT NOT NULL,
        mission_id TEXT NOT NULL,
        mission_revision INTEGER NOT NULL,
        snapshot_digest TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE(project_id,producer_id,mission_id,mission_revision))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_intakes (
        intake_id TEXT PRIMARY KEY,
        graph_id TEXT NOT NULL REFERENCES ep_parallel_action_graphs(graph_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        mission_id TEXT NOT NULL,
        producer_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        action_id TEXT NOT NULL,
        action_revision TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        intent_revision TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        write_scope TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        concurrency_profile TEXT NOT NULL,
        target_repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        baseline_revision TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE(project_id,producer_id,idempotency_key),
        UNIQUE(graph_id,action_id),
        UNIQUE(project_id,producer_id,mission_id,action_id,action_revision))""")
    connection.execute("CREATE INDEX IF NOT EXISTS ep_parallel_action_intakes_graph_lookup ON ep_parallel_action_intakes(graph_id,action_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_outcomes (
        intake_id TEXT PRIMARY KEY REFERENCES ep_parallel_action_intakes(intake_id),
        submission_id TEXT NOT NULL UNIQUE REFERENCES ep_submissions(submission_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        terminal_artifact_id TEXT NOT NULL UNIQUE REFERENCES execution_artifact_records(artifact_id),
        terminal_digest TEXT NOT NULL,
        repository_revision TEXT,
        recorded_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_qualified_artifacts (
        intake_id TEXT NOT NULL REFERENCES ep_parallel_action_outcomes(intake_id),
        artifact_id TEXT NOT NULL UNIQUE REFERENCES execution_artifact_records(artifact_id),
        qualification_artifact_id TEXT NOT NULL UNIQUE REFERENCES execution_artifact_records(artifact_id),
        content_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(intake_id,artifact_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_qualification_receipts (
        intake_id TEXT NOT NULL REFERENCES ep_parallel_action_outcomes(intake_id),
        artifact_id TEXT NOT NULL REFERENCES execution_artifact_records(artifact_id),
        qualification_artifact_id TEXT NOT NULL REFERENCES execution_artifact_records(artifact_id),
        content_digest TEXT NOT NULL,
        installed_relative_path TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(intake_id,artifact_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_parallel_action_submission_links (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        intake_id TEXT NOT NULL REFERENCES ep_parallel_action_intakes(intake_id),
        parent_submission_id TEXT UNIQUE REFERENCES ep_submissions(submission_id),
        accepted_request_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
        ep_parallel_action_submission_root_unique
        ON ep_parallel_action_submission_links(intake_id)
        WHERE parent_submission_id IS NULL""")
    for table in ("ep_parallel_action_graphs", "ep_parallel_action_intakes",
                  "ep_parallel_action_outcomes", "ep_parallel_action_qualified_artifacts",
                  "ep_parallel_action_qualification_receipts",
                  "ep_parallel_action_submission_links"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} "
                f"BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is immutable'); END"
            )


def _authoritative_scope(connection: sqlite3.Connection, project_id: str) -> CompatibilityScope:
    instance = connection.execute("SELECT instance_id FROM ep_installations").fetchone()
    project = connection.execute(
        "SELECT status FROM ep_project_registrations WHERE project_id=?", (project_id,)
    ).fetchone()
    repositories = tuple(
        str(row[0]) for row in connection.execute(
            "SELECT repository_id FROM ep_repository_registrations WHERE project_id=? ORDER BY repository_id",
            (project_id,),
        )
    )
    if instance is None or project is None or project[0] != "ACTIVE" or not repositories:
        raise ParallelAdmissionError("SCOPE_UNAVAILABLE")
    try:
        return CompatibilityScope(str(instance[0]), project_id, repositories)
    except CompatibilityError as error:
        raise ParallelAdmissionError("SCOPE_UNAVAILABLE") from error


def _target_grant_current(connection: sqlite3.Connection, *, consumer_id: str,
                          project_id: str, repository_id: str) -> bool:
    row = connection.execute(
        """SELECT 1 FROM ep_parallel_action_repository_grants g
             JOIN ep_consumer_registrations c
               ON c.consumer_id=g.consumer_id AND c.project_id=g.project_id
             JOIN ep_repository_registrations r ON r.repository_id=g.repository_id
            WHERE g.consumer_id=? AND g.project_id=? AND g.repository_id=?
              AND g.status='ACTIVE' AND c.status='ACTIVE' AND r.project_id=g.project_id""",
        (consumer_id, project_id, repository_id),
    ).fetchone()
    return row is not None


def set_repository_grant(connection: sqlite3.Connection, *, data_root: object,
                         consumer_id: str, project_id: str, repository_id: str,
                         reason: str, active: bool) -> dict[str, str]:
    """Change one target grant through the installation-owner CLI boundary."""
    from pathlib import Path
    from .platform_admin import require_installation_owner
    actor = require_installation_owner(Path(data_root))
    for value in (consumer_id, project_id, repository_id):
        _identifier(value)
    if not isinstance(reason, str) or not 0 < len(reason.strip()) <= 512:
        raise ParallelAdmissionError("INVALID_GRANT_REASON")
    if active:
        scope = connection.execute(
            """SELECT 1 FROM ep_consumer_registrations c
                 JOIN ep_project_registrations p ON p.project_id=c.project_id
                 JOIN ep_repository_registrations r ON r.project_id=p.project_id
                WHERE c.consumer_id=? AND c.project_id=? AND c.status='ACTIVE'
                  AND p.status='ACTIVE' AND r.repository_id=?""",
            (consumer_id, project_id, repository_id),
        ).fetchone()
    else:
        scope = connection.execute(
            """SELECT 1 FROM ep_parallel_action_repository_grants
                WHERE consumer_id=? AND project_id=? AND repository_id=?""",
            (consumer_id, project_id, repository_id),
        ).fetchone()
    if scope is None:
        raise ParallelAdmissionError("SCOPE_UNAVAILABLE")
    status = "ACTIVE" if active else "REVOKED"
    previous = connection.execute(
        "SELECT updated_at FROM ep_parallel_action_repository_grants "
        "WHERE consumer_id=? AND project_id=? AND repository_id=?",
        (consumer_id, project_id, repository_id),
    ).fetchone()
    now = _next_updated_at(previous[0] if previous else None)
    connection.execute(
        """INSERT INTO ep_parallel_action_repository_grants
             VALUES(?,?,?,?,?,?,?,?)
             ON CONFLICT(consumer_id,project_id,repository_id)
             DO UPDATE SET status=excluded.status,actor=excluded.actor,
                           reason=excluded.reason,updated_at=excluded.updated_at""",
        (consumer_id, project_id, repository_id, status, actor, reason.strip(), now, now),
    )
    return {"consumer_id": consumer_id, "project_id": project_id,
            "repository_id": repository_id, "status": status}


@_immediate_transaction
def stage_action(
    connection: sqlite3.Connection, graph_bytes: bytes, *, project_id: str,
    consumer_id: str, producer_id: str, action_id: str, action_revision: str,
    intent_id: str, intent_revision: str, correlation_id: str,
    idempotency_key: str, write_scope: str, policy_digest: str,
    concurrency_profile: str = "DIFFERENT_REPOSITORIES_V1",
) -> dict[str, object]:
    """Persist one immutable producer Action and its complete graph snapshot.

    The caller must already have authenticated the consumer. This function
    independently checks its current project registration and every target.
    """
    values = (project_id, consumer_id, producer_id, action_id, action_revision,
              intent_id, intent_revision, correlation_id, idempotency_key, write_scope)
    for value in values:
        _identifier(value)
    if producer_id != consumer_id:
        raise ParallelAdmissionError("PRODUCER_PRINCIPAL_MISMATCH")
    if (policy_digest not in {SUPPORTED_POLICY_DIGEST, SUPPORTED_PA_E2_POLICY_DIGEST}
            or write_scope != "repository-only"):
        raise ParallelAdmissionError("UNSUPPORTED_PARALLEL_POLICY")
    if concurrency_profile != "DIFFERENT_REPOSITORIES_V1":
        raise ParallelAdmissionError("UNSUPPORTED_CONCURRENCY_PROFILE")
    consumer = connection.execute(
        "SELECT status FROM ep_consumer_registrations WHERE consumer_id=? AND project_id=?",
        (consumer_id, project_id),
    ).fetchone()
    if consumer is None or consumer[0] != "ACTIVE":
        raise ParallelAdmissionError("CONSUMER_SCOPE_UNAVAILABLE")
    scope = _authoritative_scope(connection, project_id)
    compatible = assess_parallel_action_graph(graph_bytes, scope=scope)
    if not compatible["compatible"]:
        raise ParallelAdmissionError(str(compatible["error"]["code"]))
    action = next((item for item in compatible["actions"] if item["action_id"] == action_id), None)
    if action is None:
        raise ParallelAdmissionError("ACTION_NOT_IN_GRAPH")
    baseline = str(action["target"]["baseline_revision"])
    if _REVISION.fullmatch(baseline) is None:
        raise ParallelAdmissionError("INVALID_BASELINE")
    graph_id = sha256(_canonical([
        project_id, producer_id, compatible["mission_id"], compatible["mission_revision"],
        compatible["producer_snapshot_digest"],
    ]).encode()).hexdigest()
    intake_identity = [graph_id, action_id, action_revision, intent_id, intent_revision,
                       correlation_id, idempotency_key, write_scope, policy_digest,
                       concurrency_profile, action["target"]["repository_id"], baseline]
    intake_id = sha256(_canonical(intake_identity).encode()).hexdigest()
    prior_key = connection.execute(
        """SELECT i.intake_id,g.snapshot_digest FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
            WHERE i.project_id=? AND i.producer_id=? AND i.idempotency_key=?""",
        (project_id, producer_id, idempotency_key),
    ).fetchone()
    if prior_key is not None:
        if (prior_key[0] != intake_id
                or prior_key[1] != compatible["producer_snapshot_digest"]):
            raise ParallelAdmissionError("IDEMPOTENCY_CONFLICT")
        return {"contract_version": READBACK_VERSION, "intake_id": intake_id,
                "snapshot_digest": compatible["producer_snapshot_digest"],
                "replayed": True, "dispatch_authorized": False}
    if any(not _target_grant_current(
        connection, consumer_id=consumer_id, project_id=project_id,
        repository_id=str(item["target"]["repository_id"]),
    ) for item in compatible["actions"]):
        raise ParallelAdmissionError("SCOPE_UNAVAILABLE")
    if _staged_graph_claims_action(
        connection, project_id=project_id, mission_id=compatible["mission_id"],
        action_ids=frozenset(str(item["action_id"]) for item in compatible["actions"]),
        exclude_producer_id=producer_id,
    ):
        raise ParallelAdmissionError("GRAPH_ALREADY_STAGED")
    if any(connection.execute(
        """SELECT 1 FROM ep_operational_identity_tombstones
            WHERE identity_kind='parallel_action_conflict' AND identity_digest=?""",
        (sha256(f"{project_id}\0{compatible['mission_id']}\0{item['action_id']}".encode()).hexdigest(),),
    ).fetchone() is not None for item in compatible["actions"]):
        raise ParallelAdmissionError("PARALLEL_IDENTITY_RETIRED")
    latest_revision = connection.execute(
        """SELECT MAX(mission_revision) FROM ep_parallel_action_graphs
            WHERE project_id=? AND producer_id=? AND mission_id=?""",
        (project_id, producer_id, compatible["mission_id"]),
    ).fetchone()[0]
    if latest_revision is not None and compatible["mission_revision"] < latest_revision:
        raise ParallelAdmissionError("GRAPH_SUPERSEDED")
    if not _target_grant_current(
        connection, consumer_id=consumer_id, project_id=project_id,
        repository_id=str(action["target"]["repository_id"]),
    ):
        raise ParallelAdmissionError("SCOPE_UNAVAILABLE")
    if connection.execute(
        """SELECT 1 FROM ep_submissions WHERE project_id=? AND producer_id=?
              AND mission_id=? AND engineering_action_id=? LIMIT 1""",
        (project_id, producer_id, compatible["mission_id"], action_id),
    ).fetchone() is not None:
        existing_link = connection.execute(
            """SELECT 1 FROM ep_parallel_action_submission_links l
                 JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
                WHERE i.project_id=? AND i.producer_id=? AND i.action_id=?
                  AND i.graph_id IN (SELECT graph_id FROM ep_parallel_action_graphs
                       WHERE project_id=? AND producer_id=? AND mission_id=?)""",
            (project_id, producer_id, action_id, project_id, producer_id,
             compatible["mission_id"]),
        ).fetchone()
        if existing_link is None:
            raise ParallelAdmissionError("ACTION_ALREADY_SUBMITTED")
    for kind, identity in (
        ("parallel_graph_revision", f"{project_id}\0{producer_id}\0{compatible['mission_id']}\0{compatible['mission_revision']}"),
        ("parallel_intake_idempotency", f"{project_id}\0{producer_id}\0{idempotency_key}"),
        ("parallel_action_revision", f"{project_id}\0{producer_id}\0{compatible['mission_id']}\0{action_id}\0{action_revision}"),
        ("parallel_action_root", f"{project_id}\0{producer_id}\0{compatible['mission_id']}\0{action_id}"),
    ):
        retired = connection.execute(
            "SELECT 1 FROM ep_operational_identity_tombstones "
            "WHERE identity_kind=? AND identity_digest=?",
            (kind, sha256(identity.encode()).hexdigest()),
        ).fetchone()
        if retired is not None:
            raise ParallelAdmissionError("PARALLEL_IDENTITY_RETIRED")
    accepted_actions = connection.execute(
        """SELECT submission_id,engineering_action_id FROM ep_submissions
            WHERE project_id=? AND mission_id=?""",
        (project_id, compatible["mission_id"]),
    ).fetchall()
    graph_action_ids = {item["action_id"] for item in compatible["actions"]}
    for submission_id, accepted_action_id in accepted_actions:
        if accepted_action_id not in graph_action_ids:
            continue
        linked = connection.execute(
            """SELECT g.snapshot FROM ep_parallel_action_submission_links l
                 JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
                 JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
                WHERE l.submission_id=? AND i.project_id=? AND i.producer_id=?
                  AND i.mission_id=? AND i.action_id=?""",
            (submission_id, project_id, producer_id,
             compatible["mission_id"], accepted_action_id),
        ).fetchone()
        if linked is None:
            raise ParallelAdmissionError("GRAPH_ALREADY_DISPATCHED")
        try:
            prior_graph = json.loads(str(linked[0]))
            if (_action_contract(prior_graph, str(accepted_action_id))
                    != _action_contract(compatible, str(accepted_action_id))):
                raise ParallelAdmissionError("ACCEPTED_ACTION_CHANGED")
        except (ValueError, TypeError, KeyError) as error:
            raise ParallelAdmissionError("ACCEPTED_ACTION_CHANGED") from error
    prior_graph = connection.execute(
        "SELECT graph_id,snapshot_digest FROM ep_parallel_action_graphs "
        "WHERE project_id=? AND producer_id=? AND mission_id=? AND mission_revision=?",
        (project_id, producer_id, compatible["mission_id"], compatible["mission_revision"]),
    ).fetchone()
    if prior_graph is not None and (prior_graph[0] != graph_id or prior_graph[1] != compatible["producer_snapshot_digest"]):
        raise ParallelAdmissionError("GRAPH_REVISION_CONFLICT")
    prior_action = connection.execute(
        "SELECT intake_id FROM ep_parallel_action_intakes WHERE graph_id=? AND action_id=?",
        (graph_id, action_id),
    ).fetchone()
    if prior_action is not None and prior_action[0] != intake_id:
        raise ParallelAdmissionError("ACTION_REVISION_CONFLICT")
    prior_identity = connection.execute(
        """SELECT intake_id FROM ep_parallel_action_intakes
            WHERE project_id=? AND producer_id=? AND mission_id=?
              AND action_id=? AND action_revision=?""",
        (project_id, producer_id, compatible["mission_id"], action_id, action_revision),
    ).fetchone()
    if prior_identity is not None and prior_identity[0] != intake_id:
        raise ParallelAdmissionError("ACTION_REVISION_CONFLICT")
    recorded_at = _now()
    if prior_graph is None:
        connection.execute(
            "INSERT INTO ep_parallel_action_graphs VALUES(?,?,?,?,?,?,?,?)",
            (graph_id, project_id, producer_id, compatible["mission_id"],
             compatible["mission_revision"], compatible["producer_snapshot_digest"],
             _canonical(compatible), recorded_at),
        )
    if prior_key is None:
        connection.execute(
            "INSERT INTO ep_parallel_action_intakes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intake_id, graph_id, project_id, compatible["mission_id"], producer_id, idempotency_key,
             action_id, action_revision, intent_id, intent_revision,
             correlation_id, write_scope, policy_digest, concurrency_profile,
             action["target"]["repository_id"], baseline, recorded_at),
        )
    return {"contract_version": READBACK_VERSION, "intake_id": intake_id,
            "snapshot_digest": compatible["producer_snapshot_digest"],
            "replayed": prior_key is not None, "dispatch_authorized": False}


def validate_for_submission(connection: sqlite3.Connection,
                            request: SubmissionRequest, *,
                            check_dependencies: bool = True,
                            principal_id: str | None = None) -> str | None:
    """Gate the canonical queue when a Forge Mission has a staged graph.

    Serial Missions with no staged parallel graph retain their established
    submission path. A staged graph cannot be silently stripped by the legacy
    adapter: exact intake metadata and current predecessor proof are required.
    """
    constraints = request.constraints or {}
    if request.mission_id is None:
        if "parallel_action_intake" in constraints:
            raise ParallelAdmissionError("PARALLEL_GRAPH_REQUIRED")
        return None
    if principal_id is not None and principal_id != request.producer_id:
        principal_graph = connection.execute(
            """SELECT 1 FROM ep_parallel_action_graphs
                WHERE project_id=? AND producer_id=? AND mission_id=? LIMIT 1""",
            (request.project_id, principal_id, request.mission_id),
        ).fetchone()
        if principal_graph is not None:
            raise ParallelAdmissionError("PRODUCER_PRINCIPAL_MISMATCH")
    if _staged_graph_claims_action(
        connection, project_id=request.project_id, mission_id=request.mission_id,
        action_id=request.engineering_action_id,
        exclude_producer_id=request.producer_id,
    ):
        raise ParallelAdmissionError("PARALLEL_GRAPH_REQUIRED")
    graph = connection.execute(
        """SELECT graph_id,mission_revision,producer_id FROM ep_parallel_action_graphs
            WHERE project_id=? AND producer_id=? AND mission_id=?""",
        (request.project_id, request.producer_id, request.mission_id),
    ).fetchall()
    if not graph:
        if "parallel_action_intake" in constraints:
            raise ParallelAdmissionError("PARALLEL_GRAPH_REQUIRED")
        return None
    if request.producer_type != "FORGE":
        raise ParallelAdmissionError("PARALLEL_PRODUCER_TYPE_REQUIRED")
    if principal_id is not None and principal_id != request.producer_id:
        raise ParallelAdmissionError("PRODUCER_PRINCIPAL_MISMATCH")
    if check_dependencies and principal_id is None:
        raise ParallelAdmissionError("PARALLEL_PRINCIPAL_REQUIRED")
    if any(item[2] != request.producer_id for item in graph):
        raise ParallelAdmissionError("PARALLEL_PRODUCER_MISMATCH")
    provenance = constraints.get("forge_execution")
    revision = provenance.get("mission_revision") if isinstance(provenance, dict) else None
    matching = [item for item in graph if str(item[1]) == str(revision)]
    if len(matching) != 1:
        raise ParallelAdmissionError("PARALLEL_GRAPH_REVISION_MISMATCH")
    if check_dependencies and int(matching[0][1]) != max(int(item[1]) for item in graph):
        raise ParallelAdmissionError("GRAPH_SUPERSEDED")
    intake = connection.execute(
        """SELECT intake_id,idempotency_key,action_id,action_revision,intent_id,
                  intent_revision,correlation_id,write_scope,policy_digest,
                  concurrency_profile,target_repository_id,baseline_revision
             FROM ep_parallel_action_intakes WHERE graph_id=? AND action_id=?""",
        (matching[0][0], request.engineering_action_id),
    ).fetchone()
    if intake is None:
        raise ParallelAdmissionError("PARALLEL_INTAKE_REQUIRED")
    if check_dependencies and connection.execute(
        """SELECT 1 FROM ep_parallel_action_submission_links l
             JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
            WHERE i.project_id=? AND i.producer_id=? AND i.mission_id=?
              AND i.action_id=? AND i.intake_id!=?
              AND l.parent_submission_id IS NULL LIMIT 1""",
        (request.project_id, request.producer_id, request.mission_id,
         request.engineering_action_id, intake[0]),
    ).fetchone() is not None:
        raise ParallelAdmissionError("ACTION_ALREADY_SUBMITTED")
    if any(line.strip().lower().startswith("execution mode:")
           and line.split(":", 1)[1].strip().upper() == "GENESIS"
           for line in request.prompt.splitlines()):
        raise ParallelAdmissionError("PARALLEL_EXECUTION_MODE_UNSUPPORTED")
    binding = constraints.get("parallel_action_intake")
    expected = {
        "contract_version": "ep-parallel-action-intake/v1",
        "intake_id": intake[0],
        "snapshot_digest": connection.execute(
            "SELECT snapshot_digest FROM ep_parallel_action_graphs WHERE graph_id=?",
            (matching[0][0],),
        ).fetchone()[0],
        "action_revision": intake[3],
        "write_scope": intake[7],
        "policy_digest": intake[8],
        "concurrency_profile": intake[9],
    }
    implicit_production_binding = False
    if binding is None and set(constraints) == {
        "forge_execution", "repository_revision_binding",
    }:
        try:
            _forge_provenance(request)
        except SubmissionError:
            pass
        else:
            implicit_production_binding = (
                isinstance(provenance, dict)
                and provenance.get("contract_version") == "1.3"
            )
    revision_binding = constraints.get("repository_revision_binding")
    if ((binding != expected and not implicit_production_binding)
            or request.idempotency_key != intake[1]
            or request.engineering_action_id != intake[2]
            or request.repository_id != intake[10]
            or request.correlation_id != intake[6]
            or not isinstance(provenance, dict)
            or provenance.get("intent_id") != intake[4]
            or str(provenance.get("intent_revision")) != str(intake[5])
            or not isinstance(revision_binding, dict)
            or revision_binding.get("requested_revision") != intake[11]
            or revision_binding.get("allowed_baseline_revision") is not None):
        raise ParallelAdmissionError("PARALLEL_INTAKE_MISMATCH")
    if check_dependencies:
        reconcile_predecessors(connection, intake_id=str(intake[0]))
        decision = dependency_readback(connection, intake_id=str(intake[0]))
        if decision["state"] != "DEPENDENCY_ELIGIBLE":
            raise ParallelAdmissionError(str(decision["state"]))
    return str(intake[0])


def _stored_submission_matches_intake(connection: sqlite3.Connection, *,
                                      intake_id: str, submission_id: str,
                                      seen: frozenset[str] = frozenset()) -> bool:
    if submission_id in seen:
        return False
    row = connection.execute(
        """SELECT project_id,repository_id,producer_id,producer_type,
                  producer_version,prompt,transport,idempotency_key,
                  correlation_id,mission_id,engineering_action_id,constraints,
                  transport_receipt_id,transport_received_at,prompt_digest
             FROM ep_submissions WHERE submission_id=?""",
        (submission_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        link = connection.execute(
            """SELECT parent_submission_id,accepted_request_digest
                 FROM ep_parallel_action_submission_links
                WHERE intake_id=? AND submission_id=?""",
            (intake_id, submission_id),
        ).fetchone()
        if link is None or row[14] != sha256(str(row[5]).encode()).hexdigest():
            return False
        constraints = json.loads(str(row[11]))
        if not isinstance(constraints, dict):
            return False
        request = SubmissionRequest(*row[:11], constraints, *row[12:14])
        if _request_digest(request) != link[1]:
            return False
        if link[0] is None:
            return validate_for_submission(
                connection, request, check_dependencies=False,
            ) == intake_id
        parent = str(link[0])
        parent_link = connection.execute(
            "SELECT intake_id FROM ep_parallel_action_submission_links WHERE submission_id=?",
            (parent,),
        ).fetchone()
        resolution = connection.execute(
            """SELECT operator_resolution,resolution_submission_id
                 FROM ep_parity_lifecycle_dispatches WHERE submission_id=?""",
            (parent,),
        ).fetchone()
        return (parent_link is not None and parent_link[0] == intake_id
                and resolution is not None
                and tuple(resolution) == ("RETRIED", submission_id)
                and _stored_submission_matches_intake(
                    connection, intake_id=intake_id, submission_id=parent,
                    seen=seen | {submission_id},
                )
                and _retry_request_matches_parent(connection, request, parent))
    except (ValueError, TypeError, sqlite3.Error):
        return False


def _retry_request_matches_parent(connection: sqlite3.Connection,
                                  request: SubmissionRequest,
                                  parent_submission_id: str) -> bool:
    parent = connection.execute(
        """SELECT s.project_id,s.repository_id,s.producer_id,s.producer_type,
                  s.producer_version,s.prompt,s.transport,s.correlation_id,
                  s.mission_id,s.engineering_action_id,s.constraints,d.run_id
             FROM ep_submissions s JOIN ep_parity_lifecycle_dispatches d
               ON d.submission_id=s.submission_id WHERE s.submission_id=?""",
        (parent_submission_id,),
    ).fetchone()
    if parent is None:
        return False
    try:
        old_constraints = json.loads(str(parent[10]))
        new_constraints = json.loads(_canonical(request.constraints or {}))
        old_binding = old_constraints["repository_revision_binding"]
        new_binding = new_constraints["repository_revision_binding"]
        if (not isinstance(old_binding, dict) or not isinstance(new_binding, dict)
                or old_binding.get("requested_revision") != new_binding.get("requested_revision")
                or not isinstance(new_binding.get("allowed_baseline_revision"), str)
                or _REVISION.fullmatch(new_binding["allowed_baseline_revision"]) is None):
            return False
        old_constraints["repository_revision_binding"] = new_binding
    except (ValueError, TypeError, KeyError):
        return False
    return (
        tuple(parent[:5]) == (request.project_id, request.repository_id,
                              request.producer_id, request.producer_type,
                              request.producer_version)
        and request.prompt == f"Retry-Of: {parent[11]}\n\n{parent[5]}"
        and request.transport == parent[6]
        and (request.correlation_id, request.mission_id,
             request.engineering_action_id) == tuple(parent[7:10])
        and request.idempotency_key is None
        and old_constraints == new_constraints
    )


def _retry_pin_decision(connection: sqlite3.Connection, *, intake_id: str,
                        allowed_baseline: str,
                        decision: dict[str, object]) -> dict[str, object]:
    """Permit a clean local main behind one exact protected-main retry pin."""
    if decision["state"] != "WAITING_BASELINE":
        return decision
    row = connection.execute(
        """SELECT project_id,target_repository_id,baseline_revision
             FROM ep_parallel_action_intakes WHERE intake_id=?""",
        (intake_id,),
    ).fetchone()
    if row is None:
        return decision
    try:
        binding = resolve_execution_repository(
            connection, project_id=str(row[0]), repository_id=str(row[1]),
        )
        root = str(binding.local_root)
        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", root, *arguments], capture_output=True,
                text=True, check=False, timeout=5,
            )
        branch = git("branch", "--show-current")
        clean = git("status", "--porcelain", "--untracked-files=all")
        local = git("rev-parse", "--verify", "HEAD^{commit}")
        protected = git("rev-parse", "--verify", "origin/main^{commit}")
        if (any(result.returncode != 0 for result in (branch, clean, local, protected))
                or branch.stdout.strip() != "main" or clean.stdout.strip()
                or protected.stdout.strip() != allowed_baseline
                or _REVISION.fullmatch(local.stdout.strip()) is None
                or git("merge-base", "--is-ancestor", local.stdout.strip(),
                       allowed_baseline).returncode != 0
                or git("merge-base", "--is-ancestor", str(row[2]),
                       allowed_baseline).returncode != 0):
            return decision
    except (LocalRepositoryBindingError, OSError, subprocess.TimeoutExpired):
        return decision
    blockers = [item for item in decision["blockers"]
                if item != "TARGET_BASELINE_UNVERIFIED"]
    return {**decision, "state": "WAITING_DEPENDENCY" if blockers else "DEPENDENCY_ELIGIBLE",
            "blockers": blockers, "target_baseline_state": "PENDING_HOST_SYNC"}


def validate_operator_retry(connection: sqlite3.Connection,
                            request: SubmissionRequest, *,
                            parent_submission_id: str) -> str:
    """Authorize one operator successor of a linked failed Action run."""
    parent = connection.execute(
        """SELECT l.intake_id,d.state,d.operator_resolution
             FROM ep_parallel_action_submission_links l
             JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=l.submission_id
            WHERE l.submission_id=?""",
        (parent_submission_id,),
    ).fetchone()
    if (parent is None or parent[1] not in ("BLOCKED", "FAILED")
            or parent[2] != "OPEN"
            or not _stored_submission_matches_intake(
                connection, intake_id=str(parent[0]),
                submission_id=parent_submission_id,
            )
            or not _retry_request_matches_parent(connection, request, parent_submission_id)):
        raise ParallelAdmissionError("PARALLEL_RETRY_MISMATCH")
    intake_id = str(parent[0])
    binding = (request.constraints or {}).get("repository_revision_binding")
    baseline = binding["allowed_baseline_revision"]
    reconcile_predecessors(connection, intake_id=intake_id)
    decision = dependency_readback(connection, intake_id=intake_id,
                                   baseline_revision=baseline)
    decision = _retry_pin_decision(
        connection, intake_id=intake_id, allowed_baseline=baseline,
        decision=decision,
    )
    if decision["state"] != "DEPENDENCY_ELIGIBLE":
        raise ParallelAdmissionError(str(decision["state"]))
    return intake_id


def bind_accepted_submission(connection: sqlite3.Connection, *, intake_id: str,
                             submission_id: str, request: SubmissionRequest,
                             parent_submission_id: str | None = None) -> None:
    """Record one immutable canonical queue attempt under the same transaction."""
    existing = connection.execute(
        "SELECT intake_id,parent_submission_id,accepted_request_digest "
        "FROM ep_parallel_action_submission_links WHERE submission_id=?",
        (submission_id,),
    ).fetchone()
    if existing is not None:
        if existing != (intake_id, parent_submission_id, _request_digest(request)):
            raise ParallelAdmissionError("PARALLEL_SUBMISSION_CONFLICT")
        return
    if parent_submission_id is None and connection.execute(
        """SELECT 1 FROM ep_parallel_action_intakes current
             JOIN ep_parallel_action_intakes earlier
               ON earlier.project_id=current.project_id
              AND earlier.producer_id=current.producer_id
              AND earlier.mission_id=current.mission_id
              AND earlier.action_id=current.action_id
             JOIN ep_parallel_action_submission_links l
               ON l.intake_id=earlier.intake_id
            WHERE current.intake_id=? AND earlier.intake_id!=current.intake_id
              AND l.parent_submission_id IS NULL LIMIT 1""",
        (intake_id,),
    ).fetchone() is not None:
        raise ParallelAdmissionError("ACTION_ALREADY_SUBMITTED")
    if parent_submission_id is not None:
        parent = connection.execute(
            "SELECT intake_id FROM ep_parallel_action_submission_links WHERE submission_id=?",
            (parent_submission_id,),
        ).fetchone()
        if parent is None or parent[0] != intake_id:
            raise ParallelAdmissionError("PARALLEL_RETRY_MISMATCH")
    connection.execute(
        "INSERT INTO ep_parallel_action_submission_links VALUES(?,?,?,?,?)",
        (submission_id, intake_id, parent_submission_id, _request_digest(request), _now()),
    )


def _verified_continuation_head(connection: sqlite3.Connection, *,
                                intake_id: str, submission_id: str,
                                run_id: str, selected_baseline: str,
                                allowed_baseline: str | None) -> str | None:
    """Use only a persisted nonterminal checkpoint to follow a moving HEAD."""
    from .agent_state import StateError, TransactionState
    from .execution_errors import RunnerError
    from .execution_repository import SubprocessRepositoryClient
    row = connection.execute(
        """SELECT d.state,d.operator_resolution,t.payload,i.project_id,
                  i.target_repository_id,i.baseline_revision
             FROM ep_parity_lifecycle_dispatches d
             JOIN engineering_transactions t ON t.run_id=d.run_id
             JOIN ep_parallel_action_intakes i ON i.intake_id=?
            WHERE d.submission_id=? AND d.run_id=?""",
        (intake_id, submission_id, run_id),
    ).fetchone()
    if (row is None or row[0] not in ("CLAIMED", "RUNNING", "BLOCKED")
            or row[1] not in ("NONE", "OPEN")):
        return None
    try:
        checkpoint = TransactionState.from_dict(json.loads(str(row[2])))
        if (checkpoint.run_id != run_id
                or checkpoint.requested_repository_revision != row[5]
                or checkpoint.execution_baseline_sha != selected_baseline
                or checkpoint.allowed_baseline_revision != allowed_baseline
                or checkpoint.terminal):
            return None
        known_heads = {
            checkpoint.execution_baseline_sha, checkpoint.last_verified_sha,
            checkpoint.implementation_head_sha, checkpoint.implementation_merge_commit,
            checkpoint.finalization_head_sha, checkpoint.finalization_merge_commit,
            checkpoint.reconciliation_head_sha, checkpoint.reconciliation_merge_commit,
        }
        binding = resolve_execution_repository(
            connection, project_id=str(row[3]), repository_id=str(row[4]),
        )
        origin_identity = SubprocessRepositoryClient().trusted_origin_identity(
            binding.local_root)
        if origin_identity is None or checkpoint.repository != origin_identity:
            return None
        head = subprocess.run(
            ["git", "-C", str(binding.local_root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if head.returncode != 0 or head.stdout.strip() not in known_heads:
            return None
        current_head = head.stdout.strip()
        ancestor = subprocess.run(
            ["git", "-C", str(binding.local_root), "merge-base", "--is-ancestor",
             selected_baseline, current_head],
            capture_output=True, check=False, timeout=5,
        )
        return current_head if ancestor.returncode == 0 else None
    except (StateError, ValueError, TypeError, OSError, subprocess.TimeoutExpired,
            LocalRepositoryBindingError, RunnerError):
        return None


def linked_submission_decision(connection: sqlite3.Connection, *,
                               submission_id: str,
                               continuation_run_id: str | None = None) -> dict[str, object] | None:
    """Recheck a queued or claimed canonical attempt before dispatch."""
    link = connection.execute(
        """SELECT intake_id,parent_submission_id FROM ep_parallel_action_submission_links
            WHERE submission_id=?""", (submission_id,),
    ).fetchone()
    if link is None:
        stored = connection.execute(
            """SELECT project_id,mission_id,engineering_action_id,constraints
                 FROM ep_submissions WHERE submission_id=?""",
            (submission_id,),
        ).fetchone()
        if stored is not None:
            try:
                constraints = json.loads(str(stored[3]))
            except (ValueError, TypeError):
                raise ParallelAdmissionError("PARALLEL_SUBMISSION_UNLINKED")
            if isinstance(constraints, dict) and "parallel_action_intake" in constraints:
                raise ParallelAdmissionError("PARALLEL_SUBMISSION_UNLINKED")
            if _staged_graph_claims_action(
                connection, project_id=str(stored[0]), mission_id=stored[1],
                action_id=stored[2],
            ):
                raise ParallelAdmissionError("PARALLEL_SUBMISSION_UNLINKED")
        return None
    intake_id = str(link[0])
    if not _stored_submission_matches_intake(
        connection, intake_id=intake_id, submission_id=submission_id,
    ):
        raise ParallelAdmissionError("PARALLEL_SUBMISSION_INVALID")
    baseline = None
    if link[1] is not None:
        row = connection.execute(
            "SELECT constraints FROM ep_submissions WHERE submission_id=?",
            (submission_id,),
        ).fetchone()
        baseline = json.loads(str(row[0]))["repository_revision_binding"]["allowed_baseline_revision"]
    reconcile_predecessors(connection, intake_id=intake_id)
    decision = dependency_readback(connection, intake_id=intake_id,
                                   baseline_revision=baseline)
    if baseline is not None:
        decision = _retry_pin_decision(
            connection, intake_id=intake_id, allowed_baseline=baseline,
            decision=decision,
        )
    if decision["state"] == "WAITING_BASELINE" and continuation_run_id is not None:
        selected_baseline = baseline or str(connection.execute(
            "SELECT baseline_revision FROM ep_parallel_action_intakes WHERE intake_id=?",
            (intake_id,),
        ).fetchone()[0])
        current_head = _verified_continuation_head(
            connection, intake_id=intake_id, submission_id=submission_id,
            run_id=continuation_run_id, selected_baseline=selected_baseline,
            allowed_baseline=baseline,
        )
        if current_head is not None:
            return dependency_readback(connection, intake_id=intake_id,
                                       baseline_revision=current_head)
    return decision


def delivery_readback(connection: sqlite3.Connection, *, intake_id: str,
                      data_root: object, decision: dict[str, object],
                      continuation_run_id: str | None = None) -> dict[str, object]:
    """Add live PA-E2 resource and capacity state without granting a claim."""
    row = connection.execute(
        "SELECT policy_digest,project_id,target_repository_id FROM ep_parallel_action_intakes "
        "WHERE intake_id=?", (intake_id,),
    ).fetchone()
    if row is None or row[0] != SUPPORTED_PA_E2_POLICY_DIGEST:
        return decision
    if decision["state"] != "DEPENDENCY_ELIGIBLE":
        return decision
    from pathlib import Path
    from . import parallel_action_delivery, parallel_action_recovery
    recovery_state = (
        parallel_action_recovery.status(connection, continuation_run_id)
        if continuation_run_id is not None else "NONE"
    )
    if recovery_state == "PROVIDER_EFFECT_UNCERTAIN":
        return {**decision, "state": "WAITING_RECOVERY",
                "recovery_state": recovery_state,
                "resource_state": "HELD", "capacity_state": "HELD"}
    if recovery_state == "CANCEL_REQUESTED":
        return {**decision, "state": "CANCEL_REQUESTED",
                "recovery_state": recovery_state,
                "resource_state": "HELD", "capacity_state": "HELD"}
    if recovery_state == "CANCEL_ACKNOWLEDGED":
        resource_held = connection.execute(
            "SELECT 1 FROM ep_execution_leases WHERE run_id=? "
            "AND lease_id LIKE 'pa-e2:resource:%' AND released_at IS NULL LIMIT 1",
            (continuation_run_id,),
        ).fetchone() is not None
        return {**decision, "state": "CANCEL_ACKNOWLEDGED",
                "recovery_state": recovery_state,
                "resource_state": "HELD" if resource_held else "AVAILABLE",
                "capacity_state": "AVAILABLE"}
    try:
        gate = parallel_action_delivery.gate(
            connection, data_root=Path(data_root), project_id=str(row[1]),
            repository_id=str(row[2]), run_id=continuation_run_id,
        )
    except (parallel_action_delivery.DeliveryScopeError, ValueError, OSError):
        return {**decision, "state": "WAITING_RESOURCE",
                "resource_state": "UNQUALIFIED", "capacity_state": "NOT_EVALUATED"}
    return {**decision, "state": decision["state"] if gate.state == "READY" else gate.state,
            "resource_state": gate.resource_state, "capacity_state": gate.capacity_state,
            "recovery_state": recovery_state}


def reconcile_predecessors(connection: sqlite3.Connection, *, intake_id: str) -> int:
    """Recover canonical terminal links after a finalizer or process restart."""
    row = connection.execute(
        """SELECT i.graph_id,i.action_id,g.snapshot,i.project_id,i.producer_id,
                  i.mission_id FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
            WHERE i.intake_id=?""",
        (intake_id,),
    ).fetchone()
    if row is None:
        raise ParallelAdmissionError("UNKNOWN_INTAKE")
    decision = dependency_readback(connection, intake_id=intake_id)
    if decision["state"] == "INVALID_SNAPSHOT":
        return 0
    graph = json.loads(str(row[2]))
    action = next(item for item in graph["actions"] if item["action_id"] == row[1])
    recovered = 0
    for edge in action["dependencies"]:
        predecessor = _accepted_predecessor(
            connection, graph=graph, project_id=str(row[3]),
            producer_id=str(row[4]), mission_id=str(row[5]),
            action_id=str(edge["predecessor_action_id"]),
        )
        if predecessor is None:
            continue
        predecessor_id = predecessor[0]
        linked = connection.execute(
            """SELECT l.submission_id,o.intake_id
                 FROM ep_parallel_action_submission_links l
                 JOIN ep_parity_lifecycle_dispatches d
                   ON d.submission_id=l.submission_id
                 LEFT JOIN ep_parallel_action_outcomes o ON o.intake_id=l.intake_id
                WHERE l.intake_id=? AND d.state='COMPLETE'
                  AND d.operator_resolution='NONE'
                ORDER BY d.updated_at DESC,l.submission_id DESC LIMIT 1""",
            (predecessor_id,),
        ).fetchone()
        if linked is None:
            continue
        if linked[1] is None:
            try:
                attest_terminal_outcome(
                    connection, intake_id=predecessor_id, submission_id=str(linked[0]),
                )
            except ParallelAdmissionError:
                continue
            recovered += 1
        if edge["required_evidence"]["kind"] != "QUALIFIED_ARTIFACT":
            continue
        outcome = connection.execute(
            "SELECT run_id,submission_id FROM ep_parallel_action_outcomes WHERE intake_id=?",
            (predecessor_id,),
        ).fetchone()
        if outcome is None:
            continue
        receipts = connection.execute(
            """SELECT artifact_id,qualification_artifact_id
                 FROM ep_parallel_action_qualification_receipts
                WHERE intake_id=? AND content_digest=? ORDER BY artifact_id""",
            (predecessor_id, edge["required_evidence"]["content_digest"]),
        ).fetchall()
        for package_id, qualification_id in receipts:
            try:
                attested = attest_qualified_artifact(
                    connection, intake_id=predecessor_id,
                    artifact_id=str(package_id),
                    qualification_artifact_id=str(qualification_id),
                )
            except ParallelAdmissionError:
                continue
            if not attested["replayed"]:
                recovered += 1
            break
    return recovered


@_immediate_transaction
def attest_terminal_outcome(
    connection: sqlite3.Connection, *, intake_id: str, submission_id: str,
) -> dict[str, object]:
    """Bind one successful canonical EP terminal receipt to its Action."""
    _identifier(submission_id)
    intake = connection.execute(
        """SELECT i.project_id,i.producer_id,i.action_id,i.correlation_id,
                  i.target_repository_id,g.mission_id,g.mission_revision
             FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
            WHERE i.intake_id=?""", (intake_id,),
    ).fetchone()
    if intake is None:
        raise ParallelAdmissionError("UNKNOWN_INTAKE")
    existing = connection.execute(
        "SELECT submission_id,run_id,terminal_artifact_id,terminal_digest FROM ep_parallel_action_outcomes WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if existing is not None:
        if existing[0] != submission_id:
            raise ParallelAdmissionError("OUTCOME_CONFLICT")
        if not _current_terminal_matches(connection, intake_id):
            raise ParallelAdmissionError("PREDECESSOR_EVIDENCE_UNQUALIFIED")
        return {"intake_id": intake_id, "submission_id": submission_id, "run_id": existing[1],
                "terminal_artifact_id": existing[2], "terminal_digest": existing[3], "replayed": True}
    if connection.execute(
        "SELECT 1 FROM ep_parallel_action_submission_links WHERE intake_id=? AND submission_id=?",
        (intake_id, submission_id),
    ).fetchone() is None or not _stored_submission_matches_intake(
        connection, intake_id=intake_id, submission_id=submission_id,
    ):
        raise ParallelAdmissionError("PREDECESSOR_SUBMISSION_UNLINKED")
    readback = producer_readback(connection, project_id=str(intake[0]), submission_id=submission_id,
                                 contract_version="1.3")
    if (readback is None or readback["producer"]["id"] != intake[1]
            or readback["producer"]["type"] != "FORGE"
            or readback["submission"]["id"] != submission_id
            or readback["submission"]["project_id"] != intake[0]):
        raise ParallelAdmissionError("PREDECESSOR_PROVENANCE_MISMATCH")
    correlation = readback["correlation"]
    submission = readback["submission"]
    provenance = readback["provenance"].get("forge_execution")
    if (correlation["engineering_action_id"] != intake[2]
            or correlation["correlation_id"] != intake[3]
            or correlation["mission_id"] != intake[5]
            or submission["repository_id"] != intake[4]
            or not isinstance(provenance, dict)
            or str(provenance.get("mission_revision")) != str(intake[6])):
        raise ParallelAdmissionError("PREDECESSOR_SCOPE_MISMATCH")
    run = readback["run"]
    result = readback["result"]
    evidence = readback["evidence"]
    artifact = evidence.get("terminal_artifact")
    if (not isinstance(run, dict) or not run.get("terminal")
            or result != {"outcome": "COMPLETE", "terminal": True, "delivery_qualified": True}
            or evidence.get("status") != "AVAILABLE" or not isinstance(artifact, dict)
            or not isinstance(artifact.get("digest"), str)
            or _DIGEST.fullmatch(artifact["digest"]) is None):
        raise ParallelAdmissionError("PREDECESSOR_EVIDENCE_UNQUALIFIED")
    if not _canonical_run_complete(connection, submission_id, str(run["id"])):
        raise ParallelAdmissionError("PREDECESSOR_EVIDENCE_UNQUALIFIED")
    repository = evidence.get("repository")
    revision = repository.get("revision") if isinstance(repository, dict) else None
    if (not isinstance(repository, dict) or repository.get("id") != intake[4]
            or not isinstance(revision, str)
            or _REVISION.fullmatch(revision) is None):
        raise ParallelAdmissionError("PREDECESSOR_SCOPE_MISMATCH")
    linked = connection.execute(
        """SELECT d.run_id,d.project_id,d.repository_id,
                  a.ep_run_id,a.ep_submission_id,a.digest_algorithm,a.digest
             FROM ep_parity_lifecycle_dispatches d
             JOIN execution_artifact_records a ON a.artifact_id=?
            WHERE d.submission_id=?""",
        (artifact["id"], submission_id),
    ).fetchone()
    if (linked is None or linked[0] != run["id"] or linked[1] != intake[0]
            or linked[2] != intake[4] or linked[3] != run["id"]
            or linked[4] != submission_id or linked[5] != "sha256"
            or "sha256:" + str(linked[6]) != artifact["digest"]):
        raise ParallelAdmissionError("PREDECESSOR_LINEAGE_MISMATCH")
    connection.execute(
        "INSERT INTO ep_parallel_action_outcomes VALUES(?,?,?,?,?,?,?)",
        (intake_id, submission_id, run["id"], artifact["id"], artifact["digest"], revision, _now()),
    )
    return {"intake_id": intake_id, "submission_id": submission_id, "run_id": run["id"],
            "terminal_artifact_id": artifact["id"], "terminal_digest": artifact["digest"],
            "replayed": False}


def _canonical_run_complete(connection: sqlite3.Connection, submission_id: str,
                            run_id: str) -> bool:
    row = connection.execute(
        """SELECT d.state,d.operator_resolution,r.state
             FROM ep_parity_lifecycle_dispatches d
             JOIN ep_execution_runs r ON r.run_id=d.run_id AND r.project_id=d.project_id
            WHERE d.submission_id=? AND d.run_id=?""",
        (submission_id, run_id),
    ).fetchone()
    return row is not None and tuple(row) == ("COMPLETE", "NONE", "COMPLETE")


def _current_terminal_matches(connection: sqlite3.Connection, intake_id: str) -> bool:
    row = connection.execute(
        """SELECT i.project_id,i.producer_id,i.action_id,i.correlation_id,
                  i.target_repository_id,g.mission_id,g.mission_revision,
                  o.submission_id,o.run_id,o.terminal_artifact_id,o.terminal_digest,
                  o.repository_revision
             FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
             JOIN ep_parallel_action_outcomes o ON o.intake_id=i.intake_id
            WHERE i.intake_id=?""", (intake_id,),
    ).fetchone()
    if row is None:
        return False
    if not _canonical_run_complete(connection, str(row[7]), str(row[8])):
        return False
    if connection.execute(
        "SELECT 1 FROM ep_parallel_action_submission_links WHERE intake_id=? AND submission_id=?",
        (intake_id, row[7]),
    ).fetchone() is None or not _stored_submission_matches_intake(
        connection, intake_id=intake_id, submission_id=str(row[7]),
    ):
        return False
    readback = producer_readback(connection, project_id=str(row[0]),
                                 submission_id=str(row[7]), contract_version="1.3")
    if readback is None:
        return False
    run = readback.get("run")
    evidence = readback.get("evidence")
    result = readback.get("result")
    artifact = evidence.get("terminal_artifact") if isinstance(evidence, dict) else None
    repository = evidence.get("repository") if isinstance(evidence, dict) else None
    correlation = readback.get("correlation")
    provenance = readback.get("provenance")
    forge_execution = provenance.get("forge_execution") if isinstance(provenance, dict) else None
    effective_artifact_id, effective_digest = row[9], row[10]
    if (isinstance(artifact, dict)
            and (artifact.get("id"), artifact.get("digest")) != (row[9], row[10])):
        replacement = connection.execute(
            """SELECT replacement_artifact_id,replacement_digest
                 FROM ep_terminal_evidence_reconciliation_operations
                WHERE run_id=? AND project_id=? AND source_artifact_id=?
                  AND source_digest=? AND reason_code='MERGE_CANDIDATE_PROJECTION_V1'""",
            (row[8], row[0], row[9], str(row[10])[7:]),
        ).fetchone()
        if replacement is not None and (
            artifact.get("id"), artifact.get("digest")
        ) == (replacement[0], "sha256:" + str(replacement[1])):
            effective_artifact_id, effective_digest = (
                str(replacement[0]), "sha256:" + str(replacement[1]))
    linked = connection.execute(
        """SELECT d.run_id,d.project_id,d.repository_id,
                  a.ep_run_id,a.ep_submission_id,a.digest_algorithm,a.digest,
                  a.projection_status
             FROM ep_parity_lifecycle_dispatches d
             JOIN execution_artifact_records a ON a.artifact_id=?
            WHERE d.submission_id=?""",
        (effective_artifact_id, row[7]),
    ).fetchone()
    return bool(
        linked is not None and linked[0] == row[8] and linked[1] == row[0]
        and linked[2] == row[4] and linked[3] == row[8]
        and linked[4] == row[7] and linked[5] == "sha256"
        and "sha256:" + str(linked[6]) == effective_digest
        and linked[7] == "AVAILABLE"
        and readback.get("producer", {}).get("id") == row[1]
        and readback.get("producer", {}).get("type") == "FORGE"
        and readback.get("submission", {}).get("id") == row[7]
        and readback.get("submission", {}).get("project_id") == row[0]
        and readback.get("submission", {}).get("repository_id") == row[4]
        and isinstance(correlation, dict)
        and correlation.get("engineering_action_id") == row[2]
        and correlation.get("correlation_id") == row[3]
        and correlation.get("mission_id") == row[5]
        and isinstance(forge_execution, dict)
        and str(forge_execution.get("mission_revision")) == str(row[6])
        and isinstance(run, dict) and run.get("id") == row[8] and run.get("terminal") is True
        and result == {"outcome": "COMPLETE", "terminal": True, "delivery_qualified": True}
        and isinstance(evidence, dict) and evidence.get("status") == "AVAILABLE"
        and isinstance(artifact, dict) and artifact.get("id") == effective_artifact_id
        and artifact.get("digest") == effective_digest
        and isinstance(repository, dict) and repository.get("id") == row[4]
        and isinstance(row[11], str) and _REVISION.fullmatch(row[11]) is not None
        and repository.get("revision") == row[11]
    )


def _verified_artifact_digest(
    connection: sqlite3.Connection, *, artifact_id: str, run_id: str,
    submission_id: str, expected_type: str = "EP_DELIVERABLE_PACKAGE",
) -> str | None:
    row = connection.execute(
        """SELECT artifact_type,digest_algorithm,digest,storage_location,
                  ep_run_id,ep_submission_id,projection_status
             FROM execution_artifact_records WHERE artifact_id=?""",
        (artifact_id,),
    ).fetchone()
    if (row is None or row[0] != expected_type or row[1] != "sha256"
            or not isinstance(row[2], str) or len(row[2]) != 64
            or row[4] != run_id or row[5] != submission_id
            or row[6] != "AVAILABLE"):
        return None
    try:
        from pathlib import Path
        database = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        root = (database.parent / "artifacts").resolve()
        target = (root / str(row[3])).resolve()
        target.relative_to(root)
        digest = sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest() if digest.hexdigest() == row[2] else None
    except (OSError, ValueError, sqlite3.Error):
        return None


def _verified_qualification_manifest(
    connection: sqlite3.Connection, *, intake_id: str, artifact_id: str,
    qualification_artifact_id: str, content_digest: str,
) -> bool:
    context = connection.execute(
        """SELECT i.project_id,i.target_repository_id,o.submission_id,o.run_id,
                  o.terminal_artifact_id,o.terminal_digest
             FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_outcomes o ON o.intake_id=i.intake_id
            WHERE i.intake_id=?""", (intake_id,),
    ).fetchone()
    if context is None:
        return False
    record = connection.execute(
        """SELECT artifact_type,digest_algorithm,digest,storage_location,
                  ep_run_id,ep_submission_id,projection_status
             FROM execution_artifact_records WHERE artifact_id=?""",
        (qualification_artifact_id,),
    ).fetchone()
    if (record is None or record[0] != "EP_ARTIFACT_QUALIFICATION"
            or record[1] != "sha256"
            or not isinstance(record[2], str)
            or re.fullmatch(r"[0-9a-f]{64}", record[2]) is None
            or record[4] != context[3] or record[5] != context[2]
            or record[6] != "AVAILABLE"):
        return False
    try:
        from pathlib import Path
        database = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        root = (database.parent / "artifacts").resolve()
        target = (root / str(record[3])).resolve()
        target.relative_to(root)
        with target.open("rb") as stream:
            document = stream.read(4097)
        if len(document) > 4096 or sha256(document).hexdigest() != record[2]:
            return False
        payload = json.loads(document.decode("utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError, sqlite3.Error):
        return False
    readback = producer_readback(
        connection, project_id=str(context[0]),
        submission_id=str(context[2]), contract_version="1.3",
    )
    evidence = readback.get("evidence") if isinstance(readback, dict) else None
    active = evidence.get("terminal_artifact") if isinstance(evidence, dict) else None
    if not isinstance(active, dict):
        return False
    active_id, active_digest = active.get("id"), active.get("digest")
    if (not isinstance(active_id, str) or not isinstance(active_digest, str)
            or _DIGEST.fullmatch(active_digest) is None):
        return False
    if (active_id, active_digest) != (context[4], context[5]):
        replacement = connection.execute(
            """SELECT 1 FROM ep_terminal_evidence_reconciliation_operations
                WHERE run_id=? AND project_id=? AND source_artifact_id=?
                  AND source_digest=? AND replacement_artifact_id=?
                  AND replacement_digest=?
                  AND reason_code='MERGE_CANDIDATE_PROJECTION_V1'""",
            (context[3], context[0], context[4], str(context[5])[7:],
             active_id, str(active_digest)[7:]),
        ).fetchone()
        if replacement is None:
            return False
    expected = {
        "contract_version": "ep-artifact-qualification/v1",
        "artifact_id": artifact_id,
        "content_digest": content_digest,
        "project_id": context[0],
        "repository_id": context[1],
        "submission_id": context[2],
        "run_id": context[3],
        "terminal_artifact_id": active_id,
        "terminal_digest": active_digest,
        "status": "PASS",
        "controls": {"package_integrity": "PASS", "installed_readback": "PASS"},
    }
    return payload == expected and _canonical(payload).encode() == document


def _installed_artifact_matches(connection: sqlite3.Connection, *,
                                intake_id: str, relative_path: str,
                                content_digest: str) -> bool:
    from pathlib import Path
    relative = Path(relative_path)
    if (relative.is_absolute() or not relative.parts
            or any(part in (".", "..", ".git") for part in relative.parts)):
        return False
    row = connection.execute(
        "SELECT project_id,target_repository_id FROM ep_parallel_action_intakes WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        binding = resolve_execution_repository(
            connection, project_id=str(row[0]), repository_id=str(row[1]),
        )
        root = binding.local_root.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        target.relative_to(root)
        if not target.is_file():
            return False
        digest = sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest() == content_digest
    except (OSError, ValueError, LocalRepositoryBindingError):
        return False


def _verified_qualification(
    connection: sqlite3.Connection, *, intake_id: str, artifact_id: str,
    qualification_artifact_id: str, content_digest: str,
) -> bool:
    if not _verified_qualification_manifest(
        connection, intake_id=intake_id, artifact_id=artifact_id,
        qualification_artifact_id=qualification_artifact_id,
        content_digest=content_digest,
    ):
        return False
    receipt = connection.execute(
        """SELECT installed_relative_path FROM ep_parallel_action_qualification_receipts
            WHERE intake_id=? AND artifact_id=? AND qualification_artifact_id=?
              AND content_digest=?""",
        (intake_id, artifact_id, qualification_artifact_id, content_digest),
    ).fetchone()
    return receipt is not None and _installed_artifact_matches(
        connection, intake_id=intake_id, relative_path=str(receipt[0]),
        content_digest=content_digest,
    )


@_immediate_transaction
def record_qualification_receipt(
    connection: sqlite3.Connection, *, data_root: object, intake_id: str,
    artifact_id: str, qualification_artifact_id: str,
    installed_relative_path: str, reason: str,
) -> dict[str, object]:
    """Owner-observed package installation, independent of producer PASS text."""
    from pathlib import Path
    from .platform_admin import require_installation_owner
    owner_root = Path(data_root).resolve()
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2])).resolve()
    if database_path.parent != owner_root:
        raise ParallelAdmissionError("QUALIFICATION_INSTALLATION_MISMATCH")
    actor = require_installation_owner(owner_root)
    _identifier(artifact_id)
    _identifier(qualification_artifact_id)
    if not isinstance(installed_relative_path, str) or not installed_relative_path:
        raise ParallelAdmissionError("INSTALLED_READBACK_UNAVAILABLE")
    if not isinstance(reason, str) or not 0 < len(reason.strip()) <= 512:
        raise ParallelAdmissionError("INVALID_QUALIFICATION_REASON")
    outcome = connection.execute(
        "SELECT submission_id,run_id FROM ep_parallel_action_outcomes WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if outcome is None or not _current_terminal_matches(connection, intake_id):
        raise ParallelAdmissionError("PREDECESSOR_EVIDENCE_UNQUALIFIED")
    digest = _verified_artifact_digest(
        connection, artifact_id=artifact_id, run_id=str(outcome[1]),
        submission_id=str(outcome[0]),
    )
    if (digest is None or not _verified_qualification_manifest(
        connection, intake_id=intake_id, artifact_id=artifact_id,
        qualification_artifact_id=qualification_artifact_id, content_digest=digest,
    ) or not _installed_artifact_matches(
        connection, intake_id=intake_id, relative_path=installed_relative_path,
        content_digest=digest,
    )):
        raise ParallelAdmissionError("INSTALLED_READBACK_UNAVAILABLE")
    previous = connection.execute(
        """SELECT qualification_artifact_id,content_digest,installed_relative_path
             FROM ep_parallel_action_qualification_receipts
            WHERE intake_id=? AND artifact_id=?""",
        (intake_id, artifact_id),
    ).fetchone()
    if previous is not None:
        if previous != (qualification_artifact_id, digest, installed_relative_path):
            raise ParallelAdmissionError("QUALIFICATION_RECEIPT_CONFLICT")
        return {"intake_id": intake_id, "artifact_id": artifact_id,
                "content_digest": digest, "replayed": True}
    connection.execute(
        "INSERT INTO ep_parallel_action_qualification_receipts VALUES(?,?,?,?,?,?,?,?)",
        (intake_id, artifact_id, qualification_artifact_id, digest,
         installed_relative_path, actor, reason.strip(), _now()),
    )
    return {"intake_id": intake_id, "artifact_id": artifact_id,
            "content_digest": digest, "replayed": False}


def _target_baseline_state(
    connection: sqlite3.Connection, *, project_id: str,
    repository_id: str, baseline_revision: str,
) -> str:
    try:
        binding = resolve_execution_repository(
            connection, project_id=project_id, repository_id=repository_id,
        )
    except LocalRepositoryBindingError:
        return "WAITING_SCOPE"
    try:
        observed = subprocess.run(
            ["git", "-C", str(binding.local_root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        branch = subprocess.run(
            ["git", "-C", str(binding.local_root), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        status = subprocess.run(
            ["git", "-C", str(binding.local_root), "--no-optional-locks", "status",
             "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "WAITING_BASELINE"
    if (observed.returncode != 0 or observed.stdout.strip() != baseline_revision
            or branch.returncode != 0 or branch.stdout.strip() != "main"
            or status.returncode != 0 or status.stdout.strip()):
        return "WAITING_BASELINE"
    return "VERIFIED"


@_immediate_transaction
def attest_qualified_artifact(
    connection: sqlite3.Connection, *, intake_id: str, artifact_id: str,
    qualification_artifact_id: str,
) -> dict[str, object]:
    """Bind exact qualified deliverable bytes to a verified terminal run.

    A source merge or terminal receipt by itself never creates this record.
    """
    _identifier(artifact_id)
    _identifier(qualification_artifact_id)
    outcome = connection.execute(
        "SELECT submission_id,run_id FROM ep_parallel_action_outcomes WHERE intake_id=?",
        (intake_id,),
    ).fetchone()
    if outcome is None or not _current_terminal_matches(connection, intake_id):
        raise ParallelAdmissionError("PREDECESSOR_EVIDENCE_UNQUALIFIED")
    digest = _verified_artifact_digest(
        connection, artifact_id=artifact_id, run_id=str(outcome[1]),
        submission_id=str(outcome[0]),
    )
    if digest is None:
        raise ParallelAdmissionError("QUALIFIED_ARTIFACT_UNAVAILABLE")
    if not _verified_qualification(
        connection, intake_id=intake_id, artifact_id=artifact_id,
        qualification_artifact_id=qualification_artifact_id,
        content_digest=digest,
    ):
        raise ParallelAdmissionError("ARTIFACT_QUALIFICATION_UNAVAILABLE")
    existing = connection.execute(
        "SELECT qualification_artifact_id,content_digest FROM ep_parallel_action_qualified_artifacts WHERE intake_id=? AND artifact_id=?",
        (intake_id, artifact_id),
    ).fetchone()
    if existing is not None:
        if existing[0] != qualification_artifact_id or existing[1] != digest:
            raise ParallelAdmissionError("QUALIFIED_ARTIFACT_CONFLICT")
        return {"intake_id": intake_id, "artifact_id": artifact_id,
                "content_digest": digest, "replayed": True}
    connection.execute(
        "INSERT INTO ep_parallel_action_qualified_artifacts VALUES(?,?,?,?,?)",
        (intake_id, artifact_id, qualification_artifact_id, digest, _now()),
    )
    return {"intake_id": intake_id, "artifact_id": artifact_id,
            "content_digest": digest, "replayed": False}


def dependency_readback(connection: sqlite3.Connection, *, intake_id: str,
                        baseline_revision: str | None = None) -> dict[str, object]:
    """Resolve exact producer edges; resource/capacity remain unevaluated."""
    row = connection.execute(
        """SELECT i.graph_id,i.project_id,i.producer_id,i.action_id,i.target_repository_id,
                  i.baseline_revision,g.snapshot,g.snapshot_digest
             FROM ep_parallel_action_intakes i
             JOIN ep_parallel_action_graphs g ON g.graph_id=i.graph_id
            WHERE i.intake_id=?""", (intake_id,),
    ).fetchone()
    if row is None:
        raise ParallelAdmissionError("UNKNOWN_INTAKE")
    if baseline_revision is not None and _REVISION.fullmatch(baseline_revision) is None:
        raise ParallelAdmissionError("INVALID_BASELINE")
    effective_baseline = baseline_revision or str(row[5])
    try:
        graph = json.loads(str(row[6]))
        if (not isinstance(graph, dict) or _canonical(graph) != row[6]
                or graph.get("contract_version") != "ep-parallel-action-compat/v1"
                or graph.get("producer_snapshot_digest") != row[7]
                or graph.get("status") != "COMPATIBLE"
                or graph.get("dispatch_authorized") is not False):
            raise ValueError("invalid stored compatibility readback")
        canonical_graph = {
            "contract_version": "parallel-action-graph/v1",
            "mission_id": graph["mission_id"],
            "mission_revision": graph["mission_revision"],
            "actions": [
                {"action_id": item["action_id"], "target": item["target"],
                 "dependencies": item["dependencies"]}
                for item in graph["actions"]
            ],
        }
        digest = "sha256:" + sha256(_canonical(canonical_graph).encode()).hexdigest()
        if digest != row[7]:
            raise ValueError("snapshot digest mismatch")
        action = next(item for item in graph["actions"] if item["action_id"] == row[3])
        if (action["target"]["repository_id"] != row[4]
                or action["target"]["baseline_revision"] != row[5]):
            raise ValueError("intake target mismatch")
    except (ValueError, TypeError, KeyError, StopIteration):
        return {
            "contract_version": READBACK_VERSION, "intake_id": intake_id,
            "snapshot_digest": row[7], "state": "INVALID_SNAPSHOT",
            "blockers": ["SNAPSHOT_INTEGRITY_INVALID"],
            "target_repository_id": row[4], "target_baseline_revision": effective_baseline,
            "target_baseline_state": "UNVERIFIED", "policy_state": "UNVERIFIED",
            "admission": "NOT_GRANTED", "resource_state": "NOT_EVALUATED",
            "capacity_state": "NOT_EVALUATED", "dispatch_authorized": False,
        }
    graph_revision = connection.execute(
        "SELECT mission_revision FROM ep_parallel_action_graphs WHERE graph_id=?",
        (row[0],),
    ).fetchone()[0]
    latest_revision = connection.execute(
        """SELECT MAX(mission_revision) FROM ep_parallel_action_graphs
            WHERE project_id=? AND producer_id=? AND mission_id=?""",
        (row[1], row[2], graph["mission_id"]),
    ).fetchone()[0]
    accepted = connection.execute(
        """SELECT 1 FROM ep_parallel_action_submission_links
            WHERE intake_id=? AND parent_submission_id IS NULL""",
        (intake_id,),
    ).fetchone() is not None
    if graph_revision != latest_revision and not accepted:
        return {
            "contract_version": READBACK_VERSION, "intake_id": intake_id,
            "snapshot_digest": row[7], "state": "GRAPH_SUPERSEDED",
            "blockers": ["GRAPH_REVISION_SUPERSEDED"],
            "target_repository_id": row[4], "target_baseline_revision": effective_baseline,
            "target_baseline_state": "UNVERIFIED", "policy_state": "PROFILE_BOUND",
            "admission": "NOT_GRANTED", "resource_state": "NOT_EVALUATED",
            "capacity_state": "NOT_EVALUATED", "dispatch_authorized": False,
        }
    try:
        scope = _authoritative_scope(connection, str(row[1]))
    except ParallelAdmissionError:
        scope = None
    scope_available = bool(
        scope is not None and scope.ep_instance_id == action["target"]["ep_instance_id"]
        and row[4] in scope.repository_ids
        and _target_grant_current(
            connection, consumer_id=str(row[2]), project_id=str(row[1]),
            repository_id=str(row[4]),
        )
    )
    baseline_state = (
        _target_baseline_state(
            connection, project_id=str(row[1]), repository_id=str(row[4]),
            baseline_revision=effective_baseline,
        ) if scope_available else "UNVERIFIED"
    )
    blockers = []
    if not scope_available:
        blockers.append("TARGET_SCOPE_UNAVAILABLE")
    elif baseline_state != "VERIFIED":
        blockers.append(
            "TARGET_SCOPE_UNAVAILABLE" if baseline_state == "WAITING_SCOPE"
            else "TARGET_BASELINE_UNVERIFIED"
        )
    for edge in action["dependencies"]:
        predecessor = _accepted_predecessor(
            connection, graph=graph, project_id=str(row[1]),
            producer_id=str(row[2]), mission_id=str(graph["mission_id"]),
            action_id=str(edge["predecessor_action_id"]),
        )
        outcome = connection.execute(
            "SELECT repository_revision FROM ep_parallel_action_outcomes WHERE intake_id=?",
            (predecessor[0],),
        ).fetchone() if predecessor is not None else None
        required = edge["required_evidence"]
        if predecessor is None or outcome is None or outcome[0] is None:
            blockers.append(edge["predecessor_action_id"])
        elif predecessor[3] != required["repository_id"]:
            blockers.append(edge["predecessor_action_id"])
        elif not _current_terminal_matches(connection, str(predecessor[0])):
            blockers.append(edge["predecessor_action_id"])
        elif required["kind"] == "QUALIFIED_ARTIFACT" and not any(
            candidate[0] == required["content_digest"] and _verified_artifact_digest(
                connection, artifact_id=str(candidate[1]), run_id=str(candidate[3]),
                submission_id=str(candidate[4]),
            ) == required["content_digest"] and _verified_qualification(
                connection, intake_id=str(predecessor[0]), artifact_id=str(candidate[1]),
                qualification_artifact_id=str(candidate[2]), content_digest=str(candidate[0]),
            )
            for candidate in connection.execute(
                """SELECT qa.content_digest,qa.artifact_id,qa.qualification_artifact_id,
                          o.run_id,o.submission_id
                     FROM ep_parallel_action_qualified_artifacts qa
                     JOIN ep_parallel_action_outcomes o ON o.intake_id=qa.intake_id
                    WHERE qa.intake_id=?""", (predecessor[0],),
            )
        ):
            blockers.append(edge["predecessor_action_id"])
        elif required["kind"] == "REPOSITORY_REVISION" and (
            not isinstance(outcome[0], str)
            or "sha256:" + sha256(outcome[0].encode()).hexdigest() != required["content_digest"]
        ):
            blockers.append(edge["predecessor_action_id"])
    if not scope_available or baseline_state == "WAITING_SCOPE":
        state = "WAITING_SCOPE"
    elif baseline_state != "VERIFIED":
        state = baseline_state
    else:
        state = "WAITING_DEPENDENCY" if blockers else "DEPENDENCY_ELIGIBLE"
    return {
        "contract_version": READBACK_VERSION,
        "intake_id": intake_id,
        "snapshot_digest": row[7],
        "state": state,
        "blockers": blockers,
        "target_repository_id": row[4],
        "target_baseline_revision": effective_baseline,
        "target_baseline_state": baseline_state,
        "policy_state": "PROFILE_BOUND",
        "admission": "NOT_GRANTED",
        "resource_state": "NOT_EVALUATED",
        "capacity_state": "NOT_EVALUATED",
        "dispatch_authorized": False,
    }
