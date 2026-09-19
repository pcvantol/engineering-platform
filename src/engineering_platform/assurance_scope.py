"""Host-observed delivery diff scope for independent PR-head assurance."""

from __future__ import annotations

from pathlib import Path
import re

from .providers import GitProvider
from .reconciliation_adoption import ROLLING_RECORDS


_HISTORY = re.compile(r"docs/engineering/prompt-history/\d{4}/[^/]+\.md")
_RUN = re.compile(r"docs/engineering/runs/\d{4}/[^/]+\.md")
_RUN_INDEX = frozenset({"docs/engineering/runs/index.json", "docs/engineering/runs/latest.md"})
_PREMATURE_COMPLETION = re.compile(
    r"(?:MERGED_RECONCILED|WORKSPACE_READY|(?:^|[\s\"-])(?:Completed|completed_at)\s*[:\"])",
    re.IGNORECASE,
)


def permitted_delivery_path(role: str, path: str) -> bool:
    """Apply EP's existing governance-only authority to later PR roles."""
    if role == "RECONCILIATION":
        return path in ROLLING_RECORDS
    if role == "FINALIZATION":
        return path in ROLLING_RECORDS or path in _RUN_INDEX or bool(_HISTORY.fullmatch(path) or _RUN.fullmatch(path))
    return role == "IMPLEMENTATION"  # The approved Action governs only implementation paths.


def observed_delivery_scope(root: Path, *, role: str, candidate_sha: str) -> dict[str, object]:
    """Pin the actual local PR diff to the inspected head and synchronized main."""
    if role not in {"IMPLEMENTATION", "FINALIZATION", "RECONCILIATION"}:
        raise ValueError("unknown assurance delivery role")
    git = GitProvider()
    head = git.command(root, "git", "rev-parse", "HEAD")
    try:
        base_ref = "refs/heads/main"
        base = git.command(root, "git", "rev-parse", "--verify", base_ref)
    except RuntimeError:
        # A pinned recovery checkout clones only the PR branch. Its fetched
        # protected base remains available as origin/main.
        base_ref = "refs/remotes/origin/main"
        base = git.command(root, "git", "rev-parse", "--verify", base_ref)
    if head != candidate_sha or re.fullmatch(r"[0-9a-f]{40}", base) is None:
        raise ValueError("assurance delivery diff identity changed")
    # Disable rename folding: both the deleted source and new destination
    # must be judged against the role's path authority.
    raw = git.command(root, "git", "diff", "--no-renames", "--name-only", f"{base_ref}...HEAD")
    paths = tuple(raw.splitlines()) if raw else ()
    if any(not path or path.startswith("/") or ".." in Path(path).parts for path in paths):
        raise ValueError("assurance delivery diff contains an invalid path")
    premature = []
    if role == "FINALIZATION":
        for path in paths:
            if not permitted_delivery_path(role, path):
                continue
            patch = git.command(root, "git", "diff", "--unified=0", f"{base_ref}...HEAD", "--", path)
            if any(line.startswith("+") and not line.startswith("+++") and _PREMATURE_COMPLETION.search(line[1:]) for line in patch.splitlines()):
                premature.append(path)
    if git.command(root, "git", "rev-parse", "HEAD") != candidate_sha or git.command(root, "git", "rev-parse", base_ref) != base:
        raise ValueError("assurance delivery diff changed during inspection")
    return {
        "role": role,
        "base_branch": "main",
        "base_sha": base,
        "candidate_sha": candidate_sha,
        "changed_paths": paths,
        "out_of_scope_paths": tuple(path for path in paths if not permitted_delivery_path(role, path)),
        "premature_completion_paths": tuple(premature),
    }
