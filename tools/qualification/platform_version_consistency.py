#!/usr/bin/env python3
"""Fail closed on EP source or installed-version projection drift.

Source validation is deliberately static. Installed validation is delegated to
an isolated interpreter so a checkout's ``src`` tree cannot masquerade as the
wheel that was just qualified.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import tomllib


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT invalid JSON {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT invalid JSON root {path}")
    return value


def _field(document: dict[str, object], path: Path, *keys: str) -> str:
    value: object = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT missing {path}:{'.'.join(keys)}")
        value = value[key]
    if not isinstance(value, str):
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT invalid type {path}:{'.'.join(keys)}")
    return value


def _runtime_constant(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT invalid runtime projection {path}") from error
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name) and target.id == "CURRENT_PLATFORM_VERSION" and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(values) != 1:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT invalid runtime constant {path}")
    return values[0]


def _source_projections(root: Path) -> tuple[str, dict[str, str]]:
    try:
        package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = package["project"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError("EP_VERSION_COMPONENT_DRIFT invalid pyproject.toml") from error
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise RuntimeError("EP_VERSION_COMPONENT_DRIFT invalid [project].version")
    source = root / "src" / "engineering_platform"
    manifest = _object(source / "ENGINEERING_PLATFORM_VERSION.json")
    expected = _field(manifest, source / "ENGINEERING_PLATFORM_VERSION.json", "platform_version")
    package_lock = _object(root / "package-lock.json")
    return expected, {
        "package": project["version"], "package_json_root": _field(_object(root / "package.json"), root / "package.json", "version"),
        "package_lock_root": _field(package_lock, root / "package-lock.json", "version"), "package_lock_workspace_root": _field(package_lock, root / "package-lock.json", "packages", "", "version"),
        "manifest_platform": expected, "manifest_runner": _field(manifest, source / "ENGINEERING_PLATFORM_VERSION.json", "runner_version"),
        "manifest_dashboard": _field(manifest, source / "ENGINEERING_PLATFORM_VERSION.json", "dashboard_version"), "manifest_watcher": _field(manifest, source / "ENGINEERING_PLATFORM_VERSION.json", "watcher_version"),
        "platform_configuration": _field(_object(source / "ENGINEERING_PLATFORM_CONFIG.json"), source / "ENGINEERING_PLATFORM_CONFIG.json", "platform", "version"),
        "workspace_template": _field(_object(source / "templates" / "workspace-config.json"), source / "templates" / "workspace-config.json", "platform", "version"),
        "runtime_constant": _runtime_constant(source / "platform_version.py"),
    }


def _installed_projections(root: Path, python: Path) -> dict[str, str]:
    program = '''import importlib.metadata, importlib.resources, json, os, pathlib
import engineering_platform
from engineering_platform import server
from engineering_platform.platform_version import CURRENT_PLATFORM_VERSION, RunnerCompatibility
from engineering_platform.server_console_services import DASHBOARD_VERSION
base = importlib.resources.files("engineering_platform")
manifest = json.loads(base.joinpath("ENGINEERING_PLATFORM_VERSION.json").read_text())
config = json.loads(base.joinpath("ENGINEERING_PLATFORM_CONFIG.json").read_text())
template = json.loads(base.joinpath("templates/workspace-config.json").read_text())
if pathlib.Path(engineering_platform.__file__).resolve().is_relative_to(pathlib.Path(os.environ["EP_SOURCE_ROOT"]).resolve()): raise RuntimeError("installed module was loaded from source checkout")
print(json.dumps({"installed_package": importlib.metadata.version("engineering-platform"), "manifest_platform": manifest["platform_version"], "manifest_runner": manifest["runner_version"], "manifest_dashboard": manifest["dashboard_version"], "manifest_watcher": manifest["watcher_version"], "platform_configuration": config["platform"]["version"], "workspace_template": template["platform"]["version"], "runtime_constant": CURRENT_PLATFORM_VERSION, "runner": RunnerCompatibility().runner_version, "console": DASHBOARD_VERSION, "server": server._console_platform_version()}))'''
    environment = os.environ.copy()
    environment["EP_SOURCE_ROOT"] = str(root.resolve())
    completed = subprocess.run((str(python), "-I", "-c", program), check=False, capture_output=True, text=True, env=environment)
    if completed.returncode:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT installed verification failed: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("EP_VERSION_COMPONENT_DRIFT invalid installed verification result") from error
    if not isinstance(payload, dict) or not all(isinstance(value, str) for value in payload.values()):
        raise RuntimeError("EP_VERSION_COMPONENT_DRIFT invalid installed projection types")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--installed-python", type=Path, help="verify the wheel installed in this isolated interpreter")
    args = parser.parse_args(argv)
    expected, projections = _source_projections(args.source_root.resolve())
    if args.installed_python is not None:
        # Keep a virtualenv launcher path intact: resolving it follows its
        # symlink to the base interpreter and silently defeats isolation.
        projections.update(_installed_projections(args.source_root.resolve(), args.installed_python))
    drift = {name: value for name, value in projections.items() if value != expected}
    if drift:
        raise RuntimeError(f"EP_VERSION_COMPONENT_DRIFT expected={expected} observed={drift}")
    print(f"EP_VERSION_COMPONENTS=PASS version={expected} components={len(projections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
