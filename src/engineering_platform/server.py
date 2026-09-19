"""Standalone Engineering Platform Server foundation.

This module intentionally owns no project, Agent transport, credential, or
execution authority.  It is the installation-owned runtime boundary on which
those later capabilities can be composed.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import http.server
from ipaddress import ip_address
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import select
import shlex
import shutil
import signal
import sqlite3
# The lifecycle starts this module with a fixed argv; no shell is used.
import subprocess  # nosec B404
import sys
import time
from threading import Lock, RLock, Timer
from typing import Callable, Mapping, Protocol
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit
from uuid import uuid4

from . import agent_trust
from . import central_database
from . import merge_delegation
from . import central_data_transfer
from . import central_operational_reset
from . import console_route_ownership
from . import console_presentation
from . import development_profile
from . import server_console_services
from . import dashboard_translation
from . import dependabot_producer
from . import external_producer_binding
from . import file_inbox
from . import host_admin
from . import installation_relocation
from . import (
    installation_update_admission,
    installation_update_activation,
    installation_update_backup,
    installation_update_composition,
    installation_update_executor,
    installation_update_migration,
    installation_update_operation,
    installation_update_plan,
    installation_update_preparation,
    legacy_installation_adoption,
    operational_installation_record,
)
from . import operational_installation
from . import owner_credential_recovery
from . import product_installation_readback
from . import local_repository_binding
from . import project_topology
from . import submission_service
from . import server_relay
from . import server_service
from . import system_server_service
from . import storage
from . import telemetry_export
from . import managed_codex_runtime
from . import provider_readiness
from .platform_components import (
    PLATFORM_COMPONENT_BY_ID,
    PLATFORM_COMPONENT_IDS,
    PLATFORM_COMPONENTS,
    PLATFORM_COMPONENT_ROUTE_PATTERN,
    RETIRED_COMPONENT_ALIAS_ROUTE_PATTERN,
)
from .component_logging import (
    LOG_LEVELS_AT_OR_ABOVE,
    MAX_COMPONENT_LOG_PAGE_SIZE,
    VALID_LEVELS,
    component_logger,
    log_event,
)
from .agent_state import (
    MAX_COMMIT_EVIDENCE_RECORDS,
    is_valid_commit_evidence_record,
    redact_diagnostic,
)
from .codex_chat import (
    CHAT_RETENTION_DAYS,
    MAX_HISTORY_ITEMS,
    CodexChatError,
    chat_model,
    respond_with_context,
)
from .ep_consumer_credentials import verifier
from .execution_lifecycle import projection as lifecycle_projection
from .execution_timing import timing_summaries, timing_summary
from .provider_usage import provider_usage_summaries, provider_usage_summary
from .telemetry_contract import load_lineage_graph, run_telemetry_snapshot
from .telemetry_metrics import (
    VALID_SUBTOTAL,
    aggregate_coverage as aggregate_telemetry_coverage,
    aggregate_numeric_metric,
    metric_coverage,
)
from .parity_context import ParityProjectStore, project_context
from .platform_version import CURRENT_PLATFORM_VERSION, EngineeringPlatformManifest
from .providers import (
    GitHubProvider,
    CodexCliProvider,
    LaunchdProvider,
    LaunchdRuntimeDetails,
    MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT,
    LocalProcessProvider,
    default_engineering_platform_codex_cli_prefix,
)
from .report_analysis import RETRYABLE_REPORT_ANALYSIS_STATUSES, analyze as analyze_terminal_report
from .resources import package_path


SERVER_CONFIGURATION_FILENAME = "server.json"
SERVER_IDENTITY_FILENAME = "runtime-identity.json"
SERVER_RUNTIME_FILENAME = "runtime.json"
SERVER_DATABASE_FILENAME = central_database.DATABASE_FILENAME
MAX_TELEMETRY_DAY_RUNS = 100
SERVER_CONFIGURATION_VERSION = 3
# ADR-0026 defines the first standalone store as the canonical schema-40
# product definitions plus immutable control provenance.  This server-owned
# bootstrap is deliberately separate from the retired predecessor migration
# machinery: it creates a clean installation only and never accepts a source
# database path.
SERVER_STORE_SCHEMA_VERSION = 71
SERVER_ENVIRONMENT_DATA_ROOT = "EP_SERVER_DATA_ROOT"
FILE_INBOX_DIRECTORY = "file-inbox"
HTTP_JSON_OPENAPI_PATH = "/v1/openapi.json"
_CENTRAL_LOG_SORT_COLUMNS = {
    "line": "id",
    "timestamp": "created_at",
    "level": "json_extract(payload, '$.level')",
    "event": "json_extract(payload, '$.event')",
    "runId": "COALESCE(json_extract(payload, '$.run_id'), '')",
    "details": "COALESCE(json_extract(payload, '$.diagnostic'), '')",
}
_CHILDREN: dict[int, subprocess.Popen[object]] = {}
_SAFE_ATTACHMENT_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_REPORT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PROVIDER_LOGIN_LOCK = Lock()
_PROVIDER_INSTALL_LOCK = Lock()
_CODEX_RATE_LIMIT_CACHE: tuple[float, bytes] | None = None
_CODEX_RATE_LIMIT_CACHE_LOCK = Lock()
_CODEX_IDENTITY_CACHE: tuple[float, dict[str, str]] | None = None
_CODEX_IDENTITY_CACHE_LOCK = Lock()
_DASHBOARD_AUDIT_ACTION_PATTERN = re.compile(r"[a-z][a-z0-9_]{2,95}")


def _http_json_openapi_document() -> dict[str, object]:
    """Return the published contract for the canonical HTTP JSON ingress.

    The document is intentionally local and versioned with the Server: it is
    an interface description, not a separate dashboard-owned API surface.
    """
    submission_properties: dict[str, object] = {
        "repository_id": {"type": "string"},
        "producer": {
            "type": "object",
            "required": ["id", "type"],
            "properties": {
                "id": {"type": "string"},
                "type": {"type": "string"},
                "version": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "prompt": {"type": "string"},
        "idempotency_key": {"type": "string"},
        "correlation_id": {"type": "string"},
        "mission_id": {"type": "string"},
        "engineering_action_id": {"type": "string"},
        "constraints": {"type": "object", "additionalProperties": True},
        "transport_receipt_id": {"type": "string"},
        "transport_received_at": {"type": "string", "format": "date-time"},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Engineering Platform HTTP JSON API",
            "version": "1",
            "description": "Canonical authenticated submission ingress and platform health.",
        },
        "paths": {
            "/v1/producer-compatibility": {
                "get": {
                    "summary": "Read producer compatibility and optional authenticated consumer binding",
                    "description": "Without scope headers this returns the public installation declaration v1.0. With EP-Project-ID and EP-Repository-ID it requires a scoped bearer and returns v1.1 with EP-derived consumer identity and submission scope; it never admits or executes a submission.",
                    "security": [{}, {"consumerBearer": []}],
                    "parameters": [
                        {"name": "EP-Project-ID", "in": "header", "required": False, "schema": {"type": "string"}},
                        {"name": "EP-Repository-ID", "in": "header", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "Public v1.0 or authenticated scoped v1.1 compatibility declaration"},
                        "401": {"description": "Scoped declaration credential is absent or invalid"},
                        "403": {"description": "Credential or repository does not match the requested scope"},
                    },
                },
            },
            "/health": {
                "get": {
                    "summary": "Read aggregated Engineering Platform component health",
                    "description": "Returns every canonical Platform Component. The aggregate is healthy only when every critical component is healthy.",
                    "responses": {
                        "200": {"description": "Critical Platform Components are healthy"},
                        "503": {"description": "One or more critical Platform Components are degraded"},
                    },
                },
            },
            "/api/health": {
                "get": {
                    "summary": "Read aggregated Engineering Platform component health (compatibility alias)",
                    "description": "Compatibility alias for /health. It is platform-scoped and never requires a selected Console project.",
                    "responses": {
                        "200": {"description": "Critical Platform Components are healthy"},
                        "503": {"description": "One or more critical Platform Components are degraded"},
                    },
                },
            },
            "/healthz": {
                "get": {
                    "summary": "Read server health and readiness",
                    "responses": {"200": {"description": "Server health"}},
                },
            },
            "/readyz": {
                "get": {
                    "summary": "Read server readiness",
                    "responses": {"200": {"description": "Server readiness"}},
                },
            },
            "/v1/projects/{project_id}/submissions": {
                "post": {
                    "summary": "Submit a canonical Engineering Platform request",
                    "security": [{"consumerBearer": []}],
                    "parameters": [
                        {
                            "name": "project_id", "in": "path", "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "EP-Submission-Transport", "in": "header", "required": False,
                            "schema": {"type": "string", "default": "HTTP"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["repository_id", "producer", "prompt"],
                                    "properties": submission_properties,
                                    "additionalProperties": False,
                                },
                            },
                        },
                    },
                    "responses": {
                        "200": {"description": "Submission accepted or idempotently repeated"},
                        "400": {"description": "Malformed or invalid submission"},
                        "401": {"description": "Missing or invalid consumer credential"},
                        "404": {"description": "Unknown project"},
                        "409": {"description": "Project unavailable or idempotency conflict"},
                        "413": {"description": "Payload exceeds 128 KiB"},
                        "415": {"description": "Content type is not application/json"},
                    },
                },
            },
            "/v1/projects/{project_id}/submissions/{submission_id}": {
                "get": {
                    "summary": "Read canonical submission, run, result and evidence references",
                    "security": [{"consumerBearer": []}],
                    "parameters": [
                        {"name": "project_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "submission_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "Canonical project-scoped producer readback v1.2"},
                        "401": {"description": "Missing or invalid consumer credential"},
                        "404": {"description": "Submission absent from the authenticated project"},
                    },
                },
            },
        },
        "components": {
            "securitySchemes": {
                "consumerBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"},
            },
        },
    }


def _attachment_content_disposition(filename: object) -> str:
    """Build a fail-closed attachment header from a bounded ASCII filename.

    Route validation is not a response-header security boundary.  This helper
    rejects control characters and all non-allowlisted filenames before a
    value reaches ``BaseHTTPRequestHandler.send_header``.
    """
    if not isinstance(filename, str):
        raise ValueError("attachment filename is invalid")
    sanitized = filename.replace("\r", "").replace("\n", "")
    if sanitized != filename or not _SAFE_ATTACHMENT_FILENAME.fullmatch(sanitized):
        raise ValueError("attachment filename is invalid")
    return f'attachment; filename="{sanitized}"'


def _telemetry_export_content_type(export_format: object) -> str:
    """Return a fixed MIME type for one supported telemetry export format."""
    if export_format == "markdown":
        return "text/markdown; charset=utf-8"
    if export_format == "json":
        return "application/json; charset=utf-8"
    raise ValueError("telemetry export format is invalid")


def _report_content_disposition(report_id: object) -> str:
    """Compose the report filename only after independently validating its id."""
    if not isinstance(report_id, str) or not _SAFE_REPORT_ID.fullmatch(report_id):
        raise ValueError("report identifier is invalid")
    return _attachment_content_disposition(f"engineering-report-{report_id}.md")


def _execution_runtime_status() -> dict[str, str]:
    """Project installed Server Python readiness without Dashboard ownership."""
    # ``sys.executable`` is the installed venv launcher.  Do not resolve its
    # symlink to the base interpreter: the Console must report the runtime
    # that actually owns EP and its validation environment.
    executable = Path(sys.executable).expanduser().absolute()
    ready = executable.is_file() and os.access(executable, os.X_OK)
    return {
        "state": "READY" if ready else "UNAVAILABLE",
        "executable": str(executable) if ready else "",
        "version": sys.version.split()[0] if ready else "",
    }


def _remaining_rate_limit_capacity(rate_limits: dict[str, object]) -> float | None:
    """Return the most restrictive remaining safe quota percentage."""
    windows = rate_limits.get("windows")
    if not isinstance(windows, list):
        return None
    remaining: list[float] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            remaining.append(max(0.0, min(100.0, 100.0 - float(used))))
    return min(remaining) if remaining else None


def _github_rate_limit_status() -> dict[str, object]:
    """Read GitHub quota state without changing GitHub or repository state."""
    try:
        payload = json.loads(GitHubProvider().github("api", "rate_limit"))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        return {"limited": "rate limit" in str(error).lower()}
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return {"limited": False}
    exhausted: list[tuple[str, int]] = []
    for name in ("core", "graphql", "search"):
        resource = resources.get(name)
        if not isinstance(resource, dict):
            continue
        remaining, reset = resource.get("remaining"), resource.get("reset")
        if isinstance(remaining, int) and remaining <= 0:
            exhausted.append((name, reset if isinstance(reset, int) else 0))
    if not exhausted:
        return {"limited": False}
    reset_at = min((reset for _, reset in exhausted if reset > 0), default=None)
    return {"limited": True, "reset_at": reset_at}


def _codex_rate_limits() -> bytes:
    """Read quota through the Server-owned app-server protocol/cache."""
    global _CODEX_RATE_LIMIT_CACHE
    now = time.monotonic()
    with _CODEX_RATE_LIMIT_CACHE_LOCK:
        if _CODEX_RATE_LIMIT_CACHE and now - _CODEX_RATE_LIMIT_CACHE[0] < 60:
            return _CODEX_RATE_LIMIT_CACHE[1]
    identity = _codex_provider_identity()
    provider = CodexCliProvider(); process = None
    try:
        process = provider.app_server()
        if process.stdin is None or process.stdout is None:
            return json.dumps(identity, separators=(",", ":")).encode()
        process.stdin.write(json.dumps({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "engineering-platform-server", "title": "EP Operations", "version": _console_platform_version()}}}) + "\n")
        process.stdin.flush(); deadline = time.monotonic() + 5; requested = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select((process.stdout,), (), (), max(0, deadline - time.monotonic()))
            if not ready: break
            line = process.stdout.readline()
            if not line: break
            response = json.loads(line)
            if response.get("id") == 1 and not requested:
                process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                process.stdin.write(json.dumps({"method": "account/rateLimits/read", "id": 2, "params": {}}) + "\n")
                process.stdin.flush(); requested = True
            elif response.get("id") == 2:
                encoded = json.dumps({**identity, **_normalize_rate_limits(response.get("result"))}, separators=(",", ":")).encode()
                with _CODEX_RATE_LIMIT_CACHE_LOCK: _CODEX_RATE_LIMIT_CACHE = (time.monotonic(), encoded)
                return encoded
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    finally:
        if process is not None: provider.close_app_server(process)
    return json.dumps(identity, separators=(",", ":")).encode()


def _normalize_rate_limits(payload: object) -> dict[str, object]:
    """Keep only bounded display values from the read-only quota response."""
    limits = payload.get("rateLimits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict): return {}
    windows: list[dict[str, int | str]] = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict): continue
        used, duration, resets = item.get("usedPercent"), item.get("windowDurationMins"), item.get("resetsAt")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not isinstance(duration, int) or isinstance(duration, bool) or not isinstance(resets, int) or isinstance(resets, bool): continue
        windows.append({"label": _rate_limit_window_label(duration), "used_percent": max(0, min(100, round(used))), "window_minutes": duration, "resets_at": resets})
    credits = payload.get("rateLimitResetCredits"); available = credits.get("availableCount") if isinstance(credits, dict) else None
    result: dict[str, object] = {"windows": windows}
    if isinstance(available, int) and not isinstance(available, bool) and available >= 0: result["reset_credits"] = available
    return result if windows or "reset_credits" in result else {}


def _rate_limit_window_label(duration: int) -> str:
    if duration == 300: return "5-uursvenster"
    if duration == 10_080: return "Weekvenster"
    if duration % 1_440 == 0: return f"{duration // 1_440}-daags venster"
    if duration % 60 == 0: return f"{duration // 60}-uursvenster"
    return f"{duration}-minutenvenster"


def _codex_provider_identity() -> dict[str, str]:
    """Return the managed Codex CLI identity without selecting PATH authority."""
    global _CODEX_IDENTITY_CACHE
    now = time.monotonic()
    with _CODEX_IDENTITY_CACHE_LOCK:
        if _CODEX_IDENTITY_CACHE and now - _CODEX_IDENTITY_CACHE[0] < 300:
            return dict(_CODEX_IDENTITY_CACHE[1])
    identity = {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"}
    executable = CodexCliProvider()._executable
    if executable:
        candidate = Path(executable).expanduser()
        if candidate.is_absolute(): identity["provider_path"] = str(candidate.parent.parent)
        try: completed = LocalProcessProvider().execute(default_data_root(), (executable, "--version"))
        except OSError: completed = None
        if completed and completed.returncode == 0:
            match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", (completed.stdout or completed.stderr).strip())
            if match: identity["provider_version"] = match.group(1)
    with _CODEX_IDENTITY_CACHE_LOCK: _CODEX_IDENTITY_CACHE = (now, identity)
    return dict(identity)


def _start_provider_login(data_root: Path, provider: str) -> None:
    """Dispatch one explicit host-wide provider login from the Server."""
    commands = {
        "CODEX": (CodexCliProvider()._executable, "login", "--device-auth"),
        "GITHUB": ("gh", "auth", "login", "--hostname", "github.com", "--web"),
    }
    command = commands.get(provider)
    if command is None:
        raise ValueError("Unsupported provider login request.")
    if provider == "CODEX" and not CodexCliProvider().status().qualified:
        raise ValueError("Codex CLI is not installed.")
    if provider == "GITHUB" and shutil.which("gh") is None:
        raise ValueError("GitHub CLI is not installed.")
    if sys.platform != "darwin":
        raise ValueError("Interactive provider login is supported from the local macOS Server only.")
    script = "\n".join((
        'tell application "Terminal"', "activate",
        f"do script {json.dumps('exec ' + ' '.join(shlex.quote(part) for part in command))}",
        "end tell",
    ))
    with _PROVIDER_LOGIN_LOCK:
        completed = LocalProcessProvider().execute(data_root, ("/usr/bin/osascript", "-e", script))
    if completed.returncode:
        raise ValueError("Provider login window could not be opened.")


def _logout_provider(data_root: Path, provider: str) -> None:
    """Remove one locally stored provider session without exposing credentials."""
    if provider == "CODEX":
        completed = CodexCliProvider().command("logout")
    elif provider == "GITHUB":
        process = LocalProcessProvider()
        account = process.execute(data_root, ("gh", "api", "user", "--jq", ".login"))
        username = account.stdout.strip()
        if account.returncode or not username or not re.fullmatch(r"[A-Za-z0-9-]+", username):
            raise ValueError("GitHub session cannot be safely identified for logout.")
        completed = process.execute(data_root, ("gh", "auth", "logout", "--hostname", "github.com", "--user", username))
    else:
        raise ValueError("Unsupported provider logout request.")
    if completed.returncode:
        raise ValueError("Provider logout did not complete.")


def _central_execution_active(data_root: Path) -> bool:
    """Read active lifecycle state from CENTRAL, never a checkout status file."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            "SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE "
            "state IN ('CLAIMED','RUNNING') OR (state IN ('BLOCKED','FAILED') "
            "AND operator_resolution='OPEN') LIMIT 1"
        ).fetchone()
    return row is not None


def _choose_local_directory(data_root: Path) -> str | None:
    """Use the host-native folder chooser only after an explicit Console action."""
    if sys.platform != "darwin":
        raise ValueError("LOCAL_DIRECTORY_PICKER_UNAVAILABLE")
    result = LocalProcessProvider().execute(data_root, ("osascript", "-e", "POSIX path of (choose folder)"))
    if result.returncode:
        if "-128" in (result.stderr or ""):
            return None
        raise ValueError("LOCAL_DIRECTORY_PICKER_FAILED")
    location = result.stdout.strip()
    if not location or not Path(location).is_dir():
        raise ValueError("LOCAL_DIRECTORY_PICKER_FAILED")
    return location


def _install_provider(data_root: Path, provider: str) -> None:
    """Install one provider only through the Server installation boundary."""
    if not _PROVIDER_INSTALL_LOCK.acquire(blocking=False):
        raise ValueError("Another provider installation is already in progress.")
    try:
        if _central_execution_active(data_root):
            raise ValueError("Provider installation is unavailable while an execution is active.")
        if provider == "CODEX":
            try:
                managed_codex_runtime.provision(data_root)
            except managed_codex_runtime.ManagedCodexRuntimeError as error:
                raise ValueError(str(error)) from error
            key = "codex"
        elif provider == "GITHUB":
            brew = shutil.which("brew")
            if brew is None:
                raise ValueError("GitHub CLI installation requires Homebrew on this host.")
            completed = LocalProcessProvider().execute(data_root, (brew, "install", "gh"))
            verification = LocalProcessProvider().execute(data_root, ("gh", "--version"))
            if completed.returncode or verification.returncode:
                raise ValueError("Provider installation could not be verified.")
            key = "github"
        else:
            raise ValueError("Unsupported provider installation request.")
        if _central_provider_readiness(data_root).get(key, {}).get("state") == "UNAVAILABLE":
            raise ValueError("Provider installation could not be verified.")
    finally:
        _PROVIDER_INSTALL_LOCK.release()


class ServerConfigurationError(ValueError):
    """Raised when an installation-owned server configuration is invalid."""


SERVER_REQUIRED_TABLES = frozenset(
    {
        "engineering_schema_migrations",
        "engineering_metadata",
        "ep_installations",
        "ep_control_provenance",
        "ep_consumer_credentials",
        "ep_consumer_registrations",
        "ep_consumer_credential_recovery_operations",
        "ep_project_registrations",
        "ep_execution_runs",
        "ep_execution_leases",
        "prompt_execution_history",
        "ep_agent_registrations",
        "ep_agent_pairing_codes",
        "ep_repository_registrations",
        "ep_agent_repository_attachments",
        "ep_local_repository_bindings",
        "ep_submissions",
        "ep_submission_events",
        "ep_submission_prompt_history",
        "ep_queue_disposition_operations",
        "ep_operator_capabilities",
        "ep_merge_delegations",
        "ep_parity_lifecycle_dispatches",
        "ep_receipt_run_provenance",
        "ep_external_producer_bindings",
        "ep_external_producer_binding_audit",
        "engineering_transactions",
        "execution_lifecycle_events",
        "execution_submission_attempts",
        "execution_submission_attempt_links",
        "execution_validation_profile_identities",
        "ep_forge_exchange_audit",
        "ep_forge_action_context_envelopes",
        "ep_forge_planning_context_envelopes",
        "ep_execution_host_evidence",
        "ep_technical_diagnostics",
        "ep_terminal_evidence_reconciliation_operations",
        "ep_operational_reset_operations",
        "ep_operational_dataset_state",
        "ep_operational_identity_tombstones",
    }
)
SERVER_REQUIRED_INDEXES = frozenset(
    {
        "ep_consumer_credentials_scope_lookup",
        "ep_consumer_registrations_status_lookup",
        "ep_consumer_credential_recovery_active_scope",
        "ep_consumer_credential_recovery_scope_lookup",
        "ep_project_registrations_status_lookup",
        "ep_execution_runs_project_lookup",
        "ep_control_provenance_subject_lookup",
        "ep_repository_registrations_project_lookup",
        "ep_agent_repository_attachments_repository_lookup",
        "ep_local_repository_bindings_repository_lookup",
        "ep_submissions_project_lookup",
        "ep_submissions_idempotency_lookup",
        "ep_parity_lifecycle_dispatches_run_lookup",
        "ep_receipt_run_provenance_project_lookup",
        "ep_external_producer_bindings_active_key",
        "ep_forge_exchange_audit_project_lookup",
        "ep_forge_action_context_envelopes_project_lookup",
        "ep_forge_planning_context_envelopes_project_lookup",
        "ep_technical_diagnostics_created_lookup",
        "execution_artifact_records_active_terminal_run",
    }
)
SERVER_REQUIRED_VIEWS = frozenset({"execution_submission_run_links"})


@dataclass(frozen=True)
class ServerConfiguration:
    version: int
    bind_host: str
    bind_port: int
    managed_codex_cli_prefix: str
    product_version: str

    @classmethod
    def load(cls, data_root: Path) -> "ServerConfiguration":
        path = data_root / SERVER_CONFIGURATION_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server configuration is unavailable.") from error
        if not isinstance(raw, dict):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        legacy_keys = {"version", "bind_host", "bind_port"}
        version_two_keys = legacy_keys | {"managed_codex_cli_prefix"}
        current_keys = version_two_keys | {"product_version"}
        if set(raw) == legacy_keys and raw.get("version") == 1:
            prefix = str(default_engineering_platform_codex_cli_prefix())
            product_version = ""
        elif set(raw) == version_two_keys and raw.get("version") == 2:
            prefix = raw.get("managed_codex_cli_prefix")
            product_version = ""
        elif set(raw) == current_keys and raw.get("version") == SERVER_CONFIGURATION_VERSION:
            prefix = raw.get("managed_codex_cli_prefix")
            product_version = raw.get("product_version")
        else:
            raise ServerConfigurationError("EP Server configuration is invalid.")
        candidate = Path(prefix).expanduser() if isinstance(prefix, str) else None
        if (
            not isinstance(raw["bind_host"], str)
            or raw["bind_host"] != "127.0.0.1"
            or not isinstance(raw["bind_port"], int)
            or not 1 <= raw["bind_port"] <= 65535
            or candidate is None
            or not candidate.is_absolute()
            or not isinstance(product_version, str)
        ):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        return cls(int(raw["version"]), raw["bind_host"], raw["bind_port"],
                   str(candidate.resolve(strict=False)), product_version)


@dataclass(frozen=True)
class RuntimeIdentity:
    instance_id: str
    created_at: str


@dataclass(frozen=True)
class AgentRegistrationRequest:
    """Transport-neutral future Agent registration input.

    B3 deliberately does not define authentication, enrollment persistence,
    project attachment, or any network representation for this request.
    """

    agent_id: str
    agent_kind: str
    capabilities: tuple[str, ...]


class AgentRegistrationIntake(Protocol):
    """Future internal extension point; no transport/auth contract is implied."""

    def accept(self, request: AgentRegistrationRequest) -> None: ...


def platform_default_data_root() -> Path:
    """Return the canonical operational root without honoring process overrides."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Engineering Platform Server"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Engineering Platform Server"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "engineering-platform-server"


def default_data_root() -> Path:
    override = os.environ.get(SERVER_ENVIRONMENT_DATA_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    return platform_default_data_root()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _view_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}


def _schema_version(connection: sqlite3.Connection) -> int:
    if "engineering_schema_migrations" not in _table_names(connection):
        return 0
    row = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _install_schema_41(connection: sqlite3.Connection, identity: RuntimeIdentity) -> None:
    """Install the clean standalone schema and immutable control provenance."""
    for statement in (
        "CREATE TABLE IF NOT EXISTS engineering_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS engineering_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version=41))",
        "CREATE TABLE IF NOT EXISTS ep_control_provenance (event_id INTEGER PRIMARY KEY, event_kind TEXT NOT NULL CHECK(event_kind IN ('INSTALLATION_CREATED','CREDENTIAL_LIFECYCLE','CONSUMER_REGISTRATION','PROJECT_SCOPE_MUTATION')), subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_control_provenance_subject_lookup ON ep_control_provenance(subject_kind,subject_id,event_id DESC)",
        "CREATE TABLE IF NOT EXISTS ep_consumer_credentials (credential_id TEXT PRIMARY KEY CHECK(length(credential_id) BETWEEN 1 AND 128), consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32), fingerprint BLOB NOT NULL UNIQUE CHECK(length(fingerprint)=32), issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, replaced_by_credential_id TEXT REFERENCES ep_consumer_credentials(credential_id))",
        "CREATE INDEX IF NOT EXISTS ep_consumer_credentials_scope_lookup ON ep_consumer_credentials(consumer_id,project_id,revoked_at)",
        "CREATE TABLE IF NOT EXISTS ep_consumer_registrations (consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, disabled_at TEXT, revoked_at TEXT, audit_metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(consumer_id,project_id))",
        "CREATE INDEX IF NOT EXISTS ep_consumer_registrations_status_lookup ON ep_consumer_registrations(consumer_id,project_id,status)",
        "CREATE TABLE IF NOT EXISTS ep_project_registrations (project_id TEXT PRIMARY KEY CHECK(length(project_id) BETWEEN 1 AND 128), attachment_contract TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_project_registrations_status_lookup ON ep_project_registrations(status,project_id)",
        "CREATE TABLE IF NOT EXISTS ep_execution_runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id), state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ep_execution_runs_project_lookup ON ep_execution_runs(project_id,state,created_at DESC)",
        "CREATE TABLE IF NOT EXISTS ep_execution_leases (lease_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_execution_runs(run_id), holder_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT)",
        "CREATE TABLE IF NOT EXISTS prompt_execution_history (run_id TEXT PRIMARY KEY REFERENCES ep_execution_runs(run_id), prompt_digest TEXT NOT NULL, recorded_at TEXT NOT NULL)",
    ):
        connection.execute(statement)
    agent_trust.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(41)")
    connection.execute("INSERT OR IGNORE INTO engineering_metadata(key,value) VALUES('installation.instance_id',?)", (identity.instance_id,))
    connection.execute("INSERT OR IGNORE INTO engineering_metadata(key,value) VALUES('installation.schema_version','41')")
    connection.execute("INSERT OR IGNORE INTO ep_installations(instance_id,created_at,schema_version) VALUES(?,?,41)", (identity.instance_id, identity.created_at))
    connection.execute("INSERT OR IGNORE INTO ep_control_provenance(event_kind,subject_kind,subject_id,payload,recorded_at) VALUES('INSTALLATION_CREATED','installation',?,?,?)", (identity.instance_id, json.dumps({'schema_version': 41}, sort_keys=True), identity.created_at))
    for table in ("ep_control_provenance",):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.casefold()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, '{table} evidence is immutable.'); END")


def _install_current_schema(connection: sqlite3.Connection, identity: RuntimeIdentity) -> None:
    """Install the current clean-store shape in one Server schema revision.

    A fresh, installation-owned database has no historical rows to preserve.
    It must therefore never replay the old 41--59 upgrade chain, including
    its temporary table rebuilds and intermediate migration markers.  Those
    forward migrations remain the sole compatibility path for an existing
    installation.  This bootstrap records only the current Server schema
    revision after all current tables, indexes, views and triggers exist.
    """
    for statement in (
        "CREATE TABLE engineering_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE engineering_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        f"CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version={SERVER_STORE_SCHEMA_VERSION}))",
        "CREATE TABLE ep_control_provenance (event_id INTEGER PRIMARY KEY, event_kind TEXT NOT NULL CHECK(event_kind IN ('INSTALLATION_CREATED','CREDENTIAL_LIFECYCLE','CONSUMER_REGISTRATION','PROJECT_SCOPE_MUTATION')), subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)",
        "CREATE INDEX ep_control_provenance_subject_lookup ON ep_control_provenance(subject_kind,subject_id,event_id DESC)",
        "CREATE TABLE ep_consumer_credentials (credential_id TEXT PRIMARY KEY CHECK(length(credential_id) BETWEEN 1 AND 128), consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32), fingerprint BLOB NOT NULL UNIQUE CHECK(length(fingerprint)=32), issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, replaced_by_credential_id TEXT REFERENCES ep_consumer_credentials(credential_id))",
        "CREATE INDEX ep_consumer_credentials_scope_lookup ON ep_consumer_credentials(consumer_id,project_id,revoked_at)",
        "CREATE TABLE ep_consumer_registrations (consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128), project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128), status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, disabled_at TEXT, revoked_at TEXT, audit_metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(consumer_id,project_id))",
        "CREATE INDEX ep_consumer_registrations_status_lookup ON ep_consumer_registrations(consumer_id,project_id,status)",
        "CREATE TABLE ep_project_registrations (project_id TEXT PRIMARY KEY CHECK(length(project_id) BETWEEN 1 AND 128), attachment_contract TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX ep_project_registrations_status_lookup ON ep_project_registrations(status,project_id)",
        "CREATE TABLE ep_execution_runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id), state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, execution_mode TEXT CHECK(execution_mode IN ('MANAGED','GENESIS')))",
        "CREATE INDEX ep_execution_runs_project_lookup ON ep_execution_runs(project_id,state,created_at DESC)",
        "CREATE TABLE ep_execution_leases (lease_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_execution_runs(run_id), holder_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT)",
    ):
        connection.execute(statement)
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER ep_control_provenance_immutable_{operation.casefold()} "
            f"BEFORE {operation} ON ep_control_provenance BEGIN "
            f"SELECT RAISE(ABORT, 'ep_control_provenance evidence is immutable.'); END"
        )
    agent_trust.install_schema(connection)
    project_topology.install_schema(connection)
    local_repository_binding.install_schema(connection)

    # The retained execution evidence schema remains a shared implementation
    # surface, but this installer creates its current shape without recording
    # a second, checkout-local migration history.
    storage.install_central_operational_compatibility_schema(connection)
    _install_current_submission_schema(connection)
    _install_forge_action_context_schema(connection)
    _install_forge_planning_context_schema(connection)
    _install_execution_host_evidence_schema(connection)
    _install_technical_diagnostics_schema(connection)
    owner_credential_recovery.install_schema(connection)
    submission_service.install_terminal_evidence_reconciliation_schema(connection)
    merge_delegation.install_schema(connection)
    central_operational_reset.install_schema(connection)

    connection.execute(
        "INSERT INTO engineering_schema_migrations(version) VALUES(?)",
        (SERVER_STORE_SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT INTO engineering_metadata(key,value) VALUES('installation.instance_id',?)",
        (identity.instance_id,),
    )
    connection.execute(
        "INSERT INTO engineering_metadata(key,value) VALUES('installation.schema_version',?)",
        (str(SERVER_STORE_SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) VALUES(?,?,?)",
        (identity.instance_id, identity.created_at, SERVER_STORE_SCHEMA_VERSION),
    )
    connection.execute(
        "INSERT INTO ep_control_provenance(event_kind,subject_kind,subject_id,payload,recorded_at) VALUES('INSTALLATION_CREATED','installation',?,?,?)",
        (
            identity.instance_id,
            json.dumps({"schema_version": SERVER_STORE_SCHEMA_VERSION}, sort_keys=True),
            identity.created_at,
        ),
    )


def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute complete DDL statements without committing the caller's transaction."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ServerConfigurationError("EP Server schema definition is incomplete.")


def _install_forge_action_context_schema(connection: sqlite3.Connection) -> None:
    """Install the immutable, prospective Forge Action-context authority."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ep_forge_action_context_envelopes (
            submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            action_id TEXT NOT NULL,
            envelope_version TEXT NOT NULL,
            generator_id TEXT NOT NULL,
            generator_model TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            summary_digest TEXT NOT NULL,
            envelope_digest TEXT NOT NULL,
            document TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ep_forge_action_context_envelopes_project_lookup
            ON ep_forge_action_context_envelopes(project_id,recorded_at DESC,submission_id);
        CREATE TRIGGER IF NOT EXISTS ep_forge_action_context_envelopes_scope_insert
        BEFORE INSERT ON ep_forge_action_context_envelopes
        WHEN NOT EXISTS (
            SELECT 1 FROM ep_submissions AS submission
             WHERE submission.submission_id=NEW.submission_id
               AND submission.project_id=NEW.project_id
               AND submission.engineering_action_id=NEW.action_id
        )
        BEGIN SELECT RAISE(ABORT, 'FORGE_ACTION_CONTEXT_SUBMISSION_SCOPE_MISMATCH'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_action_context_envelopes_immutable_update
        BEFORE UPDATE ON ep_forge_action_context_envelopes
        BEGIN SELECT RAISE(ABORT, 'Forge Action context envelope is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_action_context_envelopes_immutable_delete
        BEFORE DELETE ON ep_forge_action_context_envelopes
        BEGIN SELECT RAISE(ABORT, 'Forge Action context envelope is immutable'); END;
    """)


def _install_forge_planning_context_schema(connection: sqlite3.Connection) -> None:
    """Install immutable, prospective Forge planning-context evidence."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ep_forge_planning_context_envelopes (
            submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            mission_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            envelope_version TEXT NOT NULL,
            envelope_digest TEXT NOT NULL,
            decision_evidence_reference_digest TEXT,
            document TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ep_forge_planning_context_envelopes_project_lookup
            ON ep_forge_planning_context_envelopes(project_id,recorded_at DESC,submission_id);
        CREATE TRIGGER IF NOT EXISTS ep_forge_planning_context_envelopes_scope_insert
        BEFORE INSERT ON ep_forge_planning_context_envelopes
        WHEN NOT EXISTS (
            SELECT 1 FROM ep_submissions AS submission
             WHERE submission.submission_id=NEW.submission_id
               AND submission.project_id=NEW.project_id
               AND submission.mission_id=NEW.mission_id
               AND submission.engineering_action_id=NEW.action_id
        )
        BEGIN SELECT RAISE(ABORT, 'FORGE_PLANNING_CONTEXT_SUBMISSION_SCOPE_MISMATCH'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_planning_context_envelopes_immutable_update
        BEFORE UPDATE ON ep_forge_planning_context_envelopes
        BEGIN SELECT RAISE(ABORT, 'Forge planning context envelope is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_planning_context_envelopes_immutable_delete
        BEFORE DELETE ON ep_forge_planning_context_envelopes
        BEGIN SELECT RAISE(ABORT, 'Forge planning context envelope is immutable'); END;
    """)


def _install_execution_host_evidence_schema(connection: sqlite3.Connection) -> None:
    """Install immutable start and once-finalized host execution evidence."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ep_execution_host_evidence (
            run_id TEXT PRIMARY KEY REFERENCES ep_execution_runs(run_id),
            start_document TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            terminal_document TEXT,
            terminal_captured_at TEXT
        );
        CREATE TRIGGER IF NOT EXISTS ep_execution_host_evidence_start_immutable
        BEFORE UPDATE ON ep_execution_host_evidence
        WHEN OLD.start_document != NEW.start_document
          OR OLD.captured_at != NEW.captured_at
          OR OLD.terminal_document IS NOT NULL
          OR NEW.terminal_document IS NULL
          OR NEW.terminal_captured_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'Execution Host evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ep_execution_host_evidence_immutable_delete
        BEFORE DELETE ON ep_execution_host_evidence
        BEGIN SELECT RAISE(ABORT, 'Execution Host evidence is immutable'); END;
    """)


def _install_technical_diagnostics_schema(connection: sqlite3.Connection) -> None:
    """Install the private, append-only Server technical-diagnostic store.

    Component logs expose only a diagnostic code and opaque reference. This
    table is intentionally not joined by Console projections: it retains the
    bounded exception detail for on-host, authorized diagnosis.
    """
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ep_technical_diagnostics (
            correlation_id TEXT PRIMARY KEY,
            component TEXT NOT NULL,
            run_id TEXT,
            event TEXT NOT NULL,
            level TEXT NOT NULL CHECK(level IN ('WARNING','ERROR')),
            diagnostic_code TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ep_technical_diagnostics_created_lookup
            ON ep_technical_diagnostics(created_at DESC,correlation_id);
        CREATE TRIGGER IF NOT EXISTS ep_technical_diagnostics_immutable_update
        BEFORE UPDATE ON ep_technical_diagnostics
        BEGIN SELECT RAISE(ABORT, 'Technical diagnostics are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ep_technical_diagnostics_immutable_delete
        BEFORE DELETE ON ep_technical_diagnostics
        BEGIN SELECT RAISE(ABORT, 'Technical diagnostics are immutable'); END;
    """)


def _install_current_submission_schema(connection: sqlite3.Connection) -> None:
    """Install current CENTRAL submission and exchange evidence for a new store."""
    _execute_schema_script(connection, """
        CREATE TABLE ep_submissions (
            submission_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
            producer_id TEXT NOT NULL,
            producer_type TEXT NOT NULL,
            producer_version TEXT,
            transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','DEPENDABOT','LEGACY_FILE')),
            prompt TEXT NOT NULL,
            prompt_digest TEXT NOT NULL,
            constraints TEXT NOT NULL,
            idempotency_key TEXT,
            correlation_id TEXT,
            mission_id TEXT,
            engineering_action_id TEXT,
            transport_receipt_id TEXT,
            transport_received_at TEXT,
            state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED','DEFERRED','QUARANTINED','DECLINED')),
            admission TEXT NOT NULL,
            created_at TEXT NOT NULL,
            disposition_revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC);
        CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE TABLE ep_submission_events (
            event_id INTEGER PRIMARY KEY,
            submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id),
            event_kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ep_submission_prompt_history (
            submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
            prompt_digest TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ep_parity_lifecycle_dispatches (
            submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
            run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
            state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
            prompt_path TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED')),
            resolution_submission_id TEXT REFERENCES ep_submissions(submission_id)
        );
        CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state);
        CREATE TABLE ep_queue_disposition_operations (
            operation_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id),
            actor_reference TEXT NOT NULL,
            command_digest TEXT NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            previous_revision INTEGER NOT NULL,
            resulting_revision INTEGER NOT NULL,
            event_id INTEGER NOT NULL REFERENCES ep_submission_events(event_id),
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ep_operator_capabilities (
            consumer_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            capability TEXT NOT NULL CHECK(capability IN ('QUEUE_HOLD_RESUME','QUEUE_DECLINE')),
            granted_at TEXT NOT NULL,
            revoked_at TEXT,
            PRIMARY KEY(consumer_id,project_id,capability)
        );
        CREATE TABLE ep_external_producer_bindings (
            binding_id TEXT PRIMARY KEY,
            producer_type TEXT NOT NULL,
            external_resource_type TEXT NOT NULL,
            external_resource_identity TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            provenance TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ep_external_producer_bindings_active_key
            ON ep_external_producer_bindings(producer_type,external_resource_type,external_resource_identity)
            WHERE status='ACTIVE';
        CREATE TABLE ep_external_producer_binding_audit (
            audit_id INTEGER PRIMARY KEY,
            binding_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ep_receipt_run_provenance (
            submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
            run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
            installation_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX ep_receipt_run_provenance_project_lookup
            ON ep_receipt_run_provenance(project_id,created_at DESC);
        CREATE TRIGGER ep_receipt_run_provenance_scope_insert
            BEFORE INSERT ON ep_receipt_run_provenance BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM ep_submissions s
                WHERE s.submission_id=NEW.submission_id
                  AND s.project_id=NEW.project_id
                  AND s.repository_id=NEW.repository_id
            ) THEN RAISE(ABORT,'PROVENANCE_SUBMISSION_SCOPE_MISMATCH') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM ep_execution_runs r
                WHERE r.run_id=NEW.run_id AND r.project_id=NEW.project_id
            ) THEN RAISE(ABORT,'PROVENANCE_RUN_PROJECT_MISMATCH') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM ep_parity_lifecycle_dispatches d
                WHERE d.run_id=NEW.run_id
                  AND d.submission_id=NEW.submission_id
                  AND d.project_id=NEW.project_id
                  AND d.repository_id=NEW.repository_id
            ) THEN RAISE(ABORT,'PROVENANCE_DISPATCH_SCOPE_MISMATCH') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM engineering_metadata
                WHERE key='installation.instance_id' AND value=NEW.installation_id
            ) THEN RAISE(ABORT,'PROVENANCE_INSTALLATION_MISMATCH') END;
        END;
        CREATE TRIGGER ep_receipt_run_provenance_immutable_update
            BEFORE UPDATE ON ep_receipt_run_provenance BEGIN
            SELECT RAISE(ABORT,'PROVENANCE_IMMUTABLE');
        END;
        CREATE TRIGGER ep_receipt_run_provenance_immutable_delete
            BEFORE DELETE ON ep_receipt_run_provenance BEGIN
            SELECT RAISE(ABORT,'PROVENANCE_IMMUTABLE');
        END;
        CREATE TABLE ep_forge_exchange_audit (
            audit_id TEXT PRIMARY KEY,
            submission_id TEXT NOT NULL UNIQUE REFERENCES ep_submissions(submission_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            direction TEXT NOT NULL CHECK(direction='FORGE_TO_EP'),
            event_kind TEXT NOT NULL CHECK(event_kind='FORGE_SUBMISSION_ACCEPTED'),
            receipt_id TEXT NOT NULL UNIQUE,
            producer_contract_version TEXT NOT NULL,
            forge_provenance_contract_version TEXT NOT NULL,
            forge_application_version TEXT NOT NULL,
            ep_application_version TEXT NOT NULL,
            producer_readback_contract_version TEXT NOT NULL,
            accepted_request_digest TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX ep_forge_exchange_audit_project_lookup
            ON ep_forge_exchange_audit(project_id,recorded_at DESC,submission_id);
        CREATE TRIGGER ep_forge_exchange_audit_scope_insert
            BEFORE INSERT ON ep_forge_exchange_audit
            WHEN NOT EXISTS (
                SELECT 1 FROM ep_submissions AS submission
                WHERE submission.submission_id=NEW.submission_id
                  AND submission.project_id=NEW.project_id
            )
            BEGIN SELECT RAISE(ABORT, 'FORGE_EXCHANGE_SUBMISSION_SCOPE_MISMATCH'); END;
        CREATE TRIGGER ep_forge_exchange_audit_immutable_update
            BEFORE UPDATE ON ep_forge_exchange_audit
            BEGIN SELECT RAISE(ABORT, 'Forge exchange audit is immutable'); END;
        CREATE TRIGGER ep_forge_exchange_audit_immutable_delete
            BEFORE DELETE ON ep_forge_exchange_audit
            BEGIN SELECT RAISE(ABORT, 'Forge exchange audit is immutable'); END;
    """)


def _migrate_schema_42(connection: sqlite3.Connection) -> None:
    """Forward-only topology extension; schema-41 structures remain intact."""
    # Schema 41 deliberately constrained the bootstrap record to 41.  Preserve
    # its row while widening that bootstrap-only constraint for official
    # forward migrations; no operational rows are rewritten.
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema41")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,42 FROM ep_installations_schema41")
    connection.execute("DROP TABLE ep_installations_schema41")
    project_topology.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(42)")
    connection.execute("UPDATE engineering_metadata SET value='42' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=42")


def _migrate_schema_43(connection: sqlite3.Connection) -> None:
    """Add CENTRAL-owned canonical submission persistence.

    This is deliberately a forward migration from schema 42; historical
    schema-40 execution databases are neither inspected nor imported.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema42")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,43 FROM ep_installations_schema42")
    connection.execute("DROP TABLE ep_installations_schema42")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        producer_id TEXT NOT NULL, producer_type TEXT NOT NULL, producer_version TEXT,
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL,
        idempotency_key TEXT, correlation_id TEXT, mission_id TEXT, engineering_action_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED','DEFERRED','QUARANTINED','DECLINED')), admission TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE TABLE ep_submission_events (event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id), event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE ep_submission_prompt_history (submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(43)")
    connection.execute("UPDATE engineering_metadata SET value='43' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=43")


def _migrate_schema_44(connection: sqlite3.Connection) -> None:
    """Add the private, explicit Phase-P local checkout binding surface."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema43")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,44 FROM ep_installations_schema43")
    connection.execute("DROP TABLE ep_installations_schema43")
    local_repository_binding.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(44)")
    connection.execute("UPDATE engineering_metadata SET value='44' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=44")


def _migrate_schema_45(connection: sqlite3.Connection) -> None:
    """Add the single-writer CENTRAL-to-historical lifecycle association."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema44")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,45 FROM ep_installations_schema44")
    connection.execute("DROP TABLE ep_installations_schema44")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
        prompt_path TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(45)")
    connection.execute("UPDATE engineering_metadata SET value='45' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=45")


def _migrate_schema_46(connection: sqlite3.Connection) -> None:
    """Persist the admitted execution mode with the CENTRAL run.

    The mode is decided before a submission is claimed.  It is therefore run
    evidence, rather than a presentation value to be rediscovered from a
    mutable prompt or a repository-local telemetry row.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema45")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,46 FROM ep_installations_schema45")
    connection.execute("DROP TABLE ep_installations_schema45")
    # Existing CENTRAL runs predate this evidence field.  Keep them NULL so
    # the Console accurately reports that their mode was not recorded, rather
    # than silently inventing MANAGED during migration.
    connection.execute("ALTER TABLE ep_execution_runs ADD COLUMN execution_mode TEXT CHECK(execution_mode IN ('MANAGED','GENESIS'))")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(46)")
    connection.execute("UPDATE engineering_metadata SET value='46' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=46")


def _migrate_schema_47(connection: sqlite3.Connection) -> None:
    """Keep failed project runs FIFO-blocking until CENTRAL records a resolution."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema46")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,47 FROM ep_installations_schema46")
    connection.execute("DROP TABLE ep_installations_schema46")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches ADD COLUMN operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED'))")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches ADD COLUMN resolution_submission_id TEXT REFERENCES ep_submissions(submission_id)")
    connection.execute("UPDATE ep_parity_lifecycle_dispatches SET operator_resolution='OPEN' WHERE state IN ('BLOCKED','FAILED')")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(47)")
    connection.execute("UPDATE engineering_metadata SET value='47' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=47")


def _migrate_schema_48(connection: sqlite3.Connection) -> None:
    """Move retained lifecycle persistence into the one CENTRAL database.

    No repository is opened or scanned.  These are empty compatibility tables
    for new standalone runs while the preserved runner is being invoked via
    the explicit CENTRAL operational context.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema47")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48)))"
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) "
        "SELECT instance_id,created_at,48 FROM ep_installations_schema47"
    )
    connection.execute("DROP TABLE ep_installations_schema47")
    storage.install_central_operational_compatibility_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(48)")
    connection.execute("UPDATE engineering_metadata SET value='48' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=48")


def _migrate_schema_49(connection: sqlite3.Connection) -> None:
    """Record bounded ingress receipts in the canonical submission row."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema48")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49)))"
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) "
        "SELECT instance_id,created_at,49 FROM ep_installations_schema48"
    )
    connection.execute("DROP TABLE ep_installations_schema48")
    # SQLite cannot widen the schema-43 transport CHECK in place.  Rebuild the
    # parent table while foreign-key enforcement is temporarily disabled by
    # the caller; SQLite keeps dependent references pointed at its canonical
    # name.  No submission facts are rewritten or inferred.
    # Child tables must be rebuilt too: SQLite otherwise retains a foreign-key
    # reference to the renamed historical parent table.
    connection.execute("ALTER TABLE ep_submission_events RENAME TO ep_submission_events_schema48")
    connection.execute("ALTER TABLE ep_submission_prompt_history RENAME TO ep_submission_prompt_history_schema48")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches RENAME TO ep_parity_lifecycle_dispatches_schema48")
    connection.execute("ALTER TABLE ep_submissions RENAME TO ep_submissions_schema48")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        producer_id TEXT NOT NULL, producer_type TEXT NOT NULL, producer_version TEXT,
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL,
        idempotency_key TEXT, correlation_id TEXT, mission_id TEXT, engineering_action_id TEXT,
        transport_receipt_id TEXT, transport_received_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED')), admission TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submissions(
        submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,state,admission,created_at)
        SELECT submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,state,admission,created_at
        FROM ep_submissions_schema48""")
    connection.execute("""CREATE TABLE ep_submission_events (
        event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id),
        event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_events(event_id,submission_id,event_kind,payload,recorded_at)
        SELECT event_id,submission_id,event_kind,payload,recorded_at FROM ep_submission_events_schema48""")
    connection.execute("""CREATE TABLE ep_submission_prompt_history (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_prompt_history(submission_id,prompt_digest,recorded_at)
        SELECT submission_id,prompt_digest,recorded_at FROM ep_submission_prompt_history_schema48""")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
        prompt_path TEXT NOT NULL, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED')),
        resolution_submission_id TEXT REFERENCES ep_submissions(submission_id))""")
    connection.execute("""INSERT INTO ep_parity_lifecycle_dispatches(
        submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id)
        SELECT submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id
        FROM ep_parity_lifecycle_dispatches_schema48""")
    connection.execute("DROP TABLE ep_submission_events_schema48")
    connection.execute("DROP TABLE ep_submission_prompt_history_schema48")
    connection.execute("DROP TABLE ep_parity_lifecycle_dispatches_schema48")
    connection.execute("DROP TABLE ep_submissions_schema48")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(49)")
    connection.execute("UPDATE engineering_metadata SET value='49' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=49")


def _migrate_schema_50(connection: sqlite3.Connection) -> None:
    """Add CENTRAL-owned external producer bindings and immutable audit evidence."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema49")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,50 FROM ep_installations_schema49")
    connection.execute("DROP TABLE ep_installations_schema49")
    connection.execute("""CREATE TABLE ep_external_producer_bindings (
        binding_id TEXT PRIMARY KEY, producer_type TEXT NOT NULL, external_resource_type TEXT NOT NULL,
        external_resource_identity TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id), status TEXT NOT NULL,
        version INTEGER NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL,
        provenance TEXT NOT NULL)""")
    connection.execute("CREATE UNIQUE INDEX ep_external_producer_bindings_active_key ON ep_external_producer_bindings(producer_type,external_resource_type,external_resource_identity) WHERE status='ACTIVE'")
    connection.execute("""CREATE TABLE ep_external_producer_binding_audit (
        audit_id INTEGER PRIMARY KEY, binding_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
        reason TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(50)")
    connection.execute("UPDATE engineering_metadata SET value='50' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=50")


def _migrate_schema_51(connection: sqlite3.Connection) -> None:
    """Add the explicit Server-owned Dependabot transport value.

    The producer is a bounded internal adapter, not an HTTP caller and not a
    File Inbox delivery. SQLite requires the durable submission constraint to
    be rebuilt to record that distinction truthfully.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema50")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,51 FROM ep_installations_schema50")
    connection.execute("DROP TABLE ep_installations_schema50")
    connection.execute("ALTER TABLE ep_submission_events RENAME TO ep_submission_events_schema50")
    connection.execute("ALTER TABLE ep_submission_prompt_history RENAME TO ep_submission_prompt_history_schema50")
    connection.execute("ALTER TABLE ep_parity_lifecycle_dispatches RENAME TO ep_parity_lifecycle_dispatches_schema50")
    connection.execute("ALTER TABLE ep_submissions RENAME TO ep_submissions_schema50")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        producer_id TEXT NOT NULL, producer_type TEXT NOT NULL, producer_version TEXT,
        transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','DEPENDABOT','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL,
        idempotency_key TEXT, correlation_id TEXT, mission_id TEXT, engineering_action_id TEXT,
        transport_receipt_id TEXT, transport_received_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED','DEFERRED','QUARANTINED','DECLINED')), admission TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submissions(
        submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,transport_receipt_id,transport_received_at,state,admission,created_at)
        SELECT submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,transport_receipt_id,transport_received_at,state,admission,created_at
        FROM ep_submissions_schema50""")
    connection.execute("""CREATE TABLE ep_submission_events (
        event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id),
        event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_events(event_id,submission_id,event_kind,payload,recorded_at)
        SELECT event_id,submission_id,event_kind,payload,recorded_at FROM ep_submission_events_schema50""")
    connection.execute("""CREATE TABLE ep_submission_prompt_history (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""")
    connection.execute("""INSERT INTO ep_submission_prompt_history(submission_id,prompt_digest,recorded_at)
        SELECT submission_id,prompt_digest,recorded_at FROM ep_submission_prompt_history_schema50""")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')),
        prompt_path TEXT NOT NULL, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED')),
        resolution_submission_id TEXT REFERENCES ep_submissions(submission_id))""")
    connection.execute("""INSERT INTO ep_parity_lifecycle_dispatches(
        submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id)
        SELECT submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at,operator_resolution,resolution_submission_id
        FROM ep_parity_lifecycle_dispatches_schema50""")
    for table in ("ep_submission_events_schema50", "ep_submission_prompt_history_schema50", "ep_parity_lifecycle_dispatches_schema50", "ep_submissions_schema50"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(51)")
    connection.execute("UPDATE engineering_metadata SET value='51' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=51")


def _migrate_schema_52(connection: sqlite3.Connection) -> None:
    """Add the sole immutable CENTRAL receipt-to-run provenance authority."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema51")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52)))")
    connection.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,52 FROM ep_installations_schema51")
    connection.execute("DROP TABLE ep_installations_schema51")
    connection.execute("""CREATE TABLE ep_receipt_run_provenance (
        submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id),
        run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id),
        project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        installation_id TEXT NOT NULL REFERENCES ep_installations(instance_id),
        created_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX ep_receipt_run_provenance_project_lookup ON ep_receipt_run_provenance(project_id,created_at DESC)")
    connection.execute("""CREATE TRIGGER ep_receipt_run_provenance_scope_insert
        BEFORE INSERT ON ep_receipt_run_provenance BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ep_submissions s WHERE s.submission_id=NEW.submission_id AND s.project_id=NEW.project_id AND s.repository_id=NEW.repository_id) THEN RAISE(ABORT,'PROVENANCE_SUBMISSION_SCOPE_MISMATCH') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ep_execution_runs r WHERE r.run_id=NEW.run_id AND r.project_id=NEW.project_id) THEN RAISE(ABORT,'PROVENANCE_RUN_PROJECT_MISMATCH') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ep_parity_lifecycle_dispatches d WHERE d.run_id=NEW.run_id AND d.submission_id=NEW.submission_id AND d.project_id=NEW.project_id AND d.repository_id=NEW.repository_id) THEN RAISE(ABORT,'PROVENANCE_DISPATCH_SCOPE_MISMATCH') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM engineering_metadata WHERE key='installation.instance_id' AND value=NEW.installation_id) THEN RAISE(ABORT,'PROVENANCE_INSTALLATION_MISMATCH') END;
    END""")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER ep_receipt_run_provenance_immutable_{operation.casefold()} BEFORE {operation} ON ep_receipt_run_provenance BEGIN SELECT RAISE(ABORT,'PROVENANCE_IMMUTABLE'); END")
    # Backfill only rows whose canonical dispatch already proves every scope.
    connection.execute("""INSERT INTO ep_receipt_run_provenance(submission_id,run_id,project_id,repository_id,installation_id,created_at)
        SELECT d.submission_id,d.run_id,d.project_id,d.repository_id,m.value,d.claimed_at
        FROM ep_parity_lifecycle_dispatches d JOIN ep_submissions s ON s.submission_id=d.submission_id AND s.project_id=d.project_id AND s.repository_id=d.repository_id
        JOIN ep_execution_runs r ON r.run_id=d.run_id AND r.project_id=d.project_id
        JOIN engineering_metadata m ON m.key='installation.instance_id'""")
    missing = connection.execute(
        """SELECT 1 FROM ep_parity_lifecycle_dispatches d
           WHERE NOT EXISTS (
               SELECT 1 FROM ep_receipt_run_provenance p
               WHERE p.submission_id=d.submission_id AND p.run_id=d.run_id
                 AND p.project_id=d.project_id AND p.repository_id=d.repository_id
           ) LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise ServerConfigurationError("CENTRAL receipt-to-run provenance migration is incomplete.")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(52)")
    connection.execute("UPDATE engineering_metadata SET value='52' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=52")


_CONSUMER_CREDENTIAL_COLUMNS = (
    "credential_id", "consumer_id", "project_id", "verifier", "fingerprint",
    "issued_at", "expires_at", "revoked_at", "replaced_by_credential_id",
)
_CONSUMER_REGISTRATION_COLUMNS = (
    "consumer_id", "project_id", "status", "created_at", "updated_at",
    "disabled_at", "revoked_at", "audit_metadata",
)


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _require_consumer_table_shape(
    connection: sqlite3.Connection, *, credentials: str, registrations: str,
) -> None:
    """Validate only table metadata; never surface credential values."""

    if _table_columns(connection, credentials) != _CONSUMER_CREDENTIAL_COLUMNS:
        raise ServerConfigurationError("EP consumer credential table shape is invalid.")
    if _table_columns(connection, registrations) != _CONSUMER_REGISTRATION_COLUMNS:
        raise ServerConfigurationError("EP consumer registration table shape is invalid.")
    credential_pk = tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({credentials})") if int(row[5]) > 0
    )
    registration_pk = tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({registrations})") if int(row[5]) > 0
    )
    if credential_pk != ("credential_id",) or registration_pk != ("consumer_id", "project_id"):
        raise ServerConfigurationError("EP consumer credential table identity is invalid.")


def _install_ep_consumer_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE ep_consumer_credentials ("
        "credential_id TEXT PRIMARY KEY CHECK(length(credential_id) BETWEEN 1 AND 128),"
        "consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128),"
        "project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),"
        "verifier BLOB NOT NULL UNIQUE CHECK(length(verifier)=32),"
        "fingerprint BLOB NOT NULL UNIQUE CHECK(length(fingerprint)=32),"
        "issued_at TEXT NOT NULL,expires_at TEXT,revoked_at TEXT,"
        "replaced_by_credential_id TEXT REFERENCES ep_consumer_credentials(credential_id))"
    )
    connection.execute(
        "CREATE INDEX ep_consumer_credentials_scope_lookup "
        "ON ep_consumer_credentials(consumer_id,project_id,revoked_at)"
    )
    connection.execute(
        "CREATE TABLE ep_consumer_registrations ("
        "consumer_id TEXT NOT NULL CHECK(length(consumer_id) BETWEEN 1 AND 128),"
        "project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),"
        "status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED','REVOKED')),"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,disabled_at TEXT,revoked_at TEXT,"
        "audit_metadata TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(consumer_id,project_id))"
    )
    connection.execute(
        "CREATE INDEX ep_consumer_registrations_status_lookup "
        "ON ep_consumer_registrations(consumer_id,project_id,status)"
    )


def _assert_exact_consumer_transfer(
    connection: sqlite3.Connection, *, source: str, destination: str, columns: tuple[str, ...],
) -> None:
    """Prove cardinality and values match without exposing credential material."""

    column_list = ",".join(columns)
    source_count = int(connection.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0])
    destination_count = int(connection.execute(f"SELECT COUNT(*) FROM {destination}").fetchone()[0])
    if source_count != destination_count:
        raise ServerConfigurationError("EP consumer credential transfer cardinality is invalid.")
    missing = connection.execute(
        f"SELECT {column_list} FROM {source} EXCEPT SELECT {column_list} FROM {destination} LIMIT 1"
    ).fetchone()
    extra = connection.execute(
        f"SELECT {column_list} FROM {destination} EXCEPT SELECT {column_list} FROM {source} LIMIT 1"
    ).fetchone()
    if missing is not None or extra is not None:
        raise ServerConfigurationError("EP consumer credential transfer identity is invalid.")


def _migrate_schema_53(connection: sqlite3.Connection) -> None:
    """Transfer consumer credentials to the neutral Server/CENTRAL namespace.

    The entire migration is called from the Server's enclosing immediate
    transaction.  Legacy tables are retained untouched as migration evidence;
    runtime code switches exclusively to the new tables only after the exact
    transfer proof succeeds and schema metadata advances.
    """

    if _schema_version(connection) != 52:
        raise ServerConfigurationError("EP consumer credential migration schema version is invalid.")
    metadata = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key='installation.schema_version'"
    ).fetchone()
    if metadata is None or str(metadata[0]) != "52":
        raise ServerConfigurationError("EP consumer credential migration metadata is invalid.")
    legacy_credentials, legacy_registrations = (
        "local_api_credentials", "local_api_consumer_registrations",
    )
    current_credentials, current_registrations = (
        "ep_consumer_credentials", "ep_consumer_registrations",
    )
    tables = _table_names(connection)
    legacy = {legacy_credentials, legacy_registrations} & tables
    current = {current_credentials, current_registrations} & tables
    if legacy and legacy != {legacy_credentials, legacy_registrations}:
        raise ServerConfigurationError("EP consumer credential migration source is incomplete.")
    if current and current != {current_credentials, current_registrations}:
        raise ServerConfigurationError("EP consumer credential migration destination is incomplete.")
    if legacy and current:
        raise ServerConfigurationError("EP consumer credential migration has ambiguous parallel authority.")
    if legacy:
        _require_consumer_table_shape(
            connection, credentials=legacy_credentials, registrations=legacy_registrations,
        )
        _install_ep_consumer_schema(connection)
        connection.execute(
            "INSERT INTO ep_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,disabled_at,revoked_at,audit_metadata) "
            "SELECT consumer_id,project_id,status,created_at,updated_at,disabled_at,revoked_at,audit_metadata "
            "FROM local_api_consumer_registrations"
        )
        connection.execute(
            "INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at,expires_at,revoked_at,replaced_by_credential_id) "
            "SELECT credential_id,consumer_id,project_id,verifier,fingerprint,issued_at,expires_at,revoked_at,replaced_by_credential_id "
            "FROM local_api_credentials"
        )
        _assert_exact_consumer_transfer(
            connection, source=legacy_registrations, destination=current_registrations,
            columns=_CONSUMER_REGISTRATION_COLUMNS,
        )
        _assert_exact_consumer_transfer(
            connection, source=legacy_credentials, destination=current_credentials,
            columns=_CONSUMER_CREDENTIAL_COLUMNS,
        )
    elif current:
        _require_consumer_table_shape(
            connection, credentials=current_credentials, registrations=current_registrations,
        )
    else:
        raise ServerConfigurationError("EP consumer credential migration source is absent.")
    # This table is referenced by the schema-52 receipt provenance table.  Keep
    # those foreign-key declarations pointed at the canonical name while the
    # installation CHECK constraint is widened for schema 53.  Without this
    # SQLite rewrites a dependent reference to the temporary table name, which
    # would leave the completed store structurally invalid after that temporary
    # table is retired.
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema52")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53)))"
    )
    connection.execute(
        "INSERT INTO ep_installations(instance_id,created_at,schema_version) "
        "SELECT instance_id,created_at,53 FROM ep_installations_schema52"
    )
    connection.execute("DROP TABLE ep_installations_schema52")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(53)")
    connection.execute("UPDATE engineering_metadata SET value='53' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=53")


def _migrate_schema_54(connection: sqlite3.Connection) -> None:
    """Widen CENTRAL submission state for audited operator handling."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema53")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,54 FROM ep_installations_schema53")
    connection.execute("DROP TABLE ep_installations_schema53")
    for table in ("ep_submission_events", "ep_submission_prompt_history", "ep_parity_lifecycle_dispatches", "ep_submissions"):
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_schema53")
    connection.execute("""CREATE TABLE ep_submissions (
        submission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id), producer_id TEXT NOT NULL,
        producer_type TEXT NOT NULL, producer_version TEXT, transport TEXT NOT NULL CHECK(transport IN ('HTTP','CLI','FILE_INBOX','DEPENDABOT','LEGACY_FILE')),
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, constraints TEXT NOT NULL, idempotency_key TEXT, correlation_id TEXT,
        mission_id TEXT, engineering_action_id TEXT, transport_receipt_id TEXT, transport_received_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('QUEUED','REJECTED','DEFERRED','QUARANTINED','DECLINED')), admission TEXT NOT NULL, created_at TEXT NOT NULL)""")
    # Do not use ``SELECT *`` here: an interrupted/newer installation can
    # retain later additive columns while its recorded schema is still being
    # recovered.  Schema-54 owns exactly these predecessor columns.
    connection.execute("""INSERT INTO ep_submissions(
        submission_id,project_id,repository_id,producer_id,producer_type,
        producer_version,transport,prompt,prompt_digest,constraints,
        idempotency_key,correlation_id,mission_id,engineering_action_id,
        transport_receipt_id,transport_received_at,state,admission,created_at
    ) SELECT
        submission_id,project_id,repository_id,producer_id,producer_type,
        producer_version,transport,prompt,prompt_digest,constraints,
        idempotency_key,correlation_id,mission_id,engineering_action_id,
        transport_receipt_id,transport_received_at,state,admission,created_at
      FROM ep_submissions_schema53""")
    connection.execute("CREATE TABLE ep_submission_events (event_id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id), event_kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("INSERT INTO ep_submission_events SELECT * FROM ep_submission_events_schema53")
    connection.execute("CREATE TABLE ep_submission_prompt_history (submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), prompt_digest TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    connection.execute("INSERT INTO ep_submission_prompt_history SELECT * FROM ep_submission_prompt_history_schema53")
    connection.execute("""CREATE TABLE ep_parity_lifecycle_dispatches (submission_id TEXT PRIMARY KEY REFERENCES ep_submissions(submission_id), project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id), repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id), run_id TEXT NOT NULL UNIQUE REFERENCES ep_execution_runs(run_id), state TEXT NOT NULL CHECK(state IN ('CLAIMED','RUNNING','COMPLETE','BLOCKED','FAILED')), prompt_path TEXT NOT NULL, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL, operator_resolution TEXT NOT NULL DEFAULT 'NONE' CHECK(operator_resolution IN ('NONE','OPEN','DISMISSED','RETRIED')), resolution_submission_id TEXT REFERENCES ep_submissions(submission_id))""")
    connection.execute("INSERT INTO ep_parity_lifecycle_dispatches SELECT * FROM ep_parity_lifecycle_dispatches_schema53")
    for table in ("ep_submission_events_schema53", "ep_submission_prompt_history_schema53", "ep_parity_lifecycle_dispatches_schema53", "ep_submissions_schema53"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("CREATE INDEX ep_submissions_project_lookup ON ep_submissions(project_id,state,created_at DESC)")
    connection.execute("CREATE UNIQUE INDEX ep_submissions_idempotency_lookup ON ep_submissions(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    connection.execute("CREATE INDEX ep_parity_lifecycle_dispatches_run_lookup ON ep_parity_lifecycle_dispatches(run_id,state)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(54)")
    connection.execute("UPDATE engineering_metadata SET value='54' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=54")


def _migrate_schema_55(connection: sqlite3.Connection) -> None:
    """Add durable CAS and idempotency evidence for CENTRAL queue commands."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema54")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,55 FROM ep_installations_schema54")
    connection.execute("DROP TABLE ep_installations_schema54")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ep_submissions)")}
    if "disposition_revision" not in columns:
        connection.execute("ALTER TABLE ep_submissions ADD COLUMN disposition_revision INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE TABLE IF NOT EXISTS ep_queue_disposition_operations (operation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, submission_id TEXT NOT NULL REFERENCES ep_submissions(submission_id), actor_reference TEXT NOT NULL, command_digest TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, previous_revision INTEGER NOT NULL, resulting_revision INTEGER NOT NULL, event_id INTEGER NOT NULL REFERENCES ep_submission_events(event_id), recorded_at TEXT NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(55)")
    connection.execute("UPDATE engineering_metadata SET value='55' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=55")


def _migrate_schema_56(connection: sqlite3.Connection) -> None:
    """Add explicitly granted, project-scoped queue operator capabilities."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema55")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,56 FROM ep_installations_schema55")
    connection.execute("DROP TABLE ep_installations_schema55")
    connection.execute("CREATE TABLE IF NOT EXISTS ep_operator_capabilities (consumer_id TEXT NOT NULL, project_id TEXT NOT NULL, capability TEXT NOT NULL CHECK(capability IN ('QUEUE_HOLD_RESUME','QUEUE_DECLINE')), granted_at TEXT NOT NULL, revoked_at TEXT, PRIMARY KEY(consumer_id,project_id,capability))")
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(56)")
    connection.execute("UPDATE engineering_metadata SET value='56' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=56")


def _migrate_schema_57(connection: sqlite3.Connection) -> None:
    """Activate retained-host retry-attempt lineage in the CENTRAL store."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema56")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,57 FROM ep_installations_schema56")
    connection.execute("DROP TABLE ep_installations_schema56")
    storage.install_central_execution_submission_retry_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(57)")
    connection.execute("UPDATE engineering_metadata SET value='57' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=57")


def _migrate_schema_58(connection: sqlite3.Connection) -> None:
    """Install the retained-host validation identity table in CENTRAL.

    Version 42 of the retired local store introduced this table, but its
    additive schema was not yet included in the Server-owned migration chain.
    Without it, the first real retained-host execution can fail before it
    creates a checkpoint. This migration is idempotent and does not rewrite
    any execution evidence.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema57")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,58 FROM ep_installations_schema57")
    connection.execute("DROP TABLE ep_installations_schema57")
    storage.install_central_execution_validation_profile_identity_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(58)")
    connection.execute("UPDATE engineering_metadata SET value='58' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=58")


def _migrate_schema_59(connection: sqlite3.Connection) -> None:
    """Persist immutable, versioned Forge↔EP acceptance provenance.

    The row intentionally excludes the producer prompt, bearer credential and
    any local checkout path.  It is evidence of the inter-product exchange,
    not a second source of execution evidence or an operator-retained log.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema58")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,59 FROM ep_installations_schema58")
    connection.execute("DROP TABLE ep_installations_schema58")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ep_forge_exchange_audit (
            audit_id TEXT PRIMARY KEY,
            submission_id TEXT NOT NULL UNIQUE REFERENCES ep_submissions(submission_id),
            project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
            direction TEXT NOT NULL CHECK(direction='FORGE_TO_EP'),
            event_kind TEXT NOT NULL CHECK(event_kind='FORGE_SUBMISSION_ACCEPTED'),
            receipt_id TEXT NOT NULL UNIQUE,
            producer_contract_version TEXT NOT NULL,
            forge_provenance_contract_version TEXT NOT NULL,
            forge_application_version TEXT NOT NULL,
            ep_application_version TEXT NOT NULL,
            producer_readback_contract_version TEXT NOT NULL,
            accepted_request_digest TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ep_forge_exchange_audit_project_lookup ON ep_forge_exchange_audit(project_id,recorded_at DESC,submission_id);
        CREATE TRIGGER IF NOT EXISTS ep_forge_exchange_audit_scope_insert BEFORE INSERT ON ep_forge_exchange_audit
        WHEN NOT EXISTS (SELECT 1 FROM ep_submissions AS submission WHERE submission.submission_id=NEW.submission_id AND submission.project_id=NEW.project_id)
        BEGIN SELECT RAISE(ABORT, 'FORGE_EXCHANGE_SUBMISSION_SCOPE_MISMATCH'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_exchange_audit_immutable_update BEFORE UPDATE ON ep_forge_exchange_audit
        BEGIN SELECT RAISE(ABORT, 'Forge exchange audit is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ep_forge_exchange_audit_immutable_delete BEFORE DELETE ON ep_forge_exchange_audit
        BEGIN SELECT RAISE(ABORT, 'Forge exchange audit is immutable'); END;
    """)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(59)")
    connection.execute("UPDATE engineering_metadata SET value='59' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=59")


def _migrate_schema_60(connection: sqlite3.Connection) -> None:
    """Activate CENTRAL artifact bindings for a pre-existing Server store.

    Storage schema 44 introduced the additive columns, but a Server already
    at schema 59 does not replay historical retained-store migrations.  This
    explicit Server-owned upgrade makes producer readback safe on that exact
    installed path without rewriting an execution or its immutable evidence.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema59")
    connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60)))")
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,60 FROM ep_installations_schema59")
    connection.execute("DROP TABLE ep_installations_schema59")
    storage.install_central_execution_artifact_binding_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(60)")
    connection.execute("UPDATE engineering_metadata SET value='60' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=60")


def _migrate_schema_61(connection: sqlite3.Connection) -> None:
    """Persist prospective, immutable Forge Action-context envelopes.

    No historical submission is populated here. A summary is valid only when
    it crossed the versioned Forge→EP boundary with that submission; deriving
    one later from a retained prompt would rewrite historical evidence.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema60")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61)))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,61 FROM ep_installations_schema60")
    connection.execute("DROP TABLE ep_installations_schema60")
    _install_forge_action_context_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(61)")
    connection.execute("UPDATE engineering_metadata SET value='61' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=61")


def _migrate_schema_62(connection: sqlite3.Connection) -> None:
    """Persist prospective Forge planning and Execution Host evidence.

    These are new immutable facts.  Existing submissions and completed runs
    remain untouched; their absent planning or host observations are never
    reconstructed from a prompt or a later checkout.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema61")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62)))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,62 FROM ep_installations_schema61")
    connection.execute("DROP TABLE ep_installations_schema61")
    _install_forge_planning_context_schema(connection)
    _install_execution_host_evidence_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(62)")
    connection.execute("UPDATE engineering_metadata SET value='62' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=62")


def _migrate_schema_63(connection: sqlite3.Connection) -> None:
    """Persist shielded, correlation-bound host and recovery diagnostics.

    Existing public logs stay immutable and are not backfilled: they do not
    have a trustworthy historical relation to a previously omitted traceback.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema62")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63)))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,63 FROM ep_installations_schema62")
    connection.execute("DROP TABLE ep_installations_schema62")
    _install_technical_diagnostics_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(63)")
    connection.execute("UPDATE engineering_metadata SET value='63' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=63")


def _migrate_schema_64(connection: sqlite3.Connection) -> None:
    """Add durable installed-owner Forge credential recovery operations."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema63")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64)))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,64 FROM ep_installations_schema63")
    connection.execute("DROP TABLE ep_installations_schema63")
    owner_credential_recovery.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(64)")
    connection.execute("UPDATE engineering_metadata SET value='64' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=64")


def _migrate_schema_65(connection: sqlite3.Connection) -> None:
    """Fence credential recovery and preserve guarded config-adoption evidence."""

    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema64")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65)))"
    )
    connection.execute(
        "INSERT INTO ep_installations SELECT instance_id,created_at,65 "
        "FROM ep_installations_schema64"
    )
    connection.execute("DROP TABLE ep_installations_schema64")
    columns = {
        str(row[1]) for row in connection.execute(
            "PRAGMA table_info(ep_consumer_credential_recovery_operations)"
        )
    }
    if "adopted_from_peer_configuration_digest" not in columns:
        connection.execute(
            "ALTER TABLE ep_consumer_credential_recovery_operations "
            "ADD COLUMN adopted_from_peer_configuration_digest TEXT"
        )
    if "configuration_adopted_at" not in columns:
        connection.execute(
            "ALTER TABLE ep_consumer_credential_recovery_operations "
            "ADD COLUMN configuration_adopted_at TEXT"
        )
    connection.execute("DROP INDEX ep_consumer_credential_recovery_active_scope")
    connection.execute(
        "CREATE INDEX ep_consumer_credential_recovery_active_scope "
        "ON ep_consumer_credential_recovery_operations(consumer_id,project_id) "
        "WHERE state IN ('PREPARED','CENTRAL_ACTIVATED','STOPPED_UNCERTAIN')"
    )
    # Candidates staged by the previous implementation had no explicit
    # validity bound.  Expire only those exact operation-linked candidates;
    # the owning replay route then reconciles or revokes them without touching
    # any production credential.
    connection.execute(
        "UPDATE ep_consumer_credentials SET expires_at=CURRENT_TIMESTAMP "
        "WHERE expires_at IS NULL AND credential_id IN ("
        "SELECT credential_id FROM ep_consumer_credential_recovery_operations "
        "WHERE credential_id IS NOT NULL "
        "AND state IN ('CENTRAL_ACTIVATED','STOPPED_UNCERTAIN'))"
    )
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(65)")
    connection.execute("UPDATE engineering_metadata SET value='65' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=65")


def _migrate_schema_66(connection: sqlite3.Connection) -> None:
    """Add the protected reconciliation PR gate and check evidence."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema65")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66)))"
    )
    connection.execute(
        "INSERT INTO ep_installations SELECT instance_id,created_at,66 "
        "FROM ep_installations_schema65"
    )
    connection.execute("DROP TABLE ep_installations_schema65")
    storage.install_central_reconciliation_pr_evidence_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(66)")
    connection.execute("UPDATE engineering_metadata SET value='66' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=66")


def _migrate_schema_67(connection: sqlite3.Connection) -> None:
    """Add guarded terminal-evidence projection reconciliation."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema66")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version IN "
        "(41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67)))"
    )
    connection.execute(
        "INSERT INTO ep_installations SELECT instance_id,created_at,67 "
        "FROM ep_installations_schema66"
    )
    connection.execute("DROP TABLE ep_installations_schema66")
    submission_service.install_terminal_evidence_reconciliation_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(67)")
    connection.execute("UPDATE engineering_metadata SET value='67' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=67")


def _migrate_schema_68(connection: sqlite3.Connection) -> None:
    """Install product-owned reset control and repair the CENTRAL chat parent.

    Existing chat rows are copied unchanged.  The migration validates that
    every row belongs to its canonical execution run; it never fabricates a
    prompt-history parent and never deletes historical chat evidence.
    """
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema67")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 41 AND 68))"
    )
    connection.execute(
        "INSERT INTO ep_installations SELECT instance_id,created_at,68 "
        "FROM ep_installations_schema67"
    )
    connection.execute("DROP TABLE ep_installations_schema67")
    central_operational_reset.install_schema(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(68)")
    connection.execute("UPDATE engineering_metadata SET value='68' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=68")


def _migrate_schema_69(connection: sqlite3.Connection) -> None:
    """Add owner-issued, scoped and revocable merge delegation records."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema68")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 41 AND 69))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,69 FROM ep_installations_schema68")
    connection.execute("DROP TABLE ep_installations_schema68")
    merge_delegation.install_schema(connection)
    central_operational_reset.install_writer_fences(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(69)")
    connection.execute("UPDATE engineering_metadata SET value='69' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=69")


def _migrate_schema_70(connection: sqlite3.Connection) -> None:
    """Bind an immutable, owner-selected assurance profile to each merge grant."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema69")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 41 AND 70))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,70 FROM ep_installations_schema69")
    connection.execute("DROP TABLE ep_installations_schema69")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ep_merge_delegations)")}
    if "assurance_profile_id" not in columns:
        connection.execute("ALTER TABLE ep_merge_delegations ADD COLUMN assurance_profile_id TEXT NOT NULL DEFAULT ''")
    if "assurance_profile_revision" not in columns:
        connection.execute("ALTER TABLE ep_merge_delegations ADD COLUMN assurance_profile_revision TEXT NOT NULL DEFAULT ''")
    if "assurance_policy_digest" not in columns:
        connection.execute("ALTER TABLE ep_merge_delegations ADD COLUMN assurance_policy_digest TEXT NOT NULL DEFAULT ''")
    merge_delegation.install_profile_guard(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(70)")
    connection.execute("UPDATE engineering_metadata SET value='70' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=70")


def _migrate_schema_71(connection: sqlite3.Connection) -> None:
    """Restore reset writer fences lost when schema 70 rebuilt installations."""
    connection.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema70")
    connection.execute(
        "CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 41 AND 71))"
    )
    connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,71 FROM ep_installations_schema70")
    connection.execute("DROP TABLE ep_installations_schema70")
    central_operational_reset.install_writer_fences(connection)
    connection.execute("INSERT OR IGNORE INTO engineering_schema_migrations(version) VALUES(71)")
    connection.execute("UPDATE engineering_metadata SET value='71' WHERE key='installation.schema_version'")
    connection.execute("UPDATE ep_installations SET schema_version=71")


_SERVER_SCHEMA_UPGRADE_STEPS = (
    (42, _migrate_schema_42),
    (43, _migrate_schema_43),
    (44, _migrate_schema_44),
    (45, _migrate_schema_45),
    (46, _migrate_schema_46),
    (47, _migrate_schema_47),
    (48, _migrate_schema_48),
    (49, _migrate_schema_49),
    (50, _migrate_schema_50),
    (51, _migrate_schema_51),
    (52, _migrate_schema_52),
    (53, _migrate_schema_53),
    (54, _migrate_schema_54),
    (55, _migrate_schema_55),
    (56, _migrate_schema_56),
    (57, _migrate_schema_57),
    (58, _migrate_schema_58),
    (59, _migrate_schema_59),
    (60, _migrate_schema_60),
    (61, _migrate_schema_61),
    (62, _migrate_schema_62),
    (63, _migrate_schema_63),
    (64, _migrate_schema_64),
    (65, _migrate_schema_65),
    (66, _migrate_schema_66),
    (67, _migrate_schema_67),
    (68, _migrate_schema_68),
    (69, _migrate_schema_69),
    (70, _migrate_schema_70),
    (71, _migrate_schema_71),
)
_SUPPORTED_SERVER_SCHEMA_VERSIONS = frozenset(
    range(41, SERVER_STORE_SCHEMA_VERSION + 1)
)


def _upgrade_existing_schema(connection: sqlite3.Connection, current_schema: int) -> None:
    """Apply every required forward-only step to one retained installation."""
    for target_schema, upgrade in _SERVER_SCHEMA_UPGRADE_STEPS:
        if current_schema < target_schema:
            upgrade(connection)


def validate_store(data_root: Path, identity: RuntimeIdentity) -> dict[str, object]:
    """Return a deterministic fail-closed current-schema structural report."""
    path = data_root / SERVER_DATABASE_FILENAME
    try:
        with storage.sqlite_connection(f"file:{path}?mode=ro", uri=True) as connection:
            tables = _table_names(connection)
            indexes = _index_names(connection)
            views = _view_names(connection)
            triggers = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            schema = _schema_version(connection)
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            metadata = dict(connection.execute("SELECT key,value FROM engineering_metadata WHERE key IN ('installation.instance_id','installation.schema_version')"))
            installation = connection.execute("SELECT instance_id FROM ep_installations WHERE instance_id=?", (identity.instance_id,)).fetchone()
    except (OSError, sqlite3.DatabaseError) as error:
        raise ServerConfigurationError("EP Server store is unavailable.") from error
    valid = schema == SERVER_STORE_SCHEMA_VERSION and SERVER_REQUIRED_TABLES <= tables and SERVER_REQUIRED_INDEXES <= indexes and SERVER_REQUIRED_VIEWS <= views and {
        "ep_forge_exchange_audit_scope_insert", "ep_forge_exchange_audit_immutable_update",
        "ep_forge_exchange_audit_immutable_delete", "ep_forge_action_context_envelopes_scope_insert",
        "ep_forge_action_context_envelopes_immutable_update",
        "ep_forge_action_context_envelopes_immutable_delete",
        "ep_forge_planning_context_envelopes_scope_insert",
        "ep_forge_planning_context_envelopes_immutable_update",
        "ep_forge_planning_context_envelopes_immutable_delete",
        "ep_execution_host_evidence_start_immutable",
        "ep_execution_host_evidence_immutable_delete",
        "ep_technical_diagnostics_immutable_update",
        "ep_technical_diagnostics_immutable_delete",
        "ep_terminal_evidence_reconciliation_immutable_update",
        "ep_merge_delegations_profile_immutable",
        "ep_terminal_evidence_reconciliation_immutable_delete",
    } <= triggers and integrity == ["ok"] and metadata == {"installation.instance_id": identity.instance_id, "installation.schema_version": str(SERVER_STORE_SCHEMA_VERSION)} and installation is not None
    if not valid:
        raise ServerConfigurationError(
            f"EP Server store is not a valid official schema-{SERVER_STORE_SCHEMA_VERSION} installation."
        )
    return {"schema_version": schema, "integrity": "PASS", "required_tables": sorted(SERVER_REQUIRED_TABLES), "required_indexes": sorted(SERVER_REQUIRED_INDEXES), "required_views": sorted(SERVER_REQUIRED_VIEWS)}


def initialize(data_root: Path, *, bind_host: str = "127.0.0.1", bind_port: int = 8765) -> RuntimeIdentity:
    """Create or validate an empty, installation-owned server instance."""
    data_root = data_root.resolve()
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    central_database.migrate_legacy_database(data_root)
    config_path = data_root / SERVER_CONFIGURATION_FILENAME
    if not config_path.exists():
        if bind_host != "127.0.0.1" or not 1 <= bind_port <= 65535:
            raise ServerConfigurationError("EP Server initial bind configuration is invalid.")
        _write_json(config_path, asdict(ServerConfiguration(
            SERVER_CONFIGURATION_VERSION, bind_host, bind_port,
            str(default_engineering_platform_codex_cli_prefix()),
            _console_platform_version(),
        )))
    configuration = ServerConfiguration.load(data_root)
    # Version 1 inferred the CLI installation at each process boundary from
    # HOME.  Upgrade it once, under the server's stable account identity, so
    # child workers and later restarts inherit one installation authority.
    if (configuration.version != SERVER_CONFIGURATION_VERSION
            or configuration.product_version != _console_platform_version()):
        configuration = ServerConfiguration(
            SERVER_CONFIGURATION_VERSION,
            configuration.bind_host,
            configuration.bind_port,
            configuration.managed_codex_cli_prefix,
            _console_platform_version(),
        )
        _write_json(config_path, asdict(configuration))
    identity_path = data_root / SERVER_IDENTITY_FILENAME
    if identity_path.exists():
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
            identity = RuntimeIdentity(str(raw["instance_id"]), str(raw["created_at"]))
            if not identity.instance_id or not identity.created_at:
                raise ValueError("empty identity")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server runtime identity is invalid.") from error
    else:
        identity = RuntimeIdentity(str(uuid4()), _utcnow())
        _write_json(identity_path, asdict(identity))
    database_path = data_root / SERVER_DATABASE_FILENAME
    if database_path.exists():
        try:
            with storage.sqlite_connection(f"file:{database_path}?mode=ro", uri=True) as existing:
                existing_tables = _table_names(existing)
                if existing_tables:
                    current_schema = _schema_version(existing)
                    if current_schema not in _SUPPORTED_SERVER_SCHEMA_VERSIONS:
                        raise ServerConfigurationError(
                            f"EP Server store is not a valid official schema-{SERVER_STORE_SCHEMA_VERSION} installation."
                        )
                    if current_schema == SERVER_STORE_SCHEMA_VERSION:
                        validate_store(data_root, identity)
                        return identity
                    if current_schema < SERVER_STORE_SCHEMA_VERSION:
                        with storage.sqlite_connection(database_path) as connection:
                            # Schema-49 rebuilds the submission parent table
                            # to widen its immutable transport constraint.
                            connection.execute("PRAGMA foreign_keys=OFF")
                            connection.execute("PRAGMA legacy_alter_table=ON")
                            connection.execute("BEGIN IMMEDIATE")
                            _upgrade_existing_schema(connection, current_schema)
                            connection.execute("COMMIT")
                            connection.execute("PRAGMA legacy_alter_table=OFF")
                        validate_store(data_root, identity)
                        return identity
        except sqlite3.DatabaseError as error:
            raise ServerConfigurationError("EP Server store is unavailable.") from error
    with storage.sqlite_connection(database_path) as connection:
        # Schema-53 widens ep_installations while schema-52 provenance already
        # references it.  SQLite must retain those declarations at the
        # canonical name during the enclosing rebuild transaction.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        _install_current_schema(connection, identity)
        connection.execute("COMMIT")
        connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
    database_path.chmod(0o600)
    validate_store(data_root, identity)
    return identity


def _runtime(data_root: Path) -> dict[str, object] | None:
    try:
        raw = json.loads((data_root / SERVER_RUNTIME_FILENAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _transport_components(data_root: Path, *, server_running: bool) -> dict[str, dict[str, object]]:
    """Return secret-free, platform-scoped ingress observability from CENTRAL.

    Submission timestamps are observation only.  They never change queue or
    execution semantics and intentionally retain no credential or raw prompt.
    """
    latest: dict[str, str] = {}
    with storage.sqlite_connection(f"file:{data_root / SERVER_DATABASE_FILENAME}?mode=ro", uri=True) as connection:
        for transport, created_at in connection.execute(
            "SELECT transport,MAX(created_at) FROM ep_submissions GROUP BY transport"
        ):
            latest[str(transport)] = str(created_at)
    http_status_code = "HTTP_INGRESS_HEALTHY" if server_running else "HTTP_INGRESS_DOWN"
    cli_status_code = "CLI_INGRESS_AVAILABLE" if server_running else "CLI_INGRESS_DEGRADED"
    file_last = latest.get("FILE_INBOX")
    dependabot_last = latest.get("DEPENDABOT")
    heartbeat = file_inbox.read_heartbeat(data_root / FILE_INBOX_DIRECTORY)
    heartbeat_at = str(heartbeat.get("updated_at", "")) if heartbeat else ""
    try:
        heartbeat_fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat_at)).total_seconds() <= 10
    except ValueError:
        heartbeat_fresh = False
    delivery_retry = str(heartbeat.get("delivery_retry", "NONE")) if heartbeat else "NONE"
    submission_ready = bool(heartbeat and heartbeat.get("state") == "READY" and heartbeat.get("readiness") == "SUBMISSION_CAPABLE")
    quarantine_count = int(heartbeat.get("quarantine_count", 0)) if heartbeat and isinstance(heartbeat.get("quarantine_count", 0), int) else 0
    recent_error = heartbeat.get("recent_error") if heartbeat else None
    # A live watcher with pending delivery, a bounded adapter diagnostic, or
    # quarantined ingress is operational but needs attention.  It is not a
    # CENTRAL execution/run failure and cannot affect queue authority.
    file_attention_needed = delivery_retry != "NONE" or bool(recent_error) or quarantine_count > 0
    file_status_code = (
        "FILE_INGRESS_STOPPED" if not server_running or not heartbeat_fresh
        else "FILE_INGRESS_NOT_READY" if not submission_ready
        else "FILE_INGRESS_DEGRADED" if file_attention_needed else "FILE_INGRESS_RUNNING"
    )
    dependabot_heartbeat = dependabot_producer.read_heartbeat(data_root)
    dependabot_updated = str(dependabot_heartbeat.get("updated_at", "")) if dependabot_heartbeat else ""
    try:
        dependabot_fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(dependabot_updated)).total_seconds() <= 310
    except ValueError:
        dependabot_fresh = False
    dependabot_ready = bool(
        server_running
        and dependabot_fresh
        and dependabot_heartbeat
        and dependabot_heartbeat.get("state") == "READY"
        and dependabot_heartbeat.get("readiness") == "DISCOVERY_CAPABLE"
    )
    return {
        "http_ingress": {
            "healthy": server_running, "status_code": http_status_code,
            "detail_code": "CENTRAL_LISTENER_ENDPOINT" if server_running else "CENTRAL_LISTENER_UNAVAILABLE",
            "version": "1",  # canonical submission protocol version
            "last_successful_submission": latest.get("HTTP"), "recent_error": None,
        },
        "cli_ingress": {
            "healthy": server_running, "status_code": cli_status_code,
            "detail_code": "CANONICAL_SUBMISSION_COMPATIBILITY" if server_running else "CENTRAL_ENDPOINT_UNAVAILABLE",
            "version": "1", "last_successful_submission": latest.get("CLI"), "recent_error": None,
        },
        "file_inbox_ingress": {
            "healthy": file_status_code == "FILE_INGRESS_RUNNING", "status_code": file_status_code,
            "detail_code": "FILE_INBOX_HEARTBEAT" if server_running and heartbeat_fresh else "FILE_INBOX_HEARTBEAT_MISSING",
            "watched_location": heartbeat.get("watched_location") if heartbeat else str(data_root / FILE_INBOX_DIRECTORY),
            "heartbeat": heartbeat_at or None,
            "last_successful_submission": file_last,
            "delivery_retry_code": f"FILE_INGRESS_DELIVERY_RETRY_{delivery_retry}",
            "quarantine_count": quarantine_count,
            # Never transport a raw exception into a presentation projection.
            "reason_code": "FILE_INBOX_DIAGNOSTIC" if recent_error else None,
        },
        "dependabot_producer": {
            "healthy": dependabot_ready,
            "status_code": "DEPENDABOT_READY" if dependabot_ready else "DEPENDABOT_DEGRADED",
            "detail_code": "DEPENDABOT_HEARTBEAT" if dependabot_fresh else "DEPENDABOT_HEARTBEAT_MISSING",
            "heartbeat": dependabot_updated or None,
            "last_successful_submission": dependabot_last,
            "reason_code": "DEPENDABOT_DIAGNOSTIC" if dependabot_heartbeat and dependabot_heartbeat.get("recent_error") else None,
        },
    }


def _dashboard_relay_component(*, server_running: bool) -> dict[str, object]:
    """Project the Relay only when its lifecycle and Tailnet route are live.

    The relay is an optional access adapter, but it is still a logical
    Platform Component.  A running EP Server cannot stand in for a missing or
    repeatedly exiting LaunchAgent.
    """
    definition = PLATFORM_COMPONENT_BY_ID["dashboard_relay"]
    label = definition.lifecycle_label
    try:
        lifecycle = LaunchdProvider()
        runtime = lifecycle.runtime_status(label) if label else None
        observed = lifecycle.runtime_details(label) if label else None
        observed = observed if isinstance(observed, LaunchdRuntimeDetails) else None
        relay_running = bool(runtime and runtime.qualified)
        detail = runtime.detail if runtime is not None else "lifecycle owner unavailable"
    except OSError:
        observed, relay_running, detail = None, False, "lifecycle owner unavailable"
    access = server_relay.relay_access_observation(probe=server_running and relay_running)
    relay_reachable = access["relay_reachable"] is True
    healthy = server_running and relay_running and relay_reachable
    if healthy:
        detail_code = "DASHBOARD_RELAY_TAILSCALE_AVAILABLE"
    elif observed is not None and not observed.loaded:
        detail_code = "DASHBOARD_RELAY_LAUNCH_AGENT_UNLOADED"
    elif observed is not None and not observed.active:
        detail_code = "DASHBOARD_RELAY_PROCESS_INACTIVE"
    elif access["tailscale_ipv4"] is None:
        detail_code = "DASHBOARD_RELAY_TAILSCALE_UNAVAILABLE"
        detail = "Tailscale address unavailable"
    elif not relay_reachable:
        detail_code = "DASHBOARD_RELAY_ENDPOINT_UNREACHABLE"
        detail = "Relay endpoint unreachable"
    else:
        detail_code = "DASHBOARD_RELAY_LIFECYCLE_UNAVAILABLE"
    return {
        "healthy": healthy,
        "status_code": definition.active_status if healthy else definition.inactive_status,
        "detail_code": detail_code,
        "lifecycle_label": label,
        "lifecycle_state": "RUNNING" if relay_running else "STOPPED",
        # A Relay has its own LaunchAgent process.  Never substitute a
        # placeholder (notably zero) for the observed process lifetime.
        "uptime_seconds": observed.uptime_seconds if observed is not None else None,
        "recent_error": None if healthy else detail,
        **access,
    }


def _launch_agent_configuration(plist_path: Path) -> dict[str, object]:
    """Project the safe start policy from one EP-owned LaunchAgent plist."""
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return {}
    if not isinstance(payload, dict):
        return {}
    configuration: dict[str, object] = {}
    run_at_load = payload.get("RunAtLoad")
    if isinstance(run_at_load, bool):
        configuration["run_at_load"] = run_at_load
    keep_alive = payload.get("KeepAlive")
    if isinstance(keep_alive, (bool, dict)):
        configuration["keep_alive"] = bool(keep_alive)
    return configuration


def _platform_component_detail(data_root: Path, component_id: str) -> dict[str, object] | None:
    """Expose one secret-free detail view from the same platform projection."""
    component = status(data_root)["components"].get(component_id)  # type: ignore[index]
    if not isinstance(component, dict):
        return None
    definition = PLATFORM_COMPONENT_BY_ID[component_id]
    service_paths = server_service.default_paths(data_root)
    runtime_executable = Path(sys.executable).expanduser().absolute()
    installation: dict[str, str] = {}
    if component_id == "ep_server":
        installation = {
            "runtime_path": str(runtime_executable.parent.parent),
            "central_data_path": str(data_root.resolve()),
            "launch_agent_path": str(service_paths.plist_path),
            "error_log_path": str(service_paths.stderr_log),
        }
    elif component_id == "platform_database":
        database_path = data_root / SERVER_DATABASE_FILENAME
        installation = {
            "central_data_path": str(data_root.resolve()),
            "database_path": str(database_path.resolve()),
        }
    elif component_id == "dashboard_relay":
        installation = {
            "launch_agent_path": str(server_relay.launch_agent_path()),
            "relay_binary_path": str(server_relay.relay_binary(data_root)),
        }
    lifecycle_label = server_service.LABEL if component_id == "ep_server" else definition.lifecycle_label
    observed: LaunchdRuntimeDetails | None = None
    if lifecycle_label:
        candidate = LaunchdProvider().runtime_details(lifecycle_label)
        observed = candidate if isinstance(candidate, LaunchdRuntimeDetails) else None
    detail: dict[str, object] = {
        "component": component_id,
        "machine": os.uname().nodename,
        "restart_supported": definition.restart_supported,
        "installation": installation,
        **component,
    }
    if component_id == "http_ingress":
        detail["swagger_endpoint"] = HTTP_JSON_OPENAPI_PATH
    if observed is not None:
        detail["launchd"] = {
            "label": observed.label,
            "loaded": observed.loaded,
            "active": observed.active,
            "pid": observed.pid,
            "last_exit_code": observed.last_exit_code,
            **(
                _launch_agent_configuration(Path(installation["launch_agent_path"]))
                if isinstance(installation.get("launch_agent_path"), str) else {}
            ),
        }
        detail["process_state"] = "OWNED_PROCESS"
        detail["processes"] = ([{
            "pid": observed.pid,
            "memory_kib": observed.memory_kib,
        }] if observed.active and observed.pid is not None else [])
        if observed.uptime_seconds is not None:
            detail["uptime_seconds"] = observed.uptime_seconds
    elif component_id == "platform_database":
        detail["launchd"] = {}
        detail["process_state"] = "STORAGE"
        try:
            detail["database_size_bytes"] = (data_root / SERVER_DATABASE_FILENAME).stat().st_size
        except OSError:
            pass
    else:
        host = LaunchdProvider().runtime_details(server_service.LABEL)
        host = host if isinstance(host, LaunchdRuntimeDetails) else None
        detail["launchd"] = {}
        detail["process_state"] = "IN_PROCESS"
        detail["process_host"] = {
            "component": "ep_server",
            # In-process components always run in this Server process.  A
            # manually started qualification server has no LaunchAgent to
            # inspect, but its current PID is still authoritative.
            "pid": host.pid if host is not None and host.active else os.getpid(),
            "uptime_seconds": host.uptime_seconds if host is not None and host.active else None,
        }
    return detail


def _restart_platform_component(data_root: Path, component_id: str) -> dict[str, object]:
    """Restart one explicitly restartable Platform component through its owner.

    Component identifiers and lifecycle labels come exclusively from the
    canonical model.  This deliberately does not expose a generic process or
    LaunchAgent control endpoint.
    """
    definition = PLATFORM_COMPONENT_BY_ID.get(component_id)
    if definition is None or not definition.restart_supported or not definition.lifecycle_label:
        raise ValueError("COMPONENT_RESTART_NOT_SUPPORTED")
    logger = component_logger(
        data_root,
        definition.id,
        central_database=data_root / SERVER_DATABASE_FILENAME,
    )
    log_event(
        logger,
        logging.INFO,
        "component_restart_requested",
        context={"target_component": definition.id},
    )
    lifecycle = LaunchdProvider()
    try:
        lifecycle.restart(definition.lifecycle_label)
        # ``kickstart`` only acknowledges the request.  Give launchd a small,
        # bounded interval to spawn the owned process before deciding whether
        # the repair actually reached its observable postcondition.
        deadline = time.monotonic() + 2
        postcondition = lifecycle.runtime_status(definition.lifecycle_label)
        while not postcondition.qualified and time.monotonic() < deadline:
            time.sleep(.1)
            postcondition = lifecycle.runtime_status(definition.lifecycle_label)
        if not postcondition.qualified:
            raise OSError("COMPONENT_RESTART_POSTCONDITION_FAILED")
    except OSError as error:
        log_event(
            logger,
            logging.WARNING,
            "component_restart_failed",
            diagnostic=str(error),
            context={"target_component": definition.id},
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "component_restart_completed",
        context={
            "target_component": definition.id,
            "postcondition": "LIFECYCLE_OWNER_RUNNING",
        },
    )
    return {
        "restarting": definition.id,
        "scope": "PLATFORM",
        "postcondition": "LIFECYCLE_OWNER_RUNNING",
    }


DASHBOARD_AUDIT_ACTOR = "DASHBOARD_USER"


def _operations_console_logger(data_root: Path) -> logging.Logger:
    """Return the one CENTRAL-backed logger for dashboard audit events."""
    return component_logger(
        data_root,
        "operations_console",
        central_database=data_root / SERVER_DATABASE_FILENAME,
    )


def _record_platform_component_startups(data_root: Path) -> None:
    """Write the one Server-owned startup fact for every canonical component."""
    for definition in PLATFORM_COMPONENTS:
        context: dict[str, object] = {
            "target_component": definition.id,
            # ``component_version`` is always the installed EP application.
            # Storage revisions are separate provenance and must never make
            # the database component appear to run an obsolete application.
            "component_version": _console_platform_version(),
        }
        if definition.id == "platform_database":
            context["schema_version"] = SERVER_STORE_SCHEMA_VERSION
        log_event(
            component_logger(
                data_root, definition.id,
                central_database=data_root / SERVER_DATABASE_FILENAME,
            ),
            logging.INFO,
            definition.startup_event,
            context=context,
        )


def _audit_configuration_change(
    data_root: Path,
    *,
    scope: str,
    key: str,
    previous: object,
    value: object,
) -> None:
    """Persist a bounded CENTRAL audit event for a successful setting change."""
    log_event(
        _operations_console_logger(data_root),
        logging.INFO,
        "configuration_changed",
        context={
            "audit_action": "configuration_changed",
            "audit_actor": DASHBOARD_AUDIT_ACTOR,
            "audit_outcome": "COMPLETED",
            "user_action": "configuration_changed",
            "configuration_scope": scope,
            "configuration_key": key,
            "previous_value": previous,
            "new_value": value,
        },
    )


def _audit_platform_data_action(
    data_root: Path,
    *,
    action: str,
    outcome: str,
    details: Mapping[str, object] | None = None,
) -> None:
    """Persist one secret-free, dashboard-initiated platform-data audit fact."""
    context: dict[str, object] = {
        "audit_action": action,
        "audit_actor": DASHBOARD_AUDIT_ACTOR,
        "audit_outcome": outcome,
        "user_action": f"platform_data_{action.lower()}",
    }
    if details:
        context.update(details)
    failed = outcome == "FAILED"
    log_event(
        _operations_console_logger(data_root),
        logging.WARNING if failed else logging.INFO,
        f"platform_data_{action.lower()}_failed" if failed else f"platform_data_{action.lower()}",
        context=context,
    )


def _audit_dashboard_action(
    data_root: Path,
    *,
    action: str,
    project_id: str | None = None,
    run_id: str | None = None,
    details: Mapping[str, object] | None = None,
    outcome: str = "COMPLETED",
) -> None:
    """Record one bounded Console action in the CENTRAL audit log.

    The request body, chat text, queue reason, artifact bytes and local paths
    are intentionally not audit metadata.  The action identity and its scoped
    target are sufficient to reconstruct what an operator asked EP to do.  A
    failed action is an operational warning rather than an informational
    completion, so filtering Console logs by ``WARNING`` retains the event.
    """
    if outcome not in {"COMPLETED", "FAILED"}:
        raise ValueError("INVALID_DASHBOARD_AUDIT_OUTCOME")
    context: dict[str, object] = {
        "audit_action": action,
        "audit_actor": DASHBOARD_AUDIT_ACTOR,
        "audit_outcome": outcome,
        "user_action": action,
    }
    if project_id is not None:
        context["project_id"] = project_id
    if details:
        context.update(details)
    log_event(
        _operations_console_logger(data_root),
        logging.WARNING if outcome == "FAILED" else logging.INFO,
        "dashboard_action_failed" if outcome == "FAILED" else "dashboard_action_completed",
        run_id=run_id,
        context=context,
    )


def _audit_dashboard_action_rejected(
    data_root: Path,
    *,
    action: str,
    diagnostic_code: str,
    project_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Persist a safe failure fact for a Console action button.

    A rejected click is still operationally relevant: it explains why the UI
    did not advance.  Only the stable, public error code is retained; request
    bodies and exception strings may contain user or provider material.
    """
    _audit_dashboard_action(
        data_root,
        action=action,
        project_id=project_id,
        run_id=run_id,
        details={"diagnostic_code": diagnostic_code},
        outcome="FAILED",
    )


def _retired_console_action_contract(path: str) -> tuple[str, str]:
    """Return the stable audit action and public error for a retired route.

    The installed Console used to retain controls from the checkout dashboard.
    A stale page must get an explicit, auditable response rather than a
    project-selection error or a compatibility mutation.  Route paths are not
    logged: they are implementation detail and needlessly expand the audit
    payload.
    """
    exact = {
        "/api/codex-cli-update": ("codex_cli_updated", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/rate-limit-reset": ("rate_limit_reset_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/telemetry/clear": ("telemetry_cleared", "TELEMETRY_CLEAR_RETIRED"),
        "/api/queue-defer": ("queue_deferred", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/status-reconciliation-preview": ("execution_status_reconciled", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/status-reconciliation": ("execution_status_reconciled", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/execution-emergency-rollback": ("execution_emergency_rollback_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/execution-merge-wait-abort": ("execution_merge_wait_aborted", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/execution-merge-status-check": ("execution_merge_status_checked", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/managed-branch-recovery": ("managed_branch_recovery_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/managed-branch-synchronization": ("managed_branch_synchronization_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/stale-git-lock-recovery": ("stale_git_lock_recovery_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/workspace-switch-to-main": ("workspace_switch_to_main_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/workspace-switch-to-worktree": ("workspace_switch_to_worktree_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"),
        "/api/runtime-directory/open": ("runtime_directory_opened", "RUNTIME_DIRECTORY_RETIRED"),
        "/api/central-database/download": ("central_database_downloaded", "CENTRAL_DATABASE_DOWNLOAD_RETIRED"),
    }
    if path in exact:
        return exact[path]
    if re.fullmatch(r"/api/configuration/file-inbox/relocate(?:/browse)?", path):
        return "inbox_location_changed", "FILE_INBOX_PARTIAL_RELOCATION_RETIRED"
    if re.fullmatch(r"/api/configuration/inbox-location(?:/browse)?", path):
        return "inbox_location_changed", "INBOX_WATCHER_CONFIGURATION_RETIRED"
    if re.fullmatch(r"/api/logs/(?:inbox|dashboard)", path):
        return "component_logs_cleared", "LEGACY_COMPONENT_LOG_ROUTE_RETIRED"
    if re.fullmatch(rf"/api/components/{RETIRED_COMPONENT_ALIAS_ROUTE_PATTERN}/(?:details|restart)", path):
        return "component_restart_requested", "LEGACY_COMPONENT_AUTHORITY_RETIRED"
    if re.fullmatch(r"/api/open-pull-requests(?:/[0-9]+/(?:owner-authorization|repair-failed-checks))?", path):
        return "pull_request_action_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"
    return "legacy_dashboard_action_requested", "LEGACY_DASHBOARD_ACTION_RETIRED"


def status(data_root: Path) -> dict[str, object]:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    try:
        runtime_profile = development_profile.describe(data_root)
    except development_profile.DevelopmentProfileError as error:
        raise ServerConfigurationError("EP Server development profile is invalid.") from error
    runtime = _runtime(data_root)
    running = bool(runtime and _alive(runtime.get("pid")))
    components = _transport_components(data_root, server_running=running)
    components["dashboard_relay"] = _dashboard_relay_component(server_running=running)
    platform_version = _console_platform_version()
    # One Server-native inventory feeds Components, the titlebar popout and
    # detail modals. It deliberately contains no watcher/check-out model.
    for definition in PLATFORM_COMPONENTS:
        if definition.id in components:
            components[definition.id].update({
                "kind": definition.kind, "name_key": definition.name_key,
                "group": definition.group, "critical": definition.critical, "restart_supported": definition.restart_supported,
                "log_component": definition.id,
            })
            components[definition.id].setdefault("version", platform_version)
            continue
        healthy = True if definition.id == "platform_database" else running
        components[definition.id] = {
            "kind": definition.kind, "name_key": definition.name_key,
            "group": definition.group, "critical": definition.critical, "restart_supported": definition.restart_supported,
            "log_component": definition.id, "healthy": healthy,
            "status_code": definition.active_status if healthy else definition.inactive_status,
            "detail_code": definition.detail_code,
            "version": str(SERVER_STORE_SCHEMA_VERSION) if definition.id == "platform_database" else platform_version,
        }
    server_component = components["ep_server"]
    server_component["version"] = platform_version
    started_at = runtime.get("started_at") if runtime else None
    if isinstance(started_at, str):
        try:
            server_component["uptime_seconds"] = max(
                0, int((datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
            )
        except ValueError:
            pass
    # Every canonical component is observed and included in the response.
    # The aggregate follows the component model's explicit criticality: an
    # optional access or producer adapter remains diagnosable without making
    # a core Server probe unavailable on a host that does not install it.
    unhealthy_components = [
        item.id for item in PLATFORM_COMPONENTS
        if not bool(components[item.id].get("healthy"))
    ]
    healthy = all(
        bool(components[item.id].get("healthy"))
        for item in PLATFORM_COMPONENTS
        if item.critical
    )
    live_runtime = operational_installation.live_runtime_identity(
        package=Path(__file__).resolve().parent,
        product_version=platform_version,
    )
    return {
        "service": "engineering-platform-server",
        "healthy": healthy,
        "health": "ok" if healthy else "degraded",
        "unhealthy_components": unhealthy_components,
        "instance_id": identity.instance_id,
        "product_version": platform_version,
        # This identity is generated by the process serving this response.
        # It is not copied from the operational-installation record, which
        # would turn a desired installation claim into a live observation.
        "runtime_identity": live_runtime,
        "store": "ready",
        "schema_version": SERVER_STORE_SCHEMA_VERSION,
        "operational_state": "empty-valid",
        "runtime_profile": runtime_profile,
        "running": running,
        "managed_codex_runtime": managed_codex_runtime.inspect(data_root),
        "lifecycle_worker": {
            # The worker is hosted by the sole installed Server process.  A
            # stopped process is never reported as an active worker.
            "state": "RUNNING" if running else "STOPPED",
        },
        "bind": {"host": config.bind_host, "port": config.bind_port},
        "components": components,
        "component_model": [
            {"id": item.id, "name_key": item.name_key, "kind": item.kind, "group": item.group,
             "critical": item.critical, "restart_supported": item.restart_supported,
             "lifecycle_label": item.lifecycle_label, "log_component": item.id}
            for item in PLATFORM_COMPONENTS
        ],
    }


def operations_projection(data_root: Path) -> dict[str, object]:
    """Return the installed CENTRAL's secret-free Console projection.

    The selected project remains a browser presentation preference.  This
    endpoint intentionally returns topology only; no checkout path, Agent
    credential, or execution capability is exposed here.
    """
    identity = initialize(data_root)
    with storage.sqlite_connection(f"file:{data_root / SERVER_DATABASE_FILENAME}?mode=ro", uri=True) as connection:
        topology = project_topology.topology(connection)
    return {
        "installation_id": identity.instance_id,
        "schema_version": SERVER_STORE_SCHEMA_VERSION,
        "managed_codex_runtime": managed_codex_runtime.inspect(data_root),
        "projects": topology["projects"],
    }


def _console_projects(data_root: Path) -> list[dict[str, str]]:
    """List CENTRAL project identities without opening their checkouts.

    The selector is a logical CENTRAL projection.  A local binding is checked
    only later, when a user explicitly selects that project for a transitional
    root-bound route.
    """
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        rows = connection.execute("""SELECT p.project_id, r.repository_id
            FROM ep_project_registrations AS p
            JOIN ep_repository_registrations AS r
              ON r.project_id=p.project_id AND r.role='authority'
            WHERE p.status='ACTIVE'
            ORDER BY p.project_id""").fetchall()
    return [{"project_id": str(project_id), "repository_id": str(repository_id)}
            for project_id, repository_id in rows]


def _console_queue_projection(data_root: Path, project_id: str) -> dict[str, object]:
    """Read the selected project's single transport-neutral CENTRAL FIFO."""
    for project in _console_projects(data_root):
        if project["project_id"] != project_id:
            continue
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            context = project_context(
                connection,
                data_root=data_root,
                project_id=project_id,
                repository_id=project["repository_id"],
                require_local_root=False,
            )
            queue = ParityProjectStore(connection, context).console_queue_projection()
            rows = connection.execute(
                """SELECT run_id,operator_resolution FROM ep_parity_lifecycle_dispatches
                    WHERE project_id=? AND operator_resolution IN ('DISMISSED','RETRIED')""",
                (project_id,),
            ).fetchall()
            return {**queue, "operator_handling": {str(run_id): str(resolution) for run_id, resolution in rows}}
    raise local_repository_binding.LocalRepositoryBindingError("CONSOLE_PROJECT_UNAVAILABLE")


def _console_platform_version() -> str:
    """Read the installed platform version once for CENTRAL Console snapshots."""
    return EngineeringPlatformManifest.load(
        package_path("ENGINEERING_PLATFORM_VERSION.json")
    ).platform_version


def _central_json_object(value: object) -> dict[str, object]:
    """Decode one stored CENTRAL JSON object without exposing its raw form."""
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _central_text(value: object) -> str | None:
    """Return a bounded scalar from a persisted producer record."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:512] if normalized else None


def _central_forge_action_context(value: object, action_id: str | None) -> dict[str, str] | None:
    """Return only a verified, safe Action summary from CENTRAL storage."""
    document = _central_json_object(value)
    generator = document.get("generator")
    expected = {
        "envelope_version", "action_id", "summary", "summary_digest", "envelope_digest", "generator",
    }
    if (set(document) != expected or not isinstance(generator, dict)
            or set(generator) != {"id", "model", "version", "source_digest"}
            or document.get("action_id") != action_id):
        return None
    try:
        request = submission_service.SubmissionRequest(
            project_id="projection", repository_id="projection", producer_id="forge",
            producer_type="FORGE", producer_version="projection", prompt="projection",
            transport="HTTP", engineering_action_id=action_id,
            constraints={"forge_execution": {
                "contract_version": "1.2", "host_id": "projection", "repository_id": "projection",
                "correlation_id": "projection", "mission_id": "projection", "mission_revision": "1",
                "intent_id": "projection", "intent_revision": "1", "action_id": action_id,
                "runtime_prompt": {"id": "projection", "content_digest": "sha256:" + "0" * 64},
                "retry_of_correlation_id": None, "producer_contract_version": "1.0",
                "forge_application_version": "projection", "action_context_envelope": document,
            }},
        )
        envelope = submission_service._forge_action_context(request)
    except (submission_service.SubmissionError, TypeError, ValueError):
        return None
    if envelope is None:
        return None
    safe_generator = envelope["generator"]
    assert isinstance(safe_generator, dict)
    return {
        "summary": str(envelope["summary"]),
        "summary_digest": str(envelope["summary_digest"]),
        "envelope_digest": str(envelope["envelope_digest"]),
        "generator": "{id} · {model} · {version}".format(
            id=safe_generator["id"], model=safe_generator["model"], version=safe_generator["version"],
        ),
    }


def _central_forge_planning_context(
    value: object, *, mission_id: str | None, action_id: str | None,
) -> dict[str, object] | None:
    """Return a verified, redacted v1.3 Forge planning envelope only.

    The stored document is a versioned producer contract, not free-form UI
    data.  Recheck its complete shape and digest at the presentation boundary
    so a malformed retained row cannot become apparent run context.
    """
    document = _central_json_object(value)
    expected = {
        "envelope_version", "mission_id", "mission_revision", "intent_id", "intent_revision",
        "action_id", "mission_title", "business_summary", "engineering_summary", "mission_lifecycle",
        "decision_evidence_reference", "decision_evidence_reference_digest", "envelope_digest",
    }
    if set(document) != expected or document.get("envelope_version") != "1.0":
        return None
    if document.get("mission_id") != mission_id or document.get("action_id") != action_id:
        return None
    digest = document.get("envelope_digest")
    unsigned = {key: value for key, value in document.items() if key != "envelope_digest"}
    if not isinstance(digest, str) or digest != submission_service._action_context_digest(unsigned):
        return None
    identifiers = ("mission_id", "mission_revision", "intent_id", "intent_revision", "action_id")
    text_fields = ("mission_title", "business_summary", "engineering_summary", "mission_lifecycle")
    if any(_central_text(document.get(key)) is None for key in identifiers):
        return None
    if any(document.get(key) is not None and _central_text(document.get(key)) is None for key in text_fields):
        return None
    reference = document.get("decision_evidence_reference")
    reference_digest = document.get("decision_evidence_reference_digest")
    if reference is not None and _central_text(reference) is None:
        return None
    if reference_digest is not None and (
        not isinstance(reference_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", reference_digest) is None
    ):
        return None
    return {key: document[key] for key in expected}


def _central_execution_host_projection(
    start_value: object, terminal_value: object,
) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
    """Project immutable host counters, never a workspace path or live Git state."""
    start = _central_json_object(start_value)
    terminal = _central_json_object(terminal_value)
    if start.get("status") != "AVAILABLE" or terminal.get("status") != "AVAILABLE":
        return {}, {}, None
    branch = _central_text(start.get("target_branch"))
    tracked = start.get("tracked_file_count")
    diff = terminal.get("diff")
    activity = terminal.get("activity")
    if (
        not isinstance(tracked, int) or isinstance(tracked, bool) or tracked < 0
        or not isinstance(diff, dict) or not isinstance(activity, dict)
    ):
        return {}, {}, None
    numeric_diff = {
        key: diff.get(key) for key in ("modified", "created", "deleted", "renamed")
    }
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in numeric_diff.values()):
        return {}, {}, None
    provider = activity.get("provider_invocations")
    validation = activity.get("host_validation_actions")
    if (
        not isinstance(provider, int) or isinstance(provider, bool) or provider < 0
        or not isinstance(validation, int) or isinstance(validation, bool) or validation < 0
    ):
        return {}, {}, None
    metadata = {
        "modified": numeric_diff["modified"], "created": numeric_diff["created"],
        "deleted": numeric_diff["deleted"], "renamed": numeric_diff["renamed"],
        # This is intentionally a provider-invocation count, not an inferred
        # shell-command count.  Its definition is displayed with the metric.
        "codex_commands_executed": provider,
    }
    summary = {
        "activity": {
            "metric_definition_version": 1,
            "codex_command_definition": (
                "One persisted Codex CLI provider invocation. It is not a shell command, "
                "tool call, prompt, token count, or GitHub API request."
            ),
            "provider_invocations_total": provider,
            "primary_codex_commands_total": provider,
            "host_validation_commands_total": validation,
            "overall_activity_total": provider + validation,
        },
        "terminal_delivery_diff": {
            "authority": "IMMUTABLE_EXECUTION_HOST_EVIDENCE",
            "transaction_baseline_sha": _central_text(start.get("target_commit")),
            "terminal_target_sha": None,
            # Counts are authoritative but individual paths are intentionally
            # not retained in the CENTRAL presentation contract.
            "total_unique_changed_paths": None,
            "renamed": None,
            "per_pr_changed_file_counts": "NOT_RECORDED_BY_EXECUTION_HOST_EVIDENCE_CONTRACT",
        },
    }
    return (
        {"target_branch": branch, "tracked_file_count": tracked},
        metadata,
        summary,
    )


def _central_persisted_activity_summary(value: object, run_id: str) -> dict[str, object] | None:
    """Return the safe, immutable activity summary recorded for one run.

    Host evidence is a compatibility fallback only.  It cannot distinguish
    primary provider calls from assurance reviews and it does not carry the
    terminal delivery proof.  The versioned activity record is therefore the
    presentation authority whenever it is available.
    """
    document = _central_json_object(value)
    activity = document.get("activity")
    delivery = document.get("terminal_delivery_diff")
    required_counts = (
        "primary_codex_commands_total", "reviewer_codex_commands_total",
        "host_validation_commands_total", "overall_activity_total",
    )
    if (
        document.get("run_id") != run_id
        or not isinstance(document.get("summary_version"), int)
        or isinstance(document.get("summary_version"), bool)
        or not isinstance(activity, Mapping)
        or not isinstance(delivery, Mapping)
        or any(
            not isinstance(activity.get(key), int)
            or isinstance(activity.get(key), bool)
            or activity[key] < 0
            for key in required_counts
        )
    ):
        return None
    definition = _central_text(activity.get("codex_command_definition"))
    if definition is None:
        return None
    def revision(key: str) -> str | None:
        candidate = delivery.get(key)
        return candidate if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{40}", candidate) else None

    renamed = delivery.get("renamed")
    if not isinstance(renamed, list):
        renamed = None
    changed = delivery.get("total_unique_changed_paths")
    if not isinstance(changed, int) or isinstance(changed, bool) or changed < 0:
        changed = None
    return {
        "summary_version": document["summary_version"],
        "activity": {
            "metric_definition_version": activity.get("metric_definition_version", 1),
            "codex_command_definition": definition,
            **{key: activity[key] for key in required_counts},
        },
        "terminal_delivery_diff": {
            "authority": _central_text(delivery.get("authority")),
            "transaction_baseline_sha": revision("transaction_baseline_sha"),
            "terminal_target_sha": revision("terminal_target_sha"),
            "total_unique_changed_paths": changed,
            # The renderer intentionally presents only the count, never paths.
            "renamed": renamed,
            "per_pr_changed_file_counts": _central_text(delivery.get("per_pr_changed_file_counts")),
        },
    }


@dataclass(frozen=True)
class _CentralForgeProvenance:
    """The safe, displayable subset of one admitted Forge provenance record."""

    contract_version: str | None
    host_id: str | None
    repository_id: str | None
    correlation_id: str | None
    mission_id: str | None
    mission_revision: str | None
    intent_id: str | None
    intent_revision: str | None
    action_id: str | None
    runtime_prompt_id: str | None
    runtime_prompt_digest: str | None
    retry_of_correlation_id: str | None
    producer_contract_version: str | None
    forge_application_version: str | None

    @classmethod
    def from_constraints(cls, value: object) -> _CentralForgeProvenance | None:
        """Project only known Forge keys; raw constraints never reach Console."""
        raw = _central_json_object(value).get("forge_execution")
        if not isinstance(raw, dict):
            return None
        runtime_prompt = raw.get("runtime_prompt")
        prompt = runtime_prompt if isinstance(runtime_prompt, dict) else {}
        return cls(
            contract_version=_central_text(raw.get("contract_version")),
            host_id=_central_text(raw.get("host_id")),
            repository_id=_central_text(raw.get("repository_id")),
            correlation_id=_central_text(raw.get("correlation_id")),
            mission_id=_central_text(raw.get("mission_id")),
            mission_revision=_central_text(raw.get("mission_revision")),
            intent_id=_central_text(raw.get("intent_id")),
            intent_revision=_central_text(raw.get("intent_revision")),
            action_id=_central_text(raw.get("action_id")),
            runtime_prompt_id=_central_text(prompt.get("id")),
            runtime_prompt_digest=_central_text(prompt.get("content_digest")),
            retry_of_correlation_id=_central_text(raw.get("retry_of_correlation_id")),
            producer_contract_version=_central_text(raw.get("producer_contract_version")),
            forge_application_version=_central_text(raw.get("forge_application_version")),
        )

    def execution_context(
        self,
        *,
        mission_id: str | None,
        action_id: str | None,
        execution_phase: str | None,
        dispatch_state: str | None,
        updated_at: str | None,
        transport_receipt_id: str | None,
        action_context: Mapping[str, str] | None = None,
        planning_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Build the explicit CENTRAL projection of admitted Forge facts."""
        context: dict[str, object] = {
            "context_version": self.contract_version,
            "mission_id": mission_id,
            "current_intent": self.intent_id,
            "current_engineering_action": action_id,
            # The run state is owned by the Execution Host.  The separately
            # persisted dispatcher state describes its FIFO orchestration and
            # may legitimately differ while a run is being finalized.
            "execution_phase": execution_phase,
            "last_runtime_update": updated_at,
            "dispatcher_state": dispatch_state,
            "execution_receipt_reference": transport_receipt_id,
            "producer_host_id": self.host_id,
            "mission_revision": self.mission_revision,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "runtime_prompt_id": self.runtime_prompt_id,
            "runtime_prompt_digest": self.runtime_prompt_digest,
            "retry_of_correlation_id": self.retry_of_correlation_id,
        }
        if action_context is not None:
            context.update({
                "engineering_summary": action_context["summary"],
                "action_summary_status": "AVAILABLE",
                "action_summary_generator": action_context["generator"],
                "action_summary_digest": action_context["summary_digest"],
                "action_context_envelope_digest": action_context["envelope_digest"],
            })
        elif self.contract_version in {"1.0", "1.1"}:
            # Those submissions predate the dedicated envelope. Do not infer
            # a summary from their retained prompt or from current Forge state.
            context["action_summary_status"] = "NOT_AVAILABLE_HISTORICAL"
        if planning_context is not None:
            context.update({
                "mission_title": planning_context.get("mission_title"),
                "business_summary": planning_context.get("business_summary"),
                "planning_engineering_summary": planning_context.get("engineering_summary"),
                "mission_lifecycle": planning_context.get("mission_lifecycle"),
                "decision_evidence_reference": planning_context.get("decision_evidence_reference"),
                "decision_evidence_reference_digest": planning_context.get("decision_evidence_reference_digest"),
                "planning_context_envelope_digest": planning_context.get("envelope_digest"),
            })
        # v1.0 provenance remains an exact historical Console projection.  The
        # v1.1 attributes are present only when an admitted Forge envelope
        # actually supplied them; absence is never represented as invented
        # or misleading version data.
        if self.producer_contract_version is not None:
            context["producer_contract_version"] = self.producer_contract_version
        if self.forge_application_version is not None:
            context["forge_application_version"] = self.forge_application_version
        return context


def _central_run_record(row: sqlite3.Row, project_id: str) -> dict[str, object]:
    """Project one run and its immutable admitted Producer facts for Console.

    ``ep_submissions`` is the canonical input record.  In particular, it is
    available even if host preparation stopped before the legacy-compatible
    workspace evidence could be persisted.  Do not return the raw constraints
    document: it can contain producer prompt metadata and is not a Console
    presentation contract.
    """
    forge = _CentralForgeProvenance.from_constraints(row["constraints"])
    submission_id = _central_text(row["submission_id"])
    mission_id = _central_text(row["mission_id"]) or (forge.mission_id if forge else None)
    action_id = _central_text(row["engineering_action_id"]) or (forge.action_id if forge else None)
    correlation_id = _central_text(row["correlation_id"]) or (forge.correlation_id if forge else None)
    execution_phase = _central_text(row["run_state"])
    dispatch_state = _central_text(row["dispatch_state"]) or execution_phase
    updated_at = _central_text(row["updated_at"])
    action_context = _central_forge_action_context(row["action_context_document"], action_id)
    planning_context = _central_forge_planning_context(
        row["planning_context_document"], mission_id=mission_id, action_id=action_id,
    )
    host_fields, execution_metadata, activity_summary = _central_execution_host_projection(
        row["host_start_document"], row["host_terminal_document"],
    )
    activity_summary = _central_persisted_activity_summary(
        row["activity_summary_document"], str(row["run_id"]),
    ) or activity_summary
    operator_resolution = _central_text(row["operator_resolution"]) or "NONE"
    awaiting_operator = (
        dispatch_state in {"BLOCKED", "FAILED"}
        and operator_resolution == "OPEN"
    )
    retry_child_run_id = _central_text(row["retry_child_run_id"])
    retry_dispatch_state = _central_text(row["retry_dispatch_state"])
    retry_status = (
        "ACTIVE" if retry_dispatch_state in {"CLAIMED", "RUNNING"}
        else retry_dispatch_state
    )
    execution_context = forge.execution_context(
        mission_id=mission_id,
        action_id=action_id,
        execution_phase=execution_phase,
        dispatch_state=dispatch_state,
        updated_at=updated_at,
        transport_receipt_id=_central_text(row["transport_receipt_id"]),
        action_context=action_context,
        planning_context=planning_context,
    ) if forge else None

    return {
        "run_id": str(row["run_id"]),
        "status": dispatch_state,
        "state": dispatch_state,
        # A submission identifier is not an execution title.  New Forge v1.3
        # context may contribute a redacted Mission title; otherwise the UI
        # presents the Run-ID as the sole reliable identity.
        "title": _central_text(planning_context.get("mission_title")) if planning_context else str(row["run_id"]),
        "executed_at": updated_at,
        "created_at": _central_text(row["created_at"]),
        "updated_at": updated_at,
        "execution_mode": _central_text(row["execution_mode"]),
        "project_id": project_id,
        "submission_id": submission_id,
        "producer_id": _central_text(row["producer_id"]),
        "producer_type": _central_text(row["producer_type"]),
        "producer_version": _central_text(row["producer_version"]),
        # The CENTRAL parity adapter uses the published Producer Submission
        # contract.  It has a fixed version and is separate from Forge's
        # immutable provenance-contract version below.
        "producer_submission_contract_version": (
            forge.producer_contract_version if forge and forge.producer_contract_version else "1.0" if submission_id else None
        ),
        "execution_context_version": forge.contract_version if forge else None,
        "mission_id": mission_id,
        "engineering_action_id": action_id,
        "correlation_id": correlation_id,
        "target_repository": _central_text(row["repository_id"]) or (forge.repository_id if forge else None),
        # A checkout/branch/file count describes host evidence for this run;
        # never infer it from a current local binding or current Git state.
        "target_branch": host_fields.get("target_branch"),
        "target_checkout_path": None,
        "tracked_file_count": host_fields.get("tracked_file_count"),
        "execution_metadata": execution_metadata,
        "execution_context": execution_context,
        "execution_activity_summary": activity_summary,
        "report_available": bool(row["report_available"]),
        "analysis_available": bool(row["analysis_available"]),
        # Historical operator controls are capabilities, not client-side
        # guesses based on a terminal outcome.  CENTRAL persists the mutable
        # handling decision separately from the immutable outcome, so a
        # refresh after a successful action cannot offer that action again.
        "history_source": "CENTRAL",
        "can_retry": awaiting_operator,
        "can_dismiss": awaiting_operator,
        "retry_child_run_id": retry_child_run_id,
        "retry_status": retry_status,
        "retry_timestamp": _central_text(row["retry_updated_at"]),
        "retry_pending": (
            operator_resolution == "RETRIED"
            and _central_text(row["resolution_submission_id"]) is not None
            and retry_child_run_id is None
        ),
        # A dismissal is mutable operator-handling metadata, deliberately
        # separate from the immutable BLOCKED/FAILED outcome.  The history
        # table needs it as well as the queue projection: otherwise a
        # successful dismissal is persisted but the stale action remains
        # visible and a second click correctly (but confusingly) fails.
        "operator_resolution": operator_resolution,
        "handling_state": operator_resolution,
        "dismissed": operator_resolution == "DISMISSED",
        "dismissed_at": updated_at if operator_resolution == "DISMISSED" else None,
    }


def _central_console_run_records(
    data_root: Path, project_id: str, *, _read_connection: sqlite3.Connection | None = None,
    record_limit: int | None = 1000,
) -> list[dict[str, object]]:
    """Read project runs with bounded pages from one consistent source view."""
    owns_connection = _read_connection is None
    connection = _read_connection or sqlite3.connect(data_root / SERVER_DATABASE_FILENAME)
    try:
        connection.row_factory = sqlite3.Row
        if owns_connection and record_limit is None:
            connection.execute("BEGIN")
        query = """SELECT r.run_id,r.state AS run_state,r.created_at,r.updated_at,r.execution_mode,
                      d.submission_id,d.state AS dispatch_state,d.operator_resolution,
                      d.resolution_submission_id,
                      retry.run_id AS retry_child_run_id,
                      retry.state AS retry_dispatch_state,retry.updated_at AS retry_updated_at,
                      s.repository_id,s.producer_id,s.producer_type,s.producer_version,
                      s.constraints,s.correlation_id,s.mission_id,s.engineering_action_id,
                      s.transport_receipt_id,a.document AS action_context_document,
                      p.document AS planning_context_document,
                      h.start_document AS host_start_document,h.terminal_document AS host_terminal_document,
                      activity_summary.payload AS activity_summary_document,
                      EXISTS (
                        SELECT 1 FROM prompt_execution_history AS history
                         WHERE history.run_id=r.run_id AND history.report_path LIKE 'CENTRAL:%'
                      ) AS report_available,
                      EXISTS (
                        SELECT 1 FROM execution_artifact_records AS a
                         WHERE a.artifact_type='ADVISORY_REPORT_ANALYSIS'
                           AND a.content_type='text/markdown'
                           AND a.integrity_status='VERIFIED'
                           AND (a.run_id=r.run_id OR a.ep_run_id=r.run_id)
                      ) AS analysis_available
                 FROM ep_execution_runs AS r
                 LEFT JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=r.run_id
                 LEFT JOIN ep_parity_lifecycle_dispatches AS retry
                   ON retry.submission_id=d.resolution_submission_id
                 LEFT JOIN ep_submissions AS s ON s.submission_id=d.submission_id
                 LEFT JOIN ep_forge_action_context_envelopes AS a ON a.submission_id=s.submission_id
                 LEFT JOIN ep_forge_planning_context_envelopes AS p ON p.submission_id=s.submission_id
                 LEFT JOIN ep_execution_host_evidence AS h ON h.run_id=r.run_id
                LEFT JOIN execution_activity_summaries AS activity_summary ON activity_summary.run_id=r.run_id
                WHERE r.project_id=?
                ORDER BY r.created_at DESC,r.run_id DESC LIMIT ? OFFSET ?"""
        page_size = 500
        rows: list[sqlite3.Row] = []
        offset = 0
        while record_limit is None or offset < record_limit:
            batch_size = page_size if record_limit is None else min(page_size, record_limit - offset)
            page = connection.execute(query, (project_id, batch_size, offset)).fetchall()
            rows.extend(page)
            if len(page) < batch_size:
                break
            offset += len(page)
    finally:
        if owns_connection and connection.in_transaction:
            connection.rollback()
        if owns_connection:
            connection.close()
    return [_central_run_record(row, project_id) for row in rows]


def _central_console_lifecycle(data_root: Path, run_id: str) -> dict[str, object]:
    """Project one run's persisted lifecycle from CENTRAL only."""
    return lifecycle_projection(
        data_root,
        run_id,
        central_database=data_root / SERVER_DATABASE_FILENAME,
    )


def _central_console_execution_projection(
    data_root: Path, record: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Read duration and runtime facts from CENTRAL timing evidence only."""
    run_id = record.get("run_id")
    if not isinstance(run_id, str):
        return {}, {}
    try:
        timing = timing_summary(
            data_root, run_id, central_database=data_root / SERVER_DATABASE_FILENAME,
        )
    except (storage.EngineeringStorageError, sqlite3.DatabaseError):
        timing = {}
    total_ms = timing.get("total_wall_time_ms")
    provider_ms = timing.get("provider_execution_time_ms")
    if not isinstance(total_ms, int) or isinstance(total_ms, bool) or total_ms < 1:
        total_ms = None
        created_at, updated_at = record.get("created_at"), record.get("updated_at")
        if isinstance(created_at, str) and isinstance(updated_at, str):
            try:
                started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                completed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                observed = round((completed - started).total_seconds() * 1000)
                total_ms = observed if observed >= 1 else None
            except ValueError:
                pass
    # A zero aggregate can mean either a genuinely measured sub-millisecond
    # provider turn or that the host stopped an active provider span before a
    # terminal receipt existed.  Only the former is execution-time evidence.
    # In particular, completed specialist reviews must not turn an unmeasured
    # primary invocation into a misleading ``0 sec`` on the detail card.
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            measured_primary = connection.execute(
                """SELECT 1 FROM provider_invocations
                     WHERE run_id=? AND phase='PROVIDER_EXECUTION'
                       AND role NOT LIKE 'reviewer:%'
                       AND completed_at IS NOT NULL AND duration_ms IS NOT NULL
                     LIMIT 1""",
                (run_id,),
            ).fetchone() is not None
    except (sqlite3.DatabaseError, storage.EngineeringStorageError):
        measured_primary = False
    execution = {
        "seconds": (
            provider_ms / 1000
            if isinstance(provider_ms, int) and provider_ms >= 0
            and (provider_ms > 0 or measured_primary)
            else None
        ),
        "total_seconds": total_ms / 1000 if isinstance(total_ms, int) and total_ms >= 0 else None,
    }
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        invocation = connection.execute(
            """SELECT provider,model FROM provider_invocations
                 WHERE run_id=? ORDER BY ordinal DESC LIMIT 1""", (run_id,),
        ).fetchone()
    runtime = {
        "runtime_provider": invocation[0] if invocation and isinstance(invocation[0], str) else None,
        "model": invocation[1] if invocation and isinstance(invocation[1], str) else None,
    }
    return execution, runtime


def _central_console_provider_usage(data_root: Path, run_id: str) -> dict[str, object]:
    """Project persisted provider-use snapshots without manufacturing usage."""
    try:
        summary = provider_usage_summary(
            data_root, run_id, central_database=data_root / SERVER_DATABASE_FILENAME,
        )
    except (OSError, sqlite3.DatabaseError, storage.EngineeringStorageError):
        return {}
    count = summary.get("provider_invocation_count") if isinstance(summary, Mapping) else None
    return dict(summary) if isinstance(count, int) and not isinstance(count, bool) and count > 0 else {}


def _central_console_validation_evidence(
    data_root: Path, project_id: str, run_id: str,
) -> list[dict[str, str]]:
    """Expose only redacted terminal validation results, never commands or paths."""
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute(
                """SELECT a.artifact_id FROM execution_artifact_records AS a
                     JOIN ep_parity_lifecycle_dispatches AS d
                       ON a.ep_run_id=d.run_id OR a.run_id=d.run_id
                    WHERE d.project_id=? AND d.run_id=?
                      AND a.artifact_type='EP_TERMINAL_EVIDENCE'
                      AND a.integrity_status='VERIFIED'
                    ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 1""",
                (project_id, run_id),
            ).fetchone()
            payload = submission_service.producer_evidence_artifact(
                connection, project_id=project_id, artifact_id=str(row[0]),
            ) if row else None
    except sqlite3.Error:
        return []
    try:
        document = json.loads(payload) if payload else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(document, Mapping) or document.get("artifact_type") != "EP_TERMINAL_EVIDENCE":
        return []
    submission, terminal = document.get("submission"), document.get("run")
    if (
        not isinstance(submission, Mapping) or not isinstance(terminal, Mapping)
        or submission.get("project_id") != project_id or terminal.get("id") != run_id
    ):
        return []
    references = document.get("references")
    validations = references.get("validation") if isinstance(references, Mapping) else None
    if not isinstance(validations, list):
        return []
    projected: list[dict[str, str]] = []
    for entry in validations[:20]:
        result = _central_text(entry.get("result")) if isinstance(entry, Mapping) else None
        if result:
            projected.append({"kind": "VALIDATION", "result": redact_diagnostic(result, limit=500)})
    return projected


def _central_console_checkpoint_commit_timeline(
    data_root: Path, project_id: str, submission_id: str, run_id: str, outcome: str,
) -> list[dict[str, str]]:
    """Project strict phase commits from one exactly bound terminal checkpoint."""
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute(
                """SELECT t.payload,t.phase,d.state
                     FROM engineering_transactions AS t
                     JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=t.run_id
                    WHERE d.project_id=? AND d.submission_id=? AND d.run_id=?""",
                (project_id, submission_id, run_id),
            ).fetchone()
    except sqlite3.Error:
        return []
    if row is None or row[1] != outcome or row[2] != outcome:
        return []
    try:
        checkpoint = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if (
        not isinstance(checkpoint, Mapping)
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("phase") != outcome
        or checkpoint.get("terminal") is not True
    ):
        return []
    raw = checkpoint.get("commit_evidence")
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_COMMIT_EVIDENCE_RECORDS
        or any(not is_valid_commit_evidence_record(item) for item in raw)
    ):
        return []
    events = [dict(item) for item in raw]
    identities = {(item["phase"], item["commit_sha"]) for item in events}
    if len(identities) != len(events):
        return []
    return sorted(events, key=lambda item: item["observed_at"])


def _central_console_terminal_revision_timeline(
    data_root: Path, project_id: str, run_id: str,
) -> list[dict[str, str]]:
    """Project verified phase commits plus an exact terminal revision fallback.

    The immutable terminal artifact first proves the project/submission/run
    binding.  The terminal CENTRAL checkpoint then supplies the append-only
    phase commit records that the execution host actually verified.  A
    successful no-change run has no phase commit, so its artifact revision is
    retained as a distinct fallback rather than being called a phase commit.
    """
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            row = connection.execute(
                """SELECT a.artifact_id,a.created_at
                     FROM execution_artifact_records AS a
                     JOIN ep_parity_lifecycle_dispatches AS d
                       ON a.ep_run_id=d.run_id OR a.run_id=d.run_id
                    WHERE d.project_id=? AND d.run_id=?
                      AND a.artifact_type='EP_TERMINAL_EVIDENCE'
                      AND a.integrity_status='VERIFIED'
                      AND a.projection_status='AVAILABLE'
                    ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 1""",
                (project_id, run_id),
            ).fetchone()
            if row is None:
                return []
            payload = submission_service.producer_evidence_artifact(
                connection, project_id=project_id, artifact_id=str(row[0]),
            )
    except sqlite3.Error:
        return []
    if payload is None:
        return []
    try:
        evidence = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(evidence, Mapping) or evidence.get("artifact_type") != "EP_TERMINAL_EVIDENCE":
        return []
    submission = evidence.get("submission")
    terminal_run = evidence.get("run")
    repository = evidence.get("repository")
    if not all(isinstance(value, Mapping) for value in (submission, terminal_run, repository)):
        return []
    submission_id = submission.get("id")
    revision = repository.get("revision")
    outcome = terminal_run.get("outcome")
    if (
        submission.get("project_id") != project_id
        or not isinstance(submission_id, str)
        or terminal_run.get("id") != run_id
        or outcome not in {"COMPLETE", "BLOCKED", "FAILED"}
    ):
        return []
    observed_at = row[1]
    if not isinstance(observed_at, str) or not observed_at.strip():
        return []
    events = _central_console_checkpoint_commit_timeline(
        data_root, project_id, submission_id, run_id, str(outcome),
    )
    if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
        if not any(item["commit_sha"] == revision for item in events):
            events.append({
                "phase": "TERMINAL",
                "observed_at": observed_at,
                "commit_sha": revision,
                "description": "terminal_repository_revision_verified",
            })
    return sorted(events, key=lambda item: item["observed_at"])


def _central_console_current_execution_diagnostic(data_root: Path, project_id: str) -> str | None:
    """Return the selected active run's latest safe diagnostic, if any.

    Component logs are the only operator-log authority.  A diagnostic is
    intentionally scoped through the active project run and returned as plain
    text only; a stored JSON document is not a Console presentation format.
    """
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        active = connection.execute(
            """SELECT run_id FROM ep_execution_runs
                 WHERE project_id=? AND state IN ('CLAIMED','RUNNING')
                 ORDER BY updated_at DESC,run_id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if active is None:
            return None
        rows = connection.execute(
            """SELECT payload FROM engineering_component_logs
                 WHERE json_extract(payload, '$.run_id')=?
                   AND json_extract(payload, '$.diagnostic') IS NOT NULL
                 ORDER BY id DESC LIMIT 20""",
            (str(active[0]),),
        ).fetchall()
    for (payload,) in rows:
        diagnostic = _central_text(_central_json_object(payload).get("diagnostic"))
        # Keep serialized API errors and other JSON payloads out of the UI.
        # The unavailable state is more useful and safer than raw machinery.
        if diagnostic and not diagnostic.lstrip().startswith(("{", "[")):
            return redact_diagnostic(diagnostic, limit=500)
    return None


def _central_console_report_reviewers(report: bytes | None) -> list[dict[str, object]]:
    """Project bounded reviewer findings from one immutable CENTRAL report.

    The report is the terminal authority for reviewers' stated rationale and
    accepted recommendations.  This intentionally mirrors the retained
    Console projection without consulting a repository checkout.
    """
    # The artifact is CENTRAL-authorized, yet terminal reports can be large.
    # This display-only parser needs one short, structured section; bounded
    # input prevents an oversized artifact from amplifying a history request.
    try:
        text = report[:131_072].decode("utf-8") if report is not None else ""
    except UnicodeDecodeError:
        return []
    section = re.search(
        r"^## Reviewer Findings\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return []
    records: list[dict[str, object]] = []
    for block in re.split(r"(?=^- Reviewer: )", section.group("body"), flags=re.MULTILINE):
        reviewer = re.search(r"^- Reviewer:\s*(.+)$", block, re.MULTILINE)
        if reviewer is None:
            continue

        def field(name: str) -> str | None:
            match = re.search(rf"^  - {re.escape(name)}:\s*(.+)$", block, re.MULTILINE)
            return " ".join(match.group(1).split())[:180] if match is not None else None

        reviewer_name = " ".join(reviewer.group(1).split())[:80]
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", reviewer_name) is None:
            continue
        accepted = field("Accepted recommendations")
        records.append({
            "reviewer": reviewer_name,
            "capability": field("Capability") or "engineering",
            # Keep an omitted rationale as data absence.  The dashboard supplies
            # the viewer's locale-specific fallback instead of leaking a Dutch
            # server string into another locale.
            "selected_because": field("Selected because"),
            "accepted_recommendations": int(accepted) if accepted and accepted.isdigit() else 0,
            "status": "completed",
        })
        if len(records) == 12:
            break
    return records


def _central_console_invocation_reviewers(data_root: Path, run_id: str) -> list[dict[str, object]]:
    """Project completed or still-running reviewer identities from CENTRAL.

    Before a run is terminal there is no immutable report to read.  The
    invocation ledger is the authoritative, bounded source for the reviewer
    identities that have actually been invoked; it deliberately makes no
    claim about their recommendations.
    """
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            rows = connection.execute(
                """SELECT role,completed_at FROM provider_invocations
                     WHERE run_id=? AND phase='CAPABILITY_REVIEW'
                       AND role LIKE 'reviewer:%'
                     ORDER BY ordinal LIMIT 12""",
                (run_id,),
            ).fetchall()
    except (sqlite3.DatabaseError, storage.EngineeringStorageError):
        return []
    reviewers: list[dict[str, object]] = []
    for role, completed_at in rows:
        name = str(role).removeprefix("reviewer:").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name) is None:
            continue
        reviewers.append({
            "reviewer": name,
            "capability": "engineering",
            "status": "completed" if isinstance(completed_at, str) and completed_at else "running",
        })
    return reviewers


def _central_console_reviewer_agents(
    data_root: Path, project_id: str, run_id: str,
) -> list[dict[str, object]]:
    """Return report findings when terminal, otherwise exact ledger identities."""
    from_report = _central_console_report_reviewers(
        _central_console_report(data_root, project_id, run_id),
    )
    return from_report or _central_console_invocation_reviewers(data_root, run_id)


def _central_console_assurance_reviews(
    lifecycle: Mapping[str, object],
) -> list[dict[str, object]]:
    """Project the candidate-bound Quality/Security evidence separately.

    Capability reviewers explain why a specialist was selected. Mandatory
    post-implementation assurance instead proves Quality and Security against
    one exact candidate. Keeping these contracts separate prevents terminal
    detail from hiding assurance or presenting a PASS as a recommendation.
    """
    steps = lifecycle.get("steps")
    if not isinstance(steps, list):
        return []
    quality_step = next((
        step for step in steps
        if isinstance(step, Mapping) and step.get("id") == "QUALITY_CONTROL_AGENT"
    ), None)
    reviews = quality_step.get("assurance_reviews") if isinstance(quality_step, Mapping) else None
    if not isinstance(reviews, list) or len(reviews) > 16:
        return []
    projected: list[dict[str, object]] = []
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        reviewer = review.get("reviewer")
        status = review.get("status")
        candidate_sha = review.get("candidate_sha")
        findings = review.get("findings")
        if (
            reviewer not in {"quality", "security"}
            or status not in {"PASS", "FAIL", "UNRESOLVED"}
            or not isinstance(candidate_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None
            or not isinstance(findings, list)
            or len(findings) > 12
            or any(not isinstance(finding, Mapping) for finding in findings)
        ):
            continue
        projected.append(dict(review))
    return projected


def _central_console_pull_request_context(data_root: Path, run_id: str) -> dict[str, object]:
    """Project the PRs already recorded in this run's durable checkpoint."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            """SELECT checkpoint FROM execution_lifecycle_events
                 WHERE run_id=? ORDER BY id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    checkpoint = _central_json_object(row[0]) if row else {}
    return server_console_services._checkpoint_pull_request_context(checkpoint)


def _central_console_project_snapshot(data_root: Path, project_id: str) -> dict[str, object]:
    """Return the project status and terminal-history projections from CENTRAL.

    A live lifecycle is operational state, not historical evidence. Keep it
    exclusively in the current-run status projection; the history table may
    only receive a durable terminal outcome.
    """
    queue = _console_queue_projection(data_root, project_id)
    records = _central_console_run_records(data_root, project_id)
    active = next((record for record in records if record["state"] in {"CLAIMED", "RUNNING"}), None)
    terminal_records = [
        record for record in records
        if record["state"] in {"COMPLETE", "BLOCKED", "FAILED"}
    ]
    active_status: dict[str, object] = {}
    if active is not None:
        pull_request_context = _central_console_pull_request_context(data_root, str(active["run_id"]))
        # The retained dashboard renderer recognizes this established current
        # lifecycle contract. CENTRAL owns the facts, while this small shape
        # adapter keeps the active run visible without inventing history.
        lifecycle = _central_console_lifecycle(data_root, str(active["run_id"]))
        lifecycle_phase = _central_text(lifecycle.get("current_step"))
        # The dashboard's estimator understands lifecycle step identifiers,
        # not the dispatch transport state.  Preserve the safe old fallback
        # for the rare pre-lifecycle claim, but use observed CENTRAL state
        # whenever it is available.
        lifecycle_steps = lifecycle.get("steps")
        active_step = next((
            _central_text(step.get("id"))
            for step in lifecycle_steps if isinstance(step, Mapping)
            and str(step.get("state") or "").upper() == "ACTIVE"
        ), None) if isinstance(lifecycle_steps, list) else None
        active_phase = lifecycle_phase or active_step or (
            "RUNNING" if active["state"] == "RUNNING" else "INITIALIZE"
        )
        active_status = {
            "watcher_state": "ENGINEERING_RUN_ACTIVE",
            "run_id": active["run_id"],
            "current_phase": active_phase,
            # CENTRAL does not receive a safe live Codex activity.  Do not
            # present a synthetic run state as a real activity or filename.
            "current_action": None,
            "central_synthetic_runtime_fields": True,
            "prompt_title": (
                active.get("execution_context", {}).get("mission_title")
                if isinstance(active.get("execution_context"), dict)
                else None
            ),
            "submitted_filename": None,
            # The current-run card is a distinct projection, but it must show
            # the same immutable Forge context as the terminal detail view.
            **active,
            **pull_request_context,
            "reviewer_agents": _central_console_reviewer_agents(
                data_root, project_id, str(active["run_id"]),
            ),
            "lifecycle": lifecycle,
        }
    return {
        "project_id": project_id,
        "scope": "PROJECT",
        "status": {
            "project_id": project_id,
            "platform_version": _console_platform_version(),
            "queue_depth": queue["queue_depth"],
            "queue_items": queue["queue_items"],
            "active_run": active["run_id"] if active else None,
            "active_execution_count": 1 if active else 0,
            "last_executed_run": terminal_records[0]["run_id"] if terminal_records else None,
            "lifecycle_source": "CENTRAL",
            **active_status,
        },
        "runs": terminal_records,
        "queue": queue,
        "prompt_started": active["created_at"] if active else None,
        "telemetry": _central_console_telemetry(data_root, project_id),
    }


def _no_project_console_snapshot(data_root: Path) -> dict[str, object]:
    """Give the Console a loadable CENTRAL-only snapshot at ``<geen>``.

    The no-project document deliberately hides project state, but the shared
    dashboard shell still hydrates through the snapshot endpoint. Returning a
    minimal platform projection prevents it from remaining behind the loading
    overlay while preserving the fail-closed boundary for all project routes.
    """
    # This view intentionally exposes only aggregate operational counts, never
    # a project or run identity.  A submission remains ADMITTED after its
    # dispatch has been claimed, so counting submissions alone would render a
    # live run as queued in the platform pop-out.  A dispatch is the canonical
    # ownership fact: unclaimed submissions are queued; CLAIMED/RUNNING
    # dispatches are active; terminal dispatches are neither.
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        active_execution_count = int(connection.execute(
            "SELECT COUNT(*) FROM ep_parity_lifecycle_dispatches WHERE state IN ('CLAIMED','RUNNING')"
        ).fetchone()[0])
        queue_depth = int(connection.execute(
            """SELECT COUNT(*) FROM ep_submissions AS submission
                 WHERE submission.state IN ('QUEUED','ADMITTED')
                   AND NOT EXISTS (
                       SELECT 1 FROM ep_parity_lifecycle_dispatches AS dispatch
                        WHERE dispatch.submission_id=submission.submission_id
                   )"""
        ).fetchone()[0])
    queue = {
        "operator_handling": {},
        "queue_depth": queue_depth,
        "queue_items": [],
        "active_execution_count": active_execution_count,
        "scope": "ALL_PROJECTS",
    }
    return {
        "scope": "PLATFORM",
        "queue": queue,
        "runs": [],
        "telemetry": [],
        "status": {
            "lifecycle_source": "CENTRAL",
            "platform_version": _console_platform_version(),
            **queue,
        },
    }


def _central_console_run_detail(data_root: Path, project_id: str, run_id: str) -> dict[str, object] | None:
    """Resolve a run and its persisted flow by canonical CENTRAL identity."""
    record = next((record for record in _central_console_run_records(data_root, project_id)
                   if record["run_id"] == run_id), None)
    if record is None:
        return None
    execution, runtime = _central_console_execution_projection(data_root, record)
    lifecycle = _central_console_lifecycle(data_root, run_id)
    terminal = str(record.get("status") or lifecycle.get("terminal_state") or "").upper()
    if terminal in {"BLOCKED", "FAILED"}:
        diagnostic = _central_console_terminal_execution_diagnostic(data_root, run_id)
        if diagnostic is not None:
            record["execution_diagnostic"] = diagnostic
            record["blocking_reason"] = diagnostic
    return {
        **record,
        **_central_console_pull_request_context(data_root, run_id),
        "execution": execution,
        "runtime": runtime,
        "usage": _central_console_provider_usage(data_root, run_id),
        "telemetry_snapshot": run_telemetry_snapshot(
            data_root, run_id, central_database=data_root / SERVER_DATABASE_FILENAME,
        ),
        "reviewers": _central_console_reviewer_agents(data_root, project_id, run_id),
        "assurance_reviews": _central_console_assurance_reviews(lifecycle),
        "evidence": _central_console_validation_evidence(data_root, project_id, run_id),
        # The historical detail dialog uses the same read-only bubble flow as
        # the active card.  Terminal history remains separate in the table,
        # while its exact step evidence stays available on demand.
        "lifecycle": lifecycle,
        "commit_timeline": _central_console_terminal_revision_timeline(
            data_root, project_id, run_id,
        ),
    }


def _central_console_terminal_execution_diagnostic(data_root: Path, run_id: str) -> str | None:
    """Return one redacted terminal checkpoint diagnostic for a history row."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            """SELECT checkpoint FROM execution_lifecycle_events
                 WHERE run_id=? AND phase IN ('BLOCKED', 'FAILED')
                 ORDER BY id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    diagnostic = _central_text(_central_json_object(row[0]).get("diagnostic")) if row else None
    return redact_diagnostic(diagnostic, limit=500) if diagnostic else None


@contextmanager
def _telemetry_read_snapshot(
    data_root: Path,
) -> Iterator[tuple[sqlite3.Connection, str, str]]:
    """Hold one short, read-only SQLite snapshot for a canonical export model."""
    database = (data_root / SERVER_DATABASE_FILENAME).resolve()
    connection = sqlite3.connect(
        f"file:{database}?mode=ro", uri=True, isolation_level=None, timeout=10,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("BEGIN")
        # The first read establishes the SQLite snapshot before any helper is
        # allowed to inspect runs, lineage, usage or timing.
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM engineering_schema_migrations"
        ).fetchone()
        data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
        source_as_of = datetime.now(timezone.utc).isoformat()
        source_reference = f"central-schema:{int(schema_row[0])}:data-version:{data_version}"
        yield connection, source_as_of, source_reference
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _telemetry_retention_days(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key='console.telemetry_retention_days'"
    ).fetchone()
    try:
        value = json.loads(str(row[0])) if row is not None else None
    except json.JSONDecodeError:
        value = None
    allowed = central_database.CONSOLE_CONFIGURATION_OPTIONS["telemetry_retention_days"]
    return int(value) if value in allowed else int(
        central_database.CONSOLE_CONFIGURATION_DEFAULTS["telemetry_retention_days"]
    )


_TELEMETRY_EXPORT_STORE_LOCK = RLock()


def _telemetry_export_store(server_instance: object) -> telemetry_export.ExportSnapshotStore:
    store = getattr(server_instance, "telemetry_export_snapshots", None)
    if isinstance(store, telemetry_export.ExportSnapshotStore):
        return store
    with _TELEMETRY_EXPORT_STORE_LOCK:
        store = getattr(server_instance, "telemetry_export_snapshots", None)
        if not isinstance(store, telemetry_export.ExportSnapshotStore):
            store = telemetry_export.ExportSnapshotStore()
            setattr(server_instance, "telemetry_export_snapshots", store)
        return store


def _retain_telemetry_export_snapshot(
    store: telemetry_export.ExportSnapshotStore,
    model: Mapping[str, object], *, binding: str,
) -> tuple[str | None, tuple[int, str] | None]:
    """Retain one model or return a safe HTTP status/diagnostic pair."""
    try:
        return store.retain(model, binding=binding), None
    except ValueError as error:
        code = str(error)
        safe_code = (
            code if code.startswith("TELEMETRY_EXPORT_")
            else "TELEMETRY_EXPORT_SNAPSHOT_INVALID"
        )
        return None, (
            413 if safe_code == "TELEMETRY_EXPORT_SNAPSHOT_TOO_LARGE" else 500,
            safe_code,
        )
def _central_console_telemetry(
    data_root: Path, project_id: str, *, full: bool = False,
    _read_connection: sqlite3.Connection | None = None,
) -> list[dict[str, object]]:
    """Aggregate canonical run snapshots by UTC day.

    The retired ``execution_runs`` telemetry projection is intentionally not
    consulted: Forge runs are admitted directly into ``ep_execution_runs``.
    """
    grouped: dict[str, list[tuple[Mapping[str, object], dict[str, object]]]] = {}
    terminal_records = [
        record for record in _central_console_run_records(
            data_root, project_id, _read_connection=_read_connection,
            record_limit=None if full else 1000,
        )
        if record.get("state") in {"COMPLETE", "BLOCKED", "FAILED"}
    ]
    identifiers = [str(record["run_id"]) for record in terminal_records]
    try:
        timing_by_run = timing_summaries(
            data_root, identifiers, central_database=data_root / SERVER_DATABASE_FILENAME,
            _read_connection=_read_connection,
        )
        usage_by_run = provider_usage_summaries(
            data_root, identifiers, central_database=data_root / SERVER_DATABASE_FILENAME,
            _read_connection=_read_connection,
        )
    except (storage.EngineeringStorageError, sqlite3.DatabaseError):
        timing_by_run, usage_by_run = {}, {}
    for record in terminal_records:
        completed_at = record.get("updated_at")
        if not isinstance(completed_at, str):
            continue
        try:
            date = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
        except ValueError:
            continue
        run_id = str(record["run_id"])
        snapshot = {
            "contract_version": (timing_by_run.get(run_id) or usage_by_run.get(run_id) or {}).get("contract_version"),
            "attempt": {"timing": timing_by_run.get(run_id, {}), "usage": usage_by_run.get(run_id, {})},
        }
        grouped.setdefault(date, []).append((record, snapshot))
    entries: list[dict[str, object]] = []
    for date, rows in grouped.items():
        def projection(snapshot: Mapping[str, object], name: str) -> Mapping[str, object]:
            attempt = snapshot.get("attempt", {})
            value = attempt.get(name, {}) if isinstance(attempt, Mapping) else {}
            return value if isinstance(value, Mapping) else {}

        def average(key: str) -> float | None:
            values = [int(timing[key]) / 1000 for _, snapshot in rows
                      for timing in (projection(snapshot, "timing"),)
                      if isinstance(timing.get(key), int) and timing[key] >= 0]
            return round(sum(values) / len(values), 3) if values else None
        usage_metrics: dict[str, list[Mapping[str, object]]] = {
            name: [] for name in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")
        }
        for _, snapshot in rows:
            usage = projection(snapshot, "usage")
            metrics = usage.get("metrics", {})
            for name in usage_metrics:
                metric = metrics.get(name) if isinstance(metrics, Mapping) else None
                usage_metrics[name].append(metric if isinstance(metric, Mapping) else {
                    "value": None, "unit": "tokens", "provenance": "UNAVAILABLE",
                    **metric_coverage(
                        expected=None, present=0, valid=0,
                        reason=f"Usage projection unavailable for {snapshot.get('run_id', 'run')}",
                    ),
                })
        daily_usage = {
            name: aggregate_numeric_metric(values, aggregation_level="UTC_DAY", unit="tokens")
            for name, values in usage_metrics.items()
        }
        input_tokens = daily_usage["input_tokens"]["value"]
        output_tokens = daily_usage["output_tokens"]["value"]
        coverage_states = {str(metric["coverage"]) for metric in daily_usage.values()}
        measurement_coverage = (
            "CONFLICT" if "CONFLICT" in coverage_states else
            "PARTIAL" if "PARTIAL" in coverage_states or "UNAVAILABLE" in coverage_states else
            "COMPLETE"
        )
        entries.append({
            "date": date,
            "prompt_count": len(rows),
            "complete_count": sum(row.get("state") == "COMPLETE" for row, _ in rows),
            "blocked_count": sum(row.get("state") == "BLOCKED" for row, _ in rows),
            "failed_count": sum(row.get("state") == "FAILED" for row, _ in rows),
            "average_execution_seconds": average("provider_unique_coverage_ms"),
            "average_total_execution_seconds": average("total_wall_time_ms"),
            "average_queue_wait_seconds": average("queue_wait_time_ms"),
            "average_provider_execution_seconds": average("provider_unique_coverage_ms"),
            "average_validation_seconds": average("validation_time_ms"),
            "average_external_wait_seconds": average("external_wait_time_ms"),
            "average_unassigned_seconds": average("unassigned_time_ms"),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None else None,
            "measurement_coverage": measurement_coverage,
            "usage_metrics": daily_usage,
            "contract_version": selected_version if (
                selected_version := next((snapshot.get("contract_version") for _, snapshot in rows
                                          if snapshot.get("contract_version")), None)
            ) else None,
        })
    ordered = sorted(entries, key=lambda entry: str(entry["date"]), reverse=True)
    return ordered if full else ordered[:360]


def _legacy_central_console_telemetry_detail(data_root: Path, project_id: str, execution_date: str) -> dict[str, object] | None:
    """Provide a project-isolated CENTRAL telemetry day without root fallback."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", execution_date):
        return None
    rows: list[tuple[Mapping[str, object], dict[str, object]]] = []
    for record in _central_console_run_records(data_root, project_id):
        if record.get("state") not in {"COMPLETE", "BLOCKED", "FAILED"}:
            continue
        timestamp = record.get("updated_at")
        if not isinstance(timestamp, str):
            continue
        try:
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat() != execution_date:
                continue
        except ValueError:
            continue
        try:
            timing = timing_summary(data_root, str(record["run_id"]), central_database=data_root / SERVER_DATABASE_FILENAME)
        except (storage.EngineeringStorageError, sqlite3.DatabaseError):
            timing = {}
        rows.append((record, timing))
    if not rows:
        return None
    run_rows = [{
        "run_id": str(record["run_id"]), "started_at": record.get("created_at"), "status": record.get("state"),
        "total_duration_ms": timing.get("total_wall_time_ms"),
        "queue_wait_ms": timing.get("queue_wait_time_ms"),
        "provider_duration_ms": timing.get("provider_execution_time_ms"),
        "validation_duration_ms": timing.get("validation_time_ms"),
        "external_wait_ms": timing.get("external_wait_time_ms"),
        "largest_phase": timing.get("longest_phase"), "producer_type": record.get("producer_type"),
        "repository": record.get("target_repository"), "provider": None, "model": None,
        "reasoning_profile": None,
        "phase_telemetry": "AVAILABLE" if timing.get("phase_telemetry_available") else "NOT_RECORDED",
    } for record, timing in rows]
    durations = [row["total_duration_ms"] for row in run_rows if isinstance(row["total_duration_ms"], int)]
    waits = [row["queue_wait_ms"] for row in run_rows if isinstance(row["queue_wait_ms"], int)]
    def aggregate(values: list[int]) -> dict[str, int] | None:
        return {"average_ms": round(sum(values) / len(values)), "median_ms": sorted(values)[len(values) // 2], "total_ms": sum(values), "runs": len(values)} if values else None
    phase_totals: dict[str, list[int]] = {}
    for _, timing in rows:
        for phase in timing.get("phase_aggregates", []):
            if isinstance(phase, dict) and isinstance(phase.get("phase"), str) and isinstance(phase.get("duration_ms"), int):
                phase_totals.setdefault(phase["phase"], []).append(phase["duration_ms"])
    phases = [
        {"phase": phase, "average_ms": round(sum(values) / len(values)),
         "median_ms": sorted(values)[len(values) // 2], "total_ms": sum(values), "runs": len(values),
         "share_percent": round(sum(values) * 100 / sum(
             int(item.get("total_wall_time_ms", 0)) for _, item in rows
             if isinstance(item.get("total_wall_time_ms"), int)
         ), 3) if any(isinstance(item.get("total_wall_time_ms"), int) and item.get("total_wall_time_ms", 0) > 0 for _, item in rows) else 0.0}
        for phase, values in sorted(phase_totals.items(), key=lambda item: (-sum(item[1]), item[0]))
    ]
    def timing_aggregate(key: str) -> dict[str, int] | None:
        return aggregate([
            int(timing[key]) for _, timing in rows
            if isinstance(timing.get(key), int) and timing[key] >= 0
        ])
    return {
        "date": execution_date, "timezone": "UTC", "runs": run_rows, "phases": phases,
        "phase_telemetry_available": bool(phases),
        "summary": {"executions": len(run_rows), "completed": sum(row["status"] == "COMPLETE" for row in run_rows),
                    "blocked": sum(row["status"] == "BLOCKED" for row in run_rows), "failed": sum(row["status"] == "FAILED" for row in run_rows),
                    "total_wall_time": aggregate(durations), "queue_wait": aggregate(waits),
                    "active_processing_time": timing_aggregate("active_ep_processing_time_ms"),
                    "provider_execution": timing_aggregate("provider_execution_time_ms"),
                    "validation": timing_aggregate("validation_time_ms"),
                    "external_wait": timing_aggregate("external_wait_time_ms"),
                    "overhead": timing_aggregate("overhead_time_ms"),
                    "report_generation": timing_aggregate("report_generation_time_ms"),
                    "evidence_persistence": timing_aggregate("evidence_persistence_time_ms")},
        "bottlenecks": {"longest_average_phase": phases[0]["phase"] if phases else None, "largest_accumulated_phase": phases[0]["phase"] if phases else None, "top_time_consumers": phases[:3], "shares": {}},
    }


def _central_console_telemetry_detail(
    data_root: Path, project_id: str, execution_date: str, *, full: bool = False,
    _read_connection: sqlite3.Connection | None = None,
) -> dict[str, object] | None:
    """Return the canonical contract used by UI, Markdown and JSON export."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", execution_date):
        return None
    day = datetime.strptime(execution_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day + timedelta(days=1)
    matching: list[Mapping[str, object]] = []
    all_records = _central_console_run_records(
        data_root, project_id, _read_connection=_read_connection,
        record_limit=None if full else 1000,
    )
    for record in all_records:
        if record.get("state") not in {"COMPLETE", "BLOCKED", "FAILED"}:
            continue
        timestamp = record.get("updated_at")
        try:
            observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        if not day <= observed < day_end:
            continue
        matching.append(record)
    if not matching:
        return None
    selected_records = matching if full else matching[:MAX_TELEMETRY_DAY_RUNS]
    identifiers = [str(record["run_id"]) for record in selected_records]
    lineage_graph = load_lineage_graph(
        data_root, central_database=data_root / SERVER_DATABASE_FILENAME,
        _read_connection=_read_connection,
    )
    contexts = lineage_graph[0]
    related = set(identifiers)
    # Expand only explicit parent/child edges. This preloads each related
    # attempt once and avoids a query per rendered run or retry row.
    changed = True
    while changed and len(related) < 1000:
        changed = False
        for context in contexts:
            child = str(context["run_id"])
            parent = context["retry_parent_run_id"] or context["resume_parent_run_id"]
            parent_id = str(parent) if parent is not None else None
            if child in related or (parent_id is not None and parent_id in related):
                for candidate in (child, parent_id):
                    if candidate is not None and candidate not in related and len(related) < 1000:
                        related.add(candidate)
                        changed = True
    telemetry_identifiers = identifiers + sorted(related.difference(identifiers))
    try:
        usage_cache = provider_usage_summaries(
            data_root, telemetry_identifiers, central_database=data_root / SERVER_DATABASE_FILENAME,
            invocation_limit=None if full else 250,
            _read_connection=_read_connection,
        )
        timing_cache = timing_summaries(
            data_root, telemetry_identifiers, central_database=data_root / SERVER_DATABASE_FILENAME,
            timeline_limit=None if full else 500,
            _read_connection=_read_connection,
        )
    except (storage.EngineeringStorageError, sqlite3.DatabaseError):
        usage_cache, timing_cache = {}, {}
    selected: list[tuple[Mapping[str, object], dict[str, object]]] = []
    for record in selected_records:
        snapshot = run_telemetry_snapshot(
            data_root, str(record["run_id"]),
            central_database=data_root / SERVER_DATABASE_FILENAME,
            window_start=day, window_end=day_end,
            _usage_cache=usage_cache, _timing_cache=timing_cache,
            _lineage_graph=lineage_graph,
        )
        selected.append((record, snapshot))

    def attempt(snapshot: Mapping[str, object], key: str) -> Mapping[str, object]:
        value = snapshot.get("attempt", {})
        nested = value.get(key, {}) if isinstance(value, Mapping) else {}
        return nested if isinstance(nested, Mapping) else {}

    def run_row(record: Mapping[str, object], snapshot: Mapping[str, object]) -> dict[str, object]:
        timing, usage = attempt(snapshot, "timing"), attempt(snapshot, "usage")
        metrics = usage.get("metrics", {}) if isinstance(usage.get("metrics"), Mapping) else {}
        def metric_value(name: str) -> object:
            metric = metrics.get(name)
            return metric.get("value") if isinstance(metric, Mapping) else None
        try:
            observed = datetime.fromisoformat(str(record.get("updated_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
            outside_selected_window = not day <= observed < day_end
        except ValueError:
            outside_selected_window = None
        return {
            "run_id": str(record["run_id"]), "started_at": record.get("created_at"),
            "completed_at": record.get("updated_at"),
            "status": record.get("state"), "duration_label": "duration",
            "total_duration_ms": timing.get("total_wall_time_ms"),
            "queue_wait_ms": timing.get("queue_wait_time_ms"),
            "provider_duration_ms": timing.get("provider_unique_coverage_ms"),
            "provider_cumulative_duration_ms": timing.get("provider_cumulative_process_duration_ms"),
            "validation_duration_ms": timing.get("validation_time_ms"),
            "external_wait_ms": timing.get("external_wait_time_ms"),
            "unassigned_ms": timing.get("unassigned_time_ms"),
            "largest_phase": timing.get("longest_phase"),
            "producer_type": record.get("producer_type"),
            "repository": record.get("target_repository"),
            "provider": (usage.get("invocations") or [{}])[-1].get("provider") if usage.get("invocations") else None,
            "model": (usage.get("invocations") or [{}])[-1].get("model") if usage.get("invocations") else None,
            "input_tokens": metric_value("input_tokens"), "output_tokens": metric_value("output_tokens"),
            "cache_ratio_percent": usage.get("cache_ratio_percent"),
            "usage_coverage": {name: value.get("coverage") for name, value in metrics.items() if isinstance(value, Mapping)},
            "timing_coverage": timing.get("coverage"),
            "phase_telemetry": "RECORDED" if timing.get("phase_telemetry_available") else "NOT_RECORDED",
            "outside_selected_window": outside_selected_window,
            "chain": snapshot.get("chain"), "telemetry_snapshot": snapshot,
        }

    run_rows = [run_row(record, snapshot) for record, snapshot in selected]
    chain_attempts: dict[str, list[dict[str, object]]] = {}
    if full:
        records_by_run = {str(record["run_id"]): record for record in all_records}
        snapshots_by_run = {str(record["run_id"]): snapshot for record, snapshot in selected}
        for record, snapshot in selected:
            selected_run_id = str(record["run_id"])
            chain = snapshot.get("chain", {})
            chain_rows = chain.get("runs", []) if isinstance(chain, Mapping) else []
            details: list[dict[str, object]] = []
            for chain_row in chain_rows if isinstance(chain_rows, list) else []:
                member = chain_row.get("run_id") if isinstance(chain_row, Mapping) else None
                if not isinstance(member, str) or member not in records_by_run:
                    continue
                member_snapshot = snapshots_by_run.get(member)
                if member_snapshot is None:
                    member_snapshot = run_telemetry_snapshot(
                        data_root, member,
                        central_database=data_root / SERVER_DATABASE_FILENAME,
                        window_start=day, window_end=day_end,
                        _usage_cache=usage_cache, _timing_cache=timing_cache,
                        _lineage_graph=lineage_graph,
                    )
                    snapshots_by_run[member] = member_snapshot
                details.append(run_row(records_by_run[member], member_snapshot))
            chain_attempts[selected_run_id] = details

    def aggregate(values: list[int]) -> dict[str, int] | None:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        median_value = ordered[middle] if len(ordered) % 2 else round((ordered[middle - 1] + ordered[middle]) / 2)
        return {"average_ms": round(sum(values) / len(values)), "median_ms": median_value,
                "total_ms": sum(values), "runs": len(values), "population": len(values)}

    timings = [attempt(snapshot, "timing") for _, snapshot in selected]
    usages = [attempt(snapshot, "usage") for _, snapshot in selected]
    phase_values: dict[str, list[int]] = {}
    exclusive_values: dict[str, int] = {}
    longest_spans: list[Mapping[str, object]] = []
    for timing in timings:
        for row in timing.get("inclusive_phase_rows", []):
            if isinstance(row, Mapping) and isinstance(row.get("phase"), str) and isinstance(row.get("duration_ms"), int):
                phase_values.setdefault(str(row["phase"]), []).append(int(row["duration_ms"]))
        for row in timing.get("exclusive_distribution", []):
            if isinstance(row, Mapping) and isinstance(row.get("category"), str) and isinstance(row.get("duration_ms"), int):
                exclusive_values[str(row["category"])] = exclusive_values.get(str(row["category"]), 0) + int(row["duration_ms"])
        longest_spans.extend(row for row in timing.get("longest_individual_spans", []) if isinstance(row, Mapping))
    wall_total = sum(int(value) for value in (timing.get("total_wall_time_ms") for timing in timings) if isinstance(value, int))
    exclusive_envelope_total = sum(
        int(value) for value in (timing.get("exclusive_envelope_duration_ms") for timing in timings)
        if isinstance(value, int)
    )
    phases = [
        {"phase": phase, **aggregate(values),
         "share_percent": round(sum(values) * 100 / wall_total, 3) if wall_total else None,
         "shares_additive": False}
        for phase, values in sorted(phase_values.items(), key=lambda item: (-sum(item[1]), item[0]))
    ]
    exclusive = [
        {"category": category, "duration_ms": value,
         "share_percent": round(value * 100 / exclusive_envelope_total, 3) if exclusive_envelope_total else None}
        for category, value in sorted(exclusive_values.items(), key=lambda item: (-item[1], item[0]))
    ]
    longest_average = next(iter(sorted(
        phases, key=lambda item: (-int(item["average_ms"]), str(item["phase"])),
    )), None)
    largest_accumulated = next(iter(sorted(
        phases, key=lambda item: (-int(item["total_ms"]), str(item["phase"])),
    )), None)
    longest_individual = next(iter(sorted(
        longest_spans,
        key=lambda item: (-int(item.get("duration_ms", 0)), str(item.get("phase", "")), int(item.get("ordinal", 0))),
    )), None)
    def values(key: str) -> list[int]:
        return [int(timing[key]) for timing in timings if isinstance(timing.get(key), int)]
    observed_usage: dict[str, dict[str, object]] = {}
    for name in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens"):
        metric_rows: list[Mapping[str, object]] = []
        for index, usage in enumerate(usages):
            metrics = usage.get("metrics")
            metric = metrics.get(name) if isinstance(metrics, Mapping) else None
            metric_rows.append(metric if isinstance(metric, Mapping) else {
                "value": None, "unit": "tokens", "provenance": "UNAVAILABLE",
                **metric_coverage(
                    expected=None, present=0, valid=0,
                    reason=f"Usage projection unavailable for {identifiers[index]}",
                ),
                "source_snapshot_reference": identifiers[index],
            })
        observed_usage[name] = aggregate_numeric_metric(
            metric_rows, aggregation_level="UTC_DAY", unit="tokens",
        )
    cache_sources: list[Mapping[str, object]] = []
    cache_inputs = 0
    cache_cached = 0
    cache_population_observed = False
    for index, usage in enumerate(usages):
        population = usage.get("cache_ratio_population")
        if isinstance(population, Mapping):
            cache_sources.append(population)
            if (
                population.get("coverage") != "CONFLICT"
                or population.get("value_semantics") == VALID_SUBTOTAL
            ):
                source_observed = False
                if isinstance(population.get("input_tokens"), int):
                    cache_inputs += int(population["input_tokens"])
                    source_observed = True
                if isinstance(population.get("cached_input_tokens"), int):
                    cache_cached += int(population["cached_input_tokens"])
                    source_observed = True
                cache_population_observed = cache_population_observed or source_observed
        else:
            cache_sources.append(metric_coverage(
                expected=None, present=0, valid=0,
                reason=f"Cache-ratio population unavailable for {identifiers[index]}",
            ))
    cache_coverage = aggregate_telemetry_coverage(cache_sources)
    timing_coverage_sources = [
        {
            "coverage": timing.get("coverage", {}).get("state"),
            **{key: timing.get("coverage", {}).get(key) for key in (
                "expected_observations", "present_observations", "valid_observations",
                "observed_observations", "conflicting_observations", "missing_reason",
            )},
        }
        for timing in timings if isinstance(timing.get("coverage"), Mapping)
    ]
    timing_coverage = aggregate_telemetry_coverage(timing_coverage_sources)
    summary = {
        "executions": len(run_rows), "population": len(run_rows),
        "completed": sum(row["status"] == "COMPLETE" for row in run_rows),
        "blocked": sum(row["status"] == "BLOCKED" for row in run_rows),
        "failed": sum(row["status"] == "FAILED" for row in run_rows),
        "total_wall_time": aggregate(values("total_wall_time_ms")),
        "queue_wait": aggregate(values("queue_wait_time_ms")),
        "provider_unique_coverage": aggregate(values("provider_unique_coverage_ms")),
        "provider_cumulative_process": aggregate(values("provider_cumulative_process_duration_ms")),
        "validation": aggregate(values("validation_time_ms")),
        "external_wait": aggregate(values("external_wait_time_ms")),
        "unassigned": aggregate(values("unassigned_time_ms")),
        "usage": observed_usage,
        "cache_ratio_percent": (
            round(cache_cached * 100 / cache_inputs, 3)
            if cache_inputs and cache_population_observed else None
        ),
        "cache_ratio_population": {
            **cache_coverage,
            "value_semantics": VALID_SUBTOTAL if cache_population_observed else None,
            "input_tokens": cache_inputs if cache_population_observed else None,
            "cached_input_tokens": cache_cached if cache_population_observed else None,
        },
        "timing_coverage": timing_coverage,
    }
    return {
        "contract_version": selected[0][1].get("contract_version"),
        "source_snapshot_references": [str(record["run_id"]) for record, _ in selected],
        "date": execution_date, "timezone": "UTC", "scope": "EP_RUN_ATTEMPTS_IN_UTC_DAY",
        "matching_run_count": len(matching), "returned_run_count": len(selected),
        "runs_truncated": len(matching) > len(selected), "run_limit": None if full else MAX_TELEMETRY_DAY_RUNS,
        "summary": summary, "runs": run_rows,
        "chain_attempts": chain_attempts if full else {},
        "inclusive_phases": phases, "phases": phases,
        "inclusive_shares_additive": False,
        "exclusive_distribution": exclusive,
        "exclusive_envelope_duration_ms": exclusive_envelope_total or None,
        "exclusive_distribution_closes": bool(
            exclusive_envelope_total
            and timing_coverage["coverage"] == "COMPLETE"
            and all(bool(timing.get("exclusive_distribution_closes")) for timing in timings)
            and sum(exclusive_values.values()) == exclusive_envelope_total
        ),
        "phase_telemetry_available": bool(phases),
        "bottlenecks": {
            "longest_average_phase": longest_average,
            "largest_accumulated_phase": largest_accumulated,
            "longest_individual_span": longest_individual,
            "top_time_consumers": phases[:3], "shares": {},
        },
    }


def _central_console_report_path(data_root: Path, project_id: str, run_id: str) -> Path | None:
    """Resolve one project-scoped CENTRAL report without a checkout fallback."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            """SELECT h.report_path FROM prompt_execution_history AS h
                 JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=h.run_id
                 WHERE d.project_id=? AND h.run_id=?""",
            (project_id, run_id),
        ).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0].startswith("CENTRAL:"):
        return None
    candidate = (data_root / "artifacts" / row[0].removeprefix("CENTRAL:")).resolve()
    try:
        candidate.relative_to((data_root / "artifacts").resolve())
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _central_console_report(data_root: Path, project_id: str, run_id: str) -> bytes | None:
    """Read one CENTRAL-indexed immutable report with project authorization."""
    path = _central_console_report_path(data_root, project_id, run_id)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def _central_console_analysis_path(data_root: Path, project_id: str, run_id: str) -> Path | None:
    """Resolve the latest verified advisory analysis for exactly one project run."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        row = connection.execute(
            """SELECT a.digest_algorithm,a.digest,a.storage_location
                 FROM execution_artifact_records AS a
                WHERE a.artifact_type='ADVISORY_REPORT_ANALYSIS'
                  AND a.content_type='text/markdown'
                  AND a.integrity_status='VERIFIED'
                  AND (a.run_id=? OR a.ep_run_id=?)
                  AND EXISTS (
                    SELECT 1 FROM ep_parity_lifecycle_dispatches AS d
                     WHERE d.project_id=? AND d.run_id=?
                  )
                ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 1""",
            (run_id, run_id, project_id, run_id),
        ).fetchone()
    if row is None or row[0] != "sha256" or not isinstance(row[1], str) or not isinstance(row[2], str):
        return None
    candidate = (data_root / "artifacts" / row[2]).resolve()
    try:
        candidate.relative_to((data_root / "artifacts").resolve())
        if not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != row[1]:
            return None
        return candidate
    except (OSError, ValueError):
        return None


def _central_console_analysis(data_root: Path, project_id: str, run_id: str) -> bytes | None:
    """Read only a verified, project-scoped advisory analysis artifact."""
    path = _central_console_analysis_path(data_root, project_id, run_id)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def _central_analysis_processing_status(value: bytes | None) -> str | None:
    """Parse the fixed status line without treating an analysis as a protocol."""
    if value is None:
        return None
    match = re.search(r"(?m)^- Status: `([a-z_]+)`$", value.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def _retry_central_console_analysis(data_root: Path, project_id: str, run_id: str) -> bytes:
    """Regenerate a retryable advisory analysis using CENTRAL evidence only."""
    if _central_analysis_processing_status(_central_console_analysis(data_root, project_id, run_id)) not in RETRYABLE_REPORT_ANALYSIS_STATUSES:
        raise ValueError("ANALYSIS_RETRY_UNAVAILABLE")
    report = _central_console_report_path(data_root, project_id, run_id)
    if report is None:
        raise ValueError("REPORT_NOT_FOUND")
    analysis = analyze_terminal_report(
        data_root,
        run_id,
        report,
        output_directory=data_root / "artifacts" / "report-analysis" / run_id / uuid4().hex,
    )
    artifact_id = f"report-analysis:{run_id}:{uuid4().hex}"
    storage.record_artifact(
        data_root,
        analysis,
        artifact_id=artifact_id,
        artifact_type="ADVISORY_REPORT_ANALYSIS",
        content_type="text/markdown",
        created_at=datetime.now(timezone.utc).isoformat(),
        run_id=run_id,
        ep_run_id=run_id,
        central_database=central_database.path(data_root),
        artifact_root=data_root / "artifacts",
    )
    return analysis.read_bytes()


def _central_console_chat_history(data_root: Path, project_id: str, run_id: str) -> list[dict[str, object]] | None:
    """Return a project-authorized CENTRAL transcript; no root fallback exists."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        belongs = connection.execute(
            "SELECT 1 FROM ep_parity_lifecycle_dispatches AS dispatch "
            "JOIN ep_execution_runs AS run ON run.run_id=dispatch.run_id "
            "WHERE dispatch.project_id=? AND dispatch.run_id=?",
            (project_id, run_id),
        ).fetchone()
        if belongs is None:
            return None
        rows = connection.execute(
            "SELECT role,content,model,created_at FROM execution_chat_messages WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    # ``text`` is the stable Console transcript field.  Returning the storage
    # column name (``content``) silently discarded every persisted message in
    # the browser's strict transcript normalizer.
    return [{"role": str(role), "text": str(content), "model": model, "created_at": str(created_at)}
            for role, content, model, created_at in rows]


def _central_chat_text(value: object, *, limit: int) -> str:
    """Keep CENTRAL chat context redacted, bounded and presentation-neutral."""
    if not isinstance(value, str) or not value.strip():
        return "Niet beschikbaar."
    return redact_diagnostic(value, limit=limit) or "Niet beschikbaar."


def _central_console_chat_context(
    data_root: Path, project_id: str, run_id: str,
) -> dict[str, object] | None:
    """Build one bounded CENTRAL-only context package for a terminal run."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT r.run_id,r.state AS run_state,r.created_at,r.updated_at,
                      d.submission_id,s.repository_id,s.producer_id,s.producer_type,
                      s.producer_version,s.prompt,
                      COALESCE(h.prompt_title,s.engineering_action_id,s.submission_id,r.run_id) AS prompt_title
                   FROM ep_execution_runs AS r
                   JOIN ep_parity_lifecycle_dispatches AS d ON d.run_id=r.run_id
                   JOIN ep_submissions AS s ON s.submission_id=d.submission_id
                   LEFT JOIN prompt_execution_history AS h ON h.run_id=r.run_id
                  WHERE r.project_id=? AND d.project_id=? AND r.run_id=?
                    AND r.state IN ('COMPLETE','BLOCKED','FAILED')""",
            (project_id, project_id, run_id),
        ).fetchone()
    if row is None:
        return None
    report = _central_console_report(data_root, project_id, run_id)
    try:
        report_text = report.decode("utf-8") if report is not None else "Niet beschikbaar."
    except UnicodeDecodeError:
        report_text = "Niet beschikbaar."
    transcript = _central_console_chat_history(data_root, project_id, run_id) or []
    diagnostic = _central_console_terminal_execution_diagnostic(data_root, run_id)
    conversation = [
        {
            "role": str(entry["role"]),
            "text": _central_chat_text(entry.get("text"), limit=1_000),
            "created_at": str(entry["created_at"]),
        }
        for entry in transcript[-8:]
    ]
    return {
        "execution": {
            "run_id": str(row["run_id"]),
            "terminal_state": str(row["run_state"]),
            "submission_id": str(row["submission_id"]),
            "repository_id": str(row["repository_id"]),
            "producer": {
                "id": str(row["producer_id"]),
                "type": str(row["producer_type"]),
                "version": _central_chat_text(row["producer_version"], limit=80),
            },
            "started_at": _central_chat_text(row["created_at"], limit=80),
            "completed_at": _central_chat_text(row["updated_at"], limit=80),
            "title": _central_chat_text(row["prompt_title"], limit=500),
        },
        "submitted_prompt": _central_chat_text(row["prompt"], limit=4_000),
        "verified_engineering_report": _central_chat_text(report_text, limit=6_000),
        "terminal_diagnostic": _central_chat_text(diagnostic, limit=1_000),
        "conversation": conversation,
    }


def _central_console_append_chat_message(
    data_root: Path, project_id: str, run_id: str, role: str, text: object, *, model: str | None = None,
) -> None:
    """Persist one redacted transcript item only after CENTRAL run authorization."""
    if role not in {"user", "assistant"}:
        raise ValueError("INVALID_CHAT_ROLE")
    limit = 6_000 if role == "assistant" else 2_000
    content = _central_chat_text(text, limit=limit)
    if content == "Niet beschikbaar.":
        raise CodexChatError("Het chatbericht bevat geen bewaarbare tekst.", code="CHAT_REQUEST_INVALID")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CHAT_RETENTION_DAYS)).isoformat()
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise ValueError("CHAT_FOREIGN_KEY_ENFORCEMENT_UNAVAILABLE")
        connection.execute("BEGIN IMMEDIATE")
        belongs = connection.execute(
            "SELECT 1 FROM ep_parity_lifecycle_dispatches AS dispatch "
            "JOIN ep_execution_runs AS run ON run.run_id=dispatch.run_id "
            "WHERE dispatch.project_id=? AND dispatch.run_id=?",
            (project_id, run_id),
        ).fetchone()
        if belongs is None:
            raise ValueError("CHAT_CONTEXT_UNAVAILABLE")
        connection.execute("DELETE FROM execution_chat_messages WHERE created_at<?", (cutoff,))
        connection.execute(
            "INSERT INTO execution_chat_messages(run_id,role,content,model,created_at) VALUES(?,?,?,?,?)",
            (run_id, role, content, model, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute(
            "DELETE FROM execution_chat_messages WHERE id IN ("
            "SELECT id FROM execution_chat_messages WHERE run_id=? ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (run_id, MAX_HISTORY_ITEMS),
        )


def _central_console_chat_response(
    data_root: Path, project_id: str, run_id: str, message: object,
) -> tuple[str, list[dict[str, object]]]:
    """Persist a question, then generate and retain its read-only answer.

    The question is stored before invoking the external provider.  Thus a
    browser can close while the provider is working, and a provider failure
    remains visible as a durable, redacted question when that same Run-ID is
    opened again.
    """
    context = _central_console_chat_context(data_root, project_id, run_id)
    if context is None:
        raise ValueError("CHAT_CONTEXT_UNAVAILABLE")
    _central_console_append_chat_message(data_root, project_id, run_id, "user", message)
    answer = respond_with_context(message, context)
    _central_console_append_chat_message(data_root, project_id, run_id, "assistant", answer, model=chat_model())
    return answer, _central_console_chat_history(data_root, project_id, run_id) or []


def _central_console_clear_chat_history(data_root: Path, project_id: str, run_id: str) -> bool:
    """Clear only the selected project's advisory transcript, never run evidence."""
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        belongs = connection.execute(
            "SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchone()
        if belongs is None:
            return False
        connection.execute("DELETE FROM execution_chat_messages WHERE run_id=?", (run_id,))
    return True


@dataclass(frozen=True)
class _CentralLogQuery:
    page: int
    page_size: int
    start_at: str
    end_at: str
    inclusive_end: bool
    search: str
    level: str
    events: tuple[str, ...]
    sort_key: str
    direction: str


def _parse_central_log_query(values: dict[str, list[str]] | None) -> _CentralLogQuery:
    """Validate the public query once, before composing any CENTRAL SQL."""
    query = values or {}

    def first(name: str, default: str = "") -> str:
        return str((query.get(name) or [default])[0])

    try:
        page = int(first("page", "1"))
        page_size = int(first("page_size", "50"))
    except ValueError as error:
        raise ValueError("Invalid component-log pagination.") from error
    if page < 1 or not 1 <= page_size <= MAX_COMPONENT_LOG_PAGE_SIZE:
        raise ValueError("Invalid component-log pagination.")
    level, search = first("level").upper().strip(), first("search").strip()
    if level and level not in VALID_LEVELS:
        raise ValueError("Invalid component-log level.")
    if len(search) > 160:
        raise ValueError("Component-log search is too long.")
    events = tuple(sorted({value.strip() for value in query.get("event", []) if value.strip()}))
    if len(events) > 50 or any(len(event) > 160 for event in events):
        raise ValueError("Invalid component-log event filter.")
    sort_key, direction = first("sort", "timestamp"), first("direction", "desc").lower()
    if sort_key not in _CENTRAL_LOG_SORT_COLUMNS or direction not in {"asc", "desc"}:
        raise ValueError("Invalid component-log sort.")
    return _CentralLogQuery(
        page=page,
        page_size=page_size,
        start_at=first("start").strip(),
        end_at=first("end").strip(),
        inclusive_end=first("inclusive_end") == "1",
        search=search,
        level=level,
        events=events,
        sort_key=sort_key,
        direction=direction,
    )


def _central_log_components(component: str) -> tuple[frozenset[str], tuple[str, ...]] | None:
    """Resolve only canonical component identities without project context."""
    if component == "all":
        selected = PLATFORM_COMPONENT_IDS
    elif component in PLATFORM_COMPONENT_IDS:
        selected = frozenset({component})
    else:
        return None
    return selected, tuple(selected)


def _central_console_component_logs(
    data_root: Path,
    component: str,
    query: dict[str, list[str]] | None = None,
    *,
    export_all: bool = False,
) -> dict[str, object] | None:
    """Read one filtered, sorted CENTRAL log page before it reaches the Console."""
    selection = _central_log_components(component)
    if selection is None:
        return None
    _, stored_components = selection
    filters = _parse_central_log_query(query)
    clauses = ["component IN (" + ",".join("?" for _ in stored_components) + ")"]
    parameters: list[object] = list(stored_components)
    if filters.start_at:
        clauses.append("created_at >= ?")
        parameters.append(filters.start_at)
    if filters.end_at:
        clauses.append("created_at <= ?" if filters.inclusive_end else "created_at < ?")
        parameters.append(filters.end_at)
    if filters.search:
        escaped = filters.search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(payload) LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")
    if filters.level in LOG_LEVELS_AT_OR_ABOVE:
        levels = LOG_LEVELS_AT_OR_ABOVE[filters.level]
        clauses.append("json_extract(payload, '$.level') IN (" + ",".join("?" for _ in levels) + ")")
        parameters.extend(levels)
    event_option_clauses, event_option_parameters = list(clauses), list(parameters)
    if filters.events:
        clauses.append("json_extract(payload, '$.event') IN (" + ",".join("?" for _ in filters.events) + ")")
        parameters.extend(filters.events)
    where = " WHERE " + " AND ".join(clauses)
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM engineering_component_logs" + where, parameters).fetchone()[0])
        rows = connection.execute(
            "SELECT id,component,payload,created_at FROM engineering_component_logs" + where
            + f" ORDER BY {_CENTRAL_LOG_SORT_COLUMNS[filters.sort_key]} {filters.direction.upper()}, id {filters.direction.upper()} LIMIT ? OFFSET ?",
            [
                *parameters,
                5000 if export_all else filters.page_size,
                0 if export_all else (filters.page - 1) * filters.page_size,
            ],
        ).fetchall()
        event_rows = connection.execute(
            "SELECT DISTINCT json_extract(payload, '$.event') FROM engineering_component_logs WHERE "
            + " AND ".join(event_option_clauses)
            + " AND json_extract(payload, '$.event') IS NOT NULL ORDER BY 1 LIMIT 500",
            event_option_parameters,
        ).fetchall()
    entries: list[dict[str, object]] = []
    for identifier, stored_component, payload, created_at in rows:
        try:
            decoded = json.loads(str(payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {"event": "malformed_central_log"}
        record = decoded if isinstance(decoded, dict) else {}
        entries.append({"line": int(identifier), "timestamp": str(created_at), **record, "component": str(stored_component)})
    return {
        "scope": "PLATFORM", "component": component, "entries": entries,
        "page": filters.page, "page_size": filters.page_size, "total": total,
        "events": [str(row[0]) for row in event_rows if row[0]],
    }


def _clear_central_console_component_logs(data_root: Path, component: str) -> dict[str, object] | None:
    """Delete only the explicitly selected CENTRAL component-log projection."""
    if component == "all":
        selected = PLATFORM_COMPONENT_IDS
    elif component in PLATFORM_COMPONENT_IDS:
        selected = frozenset({component})
    else:
        return None
    stored = tuple(selected)
    with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
        cursor = connection.execute(
            "DELETE FROM engineering_component_logs WHERE component IN (" + ",".join("?" for _ in stored) + ")",
            stored,
        )
    return {"scope": "PLATFORM", "component": component, "deleted": int(cursor.rowcount)}


def _central_console_configuration(data_root: Path) -> dict[str, object]:
    """Expose only configuration already owned by CENTRAL in this phase."""
    return {
        "scope": "PLATFORM",
        **central_database.maintenance_configuration(data_root),
        **central_database.capacity_configuration(data_root),
        **central_database.console_interval_configuration(data_root),
    }


def _central_provider_readiness(data_root: Path) -> dict[str, dict[str, object]]:
    """Project-independent, token-free authentication readiness for CENTRAL."""
    statuses = provider_readiness.host_status(data_root)
    runtime = provider_readiness.runtime_details(data_root)
    return {
        provider: {**value, **runtime.get(provider, {}), "scope": "PLATFORM"}
        for provider, value in statuses.items()
    }


def _audit_dashboard_provider_action(
    data_root: Path, provider: str, action: str, outcome: str, *, level: int = logging.INFO,
) -> None:
    """Persist a secret-free audit fact for one host-wide Console action."""
    log_event(
        _operations_console_logger(data_root),
        level,
        f"provider_action_{outcome.lower()}",
        context={
            "audit_action": f"provider_{action}",
            "provider": provider,
            "provider_action": action,
            "provider_action_source": "DASHBOARD",
            "audit_actor": DASHBOARD_AUDIT_ACTOR,
            "audit_outcome": outcome,
            "user_action": f"provider_{action}",
        },
    )


def _central_provider_repair(data_root: Path, payload: object) -> None:
    """Start one validated host-wide provider action without a checkout."""
    if not isinstance(payload, dict) or set(payload) != {"provider", "action"}:
        raise ValueError("Invalid provider repair request.")
    provider, action = str(payload["provider"]), str(payload["action"])
    if provider not in {"CODEX", "GITHUB"} or action not in {"login", "install"}:
        raise ValueError("Invalid provider repair request.")
    _audit_dashboard_provider_action(data_root, provider, action, "REQUESTED")
    try:
        readiness = _central_provider_readiness(data_root)
        state = str(readiness[provider.lower()]["state"])
        if (action == "login" and state != "AUTH_REQUIRED") or (action == "install" and state != "UNAVAILABLE"):
            raise ValueError("Provider is not ready for the requested repair.")
        if action == "login":
            _start_provider_login(data_root, provider)
            _audit_dashboard_provider_action(data_root, provider, action, "STARTED")
        else:
            _install_provider(data_root, provider)
            _audit_dashboard_provider_action(data_root, provider, action, "COMPLETED")
    except (OSError, RuntimeError, ValueError):
        _audit_dashboard_provider_action(data_root, provider, action, "FAILED", level=logging.WARNING)
        raise


def _central_provider_logout(data_root: Path, payload: object) -> None:
    """Remove one verified host-wide provider session through CENTRAL.

    The Console never owns provider credentials.  It can only request this
    narrowly validated host operation while the provider is known ready, so a
    stale or fabricated UI request cannot be delegated to a checkout-bound
    legacy handler.
    """
    if not isinstance(payload, dict) or set(payload) != {"provider"}:
        raise ValueError("Invalid provider logout request.")
    provider = str(payload["provider"])
    if provider not in {"CODEX", "GITHUB"}:
        raise ValueError("Invalid provider logout request.")
    _audit_dashboard_provider_action(data_root, provider, "logout", "REQUESTED")
    try:
        readiness = _central_provider_readiness(data_root)
        if str(readiness[provider.lower()]["state"]) != "READY":
            raise ValueError("Provider is not ready for logout.")
        _logout_provider(data_root, provider)
        _audit_dashboard_provider_action(data_root, provider, "logout", "COMPLETED")
    except (OSError, RuntimeError, ValueError):
        _audit_dashboard_provider_action(data_root, provider, "logout", "FAILED", level=logging.WARNING)
        raise


def _with_console_queue(payload: bytes, *, queue: dict[str, object], data_root: Path) -> bytes:
    """Overlay CENTRAL-only queue and provider evidence onto legacy payloads."""
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return payload
    if not isinstance(decoded, dict):
        return payload
    handling = queue.get("operator_handling")
    if isinstance(handling, dict) and isinstance(decoded.get("runs"), list):
        for run in decoded["runs"]:
            if isinstance(run, dict) and handling.get(run.get("run_id")) == "DISMISSED":
                run["dismissed"] = True
                run["handling_state"] = "DISMISSED"
    status_payload = decoded.get("status")
    if isinstance(status_payload, dict):
        decoded["status"] = {**status_payload, **queue}
    else:
        decoded = {**decoded, **queue}
    rate_limits = decoded.get("rate_limits")
    if isinstance(rate_limits, dict):
        provider = rate_limits.get("provider")
        remaining = _remaining_rate_limit_capacity(rate_limits)
        if isinstance(provider, str) and remaining is not None:
            decoded["ai_capacity_history"] = central_database.record_provider_capacity(
                data_root, provider=provider, remaining_percent=remaining,
            )
            decoded["capacity_scope"] = "EP"
            decoded["capacity_configuration"] = central_database.capacity_configuration(data_root)
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


def _provider_capacity_projection(data_root: Path) -> dict[str, object]:
    """Read the account-owned quota once and project it from CENTRAL."""
    try:
        payload = json.loads(_codex_rate_limits())
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    provider = payload.get("provider")
    remaining = _remaining_rate_limit_capacity(payload)
    history: list[dict[str, object]] = []
    if isinstance(provider, str) and remaining is not None:
        history = central_database.record_provider_capacity(
            data_root, provider=provider, remaining_percent=remaining,
        )
    return {
        "rate_limits": payload,
        "ai_capacity_history": history,
        "scope": "EP",
        "configuration": central_database.capacity_configuration(data_root),
    }


def _console_project_options(project_id: str | None, projects: list[dict[str, str]]) -> str:
    """Render the safe empty choice plus registered CENTRAL identities."""
    empty = '<option value=""' + (" selected" if project_id is None else "") + '>&lt;geen&gt;</option>'
    return empty + "".join(
        f'<option value="{escape(item["project_id"], quote=True)}"'
        f'{" selected" if item["project_id"] == project_id else ""}>'
        f'{escape(item["project_id"])}</option>'
        for item in projects
    )


def _central_database_section(data_root: Path) -> str:
    """Render the one installation-owned EP database panel for Configuration."""
    details = central_database.details(data_root)
    interval = central_database.maintenance_configuration(data_root)["interval_seconds"]
    size = f"{int(details['size_bytes']) / 1_000_000:.2f}".replace(".", ",") + " MB"
    options = "".join(
        f'<option value="{value}"{" selected" if value == interval else ""}>{label}</option>'
        for value, label in ((60, "1 minuut"), (3600, "1 uur"), (86400, "1 dag"), (604800, "1 week"))
    )
    return (
        '<section class="configuration-central-database" aria-labelledby="centralDatabaseHeading">'
        '<header class="configuration-central-database__header">'
        '<div><h2 id="centralDatabaseHeading" data-i18n="configuration.ep_database">EP-database</h2>'
        '<p data-i18n="configuration.ep_database_description">Platformbrede opslag voor projecten, uitvoeringen en configuratie.</p></div>'
        '<div class="configuration-central-database__actions"><a class="configuration-central-database__export" href="/api/central-data/export" download '
        'data-i18n="configuration.central_data_export" data-i18n-aria-label="configuration.central_data_export" '
        'aria-label="Exporteer platformgegevens">Exporteer platformgegevens</a><button class="configuration-central-database__relocate" id="centralDataImport" type="button" data-i18n="configuration.central_data_import">Importeer platformgegevens</button></div></header>'
        '<dl class="configuration-central-database__facts">'
        f'<div class="configuration-central-database__location"><dt class="label" data-i18n="configuration.platform_data_location">Platformgegevenslocatie</dt><dd class="configuration-central-database__location-value"><button class="configuration-central-database__location-link local-folder-link" type="button" data-local-path="{escape(str(data_root.resolve()))}">{escape(str(data_root.resolve()))}</button><button class="configuration-central-database__relocate" id="centralDatabaseRelocate" type="button" data-i18n="configuration.relocate_platform_data">Verplaats platformgegevens</button></dd></div>'
        f'<div><dt class="label" data-i18n="configuration.database_size">Databasegrootte</dt><dd>{size}</dd></div>'
        f'<div><dt class="label" data-i18n="configuration.schema_version">Schema-versie</dt><dd>{details["schema_version"]}</dd></div>'
        f'<div><dt class="label" data-i18n="configuration.integrity">Integriteit</dt><dd data-i18n="configuration.database_integrity.{details["integrity"]}">{details["integrity"]}</dd></div>'
        '</dl>'
        '<div class="configuration-central-database__maintenance">'
        '<div><span class="label" id="centralDatabaseMaintenanceLabel" data-i18n="configuration.ep_database_maintenance">Databaseonderhoud</span>'
        '<p id="centralDatabaseMaintenanceHelp" data-i18n="configuration.ep_database_maintenance_help">Optimaliseert de EP-database wanneer geen uitvoering actief is.</p></div>'
        f'<select id="centralDatabaseMaintenanceInterval" aria-labelledby="centralDatabaseMaintenanceLabel" aria-describedby="centralDatabaseMaintenanceHelp centralDatabaseMaintenanceStatus" data-saved-value="{interval}">{options}</select>'
        '</div>'
        '<p id="centralDatabaseMaintenanceStatus" role="status" aria-live="polite"></p></section>'
        f'<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation installation-relocation-modal" id="centralDatabaseRelocateModal"><section class="dashboard-modal-shell__panel"><header class="dashboard-modal-shell__header"><h2 data-modal-glyph="relocate" data-i18n="configuration.relocate_platform_data">Verplaats platformgegevens</h2><button class="dashboard-modal-shell__close" type="button" aria-label="Close" data-close-relocation="centralDatabaseRelocateModal">×</button></header><p data-i18n="configuration.relocate_platform_data_help">Verplaats alle platformgegevens als één geheel. De server stopt veilig en start daarna opnieuw.</p><dl class="installation-relocation-modal__locations"><div><dt data-i18n="configuration.current_folder">Huidige map</dt><dd><button class="local-folder-link" type="button" data-local-path="{escape(str(data_root.resolve()))}">{escape(str(data_root.resolve()))}</button></dd></div><div id="centralDatabaseRelocateDestination" hidden><dt data-i18n="configuration.new_folder">Nieuwe map</dt><dd id="centralDatabaseRelocateDestinationValue"></dd></div></dl><input id="centralDatabaseRelocateDirectory" type="hidden"><div class="dashboard-modal-shell__actions"><button class="dashboard-modal-shell__action" id="centralDatabaseRelocateBrowse" type="button" data-i18n="configuration.choose_folder">Kies map</button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="centralDatabaseRelocateSave" type="button" disabled data-i18n="configuration.relocate">Verplaatsen</button></div><p id="centralDatabaseRelocateStatus" role="status"></p></section></dialog>'
        '<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation installation-relocation-modal" id="centralDataImportModal" onclose="this.querySelector(\'#centralDataImportFile\').value=\'\';this.querySelector(\'#centralDataImportConfirm\').disabled=true;this.querySelector(\'#centralDataImportStatus\').textContent=\'\'"><section class="dashboard-modal-shell__panel"><header class="dashboard-modal-shell__header"><h2 data-modal-glyph="import" data-i18n="configuration.central_data_import">Importeer platformgegevens</h2><button class="dashboard-modal-shell__close" type="button" aria-label="Close" data-close-central-import>×</button></header><p data-i18n="configuration.central_data_import_help">Kies een eerder geëxporteerd ZIP-bestand. Alle huidige platformgegevens worden vervangen.</p><input id="centralDataImportFile" type="file" accept="application/zip,.zip"><p class="installation-relocation-modal__warning" data-i18n="configuration.central_data_import_warning">Dit vervangt de volledige huidige platformstatus.</p><div class="dashboard-modal-shell__actions"><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="centralDataImportConfirm" type="button" disabled data-i18n="configuration.central_data_import_confirm">Importeren en herstarten</button></div><p id="centralDataImportStatus" role="status"></p></section></dialog>'
        '<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation dashboard-modal-shell--destructive installation-relocation-modal" id="centralDataImportWarningModal"><section class="dashboard-modal-shell__panel"><header class="dashboard-modal-shell__header"><h2 data-modal-glyph="warning" data-i18n="configuration.central_data_import_confirm_title">Bevestig import</h2><button class="dashboard-modal-shell__close" type="button" aria-label="Close" data-close-central-import-warning>×</button></header><p class="installation-relocation-modal__warning" data-i18n="configuration.central_data_import_confirm_warning">Alle bestaande platformgegevens worden definitief vervangen en kunnen verloren gaan.</p><div class="dashboard-modal-shell__actions"><button class="dashboard-modal-shell__action" type="button" data-close-central-import-warning data-i18n="configuration.central_data_import_confirm_cancel">Annuleren</button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary dashboard-modal-shell__action--destructive" id="centralDataImportProceed" type="button" data-i18n="configuration.central_data_import_confirm_proceed">Ja, vervang gegevens en herstart</button></div></section></dialog>'
    )


def _central_database_script() -> str:
    """Bind CENTRAL maintenance plus whole-state transfer controls."""
    return '''for(const id of ['centralDatabaseRelocateModal','centralDataImportModal']){const dialog=document.getElementById(id);if(dialog&&dialog.parentElement!==document.body)document.body.append(dialog)}const maintenance=document.getElementById('centralDatabaseMaintenanceInterval'),maintenanceStatus=document.getElementById('centralDatabaseMaintenanceStatus'),translate=window.__engineeringPlatformDashboardTranslate;if(maintenance)maintenance.addEventListener('change',async()=>{const previous=maintenance.dataset.savedValue||maintenance.value,requested=Number(maintenance.value);maintenance.disabled=true;try{const response=await fetch('/api/central-database/configuration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_seconds:requested})});const result=response.ok?await response.json():null;if(!result||Number(result.interval_seconds)!==requested)throw Error();maintenance.dataset.savedValue=String(requested);if(maintenanceStatus)maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_saved')}catch{maintenance.value=previous;if(maintenanceStatus)maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_failed')}finally{maintenance.disabled=false}});const modal=document.getElementById('centralDatabaseRelocateModal'),open=document.getElementById('centralDatabaseRelocate'),input=document.getElementById('centralDatabaseRelocateDirectory'),destination=document.getElementById('centralDatabaseRelocateDestination'),destinationValue=document.getElementById('centralDatabaseRelocateDestinationValue'),save=document.getElementById('centralDatabaseRelocateSave'),status=document.getElementById('centralDatabaseRelocateStatus');let prepared=false;const showDestination=result=>{input.value=result.directory;destinationValue.replaceChildren(window.__engineeringPlatformLocalFilesystemLink(result.value));destination.hidden=false;save.disabled=false;prepared=true;};const discard=()=>{if(!prepared||!input.value)return;prepared=false;fetch('/api/central-data/relocate/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:input.value}),keepalive:true});};open?.addEventListener('click',()=>modal.showModal());modal?.addEventListener('close',discard);modal?.querySelector('[data-close-relocation]')?.addEventListener('click',()=>modal.close());document.getElementById('centralDatabaseRelocateBrowse')?.addEventListener('click',async()=>{discard();const r=await fetch('/api/central-data/relocate/browse',{method:'POST'}),p=await r.json();if(r.ok&&p.value&&p.directory)showDestination(p);else status.textContent=p.error||translate('configuration.relocation_failed');});save?.addEventListener('click',async()=>{save.disabled=true;const r=await fetch('/api/central-data/relocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:input.value})}),p=await r.json();status.textContent=r.ok?translate('configuration.relocation_restarting'):p.error||translate('configuration.relocation_failed');if(r.ok){prepared=false;setTimeout(()=>location.reload(),2500)}else save.disabled=false;});const importModal=document.getElementById('centralDataImportModal'),importOpen=document.getElementById('centralDataImport'),importFile=document.getElementById('centralDataImportFile'),importSave=document.getElementById('centralDataImportConfirm'),importStatus=document.getElementById('centralDataImportStatus');importOpen?.addEventListener('click',()=>importModal.showModal());importModal?.querySelector('[data-close-central-import]')?.addEventListener('click',()=>importModal.close());importFile?.addEventListener('change',()=>{importSave.disabled=!importFile.files?.length;});importSave?.addEventListener('click',async()=>{const file=importFile.files?.[0];if(!file)return;importSave.disabled=true;const r=await fetch('/api/central-data/import',{method:'POST',headers:{'Content-Type':'application/zip','X-EP-Central-Import-Confirmed':'true'},body:file}),p=await r.json();importStatus.textContent=r.ok?translate('configuration.relocation_restarting'):p.error||translate('configuration.relocation_failed');if(r.ok)setTimeout(()=>location.reload(),2500);else importSave.disabled=false;});'''
    return '''for(const id of ['centralDatabaseRelocateModal','centralDataImportModal','fileInboxRelocateModal']){const dialog=document.getElementById(id);if(dialog&&dialog.parentElement!==document.body)document.body.append(dialog)}const maintenance=document.getElementById('centralDatabaseMaintenanceInterval'),maintenanceStatus=document.getElementById('centralDatabaseMaintenanceStatus'),translate=window.__engineeringPlatformDashboardTranslate;if(maintenance)maintenance.addEventListener('change',async()=>{const previous=maintenance.dataset.savedValue||maintenance.value,requested=Number(maintenance.value);maintenance.disabled=true;try{const response=await fetch('/api/central-database/configuration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_seconds:requested})});const result=response.ok?await response.json():null;if(!result||Number(result.interval_seconds)!==requested)throw Error();maintenance.dataset.savedValue=String(requested);if(maintenanceStatus)maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_saved')}catch{maintenance.value=previous;if(maintenanceStatus)maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_failed')}finally{maintenance.disabled=false}});const modal=document.getElementById('centralDatabaseRelocateModal'),open=document.getElementById('centralDatabaseRelocate'),input=document.getElementById('centralDatabaseRelocateDirectory'),destination=document.getElementById('centralDatabaseRelocateDestination'),destinationValue=document.getElementById('centralDatabaseRelocateDestinationValue'),save=document.getElementById('centralDatabaseRelocateSave'),status=document.getElementById('centralDatabaseRelocateStatus'),showDestination=value=>{input.value=value;destinationValue.replaceChildren(window.__engineeringPlatformLocalFilesystemLink(value));destination.hidden=false;save.disabled=false;};open?.addEventListener('click',()=>modal.showModal());modal?.querySelector('[data-close-relocation]')?.addEventListener('click',()=>modal.close());document.getElementById('centralDatabaseRelocateBrowse')?.addEventListener('click',async()=>{const r=await fetch('/api/central-data/relocate/browse',{method:'POST'}),p=await r.json();if(p.value)showDestination(p.value);});save?.addEventListener('click',async()=>{const r=await fetch('/api/central-data/relocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:input.value})}),p=await r.json();status.textContent=r.ok?translate('configuration.relocation_restarting'):p.error||translate('configuration.relocation_failed');if(r.ok)setTimeout(()=>location.reload(),2500);});const importModal=document.getElementById('centralDataImportModal'),importOpen=document.getElementById('centralDataImport'),importFile=document.getElementById('centralDataImportFile'),importSave=document.getElementById('centralDataImportConfirm'),importStatus=document.getElementById('centralDataImportStatus');importOpen?.addEventListener('click',()=>importModal.showModal());importModal?.querySelector('[data-close-central-import]')?.addEventListener('click',()=>importModal.close());importFile?.addEventListener('change',()=>{importSave.disabled=!importFile.files?.length;});importSave?.addEventListener('click',async()=>{const file=importFile.files?.[0];if(!file)return;importSave.disabled=true;const r=await fetch('/api/central-data/import',{method:'POST',headers:{'Content-Type':'application/zip','X-EP-Central-Import-Confirmed':'true'},body:file}),p=await r.json();importStatus.textContent=r.ok?translate('configuration.relocation_restarting'):p.error||translate('configuration.relocation_failed');if(r.ok)setTimeout(()=>location.reload(),2500);else importSave.disabled=false;});'''


def _central_database_script() -> str:
    """Bind CENTRAL maintenance and whole-state transfer controls."""
    return '''for(const id of ['centralDatabaseRelocateModal','centralDataImportModal']){const dialog=document.getElementById(id);if(dialog&&dialog.parentElement!==document.body)document.body.append(dialog)}const translate=window.__engineeringPlatformDashboardTranslate,maintenance=document.getElementById('centralDatabaseMaintenanceInterval'),maintenanceStatus=document.getElementById('centralDatabaseMaintenanceStatus');maintenance?.addEventListener('change',async()=>{const previous=maintenance.dataset.savedValue||maintenance.value,requested=Number(maintenance.value);maintenance.disabled=true;try{const response=await fetch('/api/central-database/configuration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_seconds:requested})}),result=response.ok?await response.json():null;if(!result||Number(result.interval_seconds)!==requested)throw Error();maintenance.dataset.savedValue=String(requested);maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_saved')}catch{maintenance.value=previous;maintenanceStatus.textContent=translate('configuration.ep_database_maintenance_failed')}finally{maintenance.disabled=false}});const relocationModal=document.getElementById('centralDatabaseRelocateModal'),relocationOpen=document.getElementById('centralDatabaseRelocate'),relocationDirectory=document.getElementById('centralDatabaseRelocateDirectory'),relocationDestination=document.getElementById('centralDatabaseRelocateDestination'),relocationDestinationValue=document.getElementById('centralDatabaseRelocateDestinationValue'),relocationSave=document.getElementById('centralDatabaseRelocateSave'),relocationStatus=document.getElementById('centralDatabaseRelocateStatus');let relocationPrepared=false;const discardRelocation=()=>{if(!relocationPrepared||!relocationDirectory.value)return;relocationPrepared=false;fetch('/api/central-data/relocate/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:relocationDirectory.value}),keepalive:true})};relocationOpen?.addEventListener('click',()=>relocationModal.showModal());relocationModal?.addEventListener('close',discardRelocation);relocationModal?.querySelector('[data-close-relocation]')?.addEventListener('click',()=>relocationModal.close());document.getElementById('centralDatabaseRelocateBrowse')?.addEventListener('click',async()=>{discardRelocation();const response=await fetch('/api/central-data/relocate/browse',{method:'POST'}),result=await response.json();if(response.ok&&result.value&&result.directory){relocationDirectory.value=result.directory;relocationDestinationValue.replaceChildren(window.__engineeringPlatformLocalFilesystemLink(result.value));relocationDestination.hidden=false;relocationSave.disabled=false;relocationPrepared=true}else relocationStatus.textContent=result.error||translate('configuration.relocation_failed')});relocationSave?.addEventListener('click',async()=>{relocationSave.disabled=true;const response=await fetch('/api/central-data/relocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:relocationDirectory.value})}),result=await response.json();relocationStatus.textContent=response.ok?translate('configuration.relocation_restarting'):result.error||translate('configuration.relocation_failed');if(response.ok){relocationPrepared=false;setTimeout(()=>location.reload(),2500)}else relocationSave.disabled=false});const importModal=document.getElementById('centralDataImportModal'),importOpen=document.getElementById('centralDataImport'),importFile=document.getElementById('centralDataImportFile'),importSave=document.getElementById('centralDataImportConfirm'),importStatus=document.getElementById('centralDataImportStatus');if(importFile)importFile.accept='.epdata,application/vnd.engineering-platform.epdata+zip';const resetImport=()=>{importFile.value='';importSave.disabled=true;importStatus.textContent=''};importOpen?.addEventListener('click',()=>importModal.showModal());importModal?.addEventListener('close',resetImport);importModal?.querySelector('[data-close-central-import]')?.addEventListener('click',()=>importModal.close());importFile?.addEventListener('change',()=>{importSave.disabled=!importFile.files?.length});importSave?.addEventListener('click',async()=>{const file=importFile.files?.[0];if(!file)return;importSave.disabled=true;const response=await fetch('/api/central-data/import',{method:'POST',headers:{'Content-Type':'application/vnd.engineering-platform.epdata+zip','X-EP-Central-Import-Confirmed':'true'},body:file}),result=await response.json();importStatus.textContent=response.ok?translate('configuration.central_data_import_restarting'):result.error||translate('configuration.central_data_import_error.CENTRAL_IMPORT_FAILED');if(response.ok)setTimeout(()=>location.reload(),2500);else importSave.disabled=false});'''


def _file_inbox_section(data_root: Path) -> str:
    """Render File Inbox relocation with the same installation-owned affordance."""
    location = escape(str((data_root / FILE_INBOX_DIRECTORY).resolve()))
    return f'''<section class="configuration-central-database" aria-labelledby="fileInboxHeading"><header class="configuration-central-database__header"><div><h2 id="fileInboxHeading" data-i18n="configuration.file_inbox">File Inbox</h2><p data-i18n="configuration.file_inbox_relocation_help">Verplaats de File Inbox alleen wanneer deze leeg is.</p></div></header><dl class="configuration-central-database__facts"><div class="configuration-central-database__location"><dt class="label" data-i18n="configuration.file_inbox_location">File Inbox-locatie</dt><dd class="configuration-central-database__location-value"><button class="configuration-central-database__location-link local-folder-link" type="button" data-local-path="{location}">{location}</button><button class="configuration-central-database__relocate" id="fileInboxRelocate" type="button" data-i18n="configuration.relocate_file_inbox">Verplaats File Inbox</button></dd></div></dl></section><dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation installation-relocation-modal" id="fileInboxRelocateModal"><section class="dashboard-modal-shell__panel"><header class="dashboard-modal-shell__header"><h2 data-modal-glyph="relocate" data-i18n="configuration.relocate_file_inbox">Verplaats File Inbox</h2><button class="dashboard-modal-shell__close" type="button" aria-label="Close" data-close-relocation="fileInboxRelocateModal">×</button></header><p data-i18n="configuration.file_inbox_relocation_help">Verplaats de File Inbox alleen wanneer deze leeg is.</p><dl class="installation-relocation-modal__locations"><div><dt data-i18n="configuration.current_folder">Huidige map</dt><dd><button class="local-folder-link" type="button" data-local-path="{location}">{location}</button></dd></div><div id="fileInboxRelocateDestination" hidden><dt data-i18n="configuration.new_folder">Nieuwe map</dt><dd id="fileInboxRelocateDestinationValue"></dd></div></dl><input id="fileInboxRelocateDirectory" type="hidden"><div class="dashboard-modal-shell__actions"><button class="dashboard-modal-shell__action" id="fileInboxRelocateBrowse" type="button" data-i18n="configuration.choose_folder">Kies map</button><button class="dashboard-modal-shell__action dashboard-modal-shell__action--primary" id="fileInboxRelocateSave" type="button" disabled data-i18n="configuration.relocate">Verplaatsen</button></div><p id="fileInboxRelocateStatus" role="status"></p></section></dialog>'''


def _file_inbox_relocation_script() -> str:
    return '''const inboxModal=document.getElementById('fileInboxRelocateModal'),inboxOpen=document.getElementById('fileInboxRelocate'),inboxInput=document.getElementById('fileInboxRelocateDirectory'),inboxDestination=document.getElementById('fileInboxRelocateDestination'),inboxDestinationValue=document.getElementById('fileInboxRelocateDestinationValue'),inboxSave=document.getElementById('fileInboxRelocateSave'),inboxStatus=document.getElementById('fileInboxRelocateStatus'),showInboxDestination=value=>{inboxInput.value=value;inboxDestinationValue.replaceChildren(window.__engineeringPlatformLocalFilesystemLink(value));inboxDestination.hidden=false;inboxSave.disabled=false;};inboxOpen?.addEventListener('click',()=>inboxModal.showModal());inboxModal?.querySelector('[data-close-relocation]')?.addEventListener('click',()=>inboxModal.close());document.getElementById('fileInboxRelocateBrowse')?.addEventListener('click',async()=>{const r=await fetch('/api/configuration/file-inbox/relocate/browse',{method:'POST'}),p=await r.json();if(p.value)showInboxDestination(p.value);});inboxSave?.addEventListener('click',async()=>{const r=await fetch('/api/configuration/file-inbox/relocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:inboxInput.value})}),p=await r.json();inboxStatus.textContent=r.ok?window.__engineeringPlatformDashboardTranslate('configuration.relocation_restarting'):p.error||window.__engineeringPlatformDashboardTranslate('configuration.relocation_failed');if(r.ok)setTimeout(()=>location.reload(),2500);});'''


def _console_project_boundary(project_id: str, options: str) -> str:
    """Bind CENTRAL selector options and request scope to a dashboard document.

    The historical dashboard initially renders its own selector.  Replacing
    those options happens after the generic visual picker is initialized, so
    the explicit event is the boundary contract that keeps the two controls
    synchronized without exposing CENTRAL details to dashboard internals.
    """
    return '''<script>
(() => {
  window.ENGINEERING_PLATFORM_CENTRAL_CONSOLE = true;
  const project = $PROJECT;
  const options = $OPTIONS;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-Engineering-Platform-Project', project);
    return nativeFetch(input, { ...init, headers });
  };
  const NativeEventSource = window.EventSource;
  window.EventSource = function(url, config) {
    const target = new URL(url, window.location.href);
    target.searchParams.set('project', project);
    return new NativeEventSource(target, config);
  };
  window.EventSource.prototype = NativeEventSource.prototype;
  window.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('dashboardProject');
    if (!select) return;
    select.innerHTML = options;
    select.value = project;
    select.dispatchEvent(new Event('dashboard-select-options-changed', { bubbles: true }));
    select.addEventListener('change', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('project', select.value);
      window.location.assign(url);
    });
    $CENTRAL_DATABASE_SCRIPT
  });
})();
</script>'''.replace("$PROJECT", json.dumps(project_id)).replace("$OPTIONS", json.dumps(options)).replace(
        "$CENTRAL_DATABASE_SCRIPT", _central_database_script(),
    )


def _no_project_console_document(projects: list[dict[str, str]], data_root: Path) -> bytes:
    """Render global Console controls without selecting project-owned data."""
    document = server_console_services.render_console_document(
        "EP Operations",
        workspace_id="none",
        project_name="<geen>",
        workspace_location="",
        configuration_inbox="",
    )
    options = _console_project_options(None, projects)
    selector = f'''<label class="dashboard-project" for="dashboardProject"><span data-i18n="project.label"></span><select id="dashboardProject" data-i18n-aria-label="project.label">{options}</select></label>'''
    boundary = '''<script>window.ENGINEERING_PLATFORM_CENTRAL_CONSOLE=true;window.ENGINEERING_PLATFORM_NO_PROJECT=true;window.addEventListener('DOMContentLoaded',()=>{const select=document.getElementById('dashboardProject');if(select)select.addEventListener('change',()=>{const url=new URL(window.location.href);if(select.value)url.searchParams.set('project',select.value);else url.searchParams.delete('project');window.location.assign(url)});$CENTRAL_DATABASE_SCRIPT});</script>'''.replace("$CENTRAL_DATABASE_SCRIPT", _central_database_script())
    empty_state = '''<aside class="dashboard-status-banner dashboard-status-banner--no-project" id="noProjectSelected" role="status" aria-live="polite" data-testid="no-project-selected"><strong data-i18n="central.no_project_selected_title"></strong><span data-i18n="central.no_project_selected_body"></span><button class="no-project-selected__dismiss" id="noProjectSelectedDismiss" type="button" data-i18n-aria-label="action.close" data-i18n-title="action.close"><span aria-hidden="true">×</span></button></aside>'''
    scoped_style = '''<style>
body[data-project-id="none"] #queueItems,
body[data-project-id="none"] #promptHistory,
body[data-project-id="none"] #currentRun,
body[data-project-id="none"] #technicalDetails,
body[data-project-id="none"] #workspaceCard { display: none !important; }
</style>'''
    document = re.sub(
        br'<body data-project-id="[^"]*" data-project-name="[^"]*">',
        b'<body data-project-id="none" data-project-name="&lt;geen&gt;">',
        document,
        count=1,
    )
    document = document.replace(
        b'<label class="dashboard-locale"',
        selector.encode("utf-8") + b'<label class="dashboard-locale"',
        1,
    )
    document = document.replace(b'<pre></pre>', b'<pre data-i18n="format.not_available"></pre>', 1)
    # Keep the unscoped explanation in the sticky header.  It is operational
    # context, not a project card that should scroll away with the dashboard.
    document = document.replace(
        b'<aside class="dashboard-status-banner dashboard-status-banner--usage-limit"',
        empty_state.encode("utf-8")
        + b'<aside class="dashboard-status-banner dashboard-status-banner--usage-limit"',
        1,
    )
    document = document.replace(
        b'<main class="dashboard-grid"',
        boundary.encode("utf-8") + b'<main class="dashboard-grid"',
        1,
    )
    document = document.replace(
        b'<p class="category-description" data-i18n="description.configuration"></p>',
        b'<p class="category-description" data-i18n="description.configuration"></p>' + _central_database_section(data_root).encode("utf-8"),
        1,
    )
    return document.replace(b"</head>", scoped_style.encode("utf-8") + b"</head>", 1)


def _selected_project_console_document(project_id: str, projects: list[dict[str, str]], data_root: Path) -> bytes:
    """Render the installed Console shell without loading a project checkout."""
    document = server_console_services.render_console_document(
        "EP Operations", workspace_id=project_id, project_name=project_id,
        workspace_location="",
        configuration_inbox="",
    )
    options = _console_project_options(project_id, projects)
    selector = f'''<label class="dashboard-project" for="dashboardProject"><span data-i18n="project.label"></span><select id="dashboardProject" data-i18n-aria-label="project.label">{options}</select></label>'''
    document = document.replace(
        b'<label class="dashboard-locale"', selector.encode("utf-8") + b'<label class="dashboard-locale"', 1,
    )
    document = document.replace(b'<pre></pre>', b'<pre data-i18n="central.project_workspace_not_authority"></pre>', 1)
    document = document.replace(
        b'<p class="category-description" data-i18n="description.configuration"></p>',
        b'<p class="category-description" data-i18n="description.configuration"></p>' + _central_database_section(data_root).encode("utf-8"), 1,
    )
    # A project selection establishes CENTRAL scope; it is not a local
    # workspace binding.  The generic historical template still contains a
    # checkout/branch/worktree card, which has no authoritative Server data
    # and no supported action route.  Execution-bound checkout evidence stays
    # in ``#executionContext`` and is deliberately not part of this removal.
    document = re.sub(
        br'<dialog class="dashboard-modal-shell dashboard-modal-shell--confirmation confirmation-modal" id="workspaceBranchMainResultModal".*?</dialog>\n',
        b"",
        document,
        count=1,
        flags=re.DOTALL,
    )
    document = re.sub(
        br'<details class="card card--context workspace-card" id="workspaceCard".*?</details>\n',
        b"",
        document,
        count=1,
        flags=re.DOTALL,
    )
    return document.replace(b"</main>", _console_project_boundary(project_id, options).encode("utf-8") + b"</main>", 1)


_CONSOLE_STATIC_ASSETS = {
    "/assets/dashboard_translation.mjs": ("dashboard_translation.mjs", "text/javascript; charset=utf-8"),
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/assets/dashboard_locales.mjs": ("dashboard_locales.mjs", "text/javascript; charset=utf-8"),
    "/assets/dashboard_status_store.mjs": ("dashboard_status_store.mjs", "text/javascript; charset=utf-8"),
    "/assets/operations-console/icon-dark.png": ("operations-console/icon-dark.png", "image/png"),
    "/assets/operations-console/icon-light.png": ("operations-console/icon-light.png", "image/png"),
    "/assets/operations-console/icon-transparent.png": ("operations-console/icon-transparent.png", "image/png"),
    "/assets/operations-console/apple-touch-icon-dark.png": (console_presentation.APP_ICON_DARK, "image/png"),
    "/assets/operations-console/apple-touch-icon-light.png": (console_presentation.APP_ICON_LIGHT, "image/png"),
    "/assets/operations-console/manifest.webmanifest": (console_presentation.WEB_MANIFEST, "application/manifest+json; charset=utf-8"),
    "/favicon.ico": (console_presentation.APP_ICON_DARK, "image/png"),
    "/apple-touch-icon.png": (console_presentation.APP_ICON_DARK, "image/png"),
    "/apple-touch-icon-precomposed.png": (console_presentation.APP_ICON_DARK, "image/png"),
}


def _no_project_platform_projection(data_root: Path) -> dict[str, object]:
    """Return a checkout-free platform projection for the ``<geen>`` view.

    This deliberately has no project fallback.  It uses only installed Server
    state and CENTRAL metadata, so rendering a Console before a checkout is
    bound is a supported operation.
    """
    return {
        "scope": "PLATFORM",
        "server": status(data_root),
        "central_database": central_database.details(data_root),
        "capacity_configuration": central_database.capacity_configuration(data_root),
    }


def _authenticated_consumer_scope(
    connection: sqlite3.Connection, token: object, *, recovery_operation_id: str | None = None,
) -> tuple[str, str] | None:
    """Resolve credential identity under an explicit authorization purpose.

    Recovery candidates are never production credentials.  They authenticate
    only for their exact active operation when the dedicated probe supplies
    its operation ID; every normal producer and operator path excludes them.
    """
    if not isinstance(token, str) or not token or len(token) > 4096:
        return None
    recovery_schema_available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ep_consumer_credential_recovery_operations'"
    ).fetchone() is not None
    if recovery_operation_id is not None:
        if not recovery_schema_available:
            return None
        row = connection.execute("""
            SELECT c.consumer_id,c.project_id FROM ep_consumer_credentials c
            JOIN ep_consumer_registrations r
              ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id
            JOIN ep_consumer_credential_recovery_operations o
              ON o.credential_id=c.credential_id
             AND o.consumer_id=c.consumer_id AND o.project_id=c.project_id
            WHERE c.verifier=? AND c.revoked_at IS NULL
              AND c.expires_at>CURRENT_TIMESTAMP AND r.status='ACTIVE'
              AND o.operation_id=? AND o.state='CENTRAL_ACTIVATED'
              AND o.credential_fingerprint=lower(hex(c.fingerprint))
        """, (verifier(token), recovery_operation_id)).fetchone()
    elif recovery_schema_available:
        row = connection.execute("""
            SELECT c.consumer_id,c.project_id FROM ep_consumer_credentials c
            JOIN ep_consumer_registrations r
              ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id
            WHERE c.verifier=? AND c.revoked_at IS NULL
              AND (c.expires_at IS NULL OR c.expires_at>CURRENT_TIMESTAMP)
              AND r.status='ACTIVE'
              AND c.credential_id NOT LIKE ?
              AND NOT EXISTS (
                SELECT 1 FROM ep_consumer_credential_recovery_operations o
                WHERE o.credential_id=c.credential_id
                  AND o.state IN ('CENTRAL_ACTIVATED','STOPPED_UNCERTAIN')
              )
            """, (verifier(token), f"{owner_credential_recovery.RECOVERY_CANDIDATE_PREFIX}%")).fetchone()
    else:
        # Older migration boundaries cannot contain operation-linked recovery
        # candidates.  Keep their credential transfer testable while still
        # rejecting the reserved candidate namespace defense-in-depth.
        row = connection.execute("""
            SELECT c.consumer_id,c.project_id FROM ep_consumer_credentials c
            JOIN ep_consumer_registrations r
              ON r.consumer_id=c.consumer_id AND r.project_id=c.project_id
            WHERE c.verifier=? AND c.revoked_at IS NULL
              AND (c.expires_at IS NULL OR c.expires_at>CURRENT_TIMESTAMP)
              AND r.status='ACTIVE'
              AND c.credential_id NOT LIKE ?
        """, (verifier(token), f"{owner_credential_recovery.RECOVERY_CANDIDATE_PREFIX}%")).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _authenticated_consumer(connection: sqlite3.Connection, token: object, project_id: str) -> str | None:
    """Authenticate an existing scoped CENTRAL consumer credential."""
    scope = _authenticated_consumer_scope(connection, token)
    return scope[0] if scope is not None and scope[1] == project_id else None


def _operator_capability(connection: sqlite3.Connection, token: object, project_id: str, capability: str) -> str | None:
    """Resolve an explicit project capability; admission credentials are insufficient."""
    actor = _authenticated_consumer(connection, token, project_id)
    if actor is None:
        return None
    grant = connection.execute("SELECT 1 FROM ep_operator_capabilities WHERE consumer_id=? AND project_id=? AND capability=? AND revoked_at IS NULL", (actor, project_id, capability)).fetchone()
    return actor if grant is not None else ""


def _same_origin(headers: Mapping[str, str]) -> bool:
    """Accept only an absent or same-host HTTP(S) browser origin.

    Origin is CSRF protection, never an authentication substitute.  The
    capability check at the mutation boundary remains authoritative.
    """
    origin, host = headers.get("Origin", "") or "", headers.get("Host", "") or ""
    return origin in {"", f"http://{host}", f"https://{host}"}


def _strict_json_object(raw: bytes) -> dict[str, object]:
    """Decode one JSON object while rejecting duplicate member names."""
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _admit_server_owned_file_inbox(
    data_root: Path, envelope: dict[str, object], receipt_id: str, received_at: str,
) -> dict[str, object]:
    """Use the canonical application service for the Server's File Inbox child.

    The adapter bypasses only external consumer authentication. Request
    parsing, project/repository scope, execution-mode validation, idempotency,
    admission and lifecycle initialization remain owned by ``submission_service``.
    """
    project_id = envelope.get("project_id")
    submission = envelope.get("submission")
    if not isinstance(project_id, str) or not isinstance(submission, Mapping):
        raise file_inbox.FileInboxError("MALFORMED_FILE")
    payload = dict(submission)
    payload["idempotency_key"] = receipt_id
    payload["transport_receipt_id"] = receipt_id
    payload["transport_received_at"] = received_at
    constraints = payload.get("constraints")
    if constraints is None:
        payload["constraints"] = {"transport_principal": "FILE_INBOX"}
    elif isinstance(constraints, Mapping):
        payload["constraints"] = {**constraints, "transport_principal": "FILE_INBOX"}
    try:
        with storage.sqlite_connection(data_root / SERVER_DATABASE_FILENAME) as connection:
            request = submission_service.request_from_mapping(project_id, payload, transport="FILE_INBOX")
            return submission_service.submit(connection, request).to_dict()
    except submission_service.SubmissionError as error:
        raise file_inbox.FileInboxError(error.code) from error
    except (OSError, sqlite3.Error) as error:
        # A Server/database interruption is delivery availability, never an
        # execution failure. The physical item remains in ``processing`` for
        # the bounded File Inbox retry loop.
        raise URLError("CENTRAL_UNAVAILABLE") from error


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def _status(self) -> dict[str, object]:
        report = status(self.server.data_root)  # type: ignore[attr-defined]
        worker = getattr(self.server, "lifecycle_worker", None)
        if worker is not None:
            report["lifecycle_worker"] = worker.diagnostics().to_dict()
        return report

    def _send(self, status_code: int, payload: dict[str, object], instance_id: str | None = None) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if instance_id:
            self.send_header("EP-Server-Instance", instance_id)
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            # Health probes may abandon a response while the Server finishes
            # rendering it.  The request has no mutation authority; avoid a
            # traceback that obscures qualification diagnostics.
            return

    def _send_text(self, status_code: int, payload: str) -> None:
        """Return one non-JSON Console presentation response safely."""
        encoded = payload.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_download(self, payload: bytes, *, export_format: str, filename: str) -> None:
        """Return one bounded read-only export with fail-closed browser metadata."""
        content_type = _telemetry_export_content_type(export_format)
        content_disposition = _attachment_content_disposition(filename)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", content_disposition)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_artifact_bytes(self, payload: bytes, instance_id: str) -> None:
        """Return the verified immutable artifact bytes without JSON re-encoding."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("EP-Server-Instance", instance_id)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_ndjson(self, entries: list[dict[str, object]]) -> None:
        encoded = ("\n".join(json.dumps(entry, sort_keys=True) for entry in entries) + ("\n" if entries else "")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_console_asset(self, request: SplitResult) -> bool:
        """Serve installed Console assets without selecting a project/root."""
        asset = _CONSOLE_STATIC_ASSETS.get(request.path)
        if asset is None:
            return False
        name, content_type = asset
        try:
            content = (console_presentation.ASSET_DIRECTORY / name).read_bytes()
        except OSError:
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)
        return True

    def _no_project_platform_route(self, method: str, request: SplitResult) -> bool:
        """Serve only explicit platform data when no project is selected.

        Unsupported historical endpoints fail closed.  In particular, this
        method never resolves a local repository binding merely to satisfy an
        old Dashboard helper.
        """
        if method != "do_GET":
            return False
        if request.path == "/api/platform-status":
            self._send(200, _no_project_platform_projection(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path in {"/api/dashboard-snapshot", "/api/status"}:
            self._send(200, _no_project_console_snapshot(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path == "/api/events":
            self._stream_no_project_console_events()
            return True
        if request.path in {"/health", "/api/health"}:
            report = status(self.server.data_root)  # type: ignore[attr-defined]
            self._send(200 if report["healthy"] else 503, report, str(report["instance_id"]))
            return True
        if request.path == "/api/configuration":
            # CENTRAL-only settings presently supported by this phase.  The
            # root-local dashboard configuration is intentionally unavailable.
            self._send(200, _central_console_configuration(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path == "/api/execution-runtime-status":
            self._send(200, _execution_runtime_status())
            return True
        if request.path == "/api/github-rate-limit":
            self._send(200, _github_rate_limit_status())
            return True
        if request.path == "/api/host-admin/diagnostics":
            self._send(200, host_admin.diagnostics(self.server.data_root))  # type: ignore[attr-defined]
            return True
        if request.path == "/api/provider-login-status":
            self._send(200, {"providers": _central_provider_readiness(self.server.data_root)})  # type: ignore[attr-defined]
            return True
        if request.path in {"/api/process-metrics", "/api/usage"}:
            self._send(200, {"scope": "PLATFORM", "available": False})
            return True
        if re.fullmatch(rf"/api/logs/(?:all|{PLATFORM_COMPONENT_ROUTE_PATTERN})", request.path):
            component = request.path.rsplit("/", 1)[-1]
            self._send(200, _central_console_component_logs(self.server.data_root, component) or {"error": "LOG_COMPONENT_UNKNOWN"})  # type: ignore[attr-defined]
            return True
        return False

    def _send_central_database_backup(self) -> None:
        snapshot = central_database.snapshot(self.server.data_root)  # type: ignore[attr-defined]
        if snapshot is None:
            self._send(503, {"error": "CENTRAL_DATABASE_UNAVAILABLE"})
            return
        _audit_dashboard_action(
            self.server.data_root,  # type: ignore[attr-defined]
            action="central_database_backup_downloaded",
        )
        filename = f"engineering-platform-central-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Disposition", _attachment_content_disposition(filename))
        self.send_header("Content-Length", str(len(snapshot)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        route = getattr(self, "_console_route", None)
        if route is not None:
            self.send_header("EP-Console-Route-Owner", route.owner)
        self.end_headers()
        self.wfile.write(snapshot)

    def _send_central_data_export(self) -> None:
        """Export one quiesced, portable snapshot of all durable CENTRAL data."""
        if _central_execution_active(self.server.data_root):  # type: ignore[attr-defined]
            _audit_platform_data_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="EXPORT", outcome="FAILED",
                details={"diagnostic_code": "CENTRAL_DATA_TRANSFER_BLOCKED"},
            )
            self._send(409, {"error": "CENTRAL_DATA_TRANSFER_BLOCKED"})
            return
        with self.server.central_data_transfer_lock:  # type: ignore[attr-defined]
            self.server.central_data_transfer_active = True  # type: ignore[attr-defined]
            try:
                self.server.dependabot_service.stop()  # type: ignore[attr-defined]
                self.server.inbox_service.stop()  # type: ignore[attr-defined]
                self.server.lifecycle_worker.stop()  # type: ignore[attr-defined]
                if _central_execution_active(self.server.data_root):  # type: ignore[attr-defined]
                    raise central_data_transfer.CentralDataTransferError("CENTRAL_DATA_TRANSFER_BLOCKED")
                filename, snapshot = central_data_transfer.export_snapshot(self.server.data_root)  # type: ignore[attr-defined]
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="EXPORT",
                    outcome="COMPLETED",
                    details={"package_format": "EPDATA"},
                )
            except (OSError, sqlite3.DatabaseError, central_data_transfer.CentralDataTransferError) as error:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="EXPORT", outcome="FAILED",
                    details={"diagnostic_code": "CENTRAL_DATA_EXPORT_FAILED"},
                )
                self._send(409, {"error": str(error)})
                return
            finally:
                self.server.lifecycle_worker.start()  # type: ignore[attr-defined]
                self.server.inbox_service.start()  # type: ignore[attr-defined]
                self.server.dependabot_service.start()  # type: ignore[attr-defined]
                self.server.central_data_transfer_active = False  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.engineering-platform.epdata+zip")
        self.send_header("Content-Disposition", _attachment_content_disposition(filename))
        self.send_header("Content-Length", str(len(snapshot)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(snapshot)

    def _central_database_configuration(self, method: str) -> bool:
        request = urlsplit(self.path)
        if request.path == "/api/central-data/export" and method == "do_GET":
            self._send_central_data_export()
            return True
        if request.path == "/api/central-data/relocate/browse" and method == "do_POST":
            try:
                directory = _choose_local_directory(self.server.data_root)  # type: ignore[attr-defined]
                prepared = installation_relocation.prepare(self.server.data_root, directory)  # type: ignore[attr-defined]
            except (ValueError, OSError) as error:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="RELOCATE_PREPARED", outcome="FAILED",
                    details={"diagnostic_code": "PLATFORM_DATA_RELOCATION_PREPARE_FAILED"},
                )
                self._send(400, {"error": str(error)})
            else:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="RELOCATE_PREPARED", outcome="COMPLETED",
                )
                self._send(200, prepared)
            return True
        if request.path == "/api/central-data/relocate/discard" and method == "do_POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 4096 else None
                if not isinstance(payload, dict) or set(payload) != {"directory"}:
                    raise ValueError("PLATFORM_DATA_RELOCATION_BLOCKED")
                installation_relocation.discard_prepared(self.server.data_root, payload["directory"])  # type: ignore[attr-defined]
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="RELOCATE_PREPARATION_DISCARDED", outcome="FAILED",
                    details={"diagnostic_code": "PLATFORM_DATA_RELOCATION_DISCARD_FAILED"},
                )
                self._send(409, {"error": str(error)})
                return True
            _audit_platform_data_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="RELOCATE_PREPARATION_DISCARDED", outcome="COMPLETED",
            )
            self._send(204, {})
            return True
        if request.path == "/api/central-data/relocate" and method == "do_POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 4096 else None
                if not isinstance(payload, dict) or set(payload) != {"directory"} or _central_execution_active(self.server.data_root):  # type: ignore[attr-defined]
                    raise ValueError("PLATFORM_DATA_RELOCATION_BLOCKED")
                result = installation_relocation.request(self.server.data_root, "PLATFORM_DATA", payload["directory"])  # type: ignore[attr-defined]
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="RELOCATE", outcome="FAILED",
                    details={"diagnostic_code": "PLATFORM_DATA_RELOCATION_REQUEST_FAILED"},
                )
                self._send(409, {"error": str(error)})
                return True
            _audit_platform_data_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="RELOCATE", outcome="REQUESTED",
            )
            self._send(202, {**result, "restarting": True})
            self.server.restart_after_shutdown = True  # type: ignore[attr-defined]
            Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
            return True
        if request.path == "/api/central-data/import" and method == "do_POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("X-EP-Central-Import-Confirmed") != "true" or not 0 < length <= central_data_transfer.MAX_ARCHIVE_BYTES or _central_execution_active(self.server.data_root):  # type: ignore[attr-defined]
                    raise ValueError("CENTRAL_IMPORT_BLOCKED")
                imports = self.server.data_root / "runtime" / "central-data-imports"  # type: ignore[attr-defined]
                imports.mkdir(mode=0o700, parents=True, exist_ok=True)
                upload = imports / f"upload-{uuid4().hex}{central_data_transfer.PACKAGE_EXTENSION}"
                with upload.open("wb") as output:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("CENTRAL_IMPORT_UPLOAD_INCOMPLETE")
                        output.write(chunk); remaining -= len(chunk)
                result = central_data_transfer.stage_import(self.server.data_root, upload)  # type: ignore[attr-defined]
                upload.unlink(missing_ok=True)
            except (ValueError, OSError, central_data_transfer.CentralDataTransferError) as error:
                _audit_platform_data_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="IMPORT", outcome="FAILED",
                    details={"diagnostic_code": "CENTRAL_DATA_IMPORT_FAILED"},
                )
                self._send(409, {"error": str(error)})
                return True
            _audit_platform_data_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="IMPORT", outcome="STAGED", details={"package_format": "EPDATA"},
            )
            self._send(202, {**result, "restarting": True})
            self.server.restart_after_shutdown = True  # type: ignore[attr-defined]
            Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
            return True
        if request.path == "/api/central-database/download" and method == "do_GET":
            self._send(410, {"error": "CENTRAL_DATABASE_DOWNLOAD_RETIRED"})
            return True
        if request.path != "/api/central-database/configuration":
            return False
        if method == "do_GET":
            self._send(200, central_database.maintenance_configuration(self.server.data_root))  # type: ignore[attr-defined]
            return True
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            result = central_database.update_maintenance_configuration(
                self.server.data_root, payload.get("interval_seconds"),  # type: ignore[attr-defined]
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            _audit_dashboard_action_rejected(
                self.server.data_root,  # type: ignore[attr-defined]
                action="central_database_maintenance_configuration_changed",
                diagnostic_code="CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID",
            )
            self._send(400, {"error": "CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID"})
            return True
        _audit_configuration_change(
            self.server.data_root,  # type: ignore[attr-defined]
            scope="CENTRAL_DATABASE",
            key="maintenance_interval_seconds",
            previous=result.get("previous", "UNAVAILABLE"),
            value=result.get("interval_seconds", "UNAVAILABLE"),
        )
        self._send(200, result)
        return True

    def _stream_console_events(self, root: Path, project_id: str) -> None:
        """Stream the selected project from CENTRAL only.

        ``root`` is retained temporarily by the route's binding contract but
        is intentionally not read: a checkout cannot become state authority
        merely because it is attached to a selected project.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            # Event delivery is a platform concern even when its snapshot has
            # a selected-project projection.  It must therefore use the same
            # CENTRAL-owned interval as the no-project Console, rather than a
            # repository/root-local Dashboard preference.
            stream_interval = int(
                central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                    "dashboard_stream_interval_seconds"
                ]
            )
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                snapshot = json.dumps(
                    _central_console_project_snapshot(self.server.data_root, project_id),
                    separators=(",", ":"),
                ).encode("utf-8")  # type: ignore[attr-defined]
                if snapshot != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + snapshot + b"\n\n")
                    self.wfile.flush()
                    previous = snapshot
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                interval = int(
                    central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                        "dashboard_stream_interval_seconds"
                    ]
                )
                if interval != stream_interval:
                    self.wfile.write(f"retry: {interval * 1000}\n\n".encode())
                    self.wfile.flush()
                    stream_interval = interval
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_no_project_console_events(self) -> None:
        """Send a CENTRAL-only event that completes the shared Console shell."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("EP-Console-Route-Owner", "PLATFORM")
        self.end_headers()
        try:
            stream_interval = int(
                central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                    "dashboard_stream_interval_seconds"
                ]
            )
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                payload = json.dumps(
                    _no_project_console_snapshot(self.server.data_root), separators=(",", ":")  # type: ignore[attr-defined]
                ).encode("utf-8")
                if payload != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + payload + b"\n\n")
                    self.wfile.flush()
                    previous = payload
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_project_console_events(self, project_id: str) -> None:
        """Keep the selected project's CENTRAL-only dashboard stream alive."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("EP-Console-Route-Owner", "PLATFORM")
        self.end_headers()
        try:
            stream_interval = int(
                central_database.console_interval_configuration(self.server.data_root)[  # type: ignore[attr-defined]
                    "dashboard_stream_interval_seconds"
                ]
            )
            self.wfile.write(f"retry: {stream_interval * 1000}\n\n".encode())
            previous: bytes | None = None
            for iteration in range(300):
                payload = json.dumps(
                    _central_console_project_snapshot(self.server.data_root, project_id),  # type: ignore[attr-defined]
                    separators=(",", ":"),
                ).encode("utf-8")
                if payload != previous:
                    self.wfile.write(b"event: dashboard\ndata: " + payload + b"\n\n")
                    self.wfile.flush()
                    previous = payload
                elif iteration and iteration % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(stream_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _delegate_dashboard(self, method: str) -> None:
        """Route the transitional Console after CENTRAL validates its scope."""
        request = urlsplit(self.path)
        # Resolve ownership once, before any project identity is read.  The
        # header makes the runtime contract observable to browser/integration
        # coverage without granting a selected project any authority.
        self._console_route = console_route_ownership.route_owner(method.removeprefix("do_"), request.path)
        if (
            self._console_route is not None
            and self._console_route.owner == console_route_ownership.HISTORICAL_UNREACHABLE
        ):
            action, diagnostic_code = _retired_console_action_contract(request.path)
            if method == "do_POST":
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action,
                    diagnostic_code=diagnostic_code,
                )
            self._send(410, {"error": diagnostic_code})
            return
        # The export boundary stops all installed writer services. Refuse a
        # concurrent Console mutation as well, rather than claiming a ZIP is
        # a point-in-time snapshot while an HTTP request can still alter it.
        if method == "do_POST" and getattr(self.server, "central_data_transfer_active", False):
            route_component = getattr(self._console_route, "component", "operations")
            _audit_dashboard_action_rejected(
                self.server.data_root,  # type: ignore[attr-defined]
                action=f"{route_component}_action_requested",
                diagnostic_code="CENTRAL_DATA_TRANSFER_IN_PROGRESS",
            )
            self._send(423, {"error": "CENTRAL_DATA_TRANSFER_IN_PROGRESS"})
            return
        if method == "do_GET" and self._send_console_asset(request):
            return
        log_match = re.fullmatch(rf"/api/logs/(all|{PLATFORM_COMPONENT_ROUTE_PATTERN})", request.path)
        if log_match and method == "do_GET":
            query = parse_qs(request.query)
            try:
                payload = _central_console_component_logs(
                    self.server.data_root, log_match.group(1), query,
                    export_all=(query.get("format") or [""])[0] == "ndjson",
                )  # type: ignore[attr-defined]
            except ValueError:
                self._send(400, {"error": "LOG_QUERY_INVALID"})
                return
            if payload is None:
                self._send(404, {"error": "LOG_COMPONENT_UNKNOWN"})
            elif (query.get("format") or [""])[0] == "ndjson":
                self._send_ndjson(list(payload["entries"]))
            else:
                self._send(200, payload)
            return
        if log_match and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"component"} or not isinstance(payload["component"], str):
                    raise ValueError
                result = _clear_central_console_component_logs(self.server.data_root, payload["component"])  # type: ignore[attr-defined]
                if result is None:
                    raise ValueError
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="component_logs_cleared",
                    diagnostic_code="LOG_COMPONENT_INVALID",
                )
                self._send(400, {"error": "LOG_COMPONENT_INVALID"})
            else:
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="component_logs_cleared",
                    details={"log_component": payload["component"], "deleted_count": result["deleted"]},
                )
                self._send(200, result)
            return
        # Platform health is deliberately independent of the browser's
        # selected project preference, so the same components remain visible
        # in both Console modes.
        if method == "do_GET" and request.path in {"/health", "/api/health"}:
            report = status(self.server.data_root)  # type: ignore[attr-defined]
            self._send(200 if report["healthy"] else 503, report, str(report["instance_id"]))
            return
        if method == "do_GET" and request.path == "/api/host-admin/diagnostics":
            # Host Admin has an installation-only root and is intentionally
            # resolved before any selected-project header is inspected.
            self._send(200, host_admin.diagnostics(self.server.data_root))  # type: ignore[attr-defined]
            return
        component_match = re.fullmatch(r"/api/components/([a-z_]+)/details", request.path)
        if component_match and component_match.group(1) not in PLATFORM_COMPONENT_IDS:
            component_match = None
        if method == "do_GET" and component_match:
            detail = _platform_component_detail(self.server.data_root, component_match.group(1))  # type: ignore[attr-defined]
            self._send(200, detail) if detail is not None else self._send(404, {"error": "COMPONENT_UNKNOWN"})
            return
        restart_match = re.fullmatch(r"/api/components/([a-z_]+)/restart", request.path)
        if method == "do_POST" and restart_match:
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                if self.rfile.read(int(self.headers.get("Content-Length", "0"))) != b"{}":
                    raise ValueError
                result = _restart_platform_component(self.server.data_root, restart_match.group(1))  # type: ignore[attr-defined]
            except (ValueError, OSError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="component_restart_requested",
                    diagnostic_code="COMPONENT_RESTART_UNAVAILABLE",
                )
                self._send(409, {"error": "COMPONENT_RESTART_UNAVAILABLE"})
            else:
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="component_restart_requested",
                    details={"target_component": restart_match.group(1)},
                )
                self._send(202, result)
            return
        if self._central_database_configuration(method):
            return
        if request.path == "/api/provider-capacity":
            if method != "do_GET":
                self._send(405, {"error": "METHOD_NOT_ALLOWED"})
            else:
                self._send(200, _provider_capacity_projection(self.server.data_root))  # type: ignore[attr-defined]
            return
        if request.path == "/api/provider-login-status" and method == "do_GET":
            self._send(200, {"providers": _central_provider_readiness(self.server.data_root)})  # type: ignore[attr-defined]
            return
        if request.path == "/api/execution-runtime-status" and method == "do_GET":
            # Validation is an installation capability, independent of the
            # selected project.  Keep it out of the legacy checkout delegate.
            self._send(200, _execution_runtime_status())
            return
        if request.path == "/api/execution-runtime/repair" and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                if self.rfile.read(int(self.headers.get("Content-Length", "0"))) != b"{}":
                    raise ValueError
                runtime = _execution_runtime_status()
                if runtime["state"] != "READY":
                    raise ValueError
            except (ValueError, OSError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="execution_runtime_rechecked",
                    diagnostic_code="EXECUTION_RUNTIME_UNAVAILABLE",
                )
                self._send(409, {"error": "EXECUTION_RUNTIME_UNAVAILABLE"})
                return
            _audit_dashboard_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="execution_runtime_rechecked",
            )
            self._send(200, {"rechecked": True, "runtime": runtime, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-login/repair" and method == "do_POST":
            # Provider installation and interactive sign-in are host-wide
            # operations.  They must never fall through to the historical
            # checkout-bound dashboard handler: on the <geen> projection that
            # handler rejects the request for lack of a selected project and
            # the subsequent readiness refresh misleadingly becomes a check
            # failure.
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                _central_provider_repair(self.server.data_root, payload)  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "PROVIDER_REPAIR_UNAVAILABLE"})
                return
            self._send(202, {"started": True, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-login/logout" and method == "do_POST":
            # Logout is the companion host-wide action to login.  Do not let
            # the installed no-project Console fall through to the retired
            # checkout handler, which rejects it before the CLI can run.
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                _central_provider_logout(self.server.data_root, payload)  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(409, {"error": "PROVIDER_LOGOUT_UNAVAILABLE"})
                return
            self._send(200, {"logged_out": True, "scope": "PLATFORM"})
            return
        if request.path == "/api/provider-capacity/configuration":
            if method == "do_GET":
                self._send(200, central_database.capacity_configuration(self.server.data_root))  # type: ignore[attr-defined]
                return
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
                reserve = payload.get("codex_capacity_reserve_percent") if isinstance(payload, dict) else None
                live = _provider_capacity_projection(self.server.data_root)  # type: ignore[attr-defined]
                remaining = _remaining_rate_limit_capacity(live["rate_limits"])
                if not isinstance(reserve, int) or isinstance(reserve, bool) or (reserve and (remaining is None or reserve > remaining)):
                    raise ValueError
                result = central_database.update_capacity_configuration(self.server.data_root, reserve)  # type: ignore[attr-defined]
                _audit_configuration_change(
                    self.server.data_root,  # type: ignore[attr-defined]
                    scope="PROVIDER_CAPACITY",
                    key="codex_capacity_reserve_percent",
                    previous=result.get("previous", "UNAVAILABLE"),
                    value=result.get("codex_capacity_reserve_percent", "UNAVAILABLE"),
                )
                self._send(200, result)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="provider_capacity_configuration_changed",
                    diagnostic_code="CODEX_CAPACITY_RESERVE_INVALID",
                )
                self._send(409, {"error": "CODEX_CAPACITY_RESERVE_INVALID"})
            return
        if request.path == "/api/configuration" and method == "do_POST":
            try:
                if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                    raise ValueError
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"key", "value", "previous"}:
                    raise ValueError
                result = central_database.update_console_interval_configuration(
                    self.server.data_root, payload["key"], payload["value"],
                )  # type: ignore[attr-defined]
                _audit_configuration_change(
                    self.server.data_root,  # type: ignore[attr-defined]
                    scope="OPERATIONS_CONSOLE",
                    key=str(result["key"]),
                    previous=result.get("previous", "UNAVAILABLE"),
                    value=result.get("value", "UNAVAILABLE"),
                )
                self._send(200, result)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="console_configuration_changed",
                    diagnostic_code="CONSOLE_CONFIGURATION_INVALID",
                )
                self._send(409, {"error": "CONSOLE_CONFIGURATION_INVALID"})
            return
        selected = self.headers.get("X-Engineering-Platform-Project")
        if not selected:
            selected = (parse_qs(request.query).get("project") or [None])[0]
        # Listing projects is a CENTRAL-only operation.  Do not validate or
        # inspect any checkout until a selected project needs a transitional
        # project route below.
        projects = _console_projects(self.server.data_root)  # type: ignore[attr-defined]
        project_ids = {item["project_id"] for item in projects}
        if method == "do_POST" and request.path == "/api/audit/user-action":
            if not _same_origin(self.headers):
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 512:
                    raise ValueError
                payload = _strict_json_object(self.rfile.read(length))
                if set(payload) not in (
                    {"action"}, {"action", "run_id"}, {"action", "target_component"},
                    {"action", "run_id", "target_component"},
                ):
                    raise ValueError
                action = payload.get("action")
                run_id = payload.get("run_id")
                target_component = payload.get("target_component")
                if not isinstance(action, str) or not _DASHBOARD_AUDIT_ACTION_PATTERN.fullmatch(action):
                    raise ValueError
                if run_id is not None and (not isinstance(run_id, str) or not _SAFE_REPORT_ID.fullmatch(run_id)):
                    raise ValueError
                if target_component is not None and target_component not in PLATFORM_COMPONENT_IDS:
                    raise ValueError
                if run_id is not None:
                    with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                        run_project = connection.execute(
                            "SELECT project_id FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
                            (run_id,),
                        ).fetchone()
                    if run_project is None:
                        raise ValueError
                    canonical_project = str(run_project[0])
                    if isinstance(selected, str) and selected not in {canonical_project, ""}:
                        raise ValueError
                else:
                    canonical_project = selected if isinstance(selected, str) and selected in project_ids else None
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action, project_id=canonical_project,
                    run_id=run_id,
                    details={"target_component": target_component} if target_component is not None else None,
                )
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "AUDIT_ACTION_INVALID"})
                return
            self._send(200, {"logged": True})
            return
        if method == "do_GET" and isinstance(selected, str) and selected in project_ids:
            # Slice B: the core project read model is available even when its
            # checkout has been deleted or rebound.  Do this before the
            # transitional handler can resolve a root.
            if request.path == "/":
                document = _selected_project_console_document(selected, projects, self.server.data_root)  # type: ignore[attr-defined]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(document)
                return
            if request.path == "/api/configuration":
                self._send(200, _central_console_configuration(self.server.data_root))  # type: ignore[attr-defined]
                return
            if re.fullmatch(rf"/api/logs/(?:all|{PLATFORM_COMPONENT_ROUTE_PATTERN})", request.path):
                component = request.path.rsplit("/", 1)[-1]
                payload = _central_console_component_logs(self.server.data_root, component)  # type: ignore[attr-defined]
                if payload is None:
                    self._send(404, {"error": "LOG_COMPONENT_UNKNOWN"})
                else:
                    self._send(200, payload)
                return
            if request.path in {"/api/dashboard-snapshot", "/api/status"}:
                self._send(200, _central_console_project_snapshot(self.server.data_root, selected))  # type: ignore[attr-defined]
                return
            if request.path == "/api/execution-diagnostic/current":
                self._send_text(
                    200,
                    _central_console_current_execution_diagnostic(
                        self.server.data_root, selected,  # type: ignore[attr-defined]
                    ) or "",
                )
                return
            if request.path == "/api/prompt-history":
                snapshot = _central_console_project_snapshot(self.server.data_root, selected)  # type: ignore[attr-defined]
                encoded = json.dumps({"runs": snapshot["runs"]}, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)
                return
            report_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/report", request.path)
            if report_match:
                content = _central_console_report(self.server.data_root, selected, report_match.group(1))  # type: ignore[attr-defined]
                if content is None:
                    self._send(404, {"error": "REPORT_NOT_FOUND"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    _report_content_disposition(report_match.group(1)),
                )
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            analysis_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/analysis", request.path)
            if analysis_match:
                content = _central_console_analysis(self.server.data_root, selected, analysis_match.group(1))  # type: ignore[attr-defined]
                if content is None:
                    self._send(404, {"error": "ANALYSIS_NOT_FOUND"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", _report_content_disposition(analysis_match.group(1)))
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            chat_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/chat", request.path)
            if chat_match:
                messages = _central_console_chat_history(self.server.data_root, selected, chat_match.group(1))  # type: ignore[attr-defined]
                if messages is None:
                    self._send(404, {"error": "RUN_NOT_FOUND"})
                else:
                    self._send(200, {"messages": messages, "source": "CENTRAL"})
                return
            detail_match = re.fullmatch(r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/details", request.path)
            if detail_match:
                detail = _central_console_run_detail(self.server.data_root, selected, detail_match.group(1))  # type: ignore[attr-defined]
                if detail is None:
                    self._send(404, {"error": "RUN_NOT_FOUND"})
                else:
                    # The dashboard detail dialog has one stable history
                    # payload contract for both retained and CENTRAL-backed
                    # terminal runs. Returning ``run`` here left its status
                    # field unread and rendered every CENTRAL detail unknown.
                    self._send(200, {
                        "project_id": selected,
                        "history": detail,
                        "execution": detail.get("execution", {}),
                        "runtime": detail.get("runtime", {}),
                        "reviewers": detail.get("reviewers", []),
                        "assurance_reviews": detail.get("assurance_reviews", []),
                        "commits": {},
                        "commit_timeline": detail.get("commit_timeline", []),
                        "pull_requests": detail.get("pull_requests", []),
                        "usage": detail.get("usage", {}),
                        "evidence": detail.get("evidence", []),
                        "lifecycle": detail.get("lifecycle", {}),
                        "source": "CENTRAL",
                    })
                return
            if request.path == "/api/telemetry/export":
                parameters = parse_qs(request.query)
                export_format = (parameters.get("format") or [""])[0]
                prepare = (parameters.get("prepare") or [""])[0] == "1"
                snapshot_id = (parameters.get("snapshot_id") or [None])[0]
                locale = (parameters.get("locale") or ["en"])[0]
                sort_key = (parameters.get("sort") or ["date"])[0]
                direction = (parameters.get("direction") or ["desc"])[0]
                allowed_sort = {
                    "date", "prompt_count", "average_total_execution_seconds",
                    "average_queue_wait_seconds", "input_tokens", "output_tokens",
                    "total_tokens", "complete_count", "blocked_count", "failed_count",
                }
                if (
                    (not prepare and export_format not in {"markdown", "json"})
                    or (prepare and export_format not in {"", "markdown", "json"})
                    or locale not in telemetry_export.SUPPORTED_LOCALES
                    or sort_key not in allowed_sort or direction not in {"asc", "desc"}
                    or (snapshot_id is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_id))
                ):
                    self._send(400, {"error": "TELEMETRY_EXPORT_SELECTION_INVALID"})
                    return
                binding = json.dumps({
                    "project_id": selected, "scope": "TELEMETRY_OVERVIEW",
                    "locale": locale, "sort": sort_key, "direction": direction,
                }, sort_keys=True, separators=(",", ":"))
                store = _telemetry_export_store(self.server)
                model = store.read(snapshot_id, binding=binding) if snapshot_id else None
                if snapshot_id and model is None:
                    self._send(409, {"error": "TELEMETRY_EXPORT_SNAPSHOT_UNAVAILABLE"})
                    return
                if model is None:
                    with _telemetry_read_snapshot(self.server.data_root) as (  # type: ignore[attr-defined]
                        read_connection, source_as_of, source_reference,
                    ):
                        model = telemetry_export.overview_model(
                            project_id=selected,
                            rows=_central_console_telemetry(
                                self.server.data_root, selected,  # type: ignore[attr-defined]
                                full=True,
                                _read_connection=read_connection,
                            ),
                            sort_key=sort_key, sort_direction=direction, locale=locale,
                            retention_days=_telemetry_retention_days(read_connection),
                            source_as_of=source_as_of, source_reference=source_reference,
                        )
                    snapshot_id, snapshot_error = _retain_telemetry_export_snapshot(
                        store, model, binding=binding,
                    )
                    if snapshot_error is not None:
                        self._send(snapshot_error[0], {"error": snapshot_error[1]})
                        return
                if prepare:
                    self._send(200, {
                        "snapshot_id": snapshot_id,
                        "expires_in_seconds": telemetry_export.SNAPSHOT_TTL_SECONDS,
                        "selection": model["selection"],
                    })
                    return
                model = telemetry_export.download_model(model)
                markdown = export_format == "markdown"
                payload = telemetry_export.serialize_markdown(model) if markdown else telemetry_export.serialize_json(model)
                self._send_download(
                    payload,
                    export_format=export_format,
                    filename=f"telemetry-overview-{selected}-utc.{('md' if markdown else 'json')}",
                )
                return
            telemetry_export_match = re.fullmatch(
                r"/api/telemetry/([0-9]{4}-[0-9]{2}-[0-9]{2})/export", request.path,
            )
            if telemetry_export_match:
                parameters = parse_qs(request.query)
                export_format = (parameters.get("format") or [""])[0]
                prepare = (parameters.get("prepare") or [""])[0] == "1"
                snapshot_id = (parameters.get("snapshot_id") or [None])[0]
                locale = (parameters.get("locale") or ["en"])[0]
                scope = (parameters.get("scope") or ["UTC_DAY_DETAIL"])[0]
                run_id = (parameters.get("run_id") or [None])[0]
                if (
                    (not prepare and export_format not in {"markdown", "json"})
                    or (prepare and export_format not in {"", "markdown", "json"})
                    or locale not in telemetry_export.SUPPORTED_LOCALES
                    or scope not in {"UTC_DAY_DETAIL", "EP_RUN_ATTEMPT", "EXECUTION_CHAIN"}
                    or (run_id is not None and not _SAFE_REPORT_ID.fullmatch(run_id))
                    or (scope != "UTC_DAY_DETAIL" and run_id is None)
                    or (snapshot_id is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_id))
                ):
                    self._send(400, {"error": "TELEMETRY_EXPORT_SELECTION_INVALID"})
                    return
                export_date = telemetry_export_match.group(1)
                binding = json.dumps({
                    "project_id": selected, "scope": scope, "date": export_date,
                    "run_id": run_id, "locale": locale,
                }, sort_keys=True, separators=(",", ":"))
                store = _telemetry_export_store(self.server)
                model = store.read(snapshot_id, binding=binding) if snapshot_id else None
                if snapshot_id and model is None:
                    self._send(409, {"error": "TELEMETRY_EXPORT_SNAPSHOT_UNAVAILABLE"})
                    return
                if model is None:
                    with _telemetry_read_snapshot(self.server.data_root) as (  # type: ignore[attr-defined]
                        read_connection, source_as_of, source_reference,
                    ):
                        detail = _central_console_telemetry_detail(
                            self.server.data_root, selected, export_date, full=True,  # type: ignore[attr-defined]
                            _read_connection=read_connection,
                        )
                        if detail is None:
                            self._send(404, {"error": "TELEMETRY_NOT_FOUND"})
                            return
                        try:
                            model = telemetry_export.detail_model(
                                project_id=selected, execution_date=export_date, detail=detail,
                                scope=scope, run_id=run_id, locale=locale,
                                source_as_of=source_as_of, source_reference=source_reference,
                            )
                        except ValueError:
                            self._send(404, {"error": "TELEMETRY_EXPORT_RUN_NOT_FOUND"})
                            return
                    snapshot_id, snapshot_error = _retain_telemetry_export_snapshot(
                        store, model, binding=binding,
                    )
                    if snapshot_error is not None:
                        self._send(snapshot_error[0], {"error": snapshot_error[1]})
                        return
                if prepare:
                    self._send(200, {
                        "snapshot_id": snapshot_id,
                        "expires_in_seconds": telemetry_export.SNAPSHOT_TTL_SECONDS,
                        "selection": model["selection"],
                    })
                    return
                model = telemetry_export.download_model(model)
                markdown = export_format == "markdown"
                payload = telemetry_export.serialize_markdown(model) if markdown else telemetry_export.serialize_json(model)
                context = run_id if run_id is not None else export_date
                self._send_download(
                    payload,
                    export_format=export_format,
                    filename=f"telemetry-detail-{selected}-{scope.casefold().replace('_', '-')}-{context}.{('md' if markdown else 'json')}",
                )
                return
            telemetry_match = re.fullmatch(r"/api/telemetry/([0-9]{4}-[0-9]{2}-[0-9]{2})", request.path)
            if telemetry_match:
                detail = _central_console_telemetry_detail(
                    self.server.data_root, selected, telemetry_match.group(1),  # type: ignore[attr-defined]
                )
                if detail is None:
                    self._send(404, {"error": "TELEMETRY_NOT_FOUND"})
                else:
                    self._send(200, detail)
                return
            if request.path == "/api/events":
                self._stream_project_console_events(selected)
                return
        if method == "do_POST" and request.path in {"/api/execution-dismiss", "/api/execution-retry"}:
            action_name = "execution_dismissed" if request.path == "/api/execution-dismiss" else "execution_retry_submitted"
            if self.headers.get("Origin") not in {None, "", f"http://{self.headers.get('Host', '')}"}:
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action_name, diagnostic_code="CONSOLE_PROJECT_UNAVAILABLE",
                )
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            # The preserved execution lifecycle is loaded only when its
            # project-scoped mutation is requested.  Importing the canonical
            # Server must not load retired watcher-era implementation modules.
            from .parity_lifecycle_dispatcher import (
                ParityLifecycleDispatchError,
                dismiss_operator_gate,
                retry_operator_gate,
            )
            run_id: str | None = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 256:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                run_id = payload.get("run_id") if isinstance(payload, dict) and set(payload) == {"run_id"} else None
                if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
                    raise ValueError
                if request.path == "/api/execution-dismiss":
                    result: dict[str, object] = dismiss_operator_gate(
                        self.server.data_root, project_id=selected, run_id=run_id,  # type: ignore[attr-defined]
                    )
                else:
                    result = retry_operator_gate(
                        self.server.data_root, project_id=selected, run_id=run_id,  # type: ignore[attr-defined]
                    ).to_dict()
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action_name, diagnostic_code="INVALID_REQUEST",
                    project_id=selected, run_id=run_id,
                )
                self._send(400, {"error": "INVALID_REQUEST"})
                return
            except ParityLifecycleDispatchError as error:
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action_name, diagnostic_code=str(error),
                    project_id=selected, run_id=run_id,
                )
                self._send(409, {"error": str(error)})
                return
            _audit_dashboard_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action=action_name,
                project_id=selected,
                run_id=run_id,
            )
            self._send(200, result)
            return
        if method == "do_POST" and request.path in {"/api/codex-chat", "/api/codex-chat/clear"}:
            action_name = (
                "ai_chat_transcript_cleared"
                if request.path == "/api/codex-chat/clear"
                else "ai_chat_message_submitted"
            )
            if not _same_origin(self.headers):
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action_name, diagnostic_code="CONSOLE_PROJECT_UNAVAILABLE",
                )
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            run_id: str | None = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 16_000:
                    raise ValueError
                payload = _strict_json_object(self.rfile.read(length))
                if request.path == "/api/codex-chat/clear":
                    if set(payload) != {"run_id"}:
                        raise ValueError
                    run_id = payload.get("run_id")
                    if not isinstance(run_id, str) or not _SAFE_REPORT_ID.fullmatch(run_id):
                        raise ValueError
                    if not _central_console_clear_chat_history(self.server.data_root, selected, run_id):  # type: ignore[attr-defined]
                        _audit_dashboard_action_rejected(
                            self.server.data_root,  # type: ignore[attr-defined]
                            action=action_name, diagnostic_code="CHAT_CONTEXT_UNAVAILABLE",
                            project_id=selected, run_id=run_id,
                        )
                        self._send(404, {"error": "CHAT_CONTEXT_UNAVAILABLE"})
                        return
                    _audit_dashboard_action(
                        self.server.data_root,  # type: ignore[attr-defined]
                        action="ai_chat_transcript_cleared", project_id=selected, run_id=run_id,
                        details={"chat_content": "[REDACTED]"},
                    )
                    self._send(200, {"cleared": True, "source": "CENTRAL"})
                    return
                if set(payload) != {"message", "run_id"}:
                    raise ValueError
                run_id, message = payload.get("run_id"), payload.get("message")
                if (
                    not isinstance(run_id, str) or not _SAFE_REPORT_ID.fullmatch(run_id)
                    or not isinstance(message, str) or not message.strip() or len(message) > 2_000
                ):
                    raise ValueError
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="ai_chat_message_submitted", project_id=selected, run_id=run_id,
                    details={"chat_content": "[REDACTED]"},
                )
                answer, messages = _central_console_chat_response(
                    self.server.data_root, selected, run_id, message,  # type: ignore[attr-defined]
                )
            except CodexChatError as error:
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="ai_chat_response_failed", project_id=selected,
                    run_id=run_id,
                    details={"chat_content": "[REDACTED]", "failure_code": error.code},
                    outcome="FAILED",
                )
                self._send(503, {"error": "AI_CHAT_UNAVAILABLE"})
                return
            except (OSError, sqlite3.DatabaseError):
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="ai_chat_response_failed", project_id=selected, run_id=run_id,
                    details={"chat_content": "[REDACTED]", "failure_code": "CENTRAL_STORAGE_UNAVAILABLE"},
                    outcome="FAILED",
                )
                self._send(503, {"error": "AI_CHAT_UNAVAILABLE"})
                return
            except ValueError as error:
                if str(error) == "CHAT_CONTEXT_UNAVAILABLE":
                    _audit_dashboard_action(
                        self.server.data_root,  # type: ignore[attr-defined]
                        action="ai_chat_response_failed", project_id=selected, run_id=run_id,
                        details={"chat_content": "[REDACTED]", "failure_code": "CHAT_CONTEXT_UNAVAILABLE"},
                        outcome="FAILED",
                    )
                    self._send(404, {"error": "CHAT_CONTEXT_UNAVAILABLE"})
                    return
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action=action_name, diagnostic_code="INVALID_CHAT_REQUEST",
                    project_id=selected, run_id=run_id,
                )
                self._send(400, {"error": "INVALID_CHAT_REQUEST"})
                return
            _audit_dashboard_action(
                self.server.data_root,  # type: ignore[attr-defined]
                action="ai_chat_response_received", project_id=selected, run_id=run_id,
                details={"chat_content": "[REDACTED]", "model": chat_model()},
            )
            self._send(200, {"answer": answer, "model": chat_model(), "messages": messages, "source": "CENTRAL"})
            return
        if method == "do_POST" and request.path == "/api/queue-disposition":
            if not _same_origin(self.headers):
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="queue_disposition_changed", diagnostic_code="CONSOLE_PROJECT_UNAVAILABLE",
                )
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 1024:
                    raise ValueError
                payload = _strict_json_object(self.rfile.read(length))
                expected = {"contract_version", "operation_id", "submission_id", "expected_state", "expected_revision", "disposition", "reason"}
                if (not isinstance(payload, dict) or set(payload) != expected
                        or payload.get("contract_version") != "1.0"
                        or not all(isinstance(payload.get(field), str) for field in ("operation_id", "submission_id", "expected_state", "disposition", "reason"))
                        or not isinstance(payload.get("expected_revision"), int) or isinstance(payload.get("expected_revision"), bool)):
                    raise ValueError
                with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    connection.execute("BEGIN IMMEDIATE")
                    token = self.headers.get("Authorization", "")[7:] if self.headers.get("Authorization", "").startswith("Bearer ") else None
                    capability = "QUEUE_DECLINE" if payload["disposition"] == "DECLINED" else "QUEUE_HOLD_RESUME"
                    actor = _operator_capability(connection, token, selected, capability)
                    if actor is None:
                        raise submission_service.SubmissionError("UNAUTHENTICATED", 401)
                    if not actor:
                        raise submission_service.SubmissionError("OPERATOR_CAPABILITY_REQUIRED", 403)
                    result = submission_service.operator_queue_disposition(
                        connection, project_id=selected, submission_id=payload["submission_id"],
                        disposition=payload["disposition"], reason=payload["reason"],
                        expected_state=payload["expected_state"], expected_revision=payload["expected_revision"],
                        operation_id=payload["operation_id"], actor_reference=actor,
                    )
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="queue_disposition_changed",
                    project_id=selected,
                    details={
                        "submission_id": payload["submission_id"],
                        "queue_disposition": payload["disposition"],
                        "operation_id": payload["operation_id"],
                        "operator_reference": actor,
                    },
                )
                self._send(200, result)
            except submission_service.SubmissionError as error:
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="queue_disposition_changed", diagnostic_code=error.code,
                    project_id=selected,
                )
                self._send(error.status, {"error": error.code})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _audit_dashboard_action_rejected(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="queue_disposition_changed", diagnostic_code="INVALID_REQUEST",
                    project_id=selected,
                )
                self._send(400, {"error": "INVALID_REQUEST"})
            return
        if method == "do_POST" and request.path == "/api/dashboard-translate":
            if not _same_origin(self.headers):
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if not isinstance(selected, str) or selected not in project_ids:
                self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 2 <= length <= 32_768:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"locale", "texts"}:
                    raise ValueError
                translations = dashboard_translation.translate(payload["locale"], payload["texts"])
            except dashboard_translation.DashboardTranslationError as error:
                status_code = 400 if str(error).endswith(("LOCALE_INVALID", "REQUEST_INVALID")) else 503
                self._send(status_code, {"error": str(error)})
                return
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._send(400, {"error": "DASHBOARD_TRANSLATION_REQUEST_INVALID"})
                return
            self._send(200, {"translations": translations})
            return
        if isinstance(selected, str) and selected in project_ids:
            analysis_retry_match = re.fullmatch(
                r"/api/prompt-history/([a-z0-9][a-z0-9-]{0,63})/analysis-retry",
                request.path,
            )
            if method == "do_POST" and analysis_retry_match:
                if not _same_origin(self.headers):
                    self._send(403, {"error": "INVALID_ORIGIN"})
                    return
                try:
                    if self.rfile.read(int(self.headers.get("Content-Length", "0"))) != b"{}":
                        raise ValueError("INVALID_REQUEST")
                    content = _retry_central_console_analysis(
                        self.server.data_root,  # type: ignore[attr-defined]
                        selected,
                        analysis_retry_match.group(1),
                    )
                except ValueError:
                    _audit_dashboard_action_rejected(
                        self.server.data_root,  # type: ignore[attr-defined]
                        action="prompt_history_analysis_regenerated",
                        diagnostic_code="ANALYSIS_RETRY_UNAVAILABLE",
                        project_id=selected, run_id=analysis_retry_match.group(1),
                    )
                    self._send(409, {"error": "ANALYSIS_RETRY_UNAVAILABLE"})
                    return
                except (OSError, sqlite3.DatabaseError, storage.EngineeringStorageError):
                    _audit_dashboard_action_rejected(
                        self.server.data_root,  # type: ignore[attr-defined]
                        action="prompt_history_analysis_regenerated",
                        diagnostic_code="ANALYSIS_RETRY_FAILED",
                        project_id=selected, run_id=analysis_retry_match.group(1),
                    )
                    self._send(503, {"error": "ANALYSIS_RETRY_FAILED"})
                    return
                _audit_dashboard_action(
                    self.server.data_root,  # type: ignore[attr-defined]
                    action="prompt_history_analysis_regenerated",
                    project_id=selected,
                    run_id=analysis_retry_match.group(1),
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            # No supported CENTRAL Console route may fall through to the
            # retained dashboard handler.  New routes must be added above
            # with an explicit Server/CENTRAL authority classification.
            self._send(404 if method == "do_GET" else 405, {"error": "CENTRAL_CONSOLE_ROUTE_UNAVAILABLE"})
            return
        if method == "do_GET" and request.path == "/" and selected in {None, ""}:
            # No selection is a valid view.  It renders only the host-wide
            # controls and never substitutes the first project for content.
            document = _no_project_console_document(projects, self.server.data_root)  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(document)
            return
        if selected in {None, ""}:
            if self._no_project_platform_route(method, request):
                return
            self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
            return
        if not isinstance(selected, str) or selected not in project_ids:
            self._send(409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"})
            return
        # Reaching this point would mean a route escaped the explicit Console
        # projection table above. Never restore the historical root delegate.
        self._send(404 if method == "do_GET" else 405, {"error": "CENTRAL_CONSOLE_ROUTE_UNAVAILABLE"})

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path in {HTTP_JSON_OPENAPI_PATH, "/openapi.json", "/swagger.json"}:
            self._send(200, _http_json_openapi_document())
            return
        if request.path == "/v1/owner-credential-recovery-probe":
            try:
                if not ip_address(str(self.client_address[0])).is_loopback:
                    self._send(403, {"error": "OWNER_RECOVERY_PROBE_REQUIRES_LOOPBACK"})
                    return
            except ValueError:
                self._send(403, {"error": "OWNER_RECOVERY_PROBE_REQUIRES_LOOPBACK"})
                return
            operation_id = self.headers.get("EP-Recovery-Operation-ID")
            project_id = self.headers.get("EP-Project-ID")
            repository_id = self.headers.get("EP-Repository-ID")
            if not operation_id or not project_id or not repository_id:
                self._send(400, {"error": "INCOMPLETE_RECOVERY_PROBE_SCOPE"})
                return
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else None
            try:
                with storage.sqlite_connection(
                    self.server.data_root / SERVER_DATABASE_FILENAME  # type: ignore[attr-defined]
                ) as connection:
                    consumer_scope = _authenticated_consumer_scope(
                        connection, token, recovery_operation_id=operation_id,
                    )
                    operation = connection.execute(
                        "SELECT instance_id,consumer_id,project_id,repository_id,state "
                        "FROM ep_consumer_credential_recovery_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if consumer_scope is None or operation is None:
                        self._send(401, {"error": "RECOVERY_PROBE_UNAUTHENTICATED"})
                        return
                    identity = initialize(self.server.data_root).instance_id  # type: ignore[attr-defined]
                    if (
                        consumer_scope != (str(operation[1]), str(operation[2]))
                        or str(operation[0]) != identity
                        or str(operation[2]) != project_id
                        or str(operation[3]) != repository_id
                        or str(operation[4]) != "CENTRAL_ACTIVATED"
                    ):
                        self._send(403, {"error": "RECOVERY_PROBE_SCOPE_MISMATCH"})
                        return
                    self._send(200, {
                        "contract_version": "1.0",
                        "instance_id": identity,
                        "operation_id": operation_id,
                        "consumer_id": str(operation[1]),
                        "project_id": str(operation[2]),
                        "repository_id": str(operation[3]),
                        "credential_status": "PENDING_RECOVERY_PROBE",
                        "authorization": "RECOVERY_PROBE_ONLY",
                    }, identity)
                    return
            except sqlite3.Error:
                self._send(503, {"error": "CENTRAL_UNAVAILABLE"})
                return
        if request.path == "/v1/producer-compatibility":
            identity = initialize(self.server.data_root).instance_id  # type: ignore[attr-defined]
            declaration: dict[str, object] = {
                "contract_version": "1.0",
                "producer": {"id": "engineering-platform", "version": CURRENT_PLATFORM_VERSION},
                "instance": {"id": identity},
                "contracts": {
                    "producer_readback": [submission_service.PRODUCER_READBACK_CONTRACT_VERSION],
                    "terminal_evidence": [submission_service.TERMINAL_EVIDENCE_CONTRACT_VERSION],
                },
            }
            project_id = self.headers.get("EP-Project-ID")
            repository_id = self.headers.get("EP-Repository-ID")
            # Preserve the public v1.0 declaration for existing consumers.
            # The normal Forge composition opts into v1.1 by supplying both
            # scope headers; EP then derives identity from the bearer rather
            # than echoing a caller-provided consumer ID.
            if project_id is None and repository_id is None:
                self._send(200, declaration, identity)
                return
            if not project_id or not repository_id:
                self._send(400, {"error": "INCOMPLETE_CONSUMER_SCOPE"})
                return
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else None
            try:
                with storage.sqlite_connection(
                    self.server.data_root / SERVER_DATABASE_FILENAME  # type: ignore[attr-defined]
                ) as connection:
                    consumer_scope = _authenticated_consumer_scope(connection, token)
                    if consumer_scope is None:
                        self._send(401, {"error": "UNAUTHENTICATED"})
                        return
                    consumer_id, authenticated_project_id = consumer_scope
                    if authenticated_project_id != project_id:
                        self._send(403, {"error": "CONSUMER_PROJECT_SCOPE_MISMATCH"})
                        return
                    project = connection.execute(
                        "SELECT status FROM ep_project_registrations WHERE project_id=?",
                        (project_id,),
                    ).fetchone()
                    repository = connection.execute(
                        "SELECT role FROM ep_repository_registrations "
                        "WHERE project_id=? AND repository_id=?",
                        (project_id, repository_id),
                    ).fetchone()
                    local_binding = connection.execute(
                        "SELECT state FROM ep_local_repository_bindings "
                        "WHERE project_id=? AND repository_id=?",
                        (project_id, repository_id),
                    ).fetchone()
                    if (
                        project is None or str(project[0]) != "ACTIVE"
                        or repository is None or str(repository[0]) != "authority"
                        or local_binding is None or str(local_binding[0]) != "BOUND"
                    ):
                        self._send(403, {"error": "REPOSITORY_SCOPE_NOT_AUTHORIZED"})
                        return
                    declaration["contract_version"] = "1.1"
                    declaration["contracts"] = {
                        **declaration["contracts"],
                        "validation_controls": ["1.0", "1.1"],
                        "delivery_revision_validation": ["1.0"],
                        "bounded_merge_delegation": ["1.0"],
                    }
                    declaration["authentication"] = {
                        "consumer_id": consumer_id,
                        "consumer_status": "ACTIVE",
                        "project_id": authenticated_project_id,
                        "project_status": "ACTIVE",
                        "repository_id": repository_id,
                        "repository_role": str(repository[0]),
                        "local_repository_binding": str(local_binding[0]),
                        "submission_authorization": "AUTHORIZED",
                    }
            except sqlite3.Error:
                self._send(503, {"error": "CENTRAL_UNAVAILABLE"})
                return
            self._send(200, declaration, identity)
            return
        if request.path == "/diagnostics/topology":
            try:
                self._send(200, operations_projection(self.server.data_root), initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except ServerConfigurationError:
                self._send(503, {"error": "TOPOLOGY_DIAGNOSTIC_UNAVAILABLE"})
            return
        if self.path == "/v1/operations/projects":
            try:
                self._send(200, operations_projection(self.server.data_root), initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except ServerConfigurationError:
                self._send(503, {"error": "operations projection unavailable"})
            return
        delegation_readback = re.fullmatch(r"/v1/projects/([^/]+)/merge-delegations/([0-9a-f]{32})", request.path)
        if delegation_readback:
            project_id, delegation_id = delegation_readback.groups()
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else None
            try:
                with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    if _authenticated_consumer(connection, token, project_id) is None:
                        self._send(401, {"error": "UNAUTHENTICATED"})
                        return
                    grant = merge_delegation.load(connection, delegation_id)
                    bound = connection.execute(
                        "SELECT b.local_root FROM ep_local_repository_bindings b "
                        "JOIN ep_project_registrations p ON p.project_id=b.project_id AND p.status='ACTIVE' "
                        "JOIN ep_repository_registrations r ON r.repository_id=b.repository_id "
                        "AND r.project_id=b.project_id AND r.role='authority' "
                        "WHERE b.project_id=? AND b.repository_id=? AND b.state='BOUND'",
                        (grant.project_id, grant.repository_id),
                    ).fetchone() if grant is not None else None
                if grant is None or grant.project_id != project_id:
                    self._send(404, {"error": "MERGE_DELEGATION_NOT_FOUND"})
                    return
                try:
                    live_repository = merge_delegation.bound_github_repository(Path(str(bound[0]))) if bound else None
                except ValueError:
                    live_repository = None
                status = ("DRIFT" if live_repository != grant.github_repository else
                          "REVOKED" if grant.revoked_at is not None else
                          "EXPIRED" if datetime.fromisoformat(grant.expires_at) <= datetime.now(timezone.utc) else
                          "RESERVED" if grant.activated_at is None else
                          "ACTIVE" if grant.permits(
                              project_id=grant.project_id, repository_id=grant.repository_id,
                              mission_id=grant.mission_id, mission_revision=grant.mission_revision,
                              role=grant.roles[0], base_branch=grant.base_branch,
                          ) else "EXPIRED")
                self._send(200, {"contract_version": "1.1", "delegation_id": grant.delegation_id,
                                 "actor_reference": grant.actor_reference, "project_id": grant.project_id,
                                 "repository_id": grant.repository_id,
                                 "github_repository": grant.github_repository,
                                 "mission_id": grant.mission_id,
                                 "mission_revision": grant.mission_revision, "base_branch": grant.base_branch,
                                 "roles": list(grant.roles), "expires_at": grant.expires_at,
                                 "activated_at": grant.activated_at, "revoked_at": grant.revoked_at,
                                 "status": status,
                                 "assurance_profile_id": grant.assurance_profile_id,
                                 "assurance_profile_revision": grant.assurance_profile_revision,
                                 "assurance_policy_digest": grant.assurance_policy_digest},
                           initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except sqlite3.Error:
                self._send(503, {"error": "CENTRAL_UNAVAILABLE"})
            return
        readback = re.fullmatch(r"/v1/projects/([^/]+)/submissions/([^/]+)", request.path)
        if readback:
            project_id, submission_id = readback.groups()
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else None
            try:
                with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    if _authenticated_consumer(connection, token, project_id) is None:
                        self._send(401, {"error": "UNAUTHENTICATED"})
                        return
                    projection = submission_service.producer_readback(
                        connection, project_id=project_id, submission_id=submission_id,
                    )
                if projection is None:
                    self._send(404, {"error": "SUBMISSION_NOT_FOUND"})
                else:
                    self._send(200, projection, initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except sqlite3.Error:
                self._send(503, {"error": "CENTRAL_UNAVAILABLE"})
            return
        artifact = re.fullmatch(
            r"/v1/projects/([^/]+)/artifacts/((?:terminal-evidence(?:-reconciled)?|assurance-findings):[^/]+)",
            unquote(request.path),
        )
        if artifact:
            project_id, artifact_id = artifact.groups()
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else None
            try:
                with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    if _authenticated_consumer(connection, token, project_id) is None:
                        self._send(401, {"error": "UNAUTHENTICATED"})
                        return
                    payload = submission_service.producer_evidence_artifact(
                        connection, project_id=project_id, artifact_id=artifact_id,
                    )
                if payload is None:
                    self._send(404, {"error": "EVIDENCE_ARTIFACT_NOT_FOUND"})
                else:
                    self._send_artifact_bytes(payload, initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except (sqlite3.Error, ValueError, json.JSONDecodeError):
                self._send(503, {"error": "CENTRAL_UNAVAILABLE"})
            return
        if request.path == "/" or request.path.startswith("/api/") or request.path.startswith("/assets/") or request.path in {"/health", "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
            self._delegate_dashboard("do_GET")
            return
        if self.path not in {"/healthz", "/readyz"}:
            self.send_error(404)
            return
        try:
            report = self._status()
        except ServerConfigurationError:
            self._send(503, {"healthy": False, "ready": False})
            return
        self._send(200, report, str(report["instance_id"]))

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path.startswith("/api/"):
            self._delegate_dashboard("do_POST")
            return
        if self.path.startswith("/v1/projects/") and self.path.endswith("/submissions"):
            parts = self.path.split("/")
            if len(parts) != 5 or not parts[3]:
                self._send(404, {"error": "not found"})
                return
            project_id = parts[3]
            try:
                if self.headers.get_content_type() != "application/json":
                    raise submission_service.SubmissionError("UNSUPPORTED_MEDIA_TYPE", 415)
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= 131072:
                    raise submission_service.SubmissionError("PAYLOAD_TOO_LARGE", 413)
                raw = self.rfile.read(length)
                if b"\0" in raw:
                    raise submission_service.SubmissionError("MALFORMED_REQUEST")
                payload = json.loads(raw.decode("utf-8"))
                authorization = self.headers.get("Authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else None
                # CLI uses this same authenticated HTTP boundary, but the
                # durable receipt must retain the original adapter.  It is
                # observational provenance only: callers cannot select an
                # execution implementation through this header.
                transport = self.headers.get("EP-Submission-Transport", "HTTP")
                with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                    if _authenticated_consumer(connection, token, project_id) is None:
                        raise submission_service.SubmissionError("UNAUTHENTICATED", 401)
                    request = submission_service.request_from_mapping(project_id, payload, transport=transport)
                    result = submission_service.submit(connection, request)
                if result.receipt is not None:
                    receipt = result.receipt
                    logger = component_logger(
                        self.server.data_root, "http_ingress",  # type: ignore[attr-defined]
                        central_database=self.server.data_root / SERVER_DATABASE_FILENAME,  # type: ignore[attr-defined]
                    )
                    context = {
                        "submission_id": result.submission_id,
                        "project_id": result.project_id,
                        "repository_id": result.repository_id,
                        "forge_application_version": receipt["forge_application_version"],
                        "producer_contract_version": receipt["producer_contract_version"],
                        "forge_provenance_contract_version": receipt["forge_provenance_contract_version"],
                        "receipt_contract_version": receipt["contract_version"],
                        "receipt_id": receipt["id"],
                        "producer_readback_contract_version": receipt["producer_readback_contract_version"],
                        "accepted_request_digest": receipt["accepted_request_digest"],
                        "ep_instance_id": receipt["ep_instance_id"],
                        "ep_application_version": receipt["ep_application_version"],
                    }
                    log_event(logger, logging.INFO, "forge_submission_accepted",
                              context={**context, "exchange_direction": "FORGE_TO_EP"})
                    # This is the server-side issuance fact.  Forge records the
                    # corresponding receipt only after it has received and
                    # strictly bound the HTTP response to its envelope.
                    log_event(logger, logging.INFO, "forge_submission_receipt_issued",
                              context={**context, "exchange_direction": "EP_TO_FORGE"})
                self._send(200, result.to_dict(), initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
            except UnicodeDecodeError:
                self._send(400, {"error": "MALFORMED_REQUEST"})
            except json.JSONDecodeError:
                self._send(400, {"error": "MALFORMED_REQUEST"})
            except submission_service.SubmissionError as error:
                self._send(error.status, {"error": error.code})
            return
        routes = {"/v1/agent/pair": agent_trust.pair, "/v1/agent/register": agent_trust.register, "/v1/agent/heartbeat": agent_trust.heartbeat, "/v1/agent/attachment": agent_trust.register_attachment}
        action = routes.get(self.path)
        if action is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                raise agent_trust.AgentTrustError("request body is invalid")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
            with storage.sqlite_connection(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                result = action(connection, body) if action is agent_trust.pair else action(connection, body, token)
            self._send(200, result, initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
        except (ValueError, OSError, json.JSONDecodeError, agent_trust.AgentTrustError):
            self._send(400 if self.path == "/v1/agent/pair" else 401, {"error": "agent request rejected"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(data_root: Path, *, development: development_profile.DevelopmentProfile | None = None) -> int:
    if central_operational_reset.maintenance_active(data_root):
        raise ServerConfigurationError("EP_OPERATIONAL_MAINTENANCE_ACTIVE")
    relocation = installation_relocation.apply_pending(data_root)
    if relocation is not None:
        data_root = Path(relocation["value"])
    imported = central_data_transfer.apply_pending_import(data_root)
    data_root = data_root.resolve()
    identity = initialize(data_root)
    if relocation is not None:
        _audit_platform_data_action(
            data_root,
            action="RELOCATE",
            outcome="COMPLETED",
            details={
                "previous_location": relocation["previous"],
                "new_location": relocation["value"],
            },
        )
        _audit_configuration_change(
            data_root,
            scope="PLATFORM_DATA",
            key="location",
            previous=relocation["previous"],
            value=relocation["value"],
        )
        # An installed LaunchAgent owns an explicit data-root argument.  Once
        # the audit record is durable, rewrite and reload that owned service
        # rather than leaving a compatibility link at the old data location.
        server_service.repoint_after_relocation(Path(relocation["previous"]), data_root)
    if imported is not None:
        _audit_platform_data_action(
            data_root,
            action="IMPORT",
            outcome="COMPLETED",
            details={
                "package_format": "EPDATA",
                "entry_count": imported["entries"],
                "schema_version": imported["schema_version"],
            },
        )
        _audit_configuration_change(
            data_root, scope="PLATFORM_DATA", key="import", previous="REPLACED",
            value=f"{imported['entries']}_ENTRIES",
        )
    config = ServerConfiguration.load(data_root)
    os.environ[SERVER_ENVIRONMENT_DATA_ROOT] = str(data_root.resolve())
    os.environ[MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT] = config.managed_codex_cli_prefix
    server = http.server.ThreadingHTTPServer((config.bind_host, config.bind_port), _HealthHandler)
    server.data_root = data_root.resolve()  # type: ignore[attr-defined]
    server.central_data_transfer_lock = RLock()  # type: ignore[attr-defined]
    server.central_data_transfer_active = False  # type: ignore[attr-defined]
    server.restart_after_shutdown = False  # type: ignore[attr-defined]
    # Lifecycle composition is intentionally lazy: read-only Server import
    # and Console startup must stay independent of retired watcher modules.
    from .lifecycle_worker import LifecycleWorker

    worker = LifecycleWorker(data_root)
    # The File Inbox is an installed Server child, not a Dashboard or
    # checkout-owned watcher.  Its heartbeat is the source for its platform
    # component health; a prior successful file is never treated as liveness.
    inbox_service = file_inbox.FileInboxService(
        data_root / FILE_INBOX_DIRECTORY,
        admission=lambda envelope, receipt_id, received_at: _admit_server_owned_file_inbox(
            data_root, envelope, receipt_id, received_at,
        ),
    )
    dependabot_service = dependabot_producer.DependabotService(
        data_root,
        event=lambda event, context: log_event(
            component_logger(
                data_root,
                "dependabot_producer",
                central_database=data_root / SERVER_DATABASE_FILENAME,
            ),
            logging.INFO if event == "dependabot_submission_admitted" else logging.WARNING,
            event,
            context=context,
        ),
    )
    server.lifecycle_worker = worker  # type: ignore[attr-defined]
    server.inbox_service = inbox_service  # type: ignore[attr-defined]
    server.dependabot_service = dependabot_service  # type: ignore[attr-defined]
    _write_json(data_root / SERVER_RUNTIME_FILENAME, {"pid": os.getpid(), "instance_id": identity.instance_id, "started_at": _utcnow()})
    def stop(_signum: int, _frame: object) -> None:
        # ``shutdown`` must run outside the serve_forever thread.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    if not worker.wait_until_running():
        worker.stop()
        server.server_close()
        raise RuntimeError("Lifecycle Worker did not become ready.")
    inbox_service.start()
    dependabot_service.start()
    # A fresh installation must have operational evidence before its first
    # submission. These Server lifecycle events share the Console's one
    # CENTRAL component-log authority.
    _record_platform_component_startups(data_root)
    restart_after_shutdown = False
    try:
        server.serve_forever()
    finally:
        dependabot_service.stop()
        inbox_service.stop()
        worker.stop()
        server.server_close()
        (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
        restart_after_shutdown = server.restart_after_shutdown  # type: ignore[attr-defined]
    if restart_after_shutdown:
        arguments = [sys.executable, "-m", "engineering_platform.server", "serve", "--data-root", str(data_root)]
        if development is not None:
            arguments.extend(development.server_arguments())
        os.execv(sys.executable, arguments)
    return 0


def start(data_root: Path, *, development: development_profile.DevelopmentProfile | None = None) -> dict[str, object]:
    current = status(data_root)
    if current["running"]:
        return current
    if central_operational_reset.maintenance_active(data_root):
        raise ServerConfigurationError("EP_OPERATIONAL_MAINTENANCE_ACTIVE")
    # The installed entrypoint supplies the interpreter.  Run from the
    # installation-owned data root and discard Python import overrides so a
    # caller's checkout can never become the child Server's import authority.
    runtime_root = data_root.resolve()
    configuration = ServerConfiguration.load(runtime_root)
    # npm is the preserved managed-runtime installer.  These are fixed host
    # tool directories, never a provider-executable fallback or caller PATH.
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT: configuration.managed_codex_cli_prefix,
        SERVER_ENVIRONMENT_DATA_ROOT: str(runtime_root),
    }
    if home := os.environ.get("HOME"):
        environment["HOME"] = home
    # Unit tests exercise the lifecycle from an unpackaged source tree.  This
    # explicit test-only bridge is never inherited by an installed process.
    if "unittest" in sys.argv[0] or "pytest" in sys.modules:
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    arguments = [sys.executable, "-m", "engineering_platform.server", "serve", "--data-root", str(runtime_root)]
    if development is not None:
        arguments.extend(development.server_arguments())
    child = subprocess.Popen(arguments, cwd=str(runtime_root), env=environment, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603
    _CHILDREN[child.pid] = child
    for _ in range(40):
        time.sleep(0.05)
        current = status(data_root)
        if current["running"]:
            return current
    raise RuntimeError("EP Server did not become ready.")


def stop(data_root: Path) -> dict[str, object]:
    runtime = _runtime(data_root)
    if runtime and _alive(runtime.get("pid")):
        os.kill(int(runtime["pid"]), signal.SIGTERM)
        child = _CHILDREN.pop(int(runtime["pid"]), None)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for _ in range(40):
            time.sleep(0.05)
            if not _alive(runtime["pid"]):
                break
    (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
    return status(data_root)


def _health_response(bind: Mapping[str, object]) -> Mapping[str, object]:
    """Read one server-provided identity response without selecting a runtime."""
    host, port = bind.get("host"), bind.get("port")
    if not isinstance(host, str) or not host or not isinstance(port, int) or not 1 <= port <= 65535:
        raise operational_installation.OperationalInstallationError("operational health endpoint is invalid")
    # The Server computes live component status for this endpoint. A one-second
    # loopback deadline can expire while that healthy computation is finishing.
    with urlopen(f"http://{host}:{port}/health", timeout=3) as response:  # nosec B310
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise operational_installation.OperationalInstallationError("operational health response is invalid")
    return payload


def health(data_root: Path) -> dict[str, object]:
    result = status(data_root)
    if not result["running"]:
        return {**result, "healthy": False, "ready": False}
    bind = result["bind"]
    try:
        # Server configuration permits loopback host only.
        payload = _health_response(bind)
        operational_installation.validate_health(
            operational_installation.OperationalInstallation(
                "", str(data_root.resolve()), str(result["instance_id"]),
                ServerConfiguration.load(data_root).product_version, None, (),
            ), payload,
        )
        return {**result, "healthy": True, "ready": True}
    except (URLError, OSError, ValueError, operational_installation.OperationalInstallationError):
        return {**result, "healthy": False, "ready": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engineering-platform-server", description="Manage the standalone Engineering Platform Server foundation")
    parser.add_argument("command", choices=("init", "start", "serve", "stop", "status", "health", "operational-diagnose", "operational-qualify", "operational-readback", "operational-update-assess", "operational-inventory", "system-service-inventory", "legacy-adoption-inspect", "legacy-adoption-authorize", "installation-update-plan", "installation-update-prepare", "installation-update-admit", "installation-update-apply", "installation-update-resume", "installation-update-status", "owner-consumer-readback", "owner-credential-recover", "owner-credential-recovery-adopt-peer-configuration", "owner-credential-recovery-status", "service-install", "service-uninstall", "relay-install", "relay-uninstall", "pairing-create", "agent-status", "agent-revoke", "agent-reset", "topology", "submission-diagnose", "bootstrap-topology", "register-topology", "provision-declaration", "issue-consumer-credential", "issue-development-consumer-credential", "grant-operator-capability", "revoke-operator-capability", "reserve-merge-delegation", "activate-merge-delegation", "revoke-merge-delegation", "bind-repository", "rebind-repository", "unbind-repository", "resolve-repository", "register-producer-binding", "list-producer-bindings", "deactivate-producer-binding"))
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--runtime-profile", choices=("operational", "development"), default="operational")
    parser.add_argument("--development-venv", type=Path)
    parser.add_argument("--development-credential-reference")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--bind-port", type=int, default=8765)
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--repository-id")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--consumer-id")
    parser.add_argument("--submission-id")
    parser.add_argument("--producer-type")
    parser.add_argument("--external-resource-type")
    parser.add_argument("--external-resource-identity")
    parser.add_argument("--binding-id")
    parser.add_argument("--reason")
    parser.add_argument("--capability", choices=("QUEUE_HOLD_RESUME", "QUEUE_DECLINE"))
    parser.add_argument("--operation-id")
    parser.add_argument("--delegation-id")
    parser.add_argument("--mission-id")
    parser.add_argument("--mission-revision")
    parser.add_argument("--merge-role", action="append", choices=("IMPLEMENTATION", "FINALIZATION", "RECONCILIATION"))
    parser.add_argument("--assurance-profile")
    parser.add_argument("--expires-at")
    parser.add_argument("--expected-instance-id")
    parser.add_argument("--peer-binding-id")
    parser.add_argument("--peer-runtime-id")
    parser.add_argument("--peer-configuration-digest")
    parser.add_argument("--previous-peer-configuration-digest")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--target-version")
    parser.add_argument("--target-digest")
    parser.add_argument("--target-source-revision")
    parser.add_argument("--preserved-wheel", type=Path)
    parser.add_argument("--venv-builder", type=Path)
    parser.add_argument("--acknowledge-unknown-source-revision", action="store_true")
    parser.add_argument("--candidate-interpreter", action="append", type=Path, default=[])
    parser.add_argument("--service-reference", action="append", default=[])
    parser.add_argument("--declared-user-home", action="append", type=Path, default=[])
    return parser


def _installation_update_operational_actions(
    data_root: Path,
) -> installation_update_composition.InstallationUpdateOperationalActions:
    """Supply the real user-service inventory, quiesce and health adapters."""
    root = data_root.resolve()
    paths = server_service.default_paths(root)

    def admitted_source_interpreter(
        plan: installation_update_plan.InstallationUpdatePlan,
    ) -> Path:
        """Resolve the service launcher from the update's admitted source."""
        if plan.legacy_adoption is not None:
            value = plan.legacy_adoption.get("interpreter")
        else:
            try:
                record = operational_installation_record.load(root)
            except operational_installation_record.OperationalInstallationRecordError as error:
                raise ServerConfigurationError("the admitted source installation is unavailable") from error
            if (
                record["installation_id"] != plan.installation_id
                or record["version"] != plan.current_version
                or record["artifact_digest"] != plan.current_digest
            ):
                raise ServerConfigurationError("the admitted source installation changed before quiescence")
            value = record.get("interpreter")
        if not isinstance(value, str):
            raise ServerConfigurationError("the admitted source service interpreter is unavailable")
        expected = Path(value).expanduser()
        if not expected.is_absolute():
            raise ServerConfigurationError("the admitted source service interpreter is invalid")
        return expected.absolute()

    def inventory(plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
        selected = server_service.configured_interpreter(root)
        if selected is None:
            raise ServerConfigurationError("the existing EP user service is unavailable")
        package = operational_installation.package_identity(selected)
        if package.get("version") != plan.current_version:
            raise ServerConfigurationError(
                "the existing EP user service package differs from the admitted source version"
            )
        return {
            "result": "PASS", "service_label": server_service.LABEL,
            "service_interpreter": str(selected), "package_version": package["version"],
            "central": central_database.details(root),
        }

    def quiescence_binding(
        plan: installation_update_plan.InstallationUpdatePlan,
    ) -> tuple[Path, Path]:
        selected = server_service.configured_interpreter(root)
        if selected is None:
            raise ServerConfigurationError("the existing EP user service is unavailable")
        expected = admitted_source_interpreter(plan)
        if selected != expected:
            raise ServerConfigurationError("the EP user service differs from the admitted source interpreter")
        return selected, expected

    def quiesce_preflight(
        plan: installation_update_plan.InstallationUpdatePlan,
    ) -> Mapping[str, object]:
        """Prove the exact loaded service before recording quiescing intent."""
        _selected, expected = quiescence_binding(plan)
        try:
            operation_state = installation_update_operation.status(
                root, plan.operation_id,
            )["state"]
        except (AttributeError, KeyError, installation_update_operation.InstallationUpdateOperationError) as error:
            raise ServerConfigurationError("the installation update quiescence state is unavailable") from error
        if operation_state != "INVENTORIED":
            raise ServerConfigurationError("the installation update cannot prepare quiescence from its current state")
        if not server_service.service_loaded(
            data_root=root, expected_interpreter=expected,
        ):
            raise ServerConfigurationError(
                "the existing EP user service is not loaded before initial quiescence"
            )
        return {
            "result": "PASS", "service_label": server_service.LABEL,
            "state": "LOADED_SERVICE_BOUND", "interpreter": str(expected),
            "data_root": str(root),
        }

    def quiesce(plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
        lifecycle = LaunchdProvider()
        _selected, expected = quiescence_binding(plan)
        try:
            operation_state = installation_update_operation.status(
                root, plan.operation_id,
            )["state"]
        except (AttributeError, KeyError, installation_update_operation.InstallationUpdateOperationError) as error:
            raise ServerConfigurationError("the installation update quiescence state is unavailable") from error
        if operation_state not in {"QUIESCING", "QUIESCED", "BACKED_UP"}:
            raise ServerConfigurationError("the installation update cannot quiesce from its current state")
        # A loaded same-label job must still match the admitted runtime.  True
        # absence is accepted only after the durable QUIESCING intent proves
        # that this update had first observed the exact loaded service.
        server_service.service_loaded(
            data_root=root, expected_interpreter=expected,
        )
        retained = server_service.retain_update_quiescence(
            root, expected_interpreter=expected,
        )
        if not server_service.service_loaded(
            data_root=root, expected_interpreter=expected,
        ):
            return {
                "result": "PASS", "service_label": server_service.LABEL,
                "state": "ALREADY_QUIESCED", "retention": retained["state"],
            }
        try:
            lifecycle.quiesce(server_service.LABEL, paths.plist_path)
        except OSError:
            if server_service.service_loaded(
                data_root=root, expected_interpreter=expected,
            ):
                raise
            return {
                "result": "PASS", "service_label": server_service.LABEL,
                "state": "ALREADY_QUIESCED", "retention": retained["state"],
            }
        if server_service.service_loaded(
            data_root=root, expected_interpreter=expected,
        ):
            raise ServerConfigurationError("the EP user service remained loaded after quiescence")
        return {
            "result": "PASS", "service_label": server_service.LABEL,
            "state": "QUIESCED", "retention": retained["state"],
        }

    def verify(_plan: installation_update_plan.InstallationUpdatePlan) -> Mapping[str, object]:
        selected = server_service.configured_interpreter(root)
        if selected is None:
            raise ServerConfigurationError("activated EP user service is unavailable")
        installation = operational_installation.resolve(root, interpreter=selected)
        package = operational_installation.package_identity(selected)
        registered = operational_installation.record_status(installation)
        configuration = ServerConfiguration.load(root)
        response: Mapping[str, object] | None = None
        for _ in range(40):
            try:
                response = _health_response({"host": configuration.bind_host, "port": configuration.bind_port})
                break
            except (URLError, OSError, ValueError, operational_installation.OperationalInstallationError):
                time.sleep(0.05)
        if response is None:
            raise ServerConfigurationError("activated EP Server health identity is unavailable")
        qualification = operational_installation.qualify_runtime_response(
            installation, record=registered, package=package, response=response,
        )
        return {"result": "PASS", "qualification": qualification}

    return installation_update_composition.InstallationUpdateOperationalActions(
        inventory=inventory, quiesce=quiesce, verify=verify,
        quiesce_preflight=quiesce_preflight,
    )


def _development_profile_for(args: argparse.Namespace) -> development_profile.DevelopmentProfile | None:
    """Resolve the explicit development boundary before any command side effect."""
    development_profile.require_explicit_runtime(args.data_root, args.runtime_profile)
    if args.runtime_profile != "development":
        return None
    development_profile.reject_operational_command(
        args.command,
        service_labels={
            "service-install": server_service.LABEL,
            "service-uninstall": server_service.LABEL,
            "legacy-adoption-authorize": server_service.LABEL,
            "installation-update-prepare": server_service.LABEL,
            "installation-update-admit": server_service.LABEL,
            "installation-update-apply": server_service.LABEL,
            "installation-update-resume": server_service.LABEL,
            "relay-install": server_relay._definition_label(),
            "relay-uninstall": server_relay._definition_label(),
        },
    )
    operational_root = platform_default_data_root()
    # A development profile must not use the legacy per-user LaunchAgent as
    # authority for what counts as the operational interpreter.  The future
    # system-domain descriptor is the only official resolver; a malformed or
    # absent descriptor cannot be replaced by PATH or the legacy service.
    try:
        operational_service = system_server_service.configured_service(operational_root)
    except system_server_service.SystemServerServiceError:
        operational_service = None
    selected = None if operational_service is None else operational_service.interpreter
    inputs = {
        "data_root": args.data_root,
        "bind_port": args.bind_port,
        "development_venv": args.development_venv,
        "credential_reference": args.development_credential_reference,
        "interpreter": Path(sys.executable),
        "operational_data_roots": (operational_root,),
        "operational_interpreters": () if selected is None else (selected,),
        "environment": os.environ,
    }
    if args.command == "init":
        return development_profile.establish(**inputs)
    return development_profile.require(**inputs)


def _operational_inventory_inputs(args: argparse.Namespace) -> dict[str, Path]:
    """Parse only explicit EP diagnostic references, never PATH selection."""
    references: dict[str, Path] = {}
    for raw in args.service_reference:
        label, separator, path = raw.partition("=")
        if not separator or not label or not path or label in references:
            raise ServerConfigurationError("--service-reference must be a unique label=absolute-interpreter path")
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ServerConfigurationError("--service-reference interpreter path must be absolute")
        references[label] = candidate
    return references


def _system_operational_service(
    args: argparse.Namespace,
) -> tuple[system_server_service.SystemServerService | None, Mapping[str, object], str | None]:
    """Resolve only the canonical system-domain Server service.

    The inventory is retained even when the canonical descriptor is absent or
    malformed, so an ``UNKNOWN`` result contains product-owned evidence rather
    than selecting a legacy LaunchAgent, ``sys.executable`` or a PATH wheel.
    A separately read descriptor must agree with the observer's selected
    launcher and data root before it can become an official runtime.
    """
    inventory = system_server_service.machine_scope_inventory(
        args.data_root,
        user_homes=args.declared_user_home,
    )
    if not isinstance(inventory, Mapping):  # defensive against a future observer regression
        raise ServerConfigurationError("EP Server system-service inventory is invalid")
    try:
        # Readback, diagnosis and qualification must have identical evidence
        # admission: a partial observer map cannot select a real descriptor
        # for one command while readback correctly rejects it for another.
        product_installation_readback.validate_system_service_inventory(inventory)
    except product_installation_readback.ProductInstallationReadbackError:
        return None, inventory, "SYSTEM_SERVICE_INVENTORY_MISMATCH"
    try:
        configured = system_server_service.configured_service(args.data_root)
    except system_server_service.SystemServerServiceError:
        return None, inventory, "SYSTEM_SERVICE_UNAVAILABLE"
    if configured is None:
        return None, inventory, "SYSTEM_SERVICE_UNAVAILABLE"
    # The observer snapshot must bind the exact descriptor read below.  A
    # matching interpreter/data-root pair alone is insufficient: a concurrent
    # replacement can change the service account or canonical plist while
    # retaining both strings.  Do not promote a second read unless the
    # inventory has one complete selected system entry for that same daemon.
    expected_selected_entry = {
        "label": configured.label,
        "domain": "SYSTEM",
        "plist": str(system_server_service.default_paths(configured.data_root).plist_path),
        "interpreter": str(configured.interpreter),
        "data_root": str(configured.data_root),
        "service_account": configured.service_account,
        "status": "SELECTED_SYSTEM_SERVICE",
    }
    entries = inventory.get("entries")
    selected_entries = (
        [entry for entry in entries if isinstance(entry, Mapping) and entry.get("status") == "SELECTED_SYSTEM_SERVICE"]
        if isinstance(entries, list) else []
    )
    if (
        inventory.get("selected_interpreter") != str(configured.interpreter)
        or inventory.get("selected_data_root") != str(configured.data_root)
        or selected_entries != [expected_selected_entry]
    ):
        return None, inventory, "SYSTEM_SERVICE_INVENTORY_MISMATCH"
    return configured, inventory, None


def _operational_product_readback(args: argparse.Namespace) -> dict[str, object]:
    """Produce EP-owned install evidence without a caller-runtime fallback.

    This is the narrow boundary for a composition consumer.  In contrast with
    historical local diagnostics, an absent owned system service cannot fall back
    to ``sys.executable``: that would let an incidental shell/PATH package
    become the supposedly selected operational runtime.
    """
    configured, inventory, unavailable_reason = _system_operational_service(args)
    if configured is None:
        return product_installation_readback.unavailable_readback(
            reason=unavailable_reason or "SYSTEM_SERVICE_UNAVAILABLE",
            inventory=inventory,
        )
    selected = configured.interpreter
    installation = operational_installation.resolve(
        args.data_root,
        interpreter=selected,
        path_candidates=args.candidate_interpreter,
    )
    record = operational_installation.record_status(installation)
    if record.get("state") == "UNREGISTERED":
        return product_installation_readback.readback(
            installation,
            record=record,
            package=None,
            health_response=None,
            inventory=inventory,
        )
    package = operational_installation.package_identity(selected)
    configuration = ServerConfiguration.load(args.data_root)
    try:
        response: Mapping[str, object] | None = _health_response(
            {"host": configuration.bind_host, "port": configuration.bind_port}
        )
    except (URLError, OSError, ValueError, operational_installation.OperationalInstallationError):
        response = None
    return product_installation_readback.readback(
        installation,
        record=record,
        package=package,
        health_response=response,
        inventory=inventory,
    )


def _owner_credential_authority(
    data_root: Path, expected_instance_id: str,
) -> owner_credential_recovery.OwnerAuthority:
    """Prove the invoking installed user runtime owns this exact instance."""

    selected = server_service.configured_interpreter(data_root)
    if selected is None:
        raise owner_credential_recovery.CredentialRecoveryError(
            "INSTALLED_INTERPRETER_BINDING_UNAVAILABLE"
        )
    if selected.absolute() != Path(sys.executable).absolute():
        raise owner_credential_recovery.CredentialRecoveryError(
            "OWNER_ROUTE_REQUIRES_INSTALLED_INTERPRETER"
        )
    installation = operational_installation.resolve(data_root, interpreter=selected)
    package = operational_installation.package_identity(selected)
    operational_installation.validate_package_identity(installation, package)
    record = operational_installation.record_status(installation)
    operational_installation.validate_registered_package_identity(record, package)
    if installation.instance_id != expected_instance_id:
        raise owner_credential_recovery.CredentialRecoveryError(
            "INSTALLATION_INSTANCE_MISMATCH"
        )
    return owner_credential_recovery.validate_owner_authority(
        data_root,
        expected_instance_id=expected_instance_id,
        selected_interpreter=selected,
        running_status=status(data_root),
    )


def _owner_credential_authenticate(
    data_root: Path, operation_id: str | None = None,
) -> Callable[[str, owner_credential_recovery.ConsumerBinding], bool]:
    configuration = ServerConfiguration.load(data_root)
    host = configuration.bind_host
    if host in {"0.0.0.0", "::", "localhost"}:
        host = "127.0.0.1"
    elif host == "::1":
        host = "[::1]"
    elif host != "127.0.0.1":
        raise owner_credential_recovery.CredentialRecoveryError(
            "OWNER_RECOVERY_REQUIRES_LOOPBACK_HTTP"
        )
    endpoint = f"http://{host}:{configuration.bind_port}" + (
        "/v1/producer-compatibility"
        if operation_id is None else "/v1/owner-credential-recovery-probe"
    )

    class NoCredentialRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
            return None

    opener = build_opener(NoCredentialRedirect())

    def authenticate(
        material: str, binding: owner_credential_recovery.ConsumerBinding,
    ) -> bool:
        headers = {
            "Authorization": f"Bearer {material}",
            "Accept": "application/json",
            "EP-Project-ID": binding.project_id,
            "EP-Repository-ID": binding.repository_id,
        }
        if operation_id is not None:
            headers["EP-Recovery-Operation-ID"] = operation_id
        request = Request(
            endpoint,
            headers=headers,
            method="GET",
        )
        try:
            with opener.open(request, timeout=5.0) as response:  # nosec B310 -- fixed installed loopback origin, redirects disabled
                payload = json.loads(response.read(65_537))
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping):
            return False
        if operation_id is not None:
            return (
                set(payload) == {
                    "contract_version", "instance_id", "operation_id", "consumer_id",
                    "project_id", "repository_id", "credential_status", "authorization",
                }
                and payload.get("contract_version") == "1.0"
                and payload.get("instance_id") == binding.instance_id
                and payload.get("operation_id") == operation_id
                and payload.get("consumer_id") == binding.consumer_id
                and payload.get("project_id") == binding.project_id
                and payload.get("repository_id") == binding.repository_id
                and payload.get("credential_status") == "PENDING_RECOVERY_PROBE"
                and payload.get("authorization") == "RECOVERY_PROBE_ONLY"
            )
        instance = payload.get("instance")
        contracts = payload.get("contracts")
        authentication = payload.get("authentication")
        return (
            payload.get("contract_version") == "1.1"
            and isinstance(instance, Mapping)
            and instance.get("id") == binding.instance_id
            and isinstance(contracts, Mapping)
            and "1.2" in contracts.get("producer_readback", [])
            and "1.4" in contracts.get("terminal_evidence", [])
            and isinstance(authentication, Mapping)
            and authentication.get("consumer_id") == binding.consumer_id
            and authentication.get("consumer_status") == "ACTIVE"
            and authentication.get("project_id") == binding.project_id
            and authentication.get("project_status") == "ACTIVE"
            and authentication.get("repository_id") == binding.repository_id
            and authentication.get("repository_role") == "authority"
            and authentication.get("local_repository_binding") == "BOUND"
            and authentication.get("submission_authorization") == "AUTHORIZED"
        )

    return authenticate


def _owner_binding_from_args(
    args: argparse.Namespace, *, require_consumer: bool = True,
) -> owner_credential_recovery.ConsumerBinding:
    if not all((
        args.expected_instance_id, args.project_id, args.repository_id,
        args.peer_binding_id, args.peer_runtime_id,
        args.peer_configuration_digest,
    )) or (require_consumer and not args.consumer_id):
        raise ServerConfigurationError(
            "--expected-instance-id, --project-id, --repository-id, --peer-binding-id, "
            "--peer-runtime-id and --peer-configuration-digest are required; --consumer-id "
            "is additionally required for mutating owner recovery routes"
        )
    return owner_credential_recovery.readback_from_data_root(
        args.data_root,
        expected_instance_id=args.expected_instance_id,
        project_id=args.project_id,
        repository_id=args.repository_id,
        expected_consumer_id=args.consumer_id,
        peer_binding_id=args.peer_binding_id,
        peer_runtime_id=args.peer_runtime_id,
        peer_configuration_digest=args.peer_configuration_digest,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        development = _development_profile_for(args)
        if args.command == "init":
            result = {"instance_id": initialize(args.data_root, bind_host=args.bind_host, bind_port=args.bind_port).instance_id, "initialized": True}
        elif args.command == "start":
            initialize(args.data_root)
            if central_operational_reset.maintenance_active(args.data_root):
                raise ServerConfigurationError("EP_OPERATIONAL_MAINTENANCE_ACTIVE")
            configuration = ServerConfiguration.load(args.data_root)
            if (configuration.bind_host, configuration.bind_port) != (args.bind_host, args.bind_port):
                _write_json(args.data_root / SERVER_CONFIGURATION_FILENAME, asdict(ServerConfiguration(
                    configuration.version, args.bind_host, args.bind_port,
                    configuration.managed_codex_cli_prefix, configuration.product_version,
                )))
            result = start(args.data_root) if development is None else start(args.data_root, development=development)
        elif args.command == "serve":
            return serve(args.data_root) if development is None else serve(args.data_root, development=development)
        elif args.command == "stop": result = stop(args.data_root)
        elif args.command == "status": result = status(args.data_root)
        elif args.command == "health": result = health(args.data_root)
        elif args.command == "operational-diagnose":
            configured, inventory, unavailable_reason = _system_operational_service(args)
            if configured is None:
                result = product_installation_readback.unavailable_readback(
                    reason=unavailable_reason or "SYSTEM_SERVICE_UNAVAILABLE",
                    inventory=inventory,
                )
            else:
                selected = configured.interpreter
                installation = operational_installation.resolve(args.data_root, interpreter=selected)
                package = operational_installation.package_identity(selected)
                operational_installation.validate_package_identity(installation, package)
                record = operational_installation.record_status(installation)
                operational_installation.validate_registered_package_identity(record, package)
                result = {
                    "installation": installation.payload(),
                    "record": record,
                    "package": package,
                    "system_service_inventory": inventory,
                }
        elif args.command == "operational-qualify":
            # Unlike the static diagnose command, this reads the response of
            # the running Server and fails closed if it is another instance,
            # package, executable, release or observed artifact.
            configured, inventory, unavailable_reason = _system_operational_service(args)
            if configured is None:
                result = product_installation_readback.unavailable_readback(
                    reason=unavailable_reason or "SYSTEM_SERVICE_UNAVAILABLE",
                    inventory=inventory,
                )
            else:
                selected = configured.interpreter
                installation = operational_installation.resolve(args.data_root, interpreter=selected)
                package = operational_installation.package_identity(selected)
                record = operational_installation.record_status(installation)
                configuration = ServerConfiguration.load(args.data_root)
                response = _health_response({"host": configuration.bind_host, "port": configuration.bind_port})
                result = operational_installation.qualify_runtime_response(
                    installation, record=record, package=package, response=response,
                )
        elif args.command == "operational-readback":
            result = _operational_product_readback(args)
        elif args.command == "operational-update-assess":
            if not all((args.operation_id, args.artifact, args.target_version, args.target_digest, args.target_source_revision)):
                raise ServerConfigurationError("--operation-id, --artifact, --target-version, --target-digest and --target-source-revision are required")
            observation = _operational_product_readback(args)
            result = product_installation_readback.assess_update(
                args.data_root,
                operation_id=args.operation_id,
                artifact=args.artifact,
                target_version=args.target_version,
                target_digest=args.target_digest,
                target_source_revision=args.target_source_revision,
                current_observation=observation,
            )
        elif args.command == "operational-inventory":
            references = _operational_inventory_inputs(args)
            configured, system_inventory, unavailable_reason = _system_operational_service(args)
            if configured is None:
                result = {
                    "state": "UNKNOWN",
                    "reason": unavailable_reason or "SYSTEM_SERVICE_UNAVAILABLE",
                    "system_service_inventory": system_inventory,
                    "explicit_candidate_inventory": None,
                    "single_operational_installation_verified": False,
                }
            else:
                installation = operational_installation.resolve(
                    args.data_root,
                    interpreter=configured.interpreter,
                    path_candidates=args.candidate_interpreter,
                )
                result = {
                    "state": "OBSERVED",
                    "resolver": {
                        "kind": "SYSTEM_LAUNCHDAEMON",
                        "label": configured.label,
                        "service_account": configured.service_account,
                        "interpreter": str(configured.interpreter),
                        "data_root": str(configured.data_root),
                    },
                    "system_service_inventory": system_inventory,
                    # Explicit inputs are diagnostic evidence only; they can
                    # never select the official runtime or upgrade its scope.
                    "explicit_candidate_inventory": operational_installation.inventory(
                        installation,
                        service_references=references,
                        candidates=args.candidate_interpreter,
                    ),
                    "single_operational_installation_verified": False,
                }
        elif args.command == "system-service-inventory":
            # This is a read-only, product-owned evidence surface.  It does
            # not enumerate accounts itself, launch a daemon, or promote a
            # caller-declared home list into a Mac-wide uniqueness claim.
            result = system_server_service.machine_scope_inventory(
                args.data_root, user_homes=args.declared_user_home,
            )
        elif args.command in {"legacy-adoption-inspect", "legacy-adoption-authorize"}:
            if args.preserved_wheel is None:
                raise ServerConfigurationError("--preserved-wheel is required for legacy adoption")
            # The maintenance entry deliberately uses the existing user-service
            # resolver.  It neither starts the service nor re-labels it as a
            # system service; unsupported topologies fail closed here.
            selected = server_service.configured_interpreter(args.data_root)
            if selected is None:
                raise ServerConfigurationError("the existing EP user service is unavailable")
            installation = operational_installation.resolve(args.data_root, interpreter=selected)
            observed = legacy_installation_adoption.inspect(
                installation=installation, service_label=server_service.LABEL,
                service_interpreter=selected, preserved_wheel=args.preserved_wheel,
            )
            if args.command == "legacy-adoption-inspect":
                result = observed.payload()
            else:
                if not all((args.operation_id, args.target_version, args.target_digest, args.target_source_revision)):
                    raise ServerConfigurationError("--operation-id, --target-version, --target-digest and --target-source-revision are required for legacy adoption")
                authorization = legacy_installation_adoption.LegacyAdoptionAuthorization(
                    instance_id=observed.instance_id, data_root=observed.data_root,
                    service_label=observed.service_label, interpreter=observed.interpreter,
                    old_artifact_digest=observed.artifact_digest, target_version=args.target_version,
                    target_artifact_digest=args.target_digest, target_source_revision=args.target_source_revision,
                    operation_id=args.operation_id,
                    acknowledge_unknown_source_revision=args.acknowledge_unknown_source_revision,
                )
                result = legacy_installation_adoption.adopt(observation=observed, authorization=authorization)
        elif args.command in {"installation-update-plan", "installation-update-prepare"}:
            if not all((args.operation_id, args.artifact, args.target_version, args.target_digest, args.target_source_revision)):
                raise ServerConfigurationError("--operation-id, --artifact, --target-version, --target-digest and --target-source-revision are required")
            update_plan = installation_update_plan.prepare(
                args.data_root, operation_id=args.operation_id, artifact=args.artifact,
                target_version=args.target_version, target_digest=args.target_digest,
                target_source_revision=args.target_source_revision,
            )
            if args.command == "installation-update-plan":
                result = update_plan.payload()
            else:
                if args.venv_builder is None:
                    raise ServerConfigurationError("--venv-builder is required and must identify Python 3.14")
                candidate = installation_update_preparation.prepare_candidate(
                    update_plan, venv_builder=args.venv_builder,
                )
                update_plan = installation_update_preparation.staged_execution_plan(
                    update_plan, candidate=candidate,
                )
                with installation_update_operation.InstallationUpdateSession(update_plan) as session:
                    session.bind_prepared_candidate(candidate, runner=subprocess.run)
                result = {"plan": update_plan.payload(), "prepared_candidate": candidate.payload()}
        elif args.command == "installation-update-admit":
            if not args.operation_id:
                raise ServerConfigurationError("--operation-id is required for installation update admission")
            update_plan = installation_update_operation.reopen_plan(args.data_root, args.operation_id)
            result = installation_update_admission.admit(update_plan).payload()
        elif args.command in {"installation-update-apply", "installation-update-resume"}:
            if not args.operation_id:
                raise ServerConfigurationError("--operation-id is required for installation update execution")
            update_plan = installation_update_operation.reopen_plan(args.data_root, args.operation_id)
            admission_payload = installation_update_operation.execution_admission(update_plan)
            if admission_payload is None:
                raise ServerConfigurationError("installation update has not been admitted")
            try:
                update_admission = installation_update_admission.ExecutionAdmission(**admission_payload)
            except TypeError as error:
                raise ServerConfigurationError("installation update admission is invalid") from error
            result = installation_update_composition.execute(
                update_plan, admission=update_admission,
                actions=_installation_update_operational_actions(args.data_root),
            )
        elif args.command == "installation-update-status":
            if not args.operation_id:
                raise ServerConfigurationError("--operation-id is required for installation update status")
            result = installation_update_operation.status(args.data_root, args.operation_id)
        elif args.command in {
            "owner-consumer-readback", "owner-credential-recover",
            "owner-credential-recovery-adopt-peer-configuration",
        }:
            if not args.expected_instance_id:
                raise ServerConfigurationError("--expected-instance-id is required")
            # Prove installed owner authority before discovery reveals scoped
            # consumer evidence.
            authority = _owner_credential_authority(
                args.data_root, args.expected_instance_id,
            )
            binding = _owner_binding_from_args(
                args, require_consumer=args.command != "owner-consumer-readback",
            )
            if args.command == "owner-consumer-readback":
                result = {
                    "owner_authority": authority.safe_dict(),
                    "consumer_binding": binding.safe_dict(),
                    "mutation_performed": False,
                }
            elif args.command == "owner-credential-recover":
                if not args.operation_id:
                    raise ServerConfigurationError(
                        "--operation-id is required for owner credential recovery"
                    )
                result = owner_credential_recovery.recover_credential(
                    args.data_root,
                    operation_id=args.operation_id,
                    binding=binding,
                    authority=authority,
                    store=owner_credential_recovery.NativeMacOSKeychainStore(),
                    authenticate=_owner_credential_authenticate(args.data_root),
                    authenticate_candidate=_owner_credential_authenticate(
                        args.data_root, args.operation_id,
                    ),
                )
            else:
                if not args.operation_id or not args.previous_peer_configuration_digest:
                    raise ServerConfigurationError(
                        "--operation-id and --previous-peer-configuration-digest are required "
                        "for guarded recovery peer-configuration adoption"
                    )
                result = owner_credential_recovery.adopt_peer_configuration(
                    args.data_root,
                    operation_id=args.operation_id,
                    binding=binding,
                    authority=authority,
                    previous_peer_configuration_digest=args.previous_peer_configuration_digest,
                )
        elif args.command == "owner-credential-recovery-status":
            if not args.operation_id:
                raise ServerConfigurationError(
                    "--operation-id is required for owner credential recovery status"
                )
            result = owner_credential_recovery.recovery_status(args.data_root, args.operation_id)
            authority = _owner_credential_authority(args.data_root, str(result["instance_id"]))
            result = {**result, "owner_authority": authority.safe_dict()}
        elif args.command == "service-install":
            initialize(args.data_root)
            result = {"result": "INSTALLED", **server_service.install(args.data_root)}
        elif args.command == "service-uninstall": result = {"result": "UNINSTALLED", **server_service.uninstall(args.data_root)}
        elif args.command == "relay-install":
            initialize(args.data_root)
            result = {"result": "INSTALLED", **server_relay.install(args.data_root)}
        elif args.command == "relay-uninstall":
            result = {"result": "UNINSTALLED", **server_relay.uninstall()}
        elif args.command == "topology":
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.topology(connection)
        elif args.command == "submission-diagnose":
            if not args.submission_id:
                raise ServerConfigurationError("--submission-id is required for submission diagnostics.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                row = connection.execute("SELECT s.project_id,s.repository_id,s.state,s.admission,s.transport,s.transport_receipt_id,s.transport_received_at,p.run_id,d.state,d.operator_resolution,p.project_id,p.repository_id FROM ep_submissions s LEFT JOIN ep_receipt_run_provenance p ON p.submission_id=s.submission_id LEFT JOIN ep_parity_lifecycle_dispatches d ON d.run_id=p.run_id WHERE s.submission_id=?", (args.submission_id,)).fetchone()
                if row is None:
                    raise ServerConfigurationError("UNKNOWN_SUBMISSION")
                project_id, repository_id, state, admission, transport, receipt_id, received_at, run_id, dispatch_state, resolution, dispatch_project, dispatch_repository = row
                blocked = connection.execute("SELECT run_id,state FROM ep_parity_lifecycle_dispatches WHERE project_id=? AND submission_id!=? AND state IN ('CLAIMED','RUNNING','BLOCKED','FAILED') AND run_id!=? ORDER BY updated_at LIMIT 1", (project_id, args.submission_id, run_id or "")).fetchone()
                admission_audit = connection.execute("SELECT 1 FROM ep_submission_events WHERE submission_id=? AND event_kind='ADMISSION_GRANTED'", (args.submission_id,)).fetchone()
            early = None
            if run_id:
                early_path = args.data_root / "artifacts" / "projects" / str(project_id) / "runs" / str(run_id) / "early-runner-failure.json"
                if early_path.is_file():
                    try:
                        early = json.loads(early_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        early = {"diagnostic_code": "EARLY_FAILURE_EVIDENCE_UNAVAILABLE"}
            receipt_complete = transport != "FILE_INBOX" or (isinstance(receipt_id, str) and bool(receipt_id) and isinstance(received_at, str) and bool(received_at))
            scope_complete = run_id is not None and (dispatch_project, dispatch_repository) == (project_id, repository_id)
            result = {"submission_id": args.submission_id, "project_id": project_id, "repository_id": repository_id, "submission_state": state, "admission": admission, "run_id": run_id, "dispatch_state": dispatch_state, "operator_resolution": resolution, "transport_provenance": "COMPLETE" if receipt_complete else "INCOMPLETE", "admission_audit_provenance": "PRESENT" if admission_audit else "UNAVAILABLE", "receipt_run_provenance": "PRESENT" if run_id else "UNAVAILABLE", "dispatch_scope_provenance": "COMPLETE" if scope_complete else "UNAVAILABLE", "lane_blocker": {"run_id": blocked[0], "state": blocked[1]} if blocked else None, "early_failure": early, "worker_eligible": state == "QUEUED" and admission == "ADMITTED" and blocked is None}
        elif args.command == "register-topology":
            if args.declaration is None:
                raise ServerConfigurationError("--declaration is required for explicit topology registration.")
            initialize(args.data_root)
            try:
                declaration = json.loads(args.declaration.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ServerConfigurationError("REPOSITORY_DECLARATION_UNREADABLE") from error
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.register_server_local_topology(connection, declaration=declaration)
        elif args.command == "bootstrap-topology":
            if not args.project_id or not args.repository_id:
                raise ServerConfigurationError("--project-id and --repository-id are required for topology bootstrap.")
            initialize(args.data_root)
            declaration = {"schema_version": "1.0", "project": {"id": args.project_id, "authority_repository_id": args.repository_id}, "repository": {"id": args.repository_id, "role": "authority"}, "validation": {"kind": "none"}}
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = project_topology.register_server_local_topology(connection, declaration=declaration)
        elif args.command == "issue-consumer-credential":
            if not args.project_id or not args.consumer_id:
                raise ServerConfigurationError("--project-id and --consumer-id are required for credential issuance.")
            initialize(args.data_root)
            from .submission_service import issue_consumer_credential
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = issue_consumer_credential(connection, consumer_id=args.consumer_id, project_id=args.project_id)
        elif args.command == "issue-development-consumer-credential":
            if development is None:
                raise ServerConfigurationError("DEVELOPMENT_PROFILE_REQUIRED")
            if not args.project_id or not args.consumer_id:
                raise ServerConfigurationError("--project-id and --consumer-id are required for development credential issuance.")
            from .platform_admin import require_installation_owner
            try:
                require_installation_owner(args.data_root)
            except PermissionError as error:
                raise ServerConfigurationError("PLATFORM_ADMIN_FORBIDDEN") from error
            from .submission_service import issue_development_consumer_credential
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = issue_development_consumer_credential(
                    connection, consumer_id=args.consumer_id, project_id=args.project_id,
                )
        elif args.command in {"grant-operator-capability", "revoke-operator-capability"}:
            if not args.project_id or not args.consumer_id or not args.capability:
                raise ServerConfigurationError("--project-id, --consumer-id and --capability are required for queue operator capability management.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command == "grant-operator-capability":
                    registered = connection.execute("SELECT 1 FROM ep_consumer_registrations WHERE consumer_id=? AND project_id=? AND status='ACTIVE'", (args.consumer_id, args.project_id)).fetchone()
                    if registered is None:
                        raise ServerConfigurationError("ACTIVE_SCOPED_CONSUMER_REQUIRED")
                    connection.execute("INSERT INTO ep_operator_capabilities(consumer_id,project_id,capability,granted_at,revoked_at) VALUES(?,?,?,?,NULL) ON CONFLICT(consumer_id,project_id,capability) DO UPDATE SET granted_at=excluded.granted_at,revoked_at=NULL", (args.consumer_id, args.project_id, args.capability, _utcnow()))
                    result = {"result": "GRANTED", "consumer_id": args.consumer_id, "project_id": args.project_id, "capability": args.capability}
                else:
                    changed = connection.execute("UPDATE ep_operator_capabilities SET revoked_at=? WHERE consumer_id=? AND project_id=? AND capability=? AND revoked_at IS NULL", (_utcnow(), args.consumer_id, args.project_id, args.capability)).rowcount
                    result = {"result": "REVOKED" if changed else "NOT_ACTIVE", "consumer_id": args.consumer_id, "project_id": args.project_id, "capability": args.capability}
        elif args.command in {"reserve-merge-delegation", "activate-merge-delegation", "revoke-merge-delegation"}:
            if args.command != "reserve-merge-delegation" and args.assurance_profile is not None:
                raise ServerConfigurationError("Assurance profile can only be selected at reservation.")
            initialize(args.data_root)
            try:
                from .platform_admin import require_installation_owner
                owner_uid = require_installation_owner(args.data_root)
            except PermissionError as error:
                raise ServerConfigurationError("PLATFORM_ADMIN_FORBIDDEN") from error
            actor_reference = f"local-uid:{owner_uid}"
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command == "reserve-merge-delegation":
                    if not all((args.project_id, args.repository_id, args.merge_role, args.expires_at)):
                        raise ServerConfigurationError("Merge reservation requires project, repository, roles and expiry.")
                    scope = connection.execute(
                        "SELECT b.local_root FROM ep_project_registrations p JOIN ep_repository_registrations r "
                        "ON r.project_id=p.project_id JOIN ep_local_repository_bindings b "
                        "ON b.project_id=p.project_id AND b.repository_id=r.repository_id "
                        "WHERE p.project_id=? AND p.status='ACTIVE' AND r.repository_id=? "
                        "AND r.role='authority' AND b.state='BOUND'",
                        (args.project_id, args.repository_id),
                    ).fetchone()
                    if scope is None:
                        raise ServerConfigurationError("ACTIVE_AUTHORITY_REPOSITORY_REQUIRED")
                    try:
                        github_repository = merge_delegation.bound_github_repository(Path(str(scope[0])))
                        policy_digest = ""
                        if args.assurance_profile is not None:
                            if args.assurance_profile != merge_delegation.AUTONOMOUS_ASSURANCE_PROFILE:
                                raise ValueError("Unknown autonomous assurance profile")
                            from .execution_repository import GhCliClient
                            from .execution_errors import RunnerError
                            try:
                                policy_digest = str(GhCliClient(repository=github_repository)._autonomous_effective_policy()["digest"])
                            except RunnerError as error:
                                raise ValueError("Autonomous effective GitHub policy is unavailable") from error
                        grant = merge_delegation.reserve(
                            connection, delegation_id=uuid4().hex,
                            actor_reference=actor_reference,
                            project_id=args.project_id, repository_id=args.repository_id,
                            github_repository=github_repository,
                            base_branch="main", roles=args.merge_role, expires_at=args.expires_at,
                            assurance_profile=args.assurance_profile,
                            assurance_policy_digest=policy_digest,
                        )
                    except ValueError as error:
                        raise ServerConfigurationError(str(error)) from error
                    result = {"result": "RESERVED", **asdict(grant)}
                elif args.command == "activate-merge-delegation":
                    if not all((args.delegation_id, args.mission_id, args.mission_revision)):
                        raise ServerConfigurationError("Activation requires delegation ID, Mission ID and revision.")
                    try:
                        reserved = merge_delegation.load(connection, args.delegation_id)
                        if reserved is None:
                            raise ValueError("Merge reservation is unavailable")
                        bound = connection.execute(
                            "SELECT b.local_root FROM ep_local_repository_bindings b "
                            "JOIN ep_project_registrations p ON p.project_id=b.project_id AND p.status='ACTIVE' "
                            "JOIN ep_repository_registrations r ON r.repository_id=b.repository_id "
                            "AND r.project_id=b.project_id AND r.role='authority' "
                            "WHERE b.project_id=? AND b.repository_id=? AND b.state='BOUND'",
                            (reserved.project_id, reserved.repository_id),
                        ).fetchone()
                        if bound is None:
                            raise ValueError("Bound repository is unavailable")
                        grant = merge_delegation.activate(
                            connection, delegation_id=args.delegation_id,
                            mission_id=args.mission_id, mission_revision=args.mission_revision,
                            actor_reference=actor_reference,
                            github_repository=merge_delegation.bound_github_repository(Path(str(bound[0]))),
                        )
                    except ValueError as error:
                        raise ServerConfigurationError(str(error)) from error
                    result = {"result": "ACTIVE", **asdict(grant)}
                else:
                    if not args.delegation_id:
                        raise ServerConfigurationError("--delegation-id is required for revocation.")
                    result = {"result": "REVOKED" if merge_delegation.revoke(connection, args.delegation_id, actor_reference=actor_reference) else "NOT_ACTIVE",
                              "delegation_id": args.delegation_id}
        elif args.command == "register-producer-binding":
            if not all((args.producer_type, args.external_resource_type, args.external_resource_identity, args.project_id, args.repository_id, args.reason)):
                raise ServerConfigurationError("--producer-type, --external-resource-type, --external-resource-identity, --project-id, --repository-id and --reason are required for producer binding registration.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                binding = external_producer_binding.register(
                    connection,
                    data_root=args.data_root,
                    producer_type=args.producer_type,
                    external_resource_type=args.external_resource_type,
                    external_resource_identity=args.external_resource_identity,
                    project_id=args.project_id,
                    repository_id=args.repository_id,
                    reason=args.reason,
                )
            result = {"binding_id": binding.binding_id, "project_id": binding.project_id, "repository_id": binding.repository_id, "version": binding.version, "result": "REGISTERED"}
        elif args.command == "list-producer-bindings":
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                result = {"bindings": external_producer_binding.list_bindings(connection, data_root=args.data_root)}
        elif args.command == "deactivate-producer-binding":
            if not args.binding_id or not args.reason:
                raise ServerConfigurationError("--binding-id and --reason are required for producer binding deactivation.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                binding = external_producer_binding.deactivate(connection, data_root=args.data_root, binding_id=args.binding_id, reason=args.reason)
            result = {"binding_id": binding.binding_id, "project_id": binding.project_id, "repository_id": binding.repository_id, "version": binding.version, "result": "DEACTIVATED"}
        elif args.command == "provision-declaration":
            if not args.project_id or not args.repository_id or args.path is None:
                raise ServerConfigurationError("--project-id, --repository-id and --path are required for declaration provisioning.")
            initialize(args.data_root)
            from .repository_attachment import config_path, load_repository_attachment, parse_repository_attachment
            root = args.path.resolve(strict=True)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                row = connection.execute("SELECT attachment_contract FROM ep_repository_registrations WHERE project_id=? AND repository_id=?", (args.project_id, args.repository_id)).fetchone()
            if row is None:
                raise ServerConfigurationError("CENTRAL_REPOSITORY_NOT_REGISTERED")
            declaration = json.loads(str(row[0]))
            parse_repository_attachment(declaration)
            target = config_path(root)
            if target.exists():
                existing = load_repository_attachment(root)
                if (existing.project_id, existing.repository_id) != (args.project_id, args.repository_id):
                    raise ServerConfigurationError("REPOSITORY_DECLARATION_CONFLICT")
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_text(json.dumps(declaration, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                target.chmod(0o600)
            result = {"project_id": args.project_id, "repository_id": args.repository_id, "path": str(target), "result": "PROVISIONED"}
        elif args.command in {"bind-repository", "rebind-repository", "unbind-repository", "resolve-repository"}:
            if not args.project_id or not args.repository_id:
                raise ServerConfigurationError("--project-id and --repository-id are required for local binding commands.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command in {"bind-repository", "rebind-repository"}:
                    if args.path is None:
                        raise ServerConfigurationError("--path is required when binding a repository.")
                    binding = local_repository_binding.bind_local_repository(connection, project_id=args.project_id, repository_id=args.repository_id, local_root=args.path, data_root=args.data_root, rebind=args.command == "rebind-repository")
                    result = {"project_id": binding.project_id, "repository_id": binding.repository_id, "state": binding.state}
                elif args.command == "unbind-repository":
                    local_repository_binding.unbind_local_repository(connection, project_id=args.project_id, repository_id=args.repository_id)
                    result = {"project_id": args.project_id, "repository_id": args.repository_id, "state": "UNBOUND"}
                else:
                    binding = local_repository_binding.resolve_execution_repository(connection, project_id=args.project_id, repository_id=args.repository_id, data_root=args.data_root)
                    result = {"project_id": binding.project_id, "repository_id": binding.repository_id, "state": binding.state}
        else:
            if not args.agent_id:
                raise ServerConfigurationError("--agent-id is required for Agent lifecycle commands.")
            initialize(args.data_root)
            with storage.sqlite_connection(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command == "pairing-create": result = agent_trust.create_pairing_code(connection, args.agent_id)
                elif args.command == "agent-status": result = agent_trust.registration_status(connection, args.agent_id)
                elif args.command == "agent-revoke": result = {"agent_id": args.agent_id, "revoked": agent_trust.revoke(connection, args.agent_id)}
                else: result = {"agent_id": args.agent_id, "reset": agent_trust.reset(connection, args.agent_id)}
    except (OSError, RuntimeError, PermissionError, ServerConfigurationError,
            system_server_service.SystemServerServiceError,
            operational_installation.OperationalInstallationError,
            product_installation_readback.ProductInstallationReadbackError,
            installation_update_plan.InstallationUpdatePlanError,
            installation_update_preparation.InstallationUpdatePreparationError,
            installation_update_operation.InstallationUpdateOperationError,
            installation_update_admission.InstallationUpdateAdmissionError,
            installation_update_composition.InstallationUpdateCompositionError,
            installation_update_executor.InstallationUpdateExecutorError,
            installation_update_backup.InstallationUpdateBackupError,
            installation_update_migration.InstallationUpdateMigrationError,
            installation_update_activation.InstallationUpdateActivationError,
            legacy_installation_adoption.LegacyInstallationAdoptionError,
            owner_credential_recovery.CredentialRecoveryError,
            operational_installation_record.OperationalInstallationRecordError,
            server_service.ServerServiceError,
            development_profile.DevelopmentProfileError,
            local_repository_binding.LocalRepositoryBindingError,
            external_producer_binding.ProducerBindingError) as error:
        print(json.dumps({"error": str(error), "ready": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
