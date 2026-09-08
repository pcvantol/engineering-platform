"""Complete, fail-closed Postman surface manifest for EP Server.

This is deliberately distinct from the public OpenAPI collection: it covers
the installed Operations Console, administration and retired compatibility
routes as well as producer transport.  Every item is a callable HTTP request
with its intended status code, so a route cannot silently disappear or start
accepting an unsafe request without changing the qualification manifest.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str
    status: int
    body: str | None = None
    project: bool = False
    origin: bool = False


# One concrete case for every route family in console_route_ownership, plus
# transport aliases that are intentionally outside that Console matrix.
CASES: tuple[Case, ...] = (
    Case("Console shell", "GET", "/", 200),
    Case("Console JavaScript asset", "GET", "/assets/dashboard.js", 200),
    Case("Console favicon", "GET", "/favicon.ico", 200),
    Case("Platform health", "GET", "/health", 503),
    Case("Platform projection", "GET", "/api/platform-status", 200),
    Case("Dashboard snapshot", "GET", "/api/dashboard-snapshot", 200),
    Case("Status alias", "GET", "/api/status", 200),
    Case("Platform component details", "GET", "/api/components/ep_server/details", 200),
    Case("Invalid component details", "GET", "/api/components/not_a_component/details", 409),
    Case("Component restart rejects malformed body", "POST", "/api/components/ep_server/restart", 409, "{}"),
    Case("Platform logs", "GET", "/api/logs/all", 200),
    Case("Platform logs reject malformed clear", "POST", "/api/logs/all", 400, "{}"),
    Case("Provider readiness", "GET", "/api/provider-login-status", 200),
    Case("Provider repair rejects malformed body", "POST", "/api/provider-login/repair", 409, "{}"),
    Case("Provider logout rejects malformed body", "POST", "/api/provider-login/logout", 409, "{}"),
    Case("Execution runtime", "GET", "/api/execution-runtime-status", 200),
    Case("Runtime repair rechecks the installed runtime", "POST", "/api/execution-runtime/repair", 200, "{}"),
    Case("Provider capacity", "GET", "/api/provider-capacity", 200),
    Case("Provider capacity configuration", "GET", "/api/provider-capacity/configuration", 200),
    Case("Provider capacity rejects malformed update", "POST", "/api/provider-capacity/configuration", 409, "{}"),
    Case("GitHub rate-limit diagnostics", "GET", "/api/github-rate-limit", 200),
    Case("Console configuration", "GET", "/api/configuration", 200),
    Case("Console configuration rejects malformed update", "POST", "/api/configuration", 409, "{}"),
    Case("Process metrics", "GET", "/api/process-metrics", 200),
    Case("Usage diagnostics", "GET", "/api/usage", 200),
    Case("Host-admin diagnostics", "GET", "/api/host-admin/diagnostics", 200),
    Case("Central data export", "GET", "/api/central-data/export", 200),
    Case("Central relocation rejects bad request", "POST", "/api/central-data/relocate", 409, "{}"),
    Case("Central import requires confirmation", "POST", "/api/central-data/import", 409, "{}"),
    Case("Central database configuration", "GET", "/api/central-database/configuration", 200),
    Case("Central database configuration rejects bad request", "POST", "/api/central-database/configuration", 400, "{}"),
    Case("Project history", "GET", "/api/prompt-history", 200, project=True),
    Case("Missing project report", "GET", "/api/prompt-history/missing-run/report", 404, project=True),
    Case("Missing project chat", "GET", "/api/prompt-history/missing-run/chat", 404, project=True),
    Case("Missing project detail", "GET", "/api/prompt-history/missing-run/details", 404, project=True),
    Case("Missing telemetry day", "GET", "/api/telemetry/2026-01-01", 404, project=True),
    Case("Execution action rejects malformed request", "POST", "/api/execution-dismiss", 400, "{}", True, True),
    Case("Queue action rejects malformed request", "POST", "/api/queue-disposition", 400, "{}", True, True),
    Case("Translation route is fail-closed pending project mutation support", "POST", "/api/dashboard-translate", 405, "{}", True, True),
    Case("Retired runtime directory", "POST", "/api/runtime-directory/open", 410, "{}"),
    Case("Retired Inbox configuration", "POST", "/api/configuration/inbox-location", 410, "{}"),
    Case("Retired component logs", "GET", "/api/logs/inbox", 410),
    Case("Operations projects", "GET", "/v1/operations/projects", 200),
    Case("Topology diagnostics", "GET", "/diagnostics/topology", 200),
    Case("Health probe", "GET", "/healthz", 200),
    Case("Readiness probe", "GET", "/readyz", 200),
    Case("Producer compatibility declaration", "GET", "/v1/producer-compatibility", 200),
    Case("Submission requires credential", "POST", "/v1/projects/postman-project/submissions", 401, "{}"),
    Case("Submission readback requires credential", "GET", "/v1/projects/postman-project/submissions/missing", 401),
    Case("Evidence artifact requires credential", "GET", "/v1/projects/postman-project/artifacts/terminal-evidence:missing", 401),
    Case("Agent pairing rejects malformed request", "POST", "/v1/agent/pair", 400, "{}"),
    Case("Agent registration requires credential", "POST", "/v1/agent/register", 401, "{}"),
    Case("Unknown route", "GET", "/not-an-ep-route", 404),
)


def postman_collection() -> dict[str, object]:
    def item(case: Case) -> dict[str, object]:
        headers = ([{"key": "Content-Type", "value": "application/json"}]
                   if case.body is not None else [])
        if case.origin:
            headers.append({"key": "Origin", "value": "{{baseUrl}}"})
        suffix = "?project=postman-project" if case.project else ""
        request: dict[str, object] = {"method": case.method, "header": headers,
                                      "url": "{{baseUrl}}" + case.path + suffix}
        if case.body is not None:
            request["body"] = {"mode": "raw", "raw": case.body}
        return {"name": case.name, "request": request, "event": [{"listen": "test", "script": {"exec": [
            f"pm.test('expected status', () => pm.response.to.have.status({case.status}));"
        ]}}]}
    return {"info": {"name": "EP Server complete HTTP surface", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "item": [item(case) for case in CASES]}
