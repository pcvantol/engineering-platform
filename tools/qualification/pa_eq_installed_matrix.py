"""Run the EP-owned PA-EQ matrix against a non-editable installed wheel."""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

import engineering_platform


TESTS: dict[str, tuple[str, ...]] = {
    "tests.engineering.test_parallel_action_qualification.ParallelActionInstalledQualificationTest.test_controlled_provider_refuses_ambient_external_write_flow":
        ("PA-17", "PA-26-EP"),
    "tests.engineering.test_parallel_action_qualification.ParallelActionInstalledQualificationTest.test_forge_http_to_installed_ep_two_provider_processes_and_collection":
        ("PA-04", "PA-05", "PA-08", "PA-17", "PA-22-EP", "PA-23", "PA-26-EP"),
    "tests.engineering.test_parallel_action_compat.ParallelActionCompatibilityTests.test_dependency_evidence_and_cycle_fail_closed":
        ("PA-03", "PA-06"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_exact_predecessor_evidence_and_wrong_scope":
        ("PA-05", "PA-06", "PA-08"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_graph_change_and_consumer_scope_fail_closed":
        ("PA-03", "PA-17", "PA-21"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_failed_action_retry_keeps_one_intake_and_requires_operator_lineage":
        ("PA-21",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e2_claims_two_repositories_and_fences_duplicate_and_dependency":
        ("PA-16",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e2_origin_alias_waits_and_readback_distinguishes_capacity":
        ("PA-14", "PA-15-EP", "PA-16"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e2_resource_identity_rejects_unqualified_origin_and_git_failure":
        ("PA-15-EP", "PA-17"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e2_waits_behind_active_legacy_project_gate":
        ("EP-LEGACY-SERIAL-GATE",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e3_new_process_restart_requires_a_returned_checkpoint":
        ("PA-13", "PA-21"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e3_exact_cancel_before_runner_releases_only_that_action":
        ("PA-18",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e3_uncertain_and_cancel_pending_block_operator_release":
        ("PA-18", "PA-20-EP"),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e4_graph_collection_and_same_snapshot_exports":
        ("PA-22-EP",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e4_measured_overlap_and_partial_usage":
        ("PA-23",),
    "tests.engineering.test_parallel_action_admission.ParallelActionAdmissionTest.test_pa_e4_running_labels_and_conflicting_clocks_do_not_prove_overlap":
        ("PA-23",),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    source_root = args.source_root.resolve(strict=True)
    installed_file = Path(engineering_platform.__file__).resolve(strict=True)
    distribution = importlib.metadata.distribution("engineering-platform")
    expected_version = tomllib.loads((source_root / "pyproject.toml").read_text())[
        "project"]["version"]
    if sys.version_info[:2] != (3, 14):
        raise SystemExit("PA-EQ requires Python 3.14")
    if installed_file.is_relative_to(source_root):
        raise SystemExit("PA-EQ refused a source checkout import")
    if not installed_file.is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit("PA-EQ package is outside the active installed environment")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable") is True:
        raise SystemExit("PA-EQ refused an editable installation")
    if distribution.version != expected_version:
        raise SystemExit("PA-EQ installed version disagrees with source")
    source_package = source_root / "src/engineering_platform"
    installed_package = installed_file.parent
    installed_files = sorted(
        path for path in installed_package.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(installed_package).parts
    )
    if not installed_files:
        raise SystemExit("PA-EQ installed package has no files")
    expected_files = {path.relative_to(source_package)
                      for path in source_package.rglob("*.py")
                      if "__pycache__" not in path.relative_to(source_package).parts}
    package_patterns = tomllib.loads((source_root / "pyproject.toml").read_text())[
        "tool"]["setuptools"]["package-data"]["engineering_platform"]
    for pattern in package_patterns:
        expected_files.update(path.relative_to(source_package)
                              for path in source_package.glob(pattern) if path.is_file())
    installed_inventory = {path.relative_to(installed_package) for path in installed_files}
    if expected_files != installed_inventory:
        raise SystemExit("PA-EQ installed package inventory differs from source")
    package_manifest = []
    for installed_path in installed_files:
        relative = installed_path.relative_to(installed_package)
        source_path = source_package / relative
        installed_hash = sha256(installed_path.read_bytes()).hexdigest()
        if not source_path.is_file() or sha256(source_path.read_bytes()).hexdigest() != installed_hash:
            raise SystemExit(f"PA-EQ installed package differs from source: {relative}")
        package_manifest.append(f"{relative.as_posix()} {installed_hash}")
    package_digest = sha256("\n".join(package_manifest).encode()).hexdigest()
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
        raise SystemExit("PA-EQ exact-source qualification requires a clean checkout")

    # Test modules and the published Forge fixture are read from the committed
    # source tree. Product imports continue to resolve through the installed
    # engineering_platform package already loaded above.
    sys.path.insert(0, str(source_root))
    fixture = source_root / "tests/fixtures/forge-parallel-action-peer-graph-v1.json"
    fixture_digest = sha256(fixture.read_bytes()).hexdigest()
    if fixture_digest != "b938388fb7a031c574407b62f07cb3ed7b12d692170dd4acf81cba5a14a3ab9c":
        raise SystemExit("PA-EQ Forge PA-F0 fixture digest changed")
    suite = unittest.defaultTestLoader.loadTestsFromNames(tuple(TESTS))
    count = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    receipt = {
        "contract_version": "ep-pa-eq-installed-matrix/v1",
        "status": "PASS" if result.wasSuccessful() and result.testsRun == count else "FAIL",
        "source_root": str(source_root),
        "source_revision": source_revision,
        "source_tree": source_tree,
        "source_dirty": source_dirty,
        "installed_module": str(installed_file),
        "installed_package_manifest_sha256": package_digest,
        "version": distribution.version,
        "forge_fixture_sha256": fixture_digest,
        "test_count": result.testsRun,
        "test_cases": TESTS,
        "qualification_scope": "EP-owned controlled-provider and HTTP boundary only",
        "forge_planner_and_live_provider_qualification": "NOT_EVALUATED",
    }
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
