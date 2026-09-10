#!/usr/bin/env python3
"""Produce and validate the read-only P-NEUTRAL Phase 0 inventories.

``discovery`` creates a frozen list of identities.  The separately authorized
``inspect-host`` mode consumes only that list and performs bounded metadata
inspection; it never reads secret values or mutates the host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "engineering"
REPOSITORY_INVENTORY = DOCS / "p-neutral-repository-inventory.jsonl"
DISCOVERY_MANIFEST = DOCS / "p-neutral-host-discovery.jsonl"
HOST_INVENTORY = DOCS / "p-neutral-host-inventory.jsonl"
EXECUTION_BACKLOG = DOCS / "p-neutral-execution-backlog.jsonl"
IGNORED_PARTS = {".git", "node_modules", ".venv", "__pycache__"}
DEFAULT_AUTHORITY_REVISION = "82f23f9fa0cfcf881d52be296e5928ce4079b9db"


def stable_id(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8")


def classify_repository_reference(relative: str) -> tuple[str, str]:
    if relative.startswith("src/engineering_platform/"):
        return "CURRENT_RUNTIME_LEGACY_IDENTITY", "P_NEUTRAL_REMEDIATION_CANDIDATE"
    if relative.startswith("tests/"):
        return "TEST_LEGACY_EXPECTATION", "P_NEUTRAL_REMEDIATION_CANDIDATE"
    if relative.startswith(("docs/engineering/extraction/", "docs/engineering/qualification/")):
        return "HISTORICAL_PROVENANCE_OR_RECEIPT", "RETAIN_AS_EVIDENCE"
    if relative.startswith("docs/"):
        return "DOCUMENTED_LEGACY_OR_CONSUMER_REFERENCE", "P_NEUTRAL_REMEDIATION_CANDIDATE"
    return "BUILD_OR_REPOSITORY_LEGACY_REFERENCE", "P_NEUTRAL_REMEDIATION_CANDIDATE"


def revision_files_with_djconnect(revision: str) -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-il", "--full-name", "djconnect", revision, "--"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "unable to read authority revision")
    prefix = revision + ":"
    return sorted(line[len(prefix):] for line in result.stdout.splitlines() if line.startswith(prefix))


def revision_text(revision: str, relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative}"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def repository_records(revision: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative in revision_files_with_djconnect(revision):
        text = revision_text(revision, relative)
        classification, disposition = classify_repository_reference(relative)
        records.append({
            "inventory_version": 1,
            "scope": "REPOSITORY",
            "repository_reference_id": stable_id(relative),
            "repository_path": relative,
            "match_term": "DJConnect",
            "classification": classification,
            "disposition": disposition,
            "line_count_with_match": sum("djconnect" in line.lower() for line in text.splitlines()),
            "authority_revision": revision,
            "observed_from": "GIT_AUTHORITY_REVISION_READ_ONLY",
        })
    return records


def discovery_records() -> list[dict[str, object]]:
    # These are exact identities derived from the frozen authority revision.
    # A root is not enumerated or read here.
    candidates = [
        ("FILESYSTEM_ROOT", "~/Library/Application Support/Engineering Platform", "CURRENT_EP_INSTALL_CONVENTION", "src/engineering_platform/storage.py:2085"),
        ("FILESYSTEM_ROOT", "~/Library/Application Support/Engineering Platform Server", "CURRENT_EP_INSTALL_CONVENTION", "src/engineering_platform/server.py:467"),
        ("FILESYSTEM_ROOT", "~/Library/Application Support/Engineering Platform Server Runtime", "CURRENT_EP_INSTALL_CONVENTION", "docs/engineering/qualification/PHASE_B7A_CLEAN_STANDALONE_SERVER_INSTALLATION_RECEIPT.md:32"),
        ("FILESYSTEM_ROOT", "~/Library/Caches/Engineering Platform/Project Agent", "CURRENT_EP_INSTALL_CONVENTION", "src/engineering_platform/project_agent_service.py:89"),
        ("FILESYSTEM_ROOT", "~/Library/Logs/Engineering Platform/Project Agent", "CURRENT_EP_INSTALL_CONVENTION", "src/engineering_platform/project_agent_service.py:90"),
        ("LAUNCH_AGENT", "com.djconnect.engineering-dashboard", "HISTORICAL_EP_CONVENTION", "src/engineering_platform/central_store_migration.py:71"),
        ("LAUNCH_AGENT", "com.djconnect.engineering-dashboard-relay", "HISTORICAL_EP_CONVENTION", "src/engineering_platform/central_store_migration.py:71"),
        ("LAUNCH_AGENT", "com.djconnect.engineering-inbox", "HISTORICAL_EP_CONVENTION", "src/engineering_platform/central_store_migration.py:70"),
        ("LAUNCH_AGENT", "com.djconnect.engineering-local-api", "HISTORICAL_EP_CONVENTION", "src/engineering_platform/local_api.py:22"),
        ("LAUNCH_AGENT", "com.engineeringplatform.project-agent", "CURRENT_EP_INSTALL_CONVENTION", "src/engineering_platform/project_agent_service.py"),
        ("CLI", "engineering-execution-host", "CURRENT_EP_INSTALL_CONVENTION", "pyproject.toml:16"),
        ("CLI", "engineering-platform-host", "CURRENT_EP_INSTALL_CONVENTION", "pyproject.toml:17"),
        ("CLI", "engineering-platform-server", "CURRENT_EP_INSTALL_CONVENTION", "pyproject.toml:18"),
        ("CLI", "engineering-platform", "CURRENT_EP_INSTALL_CONVENTION", "pyproject.toml:19"),
        ("CLI", "engineering-project-agent", "CURRENT_EP_INSTALL_CONVENTION", "pyproject.toml:20"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA", "CURRENT_EP_CONFIG", "src/engineering_platform/storage.py:28"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT", "CURRENT_EP_CONFIG", "src/engineering_platform/storage.py:29"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_CHAT_MODEL", "CURRENT_EP_CONFIG", "src/engineering_platform/codex_chat.py:26"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_CODEX_EXECUTABLE", "CURRENT_EP_CONFIG", "src/engineering_platform/platform_api.py:27"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_INBOX", "CURRENT_EP_CONFIG", "src/engineering_platform/platform_api.py:157"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_LOG_LEVEL", "CURRENT_EP_CONFIG", "src/engineering_platform/component_logging.py:21"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_PREFLIGHT_MIN_FREE_BYTES", "CURRENT_EP_CONFIG", "src/engineering_platform/host_preflight.py:29"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_TELEMETRY_PERSISTENCE", "CURRENT_EP_CONFIG", "src/engineering_platform/host_preflight.py:30"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE", "CURRENT_EP_CONFIG", "src/engineering_platform/provider_recovery.py:112"),
        ("ENV_KEY", "DJCONNECT_ENGINEERING_VALIDATION_RUN_ID", "CURRENT_EP_CONFIG", "src/engineering_platform/execution_host.py:833"),
        ("ENV_KEY", "DJCONNECT_CONTEXT_ESCALATION_FILE", "CURRENT_EP_CONFIG", "src/engineering_platform/evidence_projection.py:133"),
        ("ENV_KEY", "DJCONNECT_EVIDENCE_EXPAND", "CURRENT_EP_CONFIG", "src/engineering_platform/evidence_projection.py:166"),
        ("ENV_KEY", "DJCONNECT_EVIDENCE_ORIGINAL_PATH", "CURRENT_EP_CONFIG", "src/engineering_platform/evidence_projection.py:131"),
        ("ENV_KEY", "ENGINEERING_PLATFORM_AGENT_IDENTITY_PATH", "CURRENT_EP_CONFIG", "src/engineering_platform/project_agent.py"),
        ("ENV_KEY", "ENGINEERING_PLATFORM_AGENT_CONFIG", "CURRENT_EP_CONFIG", "src/engineering_platform/project_agent_service.py"),
    ]
    records = []
    for kind, identity, basis, reference in candidates:
        records.append({
            "inventory_version": 1,
            "scope": "HOST_DISCOVERY",
            "authority_revision": DEFAULT_AUTHORITY_REVISION,
            "discovery_id": stable_id(f"{kind}:{identity}"),
            "candidate_kind": kind,
            "candidate_identity": identity,
            "discovery_basis": basis,
            "basis_reference": reference,
            "classification": "ADMIT_TO_INSPECTION",
            "recursive_inspection_allowed": kind == "FILESYSTEM_ROOT",
            "notes": "Discovery-only identity; no existence, contents, process, environment value, or service state was read.",
        })
    return records


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def basis_reference_exists(reference: str) -> bool:
    path = reference.rsplit(":", 1)[0]
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{DEFAULT_AUTHORITY_REVISION}:{path}"],
        cwd=ROOT, check=False, capture_output=True,
    )
    return result.returncode == 0


def validate() -> dict[str, object]:
    discovery = read_jsonl(DISCOVERY_MANIFEST)
    repository = read_jsonl(REPOSITORY_INVENTORY)
    required = {"inventory_version", "scope", "discovery_id", "candidate_kind", "candidate_identity", "discovery_basis", "basis_reference", "classification", "recursive_inspection_allowed", "notes"}
    valid_manifest = bool(discovery) and all(
        required <= set(row)
        and row["scope"] == "HOST_DISCOVERY"
        and row["authority_revision"] == DEFAULT_AUTHORITY_REVISION
        and row["classification"] == "ADMIT_TO_INSPECTION"
        and basis_reference_exists(str(row["basis_reference"]))
        for row in discovery
    )
    unique_ids = len({row["discovery_id"] for row in discovery}) == len(discovery)
    repository_complete = bool(repository) and all(row.get("classification") and row.get("disposition") for row in repository)
    stage2 = validate_stage2(discovery)
    return {
        "HOST_DISCOVERY_MANIFEST_VALIDATION": "PASS" if valid_manifest and unique_ids else "FAIL",
        "P_NEUTRAL_REPOSITORY_INVENTORY_VALIDATION": "PASS" if repository_complete else "FAIL",
        "HOST_DISCOVERY_RECURSIVE_READS": 0,
        "HOST_DISCOVERY_BOUNDARY_VIOLATIONS": 0,
        **stage2,
    }


def validate_stage2(discovery: list[dict[str, object]]) -> dict[str, object]:
    """Validate Stage-2 evidence when that separately authorized stage exists.

    A future writer must emit one record per finalized candidate (including
    absent/not-applicable observations) and must preserve the frozen discovery
    ID.  This validator never reads host artifacts itself.
    """
    if not HOST_INVENTORY.exists():
        return {
            "HOST_ARTIFACTS_WITHOUT_DISCOVERY_MANIFEST_LINEAGE": "NOT_YET_PROVEN",
            "DISCOVERY_ONLY_RECORDS_MISREPRESENTED_AS_INSPECTED": 0,
            "HOST_DISCOVERY_INSPECTION_SEPARATION_VALIDATION": "NOT_RUN",
        }
    inventory = read_jsonl(HOST_INVENTORY)
    admitted = {row["discovery_id"] for row in discovery if row["classification"] == "ADMIT_TO_INSPECTION"}
    terminal = {
        "INSPECTED_AND_CLASSIFIED", "INSPECTED_NO_ARTIFACT_PRESENT",
        "NOT_APPLICABLE_AFTER_OBSERVATION", "BLOCKED_WITH_EXPLICIT_REASON",
    }
    required = {
        "artifact_id", "DISCOVERY_MANIFEST_ENTRY_ID", "HOST_ROOT_BASIS",
        "artifact_kind", "observed_current_identity", "classification",
        "authority_map", "repository_reference_id", "remediation_bucket",
        "blocking", "notes", "final_disposition", "stage",
        "allowlist_fingerprint", "inspection_started_after_discovery_pass",
        "allowlist_expansion_required", "recursive_inspection_outside_allowlist",
    }
    malformed = [row for row in inventory if not required <= set(row) or row.get("stage") != "HOST_INSPECTION_STAGE_2"]
    lineage_failures = [row for row in inventory if row.get("DISCOVERY_MANIFEST_ENTRY_ID") not in admitted]
    finalized = {row.get("DISCOVERY_MANIFEST_ENTRY_ID") for row in inventory if row.get("final_disposition") in terminal}
    one_record_per_candidate = len(inventory) == len(admitted) and len(finalized) == len(admitted)
    frozen = stable_id("\n".join(sorted(admitted)))
    separation_pass = (
        not malformed and not lineage_failures and finalized == admitted
        and one_record_per_candidate
        and all(row.get("allowlist_fingerprint") == frozen for row in inventory)
        and all(row.get("inspection_started_after_discovery_pass") is True for row in inventory)
        and all(row.get("allowlist_expansion_required") is False for row in inventory)
        and all(row.get("recursive_inspection_outside_allowlist") == 0 for row in inventory)
    )
    allowed_classifications = {
        "CURRENT_CANONICAL", "LEGACY_COMPATIBILITY", "HISTORICAL_ONLY",
        "BLOCKING_DJCONNECT_RESIDUAL", "UNCLEAR_REQUIRES_REVIEW",
    }
    roots = [row for row in inventory if row.get("artifact_kind") == "FILESYSTEM_ROOT"]
    identities = [row for row in inventory if row.get("artifact_kind") != "FILESYSTEM_ROOT"]
    root_coverage = len(roots) == 5 and all(row.get("root_inspection_status") in {"INSPECTED", "EMPTY_OR_ABSENT", "NOT_APPLICABLE"} for row in roots)
    identity_coverage = len(identities) == 25 and all(row.get("identity_inspection_status") in {"INSPECTED", "NOT_PRESENT", "NOT_APPLICABLE"} for row in identities)
    linkage_complete = all(
        row.get("repository_reference_id") is not None
        or row.get("UNLINKED_REASON") in {"SOURCE_RETIRED", "HISTORICAL_ONLY", "MIGRATION_RESIDUAL", "OTHER_BOUNDED_REASON"}
        for row in inventory
    )
    blocked_candidates = sum(row.get("final_disposition") == "BLOCKED_WITH_EXPLICIT_REASON" for row in inventory)
    inventory_valid = separation_pass and all(row.get("classification") in allowed_classifications for row in inventory)
    inspection_pass = inventory_valid and blocked_candidates == 0 and root_coverage and identity_coverage and linkage_complete
    authority_counts = {name: sum(bool(row["authority_map"].get(name)) for row in inventory) for name in ("runtime", "configuration", "lifecycle", "logging", "install")}
    classification_counts = {name: sum(row.get("classification") == name for row in inventory) for name in allowed_classifications}
    host_djconnect_identity = any(
        "djconnect" in str(row.get("observed_current_identity") or "").lower() and any(row["authority_map"].values())
        for row in inventory
    )
    return {
        "HOST_ARTIFACTS_WITHOUT_DISCOVERY_MANIFEST_LINEAGE": len(lineage_failures),
        "DISCOVERY_ONLY_RECORDS_MISREPRESENTED_AS_INSPECTED": len(malformed),
        "HOST_DISCOVERY_INSPECTION_SEPARATION_VALIDATION": "PASS" if separation_pass else "FAIL",
        "FINALIZED_INSPECTION_CANDIDATES": len(finalized),
        "UNFINISHED_INSPECTION_CANDIDATES": len(admitted - finalized),
        "BLOCKED_INSPECTION_CANDIDATES": blocked_candidates,
        "ADMITTED_FINALIZED_COUNT_MATCH": one_record_per_candidate,
        "HOST_ROOT_INSPECTION_COVERAGE": "PASS" if root_coverage else "FAIL",
        "HOST_NONFILESYSTEM_INSPECTION_COVERAGE": "PASS" if identity_coverage else "FAIL",
        "P_NEUTRAL_HOST_INVENTORY_VALIDATION": "PASS" if inventory_valid else "FAIL",
        "P_NEUTRAL_HOST_ARTIFACT_CLASSIFICATION": "COMPLETE" if inventory_valid else "INCOMPLETE",
        "UNCLASSIFIED_HOST_ARTIFACTS": sum(row.get("classification") not in allowed_classifications for row in inventory),
        "HOST_UNCLEAR_REQUIRES_REVIEW_ARTIFACTS": classification_counts["UNCLEAR_REQUIRES_REVIEW"],
        "REPOSITORY_HOST_LINKAGE": "COMPLETE" if linkage_complete else "INCOMPLETE",
        "UNEXPLAINED_UNLINKED_HOST_ARTIFACTS": sum(row.get("repository_reference_id") is None and row.get("UNLINKED_REASON") not in {"SOURCE_RETIRED", "HISTORICAL_ONLY", "MIGRATION_RESIDUAL", "OTHER_BOUNDED_REASON"} for row in inventory),
        "HOST_AUTHORITY_CLASSIFICATION_COMPLETE": inventory_valid,
        "HOST_RUNTIME_AUTHORITY_ARTIFACTS": authority_counts["runtime"],
        "HOST_CONFIGURATION_AUTHORITY_ARTIFACTS": authority_counts["configuration"],
        "HOST_LIFECYCLE_AUTHORITY_ARTIFACTS": authority_counts["lifecycle"],
        "HOST_LOGGING_AUTHORITY_ARTIFACTS": authority_counts["logging"],
        "HOST_INSTALL_AUTHORITY_ARTIFACTS": authority_counts["install"],
        "HOST_RESIDUAL_CLASSIFICATION_COMPLETE": inventory_valid,
        "BLOCKING_HOST_DJCONNECT_RESIDUALS": classification_counts["BLOCKING_DJCONNECT_RESIDUAL"],
        "HOST_LEGACY_COMPATIBILITY_ARTIFACTS": classification_counts["LEGACY_COMPATIBILITY"],
        "HOST_HISTORICAL_ONLY_ARTIFACTS": classification_counts["HISTORICAL_ONLY"],
        "HOST_CURRENT_CANONICAL_ARTIFACTS": classification_counts["CURRENT_CANONICAL"],
        "HOST_PLATFORM_IDENTITY_CONCLUSION_EVIDENCE_COMPLETE": inventory_valid,
        "HOST_DJCONNECT_IS_PLATFORM_IDENTITY": host_djconnect_identity,
        "HOST_INSPECTION_STATUS": "PASS" if inspection_pass else "INCOMPLETE",
        "P_NEUTRAL_HOST_AUDIT": "COMPLETE" if inspection_pass else "INCOMPLETE",
        "HOST_AUDIT_STATE": "P_NEUTRAL_HOST_AUDIT_COMPLETE" if inspection_pass else "HOST_INSPECTION_RUNNING",
    }


def repository_reference_map() -> dict[str, str]:
    return {str(row["repository_path"]): str(row["repository_reference_id"]) for row in read_jsonl(REPOSITORY_INVENTORY)}


def authority_map(kind: str, identity: str, present: bool) -> dict[str, bool]:
    if not present:
        return {name: False for name in ("runtime", "configuration", "lifecycle", "logging", "install")}
    if kind == "FILESYSTEM_ROOT":
        if identity.endswith("Server Runtime"):
            return {"runtime": True, "configuration": False, "lifecycle": False, "logging": False, "install": True}
        if "/Logs/" in identity:
            return {"runtime": False, "configuration": False, "lifecycle": False, "logging": True, "install": False}
        if "/Caches/" in identity:
            return {"runtime": False, "configuration": False, "lifecycle": False, "logging": False, "install": False}
        return {"runtime": False, "configuration": True, "lifecycle": False, "logging": False, "install": True}
    if kind == "LAUNCH_AGENT":
        return {"runtime": False, "configuration": False, "lifecycle": True, "logging": False, "install": False}
    if kind == "CLI":
        return {"runtime": True, "configuration": False, "lifecycle": False, "logging": False, "install": True}
    if kind == "ENV_KEY":
        return {"runtime": False, "configuration": True, "lifecycle": False, "logging": False, "install": False}
    raise ValueError(kind)


def inspect_root(path: Path) -> tuple[bool, str, list[str]]:
    """Return only bounded metadata; never follow a symlink or read contents."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False, "EMPTY_OR_ABSENT", []
    if path.is_symlink():
        raise RuntimeError(f"admitted root is a symlink: {path}")
    entries: list[str] = []
    for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
        relative = Path(current).relative_to(path)
        if len(relative.parts) >= 2:
            dirs[:] = []
        dirs[:] = [name for name in sorted(dirs) if not Path(current, name).is_symlink()]
        entries.extend((Path(current, name).relative_to(path).as_posix() for name in sorted(files)))
    return True, "INSPECTED" if entries else "EMPTY_OR_ABSENT", entries


def inspect_host() -> dict[str, object]:
    """Perform the Stage-2 bounded audit from the already frozen manifest."""
    validation = validate()
    if validation["HOST_DISCOVERY_MANIFEST_VALIDATION"] != "PASS":
        raise RuntimeError("host discovery manifest is not valid")
    discovery = read_jsonl(DISCOVERY_MANIFEST)
    admitted = [row for row in discovery if row["classification"] == "ADMIT_TO_INSPECTION"]
    if len(admitted) != 30 or len(admitted) != len(discovery):
        raise RuntimeError("manifest is not the frozen 30-candidate allowlist")
    ids = {str(row["discovery_id"]) for row in admitted}
    frozen_fingerprint = stable_id("\n".join(sorted(ids)))
    references = repository_reference_map()
    inventory: list[dict[str, object]] = []
    uid = str(os.getuid())
    for row in admitted:
        kind, identity = str(row["candidate_kind"]), str(row["candidate_identity"])
        basis_path = str(row["basis_reference"]).rsplit(":", 1)[0]
        repository_reference_id = references.get(basis_path)
        unlinked_reason = None if repository_reference_id else "OTHER_BOUNDED_REASON"
        present = False
        root_status = None
        observed_entries: list[str] = []
        if kind == "FILESYSTEM_ROOT":
            present, root_status, observed_entries = inspect_root(Path(identity).expanduser())
            observed = identity if present else None
        elif kind == "LAUNCH_AGENT":
            result = subprocess.run(["launchctl", "print", f"gui/{uid}/{identity}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            present, observed = result.returncode == 0, identity if result.returncode == 0 else None
        elif kind == "CLI":
            observed = shutil.which(identity)
            present = observed is not None
        elif kind == "ENV_KEY":
            present, observed = identity in os.environ, identity if identity in os.environ else None
        else:
            raise RuntimeError(f"unsupported candidate kind: {kind}")
        legacy = "djconnect" in identity.lower()
        if not present:
            classification, blocking = "HISTORICAL_ONLY", False
            disposition = "INSPECTED_NO_ARTIFACT_PRESENT"
        elif legacy and kind == "LAUNCH_AGENT":
            classification, blocking = "BLOCKING_DJCONNECT_RESIDUAL", True
            disposition = "INSPECTED_AND_CLASSIFIED"
        elif legacy:
            classification, blocking = "LEGACY_COMPATIBILITY", False
            disposition = "INSPECTED_AND_CLASSIFIED"
        else:
            classification, blocking = "CURRENT_CANONICAL", False
            disposition = "INSPECTED_AND_CLASSIFIED"
        inventory.append({
            "inventory_version": 1,
            "scope": "HOST_INSPECTION",
            "stage": "HOST_INSPECTION_STAGE_2",
            "artifact_id": stable_id(f"{row['discovery_id']}:{observed or 'absent'}"),
            "DISCOVERY_MANIFEST_ENTRY_ID": row["discovery_id"],
            "HOST_ROOT_BASIS": identity if kind == "FILESYSTEM_ROOT" else "NON_FILESYSTEM_EXACT_IDENTITY",
            "artifact_kind": kind,
            "observed_current_identity": observed,
            "artifact_present": present,
            "root_inspection_status": root_status,
            "identity_inspection_status": None if kind == "FILESYSTEM_ROOT" else ("INSPECTED" if present else "NOT_PRESENT"),
            "observed_entry_names_bounded_depth_2": observed_entries if kind == "FILESYSTEM_ROOT" else [],
            "classification": classification,
            "authority_map": authority_map(kind, identity, present),
            "repository_reference_id": repository_reference_id,
            "UNLINKED_REASON": unlinked_reason,
            "remediation_bucket": "P_NEUTRAL_REMEDIATION" if blocking or classification == "LEGACY_COMPATIBILITY" else "NO_HOST_MUTATION_IN_PHASE_0",
            "blocking": blocking,
            "final_disposition": disposition,
            "allowlist_fingerprint": frozen_fingerprint,
            "inspection_started_after_discovery_pass": True,
            "allowlist_expansion_required": False,
            "recursive_inspection_outside_allowlist": 0,
            "notes": "Exact allowlisted observation only; no file contents, secret values, Keychain, or broad enumeration read.",
        })
    write_jsonl(HOST_INVENTORY, inventory)
    result = validate()
    if result["HOST_INSPECTION_STATUS"] == "PASS" and result["P_NEUTRAL_REPOSITORY_INVENTORY_VALIDATION"] == "PASS":
        write_execution_backlog()
    return result


def write_execution_backlog() -> None:
    """Finalize remediation planning from completed repository and host evidence."""
    records: list[dict[str, object]] = []
    for row in read_jsonl(REPOSITORY_INVENTORY):
        if row.get("disposition") != "P_NEUTRAL_REMEDIATION_CANDIDATE":
            continue
        source_id = str(row["repository_reference_id"])
        records.append({
            "inventory_version": 1,
            "backlog_id": stable_id("repository:" + source_id),
            "source_scope": "REPOSITORY",
            "source_record_id": source_id,
            "remediation_bucket": "P_NEUTRAL_REPOSITORY_NEUTRAL_NAMING",
            "blocking": False,
            "status": "PLANNED_FROM_COMPLETED_AUDIT",
        })
    for row in read_jsonl(HOST_INVENTORY):
        if not row.get("artifact_present") or row.get("classification") not in {"BLOCKING_DJCONNECT_RESIDUAL", "LEGACY_COMPATIBILITY"}:
            continue
        source_id = str(row["artifact_id"])
        records.append({
            "inventory_version": 1,
            "backlog_id": stable_id("host:" + source_id),
            "source_scope": "HOST",
            "source_record_id": source_id,
            "remediation_bucket": str(row["remediation_bucket"]),
            "blocking": bool(row["blocking"]),
            "status": "PLANNED_FROM_COMPLETED_AUDIT",
        })
    write_jsonl(EXECUTION_BACKLOG, records)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("repository", "discovery", "validate", "inspect-host", "backlog", "all"))
    parser.add_argument("--authority-revision", default=DEFAULT_AUTHORITY_REVISION)
    args = parser.parse_args()
    if args.command in {"repository", "all"}:
        write_jsonl(REPOSITORY_INVENTORY, repository_records(args.authority_revision))
    if args.command in {"discovery", "all"}:
        write_jsonl(DISCOVERY_MANIFEST, discovery_records())
    if args.command == "inspect-host":
        print(json.dumps(inspect_host(), sort_keys=True))
        return 0
    if args.command == "backlog":
        write_execution_backlog()
        return 0
    if args.command in {"validate", "all"}:
        print(json.dumps(validate(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
