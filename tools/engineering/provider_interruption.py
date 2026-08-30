"""Recovery of provider-proven interrupted transactions after host exit."""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from pathlib import Path

from .agent_state import StateError, StateStore, TransactionState
from .execution_lease import release_terminal_lease
from .execution_timing import reconcile_interrupted_phases
from .live_status import write_live_status
from .storage import open_storage


INTERRUPTION_CLASSIFICATION = "provider_turn_interrupted"
TERMINAL_DIAGNOSTIC = (
    "Provider turn interrupted before returning the required structured AgentResult."
)
LOGGER = logging.getLogger(__name__)


def _latest_interrupted_invocation(root: Path, run_id: str) -> tuple[str, str] | None:
    """Return only durable, allow-listed provider interruption evidence.

    Some providers terminate after streaming an interrupted child command but
    before emitting their final JSONL ``turn_aborted`` event. That remains a
    proven interruption only when usage is unavailable and a child span was
    interrupted under the same provider boundary.
    """
    try:
        connection = open_storage(root)
        try:
            row = connection.execute(
                "SELECT invocation_id,churn,usage_authority FROM provider_invocations WHERE run_id=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
    except Exception:
        return None
    if row is None:
        return None
    invocation_id = str(row[0]) if isinstance(row[0], str) else None
    if invocation_id is None:
        return None
    try:
        churn = json.loads(row[1])
    except (TypeError, json.JSONDecodeError):
        churn = {}
    if isinstance(churn, dict) and churn.get("interruption_classification") == INTERRUPTION_CLASSIFICATION:
        return invocation_id, "provider_reported_abort"
    if row[2] != "UNAVAILABLE":
        return None
    try:
        connection = open_storage(root)
        try:
            interrupted_child = connection.execute(
                "SELECT 1 FROM execution_phase_spans AS child "
                "JOIN execution_phase_spans AS provider ON provider.phase_id=child.parent_phase_id "
                "WHERE child.run_id=? AND child.outcome='INTERRUPTED' "
                "AND provider.phase_name='PROVIDER_EXECUTION' LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
    except Exception:
        return None
    if interrupted_child is None:
        return None
    return invocation_id, "interrupted_child_span_without_provider_result"


def terminalize_after_host_exit(root: Path, run_id: str) -> TransactionState | None:
    """Close a non-terminal run only when its latest provider evidence proves interruption.

    This is intentionally a watcher-side recovery boundary: the detached
    Execution Host has already exited, so releasing its lease cannot terminate
    active provider work.  Generic stale leases remain recoverable and are not
    converted into failures here.
    """
    evidence = _latest_interrupted_invocation(root, run_id)
    if evidence is None:
        return None
    invocation_id, classification = evidence
    store = StateStore(root / ".engineering" / "engineering-runs")
    try:
        state = store.load(run_id)
    except StateError:
        return None
    if state.terminal:
        return state
    terminal = replace(
        state,
        phase="FAILED",
        terminal=True,
        next_action="NONE",
        terminal_condition=INTERRUPTION_CLASSIFICATION,
        diagnostic=(
            f"{TERMINAL_DIAGNOSTIC} Provider invocation: {invocation_id}. "
            f"Interruption evidence: {classification}."
        ),
    )
    store.save(terminal)
    reconcile_interrupted_phases(root, run_id, outcome="INTERRUPTED")
    # The checkpoint is durable before cleanup. A cleanup failure therefore
    # cannot overwrite the proven failure outcome.
    try:
        release_terminal_lease(root, run_id)
    except Exception:
        # Lease cleanup is secondary evidence.  The durable checkpoint stays
        # authoritative and normal stale-lease reconciliation can record the
        # separate cleanup concern on a later cycle.
        LOGGER.exception("Terminal provider-interruption lease release failed for run %s", run_id)
    write_live_status(root, terminal, terminal.next_action)
    return terminal
