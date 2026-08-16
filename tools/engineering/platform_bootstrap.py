"""Idempotent repository bootstrap and workspace provisioning API."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

from .platform_api import PlatformConfiguration, PlatformConfigurationError, shared_workspace_store


WORKSPACE_DIRECTORY = ".engineering"
LEGACY_WORKSPACE_DIRECTORY = ".djconnect"
_AUTO_IDENTIFIER_TABLES = frozenset({
    "engineering_artifacts",
    "engineering_component_logs",
    "execution_lease_events",
    "execution_lifecycle_events",
})


def _worktree_roots(root: Path) -> tuple[Path, ...]:
    """Return accessible worktrees that share ``root``'s Git common directory."""
    root = root.resolve()
    shared = shared_workspace_store(root)
    if shared == root / WORKSPACE_DIRECTORY:
        return (root,)
    common = shared.parent
    roots = {common.parent.resolve(), root}
    worktrees = common / "worktrees"
    if worktrees.is_dir():
        for entry in worktrees.iterdir():
            marker = entry / "gitdir"
            try:
                worktree_git_marker = Path(marker.read_text(encoding="utf-8").strip())
            except OSError:
                continue
            candidate = worktree_git_marker.parent.resolve()
            if (candidate / ".git").exists():
                roots.add(candidate)
    return tuple(sorted(roots, key=lambda item: str(item)))


def _history_count(workspace: Path) -> int:
    """Use the immutable history index only to choose an initial store seed."""
    database = workspace / "engineering.db"
    if not database.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM prompt_execution_history").fetchone()[0])
    except (sqlite3.DatabaseError, OSError):
        return 0


def _database_tables(connection: sqlite3.Connection, schema: str) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def _merge_databases(source: Path, destination: Path) -> None:
    """Merge independently written local evidence without discarding run history.

    Engineering records are append-only by identity.  SQLite row ids are local
    implementation details for log/event tables, so those are regenerated at
    the destination while their durable content is retained.  Mutable status
    projections take the newest timestamp.  Any incompatible schema fails
    closed before either source evidence or the shared store is removed.
    """
    with sqlite3.connect(destination) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        in_transaction = False
        try:
            current_tables = _database_tables(connection, "main")
            legacy_tables = _database_tables(connection, "legacy")
            if current_tables != legacy_tables:
                raise RuntimeError("Engineering workspace migration found incompatible database schemas.")
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            for table in current_tables:
                columns = [row[1] for row in connection.execute(f"PRAGMA main.table_info({table})")]
                if table in _AUTO_IDENTIFIER_TABLES:
                    columns = [column for column in columns if column != "id"]
                if not columns:
                    continue
                column_list = ",".join(columns)
                if table in {"engineering_status", "engineering_transactions"}:
                    key = "name" if table == "engineering_status" else "run_id"
                    for row in connection.execute(f"SELECT {column_list} FROM legacy.{table}"):
                        values = dict(zip(columns, row, strict=True))
                        current = connection.execute(
                            f"SELECT updated_at FROM main.{table} WHERE {key}=?", (values[key],)
                        ).fetchone()
                        if current is None:
                            placeholders = ",".join("?" for _ in columns)
                            connection.execute(
                                f"INSERT INTO main.{table}({column_list}) VALUES({placeholders})",
                                tuple(values[column] for column in columns),
                            )
                        elif values["updated_at"] > current[0]:
                            assignments = ["payload=?", "updated_at=?"]
                            parameters: list[object] = [values["payload"], values["updated_at"]]
                            if table == "engineering_transactions":
                                assignments.insert(1, "phase=?")
                                parameters.insert(1, values["phase"])
                            parameters.append(values[key])
                            connection.execute(
                                f"UPDATE main.{table} SET {','.join(assignments)} WHERE {key}=?", parameters
                            )
                elif table == "daily_execution_statistics":
                    # This is a rebuildable read model.  The next telemetry
                    # update writes its fresh aggregate; never overwrite a
                    # newer shared projection during migration.
                    connection.execute(
                        f"INSERT OR IGNORE INTO main.{table}({column_list}) SELECT {column_list} FROM legacy.{table}"
                    )
                else:
                    connection.execute(
                        f"INSERT OR IGNORE INTO main.{table}({column_list}) SELECT {column_list} FROM legacy.{table}"
                    )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Engineering workspace migration would violate datastore integrity.")
            connection.execute("COMMIT")
            in_transaction = False
        except Exception:
            if in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("DETACH DATABASE legacy")


def _merge_workspace(source: Path, destination: Path) -> None:
    """Merge one worktree's local evidence into the shared workspace."""
    for child in tuple(source.iterdir()):
        target = destination / child.name
        if child.name == "engineering.db" and target.is_file():
            _merge_databases(child, target)
            child.unlink()
        elif not target.exists():
            shutil.move(str(child), str(target))
        elif child.is_dir() and target.is_dir():
            _merge_workspace(child, target)
            child.rmdir()
        elif child.is_file() and target.is_file() and child.read_bytes() == target.read_bytes():
            child.unlink()
        else:
            raise RuntimeError(f"Engineering workspace migration conflict: {child.name}")


def _link_workspace(worktree: Path, shared: Path) -> None:
    """Expose the shared private store at the established worktree-local path."""
    local = worktree / WORKSPACE_DIRECTORY
    if local.is_symlink():
        if local.resolve() != shared.resolve():
            raise RuntimeError("Engineering workspace points to an unexpected shared store.")
        return
    if local.exists():
        raise RuntimeError("Engineering workspace migration did not consume a local store.")
    os.symlink(shared, local, target_is_directory=True)


def _discard_inactive_component_locks(workspace: Path) -> None:
    """Discard only stale process locks before relocating their directory.

    A flock is meaningful only in the filesystem currently held by its owner;
    copying that file into a shared workspace would create a false lock.  A
    live owner therefore blocks the migration until the normal component
    restart has stopped it.  Stale lock files are explicitly non-evidence and
    may be recreated by their owning component after the migration.
    """
    locks = workspace / "locks"
    if not locks.exists():
        return
    if locks.is_symlink() or not locks.is_dir():
        raise RuntimeError("Engineering workspace migration found an invalid component-lock directory.")
    for path in locks.glob("*.lock"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            process_id = payload.get("pid") if isinstance(payload, dict) else None
            if isinstance(process_id, int) and process_id > 0:
                os.kill(process_id, 0)
                raise RuntimeError("Engineering workspace migration requires running components to stop first.")
        except ProcessLookupError:
            continue
        except PermissionError as error:
            raise RuntimeError("Engineering workspace migration cannot verify a component lock owner.") from error
        except (OSError, json.JSONDecodeError):
            continue
    shutil.rmtree(locks)


def migrate_worktree_workspace(root: Path) -> Path:
    """Create one durable Engineering store for every worktree of a repository."""
    root = root.resolve()
    shared = shared_workspace_store(root)
    if shared == root / WORKSPACE_DIRECTORY:
        return shared
    candidates = [worktree / WORKSPACE_DIRECTORY for worktree in _worktree_roots(root)]
    local_directories = [path for path in candidates if path.exists() and not path.is_symlink()]
    if not shared.exists() and local_directories:
        seed = max(local_directories, key=lambda path: (_history_count(path), str(path)))
        shared.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(seed), str(shared))
    shared.mkdir(mode=0o700, parents=True, exist_ok=True)
    if local_directories:
        _discard_inactive_component_locks(shared)
        for local in local_directories:
            if local.exists():
                _discard_inactive_component_locks(local)
    for local in local_directories:
        if not local.exists():
            continue
        _merge_workspace(local, shared)
        local.rmdir()
    for worktree in _worktree_roots(root):
        _link_workspace(worktree, shared)
    return shared.resolve()


def _validate_legacy_merge(source: Path, destination: Path) -> None:
    """Fail closed before moving evidence into an occupied canonical workspace."""
    if source.is_symlink() or destination.is_symlink():
        raise RuntimeError("Engineering workspace migration refuses symbolic links.")
    if source.is_dir() != destination.is_dir():
        raise RuntimeError(f"Engineering workspace migration conflict: {source.name}")
    if source.is_dir():
        for child in source.iterdir():
            target = destination / child.name
            if target.exists():
                _validate_legacy_merge(child, target)
        return
    if source.read_bytes() != destination.read_bytes():
        raise RuntimeError(f"Engineering workspace migration conflict: {source.name}")


def _merge_legacy_workspace(source: Path, destination: Path) -> None:
    """Move prevalidated legacy evidence, dropping only byte-identical duplicates."""
    for child in source.iterdir():
        target = destination / child.name
        if not target.exists():
            shutil.move(str(child), str(target))
        elif child.is_dir():
            _merge_legacy_workspace(child, target)
            child.rmdir()
        else:
            child.unlink()


def _move_legacy_logs(source: Path, workspace: Path) -> None:
    """Preserve a conflicting historic log tail outside the live log files."""
    destination = workspace / "logs" / "legacy"
    if destination.exists():
        _validate_legacy_merge(source, destination)
        _merge_legacy_workspace(source, destination)
        source.rmdir()
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def _archive_legacy_evidence(source: Path, workspace: Path) -> None:
    """Keep a conflicting historic evidence category without replacing live data."""
    destination = workspace / "legacy" / source.name
    if destination.exists():
        _validate_legacy_merge(source, destination)
        _merge_legacy_workspace(source, destination)
        source.rmdir()
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def migrate_legacy_workspace(root: Path) -> Path:
    """Move `.djconnect` evidence to the sole canonical `.engineering` location."""
    root = root.resolve()
    workspace = migrate_worktree_workspace(root)
    legacy = root / LEGACY_WORKSPACE_DIRECTORY
    if not legacy.exists():
        return workspace
    if legacy.is_symlink() or not legacy.is_dir():
        raise RuntimeError("Engineering workspace migration requires a local directory.")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    archived_categories = {"logs", "qualification"}
    for source in legacy.iterdir():
        if source.name in archived_categories and (workspace / source.name).exists():
            continue
        target = workspace / source.name
        if target.exists():
            _validate_legacy_merge(source, target)
    legacy_logs = legacy / "logs"
    if legacy_logs.exists() and (workspace / "logs").exists():
        _move_legacy_logs(legacy_logs, workspace)
    legacy_qualification = legacy / "qualification"
    if legacy_qualification.exists() and (workspace / "qualification").exists():
        _archive_legacy_evidence(legacy_qualification, workspace)
    _merge_legacy_workspace(legacy, workspace)
    legacy.rmdir()
    return workspace


def provision_workspace(root: Path) -> dict[str, Path]:
    """Provision only platform-owned local directories; safe to repeat."""
    workspace = migrate_legacy_workspace(root)
    PlatformConfiguration.load(root)
    paths = {"workspace": workspace, "reports": workspace / "reports", "status": workspace / "status", "runs": workspace / "engineering-runs", "diagnostics": workspace / "logs"}
    for path in paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return paths


def validate_repository(root: Path) -> PlatformConfiguration:
    """Fail closed unless this repository is an explicit platform consumer."""
    if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
        raise PlatformConfigurationError("Repository bootstrap compatibility failed.")
    return PlatformConfiguration.load(root)


def render_template(destination: Path, replacements: dict[str, str]) -> Path:
    """Create a deterministic config template without overwriting consumer data."""
    if destination.exists():
        return destination
    template = Path(__file__).with_name("templates") / "workspace-config.json"
    content = template.read_text(encoding="utf-8")
    for key, value in sorted(replacements.items()):
        content = content.replace(key, value)
    json.loads(content)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content + "\n", encoding="utf-8")
    return destination
