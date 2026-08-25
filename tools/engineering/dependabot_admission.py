"""Bounded admission of open Dependabot pull requests into the Engineering Inbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from .producer import ENVELOPE_CONTRACT_NAME, ENVELOPE_CONTRACT_VERSION
from .providers import GitHubProvider
from .storage import EngineeringStorageError, open_storage


PRODUCER_ID = "github-dependabot"
PRODUCER_VERSION = "1.0"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BOT_LOGINS = frozenset({"dependabot[bot]", "app/dependabot"})


@dataclass(frozen=True)
class DependabotPullRequest:
    number: int
    title: str
    url: str
    head_branch: str
    head_sha: str


def configured_repository(repo: Path) -> str:
    """Read the configured GitHub repository without consulting prompt content."""
    try:
        raw = json.loads((repo / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"))
        source = raw["workspace"]["repository"]
        owner, name = source["owner"], source["name"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Dependabot admission repository configuration is unavailable.") from error
    value = f"{owner}/{name}"
    if not _REPOSITORY.fullmatch(value):
        raise EngineeringStorageError("Dependabot admission repository configuration is invalid.")
    return value


def discover_open_pull_requests(repository: str, provider: GitHubProvider | None = None) -> tuple[DependabotPullRequest, ...]:
    """Return only well-formed, open PRs authored by Dependabot.

    Discovery is read-only and deliberately REST-based, avoiding GraphQL quota
    coupling with the dashboard's richer PR projection.
    """
    if not _REPOSITORY.fullmatch(repository):
        raise EngineeringStorageError("Dependabot admission repository identity is invalid.")
    try:
        raw = json.loads((provider or GitHubProvider()).github(
            "api", f"repos/{repository}/pulls?state=open&per_page=100"
        ))
    except (RuntimeError, json.JSONDecodeError) as error:
        raise EngineeringStorageError("Dependabot pull-request discovery is temporarily unavailable.") from error
    if not isinstance(raw, list):
        raise EngineeringStorageError("Dependabot pull-request discovery returned an invalid response.")
    result: list[DependabotPullRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        head = item.get("head")
        login = user.get("login", "").casefold() if isinstance(user, dict) else ""
        if login not in _BOT_LOGINS or not isinstance(head, dict):
            continue
        number, title, url, branch, sha = (
            item.get("number"), item.get("title"), item.get("html_url"), head.get("ref"), head.get("sha")
        )
        if (
            isinstance(number, int) and number > 0
            and all(isinstance(value, str) and value.strip() for value in (title, url, branch, sha))
            and url.startswith("https://github.com/") and _SHA.fullmatch(sha)
        ):
            result.append(DependabotPullRequest(number, title.strip()[:240], url, branch.strip()[:160], sha))
    return tuple(sorted(result, key=lambda item: item.number))


def is_already_admitted(repo: Path, repository: str, pull_request: int) -> bool:
    connection = open_storage(repo)
    try:
        return connection.execute(
            "SELECT 1 FROM dependabot_admission_events WHERE repository=? AND pull_request=? AND event_type='ENQUEUED' LIMIT 1",
            (repository, pull_request),
        ).fetchone() is not None
    finally:
        connection.close()


def record_enqueued(repo: Path, repository: str, pull_request: DependabotPullRequest, submission_id: str, *, observed_at: str) -> None:
    """Append the admission decision after atomic Inbox publication."""
    connection = open_storage(repo)
    try:
        connection.execute(
            "INSERT INTO dependabot_admission_events(repository,pull_request,head_sha,head_branch,submission_id,event_type,observed_at) VALUES(?,?,?,?,?,'ENQUEUED',?)",
            (repository, pull_request.number, pull_request.head_sha, pull_request.head_branch, submission_id, observed_at),
        )
    finally:
        connection.close()


def envelope(repository: str, pull_request: DependabotPullRequest, *, submitted_at: str | None = None) -> tuple[str, str]:
    """Create an immutable external-producer envelope for the normal Managed flow."""
    timestamp = submitted_at or datetime.now(timezone.utc).isoformat()
    submission_id = f"dependabot-pr-{pull_request.number}-{pull_request.head_sha[:12]}"
    objective = f"""# Dependabot dependency pull-request review — #{pull_request.number}

Execution Mode: Managed
Execution Host Version: 1.5.0

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
for this transaction: do not create a replacement or additional implementation
pull request. Return that same pull-request number and branch as the transaction
evidence. If a required PR check fails, use the normal bounded PR-check repair
rounds only on that same branch and pull request, then revalidate.

Do not merge this pull request, enable auto-merge, alter approvals, release,
deploy, change repository settings, or expand the dependency-update scope.
The implementation and Finalization merges remain explicit operator gates. Keep
the existing Managed audit trail, validation evidence, Finalization and automatic
post-Finalization reconciliation intact.
"""
    payload = {
        "contract": {"name": ENVELOPE_CONTRACT_NAME, "version": ENVELOPE_CONTRACT_VERSION},
        "submission": {"id": submission_id, "submitted_at": timestamp, "metadata": {
            "source": "github_dependabot", "pull_request": pull_request.number, "head_sha": pull_request.head_sha,
        }},
        "producer": {
            "id": PRODUCER_ID, "type": "EXTERNAL", "version": PRODUCER_VERSION,
            "correlation_id": f"github-pr-{pull_request.number}", "mission_id": f"dependabot-pr-{pull_request.number}",
            "engineering_action_id": f"dependabot-admission-{pull_request.number}", "execution_constraint_version": "1.0",
        },
        "prompt": {"text": objective, "metadata": {"source": "github_dependabot"}},
    }
    return submission_id, json.dumps(payload, indent=2, sort_keys=True) + "\n"


def publish_envelope(inbox: Path, pull_request: DependabotPullRequest, content: str) -> str:
    """Publish atomically; partial producer data is never eligible for admission."""
    inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = f"dependabot-pr-{pull_request.number}-{uuid.uuid4().hex[:12]}.json"
    target = inbox / filename
    partial = inbox / f".{filename}.partial"
    try:
        partial.write_text(content, encoding="utf-8")
        partial.chmod(0o600)
        partial.replace(target)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise EngineeringStorageError("Dependabot Inbox-opdracht kon niet veilig worden gepubliceerd.") from error
    return filename


def inbox_contains_submission(inbox: Path, submission_id: str) -> bool:
    """Recognize a prior atomic publication after a process interruption."""
    for path in inbox.glob("dependabot-pr-*.json"):
        try:
            if f'"id": "{submission_id}"' in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False
