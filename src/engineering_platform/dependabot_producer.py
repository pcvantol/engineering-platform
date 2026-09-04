"""Server-owned Dependabot discovery using CENTRAL producer bindings.

This adapter observes verified GitHub PR metadata, resolves a bounded external
identity through CENTRAL and then calls the normal submission application
service. It has no queue, execution, credential or repository authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any

from . import external_producer_binding
from . import submission_service
from .providers import GitHubProvider


PRODUCER_ID = "github-dependabot"
PRODUCER_VERSION = "2.0"
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BOT_LOGINS = frozenset({"dependabot[bot]", "app/dependabot"})


class DependabotProducerError(ValueError):
    """Stable, bounded failure from the Server-owned producer adapter."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DependabotPullRequest:
    number: int
    title: str
    url: str
    head_branch: str
    head_sha: str


def discover_open_pull_requests(
    external_repository: object,
    provider: GitHubProvider | None = None,
) -> tuple[DependabotPullRequest, ...]:
    """Read only valid Dependabot PRs for one already-bound GitHub identity."""
    repository = external_producer_binding.normalize_github_repository(external_repository)
    try:
        raw = json.loads((provider or GitHubProvider()).github(
            "api", f"repos/{repository}/pulls?state=open&per_page=100",
        ))
    except (RuntimeError, json.JSONDecodeError) as error:
        raise DependabotProducerError("DISCOVERY_UNAVAILABLE") from error
    if not isinstance(raw, list):
        raise DependabotProducerError("DISCOVERY_INVALID")
    candidates: list[DependabotPullRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user, head = item.get("user"), item.get("head")
        login = user.get("login", "").casefold() if isinstance(user, dict) else ""
        if login not in _BOT_LOGINS or not isinstance(head, dict):
            continue
        number, title, url, branch, sha = (
            item.get("number"), item.get("title"), item.get("html_url"),
            head.get("ref"), head.get("sha"),
        )
        expected_url = f"https://github.com/{repository}/pull/{number}"
        if (
            isinstance(number, int)
            and number > 0
            and all(isinstance(value, str) and value.strip() for value in (title, url, branch, sha))
            and url == expected_url
            and _SHA.fullmatch(sha)
        ):
            candidates.append(
                DependabotPullRequest(number, title.strip()[:240], url, branch.strip()[:160], sha)
            )
    return tuple(sorted(candidates, key=lambda item: item.number))


def _prompt(repository: str, pull_request: DependabotPullRequest) -> str:
    return f"""# Dependabot dependency pull-request review — #{pull_request.number}

Execution Mode: MANAGED

## Source evidence

- Producer: GitHub Dependabot
- Repository: `{repository}`
- Pull request: #{pull_request.number} — {pull_request.title}
- URL: {pull_request.url}
- Existing pull-request branch: `{pull_request.head_branch}`
- Observed head commit: `{pull_request.head_sha}`

## Objective

Perform the normal bounded Managed Engineering workflow for this already-open
Dependabot pull request. Inspect its dependency update, relevant release notes,
compatibility and repository validation. Treat pull request #{pull_request.number}
and branch `{pull_request.head_branch}` as the single implementation pull request
for this transaction. Do not merge this pull request, enable auto-merge, alter
approvals, release, deploy, change repository settings, or expand the update scope.
"""


def _validate_pull_request(repository: str, pull_request: object) -> DependabotPullRequest:
    """Defend the admission boundary even when discovery is not the caller."""
    if not isinstance(pull_request, DependabotPullRequest):
        raise DependabotProducerError("INVALID_SOURCE_METADATA")
    if (
        pull_request.number <= 0
        or not pull_request.title.strip()
        or not pull_request.head_branch.strip()
        or not _SHA.fullmatch(pull_request.head_sha)
        or pull_request.url != f"https://github.com/{repository}/pull/{pull_request.number}"
    ):
        raise DependabotProducerError("INVALID_SOURCE_METADATA")
    return pull_request


def admit(
    connection: sqlite3.Connection,
    *,
    external_repository: object,
    pull_request: DependabotPullRequest,
) -> submission_service.SubmissionResult:
    """Resolve CENTRAL authority and invoke the same canonical admission service.

    Binding resolution establishes identity only. ``submission_service.submit``
    remains the sole owner of project/repository, mode, idempotency, admission
    and lifecycle validation.
    """
    repository = external_producer_binding.normalize_github_repository(external_repository)
    binding = external_producer_binding.resolve(
        connection,
        producer_type=external_producer_binding.DEPENDABOT,
        external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
        external_resource_identity=repository,
    )
    pull_request = _validate_pull_request(repository, pull_request)
    key = f"dependabot:{repository}:{pull_request.number}:{pull_request.head_sha}"
    request = submission_service.SubmissionRequest(
        project_id=binding.project_id,
        repository_id=binding.repository_id,
        producer_id=PRODUCER_ID,
        producer_type="EXTERNAL_PRODUCER",
        producer_version=PRODUCER_VERSION,
        prompt=_prompt(repository, pull_request),
        transport="DEPENDABOT",
        idempotency_key=key,
        correlation_id=f"github-pr-{pull_request.number}",
        mission_id=f"dependabot-pr-{pull_request.number}",
        engineering_action_id=f"dependabot-admission-{pull_request.number}-{pull_request.head_sha[:12]}",
        constraints={
            "transport_principal": "DEPENDABOT",
            "external_resource_type": external_producer_binding.GITHUB_REPOSITORY,
            "external_resource_identity": repository,
            "binding_id": binding.binding_id,
            "binding_version": binding.version,
            "pull_request": pull_request.number,
            "head_sha": pull_request.head_sha,
            "source_validation": "DEPENDABOT_GITHUB_PR",
        },
    )
    return submission_service.submit(connection, request)


def discover_and_admit(
    connection: sqlite3.Connection,
    *,
    external_repository: object,
    provider: GitHubProvider | None = None,
) -> tuple[submission_service.SubmissionResult, ...]:
    """Process one bound identity; repeats converge by immutable PR-head key."""
    repository = external_producer_binding.normalize_github_repository(external_repository)
    # Resolve before discovery: an unbound external identity is neither
    # observed as a workload nor allowed to select a default project.
    external_producer_binding.resolve(
        connection,
        producer_type=external_producer_binding.DEPENDABOT,
        external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
        external_resource_identity=repository,
    )
    return tuple(admit(connection, external_repository=repository, pull_request=item) for item in discover_open_pull_requests(repository, provider))
