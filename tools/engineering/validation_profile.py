"""Conservative, diff-derived Engineering validation profile selection."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

DOCUMENTATION_PREFIXES = ("docs/",)
DASHBOARD_PREFIXES = ("tools/engineering/assets/",)
DASHBOARD_FILES = {"tools/engineering/dashboard.py", "tests/engineering/dashboard.spec.mjs", "package.json", "package-lock.json"}
RUNTIME_PREFIXES = ("tools/engineering/", "tests/engineering/", ".github/workflows/")
VALIDATION_PROFILE_VERSION = "1.0"
REQUIRED_CONTROLS = {
    "DOCUMENTATION": ("git_diff_check", "documentation_contract"),
    "DASHBOARD": ("git_diff_check", "engineering_python", "dashboard_browser"),
    "RUNTIME": ("git_diff_check", "engineering_python", "dashboard_browser"),
    "FULL": ("git_diff_check", "repository_suite"),
}


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

def classify(paths: list[str] | tuple[str, ...]) -> ValidationProfile:
    items = tuple(sorted({path.strip() for path in paths if path.strip()}))
    if items and all(path.startswith(DOCUMENTATION_PREFIXES) or path.endswith(".md") for path in items):
        return ValidationProfile("DOCUMENTATION", items, ("markdown/link/document-contract validation",))
    if items and all(path.startswith(DASHBOARD_PREFIXES) or path in DASHBOARD_FILES for path in items):
        return ValidationProfile("DASHBOARD", items, ("relevant Engineering Python tests", "npm run test:engineering-dashboard"))
    if items and all(path.startswith(RUNTIME_PREFIXES) for path in items):
        return ValidationProfile("RUNTIME", items, ("relevant Engineering Python tests", "npm run test:engineering-dashboard when projection is affected"))
    return ValidationProfile("FULL", items, ("full required repository suite",))

def changed_paths(root: Path, base: str) -> tuple[str, ...]:
    completed = subprocess.run(("git", "diff", "--name-only", f"{base}...HEAD"), cwd=root, text=True, capture_output=True, check=False)
    if completed.returncode:
        return ()
    return tuple(completed.stdout.splitlines())

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args()
    profile = classify(changed_paths(Path.cwd(), args.base))
    if args.github_output:
        Path(args.github_output).open("a", encoding="utf-8").write(f"tier={profile.tier}\n")
    print(profile.tier)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
