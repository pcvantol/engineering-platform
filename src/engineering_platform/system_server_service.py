"""macOS system-domain LaunchDaemon contract for the EP Server.

The installed EP Server is an operational product role, not a login-session
helper. This module retains the legacy read-only singleton observer and owns
the instance-specific service definitions/readback used by the EP system
provisioner. Every new instance binds a derived ``LaunchDaemon`` label, an
explicit service account, an absolute installed interpreter and one normalized
data root. Privileged registration remains in the provisioner controller.

The current inventory is deliberately evidence rather than a uniqueness
assertion.  It inspects the system LaunchDaemon directory and caller-declared
user LaunchAgent locations, records inaccessible surfaces, and always leaves
Mac-wide uniqueness unverified until a future privileged account-discovery
operation supplies stronger evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import stat
from typing import Any, Callable, Iterable, Mapping
from xml.parsers import expat



LABEL = "com.engineeringplatform.server"
DEFAULT_SERVICE_ACCOUNT = "_engineeringplatform"
SYSTEM_LAUNCH_DAEMONS_DIRECTORY = Path("/Library/LaunchDaemons")
SHARED_LAUNCH_AGENTS_DIRECTORY = Path("/Library/LaunchAgents")
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_ACCOUNT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_VENV_PYTHON_NAME = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_PLIST_FIELDS = frozenset({
    "Label", "ProgramArguments", "WorkingDirectory", "RunAtLoad", "KeepAlive",
    "ProcessType", "UserName", "EnvironmentVariables", "StandardOutPath", "StandardErrorPath",
})
_ENVIRONMENT = {
    "PATH": DEFAULT_PATH,
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
}


class SystemServerServiceError(ValueError):
    """The bounded system-server service contract cannot proceed safely."""


@dataclass(frozen=True)
class ServiceAccount:
    """The non-root account that owns the Server data root and process."""

    name: str
    uid: int


@dataclass(frozen=True)
class SystemServicePaths:
    """All filesystem locations belonging to one system-domain EP service."""

    data_root: Path
    launch_daemons_dir: Path

    @property
    def plist_path(self) -> Path:
        return self.launch_daemons_dir / f"{LABEL}.plist"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "runtime"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / "server-launchdaemon.err.log"


@dataclass(frozen=True)
class SystemServerService:
    """The exact runtime identity bound into the one canonical daemon."""

    data_root: Path
    interpreter: Path
    service_account: str
    label: str = LABEL


@dataclass(frozen=True)
class InstanceSystemServicePaths:
    """LaunchDaemon and log paths for one exact system Server instance."""

    instance: system_installation_topology.SystemInstanceTopology
    launch_daemons_dir: Path

    @property
    def plist_path(self) -> Path:
        return self.launch_daemons_dir / f"{self.instance.service_label}.plist"

    @property
    def stderr_log(self) -> Path:
        return self.instance.logs_root / "server-launchdaemon.err.log"


@dataclass(frozen=True)
class InstanceSystemServerService:
    """Exact immutable runtime selected for one opaque instance identity."""

    instance_id: str
    data_root: Path
    interpreter: Path
    service_account: str
    label: str
    provider_environment: Mapping[str, str]


AccountLookup = Callable[[str], Any]


def _normalized_data_root(value: str | Path) -> Path:
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise SystemServerServiceError("EP Server data root must be absolute.")
        return raw.resolve(strict=False)
    except SystemServerServiceError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemServerServiceError("EP Server data root is unavailable.") from error


def _absolute_directory(value: str | Path, *, label: str) -> Path:
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise SystemServerServiceError(f"{label} must be absolute.")
        # Canonicalize well-known macOS aliases such as ``/tmp`` → ``/private/tmp``
        # before opening a descriptor chain.  Components introduced after this
        # normalization are still opened with ``O_NOFOLLOW`` below.
        return raw.resolve(strict=False)
    except SystemServerServiceError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemServerServiceError(f"{label} is unavailable.") from error


def default_paths(
    data_root: str | Path,
    launch_daemons_dir: str | Path | None = None,
) -> SystemServicePaths:
    """Build paths without falling back to a caller's home directory or PATH."""
    return SystemServicePaths(
        _normalized_data_root(data_root),
        _absolute_directory(
            SYSTEM_LAUNCH_DAEMONS_DIRECTORY if launch_daemons_dir is None else launch_daemons_dir,
            label="LaunchDaemon directory",
        ),
    )


def installed_interpreter(value: str | Path) -> Path:
    """Retain only an absolute Python launcher from a structural venv."""
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise SystemServerServiceError("EP Server interpreter must be absolute.")
        # Canonicalize parent aliases such as ``/tmp`` → ``/private/tmp`` but
        # retain the final venv launcher link itself: ``bin/python`` is the
        # operational entrypoint even when it links to a shared base Python.
        executable = raw.parent.resolve(strict=False) / raw.name
        # A venv's bin/python is normally a symlink to a base interpreter.  It is
        # still the operational launcher and must not be resolved away.
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise SystemServerServiceError("EP Server interpreter is not an executable installed runtime.")
        if executable.parent.name != "bin" or _VENV_PYTHON_NAME.fullmatch(executable.name) is None:
            raise SystemServerServiceError("EP Server interpreter must be a venv bin/python launcher.")
        if not (executable.parent.parent / "pyvenv.cfg").is_file():
            raise SystemServerServiceError("EP Server interpreter venv metadata is unavailable.")
        return executable
    except SystemServerServiceError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemServerServiceError("EP Server interpreter is unavailable.") from error


def service_definition(
    data_root: str | Path,
    *,
    interpreter: str | Path,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> SystemServerService:
    """Construct one explicit daemon identity; no discovery is performed."""
    if not isinstance(service_account, str) or _ACCOUNT_NAME.fullmatch(service_account) is None:
        raise SystemServerServiceError("EP Server service account is invalid.")
    if service_account == "root":
        raise SystemServerServiceError("EP Server system service must not run as root.")
    return SystemServerService(
        data_root=_normalized_data_root(data_root),
        interpreter=installed_interpreter(interpreter),
        service_account=service_account,
    )


def _service_account(name: str, lookup: AccountLookup = pwd.getpwnam) -> ServiceAccount:
    if not isinstance(name, str) or _ACCOUNT_NAME.fullmatch(name) is None or name == "root":
        raise SystemServerServiceError("EP Server service account is invalid.")
    try:
        record = lookup(name)
    except (KeyError, OSError) as error:
        raise SystemServerServiceError("EP Server service account is unavailable.") from error
    uid = getattr(record, "pw_uid", None)
    if not isinstance(uid, int) or uid <= 0:
        raise SystemServerServiceError("EP Server service account is invalid.")
    return ServiceAccount(name, uid)


def plist_payload(paths: SystemServicePaths, definition: SystemServerService) -> dict[str, object]:
    """Return the closed command line accepted by the system daemon contract."""
    if definition.label != LABEL or definition.data_root != paths.data_root:
        raise SystemServerServiceError("EP Server system-service definition does not match its owned paths.")
    validated = service_definition(
        paths.data_root,
        interpreter=definition.interpreter,
        service_account=definition.service_account,
    )
    if validated != definition:
        raise SystemServerServiceError("EP Server system-service definition is not normalized.")
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(definition.interpreter), "-m", "engineering_platform.server", "serve",
            "--data-root", str(paths.data_root),
        ],
        "WorkingDirectory": str(paths.data_root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "UserName": definition.service_account,
        "EnvironmentVariables": {**_ENVIRONMENT, "EP_SERVER_DATA_ROOT": str(paths.data_root)},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(paths.stderr_log),
    }


def instance_default_paths(
    instance: system_installation_topology.SystemInstanceTopology,
    launch_daemons_dir: str | Path | None = None,
) -> InstanceSystemServicePaths:
    from . import system_installation_topology as topology_module

    if not isinstance(instance, topology_module.SystemInstanceTopology):
        raise SystemServerServiceError("EP Server instance topology is invalid.")
    return InstanceSystemServicePaths(
        instance,
        _absolute_directory(
            SYSTEM_LAUNCH_DAEMONS_DIRECTORY if launch_daemons_dir is None else launch_daemons_dir,
            label="LaunchDaemon directory",
        ),
    )


def instance_service_definition(
    instance: system_installation_topology.SystemInstanceTopology,
    *,
    interpreter: str | Path,
) -> InstanceSystemServerService:
    """Bind one installed runtime and provider environment to one instance."""
    from . import system_installation_topology as topology_module
    from . import system_provider_context as provider_context_module

    if not isinstance(instance, topology_module.SystemInstanceTopology):
        raise SystemServerServiceError("EP Server instance topology is invalid.")
    try:
        environment = provider_context_module.server_environment(instance)
    except provider_context_module.SystemProviderContextError as error:
        raise SystemServerServiceError("EP Server provider context is invalid.") from error
    return InstanceSystemServerService(
        instance_id=instance.instance_id,
        data_root=instance.data_root,
        interpreter=installed_interpreter(interpreter),
        service_account=instance.service_account,
        label=instance.service_label,
        provider_environment=environment,
    )


def instance_plist_payload(
    paths: InstanceSystemServicePaths,
    definition: InstanceSystemServerService,
) -> dict[str, object]:
    """Return the closed, cold-boot-safe LaunchDaemon payload for one instance."""
    expected = instance_service_definition(paths.instance, interpreter=definition.interpreter)
    if expected != definition:
        raise SystemServerServiceError("EP Server instance service definition does not match its owned paths.")
    return {
        "Label": definition.label,
        "ProgramArguments": [
            str(definition.interpreter), "-m", "engineering_platform.server", "serve",
            "--data-root", str(definition.data_root),
            "--expected-instance-id", definition.instance_id,
        ],
        "WorkingDirectory": str(definition.data_root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "UserName": definition.service_account,
        "EnvironmentVariables": {
            **_ENVIRONMENT,
            "HOME": str(paths.instance.root / "service-home"),
            "EP_SERVER_DATA_ROOT": str(definition.data_root),
            "EP_SERVER_INSTANCE_ID": definition.instance_id,
            **dict(definition.provider_environment),
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(paths.stderr_log),
    }


def _reference_from_payload(payload: Mapping[str, object], *, plist_path: Path, domain: str) -> dict[str, str] | None:
    """Extract only a strict EP Server invocation from an arbitrary plist.

    This is used for read-only conflict discovery.  It intentionally accepts a
    legacy per-user plist without ``UserName`` but never turns it into a
    canonical system definition.
    """
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) not in {6, 8} or not all(isinstance(value, str) for value in arguments):
        return None
    interpreter, module_flag, module_name, command, root_flag, raw_root, *instance_arguments = arguments
    if (module_flag, module_name, command, root_flag) != ("-m", "engineering_platform.server", "serve", "--data-root"):
        return None
    instance_id: str | None = None
    if instance_arguments:
        if len(instance_arguments) != 2 or instance_arguments[0] != "--expected-instance-id":
            return None
        try:
            from . import system_installation_topology as topology_module
            instance_id = topology_module.validate_instance_id(instance_arguments[1])
        except topology_module.SystemInstallationTopologyError:
            return None
    try:
        runtime = installed_interpreter(interpreter)
        data_root = _normalized_data_root(raw_root)
    except SystemServerServiceError:
        return None
    label = payload.get("Label")
    if not isinstance(label, str) or not label:
        return None
    result = {
        "label": label,
        "domain": domain,
        "plist": str(plist_path),
        "interpreter": str(runtime),
        "data_root": str(data_root),
    }
    account = payload.get("UserName")
    if isinstance(account, str) and account:
        result["service_account"] = account
    if instance_id is not None:
        result["instance_id"] = instance_id
    return result


def _open_flags(*, directory: bool) -> int:
    """Return the fail-closed flags required for bounded service inspection."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None) if directory else 0
    if nofollow is None or directory_flag is None:
        raise SystemServerServiceError("EP Server secure service inspection is unavailable on this platform.")
    return os.O_RDONLY | nofollow | directory_flag | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_directory_fd(directory: Path) -> int:
    """Open every absolute directory component without following a symlink.

    Inventory input can include a caller-declared home.  A path-level
    ``lstat`` followed by an ``open`` would be vulnerable to a replacement
    between those operations, particularly if a future privileged observer
    reuses this foundation.  Descriptor traversal keeps every ancestor pinned
    while the directory is enumerated.
    """
    if not directory.is_absolute():
        raise SystemServerServiceError("EP Server service directory must be absolute.")
    flags = _open_flags(directory=True)
    descriptor = os.open("/", flags)
    try:
        for component in directory.parts[1:]:
            if component in {".", ".."}:
                raise SystemServerServiceError("EP Server service directory is not normalized.")
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SystemServerServiceError("EP Server service directory is not a directory.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _filesystem_identity(details: os.stat_result) -> tuple[int, int]:
    """Return the filesystem identity used to bind a read-only snapshot."""
    return details.st_dev, details.st_ino


def _read_plist_snapshot_from_descriptor(
    directory_descriptor: int,
    filename: str,
) -> tuple[Mapping[str, object], tuple[int, int]]:
    """Read one plist and retain the identity of the regular file read."""
    if not filename.endswith(".plist") or Path(filename).name != filename:
        raise SystemServerServiceError("EP Server system-service plist name is invalid.")
    file_descriptor: int | None = None
    try:
        try:
            file_descriptor = os.open(filename, _open_flags(directory=False), dir_fd=directory_descriptor)
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise SystemServerServiceError(
                    "EP Server system-service plist must be a regular file, not a symlink.",
                ) from error
            raise SystemServerServiceError("EP Server system-service plist is unreadable.") from error
        details = os.fstat(file_descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SystemServerServiceError("EP Server system-service plist must be a regular file, not a symlink.")
        identity = _filesystem_identity(details)
        with os.fdopen(file_descriptor, "rb", closefd=True) as stream:
            file_descriptor = None
            value = plistlib.load(stream)
    except FileNotFoundError:
        raise
    except SystemServerServiceError:
        raise
    except (OSError, plistlib.InvalidFileException, expat.ExpatError, ValueError) as error:
        raise SystemServerServiceError("EP Server system-service plist is unreadable.") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    if not isinstance(value, dict):
        raise SystemServerServiceError("EP Server system-service plist is invalid.")
    return value, identity


def _read_plist_from_descriptor(directory_descriptor: int, filename: str) -> Mapping[str, object]:
    """Read one regular plist from an already-pinned directory descriptor."""
    value, _identity = _read_plist_snapshot_from_descriptor(directory_descriptor, filename)
    return value


def _read_plist(directory: Path, filename: str) -> Mapping[str, object]:
    """Read one regular plist through a pinned directory descriptor only."""
    directory_descriptor = _open_directory_fd(directory)
    try:
        return _read_plist_from_descriptor(directory_descriptor, filename)
    finally:
        os.close(directory_descriptor)


def configured_instance_service(
    instance: system_installation_topology.SystemInstanceTopology,
    *,
    launch_daemons_dir: str | Path | None = None,
    account_lookup: AccountLookup = pwd.getpwnam,
) -> InstanceSystemServerService | None:
    """Read one instance-specific LaunchDaemon without PATH or label discovery."""
    paths = instance_default_paths(instance, launch_daemons_dir)
    try:
        payload = _read_plist(paths.launch_daemons_dir, paths.plist_path.name)
    except FileNotFoundError:
        return None
    if set(payload) != _PLIST_FIELDS:
        raise SystemServerServiceError("EP Server instance plist has unexpected fields.")
    reference = _reference_from_payload(payload, plist_path=paths.plist_path, domain="SYSTEM")
    if (
        reference is None
        or reference.get("instance_id") != instance.instance_id
        or reference.get("label") != instance.service_label
        or reference.get("data_root") != str(instance.data_root)
        or reference.get("service_account") != instance.service_account
    ):
        raise SystemServerServiceError("EP Server instance plist does not bind the expected runtime.")
    if (
        payload.get("WorkingDirectory") != str(instance.data_root)
        or payload.get("RunAtLoad") is not True
        or payload.get("KeepAlive") != {"SuccessfulExit": False}
        or payload.get("ProcessType") != "Background"
        or payload.get("StandardOutPath") != "/dev/null"
        or payload.get("StandardErrorPath") != str(paths.stderr_log)
    ):
        raise SystemServerServiceError("EP Server instance plist is invalid.")
    _service_account(instance.service_account, account_lookup)
    expected = instance_service_definition(instance, interpreter=reference["interpreter"])
    expected_environment = {
        **_ENVIRONMENT,
        "HOME": str(instance.root / "service-home"),
        "EP_SERVER_DATA_ROOT": str(instance.data_root),
        "EP_SERVER_INSTANCE_ID": instance.instance_id,
        **dict(expected.provider_environment),
    }
    if payload.get("EnvironmentVariables") != expected_environment:
        raise SystemServerServiceError("EP Server instance plist has unexpected environment.")
    return expected


def instance_machine_inventory(
    product: system_installation_topology.SystemInstallationTopology,
    *,
    launch_daemons_dir: str | Path | None = None,
    account_lookup: AccountLookup = pwd.getpwnam,
) -> Mapping[str, object]:
    """Enumerate every declared EP instance and classify real collisions.

    The product-owned ``instances`` directory is the instance declaration
    authority.  LaunchDaemon enumeration proves service ownership.  Multiple
    healthy instances are expected; only duplicate mutable identities or
    missing/tampered service bindings are conflicts.
    """
    from . import system_installation_topology as topology_module

    if not isinstance(product, topology_module.SystemInstallationTopology):
        raise SystemServerServiceError("EP Server product topology is invalid.")
    daemon_directory = _absolute_directory(
        SYSTEM_LAUNCH_DAEMONS_DIRECTORY if launch_daemons_dir is None else launch_daemons_dir,
        label="LaunchDaemon directory",
    )
    instances_root = product.system_root / "instances"
    try:
        children = sorted(
            path for path in instances_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    except FileNotFoundError:
        children = []
    except OSError as error:
        raise SystemServerServiceError("EP Server instance inventory is unavailable.") from error
    entries: list[dict[str, object]] = []
    conflicts: list[dict[str, str]] = []
    uniqueness: dict[str, dict[str, str]] = {
        key: {} for key in (
            "instance_id", "service_label", "service_account", "data_root", "endpoint",
            "codex_home", "github_home", "lifecycle_lock",
        )
    }
    for child in children:
        descriptor_path = child / "instance.json"
        try:
            if not descriptor_path.exists():
                bootstrap_path = child / "provider-bootstrap.json"
                bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(bootstrap, dict)
                    or set(bootstrap) != {
                        "schema_version", "instance_id", "display_label", "service_account",
                        "service_label", "state",
                    }
                    or bootstrap.get("schema_version") != 1
                    or bootstrap.get("state") != "PROVIDER_BOOTSTRAP_PENDING"
                ):
                    raise ValueError
                pending = topology_module.system_instance_topology(
                    product,
                    instance_id=str(bootstrap["instance_id"]),
                    display_label=str(bootstrap["display_label"]),
                    service_account=str(bootstrap["service_account"]),
                )
                if child != pending.root or bootstrap["service_label"] != pending.service_label:
                    raise ValueError
                entries.append({
                    **bootstrap,
                    "descriptor": str(bootstrap_path),
                    "status": "PROVIDER_BOOTSTRAP_PENDING",
                })
                continue
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if not isinstance(descriptor, dict):
                raise ValueError
            required = {
                "schema_version", "instance_id", "display_label", "service_label",
                "service_account", "data_root", "endpoint", "selected_runtime",
                "provider_contexts", "lifecycle_lock", "desired_state",
            }
            if set(descriptor) != required or descriptor.get("schema_version") != 1:
                raise ValueError
            instance = topology_module.system_instance_topology(
                product,
                instance_id=str(descriptor["instance_id"]),
                display_label=str(descriptor["display_label"]),
                service_account=str(descriptor["service_account"]),
            )
            providers = descriptor["provider_contexts"]
            selected = descriptor["selected_runtime"]
            if (
                child != instance.root
                or descriptor["service_label"] != instance.service_label
                or descriptor["data_root"] != str(instance.data_root)
                or descriptor["lifecycle_lock"] != str(instance.lifecycle_lock)
                or not isinstance(providers, dict)
                or set(providers) != {"codex", "github"}
                or not all(isinstance(providers[name], dict) for name in providers)
                or any(
                    set(providers[name]) != {"home", "executable", "executable_sha256"}
                    or not all(
                        isinstance(providers[name][field], str) and providers[name][field]
                        for field in ("home", "executable", "executable_sha256")
                    )
                    or not Path(str(providers[name]["home"])).is_absolute()
                    or not Path(str(providers[name]["executable"])).is_absolute()
                    for name in providers
                )
                or not isinstance(selected, dict)
                or set(selected) != {
                    "version", "artifact_digest", "source_revision", "interpreter",
                }
                or not all(isinstance(selected[field], str) and selected[field] for field in selected)
                or not Path(str(selected["interpreter"])).is_absolute()
            ):
                raise ValueError
            service = configured_instance_service(
                instance,
                launch_daemons_dir=daemon_directory,
                account_lookup=account_lookup,
            )
            status = "READY" if service is not None else "SERVICE_ABSENT"
            if service is None and descriptor.get("desired_state") == "ACTIVE":
                conflicts.append({"instance_id": instance.instance_id, "code": "EXPECTED_SERVICE_ABSENT"})
            values = {
                "instance_id": instance.instance_id,
                "service_label": instance.service_label,
                "service_account": instance.service_account,
                "data_root": str(instance.data_root),
                "endpoint": str(descriptor["endpoint"]),
                "codex_home": str(providers["codex"].get("home")),
                "github_home": str(providers["github"].get("home")),
                "lifecycle_lock": str(instance.lifecycle_lock),
            }
            for kind, value in values.items():
                prior = uniqueness[kind].get(value)
                if prior is not None:
                    conflicts.append({
                        "instance_id": instance.instance_id,
                        "code": f"DUPLICATE_{kind.upper()}",
                        "conflicts_with": prior,
                    })
                else:
                    uniqueness[kind][value] = instance.instance_id
            entries.append({
                **descriptor,
                "descriptor": str(descriptor_path),
                "plist": str(instance_default_paths(instance, daemon_directory).plist_path),
                "status": status,
            })
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError,
                topology_module.SystemInstallationTopologyError,
                SystemServerServiceError) as error:
            conflicts.append({"instance_id": child.name, "code": "INVALID_OR_TAMPERED_INSTANCE"})
            entries.append({
                "instance_id": child.name,
                "descriptor": str(descriptor_path),
                "status": "INVALID",
                "diagnostic": type(error).__name__,
            })
    return {
        "schema_version": 1,
        "product": "engineering-platform-server",
        "product_root": str(product.system_root),
        "cardinality": "MULTI_INSTANCE",
        "instances": entries,
        "conflicts": conflicts,
        "state": "AMBIGUOUS" if conflicts else "OBSERVED",
        "instance_count": len(entries),
        "singleton_assumption": False,
    }


def _configured_definition(
    paths: SystemServicePaths,
    *,
    account_lookup: AccountLookup = pwd.getpwnam,
) -> SystemServerService:
    payload = _read_plist(paths.launch_daemons_dir, paths.plist_path.name)
    if set(payload) != _PLIST_FIELDS:
        raise SystemServerServiceError("EP Server system-service plist has unexpected fields.")
    reference = _reference_from_payload(payload, plist_path=paths.plist_path, domain="SYSTEM")
    if reference is None or reference["label"] != LABEL or reference["data_root"] != str(paths.data_root):
        raise SystemServerServiceError("EP Server system-service plist does not bind the expected runtime.")
    if payload.get("WorkingDirectory") != str(paths.data_root) or payload.get("RunAtLoad") is not True:
        raise SystemServerServiceError("EP Server system-service plist is invalid.")
    if payload.get("KeepAlive") != {"SuccessfulExit": False} or payload.get("ProcessType") != "Background":
        raise SystemServerServiceError("EP Server system-service plist is invalid.")
    account = payload.get("UserName")
    if not isinstance(account, str):
        raise SystemServerServiceError("EP Server system-service plist is invalid.")
    _service_account(account, account_lookup)
    environment = payload.get("EnvironmentVariables")
    if environment != {**_ENVIRONMENT, "EP_SERVER_DATA_ROOT": str(paths.data_root)}:
        raise SystemServerServiceError("EP Server system-service plist has unexpected environment.")
    if payload.get("StandardOutPath") != "/dev/null" or payload.get("StandardErrorPath") != str(paths.stderr_log):
        raise SystemServerServiceError("EP Server system-service plist is invalid.")
    return SystemServerService(paths.data_root, Path(reference["interpreter"]), account)


def configured_service(
    data_root: str | Path,
    *,
    launch_daemons_dir: str | Path | None = None,
    account_lookup: AccountLookup = pwd.getpwnam,
) -> SystemServerService | None:
    """Read the fixed system daemon without looking at the invoking shell."""
    paths = default_paths(data_root, launch_daemons_dir)
    try:
        return _configured_definition(paths, account_lookup=account_lookup)
    except FileNotFoundError:
        return None


def configured_interpreter(
    data_root: str | Path,
    *,
    launch_daemons_dir: str | Path | None = None,
    account_lookup: AccountLookup = pwd.getpwnam,
) -> Path | None:
    """Return only the configured system-service launcher, never PATH."""
    configured = configured_service(
        data_root, launch_daemons_dir=launch_daemons_dir, account_lookup=account_lookup,
    )
    return None if configured is None else configured.interpreter


def _is_ep_related(payload: Mapping[str, object], path: Path) -> bool:
    label = payload.get("Label")
    arguments = payload.get("ProgramArguments")
    return (
        path.name.startswith("com.engineeringplatform.")
        or (isinstance(label, str) and label.startswith("com.engineeringplatform."))
        or (isinstance(arguments, list) and "engineering_platform.server" in arguments)
    )


def _current_directory_identity(directory: Path) -> tuple[int, int] | None:
    """Return the current path identity without trusting a prior pathname read."""
    try:
        descriptor = _open_directory_fd(directory)
    except (OSError, SystemServerServiceError):
        return None
    try:
        return _filesystem_identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _current_regular_file_identity(directory_descriptor: int, filename: str) -> tuple[int, int] | None:
    """Read the current regular-file identity through a pinned directory."""
    descriptor: int | None = None
    try:
        descriptor = os.open(filename, _open_flags(directory=False), dir_fd=directory_descriptor)
        details = os.fstat(descriptor)
        return _filesystem_identity(details) if stat.S_ISREG(details.st_mode) else None
    except (OSError, SystemServerServiceError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _scan_service_directory(directory: Path, *, domain: str) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    """Return relevant EP service references and explicit inspection failures."""
    inspected = {"path": str(directory), "domain": domain, "state": "INSPECTED"}
    try:
        directory_descriptor = _open_directory_fd(directory)
    except FileNotFoundError:
        # A declared account with no LaunchAgents directory is a useful
        # negative observation, not an access failure.  It still cannot make
        # the caller's declared account list machine-wide authority.
        return [], [], {**inspected, "state": "ABSENT"}
    except (OSError, SystemServerServiceError):
        return [], [], {
            **inspected, "state": "INACCESSIBLE",
        }
    try:
        directory_identity = _filesystem_identity(os.fstat(directory_descriptor))
        try:
            with os.scandir(directory_descriptor) as entries:
                filenames = sorted(entry.name for entry in entries if entry.name.endswith(".plist"))
        except OSError:
            return [], [], {
                **inspected, "state": "INACCESSIBLE",
            }
        references: list[dict[str, str]] = []
        reference_snapshots: list[tuple[dict[str, str], str, tuple[int, int]]] = []
        invalid: list[dict[str, str]] = []
        for filename in filenames:
            candidate = directory / filename
            try:
                payload, file_identity = _read_plist_snapshot_from_descriptor(directory_descriptor, filename)
            except FileNotFoundError:
                if filename.startswith("com.engineeringplatform."):
                    invalid.append({
                        "plist": str(candidate),
                        "domain": domain,
                        "status": "INACCESSIBLE_OR_RACED_OWNED_SERVICE_REFERENCE",
                    })
                continue
            except SystemServerServiceError:
                if filename.startswith("com.engineeringplatform."):
                    invalid.append({"plist": str(candidate), "domain": domain, "status": "INVALID_OWNED_SERVICE_REFERENCE"})
                continue
            if not _is_ep_related(payload, candidate):
                continue
            reference = _reference_from_payload(payload, plist_path=candidate, domain=domain)
            if reference is None:
                invalid.append({"plist": str(candidate), "domain": domain, "status": "INVALID_OWNED_SERVICE_REFERENCE"})
            else:
                references.append(reference)
                reference_snapshots.append((reference, filename, file_identity))
        if _current_directory_identity(directory) != directory_identity:
            invalid.append({
                "path": str(directory),
                "domain": domain,
                "status": "INACCESSIBLE_OR_RACED_SERVICE_SURFACE",
            })
            return [], invalid, {**inspected, "state": "RACED"}
        stable_references: list[dict[str, str]] = []
        for reference, filename, file_identity in reference_snapshots:
            if _current_regular_file_identity(directory_descriptor, filename) == file_identity:
                stable_references.append(reference)
            else:
                invalid.append({
                    "plist": reference["plist"],
                    "domain": domain,
                    "status": "INACCESSIBLE_OR_RACED_SERVICE_REFERENCE",
                })
        return stable_references, invalid, inspected
    finally:
        os.close(directory_descriptor)


def machine_scope_inventory(
    data_root: str | Path,
    *,
    launch_daemons_dir: str | Path | None = None,
    shared_launch_agents_dir: str | Path | None = None,
    user_homes: Iterable[str | Path] = (),
    account_lookup: AccountLookup = pwd.getpwnam,
    effective_uid: int | None = None,
) -> Mapping[str, object]:
    """Inspect bounded system/user service surfaces without claiming full Mac coverage.

    ``user_homes`` is deliberately caller-declared evidence rather than an
    account-discovery authority.  The resulting scope remains incomplete even
    for root, which prevents a hand-written list of homes from becoming proof
    that every account and mount on the Mac is covered.
    """
    paths = default_paths(data_root, launch_daemons_dir)
    shared_agents = _absolute_directory(
        SHARED_LAUNCH_AGENTS_DIRECTORY if shared_launch_agents_dir is None else shared_launch_agents_dir,
        label="shared LaunchAgent directory",
    )
    normalized_homes: list[Path] = []
    for home in user_homes:
        normalized = _absolute_directory(home, label="declared user home")
        if normalized not in normalized_homes:
            normalized_homes.append(normalized)
    locations: list[tuple[Path, str]] = [
        (paths.launch_daemons_dir, "SYSTEM"),
        (shared_agents, "SHARED_USER"),
    ]
    locations.extend((home / "Library" / "LaunchAgents", "USER") for home in normalized_homes)
    references: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    inaccessible: list[dict[str, str]] = []
    inspected: list[dict[str, str]] = []
    for directory, domain in locations:
        found, malformed, result = _scan_service_directory(directory, domain=domain)
        references.extend(found)
        invalid.extend(malformed)
        inspected.append(result)
        if result["state"] in {"INACCESSIBLE", "RACED"}:
            inaccessible.append({"path": str(directory), "domain": domain, "reason": "UNAVAILABLE_OR_SYMLINK_OR_RACE"})
    # Select only after the system surface was read.  If its plist changed
    # between that observation and this strict read, it cannot be promoted to
    # a selected service merely because the launcher/root happen to match.
    try:
        selected = configured_service(
            paths.data_root,
            launch_daemons_dir=paths.launch_daemons_dir,
            account_lookup=account_lookup,
        )
    except (SystemServerServiceError, OSError):
        selected = None
    selected_interpreter = None if selected is None else str(selected.interpreter)
    selected_root = None if selected is None else str(selected.data_root)
    entries: list[dict[str, str]] = []
    for reference in references:
        canonical = (
            selected is not None
            and reference["domain"] == "SYSTEM"
            and reference["plist"] == str(paths.plist_path)
            and reference["label"] == LABEL
            and reference["interpreter"] == selected_interpreter
            and reference["data_root"] == selected_root
            and reference.get("service_account") == selected.service_account
        )
        status = "SELECTED_SYSTEM_SERVICE" if canonical else (
            "CONFLICTING_SYSTEM_SERVICE" if reference["domain"] == "SYSTEM" else "CONFLICTING_USER_SERVICE"
        )
        entries.append({**reference, "status": status})
    entries.extend(invalid)
    conflicts = [entry for entry in entries if entry["status"] != "SELECTED_SYSTEM_SERVICE"]
    uid = os.geteuid() if effective_uid is None else effective_uid
    limitations = ["ACCOUNT_HOME_DISCOVERY_NOT_AUTHORITY"]
    if uid != 0:
        limitations.append("UNPRIVILEGED_CALLER")
    if inaccessible:
        limitations.append("INACCESSIBLE_SERVICE_SURFACES")
    scope = {
        "required": "MACOS_MACHINE",
        "observed": "SYSTEM_LAUNCHDAEMONS_SHARED_AND_DECLARED_USER_LAUNCHAGENTS",
        "status": "INCOMPLETE",
        "limitations": limitations,
    }
    return {
        "selected_interpreter": selected_interpreter,
        "selected_data_root": selected_root,
        "coverage": "SYSTEM_SHARED_AND_DECLARED_USER_SERVICE_SURFACES",
        "scope": scope,
        "inspected_locations": inspected,
        "inaccessible_locations": inaccessible,
        "entries": entries,
        "conflicting_service_references": conflicts,
        "single_operational_installation": False,
        "single_operational_installation_status": "CONFLICTS_DETECTED" if conflicts else "UNVERIFIED_SCOPE",
    }
