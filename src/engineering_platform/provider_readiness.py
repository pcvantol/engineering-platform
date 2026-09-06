"""Token-free readiness checks for Engineering Platform host providers.

The result is intentionally small and safe to persist or project.  Repairs are
always explicit dashboard actions; this module never opens a login flow or
retries authentication on behalf of an execution.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

from .providers import CodexCliProvider, LocalProcessProvider, codex_cli_executable


_VERSION = re.compile(r"\b\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?\b")


def _classify(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "CHECK_FAILED"
    if result.returncode == 0:
        return "READY"
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    return "AUTH_REQUIRED" if any(word in detail for word in (
        "login", "auth", "credential", "token", "not logged in", "logged out", "not signed in",
    )) else "CHECK_FAILED"


def _repository_classify(result: subprocess.CompletedProcess[str] | None) -> str:
    """Separate denied repository access from temporary GitHub API failures."""
    if result is None:
        return "CHECK_FAILED"
    if result.returncode == 0:
        return "READY"
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    if any(word in detail for word in (
        "network", "timed out", "timeout", "resolve host", "connection", "rate limit", "api",
    )):
        return "CHECK_FAILED"
    return "AUTH_REQUIRED"


def _version(result: subprocess.CompletedProcess[str] | None) -> str:
    """Return just a CLI version, never command output or diagnostics."""
    if result is None or result.returncode:
        return ""
    match = _VERSION.search(f"{result.stdout}\n{result.stderr}")
    return match.group(0) if match else ""


def runtime_details(root: Path) -> dict[str, dict[str, str]]:
    """Project token-free CLI provenance for the host-wide Console projection."""
    codex_path = codex_cli_executable() or ""
    try:
        codex_version = _version(CodexCliProvider().command("--version")) if codex_path else ""
    except OSError:
        codex_version = ""
    github_path = shutil.which("gh") or ""
    try:
        github_version = _version(LocalProcessProvider().execute(root, (github_path, "--version"))) if github_path else ""
    except OSError:
        github_version = ""
    return {
        "codex": {"executable": codex_path, "version": codex_version},
        "github": {"executable": github_path, "version": github_version},
    }


def host_status(root: Path, *, require_github: bool = True) -> dict[str, dict[str, str]]:
    """Check host authentication without deriving any checkout authority.

    This is the Server/CENTRAL projection used before a project is selected.
    It deliberately asks GitHub only whether the local CLI has an active
    account. Repository access belongs to project admission, where an actual
    canonical repository identity is available.
    """
    codex = CodexCliProvider()
    codex_installed = codex.status().qualified
    try:
        codex_result = codex.command("login", "status") if codex_installed else None
    except OSError:
        codex_result = None
    result = {
        "codex": {"provider": "CODEX", "state": "UNAVAILABLE" if not codex_installed else _classify(codex_result)},
    }
    if not require_github:
        return result
    github_path = shutil.which("gh")
    if github_path is None:
        result["github"] = {"provider": "GITHUB", "state": "UNAVAILABLE"}
        return result
    try:
        github_result = LocalProcessProvider().execute(
            root, (github_path, "auth", "status", "--hostname", "github.com"),
        )
    except OSError:
        github_result = None
    result["github"] = {"provider": "GITHUB", "state": _classify(github_result)}
    return result


def status(root: Path, *, require_github: bool = True) -> dict[str, dict[str, str]]:
    """Return provider readiness without session details, tokens, or diagnostics."""
    result = host_status(root, require_github=False)
    if not require_github:
        return result
    if shutil.which("gh") is None:
        result["github"] = {"provider": "GITHUB", "state": "UNAVAILABLE"}
        return result
    try:
        github_result = LocalProcessProvider().execute(root, ("gh", "auth", "status", "--hostname", "github.com"))
    except OSError:
        github_result = None
    github_state = _classify(github_result)
    if github_state == "READY":
        try:
            # `gh repo view --json` uses GitHub's GraphQL quota. Readiness only
            # needs a cheap repository-access proof, so use the REST endpoint
            # and avoid turning an exhausted GraphQL quota into a login repair.
            repository_result = LocalProcessProvider().execute(
                root, ("gh", "api", "repos/{owner}/{repo}", "--jq", ".full_name")
            )
        except OSError:
            repository_result = None
        github_state = _repository_classify(repository_result)
    result["github"] = {"provider": "GITHUB", "state": github_state}
    return result


def failures(root: Path, *, require_github: bool) -> tuple[str, ...]:
    """Return the provider names that must be repaired before admission."""
    return tuple(
        value["provider"]
        for value in status(root, require_github=require_github).values()
        if value["state"] != "READY"
    )
