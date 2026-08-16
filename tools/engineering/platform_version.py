"""Deterministic Engineering Platform manifest and compatibility validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CONTRACT = re.compile(r"^(\d{4})\.(0[1-9]|1[0-2])$")
MANIFEST_FIELDS = frozenset(
    {
        "platform_version",
        "runner_version",
        "bootstrap_contract",
        "checkpoint_format",
        "memory_format",
        "report_format",
        "minimum_codex_cli",
        "watcher_version",
        "inbox_protocol",
        "dashboard_version",
        "handoff_protocol",
        "status_model",
        "storage_schema",
    }
)


class EngineeringPlatformCompatibilityError(ValueError):
    """Raised when a repository's declared engineering contract is unsupported."""


def _semver(value: str, field: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise EngineeringPlatformCompatibilityError(
            f"Engineering Platform manifest field {field} must use MAJOR.MINOR.PATCH."
        )
    return tuple(int(part) for part in match.groups())


def _contract(value: str, field: str) -> tuple[int, int]:
    match = CONTRACT.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise EngineeringPlatformCompatibilityError(
            f"Engineering Platform manifest field {field} must use YYYY.MM."
        )
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True)
class EngineeringPlatformManifest:
    platform_version: str
    runner_version: str
    bootstrap_contract: str
    checkpoint_format: int
    memory_format: int
    report_format: int
    minimum_codex_cli: str
    watcher_version: str
    inbox_protocol: int
    dashboard_version: str
    handoff_protocol: int
    status_model: int
    storage_schema: int

    @classmethod
    def load(cls, path: Path) -> "EngineeringPlatformManifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EngineeringPlatformCompatibilityError(
                "Engineering Platform manifest cannot be read."
            ) from error
        if not isinstance(raw, dict) or set(raw) != MANIFEST_FIELDS:
            raise EngineeringPlatformCompatibilityError(
                "Engineering Platform manifest fields are incompatible."
            )
        manifest = cls(**raw)
        _semver(manifest.platform_version, "platform_version")
        _semver(manifest.runner_version, "runner_version")
        _semver(manifest.minimum_codex_cli, "minimum_codex_cli")
        _semver(manifest.watcher_version, "watcher_version")
        _semver(manifest.dashboard_version, "dashboard_version")
        _contract(manifest.bootstrap_contract, "bootstrap_contract")
        for field in (
            "checkpoint_format",
            "memory_format",
            "report_format",
            "inbox_protocol",
            "handoff_protocol",
            "status_model",
            "storage_schema",
        ):
            if not isinstance(getattr(manifest, field), int) or getattr(manifest, field) < 1:
                raise EngineeringPlatformCompatibilityError(
                    f"Engineering Platform manifest field {field} must be a positive integer."
                )
        return manifest


@dataclass(frozen=True)
class RunnerCompatibility:
    platform_version: str = "1.5.0"
    runner_version: str = "1.5.0"
    bootstrap_contract: str = "2026.12"
    checkpoint_formats: frozenset[int] = frozenset({1})
    memory_formats: frozenset[int] = frozenset({1, 2})
    report_formats: frozenset[int] = frozenset({1, 2})
    # New runners retain compatibility with prior local stores while accepting
    # the current telemetry-capable schema.
    storage_schemas: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21})


def validate_compatibility(
    manifest: EngineeringPlatformManifest, runner: RunnerCompatibility, detected_codex_cli: str
) -> None:
    """Fail closed unless this runner explicitly supports every repository contract."""
    required_platform = _semver(manifest.platform_version, "platform_version")
    actual_platform = _semver(runner.platform_version, "runner platform_version")
    required_runner = _semver(manifest.runner_version, "runner_version")
    actual_runner = _semver(runner.runner_version, "runner_version")
    if required_platform[0] != actual_platform[0]:
        raise EngineeringPlatformCompatibilityError(
            f"Engineering Platform mismatch\nRepository requires: {manifest.platform_version}\nRunner: {runner.platform_version}\nBLOCKED\nEngineering Platform upgrade required."
        )
    if actual_runner < required_runner:
        raise EngineeringPlatformCompatibilityError(
            f"Runner version mismatch\nRepository requires: {manifest.runner_version}\nRunner: {runner.runner_version}\nBLOCKED\nRunner upgrade required."
        )
    required_contract = _contract(manifest.bootstrap_contract, "bootstrap_contract")
    actual_contract = _contract(runner.bootstrap_contract, "runner bootstrap_contract")
    if actual_contract < required_contract:
        raise EngineeringPlatformCompatibilityError(
            f"Bootstrap contract mismatch\nRepository requires: {manifest.bootstrap_contract}\nRunner: {runner.bootstrap_contract}\nBLOCKED\nEngineering Platform upgrade required."
        )
    for label, required, supported in (
        ("Checkpoint format", manifest.checkpoint_format, runner.checkpoint_formats),
        ("Engineering Memory format", manifest.memory_format, runner.memory_formats),
        ("Report format", manifest.report_format, runner.report_formats),
        ("Engineering storage schema", manifest.storage_schema, runner.storage_schemas),
    ):
        if required not in supported:
            detected = ", ".join(str(value) for value in sorted(supported)) or "none"
            raise EngineeringPlatformCompatibilityError(
                f"{label} mismatch\nRepository requires: {required}\nRunner supports: {detected}\nBLOCKED\nEngineering Platform upgrade required."
            )
    required_cli = _semver(manifest.minimum_codex_cli, "minimum_codex_cli")
    detected_cli = _semver(detected_codex_cli, "detected Codex CLI version")
    if detected_cli < required_cli:
        raise EngineeringPlatformCompatibilityError(
            f"Codex CLI version mismatch\nRepository requires: {manifest.minimum_codex_cli}\nDetected: {detected_codex_cli}\nBLOCKED\nCodex CLI upgrade required."
        )


def detected_codex_cli_version(output: str) -> str:
    """Extract a stable semantic version from the local CLI version output."""
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    if not match:
        raise EngineeringPlatformCompatibilityError(
            "Detected Codex CLI version is invalid. Run `codex --version` and install a supported release."
        )
    return match.group(1)
