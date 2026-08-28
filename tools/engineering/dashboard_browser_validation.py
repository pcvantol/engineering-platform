"""Run dashboard browser validation with local CI-parity and host safety."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .component_lock import single_instance


SHARDS = ("1/4", "2/4", "3/4", "4/4")
PLAYWRIGHT_COMMAND = ("npx", "playwright", "test", "tests/engineering/dashboard.spec.mjs")
LOCK_COMPONENT = "dashboard-browser-validation"
LOCAL_BATCH_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_TIMEOUT_SECONDS = 5


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


def _terminate_process_groups(processes: list[tuple[str, Path, subprocess.Popen[bytes]]]) -> None:
    """Stop every owned shard group, including descendants of a failed parent."""
    # A Playwright shard can exit before the dashboard server it started in the
    # same session. Signal every owned process group so that failure cleanup
    # does not leave that server behind merely because its parent has exited.
    for _, _, process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    active = [process for _, _, process in processes if process.poll() is None]
    for process in active:
        try:
            process.wait(timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass


def _read_results(
    processes: list[tuple[str, Path, subprocess.Popen[bytes]]],
) -> list[tuple[str, str, int | None]]:
    """Return captured output after every owned shard has reached a terminal state."""
    return [
        (shard, output.read_text(encoding="utf-8", errors="replace"), process.poll())
        for shard, output, process in processes
    ]


def _run_local_shards(root: Path) -> int:
    """Run four isolated one-worker shards, refusing overlapping local batches."""
    environment = {**os.environ, "CI": "1"}
    common_git = _common_git_directory(root)
    with single_instance(common_git, LOCK_COMPONENT):
        with tempfile.TemporaryDirectory(prefix="djconnect-dashboard-shards-") as temporary:
            directory = Path(temporary)
            processes: list[tuple[str, Path, subprocess.Popen[bytes]]] = []
            failed = False
            timed_out = False
            cleaned_up = False
            deadline = time.monotonic() + LOCAL_BATCH_TIMEOUT_SECONDS
            try:
                for index, shard in enumerate(SHARDS, start=1):
                    output = directory / f"shard-{index}.log"
                    with output.open("wb") as stream:
                        process = subprocess.Popen(
                            _command("--reporter=line", f"--shard={shard}", f"--output=test-results/dashboard-shard-{index}"),
                            cwd=root,
                            env=environment,
                            stderr=subprocess.STDOUT,
                            stdout=stream,
                            start_new_session=True,
                        )
                    processes.append((shard, output, process))
                for _, _, process in processes:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        if process.wait(timeout=remaining) != 0:
                            failed = True
                            break
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        break
            except BaseException:
                _terminate_process_groups(processes)
                cleaned_up = True
                raise
            finally:
                if not cleaned_up and (timed_out or failed or any(process.poll() is None for _, _, process in processes)):
                    _terminate_process_groups(processes)
            results = _read_results(processes)
    for shard, output, _ in results:
        print(f"\n=== Dashboard browser shard {shard} ===")
        print(output, end="" if output.endswith("\n") else "\n")
    if timed_out:
        print(f"Dashboard browser validation exceeded its {LOCAL_BATCH_TIMEOUT_SECONDS}-second local deadline.")
        return 1
    return 0 if not failed and all(result == 0 for _, _, result in results) else 1


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
