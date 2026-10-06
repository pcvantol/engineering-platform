#!/usr/bin/env python3
"""The sole supported fresh EP wheel build/install entrypoint."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterator

from advance_platform_build import _current_version


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args), check=True, text=True, capture_output=True,
    )
    return result.stdout.strip()


def _checkout_identity(root: Path) -> tuple[str, str]:
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise RuntimeError("qualification source must be the repository root")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("qualification build refuses tracked source changes")
    return _git(root, "rev-parse", "--verify", "HEAD^{commit}"), _git(root, "rev-parse", "HEAD^{tree}")


def _verify_materialized_tree(root: Path, source: Path, head: str) -> None:
    listing = subprocess.run(
        ("git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", head),
        check=True, capture_output=True,
    ).stdout
    expected: set[Path] = set()
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        identity, path = entry.split(b"\t", 1)
        mode, kind, digest = identity.split(b" ")
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            raise RuntimeError("qualification source contains an unsupported tree entry")
        relative = Path(os.fsdecode(path))
        candidate = source / relative
        expected.add(relative)
        if mode == b"120000":
            if not candidate.is_symlink():
                raise RuntimeError("materialized source tree differs from committed HEAD")
            data = os.fsencode(os.readlink(candidate))
        else:
            if not candidate.is_file() or candidate.is_symlink():
                raise RuntimeError("materialized source tree differs from committed HEAD")
            data = candidate.read_bytes()
            executable = bool(candidate.stat().st_mode & stat.S_IXUSR)
            if executable != (mode == b"100755"):
                raise RuntimeError("materialized source mode differs from committed HEAD")
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest().encode()
        if actual != digest:
            raise RuntimeError("materialized source bytes differ from committed HEAD")
    actual_paths = {path.relative_to(source) for path in source.rglob("*") if path.is_file() or path.is_symlink()}
    if actual_paths != expected:
        raise RuntimeError("materialized source inventory differs from committed HEAD")


@contextmanager
def committed_source(root: Path) -> Iterator[tuple[Path, str, str]]:
    """Materialize and verify the selected commit outside the mutable checkout."""
    root = root.resolve()
    head, tree = _checkout_identity(root)
    with tempfile.TemporaryDirectory(prefix="ep-committed-build-") as directory:
        source = Path(directory) / "source"
        source.mkdir()
        archive = subprocess.run(
            ("git", "-C", str(root), "archive", "--format=tar", head),
            check=True, capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as members:
            members.extractall(source, filter="data")
        _verify_materialized_tree(root, source, head)
        yield source, head, tree
        if _checkout_identity(root) != (head, tree):
            raise RuntimeError("qualification source changed during build")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an already-versioned EP wheel without source mutation")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel-directory", type=Path)
    parser.add_argument("--install-python", type=Path)
    parser.add_argument("--version", help="require this already-committed stable X.Y.Z release")
    parser.add_argument("--sdist", action="store_true", help="also create the matching source distribution")
    args = parser.parse_args(argv)
    root = args.source_root.resolve()
    wheel_directory = (args.wheel_directory or root / "dist").resolve()
    with committed_source(root) as (source, head, tree):
        version = _current_version(source)
        if args.version is not None and args.version != version:
            raise RuntimeError(f"requested build version {args.version} does not match committed source {version}; prepare it first")
        build_directory = source.parent / "artifacts"
        build_directory.mkdir()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment["SOURCE_DATE_EPOCH"] = _git(root, "show", "-s", "--format=%ct", head)
        if args.sdist:
            subprocess.run((sys.executable, "-m", "build", "--outdir", str(build_directory), str(source)), check=True, cwd=source.parent, env=environment)
        else:
            subprocess.run((sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(build_directory), str(source)), check=True, cwd=source.parent, env=environment)
        wheel_name = f"engineering_platform-{version}-py3-none-any.whl"
        names = [wheel_name]
        if args.sdist:
            names.append(f"engineering_platform-{version}.tar.gz")
        if {item.name for item in build_directory.iterdir()} != set(names):
            raise RuntimeError("qualification build did not produce exactly the expected distributions")
        digests = {name: hashlib.sha256((build_directory / name).read_bytes()).hexdigest() for name in names}
        wheel_directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(build_directory / name, wheel_directory / name)
    wheel = wheel_directory / wheel_name
    if args.install_python is not None:
        subprocess.run((str(args.install_python), "-m", "pip", "install", "--no-deps", "--upgrade", "--force-reinstall", str(wheel)), check=True)
    for name in names:
        if hashlib.sha256((wheel_directory / name).read_bytes()).hexdigest() != digests[name]:
            raise RuntimeError("qualification artifact changed after the committed-source build")
    print(f"EP_WHEEL_BUILD=PASS version={version} source={head} tree={tree} wheel={wheel} wheel_sha256={digests[wheel_name]}")
    if args.sdist:
        print(f"EP_SDIST_BUILD=PASS sdist={wheel_directory / names[1]} sdist_sha256={digests[names[1]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
