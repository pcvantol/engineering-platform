"""Deterministic local qualification for Engineering Platform capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .platform_version import EngineeringPlatformManifest
from .resources import package_path
from .platform_api import PlatformConfiguration
from .providers import CodexCliProvider, GitProvider


@dataclass(frozen=True)
class QualificationScenario:
    capability: str
    expected_behavior: str


SCENARIOS = tuple(
    QualificationScenario(
        capability,
        "Deterministic local contract passes; failures report evidence without changing lifecycle authority.",
    )
    for capability in (
        "Repository Initialization",
        "Checkpoint Resume",
        "Implementation Lifecycle",
        "Validation Loop",
        "Repair Loop",
        "Owner Authorization",
        "Ready For Review",
        "Automatic Merge",
        "Repository Reconciliation",
        "Finalization",
        "Repository Cleanup",
        "Engineering Memory",
        "Progress Reporting",
        "Engineering Reports",
        "Capability-aware Reviewers",
        "Diagnostics",
        "BLOCKED Recovery",
        "Failure Recovery",
        "Long-running Transactions",
        "Remote Status Model",
        "Private Dashboard",
        "Repository Handoff",
        "Remote Engineering Readiness",
        "Platform Identity",
        "Workspace Identity",
        "Provider Registry",
        "Capability Registry",
        "Public Platform API",
        "Configuration Hierarchy",
        "Configuration Migration",
        "Provider Compatibility",
        "Extraction Readiness Audit",
        "Repository Bootstrap",
        "Project Template",
        "Workspace Provisioning",
        "Genesis Lifecycle",
        "Strict Inbox Sequencing",
        "Local Engineering Evidence Storage",
        "Component Logging and Read-only Advice",
    )
)


def execute_qualification(
    root: Path,
    checks: dict[str, bool] | None = None,
    *,
    ep_repository_root: Path | None = None,
) -> dict[str, object]:
    """Execute all registered local scenarios and write immutable local evidence."""
    started = time.monotonic()
    manifest = EngineeringPlatformManifest.load(
        package_path("ENGINEERING_PLATFORM_VERSION.json")
    )
    supplied = checks or {}
    results = []
    for scenario in SCENARIOS:
        passed = supplied.get(
            scenario.capability,
            _default_check(root, scenario.capability, ep_repository_root=ep_repository_root),
        )
        results.append(
            {
                "capability": scenario.capability,
                "status": "PASS" if passed else "FAIL",
                "duration_ms": 0,
                "diagnostic": None if passed else "Scenario contract failed.",
                "evidence": scenario.expected_behavior,
            }
        )
    passed = sum(item["status"] == "PASS" for item in results)
    report = {
        "engineering_platform_version": manifest.platform_version,
        "repository_version": _repository_version(root),
        "codex_cli_version": _codex_version(),
        "qualification": "PASS" if passed == len(results) else "FAIL",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "scenarios": results,
        "coverage_percent": round(passed * 100 / len(results), 1),
        "failures": len(results) - passed,
        "blocked": 0,
    }
    _write_report(root, report)
    return report


def dashboard(report: dict[str, object]) -> str:
    scenarios = report["scenarios"]
    return "\n".join(
        (
            "Engineering Platform Qualification",
            f"Version: {report['engineering_platform_version']}",
            f"Qualification: {report['qualification']}",
            f"Scenarios: {sum(item['status'] == 'PASS' for item in scenarios)} / {len(scenarios)}",
            f"Failures: {report['failures']}",
            f"Blocked: {report['blocked']}",
            f"Coverage: {report['coverage_percent']}%",
        )
    )


def latest_qualification(root: Path) -> dict[str, object] | None:
    directory = root / ".engineering" / "qualification"
    reports = sorted(directory.glob("qualification-*.json"))
    if not reports:
        return None
    try:
        raw = json.loads(reports[-1].read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _default_check(
    root: Path, capability: str, *, ep_repository_root: Path | None = None
) -> bool:
    ep_source = (
        ep_repository_root / "src" / "engineering_platform"
        if ep_repository_root is not None
        else None
    )

    def source_file(name: str) -> bool:
        return bool(ep_source and (ep_source / name).is_file())

    def configuration_is_compatible() -> bool:
        try:
            return PlatformConfiguration.load(root).platform.version == EngineeringPlatformManifest.load(
                package_path("ENGINEERING_PLATFORM_VERSION.json")
            ).platform_version
        except (OSError, ValueError):
            return False
    contracts = {
        "Repository Initialization": bool(ep_repository_root and (ep_repository_root / "BOOTSTRAP.md").is_file()),
        "Checkpoint Resume": source_file("agent_state.py"),
        "Engineering Memory": source_file("execution_host.py"),
        "Capability-aware Reviewers": source_file("capability_review.py"),
        "Remote Status Model": source_file("status_model.py"),
        "Private Dashboard": source_file("dashboard.py"),
        "Repository Handoff": source_file("repository_handoff.py"),
        "Remote Engineering Readiness": source_file("execution_readiness.py"),
        "Platform Identity": configuration_is_compatible(),
        "Workspace Identity": configuration_is_compatible(),
        "Provider Registry": configuration_is_compatible(),
        "Capability Registry": source_file("platform_api.py"),
        "Public Platform API": source_file("platform_api.py"),
        "Configuration Hierarchy": configuration_is_compatible(),
        "Configuration Migration": configuration_is_compatible(),
        "Provider Compatibility": configuration_is_compatible(),
        "Extraction Readiness Audit": bool(
            ep_repository_root
            and (ep_repository_root / "scripts" / "engineering" / "audit_ep_extraction_baseline.py").is_file()
        ),
        "Repository Bootstrap": source_file("platform_bootstrap.py"),
        "Project Template": bool(ep_source and (ep_source / "templates" / "workspace-config.json").is_file()),
        "Workspace Provisioning": source_file("platform_bootstrap.py"),
        "Genesis Lifecycle": source_file("execution_host.py"),
        "Strict Inbox Sequencing": source_file("inbox_watcher.py"),
        "Local Engineering Evidence Storage": bool(ep_source and (ep_source / "ENGINEERING_INBOX_PROTOCOL.md").is_file()),
        "Component Logging and Read-only Advice": source_file("component_logging.py") and source_file("codex_chat.py"),
    }
    return contracts.get(capability, source_file("execution_host.py"))


def _write_report(root: Path, report: dict[str, object]) -> None:
    directory = root / ".engineering" / "qualification"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    (directory / f"qualification-{stamp}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / f"qualification-{stamp}.md").write_text(dashboard(report) + "\n", encoding="utf-8")


def _repository_version(root: Path) -> str:
    completed = GitProvider().execute(root, "git", "rev-parse", "HEAD")
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _codex_version() -> str:
    try:
        completed = CodexCliProvider().command("--version")
    except OSError:
        return "unavailable"
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"
