"""Synthetic own targets only: source binding and inert metadata observation."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import shutil
import socket
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from types import SimpleNamespace

from engineering_platform import preproject_inspection as reader, preproject_service as service, server
from engineering_platform import preproject_cli, preproject_worker
from engineering_platform import effect_workspace, providers


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
    def committed_fixture(self):
        self.git("init", "-q"); (self.target / "README.md").write_text("# Own committed\n")
        self.git("add", "README.md"); self.git("commit", "-qm", "own snapshot")

    def test_real_packed_refs_preserve_actual_loose_commit_and_tree_identity(self):
        self.committed_fixture(); head = self.git("rev-parse", "HEAD")
        self.git("pack-refs", "--all")
        result = self.inspect()
        self.assertEqual(result["git"]["head"], head)

    def test_packed_only_objects_and_corrupt_index_are_explicitly_unsupported(self):
        self.committed_fixture()
        index = self.target / ".git/index"; original = index.read_bytes(); index.write_bytes(original[:-1]+b"x")
        with self.assertRaises(reader.InspectionError): self.inspect()
        index.write_bytes(original); self.git("repack", "-ad"); self.git("prune-packed")
        with self.assertRaises(reader.InspectionError): self.inspect()

    def test_corrupt_loose_object_is_not_a_clean_snapshot(self):
        self.committed_fixture(); oid = self.git("rev-parse", "HEAD")
        path = self.target / ".git/objects" / oid[:2] / oid[2:]
        path.chmod(0o600); path.write_bytes(b"not zlib")
        with self.assertRaises(reader.InspectionError): self.inspect()

    def test_real_bare_repository_is_explicitly_unsupported(self):
        self.git("init", "--bare", "-q")
        with self.assertRaisesRegex(reader.InspectionError, "GIT_FORMAT_UNSUPPORTED"):
            self.inspect()

    def test_source_content_limits_binary_and_unselected_items(self):
        path = self.target / "README.md"; path.write_bytes(b"x"*65537)
        with self.assertRaisesRegex(reader.InspectionError, "BYTE_LIMIT"): self.inspect()
        path.write_bytes(b"own\x00binary")
        with self.assertRaisesRegex(reader.InspectionError, "TEXT_UNSUPPORTED"): self.inspect()
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(reader.InspectionError, "TEXT_UNSUPPORTED"): self.inspect()
        path.write_text("# Metadata only\n")
        self.assertIsNone(self.inspect()["documents"][0]["content"])

    def test_declaration_identity_is_observation_not_registration(self):
        path = self.target / ".engineering-platform/repository.json"; path.parent.mkdir()
        value = {"schema_version":"1.0","project":{"id":"own-project","authority_repository_id":"own-core"},
            "repository":{"id":"own-core","role":"authority"},"validation":{"kind":"none"}}
        path.write_text(json.dumps(value))
        observed = self.inspect(paths=[".engineering-platform/repository.json"])
        self.assertEqual(observed["canonical_declaration"]["state"], "OBSERVED_DECLARATION_ONLY")
        value["repository"]["role"] = "child"; path.write_text(json.dumps(value))
        self.assertEqual(self.inspect(paths=[".engineering-platform/repository.json"])["canonical_declaration"]["state"],
            "INVALID_OR_CONFLICTING")

    def test_unsupported_name_collision_hardlink_and_expired_budget(self):
        name = self.target / "e\u0301.md"; name.write_text("# Not NFC\n")
        with self.assertRaises(reader.InspectionError): self.inspect()
        name.unlink(); path = self.target / "README.md"; path.write_text("# Own\n")
        os.link(path, self.target / "other.md")
        with self.assertRaises(reader.InspectionError): self.inspect()
        with self.assertRaisesRegex(reader.InspectionError, "LIMIT"):
            reader.scan(self.binding(), ["README.md"], [], seconds=0)

    def test_external_attributes_normalization_profile_is_explicitly_unsupported(self):
        self.git("init", "-q")
        self.git("config", "core.attributesfile", "/ungranted/synthetic/attributes")
        with self.assertRaises(reader.InspectionError): self.inspect()

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
    def test_invalid_finite_grant_fields_are_rejected_before_storage(self):
        service.bind_source(self.root, source_ref="source-1", host_root=self.host,
            workspace_root=self.workspace, child="target")
        values=dict(grant_id="own-grant", source_ref="source-1", consumer_id="own-consumer", principal="own-actor",
            audience="workspace", paths=["README.md"], content_paths=[])
        for change in ({"ttl_seconds":0},{"ttl_seconds":3601},{"max_operations":0},{"max_operations":True},
                {"paths":[]},{"paths":["../README.md"]},{"content_paths":["not-selected.md"]}):
            with self.assertRaises(reader.InspectionError): service.issue(self.root, **(values|change))
        with self.assertRaises(reader.InspectionError): service.grant_status(self.root,"missing")

    def test_every_scope_header_and_unknown_contract_is_denied_before_worker(self):
        self.bind_issue()
        with patch.object(service,"_worker",side_effect=AssertionError("denied worker")):
            for key in self.headers:
                value=self.headers[key];self.headers[key]="wrong"
                try:
                    with self.assertRaises(reader.InspectionError):self.run_operation()
                finally:self.headers[key]=value

    def test_structurally_corrupt_grant_never_reaches_a_worker(self):
        self.bind_issue()
        with service.store(self.root, write=True) as (db, _):
            row = db.execute("SELECT scope FROM grants").fetchone()
            scope = json.loads(row[0]); del scope["paths"]
            db.execute("UPDATE grants SET scope=?", (json.dumps(scope),))
        with patch.object(service, "_worker", side_effect=AssertionError("corrupt scope reached worker")):
            with self.assertRaisesRegex(reader.InspectionError, "GRANT_CORRUPT") as refused:
                self.run_operation()
        self.assertEqual(refused.exception.status, 500)

    def test_corrupt_original_intent_is_not_healthy_historical_readback(self):
        self.bind_issue()
        with patch.object(service, "_worker", return_value={"own": "unit observation fixture"}):
            self.run_operation()
        with service.store(self.root, write=True) as (db, _):
            db.execute("UPDATE operations SET intent='{}'")
        with self.assertRaisesRegex(reader.InspectionError, "INTENT_CORRUPT"):
            self.run_operation(False)

    def test_stored_json_corruption_is_not_client_input_error(self):
        self.bind_issue()
        with service.store(self.root, write=True) as (db, _):
            db.execute("UPDATE grants SET scope='not-json'")
        with self.assertRaises(reader.InspectionError) as failed:
            self.run_operation()
        self.assertEqual(failed.exception.status, 500)

    def test_actual_finite_expiry_and_wrong_domain_token_have_no_worker_calls(self):
        self.bind_issue(ttl_seconds=1)
        with patch.object(service, "_worker", side_effect=AssertionError("unauthorized work")):
            with self.assertRaises(reader.InspectionError):
                service.operation(self.root, token="not-inspect-domain-" + "a" * 32,
                    headers=self.headers, operation_id="op-1", request=self.request)
            time.sleep(1.05)
            with self.assertRaisesRegex(reader.InspectionError, "EXPIRED"):
                self.run_operation()

    def test_new_operation_limit_and_result_integrity_remain_fail_closed(self):
        self.bind_issue(max_operations=1)
        with patch.object(service, "_worker", return_value={"own": "fixture"}) as worker:
            original = self.run_operation()
            self.assertEqual(original, self.run_operation())
            second = {**self.request, "operation_id": "op-2"}
            with self.assertRaisesRegex(reader.InspectionError, "OPERATION_LIMIT"):
                service.operation(self.root, token=self.grant["credential"], headers=self.headers,
                    operation_id="op-2", request=second)
        self.assertEqual(worker.call_count, 1)
        with service.store(self.root, write=True) as (db, _):
            db.execute("UPDATE operations SET result='{}'")
        with self.assertRaisesRegex(reader.InspectionError, "RESULT_CORRUPT"):
            self.run_operation(False)

    def test_filesystem_root_never_becomes_a_genesis_workspace(self):
        (self.host / ".engineering/engineering-platform.local.json").write_text(json.dumps({"workspace": {
            "workspace_authorization": {"allowed_roots": [{"path": "/", "repository_scope": "direct_children"}],
                "allowed_repositories": [], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}))
        with patch.object(service, "directory", side_effect=AssertionError("must refuse before root read")):
            with self.assertRaises(reader.InspectionError):
                service.bind_source(self.root, source_ref="forbidden-root", host_root=self.host,
                    workspace_root=Path("/"), child="synthetic-target")

    @unittest.skipUnless(sys.platform == "darwin", "macOS host case semantics")
    def test_existing_host_deny_case_semantics_are_preserved(self):
        path = self.host / ".engineering/engineering-platform.local.json"
        config = json.loads(path.read_text())
        config["workspace"]["workspace_authorization"]["denied_repositories"] = [str(self.workspace / "TARGET")]
        path.write_text(json.dumps(config))
        with self.assertRaises(reader.InspectionError):
            service.bind_source(self.root, source_ref="denied-source", host_root=self.host,
                workspace_root=self.workspace, child="target")

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


class InspectionLocalCLIAndWorkerTests(InspectionFixture):
    def test_bad_git_text_is_unsupported_not_source_unavailable(self):
        self.git("init", "-q"); (self.target / ".git/HEAD").write_bytes(b"\xff\n")
        raw = json.dumps({"binding": self.binding(), "paths": ["README.md"], "content_paths": []}).encode()
        out = io.StringIO()
        with redirect_stdout(out), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))):
            self.assertEqual(preproject_worker.main(), 1)
        self.assertEqual(json.loads(out.getvalue())["status"], 422)

    def cli(self, command, *, config=None, extra=(), token=""):
        argv = [command, "--data-root", str(self.root), "--expected-instance-id", self.instance, *extra]
        if config is not None:
            path = self.base / "private-command.json"; path.write_text(json.dumps(config)); path.chmod(0o600)
            argv.extend(("--config", str(path)))
        out = io.StringIO()
        with redirect_stdout(out), patch.object(sys, "stdin", io.StringIO(token)):
            code = preproject_cli.main(argv)
        return code, json.loads(out.getvalue())

    def test_local_cli_same_owning_issue_status_revoke_and_instance_error(self):
        code, bound = self.cli("bind-source", config={"source_ref": "cli-source", "host_root": str(self.host),
            "workspace_root": str(self.workspace), "child": "target"})
        self.assertEqual((code, bound["source_ref"]), (0, "cli-source"))
        code, grant = self.cli("issue", config={"grant_id": "cli-grant", "source_ref": "cli-source",
            "consumer_id": "workspace-own", "principal": "actor-own", "audience": "workspace",
            "paths": ["README.md"], "content_paths": []})
        self.assertEqual(code, 0); self.assertIn("credential", grant)
        code, state = self.cli("status", extra=("--grant-id", "cli-grant"))
        self.assertEqual(state["state"], "ACTIVE"); self.assertNotIn("credential", state)
        code, state = self.cli("revoke", extra=("--grant-id", "cli-grant"))
        self.assertEqual((code, state["state"]), (0, "REVOKED"))
        code, state = self.cli("init")
        self.assertEqual(code, 2)
        code, state = self.cli("status", extra=("--expected-instance-id", "wrong", "--grant-id", "cli-grant"))
        self.assertEqual((code, state["error"]), (2, "INSPECTION_INSTANCE_MISMATCH"))

    def test_cli_parity_requires_stdin_and_never_scans_on_read(self):
        self.bind_issue()
        config = {"headers": self.headers, "source_ref": "source-1"}
        code, failed = self.cli("run", config=config, extra=("--operation-id", "op-1"))
        self.assertEqual((code, failed["error"]), (2, "INSPECTION_CREDENTIAL_STDIN_REQUIRED"))
        with patch.object(service, "_worker", return_value={"own": "unit worker fixture"}):
            code, original = self.cli("run", config=config,
                extra=("--operation-id", "op-1", "--credential-stdin"), token=self.grant["credential"])
        self.assertEqual(code, 0)
        with patch.object(service, "_worker", side_effect=AssertionError("read rescanned")):
            code, replay = self.cli("read", config=config,
                extra=("--operation-id", "op-1", "--credential-stdin"), token=self.grant["credential"])
        self.assertEqual((code, replay), (0, original))

    def test_fixed_worker_parser_errors_are_typed_and_redacted(self):
        for raw in (b"x" * 16385, b"not json", b"{}"):
            out = io.StringIO()
            with redirect_stdout(out), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))):
                self.assertEqual(preproject_worker.main(), 1)
            body = json.loads(out.getvalue()); self.assertEqual(set(body), {"error", "status"})
            self.assertNotIn(str(self.workspace), out.getvalue())

    def test_actual_installed_worker_entrypoint_uses_installed_distribution(self):
        if not Path(preproject_worker.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            self.skipTest("Source diagnostic invocation; real owning installed suite exercises distribution boundary")
        (self.target / "README.md").write_text("# Own worker input\n")
        raw = json.dumps({"binding": self.binding(), "paths": ["README.md"], "content_paths": ["README.md"]}).encode()
        out = io.StringIO()
        with redirect_stdout(out), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))):
            self.assertEqual(preproject_worker.main(), 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["runtime"]["prefix"], sys.prefix)
        self.assertEqual(body["result"]["documents"][0]["content"], "# Own worker input\n")


class InspectionHTTPAdmissionTests(InspectionFixture):
    """Real socket/store controls; worker fixture explicitly not native proof."""
    def setUp(self):
        super().setUp(); self.bind_issue()
        (self.target / "README.md").write_text("# Private own fixture\n")
        self.listener = server._ConsoleHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.listener.daemon_threads = False; self.listener.data_root = self.root
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop_listener)

    def stop_listener(self):
        self.listener.shutdown(); self.listener.server_close(); self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def request_http(self, method="POST", *, body=None, headers=None, path="/v1/onboarding/inspections"):
        values = {**self.headers, "Authorization": "Bearer " + self.grant["credential"], "Content-Type": "application/json"}
        values.update(headers or {})
        c = http.client.HTTPConnection(*self.listener.server_address, timeout=5)
        try:
            c.request(method, path, body=json.dumps(self.request if body is None else body) if method=="POST" else None, headers=values)
            r = c.getresponse(); return r.status, dict(r.getheaders()), json.loads(r.read())
        finally:
            c.close()

    def real_reader_fixture(self, root, binding, scope, recheck):
        return reader.scan(binding, scope["paths"], scope["content_paths"])

    def test_socket_request_unknown_fields_scope_and_replay(self):
        with patch.object(service, "_worker", side_effect=self.real_reader_fixture) as worker:
            status, headers, original = self.request_http()
            self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
            self.assertEqual(original["observation"]["documents"][0]["content"], "# Private own fixture\n")
            self.assertEqual(self.request_http("GET", path="/v1/onboarding/inspections/op-1")[2], original)
            self.assertEqual(self.request_http()[2], original)
            self.assertEqual(self.request_http(body={**self.request, "unexpected": True})[0], 400)
            self.assertEqual(self.request_http(headers={"EP-Principal": "wrong-actor"})[0], 403)
            self.assertEqual(self.request_http(body={**self.request, "source_ref": "wrong-source"})[0], 403)
            self.assertEqual(self.request_http("GET", path="/v1/onboarding/inspections/op-1?extra=1")[0], 400)
        self.assertEqual(worker.call_count, 1)

    def test_published_openapi_names_only_the_selected_inspection_subset(self):
        c = http.client.HTTPConnection(*self.listener.server_address,timeout=5)
        try:
            c.request("GET", "/openapi.json");r=c.getresponse();body=json.loads(r.read())
        finally:c.close()
        self.assertEqual(r.status,200)
        self.assertIn("/v1/onboarding/inspections",body["paths"])
        self.assertNotIn("/v1/onboarding/assessments",body["paths"])
        self.assertIn("inspectionBearer",body["components"]["securitySchemes"])

    def test_actual_installed_native_worker_uses_fixed_owner_runtime(self):
        if not Path(service.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            self.skipTest("Source diagnostic; installed suite owns actual native package boundary")
        actual_native = shutil.which("codex")
        self.assertIsNotNone(actual_native,"Existing pinned native qualification runtime required")
        prefix=self.base/"own-fixed-runtime";(prefix/"bin").mkdir(parents=True)
        (prefix/"bin/codex").symlink_to(actual_native)
        with patch.dict(os.environ,{"EP_MANAGED_CODEX_CLI_PREFIX":str(prefix)}):
            status,_,body=self.request_http()
        self.assertEqual(status,200,body)
        self.assertEqual(body["observation"]["worker_runtime_binding"]["state"],"CURRENT_INSTALLED_PACKAGE_VERIFIED")

    def test_late_revocation_after_encoding_has_no_private_response(self):
        actual_send = server._HealthHandler._send
        def revoke_at_transport(handler, status, payload, *args, **kwargs):
            if payload.get("outcome") == "COMPLETE":
                actual_check = kwargs["before_headers"]
                def before_headers():
                    service.grant_status(self.root, "grant-1", revoke=True)
                    actual_check()
                kwargs["before_headers"] = before_headers
            return actual_send(handler, status, payload, *args, **kwargs)
        with patch.object(service, "_worker", side_effect=self.real_reader_fixture), \
                patch.object(server._HealthHandler, "_send", revoke_at_transport):
            status, _, body = self.request_http()
        self.assertEqual(status, 401); self.assertNotIn("Private own fixture", json.dumps(body))
        self.assertEqual(set(body), {"error"})

    def test_actual_socket_incomplete_body_deadline_and_unauthenticated_refusal(self):
        for authenticated in (False, True):
            c = socket.create_connection(self.listener.server_address, timeout=5)
            try:
                lines = ["POST /v1/onboarding/inspections HTTP/1.1", "Host: localhost", "Content-Type: application/json", "Content-Length: 200"]
                if authenticated:
                    lines += [k+": "+v for k,v in self.headers.items()]
                    lines.append("Authorization: Bearer "+self.grant["credential"])
                began = time.monotonic(); c.sendall(("\r\n".join(lines)+"\r\n\r\n{").encode())
                raw = c.recv(4096)
                self.assertIn(b"408" if authenticated else b"401", raw.split(b"\r\n",1)[0])
                self.assertLess(time.monotonic()-began, 4 if authenticated else 1)
            finally:
                c.close()


class InspectionWorkerLifecycleTests(InspectionFixture):
    """Actual owned processes; launch policy stubbed, not native isolation proof."""
    def invoke_owned_child(self, code, *, recheck=lambda: None, signal_failure=False):
        actual_popen = subprocess.Popen
        observed = {"process": None, "waits": []}
        def launch(_command, **kwargs):
            child = actual_popen((sys.executable, "-c", code), **kwargs)
            observed["process"] = child
            actual_wait = child.wait
            def wait(timeout=None):
                observed["waits"].append(timeout)
                return actual_wait(timeout=timeout)
            child.wait = wait
            return child
        with patch.object(providers, "codex_cli_executable", return_value="/own-fixed-fixture/runtime"), \
                patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b"codex-cli 0.160.1\n")), \
                patch.object(effect_workspace, "sandbox_options", return_value=()), \
                patch.object(service.subprocess, "Popen", side_effect=launch):
            try:
                if signal_failure:
                    with patch.object(service.os, "killpg", side_effect=PermissionError("own injected signal denial")):
                        result = service._worker(self.root, self.binding(), {"paths": ["README.md"], "content_paths": []}, recheck)
                else:
                    result = service._worker(self.root, self.binding(), {"paths": ["README.md"], "content_paths": []}, recheck)
                return result, observed
            except BaseException as error:
                error.own_observed_process = observed
                raise

    def assert_reaped(self, observed):
        self.assertIn(5, observed["waits"])
        self.assertIsNotNone(observed["process"].returncode)
        self.assertTrue(observed["process"].stdout.closed)
        self.assertTrue(observed["process"].stdin.closed)
        with self.assertRaises(ProcessLookupError): os.kill(observed["process"].pid, 0)

    def test_actual_primary_and_signal_failure_both_retained_and_child_waited(self):
        def withdrawn(): raise reader.InspectionError("ACTUAL_OWN_PRIMARY_WITHDRAWAL", 401)
        with self.assertRaises(reader.InspectionError) as failed:
            self.invoke_owned_child("import time;time.sleep(.15)", recheck=withdrawn, signal_failure=True)
        self.assertEqual(failed.exception.code, "INSPECTION_WORKER_STOP_UNCERTAIN")
        reasons = failed.exception.__cause__.exceptions
        self.assertTrue(any(isinstance(e, PermissionError) for e in reasons))
        self.assertTrue(any(isinstance(e, reader.InspectionError) and e.code=="ACTUAL_OWN_PRIMARY_WITHDRAWAL" for e in reasons))
        self.assert_reaped(failed.exception.own_observed_process)

    def test_actual_cancelled_worker_is_killed_and_reaped(self):
        def withdrawn(): raise reader.InspectionError("ACTUAL_OWN_CANCEL", 401)
        with self.assertRaisesRegex(reader.InspectionError, "ACTUAL_OWN_CANCEL") as failed:
            self.invoke_owned_child("import time;time.sleep(30)", recheck=withdrawn)
        self.assert_reaped(failed.exception.own_observed_process)

    def test_actual_output_limit_is_red_and_child_reaped(self):
        with self.assertRaisesRegex(reader.InspectionError, "OUTPUT_LIMIT") as failed:
            self.invoke_owned_child("import sys;sys.stdout.write('x'*1100000);sys.stdout.flush()")
        self.assert_reaped(failed.exception.own_observed_process)

    def test_actual_nonconsuming_worker_hits_original_15_second_deadline(self):
        began = time.monotonic()
        with self.assertRaisesRegex(reader.InspectionError, "TIME_LIMIT") as failed:
            self.invoke_owned_child("import time;time.sleep(30)")
        self.assertGreaterEqual(time.monotonic()-began, 15)
        self.assertLess(time.monotonic()-began, 22)
        self.assert_reaped(failed.exception.own_observed_process)


if __name__ == "__main__":
    unittest.main()
