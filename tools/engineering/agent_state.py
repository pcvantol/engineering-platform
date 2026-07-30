"""Validated, atomic local checkpoints for bounded engineering transactions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile


SCHEMA_VERSION = 1
PHASES = frozenset({"INITIALIZE", "EXECUTE_AGENT", "WAIT_FOR_TERMINAL_EVIDENCE", "COMPLETE", "BLOCKED", "FAILED"})
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class StateError(ValueError):
    """Raised when local advisory state is missing, malformed, or unsafe."""


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
    terminal: bool = False
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: object) -> "TransactionState":
        if not isinstance(raw, dict):
            raise StateError("checkpoint must be a JSON object")
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        if set(raw) != expected:
            raise StateError("checkpoint fields are incompatible")
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
        if not isinstance(state.terminal, bool) or state.terminal != (state.phase in {"COMPLETE", "BLOCKED", "FAILED"}):
            raise StateError("checkpoint terminal flag conflicts with phase")
        return state

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    """Stores advisory state below an ignored, repository-local directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise StateError("run_id must use lowercase letters, digits, and hyphens")
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> TransactionState:
        path = self.path_for(run_id)
        if not path.is_file():
            raise StateError(f"no checkpoint exists for run_id {run_id}")
        try:
            return TransactionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"checkpoint cannot be read: {path}") from error

    def save(self, state: TransactionState) -> Path:
        path = self.path_for(state.run_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
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
        if path.exists():
            path.unlink()

    def _prune_completed_history(self) -> None:
        completed: list[Path] = []
        for candidate in self.directory.glob("*.json"):
            try:
                if TransactionState.from_dict(json.loads(candidate.read_text(encoding="utf-8"))).phase == "COMPLETE":
                    completed.append(candidate)
            except (OSError, json.JSONDecodeError, StateError):
                # Unknown local content is preserved for fail-closed diagnosis.
                continue
        for candidate in sorted(completed, key=lambda item: item.stat().st_mtime, reverse=True)[10:]:
            candidate.unlink()
