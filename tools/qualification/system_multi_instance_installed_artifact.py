#!/usr/bin/env python3
"""Qualify two isolated system Server instances from installed wheel bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


_CHILD = r'''
from pathlib import Path
import json, os, plistlib, sys
from types import SimpleNamespace
from engineering_platform import system_instance_provisioner as provisioner
from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context as providers
from engineering_platform import system_server_service as services

wheel, product_root, launch_root, version, digest, revision, baseline_wheel, baseline_version, baseline_digest, baseline_revision = sys.argv[1:]
product_root, launch_root, wheel, baseline_wheel = Path(product_root), Path(launch_root), Path(wheel), Path(baseline_wheel)
launch_root.mkdir(parents=True)

class Controller:
    def __init__(self): self.loaded_ids = set()
    def register(self, instance, interpreter):
        paths = services.instance_default_paths(instance, launch_root)
        definition = services.instance_service_definition(instance, interpreter=interpreter)
        paths.plist_path.write_bytes(plistlib.dumps(services.instance_plist_payload(paths, definition)))
        return {"result":"REGISTERED","label":instance.service_label}
    def quiesce(self, instance): self.loaded_ids.discard(instance.instance_id); return {"result":"QUIESCED"}
    def start(self, instance): self.loaded_ids.add(instance.instance_id); return {"result":"RUNNING"}
    def loaded(self, instance): return instance.instance_id in self.loaded_ids
    def remove(self, instance):
        self.quiesce(instance)
        services.instance_default_paths(instance, launch_root).plist_path.unlink(missing_ok=True)
        return {"result":"REMOVED"}

accounts = {"ep-installed-alpha":"_ep_alpha", "ep-installed-bravo":"_ep_bravo"}
controller = Controller()
engine = provisioner.SystemInstanceProvisioner(
    product_root, controller=controller, launch_daemons_dir=launch_root,
    account_lookup=lambda name: SimpleNamespace(pw_uid=501) if name in accounts.values() else (_ for _ in ()).throw(KeyError(name)),
    health_verifier=lambda instance, interpreter: {"result":"PASS","instance_id":instance.instance_id,"interpreter":str(interpreter),"identity_aware":True},
)
release = provisioner.ReleaseRequest(version, wheel, digest, revision)
baseline_release = provisioner.ReleaseRequest(
    baseline_version, baseline_wheel, baseline_digest, baseline_revision,
)

def bootstrap(identity, label):
    instance = topology.system_instance_topology(
        engine.product, instance_id=identity, display_label=label,
        service_account=accounts[identity],
    )
    for context in providers.provider_contexts(instance):
        context.executable.parent.mkdir(parents=True, exist_ok=True)
        context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        context.executable.chmod(0o755)
        engine.provider_register(
            instance_id=identity, display_label=label, service_account=accounts[identity],
            provider=context.provider, executable_sha256=providers.executable_digest(context.executable),
            version="qualification-1", auth_reference=f"{context.provider}:auth:{identity}",
            auth_bootstrap_receipt=f"{context.provider}:receipt:{identity}",
        )
    return instance

alpha = bootstrap("ep-installed-alpha", "Installed alpha")
engine.create(provisioner.InstanceRequest("install-alpha-0001", alpha.instance_id, alpha.display_label, alpha.service_account, 18765, baseline_release))
bravo = bootstrap("ep-installed-bravo", "Installed bravo")
engine.create(provisioner.InstanceRequest("install-bravo-0001", bravo.instance_id, bravo.display_label, bravo.service_account, 18766, baseline_release))

inventory = engine.inventory()
if inventory["state"] != "OBSERVED" or inventory["instance_count"] != 2:
    raise SystemExit("installed multi-instance inventory failed")
alpha_status, bravo_status = engine.status(alpha.instance_id), engine.status(bravo.instance_id)
if not alpha_status["ready"] or not bravo_status["ready"]:
    raise SystemExit("installed cold-boot readiness failed")

alpha_record = alpha_status["installation"]
bravo_record = bravo_status["installation"]
if alpha_record["interpreter"] != bravo_record["interpreter"]:
    raise SystemExit("identical installed bytes did not share the immutable runtime slot")
if len({alpha.data_root, bravo.data_root, alpha.lifecycle_lock, bravo.lifecycle_lock, alpha.root, bravo.root}) != 6:
    raise SystemExit("instance mutable topology collision")
provider_homes = {
    item["home"] for status in (alpha_status, bravo_status)
    for item in status["providers"].values()
}
if len(provider_homes) != 4:
    raise SystemExit("provider context collision")

# Update only alpha through the installed EP-owned durable update engine.
bravo_before_update = bravo.descriptor.read_bytes()
updated = engine.update(
    instance_id=alpha.instance_id,
    operation_id="update-alpha-0001",
    release=release,
)
if updated["result"] != "COMPLETE":
    raise SystemExit("installed exact-instance update did not complete")
alpha_after, bravo_after = engine.status(alpha.instance_id), engine.status(bravo.instance_id)
if alpha_after["installation"]["version"] != version:
    raise SystemExit("updated instance did not select the exact current release")
if bravo_after["installation"]["version"] != baseline_version:
    raise SystemExit("other instance runtime selection changed during update")
if bravo.descriptor.read_bytes() != bravo_before_update or not bravo_after["ready"]:
    raise SystemExit("cross-instance update leakage")

# Stop/start one service without changing the other, then remove only alpha.
controller.quiesce(alpha)
if not controller.loaded(bravo) or controller.loaded(alpha):
    raise SystemExit("cross-instance quiescence")
controller.start(alpha)
bravo_before = bravo.descriptor.read_bytes()
engine.remove(alpha.instance_id, "remove-alpha-0001", confirm_instance_id=alpha.instance_id)
if not bravo.root.is_dir() or bravo.descriptor.read_bytes() != bravo_before or not engine.status(bravo.instance_id)["ready"]:
    raise SystemExit("cross-instance remove leakage")

print(json.dumps({
    "result":"PASS",
    "instance_ids":["ep-installed-alpha","ep-installed-bravo"],
    "shared_runtime":bravo_record["interpreter"],
    "distinct_provider_homes":len(provider_homes),
    "inventory_count":2,
    "stop_restart_isolation":"PASS",
    "update_isolation":"PASS",
    "baseline_version":baseline_version,
    "updated_version":version,
    "remove_isolation":"PASS",
    "project_agent_scope":"UNTOUCHED_USER_OWNED",
}, sort_keys=True))
'''


def qualify(wheel: Path, version: str, baseline_wheel: Path, baseline_version: str) -> dict[str, object]:
    wheel = wheel.resolve()
    baseline_wheel = baseline_wheel.resolve()
    digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    baseline_digest = "sha256:" + hashlib.sha256(baseline_wheel.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ep-system-multi-instance-") as temporary:
        root = Path(temporary)
        bootstrap = root / "bootstrap"
        created = subprocess.run(
            (sys.executable, "-I", "-m", "venv", str(bootstrap)),
            text=True, capture_output=True, check=False,
        )
        if created.returncode:
            raise RuntimeError("installed-artifact bootstrap venv failed")
        installed = subprocess.run(
            (
                str(bootstrap / "bin" / "python"), "-I", "-m", "pip", "install",
                "--isolated", "--no-deps", "--no-index", str(wheel),
            ),
            text=True, capture_output=True, check=False,
        )
        if installed.returncode:
            raise RuntimeError("installed-artifact wheel installation failed")
        child = subprocess.run(
            (
                str(bootstrap / "bin" / "python"), "-I", "-c", _CHILD,
                str(wheel), str(root / "product"), str(root / "LaunchDaemons"),
                version, digest, "b" * 40,
                str(baseline_wheel), baseline_version, baseline_digest, "a" * 40,
            ),
            cwd=root,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"},
            text=True, capture_output=True, check=False,
        )
        if child.returncode:
            raise RuntimeError("installed multi-instance qualification failed: " + child.stderr.strip())
        result = json.loads(child.stdout)
    return {
        "schema_version": 1,
        "kind": "engineering-platform-system-multi-instance-installed-artifact",
        "wheel": wheel.name,
        "version": version,
        "baseline_version": baseline_version,
        "baseline_artifact_digest": baseline_digest,
        "artifact_digest": digest,
        "source_checkout_authority": False,
        "pythonpath_authority": False,
        "qualification": result,
        "result": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--baseline-wheel", type=Path, required=True)
    parser.add_argument("--baseline-version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = qualify(
        args.wheel,
        args.version,
        args.baseline_wheel,
        args.baseline_version,
    )
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"EP_SYSTEM_MULTI_INSTANCE_INSTALLED_ARTIFACT=PASS version={args.version} instances=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
