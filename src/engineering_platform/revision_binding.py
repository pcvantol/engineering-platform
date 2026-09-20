"""Immutable request-to-baseline revision bindings for Managed execution."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


_SHA = re.compile(r"[0-9a-f]{40}")
CONSTRAINT_KEY = "repository_revision_binding"


@dataclass(frozen=True)
class RepositoryRevisionBinding:
    """A producer-pinned request, optionally allowing one named baseline."""

    requested_revision: str
    allowed_baseline_revision: str | None = None
    repository_identity: str | None = None


def parse_repository_revision_binding(
    constraints: Mapping[str, object] | None,
) -> RepositoryRevisionBinding | None:
    """Parse only the accepted constraint; absent bindings remain explicit."""
    if constraints is None or CONSTRAINT_KEY not in constraints:
        return None
    raw = constraints[CONSTRAINT_KEY]
    if not isinstance(raw, Mapping) or set(raw) not in (
        {"requested_revision", "allowed_baseline_revision"},
        {"requested_revision", "allowed_baseline_revision", "repository_identity"},
    ):
        raise ValueError("repository revision binding shape is invalid")
    requested = raw.get("requested_revision")
    allowed = raw.get("allowed_baseline_revision")
    if not isinstance(requested, str) or _SHA.fullmatch(requested) is None:
        raise ValueError("requested repository revision is invalid")
    if allowed is not None and (not isinstance(allowed, str) or _SHA.fullmatch(allowed) is None):
        raise ValueError("allowed baseline repository revision is invalid")
    identity = raw.get("repository_identity")
    if identity is not None and (not isinstance(identity, str)
                                 or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity) is None):
        raise ValueError("repository identity is invalid")
    return RepositoryRevisionBinding(requested, allowed, identity)
