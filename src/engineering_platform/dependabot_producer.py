"""Server-owned Dependabot discovery using CENTRAL producer bindings.

This adapter observes verified GitHub PR metadata, resolves a bounded external
identity through CENTRAL and then calls the normal submission application
service. It has no queue, execution, credential or repository authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import sqlite3
import threading
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

from . import external_producer_binding
from . import submission_service
from .providers import GitHubProvider


PRODUCER_ID = "github-dependabot"
PRODUCER_VERSION = "2.0"
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BOT_LOGINS = frozenset({"dependabot[bot]", "app/dependabot"})
HEARTBEAT_FILENAME = "dependabot-producer-heartbeat.json"
QUALIFICATION_FIXTURE_ENVIRONMENT = "EP_DEPENDABOT_QUALIFICATION_FIXTURE"


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


class _QualificationFixtureProvider:
    """Test-only GitHub boundary replacement for the installed-wheel gate."""

    def __init__(self, fixture: Path) -> None:
        try:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DependabotProducerError("QUALIFICATION_FIXTURE_INVALID") from error
        if not isinstance(payload, dict) or not all(isinstance(key, str) and isinstance(value, list) for key, value in payload.items()):
            raise DependabotProducerError("QUALIFICATION_FIXTURE_INVALID")
        self.payload = payload

    def github(self, _operation: str, endpoint: str) -> str:
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/pulls\?state=open&per_page=100", endpoint)
        if match is None:
            raise RuntimeError("qualification endpoint rejected")
        return json.dumps(self.payload.get(match.group(1).casefold(), []))


def _qualification_provider() -> GitHubProvider | None:
    """Permit an installed real producer test to fake only GitHub's response."""
    fixture = os.environ.get(QUALIFICATION_FIXTURE_ENVIRONMENT)
    if fixture is None:
        return None
    if os.environ.get("EP_QUALIFICATION_INITIALIZE_ONLY") != "1":
        raise DependabotProducerError("QUALIFICATION_FIXTURE_FORBIDDEN")
    return _QualificationFixtureProvider(Path(fixture))  # type: ignore[return-value]


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
    pull_request = _validate_pull_request(repository, pull_request)
    binding = external_producer_binding.resolve(
        connection,
        producer_type=external_producer_binding.DEPENDABOT,
        external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
        external_resource_identity=repository,
    )
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


def heartbeat_path(data_root: Path) -> Path:
    """Return the Server-owned observation record; it is never work authority."""
    return data_root / HEARTBEAT_FILENAME


def read_heartbeat(data_root: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(heartbeat_path(data_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class DependabotService:
    """A Server child that observes only currently active CENTRAL bindings."""

    def __init__(
        self,
        data_root: Path,
        *,
        provider: GitHubProvider | None = None,
        interval_seconds: float = 300.0,
        event: Any | None = None,
    ) -> None:
        self.data_root = data_root
        self.provider = provider or _qualification_provider()
        self.interval_seconds = interval_seconds
        self.event = event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recent_error: str | None = None
        self._last_discovery: str | None = None
        self._last_submission: str | None = None

    def _emit(self, name: str, context: dict[str, object]) -> None:
        if self.event is not None:
            self.event(name, context)

    def _write_heartbeat(self, *, ready: bool) -> None:
        payload = {
            "state": "READY" if ready else "DEGRADED",
            "readiness": "DISCOVERY_CAPABLE" if ready else "DISCOVERY_UNAVAILABLE",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_discovery": self._last_discovery,
            "last_submission": self._last_submission,
            "recent_error": self._recent_error,
        }
        target = heartbeat_path(self.data_root)
        temporary = target.with_suffix(".partial")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)

    def tick(self) -> int:
        """Discover all active bindings without retaining a local cursor/store."""
        database = self.data_root / "engineering.db"
        with sqlite3.connect(database) as connection:
            identities = external_producer_binding.active_external_identities(
                connection,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
            )
        admitted = 0
        for identity in identities:
            pull_requests = discover_open_pull_requests(identity, self.provider)
            with sqlite3.connect(database) as connection:
                for pull_request in pull_requests:
                    result = admit(
                        connection,
                        external_repository=identity,
                        pull_request=pull_request,
                    )
                    if not result.duplicate:
                        admitted += 1
                        self._last_submission = result.submission_id
                        self._emit("dependabot_submission_admitted", {
                            "submission_id": result.submission_id,
                            "external_resource_identity": identity,
                            "project_id": result.project_id,
                            "repository_id": result.repository_id,
                        })
        self._last_discovery = datetime.now(timezone.utc).isoformat()
        return admitted

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self._recent_error = None
                self._write_heartbeat(ready=True)
            except (DependabotProducerError, external_producer_binding.ProducerBindingError, OSError, sqlite3.Error) as error:
                self._recent_error = str(error)[:160]
                self._emit("dependabot_discovery_degraded", {"diagnostic": self._recent_error})
                self._write_heartbeat(ready=False)
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="engineering-platform-dependabot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
