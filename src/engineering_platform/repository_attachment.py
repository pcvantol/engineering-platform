"""Read-only repository attachment contract for future EP Project Agents.

This module validates declarations only.  It never executes a validation
entrypoint, probes a host, writes local state, or registers with a server.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping


CONFIG_RELATIVE_PATH = Path(".engineering-platform/repository.json")
SCHEMA_VERSION = "1.0"
SCHEMA_RESOURCE = "schemas/repository-attachment.schema.json"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9._-]*$")
_ROLES = frozenset({"authority", "child"})
_VALIDATION_KINDS = frozenset({"command", "script", "workflow", "none"})


class RepositoryAttachmentError(ValueError):
    """Raised when a repository attachment is absent or structurally unsafe."""


@dataclass(frozen=True)
class ValidationBoundary:
    kind: str
    entrypoint: str | None
    description: str | None


@dataclass(frozen=True)
class RepositoryAttachment:
    project_id: str
    authority_repository_id: str
    repository_id: str
    repository_role: str
    validation: ValidationBoundary
    host_requirements: Mapping[str, str]
    tool_requirements: Mapping[str, str]
    integrations: Mapping[str, Mapping[str, Any]]

    def agent_read_surface(self) -> dict[str, object]:
        """Return the deliberately small, non-secret future Agent read surface."""
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {
                "id": self.project_id,
                "authority_repository_id": self.authority_repository_id,
            },
            "repository": {"id": self.repository_id, "role": self.repository_role},
            "validation": {
                "kind": self.validation.kind,
                "entrypoint": self.validation.entrypoint,
                "description": self.validation.description,
            },
            "requirements": {"host": dict(self.host_requirements), "tools": dict(self.tool_requirements)},
            "integrations": {name: dict(metadata) for name, metadata in self.integrations.items()},
        }


def config_path(repository_root: Path) -> Path:
    """Return the one canonical repository-local attachment location."""
    return repository_root / CONFIG_RELATIVE_PATH


def load_repository_attachment(repository_root: Path) -> RepositoryAttachment:
    """Load and validate one repository declaration without discovering peers."""
    path = config_path(repository_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RepositoryAttachmentError(f"repository attachment is missing: {CONFIG_RELATIVE_PATH}") from error
    except json.JSONDecodeError as error:
        raise RepositoryAttachmentError("repository attachment is not valid JSON") from error
    return parse_repository_attachment(payload)


def parse_repository_attachment(payload: object) -> RepositoryAttachment:
    """Validate JSON-compatible attachment data against schema version 1.0."""
    root = _object(payload, "attachment")
    _only(root, {"schema_version", "project", "repository", "validation", "requirements", "integrations"}, "attachment")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise RepositoryAttachmentError("unsupported repository attachment schema_version")

    project = _object(_required(root, "project", "attachment"), "project")
    _only(project, {"id", "authority_repository_id"}, "project")
    project_id = _identifier(_required(project, "id", "project"), "project.id")
    authority_repository_id = _identifier(_required(project, "authority_repository_id", "project"), "project.authority_repository_id")

    repository = _object(_required(root, "repository", "attachment"), "repository")
    _only(repository, {"id", "role"}, "repository")
    repository_id = _identifier(_required(repository, "id", "repository"), "repository.id")
    role = _required(repository, "role", "repository")
    if role not in _ROLES:
        raise RepositoryAttachmentError("repository.role must be authority or child")
    if (role == "authority") != (repository_id == authority_repository_id):
        raise RepositoryAttachmentError("repository.role and project.authority_repository_id disagree")

    validation_data = _object(_required(root, "validation", "attachment"), "validation")
    _only(validation_data, {"kind", "entrypoint", "description"}, "validation")
    kind = _required(validation_data, "kind", "validation")
    if kind not in _VALIDATION_KINDS:
        raise RepositoryAttachmentError("validation.kind is unsupported")
    entrypoint = validation_data.get("entrypoint")
    if kind == "none" and entrypoint is not None:
        raise RepositoryAttachmentError("validation.entrypoint is forbidden when validation.kind is none")
    if kind != "none":
        entrypoint = _bounded_text(entrypoint, "validation.entrypoint")
    description = validation_data.get("description")
    if description is not None:
        description = _bounded_text(description, "validation.description")

    requirements = _object(root.get("requirements", {}), "requirements")
    _only(requirements, {"host", "tools"}, "requirements")
    host = _string_map(requirements.get("host", {}), "requirements.host")
    tools = _string_map(requirements.get("tools", {}), "requirements.tools")
    integrations = _integration_map(root.get("integrations", {}))
    return RepositoryAttachment(project_id, authority_repository_id, repository_id, role, ValidationBoundary(kind, entrypoint, description), host, tools, integrations)


def _required(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise RepositoryAttachmentError(f"{context}.{key} is required")
    return mapping[key]


def _object(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RepositoryAttachmentError(f"{context} must be an object")
    return value


def _only(mapping: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unexpected = set(mapping) - allowed
    if unexpected:
        raise RepositoryAttachmentError(f"{context} has unsupported fields: {', '.join(sorted(unexpected))}")


def _identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 128 or not _IDENTIFIER.fullmatch(value):
        raise RepositoryAttachmentError(f"{context} must be a lowercase stable identifier")
    return value


def _bounded_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value or "\n" in value or "\r" in value:
        raise RepositoryAttachmentError(f"{context} must be one non-empty line of at most 512 characters")
    return value


def _string_map(value: object, context: str) -> Mapping[str, str]:
    mapping = _object(value, context)
    return {_metadata_key(key, f"{context} key"): _bounded_text(item, f"{context}.{key}") for key, item in mapping.items()}


def _integration_map(value: object) -> Mapping[str, Mapping[str, Any]]:
    mapping = _object(value, "integrations")
    result: dict[str, Mapping[str, Any]] = {}
    for name, metadata in mapping.items():
        result[_metadata_key(name, "integrations key")] = _object(metadata, f"integrations.{name}")
    return result


def _metadata_key(value: object, context: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not _METADATA_KEY.fullmatch(value):
        raise RepositoryAttachmentError(f"{context} must be a lowercase metadata key")
    return value
