#!/usr/bin/env python3
"""Exact-wheel EP preserve/restore/purge SIGKILL phase qualification.

The supervised child enters the installed product through the normal
SystemInstanceProvisioner API.  The local service adapter writes only inside
its disposable root.  Unobserved boundaries remain NOT_HIT.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


ALPHA = "ep-crash-alpha"
BRAVO = "ep-crash-bravo"
OPERATIONS = {"preserve": "preserve-crash-alpha", "restore": "restore-crash-alpha", "purge": "purge-crash-alpha"}
BOUNDARIES = {
    "preserve": ("PREPARED", "VERIFIED", "RECEIPT", "COMPLETE"),
    "restore": ("PREPARED", "VERIFIED", "RECEIPT", "COMPLETE"),
    "purge": ("PREPARED", "PURGE_READY", "REMOVE_RECEIPT", "PARTIAL_DELETE", "TOMBSTONE", "RECEIPT", "COMPLETE"),
}
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"}


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("disposable sibling gained a symlink")
        rows.append((path.relative_to(root).as_posix(), file_digest(path) if path.is_file() else "DIRECTORY"))
    return "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def auth_states(instance_root: Path) -> dict[str, object]:
    result = {}
    for provider in ("codex", "github"):
        path = instance_root / "providers" / provider / "auth-state.json"
        result[provider] = json.loads(path.read_text())["state"] if path.exists() else "ABSENT"
    return result


def child_command(python: Path, helper: Path, mode: str, root: Path, wheel: Path,
                  source: str, version: str, files: int) -> list[str]:
    return [str(python), "-I", str(helper), "--internal", mode, str(root),
            str(wheel), source, version, str(files)]


def run_child(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, env=ENV, capture_output=True, text=True, check=False)


def require_child(command: list[str], root: Path, label: str) -> None:
    result = run_child(command, root)
    if result.returncode:
        raise RuntimeError(label + " failed: " + result.stderr[-300:])


def read_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def observed_boundary(boundary: str, phase: object, receipt: Path, remove_receipt: Path,
                      tombstone: Path, sentinel: Path, files: int) -> dict[str, object] | None:
    if boundary in ("PREPARED", "VERIFIED", "PURGE_READY", "COMPLETE"):
        return {"phase": phase} if phase == boundary else None
    if boundary == "RECEIPT":
        return {"phase": phase, "receipt_observed": True} if receipt.exists() and phase != "COMPLETE" else None
    if boundary == "REMOVE_RECEIPT":
        return {"phase": phase, "remove_receipt_observed": True} if phase == "PURGE_READY" and remove_receipt.exists() else None
    if boundary == "TOMBSTONE":
        return {"phase": phase, "tombstone_observed": True} if tombstone.exists() and not receipt.exists() else None
    if boundary == "PARTIAL_DELETE" and phase == "PURGE_READY" and sentinel.is_dir():
        remaining = len(os.listdir(sentinel))
        if 0 < remaining < files:
            return {"phase": phase, "files_at_signal": remaining, "files_before": files}
    return None


def qualify(wheel: Path, expected_digest: str, version: str, source: str,
            operation: str, boundary: str, files: int) -> dict[str, object]:
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("EP qualification requires Python 3.14 without bypass")
    if operation not in BOUNDARIES or boundary not in BOUNDARIES[operation]:
        raise RuntimeError("operation or durable boundary is not registered")
    wheel = wheel.resolve(strict=True)
    if file_digest(wheel) != expected_digest:
        raise RuntimeError("exact EP wheel digest mismatch")
    helper = Path(__file__).with_name("system_instance_purge_sigkill.py").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ep-lifecycle-sigkill-") as temporary:
        root = Path(temporary)
        venv = root / "bootstrap-venv"
        made = subprocess.run((sys.executable, "-I", "-m", "venv", str(venv)),
                              capture_output=True, text=True, check=False)
        if made.returncode:
            raise RuntimeError("disposable Python 3.14 environment failed")
        python = venv / "bin" / "python"
        installed = subprocess.run((str(python), "-I", "-m", "pip", "install", "--isolated",
                                    "--no-deps", "--no-index", str(wheel)),
                                   capture_output=True, text=True, check=False)
        if installed.returncode:
            raise RuntimeError("exact EP wheel installation failed")
        command = lambda mode: child_command(python, helper, mode, root, wheel, source, version, files)
        require_child(command("setup"), root, "two-instance setup")
        if operation == "restore":
            require_child(command("preserve"), root, "normal preserve prerequisite")

        product = root / "product"
        selected = product / "instances" / ALPHA
        sibling = product / "instances" / BRAVO
        lifecycle_lock = selected / "locks" / "lifecycle.lock"
        lifecycle_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lifecycle_lock.open("a+b") as lock_stream:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            live_conflict_rejected = run_child(command("conflict-" + operation), root).returncode != 0
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
        if not live_conflict_rejected:
            raise RuntimeError("normal provisioner accepted a competing live lock owner")
        sibling_before = tree_digest(sibling)
        op_id = OPERATIONS[operation]
        operation_root = product / "instance-lifecycle-v1" / ALPHA / "operations" / op_id
        state_path = operation_root / "state.json"
        receipt_path = operation_root / "receipt.json"
        remove_receipt = product / "receipts" / ALPHA / (op_id + ".json")
        tombstone = product / "instance-lifecycle-v1" / ALPHA / "purged.json"
        sentinel = selected / "crash-sentinel"
        detached_sentinel = selected.parent / "missing"
        if operation == "purge":
            # Legacy REMOVE first renames or removes the instance tree directly;
            # observe deletion in its original root using the product's state.
            detached_sentinel = sentinel
        child = subprocess.Popen(command(operation), cwd=root, env=ENV,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        observed = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and child.poll() is None:
            state = read_state(state_path)
            observed = observed_boundary(boundary, state.get("phase"), receipt_path,
                                         remove_receipt, tombstone, detached_sentinel, files)
            if observed is not None and child.poll() is None:
                os.kill(child.pid, signal.SIGKILL)
                break
            observed = None
            time.sleep(0)
        _, stderr = child.communicate(timeout=30)
        after = read_state(state_path)
        process_table = subprocess.run(("/bin/ps", "-axo", "pid=,ppid="),
                                       capture_output=True, text=True, check=False)
        if process_table.returncode:
            return {"result": "UNPROVEN", "reason": "descendant readback unavailable"}
        descendants = sum(1 for line in process_table.stdout.splitlines()
                          if len(line.split()) == 2 and line.split()[1] == str(child.pid))
        result: dict[str, object] = {
            "operation": operation, "boundary": boundary, "version": version,
            "wheel_sha256": expected_digest, "source_checkout_import": False,
            "observed": observed, "phase_after_kill": after.get("phase"),
            "signal_exit": "SIGKILL" if child.returncode == -signal.SIGKILL else "OTHER",
            "owned_descendants_alive": descendants,
            "live_conflict_rejected": live_conflict_rejected,
            "remove_receipt_after_kill": remove_receipt.exists(),
            "lifecycle_receipt_after_kill": receipt_path.exists(),
            "tombstone_after_kill": tombstone.exists(),
            "selected_root_after_kill": selected.exists(),
            "service_definition_count_after_kill": len(list((root / "LaunchDaemons").glob("*.plist"))),
            "provider_states_after_kill": auth_states(selected) if selected.exists() else {"codex": "ABSENT", "github": "ABSENT"},
            "sibling_byte_identical_after_kill": tree_digest(sibling) == sibling_before,
        }
        if boundary == "PARTIAL_DELETE":
            result["files_after_kill"] = len(os.listdir(sentinel)) if sentinel.is_dir() else 0
        if (observed is None or child.returncode != -signal.SIGKILL or descendants
                or after.get("phase") != observed["phase"]):
            result.update(result="NOT_HIT", child_error=stderr[-200:])
            return result
        if boundary == "PARTIAL_DELETE" and not 0 < result["files_after_kill"] < files:
            result.update(result="UNPROVEN", reason="partial physical deletion not retained")
            return result
        changed_request_rejected = run_child(command("changed-" + operation), root).returncode != 0
        foreign_operation_rejected = run_child(command("foreign-operation-" + operation), root).returncode != 0
        foreign_instance_rejected = run_child(command("foreign-instance-" + operation), root).returncode != 0
        resumed = run_child(command(operation), root)
        if resumed.returncode:
            result.update(result="GAP_PROVEN", resume_error_class=resumed.stderr.splitlines()[-1].split(":", 1)[0])
            return result
        terminal = json.loads(resumed.stdout)
        receipt_bytes = receipt_path.read_bytes()
        replay = run_child(command(operation), root)
        identical = (replay.returncode == 0 and json.loads(replay.stdout) == terminal
                     and receipt_path.read_bytes() == receipt_bytes)
        evidence = terminal["receipt"]["evidence"]
        provider_states = auth_states(selected) if selected.exists() else {"codex": "ABSENT", "github": "ABSENT"}
        expect_purged = operation == "purge"
        restore_after_purge_rejected = (
            run_child(command("restore-after-purge"), root).returncode != 0
            if expect_purged else None
        )
        okay = (identical and changed_request_rejected and foreign_operation_rejected
                and foreign_instance_rejected and live_conflict_rejected
                and (not expect_purged or restore_after_purge_rejected)
                and tree_digest(sibling) == sibling_before
                and selected.exists() != expect_purged and tombstone.exists() == expect_purged
                and ((evidence.get("lifecycle_state") == "PURGED" and evidence.get("restorable") is False)
                     if expect_purged else all(value == "PRESERVED_REQUIRES_REVERIFICATION" for value in provider_states.values()))
                and (operation != "restore" or evidence.get("service_state") == "REGISTERED_INACTIVE"
                     and evidence.get("ready") is False))
        result.update(result="PASS" if okay else "FAIL", terminal_replay_identical=identical,
                      changed_request_rejected=changed_request_rejected,
                      foreign_operation_status_rejected=foreign_operation_rejected,
                      foreign_instance_status_rejected=foreign_instance_rejected,
                      restore_after_purge_rejected=restore_after_purge_rejected,
                      sibling_byte_identical=tree_digest(sibling) == sibling_before,
                      selected_root_final_exists=selected.exists(), tombstone_final_exists=tombstone.exists(),
                      provider_states_final=provider_states,
                      lifecycle_state=evidence.get("lifecycle_state"),
                      service_state=evidence.get("service_state"), ready=evidence.get("ready"))
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--operation", choices=tuple(OPERATIONS), required=True)
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not 1000 <= args.files <= 50000:
        parser.error("bounded file count must be between 1000 and 50000")
    result = qualify(args.wheel, args.wheel_sha256, args.version, args.source_revision,
                     args.operation, args.boundary, args.files)
    args.evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
