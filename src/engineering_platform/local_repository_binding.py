"""Private CENTRAL mappings from logical repositories to local checkout roots.

This is intentionally an operator-only Phase-P bridge.  It does not discover
checkouts, consult Agent storage, or grant execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .repository_attachment import RepositoryAttachmentError, load_repository_attachment


class LocalRepositoryBindingError(ValueError):
    """A stable, redacted failure for local-binding lifecycle operations."""


@dataclass(frozen=True)
class LocalRepositoryBinding:
    project_id: str
    repository_id: str
    local_root: Path
    state: str
    created_at: str
    updated_at: str


def _fail(code: str) -> None:
    raise LocalRepositoryBindingError(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_local_repository_bindings (
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        local_root TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('BOUND','UNBOUND')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id,repository_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS ep_local_repository_bindings_repository_lookup ON ep_local_repository_bindings(repository_id,state)")


def _validated_root(local_root: object, *, data_root: Path | None = None) -> Path:
    if not isinstance(local_root, (str, Path)):
        _fail("LOCAL_ROOT_INVALID")
    candidate = Path(local_root).expanduser()
    if not candidate.is_absolute():
        _fail("LOCAL_ROOT_NOT_ABSOLUTE")
    try:
        root = candidate.resolve(strict=True)
    except OSError:
        _fail("LOCAL_ROOT_UNAVAILABLE")
    if not root.is_dir():
        _fail("LOCAL_ROOT_NOT_DIRECTORY")
    # Never allow a filesystem anchor, home directory, CENTRAL data root, or
    # the installed package/runtime tree to be accidentally treated as a repo.
    forbidden = {root.anchor, str(Path.home().resolve())}
    if str(root) in forbidden:
        _fail("LOCAL_ROOT_UNSAFE")
    runtime_root = Path(__file__).resolve().parents[1]
    protected = [runtime_root]
    if data_root is not None:
        try:
            protected.append(data_root.resolve())
        except OSError:
            _fail("LOCAL_ROOT_UNAVAILABLE")
    if any(root == protected_root or root.is_relative_to(protected_root) for protected_root in protected):
        _fail("LOCAL_ROOT_UNSAFE")
    return root


def _validate_topology(connection: sqlite3.Connection, project_id: str, repository_id: str) -> None:
    project = connection.execute("SELECT 1 FROM ep_project_registrations WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        _fail("UNKNOWN_PROJECT")
    repository = connection.execute("SELECT project_id FROM ep_repository_registrations WHERE repository_id=?", (repository_id,)).fetchone()
    if repository is None:
        _fail("UNKNOWN_REPOSITORY")
    if str(repository[0]) != project_id:
        _fail("PROJECT_REPOSITORY_MISMATCH")


def _validate_declaration(root: Path, project_id: str, repository_id: str) -> None:
    try:
        declaration = load_repository_attachment(root)
    except RepositoryAttachmentError:
        _fail("REPOSITORY_DECLARATION_INVALID")
    if declaration.project_id != project_id or declaration.repository_id != repository_id:
        _fail("REPOSITORY_DECLARATION_MISMATCH")


def bind_local_repository(
    connection: sqlite3.Connection, *, project_id: str, repository_id: str,
    local_root: object, data_root: Path | None = None, rebind: bool = False,
) -> LocalRepositoryBinding:
    """Create or deliberately replace the one active path for a repository."""
    _validate_topology(connection, project_id, repository_id)
    root = _validated_root(local_root, data_root=data_root)
    _validate_declaration(root, project_id, repository_id)
    existing = connection.execute("SELECT local_root,state,created_at FROM ep_local_repository_bindings WHERE project_id=? AND repository_id=?", (project_id, repository_id)).fetchone()
    if existing is not None and str(existing[0]) != str(root) and not rebind:
        _fail("LOCAL_BINDING_EXISTS_REBIND_REQUIRED")
    now = _now()
    if existing is None:
        connection.execute("INSERT INTO ep_local_repository_bindings(project_id,repository_id,local_root,state,created_at,updated_at) VALUES(?,?,?,'BOUND',?,?)", (project_id, repository_id, str(root), now, now))
        created_at = now
    else:
        connection.execute("UPDATE ep_local_repository_bindings SET local_root=?,state='BOUND',updated_at=? WHERE project_id=? AND repository_id=?", (str(root), now, project_id, repository_id))
        created_at = str(existing[2])
    return LocalRepositoryBinding(project_id, repository_id, root, "BOUND", created_at, now)


def resolve_local_repository_binding(connection: sqlite3.Connection, *, project_id: str, repository_id: str, data_root: Path | None = None) -> LocalRepositoryBinding:
    """Return the current validated root, or fail closed without CWD fallback."""
    _validate_topology(connection, project_id, repository_id)
    row = connection.execute("SELECT local_root,state,created_at,updated_at FROM ep_local_repository_bindings WHERE project_id=? AND repository_id=?", (project_id, repository_id)).fetchone()
    if row is None or str(row[1]) != "BOUND":
        _fail("LOCAL_BINDING_UNBOUND")
    root = _validated_root(str(row[0]), data_root=data_root)
    _validate_declaration(root, project_id, repository_id)
    return LocalRepositoryBinding(project_id, repository_id, root, "BOUND", str(row[2]), str(row[3]))


def unbind_local_repository(connection: sqlite3.Connection, *, project_id: str, repository_id: str) -> None:
    """Disable the mapping while preserving historical binding evidence."""
    _validate_topology(connection, project_id, repository_id)
    now = _now()
    connection.execute("UPDATE ep_local_repository_bindings SET state='UNBOUND',updated_at=? WHERE project_id=? AND repository_id=?", (now, project_id, repository_id))


# Phase-P's exact internal boundary.  The alias keeps call sites free of CLI
# wording and makes clear that lookup itself supplies no execution eligibility.
resolve_execution_repository = resolve_local_repository_binding
