"""Validated, atomic local checkpoints for bounded engineering transactions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from .storage import EngineeringStorageError, open_storage


SCHEMA_VERSION = 1
PHASES = frozenset({"INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT", "FINALIZE_AGENT", "WAIT_FOR_TERMINAL_EVIDENCE", "WAIT_FOR_OPERATOR_MERGE", "REPOSITORY_CLEANUP", "COMPLETE", "BLOCKED", "FAILED"})
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DIAGNOSTIC_LENGTH = 500
SENSITIVE_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|oauth|access[_ -]?token|refresh[_ -]?token|secret|cookie|authorization|password)\b\s*[:=]\s*\S+|\bbearer\s+\S+|\b[A-Z][A-Z0-9_]{2,}\s*=\s*\S+"
)


class StateError(ValueError):
    """Raised when local advisory state is missing, malformed, or unsafe."""


def redact_diagnostic(value: str, *, limit: int = MAX_DIAGNOSTIC_LENGTH) -> str:
    """Return bounded human-readable text that is safe to display or persist."""
    if not isinstance(value, str):
        return "Diagnostic omitted because it was not valid text."
    compact = " ".join(value.replace("\x00", " ").split())
    compact = SENSITIVE_DIAGNOSTIC_PATTERN.sub("[REDACTED]", compact)
    return compact[:limit]


@dataclass(frozen=True)
class TransactionState:
    run_id: str
    repository: str
    prompt_path: str
    phase: str
    branch: str | None = None
    pull_request: int | None = None
    last_verified_sha: str | None = None
    next_action: str = "invoke_agent"
    terminal_condition: str = "repository_reconciled"
    diagnostic: str | None = None
    owner_authorized: bool = False
    transaction_kind: str = "IMPLEMENTATION"
    execution_mode: str = "MANAGED"
    genesis_repository_path: str | None = None
    genesis_commit_sha: str | None = None
    implementation_branch: str | None = None
    implementation_pull_request: int | None = None
    implementation_head_sha: str | None = None
    implementation_merge_commit: str | None = None
    finalization_branch: str | None = None
    finalization_pull_request: int | None = None
    finalization_head_sha: str | None = None
    finalization_merge_commit: str | None = None
    latest_repository_evidence: str | None = None
    latest_github_evidence: str | None = None
    agent_execution_seconds: float | None = None
    validation_evidence: tuple[dict[str, str], ...] = ()
    repair_iterations: int = 0
    repair_audit: tuple[dict[str, str], ...] = ()
    waiting_for_merge_since: str | None = None
    terminal: bool = False
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: object) -> "TransactionState":
        if not isinstance(raw, dict):
            raise StateError("checkpoint must be a JSON object")
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        defaults = {
            "diagnostic": None, "owner_authorized": False, "transaction_kind": "IMPLEMENTATION",
            "execution_mode": "MANAGED", "genesis_repository_path": None, "genesis_commit_sha": None,
            "implementation_branch": None, "implementation_pull_request": None,
            "implementation_head_sha": None, "implementation_merge_commit": None,
            "finalization_branch": None, "finalization_pull_request": None,
            "finalization_head_sha": None, "finalization_merge_commit": None,
            "latest_repository_evidence": None, "latest_github_evidence": None,
            "agent_execution_seconds": None,
            "validation_evidence": (),
            "repair_iterations": 0,
            "repair_audit": (),
            "waiting_for_merge_since": None,
        }
        if set(raw).issubset(expected) and set(raw) | set(defaults) == expected:
            raw = {**defaults, **raw}
        elif set(raw) != expected:
            raise StateError("checkpoint fields are incompatible")
        if isinstance(raw.get("validation_evidence"), list):
            raw = {**raw, "validation_evidence": tuple(raw["validation_evidence"])}
        if isinstance(raw.get("repair_audit"), list):
            raw = {**raw, "repair_audit": tuple(raw["repair_audit"])}
        try:
            state = cls(**raw)
        except TypeError as error:
            raise StateError("checkpoint fields are invalid") from error
        if state.schema_version != SCHEMA_VERSION:
            raise StateError("unsupported checkpoint schema version")
        if not RUN_ID_PATTERN.fullmatch(state.run_id):
            raise StateError("checkpoint run_id is invalid")
        if not all(isinstance(value, str) and value for value in (state.repository, state.prompt_path, state.phase, state.next_action, state.terminal_condition)):
            raise StateError("checkpoint identity or phase is invalid")
        if state.phase not in PHASES or (state.branch is not None and not isinstance(state.branch, str)):
            raise StateError("checkpoint phase or branch is invalid")
        if state.pull_request is not None and (not isinstance(state.pull_request, int) or state.pull_request < 1):
            raise StateError("checkpoint pull_request is invalid")
        if state.last_verified_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", state.last_verified_sha):
            raise StateError("checkpoint last_verified_sha is invalid")
        if state.diagnostic is not None and (not isinstance(state.diagnostic, str) or not state.diagnostic or state.diagnostic != redact_diagnostic(state.diagnostic)):
            raise StateError("checkpoint diagnostic is invalid or unsafe")
        if not isinstance(state.owner_authorized, bool) or state.transaction_kind not in {"IMPLEMENTATION", "FINALIZATION"} or state.execution_mode not in {"MANAGED", "GENESIS"}:
            raise StateError("checkpoint authorization or transaction kind is invalid")
        if state.genesis_repository_path is not None and (not isinstance(state.genesis_repository_path, str) or not Path(state.genesis_repository_path).is_absolute()):
            raise StateError("genesis repository path is invalid")
        if state.genesis_commit_sha is not None and (not isinstance(state.genesis_commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", state.genesis_commit_sha)):
            raise StateError("genesis commit SHA is invalid")
        optional_sha_fields = (state.implementation_head_sha, state.implementation_merge_commit, state.finalization_head_sha, state.finalization_merge_commit)
        if any(value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)) for value in optional_sha_fields):
            raise StateError("checkpoint lifecycle SHA is invalid")
        optional_pr_fields = (state.implementation_pull_request, state.finalization_pull_request)
        if any(value is not None and (not isinstance(value, int) or value < 1) for value in optional_pr_fields):
            raise StateError("checkpoint lifecycle pull request is invalid")
        if not isinstance(state.repair_iterations, int) or state.repair_iterations < 0:
            raise StateError("checkpoint repair iteration count is invalid")
        audit_fields = {"iteration", "observed_at", "failed_checks", "proposed_action", "agent_summary", "commit_sha", "outcome"}
        if (
            not isinstance(state.repair_audit, tuple)
            or len(state.repair_audit) > 3
            or any(
                not isinstance(item, dict) or set(item) != audit_fields
                or not all(isinstance(value, str) and value and len(value) <= MAX_DIAGNOSTIC_LENGTH and value == redact_diagnostic(value) for value in item.values())
                or not item["iteration"].isdigit() or int(item["iteration"]) < 1
                or item["outcome"] not in {"submitted_for_recheck", "agent_failed"}
                or (item["commit_sha"] != "not_recorded" and not re.fullmatch(r"[0-9a-f]{40}", item["commit_sha"]))
                for item in state.repair_audit
            )
        ):
            raise StateError("checkpoint repair audit is invalid or unsafe")
        if state.waiting_for_merge_since is not None and (
            not isinstance(state.waiting_for_merge_since, str)
            or len(state.waiting_for_merge_since) > 80
        ):
            raise StateError("checkpoint merge wait timestamp is invalid")
        for value in (state.latest_repository_evidence, state.latest_github_evidence):
            if value is not None and (not isinstance(value, str) or len(value) > MAX_DIAGNOSTIC_LENGTH or value != redact_diagnostic(value)):
                raise StateError("checkpoint evidence is invalid or unsafe")
        if state.agent_execution_seconds is not None and (
            isinstance(state.agent_execution_seconds, bool)
            or not isinstance(state.agent_execution_seconds, (int, float))
            or not 0 <= state.agent_execution_seconds <= 86_400
        ):
            raise StateError("checkpoint agent execution duration is invalid")
        if (
            not isinstance(state.validation_evidence, tuple)
            or len(state.validation_evidence) > 12
            or any(
                not isinstance(item, dict)
                or set(item) != {"command", "result"}
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > 240
                    or value != redact_diagnostic(value, limit=240)
                    for value in item.values()
                )
                for item in state.validation_evidence
            )
        ):
            raise StateError("checkpoint validation evidence is invalid or unsafe")
        if not isinstance(state.terminal, bool) or state.terminal != (state.phase in {"COMPLETE", "BLOCKED", "FAILED"}):
            raise StateError("checkpoint terminal flag conflicts with phase")
        return state

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    """Stores canonical transaction state in SQLite and emits JSON projections.

    The run JSON files are intentionally retained for recovery tooling and
    operator inspection, but are never consulted for normal lifecycle reads.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise StateError("run_id must use lowercase letters, digits, and hyphens")
        return self.directory / f"{run_id}.json"

    @property
    def root(self) -> Path:
        # StateStore is constructed with ``<root>/.engineering/engineering-runs``.
        return self.directory.parent.parent

    def load(self, run_id: str) -> TransactionState:
        path = self.path_for(run_id)
        try:
            connection = open_storage(self.root, create=False)
            try:
                row = connection.execute(
                    "SELECT payload FROM engineering_transactions WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                connection.close()
        except EngineeringStorageError as error:
            raise StateError("canonical engineering storage is unavailable") from error
        if not row:
            raise StateError(f"no checkpoint exists for run_id {run_id}")
        try:
            return TransactionState.from_dict(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError) as error:
            raise StateError("canonical checkpoint is corrupt") from error

    def save(self, state: TransactionState) -> Path:
        path = self.path_for(state.run_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        canonical = json.dumps(state.to_dict(), separators=(",", ":"), sort_keys=True)
        try:
            connection = open_storage(self.root)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload,phase=excluded.phase,updated_at=excluded.updated_at",
                    (state.run_id, canonical, state.phase),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (state.run_id, state.phase, canonical),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        except (EngineeringStorageError, OSError) as error:
            raise StateError("canonical engineering storage could not save checkpoint") from error
        descriptor, temporary = tempfile.mkstemp(prefix=f".{state.run_id}.", suffix=".tmp", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        if state.phase == "COMPLETE":
            self._prune_completed_history()
        return path

    def remove(self, run_id: str) -> None:
        path = self.path_for(run_id)
        try:
            connection = open_storage(self.root, create=False)
            try:
                connection.execute("DELETE FROM engineering_transactions WHERE run_id=?", (run_id,))
            finally:
                connection.close()
        except EngineeringStorageError as error:
            raise StateError("canonical engineering storage is unavailable") from error
        if path.exists():
            path.unlink()

    def _prune_completed_history(self) -> None:
        try:
            connection = open_storage(self.root, create=False)
            try:
                rows = connection.execute(
                    "SELECT run_id FROM engineering_transactions WHERE phase='COMPLETE' ORDER BY updated_at DESC, run_id DESC LIMIT -1 OFFSET 10"
                ).fetchall()
            finally:
                connection.close()
        except EngineeringStorageError:
            return
        # Removing a compatibility projection must never erase canonical state.
        for (run_id,) in rows:
            self.path_for(str(run_id)).unlink(missing_ok=True)
