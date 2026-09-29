#!/usr/bin/env python3
"""Bounded EP 2.3.105 foreground process/API qualification under a real macOS test UID."""
import hashlib
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ACCOUNT = ""
HOME = Path("/")
WHEEL = Path("/")
WHEEL_SHA = "22dd1e49c263b55dc9eee396810a09fc43509984fe685f3c00d26289d55e8adc"
SIBLING_FIXTURE = Path("/")
VENV = Path("/")
PYTHON = Path("/")
EXPECTED_UID = -1
EXPECTED_ENV = {}

def configure():
    global ACCOUNT, HOME, WHEEL, SIBLING_FIXTURE, VENV, PYTHON, EXPECTED_UID, EXPECTED_ENV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--expected-uid", type=int, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--synthetic-sibling-fixture", type=Path, required=True)
    args = parser.parse_args()
    ACCOUNT, EXPECTED_UID = args.account, args.expected_uid
    HOME, WHEEL, VENV = args.home, args.wheel, args.venv
    SIBLING_FIXTURE = args.synthetic_sibling_fixture
    require(EXPECTED_UID > 0 and all(
        path.is_absolute() for path in (HOME, WHEEL, VENV, SIBLING_FIXTURE)
    ), "Explicit absolute qualification inputs required")
    PYTHON = VENV / "bin/python"
    EXPECTED_ENV = {
        "HOME": str(HOME), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C", "LC_ALL": "C", "TMPDIR": str(HOME),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def command(args, *, timeout=30):
    return subprocess.run(
        args, cwd=HOME, env=EXPECTED_ENV, text=True, capture_output=True,
        timeout=timeout, check=False,
    )

def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def api(port_number, path, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port_number}{path}", headers=headers or {},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

def launch(data_root, instance_id, log_path):
    log = open(log_path, "ab", buffering=0)
    child = subprocess.Popen(
        [str(PYTHON), "-I", "-m", "engineering_platform.server", "serve",
         "--data-root", str(data_root), "--expected-instance-id", instance_id],
        cwd=HOME, env=EXPECTED_ENV, stdin=subprocess.DEVNULL, stdout=log,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    log.close()
    return child

def stop(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=12)
    except subprocess.TimeoutExpired:
        child.kill()  # exact supervised child only
        child.wait(timeout=5)

def wait_api(child, port_number, expected_id):
    deadline = time.monotonic() + 20
    last = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Server exited early: {child.returncode}")
        try:
            status, body = api(port_number, "/health")
            if body.get("instance_id") == expected_id:
                return status, body
            last = f"foreign instance {body.get('instance_id')}"
        except (OSError, ValueError) as error:
            last = type(error).__name__
        time.sleep(0.2)
    raise RuntimeError(f"Server health timeout: {last}")

def main():
    result = {
        "assignment": "L2-EP-NONROOT-RUNTIME-CONFORMANCE-V1-20260929",
        "boundary": "REAL_MACOS_NONROOT_FOREGROUND_SERVER",
        "cases": {},
    }
    uid = os.geteuid()
    gid = os.getegid()
    pw = pwd.getpwnam(ACCOUNT)
    require(uid == pw.pw_uid and uid == EXPECTED_UID and gid == pw.pw_gid, "Wrong process UID/GID")
    require(os.getuid() == uid and uid != 0, "Unexpected real UID")
    require(HOME.resolve() == Path(pw.pw_dir).resolve()
            and HOME.stat().st_uid == uid
            and HOME.stat().st_mode & 0o077 == 0,
            "Test home is not private and owned by the exact process identity")
    unexpected = set(os.environ) - set(EXPECTED_ENV)
    changed = {key for key, value in EXPECTED_ENV.items() if os.environ.get(key) != value}
    require(
        unexpected <= {"__CF_USER_TEXT_ENCODING"} and not changed,
        f"Ambient environment was not scrubbed: unexpected_keys={sorted(unexpected)}, "
        f"missing_or_changed_keys={sorted(changed)}",
    )
    require(sys.executable == str(PYTHON), "Unexpected Python executable")
    require(sys.version_info[:2] == (3, 14), "Wrong interpreter version")
    require(importlib.metadata.version("engineering-platform") == "2.3.105", "Wrong distribution version")
    import engineering_platform
    package_file = Path(engineering_platform.__file__).resolve()
    require(package_file.is_relative_to(VENV), "Import escaped installed venv")
    require(hashlib.sha256(WHEEL.read_bytes()).hexdigest() == WHEEL_SHA, "Published wheel digest mismatch")
    result["uid"] = uid
    result["gid"] = gid
    result["groups"] = os.getgroups()
    result["executable"] = sys.executable
    result["python_version"] = sys.version.split()[0]
    result["installed_package"] = str(package_file)
    result["wheel_sha256"] = WHEEL_SHA
    result["cases"]["native_executable_and_wheel"] = "PASS"

    work = Path(tempfile.mkdtemp(prefix="ep-r27-native-", dir=HOME))
    result["test_root"] = str(work)
    child = None
    child_b = None
    try:
        data_a, data_b = work / "instance-a", work / "instance-b"
        pa, pb = port(), port()
        require(pa != pb, "Port collision")
        init_a = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                          "init", "--data-root", str(data_a), "--bind-port", str(pa)])
        require(init_a.returncode == 0, f"A init failed: {init_a.stderr[-400:]}")
        identity_a = json.loads(init_a.stdout)["instance_id"]
        init_b = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                          "init", "--data-root", str(data_b), "--bind-port", str(pb)])
        require(init_b.returncode == 0, f"B init failed: {init_b.stderr[-400:]}")
        identity_b = json.loads(init_b.stdout)["instance_id"]
        require(identity_a != identity_b, "Instance ID collision")
        for data_root in (data_a, data_b):
            configuration = json.loads((data_root / "server.json").read_text())
            managed_prefix = Path(configuration["managed_codex_cli_prefix"])
            require(managed_prefix.is_relative_to(HOME),
                    "Product configuration selected a different user's HOME")
        result["cases"]["disposable_product_init_ab"] = "PASS"
        result["cases"]["interactive_user_home_independence"] = "PASS"

        wrong = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                         "serve", "--data-root", str(data_a),
                         "--expected-instance-id", identity_b], timeout=12)
        require(wrong.returncode != 0 and "EP_SERVER_INSTANCE_ID_MISMATCH" in wrong.stdout,
                f"Wrong expected instance did not fail closed: {wrong.stdout[-400:]} {wrong.stderr[-200:]}")
        result["cases"]["wrong_expected_instance_rejected"] = "PASS"

        child = launch(data_a, identity_a, work / "server-a.log")
        http_status, health = wait_api(child, pa, identity_a)
        require(health.get("service") == "engineering-platform-server", "Wrong service")
        require(health.get("product_version") == "2.3.105", "Wrong API product release")
        require(health.get("schema_version") == 72, "Wrong live Server store schema")
        require(health.get("running") is True, "Health is not live")
        require(
            health.get("runtime_identity", {}).get("artifact", {}).get("digest")
            == "sha256:" + WHEEL_SHA,
            "Live response did not observe the pinned wheel",
        )
        result["server_pid"] = child.pid
        process = command(["/bin/ps", "-ww", "-p", str(child.pid), "-o", "uid=", "-o", "gid=", "-o", "command="])
        require(process.returncode == 0, "Live server process inventory unavailable")
        process_line = process.stdout.strip()
        require(process_line.split()[:2] == [str(uid), str(gid)]
                and "/Python.app/Contents/MacOS/Python " in process_line
                and "engineering_platform.server" in process_line
                and " serve " in process_line,
                f"Live server process is not the expected non-root executable: {process_line}")
        result["live_process_readback"] = process_line
        result["api_http_status"] = http_status
        result["api_instance_id"] = identity_a
        result["api_product_version"] = health.get("product_version")
        result["api_runtime_identity"] = health.get("runtime_identity")
        result["cases"]["real_server_api_identity"] = "PASS"

        child_b = launch(data_b, identity_b, work / "server-b.log")
        _, health_b = wait_api(child_b, pb, identity_b)
        require(health_b.get("product_version") == "2.3.105"
                and health_b.get("instance_id") != health.get("instance_id"),
                "A/B live identity collision")
        result["cases"]["live_ab_logical_isolation"] = "PASS"
        stop(child_b)
        child_b = None

        require(SIBLING_FIXTURE.stat().st_uid != uid
                and SIBLING_FIXTURE.stat().st_mode & 0o777 == 0o600,
                "Synthetic sibling fixture ownership invalid")
        try:
            SIBLING_FIXTURE.read_text()
        except PermissionError:
            pass
        else:
            raise RuntimeError("Test UID could read synthetic other-UID private fixture")
        result["cases"]["synthetic_cross_uid_denial"] = "PASS"

        compatibility_status, compatibility = api(pa, "/v1/producer-compatibility")
        require(compatibility_status == 200
                and compatibility.get("instance", {}).get("id") == identity_a
                and compatibility.get("producer", {}).get("version") == "2.3.105",
                "Public compatibility declaration unavailable")
        scoped_status, scoped = api(
            pa, "/v1/producer-compatibility",
            {"EP-Project-ID": "ep-r27-disposable-project",
             "EP-Repository-ID": "ep-r27-disposable-repository"},
        )
        require(scoped_status in (401, 403), "Missing scoped bearer was accepted")
        result["cases"]["api_auth_required_for_scoped_readback"] = "PASS"

        project = "ep-r27-disposable-project"
        repository = "ep-r27-disposable-repository"
        consumer = "ep-r27-disposable-consumer"
        repo_root = work / "synthetic-repository"
        declaration = repo_root / ".engineering-platform/repository.json"
        declaration.parent.mkdir(parents=True)
        declaration.write_text(json.dumps({
            "schema_version": "1.0",
            "project": {"id": project, "authority_repository_id": repository},
            "repository": {"id": repository, "role": "authority"},
            "validation": {"kind": "none"},
        }))
        for arguments in (
            ["bootstrap-topology", "--project-id", project, "--repository-id", repository],
            ["bind-repository", "--project-id", project, "--repository-id", repository,
             "--path", str(repo_root)],
        ):
            outcome = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                               *arguments, "--data-root", str(data_a)])
            require(outcome.returncode == 0, f"Disposable API topology failed: {outcome.stderr[-300:]} {outcome.stdout[-300:]}")
        issued = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                          "issue-consumer-credential", "--project-id", project,
                          "--consumer-id", consumer, "--data-root", str(data_a)])
        require(issued.returncode == 0, "Disposable consumer credential issuance failed")
        # The synthetic credential remains in process memory and is never printed.
        token = json.loads(issued.stdout)["credential"]
        scoped_headers = {
            "EP-Project-ID": project,
            "EP-Repository-ID": repository,
            "Authorization": "Bearer " + token,
        }
        authenticated_status, authenticated = api(pa, "/v1/producer-compatibility", scoped_headers)
        require(authenticated_status == 200
                and authenticated.get("instance", {}).get("id") == identity_a
                and authenticated.get("authentication", {}).get("consumer_id") == consumer
                and authenticated.get("authentication", {}).get("local_repository_binding") == "BOUND",
                f"Authenticated scoped API readback failed: {authenticated_status} {authenticated}")
        result["cases"]["authenticated_scoped_api_identity"] = "PASS"
        wrong_scope_status, _ = api(pa, "/v1/producer-compatibility", {
            **scoped_headers, "EP-Project-ID": "ep-r27-wrong-project",
        })
        require(wrong_scope_status == 403, "Wrong project scope was accepted")
        result["cases"]["wrong_scoped_api_identity_rejected"] = "PASS"

        provider_status, provider = api(pa, "/api/provider-login-status")
        require(provider_status == 200, "Provider status endpoint unavailable")
        result["provider_readback"] = provider
        require("READY" not in json.dumps(provider), "Provider unexpectedly READY without evidence")
        result["cases"]["missing_provider_not_ready"] = "PASS"

        original_wheel = hashlib.sha256(WHEEL.read_bytes()).hexdigest()
        try:
            with open(package_file, "ab") as writable:
                writable.write(b"EP-R27-SHOULD-NOT-WRITE")
        except PermissionError:
            pass
        else:
            raise RuntimeError("Immutable installed package unexpectedly writable by test UID")
        require(hashlib.sha256(WHEEL.read_bytes()).hexdigest() == original_wheel,
                "Published wheel changed during server test")
        result["cases"]["installed_runtime_immutable_to_test_uid"] = "PASS"

        blocked_parent = work / "blocked-parent"
        blocked_parent.mkdir(mode=0o500)
        try:
            blocked_init = command([str(PYTHON), "-I", "-m", "engineering_platform.server",
                                    "init", "--data-root", str(blocked_parent / "forbidden")])
            require(blocked_init.returncode != 0
                    and not (blocked_parent / "forbidden").exists(),
                    "Unwritable data parent was silently repaired or accepted")
        finally:
            blocked_parent.chmod(0o700)
        result["cases"]["nonwritable_data_parent_rejected"] = "PASS"

        stop(child)
        child = None
        child = launch(data_a, identity_a, work / "server-a-restart.log")
        _, restarted = wait_api(child, pa, identity_a)
        require(restarted.get("product_version") == "2.3.105", "Restart changed release")
        again_status, again_provider = api(pa, "/api/provider-login-status")
        require(again_status == 200 and "READY" not in json.dumps(again_provider),
                "Restart promoted provider state")
        result["cases"]["real_foreground_restart_identity_and_provider"] = "PASS"
        persisted_status, persisted = api(pa, "/v1/producer-compatibility", scoped_headers)
        require(persisted_status == 200
                and persisted.get("authentication", {}).get("consumer_id") == consumer,
                "Disposable authenticated API identity was not preserved on restart")
        result["cases"]["restart_preserves_disposable_api_identity"] = "PASS"

        result["status"] = "PASS_WITH_DECLARED_LIMITS"
        result["limits"] = [
            "No LaunchDaemon/LaunchAgent or physical reboot",
            "No live provider authentication",
            "Cross-UID denial uses a synthetic sibling fixture, not another EP server",
            "No EP preserve/restore in this smoke slice",
        ]
        return result
    finally:
        stop(child_b)
        stop(child)
        result["own_server_stopped"] = child is None or child.poll() is not None

if __name__ == "__main__":
    try:
        configure()
        print(json.dumps(main(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        raise
