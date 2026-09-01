"""Generate reviewable, sanitized repository run-handoff records."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path

from .agent_state import redact_diagnostic


def publish(
    root: Path,
    *,
    run_id: str,
    platform_version: str,
    implementation_pr: int,
    finalization_pr: int,
    objective: str = "Engineering transaction",
) -> Path:
    """Publish only durable completion metadata after a successful finalization."""
    directory = root / "docs" / "engineering" / "runs" / str(datetime.now(timezone.utc).year)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    relative = directory / f"{stamp.date().isoformat()}-{run_id}.md"
    body = "\n".join(
        (
            "# Engineering Run Handoff",
            "",
            f"- Engineering Platform: `{platform_version}`",
            f"- Run ID: `{run_id}`",
            f"- Completed: `{stamp.isoformat()}`",
            f"- Objective: {redact_diagnostic(objective)}",
            f"- Implementation PR: `#{implementation_pr}`",
            f"- Finalization PR: `#{finalization_pr}`",
            "- Repository State: `MERGED_RECONCILED`",
            "- Workspace State: `WORKSPACE_READY`",
            "",
            "## Platform Architect Handoff",
            "",
            "- Recommended next capability: repository-evidence-based selection by the Product & Platform Architect.",
            "- Architect attention: no remote authority, release, deployment or publication authority was added.",
            "- Qualification summary: validate the completed increment before relying on it.",
            "",
        )
    )
    relative.write_text(body, encoding="utf-8")
    latest = root / "docs" / "engineering" / "runs" / "latest.md"
    latest.write_text(
        f"# Latest Engineering Run\n\n- Run ID: `{run_id}`\n- Engineering Platform: `{platform_version}`\n- Implementation PR: `#{implementation_pr}`\n- Finalization PR: `#{finalization_pr}`\n- Repository State: `MERGED_RECONCILED`\n- Workspace State: `WORKSPACE_READY`\n- Handoff: `{relative.relative_to(root)}`\n",
        encoding="utf-8",
    )
    index = {
        "schema_version": 1,
        "latest_run_id": run_id,
        "completed_at": stamp.isoformat(),
        "platform_version": platform_version,
        "report_path": str(relative.relative_to(root)),
        "implementation_pr": implementation_pr,
        "finalization_pr": finalization_pr,
        "repository_state": "MERGED_RECONCILED",
        "workspace_state": "WORKSPACE_READY",
    }
    (root / "docs" / "engineering" / "runs" / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return relative


def main(argv: list[str] | None = None) -> int:
    """Write the handoff record on the Finalization PR branch before merge."""
    parser = argparse.ArgumentParser(
        prog="engineering-repository-handoff",
        description="Publish durable Engineering Platform Finalization handoff records.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--platform-version", required=True)
    parser.add_argument("--implementation-pr", required=True, type=int)
    parser.add_argument("--finalization-pr", required=True, type=int)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    handoff = publish(
        args.root.resolve(),
        run_id=args.run_id,
        platform_version=args.platform_version,
        implementation_pr=args.implementation_pr,
        finalization_pr=args.finalization_pr,
    )
    print(handoff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
