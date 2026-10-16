"""Existing pinned native host isolation for finite local Genesis effects.

The unchanged portable validator opens each absolute ancestor explicitly.
Allow those directories while excluding every sibling; never grant a whole
parent subtree as a fallback. Actual profile capacity is qualified before effects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from . import effect_workspace
from .effect_contract import EffectContractError
from .genesis_contract import BootstrapError
from .providers import codex_cli_executable


def runtime() -> Path:
    selected = codex_cli_executable()
    if selected is None:
        raise BootstrapError("BOOTSTRAP_NATIVE_RUNTIME_UNAVAILABLE", 422)
    try:
        native = effect_workspace.native_runtime_file(Path(selected))
        result = subprocess.run((str(native), "--version"), env=effect_workspace.child_environment(),
            capture_output=True, timeout=5, check=False)
    except (EffectContractError, OSError, subprocess.SubprocessError) as error:
        raise BootstrapError("BOOTSTRAP_NATIVE_RUNTIME_UNAVAILABLE", 422) from error
    if result.returncode or result.stdout.strip() != b"codex-cli 0.160.1":
        raise BootstrapError("BOOTSTRAP_NATIVE_RUNTIME_UNQUALIFIED", 422)
    return native


def environment(native: Path) -> dict[str, str]:
    env = effect_workspace.child_environment()
    env["PATH"] = str(native.parent) + os.pathsep + os.defpath + ":/opt/homebrew/bin"
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1")
    return env


def command(*, native: Path, anchor: Path, target: Path, writable: bool,
            git_metadata: bool = False, readable: tuple[Path, ...] = (),
            child: tuple[str, ...] | None = None, profile_id: str,
            prepared: Path | None = None) -> tuple[str, ...]:
    """No consumer-chosen commands; caller is the fixed owning host adapter."""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", profile_id):
        raise BootstrapError("BOOTSTRAP_NATIVE_PROFILE_INVALID")
    if not target.is_absolute() or ".." in target.parts or target == Path(target.anchor):
        raise BootstrapError("BOOTSTRAP_NATIVE_PLACEMENT_UNSUPPORTED", 422)
    # These are existing native FME minimal/runtime grants, not a different
    # executable, broader filesystem profile, or network-enabled fallback.
    options = effect_workspace.sandbox_options(anchor, readable=readable, runtime=native)
    filesystem = tomllib.loads("filesystem=" + options[1].split("=", 1)[1])["filesystem"]
    filesystem[str(anchor)] = "write" if prepared is not None else "read"
    filesystem[str(target)] = "write" if writable else "read"
    if prepared is not None:
        if not writable or prepared.parent != anchor or not re.fullmatch(r"s[0-9a-f]{4}", prepared.name):
            raise BootstrapError("BOOTSTRAP_PREPARATION_UNSAFE", 403)
        # The capability is the one permitted child of its owning anchor.
        # A separate writable-root binding protects it from unlink/rename.
    if git_metadata:
        if not writable:
            raise BootstrapError("BOOTSTRAP_GIT_SCOPE_INVALID")
        filesystem[str(target / ".git")] = "write"
    traversals = (target,) if prepared is None else (target, prepared)
    ancestors = {}
    for leaf in traversals:
        for ancestor in reversed(leaf.parents):
            if ancestor != Path(ancestor.anchor):
                ancestors.setdefault(ancestor, set()).add(leaf.relative_to(ancestor).parts[0])
    for ancestor, next_children in ancestors.items():
        for runtime_root in {Path(sys.prefix).absolute(), Path(sys.prefix).resolve()}:
            if runtime_root != ancestor and runtime_root.is_relative_to(ancestor):
                next_children.add(runtime_root.relative_to(ancestor).parts[0])
        if ancestor == Path(ancestor.anchor):
            continue
        if any(left != right and right.startswith(left) for left in next_children for right in next_children):
            raise BootstrapError("BOOTSTRAP_NATIVE_PLACEMENT_UNSUPPORTED", 422)
        if any(not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) for name in next_children):
            raise BootstrapError("BOOTSTRAP_NATIVE_PLACEMENT_UNSUPPORTED", 422)
        filesystem[str(ancestor)] = "write" if prepared is not None and ancestor == anchor else "read"
        # A trie partitions the complement of the finite allowed next names.
        # Traversal may need both the target branch and private prepared branch;
        # every other sibling remains denied, including CENTRAL/evidence.
        def deny_other_names(prefix, names):
            characters = {name[len(prefix)] for name in names if len(name) > len(prefix)}
            location = str(ancestor) + "/" + prefix
            if not characters:
                filesystem[location + "?*"] = "none"
                return
            allowed = ("-" if "-" in characters else "") + "".join(sorted(characters - {"-"}))
            filesystem[location + "[!" + allowed + "]*"] = "none"
            if prefix and prefix not in names:
                filesystem[location] = "none"
            for character in sorted(characters):
                deny_other_names(prefix + character, {name for name in names if name.startswith(prefix + character)})
        deny_other_names("", next_children)
    if prepared is not None:
        ancestor = anchor
        deny_other_names("", {prepared.name})
    for name, permission in list(filesystem.items()):
        if permission == "none":
            filesystem[name + "/**"] = "none"
    value = "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in filesystem.items()) + "}"
    if len(value.encode()) > 65536:
        raise BootstrapError("BOOTSTRAP_NATIVE_PROFILE_CAPACITY", 422)
    # Preserve the installed venv launcher for -m imports, as the owning
    # inspection host already does; resolving it to base Python loses EP.
    invocation = child or (sys.executable, "-I", "-m", "engineering_platform.genesis_worker")
    return (str(native), "sandbox", "-P", profile_id, "-c",
        f"permissions.{profile_id}.filesystem={value}", "-c",
        f"permissions.{profile_id}.network.enabled=false", "-C", str(anchor), "--", *invocation)
