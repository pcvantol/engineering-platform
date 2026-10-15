"""Bounded CENTRAL-only execution progress. This reader cannot execute work."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from . import execution_lifecycle, storage, submission_service
from . import execution_progress_authority as authority
from .platform_version import CURRENT_PLATFORM_VERSION

ProgressError = authority.ProgressError
MAX_RECORDS = 100
MAX_EVENTS = 100
MAX_BYTES = 524288
HEADERS = ("EP-Progress-Contract", "EP-Instance-ID", "EP-Consumer-ID",
           "Workspace-Instance-ID", "EP-Audience")
ROLES = {
    "INITIALIZE": "EXECUTION_HOST", "CAPABILITY_REVIEW": "EXECUTION_HOST",
    "EXECUTE_AGENT": "IMPLEMENTATION_AGENT", "QUALITY_CONTROL_AGENT": "AUTONOMOUS_QUALITY_SECURITY",
    "LOCAL_REPOSITORY_VALIDATION": "EXECUTION_HOST", "REPAIR_AGENT": "IMPLEMENTATION_AGENT",
    "FINALIZATION_REPAIR_AGENT": "IMPLEMENTATION_AGENT", "FINALIZE_AGENT": "FINALIZATION_AGENT",
    "RECONCILE_AGENT": "EXECUTION_HOST", "REPOSITORY_CLEANUP": "EXECUTION_HOST",
    "WAIT_FOR_OPERATOR_MERGE": "OPERATOR", "WAIT_FOR_FINALIZATION_MERGE": "OPERATOR",
    "WAIT_FOR_RECONCILIATION_MERGE": "OPERATOR",
}


def openapi_paths():
    headers = [{"name": key, "in": "header", "required": True, "schema": {"type": "string"}}
               for key in HEADERS]
    result = {}
    for route in ("/v1/execution-progress/projects/{project_id}", "/v1/execution-progress/projects/{project_id}/runs/{run_id}"):
        paths = ["project_id"] + (["run_id"] if "{run_id}" in route else [])
        result[route] = {"get": {"summary": "Read bounded CENTRAL Mission/EA execution progress; no action authority",
            "security": [{"executionProgressBearer": []}],
            "parameters": headers + [{"name": key, "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}} for key in paths]
                + [{"name": "mission_id", "in": "query", "required": True, "schema": {"type": "string", "maxLength": 128}},
                   {"name": "expected_snapshot", "in": "query", "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}],
            "responses": {str(code): {"description": description} for code, description in (
                (200, "Closed execution-progress snapshot1.0"), (400, "Invalid request"), (401, "Invalid/revoked/expired progress grant"),
                (403, "Scope/authority denied"), (404, "Exact run not found in selected scope"),
                (409, "Snapshot/provenance/limit conflict; explicit reread needed"), (422, "Unsupported contract/schema"),
                (500, "Source corrupt"), (503, "Source unavailable/deadline"))},
            "x-response-json-schema": "urn:engineering-platform:workspace-execution-progress:1.0"}}
    return result


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 262144:
        raise ProgressError("PROGRESS_SOURCE_LIMIT", 409)
    try:
        value = json.loads(raw)
    except ValueError:
        raise ProgressError("PROGRESS_SOURCE_CORRUPT", 500) from None
    if not isinstance(value, dict):
        raise ProgressError("PROGRESS_SOURCE_CORRUPT", 500)
    return value


def _timestamp(value):
    try:
        # Existing lifecycle schema uses SQLite CURRENT_TIMESTAMP, defined UTC.
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", value):
            value = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").isoformat() + "+00:00"
        if not isinstance(value, str) or len(value) > 40 or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError
    except ValueError:
        raise ProgressError("PROGRESS_TIMESTAMP_CORRUPT", 500) from None
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def source_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ProgressError("PROGRESS_SOURCE_CONFLICT", 409)
    return info.st_dev, info.st_ino


def _generation(path, identity):
    """Rollback-journal SQLite's own persistent source-generation header.

    Read while the consistent transaction holds its normal SQLite read lock.
    Unlike connection-local data_version it remains comparable after restart.
    No source data or independent status is persisted by this reader.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            raise ProgressError("PROGRESS_SOURCE_CONFLICT", 409)
        header = os.read(fd, 100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\0":
            raise ProgressError("PROGRESS_SOURCE_CORRUPT", 500)
        return hashlib.sha256(header).hexdigest()
    finally:
        os.close(fd)


def _provenance(row):
    constraints = _json(row["constraints"])
    if row["producer_type"] != "FORGE" or "forge_execution" not in constraints:
        return None
    request = submission_service.SubmissionRequest(
        project_id=row["project_id"], repository_id=row["repository_id"],
        producer_id=row["producer_id"], producer_type=row["producer_type"],
        producer_version=row["producer_version"], prompt="not inspected",
        transport=row["transport"], correlation_id=row["correlation_id"],
        mission_id=row["mission_id"], engineering_action_id=row["engineering_action_id"],
        constraints=constraints)
    try:
        submission_service._forge_provenance(request)
    except (submission_service.SubmissionError, TypeError, ValueError):
        raise ProgressError("PROGRESS_PROVENANCE_CONFLICT", 409) from None
    raw = constraints["forge_execution"]
    for key in ("mission_id", "action_id", "mission_revision", "correlation_id"):
        authority._id(raw[key])
    envelope = submission_service._forge_action_context(request)
    return raw, envelope


def _lifecycle(connection, run_id, mode):
    row = connection.execute("SELECT substr(payload,1,262145),phase,updated_at FROM engineering_transactions WHERE run_id=?", (run_id,)).fetchone()
    events = connection.execute("SELECT phase,substr(checkpoint,1,262145),recorded_at FROM execution_lifecycle_events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, MAX_EVENTS + 1)).fetchall()
    if row is None or not events:
        return dict(available=False, current_phase=None, activity="UNKNOWN", wait_reason="NOT_RECORDED",
                    responsible_role="UNKNOWN", steps=[], history=[], history_completeness="UNAVAILABLE"), None
    checkpoint = _json(row[0])
    if checkpoint.get("run_id") != run_id or checkpoint.get("phase") != row[1]:
        raise ProgressError("PROGRESS_RUN_CONFLICT", 409)
    # Existing pure lifecycle mapping; timings, raw checkpoints and review text
    # are deliberately not part of this external record class.
    truncated = len(events) > MAX_EVENTS
    events = list(reversed(events[:MAX_EVENTS]))
    for phase, payload, at in events:
        event = _json(payload)
        _timestamp(at)
        if event.get("run_id") != run_id or event.get("phase") != phase:
            raise ProgressError("PROGRESS_EVENT_CONFLICT", 409)
    if truncated:
        raise ProgressError("PROGRESS_HISTORY_LIMIT", 409)
    value = execution_lifecycle.project_records(run_id, row, events, (mode,), [], None)
    current = value["current_step"]
    waiting = current in {"WAIT_FOR_OPERATOR_MERGE", "WAIT_FOR_FINALIZATION_MERGE", "WAIT_FOR_RECONCILIATION_MERGE"} and row[1] == "WAIT_FOR_OPERATOR_MERGE" and checkpoint.get("next_action") != "resume_verified_merge"
    terminal = row[1] in execution_lifecycle.TERMINAL
    activity = "TERMINAL" if terminal else "WAITING" if waiting else "PROCESSING" if current in ROLES else "UNKNOWN"
    reason = "OPERATOR_MERGE_REQUIRED" if waiting else "TERMINAL_OUTCOME" if terminal else "NOT_RECORDED"
    if row[1] == "WAIT_FOR_TERMINAL_EVIDENCE":
        activity, reason = "WAITING", "AUTOMATED_CHECK_EVIDENCE"
    steps = [{"phase": s["id"], "state": s["state"]} for s in value["steps"]]
    history = [{"phase": execution_lifecycle._display_phase(phase, _json(payload)), "recorded_at": _timestamp(at),
                "responsible_role": "EXECUTION_HOST" if phase == "WAIT_FOR_TERMINAL_EVIDENCE" else "OPERATOR" if phase == "WAIT_FOR_OPERATOR_MERGE" and _json(payload).get("next_action") != "resume_verified_merge" else ROLES.get(execution_lifecycle._display_phase(phase, _json(payload)), "UNKNOWN"),
                "wait_reason": "OPERATOR_MERGE_REQUIRED" if phase == "WAIT_FOR_OPERATOR_MERGE" and _json(payload).get("next_action") != "resume_verified_merge" else "AUTOMATED_CHECK_EVIDENCE" if phase == "WAIT_FOR_TERMINAL_EVIDENCE" else "NOT_RECORDED"}
               for phase, payload, at in events]
    permitted = set(execution_lifecycle._MANAGED_PATH) | set(execution_lifecycle._FINALIZATION_REPAIR_PATH) | {"COMPLETE", "BLOCKED", "FAILED", "WAIT_FOR_TERMINAL_EVIDENCE"}
    for e in history:
        if e["phase"] not in permitted:
            e["phase"] = "UNAVAILABLE"
    return dict(available=True, current_phase=current, activity=activity, wait_reason=reason,
                terminal_outcome=value["terminal_state"],
                responsible_role="EXECUTION_HOST" if reason == "AUTOMATED_CHECK_EVIDENCE" else ROLES.get(current, "UNKNOWN"),
                steps=steps, history=history, history_completeness="COMPLETE_WITHIN_RECORDED_EVENTS"), row[2]


def _lineage(connection, row):
    run_id = row["run_id"]
    link = connection.execute("""SELECT a.submission_id,a.canonical_submission_id,a.retry_parent_submission_id,
        p.canonical_submission_id,pl.run_id FROM execution_submission_attempt_links l
        JOIN execution_submission_attempts a ON a.submission_id=l.submission_id
        LEFT JOIN execution_submission_attempts p ON p.submission_id=a.retry_parent_submission_id
        LEFT JOIN execution_submission_attempt_links pl ON pl.submission_id=p.submission_id WHERE l.run_id=?""", (run_id,)).fetchone()
    if link is None:
        return dict(state="UNAVAILABLE", attempt_submission_id=None, root_submission_id=None, retry_parent_submission_id=None, retry_parent_run_id=None)
    if link[0] != row["submission_id"] or link[2] == link[0] or (link[2] is None and link[0] != link[1]):
        raise ProgressError("PROGRESS_ATTEMPT_CONFLICT", 409)
    root = connection.execute("""SELECT e.producer_id,e.producer_type,e.mission_id,e.engineering_action_id,
        s.project_id,s.repository_id FROM execution_submissions e LEFT JOIN ep_submissions s ON s.submission_id=e.submission_id
        WHERE e.submission_id=?""", (link[1],)).fetchone()
    if root is None:
        raise ProgressError("PROGRESS_ROOT_UNAVAILABLE", 409)
    if tuple(root) != tuple(row[k] for k in ("producer_id", "producer_type", "mission_id", "engineering_action_id", "project_id", "repository_id")):
        raise ProgressError("PROGRESS_ROOT_CONFLICT", 409)
    if link[2] is not None:
        parent = connection.execute("SELECT project_id,repository_id,producer_id,mission_id,engineering_action_id FROM ep_submissions WHERE submission_id=?", (link[2],)).fetchone()
        if link[3] != link[1] or link[4] is None or parent is None or tuple(parent) != tuple(row[k] for k in ("project_id", "repository_id", "producer_id", "mission_id", "engineering_action_id")):
            raise ProgressError("PROGRESS_PARENT_CONFLICT", 409)
    qualified = connection.execute("SELECT submission_id,fresh_submission,retry_parent_run_id,resume_parent_run_id FROM execution_run_qualification_context WHERE run_id=?", (run_id,)).fetchone()
    if qualified is not None and (qualified[0] != link[0] or (link[2] is not None and (qualified[1] or qualified[2] != link[4] or qualified[3] is not None)) or (link[2] is None and qualified[2] is not None)):
        raise ProgressError("PROGRESS_LINEAGE_CONFLICT", 409)
    return dict(state="AVAILABLE", attempt_submission_id=link[0], root_submission_id=link[1], retry_parent_submission_id=link[2], retry_parent_run_id=link[4])


def _accepted_source(connection, row, raw):
    """Follow only real owner retry lineage to the original accepted exchange.

    An internal retry is not another Forge HTTP exchange. It has its own exact
    accepted submission/attempt, but no newly minted exchange receipt. Missing
    or contradictory modern evidence remains a conflict, never a fallback.
    """
    current, first_parent, visited = row, None, set()
    identity = ("project_id", "repository_id", "producer_id", "producer_type", "mission_id", "engineering_action_id")
    for _ in range(16):
        if current["submission_id"] in visited:
            raise ProgressError("PROGRESS_RETRY_CONFLICT", 409)
        visited.add(current["submission_id"])
        receipt = submission_service._forge_submission_receipt(connection, project_id=current["project_id"], submission_id=current["submission_id"])
        if receipt is not None:
            digest = submission_service._accepted_request_digest(**{k: current[k] for k in (
                "repository_id", "producer_id", "producer_type", "producer_version", "prompt_digest", "correlation_id", "mission_id", "engineering_action_id")}, constraints=_json(current["constraints"]))
            if receipt["accepted_request_digest"] != digest:
                raise ProgressError("PROGRESS_ACCEPTANCE_CONFLICT", 409)
            return dict(status="INTERNAL_RETRY_FROM_ACCEPTED_HTTP" if first_parent is not None else "ACCEPTED_HTTP",
                        acceptance_submission_id=current["submission_id"], acceptance_receipt_id=authority._id(receipt["id"])), first_parent
        parents = connection.execute("""SELECT s.submission_id,s.project_id,s.repository_id,s.producer_id,s.producer_type,
            s.producer_version,s.prompt_digest,substr(s.constraints,1,262145) AS constraints,s.correlation_id,s.mission_id,s.engineering_action_id,d.run_id
            FROM ep_parity_lifecycle_dispatches d JOIN ep_submissions s ON s.submission_id=d.submission_id
            WHERE d.resolution_submission_id=? AND d.operator_resolution='RETRIED' LIMIT 2""", (current["submission_id"],)).fetchall()
        if not parents:
            if raw["contract_version"] != "1.0":
                raise ProgressError("PROGRESS_ACCEPTANCE_CONFLICT", 409)
            return dict(status="INTERNAL_RETRY_PERSISTED_V1_0" if first_parent is not None else "PERSISTED_V1_0",
                        acceptance_submission_id=None, acceptance_receipt_id=None), first_parent
        if len(parents) != 1:
            raise ProgressError("PROGRESS_RETRY_CONFLICT", 409)
        parent = parents[0]
        if tuple(parent[k] for k in identity) != tuple(row[k] for k in identity) or _json(parent["constraints"]).get("forge_execution") != raw:
            raise ProgressError("PROGRESS_RETRY_CONFLICT", 409)
        first_parent = parent if first_parent is None else first_parent
        current = parent
    raise ProgressError("PROGRESS_RETRY_LIMIT", 409)


def _record(connection, row):
    provenance = _provenance(row)
    if provenance is None:
        return None
    raw, envelope = provenance
    accepted, retry_parent = _accepted_source(connection, row, raw)
    run_id = row["run_id"]
    lineage = None
    if run_id is not None:
        authority._id(run_id)
        if row["run_project"] != row["project_id"] or row["dispatch_repository"] != row["repository_id"]:
            raise ProgressError("PROGRESS_RUN_CONFLICT", 409)
        receipt = connection.execute("SELECT submission_id,project_id,repository_id,installation_id FROM ep_receipt_run_provenance WHERE run_id=?", (run_id,)).fetchone()
        instance = connection.execute("SELECT value FROM engineering_metadata WHERE key='installation.instance_id'").fetchone()
        if receipt is None or instance is None or tuple(receipt) != tuple(row[k] for k in ("submission_id", "project_id", "repository_id")) + (instance[0],):
            raise ProgressError("PROGRESS_RUN_PROVENANCE_CONFLICT", 409)
        lineage = _lineage(connection, row)
    elif retry_parent is not None:
        parent_lineage = _lineage(connection, retry_parent)
        lineage = dict(state="PENDING_RUN", attempt_submission_id=row["submission_id"],
                       root_submission_id=parent_lineage["root_submission_id"], retry_parent_submission_id=retry_parent["submission_id"],
                       retry_parent_run_id=retry_parent["run_id"])
    progress, updated = _lifecycle(connection, run_id, row["execution_mode"]) if run_id else (
        dict(available=False, current_phase=None, activity="QUEUED" if row["state"] == "QUEUED" else "NOT_STARTED",
             wait_reason="DISPATCH_NOT_RECORDED", responsible_role="EXECUTION_HOST", steps=[], history=[], history_completeness="UNAVAILABLE"), None)
    qualification = connection.execute("SELECT substr(payload,1,262145),persisted_at FROM execution_run_qualification_snapshots WHERE run_id=?", (run_id,)).fetchone() if run_id else None
    qualified = _json(qualification[0]) if qualification else {}
    if qualified and qualified.get("run_id") != run_id:
        raise ProgressError("PROGRESS_QUALIFICATION_CONFLICT", 409)
    technical = progress.get("terminal_outcome") or row["dispatch_state"] or "NOT_STARTED"
    if technical not in {"NOT_STARTED", "CLAIMED", "RUNNING", "COMPLETE", "BLOCKED", "FAILED"}:
        technical = "UNAVAILABLE"
    if qualified.get("terminal_execution_state") is not None and qualified["terminal_execution_state"] != technical:
        raise ProgressError("PROGRESS_QUALIFICATION_CONFLICT", 409)
    disposition = row["state"] if row["state"] in {"QUEUED", "DEFERRED", "QUARANTINED", "DECLINED"} else "UNAVAILABLE"
    operation = connection.execute("""SELECT to_state,actor_reference,recorded_at FROM ep_queue_disposition_operations
        WHERE project_id=? AND submission_id=? ORDER BY recorded_at DESC,operation_id DESC LIMIT 1""", (row["project_id"], row["submission_id"])).fetchone()
    if run_id is None and disposition in {"DEFERRED", "QUARANTINED", "DECLINED"}:
        verified = operation is not None and operation[0] == disposition and operation[1] != "LEGACY_NOT_RECORDED"
        progress.update(activity="NOT_STARTED" if disposition == "DECLINED" else "WAITING" if verified else "UNKNOWN",
                        wait_reason="SUBMISSION_" + disposition if verified else "NOT_RECORDED",
                        responsible_role="OPERATOR" if verified else "UNKNOWN")
    result = {"technical_outcome": technical, "qualification": {
        key: qualified.get(key, "UNAVAILABLE") for key in ("required_validation_state", "run_qualification")},
        "forge_mission_acceptance": "NOT_OWNED_BY_EP"}
    qualification_states = {
        "required_validation_state": {"PASS", "FAIL", "UNRESOLVED", "WAITING", "UNAVAILABLE"},
        "run_qualification": {"QUALIFIED", "NOT_QUALIFIED", "UNAVAILABLE"},
    }
    for key, val in result["qualification"].items():
        if not isinstance(val, str) or val not in qualification_states[key]:
            raise ProgressError("PROGRESS_QUALIFICATION_CONFLICT", 409)
    if result["qualification"]["run_qualification"] == "QUALIFIED" and technical != "COMPLETE":
        raise ProgressError("PROGRESS_QUALIFICATION_CONFLICT", 409)
    summary = envelope["summary"] if envelope else None
    description_status = "AVAILABLE" if summary else "NOT_RECORDED"
    # A bounded public summary must never expose a host path. Withhold the
    # whole value when either path separator occurs, including quoted paths.
    if summary and ("/" in summary or "\\" in summary):
        summary = None
        description_status = "WITHHELD_PRIVACY"
    if run_id is None and row["state"] == "QUEUED":
        gate = connection.execute("SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE project_id=? AND state IN ('BLOCKED','FAILED') AND operator_resolution='OPEN' LIMIT 1", (row["project_id"],)).fetchone()
        if gate is not None:
            progress.update(activity="WAITING", wait_reason="PROJECT_OPERATOR_GATE", responsible_role="OPERATOR")
    return dict(submission_id=row["submission_id"], submission_disposition=disposition,
                project_id=row["project_id"], repository_id=row["repository_id"],
                producer_id=row["producer_id"], mission_id=raw["mission_id"], mission_revision=raw["mission_revision"],
                engineering_action_id=raw["action_id"], correlation_id=raw["correlation_id"], run_id=run_id,
                lineage=lineage, provenance=accepted, description=summary,
                description_status=description_status, progress=progress, result=result,
                source_updated_at=max(_timestamp(v) for v in (row["created_at"], row["dispatch_updated"], row["run_updated"], updated, qualification[1] if qualification else None, operation[2] if operation else None) if v is not None))


def _capture(connection, selection, project_id, mission_id, run_id, deadline):
    connection.row_factory = sqlite3.Row
    repositories, producers = selection["repository_ids"], selection["producer_ids"]
    rows = connection.execute("""SELECT s.submission_id,s.project_id,s.repository_id,s.producer_id,s.producer_type,
        s.producer_version,s.prompt_digest,substr(s.constraints,1,262145) AS constraints,s.correlation_id,s.mission_id,s.engineering_action_id,s.transport,s.state,s.created_at,
        d.run_id,d.repository_id AS dispatch_repository,d.state AS dispatch_state,d.updated_at AS dispatch_updated,
        r.project_id AS run_project,r.execution_mode,r.updated_at AS run_updated
        FROM ep_submissions s LEFT JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id
        LEFT JOIN ep_execution_runs r ON r.run_id=d.run_id
        WHERE s.project_id=? AND s.mission_id=? AND s.repository_id IN (""" + ",".join("?" for _ in repositories) + ") AND s.producer_id IN (" + ",".join("?" for _ in producers) + ") ORDER BY s.created_at,s.submission_id LIMIT ?",
        (project_id, mission_id, *repositories, *producers, MAX_RECORDS + 1)).fetchall() if run_id is None else connection.execute("""SELECT s.submission_id,s.project_id,s.repository_id,s.producer_id,s.producer_type,
        s.producer_version,s.prompt_digest,substr(s.constraints,1,262145) AS constraints,s.correlation_id,s.mission_id,s.engineering_action_id,s.transport,s.state,s.created_at,
        d.run_id,d.repository_id AS dispatch_repository,d.state AS dispatch_state,d.updated_at AS dispatch_updated,
        r.project_id AS run_project,r.execution_mode,r.updated_at AS run_updated
        FROM ep_submissions s JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id
        JOIN ep_execution_runs r ON r.run_id=d.run_id WHERE d.run_id=? AND s.project_id=? AND s.mission_id=? LIMIT 2""", (run_id, project_id, mission_id)).fetchall()
    if len(rows) > MAX_RECORDS:
        raise ProgressError("PROGRESS_RECORD_LIMIT", 409)
    records, unlinked = [], 0
    for row in rows:
        if time.monotonic() > deadline:
            raise ProgressError("PROGRESS_DEADLINE", 503)
        if row["repository_id"] not in selection["repository_ids"] or row["producer_id"] not in selection["producer_ids"]:
            continue
        value = _record(connection, row)
        if value is None:
            unlinked += 1
        else:
            records.append(value)
    return records, unlinked


def read(data_root: Path, *, token, headers, query, project_id, run_id, publish, expected_source_identity=None):
    """One consistent bounded generation, then fresh current publication checks.

    No transaction or authority lock spans serialization or socket publication.
    The open monitor connection's data_version conservatively detects any
    committed CENTRAL change; no snapshot is silently mixed across reads.
    """
    if headers.get("EP-Progress-Contract") != "1.0":
        raise ProgressError("PROGRESS_CONTRACT_UNSUPPORTED", 422)
    if set(query) - {"mission_id", "expected_snapshot"} or any(len(v) != 1 for v in query.values()):
        raise ProgressError("PROGRESS_QUERY_INVALID")
    project_id = authority._id(project_id)
    mission_id = authority._id(query.get("mission_id", [None])[0])
    if run_id is not None:
        authority._id(run_id)
    path = data_root / "epdata.sqlite"
    identity = source_identity(path)
    if expected_source_identity is not None and identity != expected_source_identity:
        raise ProgressError("PROGRESS_SOURCE_CONFLICT", 409)
    deadline = time.monotonic() + 3
    with storage.sqlite_read_connection(path) as monitor:
        monitor.execute("PRAGMA busy_timeout=200")
        monitor.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        generation = monitor.execute("PRAGMA data_version").fetchone()[0]
        monitor.execute("BEGIN")
        scope = authority._authenticate(monitor, token, headers)
        source_generation = _generation(path, identity)
        selection = next((s for s in scope["selections"] if s["project_id"] == project_id), None)
        if selection is None or mission_id not in selection["mission_ids"]:
            raise ProgressError("PROGRESS_SELECTION_DENIED", 403)
        records, unlinked = _capture(monitor, selection, project_id, mission_id, run_id, deadline)
        monitor.rollback()
        snapshot = hashlib.sha256(authority._canonical([identity, source_generation, scope, selection, mission_id])).hexdigest()
        if "expected_snapshot" in query and query["expected_snapshot"][0] != snapshot:
            raise ProgressError("PROGRESS_SNAPSHOT_CONFLICT", 409)
        if run_id is not None:
            records = [r for r in records if r["run_id"] == run_id]
            if len(records) != 1:
                raise ProgressError("PROGRESS_RUN_NOT_FOUND", 404)
        payload = dict(contract_version="1.0", producer_version=CURRENT_PLATFORM_VERSION,
                       instance_id=scope["instance_id"], purpose="EXECUTION_PROGRESS_READ", snapshot_id=snapshot,
                       observed_at=authority._now().isoformat(), source_updated_at=max((r["source_updated_at"] for r in records), default=None),
                       freshness="AS_OF_CENTRAL_SNAPSHOT_NOT_EXECUTOR_LIVENESS", selection=selection,
                       selected_mission_id=mission_id, completeness="COMPLETE_WITHIN_KNOWN_SCOPED_SUBMISSIONS",
                       selected_run_id=run_id,
                       unlinked_legacy_records=unlinked, logical_action_count=len({(r["producer_id"], r["mission_id"], r["engineering_action_id"]) for r in records}),
                       records=records, business_mutations=0)
        if run_id is not None:
            payload["completeness"] = "EXACT_RUN"
        if len(json.dumps(payload, sort_keys=True).encode("utf-8")) > MAX_BYTES:
            raise ProgressError("PROGRESS_PAYLOAD_LIMIT", 409)
        def check():
            if time.monotonic() > deadline:
                raise ProgressError("PROGRESS_DEADLINE", 503)
            with storage.sqlite_read_connection(path) as current:
                current.execute("PRAGMA busy_timeout=200")
                current.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                if authority._authenticate(current, token, headers) != scope:
                    raise ProgressError("PROGRESS_SCOPE_CHANGED", 403)
            current_identity = source_identity(path)
            if current_identity != identity or monitor.execute("PRAGMA data_version").fetchone()[0] != generation:
                raise ProgressError("PROGRESS_SNAPSHOT_CONFLICT", 409)
            if authority._now() >= datetime.fromisoformat(scope["expires_at"]):
                raise ProgressError("PROGRESS_INACTIVE", 401)
            if time.monotonic() > deadline:
                raise ProgressError("PROGRESS_DEADLINE", 503)
        check()
        publish(payload, str(scope["instance_id"]), check)
