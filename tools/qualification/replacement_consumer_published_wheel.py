#!/usr/bin/env python3
"""Secret-free conformance of the published EP replacement-consumer route.

Run this script with an interpreter that has the exact published wheel
installed and no PYTHONPATH/source checkout authority. It creates only a
temporary EP data root, captures credential disclosures in memory, and emits
only non-secret assertions.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if sha256(wheel.read_bytes()).hexdigest() != args.expected_sha256:
        raise RuntimeError("published wheel digest mismatch")

    from engineering_platform.ep_consumer_credentials import CredentialAuthority
    import engineering_platform

    installed = Path(engineering_platform.__file__).resolve()
    if installed == wheel or "site-packages" not in installed.parts:
        raise RuntimeError("qualification did not import an installed package")

    def command(root: Path, action: str, *, consumer: str | None = None,
                project: str | None = None, credential: str | None = None,
                expected_exit: int = 0):
        argv = [sys.executable, "-m", "engineering_platform.ep_consumer_credentials",
                action, "--repo", str(root)]
        if consumer is not None:
            argv.extend(("--consumer-id", consumer))
        if project is not None:
            argv.extend(("--project-id", project))
        if credential is not None:
            argv.extend(("--credential-id", credential))
        completed = subprocess.run(argv, capture_output=True, text=True, check=False,
                                   cwd=root, env={"PATH": "/usr/bin:/bin"})
        if completed.returncode != expected_exit:
            raise RuntimeError(f"{action} returned an unexpected status")
        return json.loads(completed.stdout) if expected_exit == 0 else None

    with TemporaryDirectory(prefix="ep-replacement-consumer-") as temporary:
        root = Path(temporary)
        project = "qualification-project"
        old, replacement = "qualification-consumer-old", "qualification-consumer-new"
        assert command(root, "consumer-register", consumer=old, project=project)["status"] == "ACTIVE"
        issued = command(root, "credential-issue", consumer=old, project=project)
        old_secret = issued.pop("credential")
        authority = CredentialAuthority(root)
        old_scope = authority.authenticate(old_secret)
        assert old_scope is not None and authority.authorized(old_scope)

        assert command(root, "consumer-revoke", consumer=old, project=project) is True
        old_scope = authority.authenticate(old_secret)
        assert old_scope is None or not authority.authorized(old_scope)
        command(root, "consumer-register", consumer=old, project=project, expected_exit=2)
        assert command(root, "consumer-status", consumer=old, project=project)["status"] == "REVOKED"

        first = command(root, "consumer-register", consumer=replacement, project=project)
        assert first["status"] == "ACTIVE" and not first["idempotent"]
        assert command(root, "consumer-register", consumer=replacement, project=project)["idempotent"]
        uncertain = command(root, "credential-issue", consumer=replacement, project=project)
        uncertain.pop("credential")  # Simulated lost disclosure; never log it.
        status = command(root, "credential-status", consumer=replacement, project=project)
        assert len(status) == 1 and status[0]["credential_id"] == uncertain["credential_id"]
        assert command(root, "credential-revoke", credential=uncertain["credential_id"])["revoked"]
        fresh = command(root, "credential-issue", consumer=replacement, project=project)
        fresh_secret = fresh.pop("credential")
        fresh_scope = authority.authenticate(fresh_secret)
        assert fresh_scope is not None and fresh_scope.consumer_id == replacement
        assert fresh_scope.project_id == project and authority.authorized(fresh_scope)
        assert command(root, "consumer-status", consumer=replacement, project=project)["active_production_credentials"] == 1
        assert command(root, "consumer-status", consumer=old, project=project)["status"] == "REVOKED"
        print(json.dumps({
            "published_wheel_digest": "sha256:" + args.expected_sha256,
            "same_project_replacement_consumer": "PASS",
            "old_consumer_authority_denied": "PASS",
            "lost_issuance_response_reconciled": "PASS",
            "untracked_active_replacement_grants": 0,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
