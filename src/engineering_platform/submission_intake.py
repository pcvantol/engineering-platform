"""Deterministic, transport-neutral Human Intent normalization.

This module parses explicit metadata only.  It never selects a repository,
opens storage, dispatches a run, or interprets product intent with an AI.
"""
from __future__ import annotations

from pathlib import Path


class SubmissionIntakeError(ValueError):
    pass


def normalize_human_file(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionIntakeError("UNSUPPORTED_ENCODING") from error
    if not text.strip() or not text.startswith("---\n"):
        raise SubmissionIntakeError("HUMAN_METADATA_REQUIRED")
    try:
        header, body = text[4:].split("\n---\n", 1)
    except ValueError as error:
        raise SubmissionIntakeError("MALFORMED_HUMAN_METADATA") from error
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            raise SubmissionIntakeError("MALFORMED_HUMAN_METADATA")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in metadata or key not in {"project", "repository", "mode", "target"} or not value:
            raise SubmissionIntakeError("MALFORMED_HUMAN_METADATA")
        metadata[key] = value
    if set(metadata) - {"project", "repository", "mode", "target"} or not {"project", "repository", "mode"} <= set(metadata):
        raise SubmissionIntakeError("HUMAN_METADATA_REQUIRED")
    mode = metadata["mode"].upper()
    if mode not in {"MANAGED", "GENESIS"} or (mode == "GENESIS") != ("target" in metadata):
        raise SubmissionIntakeError("INVALID_HUMAN_MODE_OR_TARGET")
    if not body.strip():
        raise SubmissionIntakeError("EMPTY_HUMAN_INTENT")
    prompt = f"Execution Mode: {mode}\n"
    if mode == "GENESIS":
        prompt += f"Target repository: {metadata['target']}\n"
    prompt += "\n" + body.strip() + "\n"
    return {"project_id": metadata["project"], "submission": {"repository_id": metadata["repository"], "producer": {"id": "file-human", "type": "HUMAN", "version": "submission-intake-v1"}, "prompt": prompt, "constraints": {"normalization": "submission-intake-v1", "mode": mode}}}
