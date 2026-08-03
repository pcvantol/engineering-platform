"""Fail-closed Level 3 capability admission for Engineering Inbox work.

This module evaluates only declared transaction requirements against the local
Execution Host contract.  It never claims an Inbox item, allocates a run or
touches a target repository.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from time import monotonic

from .platform_version import (
    EngineeringPlatformCompatibilityError,
    EngineeringPlatformManifest,
    RunnerCompatibility,
    _semver,
)

RECOVERABILITY = frozenset(
    {
        "RETRYABLE",
        "RETRYABLE_AFTER_HOST_REPAIR",
        "REQUIRES_NEW_PROMPT",
        "REQUIRES_OPERATOR_DECISION",
        "NON_RETRYABLE",
    }
)
FAILURE_ORIGINS = frozenset(
    {"HOST", "WORKSPACE", "CAPABILITY", "VALIDATION", "ENGINEERING", "GOVERNANCE"}
)


@dataclass(frozen=True)
class CapabilityCheck:
    identifier: str
    outcome: str
    reason: str
    recovery: str


@dataclass(frozen=True)
class CapabilityPreflightResult:
    outcome: str
    timestamp: str
    duration_ms: int
    checks: tuple[CapabilityCheck, ...]
    recoverability: str
    failure_origin: str | None
    recommendation: str

    def payload(self, run_id: str | None = None) -> dict[str, object]:
        value = asdict(self)
        value["checks"] = [asdict(check) for check in self.checks]
        value["run_id"] = run_id
        return value


def _check(identifier: str, passed: bool, reason: str, recovery: str) -> CapabilityCheck:
    return CapabilityCheck(identifier, "PASS" if passed else "FAIL", reason, recovery)


def _value(prompt: str, field: str) -> str | None:
    match = re.search(rf"(?mi)^\s*{re.escape(field)}\s*:\s*([^\n]+)$", prompt)
    return match.group(1).strip() if match else None


def _requirements(prompt: str) -> dict[str, str]:
    """Read a bounded, provider-neutral declaration from the transaction."""
    aliases = {
        "Execution Host Version": "platform_version",
        "Runner Version": "runner_version",
        "Configuration Schema": "configuration_schema",
        "Engineering Database Schema": "storage_schema",
        "Checkpoint Format": "checkpoint_format",
        "Memory Format": "memory_format",
        "Report Format": "report_format",
        "Execution Mode": "execution_mode",
        "Required Runtime Components": "runtime_components",
        "Required Provider Support": "provider_support",
        "Required Capabilities": "capabilities",
    }
    return {key: value for field, key in aliases.items() if (value := _value(prompt, field))}


def _persist(root: Path, result: CapabilityPreflightResult, run_id: str | None) -> None:
    directory = root / ".engineering" / "status"
    if not directory.is_dir():
        return
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".capability-preflight-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(result.payload(run_id), separators=(",", ":"), sort_keys=True) + "\n"
            )
        os.replace(temporary, directory / "capability_preflight.json")
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def execute(root: Path, prompt: str, *, run_id: str | None = None) -> CapabilityPreflightResult:
    """Evaluate declared requirements before claim; absent declarations are compatible."""
    started, checks = monotonic(), []
    requirements = _requirements(prompt)
    try:
        manifest = EngineeringPlatformManifest.load(
            root / "tools/engineering/ENGINEERING_PLATFORM_VERSION.json"
        )
        runner = RunnerCompatibility()
    except EngineeringPlatformCompatibilityError as error:
        checks.append(
            _check("host_contract", False, str(error), "Repair the Execution Host contract.")
        )
        manifest = None
        runner = None
    if manifest and runner:
        versions = (
            (
                "platform_version",
                "execution_host_version",
                manifest.platform_version,
                runner.platform_version,
            ),
            ("runner_version", "runner_version", manifest.runner_version, runner.runner_version),
        )
        for requirement, identifier, available, actual in versions:
            required = requirements.get(requirement)
            passed = not required or _semver(actual, identifier) >= _semver(required, requirement)
            checks.append(
                _check(
                    identifier,
                    passed,
                    f"Required {required or 'none'}; available {available}.",
                    "Upgrade the Execution Host or submit a compatible prompt.",
                )
            )
        formats = (
            ("checkpoint_format", manifest.checkpoint_format, runner.checkpoint_formats),
            ("memory_format", manifest.memory_format, runner.memory_formats),
            ("report_format", manifest.report_format, runner.report_formats),
            ("storage_schema", manifest.storage_schema, runner.storage_schemas),
        )
        for requirement, available, supported in formats:
            required = (
                int(requirements[requirement])
                if requirements.get(requirement, "").isdigit()
                else available
            )
            checks.append(
                _check(
                    requirement,
                    required in supported,
                    f"Required {required}; supported {sorted(supported)}.",
                    "Upgrade the Execution Host or use a supported format.",
                )
            )
        configuration_schema = (
            int(requirements["configuration_schema"])
            if requirements.get("configuration_schema", "").isdigit()
            else 1
        )
        checks.append(
            _check(
                "configuration_schema",
                configuration_schema == 1,
                f"Required {configuration_schema}; supported [1].",
                "Upgrade the Execution Host or use configuration schema 1.",
            )
        )
        mode = requirements.get("execution_mode", "MANAGED").upper()
        checks.append(
            _check(
                "execution_mode",
                mode in {"MANAGED", "GENESIS"},
                f"Execution mode {mode} is supported."
                if mode in {"MANAGED", "GENESIS"}
                else f"Execution mode {mode} is unsupported.",
                "Use Managed or Genesis execution mode.",
            )
        )
        for requirement, identifier, available in (
            ("runtime_components", "runtime_components", {"codex", "python", "git"}),
            ("provider_support", "provider_support", {"launchd"}),
            (
                "capabilities",
                "required_capabilities",
                {
                    "workspace_authorization",
                    "host_preflight",
                    "workspace_preflight",
                    "capability_preflight",
                },
            ),
        ):
            requested = {
                item.strip().casefold()
                for item in requirements.get(requirement, "").split(",")
                if item.strip()
            }
            missing = requested - available
            checks.append(
                _check(
                    identifier,
                    not missing,
                    "All declared requirements are available."
                    if not missing
                    else f"Unsupported requirements: {', '.join(sorted(missing))}.",
                    "Install or configure the required host capability before retrying.",
                )
            )
    failed = any(check.outcome == "FAIL" for check in checks)
    result = CapabilityPreflightResult(
        "FAIL" if failed else "PASS",
        datetime.now(timezone.utc).isoformat(),
        round((monotonic() - started) * 1000),
        tuple(checks),
        "RETRYABLE_AFTER_HOST_REPAIR" if failed else "RETRYABLE",
        "CAPABILITY" if failed else None,
        "Repair or upgrade the Execution Host before resubmitting."
        if failed
        else "Capability admission passed.",
    )
    _persist(root, result, run_id)
    return result


def latest(root: Path) -> dict[str, object]:
    try:
        payload = json.loads(
            (root / ".engineering/status/capability_preflight.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
