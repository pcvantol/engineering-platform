"""Conservative, diff-derived Engineering validation profile selection."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

DOCUMENTATION_PREFIXES = ("docs/",)
DASHBOARD_PREFIXES = ("src/engineering_platform/assets/",)
DASHBOARD_FILES = {"src/engineering_platform/server_console_services.py", "src/engineering_platform/server.py", "tests/engineering/dashboard.spec.mjs", "package.json", "package-lock.json"}
RUNTIME_PREFIXES = ("src/engineering_platform/", "tests/engineering/", ".github/workflows/")
VALIDATION_PROFILE_VERSION = "1.0"
REQUIRED_CONTROLS = {
    "DOCUMENTATION": ("git_diff_check", "documentation_contract"),
    "DASHBOARD": ("git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"),
    "RUNTIME_UI": ("git_diff_check", "engineering_python", "console_route_ownership", "ui_localization", "dashboard_browser"),
    "RUNTIME": ("git_diff_check", "engineering_python", "console_route_ownership", "dashboard_browser"),
    "FULL": ("git_diff_check", "repository_suite"),
    # A governed P-CENTRAL-CORE change retains the full Python/core suite but
    # deliberately does not claim the deferred Operations Console browser
    # qualification.  It is selected only by the CI phase boundary below.
    "P_CENTRAL_CORE": ("git_diff_check", "engineering_python"),
}
P_CENTRAL_CORE_BRANCH = re.compile(r"^codex/phase-p-central-core(?:-.+)?$")


@dataclass(frozen=True)
class ValidationControlLauncher:
    """One deterministic launcher for a resolved validation-control identity.

    Profiles select identities; this registry owns the repository-local
    implementation of those identities. Lifecycle code only schedules the
    persisted identities and never branches on a project-specific control.
    """

    validation_id: str
    category: str
    control_identity: str
    command: tuple[str, ...]


class ValidationProfileResolutionError(ValueError):
    """The selected run profile is absent or does not match this registry."""


def validation_profile_identity(
    *, candidate_sha: str, currentness: int, selected_validation_tier: str,
    validation_profile_version: str, profile_reference: str,
    profile_selection_source: str, required_validation_controls: tuple[str, ...],
    control_bindings: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], str]:
    """Build the canonical candidate-bound identity of one validation pass."""
    if (
        re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None
        or isinstance(currentness, bool) or not isinstance(currentness, int) or currentness < 0
        or not all(isinstance(value, str) and value for value in (
            selected_validation_tier, validation_profile_version,
            profile_reference, profile_selection_source,
        ))
        or not required_validation_controls
        or len(set(required_validation_controls)) != len(required_validation_controls)
        or any(not isinstance(control, str) or not control for control in required_validation_controls)
        or len(control_bindings) != len(required_validation_controls)
        or any(not isinstance(binding, dict) for binding in control_bindings)
        or tuple(binding.get("validation_id") for binding in control_bindings) != required_validation_controls
    ):
        raise ValidationProfileResolutionError("Validation profile identity is invalid.")
    identity: dict[str, object] = {
        "candidate_sha": candidate_sha,
        "currentness": currentness,
        "selected_validation_tier": selected_validation_tier,
        "validation_profile_version": validation_profile_version,
        "profile_reference": profile_reference,
        "profile_selection_source": profile_selection_source,
        "required_validation_controls": list(required_validation_controls),
        "control_bindings": list(control_bindings),
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return identity, digest


def _valid_terminal_times(started_at: object, ended_at: object) -> bool:
    if not isinstance(started_at, str) or not started_at or not isinstance(ended_at, str) or not ended_at:
        return False
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:
        return False
    return start.tzinfo is not None and end.tzinfo is not None and end >= start


def strict_required_controls_pass(
    validation_context: object, *, candidate_sha: str, currentness: int,
) -> bool:
    """Require exact terminal receipts for every profile-owned required control."""
    if not isinstance(validation_context, dict):
        return False
    try:
        identity, digest = validation_profile_identity(
            candidate_sha=candidate_sha,
            currentness=currentness,
            selected_validation_tier=validation_context["selected_validation_tier"],
            validation_profile_version=validation_context["validation_profile_version"],
            profile_reference=validation_context["profile_reference"],
            profile_selection_source=validation_context["profile_selection_source"],
            required_validation_controls=validation_context["required_validation_controls"],
            control_bindings=validation_context["control_bindings"],
        )
    except (KeyError, TypeError, ValidationProfileResolutionError):
        return False
    if (
        validation_context.get("candidate_sha") != candidate_sha
        or validation_context.get("currentness") != currentness
        or validation_context.get("profile_digest") != digest
        or validation_context.get("profile_currentness_conflict") is True
    ):
        return False
    required = tuple(identity["required_validation_controls"])
    bindings = tuple(identity["control_bindings"])
    controls = validation_context.get("controls")
    if not isinstance(controls, dict):
        return False
    binding_by_id = {binding.get("validation_id"): binding for binding in bindings}
    for validation_id in required:
        binding = binding_by_id.get(validation_id)
        control = controls.get(validation_id)
        if not isinstance(binding, dict) or binding.get("required") is not True or not isinstance(control, dict):
            return False
        if (
            control.get("validation_id") != validation_id
            or control.get("required_for_profile") is not True
            or control.get("execution_status") != "EXECUTED"
            or control.get("result") != "PASS"
            or control.get("exit_code") != 0
            or control.get("currentness") != currentness
            or not isinstance(control.get("command_id"), str)
            or not control.get("command_id")
            or control.get("evidence_authority") != "command_terminal"
            or control.get("category") != binding.get("category")
            or control.get("control_identity") != binding.get("control_identity")
            or not _valid_terminal_times(control.get("started_at"), control.get("ended_at"))
        ):
            return False
    return True


def _python_command(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, *arguments)


CONTROL_LAUNCHERS = {
    "git_diff_check": ValidationControlLauncher(
        "git_diff_check", "repository", "git diff --check", ("git", "diff", "--check"),
    ),
    "documentation_contract": ValidationControlLauncher(
        "documentation_contract", "documentation",
        "python3 -m unittest tests.engineering.test_engineering_operational_documentation",
        _python_command("-m", "unittest", "tests.engineering.test_engineering_operational_documentation"),
    ),
    "engineering_python": ValidationControlLauncher(
        "engineering_python", "python", "python3 -m unittest discover -s tests/engineering",
        _python_command("-m", "unittest", "discover", "-s", "tests/engineering"),
    ),
    "dashboard_browser": ValidationControlLauncher(
        "dashboard_browser", "browser", "npm run test:engineering-dashboard",
        ("npm", "run", "test:engineering-dashboard"),
    ),
    "ui_localization": ValidationControlLauncher(
        "ui_localization", "browser", "npm run test:ui-localization",
        ("npm", "run", "test:ui-localization"),
    ),
    "console_route_ownership": ValidationControlLauncher(
        "console_route_ownership", "python", "Console route ownership guard",
        _python_command("tools/qualification/console_route_ownership_guard.py", "--source-root", "src"),
    ),
    "repository_suite": ValidationControlLauncher(
        "repository_suite", "repository", "python3 -m unittest discover",
        _python_command("-m", "unittest", "discover"),
    ),
}


def control_launcher(validation_id: str) -> ValidationControlLauncher | None:
    """Resolve a persisted required-control identity to its canonical launcher."""
    return CONTROL_LAUNCHERS.get(validation_id)


def control_binding(validation_id: str) -> dict[str, object] | None:
    """Return the immutable launcher snapshot for one registry control."""
    launcher = control_launcher(validation_id)
    if launcher is None:
        return None
    return {
        "validation_id": launcher.validation_id,
        "required": True,
        "category": launcher.category,
        "control_identity": launcher.control_identity,
        "command": list(launcher.command),
    }


def resolve_producer_profile(payload: object) -> tuple["ValidationProfile", str]:
    """Resolve a producer-selected profile against the canonical registry.

    A validation-only request carries the selection as structured execution
    context; prose is never a selection input.  The producer may select a
    registry profile, but may not substitute its own control set.
    """
    if not isinstance(payload, dict):
        raise ValidationProfileResolutionError("Selected validation profile is unavailable.")
    tier, version, controls = payload.get("tier"), payload.get("version"), payload.get("required_controls")
    if not isinstance(tier, str) or tier not in REQUIRED_CONTROLS:
        raise ValidationProfileResolutionError("Selected validation profile is invalid.")
    if version != VALIDATION_PROFILE_VERSION:
        raise ValidationProfileResolutionError("Selected validation profile version is unavailable.")
    expected = REQUIRED_CONTROLS[tier]
    if not isinstance(controls, list) or tuple(controls) != expected:
        raise ValidationProfileResolutionError("Selected validation profile controls are invalid.")
    return ValidationProfile(tier, (), tuple()), f"validation-profile-registry:{tier}@{version}"


def producer_profile_payload(tier: object) -> dict[str, object]:
    """Build the one allowed producer envelope value for a registry tier.

    Producers select only the canonical tier.  The registry remains the sole
    owner of profile version and required-control identities, so a caller
    cannot create a second profile representation or substitute controls.
    """
    if not isinstance(tier, str) or tier not in REQUIRED_CONTROLS:
        raise ValidationProfileResolutionError("Selected validation profile is invalid.")
    payload: dict[str, object] = {
        "tier": tier,
        "version": VALIDATION_PROFILE_VERSION,
        "required_controls": list(REQUIRED_CONTROLS[tier]),
    }
    resolve_producer_profile(payload)
    return payload


def profile_control_bindings(profile: "ValidationProfile") -> tuple[dict[str, object], ...]:
    """Snapshot every launcher selected by a profile before execution."""
    bindings = tuple(control_binding(validation_id) for validation_id in profile.required_controls)
    if any(binding is None for binding in bindings):
        raise ValidationProfileResolutionError("Selected validation profile launcher is unavailable.")
    return tuple(binding for binding in bindings if binding is not None)


def matching_control_binding(
    command: str, control_bindings: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    """Resolve only an exact, standalone profile-owned launcher command.

    Python interpreter paths are normalized because the registry snapshots the
    host interpreter while provider telemetry commonly reports ``python3``.
    Arguments, shell composition and environment overrides remain exact.
    """
    if not isinstance(command, str) or not command or "\n" in command or "\r" in command:
        return None
    try:
        observed = shlex.split(command)
    except ValueError:
        return None
    if not observed or any(token in {"&&", "||", ";", "|"} for token in observed):
        return None

    def normalized(tokens: list[str]) -> tuple[str, ...]:
        values = list(tokens)
        if values and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(values[0]).name):
            values[0] = "python"
        return tuple(values)

    observed_identity = normalized(observed)
    matches = []
    for binding in control_bindings:
        expected = binding.get("command") if isinstance(binding, dict) else None
        if (
            isinstance(expected, list)
            and expected
            and all(isinstance(token, str) and token for token in expected)
            and normalized(expected) == observed_identity
        ):
            matches.append(binding)
    return matches[0] if len(matches) == 1 else None

@dataclass(frozen=True)
class ValidationProfile:
    tier: str
    paths: tuple[str, ...]
    commands: tuple[str, ...]

    @property
    def required_controls(self) -> tuple[str, ...]:
        return REQUIRED_CONTROLS[self.tier]

def classify(paths: list[str] | tuple[str, ...], *, governed_phase: str | None = None) -> ValidationProfile:
    items = tuple(sorted({path.strip() for path in paths if path.strip()}))
    if governed_phase == "P_CENTRAL_CORE":
        return ValidationProfile("P_CENTRAL_CORE", items, ("relevant Engineering Python tests", "P-CENTRAL-CONSOLE browser deferred"))
    if items and all(path.startswith(DOCUMENTATION_PREFIXES) or path.endswith(".md") for path in items):
        return ValidationProfile("DOCUMENTATION", items, ("markdown/link/document-contract validation",))
    if items and all(path.startswith(DASHBOARD_PREFIXES) or path in DASHBOARD_FILES for path in items):
        return ValidationProfile("DASHBOARD", items, ("relevant Engineering Python tests", "npm run test:engineering-dashboard"))
    if any(path.startswith(DASHBOARD_PREFIXES) or path in DASHBOARD_FILES for path in items):
        return ValidationProfile("RUNTIME_UI", items, ("relevant Engineering Python tests", "UI-GOLDEN-LOCALIZATION", "npm run test:engineering-dashboard"))
    if items and all(path.startswith(RUNTIME_PREFIXES) for path in items):
        return ValidationProfile("RUNTIME", items, ("relevant Engineering Python tests", "npm run test:engineering-dashboard when projection is affected"))
    return ValidationProfile("FULL", items, ("full required repository suite",))


def browser_dashboard_required(profile: ValidationProfile) -> bool:
    """Browser coverage is mandatory except for the governed CORE boundary."""
    return profile.tier not in {"DOCUMENTATION", "P_CENTRAL_CORE"}


def localization_required(profile: ValidationProfile) -> bool:
    """Make the five-locale gate mandatory whenever a Console surface changes."""
    return profile.tier in {"DASHBOARD", "RUNTIME_UI"}


def phase_for_branch(branch: str | None) -> str | None:
    """Return the only branch-governed exception to the browser requirement."""
    return "P_CENTRAL_CORE" if isinstance(branch, str) and P_CENTRAL_CORE_BRANCH.fullmatch(branch) else None

def changed_paths(root: Path, base: str) -> tuple[str, ...]:
    completed = subprocess.run(("git", "diff", "--name-only", f"{base}...HEAD"), cwd=root, text=True, capture_output=True, check=False)
    if completed.returncode:
        return ()
    return tuple(completed.stdout.splitlines())

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--github-output")
    parser.add_argument("--branch")
    args = parser.parse_args()
    phase = phase_for_branch(args.branch)
    profile = classify(changed_paths(Path.cwd(), args.base), governed_phase=phase)
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write(f"tier={profile.tier}\n")
            output.write(f"phase={phase or 'DEFAULT'}\n")
            output.write(f"browser_dashboard_required={'true' if browser_dashboard_required(profile) else 'false'}\n")
            output.write(f"localization_required={'true' if localization_required(profile) else 'false'}\n")
    print(profile.tier)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
