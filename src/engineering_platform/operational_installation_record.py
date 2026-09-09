"""Durable identity record for the single EP operational installation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Mapping


FILENAME = "operational-installation.json"


class OperationalInstallationRecordError(ValueError): pass


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
    root, executable = Path(data_root).resolve(), Path(interpreter).expanduser().resolve()
    if not all(isinstance(item, str) and item for item in (installation_id, version, channel, artifact_digest, source_revision, desired_state, observed_state)):
        raise OperationalInstallationRecordError("operational installation identity is invalid")
    if not artifact_digest.startswith("sha256:") or len(source_revision) != 40 or not executable.is_file():
        raise OperationalInstallationRecordError("operational artifact, source, or interpreter is invalid")
    if not isinstance(roles, Mapping) or not roles or not all(isinstance(key, str) and isinstance(value, str) for key, value in roles.items()):
        raise OperationalInstallationRecordError("operational roles are invalid")
    value: dict[str, object] = {"schema_version": 1, "installation_id": installation_id, "version": version, "channel": channel, "artifact_digest": artifact_digest, "source_revision": source_revision, "interpreter": str(executable), "roles": dict(roles), "desired_state": desired_state, "observed_state": observed_state, "verification": dict(verification), "cleanup": dict(cleanup)}
    root.mkdir(mode=0o700, parents=True, exist_ok=True); path = root / FILENAME
    if path.exists():
        existing = load(root)
        if existing != value: raise OperationalInstallationRecordError("operational installation record already exists with different identity")
        return existing
    _write(path, value); return value


def load(data_root: Path) -> dict[str, object]:
    try: value = json.loads((Path(data_root).resolve() / FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise OperationalInstallationRecordError("operational installation record is unreadable") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1: raise OperationalInstallationRecordError("operational installation record is invalid")
    return value
