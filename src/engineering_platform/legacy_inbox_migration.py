"""One-shot migration of historical iCloud Inbox archives.

This tool is migration-only: it moves retained evidence into the current
archive layout and never admits, queues, dispatches or executes work.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))


def migrate_icloud_archives(repo: Path, root: Path) -> dict[str, int]:
    """Move historical archive evidence, leaving no runtime transport behind."""
    targets = {
        "Running": repo / ".engineering" / "inbox" / "Running",
        "Completed": repo / ".engineering" / "inbox" / "Completed",
        "Failed": repo / ".engineering" / "inbox" / "Failed",
        "Reports": repo / ".engineering" / "reports",
    }
    moved = deleted = 0
    for name, target in targets.items():
        source_directory = root / name
        if not source_directory.is_dir():
            continue
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for source in source_directory.iterdir():
            if source.is_symlink() or not source.is_file():
                continue
            destination = target / source.name
            if destination.exists():
                source.unlink()
                deleted += 1
            else:
                _move(source, destination)
                moved += 1
        # A skipped symlink or non-file is intentionally retained as
        # historical evidence.  It must not turn a safe archive migration
        # into a partial failure merely because the source directory is no
        # longer empty.
        try:
            source_directory.rmdir()
        except OSError:
            pass
    for name in ("status.json", "status.md"):
        source, destination = root / name, repo / ".engineering" / "status" / name
        if not source.is_file() or source.is_symlink():
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            source.unlink()
            deleted += 1
        else:
            _move(source, destination)
            moved += 1
    return {"moved": moved, "deleted_duplicates": deleted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--icloud-root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(migrate_icloud_archives(args.repo.resolve(), args.icloud_root.resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
