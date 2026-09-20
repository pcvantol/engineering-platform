from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from engineering_platform import managed_workspace_readiness, server
from engineering_platform.storage import sqlite_connection


class ManagedWorkspaceReadinessTests(unittest.TestCase):
    def test_readiness_inspects_without_mutating_unknown_future_baseline(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "central"
            checkout = root / "workspace"
            checkout.mkdir()
            server.initialize(data)

            def git(*arguments: str) -> str:
                completed = subprocess.run(
                    ("git", *arguments), cwd=checkout, text=True,
                    capture_output=True, check=True,
                )
                return completed.stdout.strip()

            git("init", "-b", "main")
            git("config", "user.email", "tests@example.invalid")
            git("config", "user.name", "Tests")
            git("remote", "add", "origin", "https://github.com/pcvantol/forge.git")
            (checkout / "BOOTSTRAP.md").write_text("test\n", encoding="utf-8")
            git("add", "BOOTSTRAP.md")
            git("commit", "-m", "baseline")
            head = git("rev-parse", "HEAD")
            with sqlite_connection(data / server.SERVER_DATABASE_FILENAME) as connection:
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)", ("forge", "{}", "ACTIVE", "now", "now"))
                connection.execute("INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)", ("forge", "forge", "forge", "authority", "{}", "now", "now"))
                connection.execute(
                    "INSERT INTO ep_local_repository_bindings(project_id,repository_id,local_root,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("forge", "forge", str(checkout), "BOUND", "now", "now"),
                )
                ready = managed_workspace_readiness.project_readiness(
                    connection, project_id="forge", repository_id="forge",
                )
                self.assertEqual(ready["status"], "READY")
                self.assertEqual(ready["repository_identity"], "pcvantol/forge")
                self.assertEqual(ready["head_sha"], head)
                self.assertEqual(ready["preparation_capability"], "EXACT_MAIN_FAST_FORWARD_V1")
                (checkout / "unknown.txt").write_text("local work", encoding="utf-8")
                blocked = managed_workspace_readiness.project_readiness(
                    connection, project_id="forge", repository_id="forge",
                )
                self.assertEqual(blocked["known_blocker"], "MANAGED_WORKSPACE_DIRTY")
                self.assertEqual(git("rev-parse", "HEAD"), head)
                self.assertTrue((checkout / "unknown.txt").exists())
