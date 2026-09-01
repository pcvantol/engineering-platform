"""Installed HTTP consumer CLI for canonical submissions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-platform")
    parser.add_argument("command", choices=("submit",))
    parser.add_argument("--server", required=True, help="CENTRAL base URL")
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--producer-id", default="cli")
    parser.add_argument("--producer-type", default="HUMAN")
    parser.add_argument("--producer-version")
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--credential-env", default="EP_CONSUMER_TOKEN")
    args = parser.parse_args(argv)
    token = os.environ.get(args.credential_env)
    if not token:
        parser.error(f"{args.credential_env} is required")
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    except OSError as error:
        parser.error(str(error))
    payload: dict[str, object] = {"repository_id": args.repository, "producer": {"id": args.producer_id, "type": args.producer_type}, "prompt": prompt}
    if args.producer_version:
        payload["producer"] = {**payload["producer"], "version": args.producer_version}  # type: ignore[arg-type]
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key
    request = Request(args.server.rstrip("/") + f"/v1/projects/{args.project}/submissions", data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=15) as response:  # nosec B310 -- operator supplied loopback CENTRAL URL
            print(response.read().decode("utf-8"))
    except HTTPError as error:
        print(error.read().decode("utf-8"))
        return 1
    except URLError as error:
        print(json.dumps({"error": "CENTRAL_UNAVAILABLE"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
