"""Authority-free PA-E0 compatibility for Forge's parallel Action peer graph.

The only input from Forge is its versioned graph.  Scope is supplied separately
by an EP caller; matching that scope is not a repository grant or admission.
This module has no storage, network, provider, or execution dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any


PRODUCER_VERSION = "parallel-action-graph/v1"
READBACK_VERSION = "ep-parallel-action-compat/v1"
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_ACTIONS = 256
_MAX_ARRAYS = MAX_ACTIONS + 1
_MAX_ARRAY_ITEMS = MAX_ACTIONS
_MAX_OBJECT_FIELDS = 8  # twice the largest producer object
_MAX_STRING_BYTES = 6 * 128  # a 128-character identifier with every byte as \uXXXX
_MAX_DEPTH = 6  # envelope, Actions, Action, dependencies, edge, evidence
_MAX_EDGES = MAX_ACTIONS * (MAX_ACTIONS - 1) // 2  # acyclic graph
_MAX_OBJECTS = 1 + 2 * MAX_ACTIONS + 2 * _MAX_EDGES
_MAX_COMMAS = 4 * _MAX_EDGES + 6 * MAX_ACTIONS + 3

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVIDENCE_KINDS = frozenset({"QUALIFIED_ARTIFACT", "REPOSITORY_REVISION"})
_ENVELOPE_FIELDS = frozenset({"contract_version", "mission_id", "mission_revision", "actions"})
_ACTION_FIELDS = frozenset({"action_id", "target", "dependencies"})
_TARGET_FIELDS = frozenset({"ep_instance_id", "project_id", "repository_id", "baseline_revision"})
_EDGE_FIELDS = frozenset({"predecessor_action_id", "required_evidence"})
_EVIDENCE_FIELDS = frozenset({"kind", "repository_id", "content_digest"})
_ROLE_FIELDS = {
    "envelope": (_ENVELOPE_FIELDS, "INVALID_ENVELOPE"),
    "action": (_ACTION_FIELDS, "INVALID_ACTION"),
    "target": (_TARGET_FIELDS, "INVALID_TARGET"),
    "edge": (_EDGE_FIELDS, "INVALID_DEPENDENCY"),
    "evidence": (_EVIDENCE_FIELDS, "INVALID_EVIDENCE"),
}
_OBJECT_CHILDREN = {
    "envelope": {"actions": (91, "actions")},
    "action": {"target": (123, "target"), "dependencies": (91, "dependencies")},
    "edge": {"required_evidence": (123, "evidence")},
}
_ARRAY_CHILDREN = {"actions": (123, "action"), "dependencies": (123, "edge")}
_KNOWN_OBJECT_FIELDS = frozenset(fields for fields, _code in _ROLE_FIELDS.values())
_INVALID_OBJECT = object()


@dataclass(frozen=True)
class CompatibilityError(ValueError):
    """A stable rejection code and structural path, without submitted values."""

    code: str
    path: str = "$"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class CompatibilityScope:
    """Expected EP identities for comparison, not verified execution authority."""

    ep_instance_id: str
    project_id: str
    repository_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        repositories = self.repository_ids
        if (
            not _is_identifier(self.ep_instance_id)
            or not _is_identifier(self.project_id)
            or not isinstance(repositories, tuple)
            or not 1 <= len(repositories) <= MAX_ACTIONS
            or any(not _is_identifier(repository) for repository in repositories)
            or len(set(repositories)) != len(repositories)
        ):
            raise CompatibilityError("INVALID_SCOPE")


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _require_object(value: object, fields: frozenset[str], code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CompatibilityError(code, path)
    return value


def _object_pairs(pairs: list[tuple[str, Any]]) -> object:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompatibilityError("MALFORMED_INPUT")
        result[key] = value
    return result if frozenset(result) in _KNOWN_OBJECT_FIELDS else _INVALID_OBJECT


def _invalid_constant(_value: str) -> None:
    raise CompatibilityError("MALFORMED_INPUT")


def _bound_container_items(document: bytes) -> None:
    """Bound JSON token sizes and producer shape before the decoder allocates."""
    stack: list[list[Any]] = []  # container byte, comma count, role, pending key
    in_string = False
    escaped = False
    string_bytes = 0
    string_start = 0
    last_string: bytes | None = None
    for index, byte in enumerate(document):
        if in_string:
            if escaped:
                if byte == 117 and index + 4 < len(document):  # \uXXXX
                    try:
                        codepoint = int(document[index + 1:index + 5], 16)
                    except ValueError:
                        codepoint = None  # the JSON decoder reports malformed escapes
                    if codepoint is not None and codepoint > 127:
                        raise CompatibilityError("MALFORMED_INPUT")
                escaped = False
            elif byte == 92:  # backslash
                escaped = True
            elif byte == 34:  # quote
                in_string = False
                last_string = document[string_start:index + 1]
                continue
            string_bytes += 1
            if string_bytes > _MAX_STRING_BYTES:
                raise CompatibilityError("STRUCTURE_LIMIT_EXCEEDED")
            continue
        if byte in (9, 10, 13, 32):  # JSON whitespace
            continue
        if byte == 34:
            if stack and stack[-1][0] == 123 and stack[-1][3] is not None:
                stack[-1][3] = None  # string value, not a child container
            in_string = True
            string_bytes = 0
            string_start = index
        elif byte == 58 and last_string is not None and stack and stack[-1][0] == 123:
            try:
                key, _ = json.decoder.scanstring(last_string.decode("ascii"), 1)
            except ValueError as error:
                raise CompatibilityError("MALFORMED_INPUT") from error
            stack[-1][3] = key
        elif byte in (91, 123):  # [ or {
            if not stack:
                role = "envelope" if byte == 123 else "unknown"
            elif stack[-1][0] == 91:
                child = _ARRAY_CHILDREN.get(stack[-1][2])
                if child is None or child[0] != byte:
                    raise CompatibilityError("STRUCTURE_LIMIT_EXCEEDED")
                role = child[1]
            else:
                parent = stack[-1]
                child = _OBJECT_CHILDREN.get(parent[2], {}).get(parent[3])
                if child is None or child[0] != byte:
                    raise CompatibilityError(_ROLE_FIELDS[parent[2]][1])
                parent[3] = None
                role = child[1]
            stack.append([byte, 0, role, None])
        elif byte == 44 and stack:  # comma in the current array or object
            stack[-1][1] += 1
            limit = _MAX_ARRAY_ITEMS if stack[-1][0] == 91 else _MAX_OBJECT_FIELDS
            if stack[-1][1] >= limit:
                raise CompatibilityError("STRUCTURE_LIMIT_EXCEEDED")
            stack[-1][3] = None
        elif byte in (93, 125) and stack:  # ] or }; syntax belongs to json.loads
            stack.pop()
        last_string = None
        if len(stack) > _MAX_DEPTH:
            raise CompatibilityError("STRUCTURE_LIMIT_EXCEEDED")


def _decode(document: bytes) -> object:
    if not isinstance(document, bytes) or not document:
        raise CompatibilityError("MALFORMED_INPUT")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise CompatibilityError("INPUT_TOO_LARGE")
    if not document.isascii():
        raise CompatibilityError("MALFORMED_INPUT")
    # No valid producer value contains these punctuation bytes. Bound container
    # and element counts before json.loads can materialize a huge invalid tree.
    if (document.count(b"[") > _MAX_ARRAYS
            or document.count(b"{") > _MAX_OBJECTS
            or document.count(b",") > _MAX_COMMAS):
        raise CompatibilityError("STRUCTURE_LIMIT_EXCEEDED")
    _bound_container_items(document)
    try:
        return json.loads(
            document.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        if isinstance(error, CompatibilityError):
            raise
        raise CompatibilityError("MALFORMED_INPUT") from error


def _parse_actions(items: object, scope: CompatibilityScope) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ACTIONS:
        raise CompatibilityError("INVALID_ENVELOPE", "$.actions")
    actions: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(items):
        path = f"$.actions[{index}]"
        action = _require_object(value, _ACTION_FIELDS, "INVALID_ACTION", path)
        action_id = action["action_id"]
        if not _is_identifier(action_id) or action_id in actions:
            raise CompatibilityError("INVALID_ACTION", f"{path}.action_id")
        target = _require_object(action["target"], _TARGET_FIELDS, "INVALID_TARGET", f"{path}.target")
        if any(not _is_identifier(target[field]) for field in _TARGET_FIELDS):
            raise CompatibilityError("INVALID_TARGET", f"{path}.target")
        if target["ep_instance_id"] != scope.ep_instance_id or target["project_id"] != scope.project_id:
            raise CompatibilityError("TARGET_SCOPE_MISMATCH", f"{path}.target")
        if target["repository_id"] not in scope.repository_ids:
            raise CompatibilityError("FOREIGN_REPOSITORY", f"{path}.target.repository_id")
        edges = action["dependencies"]
        if not isinstance(edges, list) or len(edges) > MAX_ACTIONS - 1:
            raise CompatibilityError("INVALID_DEPENDENCY", f"{path}.dependencies")
        dependencies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge_index, value in enumerate(edges):
            edge_path = f"{path}.dependencies[{edge_index}]"
            edge = _require_object(value, _EDGE_FIELDS, "INVALID_DEPENDENCY", edge_path)
            predecessor_id = edge["predecessor_action_id"]
            if not _is_identifier(predecessor_id) or predecessor_id in seen:
                raise CompatibilityError("INVALID_DEPENDENCY", f"{edge_path}.predecessor_action_id")
            if predecessor_id == action_id:
                raise CompatibilityError("CYCLE", f"{edge_path}.predecessor_action_id")
            evidence = _require_object(
                edge["required_evidence"], _EVIDENCE_FIELDS, "INVALID_EVIDENCE",
                f"{edge_path}.required_evidence",
            )
            if (
                not isinstance(evidence["kind"], str)
                or evidence["kind"] not in _EVIDENCE_KINDS
                or not _is_identifier(evidence["repository_id"])
                or not isinstance(evidence["content_digest"], str)
                or _DIGEST.fullmatch(evidence["content_digest"]) is None
            ):
                raise CompatibilityError("INVALID_EVIDENCE", f"{edge_path}.required_evidence")
            seen.add(predecessor_id)
            dependencies.append({
                "predecessor_action_id": predecessor_id,
                "required_evidence": dict(evidence),
            })
        actions[action_id] = {
            "action_id": action_id,
            "target": dict(target),
            "dependencies": dependencies,
        }
    return actions


def _ancestors(actions: dict[str, dict[str, Any]]) -> dict[str, frozenset[str]]:
    for action in actions.values():
        for edge in action["dependencies"]:
            predecessor = actions.get(edge["predecessor_action_id"])
            if predecessor is None:
                raise CompatibilityError("MISSING_PREDECESSOR")
            if edge["required_evidence"]["repository_id"] != predecessor["target"]["repository_id"]:
                raise CompatibilityError("EVIDENCE_TARGET_MISMATCH")
    visiting: set[str] = set()
    ancestors: dict[str, frozenset[str]] = {}

    def visit(action_id: str) -> frozenset[str]:
        if action_id in visiting:
            raise CompatibilityError("CYCLE")
        if action_id in ancestors:
            return ancestors[action_id]
        visiting.add(action_id)
        predecessors: set[str] = set()
        for edge in actions[action_id]["dependencies"]:
            predecessor_id = edge["predecessor_action_id"]
            predecessors.add(predecessor_id)
            predecessors.update(visit(predecessor_id))
        visiting.remove(action_id)
        ancestors[action_id] = frozenset(predecessors)
        return ancestors[action_id]

    for action_id in actions:
        visit(action_id)
    return ancestors


def _compatible(document: bytes, scope: CompatibilityScope) -> dict[str, Any]:
    graph = _decode(document)
    if not isinstance(graph, dict):
        raise CompatibilityError("INVALID_ENVELOPE")
    if graph.get("contract_version") != PRODUCER_VERSION:
        raise CompatibilityError("UNSUPPORTED_VERSION", "$.contract_version")
    _require_object(graph, _ENVELOPE_FIELDS, "INVALID_ENVELOPE", "$")
    if not _is_identifier(graph["mission_id"]):
        raise CompatibilityError("INVALID_ENVELOPE", "$.mission_id")
    revision = graph["mission_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise CompatibilityError("INVALID_ENVELOPE", "$.mission_revision")
    actions_by_id = _parse_actions(graph["actions"], scope)
    ancestors = _ancestors(actions_by_id)
    actions = [actions_by_id[key] for key in sorted(actions_by_id)]
    canonical_graph = {
        "contract_version": PRODUCER_VERSION,
        "mission_id": graph["mission_id"],
        "mission_revision": revision,
        "actions": actions,
    }
    graph_digest = "sha256:" + sha256(
        json.dumps(canonical_graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    pairs = [
        [left["action_id"], right["action_id"]]
        for index, left in enumerate(actions)
        for right in actions[index + 1:]
        if left["action_id"] not in ancestors[right["action_id"]]
        and right["action_id"] not in ancestors[left["action_id"]]
        and left["target"]["repository_id"] != right["target"]["repository_id"]
    ]
    return {
        "contract_version": READBACK_VERSION,
        "producer_contract_version": PRODUCER_VERSION,
        "status": "COMPATIBLE",
        "compatible": True,
        "dispatch_authorized": False,
        "execution_authority": "NOT_EVALUATED",
        "admission": "COMPATIBILITY_ONLY",
        "producer_snapshot_digest": graph_digest,
        "mission_id": graph["mission_id"],
        "mission_revision": revision,
        "actions": [
            {
                **action,
                "target_baseline_state": "UNVERIFIED",
                "predecessor_evidence_state": (
                    "UNVERIFIED" if action["dependencies"] else "NOT_REQUIRED"
                ),
            }
            for action in actions
        ],
        "concurrency": {
            "classification": "TOPOLOGY_ONLY_UNQUALIFIED",
            "independent_repository_action_pairs": pairs,
            "resource_and_capacity_verified": False,
        },
    }


def assess_parallel_action_graph(document: bytes, *, scope: CompatibilityScope) -> dict[str, Any]:
    """Return a typed, fail-closed readback; never admit or dispatch an Action."""
    try:
        if not isinstance(scope, CompatibilityScope):
            raise CompatibilityError("INVALID_SCOPE")
        return _compatible(document, scope)
    except CompatibilityError as error:
        return {
            "contract_version": READBACK_VERSION,
            "status": "REJECTED",
            "compatible": False,
            "dispatch_authorized": False,
            "execution_authority": "NOT_EVALUATED",
            "admission": "DENIED",
            "error": {"code": error.code, "path": error.path},
        }
