"""Reject unclassified local operational-storage authority in product source.

This is deliberately a small packaging/source gate rather than a grep
allowlist.  A module that retains a root-storage primitive must be named here
with its bounded classification; a newly added product module cannot inherit
that exception.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


# Exact source paths only.  These are retained compatibility implementations,
# not an exemption for a directory or import tree.
CLASSIFIED_FALLBACKS = {
    "agent_state.py": "CENTRAL_BOUND_STATESTORE_IMPLEMENTATION",
    "codex_chat.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "component_logging.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "dashboard.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "dashboard_configuration.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "dashboard_state.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "emergency_recovery.py": "HISTORICAL_COMPATIBILITY_ONLY",
    "execution_activity.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "execution_executor.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "execution_host.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "execution_lease.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "execution_lifecycle.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "execution_timing.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "host_preflight.py": "RETIRED_DIRECT_HOST_COMPATIBILITY",
    "live_status.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "local_api.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "local_api_credentials.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "managed_autonomy.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "parity_lifecycle_dispatcher.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "pr_evidence_backfill.py": "FORENSIC_MIGRATION_ONLY",
    "prompt_history.py": "CENTRAL_BOUND_EVIDENCE_IMPLEMENTATION",
    "platform_bootstrap.py": "RETIRED_WORKSPACE_COMPATIBILITY",
    "provider_interruption.py": "HISTORICAL_COMPATIBILITY_ONLY",
    "provider_recovery.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "provider_usage.py": "CENTRAL_BOUND_EXECUTION_IMPLEMENTATION",
    "status_model.py": "P_CENTRAL_CONSOLE_COMPATIBILITY",
    "storage.py": "HISTORICAL_FORENSIC_STORAGE_IMPLEMENTATION",
    "telemetry.py": "HISTORICAL_COMPATIBILITY_ONLY",
    "worktree_provenance.py": "FORENSIC_MIGRATION_ONLY",
}

FORBIDDEN_MARKERS = ("open_storage(", "StateStore(", "engineering-runs", "ep-server.db")


def violations(source_root: Path) -> list[str]:
    package = source_root / "engineering_platform"
    findings: list[str] = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in FORBIDDEN_MARKERS):
            continue
        classification = CLASSIFIED_FALLBACKS.get(path.name)
        if classification is None:
            findings.append(f"UNCLASSIFIED_OPERATIONAL_STORAGE:{path.relative_to(source_root)}")
    host = (package / "execution_host.py").read_text(encoding="utf-8")
    if 'raise SystemExit("CENTRAL_OPERATIONAL_DATABASE_REQUIRED")' not in host:
        findings.append("DIRECT_HOST_CENTRAL_DATABASE_GATE_MISSING")
    if (package / "inbox_watcher.py").exists():
        findings.append("RETIRED_WATCHER_RUNTIME_PACKAGED")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    findings = violations(args.source_root.resolve())
    if findings:
        print("\n".join(findings))
        return 1
    print("P_CENTRAL_CORE_SOURCE_AUTHORITY_GUARD=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
