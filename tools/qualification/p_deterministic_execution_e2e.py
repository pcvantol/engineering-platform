#!/usr/bin/env python3
"""Installed CENTRAL execution qualification for Genesis, Managed and recovery.

The default Managed fixture uses a local bare Git remote.  Pass
``--managed-repository`` with a clean checkout of the explicitly approved
dummy GitHub repository to qualify the same flow against GitHub transport;
the runtime still uses :class:`LocalQualificationGitHub`, so it never creates
or mutates a GitHub pull request.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


CENTRAL_DATABASE_FILENAME = "epdata.sqlite"


def command(binary: Path, *args: str) -> dict[str, object]:
    result = subprocess.run((str(binary), *args), check=True, text=True, capture_output=True)  # nosec B603
    return json.loads(result.stdout)


def git(path: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(path), *args), check=True, capture_output=True)  # nosec B603


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def create_repository(path: Path, *, origin: Path | None = None) -> None:
    path.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(path)), check=True)  # nosec B603
    git(path, "config", "user.email", "qualification@example.invalid")
    git(path, "config", "user.name", "Installed qualification")
    (path / "BOOTSTRAP.md").write_text("# Installed qualification\n", encoding="utf-8")
    git(path, "add", "BOOTSTRAP.md")
    git(path, "commit", "-qm", "initial qualification repository")
    if origin is not None:
        subprocess.run(("git", "init", "-q", "--bare", str(origin)), check=True)  # nosec B603
        git(path, "remote", "add", "origin", str(origin))
        git(path, "push", "-qu", "origin", "main")
        subprocess.run(("git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"), check=True)  # nosec B603


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


def verify_controlled_recovery(data_root: Path, checkout: Path, run_id: str, base: str) -> dict[str, object]:
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
    with urlopen(base + "/api/prompt-history?project=recovery", timeout=5) as response:  # nosec B310
        history = json.loads(response.read())
    if not isinstance(history, list) or not any(item.get("run_id") == run_id for item in history if isinstance(item, dict)):
        raise RuntimeError("CONTROLLED_RECOVERY_DASHBOARD_HISTORY_UNAVAILABLE")
    return {"run_id": run_id, "control": "CONSUMED", "recovery": "RECOVERED", "dashboard_history": "VISIBLE"}


def submit(base: str, credential: str, project: str, repository: str, prompt: str, key: str) -> str:
    body = {"repository_id": repository, "producer": {"id": "installed-e2e", "type": "HUMAN", "version": "1"}, "prompt": prompt, "idempotency_key": key, "constraints": {"mode": "GENESIS" if "Genesis" in prompt else "MANAGED"}}
    request = Request(base + f"/v1/projects/{project}/submissions", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"})
    with urlopen(request, timeout=5) as response:  # nosec B310
        return str(json.loads(response.read())["submission_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--managed-repository", type=Path, help="Clean checkout of the approved dummy GitHub repository.")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="ep-deterministic-e2e-") as temporary:
        root, wheelhouse, venv, data = Path(temporary), Path(temporary) / "wheelhouse", Path(temporary) / "venv", Path(temporary) / "central"
        wheelhouse.mkdir()
        subprocess.run((sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(args.source_root)), check=True, capture_output=True, text=True)  # nosec B603
        subprocess.run((sys.executable, "-m", "venv", str(venv)), check=True)  # nosec B603
        subprocess.run((str(venv / "bin" / "pip"), "install", "--no-index", "--find-links", str(wheelhouse), "engineering-platform"), check=True, capture_output=True, text=True)  # nosec B603
        server = venv / "bin" / "engineering-platform-server"
        bind_port = port()
        command(server, "init", "--data-root", str(data), "--bind-port", str(bind_port))
        genesis_host, genesis_target = root / "genesis-host", root / "genesis-target"
        create_repository(genesis_host)
        create_repository(genesis_target)
        local = genesis_host / ".engineering"
        local.mkdir()
        (local / "engineering-platform.local.json").write_text(json.dumps({"workspace": {"workspace_authorization": {"allowed_roots": [], "allowed_repositories": [str(genesis_target.resolve())], "denied_repositories": [], "symlink_policy": "reject", "case_sensitivity": "host"}}}), encoding="utf-8")
        if args.managed_repository:
            managed = args.managed_repository.resolve()
            if not (managed / ".git").exists() or subprocess.run(("git", "-C", str(managed), "status", "--porcelain"), text=True, capture_output=True).stdout.strip():
                raise RuntimeError("MANAGED_GITHUB_FIXTURE_MUST_BE_A_CLEAN_GIT_CHECKOUT")
        else:
            managed = root / "managed"
            create_repository(managed, origin=root / "managed-origin.git")
        evidence: dict[str, object] = {}
        layouts = (("genesis", "genesis-repo", genesis_host), ("managed", "managed-repo", managed))
        for project, repository, checkout in layouts:
            command(server, "bootstrap-topology", "--data-root", str(data), "--project-id", project, "--repository-id", repository)
            command(server, "provision-declaration", "--data-root", str(data), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            git(checkout, "add", ".engineering-platform")
            git(checkout, "commit", "-qm", "bind installed e2e project")
            if project == "managed":
                git(checkout, "push", "-q", "origin", "main")
            if project == "managed" and args.managed_repository:
                raise RuntimeError("MANAGED_GITHUB_FIXTURE_NEEDS_MATCHING_DECLARATION")
            command(server, "bind-repository", "--data-root", str(data), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        control_ready = root / "controlled-recovery-ready.json"
        env = {
            **os.environ,
            "EP_QUALIFICATION_DETERMINISTIC_FLOW": "1",
            "EP_CENTRAL_OPERATIONAL_DATABASE": str(data / CENTRAL_DATABASE_FILENAME),
            "EP_QUALIFICATION_CONTROL_ARM_READY_FILE": str(control_ready),
        }
        process = subprocess.Popen((str(server), "serve", "--data-root", str(data)), env=env)  # nosec B603
        try:
            base = f"http://127.0.0.1:{bind_port}"
            for _ in range(100):
                try:
                    urlopen(base + "/readyz", timeout=.2).read()  # nosec B310
                    break
                except OSError:
                    time.sleep(.1)
            for mode, project, repository, prompt in (("genesis", "genesis", "genesis-repo", f"Execution Mode: Genesis\nTarget repository: {genesis_target}\n\nInstalled deterministic qualification."), ("managed", "managed", "managed-repo", "Execution Mode: Managed\n\nInstalled deterministic qualification.")):
                credential = str(command(server, "issue-consumer-credential", "--data-root", str(data), "--project-id", project, "--consumer-id", f"{mode}-e2e")["credential"])
                submission = submit(base, credential, project, repository, prompt, f"{mode}-e2e")
                _, run_id = wait_terminal(server, data, submission)
                evidence[mode] = verify_receipt(data, project, run_id)
            recovery = root / "controlled-recovery"
            create_repository(recovery, origin=root / "controlled-recovery-origin.git")
            command(server, "bootstrap-topology", "--data-root", str(data), "--project-id", "recovery", "--repository-id", "recovery-repo")
            command(server, "provision-declaration", "--data-root", str(data), "--project-id", "recovery", "--repository-id", "recovery-repo", "--path", str(recovery))
            git(recovery, "add", ".engineering-platform")
            git(recovery, "commit", "-qm", "bind recovery qualification project")
            git(recovery, "push", "-q", "origin", "main")
            command(server, "bind-repository", "--data-root", str(data), "--project-id", "recovery", "--repository-id", "recovery-repo", "--path", str(recovery))
            control_ready.with_suffix(control_ready.suffix + ".enable").write_text("enabled\n", encoding="utf-8")
            credential = str(command(server, "issue-consumer-credential", "--data-root", str(data), "--project-id", "recovery", "--consumer-id", "recovery-e2e")["credential"])
            submission = submit(base, credential, "recovery", "recovery-repo", "Execution Mode: Managed\n\nInstalled controlled recovery qualification.", "controlled-recovery-e2e")
            deadline = time.monotonic() + 30
            while not control_ready.is_file() and time.monotonic() < deadline:
                time.sleep(.05)
            if not control_ready.is_file():
                raise RuntimeError("CONTROLLED_RECOVERY_ARM_WINDOW_UNAVAILABLE")
            control = json.loads(control_ready.read_text(encoding="utf-8"))
            run_id = control.get("run_id")
            if not isinstance(run_id, str) or control.get("phase") != "EXECUTE_AGENT":
                raise RuntimeError(f"CONTROLLED_RECOVERY_ARM_WINDOW_INVALID: {control}")
            subprocess.run((str(venv / "bin" / "python"), "-m", "engineering_platform.provider_recovery", "arm-controlled-interruption", "--repo", str(recovery), "--run-id", run_id, "--phase", "EXECUTE_AGENT", "--central-database", str(data / CENTRAL_DATABASE_FILENAME)), check=True, capture_output=True, text=True)  # nosec B603
            control_ready.with_suffix(control_ready.suffix + ".continue").write_text("armed\n", encoding="utf-8")
            _, terminal_run_id = wait_terminal(server, data, submission)
            if terminal_run_id != run_id:
                raise RuntimeError("CONTROLLED_RECOVERY_RUN_ID_CHANGED")
            verify_receipt(data, "recovery", run_id)
            evidence["controlled_recovery"] = verify_controlled_recovery(data, recovery, run_id, base)
        finally:
            process.terminate(); process.wait(timeout=10)
        print(json.dumps({"result": "PASS", "managed_fixture": "github" if args.managed_repository else "local-origin", "evidence": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
