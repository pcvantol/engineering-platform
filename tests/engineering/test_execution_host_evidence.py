from __future__ import annotations

from engineering_platform.storage import sqlite_connection

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_platform import execution_host_evidence, server


class ExecutionHostEvidenceTest(unittest.TestCase):
    def test_start_and_terminal_snapshots_are_path_free_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.email", "test@example.invalid"), check=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.name", "Test"), check=True)
            (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(repository), "add", "tracked.txt"), check=True)
            subprocess.run(("git", "-C", str(repository), "commit", "-qm", "baseline"), check=True)

            data_root = root / "central"
            server.initialize(data_root)
            database = data_root / server.SERVER_DATABASE_FILENAME
            with sqlite_connection(database) as connection:
                connection.execute(
                    "INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                    ("project", "{}", "ACTIVE", "now", "now"),
                )
                connection.execute(
                    "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                    ("run", "project", "RUNNING", "now", "now"),
                )
            execution_host_evidence.record_start(
                data_root, run_id="run", repository_root=repository, captured_at="2026-09-13T00:00:00+00:00",
            )
            execution_host_evidence.record_terminal(
                data_root, run_id="run", repository_root=repository, worktree_state="clean",
                modified=0, created=0, deleted=0, renamed=0, captured_at="2026-09-13T00:01:00+00:00",
            )
            with sqlite_connection(database) as connection:
                projected = execution_host_evidence.terminal_projection(connection, run_id="run")
                self.assertEqual(projected["contract_version"], "1.0")
                self.assertEqual(projected["start"]["status"], "AVAILABLE")
                self.assertEqual(projected["start"]["target_branch"], "main")
                self.assertEqual(projected["terminal"], {
                    "status": "AVAILABLE", "tracked_file_count": 1,
                    "inventory_digest": projected["start"]["inventory_digest"], "worktree_state": "CLEAN",
                    "diff": {"modified": 0, "created": 0, "deleted": 0, "renamed": 0},
                    "activity": {"provider_invocations": 0, "host_validation_actions": 0},
                })
                self.assertNotIn(str(repository), str(projected))
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE ep_execution_host_evidence SET start_document='{}' WHERE run_id='run'",
                    )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM ep_execution_host_evidence WHERE run_id='run'")

