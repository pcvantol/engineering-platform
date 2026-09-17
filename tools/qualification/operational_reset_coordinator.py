#!/usr/bin/env python3
"""Qualification-only coordinator for two product-owned operational resets.

The coordinator owns only its secret-free progress receipt.  Forge and
Engineering Platform remain the sole authorities for preview, maintenance,
backup, apply, verification, recovery and finish.  They are invoked only as
subprocesses through their installed local CLIs; this module never imports a
product package or opens either product database.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Callable, Iterator, Mapping, Sequence


CONTRACT_VERSION = "cross-product-operational-reset-coordinator-v1"
PRODUCT_CONTRACT = "operational-reset-v1"
PRODUCTS = ("forge", "engineering-platform")
STATES = frozenset({
    "DISCOVERED", "BOTH_PREVIEWED", "BOTH_MAINTENANCE", "BACKUPS_VERIFIED",
    "PLANS_REVALIDATED", "FORGE_APPLIED", "EP_APPLIED", "BOTH_APPLIED",
    "BOTH_VERIFIED", "RESUME_AUTHORIZED", "COMPLETE", "RECONCILIATION_REQUIRED",
})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_OUTPUT = 2 * 1024 * 1024
_MILESTONE_KEYS = frozenset({
    "previewed", "maintenance", "backups_verified", "plans_revalidated",
    "forge_apply_started", "ep_apply_started", "forge_applied", "ep_applied",
    "forge_verified", "ep_verified", "resume_authorized", "forge_finished",
    "ep_finished",
})


class CoordinatorError(ValueError):
    """The coordinated operation cannot progress safely."""


class ProductCommandError(CoordinatorError):
    """One owning CLI rejected or failed its exact operation."""

    def __init__(self, product: str, action: str, code: str) -> None:
        super().__init__(f"{product} {action} failed: {code}")
        self.product, self.action, self.code = product, action, code


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    candidate = path.expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise CoordinatorError(f"owning CLI is not a regular file: {path}")
    hasher = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _validated_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CoordinatorError(f"{label} is invalid")
    return value


def _absolute_path(value: Path, label: str, *, executable: bool = False) -> Path:
    candidate = value.expanduser().absolute()
    if not candidate.is_absolute():
        raise CoordinatorError(f"{label} must be absolute")
    if executable and (not candidate.exists() or not os.access(candidate, os.X_OK)):
        raise CoordinatorError(f"{label} is not an executable file")
    return candidate


@dataclass(frozen=True)
class CoordinationConfig:
    coordinator_id: str
    forge_cli: Path
    forge_data_root: Path
    forge_operation_id: str
    ep_cli: Path
    ep_data_root: Path
    ep_operation_id: str
    ep_backup_root: Path
    forge_fk_acknowledgements: tuple[str, ...] = ()
    ep_fk_acknowledgements: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        coordinator_id: str,
        forge_cli: Path,
        forge_data_root: Path,
        forge_operation_id: str,
        ep_cli: Path,
        ep_data_root: Path,
        ep_operation_id: str,
        ep_backup_root: Path,
        forge_fk_acknowledgements: Sequence[str] = (),
        ep_fk_acknowledgements: Sequence[str] = (),
    ) -> "CoordinationConfig":
        coordinator_id = _validated_identifier(coordinator_id, "coordinator ID")
        forge_operation_id = _validated_identifier(forge_operation_id, "Forge operation ID")
        ep_operation_id = _validated_identifier(ep_operation_id, "EP operation ID")
        if forge_operation_id == ep_operation_id:
            raise CoordinatorError("owning operation IDs must be distinct")
        return cls(
            coordinator_id=coordinator_id,
            forge_cli=_absolute_path(forge_cli, "Forge CLI", executable=True),
            forge_data_root=_absolute_path(forge_data_root, "Forge data root"),
            forge_operation_id=forge_operation_id,
            ep_cli=_absolute_path(ep_cli, "EP CLI", executable=True),
            ep_data_root=_absolute_path(ep_data_root, "EP data root"),
            ep_operation_id=ep_operation_id,
            ep_backup_root=_absolute_path(ep_backup_root, "EP backup root"),
            forge_fk_acknowledgements=tuple(sorted(set(forge_fk_acknowledgements))),
            ep_fk_acknowledgements=tuple(sorted(set(ep_fk_acknowledgements))),
        )

    def payload(self) -> dict[str, object]:
        return {
            "coordinator_id": self.coordinator_id,
            "forge": {
                "cli": str(self.forge_cli), "cli_digest": _file_digest(self.forge_cli),
                "data_root": str(self.forge_data_root),
                "operation_id": self.forge_operation_id,
                "fk_acknowledgements": list(self.forge_fk_acknowledgements),
            },
            "engineering-platform": {
                "cli": str(self.ep_cli), "cli_digest": _file_digest(self.ep_cli),
                "data_root": str(self.ep_data_root),
                "operation_id": self.ep_operation_id,
                "backup_root": str(self.ep_backup_root),
                "fk_acknowledgements": list(self.ep_fk_acknowledgements),
            },
        }

    @classmethod
    def parse(cls, value: object) -> "CoordinationConfig":
        if not isinstance(value, Mapping):
            raise CoordinatorError("coordinator configuration is invalid")
        forge, ep = value.get("forge"), value.get("engineering-platform")
        if not isinstance(forge, Mapping) or not isinstance(ep, Mapping):
            raise CoordinatorError("coordinator product configuration is invalid")
        config = cls.create(
            coordinator_id=str(value.get("coordinator_id", "")),
            forge_cli=Path(str(forge.get("cli", ""))),
            forge_data_root=Path(str(forge.get("data_root", ""))),
            forge_operation_id=str(forge.get("operation_id", "")),
            ep_cli=Path(str(ep.get("cli", ""))),
            ep_data_root=Path(str(ep.get("data_root", ""))),
            ep_operation_id=str(ep.get("operation_id", "")),
            ep_backup_root=Path(str(ep.get("backup_root", ""))),
            forge_fk_acknowledgements=tuple(forge.get("fk_acknowledgements", ())),
            ep_fk_acknowledgements=tuple(ep.get("fk_acknowledgements", ())),
        )
        expected = config.payload()
        if dict(value) != expected:
            raise CoordinatorError("coordinator configuration or owning CLI bytes changed")
        return config


class ReceiptStore:
    """Crash-safe one-operation coordinator receipt with a local flock."""

    def __init__(self, root: Path, coordinator_id: str) -> None:
        self.root = root.expanduser().absolute()
        self.coordinator_id = _validated_identifier(coordinator_id, "coordinator ID")
        self.directory = self.root / coordinator_id
        self.receipt_path = self.directory / "receipt.json"
        self.lock_path = self.directory / "coordinator.lock"
        self._prepare_directory(self.root)
        self._prepare_directory(self.directory)

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise CoordinatorError(
                    f"coordinator receipt path contains a symlink component: {current}"
                )

    @classmethod
    def _prepare_directory(cls, path: Path) -> None:
        cls._reject_symlink_components(path)
        if path.exists() and not path.is_dir():
            raise CoordinatorError(f"coordinator receipt path is not a directory: {path}")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls._reject_symlink_components(path)
        os.chmod(path, 0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o700 or path.stat().st_uid != os.getuid():
            raise CoordinatorError("coordinator receipt directory permissions are unsafe")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._reject_symlink_components(self.directory)
        if self.lock_path.is_symlink():
            raise CoordinatorError("coordinator lock path is a symlink")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise CoordinatorError("another coordinator process owns this receipt") from error
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (self.coordinator_id + "\n").encode("ascii"))
            os.fsync(descriptor)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load(self) -> dict[str, object] | None:
        self._reject_symlink_components(self.directory)
        if self.receipt_path.is_symlink():
            raise CoordinatorError("coordinator receipt is a symlink")
        if not self.receipt_path.exists():
            return None
        metadata = self.receipt_path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CoordinatorError("coordinator receipt permissions or type are unsafe")
        try:
            value = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CoordinatorError("coordinator receipt is unreadable") from error
        _validate_receipt(value, self.coordinator_id)
        return value

    def save(self, receipt: Mapping[str, object]) -> None:
        _validate_receipt(receipt, self.coordinator_id)
        self._reject_symlink_components(self.directory)
        if self.receipt_path.is_symlink():
            raise CoordinatorError("coordinator receipt is a symlink")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".receipt.", suffix=".json", dir=self.directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_canonical(receipt) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.receipt_path)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise


def _validate_receipt(value: object, coordinator_id: str) -> None:
    if not isinstance(value, Mapping):
        raise CoordinatorError("coordinator receipt is not an object")
    required = {
        "contract_version", "coordinator_id", "state", "configuration",
        "configuration_digest", "products", "milestones", "events", "created_at", "updated_at",
    }
    if set(value) != required or value.get("contract_version") != CONTRACT_VERSION:
        raise CoordinatorError("coordinator receipt contract is invalid")
    if value.get("coordinator_id") != coordinator_id or value.get("state") not in STATES:
        raise CoordinatorError("coordinator receipt identity or state is invalid")
    configuration = value.get("configuration")
    if value.get("configuration_digest") != _digest(configuration):
        raise CoordinatorError("coordinator configuration digest does not match")
    if not isinstance(value.get("products"), Mapping) or set(value["products"]) != set(PRODUCTS):
        raise CoordinatorError("coordinator product receipt is invalid")
    if not isinstance(value.get("milestones"), Mapping) or not isinstance(value.get("events"), list):
        raise CoordinatorError("coordinator progress receipt is invalid")
    milestones = value["milestones"]
    if set(milestones) != _MILESTONE_KEYS or not all(
        isinstance(item, bool) for item in milestones.values()
    ):
        raise CoordinatorError("coordinator milestone receipt is invalid")
    for index, event in enumerate(value["events"], start=1):
        if not isinstance(event, Mapping) or event.get("index") != index:
            raise CoordinatorError("coordinator event sequence is invalid")
    has_revalidation = any(
        event.get("state") == "PLANS_REVALIDATED" for event in value["events"]
        if isinstance(event, Mapping)
    )
    if milestones["plans_revalidated"] != has_revalidation:
        raise CoordinatorError("coordinator plan revalidation evidence is invalid")
    for product in ("forge", "ep"):
        if milestones[f"{product}_apply_started"] and not milestones["plans_revalidated"]:
            raise CoordinatorError("coordinator apply admission precedes plan revalidation")
        if milestones[f"{product}_applied"] and not milestones[f"{product}_apply_started"]:
            raise CoordinatorError("coordinator apply result lacks durable admission evidence")


def _empty_product(product: str, operation_id: str) -> dict[str, object]:
    return {
        "product": product, "operation_id": operation_id, "state": "UNOBSERVED",
        "target": None, "target_digest": None, "plan_digest": None,
        "relevant_revision_digest": None, "preserved_bindings_digest": None,
        "backup_digest": None, "request_digest": None, "result_digest": None,
        "dataset_generation": None, "last_receipt_digest": None, "finished": False,
    }


def _new_receipt(config: CoordinationConfig) -> dict[str, object]:
    configuration = config.payload()
    now = _utcnow()
    receipt: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "coordinator_id": config.coordinator_id,
        "state": "DISCOVERED",
        "configuration": configuration,
        "configuration_digest": _digest(configuration),
        "products": {
            "forge": _empty_product("forge", config.forge_operation_id),
            "engineering-platform": _empty_product(
                "engineering-platform", config.ep_operation_id,
            ),
        },
        "milestones": {
            "previewed": False, "maintenance": False, "backups_verified": False,
            "plans_revalidated": False, "forge_apply_started": False,
            "ep_apply_started": False, "forge_applied": False, "ep_applied": False,
            "forge_verified": False, "ep_verified": False, "resume_authorized": False,
            "forge_finished": False, "ep_finished": False,
        },
        "events": [], "created_at": now, "updated_at": now,
    }
    _event(receipt, "DISCOVERED", "coordinator operation discovered")
    return receipt


def _event(
    receipt: dict[str, object],
    state: str,
    event: str,
    *,
    product: str | None = None,
    evidence: object | None = None,
) -> None:
    if state not in STATES:
        raise CoordinatorError("coordinator transition state is invalid")
    events = receipt["events"]
    if not isinstance(events, list):
        raise CoordinatorError("coordinator event journal is invalid")
    events.append({
        "index": len(events) + 1, "at": _utcnow(), "state": state, "event": event,
        "product": product, "evidence_digest": None if evidence is None else _digest(evidence),
    })
    receipt["state"] = state
    receipt["updated_at"] = _utcnow()


def _find_digest(value: object, key: str) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, str) and _DIGEST.fullmatch(candidate):
            return candidate
        for item in value.values():
            found = _find_digest(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_digest(item, key)
            if found is not None:
                return found
    return None


def _validate_envelope(
    product: str,
    action: str,
    envelope: object,
    operation_id: str | None,
) -> dict[str, object]:
    if not isinstance(envelope, dict):
        raise ProductCommandError(product, action, "NON_OBJECT_RECEIPT")
    if envelope.get("contract_version") != PRODUCT_CONTRACT or envelope.get("product") != product:
        raise ProductCommandError(product, action, "WRONG_PRODUCT_CONTRACT")
    if envelope.get("command") != action:
        raise ProductCommandError(product, action, "COMMAND_RECEIPT_MISMATCH")
    if operation_id is not None and envelope.get("operation_id") != operation_id:
        raise ProductCommandError(product, action, "OPERATION_ID_MISMATCH")
    target = envelope.get("target")
    if not isinstance(target, dict) or set(target) != {
        "instance_id", "database_path", "database_identity", "schema_version",
    }:
        raise ProductCommandError(product, action, "TARGET_IDENTITY_INVALID")
    if not isinstance(target.get("instance_id"), str) or not target["instance_id"]:
        raise ProductCommandError(product, action, "TARGET_INSTANCE_INVALID")
    plan_digest = envelope.get("plan_digest")
    if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
        raise ProductCommandError(product, action, "PLAN_DIGEST_INVALID")
    if not isinstance(envelope.get("state"), str):
        raise ProductCommandError(product, action, "PRODUCT_STATE_INVALID")
    return envelope


Runner = Callable[..., subprocess.CompletedProcess[str]]
FaultHook = Callable[[str], None]


class OperationalResetCoordinator:
    """Durably coordinate only the two owning product maintenance CLIs."""

    def __init__(
        self,
        store: ReceiptStore,
        *,
        runner: Runner = subprocess.run,
        command_timeout_seconds: float = 1800,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if not 1 <= command_timeout_seconds <= 7200:
            raise CoordinatorError("owning CLI timeout is outside the qualified bound")
        self.store, self.runner = store, runner
        self.command_timeout_seconds = command_timeout_seconds
        self.fault_hook = fault_hook or (lambda _boundary: None)

    def _config(self, receipt: Mapping[str, object]) -> CoordinationConfig:
        return CoordinationConfig.parse(receipt["configuration"])

    def _command(
        self,
        config: CoordinationConfig,
        receipt: Mapping[str, object],
        product: str,
        action: str,
    ) -> list[str]:
        products = receipt["products"]
        current = products[product]
        if not isinstance(current, Mapping):
            raise CoordinatorError("coordinator product state is invalid")
        if product == "forge":
            command = [
                str(config.forge_cli), "--data-root", str(config.forge_data_root),
                "server", "reset", action,
            ]
            operation_id = config.forge_operation_id
        else:
            command = [str(config.ep_cli), action, "--data-root", str(config.ep_data_root)]
            operation_id = config.ep_operation_id
        if action == "preview":
            return command
        command.extend(("--operation-id", operation_id))
        if action == "status":
            return command
        plan_digest = current.get("plan_digest")
        if not isinstance(plan_digest, str):
            raise CoordinatorError(f"{product} plan digest is unavailable")
        command.extend(("--plan-digest", plan_digest))
        if action == "prepare":
            acknowledgements = (
                config.forge_fk_acknowledgements if product == "forge"
                else config.ep_fk_acknowledgements
            )
            flag = (
                "--acknowledge-operational-fk" if product == "forge"
                else "--allow-operational-fk"
            )
            for finding in acknowledgements:
                command.extend((flag, finding))
            if product == "engineering-platform":
                command.extend(("--backup-root", str(config.ep_backup_root)))
        elif product == "forge" and action in {"apply", "verify", "resume"}:
            request_digest = current.get("request_digest")
            if not isinstance(request_digest, str):
                raise CoordinatorError("Forge request digest is unavailable")
            command.extend(("--request-digest", request_digest))
            backup_digest = current.get("backup_digest")
            if isinstance(backup_digest, str):
                command.extend(("--backup-digest", backup_digest))
            elif action != "resume":
                raise CoordinatorError("Forge backup digest is unavailable")
        elif product == "engineering-platform" and action == "resume":
            command.extend(("--backup-root", str(config.ep_backup_root)))
        elif action == "finish" and product == "forge":
            result_digest = current.get("result_digest")
            if not isinstance(result_digest, str):
                raise CoordinatorError("Forge verification digest is unavailable")
            # The owning Forge finish command binds its own verification digest.
            command = command[:-2] + ["--verification-digest", result_digest]
        return command

    def _invoke(
        self,
        config: CoordinationConfig,
        receipt: Mapping[str, object],
        product: str,
        action: str,
    ) -> dict[str, object]:
        product_config = config.payload()[product]
        if not isinstance(product_config, Mapping):
            raise CoordinatorError("owning product configuration is invalid")
        cli = Path(str(product_config["cli"]))
        if _file_digest(cli) != product_config["cli_digest"]:
            raise ProductCommandError(product, action, "OWNING_CLI_CHANGED")
        command = self._command(config, receipt, product, action)
        try:
            completed = self.runner(
                command, capture_output=True, text=True, check=False,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProductCommandError(product, action, type(error).__name__) from error
        stdout = completed.stdout.encode("utf-8", errors="replace")
        stderr = completed.stderr.encode("utf-8", errors="replace")
        if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT:
            raise ProductCommandError(product, action, "OWNING_CLI_OUTPUT_TOO_LARGE")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        parsed: object = None
        if len(lines) == 1:
            try:
                parsed = json.loads(lines[0])
            except json.JSONDecodeError:
                parsed = None
        if completed.returncode != 0:
            code = parsed.get("error") if isinstance(parsed, Mapping) else None
            raise ProductCommandError(
                product, action, str(code) if isinstance(code, str) else f"EXIT_{completed.returncode}",
            )
        operation_id = None if action == "preview" else str(product_config["operation_id"])
        return _validate_envelope(product, action, parsed, operation_id)

    @staticmethod
    def _record_product(
        receipt: dict[str, object], product: str, envelope: Mapping[str, object],
    ) -> None:
        products = receipt["products"]
        current = products[product]
        target = dict(envelope["target"])
        target_digest = _digest(target)
        prior_target = current.get("target_digest")
        if prior_target is not None and prior_target != target_digest:
            raise ProductCommandError(product, str(envelope["command"]), "TARGET_CHANGED")
        prior_plan = current.get("plan_digest")
        plan_digest = envelope.get("plan_digest")
        if prior_plan is not None and prior_plan != plan_digest:
            raise ProductCommandError(product, str(envelope["command"]), "PLAN_CHANGED")
        current.update({
            "state": envelope["state"], "target": target, "target_digest": target_digest,
            "plan_digest": plan_digest,
            "relevant_revision_digest": envelope.get("relevant_revision_digest"),
            "preserved_bindings_digest": envelope.get("preserved_bindings_digest"),
            "dataset_generation": envelope.get("dataset_generation"),
            "last_receipt_digest": _digest(envelope),
        })
        backup = envelope.get("backup")
        if isinstance(backup, Mapping) and backup.get("verified") is True:
            backup_digest = backup.get("digest")
            if not isinstance(backup_digest, str) or _DIGEST.fullmatch(backup_digest) is None:
                raise ProductCommandError(product, str(envelope["command"]), "BACKUP_DIGEST_INVALID")
            previous = current.get("backup_digest")
            if previous is not None and previous != backup_digest:
                raise ProductCommandError(product, str(envelope["command"]), "BACKUP_CHANGED")
            current["backup_digest"] = backup_digest
        request_digest = _find_digest(envelope, "request_digest")
        if request_digest is not None:
            previous = current.get("request_digest")
            if previous is not None and previous != request_digest:
                raise ProductCommandError(product, str(envelope["command"]), "REQUEST_CHANGED")
            current["request_digest"] = request_digest
        if envelope.get("state") in {"VERIFIED", "COMPLETED"}:
            previous = current.get("result_digest")
            explicit = _find_digest(envelope, "verification_digest")
            if explicit is not None:
                if previous is not None and previous != explicit:
                    raise ProductCommandError(
                        product, str(envelope["command"]), "RESULT_CHANGED",
                    )
                current["result_digest"] = explicit
            elif previous is None:
                # EP does not currently expose a separate verification digest.  Bind
                # its first verified semantic result once; later status/finish
                # receipts contain command-specific fields and must not silently
                # redefine that result identity.
                current["result_digest"] = _digest({
                    "product": product, "operation_id": current["operation_id"],
                    "target_digest": target_digest, "plan_digest": plan_digest,
                    "backup_digest": current.get("backup_digest"),
                    "dataset_generation": envelope.get("dataset_generation"),
                    "integrity": envelope.get("integrity"), "details": envelope.get("details"),
                    "preserved_bindings_digest": envelope.get("preserved_bindings_digest"),
                })
        current["finished"] = envelope.get("state") == "COMPLETED"

    @staticmethod
    def _product_stage(product: str, state: object) -> str:
        if state == "COMPLETED":
            return "finished"
        if state == "VERIFIED":
            return "verified"
        if state in ({"DATABASE_APPLIED", "APPLIED"} if product == "forge" else {"DB_APPLIED"}):
            return "applied"
        if state in (
            {"BACKUP_VERIFIED"} if product == "forge"
            else {"AUTHORIZED", "ARTIFACTS_ARCHIVING", "ARTIFACTS_ARCHIVED"}
        ):
            return "backup"
        if state in ({"PREPARED"} if product == "forge" else {"PREPARING"}):
            return "maintenance"
        if state in {"ERROR", "FAILED", "ABORTED"}:
            return "failed"
        return "other"

    def _save_observation(
        self,
        receipt: dict[str, object],
        product: str,
        action: str,
        envelope: Mapping[str, object],
    ) -> None:
        self._record_product(receipt, product, envelope)
        _event(
            receipt, str(receipt["state"]), f"{product} {action} observed",
            product=product, evidence=envelope,
        )
        self.store.save(receipt)

    def _mark_failure(
        self, receipt: dict[str, object], error: ProductCommandError,
    ) -> None:
        evidence = {"product": error.product, "action": error.action, "code": error.code}
        _event(
            receipt, "RECONCILIATION_REQUIRED", "owning command requires reconciliation",
            product=error.product, evidence=evidence,
        )
        self.store.save(receipt)

    def preview(self, config: CoordinationConfig) -> dict[str, object]:
        with self.store.locked():
            receipt = self.store.load()
            if receipt is None:
                receipt = _new_receipt(config)
                self.store.save(receipt)
            elif receipt["configuration_digest"] != _digest(config.payload()):
                raise CoordinatorError("coordinator ID already binds another request")
            if receipt["state"] != "DISCOVERED":
                return self._readback(receipt)
            try:
                for product in PRODUCTS:
                    envelope = self._invoke(config, receipt, product, "preview")
                    self._save_observation(receipt, product, "preview", envelope)
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            milestones = receipt["milestones"]
            milestones["previewed"] = True
            _event(receipt, "BOTH_PREVIEWED", "both exact owning plans previewed")
            self.store.save(receipt)
            return self._readback(receipt)

    def prepare(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] not in {"BOTH_PREVIEWED", "RECONCILIATION_REQUIRED"}:
                raise CoordinatorError("both previews are required before maintenance")
            config = self._config(receipt)
            try:
                for product in PRODUCTS:
                    stage = self._product_stage(product, receipt["products"][product]["state"])
                    if stage not in {"maintenance", "backup", "applied", "verified", "finished"}:
                        envelope = self._invoke(config, receipt, product, "prepare")
                        self.fault_hook(f"after-{product}-prepare")
                        self._save_observation(receipt, product, "prepare", envelope)
                for product in PRODUCTS:
                    product_receipt = receipt["products"][product]
                    if (
                        self._product_stage(product, product_receipt["state"]) != "backup"
                        or not isinstance(product_receipt["backup_digest"], str)
                        or _DIGEST.fullmatch(product_receipt["backup_digest"]) is None
                    ):
                        raise ProductCommandError(
                            product, "prepare", "VERIFIED_BACKUP_REQUIRED",
                        )
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            milestones = receipt["milestones"]
            milestones["maintenance"] = True
            _event(receipt, "BOTH_MAINTENANCE", "both owning writer fences observed")
            milestones["backups_verified"] = True
            _event(receipt, "BACKUPS_VERIFIED", "both owning backups verified")
            self.store.save(receipt)
            return self._readback(receipt)

    def revalidate(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] != "BACKUPS_VERIFIED":
                raise CoordinatorError("both backups must be verified before plan revalidation")
            config = self._config(receipt)
            try:
                for product in PRODUCTS:
                    envelope = self._invoke(config, receipt, product, "preview")
                    current = receipt["products"][product]
                    if (
                        _digest(envelope["target"]) != current["target_digest"]
                        or envelope["plan_digest"] != current["plan_digest"]
                        or envelope.get("relevant_revision_digest") != current["relevant_revision_digest"]
                        or envelope.get("preserved_bindings_digest") != current["preserved_bindings_digest"]
                    ):
                        raise ProductCommandError(product, "preview", "REVALIDATION_CHANGED")
                    _event(
                        receipt, str(receipt["state"]), f"{product} plan revalidated",
                        product=product, evidence=envelope,
                    )
                    self.store.save(receipt)
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            receipt["milestones"]["plans_revalidated"] = True
            _event(receipt, "PLANS_REVALIDATED", "both plans remain exact under writer fences")
            self.store.save(receipt)
            return self._readback(receipt)

    def apply(self, product: str) -> dict[str, object]:
        if product not in PRODUCTS:
            raise CoordinatorError("owning product is invalid")
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] not in {"PLANS_REVALIDATED", "FORGE_APPLIED", "EP_APPLIED"}:
                raise CoordinatorError("both exact plans must be revalidated before apply")
            config = self._config(receipt)
            other = "engineering-platform" if product == "forge" else "forge"
            current_stage = self._product_stage(product, receipt["products"][product]["state"])
            started_milestone = (
                "forge_apply_started" if product == "forge" else "ep_apply_started"
            )
            try:
                if current_stage not in {"applied", "verified", "finished"}:
                    if not receipt["milestones"][started_milestone]:
                        receipt["milestones"][started_milestone] = True
                        _event(
                            receipt, str(receipt["state"]),
                            f"{product} owning apply durably admitted",
                            product=product,
                            evidence={"joint_gate": "PLANS_REVALIDATED"},
                        )
                        # Persist the recovery authority before a subprocess can
                        # commit an owning effect.
                        self.store.save(receipt)
                    envelope = self._invoke(config, receipt, product, "apply")
                    self.fault_hook(f"after-{product}-apply")
                    self._save_observation(receipt, product, "apply", envelope)
                if self._product_stage(product, receipt["products"][product]["state"]) not in {
                    "applied", "verified", "finished",
                }:
                    raise ProductCommandError(product, "apply", "APPLIED_STATE_REQUIRED")
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            milestone = "forge_applied" if product == "forge" else "ep_applied"
            receipt["milestones"][milestone] = True
            other_stage = self._product_stage(other, receipt["products"][other]["state"])
            if other_stage in {"applied", "verified", "finished"}:
                state = "BOTH_APPLIED"
            else:
                state = "FORGE_APPLIED" if product == "forge" else "EP_APPLIED"
            _event(receipt, state, f"{product} owning reset applied", product=product)
            self.store.save(receipt)
            return self._readback(receipt)

    def verify(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] != "BOTH_APPLIED":
                raise CoordinatorError("both owning resets must be applied before verification")
            config = self._config(receipt)
            try:
                for product in PRODUCTS:
                    envelope = self._invoke(config, receipt, product, "verify")
                    self.fault_hook(f"after-{product}-verify")
                    self._save_observation(receipt, product, "verify", envelope)
                    if self._product_stage(product, envelope["state"]) != "verified":
                        raise ProductCommandError(product, "verify", "VERIFIED_STATE_REQUIRED")
                    receipt["milestones"][
                        "forge_verified" if product == "forge" else "ep_verified"
                    ] = True
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            _event(receipt, "BOTH_VERIFIED", "both owning reset results verified")
            self.store.save(receipt)
            return self._readback(receipt)

    def authorize_resume(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] != "BOTH_VERIFIED":
                raise CoordinatorError("normal writers cannot resume before both verifications")
            receipt["milestones"]["resume_authorized"] = True
            _event(receipt, "RESUME_AUTHORIZED", "joint verification authorizes owning finishes")
            self.store.save(receipt)
            return self._readback(receipt)

    def finish(self, product: str) -> dict[str, object]:
        if product not in PRODUCTS:
            raise CoordinatorError("owning product is invalid")
        with self.store.locked():
            receipt = self._required_receipt()
            if receipt["state"] != "RESUME_AUTHORIZED":
                raise CoordinatorError("finish is forbidden until both reset results are verified")
            config = self._config(receipt)
            try:
                # Re-prove both owning results immediately before each
                # sequential finish.  This is the two-phase readiness boundary;
                # a failed peer check cannot be followed by the requested finish.
                for candidate in PRODUCTS:
                    readiness = self._invoke(config, receipt, candidate, "verify")
                    if self._product_stage(candidate, readiness["state"]) not in {
                        "verified", "finished",
                    }:
                        raise ProductCommandError(
                            candidate, "verify", "FINISH_READINESS_REQUIRED",
                        )
                    self._save_observation(
                        receipt, candidate, "pre-finish readiness", readiness,
                    )
                if not receipt["products"][product]["finished"]:
                    envelope = self._invoke(config, receipt, product, "finish")
                    self.fault_hook(f"after-{product}-finish")
                    self._save_observation(receipt, product, "finish", envelope)
                if not receipt["products"][product]["finished"]:
                    raise ProductCommandError(product, "finish", "COMPLETED_STATE_REQUIRED")
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise
            milestone = "forge_finished" if product == "forge" else "ep_finished"
            receipt["milestones"][milestone] = True
            both = all(receipt["milestones"][name] for name in ("forge_finished", "ep_finished"))
            _event(
                receipt, "COMPLETE" if both else "RESUME_AUTHORIZED",
                f"{product} owning maintenance finished", product=product,
            )
            self.store.save(receipt)
            return self._readback(receipt)

    def resume(self, product: str) -> dict[str, object]:
        if product not in PRODUCTS:
            raise CoordinatorError("owning product is invalid")
        with self.store.locked():
            receipt = self._required_receipt()
            milestone_prefix = "forge" if product == "forge" else "ep"
            milestones = receipt["milestones"]
            has_revalidation = any(
                event.get("state") == "PLANS_REVALIDATED"
                for event in receipt["events"] if isinstance(event, Mapping)
            )
            if not milestones["plans_revalidated"] or not has_revalidation:
                raise CoordinatorError(
                    "owning resume is forbidden before joint plan revalidation",
                )
            if not (
                milestones[f"{milestone_prefix}_apply_started"]
                or milestones[f"{milestone_prefix}_applied"]
            ):
                raise CoordinatorError(
                    "owning resume is forbidden before durable product apply admission",
                )
            config = self._config(receipt)
            try:
                envelope = self._invoke(config, receipt, product, "resume")
                self.fault_hook(f"after-{product}-resume")
                self._save_observation(receipt, product, "resume", envelope)
                return self._reconcile_locked(receipt, config)
            except ProductCommandError as error:
                self._mark_failure(receipt, error)
                raise

    def reconcile(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            return self._reconcile_locked(receipt, self._config(receipt))

    def _reconcile_locked(
        self, receipt: dict[str, object], config: CoordinationConfig,
    ) -> dict[str, object]:
        try:
            for product in PRODUCTS:
                envelope = self._invoke(config, receipt, product, "status")
                self._record_product(receipt, product, envelope)
        except ProductCommandError as error:
            self._mark_failure(receipt, error)
            raise
        forge = self._product_stage("forge", receipt["products"]["forge"]["state"])
        ep = self._product_stage(
            "engineering-platform", receipt["products"]["engineering-platform"]["state"],
        )
        milestones = receipt["milestones"]
        if forge == ep == "finished":
            milestones["forge_finished"] = milestones["ep_finished"] = True
            state = "COMPLETE"
        elif forge in {"finished", "verified"} and ep in {"finished", "verified"}:
            milestones["forge_verified"] = milestones["ep_verified"] = True
            state = "RESUME_AUTHORIZED" if milestones["resume_authorized"] else "BOTH_VERIFIED"
        elif forge in {"applied", "verified"} and ep in {"applied", "verified"}:
            milestones["forge_applied"] = milestones["ep_applied"] = True
            state = "BOTH_APPLIED"
        elif forge in {"applied", "verified"} and ep == "backup":
            milestones["forge_applied"] = True
            state = "FORGE_APPLIED"
        elif ep in {"applied", "verified"} and forge == "backup":
            milestones["ep_applied"] = True
            state = "EP_APPLIED"
        elif forge == ep == "backup":
            milestones["maintenance"] = milestones["backups_verified"] = True
            state = "PLANS_REVALIDATED" if milestones["plans_revalidated"] else "BACKUPS_VERIFIED"
        elif forge in {"maintenance", "backup"} and ep in {"maintenance", "backup"}:
            milestones["maintenance"] = True
            state = "BOTH_MAINTENANCE"
        else:
            state = "RECONCILIATION_REQUIRED"
        _event(receipt, state, "owning status reconciled", evidence={"forge": forge, "ep": ep})
        self.store.save(receipt)
        return self._readback(receipt)

    def status(self) -> dict[str, object]:
        with self.store.locked():
            receipt = self._required_receipt()
            config = self._config(receipt)
            owning = {
                product: self._invoke(config, receipt, product, "status") for product in PRODUCTS
            }
            return {**self._readback(receipt), "owning_readback": owning}

    def _required_receipt(self) -> dict[str, object]:
        receipt = self.store.load()
        if receipt is None:
            raise CoordinatorError("coordinator receipt does not exist; run preview first")
        return receipt

    def _readback(self, receipt: Mapping[str, object]) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "coordinator_id": receipt["coordinator_id"], "state": receipt["state"],
            "receipt": str(self.store.receipt_path), "receipt_digest": _digest(receipt),
            "configuration_digest": receipt["configuration_digest"],
            "products": receipt["products"], "milestones": receipt["milestones"],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operational-reset-coordinator",
        description="Coordinate only installed Forge and EP owning reset CLIs.",
    )
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--coordinator-id", required=True)
    parser.add_argument("--command-timeout-seconds", type=float, default=1800)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview")
    preview.add_argument("--forge-cli", type=Path, required=True)
    preview.add_argument("--forge-data-root", type=Path, required=True)
    preview.add_argument("--forge-operation-id", required=True)
    preview.add_argument("--ep-cli", type=Path, required=True)
    preview.add_argument("--ep-data-root", type=Path, required=True)
    preview.add_argument("--ep-operation-id", required=True)
    preview.add_argument("--ep-backup-root", type=Path, required=True)
    preview.add_argument("--forge-fk-acknowledgement", action="append", default=[])
    preview.add_argument("--ep-fk-acknowledgement", action="append", default=[])
    for name in ("prepare", "revalidate", "verify", "authorize-resume", "reconcile", "status"):
        commands.add_parser(name)
    for name in ("apply", "resume", "finish"):
        product_command = commands.add_parser(name)
        product_command.add_argument("--product", choices=PRODUCTS, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        store = ReceiptStore(args.receipt_root, args.coordinator_id)
        coordinator = OperationalResetCoordinator(
            store, command_timeout_seconds=args.command_timeout_seconds,
        )
        if args.command == "preview":
            config = CoordinationConfig.create(
                coordinator_id=args.coordinator_id,
                forge_cli=args.forge_cli, forge_data_root=args.forge_data_root,
                forge_operation_id=args.forge_operation_id,
                ep_cli=args.ep_cli, ep_data_root=args.ep_data_root,
                ep_operation_id=args.ep_operation_id, ep_backup_root=args.ep_backup_root,
                forge_fk_acknowledgements=args.forge_fk_acknowledgement,
                ep_fk_acknowledgements=args.ep_fk_acknowledgement,
            )
            result = coordinator.preview(config)
        elif args.command in {"apply", "resume", "finish"}:
            result = getattr(coordinator, args.command)(args.product)
        else:
            result = getattr(coordinator, args.command.replace("-", "_"))()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CoordinatorError, OSError) as error:
        print(json.dumps({
            "contract_version": CONTRACT_VERSION, "coordinator_id": args.coordinator_id,
            "state": "ERROR", "allowed": False, "error": str(error),
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
