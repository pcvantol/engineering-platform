#!/usr/bin/env python3
"""Qualify MPR from exact committed wheel bytes outside the source checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


def installed_matrix(wheel: Path, source: str, evidence: Path) -> None:
    import engineering_platform
    from tests.engineering.test_managed_adoption import AdoptionLifecycleTests
    from engineering_platform.agent_state import StateStore
    from engineering_platform.execution_lease import LEASE_TIMEOUT_SECONDS
    package = Path(engineering_platform.__file__).resolve().parent
    if Path(sys.prefix).resolve() not in package.parents or "site-packages" not in package.parts:
        raise RuntimeError("MPR_REQUIRES_NONEDITABLE_INSTALLED_PACKAGE")
    with zipfile.ZipFile(wheel) as archive:
        for relative in ("managed_adoption.py", "managed_publication.py", "execution_host.py", "agent_state.py", "submission_service.py"):
            if (package / relative).read_bytes() != archive.read("engineering_platform/" + relative):
                raise RuntimeError("MPR_INSTALLED_MODULE_BYTES_DIFFER")
    fixture = AdoptionLifecycleTests()
    fixture.setUp()
    try:
        area = fixture.area
        (area / "selection.json").write_text(json.dumps(fixture.selection))
        command = [sys.executable, "-m", "tests.engineering.mpr_process_worker", str(area)]
        started = time.monotonic()
        first = subprocess.run([*command, "start"], text=True, capture_output=True, timeout=45)
        if first.returncode != 91:
            raise RuntimeError("MPR_CRASH_BOUNDARY_NOT_REACHED: " + first.stderr[-1500:])
        store = StateStore(fixture.store.directory, central_database=fixture.database, emit_local_projection=False)
        before = store.load("adopt-run")
        if before.publication_intent["status"] != "CREATE_UNCERTAIN":
            raise RuntimeError("MPR_CREATE_INTENT_NOT_DURABLE")
        refused = subprocess.run([*command, "resume"], text=True, capture_output=True, timeout=15)
        if refused.returncode == 0 or "active-run ownership conflict" not in refused.stderr:
            raise RuntimeError("MPR_LIVE_LEASE_NOT_RESPECTED")
        # Respect the real product lease timeout. No fixture edits expiry or
        # substitutes a clock to turn a dead process into fresh authority.
        print("MPR_RESTART_WAIT=existing_lease_expiry", flush=True)
        remaining = LEASE_TIMEOUT_SECONDS + 2 - (time.monotonic() - started)
        if remaining > 0: time.sleep(remaining)
        resumed = subprocess.run([*command, "resume"], text=True, capture_output=True, timeout=30)
        if resumed.returncode:
            raise RuntimeError("MPR_RESTART_FAILED: " + resumed.stderr[-1500:])
        repeated = subprocess.run([*command, "resume"], text=True, capture_output=True, timeout=30)
        if repeated.returncode:
            raise RuntimeError("MPR_REPEATED_RESUME_FAILED: " + repeated.stderr[-1500:])
        after = store.load("adopt-run")
        remote = json.loads((area / "github-ledger.json").read_text())
        invocations = [json.loads(line) for line in (area / "provider-ledger.jsonl").read_text().splitlines()]
        if (after.pull_request != 71 or after.phase != "WAIT_FOR_OPERATOR_MERGE"
                or after.managed_candidate_adoption != before.managed_candidate_adoption
                or after.repair_iterations != before.repair_iterations
                or remote["creates"] != 1
                or sorted(item["role"] for item in invocations) != ["quality", "security", "validation_assessment"]):
            raise RuntimeError("MPR_RESTART_IDENTITY_OR_COUNT_MISMATCH")
        result = {"status": "PASS", "source": source, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  "python": sys.version.split()[0], "installed_package": str(package),
                  "adoption_to_real_validation_and_assurance": "PASS", "real_process_crash_and_restart": "PASS",
                  "live_lease_refusal": "PASS", "repeated_resume": "PASS", "github_creates": remote["creates"],
                  "implementation_invocations": 0, "repair_count_preserved": True,
                  "adapters": ["external_provider_and_credentials", "GitHub_PR_transport", "GitHub_Git_transport_to_local_bare_origin"],
                  "live_github_mutation": False}
        evidence.write_text(json.dumps(result, indent=2) + "\n")
        print("MPR_INSTALLED_MATRIX=PASS", flush=True)
    finally:
        fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--source-sha")
    args = parser.parse_args()
    if args.installed:
        installed_matrix(args.wheel.resolve(), args.source_sha, args.evidence.resolve())
        return
    source = args.source_root.resolve()
    sha = subprocess.check_output(("git", "-C", str(source), "rev-parse", "HEAD"), text=True).strip()
    with tempfile.TemporaryDirectory(prefix="ep-mpr-installed-") as temporary:
        work = Path(temporary)
        if args.wheel:
            wheel = args.wheel.resolve()
        else:
            subprocess.run([sys.executable, str(source / "tools/qualification/build_platform_wheel.py"), "--source-root", str(source), "--wheel-directory", str(work / "dist")], check=True)
            wheel, = (work / "dist").glob("*.whl")
        if subprocess.check_output(("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"), text=True).strip():
            raise RuntimeError("MPR_SOURCE_MUST_BE_COMMITTED")
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.namelist():
                if member.startswith("engineering_platform/") and not member.endswith("/"):
                    committed = subprocess.check_output(("git", "-C", str(source), "show", f"{sha}:src/{member}"))
                    if archive.read(member) != committed:
                        raise RuntimeError("MPR_WHEEL_SOURCE_IDENTITY_MISMATCH")
        subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True)
        python = work / "venv/bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], check=True)
        runner = work / "runner"
        (runner / "tests/engineering").mkdir(parents=True)
        for relative in ("tests/__init__.py", "tests/engineering/__init__.py", "tests/engineering/harness_isolation.py",
                         "tests/engineering/test_execution_host.py", "tests/engineering/test_managed_adoption.py",
                         "tests/engineering/test_managed_publication.py", "tests/engineering/mpr_process_worker.py"):
            shutil.copy2(source / relative, runner / relative)
        shutil.copy2(Path(__file__), runner / "mpr_matrix.py")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        subprocess.run([str(python), "-m", "unittest", "tests.engineering.test_managed_adoption", "tests.engineering.test_managed_publication", "-q"],
                       cwd=runner, env=environment, check=True)
        subprocess.run([str(python), str(runner / "mpr_matrix.py"), "--installed", "--wheel", str(wheel),
                        "--source-sha", sha, "--evidence", str(args.evidence.resolve())], cwd=runner, env=environment, check=True, timeout=150)


if __name__ == "__main__": main()
