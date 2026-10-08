"""Producer-owned, explicit effects for one existing managed execution.

The contract grants neither provider-side filesystem writes nor remote access.
It bounds the ordinary file proposals the host may subsequently publish.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Mapping


VERSION = "1.0"
MODES = frozenset({"READ_ONLY_ASSESSMENT", "DOCUMENTATION_ONLY",
                   "ARCHITECTURE_DESIGN_ONLY", "BOUNDED_REPOSITORY_CHANGE"})
_CONTROL = frozenset({".git", ".codex", ".agents", ".engineering", ".ssh", ".aws",
                      ".env", ".gitconfig", ".gitattributes", ".gitmodules", "agents.md"})
_DOCUMENT_SUFFIXES = frozenset({".md", ".txt", ".rst", ".adoc", ".mmd", ".puml"})
MAX_RESULT_BYTES = 1_048_576
_SECRET = re.compile(
    r"(?i)-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-[a-z0-9_-]{12,}|ghp_[a-z0-9]{12,}|github_pat_[a-z0-9_]{12,})\b|"
    r"\bbearer\s+[a-z0-9._~+/=-]{8,}|https?://[^\s/@:]+:[^\s/@]+@|"
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)


CREDENTIAL_SHAPE_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)

class EffectContractError(ValueError):
    """An explicit effect grant or its proposed result is invalid."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode("ascii")).hexdigest()


def require_redacted(value: object) -> None:
    """Reject detectable secrets before artifact persistence; never rewrite evidence."""
    text = json.dumps(value, ensure_ascii=False)
    if _SECRET.search(text) or CREDENTIAL_SHAPE_PATTERN.search(text):
        raise EffectContractError("EFFECT_SENSITIVE_CONTENT_REJECTED")


def safe_path(value: object, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise EffectContractError("INVALID_EFFECT_PATH")
    path = value[:-1] if directory and value.endswith("/") else value
    parts = path.split("/")
    if (any(not part or part in {".", ".."} or part.casefold() in _CONTROL
            or part.endswith((".", " ")) or part.casefold().startswith(".env.")
            for part in parts) or "\\" in path or ":" in path
            or any(ord(char) < 32 or ord(char) > 126 for char in path)
            or any(char in path for char in "*?[]")):
        raise EffectContractError("INVALID_EFFECT_PATH")
    return value


def contains(scopes: list[str], path: str) -> bool:
    return any(path == scope or (scope.endswith("/") and path.startswith(scope))
               for scope in scopes)


def parse(constraints: Mapping[str, object] | None) -> dict[str, object] | None:
    if constraints is None or "effect_contract" not in constraints:
        return None
    raw = constraints["effect_contract"]
    required = {"contract_version", "mode", "delivery", "source_revision",
                "read_paths", "write_paths", "criteria"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise EffectContractError("INVALID_EFFECT_CONTRACT")
    if (raw["contract_version"] != VERSION or not isinstance(raw["mode"], str)
            or raw["mode"] not in MODES or raw["delivery"] not in ("EVIDENCE_ONLY", "GIT")
            or not isinstance(raw["source_revision"], str)
            or re.fullmatch(r"[0-9a-f]{40}", raw["source_revision"]) is None):
        raise EffectContractError("INVALID_EFFECT_CONTRACT")
    for key in ("read_paths", "write_paths"):
        paths = raw[key]
        if not isinstance(paths, list) or len(paths) > 64:
            raise EffectContractError("INVALID_EFFECT_SCOPE")
        for path in paths:
            safe_path(path, directory=True)
        if len({path.casefold() for path in paths}) != len(paths):
            raise EffectContractError("DUPLICATE_EFFECT_PATH")
    if not raw["read_paths"]:
        raise EffectContractError("MISSING_EFFECT_READ_SCOPE")
    evidence_only = raw["delivery"] == "EVIDENCE_ONLY"
    if (evidence_only != (not raw["write_paths"])
            or (raw["mode"] == "READ_ONLY_ASSESSMENT" and not evidence_only)
            or (raw["mode"] in {"DOCUMENTATION_ONLY", "BOUNDED_REPOSITORY_CHANGE"}
                and evidence_only)):
        raise EffectContractError("INCOMPATIBLE_EFFECT_DELIVERY")
    criteria = raw["criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 16:
        raise EffectContractError("INVALID_EFFECT_CRITERIA")
    seen = set()
    for criterion in criteria:
        if (not isinstance(criterion, dict) or set(criterion) != {"id", "description"}
                or not isinstance(criterion["id"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", criterion["id"]) is None
                or criterion["id"] in seen or not isinstance(criterion["description"], str)
                or not 20 <= len(criterion["description"].strip()) <= 1000):
            raise EffectContractError("INVALID_EFFECT_CRITERIA")
        seen.add(criterion["id"])
    # A JSON round trip prevents caller mutation after validation.
    return json.loads(json.dumps(raw))


def validate_result(contract: dict[str, object], raw: object,
                    source_paths: set[str]) -> dict[str, object]:
    """Check syntax and scope; independent quality/security decide usefulness."""
    if (not isinstance(raw, dict) or set(raw) != {"summary", "criteria", "files"}
            or len(json.dumps(raw).encode()) > MAX_RESULT_BYTES
            or not isinstance(raw["summary"], str) or not 40 <= len(raw["summary"]) <= 16000
            or not isinstance(raw["criteria"], list) or not isinstance(raw["files"], list)
            or len(raw["files"]) > 64):
        raise EffectContractError("INVALID_EFFECT_RESULT")
    expected = {item["id"] for item in contract["criteria"]}
    seen = set()
    for item in raw["criteria"]:
        if (not isinstance(item, dict) or set(item) != {"id", "status", "analysis", "source_paths"}
                or not isinstance(item["id"], str) or item["id"] not in expected
                or item["id"] in seen or item["status"] != "SATISFIED"
                or not isinstance(item["analysis"], str) or not 40 <= len(item["analysis"]) <= 16000
                or not isinstance(item["source_paths"], list) or not item["source_paths"]
                or any(not isinstance(path, str) or path not in source_paths
                       for path in item["source_paths"])):
            raise EffectContractError("UNSATISFIED_EFFECT_CRITERION")
        seen.add(item["id"])
    if seen != expected:
        raise EffectContractError("MISSING_EFFECT_CRITERION")
    paths = set()
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise EffectContractError("INVALID_EFFECT_FILE")
        path = safe_path(item["path"])
        if not contains(contract["write_paths"], path) or path.casefold() in paths:
            raise EffectContractError("EFFECT_WRITE_OUTSIDE_SCOPE")
        if (not isinstance(item["content"], str) or "\x00" in item["content"]
                or not item["content"].strip()):
            raise EffectContractError("INVALID_EFFECT_FILE")
        if (contract["mode"] in {"DOCUMENTATION_ONLY", "ARCHITECTURE_DESIGN_ONLY"}
                and PurePosixPath(path).suffix.casefold() not in _DOCUMENT_SUFFIXES):
            raise EffectContractError("NON_DOCUMENT_EFFECT")
        paths.add(path.casefold())
    if bool(raw["files"]) != (contract["delivery"] == "GIT"):
        raise EffectContractError("EMPTY_OR_FORBIDDEN_EFFECT_OUTPUT")
    require_redacted(raw)
    return json.loads(json.dumps(raw))
