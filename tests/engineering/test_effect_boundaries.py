"""Negative FME authority/integrity cases and actual installed CLI enforcement."""
from __future__ import annotations

from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import effect_contract as contract, effect_provider, effect_state, effect_validation
from engineering_platform import effect_workspace as workspace
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.providers import CodexCliProvider
from tests.engineering.test_effect_execution import effect, result
from tests.engineering import test_effect_execution as fixtures


class EffectControlTests(unittest.TestCase):
    setUp = fixtures.EffectWorkspaceTests.setUp

    def test_native_runtime_grants_only_the_reexecuted_file(self):
        package = self.base / "node_modules/@openai/codex"
        (package / "bin").mkdir(parents=True)
        launcher = package / "bin/codex.js"
        launcher.write_text("// qualified npm layout fixture\n")
        selected = self.base / "selected-codex"
        selected.symlink_to(launcher)
        platforms = (("Linux", "x86_64", "linux-x64", "x86_64-unknown-linux-musl"),
                     ("Linux", "aarch64", "linux-arm64", "aarch64-unknown-linux-musl"),
                     ("Darwin", "x86_64", "darwin-x64", "x86_64-apple-darwin"),
                     ("Darwin", "arm64", "darwin-arm64", "aarch64-apple-darwin"))
        for system, machine, name, triple in platforms:
            for bundled in (False, True):
                installation = package if bundled else package.parent / ("codex-" + name)
                binary = installation / "vendor" / triple / "bin/codex"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o700)
                with self.subTest(system=system, machine=machine, bundled=bundled), \
                        patch.object(workspace.platform, "system", return_value=system), \
                        patch.object(workspace.platform, "machine", return_value=machine):
                    import tomllib
                    options = workspace.sandbox_options(self.target, runtime=selected)
                    grants = tomllib.loads(options[1])["permissions"]["ep-effects"]["filesystem"]
                    self.assertEqual(grants[str(binary)], "read")
                    for directory in (binary.parent, installation, package.parent, self.base):
                        self.assertNotIn(str(directory), grants)
                    self.assertNotIn(str(launcher), grants)
                binary.unlink()
        with patch.object(workspace.platform, "system", return_value="unsupported"):
            with self.assertRaisesRegex(contract.EffectContractError, "RUNTIME_UNAVAILABLE"):
                workspace.native_runtime_file(selected)
        with self.assertRaises(FileNotFoundError):
            workspace.native_runtime_file(selected)
        direct = self.base / "native-codex"
        direct.write_text("fixture")
        with self.assertRaisesRegex(contract.EffectContractError, "RUNTIME_UNAVAILABLE"):
            workspace.native_runtime_file(direct)
        direct.chmod(0o700)
        self.assertEqual(workspace.native_runtime_file(direct), direct)

    def envelope(self):
        source = self.base / "input"
        effects = effect(source_revision=self.revision)
        manifest = workspace.snapshot(self.target, source, effects)
        return source, {"contract": effects, "contract_digest": contract.digest(effects),
                        "source_manifest": manifest, "source_manifest_digest": contract.digest(manifest),
                        "binding": {"source_revision": self.revision}, "result": result()}

    def test_each_report_control_executes_and_rejects_wrong_identity(self):
        source, envelope = self.envelope()
        for control in effect_validation.control_ids(envelope["contract"]):
            effect_validation.execute(control, envelope, source)
        with self.assertRaisesRegex(contract.EffectContractError, "UNKNOWN_EFFECT_CONTROL"):
            effect_validation.execute("invented_control", envelope, source)
        for key in ("contract_digest", "source_manifest_digest"):
            with self.subTest(key=key), self.assertRaises(contract.EffectContractError):
                effect_validation.execute("effect_source_binding", {**envelope, key: "wrong"}, source)
        wrong = deepcopy(envelope)
        wrong["binding"]["source_revision"] = "b" * 40
        with self.assertRaises(contract.EffectContractError):
            effect_validation.execute("effect_source_binding", wrong, source)
        (source / "docs/design.md").write_text("changed")
        with self.assertRaises(contract.EffectContractError):
            effect_validation.execute("effect_scope_containment", envelope, source)

    def test_document_links_content_and_design_criteria(self):
        source, envelope = self.envelope()
        envelope["contract"] = effect("ARCHITECTURE_DESIGN_ONLY", "GIT", source_revision=self.revision)
        text = ("# Decision record\nSee [source](./design.md) and [external](https://example.invalid).\n"
                "## Alternatives\nAn isolated service.\n## Boundaries\nOperator-owned.\n"
                "## Decision\nRetain the existing model.\n## Open questions\nNo remaining questions.\n")
        envelope["result"] = result([{"path": "docs/adr.md", "content": text}])
        for control in ("document_content_links_schema", "design_criteria_contract"):
            effect_validation.execute(control, envelope, source)
        for link in ("../../escape", "/absolute", "file:///private", "missing.md", "%2e%2e/%2e%2e/escape", "a\\b"):
            with self.subTest(link=link), self.assertRaises(contract.EffectContractError):
                effect_validation.validate_document_links(result([{"path": "docs/adr.md", "content": text + f"[bad]({link})"}]), {"docs/design.md"})
        effect_validation.validate_document_links(result([{"path": "docs/adr.md", "content": text + "[up](../docs/design.md) [anchor](#decision)"}]), {"docs/design.md"})
        with self.assertRaisesRegex(contract.EffectContractError, "DOCUMENT_EMPTY"):
            effect_validation.validate_document_links(result([{"path": "docs/a.md", "content": "# Done"}]), set())
        envelope["result"]["files"][0]["content"] = text.replace("## Alternatives", "## Missing")
        with self.assertRaisesRegex(contract.EffectContractError, "DESIGN_SECTION"):
            effect_validation.execute("design_criteria_contract", envelope, source)

    def test_control_cli_never_reports_unavailable_as_pass(self):
        source, envelope = self.envelope()
        path = self.base / "report.json"
        fingerprint = workspace.immutable_json(path, envelope)
        with patch.object(sys, "argv", ["control", "report_criteria_contract", str(path), fingerprint, str(source)]):
            self.assertEqual(effect_validation.main(), 0)
        with patch.object(sys, "argv", ["control", "report_criteria_contract", str(path), "b" * 64, str(source)]):
            self.assertEqual(effect_validation.main(), 1)

    def test_required_repository_validator_has_no_fallback(self):
        path = self.target / ".engineering-platform/repository.json"
        path.parent.mkdir()
        for validation in ({}, {"kind": "none"}, {"kind": "command", "entrypoint": ""},
                           {"kind": "command", "entrypoint": "python ok.py && bypass"},
                           {"kind": "command", "entrypoint": "PYTHONPATH=src"},
                           {"kind": "script", "entrypoint": "GIT_NO_REPLACE_OBJECTS=0 bash scripts/validate.sh"}):
            path.write_text(json.dumps({"validation": validation}))
            with self.subTest(validation=validation), self.assertRaises(contract.EffectContractError):
                effect_validation.repository_command(self.target)
        path.write_text(json.dumps({"validation": {"kind": "command", "entrypoint": "python -m unittest"}}))
        self.assertEqual(effect_validation.repository_command(self.target), (sys.executable, "-m", "unittest"))
        path.write_text(json.dumps({"validation": {"kind": "command", "entrypoint": "node test.mjs"}}))
        self.assertEqual(effect_validation.repository_command(self.target), ("node", "test.mjs"))

    def test_source_scope_and_artifact_paths_fail_closed(self):
        source, envelope = self.envelope()
        with self.assertRaisesRegex(contract.EffectContractError, "ALREADY_EXISTS"):
            workspace.snapshot(self.target, source, envelope["contract"])
        with self.assertRaisesRegex(contract.EffectContractError, "SCOPE_UNAVAILABLE"):
            workspace.snapshot(self.target, self.base / "absent", effect(source_revision=self.revision, read_paths=["missing/"]))
        (source / "extra").write_text("ungranted")
        with self.assertRaisesRegex(contract.EffectContractError, "SOURCE_CHANGED"):
            workspace.verify_snapshot(source, envelope["source_manifest"])
        (source / "extra").unlink()
        (source / "docs/design.md").unlink()
        with self.assertRaisesRegex(contract.EffectContractError, "SCOPE_CHANGED"):
            workspace.verify_snapshot(source, envelope["source_manifest"])
        link = self.base / "source-link"
        link.symlink_to(source)
        with self.assertRaisesRegex(contract.EffectContractError, "SOURCE_CHANGED"):
            workspace.verify_snapshot(link, envelope["source_manifest"])
        artifact = self.base / "large.json"
        artifact.write_bytes(b"x" * 2097153)
        with self.assertRaisesRegex(contract.EffectContractError, "UNSAFE_EFFECT_ARTIFACT"):
            workspace.read_json(artifact)
        with self.assertRaisesRegex(contract.EffectContractError, "GIT_OBSERVATION_FAILED"):
            workspace.git(self.target, "rev-parse", "nonexistent")
        with patch("engineering_platform.effect_workspace.shutil.which", return_value=None):
            with self.assertRaisesRegex(contract.EffectContractError, "SANDBOX_UNAVAILABLE"):
                workspace.sandbox_command(source, ("true",))

    def test_bounded_control_process_and_scratch_cleanup(self):
        scratch = self.base / "validation-scratch-0-0"
        scratch.mkdir()
        (scratch / "ordinary").write_text("temporary")
        (self.base / "report.json").write_text("retained")
        workspace.cleanup_scratch(self.base)
        self.assertFalse(scratch.exists())
        self.assertTrue((self.base / "report.json").exists())
        scratch.symlink_to(self.target)
        with self.assertRaisesRegex(contract.EffectContractError, "UNSAFE_EFFECT_SCRATCH"):
            workspace.cleanup_scratch(self.base)
        self.assertEqual(workspace.run_control((sys.executable, "-c", "raise SystemExit(7)"), workspace.child_environment()), 7)
        with self.assertRaises(subprocess.TimeoutExpired):
            workspace.run_control((sys.executable, "-c", "import time;time.sleep(30)"), workspace.child_environment(), timeout=1)

    def test_secret_bearing_output_is_rejected_before_storage(self):
        for secret in ("ghp_" + "a" * 30, "password=fixture-sensitive-value", "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(secret_kind=secret[:4]), self.assertRaisesRegex(contract.EffectContractError, "SENSITIVE_CONTENT"):
                contract.validate_result(effect(), {**result(), "summary": result()["summary"] + secret}, {"docs/design.md"})


class EffectProviderTests(unittest.TestCase):
    def test_failure_responses_and_scoped_review_boundaries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            class ExternalCLI:
                def __init__(self, outputs): self.outputs = iter(outputs)
                def invoke(self, *args, **kwargs): return next(self.outputs)
            def observed(stdout, code=0): return SimpleNamespace(stdout=stdout, stderr="", returncode=code)
            for outputs, reason in (([observed("codex-cli 0.1")], "VERSION_UNQUALIFIED"),
                ([observed("codex-cli 0.160.1"), observed("not json")], "TOOL_POLICY"),
                ([observed("codex-cli 0.160.1"), observed('[{"name":"unsafe.name"}]')], "TOOL_POLICY")):
                with self.subTest(reason=reason), self.assertRaisesRegex(contract.EffectContractError, reason):
                    effect_provider.policy(SimpleNamespace(provider=ExternalCLI(outputs)), root)
            client = SimpleNamespace(provider=ExternalCLI([observed("codex-cli 0.160.1"), observed('[{"name":"fixture"}]')]))
            options = effect_provider.policy(client, root)
            self.assertIn("mcp_servers.fixture.enabled=false", options)
            self.assertEqual(effect_provider.review_input(), {})
            self.assertEqual(effect_provider.schema_directory(root), root / ".engineering")
            self.assertEqual(effect_provider.restrict_review(("unchanged",)), ("unchanged",))
            with effect_provider.scoped(root, options, "exact result"):
                self.assertEqual(effect_provider.review_input()["input_text"], "exact result")
                self.assertEqual(effect_provider.schema_directory(root), root)
                with self.assertRaisesRegex(contract.EffectContractError, "REVIEW_BOUNDARY_CHANGED"):
                    effect_provider.restrict_review(("changed",))
            for output, reason in ((observed("", 1), "PROVIDER_FAILED"), (observed("not-json"), "RESULT_INVALID")):
                client = SimpleNamespace(provider=ExternalCLI([output]))
                with self.subTest(reason=reason), self.assertRaisesRegex(contract.EffectContractError, reason):
                    effect_provider.propose(client, root, root, options, {})

    def test_shared_privacy_guard_stays_stateless_in_the_actual_native_control_sandbox(self):
        from engineering_platform import effect_contract
        from engineering_platform.agent_state import CREDENTIAL_SHAPE_PATTERN
        self.assertIs(CREDENTIAL_SHAPE_PATTERN,effect_contract.CREDENTIAL_SHAPE_PATTERN)
        package=Path(effect_contract.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix='ep-private-control-') as area:
            root=Path(area)
            code="""import sys
from engineering_platform.effect_contract import require_redacted,EffectContractError
require_redacted({'safe':'bounded evidence'})
assert 'sqlite3' not in sys.modules
for family in ('ghp_','gho_','ghu_','ghs_','ghr_'):
 try:require_redacted({'source':family+'A'*36})
 except EffectContractError:pass
 else:raise AssertionError('Sensitive source accepted')
assert 'sqlite3' not in sys.modules
print('STATELESS_PRIVATE_CONTROL=PASS')
"""
            command=workspace.sandbox_command(root,(sys.executable,'-c',code),readable=(package,))
            observed=subprocess.run(command,env=workspace.child_environment()|{'PYTHONPATH':str(package)},
                capture_output=True,text=True,timeout=20)
            self.assertEqual(observed.returncode,0,observed.stderr)
            self.assertIn('STATELESS_PRIVATE_CONTROL=PASS',observed.stdout)

    def test_actual_specialist_cli_denies_inherited_effects_and_escalation(self):
        self._assert_actual_installed_cli_denies_effects_and_escalation(specialist=True)

    def test_actual_installed_cli_denies_effects_and_escalation(self):
        self._assert_actual_installed_cli_denies_effects_and_escalation()

    def _assert_actual_installed_cli_denies_effects_and_escalation(self, *, specialist=False):
        """Only the external model HTTP transport is replaced; tool execution is real."""
        requests, probes = [], []
        output=result()
        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                item = {"id": "message", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(output)}]}
                if len(requests) <= len(probes):
                    name, arguments = probes[len(requests) - 1]
                    item = {"type": "function_call", "id": "function", "call_id": "call_" + str(len(requests)),
                            "name": name, "arguments": json.dumps(arguments), "status": "completed"}
                events = [{"type": "response.created", "response": {"id": "fixture", "status": "in_progress", "output": []}},
                          {"type": "response.output_item.added", "output_index": 0, "item": item},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": {"id": "fixture", "status": "completed", "output": [item],
                              "usage": {"input_tokens": 20, "output_tokens": 20, "total_tokens": 40}}}]
                data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        with tempfile.TemporaryDirectory(prefix="ep-fme-native-") as raw:
            root = Path(raw).resolve()
            source = root / "source"
            source.mkdir()
            workspace.write_file(source, "docs/design.md", b"Operator-owned deployment boundary.\n")
            selection=None
            if specialist:
                from engineering_platform.capability_review import select_reviewers,SPECIALIST_CONTRACT_VERSION
                from dataclasses import replace
                for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),('add','.'),('commit','-qm','source')):
                    subprocess.run(('git',*args),cwd=source,check=True,capture_output=True)
                sha=subprocess.check_output(('git','rev-parse','HEAD'),cwd=source,text=True).strip()
                objective='Specialist review requests: '+json.dumps([{'reviewer':'documentation','question':'Which deployment detail is missing?',
                    'paths':['docs/design.md'],'consumer':'EXECUTE_AGENT','risk':'NORMAL'}])
                selection=select_reviewers(objective,source/'objective.md','IMPLEMENTATION',{},root=source,run_id='sandbox-run',
                    repository='qualification/managed',candidate_sha=sha,qualified_roles=('documentation',),remaining_percent=100,reserve_percent=0).selections[0]
                selection=replace(selection,specialist_binding={**selection.specialist_binding,'invocation_id':'a'*32})
                output={'contract_version':SPECIALIST_CONTRACT_VERSION,'contribution':'Bounded advice','recommendations':[],
                        'specialist_binding':dict(selection.specialist_binding),'findings':[]}
            artifacts, prefix, home, protected = (root / name for name in ("artifacts", "provider", "cli-home", "protected"))
            for path in (artifacts, prefix / "bin", home, protected): path.mkdir(parents=True)
            binary = shutil.which("codex")
            self.assertIsNotNone(binary, "The pinned real sandbox CLI is required, never skipped.")
            (prefix / "bin/codex").symlink_to(binary)
            (protected / "private.txt").write_text("private fixture sentinel")
            # Inherited dangerous configuration must not widen the unique profile.
            (home / "config.toml").write_text('sandbox_mode="danger-full-access"\napproval_policy="never"\n')
            configured = subprocess.run((binary, "mcp", "add", "fixture", "--", "must-not-execute"),
                env=os.environ | {"CODEX_HOME": str(home)}, capture_output=True, text=True, timeout=15)
            self.assertEqual(configured.returncode, 0, configured.stderr)
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
            worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
            code = """import pathlib,socket,json
r={}
for name,action in [('write',lambda:(pathlib.Path(TARGET)/'changed').write_text('bad')),('read',lambda:(pathlib.Path(TARGET)/'private.txt').read_text()),('network',lambda:socket.create_connection(('127.0.0.1',1),timeout=1))]:
 try: action();r[name]='ALLOWED'
 except OSError as e:r[name]=type(e).__name__
print(json.dumps(r))
""".replace("TARGET", repr(str(protected)))
            command = shlex.join((str(Path(sys.executable).resolve()), "-c", code))
            probes.extend([("exec_command", {"cmd": command, "yield_time_ms": 1000}),
                           ("exec_command", {"cmd": command, "sandbox_permissions": "require_escalated", "justification": "Must deny fixture"})])
            try:
                with patch.dict(os.environ, {"EP_MANAGED_CODEX_CLI_PREFIX": str(prefix), "CODEX_HOME": str(home)}):
                    client = CodexCliClient(CodexCliProvider())
                    options = effect_provider.policy(client, source) + (
                        "-c", 'model="fixture"', "-c", 'model_provider="fixture"', "-c",
                        f'model_providers.fixture={{name="fixture",base_url="http://127.0.0.1:{httpd.server_port}/v1",wire_api="responses",requires_openai_auth=false}}')
                    if specialist:
                        original_policy=effect_provider.policy
                        # Only the external model endpoint is replaced; the exact production
                        # policy and snapshot wrapper still execute and enforce tool access.
                        model_options=options[-6:]
                        def local_model_policy(client,root):
                            return original_policy(client,root)+model_options
                        with patch.object(effect_provider,'policy',side_effect=local_model_policy):
                            observed=client.review(source,selection,objective)
                        self.assertFalse(observed.failed,observed.contribution)
                        self.assertEqual(observed.specialist_binding,selection.specialist_binding)
                    else:
                        self.assertEqual(effect_provider.propose(client, source, artifacts, options, {"effect_contract": effect()}), result())
            finally:
                httpd.shutdown(); worker.join(); httpd.server_close()
            self.assertEqual(len(requests), 3)
            self.assertLessEqual({item.get("name", item.get("type")) for item in requests[0]["tools"]},
                                 {"exec_command", "write_stdin", "request_user_input", "view_image"})
            outputs = json.dumps([item for request in requests for item in request.get("input", []) if item.get("type") == "function_call_output"])
            self.assertIn("PermissionError", outputs)
            self.assertNotIn("ALLOWED", outputs)
            self.assertIn("Never", outputs)
            self.assertFalse((protected / "changed").exists())
            self.assertEqual((protected / "private.txt").read_text(), "private fixture sentinel")


if __name__ == "__main__":
    unittest.main()
