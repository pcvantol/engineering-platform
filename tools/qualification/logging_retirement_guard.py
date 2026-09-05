"""Static qualification guard for CENTRAL-only operational logging."""
from __future__ import annotations

import argparse
from pathlib import Path


def violations(source_root: Path) -> list[str]:
    package = source_root / "engineering_platform"
    logger = (package / "component_logging.py").read_text(encoding="utf-8")
    executor = (package / "execution_executor.py").read_text(encoding="utf-8")
    console = (package / "server_console_services.py").read_text(encoding="utf-8")
    agent = (package / "project_agent_service.py").read_text(encoding="utf-8")
    findings: list[str] = []
    if "INSERT INTO engineering_component_logs" not in logger or "sys.stderr.write" not in logger:
        findings.append("CENTRAL_LOG_WRITER_MISSING")
    if ".engineering\" / \"logs\" / \"codex" in executor or "write_redacted_codex_cli_log" in executor:
        findings.append("ACTIVE_LOCAL_CODEX_LOG_WRITER")
    if ".engineering\" / \"logs\" / \"codex" in console or ".glob(\"*.log\")" in console:
        findings.append("ACTIVE_LOCAL_CODEX_LOG_READER")
    if 'return self.log_dir / "agent.stdout.log"' in agent or 'return self.log_dir / "agent.stderr.log"' in agent:
        findings.append("LAUNCHAGENT_PERSISTENT_LOG_PATH")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    findings = violations(parser.parse_args(argv).source_root.resolve())
    if findings:
        print("\n".join(findings)); return 1
    print("CANONICAL_OPERATIONAL_LOG_AUTHORITY=CENTRAL")
    print("ACTIVE_LOCAL_PERSISTENT_LOG_FALLBACKS=0")
    print("ACTIVE_LEGACY_PERSISTENT_LOG_WRITERS=0")
    print("ACTIVE_LEGACY_PERSISTENT_LOG_READERS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
