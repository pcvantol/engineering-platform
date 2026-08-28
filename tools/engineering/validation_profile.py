"""Conservative, diff-derived Engineering validation profile selection."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess

DOCUMENTATION_PREFIXES = ("docs/",)
DASHBOARD_PREFIXES = ("tools/engineering/assets/",)
DASHBOARD_FILES = {"tools/engineering/dashboard.py", "tests/engineering/dashboard.spec.mjs", "package.json", "package-lock.json"}
RUNTIME_PREFIXES = ("tools/engineering/", "tests/engineering/", ".github/workflows/")
VALIDATION_PROFILE_VERSION = "1.0"
REQUIRED_CONTROLS = {
    "DOCUMENTATION": ("git_diff_check", "documentation_contract"),
    "DASHBOARD": ("git_diff_check", "engineering_python", "engineering_dashboard"),
    "RUNTIME": ("git_diff_check", "engineering_python", "projection_dashboard"),
    "FULL": ("git_diff_check", "repository_suite"),
}

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
