"""Exact-target CENTRAL migration action for a prepared EP update."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from . import operational_installation
from .installation_update_plan import InstallationUpdatePlan


class InstallationUpdateMigrationError(ValueError):
    """The target release cannot safely migrate the bound CENTRAL database."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def migrate(plan: InstallationUpdatePlan, *, interpreter: str | Path,
            runner: Runner = subprocess.run) -> Mapping[str, object]:
    """Run the target release's idempotent CENTRAL migration, never via PATH.

    The executor invokes this only after its quiesce and retained-backup steps.
    It deliberately imports the migration code through the exact replacement
    interpreter; the current runtime cannot accidentally migrate data for a
    different wheel.
    """
    target = Path(interpreter).expanduser().absolute()
    try:
        identity = operational_installation.package_identity(target, runner=runner)
    except operational_installation.OperationalInstallationError as error:
        raise InstallationUpdateMigrationError("target EP interpreter is unavailable") from error
    if identity["version"] != plan.target_version:
        raise InstallationUpdateMigrationError("target interpreter does not provide the planned EP version")
    program = (
        "import json,pathlib,sys;"
        "from engineering_platform.server import initialize,validate_store;"
        "root=pathlib.Path(sys.argv[1]).resolve();"
        "identity=initialize(root);report=validate_store(root,identity);"
        "print(json.dumps({'interpreter':sys.executable,'instance_id':identity.instance_id,"
        "'schema_version':report['schema_version'],'integrity':report['integrity']},sort_keys=True))"
    )
    result = runner((str(target), "-I", "-c", program, str(Path(plan.data_root).resolve())),
                    capture_output=True, text=True, check=False)
    if result.returncode:
        raise InstallationUpdateMigrationError("target CENTRAL migration failed")
    try:
        evidence = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise InstallationUpdateMigrationError("target CENTRAL migration returned invalid evidence") from error
    if (not isinstance(evidence, dict) or set(evidence) != {"interpreter", "instance_id", "schema_version", "integrity"}
            or not isinstance(evidence["interpreter"], str) or not evidence["interpreter"]
            or not isinstance(evidence["instance_id"], str) or not evidence["instance_id"]
            or not isinstance(evidence["schema_version"], int) or evidence["schema_version"] < 1
            or evidence["integrity"] != "PASS"):
        raise InstallationUpdateMigrationError("target CENTRAL migration evidence is invalid")
    if operational_installation.normalized(evidence["interpreter"]) != operational_installation.normalized(target):
        raise InstallationUpdateMigrationError("target CENTRAL migration used a different interpreter")
    if evidence["instance_id"] != plan.installation_id:
        raise InstallationUpdateMigrationError("target CENTRAL migration used a different instance")
    return {**evidence, "package_version": identity["version"]}
