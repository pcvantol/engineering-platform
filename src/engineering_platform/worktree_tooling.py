"""Deterministic local tooling preparation for isolated worktrees."""
from __future__ import annotations

from pathlib import Path
import subprocess  # nosec B404 - fixed local package bootstrap


class WorktreeToolingError(RuntimeError):
    """Raised when a declared browser runtime cannot be prepared."""


def prepare(worktree: Path) -> None:
    """Install the locked Playwright package when this worktree declares it."""
    if not (worktree / "package-lock.json").is_file() or not (worktree / "playwright.config.mjs").is_file():
        return
    completed = subprocess.run(("npm", "ci"), cwd=worktree, check=False, capture_output=True, text=True)
    if completed.returncode or not (worktree / "node_modules" / "@playwright" / "test").is_dir():
        raise WorktreeToolingError("worktree_playwright_tooling_unavailable")
