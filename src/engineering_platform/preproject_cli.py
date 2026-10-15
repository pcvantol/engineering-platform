"""Trusted local owner setup and local-only parity for INSPECT_READ.

Inter-product callers use authenticated HTTP. Credentials enter parity reads
via stdin, never argv. No command initializes CENTRAL or an inspected target.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import preproject_service as service
from .preproject_inspection import InspectionError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-platform-inspection")
    parser.add_argument("command", choices=("init", "bind-source", "issue", "status", "revoke", "run", "read"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--config", type=Path, help="Private owning setup JSON; never HTTP-controlled")
    parser.add_argument("--grant-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--credential-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.data_root.absolute()
        if service._instance(root) != args.expected_instance_id:
            raise InspectionError("INSPECTION_INSTANCE_MISMATCH", 403)
        config = {} if args.config is None else json.loads(args.config.read_text())
        if args.command == "init":
            result = service.initialize(root, expected_instance=args.expected_instance_id)
        elif args.command == "bind-source":
            result = service.bind_source(root, source_ref=config["source_ref"], host_root=Path(config["host_root"]),
                workspace_root=Path(config["workspace_root"]), child=config["child"])
        elif args.command == "issue":
            result = service.issue(root, **config)
        elif args.command in {"status", "revoke"}:
            result = service.grant_status(root, args.grant_id, revoke=args.command == "revoke")
        else:
            if not args.credential_stdin:
                raise InspectionError("INSPECTION_CREDENTIAL_STDIN_REQUIRED")
            token = sys.stdin.read(129).strip()
            result = service.operation(root, token=token, headers=config["headers"], operation_id=args.operation_id,
                request=None if args.command == "read" else {"contract_version": "1.0",
                    "operation_id": args.operation_id, "source_ref": config["source_ref"]})
        print(service.canonical(result))
        return 0
    except (InspectionError, OSError, ValueError, KeyError, TypeError) as error:
        print(service.canonical({"error": error.code if isinstance(error, InspectionError) else "INSPECTION_OWNER_COMMAND_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
