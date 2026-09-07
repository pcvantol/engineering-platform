#!/usr/bin/env python3
"""Fail closed when the HTTP implementation, OpenAPI and Postman drift."""
from __future__ import annotations

import argparse
import http.server
import json
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from threading import Lock, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import server, submission_service
from ep_server_postman_surface import postman_collection


COLLECTION = Path("tests/engineering/postman/http-json-api.postman_collection.json")
SURFACE_COLLECTION = Path("tests/engineering/postman/ep-server-http-surface.postman_collection.json")
_STATUS = re.compile(r"pm\.response\.to\.have\.status\((\d+)\)")


class _NoopService:
    """Minimal lifecycle double for isolated HTTP surface qualification."""
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def diagnostics(self) -> "_NoopService": return self
    def to_dict(self) -> dict[str, object]: return {"state": "QUALIFICATION_NOOP"}


def _operations_from_openapi(document: dict[str, object]) -> set[tuple[str, str]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("OPENAPI_PATHS_INVALID")
    return {
        (method.upper(), path)
        for path, item in paths.items()
        if isinstance(path, str) and isinstance(item, dict)
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def _collection_items(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        raise RuntimeError("POSTMAN_ITEMS_INVALID")
    result: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("POSTMAN_ITEM_INVALID")
        if "item" in item:
            result.extend(_collection_items(item["item"]))
        elif isinstance(item.get("request"), dict):
            result.append(item)
        else:
            raise RuntimeError("POSTMAN_REQUEST_MISSING")
    return result


def _collection_operation(item: dict[str, object]) -> tuple[str, str]:
    request = item["request"]
    assert isinstance(request, dict)
    method, raw = request.get("method"), request.get("url")
    if not isinstance(method, str) or not isinstance(raw, str):
        raise RuntimeError("POSTMAN_REQUEST_INVALID")
    path = raw.removeprefix("{{baseUrl}}")
    # Postman's ``:name`` parameters occur at a path-segment boundary.  Do
    # not rewrite a literal colon in an evidence-artifact identifier.
    path = re.sub(r"/:([A-Za-z_][A-Za-z0-9_]*)", r"/{\1}", path)
    if not path.startswith("/"):
        raise RuntimeError("POSTMAN_URL_INVALID")
    return method.upper(), path


def _expected_status(item: dict[str, object]) -> int:
    events = item.get("event", [])
    scripts = [line for event in events if isinstance(event, dict) and event.get("listen") == "test"
               for line in (event.get("script", {}).get("exec", []) if isinstance(event.get("script"), dict) else [])]
    matches = [int(match.group(1)) for line in scripts if isinstance(line, str) for match in _STATUS.finditer(line)]
    if len(matches) != 1:
        raise RuntimeError("POSTMAN_STATUS_ASSERTION_MISSING")
    return matches[0]


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(base_url: str, item: dict[str, object]) -> int:
    request = item["request"]
    assert isinstance(request, dict)
    method, path = _collection_operation(item)
    url = base_url + path.replace("{project_id}", "postman-project")
    headers = {str(header["key"]): str(header["value"]).replace("{{baseUrl}}", base_url)
               for header in request.get("header", []) if isinstance(header, dict) and "key" in header and "value" in header}
    body = request.get("body", {})
    data = str(body.get("raw", "")).encode("utf-8") if isinstance(body, dict) and method != "GET" else None
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=3) as response:
            return response.status
    except HTTPError as error:
        return error.code


def _queue_action_contract(data_root: Path, base_url: str) -> None:
    """Exercise Operations Console actions through their real HTTP boundary.

    These are intentionally separate from the public OpenAPI collection: the
    queue controls are authenticated local-console operations, not producer
    transport endpoints.  Keeping the distinction prevents a console write
    from being silently advertised as a public API operation.
    """
    project, repository = "postman-queue", "postman-queue-repository"
    declaration = {
        "schema_version": "1.0",
        "project": {"id": project, "authority_repository_id": repository},
        "repository": {"id": repository, "role": "authority"},
        "validation": {"kind": "none"},
    }
    with sqlite3.connect(data_root / server.SERVER_DATABASE_FILENAME) as connection:
        server.project_topology.register_server_local_topology(connection, declaration=declaration)
        credential = str(submission_service.issue_consumer_credential(
            connection, consumer_id="postman-queue", project_id=project,
        )["credential"])
    payload = {
        "repository_id": repository,
        "producer": {"id": "postman-queue", "type": "HUMAN", "version": "1"},
        "prompt": "Postman Operations Console queue qualification",
        "idempotency_key": "postman-queue-actions",
    }
    submit = Request(
        base_url + f"/v1/projects/{project}/submissions", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"},
    )
    with urlopen(submit, timeout=3) as response:  # nosec B310
        submission_id = str(json.loads(response.read())["submission_id"])

    def action(disposition: str, reason: str, *, origin: str | None = None) -> tuple[int, str]:
        request = Request(
            base_url + f"/api/queue-disposition?project={project}",
            data=json.dumps({"submission_id": submission_id, "disposition": disposition, "reason": reason}).encode(),
            method="POST", headers={"Content-Type": "application/json", "Origin": origin or base_url},
        )
        try:
            with urlopen(request, timeout=3) as response:  # nosec B310
                response.read()
                return response.status, ""
        except HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    for disposition, reason in (
        ("DEFERRED", "Postman defer contract"), ("QUEUED", "Postman resume contract"),
        ("QUARANTINED", "Postman quarantine contract"), ("QUEUED", "Postman resume after quarantine contract"),
        ("DECLINED", "Postman decline contract"),
    ):
        status, detail = action(disposition, reason)
        if status != 200:
            raise RuntimeError(f"POSTMAN_QUEUE_ACTION_FAILED:{disposition}:{status}:{detail}")
    status, detail = action("QUEUED", "Must not revive a declined submission")
    if status != 409:
        raise RuntimeError(f"POSTMAN_QUEUE_TERMINAL_TRANSITION_NOT_REJECTED:{status}:{detail}")
    status, detail = action("DEFERRED", "Untrusted origin", origin="https://untrusted.example")
    if status != 403:
        raise RuntimeError("POSTMAN_QUEUE_ORIGIN_ISOLATION_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    root = parser.parse_args(argv).source_root.resolve()
    collection = json.loads((root / COLLECTION).read_text(encoding="utf-8"))
    surface_collection = json.loads((root / SURFACE_COLLECTION).read_text(encoding="utf-8"))
    if collection.get("info", {}).get("schema") != "https://schema.getpostman.com/json/collection/v2.1.0/collection.json":
        raise RuntimeError("POSTMAN_SCHEMA_INVALID")
    if surface_collection != postman_collection():
        raise RuntimeError("POSTMAN_SERVER_SURFACE_MANIFEST_DRIFT")
    expected_openapi = server._http_json_openapi_document()
    items = _collection_items(collection.get("item"))
    if _operations_from_openapi(expected_openapi) != {_collection_operation(item) for item in items}:
        raise RuntimeError("OPENAPI_POSTMAN_OPERATION_DRIFT")
    with tempfile.TemporaryDirectory(prefix="ep-postman-contract-") as temporary:
        data_root = Path(temporary) / "data"
        port = _port()
        server.initialize(data_root, bind_port=port)
        with sqlite3.connect(data_root / server.SERVER_DATABASE_FILENAME) as connection:
            server.project_topology.register_server_local_topology(connection, declaration={
                "schema_version": "1.0", "project": {"id": "postman-project", "authority_repository_id": "postman-repository"},
                "repository": {"id": "postman-repository", "role": "authority"}, "validation": {"kind": "none"},
            })
        http_server = http.server.ThreadingHTTPServer(("127.0.0.1", port), server._HealthHandler)
        http_server.data_root = data_root  # type: ignore[attr-defined]
        http_server.central_data_transfer_lock = Lock()  # type: ignore[attr-defined]
        http_server.central_data_transfer_active = False  # type: ignore[attr-defined]
        http_server.dependabot_service = _NoopService()  # type: ignore[attr-defined]
        http_server.inbox_service = _NoopService()  # type: ignore[attr-defined]
        http_server.lifecycle_worker = _NoopService()  # type: ignore[attr-defined]
        worker = Thread(target=http_server.serve_forever, daemon=True)
        worker.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            with urlopen(base_url + server.HTTP_JSON_OPENAPI_PATH, timeout=3) as response:
                served_openapi = json.loads(response.read().decode("utf-8"))
            if served_openapi != expected_openapi:
                raise RuntimeError("API_OPENAPI_DRIFT")
            for item in items:
                observed, expected = _request(base_url, item), _expected_status(item)
                if observed != expected:
                    raise RuntimeError(f"API_POSTMAN_DRIFT {item.get('name')}: expected {expected}, got {observed}")
            for item in _collection_items(surface_collection.get("item")):
                observed, expected = _request(base_url, item), _expected_status(item)
                if observed != expected:
                    raise RuntimeError(f"SERVER_SURFACE_POSTMAN_DRIFT {item.get('name')}: expected {expected}, got {observed}")
            _queue_action_contract(data_root, base_url)
        finally:
            http_server.shutdown()
            worker.join(timeout=3)
    print("HTTP_JSON_API_OPENAPI_POSTMAN_CONTRACT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
