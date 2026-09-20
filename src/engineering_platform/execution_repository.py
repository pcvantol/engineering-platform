"""Repository and GitHub coordination behind canonical providers."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import time
from typing import Protocol

from .execution_errors import RunnerError
from .execution_models import PullRequestEvidence, RepositoryEvidence
from .providers import GitProvider, GitHubProvider


GIT_SYNC_LOCK_RETRY_ATTEMPTS = 3
GIT_SYNC_LOCK_RETRY_DELAY_SECONDS = 0.5
_TRANSIENT_INDEX_LOCK_CONFLICT = re.compile(
    r"(?:index\.lock.*(?:file exists|already exists)|another git process)",
    re.IGNORECASE,
)


def github_repository_slug(remote: str) -> str:
    """Return the canonical owner/repository slug for GitHub origin forms."""
    value = remote.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    if value.startswith("https://github.com/"):
        return value.removeprefix("https://github.com/")
    return value


class RepositoryClient(Protocol):
    def inspect(self, root: Path) -> RepositoryEvidence: ...
    def main_contains(self, root: Path, sha: str) -> bool: ...
    def protected_main_revision(self, root: Path) -> str: ...
    def refresh_main_reference(self, root: Path) -> None: ...
    def remote_main_contains(self, root: Path, sha: str) -> bool: ...
    def synchronize_main(self, root: Path) -> None: ...


class GitHubClient(Protocol):
    def pull_request(self, number: int) -> PullRequestEvidence: ...
    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None: ...
    def ready(self, number: int) -> None: ...
    def normalize_markdown_body(self, number: int) -> bool: ...
    def merge(self, number: int, *, expected_head_sha: str | None = None) -> None: ...
    def delegated_merge_qualification(self, number: int, head_sha: str, *, assurance_profile: str | None = None) -> dict[str, object]: ...
    def create_or_recover_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False,
    ) -> PullRequestEvidence: ...
    def qualification_for_exact_head(self, number: int, head_sha: str) -> dict[str, object]: ...
    def version_preparation_writer(self) -> dict[str, object]: ...


class SubprocessRepositoryClient:
    """Repository workflow implementation; process ownership remains GitProvider."""
    def __init__(self, provider: GitProvider | None = None) -> None:
        self.provider = provider or GitProvider()

    def _run(self, root: Path, *args: str) -> str:
        try: return self.provider.command(root, *args)
        except RuntimeError as error: raise RunnerError(str(error)) from error

    def inspect(self, root: Path) -> RepositoryEvidence:
        if not (root / "BOOTSTRAP.md").is_file() or not (root / ".git").exists():
            raise RunnerError("this is not a repository with canonical BOOTSTRAP.md")
        remote = self._run(root, "git", "remote", "get-url", "origin")
        repository = github_repository_slug(remote)
        branch, head_sha = self._run(root, "git", "branch", "--show-current"), self._run(root, "git", "rev-parse", "HEAD")
        clean = not self._run(root, "git", "status", "--porcelain", "--untracked-files=all")
        contained = self.provider.execute(root, "git", "merge-base", "--is-ancestor", head_sha, "main").returncode == 0
        return RepositoryEvidence(repository, branch, head_sha, clean, contained)

    def main_contains(self, root: Path, sha: str) -> bool:
        return self.provider.execute(root, "git", "merge-base", "--is-ancestor", sha, "main").returncode == 0

    def refresh_main_reference(self, root: Path) -> None:
        """Refresh remote main evidence without changing the shared checkout."""
        self._synchronize_command(root, "git", "fetch", "origin", "main")

    def protected_main_revision(self, root: Path) -> str:
        """Return the freshly observed protected-main revision without checkout mutation."""
        self.refresh_main_reference(root)
        revision = self._run(root, "git", "rev-parse", "--verify", "origin/main")
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise RunnerError("protected main revision is unavailable")
        return revision

    def remote_main_contains(self, root: Path, sha: str) -> bool:
        return self.provider.execute(root, "git", "merge-base", "--is-ancestor", sha, "origin/main").returncode == 0

    @staticmethod
    def _is_transient_index_lock_conflict(error: RuntimeError) -> bool:
        """Recognize only Git's contention signal, never a permission failure."""
        return bool(_TRANSIENT_INDEX_LOCK_CONFLICT.search(str(error)))

    def _synchronize_command(self, root: Path, *args: str) -> None:
        """Run one synchronization step with bounded, non-destructive lock retries."""
        for attempt in range(1, GIT_SYNC_LOCK_RETRY_ATTEMPTS + 1):
            try:
                self._run(root, *args)
                return
            except RunnerError as error:
                if (
                    attempt == GIT_SYNC_LOCK_RETRY_ATTEMPTS
                    or not self._is_transient_index_lock_conflict(error)
                ):
                    raise
                time.sleep(GIT_SYNC_LOCK_RETRY_DELAY_SECONDS * attempt)

    def _managed_sync_diagnostics(self, root: Path) -> str:
        """Return bounded, read-only context for a failed managed synchronization."""
        execute = getattr(self.provider, "execute", None)
        if not callable(execute):
            return (
                "target_branch=main authoritative_ref=origin/main local_sha=unavailable "
                "remote_sha=unavailable configured_upstream=unavailable fast_forward_state=unknown"
            )

        def value(*args: str) -> str:
            try:
                completed = execute(root, "git", *args)
            except (OSError, RuntimeError):
                return "unavailable"
            if completed.returncode:
                return "unavailable"
            candidate = completed.stdout.strip()
            return candidate if re.fullmatch(r"[0-9a-f]{40}|[A-Za-z0-9_./@{}-]+", candidate) else "unavailable"

        local_sha = value("rev-parse", "--verify", "main")
        remote_sha = value("rev-parse", "--verify", "origin/main")
        upstream = value("rev-parse", "--abbrev-ref", "--symbolic-full-name", "main@{upstream}")
        state = "unknown"
        if local_sha != "unavailable" and remote_sha != "unavailable":
            try:
                local_is_ancestor = execute(
                    root, "git", "merge-base", "--is-ancestor", "main", "origin/main"
                ).returncode == 0
                remote_is_ancestor = execute(
                    root, "git", "merge-base", "--is-ancestor", "origin/main", "main"
                ).returncode == 0
            except (OSError, RuntimeError):
                pass
            else:
                state = (
                    "synchronized" if local_is_ancestor and remote_is_ancestor
                    else "fast_forward_possible" if local_is_ancestor
                    else "local_ahead" if remote_is_ancestor
                    else "diverged"
                )
        return (
            "target_branch=main authoritative_ref=origin/main "
            f"local_sha={local_sha} remote_sha={remote_sha} configured_upstream={upstream} "
            f"fast_forward_state={state}"
        )

    @staticmethod
    def _managed_sync_error_detail(error: RunnerError) -> str:
        """Keep a Git failure useful without persisting unbounded process output."""
        return re.sub(r"\s+", " ", str(error)).strip()[:512]

    def synchronize_main(self, root: Path) -> None:
        self._synchronize_command(root, "git", "switch", "main")
        # Managed synchronization has one authority.  Do not let any local
        # branch.*.merge, pull.*, or upstream configuration select its source.
        try:
            self._synchronize_command(root, "git", "fetch", "origin", "main")
        except RunnerError as error:
            raise RunnerError(
                "MANAGED_MAIN_FETCH_FAILED: operation=git-fetch-origin-main "
                f"{self._managed_sync_diagnostics(root)}; {self._managed_sync_error_detail(error)}"
            ) from error
        try:
            self._synchronize_command(root, "git", "merge", "--ff-only", "origin/main")
        except RunnerError as error:
            raise RunnerError(
                "MANAGED_MAIN_FAST_FORWARD_FAILED: operation=git-merge-ff-only-origin-main "
                f"{self._managed_sync_diagnostics(root)}; {self._managed_sync_error_detail(error)}"
            ) from error

    def cleanup_transaction(self, root: Path, branches: tuple[str | None, ...]) -> str:
        self._run(root, "git", "fetch", "--prune"); self.synchronize_main(root)
        if not self.inspect(root).clean: raise RunnerError("Cleanup blocked: workspace is not clean.")
        removed: list[str] = []; squash_reconciled: list[str] = []
        for branch in dict.fromkeys(branch for branch in branches if branch):
            if branch == "main": raise RunnerError("Cleanup blocked: transaction branch resolves to main.")
            if self.provider.execute(root, "git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0: continue
            if self.provider.execute(root, "git", "branch", "-d", branch).returncode:
                if self.provider.execute(root, "git", "branch", "-D", branch).returncode:
                    raise RunnerError(f"Cleanup blocked: transaction branch {branch} could not be safely removed.")
                squash_reconciled.append(branch)
            removed.append(branch)
        evidence = self.inspect(root)
        if evidence.branch != "main" or not evidence.clean or not evidence.main_contains_head:
            raise RunnerError("Cleanup blocked: main synchronization or workspace verification failed.")
        squash = f"; squash-reconciled={','.join(squash_reconciled)}" if squash_reconciled else ""
        return f"fetched/pruned; main synchronized; removed={','.join(removed) or 'already-absent'}{squash}"


class GhCliClient:
    def __init__(self, provider: GitHubProvider | None = None, repository: str | None = None) -> None:
        self.provider = provider or GitHubProvider()
        self.repository = repository

    def _github(self, *args: str) -> str:
        # REST endpoints already carry the exact repository in their path.
        # `gh api` has no `--repo` flag; only `gh pr` accepts that selector.
        scoped = (*args, "--repo", self.repository) if self.repository and args and args[0] == "pr" else args
        return self.provider.github(*scoped)

    def version_preparation_writer(self) -> dict[str, object]:
        """Read the configured writer's safe identity and repository scope.

        This is a preflight observation, never a credential issuer or branch
        protection bypass.  GitHub does not expose a general branch-write
        guarantee here, so a protected merge remains GitHub's authority.
        """
        if not self.repository or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise RunnerError("Version preparation writer requires one exact GitHub repository scope.")
        try:
            identity = json.loads(self.provider.github("api", "user"))
            repository = json.loads(self._github("api", f"repos/{self.repository}"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation writer identity could not be read.") from error
        actor = identity.get("login") if isinstance(identity, dict) else None
        permissions = repository.get("permissions") if isinstance(repository, dict) else None
        if not isinstance(actor, str) or not actor or not isinstance(permissions, dict):
            raise RunnerError("Version preparation writer identity is incomplete.")
        return {"actor": actor, "repository_id": self.repository, "can_push": permissions.get("push") is True}

    def pull_request(self, number: int) -> PullRequestEvidence:
        try: raw = json.loads(self._github("pr", "view", str(number), "--json", "number,state,isDraft,mergeCommit,statusCheckRollup,headRefName,headRefOid,baseRefName,mergeStateStatus"))
        except RuntimeError as error: raise RunnerError(str(error)) from error
        # GitHub can append an empty rollup entry to an otherwise completed
        # merged PR. It is not a check and must not keep terminal evidence in
        # an endless polling loop.
        checks = [
            item
            for item in (raw.get("statusCheckRollup") or [])
            if isinstance(item, dict) and isinstance(item.get("status"), str)
        ]
        terminal = bool(checks) and all(item.get("status") == "COMPLETED" for item in checks)
        passed = terminal and all(item.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks)
        failed = tuple(str(item.get("name") or "unnamed check") for item in checks if item.get("status") == "COMPLETED" and item.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"})
        merge = raw.get("mergeCommit") or {}
        return PullRequestEvidence(
            raw["number"], raw["state"], terminal, passed, merge.get("oid"), raw["isDraft"], failed,
            raw.get("headRefName"), raw.get("baseRefName"), raw.get("mergeStateStatus"), raw.get("headRefOid"),
        )

    def pull_request_for_head_branch(self, branch: str) -> PullRequestEvidence | None:
        """Return exactly one current PR for an already-checkpointed branch."""
        try:
            raw = json.loads(self._github(
                "pr", "list", "--head", branch, "--state", "all", "--json", "number"
            ))
        except RuntimeError as error:
            raise RunnerError(str(error)) from error
        numbers = [item.get("number") for item in raw if isinstance(item, dict) and isinstance(item.get("number"), int)]
        if not numbers:
            return None
        if len(numbers) != 1:
            raise RunnerError("Finalization recovery found more than one pull request for its checkpointed branch.")
        return self.pull_request(numbers[0])

    def create_or_recover_pull_request(
        self, branch: str, base: str, title: str, body: str, *, draft: bool = False,
    ) -> PullRequestEvidence:
        """Create one bounded PR or recover the sole existing branch identity."""
        existing = self.pull_request_for_head_branch(branch)
        if existing is not None:
            if existing.base_branch != base:
                raise RunnerError("Version preparation branch already has a pull request for another base.")
            return existing
        try:
            arguments = [
                "pr", "create", "--head", branch, "--base", base,
                "--title", title, "--body", body,
            ]
            if draft:
                arguments.append("--draft")
            raw = self._github(*arguments)
        except RuntimeError as error:
            # A successful create may lose its acknowledgement.  Only recover
            # the deterministic branch identity; never create a second PR.
            recovered = self.pull_request_for_head_branch(branch)
            if recovered is None:
                raise RunnerError(str(error)) from error
            if recovered.base_branch != base:
                raise RunnerError("Version preparation PR recovery found wrong base.") from error
            return recovered
        match = re.search(r"/pull/(\d+)(?:\s|$)", raw)
        if match is None:
            recovered = self.pull_request_for_head_branch(branch)
            if recovered is None:
                raise RunnerError("Version preparation PR create acknowledgement is ambiguous.")
            return recovered
        return self.pull_request(int(match.group(1)))

    def qualification_for_exact_head(self, number: int, head_sha: str) -> dict[str, object]:
        """Read the base branch's required checks for this exact candidate."""
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise RunnerError("Qualification requires an exact candidate SHA.")
        try:
            raw = json.loads(self._github("pr", "view", str(number), "--json", "headRefOid,baseRefOid,baseRefName,statusCheckRollup"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation qualification could not be read.") from error
        if raw.get("headRefOid") != head_sha:
            raise RunnerError("Qualification evidence belongs to a different pull request head.")
        base = raw.get("baseRefName")
        if not self.repository or not isinstance(base, str) or not base:
            raise RunnerError("Version preparation qualification lacks an exact protected base branch.")
        try:
            required = json.loads(self._github(
                "api", f"repos/{self.repository}/branches/{base}/protection/required_status_checks",
            ))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Version preparation required-check policy could not be read.") from error
        contexts = required.get("contexts") if isinstance(required, dict) else None
        protected_checks = required.get("checks") if isinstance(required, dict) else None
        names = set()
        if isinstance(contexts, list):
            names.update(item for item in contexts if isinstance(item, str) and item)
        if isinstance(protected_checks, list):
            names.update(item.get("context") for item in protected_checks if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"])
        if not names:
            raise RunnerError("Version preparation qualification has no required checks configured.")
        checks = [item for item in (raw.get("statusCheckRollup") or []) if isinstance(item, dict) and isinstance(item.get("status"), str)]
        if not checks or any(item.get("status") != "COMPLETED" for item in checks):
            raise RunnerError("Version preparation qualification is incomplete.")
        failed = [str(item.get("name") or "unnamed check") for item in checks if item.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"}]
        if failed:
            raise RunnerError("Version preparation qualification failed: " + ", ".join(failed))
        observed = {item.get("name") for item in checks if isinstance(item.get("name"), str) and item["name"]}
        missing = sorted(names - observed)
        if missing:
            raise RunnerError("Version preparation qualification is missing required checks: " + ", ".join(missing))
        return {"pull_request_id": number, "exact_qualified_sha": head_sha, "base_revision": raw.get("baseRefOid"), "required_checks": sorted(names), "strict_checks": required.get("strict") is True, "checks": checks, "conclusion": "PASS"}
    def ready(self, number: int) -> None:
        try: self._github("pr", "ready", str(number))
        except RuntimeError as error:
            if "already ready" not in str(error).lower(): raise RunnerError(str(error)) from error

    def normalize_markdown_body(self, number: int) -> bool:
        """Repair only a fully escaped PR body generated by an agent."""
        try:
            raw = json.loads(self._github("pr", "view", str(number), "--json", "body"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Pull request Markdown could not be inspected.") from error
        body = raw.get("body") if isinstance(raw, dict) else None
        # Preserve real Markdown, including code snippets that deliberately
        # contain an escaped newline.  Only a one-line serialized body is bad.
        if not isinstance(body, str) or "\n" in body or "\\n" not in body:
            return False
        normalized = body.replace("\\r\\n", "\n").replace("\\n", "\n")
        try:
            self._github("pr", "edit", str(number), "--body", normalized)
        except RuntimeError as error:
            raise RunnerError("Pull request Markdown could not be normalized.") from error
        return True
    def _autonomous_effective_policy(self, *, assurance_profile: str = "qualification-autonomous-qs@1") -> dict[str, object]:
        """Read all active main rules and classic protection without inferring zero from errors."""
        if (assurance_profile == "qualification-autonomous-qs@1"
                and self.repository != "pcvantol/forge-mission-qualification"):
            raise RunnerError("Autonomous assurance profile has a different repository scope.")
        if assurance_profile not in {"qualification-autonomous-qs@1", "repository-autonomous-qs@1"}:
            raise RunnerError("Unknown delegated merge assurance profile.")
        repository_profile = assurance_profile == "repository-autonomous-qs@1"
        if repository_profile and (not isinstance(self.repository, str)
                                   or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None):
            raise RunnerError("Autonomous assurance requires an exact repository.")
        try:
            raw_rules = json.loads(self._github("api", f"repos/{self.repository}/rules/branches/main?per_page=100"))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Effective GitHub ruleset policy could not be read.") from error
        if not isinstance(raw_rules, list) or len(raw_rules) >= 100:
            raise RunnerError("Effective GitHub ruleset policy is incomplete.")
        rulesets: dict[int, list[dict[str, object]]] = {}
        for rule in raw_rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("ruleset_id"), int):
                raise RunnerError("Effective GitHub ruleset identity is incomplete.")
            rulesets.setdefault(rule["ruleset_id"], []).append(rule)
        verified_rules: list[dict[str, object]] = []
        for ruleset_id, applied in sorted(rulesets.items()):
            try:
                detail = json.loads(self._github("api", f"repos/{self.repository}/rulesets/{ruleset_id}"))
            except (RuntimeError, json.JSONDecodeError) as error:
                raise RunnerError("Effective GitHub ruleset detail could not be read.") from error
            if (not isinstance(detail, dict) or detail.get("id") != ruleset_id
                    or detail.get("target") != "branch" or detail.get("enforcement") != "active"
                    or detail.get("bypass_actors") != [] or not isinstance(detail.get("rules"), list)):
                raise RunnerError("Effective GitHub ruleset detail is incomplete or permits bypass.")
            detailed = detail["rules"]
            if sorted(json.dumps({"type": item.get("type"), "parameters": item.get("parameters")}, sort_keys=True)
                      for item in applied) != sorted(json.dumps({"type": item.get("type"), "parameters": item.get("parameters")}, sort_keys=True)
                      for item in detailed if isinstance(item, dict)):
                raise RunnerError("Effective GitHub ruleset changed during qualification.")
            verified_rules.extend(applied)
        try:
            classic = json.loads(self._github("api", f"repos/{self.repository}/branches/main/protection"))
        except RuntimeError as error:
            # A 404 is explained only by independently verified, active and
            # sufficient ruleset protection. Other failures remain unknown.
            if not re.search(r"\bHTTP 404\b", str(error)) or not verified_rules:
                raise RunnerError("Classic GitHub protection policy could not be read.") from error
            classic = None
        except json.JSONDecodeError as error:
            raise RunnerError("Classic GitHub protection policy is malformed.") from error
        if classic is not None and not isinstance(classic, dict):
            raise RunnerError("Classic GitHub protection policy is malformed.")
        if classic is not None:
            enforcement = classic.get("enforce_admins")
            if not isinstance(enforcement, dict) or enforcement.get("enabled") is not True:
                raise RunnerError("Autonomous assurance requires classic protection to apply to administrators.")
            for setting in ("allow_force_pushes", "allow_deletions"):
                value = classic.get(setting)
                if not isinstance(value, dict) or value.get("enabled") is not False:
                    raise RunnerError("Autonomous assurance requires protected main to reject direct destructive writes.")
        required_checks: set[str] = set()
        required_apps: dict[str, set[int]] = {}
        strict_values: list[bool] = []
        required_approvals: list[int] = []
        required_thread_resolution = False
        classic_conversations = classic.get("required_conversation_resolution") if classic else None
        if repository_profile and classic_conversations is not None:
            if (not isinstance(classic_conversations, dict)
                    or not isinstance(classic_conversations.get("enabled"), bool)):
                raise RunnerError("Classic review-thread policy is incomplete.")
            required_thread_resolution = classic_conversations["enabled"]
        classic_checks = classic.get("required_status_checks") if classic else None
        if classic_checks is not None:
            if not isinstance(classic_checks, dict) or not isinstance(classic_checks.get("strict"), bool):
                raise RunnerError("Classic required-check policy is incomplete.")
            if not isinstance(classic_checks.get("contexts"), list) or not isinstance(classic_checks.get("checks"), list):
                raise RunnerError("Classic required-check policy is incomplete.")
            strict_values.append(classic_checks["strict"])
            required_checks.update(item for item in classic_checks.get("contexts", []) if isinstance(item, str) and item)
            required_checks.update(item["context"] for item in classic_checks.get("checks", [])
                                   if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"])
            for item in classic_checks["checks"]:
                if not isinstance(item, dict) or not isinstance(item.get("context"), str) or not item["context"]:
                    raise RunnerError("Classic required-check identity is incomplete.")
                app_id = item.get("app_id")
                if app_id is not None:
                    if isinstance(app_id, bool) or not isinstance(app_id, int):
                        raise RunnerError("Classic required-check application identity is invalid.")
                    if app_id >= 0:
                        required_apps.setdefault(item["context"], set()).add(app_id)
        classic_reviews = classic.get("required_pull_request_reviews") if classic else None
        if classic_reviews is not None:
            if not isinstance(classic_reviews, dict):
                raise RunnerError("Classic review policy is incomplete.")
            count = classic_reviews.get("required_approving_review_count")
            if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                    or classic_reviews.get("require_code_owner_reviews") is True
                    or classic_reviews.get("require_last_push_approval") is True
                    or (classic_reviews.get("required_review_thread_resolution") is True and not repository_profile)):
                raise RunnerError("Classic review policy requires unsupported or unknown approval evidence.")
            required_approvals.append(count)
            required_thread_resolution |= classic_reviews.get("required_review_thread_resolution") is True
        supported = {"required_status_checks", "pull_request", "non_fast_forward", "deletion", "required_linear_history"}
        for rule in verified_rules:
            rule_type, parameters = rule.get("type"), rule.get("parameters")
            if rule_type not in supported:
                raise RunnerError("Effective GitHub ruleset contains an unsupported rule.")
            if rule_type == "required_status_checks":
                if not isinstance(parameters, dict) or not isinstance(parameters.get("strict_required_status_checks_policy"), bool) or not isinstance(parameters.get("required_status_checks"), list):
                    raise RunnerError("Ruleset required-check policy is incomplete.")
                strict_values.append(parameters["strict_required_status_checks_policy"])
                for item in parameters["required_status_checks"]:
                    if not isinstance(item, dict) or not isinstance(item.get("context"), str) or not item["context"]:
                        raise RunnerError("Ruleset required-check policy is incomplete.")
                    required_checks.add(item["context"])
                    app_id = item.get("integration_id")
                    if app_id is not None:
                        if isinstance(app_id, bool) or not isinstance(app_id, int):
                            raise RunnerError("Ruleset required-check application identity is invalid.")
                        if app_id >= 0:
                            required_apps.setdefault(item["context"], set()).add(app_id)
            elif rule_type == "pull_request":
                if not isinstance(parameters, dict):
                    raise RunnerError("Ruleset pull-request policy is incomplete.")
                if repository_profile:
                    supported_parameters = {
                        "allowed_merge_methods", "dismiss_stale_reviews_on_push",
                        "require_code_owner_review", "require_extra_approval_for_unattributed_changes",
                        "require_last_push_approval", "required_approving_review_count",
                        "required_review_thread_resolution", "required_reviewers",
                    }
                    if (set(parameters) - supported_parameters
                            or parameters.get("require_extra_approval_for_unattributed_changes") is True
                            or parameters.get("require_last_push_approval") is True
                            or ("allowed_merge_methods" in parameters
                                and (not isinstance(parameters["allowed_merge_methods"], list)
                                     or "squash" not in parameters["allowed_merge_methods"]))):
                        raise RunnerError("Ruleset pull-request policy has an unsupported merge condition.")
                count = parameters.get("required_approving_review_count")
                if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                        or parameters.get("require_code_owner_review") is True
                        or (parameters.get("required_review_thread_resolution") is True and not repository_profile)
                        or parameters.get("required_reviewers")):
                    raise RunnerError("Ruleset review policy requires unsupported or unknown approval evidence.")
                required_approvals.append(count)
                required_thread_resolution |= parameters.get("required_review_thread_resolution") is True
        if not required_checks or not strict_values or not all(strict_values):
            raise RunnerError("Autonomous assurance requires strict protected status checks.")
        effective_rule_types = {rule.get("type") for rule in verified_rules}
        if classic_reviews is None and "pull_request" not in effective_rule_types:
            raise RunnerError("Effective protection lacks a required pull-request rule.")
        if classic is None and not {"non_fast_forward", "deletion"} <= effective_rule_types:
            raise RunnerError("Ruleset-only protection lacks force-push and deletion prevention.")
        policy = {"source": "github-effective-main", "classic": classic,
                  "rulesets": verified_rules, "required_checks": sorted(required_checks),
                  "required_apps": {name: sorted(apps) for name, apps in sorted(required_apps.items())},
                  "required_approvals": max(required_approvals, default=0), "strict_checks": True,
                  "required_thread_resolution": required_thread_resolution}
        if repository_profile:
            # GitHub embeds observation URLs and other changing metadata in
            # protection responses. Bind only the effective rule semantics.
            classic_binding = None
            if classic is not None:
                classic_binding = {
                    "enforce_admins": classic["enforce_admins"]["enabled"],
                    "allow_force_pushes": classic["allow_force_pushes"]["enabled"],
                    "allow_deletions": classic["allow_deletions"]["enabled"],
                    "required_conversation_resolution": None if classic_conversations is None
                        else classic_conversations["enabled"],
                    "required_status_checks": None if classic_checks is None else {
                        "strict": classic_checks["strict"],
                        "contexts": sorted(classic_checks["contexts"]),
                        "checks": sorted(({"context": item["context"],
                                           "app_id": item.get("app_id")}
                                          for item in classic_checks["checks"]),
                                         key=lambda item: (item["context"], str(item["app_id"]))),
                    },
                    "required_pull_request_reviews": None if classic_reviews is None else {
                        "required_approving_review_count": classic_reviews["required_approving_review_count"],
                        "dismiss_stale_reviews": classic_reviews.get("dismiss_stale_reviews", False),
                        "require_code_owner_reviews": classic_reviews.get("require_code_owner_reviews", False),
                        "require_last_push_approval": classic_reviews.get("require_last_push_approval", False),
                        "required_review_thread_resolution": classic_reviews.get("required_review_thread_resolution", False),
                    },
                }
            stable_rules = []
            for item in verified_rules:
                parameters = item.get("parameters")
                if isinstance(parameters, dict):
                    parameters = dict(parameters)
                    if item["type"] == "required_status_checks":
                        parameters["required_status_checks"] = sorted(
                            parameters["required_status_checks"],
                            key=lambda check: (check["context"], str(check.get("integration_id"))))
                    elif item["type"] == "pull_request" and "allowed_merge_methods" in parameters:
                        parameters["allowed_merge_methods"] = sorted(parameters["allowed_merge_methods"])
                stable_rules.append({"ruleset_id": item["ruleset_id"],
                                     "type": item["type"], "parameters": parameters})
            policy_binding = {
                "version": "repository-effective-main/v1",
                "repository": self.repository,
                "classic": classic_binding,
                "rulesets": sorted(stable_rules,
                                    key=lambda item: (item["ruleset_id"], item["type"],
                                                      json.dumps(item["parameters"], sort_keys=True))),
                "required_checks": policy["required_checks"],
                "required_apps": policy["required_apps"],
                "required_approvals": policy["required_approvals"],
                "required_thread_resolution": required_thread_resolution,
                "strict_checks": True,
            }
        else:
            # Preserve the original synthetic profile's immutable digest semantics.
            policy_binding = {key: value for key, value in policy.items()
                              if key != "required_thread_resolution"}
        policy["digest"] = "sha256:" + hashlib.sha256(json.dumps(policy_binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return policy

    def delegated_merge_qualification(self, number: int, head_sha: str, *, assurance_profile: str | None = None) -> dict[str, object]:
        """Fresh protected checks and independent head-bound GitHub reviews."""
        autonomous = assurance_profile in {"qualification-autonomous-qs@1", "repository-autonomous-qs@1"}
        if assurance_profile is not None and not autonomous:
            raise RunnerError("Unknown delegated merge assurance profile.")
        policy = self._autonomous_effective_policy(assurance_profile=assurance_profile) if autonomous else None
        if policy is None:
            checks = self.qualification_for_exact_head(number, head_sha)
        else:
            if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
                raise RunnerError("Qualification requires an exact candidate SHA.")
            try:
                raw = json.loads(self._github("pr", "view", str(number), "--json", "headRefOid,baseRefOid,baseRefName,statusCheckRollup"))
            except (RuntimeError, json.JSONDecodeError) as error:
                raise RunnerError("Autonomous PR qualification could not be read.") from error
            if not isinstance(raw, dict) or raw.get("headRefOid") != head_sha or raw.get("baseRefName") != "main":
                raise RunnerError("Autonomous PR qualification has a changed head or base.")
            rollup = raw.get("statusCheckRollup")
            if not isinstance(rollup, list) or not rollup:
                raise RunnerError("Autonomous PR required checks are incomplete or failed.")
            observed: set[str] = set()
            for item in rollup:
                if not isinstance(item, dict):
                    raise RunnerError("Autonomous PR required checks are incomplete or failed.")
                if item.get("__typename") == "StatusContext" or (
                    "context" in item and "state" in item and "name" not in item
                ):
                    name = item.get("context")
                    passed = item.get("state") == "SUCCESS"
                else:
                    name = item.get("name")
                    passed = (item.get("status") == "COMPLETED" and
                              item.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"})
                if not isinstance(name, str) or not name or not passed:
                    raise RunnerError("Autonomous PR required checks are incomplete or failed.")
                observed.add(name)
            if not set(policy["required_checks"]) <= observed:
                raise RunnerError("Autonomous PR is missing effective required checks.")
            if policy["required_apps"]:
                try:
                    runs = json.loads(self._github("api", f"repos/{self.repository}/commits/{head_sha}/check-runs?per_page=100"))
                except (RuntimeError, json.JSONDecodeError) as error:
                    raise RunnerError("Required check application identity could not be read.") from error
                check_runs = runs.get("check_runs") if isinstance(runs, dict) else None
                if (not isinstance(check_runs, list) or len(check_runs) >= 100
                        or runs.get("total_count") != len(check_runs)):
                    raise RunnerError("Required check application identity is incomplete.")
                for name, app_ids in policy["required_apps"].items():
                    for app_id in app_ids:
                        if not any(item.get("name") == name and item.get("head_sha") == head_sha
                                   and item.get("status") == "completed"
                                   and item.get("conclusion") in {"success", "neutral", "skipped"}
                                   and isinstance(item.get("app"), dict)
                                   and item["app"].get("id") == app_id
                                   for item in check_runs if isinstance(item, dict)):
                            raise RunnerError("Required check application identity does not match policy.")
            checks = {"pull_request_id": number, "exact_qualified_sha": head_sha,
                      "base_revision": raw.get("baseRefOid"), "required_checks": policy["required_checks"],
                      "strict_checks": True, "checks": rollup, "conclusion": "PASS",
                      "effective_policy": {"digest": policy["digest"], "source": policy["source"],
                                           "ruleset_ids": sorted({item["ruleset_id"] for item in policy["rulesets"]}),
                                           "required_approvals": policy["required_approvals"]}}
        if checks.get("strict_checks") is not True:
            raise RunnerError("Delegated merge requires strict protected status checks against the current base.")
        if not self.repository:
            raise RunnerError("Delegated merge requires an exact GitHub repository.")
        try:
            pull = json.loads(self._github("api", f"repos/{self.repository}/pulls/{number}"))
            reviews = json.loads(self._github("api", f"repos/{self.repository}/pulls/{number}/reviews?per_page=100"))
            protection = ({"required_approving_review_count": policy["required_approvals"]} if policy is not None else
                          json.loads(self._github("api", f"repos/{self.repository}/branches/main/protection/required_pull_request_reviews")))
        except (RuntimeError, json.JSONDecodeError) as error:
            raise RunnerError("Delegated merge review policy could not be verified.") from error
        if (not isinstance(pull, dict) or pull.get("number") != number
                or pull.get("state") != "open" or pull.get("draft") is True
                or (pull.get("head") or {}).get("sha") != head_sha
                or (pull.get("base") or {}).get("ref") != "main"
                or not isinstance(reviews, list) or len(reviews) >= 100
                or not isinstance(protection, dict)):
            raise RunnerError("Delegated merge PR identity changed during review qualification.")
        author = (pull.get("user") or {}).get("login")
        required = protection.get("required_approving_review_count")
        if isinstance(required, bool) or not isinstance(required, int) or required < (0 if autonomous else 1):
            raise RunnerError("Delegated merge requires protected independent review policy.")
        latest: dict[str, tuple[str, str]] = {}
        for review in reviews:
            if not isinstance(review, dict) or not isinstance(review.get("user"), dict):
                raise RunnerError("Delegated merge review evidence is incomplete.")
            reviewer = review["user"].get("login")
            decision = review.get("state")
            if not isinstance(reviewer, str) or not reviewer or not isinstance(decision, str):
                raise RunnerError("Delegated merge review evidence is incomplete.")
            latest[reviewer] = (decision, str(review.get("commit_id")))
        if any(decision == "CHANGES_REQUESTED" for decision, _commit in latest.values()):
            raise RunnerError("Delegated merge has an unresolved changes-requested review.")
        approvals = sorted(
            reviewer for reviewer, (decision, commit) in latest.items()
            if reviewer != author and decision == "APPROVED" and commit == head_sha
        )
        if len(approvals) < required:
            raise RunnerError("Delegated merge lacks required independent approvals for this exact head.")
        if policy is not None and policy["required_thread_resolution"]:
            owner, repository_name = self.repository.split("/", 1)
            query = ("query { repository(owner:\"" + owner + "\",name:\"" + repository_name
                     + "\") { pullRequest(number:" + str(number)
                     + ") { reviewThreads(first:100) { totalCount nodes { isResolved } } } } }")
            try:
                response = json.loads(self._github("api", "graphql", "-f", "query=" + query))
                threads = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                raise RunnerError("Required review-thread resolution could not be read.") from error
            nodes = threads.get("nodes") if isinstance(threads, dict) else None
            if (not isinstance(nodes, list) or not isinstance(threads.get("totalCount"), int)
                    or threads["totalCount"] != len(nodes) or len(nodes) >= 100
                    or any(not isinstance(item, dict) or item.get("isResolved") is not True
                           for item in nodes)):
                raise RunnerError("Delegated merge has unresolved or unreadable review threads.")
        return {**checks, "reviewers": approvals, "required_approvals": required}

    def merge(self, number: int, *, expected_head_sha: str | None = None) -> None:
        try:
            if expected_head_sha is None:
                self._github("pr", "merge", str(number), "--squash", "--delete-branch")
            elif self.repository and re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
                self._github("api", "--method", "PUT", f"repos/{self.repository}/pulls/{number}/merge",
                             "-f", "merge_method=squash", "-f", f"sha={expected_head_sha}")
            else:
                raise RunnerError("Delegated merge requires an exact repository and head SHA.")
        except RuntimeError as error:
            raise RunnerError(str(error)) from error
