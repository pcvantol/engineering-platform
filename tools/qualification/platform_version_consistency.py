#!/usr/bin/env python3
"""Fail closed when a public Engineering Platform version projection drifts."""

from __future__ import annotations

import argparse
from importlib.metadata import version as installed_version
import json
from pathlib import Path
import sys
import tomllib


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.source_root.resolve()
    source = root / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    from engineering_platform import server
    from engineering_platform.platform_api import PlatformConfiguration
    from engineering_platform.platform_version import (
        CURRENT_PLATFORM_VERSION,
        EngineeringPlatformManifest,
        RunnerCompatibility,
    )
    from engineering_platform.server_console_services import DASHBOARD_VERSION

    manifest_path = source / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["platform_version"]
    package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    configuration = json.loads((source / "engineering_platform" / "ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"))
    template = json.loads((source / "engineering_platform" / "templates" / "workspace-config.json").read_text(encoding="utf-8"))

    projections = {
        "package": package["project"]["version"],
        "installed_package": installed_version("engineering-platform"),
        "manifest_platform": manifest["platform_version"],
        "manifest_runner": manifest["runner_version"],
        "manifest_dashboard": manifest["dashboard_version"],
        "manifest_watcher": manifest["watcher_version"],
        "platform_configuration": configuration["platform"]["version"],
        "workspace_template": template["platform"]["version"],
        "runtime_constant": CURRENT_PLATFORM_VERSION,
        "runner": RunnerCompatibility().runner_version,
        "console": DASHBOARD_VERSION,
        "server": server._console_platform_version(),
        "platform_api": PlatformConfiguration.load(root).platform.version,
        "installed_manifest": EngineeringPlatformManifest.load(manifest_path).platform_version,
    }
    drift = {name: value for name, value in projections.items() if value != expected}
    if drift:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT expected={expected} observed={drift}")
    print(f"EP_VERSION_COMPONENTS=PASS version={expected} components={len(projections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
