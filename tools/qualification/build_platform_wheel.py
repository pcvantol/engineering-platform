#!/usr/bin/env python3
"""The sole supported fresh EP wheel build/install entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from advance_platform_build import advance, set_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advance, build, and optionally install an EP wheel")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel-directory", type=Path)
    parser.add_argument("--install-python", type=Path)
    parser.add_argument("--version", help="build this exact stable X.Y.Z release instead of advancing a patch")
    parser.add_argument("--sdist", action="store_true", help="also create the matching source distribution")
    args = parser.parse_args(argv)
    root = args.source_root.resolve()
    version = set_version(root, args.version) if args.version else advance(root)
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
