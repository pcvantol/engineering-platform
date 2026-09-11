#!/usr/bin/env python3
"""Installed CENTRAL execution qualification for Genesis, Managed and recovery.

The default Managed fixture uses a local bare Git remote.  An external GitHub
qualification is deliberately separate: it requires a clean checkout, its
exact approved ``owner/repository`` identity, and an explicit write flag.  It
creates one branch and pull request, then stops at the normal human merge
boundary; CI never selects that profile.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import Request, urlopen


CENTRAL_DATABASE_FILENAME = "epdata.sqlite"
SERVER_CONFIGURATION_FILENAME = "server.json"
QUALIFICATION_RUNTIME_VERSION = "0.153.4"


def command(binary: Path, *args: str) -> dict[str, object]:
    result = subprocess.run((str(binary), "-m", "engineering_platform.server", *args), check=True, text=True, capture_output=True)  # nosec B603
    return json.loads(result.stdout)


def git(path: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(path), *args), check=True, capture_output=True)  # nosec B603


def git_output(path: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(path), *args), check=True, text=True, capture_output=True).stdout.strip()  # nosec B603


def approved_github_checkout(path: Path, repository: str) -> Path:
    """Fail closed unless the operator named this exact clean GitHub checkout."""
    checkout = path.resolve()
    if not (checkout / ".git").exists() or git_output(checkout, "status", "--porcelain"):
        raise RuntimeError("MANAGED_GITHUB_FIXTURE_MUST_BE_A_CLEAN_GIT_CHECKOUT")
    remote = git_output(checkout, "remote", "get-url", "origin").removesuffix(".git")
    normalized = remote.removeprefix("https://github.com/").removeprefix("git@github.com:")
    if normalized != repository:
        raise RuntimeError("MANAGED_GITHUB_FIXTURE_REPOSITORY_MISMATCH")
    subprocess.run(("gh", "api", f"repos/{repository}"), check=True, capture_output=True)  # nosec B603
    return checkout


def declared_fixture_identity(checkout: Path) -> tuple[str, str]:
    """Use the fixture's committed binding; never overwrite its authority."""
    try:
        declaration = json.loads((checkout / ".engineering-platform" / "repository.json").read_text(encoding="utf-8"))
        project = declaration["project"]["id"]
        repository = declaration["repository"]["id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("MANAGED_GITHUB_FIXTURE_DECLARATION_REQUIRED") from error
    if not isinstance(project, str) or not isinstance(repository, str) or not project or not repository:
        raise RuntimeError("MANAGED_GITHUB_FIXTURE_DECLARATION_INVALID")
    return project, repository


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def create_repository(path: Path, *, origin: Path | None = None) -> None:
    path.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(path)), check=True)  # nosec B603
    git(path, "config", "user.email", "qualification@example.invalid")
    git(path, "config", "user.name", "Installed qualification")
    (path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (path / "BOOTSTRAP.md").write_text("# Installed qualification\n", encoding="utf-8")
    (path / "test_qualification_fixture.py").write_text(
        "import unittest\n\n"
        "class QualificationFixtureTest(unittest.TestCase):\n"
        "    def test_fixture_is_executable(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    git(path, "add", ".gitignore", "BOOTSTRAP.md", "test_qualification_fixture.py")
    git(path, "commit", "-qm", "initial qualification repository")
    if origin is not None:
        subprocess.run(("git", "init", "-q", "--bare", str(origin)), check=True)  # nosec B603
        git(path, "remote", "add", "origin", str(origin))
        git(path, "push", "-qu", "origin", "main")
        subprocess.run(("git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"), check=True)  # nosec B603


def configure_deterministic_runtime(data_root: Path, root: Path) -> Path:
    """Install the minimal managed-runtime contract inside this qualification only.

    Host preflight must remain real in the installed E2E.  CI deliberately has
    no account-wide EP-managed Codex installation, so the isolated data root
    owns a tiny version-reporting launcher.  The deterministic provider never
    invokes it for agent work; it exists solely to exercise the same resolved
    launcher and invocation checks that production performs.
    """
    prefix = root / "managed-codex-cli"
    executable = prefix / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/sh\nprintf 'codex {QUALIFICATION_RUNTIME_VERSION}\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    configuration_path = data_root / SERVER_CONFIGURATION_FILENAME
    configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    if not isinstance(configuration, dict) or set(configuration) != {
        "version", "bind_host", "bind_port", "managed_codex_cli_prefix", "product_version",
    }:
        raise RuntimeError("QUALIFICATION_SERVER_CONFIGURATION_INVALID")
    if not isinstance(configuration["product_version"], str) or not configuration["product_version"]:
        raise RuntimeError("QUALIFICATION_SERVER_CONFIGURATION_INVALID")
    configuration["managed_codex_cli_prefix"] = str(prefix.resolve())
    configuration_path.write_text(json.dumps(configuration, sort_keys=True) + "\n", encoding="utf-8")
    return executable


def wait_terminal(server: Path, data_root: Path, submission_id: str) -> tuple[str, str]:
    # Managed deliberately yields between the implementation merge and the
    # finalization/reconciliation polls; leave room for those bounded resumes.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        diagnosis = command(server, "submission-diagnose", "--data-root", str(data_root), "--submission-id", submission_id)
        state, run_id = diagnosis.get("dispatch_state"), diagnosis.get("run_id")
        if state in {"COMPLETE", "BLOCKED", "FAILED"}:
            if state != "COMPLETE" or not isinstance(run_id, str):
                raise RuntimeError(f"E2E_EXECUTION_NOT_COMPLETE: {diagnosis}")
            return submission_id, run_id
        time.sleep(.2)
    raise RuntimeError(f"E2E_EXECUTION_TIMED_OUT: {submission_id}")


def wait_github_handoff(checkout: Path, repository: str, branch: str) -> dict[str, object]:
    """Prove the external write boundary without bypassing human merge authority."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = subprocess.run(
            ("gh", "pr", "list", "--repo", repository, "--head", branch, "--state", "open", "--json", "number,url,headRefName,baseRefName"),
            check=True, text=True, capture_output=True,
        )  # nosec B603
        pull_requests = json.loads(result.stdout)
        if isinstance(pull_requests, list) and len(pull_requests) == 1:
            pull_request = pull_requests[0]
            if pull_request.get("headRefName") != branch or pull_request.get("baseRefName") != "main":
                raise RuntimeError("MANAGED_GITHUB_PULL_REQUEST_SCOPE_INVALID")
            remote_branch = git_output(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
            if not remote_branch:
                raise RuntimeError("MANAGED_GITHUB_REMOTE_BRANCH_UNAVAILABLE")
            return {"repository": repository, "branch": branch, "pull_request": pull_request}
        time.sleep(.2)
    raise RuntimeError("MANAGED_GITHUB_PULL_REQUEST_TIMED_OUT")


def verify_receipt(data_root: Path, project: str, run_id: str) -> dict[str, object]:
    findings = data_root / "artifacts" / "projects" / project / "runs" / run_id / "assurance-findings-v1.json"
    receipt = json.loads(findings.read_text(encoding="utf-8"))
    reviews = receipt.get("reviews")
    observed = [(item.get("reviewer"), item.get("status")) for item in reviews] if isinstance(reviews, list) else []
    if observed != [("quality", "PASS"), ("security", "PASS")]:
        raise RuntimeError(f"ASSURANCE_RECEIPT_INVALID: {observed}")
    # The standalone Server owns epdata.sqlite.  The former engineering.db
    # name is deliberately retired and must never become an accidental second
    # lifecycle authority during qualification.
    with sqlite3.connect(data_root / CENTRAL_DATABASE_FILENAME) as connection:
        row = connection.execute("SELECT phase,payload FROM engineering_transactions WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row[0] != "COMPLETE":
        raise RuntimeError("TERMINAL_CHECKPOINT_UNAVAILABLE")
    return {"run_id": run_id, "assurance_reviews": observed, "terminal_phase": row[0]}


def verify_controlled_recovery(data_root: Path, checkout: Path, run_id: str, base: str, project: str = "recovery") -> dict[str, object]:
    control = checkout / ".engineering" / "artifacts" / "provider-recovery-fault-injection" / f"{run_id}-EXECUTE_AGENT.json"
    consumed = json.loads(control.read_text(encoding="utf-8"))
    if consumed.get("kind") != "CONTROLLED_PROVIDER_INTERRUPTION" or consumed.get("phase") != "EXECUTE_AGENT":
        raise RuntimeError(f"CONTROLLED_INTERRUPTION_EVIDENCE_INVALID: {consumed}")
    with sqlite3.connect(data_root / CENTRAL_DATABASE_FILENAME) as connection:
        recovery = connection.execute(
            "SELECT lifecycle_phase,state,result FROM provider_recovery_attempts WHERE run_id=?", (run_id,)
        ).fetchone()
    if recovery != ("EXECUTE_AGENT", "RECOVERED", "SUCCESS"):
        raise RuntimeError(f"CONTROLLED_RECOVERY_LINEAGE_INVALID: {recovery}")
    with urlopen(base + f"/api/prompt-history?project={project}", timeout=5) as response:  # nosec B310
        history = json.loads(response.read())
    runs = history.get("runs") if isinstance(history, dict) else None
    if not isinstance(runs, list) or not any(item.get("run_id") == run_id for item in runs if isinstance(item, dict)):
        raise RuntimeError("CONTROLLED_RECOVERY_DASHBOARD_HISTORY_UNAVAILABLE")
    return {"run_id": run_id, "control": "CONSUMED", "recovery": "RECOVERED", "dashboard_history": "VISIBLE"}


def merge_github_pull_request(repository: str, pull_request: int) -> None:
    """Cross the explicit human merge boundary of the disposable fixture."""
    subprocess.run(
        ("gh", "pr", "merge", str(pull_request), "--repo", repository, "--merge", "--delete-branch"),
        check=True, capture_output=True, text=True,
    )  # nosec B603


def run_id_for_submission(server: Path, data_root: Path, submission_id: str) -> str:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        diagnosis = command(server, "submission-diagnose", "--data-root", str(data_root), "--submission-id", submission_id)
        run_id = diagnosis.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
        time.sleep(.2)
    raise RuntimeError("MANAGED_GITHUB_RUN_ID_UNAVAILABLE")


def complete_github_managed_run(
    server: Path, data_root: Path, submission_id: str, checkout: Path,
    repository: str, implementation_branch: str, project: str,
) -> dict[str, object]:
    """Verify the real remote implementation and Finalization handoffs to COMPLETE.

    The script is the designated human operator for this disposable fixture.
    It never changes production merge authority: each merge is an explicit
    GitHub CLI mutation on the exact repository named by the caller.
    """
    implementation = wait_github_handoff(checkout, repository, implementation_branch)
    pull_request = implementation["pull_request"]
    if not isinstance(pull_request, dict) or not isinstance(pull_request.get("number"), int):
        raise RuntimeError("MANAGED_GITHUB_IMPLEMENTATION_PR_INVALID")
    merge_github_pull_request(repository, pull_request["number"])
    run_id = run_id_for_submission(server, data_root, submission_id)
    finalization_branch = f"codex/finalize-{run_id}"
    finalization = wait_github_handoff(checkout, repository, finalization_branch)
    finalization_pr = finalization["pull_request"]
    if not isinstance(finalization_pr, dict) or not isinstance(finalization_pr.get("number"), int):
        raise RuntimeError("MANAGED_GITHUB_FINALIZATION_PR_INVALID")
    merge_github_pull_request(repository, finalization_pr["number"])
    _, terminal_run_id = wait_terminal(server, data_root, submission_id)
    if terminal_run_id != run_id:
        raise RuntimeError("MANAGED_GITHUB_RUN_ID_CHANGED")
    receipt = verify_receipt(data_root, project, run_id)
    return {
        **receipt,
        "implementation": implementation,
        "finalization": finalization,
    }


def submit(base: str, credential: str, project: str, repository: str, prompt: str, key: str) -> str:
    body = {"repository_id": repository, "producer": {"id": "installed-e2e", "type": "HUMAN", "version": "1"}, "prompt": prompt, "idempotency_key": key, "constraints": {"mode": "GENESIS" if "Genesis" in prompt else "MANAGED"}}
    request = Request(base + f"/v1/projects/{project}/submissions", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"})
    with urlopen(request, timeout=5) as response:  # nosec B310
        return str(json.loads(response.read())["submission_id"])


def bind_project(server: Path, data_root: Path, *, project: str, repository: str,
                 checkout: Path, push_declaration: bool = False, existing_declaration: bool = False) -> None:
    """Bind a clean fixture through the same installed Server commands as production."""
    command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
    if not existing_declaration:
        command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        git(checkout, "add", ".engineering-platform")
        git(checkout, "commit", "-qm", "bind installed e2e project")
        if push_declaration:
            git(checkout, "push", "-q", "origin", "main")
    command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))


def arm_controlled_recovery(venv: Path, data_root: Path, checkout: Path, ready: Path) -> str:
    """Wait for the deterministic adapter's bounded arm window, then use its public CLI."""
    deadline = time.monotonic() + 30
    while not ready.is_file() and time.monotonic() < deadline:
        time.sleep(.05)
    if not ready.is_file():
        raise RuntimeError("CONTROLLED_RECOVERY_ARM_WINDOW_UNAVAILABLE")
    control = json.loads(ready.read_text(encoding="utf-8"))
    run_id = control.get("run_id")
    if not isinstance(run_id, str) or control.get("phase") != "EXECUTE_AGENT":
        raise RuntimeError(f"CONTROLLED_RECOVERY_ARM_WINDOW_INVALID: {control}")
    subprocess.run(
        (str(venv / "bin" / "python"), "-m", "engineering_platform.provider_recovery",
         "arm-controlled-interruption", "--repo", str(checkout), "--run-id", run_id,
         "--phase", "EXECUTE_AGENT", "--central-database", str(data_root / CENTRAL_DATABASE_FILENAME)),
        check=True, capture_output=True, text=True,
    )  # nosec B603
    ready.with_suffix(ready.suffix + ".continue").write_text("armed\n", encoding="utf-8")
    return run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--managed-repository", type=Path, help="Clean checkout of the approved dummy GitHub repository.")
    parser.add_argument("--managed-github-repository", help="Exact approved dummy GitHub owner/repository identity.")
    parser.add_argument("--allow-managed-github-writes", action="store_true", help="Explicitly authorize one dummy-repository branch and pull request.")
    parser.add_argument("--persistent-root", type=Path, help="New isolated qualification root retained for inspection; it never shares canonical production data.")
    parser.add_argument("--bind-port", type=int, help="Fixed localhost port for a retained qualification Server.")
    args = parser.parse_args(argv)
    github_write = args.allow_managed_github_writes or args.managed_github_repository is not None
    if github_write and (not args.allow_managed_github_writes or not args.managed_repository or not args.managed_github_repository):
        raise RuntimeError("MANAGED_GITHUB_WRITE_AUTHORIZATION_REQUIRED")
    if args.persistent_root and args.persistent_root.exists() and any(args.persistent_root.iterdir()):
        raise RuntimeError("PERSISTENT_QUALIFICATION_ROOT_MUST_BE_EMPTY")
    context = nullcontext(str(args.persistent_root.resolve())) if args.persistent_root else tempfile.TemporaryDirectory(prefix="ep-deterministic-e2e-")
    with context as temporary:
        root, wheelhouse, venv, data = Path(temporary), Path(temporary) / "wheelhouse", Path(temporary) / "venv", Path(temporary) / "central"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wheelhouse.mkdir()
        subprocess.run((sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(args.source_root)), check=True, capture_output=True, text=True)  # nosec B603
        subprocess.run((sys.executable, "-m", "venv", str(venv)), check=True)  # nosec B603
        wheels = tuple(wheelhouse.glob("engineering_platform-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("QUALIFICATION_WHEEL_UNAVAILABLE")
        # Install the exact wheel path. A requirement-name install from the
        # source checkout can falsely treat its adjacent metadata as already
        # installed, leaving a non-restartable qualification venv.
        subprocess.run((str(venv / "bin" / "pip"), "install", "--no-index", "--force-reinstall", str(wheels[0])), check=True, capture_output=True, text=True)  # nosec B603
        # Invoke the installed module directly so qualification verifies the
        # wheel contents rather than relying on a platform-specific console
        # script wrapper being present in the virtual environment.
        server = venv / "bin" / "python"
        bind_port = args.bind_port or port()
        command(server, "init", "--data-root", str(data), "--bind-port", str(bind_port))
        configure_deterministic_runtime(data, root)
        genesis_host, genesis_target = root / "genesis-host", root / "genesis-target"
        create_repository(genesis_host)
        create_repository(genesis_target)
        local = genesis_host / ".engineering"
        local.mkdir()
        (local / "engineering-platform.local.json").write_text(json.dumps({"workspace": {"workspace_authorization": {"allowed_roots": [], "allowed_repositories": [str(genesis_target.resolve())], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}), encoding="utf-8")
        if args.managed_repository:
            if not github_write:
                raise RuntimeError("MANAGED_GITHUB_WRITE_AUTHORIZATION_REQUIRED")
            managed = approved_github_checkout(args.managed_repository, str(args.managed_github_repository))
            managed_project, managed_identity = declared_fixture_identity(managed)
        else:
            managed = root / "managed"
            create_repository(managed, origin=root / "managed-origin.git")
            managed_project, managed_identity = "managed", "managed-repo"
        evidence: dict[str, object] = {}
        layouts = (("genesis", "genesis", "genesis-repo", genesis_host), ("managed", managed_project, managed_identity, managed))
        for mode, project, repository, checkout in layouts:
            bind_project(server, data, project=project, repository=repository, checkout=checkout, push_declaration=mode == "managed", existing_declaration=github_write and mode == "managed")
        control_ready = root / "controlled-recovery-ready.json"
        env = {
            **os.environ,
            "EP_QUALIFICATION_DETERMINISTIC_FLOW": "1",
            "EP_CENTRAL_OPERATIONAL_DATABASE": str(data / CENTRAL_DATABASE_FILENAME),
            "EP_QUALIFICATION_CONTROL_ARM_READY_FILE": str(control_ready),
        }
        github_branch = None
        if github_write:
            github_branch = f"qualification/managed-e2e-{uuid.uuid4().hex[:12]}"
            env.update({"EP_QUALIFICATION_GITHUB_WRITE_FLOW": "1", "EP_QUALIFICATION_GITHUB_REPOSITORY": str(args.managed_github_repository), "EP_QUALIFICATION_GITHUB_BRANCH": github_branch})
        process = subprocess.Popen((str(server), "-m", "engineering_platform.server", "serve", "--data-root", str(data)), env=env)  # nosec B603
        try:
            base = f"http://127.0.0.1:{bind_port}"
            for _ in range(100):
                try:
                    urlopen(base + "/readyz", timeout=.2).read()  # nosec B310
                    break
                except OSError:
                    time.sleep(.1)
            for mode, project, repository, prompt in (("genesis", "genesis", "genesis-repo", f"Execution Mode: Genesis\nTarget repository: {genesis_target}\n\nInstalled deterministic qualification."), ("managed", managed_project, managed_identity, "Execution Mode: Managed\n\nInstalled deterministic qualification.")):
                credential = str(command(server, "issue-consumer-credential", "--data-root", str(data), "--project-id", project, "--consumer-id", f"{mode}-e2e")["credential"])
                submission = submit(base, credential, project, repository, prompt, f"{mode}-e2e")
                if mode == "managed" and github_write:
                    evidence[mode] = complete_github_managed_run(
                        server, data, submission, managed,
                        str(args.managed_github_repository), str(github_branch), project,
                    )
                    continue
                _, run_id = wait_terminal(server, data, submission)
                evidence[mode] = verify_receipt(data, project, run_id)
            recovery_project, recovery_repository = "recovery", "recovery-repo"
            if github_write:
                # Reuse the installed, already bound dummy fixture after the
                # first run reached COMPLETE. This proves interruption/retry
                # with the same real GitHub provider adapter and remote write
                # boundary, without inventing another authority declaration.
                recovery, recovery_project, recovery_repository = managed, managed_project, managed_identity
                recovery_branch = f"{github_branch}-recovery"
            else:
                recovery = root / "controlled-recovery"
                create_repository(recovery, origin=root / "controlled-recovery-origin.git")
                bind_project(server, data, project=recovery_project, repository=recovery_repository, checkout=recovery, push_declaration=True)
            control_ready.with_suffix(control_ready.suffix + ".enable").write_text("enabled\n", encoding="utf-8")
            credential = str(command(server, "issue-consumer-credential", "--data-root", str(data), "--project-id", recovery_project, "--consumer-id", "recovery-e2e")["credential"])
            submission = submit(base, credential, recovery_project, recovery_repository, "Execution Mode: Managed\n\nInstalled controlled recovery qualification.", "controlled-recovery-e2e")
            run_id = arm_controlled_recovery(venv, data, recovery, control_ready)
            if github_write:
                recovery_evidence = complete_github_managed_run(
                    server, data, submission, recovery, str(args.managed_github_repository),
                    recovery_branch, recovery_project,
                )
                if recovery_evidence["run_id"] != run_id:
                    raise RuntimeError("CONTROLLED_RECOVERY_RUN_ID_CHANGED")
            else:
                _, terminal_run_id = wait_terminal(server, data, submission)
                if terminal_run_id != run_id:
                    raise RuntimeError("CONTROLLED_RECOVERY_RUN_ID_CHANGED")
                recovery_evidence = verify_receipt(data, recovery_project, run_id)
            evidence["controlled_recovery"] = {
                **recovery_evidence,
                **verify_controlled_recovery(data, recovery, run_id, base, recovery_project),
            }
        finally:
            if not args.persistent_root:
                process.terminate(); process.wait(timeout=10)
            else:
                (root / "qualification-runtime.json").write_text(json.dumps({"data_root": str(data), "port": bind_port, "pid": process.pid, "managed_project": managed_project}, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"result": "PASS", "managed_fixture": "github-write" if github_write else "local-origin", "evidence": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
