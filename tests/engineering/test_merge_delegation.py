"""Bounded owner merge delegation identity checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from engineering_platform import merge_delegation, server
from engineering_platform.storage import sqlite_connection


class MergeDelegationTest(unittest.TestCase):
    def test_local_owner_cli_reserves_activates_and_revokes_bound_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data, root = base / "data", base / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                            "https://github.com/pcvantol/forge.git"], check=True)
            server.initialize(data)
            now = datetime.now(timezone.utc).isoformat()
            with sqlite_connection(data / "epdata.sqlite") as connection:
                connection.execute("INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                                   ("project", "{}", "ACTIVE", now, now))
                connection.execute("INSERT INTO ep_repository_registrations "
                                   "(repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at) "
                                   "VALUES(?,?,?,?,?,?,?)",
                                   ("opaque", "project", "opaque", "authority", "{}", now, now))
                connection.execute("INSERT INTO ep_local_repository_bindings VALUES(?,?,?,?,?,?)",
                                   ("project", "opaque", str(root), "BOUND", now, now))
            def cli(*args: str) -> dict[str, object]:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(server.main([*args, "--data-root", str(data)]), 0)
                return json.loads(output.getvalue())
            expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            reserved = cli("reserve-merge-delegation", "--project-id", "project",
                           "--repository-id", "opaque", "--merge-role", "IMPLEMENTATION",
                           "--expires-at", expiry)
            self.assertEqual(reserved["result"], "RESERVED")
            self.assertEqual(reserved["github_repository"], "pcvantol/forge")
            delegation_id = str(reserved["delegation_id"])
            active = cli("activate-merge-delegation", "--delegation-id", delegation_id,
                         "--mission-id", "mission-1", "--mission-revision", "1")
            self.assertEqual(active["result"], "ACTIVE")
            self.assertEqual(active["mission_id"], "mission-1")
            revoked = cli("revoke-merge-delegation", "--delegation-id", delegation_id)
            self.assertEqual(revoked["result"], "REVOKED")

    def test_bound_origin_must_be_github_and_exact_owner_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                            "https://github.com/pcvantol/forge.git"], check=True)
            self.assertEqual(merge_delegation.bound_github_repository(root), "pcvantol/forge")
            subprocess.run(["git", "-C", str(root), "remote", "set-url", "origin",
                            "https://example.com/pcvantol/forge.git"], check=True)
            with self.assertRaisesRegex(ValueError, "not a GitHub repository"):
                merge_delegation.bound_github_repository(root)

    def test_reservation_cannot_merge_and_activation_is_one_time_and_actor_bound(self) -> None:
        connection = sqlite3.connect(":memory:")
        merge_delegation.install_schema(connection)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        grant = merge_delegation.reserve(
            connection, delegation_id="a" * 32, actor_reference="local-uid:501",
            project_id="project", repository_id="opaque", github_repository="pcvantol/forge",
            base_branch="main", roles=("IMPLEMENTATION",), expires_at=expiry,
        )
        scope = dict(project_id="project", repository_id="opaque", mission_id="mission-1",
                     mission_revision="1", role="IMPLEMENTATION", base_branch="main")
        self.assertFalse(grant.permits(**scope))
        with self.assertRaises(ValueError):
            merge_delegation.activate(
                connection, delegation_id=grant.delegation_id, mission_id="mission-1",
                mission_revision="1", actor_reference="local-uid:502",
                github_repository="pcvantol/forge",
            )
        active = merge_delegation.activate(
            connection, delegation_id=grant.delegation_id, mission_id="mission-1",
            mission_revision="1", actor_reference="local-uid:501",
            github_repository="pcvantol/forge",
        )
        self.assertTrue(active.permits(**scope))
        self.assertFalse(active.permits(**{**scope, "mission_revision": "2"}))
        with self.assertRaises(ValueError):
            merge_delegation.activate(
                connection, delegation_id=grant.delegation_id, mission_id="mission-2",
                mission_revision="1", actor_reference="local-uid:501",
                github_repository="pcvantol/forge",
            )
        self.assertFalse(merge_delegation.revoke(connection, grant.delegation_id, actor_reference="local-uid:502"))
        self.assertTrue(merge_delegation.revoke(connection, grant.delegation_id, actor_reference="local-uid:501"))
        self.assertFalse(merge_delegation.load(connection, grant.delegation_id).permits(**scope))


if __name__ == "__main__":
    unittest.main()
