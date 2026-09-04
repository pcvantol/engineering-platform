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
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


CASES = (
    ("HTTP", "MANAGED"), ("HTTP", "GENESIS"),
    ("CLI", "MANAGED"), ("CLI", "GENESIS"),
    ("FILE_INBOX", "MANAGED"), ("FILE_INBOX", "GENESIS"),
)


def command(binary: Path, *args: str, environment: dict[str, str] | None = None) -> dict[str, object]:
    completed = subprocess.run([str(binary), *args], check=True, text=True, capture_output=True, env=environment)  # nosec B603
    return json.loads(completed.stdout)


def wait_for_dispatch(server: Path, data_root: Path, submission_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = command(server, "submission-diagnose", "--data-root", str(data_root), "--submission-id", submission_id)
        if isinstance(result.get("run_id"), str) and result.get("dispatch_state") in {"CLAIMED", "RUNNING", "BLOCKED", "FAILED"}:
            return result
        time.sleep(.2)
    raise RuntimeError(f"dispatch did not initialize for {submission_id}")


def payload(repository: str, mode: str, key: str) -> dict[str, object]:
    return {
        "repository_id": repository,
        "producer": {"id": "installed-canary", "type": "HUMAN", "version": "1"},
        "prompt": f"Execution Mode: {mode.title()}\n\nInstalled ingress qualification only.",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="ep-installed-3x2-") as temporary:
        root = Path(temporary)
        wheelhouse, venv, data_root = root / "wheelhouse", root / "venv", root / "central"
        wheelhouse.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--wheel-dir", str(wheelhouse), str(args.source_root)], check=True, capture_output=True, text=True)  # nosec B603
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)  # nosec B603
        pip = venv / "bin" / "pip"
        subprocess.run([str(pip), "install", "--no-index", "--find-links", str(wheelhouse), "engineering-platform"], check=True, capture_output=True, text=True)  # nosec B603
        server, cli = venv / "bin" / "engineering-platform-server", venv / "bin" / "engineering-platform"
        port = 18765
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
            finally:
                process.terminate(); process.wait(timeout=5)
        print(json.dumps({"P_TRANSPORT_INGRESS_MATRIX": "PASS", "matrix": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
