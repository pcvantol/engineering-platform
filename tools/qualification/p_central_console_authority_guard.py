"""Fail closed if an installed Console route regains local-root authority."""
from __future__ import annotations

import argparse
from pathlib import Path


FORBIDDEN = {
    "SUPPORTED_CONSOLE_ROOT_BOUND_ROUTES": "_console_root(",
    "SUPPORTED_CONSOLE_DASHBOARD_DELEGATE_ROUTES": "dashboard.handler(",
    "SUPPORTED_CONSOLE_OPEN_STORAGE_ROOT": "open_storage(",
    "SUPPORTED_CONSOLE_STATESTORE": "StateStore(",
}


def violations(source_root: Path) -> list[str]:
    server = (source_root / "engineering_platform" / "server.py").read_text(encoding="utf-8")
    findings = [name for name, marker in FORBIDDEN.items() if marker in server]
    if 'selected = (parse_qs(request.query).get("project") or [None])[0]' not in server:
        findings.append("AMBIGUOUS_CONSOLE_AUTHORITY")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    findings = violations(args.source_root.resolve())
    if findings:
        print("\n".join(findings))
        return 1
    for name in (*FORBIDDEN, "AMBIGUOUS_CONSOLE_AUTHORITY"):
        print(f"{name}=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
