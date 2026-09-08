#!/usr/bin/env python3
"""The sole supported fresh EP wheel build/install entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from advance_platform_build import _current_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an already-versioned EP wheel without source mutation")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel-directory", type=Path)
    parser.add_argument("--install-python", type=Path)
    parser.add_argument("--version", help="require this already-committed stable X.Y.Z release")
    parser.add_argument("--sdist", action="store_true", help="also create the matching source distribution")
    args = parser.parse_args(argv)
    root = args.source_root.resolve()
    version = _current_version(root)
    if args.version is not None and args.version != version:
        raise RuntimeError(f"requested build version {args.version} does not match committed source {version}; prepare it first")
    wheel_directory = (args.wheel_directory or root / "dist").resolve()
    wheel_directory.mkdir(parents=True, exist_ok=True)
    if args.sdist:
        # Do not import an incidental ``build/`` directory from the source
        # checkout in preference to the PyPA build module.
        subprocess.run((sys.executable, "-m", "build", "--outdir", str(wheel_directory), str(root)), check=True, cwd=root.parent)
    else:
        subprocess.run((sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_directory), str(root)), check=True)
    wheel = wheel_directory / f"engineering_platform-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise RuntimeError("expected canonical Engineering Platform wheel was not produced")
    if args.sdist and not (wheel_directory / f"engineering_platform-{version}.tar.gz").is_file():
        raise RuntimeError("expected canonical Engineering Platform source distribution was not produced")
    if args.install_python is not None:
        subprocess.run((str(args.install_python), "-m", "pip", "install", "--no-deps", "--upgrade", "--force-reinstall", str(wheel)), check=True)
    print(f"EP_WHEEL_BUILD=PASS version={version} wheel={wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
