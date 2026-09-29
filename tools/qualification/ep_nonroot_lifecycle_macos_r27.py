#!/usr/bin/env python3
"""R27 EP preserve/restore on disposable roots with a local service adapter."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import plistlib
import pwd
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from engineering_platform import system_instance_provisioner as provisioner
from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context as providers
from engineering_platform import system_server_service as services

ACCOUNT = ""
HOME = Path("/")
WHEEL = Path("/")
EXPECTED_UID = -1
DIGEST = "sha256:22dd1e49c263b55dc9eee396810a09fc43509984fe685f3c00d26289d55e8adc"
REVISION = "ad44263f6ec87ea018cda11f053fa12521ae9d79"

def configure():
    global ACCOUNT, HOME, WHEEL, EXPECTED_UID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--expected-uid", type=int, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    ACCOUNT, EXPECTED_UID = args.account, args.expected_uid
    HOME, WHEEL = args.home, args.wheel
    require(EXPECTED_UID > 0 and HOME.is_absolute() and WHEEL.is_absolute(),
            "Explicit absolute qualification inputs required")

def require(value, error):
    if not value:
        raise RuntimeError(error)

def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

class Controller:
    def __init__(self, launch_root):
        self.launch_root = launch_root
        self.interpreters = {}
        self.children = {}
        self.started_pid = None

    def register(self, instance, interpreter):
        paths = services.instance_default_paths(instance, self.launch_root)
        paths.launch_daemons_dir.mkdir(parents=True, exist_ok=True)
        definition = services.instance_service_definition(instance, interpreter=interpreter)
        paths.plist_path.write_bytes(plistlib.dumps(services.instance_plist_payload(paths, definition)))
        self.interpreters[instance.instance_id] = interpreter
        return {"result": "REGISTERED", "label": instance.service_label}

    def quiesce(self, instance):
        child = self.children.pop(instance.instance_id, None)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        return {"result": "QUIESCED"}

    def start(self, instance):
        interpreter = self.interpreters[instance.instance_id]
        env = {
            "HOME": str(HOME),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C", "LC_ALL": "C",
            "TMPDIR": str(HOME),
            "PYTHONDONTWRITEBYTECODE": "1",
            **providers.server_environment(instance),
        }
        log = open(instance.root / "supervised-server.log", "ab", buffering=0)
        child = subprocess.Popen(
            [str(interpreter), "-I", "-m", "engineering_platform.server", "serve",
             "--data-root", str(instance.data_root), "--expected-instance-id", instance.instance_id],
            cwd=HOME, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        log.close()
        self.children[instance.instance_id] = child
        self.started_pid = child.pid
        bind_port = json.loads((instance.data_root / "server.json").read_text())["bind_port"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Adapter foreground server exited: {child.returncode}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{bind_port}/health", timeout=2
                ) as response:
                    report = json.load(response)
                if report.get("instance_id") == instance.instance_id:
                    return {"result": "RUNNING"}
            except (OSError, ValueError, urllib.error.HTTPError):
                pass
            time.sleep(0.2)
        raise RuntimeError("Adapter foreground server did not answer")

    def loaded(self, instance):
        child = self.children.get(instance.instance_id)
        return child is not None and child.poll() is None

    def remove(self, instance):
        self.quiesce(instance)
        paths = services.instance_default_paths(instance, self.launch_root)
        paths.plist_path.unlink(missing_ok=True)
        return {"result": "REMOVED"}

def main():
    uid, gid = os.geteuid(), os.getegid()
    account = pwd.getpwnam(ACCOUNT)
    require(uid == EXPECTED_UID and uid == account.pw_uid and gid == account.pw_gid,
            "Wrong lifecycle process identity")
    require(HOME.resolve() == Path(account.pw_dir).resolve()
            and HOME.stat().st_uid == uid
            and HOME.stat().st_mode & 0o077 == 0,
            "Test home is not private and owned by the exact process identity")
    require(sys.version_info[:2] == (3, 14), "Wrong Python version")
    require(hashlib.sha256(WHEEL.read_bytes()).hexdigest() == DIGEST[7:],
            "Wrong published wheel digest")
    work = Path(tempfile.mkdtemp(prefix="ep-r27-lifecycle-", dir=HOME))
    product_root = work / "product"
    launch_root = work / "launch-standin-only"
    launch_root.mkdir()
    controller = Controller(launch_root)
    engine = provisioner.SystemInstanceProvisioner(
        product_root, controller=controller, launch_daemons_dir=launch_root,
        account_lookup=pwd.getpwnam,
    )
    instance_id = "ep-r27-native-instance-001"
    instance = topology.system_instance_topology(
        engine.product, instance_id=instance_id, display_label="EP R27 native lifecycle",
        service_account=ACCOUNT,
    )
    try:
        # Synthetically recorded provider receipts are needed only to exercise
        # the lifecycle state machine. The executables deliberately fail.
        for context in providers.provider_contexts(instance):
            context.executable.parent.mkdir(parents=True, exist_ok=True)
            context.executable.write_text("#!/bin/sh\nexit 67\n")
            context.executable.chmod(0o755)
            engine.provider_register(
                instance_id=instance.instance_id, display_label=instance.display_label,
                service_account=ACCOUNT, provider=context.provider,
                executable_sha256=providers.executable_digest(context.executable),
                version="synthetic-lifecycle-only",
                auth_reference=f"r27-synthetic:{context.provider}:{instance_id}",
                auth_bootstrap_receipt=f"r27-synthetic-receipt:{context.provider}:{instance_id}",
            )
        release = provisioner.ReleaseRequest("2.3.105", WHEEL, DIGEST, REVISION)
        original_run = provisioner.subprocess.run
        failed_child = []
        def observed_run(*args, **kwargs):
            outcome = original_run(*args, **kwargs)
            if outcome.returncode:
                failed_child.append({
                    "command_kind": "pip-install" if "pip" in str(args[0]) else "other",
                    "returncode": outcome.returncode,
                    "stderr_tail": (outcome.stderr or "")[-1000:],
                    "stdout_tail": (outcome.stdout or "")[-500:],
                })
            return outcome
        provisioner.subprocess.run = observed_run
        try:
            creation = engine.create(provisioner.InstanceRequest(
                "install-r27-native-001", instance_id, instance.display_label,
                ACCOUNT, port(), release,
            ))
        except Exception as error:
            raise RuntimeError(
                f"create failed in disposable root {work}: {error}; "
                f"actual subprocess failures={failed_child}"
            ) from error
        finally:
            provisioner.subprocess.run = original_run
        require(creation.get("result") == "COMPLETE" and controller.loaded(instance),
                "Product instance creation with real server failed")
        identity_before = (instance.data_root / "runtime-identity.json").read_bytes()
        descriptor_before = instance.descriptor.read_bytes()
        preserve = engine.preserve(
            instance_id, "preserve-r27-native-001", confirm_instance_id=instance_id,
        )
        preserved = preserve["receipt"]["evidence"]
        require(preserved.get("lifecycle_state") == "UNINSTALLED_DATA_PRESERVED"
                and preserved.get("provider_auth_state") == "PRESERVED_REQUIRES_REVERIFICATION"
                and not controller.loaded(instance),
                "Preserve did not remove own server and demote provider state")
        require((instance.data_root / "runtime-identity.json").read_bytes() == identity_before
                and instance.descriptor.read_bytes() == descriptor_before,
                "Preserve changed instance identity/data")
        provider_rejected = []
        for context in providers.provider_contexts(instance):
            try:
                providers.readback(context)
            except providers.SystemProviderContextError:
                provider_rejected.append(context.provider)
        require(set(provider_rejected) == {"codex", "github"},
                "Preserved synthetic provider evidence remained ready")
        restore = engine.restore(
            instance_id, "restore-r27-native-001",
            preserve_operation_id="preserve-r27-native-001", release=release,
        )
        restored = restore["receipt"]["evidence"]
        require(restored.get("lifecycle_state") == "RESTORED_REQUIRES_PROVIDER_REVERIFICATION"
                and restored.get("provider_auth_state") == "PRESERVED_REQUIRES_REVERIFICATION"
                and restored.get("ready") is False and not controller.loaded(instance),
                "Restore activated service or promoted provider")
        require((instance.data_root / "runtime-identity.json").read_bytes() == identity_before
                and instance.descriptor.read_bytes() == descriptor_before,
                "Restore changed instance identity/data")
        require(all(
            not context.auth_receipt.exists()
            or "PRESERVED" in context.auth_receipt.read_text()
            for context in providers.provider_contexts(instance)
        ), "Restore unexpectedly generated fresh provider authentication")
        return {
            "status": "PASS_ADAPTED_LIFECYCLE_WITH_REAL_INITIAL_SERVER",
            "uid": uid, "gid": gid, "server_pid": controller.started_pid,
            "release": "2.3.105", "wheel_sha256": DIGEST,
            "preserve_state": preserved["lifecycle_state"],
            "restore_state": restored["lifecycle_state"],
            "provider_readback_rejected_after_preserve": provider_rejected,
            "restore_ready": restored["ready"],
            "server_loaded_after_restore": controller.loaded(instance),
            "test_root": str(work),
            "limits": "Synthetic provider records and filesystem service adapter; no launchd or live provider authentication",
        }
    finally:
        controller.quiesce(instance)

if __name__ == "__main__":
    try:
        configure()
        print(json.dumps(main(), sort_keys=True))
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, sort_keys=True))
        raise
