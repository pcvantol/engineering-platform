"""Fail-closed Level 1 health checks for the local Engineering Execution Host.

This module intentionally validates only the host's own configuration, runtime
and evidence services.  It never inspects a target repository or prompt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from time import monotonic

from .component_logging import component_logger
from .platform_api import PlatformConfiguration, PlatformConfigurationError
from .platform_version import EngineeringPlatformManifest
from .storage import open_storage


DEFAULT_MINIMUM_FREE_BYTES = 1_073_741_824
MINIMUM_FREE_BYTES_ENVIRONMENT = "DJCONNECT_ENGINEERING_PREFLIGHT_MIN_FREE_BYTES"
TELEMETRY_ENABLED_ENVIRONMENT = "DJCONNECT_ENGINEERING_TELEMETRY_PERSISTENCE"


@dataclass(frozen=True)
class HostPreflightCheck:
    identifier: str
    outcome: str
    reason: str
    recovery: str


@dataclass(frozen=True)
class HostPreflightResult:
    outcome: str
    execution_host: str
    version: str
    bootstrap_contract: str
    timestamp: str
    duration_ms: int
    checks: tuple[HostPreflightCheck, ...]

    def payload(self, run_id: str | None = None) -> dict[str, object]:
        result = asdict(self)
        result["checks"] = [asdict(check) for check in self.checks]
        result["run_id"] = run_id
        return result


def _check(identifier: str, passed: bool, reason: str, recovery: str) -> HostPreflightCheck:
    return HostPreflightCheck(identifier, "PASS" if passed else "FAIL", reason, recovery)


def _minimum_free_bytes() -> int:
    value = os.environ.get(MINIMUM_FREE_BYTES_ENVIRONMENT, str(DEFAULT_MINIMUM_FREE_BYTES))
    try:
        parsed = int(value)
    except ValueError:
        return -1
    return parsed if parsed >= 0 else -1


def _telemetry_enabled() -> bool:
    return os.environ.get(TELEMETRY_ENABLED_ENVIRONMENT, "true").strip().casefold() not in {"0", "false", "no"}


def _writable(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".preflight-", dir=path)
        os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _persist(root: Path, result: HostPreflightResult, run_id: str | None) -> None:
    directory = root / ".engineering" / "status"
    if not directory.is_dir() or not _writable(directory):
        return
    payload = json.dumps(result.payload(run_id), separators=(",", ":"), sort_keys=True) + "\n"
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".host-preflight-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "host_preflight.json")
    except OSError:
        try:
            Path(temporary).unlink(missing_ok=True)
        except UnboundLocalError:
            pass


def execute(root: Path, *, run_id: str | None = None) -> HostPreflightResult:
    """Run Level 1 checks and persist bounded evidence without claiming work."""
    started = monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    checks: list[HostPreflightCheck] = []
    configuration = None
    try:
        configuration = PlatformConfiguration.load(root)
        checks.append(_check("configuration", True, "Required host configuration is readable.", "No action required."))
    except PlatformConfigurationError:
        checks.append(_check("configuration", False, "Required host configuration is unavailable.", "Restore a valid Engineering Platform configuration."))

    manifest: EngineeringPlatformManifest | None = None
    try:
        manifest = EngineeringPlatformManifest.load(Path(__file__).with_name("ENGINEERING_PLATFORM_VERSION.json"))
        checks.append(_check("host_identity", True, "Execution Host identity, version and bootstrap contract are available.", "No action required."))
    except Exception:
        checks.append(_check("host_identity", False, "Execution Host identity or bootstrap contract is unavailable.", "Restore the Engineering Platform version manifest."))

    directories = ("status", "reports", "logs", "inbox-processing")
    for name in directories:
        path = root / ".engineering" / name
        checks.append(_check(f"directory_{name}", path.is_dir(), f"Runtime directory {name} is available." if path.is_dir() else f"Runtime directory {name} is missing.", f"Create and secure .engineering/{name} before accepting work."))
        if path.is_dir():
            writable = _writable(path)
            checks.append(_check(f"writable_{name}", writable, f"Runtime directory {name} is writable." if writable else f"Runtime directory {name} is not writable.", f"Restore write access to .engineering/{name}."))

    threshold = _minimum_free_bytes()
    if threshold < 0:
        checks.append(_check("disk_space", False, "Configured free-disk threshold is invalid.", f"Set {MINIMUM_FREE_BYTES_ENVIRONMENT} to a non-negative byte value."))
    else:
        free = shutil.disk_usage(root).free
        checks.append(_check("disk_space", free >= threshold, "Sufficient free disk space is available." if free >= threshold else "Free disk space is below the configured host threshold.", "Free disk space or lower the configured host preflight threshold."))

    executable = shutil.which("codex") if configuration is not None else None
    checks.append(_check("runtime_executable", bool(executable), "Configured runtime executable is available." if executable else "Configured runtime executable is unavailable.", "Install or expose the Codex CLI on the Execution Host PATH."))
    if executable:
        try:
            invoked = subprocess.run((executable, "--version"), text=True, capture_output=True, check=False, timeout=3)
            available = invoked.returncode == 0
        except (OSError, subprocess.SubprocessError):
            available = False
        checks.append(_check("runtime_invocation", available, "Configured runtime executable is invokable." if available else "Configured runtime executable cannot be invoked.", "Repair the Codex CLI installation before accepting work."))

    if _telemetry_enabled():
        try:
            connection = open_storage(root)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()
            checks.append(_check("telemetry_storage", True, "Telemetry SQLite storage is accessible and writable.", "No action required."))
        except (OSError, sqlite3.DatabaseError, RuntimeError):
            checks.append(_check("telemetry_storage", False, "Telemetry SQLite storage is unavailable.", "Restore local SQLite evidence storage before accepting work."))

    try:
        component_logger(root, "inbox")
        checks.append(_check("structured_logging", True, "Structured logging initializes successfully.", "No action required."))
    except Exception:
        checks.append(_check("structured_logging", False, "Structured logging cannot initialize.", "Restore the local logging destination before accepting work."))

    outcome = (
        "FAIL"
        if any(check.outcome == "FAIL" for check in checks)
        else "WARNING"
        if any(check.outcome == "WARNING" for check in checks)
        else "PASS"
    )
    result = HostPreflightResult(
        outcome,
        configuration.platform.name if configuration else "Engineering Platform",
        manifest.platform_version if manifest else "unavailable",
        manifest.bootstrap_contract if manifest else "unavailable",
        timestamp,
        round((monotonic() - started) * 1000),
        tuple(checks),
    )
    _persist(root, result, run_id)
    return result


def latest(root: Path) -> dict[str, object]:
    """Return only safe, compact preflight evidence for the dashboard/report."""
    try:
        payload = json.loads((root / ".engineering" / "status" / "host_preflight.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in ("outcome", "timestamp", "duration_ms", "execution_host", "version", "bootstrap_contract", "checks", "run_id") if key in payload}
