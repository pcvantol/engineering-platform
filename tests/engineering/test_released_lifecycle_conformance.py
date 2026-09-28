from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

VERSION = "2.3.103"
WHEEL_SHA256 = "0199a7aab3b25260b6cd4ad53f0aecc7e59c9403ef9a3bd4639993ab9e56910c"

CHILD = r"""
from pathlib import Path
import json, os, plistlib, sys, tempfile
from types import SimpleNamespace
from engineering_platform import system_instance_provisioner as provisioner
from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context as providers
from engineering_platform import system_server_service as services

wheel, root_s, case = sys.argv[1:]
root = Path(root_s)
product_root = root / "product"
launch_root = root / "LaunchDaemons"
launch_root.mkdir(parents=True, exist_ok=True)

class Controller:
    def __init__(self): self.loaded_ids=set()
    def register(self, instance, interpreter):
        paths=services.instance_default_paths(instance, launch_root)
        definition=services.instance_service_definition(instance, interpreter=interpreter)
        paths.plist_path.write_bytes(plistlib.dumps(services.instance_plist_payload(paths, definition)))
        return {"result":"REGISTERED","label":instance.service_label}
    def quiesce(self, instance):
        self.loaded_ids.discard(instance.instance_id)
        return {"result":"QUIESCED"}
    def start(self, instance):
        self.loaded_ids.add(instance.instance_id)
        return {"result":"RUNNING"}
    def loaded(self, instance): return instance.instance_id in self.loaded_ids
    def remove(self, instance):
        self.quiesce(instance)
        services.instance_default_paths(instance, launch_root).plist_path.unlink(missing_ok=True)
        return {"result":"REMOVED"}

class FixtureProvisioner(provisioner.SystemInstanceProvisioner):
    def _install_slot(self, release):
        slot=topology.runtime_slot(self.product, release.identity())
        slot.interpreter.parent.mkdir(parents=True, exist_ok=True)
        slot.interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        slot.interpreter.chmod(0o755)
        (slot.venv / "pyvenv.cfg").write_text("home=/usr/bin\n", encoding="utf-8")
        (slot.root / "runtime-slot.json").write_text(json.dumps({
            "schema_version":1, **release.identity().payload(), "venv":str(slot.venv)
        }), encoding="utf-8")
        return slot
    def _initialize_data(self, instance, interpreter, port):
        instance.data_root.mkdir(parents=True, exist_ok=True)
        (instance.data_root / "runtime-identity.json").write_text(json.dumps({
            "instance_id":instance.instance_id,"created_at":"2026-09-28T00:00:00Z"
        }), encoding="utf-8")
        (instance.data_root / "server.json").write_text(json.dumps({
            "version":3,"bind_host":"127.0.0.1","bind_port":port,
            "managed_codex_cli_prefix":str(providers.provider_context(instance,"codex").installation_root),
            "product_version":"2.3.103",
        }), encoding="utf-8")

controller=Controller()
engine=FixtureProvisioner(
    product_root, controller=controller, launch_daemons_dir=launch_root,
    health_verifier=lambda instance,_interpreter: {
        "result":"PASS","instance_id":instance.instance_id,"identity_aware":True
    },
    account_lookup=lambda name: SimpleNamespace(pw_uid=501),
)
wheel=Path(wheel)
digest="sha256:"+__import__("hashlib").sha256(wheel.read_bytes()).hexdigest()
release=provisioner.ReleaseRequest("2.3.103", wheel, digest, "9b1b9d49d7c8f6ceb7cae914078f56b475e8f4a2")
identity="ep-audit-0001"
instance=topology.system_instance_topology(
    engine.product, instance_id=identity, display_label="EP audit", service_account="_ep_audit"
)
for context in providers.provider_contexts(instance):
    context.executable.parent.mkdir(parents=True, exist_ok=True)
    context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    context.executable.chmod(0o755)
    engine.provider_register(
        instance_id=identity, display_label="EP audit", service_account="_ep_audit",
        provider=context.provider, executable_sha256=providers.executable_digest(context.executable),
        version="audit-1", auth_reference=f"{context.provider}:auth:{identity}",
        auth_bootstrap_receipt=f"{context.provider}:receipt:{identity}",
    )
engine.create(provisioner.InstanceRequest("install-audit-0001", identity, "EP audit", "_ep_audit", 18765, release))

if case == "hardlink":
    external=root/"foreign-owned"
    external.write_text("foreign", encoding="utf-8")
    os.link(external, instance.data_root/"foreign-hardlink")
elif case == "permissive-root":
    instance.root.chmod(0o777)
else:
    raise SystemExit("unknown case")

try:
    result=engine.preserve(identity, "preserve-audit-0001", confirm_instance_id=identity)
except Exception as error:
    print(json.dumps({"result":"REJECTED","type":type(error).__name__,"message":str(error)}, sort_keys=True))
    raise SystemExit(0)
print(json.dumps({"result":"ACCEPTED","receipt":result["receipt"]}, sort_keys=True))
raise SystemExit(9)
"""

class ReleasedLifecycleSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sys.version_info < (3, 14):
            raise unittest.SkipTest("released EP conformance requires Python >=3.14")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.wheel_dir = cls.root / "wheel"
        cls.wheel_dir.mkdir()
        subprocess.run([
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--no-cache-dir", "--no-deps", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple", "--dest", str(cls.wheel_dir),
            f"engineering-platform=={VERSION}",
        ], check=True)
        wheels=list(cls.wheel_dir.glob("engineering_platform-2.3.103-*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"expected one released wheel, got {wheels}")
        cls.wheel=wheels[0]
        digest=hashlib.sha256(cls.wheel.read_bytes()).hexdigest()
        if digest != WHEEL_SHA256:
            raise AssertionError(f"released wheel digest mismatch: {digest}")
        cls.venv=cls.root/"venv"
        subprocess.run([sys.executable, "-m", "venv", str(cls.venv)], check=True)
        cls.python=cls.venv/"bin"/"python"
        subprocess.run([
            str(cls.python), "-I", "-m", "pip", "install", "--isolated", "--no-deps",
            "--no-index", str(cls.wheel),
        ], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _negative(self, case: str) -> None:
        case_root=self.root/case
        case_root.mkdir()
        result=subprocess.run(
            [str(self.python), "-I", "-c", CHILD, str(self.wheel), str(case_root), case],
            cwd=self.root, text=True, capture_output=True,
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"GAP: released EP {VERSION} accepted unsafe {case}: stdout={result.stdout} stderr={result.stderr}",
        )
        self.assertIn('"result": "REJECTED"', result.stdout)

    def test_released_wheel_rejects_foreign_hardlink(self) -> None:
        self._negative("hardlink")

    def test_released_wheel_rejects_world_writable_instance_root(self) -> None:
        self._negative("permissive-root")

if __name__ == "__main__":
    unittest.main()
