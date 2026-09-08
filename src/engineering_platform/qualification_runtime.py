"""Deterministic, local-only provider seams for installed qualification.

Never selected in a normal runtime.  The installed qualification executable
opts in explicitly, so no real Codex or GitHub write can escape its fixture.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import time

from .capability_review import ReviewerResult
from .execution_models import AgentResult, PullRequestEvidence


class DeterministicQualificationAgent:
    def __init__(self) -> None:
        self._process_callback = None

    def set_process_callback(self, callback: object) -> None:
        self._process_callback = callback

    def wait_for_controlled_interruption_arm(self, _root: Path, state: object) -> None:
        """Offer the installed recovery E2E one bounded, non-production arm window."""
        ready = os.environ.get("EP_QUALIFICATION_CONTROL_ARM_READY_FILE")
        if not ready or not Path(ready).with_suffix(Path(ready).suffix + ".enable").is_file():
            return
        ready_path = Path(ready)
        ready_path.write_text(json.dumps({"run_id": getattr(state, "run_id", None), "phase": "EXECUTE_AGENT"}), encoding="utf-8")
        continue_path = ready_path.with_suffix(ready_path.suffix + ".continue")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if continue_path.is_file():
                return
            time.sleep(.02)
        raise RuntimeError("QUALIFICATION_CONTROL_ARM_TIMED_OUT")

    def invoke(self, root: Path, prompt: str) -> AgentResult:
        if callable(self._process_callback):
            self._process_callback({"pid": os.getpid(), "process_group": os.getpgrp()})
        if "sole automatic post-finalization reconciliation" in prompt.lower():
            sha = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
            return AgentResult("COMPLETE", terminal_condition="repository_reconciled", commit_sha=sha)
        if "execution mode: genesis" in prompt.lower():
            target = next(
                (line.split(":", 1)[1].strip() for line in prompt.splitlines()
                 if line.strip().lower().startswith("target repository:")),
                "",
            )
            target_root = Path(target).resolve()
            sha = subprocess.run(("git", "-C", str(target_root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
            return AgentResult("COMPLETE", terminal_condition="local_commit_reconciled", repository_path=str(target_root), commit_sha=sha)
        is_finalization = "finalization pr on exactly" in prompt.lower()
        if self._github_write_target(root):
            return self._create_github_managed_handoff(
                root,
                finalization=is_finalization,
                prompt=prompt,
            )
        sha = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        branch = self._finalization_branch(prompt) if is_finalization else "qualification-managed"
        return AgentResult("COMPLETE", branch=branch, pull_request=1, commit_sha=sha)

    @staticmethod
    def _finalization_branch(prompt: str) -> str:
        match = re.search(
            r"finalization pr on exactly `([^`]+)`",
            prompt,
            flags=re.IGNORECASE,
        )
        if not match:
            raise RuntimeError("QUALIFICATION_FINALIZATION_BRANCH_UNAVAILABLE")
        return match.group(1)

    @staticmethod
    def _github_write_target(root: Path) -> bool:
        """Limit the external seam to its exact approved origin only."""
        if os.environ.get("EP_QUALIFICATION_GITHUB_WRITE_FLOW") != "1":
            return False
        expected = os.environ.get("EP_QUALIFICATION_GITHUB_REPOSITORY", "")
        remote = subprocess.run(("git", "-C", str(root), "remote", "get-url", "origin"), text=True, capture_output=True)  # nosec B603
        if remote.returncode:
            return False
        value = remote.stdout.strip().removesuffix(".git")
        return value.removeprefix("https://github.com/").removeprefix("git@github.com:") == expected

    @staticmethod
    def _create_github_managed_handoff(
        root: Path, *, finalization: bool = False, prompt: str = ""
    ) -> AgentResult:
        """Create one bounded dummy-repository branch and GitHub PR.

        This seam is reachable only through the explicit external qualification
        command.  It intentionally stops at the normal human merge boundary:
        the production lifecycle must not auto-merge merely because this is a
        dummy repository.
        """
        repository = os.environ.get("EP_QUALIFICATION_GITHUB_REPOSITORY", "")
        branch = os.environ.get("EP_QUALIFICATION_GITHUB_BRANCH", "")
        if finalization:
            # The host checkpoints the only permitted Finalization branch
            # before it invokes a provider.  The external qualification seam
            # must exercise that contract exactly; inventing a fixture branch
            # makes a successful remote PR unrecoverable by the host.
            branch = DeterministicQualificationAgent._finalization_branch(prompt)
        elif "controlled recovery qualification" in prompt.lower():
            # A second Managed transaction in the same fixture must retain a
            # distinct remote handoff identity after the armed interruption.
            branch = branch + "-recovery"
        if not repository or not branch:
            raise RuntimeError("QUALIFICATION_GITHUB_WRITE_CONFIGURATION_INVALID")

        def run(*args: str) -> str:
            return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()  # nosec B603

        run("git", "-C", str(root), "switch", "-c", branch)
        proof = root / ".engineering-platform" / ("managed-github-e2e-finalization-proof.json" if finalization else "managed-github-e2e-proof.json")
        proof.write_text(json.dumps({
            "branch": branch,
            "kind": "EP_MANAGED_GITHUB_E2E",
            "stage": "FINALIZATION" if finalization else "IMPLEMENTATION",
            "version": 1,
        }, sort_keys=True) + "\n", encoding="utf-8")
        run("git", "-C", str(root), "add", str(proof.relative_to(root)))
        run("git", "-C", str(root), "commit", "-m", "test: record managed GitHub qualification handoff")
        run("git", "-C", str(root), "push", "--set-upstream", "origin", branch)
        title = "test: managed GitHub qualification finalization" if finalization else "test: managed GitHub qualification"
        run("gh", "pr", "create", "--repo", repository, "--head", branch, "--base", "main", "--title", title, "--body", "Explicitly authorized Engineering Platform dummy-repository qualification.")
        number = int(run("gh", "pr", "view", branch, "--repo", repository, "--json", "number", "--jq", ".number"))
        sha = run("git", "-C", str(root), "rev-parse", "HEAD")
        return AgentResult("COMPLETE", branch=branch, pull_request=number, commit_sha=sha)

    def available(self) -> bool: return True
    # Keep the public provider-version contract valid so the normal installed
    # compatibility gate remains part of qualification.
    def version(self) -> str: return "0.153.4"
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
