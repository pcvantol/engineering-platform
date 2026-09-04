"""Conservative, diff-derived Engineering validation profile selection."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

DOCUMENTATION_PREFIXES = ("docs/",)
DASHBOARD_PREFIXES = ("src/engineering_platform/assets/",)
DASHBOARD_FILES = {"src/engineering_platform/dashboard.py", "src/engineering_platform/server.py", "tests/engineering/dashboard.spec.mjs", "package.json", "package-lock.json"}
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
