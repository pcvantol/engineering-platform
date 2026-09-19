"""Bounded owner merge delegation identity checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import merge_delegation, server, submission_service
from engineering_platform.storage import sqlite_connection
from engineering_platform.validation_profile import (
    ValidationProfileResolutionError, delivery_unittest_binding, delivery_unittest_selectors,
)


class MergeDelegationTest(unittest.TestCase):
    def test_delivery_observation_selectors_are_bounded_and_immutable_commands(self) -> None:
        accepted = ["ep-delivery-control-validation:1",
                    "ep-delivery-unittest:tests.test_cli.ValidCase.test_valid",
                    "ep-delivery-unittest:tests.test_cli.InvalidCase.test_invalid"]
        selectors = delivery_unittest_selectors(accepted)
        self.assertEqual(selectors, ("tests.test_cli.InvalidCase.test_invalid",
                                     "tests.test_cli.ValidCase.test_valid"))
        binding = delivery_unittest_binding(selectors[0])
        self.assertFalse(binding["required"])
        self.assertEqual(binding["command"][1:], ["-m", "unittest", selectors[0]])
        self.assertEqual(binding["control_identity"], "python3 -m unittest " + selectors[0])
        for malformed in (
            ["ep-delivery-unittest:tests.test_cli.ValidCase.test_valid"],
            ["ep-delivery-control-validation:1", "ep-delivery-unittest:tests.test_cli;rm"],
            ["ep-delivery-control-validation:1", "ep-delivery-unittest:tests.test_cli.ValidCase.test_valid"] * 2,
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValidationProfileResolutionError):
                delivery_unittest_selectors(malformed)

    def test_terminal_v11_publishes_only_approved_optional_receipt(self) -> None:
        selector = "tests.test_cli.ValidCase.test_valid"
        binding = delivery_unittest_binding(selector)
        required = {"validation_id": "repository_suite", "required": True,
                    "category": "repository", "control_identity": "python3 -m unittest discover -s tests",
                    "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}
        def receipt(selected: dict[str, object], command_id: str, required_for_profile: bool) -> dict[str, object]:
            return {"validation_id": selected["validation_id"], "category": selected["category"],
                    "control_identity": selected["control_identity"],
                    "required_for_profile": required_for_profile, "execution_status": "EXECUTED",
                    "result": "PASS", "evidence_authority": "command_terminal",
                    "command_id": command_id, "currentness": 3, "exit_code": 0}
        context = {
            "selected_validation_tier": "FULL", "validation_profile_version": "1.0",
            "profile_reference": "validation-profile-registry:FULL@1.0",
            "profile_selection_source": "delivery_revision", "profile_digest": "sha256:" + "a" * 64,
            "candidate_sha": "c" * 40, "currentness": 3,
            "profile_currentness_conflict": False,
            "required_validation_controls": ("repository_suite",),
            "control_bindings": (required,),
            "controls": {"repository_suite": receipt(required, "required-command", True),
                         str(binding["validation_id"]): receipt(binding, "optional-command", False),
                         "provider_observed_unapproved": {"result": "PASS"}},
        }
        accepted = {"constraints": {"forge_execution": {"execution_constraints": [
            "ep-delivery-control-validation:1", "ep-delivery-unittest:" + selector,
        ]}}}
        connection = sqlite3.connect(":memory:")
        try:
            with (patch("engineering_platform.storage.load_validation_context", return_value=context),
                  patch("engineering_platform.submission_service.load_submission_for_run", return_value=accepted),
                  patch("engineering_platform.submission_service.sqlite_connection", return_value=connection),
                  patch("engineering_platform.submission_service._validation_result_detail",
                        return_value={"status": "AVAILABLE", "test_count": 1,
                                      "output_digest": "sha256:" + "b" * 64})):
                published = submission_service._terminal_validation_controls(
                    Path("/tmp/ep-observation-test"), repository_root=Path("/tmp/ep-observation-test"),
                    run_id="run-observation",
                )
        finally:
            connection.close()
        self.assertEqual(published["contract_version"], "1.1")
        self.assertEqual(published["candidate_sha"], "c" * 40)
        self.assertEqual(len(published["observation_validation_controls"]), 1)
        optional = published["observation_validation_controls"][0]
        self.assertEqual(optional["validation_id"], binding["validation_id"])
        self.assertEqual(optional["command_id"], "optional-command")
        self.assertEqual(optional["currentness"], 3)
        self.assertEqual(optional["result_detail"]["test_count"], 1)
        self.assertTrue(optional["control_definition_digest"].startswith("sha256:"))
        self.assertNotIn("provider_observed_unapproved", str(published))

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
