"""Owning local parity; normal peer product calls use authenticated HTTP."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

from . import genesis_authority as authority, genesis_service as service
from .genesis_contract import BootstrapError, load, MAX_BODY
from .platform_admin import require_installation_owner


def config(path: Path | None) -> dict:
    if path is None:
        return {}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise BootstrapError("BOOTSTRAP_PRIVATE_CONFIG_REQUIRED")
        value = load(os.read(fd, MAX_BODY+1))
        if not isinstance(value, dict):
            raise BootstrapError("BOOTSTRAP_PRIVATE_CONFIG_INVALID")
        return value
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-platform-genesis")
    parser.add_argument("command", choices=("init", "issue", "status", "revoke", "apply", "resume", "read"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--grant-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--credential-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = config(args.config)
        if args.command in {"apply", "resume", "read"}:
            if not args.credential_stdin or value["headers"].get("EP-Instance-ID") != args.expected_instance_id:
                raise BootstrapError("BOOTSTRAP_EXPLICIT_CREDENTIAL_CONTEXT_REQUIRED")
            results = []
            service.operation(args.data_root, token=sys.stdin.read(129).strip(), headers=value["headers"],
                operation_id=value["request"]["operation_id"] if args.command == "apply" else args.operation_id,
                body=value["request"] if args.command == "apply" else None, resume=args.command == "resume",
                publish=lambda result, instance, check: (check(), results.append(result)))
            result = results[0]
        else:
            require_installation_owner(args.data_root)
            if args.command == "init":
                result = service.initialize(args.data_root, expected_instance=args.expected_instance_id)
            elif args.command == "issue":
                result = authority.issue(args.data_root, expected_instance=args.expected_instance_id, config=value)
            else:
                with authority.store(args.data_root, write=args.command == "revoke") as db:
                    if db.execute("SELECT instance_id FROM ep_installations").fetchone()[0] != args.expected_instance_id:
                        raise BootstrapError("BOOTSTRAP_INSTANCE_MISMATCH", 403)
                    result = authority.revoke(db, grant_id=args.grant_id, operation_id=args.operation_id) if args.command == "revoke" else authority.status(db, args.grant_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BootstrapError as error:
        print(json.dumps({"error": error.code, "http_status": error.status}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print(json.dumps({"error": "BOOTSTRAP_COMMAND_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
