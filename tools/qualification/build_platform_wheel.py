#!/usr/bin/env python3
"""The sole supported fresh EP wheel build/install entrypoint."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from advance_platform_build import _current_version


def _extract_committed_source(root: Path, destination: Path) -> None:
    """Materialize exactly HEAD without consuming ignored build residue."""
    tracked = subprocess.run(
        ("git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"),
        check=True, text=True, capture_output=True,
    )
    if tracked.stdout.strip():
        raise RuntimeError("candidate source has uncommitted tracked changes")
    archive = subprocess.run(
        ("git", "-C", str(root), "archive", "--format=tar", "HEAD"),
        check=True, capture_output=True,
    ).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(destination, filter="data")


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
    with tempfile.TemporaryDirectory(prefix="ep-committed-wheel-source-") as temporary:
        staged_root = Path(temporary) / "source"
        _extract_committed_source(root, staged_root)
        if _current_version(staged_root) != version:
            raise RuntimeError("committed candidate version does not match source version")
        if args.sdist:
            subprocess.run(
                (sys.executable, "-m", "build", "--outdir", str(wheel_directory), str(staged_root)),
                check=True, cwd=Path(temporary),
            )
        else:
            subprocess.run(
                (sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_directory), str(staged_root)),
                check=True, cwd=Path(temporary),
            )
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
