"""New-process MPR fixture. Only provider and GitHub effects are simulated."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from engineering_platform.agent_state import StateStore
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import AgentResult, PullRequestEvidence
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_publication import PublicationCandidate
from tests.engineering.test_managed_adoption import LocalGitHubTransport
from tests.engineering.test_execution_host import FakeAgent, mandatory_review_result


def main():
    area, stage = Path(sys.argv[1]), sys.argv[2]
    selected = json.loads((area / "selection.json").read_text())
    root, database = area / "repo", area / "central/epdata.sqlite"
    remote_file, provider_file = area / "github-ledger.json", area / "provider-ledger.jsonl"
    def event(role):
        with provider_file.open("a") as output:
            output.write(json.dumps({"role": role, "pid": os.getpid()}) + "\n")
            output.flush()
            os.fsync(output.fileno())
    class Agent(FakeAgent):
        def invoke(self, root, prompt):
            if "Local repository validation gate" not in prompt:
                raise AssertionError("MPR_IMPLEMENTATION_REPLAY")
            event("validation_assessment")
            return AgentResult("COMPLETE", selected["branch"], commit_sha=selected["candidate_sha"])
        def review(self, root, selection, objective, evidence=None):
            event(selection.reviewer)
            return mandatory_review_result(selection.reviewer, objective)
    class GitHub:
        def publication_candidates(self, *args):
            data = json.loads(remote_file.read_text()) if remote_file.exists() else {"candidates": []}
            return [PublicationCandidate(**item) for item in data["candidates"]]
        def create_draft_publication(self, repository, branch, base, *args):
            if remote_file.exists(): raise AssertionError("MPR_DUPLICATE_CREATE")
            candidate = PublicationCandidate(71, repository, repository, branch, base, selected["candidate_sha"], "OPEN", True)
            from dataclasses import asdict
            with remote_file.open("x") as output:
                json.dump({"creates": 1, "candidates": [asdict(candidate)]}, output)
                output.flush()
                os.fsync(output.fileno())
            os._exit(91)  # real process death after the external accepted effect
        def pull_request(self, number):
            return PullRequestEvidence(number, "OPEN", True, True, head_branch=selected["branch"], base_branch="main", head_sha=selected["candidate_sha"])
        def ready(self, number): pass
        def normalize_markdown_body(self, number): return False
    store = StateStore(root / ".engineering/engineering-runs", central_database=database, emit_local_projection=False)
    runner = EngineeringRunner(root, store, SubprocessRepositoryClient(LocalGitHubTransport(area / "remote.git")), GitHub(), Agent(AgentResult("COMPLETE")), lambda _: None)
    # Credential discovery is the external provider seam; the host admission,
    # lease, owner selection, validation, Q/S and publication services are real.
    with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=()):
        state = runner.run(area / "prompt.md", run_id=selected["run_id"], resume=stage == "resume",
                           owner_authorized=stage == "start", managed_candidate=selected if stage == "start" else None)
    print(json.dumps({"phase": state.phase, "run_id": state.run_id, "pull_request": state.pull_request,
                      "repair_iterations": state.repair_iterations, "pid": os.getpid()}))


if __name__ == "__main__": main()
