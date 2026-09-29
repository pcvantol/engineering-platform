#!/usr/bin/env python3
"""Observe a real SIGKILL during exact-wheel EP system-instance purge.

The only child service adapter writes disposable plist files below the selected
temporary root.  The installed wheel supplies the product provisioner and
lifecycle code; this harness never imports the checkout's package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace


ALPHA = "ep-crash-alpha"
BRAVO = "ep-crash-bravo"
OPERATION = "purge-crash-alpha"


def _digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RuntimeError("disposable sibling gained a symbolic link")
        rows.append((relative, _digest_file(path) if path.is_file() else "DIRECTORY"))
    return "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _child(mode: str, root: Path, wheel: Path, source: str, version: str, files: int) -> None:
    import engineering_platform
    from engineering_platform import system_instance_provisioner as provisioner
    from engineering_platform import system_installation_topology as topology
    from engineering_platform import system_provider_context as providers
    from engineering_platform import system_server_service as services

    if not Path(engineering_platform.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("EP child imported outside its exact-wheel installation")

    launch_root = root / "LaunchDaemons"
    launch_root.mkdir(parents=True, exist_ok=True)

    class Controller:
        def register(self, instance, interpreter):
            paths = services.instance_default_paths(instance, launch_root)
            definition = services.instance_service_definition(instance, interpreter=interpreter)
            paths.plist_path.write_bytes(plistlib.dumps(services.instance_plist_payload(paths, definition)))
            return {"result": "REGISTERED", "label": instance.service_label}

        def quiesce(self, instance):
            return {"result": "QUIESCED"}

        def start(self, instance):
            return {"result": "RUNNING"}

        def loaded(self, instance):
            return False

        def remove(self, instance):
            services.instance_default_paths(instance, launch_root).plist_path.unlink(missing_ok=True)
            return {"result": "REMOVED"}

    accounts = {ALPHA: "_ep_crash_alpha", BRAVO: "_ep_crash_bravo"}
    engine = provisioner.SystemInstanceProvisioner(
        root / "product", controller=Controller(), launch_daemons_dir=launch_root,
        account_lookup=lambda name: SimpleNamespace(pw_uid=501) if name in accounts.values()
        else (_ for _ in ()).throw(KeyError(name)),
        health_verifier=lambda instance, interpreter: {
            "result": "PASS", "instance_id": instance.instance_id,
            "interpreter": str(interpreter), "identity_aware": True,
        },
    )
    release = provisioner.ReleaseRequest(version, wheel, _digest_file(wheel), source)

    if mode == "setup":
        for identity, port in ((ALPHA, 18765), (BRAVO, 18766)):
            instance = topology.system_instance_topology(
                engine.product, instance_id=identity, display_label=identity,
                service_account=accounts[identity],
            )
            for context in providers.provider_contexts(instance):
                context.executable.parent.mkdir(parents=True, exist_ok=True)
                context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                context.executable.chmod(0o755)
                engine.provider_register(
                    instance_id=identity, display_label=identity,
                    service_account=accounts[identity], provider=context.provider,
                    executable_sha256=providers.executable_digest(context.executable),
                    version="qualification-1",
                    auth_reference=f"{context.provider}:auth:{identity}",
                    auth_bootstrap_receipt=f"{context.provider}:receipt:{identity}",
                )
            engine.create(provisioner.InstanceRequest(
                "install-" + identity, identity, identity, accounts[identity], port, release,
            ))
        payload = root / "product" / "instances" / ALPHA / "crash-sentinel"
        payload.mkdir(mode=0o700)
        for index in range(files):
            (payload / f"file-{index:05d}").write_bytes(b"sentinel\n")
        print("SETUP_COMPLETE", flush=True)
    elif mode == "purge":
        result = engine.purge(ALPHA, OPERATION, confirm_instance_id=ALPHA)
        print(json.dumps(result, sort_keys=True), flush=True)
    elif mode == "preserve":
        result = engine.preserve(ALPHA, "preserve-crash-alpha", confirm_instance_id=ALPHA)
        print(json.dumps(result, sort_keys=True), flush=True)
    elif mode == "restore":
        result = engine.restore(
            ALPHA, "restore-crash-alpha", preserve_operation_id="preserve-crash-alpha",
            release=release,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
    elif mode == "conflict-preserve":
        engine.preserve(ALPHA, "conflict-preflight", confirm_instance_id=ALPHA)
    elif mode == "conflict-restore":
        engine.restore(ALPHA, "conflict-preflight", preserve_operation_id="preserve-crash-alpha",
                       release=release)
    elif mode == "conflict-purge":
        engine.purge(ALPHA, "conflict-preflight", confirm_instance_id=ALPHA)
    elif mode == "changed-preserve":
        engine.preserve(ALPHA, "preserve-crash-alpha", confirm_instance_id=BRAVO)
    elif mode == "changed-restore":
        changed = provisioner.ReleaseRequest(version, wheel, _digest_file(wheel), "0" * 40)
        engine.restore(ALPHA, "restore-crash-alpha", preserve_operation_id="preserve-crash-alpha",
                       release=changed)
    elif mode == "changed-purge":
        engine.purge(ALPHA, OPERATION, confirm_instance_id=BRAVO)
    elif mode.startswith("foreign-operation-"):
        engine.lifecycle_status(ALPHA, "foreign-operation-no-evidence")
    elif mode.startswith("foreign-instance-"):
        operation = mode.removeprefix("foreign-instance-")
        operation_id = {"preserve": "preserve-crash-alpha", "restore": "restore-crash-alpha",
                        "purge": OPERATION}[operation]
        engine.lifecycle_status(BRAVO, operation_id)
    elif mode == "restore-after-purge":
        engine.restore(ALPHA, "restore-after-purge", preserve_operation_id="preserve-crash-alpha",
                       release=release)
    else:
        raise RuntimeError("invalid child mode")


def _run_child(python: Path, script: Path, mode: str, root: Path, wheel: Path,
               source: str, version: str, files: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(python), "-I", str(script), "--internal", mode, str(root), str(wheel),
         source, version, str(files)),
        cwd=root, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1",
                       "PYTHONSAFEPATH": "1"},
        text=True, capture_output=True, check=False,
    )


def qualify(wheel: Path, expected_digest: str, source: str, version: str,
            files: int) -> dict[str, object]:
    wheel = wheel.resolve(strict=True)
    if _digest_file(wheel) != expected_digest:
        raise RuntimeError("selected EP wheel digest does not match the declared candidate")
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("EP process-crash qualification requires Python 3.14")
    script = Path(__file__).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ep-purge-sigkill-") as temporary:
        root = Path(temporary)
        venv = root / "bootstrap-venv"
        made = subprocess.run((sys.executable, "-I", "-m", "venv", str(venv)),
                              text=True, capture_output=True, check=False)
        if made.returncode:
            raise RuntimeError("disposable Python 3.14 venv creation failed")
        python = venv / "bin" / "python"
        installed = subprocess.run(
            (str(python), "-I", "-m", "pip", "install", "--isolated", "--no-deps",
             "--no-index", str(wheel)),
            text=True, capture_output=True, check=False,
        )
        if installed.returncode:
            raise RuntimeError("exact EP wheel installation failed")
        setup = _run_child(python, script, "setup", root, wheel, source, version, files)
        if setup.returncode or setup.stdout.strip() != "SETUP_COMPLETE":
            raise RuntimeError("disposable EP two-instance setup failed: " + setup.stderr[-300:])

        product = root / "product"
        sibling = product / "instances" / BRAVO
        sibling_before = _tree_digest(sibling)
        sentinel = product / "instances" / ALPHA / "crash-sentinel"
        state_path = product / "instance-lifecycle-v1" / ALPHA / "operations" / OPERATION / "state.json"
        command = (str(python), "-I", str(script), "--internal", "purge", str(root),
                   str(wheel), source, version, str(files))
        child = subprocess.Popen(
            command, cwd=root, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                    "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        observed = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and child.poll() is None:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("phase") == "PURGE_READY" and sentinel.is_dir():
                remaining = len(os.listdir(sentinel))
                if 0 < remaining < files and child.poll() is None:
                    observed = {"phase": "PURGE_READY", "files_before": files,
                                "files_at_signal": remaining}
                    os.kill(child.pid, signal.SIGKILL)
                    break
            time.sleep(0.001)
        stdout, stderr = child.communicate(timeout=15)
        if observed is None or child.returncode != -signal.SIGKILL:
            return {"result": "NOT_HIT", "observed": observed,
                    "exit_kind": "OTHER", "child_error": stderr[-200:]}
        process_table = subprocess.run(
            ("/bin/ps", "-axo", "pid=,ppid="), text=True,
            capture_output=True, check=False,
        )
        descendants = 0
        if process_table.returncode == 0:
            for line in process_table.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) == child.pid:
                    descendants += 1
        else:
            return {"result": "UNPROVEN", "reason": "DESCENDANT_READBACK_UNAVAILABLE"}
        if descendants:
            return {"result": "UNPROVEN", "reason": "OWNED_DESCENDANT_SURVIVED",
                    "owned_descendants_alive": descendants}
        after_kill = len(os.listdir(sentinel)) if sentinel.is_dir() else 0
        if not 0 < after_kill < files:
            return {"result": "UNPROVEN", "observed": observed,
                    "files_after_kill": after_kill}
        if _tree_digest(sibling) != sibling_before:
            return {"result": "FAIL", "reason": "SIBLING_CHANGED_AFTER_KILL"}
        resumed = _run_child(python, script, "purge", root, wheel, source, version, files)
        tombstone = product / "instance-lifecycle-v1" / ALPHA / "purged.json"
        selected = product / "instances" / ALPHA
        sibling_after = _tree_digest(sibling)
        result: dict[str, object] = {
            "result": "PASS" if resumed.returncode == 0 else "GAP_PROVEN",
            "wheel_sha256": expected_digest,
            "version": version,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "source_checkout_import": False,
            "mutation_phase": observed["phase"],
            "signal_exit": "SIGKILL",
            "owned_descendants_alive": descendants,
            "files_before": files,
            "files_at_signal": observed["files_at_signal"],
            "files_after_kill": after_kill,
            "same_operation_replay": "COMPLETE" if resumed.returncode == 0 else "FAILED",
            "selected_root_exists": selected.exists(),
            "tombstone_exists": tombstone.exists(),
            "sibling_byte_identical": sibling_after == sibling_before,
        }
        if resumed.returncode == 0:
            receipt = json.loads(resumed.stdout)
            result["lifecycle_state"] = receipt["receipt"]["evidence"]["lifecycle_state"]
            again = _run_child(python, script, "purge", root, wheel, source, version, files)
            result["terminal_replay_identical"] = (
                again.returncode == 0 and json.loads(again.stdout) == receipt
            )
            if (result["lifecycle_state"] != "PURGED" or selected.exists()
                    or not tombstone.exists() or not result["sibling_byte_identical"]
                    or not result["terminal_replay_identical"]):
                result["result"] = "FAIL"
        else:
            result["resume_error_class"] = resumed.stderr.splitlines()[-1].split(":", 1)[0]
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--source-revision")
    parser.add_argument("--version")
    parser.add_argument("--files", type=int, default=20000)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--internal", nargs=6, metavar=("MODE", "ROOT", "WHEEL", "SOURCE", "VERSION", "FILES"))
    args = parser.parse_args()
    if args.internal is not None:
        mode, root, wheel, source, version, files = args.internal
        _child(mode, Path(root), Path(wheel), source, version, int(files))
        return 0
    if (args.wheel is None or args.wheel_sha256 is None or args.source_revision is None
            or args.version is None
            or args.evidence is None or not 1000 <= args.files <= 50000):
        parser.error("exact wheel, digest, source, evidence and bounded file count are required")
    result = qualify(args.wheel, args.wheel_sha256, args.source_revision, args.version, args.files)
    args.evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
