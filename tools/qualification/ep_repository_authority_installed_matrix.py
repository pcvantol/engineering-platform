"""Qualify the versioned per-repository authority HTTP route from site-packages."""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

import engineering_platform


TEST_MODULE = "tests.engineering.test_repository_authority_readback"
SCHEMA = Path("schemas/repository-consumer-authority-v1.schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    source_root = args.source_root.resolve(strict=True)
    installed_file = Path(engineering_platform.__file__).resolve(strict=True)
    distribution = importlib.metadata.distribution("engineering-platform")
    project = tomllib.loads((source_root / "pyproject.toml").read_text())
    if sys.version_info[:2] != (3, 14):
        raise SystemExit("Repository authority qualification requires Python 3.14")
    if installed_file.is_relative_to(source_root):
        raise SystemExit("Repository authority qualification refused a source import")
    if not installed_file.is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit("Repository authority package is outside the installed environment")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable") is True:
        raise SystemExit("Repository authority qualification refused an editable installation")
    if distribution.version != project["project"]["version"]:
        raise SystemExit("Installed version differs from source")
    source_package = source_root / "src/engineering_platform"
    installed_package = installed_file.parent
    expected_files = {
        path.relative_to(source_package) for path in source_package.rglob("*.py")
        if "__pycache__" not in path.relative_to(source_package).parts
    }
    for pattern in project["tool"]["setuptools"]["package-data"]["engineering_platform"]:
        expected_files.update(
            path.relative_to(source_package) for path in source_package.glob(pattern)
            if path.is_file()
        )
    installed_files = sorted(
        path for path in installed_package.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(installed_package).parts
    )
    if expected_files != {path.relative_to(installed_package) for path in installed_files}:
        raise SystemExit("Installed package inventory differs from source")
    manifest = []
    for installed_path in installed_files:
        relative = installed_path.relative_to(installed_package)
        digest = sha256(installed_path.read_bytes()).hexdigest()
        if sha256((source_package / relative).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Installed package differs from source: {relative}")
        manifest.append(f"{relative.as_posix()} {digest}")
    package_digest = sha256("\n".join(manifest).encode()).hexdigest()
    source_revision = subprocess.run(
        ("git", "-C", str(source_root), "rev-parse", "HEAD"),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_tree = subprocess.run(
        ("git", "-C", str(source_root), "rev-parse", "HEAD^{tree}"),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_dirty = bool(subprocess.run(
        ("git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=all"),
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    if args.require_clean and source_dirty:
        raise SystemExit("Exact-source authority qualification requires a clean checkout")

    # Tests are committed source; product imports were already fixed to the
    # non-editable installed package before the source test path is added.
    sys.path.insert(0, str(source_root))
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE)
    count = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    fixture_case = importlib.import_module(TEST_MODULE).RepositoryAuthorityHTTPTest(
        "test_two_repositories_have_independent_stable_pinnable_authority"
    )
    fixture_case.setUp()
    try:
        responses = {
            repository_id: fixture_case._read(repository_id)
            for repository_id in ("repository-a", "repository-b")
        }
    finally:
        fixture_case.tearDown()
    readbacks = {
        repository_id: response[1] for repository_id, response in responses.items()
        if response[0] == 200
    }
    receipt = {
        "contract_version": "ep-repository-authority-installed-matrix/v1",
        "status": "PASS" if count == 8 and result.testsRun == count and result.wasSuccessful()
                  and len(readbacks) == 2 else "FAIL",
        "source_revision": source_revision,
        "source_tree": source_tree,
        "source_dirty": source_dirty,
        "installed_module": str(installed_file),
        "installed_package_manifest_sha256": package_digest,
        "schema_sha256": sha256((installed_package / SCHEMA).read_bytes()).hexdigest(),
        "version": distribution.version,
        "test_count": result.testsRun,
        "synthetic_http_readbacks": readbacks,
        "scope": "EP-owned authenticated per-repository authority readback only",
        "forge_multi_repository_acceptance": "NOT_EVALUATED",
    }
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
