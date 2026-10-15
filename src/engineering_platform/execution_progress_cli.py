"""Installation-owner progress grants and explicit local read parity.

Inter-product consumers use HTTP, not this CLI. Credentials enter read parity
only on stdin. Reads never initialize, migrate or repair CENTRAL.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys

from . import execution_progress as progress, execution_progress_authority as authority, storage
from .platform_admin import require_installation_owner


def _config(path):
    if path is None:
        return {}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
            raise authority.ProgressError("PROGRESS_PRIVATE_CONFIG_REQUIRED")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise authority.ProgressError("PROGRESS_CONFIG_LIMIT")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise authority.ProgressError("PROGRESS_CONFIG_INVALID")
        return value
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="engineering-platform-progress")
    parser.add_argument("command", choices=("issue", "status", "revoke", "read"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--grant-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--credential-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = _config(args.config)
        if args.command == "read":
            if not args.credential_stdin or config["headers"].get("EP-Instance-ID") != args.expected_instance_id:
                raise authority.ProgressError("PROGRESS_READ_CONTEXT_REQUIRED")
            result = []
            progress.read(args.data_root, token=sys.stdin.read(129).strip(), headers=config["headers"],
                          query=config["query"], project_id=config["project_id"], run_id=config.get("run_id"),
                          publish=lambda body, instance, check: (check(), result.append(body)))
            result = result[0]
        else:
            require_installation_owner(args.data_root)
            database = args.data_root / "epdata.sqlite"
            context = storage.sqlite_read_connection(database) if args.command == "status" else storage.sqlite_connection(database.resolve().as_uri() + "?mode=rw", uri=True)
            with context as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN" if args.command == "status" else "BEGIN IMMEDIATE")
                authority.require_current_instance(connection, args.expected_instance_id)
                if args.command == "issue":
                    result = authority.issue(connection, instance_id=args.expected_instance_id, **config)
                elif args.command == "revoke":
                    result = authority.revoke(connection, grant_id=args.grant_id, operation_id=args.operation_id)
                else:
                    result = authority.status(connection, grant_id=args.grant_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (authority.ProgressError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, storage.EngineeringStorageError) as error:
        print(json.dumps({"error": error.code if isinstance(error, authority.ProgressError) else "PROGRESS_COMMAND_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
