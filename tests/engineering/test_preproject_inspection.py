"""Synthetic own targets only: source binding and inert metadata observation."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from engineering_platform import preproject_inspection as reader, preproject_service as service, server


class InspectionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ep-preproject-own-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"; self.workspace.mkdir()
        self.target = self.workspace / "target"; self.target.mkdir()
        self.host = self.base / "host"; (self.host / ".engineering").mkdir(parents=True)
        self.configure()
        self.root = self.base / "server"
        self.instance = server.initialize(self.root).instance_id
        service.initialize(self.root, expected_instance=self.instance)

    def configure(self, allowed=True):
        (self.host / ".engineering/engineering-platform.local.json").write_text(json.dumps({
            "workspace": {"workspace_authorization": {"allowed_roots": [
                {"path": str(self.workspace), "repository_scope": "direct_children"}] if allowed else [],
                "allowed_repositories": [], "denied_repositories": [], "symlink_policy": "reject",
                "case_sensitivity": "host"}}}))

    def git(self, *args):
        return subprocess.check_output(("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "user.name=Own synthetic", "-c", "user.email=own@example.invalid", *args), cwd=self.target,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_OPTIONAL_LOCKS": "0"}, stderr=subprocess.DEVNULL).decode().strip()

    def binding(self, child="target"):
        path = self.workspace / child
        return {"workspace_root": str(self.workspace), "child": child,
            "root_identity": list(reader.identity(self.workspace.stat())),
            "target_identity": list(reader.identity(path.stat())) if path.exists() else None}

    def inspect(self, paths=None, content=None):
        return reader.scan(self.binding(), paths or ["README.md"], content or [])

    def bind_issue(self, **overrides):
        service.bind_source(self.root, source_ref="source-1", host_root=self.host,
            workspace_root=self.workspace, child="target")
        values = dict(grant_id="grant-1", source_ref="source-1", consumer_id="workspace-1",
            principal="actor-1", audience="workspace", paths=["README.md"], content_paths=["README.md"])
        values.update(overrides)
        self.grant = service.issue(self.root, **values)
        self.headers = {"EP-Inspection-Contract": "1.0", "EP-Instance-ID": self.instance,
            "EP-Host-ID": self.grant["host_id"], "EP-Consumer-ID": "workspace-1",
            "EP-Principal": "actor-1", "EP-Audience": "workspace"}
        self.request = {"contract_version": "1.0", "operation_id": "op-1", "source_ref": "source-1"}

    def run_operation(self, request=True, **extra):
        return service.operation(self.root, token=self.grant["credential"], headers=self.headers,
            operation_id="op-1", request=self.request if request else None, **extra)


class PhysicalInspectionTests(InspectionFixture):
    def test_isolated_worker_never_opens_ungranted_workspace_parent(self):
        (self.target / "README.md").write_text("# Own selected bytes\n")
        with patch.object(reader, "directory", side_effect=AssertionError("ungranted parent traversal")):
            result = reader.scan(self.binding(), ["README.md"], ["README.md"], isolated=True)
            absent = reader.scan(self.binding("absent"), ["README.md"], [], isolated=True)
        self.assertEqual(result["documents"][0]["content"], "# Own selected bytes\n")
        self.assertEqual(absent["state"], "ABSENT")

    def test_selected_descendant_below_non_directory_is_not_proven_absent(self):
        (self.target / "docs").write_text("not a directory")
        with self.assertRaises(reader.InspectionError):
            self.inspect(paths=["docs/README.md"])

    def test_configured_normalization_is_not_reported_as_canonical_dirty_state(self):
        self.git("init", "-q"); self.git("config", "core.autocrlf", "true")
        with self.assertRaises(reader.InspectionError): self.inspect()

    def test_absent_empty_directory_and_selection_are_distinct(self):
        absent = reader.scan(self.binding("absent"), ["README.md"], [])
        self.assertEqual(absent["state"], "ABSENT")
        self.assertEqual(self.inspect()["state"], "EMPTY_DIRECTORY")
        (self.target / "private.env").write_text("unread synthetic secret")
        (self.target / "README.md").write_text("# Exact synthetic document\n")
        result = self.inspect(content=["README.md"])
        self.assertEqual(result["state"], "DIRECTORY")
        self.assertEqual(result["documents"][0]["content"], "# Exact synthetic document\n")
        self.assertNotIn("secret", json.dumps(result))
        self.assertIn("ALL_UNSELECTED_CONTENT", result["exclusions"])

    def test_actual_unborn_existing_staged_unstaged_and_untracked(self):
        self.git("init", "-q")
        result = self.inspect()
        self.assertEqual(result["state"], "UNBORN_GIT")
        self.assertIsNone(result["git"]["head"])
        (self.target / "README.md").write_text("# One\n")
        self.git("add", "README.md"); self.git("commit", "-qm", "own initial")
        result = self.inspect()
        self.assertEqual(result["git"]["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["git"]["tree"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(result["git"]["staged_count"], 0)
        self.assertEqual(result["git"]["unstaged_count"], 0)
        (self.target / "README.md").write_text("# Two\n"); self.git("add", "README.md")
        (self.target / "README.md").write_text("# Three\n")
        (self.target / "untracked.txt").write_text("untracked")
        result = self.inspect()
        self.assertEqual(result["git"]["staged_count"], 1)
        self.assertEqual(result["git"]["unstaged_count"], 1)
        self.assertEqual(result["git"]["untracked_candidate_count"], 1)

    def test_original_negative_control_wrong_content_expectation_is_red(self):
        (self.target / "README.md").write_text("# Actual\n")
        result = self.inspect(content=["README.md"])
        # The dedicated installed negative control independently asserts a wrong
        # byte expectation and must exit nonzero; this verifies its discriminator.
        self.assertNotEqual(result["documents"][0]["sha256"], hashlib.sha256(b"# Wrong\n").hexdigest())

    def test_source_rename_target_swap_symlink_fifo_are_not_read(self):
        binding = self.binding()
        self.target.rename(self.workspace / "old")
        self.target.mkdir()
        with self.assertRaises(reader.InspectionError): reader.scan(binding, ["README.md"], [])
        (self.target / "README.md").symlink_to(self.workspace / "old/private.txt")
        with self.assertRaises(reader.InspectionError): self.inspect()
        (self.target / "README.md").unlink(); os.mkfifo(self.target / "README.md")
        with self.assertRaises(reader.InspectionError): self.inspect()

    def test_config_hooks_filters_and_remote_values_are_never_executed_or_disclosed(self):
        self.git("init", "-q")
        self.git("config", "core.fsmonitor", "touch forbidden")
        self.git("config", "filter.evil.clean", "touch forbidden")
        self.git("config", "remote.origin.url", "https://private:credential@example.invalid/target")
        result = self.inspect()
        self.assertTrue(result["git"]["remote_present"])
        self.assertEqual(result["git"]["git_execution"], "NONE")
        self.assertFalse((self.target / "forbidden").exists())
        self.assertNotIn("credential", json.dumps(result))

    def test_gitfile_alternates_nested_and_unknown_formats_are_unsupported(self):
        self.git("init", "-q")
        for path in ("objects/info/alternates", "commondir", "shallow"):
            leaf = self.target / ".git" / path; leaf.parent.mkdir(parents=True, exist_ok=True); leaf.write_text("outside")
            with self.assertRaises(reader.InspectionError): self.inspect()
            leaf.unlink()
        nested = self.target / "nested/.git"; nested.mkdir(parents=True)
        with self.assertRaises(reader.InspectionError): self.inspect()

    def test_selection_refuses_secrets_traversal_and_non_document_content(self):
        for paths, content in [(["../README.md"], []), ([".env"], []), (["secret.md"], []),
                (["code.py"], []), (["package.json"], ["package.json"])]:
            with self.assertRaises(reader.InspectionError): reader.validate_selection(paths, content)


class DurableInspectionTests(InspectionFixture):
    def test_whole_private_ledger_loss_does_not_reinitialize_same_instance(self):
        shutil.rmtree(self.root / "preproject-inspection-v1")
        with self.assertRaises((reader.InspectionError, FileExistsError)):
            service.initialize(self.root, expected_instance=self.instance)

    def test_revocation_during_last_placement_prevents_publication(self):
        self.bind_issue()
        actual = service.placement
        count = 0
        def placement(binding):
            nonlocal count
            count += 1
            values = actual(binding)
            if count == 2:
                service.grant_status(self.root, "grant-1", revoke=True)
            return values
        with patch.object(service, "placement", side_effect=placement), \
                patch.object(service, "_worker", return_value={"own": "synthetic worker fixture"}):
            with self.assertRaises(reader.InspectionError) as refused:
                self.run_operation()
        self.assertEqual(refused.exception.status, 401)

    def test_replay_is_same_original_without_worker_and_target_reread(self):
        self.bind_issue()
        with patch.object(service, "_worker", return_value={"own": "synthetic worker fixture"}) as worker:
            result = self.run_operation()
            self.assertEqual(result["outcome"], "COMPLETE")
        with patch.object(service, "_worker", side_effect=AssertionError("replay rescanned")), \
                patch.object(service, "placement", side_effect=AssertionError("replay touched target")):
            self.assertEqual(result, self.run_operation(False))
            self.assertEqual(result, self.run_operation())
        self.assertEqual(worker.call_count, 1)

    def test_wrong_scope_revocation_expiry_and_missing_store_fail_closed(self):
        self.bind_issue()
        self.headers["EP-Principal"] = "wrong"
        with self.assertRaises(reader.InspectionError): self.run_operation()
        self.headers["EP-Principal"] = "actor-1"
        service.grant_status(self.root, "grant-1", revoke=True)
        with self.assertRaises(reader.InspectionError): self.run_operation()
        database = self.root / "preproject-inspection-v1/inspection.sqlite3"
        database.rename(database.with_suffix(".saved"))
        with self.assertRaises(reader.InspectionError): service.grant_status(self.root, "grant-1")
        self.assertFalse(database.exists())
        with self.assertRaises((FileExistsError, reader.InspectionError)):
            service.initialize(self.root, expected_instance=self.instance)

    def test_changed_request_conflicts_and_interrupted_operation_never_restarts(self):
        self.bind_issue()
        with patch.object(service, "_worker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_operation()
        with patch.object(service, "_worker", side_effect=AssertionError("must not restart")):
            with self.assertRaisesRegex(reader.InspectionError, "IN_PROGRESS_OR_INTERRUPTED"): self.run_operation()
            self.request["contract_version"] = "2.0"
            with self.assertRaises(reader.InspectionError): self.run_operation()

    def test_owner_policy_revoked_before_worker_is_not_a_read_grant_fallback(self):
        self.bind_issue(); self.configure(False)
        with patch.object(service, "_worker", side_effect=AssertionError("forbidden call")):
            with self.assertRaises(reader.InspectionError) as refusal:
                self.run_operation()
        self.assertEqual(refusal.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
