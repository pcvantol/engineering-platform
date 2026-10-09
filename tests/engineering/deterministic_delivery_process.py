"""Process qualification driver: only external model/GitHub transports doubled."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from engineering_platform.agent_state import StateStore
from engineering_platform.central_database import DATABASE_FILENAME
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_publication import PublicationCandidate
from tests.engineering.test_managed_adoption import LocalGitHubTransport
from tests.engineering.test_execution_host import mandatory_review_result


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    mode, boundary = sys.argv[2:4]
    root, data = Path(spec["root"]), Path(spec["data"])
    remote_receipt, turns = data / "remote-receipt.json", data / "model-turns.jsonl"
    selection = spec["selection"]
    from tests.engineering.inline_review_backend import InlineReviewBackend
    class Agent(InlineReviewBackend):
        def available(self): return True
        def version(self): return "0.160.1"
        def invoke(self, *args):
            raise AssertionError("mechanical delivery replayed implementation/model validation")
        def review(self, root, selected, objective, evidence=None):
            with turns.open("a") as output:
                output.write(json.dumps({"role": selected.reviewer}) + "\n")
            if mode == "start" and boundary == "assurance":
                os._exit(73)
            return mandatory_review_result(selected.reviewer, objective)
    class GitHub:
        def publication_candidates(self, *args):
            return [PublicationCandidate(**json.loads(remote_receipt.read_text()))] if remote_receipt.exists() else []
        def create_draft_publication(self, repository, branch, base, title, body):
            # Exclusive write proves a second external create is not attempted.
            with remote_receipt.open("x") as output:
                json.dump(dict(number=71, repository=repository, head_repository=repository,
                               branch=branch, base=base, head_sha=selection["candidate_sha"],
                               state="OPEN", draft=True), output)
                output.flush()
                os.fsync(output.fileno())
            if mode == "start" and boundary == "publication":
                os._exit(73)
        def pull_request(self, number):
            return PullRequestEvidence(number, "OPEN", True, True,
                                       head_branch=selection["branch"], base_branch="main",
                                       head_sha=selection["candidate_sha"])
        def ready(self, number): pass
        def normalize_markdown_body(self, number): return False
    store = StateStore(root / ".engineering/engineering-runs",
                       central_database=data / DATABASE_FILENAME, emit_local_projection=False)
    runner = EngineeringRunner(root, store,
                               SubprocessRepositoryClient(LocalGitHubTransport(Path(spec["remote"]))),
                               GitHub(), Agent(), lambda _: None)
    # Authentication/network are external transports, not control or MPR logic.
    with patch("engineering_platform.execution_host.provider_readiness_failures", return_value=()):
        result = runner.run(Path(spec["prompt"]), run_id="adopt-run", resume=mode == "resume",
                            owner_authorized=mode == "start",
                            managed_candidate=selection if mode == "start" else None)
    if result.phase != "WAIT_FOR_OPERATOR_MERGE" or result.pull_request != 71:
        raise AssertionError(result)


if __name__ == "__main__":
    main()
