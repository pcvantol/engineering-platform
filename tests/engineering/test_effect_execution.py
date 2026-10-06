"""Real CENTRAL/HTTP qualification for bounded effect and result contracts."""
from __future__ import annotations

import http.server
import json
import importlib.resources
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import server
from engineering_platform import effect_contract as contract
from engineering_platform import effect_workspace as workspace
from engineering_platform import local_repository_binding, submission_service
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher
from engineering_platform.agent_state import StateStore
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.execution_repository import SubprocessRepositoryClient, GhCliClient
from engineering_platform.providers import GitProvider, CodexCliProvider
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_submission_service


def validate_public_schema(name, value):
    from jsonschema import Draft202012Validator
    schema = json.loads(importlib.resources.files("engineering_platform").joinpath("schemas", name + ".schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)


def effect(mode="READ_ONLY_ASSESSMENT", delivery="EVIDENCE_ONLY", **updates):
    return {"contract_version": "1.0", "mode": mode, "delivery": delivery,
            "source_revision": "a" * 40, "read_paths": ["docs/"],
            "write_paths": [] if delivery == "EVIDENCE_ONLY" else ["docs/"],
            "criteria": [{"id": "criterion-1", "description": "Explain the deployment boundary with source evidence."}],
            **updates}


def result(files=None):
    return {"summary": "The deployment boundary is explained using the committed architecture record.",
            "criteria": [{"id": "criterion-1", "status": "SATISFIED",
                          "analysis": "The architecture record confines deployment to the operator-owned boundary.",
                          "source_paths": ["docs/design.md"]}], "files": files or []}


class EffectContractTests(unittest.TestCase):
    def test_modes_explicit_empty_and_nonempty_writes(self):
        for mode in sorted(contract.MODES):
            delivery = "EVIDENCE_ONLY" if mode == "READ_ONLY_ASSESSMENT" else "GIT"
            value = effect(mode, delivery)
            self.assertEqual(contract.parse({"effect_contract": value}), value)
        self.assertEqual(contract.parse({"effect_contract": effect("ARCHITECTURE_DESIGN_ONLY")}),
                         effect("ARCHITECTURE_DESIGN_ONLY"))
        self.assertIsNone(contract.parse({}))
        self.assertIsNone(contract.parse(None))

    def test_malformed_scopes_and_hidden_grants(self):
        variants = [None, {}, {**effect(), "extra_authority": True}]
        for updates in ({"mode": "OTHER"}, {"contract_version": "2"}, {"source_revision": "main"},
                        {"delivery": "GIT"}, {"write_paths": ["docs/"]}, {"read_paths": []},
                        {"read_paths": ["Docs/", "docs/"]}, {"criteria": []},
                        {"criteria": [effect()["criteria"][0]] * 2}, {"read_paths": "docs/"}):
            variants.append(effect(**updates))
        for value in variants:
            with self.subTest(value=value), self.assertRaises(contract.EffectContractError):
                contract.parse({"effect_contract": value})
        for path in ("../x", "/x", "docs//x", "docs/./x", "docs/.git/x", "docs/AGENTS.md",
                     "docs/a\\b", "docs/a:b", "docs/a*", "docs/a\n", "docs/.env.secret", "docs/x."):
            with self.subTest(path=path), self.assertRaises(contract.EffectContractError):
                contract.safe_path(path)
        self.assertFalse(contract.contains(["docs/"], "docs2/secret"))

    def test_result_requires_each_source_backed_criterion_and_scoped_output(self):
        self.assertEqual(contract.validate_result(effect(), result(), {"docs/design.md"}), result())
        variants = [{}, result([{ "path": "docs/design.md", "content": "forbidden"}]),
                    {**result(), "summary": "done"}, {**result(), "criteria": []}]
        for updates in ({"id": "foreign"}, {"status": "UNSATISFIED"}, {"analysis": "done"},
                        {"source_paths": ["secret.txt"]}):
            variants.append({**result(), "criteria": [{**result()["criteria"][0], **updates}]})
        for value in variants:
            with self.subTest(value=value), self.assertRaises(contract.EffectContractError):
                contract.validate_result(effect(), value, {"docs/design.md"})
        docs = effect("DOCUMENTATION_ONLY", "GIT")
        for files in ([], [{"path": "docs/a.py", "content": "print(1)"}],
                      [{"path": "src/a.md", "content": "text"}],
                      [{"path": "docs/a.md", "content": "\x00"}],
                      [{"path": "docs/a.md", "content": "text"}] * 2):
            with self.subTest(files=files), self.assertRaises(contract.EffectContractError):
                contract.validate_result(docs, result(files), {"docs/design.md"})
        self.assertTrue(contract.validate_result(docs, result([{ "path": "docs/a.md", "content": "Useful text"}]),
                                                {"docs/design.md"}))


class EffectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.target = self.base / "target"
        self.target.mkdir()
        workspace.git(self.target, "init", "-b", "main")
        workspace.git(self.target, "config", "user.name", "Fixture")
        workspace.git(self.target, "config", "user.email", "fixture@example.invalid")
        workspace.write_file(self.target, "docs/design.md", b"The accepted architecture boundary.\n")
        workspace.write_file(self.target, "private.txt", b"out of scope\n")
        workspace.git(self.target, "add", ".")
        workspace.git(self.target, "commit", "-m", "fixture")
        self.revision = workspace.git(self.target, "rev-parse", "HEAD").decode().strip()

    def test_snapshot_contains_only_committed_approved_files(self):
        workspace.write_file(self.target, "docs/design.md", b"uncommitted override\n")
        workspace.write_file(self.target, "docs/untracked.md", b"untracked residue\n")
        destination = self.base / "snapshot"
        manifest = workspace.snapshot(self.target, destination, effect(source_revision=self.revision))
        self.assertEqual(set(manifest), {"docs/design.md"})
        self.assertEqual((destination / "docs/design.md").read_bytes(), b"The accepted architecture boundary.\n")
        self.assertFalse((destination / "private.txt").exists())
        self.assertFalse((destination / ".git").exists())
        with self.assertRaises(contract.EffectContractError):
            workspace.snapshot(self.target, destination, effect(source_revision=self.revision))

    def test_symlinks_hardlinks_and_executable_files_cannot_be_output(self):
        sentinel = self.base / "sentinel"
        sentinel.write_text("unchanged")
        (self.target / "link").symlink_to(self.base, target_is_directory=True)
        (self.target / "hard.md").hardlink_to(sentinel)
        (self.target / "sym.md").symlink_to(sentinel)
        (self.target / "exec.md").write_text("unchanged")
        (self.target / "exec.md").chmod(0o700)
        for path in ("link/sentinel", "hard.md", "sym.md", "exec.md"):
            with self.subTest(path=path), self.assertRaises((OSError, contract.EffectContractError)):
                workspace.write_file(self.target, path, b"changed")
        self.assertEqual(sentinel.read_text(), "unchanged")

    def test_immutable_artifact_rejects_replacement_and_links(self):
        path = self.base / "report.json"
        digest = workspace.immutable_json(path, {"report": "meaningful"})
        self.assertEqual(workspace.immutable_json(path, {"report": "meaningful"}), digest)
        self.assertEqual(workspace.read_json(path, digest), {"report": "meaningful"})
        with self.assertRaises(contract.EffectContractError):
            workspace.immutable_json(path, {"report": "replacement"})
        link = self.base / "link.json"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            workspace.read_json(link, digest)

    def test_real_sandbox_denies_target_scratch_escape_and_network(self):
        scratch = self.base / "scratch"
        scratch.mkdir()
        snapshot = self.base / "snapshot"
        snapshot.mkdir()
        (scratch / "escape").symlink_to(self.target, target_is_directory=True)
        probe = self.base / "probe.py"
        probe.write_text("""import errno, os, socket, sys
for path in sys.argv[1:-1]:
    try:
        with open(path, 'w') as handle: handle.write('forbidden')
    except OSError as error:
        if error.errno not in {errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS}: raise
    else: raise SystemExit('write was allowed')
try:
    socket.create_connection(('127.0.0.1', 1), timeout=1)
except PermissionError: pass
except OSError: raise SystemExit('network was not sandbox denied')
else: raise SystemExit('network was allowed')
# An ungranted ancestor may exist only in Linux's private mount namespace.
# The parent process must prove this can never create the host-side file.
try:
    with open(sys.argv[-1], 'w') as handle: handle.write('namespace-only')
except OSError as error:
    if error.errno not in {errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS}: raise
""")
        # The executable probe itself is copied to the explicitly readable input.
        (snapshot / "probe.py").write_bytes(probe.read_bytes())
        import sys
        command = workspace.sandbox_command(snapshot, (sys.executable, str(snapshot / "probe.py"),
            str(self.target / "docs/design.md"), str(self.target / ".git/index.lock"),
            str(self.target / ".ignored"), str(self.target / "temporary-then-reverted"),
            str(snapshot / "forbidden-source-write"), str(scratch / "escape/private.txt"),
            str(snapshot.parent / "namespace-only")), scratch=scratch)
        completed = subprocess.run(command, env=workspace.child_environment(), text=True,
                                   capture_output=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse((snapshot.parent / "namespace-only").exists())
        self.assertFalse((snapshot / "forbidden-source-write").exists())
        self.assertEqual(workspace.git(self.target, "status", "--porcelain").decode().strip(), "")


class EffectSubmissionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_submission_service.CanonicalSubmissionServiceTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.httpd.data_root = self.fixture.root
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.worker.join(timeout=5)
        self.httpd.server_close()

    def test_unknown_effect_mode_is_rejected_before_durable_acceptance(self):
        request = {
            "repository_id": "djconnect",
            "producer": {"id": "cli", "type": "CLI"},
            "prompt": "Assess the bounded repository questions.",
            "constraints": {"effect_contract": {"contract_version": "1.0", "mode": "UNBOUNDED_WRITE"}},
        }
        endpoint = f"http://127.0.0.1:{self.httpd.server_port}/v1/projects/djconnect/submissions"
        with self.assertRaises(HTTPError) as rejected:
            urlopen(Request(endpoint, data=json.dumps(request).encode(), headers={  # nosec B310
                "Authorization": f"Bearer {self.fixture.credential}",
                "Content-Type": "application/json",
            }, method="POST"))
        self.assertEqual(rejected.exception.code, 400)
        rejected.exception.close()
        with sqlite_connection(self.fixture.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM ep_submissions").fetchone()[0], 0)


class EffectLifecycleTests(unittest.TestCase):
    """Real HTTP, dispatcher, host, CENTRAL, controls and leases; only CLI LLM boundary simulated."""
    def setUp(self):
        self.fixture = test_submission_service.CanonicalSubmissionServiceTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.base = Path(self.fixture.temporary.name).resolve()
        self.root = self.base / "target"
        self.root.mkdir()
        workspace.git(self.root, "init", "-b", "main")
        workspace.git(self.root, "config", "user.name", "Fixture")
        workspace.git(self.root, "config", "user.email", "fixture@example.invalid")
        workspace.git(self.root, "remote", "add", "origin", "https://github.com/fixture/djconnect.git")
        workspace.write_file(self.root, "BOOTSTRAP.md", b"# Bound fixture repository\n")
        workspace.write_file(self.root, "docs/design.md", b"# Design\nDeployment uses the explicit operator-owned boundary.\n")
        workspace.write_file(self.root, "tests/test_fixture.py", b"import pathlib,unittest\nclass Boundary(unittest.TestCase):\n def test_committed_source(self):\n  self.assertIn('operator-owned',pathlib.Path('docs/design.md').read_text())\n")
        declaration = {"schema_version": "1.0", "project": {"id": "djconnect", "authority_repository_id": "djconnect"},
                       "repository": {"id": "djconnect", "role": "authority"},
                       "validation": {"kind": "command", "entrypoint": "python -m unittest discover -s tests -v"},
                       "requirements": {"host": {}, "tools": {}}, "integrations": {}}
        workspace.write_file(self.root, ".engineering-platform/repository.json", json.dumps(declaration).encode())
        workspace.git(self.root, "add", ".")
        workspace.git(self.root, "commit", "-m", "Bound source fixture")
        self.revision = workspace.git(self.root, "rev-parse", "HEAD").decode().strip()
        self.database = self.fixture.root / server.SERVER_DATABASE_FILENAME
        with sqlite_connection(self.database) as connection:
            connection.execute("DELETE FROM ep_local_repository_bindings WHERE project_id='djconnect'")
            local_repository_binding.bind_local_repository(connection, project_id="djconnect", repository_id="djconnect",
                                                           local_root=self.root, data_root=self.fixture.root)
        self.prefix = self.base / "provider"
        (self.prefix / "bin").mkdir(parents=True)
        self.provider = self.prefix / "bin/codex"
        import sys
        self.provider.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
args=sys.argv[1:]
if args==['--version']:
    print('codex-cli 0.160.1'); raise SystemExit(0)
if args[:2]==['login','status']:
    print('Logged in'); raise SystemExit(0)
if args[:2]==['mcp','list']:
    print('[]'); raise SystemExit(0)
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
request=json.loads(sys.stdin.read())
trace=pathlib.Path(__file__).parents[1]/'calls.jsonl'
with trace.open('a') as log: log.write(json.dumps({'args':args,'request':request})+'\\n')
if 'reviewer' in request:
    output={'contract_version':'3.0','contribution':'Exact effect result reviewed.','recommendations':[], 'findings':[],
       'coverage':{key:{'status':'REVIEWED','evidence_ref':request['subject']['subject_digest']}
                   for key in schema['properties']['coverage']['required']},'finding_dispositions':{}}
else:
    output={'summary':'The deployment boundary is explained using the committed architecture record.',
        'criteria':[{'id':'criterion-1','status':'SATISFIED',
            'analysis':'The architecture record confines deployment to the operator-owned boundary.',
            'source_paths':['docs/design.md']}], 'files':[]}
    effects=request['effect_contract']
    if effects['mode']=='ARCHITECTURE_DESIGN_ONLY':
        output['summary']+='\\n## Alternatives\\nAn external broker was considered.\\n## Boundaries\\nThe operator owns deployment.\\n## Decision\\nRetain the explicit boundary.\\n## Open questions\\nNone within this assessment.'
    if effects['delivery']=='GIT':
        output['files']=[{'path':'docs/report.md','content':'# Deployment assessment\\nThe operator-owned boundary remains explicit. See [source](design.md).\\n'}]
        if effects['mode']=='ARCHITECTURE_DESIGN_ONLY': output['files'][0]['content']+=output['summary']
        if effects['mode']=='BOUNDED_REPOSITORY_CHANGE':
            output['files']=[{'path':'src/boundary.py','content':'def deployment_boundary():\\n    return "operator-owned"\\n'}]
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(output)}}))
''')
        self.provider.chmod(0o700)
        self.environment = patch.dict(os.environ, {"EP_MANAGED_CODEX_CLI_PREFIX": str(self.prefix)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.httpd.data_root = self.fixture.root
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.worker.join(timeout=5)
        self.httpd.server_close()

    def http(self, path, payload=None, credential=None):
        endpoint = f"http://127.0.0.1:{self.httpd.server_port}" + path
        with urlopen(Request(endpoint, data=None if payload is None else json.dumps(payload).encode(), headers={  # nosec B310
            "Authorization": "Bearer " + (credential or self.fixture.credential), "Content-Type": "application/json",
        })) as response:
            return json.load(response)

    def submit(self, effects):
        validate_public_schema("effect-request-v1", effects)
        receipt = self.http("/v1/projects/djconnect/submissions", {
                "repository_id": "djconnect", "producer": {"id": "cli", "type": "CLI"},
                "prompt": "Assess the deployment boundary from the committed architecture record.",
                "idempotency_key": effects['mode'] + '-' + effects['delivery'], "engineering_action_id": "action-effect",
                "constraints": {"effect_contract": effects},
            })
        return receipt["submission_id"]

    def test_read_only_qualified_report_and_resume_leave_target_unchanged(self):
        submission = self.submit(effect(source_revision=self.revision))
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        with sqlite_connection(self.database) as connection:
            payload = json.loads(connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (receipt.run_id,)).fetchone()[0])
        self.assertEqual(receipt.state, "COMPLETE", payload)
        self.assertEqual(payload["terminal_condition"], "effect_report_qualified")
        self.assertIsNone(payload["implementation_head_sha"])
        self.assertIsNone(payload["implementation_merge_commit"])
        self.assertIsNone(payload["assurance_profile"])
        attempt = payload["effect_execution"]["attempts"][0]
        self.assertEqual(len(attempt["controls"]), 4)
        self.assertEqual([review["status"] for review in attempt["reviews"]], ["PASS", "PASS"])
        self.assertTrue(all(item["exit_code"] == 0 for item in attempt["controls"]))
        calls = (self.prefix / "calls.jsonl").read_text().splitlines()
        self.assertEqual(len(calls), 3)
        report = self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")
        validate_public_schema("effect-result-v1.1", report)
        validate_public_schema("effect-report-envelope-v1", report["artifact"]["content"])
        readback = self.http(f"/v1/projects/djconnect/submissions/{submission}")
        terminal_id = readback["evidence"]["terminal_artifact"]["id"]
        terminal = self.http(f"/v1/projects/djconnect/artifacts/{terminal_id}")
        validate_public_schema("terminal-evidence-v1.6", terminal)
        self.assertTrue(terminal["run"]["effect_qualified"])
        self.assertIsNone(terminal["repository"]["revision"])
        self.assertTrue(report["effect_qualified"])
        self.assertEqual(report["subject"]["source_revision"], self.revision)
        self.assertIsNone(report["subject"]["candidate_revision"])
        self.assertEqual(report["artifact"]["digest"], "sha256:" + contract.digest(report["artifact"]["content"]))
        self.assertEqual(report["artifact"]["content"]["result"], result())
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / ".engineering").exists())

    def test_new_process_reopens_complete_report_without_another_provider(self):
        import sys
        submission = self.submit(effect(source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        before = (self.prefix / "calls.jsonl").read_bytes()
        code = """import sys
from pathlib import Path
from engineering_platform.parity_lifecycle_dispatcher import _default_runner, _historical_admission_environment
from engineering_platform.storage import sqlite_connection
import json
root,data,run_id=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
database=data/'ep.sqlite3'
with sqlite_connection(database) as connection:
    state=json.loads(connection.execute('SELECT payload FROM engineering_transactions WHERE run_id=?',(run_id,)).fetchone()[0])
with _historical_admission_environment(root,data):
    runner=_default_runner(root,central_database=database)
    resumed=runner.run(Path(state['prompt_path']),run_id=run_id,resume=True,owner_authorized=True)
print(resumed.phase)
""".replace("database=data/'ep.sqlite3'", "database=data/" + repr(server.SERVER_DATABASE_FILENAME))
        reopened = subprocess.run((sys.executable, "-c", code, str(self.root), str(self.fixture.root), receipt.run_id),
                                  env=os.environ | {"PYTHONPATH": str(Path(__import__('engineering_platform').__file__).resolve().parent.parent)},
                                  text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertEqual(reopened.stdout.strip(), "COMPLETE")
        self.assertEqual((self.prefix / "calls.jsonl").read_bytes(), before)

    def test_report_readback_enforces_producer_and_detects_changed_bytes(self):
        submission = self.submit(effect(source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        path = f"/v1/projects/djconnect/submissions/{submission}/effect-result"
        with sqlite_connection(self.database) as connection:
            other = submission_service.issue_consumer_credential(connection, consumer_id="other", project_id="djconnect")["credential"]
        for credential, expected in ((other, 404), ("invalid-credential", 401)):
            with self.subTest(credential=expected), self.assertRaises(HTTPError) as denied:
                self.http(path, credential=credential)
            self.assertEqual(denied.exception.code, expected)
            denied.exception.close()
        report = self.http(path)
        target = self.fixture.root / "artifacts/effects" / receipt.run_id / "result-0.json"
        target.write_text("{}")
        with self.assertRaises(HTTPError) as corrupt:
            self.http(path)
        self.assertEqual(corrupt.exception.code, 409)
        corrupt.exception.close()
        self.assertTrue(report["effect_qualified"])

    def test_design_evidence_only_is_useful_without_git_changes(self):
        submission = self.submit(effect("ARCHITECTURE_DESIGN_ONLY", source_revision=self.revision))
        receipt = ParityLifecycleDispatcher(self.fixture.root).dispatch(submission)
        report = self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")
        self.assertEqual(receipt.state, "COMPLETE", report)
        validate_public_schema("effect-result-v1.1", report)
        self.assertTrue(report["effect_qualified"])
        self.assertEqual(len(report["validation_controls"]), 6)
        self.assertIsNone(report["delivery"]["revision"])
        self.assertIn("## Alternatives", report["artifact"]["content"]["result"]["summary"])

    def test_git_modes_publish_only_scoped_files_and_recover_same_pr(self):
        for mode in ("DOCUMENTATION_ONLY", "ARCHITECTURE_DESIGN_ONLY", "BOUNDED_REPOSITORY_CHANGE"):
            with self.subTest(mode=mode):
                bare = self.base / (mode + ".git")
                workspace.git(self.base, "clone", "--bare", str(self.root), str(bare))
                git_transport = _LocalGitTransport(bare)
                github_transport = _LocalGitHubTransport(bare)
                def factory(root):
                    return EngineeringRunner(root, StateStore(root / ".engineering/engineering-runs",
                        central_database=self.database, emit_local_projection=False),
                        SubprocessRepositoryClient(git_transport), GhCliClient(github_transport, repository="fixture/djconnect"),
                        CodexCliClient(CodexCliProvider()))
                # The readiness executable is an external authentication seam;
                # product admission, validation and publication remain real.
                github_cli = self.base / "gh"
                github_cli.write_text("#!/bin/sh\nexit 0\n")
                github_cli.chmod(0o700)
                with patch.dict(os.environ, {"EP_GITHUB_CLI_EXECUTABLE": str(github_cli)}):
                    expected = "src/boundary.py" if mode == "BOUNDED_REPOSITORY_CHANGE" else "docs/report.md"
                    submission = self.submit(effect(mode, "GIT", source_revision=self.revision,
                                                    write_paths=[expected]))
                    dispatcher = ParityLifecycleDispatcher(self.fixture.root, runner_factory=factory)
                    receipt = dispatcher.dispatch(submission)
                    with sqlite_connection(self.database) as connection:
                        payload = json.loads(connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?", (receipt.run_id,)).fetchone()[0])
                    self.assertEqual(payload["phase"], "WAIT_FOR_OPERATOR_MERGE", (payload["next_action"], payload["diagnostic"]))
                    self.assertEqual(github_transport.creates, 1)
                    self.assertEqual(workspace.git(bare, "diff", "--name-only", "main", github_transport.head).decode().strip(), expected)
                    self.assertFalse((self.root / expected).exists())
                    calls = len((self.prefix / "calls.jsonl").read_text().splitlines())
                    github_transport.protected_merge()
                    completed = dispatcher.dispatch(submission)
                    report = self.http(f"/v1/projects/djconnect/submissions/{submission}/effect-result")
                    validate_public_schema("effect-result-v1.1", report)
                    self.assertEqual(completed.state, "COMPLETE", report)
                    self.assertTrue(report["effect_qualified"])
                    self.assertEqual(report["delivery"]["revision"], github_transport.head)
                    self.assertEqual(github_transport.creates, 1)
                    self.assertEqual(len((self.prefix / "calls.jsonl").read_text().splitlines()), calls)


class _LocalGitTransport(GitProvider):
    """Replace only remote transport; all Git objects, refs and product checks are real."""
    def __init__(self, bare):
        self.bare = bare

    def execute(self, root, *args):
        if args[0] == "git" and args[1] in {"push", "fetch", "ls-remote"}:
            args = tuple(str(self.bare) if item == "origin" else item for item in args)
            if args[1] == "fetch":
                args = tuple("refs/heads/main:refs/remotes/origin/main" if item == "main" else item for item in args)
        return super().execute(root, *args)


class _LocalGitHubTransport:
    """Fixture for gh's external API; never replaces host lifecycle decisions."""
    def __init__(self, bare):
        self.bare, self.creates, self.branch, self.head = bare, 0, None, None
        self.draft, self.merged = True, False
        self.ledger = bare / "external-fixture-pr.json"
        if self.ledger.exists():
            for key, value in json.loads(self.ledger.read_text()).items(): setattr(self, key, value)

    def persist(self):
        self.ledger.write_text(json.dumps({key: getattr(self, key) for key in ("creates", "branch", "head", "draft", "merged")}))

    def github(self, *args):
        if args[0] == "api":
            items = [] if self.head is None else [{"number": 17, "state": "closed" if self.merged else "open",
                "draft": self.draft, "merged_at": "2026-10-06T12:00:00Z" if self.merged else None,
                "head": {"ref": self.branch, "sha": self.head, "repo": {"full_name": "fixture/djconnect"}},
                "base": {"ref": "main", "repo": {"full_name": "fixture/djconnect"}}}]
            return json.dumps([items])
        if args[:2] == ("pr", "create"):
            self.creates += 1
            self.branch = args[args.index("--head") + 1]
            self.head = workspace.git(self.bare, "rev-parse", self.branch).decode().strip()
            self.persist()
            return ""
        if args[:2] == ("pr", "ready"):
            self.draft = False
            self.persist()
            return ""
        if args[:2] == ("pr", "view"):
            return json.dumps({"number": 17, "state": "MERGED" if self.merged else "OPEN", "isDraft": self.draft,
                "mergeCommit": {"oid": self.head} if self.merged else None,
                "statusCheckRollup": [{"name": "required", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                "headRefName": self.branch, "headRefOid": self.head, "baseRefName": "main", "mergeStateStatus": "CLEAN"})
        raise AssertionError(args)

    def protected_merge(self):
        assert self.head is not None and self.draft is False
        workspace.git(self.bare, "update-ref", "refs/heads/main", self.head)
        self.merged = True
        self.persist()


if __name__ == "__main__":
    unittest.main()
