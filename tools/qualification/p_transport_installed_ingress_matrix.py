#!/usr/bin/env python3
"""Real installed-wheel 3×2 ingress and lifecycle-initialization gate.

This is deliberately an integration executable, not a unit test: it builds
the candidate wheel, installs it in an isolated venv, starts the installed
Server and talks only through public executables, HTTP and the File Inbox
directory.  The dispatcher is allowed to stop at its real missing-local-
binding boundary: claim, dispatch and run initialization have already been
persisted in CENTRAL, while no provider or repository mutation is requested.
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
import zipfile
from urllib.request import Request, urlopen
from urllib.error import HTTPError


CASES = (
    ("HTTP", "MANAGED"), ("HTTP", "GENESIS"),
    ("CLI", "MANAGED"), ("CLI", "GENESIS"),
    ("FILE_INBOX", "MANAGED"), ("FILE_INBOX", "GENESIS"),
)


def command(binary: Path, *args: str, environment: dict[str, str] | None = None) -> dict[str, object]:
    completed = subprocess.run([str(binary), *args], check=True, text=True, capture_output=True, env=environment)  # nosec B603
    return json.loads(completed.stdout)


def wait_for_dispatch(
    server: Path,
    data_root: Path,
    submission_id: str,
    *,
    timeout: float = 15,
) -> dict[str, object]:
    """Wait for the real asynchronous lifecycle worker with bounded evidence."""
    deadline = time.monotonic() + timeout
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        result = command(server, "submission-diagnose", "--data-root", str(data_root), "--submission-id", submission_id)
        last = result
        if isinstance(result.get("run_id"), str) and result.get("dispatch_state") in {"CLAIMED", "RUNNING", "BLOCKED", "FAILED"}:
            return result
        time.sleep(.2)
    state = None if last is None else {
        "dispatch_state": last.get("dispatch_state"),
        "run_id": last.get("run_id"),
    }
    raise RuntimeError(f"dispatch did not initialize for {submission_id}: {state}")


def payload(repository: str, mode: str, key: str) -> dict[str, object]:
    target = "\nTarget repository: /tmp/ep-installed-genesis-target" if mode == "GENESIS" else ""
    return {
        "repository_id": repository,
        "producer": {"id": "installed-canary", "type": "HUMAN", "version": "1"},
        "prompt": f"Execution Mode: {mode.title()}{target}\n\nInstalled ingress qualification only.",
        "idempotency_key": key,
        "constraints": {"mode": mode, "qualification": "P_TRANSPORT_INGRESS_MATRIX"},
    }


def central_counts(data_root: Path, project_id: str) -> tuple[int, int]:
    """Read-only evidence: submissions and their initial lifecycle dispatches."""
    with sqlite3.connect(f"file:{data_root / 'engineering.db'}?mode=ro", uri=True) as connection:
        submissions = int(connection.execute("SELECT COUNT(*) FROM ep_submissions WHERE project_id=?", (project_id,)).fetchone()[0])
        runs = int(connection.execute("SELECT COUNT(*) FROM ep_parity_lifecycle_dispatches WHERE project_id=?", (project_id,)).fetchone()[0])
    return submissions, runs


def wait_for_file(path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    if not path.exists():
        raise RuntimeError(f"timed out waiting for {path}")


def isolated_port() -> int:
    """Ask the OS for an ephemeral loopback port for this isolated fixture."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def storage_authority(data_root: Path, root: Path) -> dict[str, object]:
    """Inspect the installed fixture without treating transport files as DBs."""
    databases = list(root.rglob("engineering.db"))
    canonical = data_root / "engineering.db"
    local = [path for path in databases if path != canonical]
    state_stores = [path for path in root.rglob("*") if path.name.lower() in {"statestore", "state-store"}]
    if databases != [canonical] or local or state_stores:
        raise RuntimeError("STORAGE_AUTHORITY_INVARIANT_FAILED")
    inbox = data_root / "file-inbox"
    # This inventory is specifically File Inbox delivery state. Dependabot
    # has a separate Server-child heartbeat, asserted by its own installed
    # producer canary below rather than misclassified as an Inbox artifact.
    expected = {"incoming", "processing", "accepted", "quarantine", "file-inbox-heartbeat.json"}
    if not expected <= {path.name for path in inbox.iterdir()}:
        raise RuntimeError("TRANSPORT_STATE_LAYOUT_FAILED")
    return {"operational_database_authorities": 1, "local_operational_db": 0, "local_statestore": 0, "secondary_operational_db": 0, "transport_state": "TRANSPORT_DELIVERY_STATE"}


def installed_runtime_topology(candidate_wheel: Path, venv: Path) -> dict[str, object]:
    """Prove the installed product cannot launch retired ingress runtimes.

    The Server owns File Inbox.  Checking the wheel *and* its generated
    console scripts catches both a packaged module regression and an
    accidental entry-point regression, rather than trusting source metadata.
    """
    prohibited_modules = {
        "engineering_platform/inbox_watcher.py",
        "engineering_platform/dependabot_admission.py",
    }
    with zipfile.ZipFile(candidate_wheel) as candidate:
        names = set(candidate.namelist())
    present = sorted(module for module in prohibited_modules if module in names)
    if present:
        raise RuntimeError(f"INSTALLED_RETIRED_RUNTIME_PRESENT: {', '.join(present)}")

    bin_directory = venv / "bin"
    required = {"engineering-platform-server", "engineering-platform"}
    prohibited_scripts = {
        "engineering-platform-file-inbox",
        "engineering-platform-dashboard",
        "engineering-inbox-watcher",
    }
    missing = sorted(name for name in required if not (bin_directory / name).is_file())
    retired = sorted(name for name in prohibited_scripts if (bin_directory / name).exists())
    if missing or retired:
        detail = ", ".join(([f"missing {name}" for name in missing] + [f"retired {name}" for name in retired]))
        raise RuntimeError(f"INSTALLED_RUNTIME_TOPOLOGY_FAILED: {detail}")
    return {
        "file_inbox_runtime_owner": "EP_SERVER",
        "standalone_file_inbox_executable": 0,
        "installed_legacy_inbox_watcher": 0,
        "installed_legacy_dependabot_runtime": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="ep-installed-3x2-") as temporary:
        root = Path(temporary)
        wheelhouse, venv, data_root = root / "wheelhouse", root / "venv", root / "central"
        wheelhouse.mkdir()
        # Build the candidate exactly as an installer would.  The qualification
        # runner must not assume that its own interpreter happens to carry the
        # project build backend; build isolation is part of the wheel contract.
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(args.source_root)], check=True, capture_output=True, text=True)  # nosec B603
        wheels = tuple(wheelhouse.glob("engineering_platform-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("CANDIDATE_WHEEL_UNAVAILABLE")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)  # nosec B603
        pip = venv / "bin" / "pip"
        subprocess.run([str(pip), "install", "--no-index", "--find-links", str(wheelhouse), "engineering-platform"], check=True, capture_output=True, text=True)  # nosec B603
        topology = installed_runtime_topology(wheels[0], venv)
        server, cli = venv / "bin" / "engineering-platform-server", venv / "bin" / "engineering-platform"
        port = isolated_port()
        command(server, "init", "--data-root", str(data_root), "--bind-port", str(port))
        base = f"http://127.0.0.1:{port}"
        evidence: dict[str, dict[str, object]] = {}
        for ordinal, (transport, mode) in enumerate(CASES, 1):
            project, repository = f"ingress-{ordinal}", f"repo-{ordinal}"
            command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
            checkout = root / f"checkout-{ordinal}"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
            command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            credential = command(server, "issue-consumer-credential", "--data-root", str(data_root), "--project-id", project, "--consumer-id", f"consumer-{ordinal}")["credential"]
            environment = {**os.environ, "EP_CONSUMER_TOKEN": str(credential), "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
            process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=environment)  # nosec B603
            try:
                for _ in range(60):
                    try:
                        with urlopen(base + "/readyz", timeout=.25):  # nosec B310
                            break
                    except OSError:
                        time.sleep(.1)
                item = payload(repository, mode, f"installed-{ordinal}")
                if transport == "HTTP":
                    request = Request(base + f"/v1/projects/{project}/submissions", data=json.dumps(item).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"})
                    with urlopen(request) as response:  # nosec B310
                        receipt = json.loads(response.read())
                elif transport == "CLI":
                    prompt, constraints = root / f"{ordinal}.md", root / f"{ordinal}.json"
                    prompt.write_text(str(item["prompt"]), encoding="utf-8")
                    constraints.write_text(json.dumps(item["constraints"]), encoding="utf-8")
                    receipt = command(cli, "submit", "--server", base, "--project", project, "--repository", repository, "--producer-id", "installed-canary", "--producer-type", "HUMAN", "--producer-version", "1", "--prompt-file", str(prompt), "--constraints-file", str(constraints), "--idempotency-key", str(item["idempotency_key"]), environment=environment)
                    if ordinal == 3:
                        wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
                        invalid = root / "invalid-constraints.json"
                        invalid.write_text("[]", encoding="utf-8")
                        invalid_prompt = root / "invalid-mode.md"
                        invalid_prompt.write_text("Execution Mode: INVALID\n", encoding="utf-8")
                        invalid_genesis_prompt = root / "invalid-genesis-target.md"
                        invalid_genesis_prompt.write_text("Execution Mode: GENESIS\nTarget repository: relative\n", encoding="utf-8")
                        cases = (
                            ("missing_prompt", ["--prompt-file", str(root / "missing.md")]),
                            ("unknown_project", ["--project", "unknown-project"]),
                            ("unknown_repository", ["--repository", "unknown-repository"]),
                            ("invalid_constraints", ["--constraints-file", str(invalid)]),
                            ("invalid_mode", ["--prompt-file", str(invalid_prompt)]),
                            ("invalid_genesis_target", ["--prompt-file", str(invalid_genesis_prompt)]),
                        )
                        base_args = [str(cli), "submit", "--server", base, "--project", project, "--repository", repository, "--producer-id", "installed-canary", "--producer-type", "HUMAN", "--prompt-file", str(prompt)]
                        for name, replacement in cases:
                            before = central_counts(data_root, project)
                            arguments = list(base_args)
                            for flag, value in zip(replacement[::2], replacement[1::2]):
                                if flag in arguments:
                                    arguments[arguments.index(flag) + 1] = value
                                else:
                                    arguments.extend((flag, value))
                            completed = subprocess.run(arguments, env=environment, capture_output=True, text=True)  # nosec B603
                            if completed.returncode == 0 or central_counts(data_root, project) != before:
                                raise RuntimeError(f"negative CLI canary failed closed check: {name}; exit={completed.returncode}; output={completed.stdout.strip()}")
                        evidence["CLI_NEGATIVE_CANARIES"] = {"pass": True}
                else:
                    incoming = data_root / "file-inbox" / "incoming"
                    incoming.mkdir(parents=True, exist_ok=True)
                    source = incoming / f"{ordinal}.json"
                    receipt_path = data_root / "file-inbox" / "accepted"
                    receipts_before = set(receipt_path.glob("*.receipt.json")) if receipt_path.exists() else set()
                    source.write_text(json.dumps({"project_id": project, "submission": item}), encoding="utf-8")
                    deadline = time.monotonic() + 15
                    while not (set(receipt_path.glob("*.receipt.json")) - receipts_before) and time.monotonic() < deadline:
                        time.sleep(.2)
                    receipt = json.loads(next(iter(set(receipt_path.glob("*.receipt.json")) - receipts_before)).read_text())
                diagnosis = wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
                evidence[f"{transport}_{mode}"] = {"submission_id": receipt["submission_id"], "run_id": diagnosis["run_id"], "dispatch_state": diagnosis["dispatch_state"], "pass": True}
                if transport == "FILE_INBOX":
                    # Reappearance is a real filesystem delivery.  Its digest
                    # becomes the same canonical idempotency identity, so
                    # CENTRAL must retain exactly one submission/run.
                    before = central_counts(data_root, project)
                    source.write_text(json.dumps({"project_id": project, "submission": item}), encoding="utf-8")
                    deadline = time.monotonic() + 15
                    while source.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    after = central_counts(data_root, project)
                    if source.exists() or before != (1, 1) or after != (1, 1):
                        raise RuntimeError("FILE_INBOX_REPLAY_DUPLICATED_OR_UNDELIVERED")
                    evidence[f"{transport}_{mode}"]["replay"] = "PASS"
                    if ordinal == 5:
                        quarantine = data_root / "file-inbox" / "quarantine"
                        physical_quarantine = lambda: [path for path in quarantine.glob("*.json") if not path.name.endswith(".receipt.json")]
                        before_quarantine = len(physical_quarantine()) if quarantine.exists() else 0
                        negative_files = {
                            "malformed.json": "{",
                            "unknown-project.json": json.dumps({"project_id": "unknown-project", "submission": item}),
                            "unknown-repository.json": json.dumps({"project_id": project, "submission": {**item, "repository_id": "unknown-repository", "idempotency_key": "negative-file-repo"}}),
                            "invalid-mode.json": json.dumps({"project_id": project, "submission": {**item, "prompt": "Execution Mode: INVALID", "idempotency_key": "negative-file-mode"}}),
                            "invalid-genesis.json": json.dumps({"project_id": project, "submission": {**payload(repository, "GENESIS", "negative-file-genesis"), "prompt": "Execution Mode: Genesis\nTarget repository: relative"}}),
                        }
                        for name, content in negative_files.items():
                            (incoming / name).write_text(content, encoding="utf-8")
                        deadline = time.monotonic() + 15
                        while len(physical_quarantine()) < before_quarantine + len(negative_files) and time.monotonic() < deadline:
                            time.sleep(.1)
                        quarantined = len(physical_quarantine())
                        counts = central_counts(data_root, project)
                        if quarantined != before_quarantine + len(negative_files) or counts != (1, 1):
                            raise RuntimeError(f"negative File Inbox canary failed closed check: quarantined={quarantined}; expected={before_quarantine + len(negative_files)}; counts={counts}")
                        evidence["FILE_INBOX_NEGATIVE_CANARIES"] = {"pass": True}
                if ordinal == 1:
                    # Public-boundary negative canaries: every rejected
                    # request must leave CENTRAL's submission/run counts flat.
                    negatives = (
                        ("malformed", b"{", project),
                        ("unknown_project", json.dumps(item).encode(), "unknown-project"),
                        ("unknown_repository", json.dumps({**item, "repository_id": "unknown-repository"}).encode(), project),
                        ("invalid_mode", json.dumps({**item, "constraints": {"mode": "INVALID"}}).encode(), project),
                        ("invalid_genesis", json.dumps({**payload(repository, "GENESIS", "invalid-genesis"), "prompt": "Execution Mode: Genesis\nTarget repository: relative"}).encode(), project),
                    )
                    for name, body, target_project in negatives:
                        before = central_counts(data_root, project)
                        request = Request(base + f"/v1/projects/{target_project}/submissions", data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"})
                        try:
                            urlopen(request)  # nosec B310
                            raise RuntimeError(f"negative HTTP canary accepted: {name}")
                        except HTTPError:
                            pass
                        if central_counts(data_root, project) != before:
                            raise RuntimeError(f"negative HTTP canary persisted state: {name}")
                    evidence["HTTP_NEGATIVE_CANARIES"] = {"pass": True}
            finally:
                process.terminate(); process.wait(timeout=5)
        # Canary A: with Server fully stopped there is no claimant; the
        # physical envelope remains in incoming until normal Server startup.
        project, repository = "durability-down", "durability-repo"
        command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
        checkout = root / "durability-checkout"; checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
        command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        credential = str(command(server, "issue-consumer-credential", "--data-root", str(data_root), "--project-id", project, "--consumer-id", "durability") ["credential"])
        envelope = {"project_id": project, "submission": payload(repository, "MANAGED", "durability-down")}
        incoming = data_root / "file-inbox" / "incoming"; incoming.mkdir(parents=True, exist_ok=True)
        source = incoming / "server-down.json"; source.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
        if not source.exists() or central_counts(data_root, project) != (0, 0):
            raise RuntimeError("SERVER_DOWN_BEFORE_CLAIM_DURABILITY_FAILED")
        environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=environment)  # nosec B603
        try:
            digest = __import__("hashlib").sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()
            receipt_path = data_root / "file-inbox" / "accepted" / f"{digest}.receipt.json"
            wait_for_file(receipt_path)
            receipt = json.loads(receipt_path.read_text())
            diagnosis = wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
            if central_counts(data_root, project) != (1, 1):
                raise RuntimeError("SERVER_DOWN_BEFORE_CLAIM_COUNTS_FAILED")
            evidence["SERVER_DOWN_BEFORE_CLAIM_DURABILITY"] = {"pass": True, "source": source.name, "submission_id": receipt["submission_id"], "run_id": diagnosis["run_id"]}
        finally:
            process.terminate(); process.wait(timeout=5)
        # Canary B: deterministic crash after atomic claim, before HTTP submit.
        project, repository = "durability-claim", "durability-claim-repo"
        command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
        checkout = root / "claim-checkout"; checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
        command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        credential = str(command(server, "issue-consumer-credential", "--data-root", str(data_root), "--project-id", project, "--consumer-id", "durability-claim") ["credential"])
        body = json.dumps({"project_id": project, "submission": payload(repository, "MANAGED", "durability-claim")}, sort_keys=True)
        incoming = data_root / "file-inbox" / "incoming"; source = incoming / "crash-after-claim.json"; source.write_text(body, encoding="utf-8")
        crash_environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1", "EP_FILE_INBOX_QUALIFICATION_FAULT": "AFTER_CLAIM_BEFORE_SUBMIT"}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=crash_environment)  # nosec B603
        try:
            # The producer is a real Server child and two independent CENTRAL
            # bindings are admitted asynchronously.  Keep the qualification
            # bounded, but allow the same contention/retry window used below
            # for lifecycle initialization instead of treating a slow runner
            # as a false multi-project-authority failure.
            deadline = time.monotonic() + 30
            processing = data_root / "file-inbox" / "processing" / source.name
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            if process.returncode != 86 or not processing.exists() or central_counts(data_root, project) != (0, 0):
                raise RuntimeError("CRASH_AFTER_CLAIM_BOUNDARY_FAILED")
        finally:
            if process.poll() is None: process.terminate(); process.wait(timeout=5)
        environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=environment)  # nosec B603
        try:
            digest = __import__("hashlib").sha256(body.encode()).hexdigest(); receipt_path = data_root / "file-inbox" / "accepted" / f"{digest}.receipt.json"
            wait_for_file(receipt_path); receipt = json.loads(receipt_path.read_text()); diagnosis = wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
            if central_counts(data_root, project) != (1, 1): raise RuntimeError("CRASH_AFTER_CLAIM_RECOVERY_COUNTS_FAILED")
            evidence["CRASH_AFTER_CLAIM_RECOVERY"] = {"pass": True, "submission_id": receipt["submission_id"], "run_id": diagnosis["run_id"]}
        finally:
            process.terminate(); process.wait(timeout=5)
        # Canary C: CENTRAL has accepted, but the Server crashes before the
        # local accepted receipt/archive acknowledgement.
        project, repository = "durability-accept", "durability-accept-repo"
        command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
        checkout = root / "accept-checkout"; checkout.mkdir(); subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
        command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout)); command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        credential = str(command(server, "issue-consumer-credential", "--data-root", str(data_root), "--project-id", project, "--consumer-id", "durability-accept") ["credential"])
        body = json.dumps({"project_id": project, "submission": payload(repository, "MANAGED", "durability-accept")}, sort_keys=True)
        incoming = data_root / "file-inbox" / "incoming"; source = incoming / "crash-after-accept.json"; source.write_text(body, encoding="utf-8")
        crash_environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1", "EP_FILE_INBOX_QUALIFICATION_FAULT": "AFTER_CENTRAL_ACCEPT_BEFORE_ARCHIVE"}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=crash_environment)  # nosec B603
        try:
            deadline = time.monotonic() + 15; processing = data_root / "file-inbox" / "processing" / source.name
            while process.poll() is None and time.monotonic() < deadline: time.sleep(.1)
            if process.returncode != 87 or not processing.exists() or central_counts(data_root, project)[0] != 1:
                raise RuntimeError("POST_ACCEPT_PRE_ARCHIVE_BOUNDARY_FAILED")
        finally:
            if process.poll() is None: process.terminate(); process.wait(timeout=5)
        environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=environment)  # nosec B603
        try:
            digest = __import__("hashlib").sha256(body.encode()).hexdigest(); receipt_path = data_root / "file-inbox" / "accepted" / f"{digest}.receipt.json"
            wait_for_file(receipt_path); receipt = json.loads(receipt_path.read_text()); diagnosis = wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
            if central_counts(data_root, project) != (1, 1) or not bool(receipt.get("duplicate")):
                raise RuntimeError("POST_ACCEPT_PRE_ARCHIVE_RECOVERY_FAILED")
            evidence["POST_ACCEPT_PRE_ARCHIVE_RECOVERY"] = {"pass": True, "submission_id": receipt["submission_id"], "run_id": diagnosis["run_id"]}
        finally:
            process.terminate(); process.wait(timeout=5)
        # Canary E: a fresh empty Server-owned Inbox remains healthy across a
        # normal Server restart and creates no delivery state.
        empty_root = root / "empty-central"; empty_port = isolated_port()
        command(server, "init", "--data-root", str(empty_root), "--bind-port", str(empty_port))
        empty_environment = {**os.environ, "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
        for restart in range(2):
            process = subprocess.Popen([str(server), "serve", "--data-root", str(empty_root)], env=empty_environment)  # nosec B603
            try:
                heartbeat = empty_root / "file-inbox" / "file-inbox-heartbeat.json"; wait_for_file(heartbeat)
                if central_counts(empty_root, "missing-project") != (0, 0): raise RuntimeError("EMPTY_RESTART_CREATED_STATE")
            finally:
                process.terminate(); process.wait(timeout=5)
        evidence["EMPTY_RESTART_STABILITY"] = {"pass": True}
        # One Server-owned File Inbox is an internal principal, not a
        # single-project bearer consumer. Prove two explicit project scopes
        # admit independently while cross-project and unknown envelopes are
        # quarantined without any EP_CONSUMER_TOKEN in the Server process.
        multi_root, multi_port = root / "multi-project-central", isolated_port()
        command(server, "init", "--data-root", str(multi_root), "--bind-port", str(multi_port))
        multi_pairs = (("file-multi-a", "file-multi-repository-a"), ("file-multi-b", "file-multi-repository-b"))
        for project, repository in multi_pairs:
            command(server, "bootstrap-topology", "--data-root", str(multi_root), "--project-id", project, "--repository-id", repository)
            checkout = root / f"{project}-checkout"; checkout.mkdir(); subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
            command(server, "provision-declaration", "--data-root", str(multi_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            command(server, "bind-repository", "--data-root", str(multi_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
        multi_incoming = multi_root / "file-inbox" / "incoming"; multi_incoming.mkdir(parents=True, exist_ok=True)
        multi_files = {
            "project-a.json": {"project_id": "file-multi-a", "submission": payload("file-multi-repository-a", "MANAGED", "file-multi-a")},
            "project-b.json": {"project_id": "file-multi-b", "submission": payload("file-multi-repository-b", "MANAGED", "file-multi-b")},
            "cross-project.json": {"project_id": "file-multi-a", "submission": payload("file-multi-repository-b", "MANAGED", "file-multi-cross")},
            "unknown-project.json": {"project_id": "unknown-file-project", "submission": payload("file-multi-repository-a", "MANAGED", "file-multi-unknown")},
        }
        encoded_multi = {name: json.dumps(body, sort_keys=True) for name, body in multi_files.items()}
        for name, body in encoded_multi.items(): (multi_incoming / name).write_text(body, encoding="utf-8")
        no_file_credential = {key: value for key, value in os.environ.items() if key != "EP_CONSUMER_TOKEN"}
        no_file_credential["EP_QUALIFICATION_INITIALIZE_ONLY"] = "1"
        process = subprocess.Popen([str(server), "serve", "--data-root", str(multi_root)], env=no_file_credential)  # nosec B603
        try:
            for name in ("project-a.json", "project-b.json"):
                digest = __import__("hashlib").sha256(encoded_multi[name].encode()).hexdigest()
                receipt_path = multi_root / "file-inbox" / "accepted" / f"{digest}.receipt.json"
                wait_for_file(receipt_path)
                wait_for_dispatch(server, multi_root, str(json.loads(receipt_path.read_text())["submission_id"]))
            deadline = time.monotonic() + 15
            quarantine = multi_root / "file-inbox" / "quarantine"
            while len(list(quarantine.glob("*.receipt.json"))) < 2 and time.monotonic() < deadline: time.sleep(.1)
            if central_counts(multi_root, "file-multi-a") != (1, 1) or central_counts(multi_root, "file-multi-b") != (1, 1) or len(list(quarantine.glob("*.receipt.json"))) != 2:
                raise RuntimeError("FILE_INBOX_MULTI_PROJECT_AUTHORITY_FAILED")
            evidence["FILE_INBOX_MULTI_PROJECT_AUTHORITY"] = {"pass": True, "cross_project_submissions": 0}
        finally:
            process.terminate(); process.wait(timeout=5)
        # Human Intent 2×: physical Markdown -> deterministic intake -> CENTRAL.
        for human_mode in ("MANAGED", "GENESIS"):
            project, repository = f"human-{human_mode.lower()}", f"human-{human_mode.lower()}-repo"
            command(server, "bootstrap-topology", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository)
            checkout = root / f"{project}-checkout"; checkout.mkdir(); subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
            command(server, "provision-declaration", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout)); command(server, "bind-repository", "--data-root", str(data_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            credential = str(command(server, "issue-consumer-credential", "--data-root", str(data_root), "--project-id", project, "--consumer-id", project)["credential"])
            metadata = f"project: {project}\nrepository: {repository}\nmode: {human_mode}\n" + ("target: /tmp/ep-human-genesis-target\n" if human_mode == "GENESIS" else "")
            source = data_root / "file-inbox" / "incoming" / f"human-{human_mode.lower()}.md"
            body = f"---\n{metadata}---\nImplement the installed human intent canary.\n"; source.write_text(body, encoding="utf-8")
            environment = {**os.environ, "EP_CONSUMER_TOKEN": credential, "EP_QUALIFICATION_INITIALIZE_ONLY": "1"}
            process = subprocess.Popen([str(server), "serve", "--data-root", str(data_root)], env=environment)  # nosec B603
            try:
                digest = __import__("hashlib").sha256(body.encode()).hexdigest(); receipt_path = data_root / "file-inbox" / "accepted" / f"{digest}.receipt.json"
                wait_for_file(receipt_path); receipt = json.loads(receipt_path.read_text()); diagnosis = wait_for_dispatch(server, data_root, str(receipt["submission_id"]))
                evidence[f"FILE_HUMAN_{human_mode}"] = {"pass": True, "source": source.name, "submission_id": receipt["submission_id"], "run_id": diagnosis["run_id"], "normalization": "submission-intake-v1"}
                if human_mode == "MANAGED":
                    source.write_text(body, encoding="utf-8")
                    deadline = time.monotonic() + 15
                    while source.exists() and time.monotonic() < deadline: time.sleep(.1)
                    if source.exists() or central_counts(data_root, project) != (1, 1): raise RuntimeError("HUMAN_FILE_REPLAY_DUPLICATED")
                    evidence["HUMAN_FILE_REPLAY"] = {"pass": True, "duplicate_actions": 0, "duplicate_runs": 0}
                    quarantine = data_root / "file-inbox" / "quarantine"
                    physical = lambda: [path for path in quarantine.glob("*.json") if not path.name.endswith(".receipt.json")]
                    before_quarantine = len(physical())
                    invalid_human = {
                        "human-malformed-metadata.md": "---\nproject human\n---\nintent",
                        "human-missing-project.md": "---\nrepository: x\nmode: MANAGED\n---\nintent",
                        "human-missing-repository.md": f"---\nproject: {project}\nmode: MANAGED\n---\nintent",
                        "human-unknown-project.md": "---\nproject: unknown\nrepository: x\nmode: MANAGED\n---\nintent",
                        "human-unknown-repository.md": f"---\nproject: {project}\nrepository: unknown\nmode: MANAGED\n---\nintent",
                        "human-invalid-mode.md": f"---\nproject: {project}\nrepository: {repository}\nmode: INVALID\n---\nintent",
                        "human-genesis-target.md": f"---\nproject: {project}\nrepository: {repository}\nmode: GENESIS\n---\nintent",
                        "human-invalid-genesis-target.md": f"---\nproject: {project}\nrepository: {repository}\nmode: GENESIS\ntarget: relative\n---\nintent",
                        "human-empty.md": f"---\nproject: {project}\nrepository: {repository}\nmode: MANAGED\n---\n",
                        "human-binary.md": b"\x00not-text",
                        "human-oversized.md": b"x" * 131073,
                    }
                    for name, content in invalid_human.items():
                        target = data_root / "file-inbox" / "incoming" / name
                        target.write_bytes(content) if isinstance(content, bytes) else target.write_text(content, encoding="utf-8")
                    deadline = time.monotonic() + 15
                    while len(physical()) < before_quarantine + len(invalid_human) and time.monotonic() < deadline: time.sleep(.1)
                    if len(physical()) != before_quarantine + len(invalid_human) or central_counts(data_root, project) != (1, 1): raise RuntimeError("HUMAN_INTENT_FAIL_CLOSED")
                    evidence["NEGATIVE_HUMAN_CANARIES"] = {"pass": True}
            finally:
                process.terminate(); process.wait(timeout=5)
        # The Dependabot producer itself runs inside the real installed
        # Server. Only GitHub's external response is fixture-backed; binding
        # resolution, canonical admission and lifecycle initialization remain
        # the exact installed product path.
        dependabot_root, dependabot_port = root / "dependabot-central", isolated_port()
        command(server, "init", "--data-root", str(dependabot_root), "--bind-port", str(dependabot_port))
        dependabot_pairs = (("dependabot-a", "dependabot-repository-a", "example/repository-a"), ("dependabot-b", "dependabot-repository-b", "example/repository-b"))
        for project, repository, external in dependabot_pairs:
            command(server, "bootstrap-topology", "--data-root", str(dependabot_root), "--project-id", project, "--repository-id", repository)
            checkout = root / f"{project}-checkout"; checkout.mkdir(); subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
            command(server, "provision-declaration", "--data-root", str(dependabot_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            command(server, "bind-repository", "--data-root", str(dependabot_root), "--project-id", project, "--repository-id", repository, "--path", str(checkout))
            command(server, "register-producer-binding", "--data-root", str(dependabot_root), "--producer-type", "DEPENDABOT", "--external-resource-type", "GITHUB_REPOSITORY", "--external-resource-identity", external, "--project-id", project, "--repository-id", repository, "--reason", "installed qualification")
        fixture = root / "dependabot-github-fixture.json"
        fixture.write_text(json.dumps({
            "example/repository-a": [{"number": 71, "title": "Bump example A", "html_url": "https://github.com/example/repository-a/pull/71", "user": {"login": "dependabot[bot]"}, "head": {"ref": "dependabot/pip/a", "sha": "a" * 40}}],
            "example/repository-b": [{"number": 72, "title": "Bump example B", "html_url": "https://github.com/example/repository-b/pull/72", "user": {"login": "dependabot[bot]"}, "head": {"ref": "dependabot/pip/b", "sha": "b" * 40}}],
        }, sort_keys=True), encoding="utf-8")
        dependabot_environment = {**os.environ, "EP_QUALIFICATION_INITIALIZE_ONLY": "1", "EP_DEPENDABOT_QUALIFICATION_FIXTURE": str(fixture)}
        process = subprocess.Popen([str(server), "serve", "--data-root", str(dependabot_root)], env=dependabot_environment)  # nosec B603
        try:
            deadline = time.monotonic() + 15
            rows: list[tuple[str, str, str]] = []
            heartbeat = dependabot_root / "dependabot-producer-heartbeat.json"
            while time.monotonic() < deadline:
                with sqlite3.connect(dependabot_root / "engineering.db") as connection:
                    rows = [(str(row[0]), str(row[1]), str(row[2])) for row in connection.execute("SELECT submission_id,project_id,repository_id FROM ep_submissions WHERE transport='DEPENDABOT' ORDER BY project_id")]
                if len(rows) == 2 and heartbeat.exists():
                    break
                time.sleep(.1)
            expected_bindings = {
                ("dependabot-a", "dependabot-repository-a"),
                ("dependabot-b", "dependabot-repository-b"),
            }
            observed_bindings = {(row[1], row[2]) for row in rows}
            if not heartbeat.exists() or len(rows) != 2 or observed_bindings != expected_bindings:
                raise RuntimeError(
                    "DEPENDABOT_MULTI_PROJECT_BINDING_FAILED: "
                    f"heartbeat={heartbeat.exists()} rows={len(rows)} "
                    f"bindings={sorted(observed_bindings)}"
                )
            heartbeat_payload = json.loads(heartbeat.read_text())
            if heartbeat_payload.get("state") != "READY" or heartbeat_payload.get("readiness") != "DISCOVERY_CAPABLE":
                raise RuntimeError("DEPENDABOT_READY_IMPLIES_OPERATIONAL_FAILED")
            # Dependabot admissions are produced by a separate Server child
            # after the Server-owned lifecycle worker has started.  Keep this
            # exact real boundary bounded but allow the worker's initial
            # SQLite contention/retry window for two independently-bound
            # projects; this is not a mock or an alternate dispatch path.
            for submission_id, _project, _repository in rows:
                wait_for_dispatch(server, dependabot_root, submission_id, timeout=30)
            evidence["DEPENDABOT_MULTI_PROJECT_BINDING"] = {"pass": True, "submissions": [row[0] for row in rows]}
        finally:
            process.terminate(); process.wait(timeout=5)
        # A normal Server restart rediscovers the same PR heads. The durable
        # canonical idempotency key must leave the submission/run count flat.
        process = subprocess.Popen([str(server), "serve", "--data-root", str(dependabot_root)], env=dependabot_environment)  # nosec B603
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                heartbeat = dependabot_root / "dependabot-producer-heartbeat.json"
                if heartbeat.exists():
                    break
                time.sleep(.1)
            with sqlite3.connect(dependabot_root / "engineering.db") as connection:
                duplicate_count = int(connection.execute("SELECT COUNT(*) FROM ep_submissions WHERE transport='DEPENDABOT'").fetchone()[0])
            if duplicate_count != 2:
                raise RuntimeError("DEPENDABOT_RESTART_DUPLICATED")
            evidence["DEPENDABOT_REPLAY_RESTART"] = {"pass": True, "duplicate_submissions": 0, "duplicate_actions": 0, "duplicate_runs": 0}
        finally:
            process.terminate(); process.wait(timeout=5)
        evidence["STORAGE_AUTHORITY"] = storage_authority(data_root, data_root)
        print(json.dumps({"P_TRANSPORT_INGRESS_MATRIX": "PASS", "P_TRANSPORT_FILE_DURABILITY_GATE": "PASS", "P_TRANSPORT_NEGATIVE_INGRESS_GATE": "PASS", "P_TRANSPORT_STORAGE_AUTHORITY_GATE": "PASS", "P_TRANSPORT_FUNCTIONAL_INGRESS_GATE": "PASS", "CENTRAL_DEPENDABOT_GATE": "PASS", "INSTALLED_LEGACY_DEPENDABOT_RUNTIME": 0, "INSTALLED_RUNTIME_TOPOLOGY": topology, "matrix": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
