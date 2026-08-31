"""Strict iCloud text convenience ingress for canonical HUMAN submissions.

This is an adapter, not an execution path. It turns a stable post-activation
``.txt`` source into the structured HUMAN envelope used by workspace_inbox_api.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from .workspace_inbox_api import WorkspaceInboxSubmissionError, submit_human


DEFAULT_PRODUCER_IDENTITY = "operator-peter"
ACTIVATION_FILENAME = "activation.json"
AUDIT_DIRECTORY = "human-text-ingress"
SUPPORTED_ACTION_INTENTS = frozenset({"MUTATING_DELIVERY", "VALIDATION_ONLY"})
SUPPORTED_HEADER_KEYS = frozenset({"action_intent", "validation_profile"})


class HumanTextIngressError(ValueError):
    """A deterministic source rejection that must never be submitted."""


@dataclass(frozen=True)
class TextSubmission:
    prompt: str
    action_intent: str
    validation_profile: str | None


def producer_identity() -> str:
    """Read local operator configuration without coupling core to a user."""
    return os.environ.get("DJCONNECT_ENGINEERING_HUMAN_INGRESS_PRODUCER_ID", DEFAULT_PRODUCER_IDENTITY)


def parse_text_submission(content: str) -> TextSubmission:
    """Parse only an exact leading metadata block; never inspect prompt prose."""
    if not isinstance(content, str) or not content.strip():
        raise HumanTextIngressError("text_source_empty")
    if not content.startswith("---\n"):
        return TextSubmission(content, "MUTATING_DELIVERY", None)
    end = content.find("\n---\n", 4)
    if end < 0:
        raise HumanTextIngressError("metadata_header_malformed")
    header, prompt = content[4:end], content[end + 5 :]
    if not prompt.strip():
        raise HumanTextIngressError("text_source_empty")
    values: dict[str, str] = {}
    for line in header.splitlines():
        if line.count(":") != 1:
            raise HumanTextIngressError("metadata_header_malformed")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or not value:
            raise HumanTextIngressError("metadata_header_malformed")
        if key not in SUPPORTED_HEADER_KEYS:
            raise HumanTextIngressError("metadata_header_unknown_key")
        if key in values:
            raise HumanTextIngressError("metadata_header_duplicate_key")
        values[key] = value
    action_intent = values.get("action_intent", "MUTATING_DELIVERY")
    if action_intent not in SUPPORTED_ACTION_INTENTS:
        raise HumanTextIngressError("metadata_header_unknown_action_intent")
    validation_profile = values.get("validation_profile")
    if action_intent == "VALIDATION_ONLY" and validation_profile is None:
        raise HumanTextIngressError("validation_profile_required")
    return TextSubmission(prompt, action_intent, validation_profile)


def _source_identity(source: Path, content: str) -> str:
    """Bind retries to one stable observed filesystem source and bytes."""
    stat = source.stat()
    material = "\0".join((str(source.resolve()), str(stat.st_dev), str(stat.st_ino), str(stat.st_size), str(stat.st_mtime_ns), content))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _state_directory(repo: Path) -> Path:
    directory = repo / ".engineering" / AUDIT_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _archive(source: Path, directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / source.name
    if target.exists():
        target = directory / f"{source.stem}-{uuid.uuid4().hex[:8]}{source.suffix}"
    os.replace(source, target)
    return target


def _activation(repo: Path, inbox: Path) -> dict[str, object] | None:
    path = _state_directory(repo) / ACTIVATION_FILENAME
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    baseline: list[str] = []
    for source in inbox.iterdir():
        if source.suffix.lower() != ".txt":
            continue
        try:
            baseline.append(_source_identity(source, source.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    payload: dict[str, object] = {"activated_at": datetime.now(timezone.utc).isoformat(), "legacy_source_identities": sorted(baseline)}
    _atomic_json(path, payload)
    return payload


def ingest(repo: Path, inbox: Path, *, read_source: object) -> int:
    """Convert stable post-activation text sources; only JSON reaches watcher claim."""
    activation = _activation(repo, inbox)
    if activation is None:
        return 0
    legacy = set(activation.get("legacy_source_identities", []))
    accepted = rejected = 0
    for source in sorted(inbox.glob("*.txt"), key=lambda path: (path.stat().st_mtime_ns, path.name)):
        content = read_source(source)  # type: ignore[operator]
        if content is None:
            continue
        try:
            ingestion_id = _source_identity(source, content)
        except OSError:
            continue
        if ingestion_id in legacy:
            continue
        audit = _state_directory(repo) / f"{ingestion_id}.json"
        try:
            parsed = parse_text_submission(content)
            receipt = submit_human(
                repo, prompt=parsed.prompt, title=source.stem, producer_identity=producer_identity(), action_intent=parsed.action_intent,
                validation_profile=parsed.validation_profile, submission_id=f"human-ingress-{ingestion_id[:48]}",
            )
            archive = _archive(source, inbox.parent / "Accepted")
            _atomic_json(audit, {"ingestion_id": ingestion_id, "detected_at": datetime.now(timezone.utc).isoformat(), "state": "SUBMITTED", "source_path": str(source), "source_archive": str(archive), "submission_id": receipt.submission_id, "envelope_path": str(receipt.inbox / receipt.filename), "action_intent": parsed.action_intent, "validation_profile": parsed.validation_profile})
            accepted += 1
        except HumanTextIngressError as error:
            try:
                archive = _archive(source, inbox.parent / "Rejected")
                _atomic_json(audit, {"ingestion_id": ingestion_id, "detected_at": datetime.now(timezone.utc).isoformat(), "state": "REJECTED", "source_path": str(source), "source_archive": str(archive), "rejection_reason": str(error)})
                rejected += 1
            except OSError:
                continue
        except (OSError, WorkspaceInboxSubmissionError):
            # The deterministic submission identity makes recovery safe after
            # persistence, publication or source-archive failure.
            continue
    return accepted + rejected
