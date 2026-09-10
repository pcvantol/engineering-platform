"""Product-owned installation observations for a universal installer consumer.

This module is deliberately an EP boundary.  It reports the runtime selected
by EP's resolver and makes the narrow, exact-artifact update assessment that
EP can prove locally.  A coordinator may retain and correlate this data, but
it cannot supply a runtime path, service label, migration instruction, or
installation lock in return.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import (
    installation_update_plan,
    operational_installation,
    operational_installation_record,
    system_server_service,
)


COMPONENT = "engineering-platform-server"
CONTRACT_VERSION = "1.0"
_PARTIAL_SCOPE = {
    "required": "MACOS_MACHINE",
    "observed": "CURRENT_OS_USER_EXPLICIT_REFERENCES_ONLY",
    "status": "INCOMPLETE",
}
_SYSTEM_INVENTORY_COVERAGE = "SYSTEM_SHARED_AND_DECLARED_USER_SERVICE_SURFACES"
_SYSTEM_SCOPE_OBSERVED = "SYSTEM_LAUNCHDAEMONS_SHARED_AND_DECLARED_USER_LAUNCHAGENTS"
_SYSTEM_SCOPE_LIMITATIONS = frozenset({
    "ACCOUNT_HOME_DISCOVERY_NOT_AUTHORITY",
    "UNPRIVILEGED_CALLER",
    "INACCESSIBLE_SERVICE_SURFACES",
})
_SYSTEM_ENTRY_STATUSES = frozenset({
    "SELECTED_SYSTEM_SERVICE",
    "CONFLICTING_SYSTEM_SERVICE",
    "CONFLICTING_USER_SERVICE",
    "INVALID_OWNED_SERVICE_REFERENCE",
    "INACCESSIBLE_OR_RACED_OWNED_SERVICE_REFERENCE",
    "INACCESSIBLE_OR_RACED_SERVICE_SURFACE",
})
_SYSTEM_LOCATION_STATES = frozenset({"INSPECTED", "ABSENT", "INACCESSIBLE", "RACED"})


class ProductInstallationReadbackError(ValueError):
    """The product cannot safely construct an installation observation."""


def _required(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProductInstallationReadbackError(f"{label} is incomplete")
    return value


def _artifact(record: Mapping[str, object]) -> dict[str, str]:
    """Expose only the artifact facts that the installation record owns.

    A registry location and release qualification are intentionally absent:
    those are release-evidence facts, not installation-record facts.  A
    universal coordinator must correlate its already-qualified candidate with
    these fields rather than asking EP to adopt foreign provenance.
    """
    return {
        "version": _required(record, "version", "registered installation"),
        "digest": _required(record, "artifact_digest", "registered installation"),
        "source_revision": _required(record, "source_revision", "registered installation"),
        "channel": _required(record, "channel", "registered installation"),
    }


def _system_inventory_coverage(
    installation: operational_installation.OperationalInstallation | None,
    inventory: Mapping[str, object],
) -> tuple[str, str, Mapping[str, object], Mapping[str, object]]:
    """Validate EP-issued system-service evidence without upgrading coverage.

    This bounded inventory is canonical resolver evidence, but it is still
    incomplete for the Mac: caller-declared homes are not account-discovery
    authority.  Its parser is deliberately strict so a coordinator cannot
    turn an arbitrary mapping or an empty scan into a uniqueness assertion.
    """
    expected_keys = {
        "selected_interpreter",
        "selected_data_root",
        "coverage",
        "scope",
        "inspected_locations",
        "inaccessible_locations",
        "entries",
        "conflicting_service_references",
        "single_operational_installation",
        "single_operational_installation_status",
    }
    if set(inventory) != expected_keys or inventory.get("coverage") != _SYSTEM_INVENTORY_COVERAGE:
        raise ProductInstallationReadbackError("operational inventory is invalid")
    scope = inventory.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {"required", "observed", "status", "limitations"}:
        raise ProductInstallationReadbackError("operational inventory is invalid")
    limitations = scope.get("limitations")
    if (
        scope.get("required") != "MACOS_MACHINE"
        or scope.get("observed") != _SYSTEM_SCOPE_OBSERVED
        or scope.get("status") != "INCOMPLETE"
        or not isinstance(limitations, list)
        or not all(isinstance(value, str) for value in limitations)
        or len(set(limitations)) != len(limitations)
        or "ACCOUNT_HOME_DISCOVERY_NOT_AUTHORITY" not in limitations
        or not set(limitations).issubset(_SYSTEM_SCOPE_LIMITATIONS)
    ):
        raise ProductInstallationReadbackError("operational inventory is invalid")

    selected_interpreter = inventory.get("selected_interpreter")
    selected_data_root = inventory.get("selected_data_root")
    selected_pair = (
        isinstance(selected_interpreter, str) and bool(selected_interpreter)
        and isinstance(selected_data_root, str) and bool(selected_data_root)
    )
    if not selected_pair and (selected_interpreter is not None or selected_data_root is not None):
        raise ProductInstallationReadbackError("operational inventory is invalid")
    if installation is not None and (
        not selected_pair
        or selected_interpreter != str(operational_installation.launcher(installation.interpreter))
        or selected_data_root != installation.data_root
    ):
        raise ProductInstallationReadbackError("operational inventory does not bind the selected runtime")

    def mappings(value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise ProductInstallationReadbackError("operational inventory is invalid")
        normalized: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, Mapping) or not all(isinstance(key, str) and isinstance(item[key], str) for key in item):
                raise ProductInstallationReadbackError("operational inventory is invalid")
            normalized.append(dict(item))
        return normalized

    entries = mappings(inventory.get("entries"))
    for entry in entries:
        status = entry.get("status")
        if status not in _SYSTEM_ENTRY_STATUSES or not entry.get("domain"):
            raise ProductInstallationReadbackError("operational inventory is invalid")
        if not entry.get("plist") and not entry.get("path"):
            raise ProductInstallationReadbackError("operational inventory is invalid")
    selected_entries = [entry for entry in entries if entry["status"] == "SELECTED_SYSTEM_SERVICE"]
    if selected_pair:
        # A selected pair is usable only when the observer has also retained
        # the complete canonical descriptor entry that selected it.  This
        # prevents a raced or forged summary pair from becoming an official
        # runtime solely because its launcher and data-root strings look
        # plausible.
        try:
            expected_plist = str(system_server_service.default_paths(selected_data_root).plist_path)
        except system_server_service.SystemServerServiceError as error:
            raise ProductInstallationReadbackError("operational inventory is invalid") from error
        expected_selected_keys = {
            "label",
            "domain",
            "plist",
            "interpreter",
            "data_root",
            "service_account",
            "status",
        }
        if (
            len(selected_entries) != 1
            or set(selected_entries[0]) != expected_selected_keys
            or selected_entries[0].get("label") != system_server_service.LABEL
            or selected_entries[0].get("domain") != "SYSTEM"
            or selected_entries[0].get("plist") != expected_plist
            or selected_entries[0].get("interpreter") != selected_interpreter
            or selected_entries[0].get("data_root") != selected_data_root
            or not selected_entries[0].get("service_account")
        ):
            raise ProductInstallationReadbackError("operational inventory does not bind the selected system service")
    elif selected_entries:
        raise ProductInstallationReadbackError("operational inventory selects a system service without a runtime")
    conflicts = mappings(inventory.get("conflicting_service_references"))
    if conflicts != [entry for entry in entries if entry["status"] != "SELECTED_SYSTEM_SERVICE"]:
        raise ProductInstallationReadbackError("operational inventory is invalid")
    if (
        inventory.get("single_operational_installation") is not False
        or inventory.get("single_operational_installation_status")
        != ("CONFLICTS_DETECTED" if conflicts else "UNVERIFIED_SCOPE")
    ):
        raise ProductInstallationReadbackError("operational inventory is invalid")

    inspected = mappings(inventory.get("inspected_locations"))
    for location in inspected:
        if set(location) != {"path", "domain", "state"} or location["state"] not in _SYSTEM_LOCATION_STATES:
            raise ProductInstallationReadbackError("operational inventory is invalid")
    inaccessible = mappings(inventory.get("inaccessible_locations"))
    expected_inaccessible = [
        {"path": location["path"], "domain": location["domain"], "reason": "UNAVAILABLE_OR_SYMLINK_OR_RACE"}
        for location in inspected
        if location["state"] in {"INACCESSIBLE", "RACED"}
    ]
    if inaccessible != expected_inaccessible:
        raise ProductInstallationReadbackError("operational inventory is invalid")
    return "PARTIAL", "CONFLICTING" if conflicts else "UNKNOWN", dict(scope), {
        "state": "SYSTEM_SERVICE_SURFACES",
        "selected_interpreter": selected_interpreter,
        "selected_data_root": selected_data_root,
        "entries": entries,
        "conflicting_service_references": conflicts,
        "inspected_locations": inspected,
        "inaccessible_locations": inaccessible,
    }


def validate_system_service_inventory(inventory: Mapping[str, object]) -> None:
    """Fail closed unless an EP system-service observer has the full V1 shape.

    The resolver, diagnose and qualification boundaries all use this before
    selecting an operational interpreter.  Keeping it alongside the readback
    decoder ensures a malformed observer cannot be rejected by readback yet
    still promoted by another official command surface.
    """
    _system_inventory_coverage(None, inventory)


def _coverage(
    installation: operational_installation.OperationalInstallation | None,
    inventory: Mapping[str, object] | None,
) -> tuple[str, str, Mapping[str, object], Mapping[str, object]]:
    """Translate only current EP-issued inventory shapes.

    Legacy explicit inventory needs an official selected runtime.  The newer
    system-service inventory may be retained with an unavailable resolver, so
    a resulting ``UNKNOWN`` stays diagnosable without a PATH, ``sys.executable``
    or legacy-LaunchAgent fallback.
    """
    if inventory is None:
        return "PARTIAL", "UNKNOWN", _PARTIAL_SCOPE, {"state": "NOT_REQUESTED"}
    if not isinstance(inventory, Mapping):
        raise ProductInstallationReadbackError("operational inventory is invalid")
    if inventory.get("coverage") == _SYSTEM_INVENTORY_COVERAGE:
        return _system_inventory_coverage(installation, inventory)
    if installation is None:
        raise ProductInstallationReadbackError("operational inventory requires a selected runtime")
    scope, entries = inventory.get("scope"), inventory.get("entries")
    conflicts = inventory.get("conflicting_service_references")
    if (
        not isinstance(scope, Mapping)
        or not isinstance(entries, list)
        or not isinstance(conflicts, list)
        or inventory.get("coverage") != "EXPLICIT_PATHS_ONLY"
        or inventory.get("selected_interpreter") != str(operational_installation.launcher(installation.interpreter))
        or dict(scope) != _PARTIAL_SCOPE
        or inventory.get("single_operational_installation") is not False
    ):
        raise ProductInstallationReadbackError("operational inventory is invalid")
    conflict_state = "CONFLICTING" if conflicts else "UNKNOWN"
    evidence = {
        "state": "EXPLICIT_PATHS_ONLY",
        "entries": entries,
        "conflicting_service_references": conflicts,
    }
    return "PARTIAL", conflict_state, _PARTIAL_SCOPE, evidence


def unavailable_readback(
    *,
    reason: str,
    inventory: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Report that EP cannot resolve an official runtime without guessing.

    In particular, callers must not substitute their own interpreter or a
    PATH result when the EP-owned service record is absent or invalid.
    """
    if reason not in {
        "OWNED_SERVICE_INTERPRETER_UNAVAILABLE",
        "SYSTEM_SERVICE_UNAVAILABLE",
        "SYSTEM_SERVICE_INVENTORY_MISMATCH",
    }:
        raise ProductInstallationReadbackError("operational readback reason is invalid")
    if inventory is None:
        # Preserve the OI-3 response for the historical no-observation case.
        # A system observer changes this to bounded ``PARTIAL`` evidence only
        # when EP actually supplied that observer's result.
        coverage, conflict_state, scope, inventory_evidence = (
            "UNKNOWN", "UNKNOWN", _PARTIAL_SCOPE, {"state": "NOT_REQUESTED"},
        )
    else:
        coverage, conflict_state, scope, inventory_evidence = _coverage(None, inventory)
    return {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "installation_identity": None,
        "state": "UNKNOWN",
        "selected_runtime_identity": None,
        "selected_executable_identity": None,
        "selected_server_identity": None,
        "selected_instance_identity": None,
        "artifact": None,
        "health_state": "UNKNOWN",
        "inventory_coverage": coverage,
        "conflict_state": conflict_state,
        "inventory_scope": scope,
        "single_operational_installation_verified": False,
        "evidence": {
            "reason": reason,
            "inventory": inventory_evidence,
        },
    }


def _registered_record(data_root: Path) -> Mapping[str, object] | None:
    """Read only an authoritative record identity for a rejected candidate.

    This is intentionally best-effort: a malformed or absent record must not
    become a coordinator-supplied identity.  The later successful plan is the
    only route that can make the identity mandatory.
    """
    try:
        record = operational_installation_record.load(Path(data_root).expanduser().resolve())
    except operational_installation_record.OperationalInstallationRecordError:
        return None
    return record


def _registered_installation_identity(data_root: Path) -> str | None:
    record = _registered_record(data_root)
    if record is None:
        return None
    identity = record.get("installation_id")
    return identity if isinstance(identity, str) and identity else None


def _health_failure_code(error: operational_installation.OperationalInstallationError) -> str:
    """Keep failed live qualification useful without leaking runtime paths."""
    message = str(error)
    if "not healthy" in message:
        return "SERVER_UNHEALTHY"
    if "does not identify" in message or "differs" in message:
        return "IDENTITY_MISMATCH"
    return "INVALID_HEALTH_IDENTITY"


def readback(
    installation: operational_installation.OperationalInstallation,
    *,
    record: Mapping[str, object],
    package: Mapping[str, str] | None,
    health_response: Mapping[str, object] | None,
    inventory: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return an EP-owned selected-runtime observation without mutation.

    A health response that is missing or does not prove the selected runtime is
    an ``UNHEALTHY`` observation, rather than a failed assertion that another
    product might silently reinterpret.  It names the selected EP identities
    and states that no single-installation claim has been proven.
    """
    if not isinstance(record, Mapping):
        raise ProductInstallationReadbackError("operational installation record is invalid")
    if record.get("state") == "UNREGISTERED":
        coverage, conflict_state, scope, inventory_evidence = _coverage(installation, inventory)
        return {
            "contract_version": CONTRACT_VERSION,
            "component": COMPONENT,
            "installation_identity": installation.instance_id,
            "state": "ABSENT",
            "selected_runtime_identity": None,
            "selected_executable_identity": None,
            "selected_server_identity": None,
            "selected_instance_identity": None,
            "artifact": None,
            "health_state": "UNKNOWN",
            "inventory_coverage": coverage,
            "conflict_state": conflict_state,
            "inventory_scope": scope,
            "single_operational_installation_verified": False,
            "evidence": {
                "record_state": "UNREGISTERED",
                # A missing EP-owned record is authoritative.  Do not turn
                # an incidental package or PATH result into an installation.
                "package_state": "UNOBSERVED",
                "inventory": inventory_evidence,
            },
        }
    if record.get("state") != "REGISTERED":
        raise ProductInstallationReadbackError("operational installation record state is invalid")

    if not isinstance(package, Mapping):
        raise ProductInstallationReadbackError("selected package identity is unavailable")
    try:
        operational_installation.validate_package_identity(installation, package)
        operational_installation.validate_registered_package_identity(record, package)
    except operational_installation.OperationalInstallationError as error:
        raise ProductInstallationReadbackError("selected package does not match the registered installation") from error

    coverage, conflict_state, scope, inventory_evidence = _coverage(installation, inventory)
    selected_runtime = str(operational_installation.launcher(installation.interpreter))
    selected_executable = _required(package, "interpreter", "selected package")
    selected_instance = _required(record, "installation_id", "registered installation")
    observed_artifact = _artifact(record)
    base: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "installation_identity": selected_instance,
        "selected_runtime_identity": selected_runtime,
        "selected_executable_identity": selected_executable,
        "selected_instance_identity": selected_instance,
        "artifact": observed_artifact,
        "inventory_coverage": coverage,
        "conflict_state": conflict_state,
        "inventory_scope": scope,
        # This is always false for today's explicit-path inventory.  Do not
        # infer a Mac-wide uniqueness result from one service response.
        "single_operational_installation_verified": False,
        "evidence": {
            "record_state": "REGISTERED",
            "record_version": observed_artifact["version"],
            "record_digest": observed_artifact["digest"],
            "inventory": inventory_evidence,
        },
    }
    if health_response is None:
        return {
            **base,
            "state": "UNHEALTHY",
            "selected_server_identity": None,
            "health_state": "UNHEALTHY",
            "evidence": {**base["evidence"], "health_qualification": "UNAVAILABLE"},
        }
    try:
        qualification = operational_installation.qualify_runtime_response(
            installation,
            record=record,
            package=package,
            response=health_response,
        )
    except operational_installation.OperationalInstallationError as error:
        return {
            **base,
            "state": "UNHEALTHY",
            "selected_server_identity": None,
            "health_state": "UNHEALTHY",
            "evidence": {
                **base["evidence"],
                "health_qualification": "FAILED",
                "health_failure": _health_failure_code(error),
            },
        }
    live = qualification.get("live")
    if not isinstance(live, Mapping):  # defensive for future qualification implementations
        raise ProductInstallationReadbackError("runtime qualification is invalid")
    runtime_identity = live.get("runtime_identity")
    if not isinstance(runtime_identity, Mapping):
        raise ProductInstallationReadbackError("runtime qualification is invalid")
    return {
        **base,
        "state": "ACTIVE",
        "selected_runtime_identity": _required(runtime_identity, "interpreter", "live runtime"),
        "selected_executable_identity": _required(runtime_identity, "executable", "live runtime"),
        "selected_server_identity": _required(live, "service", "live runtime"),
        "selected_instance_identity": _required(live, "instance_id", "live runtime"),
        "health_state": "HEALTHY",
        "evidence": {**base["evidence"], "health_qualification": "PASS", "runtime": dict(live)},
    }


def assess_update(
    data_root: Path,
    *,
    operation_id: str,
    artifact: Path,
    target_version: str,
    target_digest: str,
    target_source_revision: str,
    current_observation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assess one exact wheel before a coordinator can request an update.

    The answer does not execute an update or grant a migration/rollback.  A
    future EP provisioner still owns the installation lock, backup, migration,
    activation and postflight qualification.  ``UPDATE_AVAILABLE`` therefore
    means only that this product proved the precise forward candidate can be
    prepared against the current registered record.
    """
    candidate = {
        "version": target_version,
        "digest": target_digest,
        "source_revision": target_source_revision,
    }
    if (
        not isinstance(current_observation, Mapping)
        or current_observation.get("contract_version") != CONTRACT_VERSION
        or current_observation.get("component") != COMPONENT
        or current_observation.get("state") != "ACTIVE"
        or current_observation.get("health_state") != "HEALTHY"
        or not isinstance(current_observation.get("installation_identity"), str)
    ):
        return {
            "contract_version": CONTRACT_VERSION,
            "component": COMPONENT,
            "installation_identity": None,
            "candidate": candidate,
            "state": "UNKNOWN",
            "evidence": {
                "assessment": "REJECTED",
                "reason": "selected operational runtime is not qualified",
            },
        }
    record = _registered_record(data_root)
    installation_identity = current_observation["installation_identity"]
    if record is None or record.get("installation_id") != installation_identity:
        return {
            "contract_version": CONTRACT_VERSION,
            "component": COMPONENT,
            "installation_identity": installation_identity,
            "candidate": candidate,
            "state": "UNKNOWN",
            "evidence": {
                "assessment": "REJECTED",
                "reason": "registered installation changed before update assessment",
            },
        }
    try:
        plan = installation_update_plan.prepare(
            data_root,
            operation_id=operation_id,
            artifact=artifact,
            target_version=target_version,
            target_digest=target_digest,
            target_source_revision=target_source_revision,
        )
    except installation_update_plan.InstallationUpdatePlanError as error:
        message = str(error)
        state = "INCOMPATIBLE" if message in {
            "downgrade or rollback requires a compatible recovery operation",
            "same release identity cannot use different artifact bytes",
            "same release identity cannot use different source revision",
        } else "UNKNOWN"
        return {
            "contract_version": CONTRACT_VERSION,
            "component": COMPONENT,
            # This is read only from EP's record and can remain absent if
            # that record is missing or invalid.  Never echo a caller-owned
            # selector as an installation identity.
            "installation_identity": _registered_installation_identity(data_root),
            "candidate": candidate,
            "state": state,
            "evidence": {"assessment": "REJECTED", "reason": message},
        }
    state = "UP_TO_DATE" if (
        plan.current_version == plan.target_version and plan.current_digest == plan.target_digest
    ) else "UPDATE_AVAILABLE"
    return {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "installation_identity": plan.installation_id,
        "candidate": candidate,
        "state": state,
        "evidence": {
            "assessment": "PREPARED",
            "operation_id": plan.operation_id,
            "current_version": plan.current_version,
            "current_digest": plan.current_digest,
            "current_source_revision": record["source_revision"],
            "planned_artifact": plan.artifact,
        },
    }
