"""Durable identity record for the single EP operational installation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


FILENAME = "operational-installation.json"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = frozenset({"schema_version", "installation_id", "version", "channel", "artifact_digest",
                     "source_revision", "interpreter", "roles", "desired_state", "observed_state",
                     "verification", "cleanup"})


class OperationalInstallationRecordError(ValueError): pass


def _validate(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _FIELDS or value.get("schema_version") != 1:
        raise OperationalInstallationRecordError("operational installation record is invalid")
    required = ("installation_id", "version", "channel", "artifact_digest", "source_revision",
                "interpreter", "desired_state", "observed_state")
    if not all(isinstance(value.get(key), str) and value[key] for key in required):
        raise OperationalInstallationRecordError("operational installation record is invalid")
    if (_DIGEST.fullmatch(str(value["artifact_digest"])) is None
            or _REVISION.fullmatch(str(value["source_revision"])) is None
            or not Path(str(value["interpreter"])).is_absolute()):
        raise OperationalInstallationRecordError("operational installation record is invalid")
    for key in ("roles", "verification", "cleanup"):
        if not isinstance(value[key], dict):
            raise OperationalInstallationRecordError("operational installation record is invalid")
    if not value["roles"] or not all(isinstance(key, str) and isinstance(item, str) and item
                                      for key, item in value["roles"].items()):
        raise OperationalInstallationRecordError("operational installation record is invalid")
    return value


def _write(path: Path, value: Mapping[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".operational-installation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True); raise


def record(data_root: Path, *, installation_id: str, version: str, channel: str,
           artifact_digest: str, source_revision: str, interpreter: Path,
           roles: Mapping[str, str], desired_state: str, observed_state: str,
           verification: Mapping[str, object], cleanup: Mapping[str, object]) -> dict[str, object]:
    # Retain the venv launcher spelling.  ``resolve`` follows the launcher
    # symlink to its base Python and silently changes the registered runtime.
    root, executable = Path(data_root).resolve(), Path(interpreter).expanduser().absolute()
    if not all(isinstance(item, str) and item for item in (installation_id, version, channel, artifact_digest, source_revision, desired_state, observed_state)):
        raise OperationalInstallationRecordError("operational installation identity is invalid")
    if _DIGEST.fullmatch(artifact_digest) is None or _REVISION.fullmatch(source_revision) is None or not executable.is_file():
        raise OperationalInstallationRecordError("operational artifact, source, or interpreter is invalid")
    if (not isinstance(roles, Mapping) or not roles or not isinstance(verification, Mapping)
            or not isinstance(cleanup, Mapping) or not all(isinstance(key, str) and isinstance(value, str) and value for key, value in roles.items())):
        raise OperationalInstallationRecordError("operational roles are invalid")
    value: dict[str, object] = {"schema_version": 1, "installation_id": installation_id, "version": version, "channel": channel, "artifact_digest": artifact_digest, "source_revision": source_revision, "interpreter": str(executable), "roles": dict(roles), "desired_state": desired_state, "observed_state": observed_state, "verification": dict(verification), "cleanup": dict(cleanup)}
    _validate(value)
    root.mkdir(mode=0o700, parents=True, exist_ok=True); path = root / FILENAME
    if path.exists():
        existing = load(root)
        if existing != value: raise OperationalInstallationRecordError("operational installation record already exists with different identity")
        return existing
    _write(path, value); return value


def replace_for_update(data_root: Path, *, expected_version: str,
                       expected_artifact_digest: str,
                       replacement: Mapping[str, object]) -> dict[str, object]:
    """Atomically replace one verified installation record under exact preconditions.

    The installation-update session owns serialization. This boundary adds the
    durable compare-and-swap check: a stale or different update cannot replace
    a record it did not inventory. The installation identity is intentionally
    stable across an in-place update.
    """
    root, path = Path(data_root).resolve(), Path(data_root).resolve() / FILENAME
    existing = load(root)
    if (existing["version"] != expected_version
            or existing["artifact_digest"] != expected_artifact_digest):
        raise OperationalInstallationRecordError("operational installation record changed before update activation")
    candidate = _validate(dict(replacement))
    if candidate["installation_id"] != existing["installation_id"]:
        raise OperationalInstallationRecordError("operational installation identity cannot change during update")
    if candidate == existing:
        return existing
    _write(path, candidate)
    return candidate


def load(data_root: Path) -> dict[str, object]:
    try: value = json.loads((Path(data_root).resolve() / FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise OperationalInstallationRecordError("operational installation record is unreadable") from error
    return _validate(value)
