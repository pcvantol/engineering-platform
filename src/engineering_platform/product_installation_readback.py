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

from . import installation_update_plan, operational_installation, operational_installation_record


COMPONENT = "engineering-platform-server"
CONTRACT_VERSION = "1.0"
_PARTIAL_SCOPE = {
    "required": "MACOS_MACHINE",
    "observed": "CURRENT_OS_USER_EXPLICIT_REFERENCES_ONLY",
    "status": "INCOMPLETE",
}


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


def _coverage(
    installation: operational_installation.OperationalInstallation,
    inventory: Mapping[str, object] | None,
) -> tuple[str, str, Mapping[str, object], Mapping[str, object]]:
    """Translate only the current EP-issued explicit inventory shape.

    ``inventory`` is an in-process product observation, not a coordinator
    assertion.  Today's scanner is deliberately bounded to explicit current
    user references, so this adapter must never translate arbitrary mapping
    values into a machine-wide no-conflict conclusion.
    """
    if inventory is None:
        return "PARTIAL", "UNKNOWN", _PARTIAL_SCOPE, {"state": "NOT_REQUESTED"}
    if not isinstance(inventory, Mapping):
        raise ProductInstallationReadbackError("operational inventory is invalid")
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
    # The current product scanner never establishes complete Mac-wide
    # coverage.  Its no-conflict result is therefore still ``UNKNOWN`` rather
    # than a claim that a coordinator can upgrade to ``NONE``.
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
    if reason not in {"OWNED_SERVICE_INTERPRETER_UNAVAILABLE"}:
        raise ProductInstallationReadbackError("operational readback reason is invalid")
    if inventory is not None:
        # Without an EP-selected interpreter there is no safe identity to
        # bind an explicit inventory to.
        raise ProductInstallationReadbackError("operational inventory requires a selected runtime")
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
        "inventory_coverage": "UNKNOWN",
        "conflict_state": "UNKNOWN",
        "inventory_scope": _PARTIAL_SCOPE,
        "single_operational_installation_verified": False,
        "evidence": {
            "reason": reason,
            "inventory": {"state": "NOT_REQUESTED"},
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
