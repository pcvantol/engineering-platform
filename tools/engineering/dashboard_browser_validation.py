"""Run dashboard browser validation with local CI-parity and host safety."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .component_lock import single_instance


SHARDS = ("1/4", "2/4", "3/4", "4/4")
PLAYWRIGHT_COMMAND = ("npx", "playwright", "test", "tests/engineering/dashboard.spec.mjs")
LOCK_COMPONENT = "dashboard-browser-validation"


def _common_git_directory(root: Path) -> Path:
    """Return the Git directory shared by every worktree of this repository."""
    observed = subprocess.run(
        ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    directory = observed.stdout.strip()
    if observed.returncode or not directory:
        raise RuntimeError("Dashboard browser validation requires a Git worktree.")
    return Path(directory)


def _command(*arguments: str) -> tuple[str, ...]:
    return (*PLAYWRIGHT_COMMAND, *arguments)


def _run_ci(root: Path, arguments: tuple[str, ...]) -> int:
    """Delegate one CI shard unchanged to Playwright."""
    return subprocess.run(_command(*arguments), cwd=root, check=False).returncode


def _run_local_shards(root: Path) -> int:
    """Run four isolated one-worker shards, refusing overlapping local batches."""
    environment = {**os.environ, "CI": "1"}
    common_git = _common_git_directory(root)
    with single_instance(common_git, LOCK_COMPONENT):
        with tempfile.TemporaryDirectory(prefix="djconnect-dashboard-shards-") as temporary:
            directory = Path(temporary)
            processes: list[tuple[str, Path, subprocess.Popen[bytes]]] = []
            for index, shard in enumerate(SHARDS, start=1):
                output = directory / f"shard-{index}.log"
                with output.open("wb") as stream:
                    process = subprocess.Popen(
                        _command("--reporter=line", f"--shard={shard}", f"--output=test-results/dashboard-shard-{index}"),
                        cwd=root,
                        env=environment,
                        stderr=subprocess.STDOUT,
                        stdout=stream,
                    )
                processes.append((shard, output, process))
            results = []
            for shard, output, process in processes:
                result = process.wait()
                results.append((shard, output.read_text(encoding="utf-8", errors="replace"), result))
    for shard, output, _ in results:
        print(f"\n=== Dashboard browser shard {shard} ===")
        print(output, end="" if output.endswith("\n") else "\n")
    return 0 if all(result == 0 for _, _, result in results) else 1


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Keep GitHub's one-worker shard contract and coordinate local parity runs."""
    root = Path.cwd()
    arguments = arguments if arguments is not None else tuple(sys.argv[1:])
    if os.environ.get("CI"):
        return _run_ci(root, arguments)
    if arguments:
        raise SystemExit("Local dashboard validation does not accept Playwright arguments; run the coordinated four-shard batch.")
    return _run_local_shards(root)


if __name__ == "__main__":
    raise SystemExit(main())
