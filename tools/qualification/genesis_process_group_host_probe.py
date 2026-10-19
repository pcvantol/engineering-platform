#!/usr/bin/env python3
"""One installed owning Genesis chain with private, read-only group facts.

Only the whitelisted public summary and encrypted primary evidence may be
uploaded. The ordinary owning detector and private effect scope are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import traceback
import zipfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "native", "bundle", "validator", "wheel", "output", "recipient"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--assignment", default="L2-GENESIS-MACOS-PROCESS-GROUP-CLOSURE-V1-20261019")
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--root-prefix", required=True)
    parser.add_argument("--case", choices=("concurrency", "before-register", "lost-success"), default="before-register")
    args = parser.parse_args()
    # Case executes installed commands with cwd set to its new synthetic root.
    # Inherit no relative PATH/prefix/fixture interpretation across that boundary.
    for name in ("python", "native", "bundle", "validator", "wheel", "output", "recipient"):
        setattr(args, name, getattr(args, name).absolute())
    if sys.platform != "darwin" or not args.root_prefix.isalnum() or len(args.root_prefix) > 4:
        raise ValueError("Explicit bounded owning Darwin placement required")
    if len(args.source) != 40 or any(c not in "0123456789abcdef" for c in args.source):
        raise ValueError("Exact source pin required")
    if hashlib.sha256(args.wheel.read_bytes()).hexdigest() != args.wheel_sha256:
        raise ValueError("Wrong original wheel")
    args.output.mkdir(mode=0o700)
    public = args.output / "public"
    public.mkdir(mode=0o700)
    private = args.output / "private"
    private.mkdir(mode=0o700)
    args.output = private
    own_tools = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("own_genesis_probe", own_tools / "genesis_local_bootstrap_http.py")
    owning = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owning)
    # Copy the observer into the synthetic fault launcher; its imports still
    # select the ordinary installed package, not a checkout/PYTHONPATH overlay.
    observer_source = (own_tools / "genesis_process_group_observer.py").read_text()
    observer_source = observer_source.replace("from __future__ import annotations\n", "")
    prelude = observer_source + "\nfrom engineering_platform import genesis_service as _observed_service\n"
    prelude += "install_observer(_observed_service, Path(sys.argv[3]) / 'own-group-observations.jsonl')\n"
    owning.FAULT_LAUNCHER = prelude + owning.FAULT_LAUNCHER
    from jsonschema import Draft202012Validator, FormatChecker
    import engineering_platform
    installed = Path(engineering_platform.__file__).resolve().parent
    if installed.is_relative_to(own_tools.parents[1]):
        raise ValueError("Noneditable installation outside checkout required")
    with zipfile.ZipFile(args.wheel) as wheel:
        files = [name for name in wheel.namelist() if name.startswith("engineering_platform/") and not name.endswith("/")]
        for name in files:
            if (installed.parent / name).read_bytes() != wheel.read(name):
                raise ValueError("Installed product differs from original wheel")
        schema = Draft202012Validator(json.loads(wheel.read("engineering_platform/schemas/genesis-local-bootstrap-v1.schema.json")),
            format_checker=FormatChecker())
    case = None
    primary = cleanup = None
    try:
        case = owning.Case(args, args.case, 0, schema)
        # Retain only this finite synthetic owner-issued credential privately.
        # An unexpected real failure must not destroy the possibility of an
        # explicitly authorized original-operation resume after Server restart.
        owning.write_private(case.root / "original-grant.private.json", case.grant)
        case.run()
        case.record["state"] = "PASS"
    except BaseException as error:
        primary = error
        if case:
            case.record.update(state="FAILED", failure=traceback.format_exc())
    finally:
        if case:
            try:
                case.finish()
            except BaseException as error:
                cleanup = error
    owning.write_private(private / "probe-errors.json", {
        "primary": None if primary is None else {
            "type": type(primary).__name__,
            "traceback": "".join(traceback.format_exception(primary)),
        },
        "cleanup": None if cleanup is None else {
            "type": type(cleanup).__name__,
            "traceback": "".join(traceback.format_exception(cleanup)),
        },
    })
    observations = []
    if case and (case.data / "own-group-observations.jsonl").is_file():
        observations = [json.loads(line) for line in (case.data / "own-group-observations.jsonl").read_text().splitlines()]
    # This file has no tokens, paths, command lines, argv or environment data.
    summary = {"source": args.source, "wheel_sha256": args.wheel_sha256,
        "installed_payload_files": len(files), "system": platform.system(), "release": platform.release(),
        "macos": platform.mac_ver()[0], "machine": platform.machine(), "case": args.case,
        "result": "PASS" if case and case.record.get("state") == "PASS" and not primary and not cleanup else "FAIL",
        "primary_type": None if primary is None else type(primary).__name__,
        "cleanup_type": None if cleanup is None else type(cleanup).__name__,
        "group_observations": observations,
        "limitation": "Instrumented owning chain; not full Forge R57 or final-main qualification"}
    if not observations:
        summary["result"] = "FAIL"
        summary["diagnostic_error"] = "NO_GROUP_OBSERVATIONS"
    # Preserve private original receipts, stdout/stderr, database, Git and
    # exact observation records after the attempted actual process stop.
    archive = private / "primary.tar.gz"
    with tarfile.open(archive, "w:gz") as saved:
        saved.add(private / "probe-errors.json", arcname="probe-errors.json")
        if case:
            saved.add(case.root, arcname="own-synthetic-root", recursive=True)
            saved.add(case.evidence, arcname="own-original-qualification", recursive=True)
    encrypted = public / "primary-evidence.cms"
    subprocess.run(("openssl", "cms", "-encrypt", "-binary", "-aes-256-cbc", "-in", str(archive),
        "-outform", "DER", "-out", str(encrypted), str(args.recipient)), check=True, capture_output=True)
    summary["private_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    summary["encrypted_archive_sha256"] = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    (public / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"result": summary["result"], "case": args.case,
        "observed_checks": len(observations), "failed_checks": sum(row["detector"] == "FAIL" for row in observations)}))
    # No failure suppression, including original chain/cleanup failures.
    return 0 if summary["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
