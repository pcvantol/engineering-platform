#!/usr/bin/env python3
"""Exercise the installed CENTRAL cutover contract in isolated storage.

This is deliberately a qualification harness, not another migration path.  It
imports the installed package, creates only disposable legacy stores, and uses
a deterministic service-control double so no host LaunchAgent is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile

from engineering_platform import central_store_migration as migration
from engineering_platform import storage
from engineering_platform.storage import open_storage


class Services:
    """Model the bounded launchd postconditions required by cutover."""

    def __init__(self, *, fail_stop: bool = False) -> None:
        self.fail_stop = fail_stop
        self.states = {label: True for label in migration.SERVICE_STOP_ORDER}

    def running(self, label: str) -> bool:
        return self.states.get(label, False)

    def stopped(self, label: str) -> bool:
        return not self.states.get(label, True)

    def stop(self, label: str) -> None:
        if self.fail_stop:
            raise migration.CutoverError("SERVICE_STOP_FAILED")
        self.states[label] = False

    def start(self, label: str) -> None:
        self.states[label] = True


def _assert(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def _source(repo: Path) -> Path:
    with open_storage(repo) as connection:
        connection.execute(
            "INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("canary-client", "project-canary", "ACTIVE", "now", "now"),
        )
        connection.execute(
            "INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
            ("credential-canary", "canary-client", "project-canary", b"v" * 32, b"f" * 32, "now"),
        )
    return repo / ".engineering" / "engineering.db"


def _with_home(root: Path) -> None:
    home = root / "home"
    home.mkdir(parents=True)
    os.environ["HOME"] = str(home)
    os.environ.pop(storage.CENTRAL_OPERATIONAL_DATABASE_ENVIRONMENT, None)


def _happy_path(root: Path, *, post_write: bool) -> dict[str, object]:
    _with_home(root)
    repo = root / "repo"
    repo.mkdir()
    legacy = _source(repo)
    preflight = migration.preflight(repo)
    _assert(preflight["eligible"] is True, "PREFLIGHT_NOT_ELIGIBLE")
    migration_id = "post-write" if post_write else "rollback"
    migration.set_admission_freeze(repo, migration_id=migration_id, reason="installed qualification")
    services = Services()
    receipt = migration.controlled_cutover(repo, services=services)
    _assert(receipt["state"] == "SERVICES_RESTARTED", "CUTOVER_NOT_RESTARTED")
    receipt = migration.complete_stage_a(repo, migration_id=migration_id, services=services, desired_state_check=lambda _repo: True)
    _assert(receipt["state"] == "LEGACY_ROLLBACK_COMPATIBLE", "STAGE_A_NOT_COMPLETE")
    pointer = json.loads(migration.authority_pointer_path().read_text(encoding="utf-8"))
    target = migration.central_store_path()
    _assert(pointer["authoritative_path"] == str(target.resolve()), "CENTRAL_POINTER_MISSING")
    _assert(storage.database_path(repo).resolve() == target.resolve(), "CENTRAL_AUTHORITY_NOT_SINGLE")
    _assert(migration.validate_target_equivalence(legacy, target)["equivalent"] is True, "COPY_NOT_VERIFIED")
    if post_write:
        migration.mark_central_post_write(repo)
        try:
            migration.rollback(repo, migration_id=migration_id)
        except migration.CutoverError as error:
            _assert(error.code == "DIRECT_ROLLBACK_NOT_SAFE", "POST_WRITE_ROLLBACK_WRONG_ERROR")
        else:
            raise RuntimeError("POST_WRITE_ROLLBACK_ACCEPTED")
        return {"postcondition": "PASS", "rollback": "BLOCKED_AFTER_POST_WRITE"}
    rolled_back = migration.rollback(repo, migration_id=migration_id)
    _assert(rolled_back["rollback"]["authority_pointer"]["authoritative_path"] == str(legacy.resolve()), "ROLLBACK_POINTER_WRONG")
    _assert(storage.database_path(repo).resolve() == legacy.resolve(), "ROLLBACK_AUTHORITY_WRONG")
    return {"rollback": "PASS", "authority": "LEGACY_AFTER_ROLLBACK"}


def _negative_cases(root: Path) -> dict[str, str]:
    malformed_home = root / "malformed"
    malformed_home.mkdir()
    _with_home(malformed_home)
    malformed_repo = malformed_home / "repo"
    (malformed_repo / ".engineering").mkdir(parents=True)
    (malformed_repo / ".engineering" / "engineering.db").write_text("not sqlite", encoding="utf-8")
    malformed = migration.preflight(malformed_repo)
    _assert("SOURCE_INTEGRITY_FAILED" in malformed["blocking_codes"], "MALFORMED_SOURCE_ACCEPTED")

    conflict_home = root / "conflict"
    conflict_home.mkdir()
    _with_home(conflict_home)
    conflict_repo = conflict_home / "repo"
    conflict_repo.mkdir()
    _source(conflict_repo)
    target = migration.central_store_path()
    target.parent.mkdir(parents=True)
    target.write_text("not sqlite", encoding="utf-8")
    conflict = migration.preflight(conflict_repo)
    _assert("TARGET_UNREADABLE" in conflict["blocking_codes"], "CONFLICTING_TARGET_ACCEPTED")

    interrupted_home = root / "interrupted"
    interrupted_home.mkdir()
    _with_home(interrupted_home)
    interrupted_repo = interrupted_home / "repo"
    interrupted_repo.mkdir()
    _source(interrupted_repo)
    migration.set_admission_freeze(interrupted_repo, migration_id="interrupted", reason="installed qualification")
    try:
        migration.controlled_cutover(interrupted_repo, services=Services(fail_stop=True))
    except migration.CutoverError as error:
        _assert(error.code == "SERVICE_STOP_FAILED", "INTERRUPTION_WRONG_ERROR")
    else:
        raise RuntimeError("INTERRUPTED_CUTOVER_ACCEPTED")
    _assert(not migration.central_store_path().exists(), "INTERRUPTED_TARGET_CREATED")

    stale_home = root / "stale"
    stale_home.mkdir()
    _with_home(stale_home)
    stale_repo = stale_home / "repo"
    stale_repo.mkdir()
    stale_source = _source(stale_repo)
    migration.set_admission_freeze(stale_repo, migration_id="stale", reason="installed qualification")
    stale_services = Services()
    migration.controlled_cutover(stale_repo, services=stale_services)
    with sqlite3.connect(stale_source) as connection:
        connection.execute(
            "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
            ("stale-source-write", "{}", "COMPLETE", "now"),
        )
    try:
        migration.complete_stage_a(stale_repo, migration_id="stale", services=stale_services, desired_state_check=lambda _repo: True)
    except migration.CutoverError as error:
        _assert(error.code == "POST_CUTOVER_READINESS_FAILED", "STALE_SOURCE_WRONG_ERROR")
    else:
        raise RuntimeError("STALE_SOURCE_ACCEPTED")
    return {"malformed_source": "PASS", "target_conflict": "PASS", "interruption": "PASS", "stale_source": "PASS"}


def main() -> int:
    original_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="ep-central-installed-") as temporary:
            root = Path(temporary)
            result = {
                "installed_central_migration": "PASS",
                "positive": _happy_path(root / "positive", post_write=False),
                "post_write": _happy_path(root / "post-write", post_write=True),
                "negative": _negative_cases(root),
                "central_operational_authority_count": 1,
            }
            print(json.dumps(result, sort_keys=True))
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
