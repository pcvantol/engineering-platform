"""Run dashboard browser validation with local CI-parity and host safety."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import json

from .component_lock import single_instance


SHARDS = ("1/4", "2/4", "3/4", "4/4")
PLAYWRIGHT_COMMAND = ("npx", "playwright", "test", "tests/engineering/dashboard.spec.mjs")
LOCK_COMPONENT = "dashboard-browser-validation"
LOCAL_BATCH_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_TIMEOUT_SECONDS = 5
EVIDENCE_RUN_ID_ENV = "DJCONNECT_ENGINEERING_VALIDATION_RUN_ID"


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
    return (*PLAYWRIGHT_COMMAND, "--workers=1", *arguments)


def dashboard_evidence_path(root: Path, run_id: str) -> Path:
    """Return the ignored, run-scoped shard evidence payload location."""
    return root / ".engineering" / "validation" / "dashboard-browser" / f"{run_id}.json"


def _write_evidence(root: Path, results: list[tuple[str, str, int | None]], *, cleanup: str) -> None:
    """Write bounded structured shard facts only when the host supplied a run id."""
    run_id = os.environ.get(EVIDENCE_RUN_ID_ENV)
    if not run_id:
        return
    if not run_id.replace("-", "").isalnum():
        return
    path = dashboard_evidence_path(root, run_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "expected_shard_count": len(SHARDS),
        "actual_shard_count": len(results),
        "workers_per_shard": 1,
        "shards": [
            {"shard": shard, "exit_code": result, "result": "PASS" if result == 0 else "FAIL" if result is not None else "UNAVAILABLE"}
            for shard, _, result in results
        ],
        "cleanup": cleanup,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_dashboard_evidence(root: Path, run_id: str) -> dict[str, object] | None:
    """Load only a complete, fixed-topology dashboard shard payload."""
    try:
        payload = json.loads(dashboard_evidence_path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    shards = payload.get("shards")
    if (
        payload.get("expected_shard_count") != len(SHARDS)
        or payload.get("actual_shard_count") != len(SHARDS)
        or payload.get("workers_per_shard") != 1
        or not isinstance(shards, list)
        or len(shards) != len(SHARDS)
        or tuple(item.get("shard") for item in shards if isinstance(item, dict)) != SHARDS
    ):
        return None
    return payload


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
        except (PermissionError, ProcessLookupError):
            # A shard can already have exited and its process-group identity
            # may no longer be signalable on macOS. Cleanup is best-effort;
            # it must not replace the authoritative shard exit result.
            pass
    active = [process for _, _, process in processes if process.poll() is None]
    for process in active:
        try:
            process.wait(timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
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
            cleanup_attempted = False
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
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        break
            except BaseException:
                _terminate_process_groups(processes)
                cleaned_up = True
                raise
            finally:
                if not cleaned_up and (timed_out or any(process.poll() is None for _, _, process in processes)):
                    _terminate_process_groups(processes)
                    cleanup_attempted = True
            results = _read_results(processes)
    _write_evidence(root, results, cleanup="ATTEMPTED" if cleanup_attempted else "NOT_REQUIRED")
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
