"""Deterministic, local-only provider seams for installed qualification.

Never selected in a normal runtime.  The installed qualification executable
opts in explicitly, so no real Codex or GitHub write can escape its fixture.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

from .capability_review import ReviewerResult
from .execution_models import AgentResult, PullRequestEvidence


class DeterministicQualificationAgent:
    def invoke(self, root: Path, prompt: str) -> AgentResult:
        sha = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        if "execution mode: genesis" in prompt.lower():
            return AgentResult("COMPLETE", terminal_condition="local_commit_reconciled", repository_path=str(root), commit_sha=sha)
        return AgentResult("COMPLETE", branch="qualification-managed", pull_request=1, commit_sha=sha)

    def available(self) -> bool: return True
    def version(self) -> str: return "qualification-deterministic-v1"
    def review(self, _root: Path, selection: object, _objective: str, evidence: object = None) -> ReviewerResult:
        return ReviewerResult(getattr(selection, "reviewer"), "Deterministic read-only assurance passed.")


class LocalQualificationGitHub:
    """A local PR/check adapter: no network, push, or GitHub mutation."""
    def __init__(self, root: Path) -> None:
        self.root, self.calls = root, 0

    def pull_request(self, number: int) -> PullRequestEvidence:
        self.calls += 1
        sha = subprocess.run(("git", "-C", str(self.root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        if self.calls == 1:
            return PullRequestEvidence(number, "OPEN", True, True, head_branch="qualification-managed", base_branch="main")
        return PullRequestEvidence(number, "MERGED", True, True, merge_commit=sha, head_branch="qualification-managed", base_branch="main")

    def pull_request_for_head_branch(self, _branch: str): return None
    def normalize_markdown_body(self, _number: int) -> bool: return False
    def ready(self, _number: int) -> None: return None
    def merge(self, _number: int) -> None: return None
