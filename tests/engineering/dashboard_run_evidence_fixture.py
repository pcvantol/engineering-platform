"""Real persisted Console canary; only external model/GitHub transports double.

Topology, binding, submission, admission, runner, Git, controls, journal, leases,
publication, CENTRAL readback and HTTP handler are production implementations.
No successful run or authority record is inserted by this fixture. Native login
status and capacity are declared external-model transport responses; no account
or credential is created. The listener deliberately has no execution worker.
"""
from __future__ import annotations

import json
import hashlib
import io
from contextlib import ExitStack, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import os
import socket
import struct
from unittest.mock import patch

# The standalone canary imports only installed product services and declared
# external transports. No unittest harness alters a storage/auth boundary.
_isolated_installation = None
if __name__ == "__main__" and "ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT" not in os.environ:
    _isolated_installation = tempfile.TemporaryDirectory(prefix="ep-dashboard-installation-")
    os.environ["ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT"] = _isolated_installation.name

from engineering_platform import capability_review as cr, server, submission_service
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import AgentResult
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.agent_state import StateStore
from engineering_platform.managed_publication import PublicationCandidate
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher, ParityLifecycleDispatchError
from engineering_platform.providers import CodexCliProvider, GitProvider, LocalProcessProvider, process_effect_start
from engineering_platform.qualification_runtime import QualificationReviewBackend
from engineering_platform.storage import sqlite_connection


class LocalGitHubTransport(GitProvider):
    """External remote transport only; all local Git commands remain real."""
    def __init__(self, remote):
        self.remote = remote

    def execute(self, root, *args):
        if len(args) > 1 and args[0] == "git" and args[1] in {"fetch", "push", "ls-remote"}:
            args = ("git", "-c", f"url.{self.remote}.insteadOf=https://github.com/qualification/managed", *args[1:])
        return super().execute(root, *args)


class RealTopologyFixture:
    """Supported services only; no unittest harness or storage substitutions."""
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ep-dashboard-stored-canary-")
        self.area = Path(self.temp.name)
        self.root, self.remote, self.data = self.area / "repo", self.area / "remote.git", self.area / "central"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "qualification@example.invalid")
        self.git("config", "user.name", "Dashboard qualification")
        (self.root / "BOOTSTRAP.md").write_text("# Qualification\n")
        (self.root / ".gitignore").write_text(".engineering/\n__pycache__/\n")
        (self.root / "README.md").write_text("# Before\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_docs.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass Documentation(unittest.TestCase):\n"
            "    def test_heading(self):\n        self.assertTrue(Path('README.md').read_text().startswith('# '))\n")
        with redirect_stdout(io.StringIO()):
            for command in (
                ["bootstrap-topology", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo"],
                ["provision-declaration", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo", "--path", str(self.root)],
            ):
                if server.main(command) != 0:
                    raise AssertionError("Canary topology/provision failed")
        self.git("add", "."); self.git("commit", "-qm", "synthetic baseline")
        self.base = self.git("rev-parse", "HEAD")
        subprocess.run(("git", "init", "-q", "--bare", str(self.remote)), check=True)
        self.git("remote", "add", "origin", "https://github.com/qualification/managed")
        self.transport = LocalGitHubTransport(self.remote)
        self.transport.command(self.root, "git", "push", "-u", "origin", "main")
        self.repository = SubprocessRepositoryClient(self.transport)
        self.database = self.data / server.SERVER_DATABASE_FILENAME
        with redirect_stdout(io.StringIO()):
            if server.main(["bind-repository", "--data-root", str(self.data), "--project-id", "project", "--repository-id", "repo", "--path", str(self.root)]) != 0:
                raise AssertionError("Canary binding failed")
        self.store = StateStore(self.root / ".engineering/engineering-runs", central_database=self.database, emit_local_projection=False)

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.root, check=True, text=True, capture_output=True).stdout.strip()

    def stop_host(self, runner):
        if runner.lease_heartbeat:
            from engineering_platform.execution_lease import release
            release(self.root, runner.lease_heartbeat.stop(), central_database=self.database)
            runner.active_lease = runner.lease_heartbeat = None

    def lifecycle_adapters(self):
        fixture = self
        class GitHub:
            def __init__(self):
                self.candidates, self.creates = [], 0
            def publication_candidates(self, *args):
                return self.candidates
            def create_draft_publication(inner, repository, branch, base, title, body):
                sha = fixture.git("rev-parse", "HEAD")
                with process_effect_start():
                    inner.creates += 1
                    inner.candidates = [PublicationCandidate(71, repository, repository, branch, "main", sha, "OPEN", True)]
            def pull_request(self, number):
                item = self.candidates[0]
                from engineering_platform.execution_models import PullRequestEvidence
                return PullRequestEvidence(number, "OPEN", True, True, head_branch=item.branch, base_branch="main", head_sha=item.head_sha)
            def ready(self, number):
                pass
            def normalize_markdown_body(self, number):
                return False
            def find_open_pull_request(self, *args):
                return None
        return None, GitHub()

    def close(self):
        self.temp.cleanup()


class ExternalModel(QualificationReviewBackend):
    qualified_specialist_roles = ("documentation", "validation")

    def __init__(self, fixture, *, finding_mode="normal", recovery_mode=False):
        self.fixture = fixture
        self.run_id = None
        self.calls = []
        self.finding_mode = finding_mode
        if finding_mode == "duplicate": self.qualified_specialist_roles = ("documentation", "finalization")
        self.recovery_mode = recovery_mode
        self.process_callback = None
        self.runtime_metadata_callback = None

    def set_process_callback(self, callback):
        self.process_callback = callback

    def set_runtime_metadata_callback(self, callback):
        self.runtime_metadata_callback = callback

    def available(self):
        return True  # This declared external transport is local and ready.

    def version(self):
        return CodexCliProvider().command("--version").stdout.strip().removeprefix("codex-cli ")

    def prepare_review(self, root, selected, objective, evidence=None):
        if self.finding_mode != "uncertain-result" or not selected.specialist_binding:
            return super().prepare_review(root, selected, objective, evidence)
        self.run_id = dict(selected.specialist_binding)["run_id"]
        payload = cr.review_request_bytes(root, selected, objective, evidence)
        front, back = socket.socketpair()
        front.settimeout(2)
        def lose_acceptance():
            try:
                # External transport receives the actual immutable request,
                # then loses its acceptance response. Real host must classify
                # uncertainty without inventing a recorded process start.
                from engineering_platform.qualification_runtime import receive
                size = struct.unpack("!I", receive(back, 4))[0]
                if receive(back, size) != payload: raise AssertionError("Request differs")
            finally:
                back.close()
        threading.Thread(target=lose_acceptance, daemon=True).start()
        return cr.SocketReviewRequest(front, payload)

    def review(self, root, selected, objective, evidence=None):
        self.calls.append(selected.reviewer)
        if selected.reviewer in {"quality", "security"}:
            if "Optional non-assurance specialist findings" in objective:
                raise AssertionError("Optional advice crossed the assurance boundary")
            return cr.ReviewerResult(selected.reviewer, "No blocking finding.",
                contract_version=cr.MANDATORY_REVIEW_OUTPUT_CONTRACT_VERSION,
                coverage=tuple({"surface": surface, "status": "REVIEWED", "evidence_ref": "Declared synthetic transport assessment"}
                               for surface in cr.mandatory_coverage_surfaces(selected.reviewer, "IMPLEMENTATION")))
        self.run_id = dict(selected.specialist_binding)["run_id"]
        if self.finding_mode == "failed":
            raise OSError("Declared external specialist transport failed")
        if self.finding_mode == "uncertain-result":
            raise cr.ReviewStartUncertain("Declared external specialist acceptance uncertain")
        path = selected.specialist_paths[0]
        findings = [{"id": "missing-acceptance", "summary": "Add the acceptance sentence.",
                     "path": path, "evidence_ref": "git-blob:" + dict(selected.specialist_source_blobs)[path],
                     "proposed_disposition": "ACCEPTED"}]
        if self.finding_mode == "privacy":
            findings[0]["summary"] = "Add <script>alert(1)</script> from /Users/qualification/local.txt and </Users/qualification/private-note.txt>; Source:/Users/qualification/label-note.txt."
        if self.finding_mode == "dispositions" and selected.reviewer == "documentation":
            findings = [{**findings[0], "id": identifier, "summary": summary} for identifier, summary in (
                ("accepted-only", "Consider the optional overview wording."),
                ("rejected-detail", "Consider a broader document restructure."),
                ("implemented-detail", "Add the acceptance sentence."),
                ("deferred-detail", "Consider a later documentation example."),
            )]
            findings.append(dict(findings[2]))  # One real duplicate transport observation.
        return cr.ReviewerResult(
            selected.reviewer, "Bounded documentation advice",
            findings=tuple(findings),
            contract_version=cr.SPECIALIST_CONTRACT_VERSION,
            specialist_binding=dict(selected.specialist_binding), usage={"input_tokens": 10},
        )

    def invoke(self, root, prompt):
        self.calls.append("implementation")
        if self.recovery_mode:
            child = subprocess.Popen((sys.executable, "-c", "pass"), start_new_session=True)
            try:
                if self.process_callback:
                    self.process_callback({"pid": child.pid, "process_group": child.pid})
                if self.runtime_metadata_callback:
                    self.runtime_metadata_callback({"provider_session_id": "synthetic-model-session"})
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.terminate(); child.wait(timeout=5)
                if self.process_callback:
                    self.process_callback(None)
        if self.recovery_mode and (self.recovery_mode is True or self.calls.count("implementation") == 1):
            from engineering_platform.execution_errors import CodexInvocationError
            raise CodexInvocationError("Declared external transport interrupted.", "Synthetic transport interruption.",
                next_action="NONE", terminal_condition="provider_turn_interrupted", interruption_reason="interrupted")
        if self.run_id is None:
            # A genuine no-selection run has no specialist request from which
            # the external adapter could learn its run identity.
            with sqlite_connection(self.fixture.database) as connection:
                rows = connection.execute("SELECT run_id FROM engineering_transactions").fetchall()
            if len(rows) != 1:
                raise AssertionError("Canary requires one actual stored transaction")
            self.run_id = rows[0][0]
        state = self.fixture.store.load(self.run_id)
        findings = cr.specialist_readback(state.specialist_records)["findings"]
        branch = state.branch or "codex/dashboard-canary"
        self.fixture.git("switch", "-c", branch)
        (root / "README.md").write_text("# Qualified documentation\n\nAcceptance sentence.\n")
        self.fixture.git("add", "README.md")
        self.fixture.git("commit", "-qm", "synthetic bounded documentation")
        choices = []
        for item in findings:
            if item["disposition"] != "PROPOSED": continue
            disposition = ({"accepted-only": "ACCEPTED", "rejected-detail": "REJECTED", "deferred-detail": "DEFERRED"}
                           .get(item.get("provider_finding_id"), "IMPLEMENTED" if item["path"] == "README.md" else "DEFERRED"))
            choices.append({"finding_id": item["id"], "disposition": disposition,
                            "reason": "Approved documentation applied." if disposition == "IMPLEMENTED" else "Explicit bounded primary decision.",
                            "changed_paths": ["README.md"] if disposition == "IMPLEMENTED" else []})
        return AgentResult("COMPLETE", branch, commit_sha=self.fixture.git("rev-parse", "HEAD"),
                           specialist_dispositions=tuple(choices))


class IsolatedProviderTransport:
    """Own every provider boundary for the full fixture lifetime.

    Only native version inspection is passed through. Account responses travel
    over a local Python pipe with an empty environment, never a Codex process.
    Unexpected execution is recorded and fails qualification before spawning.
    """
    def __init__(self, *, remaining=100, authenticated=True):
        self.remaining, self.authenticated = remaining, authenticated
        self.metadata_sessions = self.metadata_requests = 0
        self.unexpected = []
        self.processes = []
        self.stack = ExitStack()

    def start(self):
        native_command = CodexCliProvider.command
        adapter = self
        def command(provider, *args, **kwargs):
            if args == ("login", "status"):
                return subprocess.CompletedProcess(args, 0 if adapter.authenticated else 1,
                                                   "Declared local external readiness response", "")
            if args == ("--version",):
                return native_command(provider, *args, **kwargs)
            return adapter.deny("command", args)
        def app_server(provider):
            adapter.metadata_sessions += 1
            # Isolated child knows only the fixed response. No filesystem,
            # credential, account, native CLI or network API is consulted.
            script = """import sys,json
remaining=float(sys.argv[1])
for line in sys.stdin:
 request=json.loads(line);method=request.get('method')
 if method=='initialize': result={}
 elif method=='initialized': continue
 elif method=='account/rateLimits/read':
  result={'rateLimits':{'primary':{'usedPercent':100-remaining,'windowDurationMins':300,'resetsAt':2000000000}}}
 else: raise RuntimeError('Unexpected external metadata method')
 print(json.dumps({'id':request['id'],'result':result}),flush=True)
"""
            process = subprocess.Popen((sys.executable, "-I", "-c", script, str(adapter.remaining)),
                                       env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
            adapter.processes.append(process)
            underlying = process.stdin
            class ObservedInput:
                def write(self, value):
                    for line in value.splitlines():
                        message = json.loads(line)
                        if message.get("method") not in {"initialize", "initialized", "account/rateLimits/read"}:
                            return adapter.deny("metadata", message.get("method"))
                        if message.get("method") == "account/rateLimits/read":
                            adapter.metadata_requests += 1
                    return underlying.write(value)
                def flush(self):
                    return underlying.flush()
                def close(self):
                    return underlying.close()
            process.stdin = ObservedInput()
            return process
        # Server identity/login code also uses the shared local executor.
        # Keep native Git/control processes real, but guard external provider
        # and account-launch commands at this process transport boundary too.
        for name in ("execute", "spawn", "spawn_detached"):
            original = getattr(LocalProcessProvider, name)
            def local_process(provider, root, arguments, *extra, _original=original, _name=name, **kwargs):
                executable = Path(arguments[0]).name.lower() if arguments else ""
                if executable in {"codex", "gh", "osascript"} and tuple(arguments[1:]) != ("--version",):
                    return adapter.deny(f"local_{_name}", arguments)
                return _original(provider, root, arguments, *extra, **kwargs)
            self.stack.enter_context(patch.object(LocalProcessProvider, name, local_process))
        self.stack.enter_context(patch.object(CodexCliProvider, "command", command))
        self.stack.enter_context(patch.object(CodexCliProvider, "app_server", app_server))
        for name in ("invoke", "execute", "spawn", "spawn_detached"):
            self.stack.enter_context(patch.object(CodexCliProvider, name,
                                     lambda *args, _name=name, **kwargs: adapter.deny(_name, args)))
        return self

    def deny(self, channel, arguments):
        self.unexpected.append(channel)
        raise AssertionError(f"Unexpected external provider transport: {channel}")

    def snapshot(self):
        if self.unexpected:
            raise AssertionError(f"Unexpected external transport attempts: {self.unexpected}")
        return {"metadata_sessions": self.metadata_sessions, "metadata_requests": self.metadata_requests,
                "unexpected_transport_attempts": 0, "native_app_server_starts": 0}

    def close(self):
        try:
            for process in self.processes:
                if process.poll() is None:
                    CodexCliProvider().close_app_server(process)
                if process.stderr:
                    process.stderr.close()
            self.snapshot()
        finally:
            self.stack.close()


class StoredConsoleCanary:
    def __init__(self, *, publication_mode="normal", selection_mode="normal", finding_mode="normal", recovery_mode=False):
        self.fixture = RealTopologyFixture()
        self.external_transport = IsolatedProviderTransport(
            remaining=50 if selection_mode == "no_capacity" else 100,
            authenticated=selection_mode != "missing").start()
        with redirect_stdout(io.StringIO()):
            if server.main(["bootstrap-topology", "--data-root", str(self.fixture.data),
                            "--project-id", "other", "--repository-id", "other-repo"]) != 0:
                raise AssertionError("Second read-scope topology was not registered")
            if server.main(["bootstrap-topology", "--data-root", str(self.fixture.data),
                            "--project-id", "none", "--repository-id", "none-repo"]) != 0:
                raise AssertionError("Valid project identity none was not registered")
        self.model = ExternalModel(self.fixture, finding_mode=finding_mode, recovery_mode=recovery_mode)
        self.runner = None
        self.listener = None
        self.transport_patches = []
        self.publication_mode = publication_mode
        self.selection_mode = selection_mode
        self.recovery_mode = recovery_mode
        self.console_model_requests = 0
        self.console_model_texts = []
        self.console_transport = None
        self.console_git_mutations = 0
        self.console_git_transport = None
        self.generation_model_requests = 0

    def generate(self):
        native_command = CodexCliProvider.command

        def unavailable_terminal_analysis(provider, root, *args, **kwargs):
            # The real terminal-report service may request external analysis.
            # Its explicit unavailable transport exercises the real fallback;
            # never allow it to reach the user's live model session.
            self.generation_model_requests += 1
            return subprocess.CompletedProcess(args, 1, "", "Declared external terminal analysis unavailable")

        def model_metadata(provider, *args, **kwargs):
            if args == ("login", "status"):
                if self.selection_mode == "missing":
                    return subprocess.CompletedProcess(args, 1, "", "Declared external transport authentication unavailable")
                return subprocess.CompletedProcess(args, 0, "External fixture model transport ready", "")
            return native_command(provider, *args, **kwargs)

        self.transport_patches = [
            patch.object(CodexCliProvider, "command", model_metadata),
            patch.object(CodexCliProvider, "invoke", unavailable_terminal_analysis),
        ]
        for transport in self.transport_patches:
            transport.start()
        try:
            requests = [
                {"reviewer": "documentation", "question": "Which README acceptance detail is absent?",
                 "paths": ["README.md"], "consumer": "EXECUTE_AGENT", "risk": "NORMAL"},
                {"reviewer": "validation", "question": "Which documentation test edge case needs checking?",
                 "paths": ["tests/test_docs.py"], "consumer": "EXECUTE_AGENT", "risk": "HIGH"},
            ]
            if self.model.finding_mode == "duplicate":
                requests[1] = {"reviewer": "finalization", "question": "Which README evidence needs finalization?",
                               "paths": ["README.md"], "consumer": "EXECUTE_AGENT", "risk": "NORMAL"}
            if self.selection_mode == "no_consumer":
                for request in requests:
                    request["consumer"] = "NONE"
            elif self.selection_mode == "irrelevant":
                requests[0]["paths"], requests[1]["paths"] = ["tests/test_docs.py"], ["README.md"]
            prompt = ("# Objective\nUpdate the README inside approved scope.\n# Safety\n"
                      "Never bypass independent assurance.\nSpecialist review requests: " + json.dumps(requests))
            with sqlite_connection(self.fixture.database) as connection:
                submission = submission_service.submit(connection, submission_service.SubmissionRequest(
                    "project", "repo", "dashboard-canary", "HUMAN", "1", prompt, "HTTP",
                ))

            def factory(root):
                _, github = self.fixture.lifecycle_adapters()
                self.github = github
                if self.publication_mode == "uncertain":
                    create = github.create_draft_publication

                    def lost_create(*args):
                        create(*args)
                        raise SystemExit("Declared external create response lost")

                    github.create_draft_publication = lost_create
                elif self.publication_mode == "prepared":
                    def interrupted_read(*args):
                        raise SystemExit("Declared external candidate lookup interrupted")
                    github.publication_candidates = interrupted_read
                self.runner = EngineeringRunner(root, self.fixture.store, self.fixture.repository,
                                                github, self.model, lambda _: None)
                return self.runner

            try:
                receipt = ParityLifecycleDispatcher(self.fixture.data, runner_factory=factory).dispatch(submission.submission_id)
                self.run_id = receipt.run_id
                self.receipt = receipt
            except ParityLifecycleDispatchError:
                if self.selection_mode != "missing":
                    raise
                with sqlite_connection(self.fixture.database) as connection:
                    row = connection.execute("SELECT run_id,state FROM ep_parity_lifecycle_dispatches WHERE submission_id=?", (submission.submission_id,)).fetchone()
                if row is None or row[1] != "BLOCKED":
                    raise AssertionError("Actual missing-evidence admission did not fail closed")
                self.run_id = row[0]
                return self.run_id
            except SystemExit:
                if self.publication_mode not in {"uncertain", "prepared"}:
                    raise
                self.run_id = self.model.run_id
                self.fixture.stop_host(self.runner)
            state = self.fixture.store.load(self.run_id)
            expected_intent = {"uncertain": "CREATE_UNCERTAIN", "prepared": "PREPARED"}.get(self.publication_mode)
            if expected_intent:
                if state.publication_intent["status"] != expected_intent:
                    raise AssertionError(state.publication_intent)
            elif self.recovery_mode is True or self.model.finding_mode == "uncertain-result":
                if state.phase not in {"BLOCKED", "FAILED"}:
                    raise AssertionError((state.phase, state.next_action))
            elif state.phase != "WAIT_FOR_OPERATOR_MERGE":
                raise AssertionError((state.phase, state.next_action, state.diagnostic))
            return self.run_id
        finally:
            for transport in reversed(self.transport_patches):
                transport.stop()
            self.transport_patches = []

    def start_listener(self):
        server._CODEX_RATE_LIMIT_CACHE = None
        def unavailable_external_model(*args, **kwargs):
            self.console_model_requests += 1
            instruction = kwargs.get("input_text", "")
            self.console_model_texts.append(instruction.split("\n\nINPUT:\n", 1)[-1])
            raise OSError("No external Console model transport selected for this read-only canary")

        self.console_transport = patch.object(CodexCliProvider, "invoke", unavailable_external_model)
        self.console_transport.start()
        original_git = GitProvider.execute

        def observed_console_git(provider, root, *args):
            if len(args) > 1 and args[0] == "git" and args[1] in {
                "fetch", "push", "pull", "switch", "checkout", "reset", "commit", "add", "merge", "rebase", "clean",
            }:
                self.console_git_mutations += 1
                raise OSError("Execution Git effect attempted by the read-only Console")
            return original_git(provider, root, *args)

        self.console_git_transport = patch.object(GitProvider, "execute", observed_console_git)
        self.console_git_transport.start()
        self.listener = ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.listener.data_root = self.fixture.data
        self.listener.platform_data_transfer_lock = threading.RLock()
        self.listener.platform_data_transfer_in_progress = False
        self.listener.platform_data_import_restart_required = False
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.listener.server_address[1]}"

    def effect_snapshot(self):
        with sqlite_connection(self.fixture.database) as connection:
            tables = ("ep_execution_runs", "ep_submissions", "ep_parity_lifecycle_dispatches",
                      "engineering_transactions", "execution_lifecycle_events", "provider_invocations",
                      "execution_run_leases", "execution_lease_events", "provider_recovery_attempts",
                      "execution_validation_profiles", "execution_validation_control_results",
                      "execution_validation_command_invocations", "execution_validation_command_terminals")
            stored = {table: [list(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                      for table in tables}
            stored["schema"] = [list(row) for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        return {"stored": stored, "model_calls": list(self.model.calls), "generation_model_requests": self.generation_model_requests,
                "creates": getattr(getattr(self, "github", None), "creates", 0),
                "console_model_requests": self.console_model_requests,
                "console_model_texts": self.console_model_texts,
                "console_git_mutations": self.console_git_mutations,
                "git_refs": self.fixture.git("show-ref"), "git_status": self.fixture.git("status", "--porcelain")}

    def reconcile_publication(self):
        """Explicit fixture execution step, never triggered by a Console read."""
        if self.publication_mode != "uncertain":
            raise AssertionError("Only the stored uncertain operation may be reconciled")
        native_command = CodexCliProvider.command
        def model_metadata(provider, *args, **kwargs):
            if args == ("login", "status"):
                return subprocess.CompletedProcess(args, 0, "Declared external model transport ready", "")
            return native_command(provider, *args, **kwargs)
        stored = self.fixture.store.load(self.run_id)
        calls, creates = list(self.model.calls), self.github.creates
        # Temporarily release only the qualification transport spies; the
        # explicit controller action owns effects. Product authority stays real.
        self.console_git_transport.stop(); self.console_transport.stop()
        try:
            with patch.object(CodexCliProvider, "command", model_metadata):
                state = self.runner.run(Path(stored.prompt_path), run_id=self.run_id, resume=True)
            if state.publication_intent["status"] != "RECONCILED" or state.phase != "WAIT_FOR_OPERATOR_MERGE":
                raise AssertionError((state.phase, state.publication_intent))
            if self.github.creates != creates or self.model.calls != calls:
                raise AssertionError("Publication reconciliation replayed an external effect")
            return self.effect_snapshot()
        finally:
            self.console_transport.start(); self.console_git_transport.start()

    def dispatch_operator_retry(self):
        """Execute only the successor admitted by the real public retry API."""
        with sqlite_connection(self.fixture.database) as connection:
            row = connection.execute(
                "SELECT resolution_submission_id,operator_resolution FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
                (self.run_id,),
            ).fetchone()
        if not row or row[1] != "RETRIED" or not row[0]:
            raise AssertionError("Public retry did not admit a successor")
        self.fixture.stop_host(self.runner)
        self.console_git_transport.stop(); self.console_transport.stop()
        self.model.recovery_mode = False  # Declared external transport recovers.
        native_command = CodexCliProvider.command
        def metadata(provider, *args, **kwargs):
            if args == ("login", "status"):
                return subprocess.CompletedProcess(args, 0, "Declared external transport ready", "")
            return native_command(provider, *args, **kwargs)
        def unavailable_analysis(*args, **kwargs):
            self.generation_model_requests += 1
            return subprocess.CompletedProcess(args, 1, "", "Declared terminal analysis unavailable")
        def factory(root):
            _, self.github = self.fixture.lifecycle_adapters()
            self.runner = EngineeringRunner(root, self.fixture.store, self.fixture.repository,
                                            self.github, self.model, lambda _: None)
            return self.runner
        try:
            with ExitStack() as stack, redirect_stdout(io.StringIO()):
                stack.enter_context(patch.object(CodexCliProvider, "command", metadata))
                stack.enter_context(patch.object(CodexCliProvider, "invoke", unavailable_analysis))
                receipt = ParityLifecycleDispatcher(self.fixture.data, runner_factory=factory).dispatch(row[0])
            self.run_id = receipt.run_id
            state = self.fixture.store.load(self.run_id)
            if state.phase != "WAIT_FOR_OPERATOR_MERGE":
                raise AssertionError((state.phase, state.diagnostic))
        finally:
            self.console_transport.start(); self.console_git_transport.start()
        return {"run_id": self.run_id, "state": state.phase, "baseline": self.effect_snapshot()}

    def close(self):
        if self.listener:
            self.listener.shutdown()
            self.listener.server_close()
            self.thread.join()
        if self.runner:
            self.fixture.stop_host(self.runner)
        if self.console_transport:
            self.console_transport.stop()
        if self.console_git_transport:
            self.console_git_transport.stop()
        for transport in reversed(self.transport_patches):
            transport.stop()
        self.fixture.close()
        self.external_transport.close()


def passive_reader(data_root):
    """A new installed Server process reading the existing canary store only."""
    counts = {"model": 0, "git": 0}
    def no_live_model(*args, **kwargs):
        counts["model"] += 1
        raise OSError("No Console model execution allowed in passive qualification")
    original_git = GitProvider.execute
    def no_execution_git(provider, root, *args):
        if len(args) > 1 and args[0] == "git" and args[1] in {"fetch", "push", "pull", "switch", "checkout", "reset", "commit", "add", "merge", "rebase", "clean"}:
            counts["git"] += 1
            raise OSError("No execution Git effect allowed in passive qualification")
        return original_git(provider, root, *args)
    transport = IsolatedProviderTransport().start()
    server._CODEX_RATE_LIMIT_CACHE = None
    with patch.object(CodexCliProvider, "invoke", no_live_model), patch.object(GitProvider, "execute", no_execution_git):
        listener = ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        listener.data_root = data_root
        listener.platform_data_transfer_lock = threading.RLock()
        listener.platform_data_transfer_in_progress = False
        listener.platform_data_import_restart_required = False
        worker = threading.Thread(target=listener.serve_forever, daemon=True)
        worker.start()
        try:
            print(json.dumps({"url": f"http://127.0.0.1:{listener.server_address[1]}/?project=project", "module": server.__file__}), flush=True)
            for instruction in sys.stdin:
                if instruction.strip() == "snapshot":
                    print(json.dumps({"passive_effects": counts, "transport_observations": transport.snapshot()}), flush=True)
                elif instruction.strip() == "stop":
                    break
        finally:
            listener.shutdown(); listener.server_close(); worker.join()
            transport.close()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--read":
    passive_reader(Path(sys.argv[2]))
elif __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
    configuration = {"normal": {}, "prepared": {"publication_mode": "prepared"},
                     "uncertain": {"publication_mode": "uncertain"},
                     "no-consumer": {"selection_mode": "no_consumer"}}
    configuration.update({"no-capacity": {"selection_mode": "no_capacity"},
                          "irrelevant": {"selection_mode": "irrelevant"},
                          "dispositions": {"finding_mode": "dispositions"}, "withdrawn": {},
                          "provider-blocked": {"recovery_mode": True}})
    configuration["missing"] = {"selection_mode": "missing"}
    configuration["provider-recovered"] = {"recovery_mode": "recovered"}
    configuration["privacy"] = {"finding_mode": "privacy"}
    configuration.update({name: {"finding_mode": name} for name in ("failed", "uncertain-result", "duplicate")})
    configuration["failed"]["recovery_mode"] = True
    configuration["read-revoked"] = {}
    canary = StoredConsoleCanary(**configuration[scenario])
    try:
        run_id = canary.generate()
        if scenario == "withdrawn":
            from engineering_platform.local_repository_binding import unbind_local_repository
            with sqlite_connection(canary.fixture.database) as connection:
                unbind_local_repository(connection, project_id="project", repository_id="repo")
        before = canary.effect_snapshot()
        url = canary.start_listener()
        print(json.dumps({"url": url + "/?project=project", "run_id": run_id,
                          "module": server.__file__, "data_root": str(canary.fixture.data),
                          "assets": {name: hashlib.sha256((Path(server.__file__).parent / "assets" / name).read_bytes()).hexdigest()
                                     for name in ("dashboard.js", "dashboard.css", "dashboard_locales.mjs", "dashboard_run_evidence.mjs")},
                          "before": before}), flush=True)
        for instruction in sys.stdin:
            if instruction.strip() == "snapshot":
                print(json.dumps({"after": canary.effect_snapshot(), "transport_observations": canary.external_transport.snapshot()}), flush=True)
            elif instruction.strip() == "reconcile":
                print(json.dumps({"reconciled_baseline": canary.reconcile_publication()}), flush=True)
            elif instruction.strip() == "dispatch-retry":
                print(json.dumps(canary.dispatch_operator_retry()), flush=True)
            elif instruction.strip() == "revoke-read":
                # Negative persisted scope input only in this own fixture.
                # The real Server policy/read routes must observe it directly.
                with sqlite_connection(canary.fixture.database) as connection:
                    connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='project'")
                print(json.dumps({"revoked_baseline": canary.effect_snapshot()}), flush=True)
            elif instruction.strip() == "advance-target":
                # Explicit local fixture authoring, never a Console action:
                # retain the actual stored run and its verification receipt.
                path = canary.fixture.root / "README.md"
                path.write_text(path.read_text() + "\nLater unrelated target revision.\n")
                canary.fixture.git("add", "README.md")
                canary.fixture.git("commit", "-qm", "later target revision")
                print(json.dumps({"target_head": canary.fixture.git("rev-parse", "HEAD"),
                                  "advanced_baseline": canary.effect_snapshot()}), flush=True)
            elif instruction.strip() == "stop":
                break
    finally:
        canary.close()
