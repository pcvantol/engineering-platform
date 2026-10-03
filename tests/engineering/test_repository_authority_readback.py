"""Installed HTTP authority evidence for two independently bound repositories."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import (
    local_repository_binding, parallel_action_admission, project_topology,
    server, submission_service,
)
from engineering_platform.repository_attachment import config_path
from engineering_platform.storage import sqlite_connection


class RepositoryAuthorityHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "central"
        self.instance = server.initialize(self.data_root).instance_id
        self.roots: dict[str, Path] = {}
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            for repository_id, role in (("repository-a", "authority"), ("repository-b", "child")):
                checkout = self._checkout(repository_id)
                self.roots[repository_id] = checkout
                project_topology.register_server_local_topology(
                    connection, declaration=self._declaration(repository_id, role),
                )
                local_repository_binding.bind_local_repository(
                    connection, project_id="project-test", repository_id=repository_id,
                    local_root=checkout, data_root=self.data_root,
                )
            self.token = submission_service.issue_consumer_credential(
                connection, consumer_id="forge", project_id="project-test",
            )["credential"]
            self.other_token = submission_service.issue_consumer_credential(
                connection, consumer_id="other", project_id="project-test",
            )["credential"]
            for repository_id in self.roots:
                self._grant(connection, repository_id, active=True)
        self._start_http()

    def tearDown(self) -> None:
        self._stop_http()
        self.temporary.cleanup()

    def _declaration(self, repository_id: str, role: str) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "project": {"id": "project-test", "authority_repository_id": "repository-a"},
            "repository": {"id": repository_id, "role": role},
            "validation": {"kind": "none"},
        }

    def _checkout(self, repository_id: str, *, suffix: str = "") -> Path:
        checkout = self.root / f"{repository_id}{suffix}"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # nosec B603
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "add", "origin",
             f"https://github.com/pcvantol/{repository_id}.git"], check=True,
        )
        declaration = self._declaration(
            repository_id, "authority" if repository_id == "repository-a" else "child",
        )
        target = config_path(checkout)
        target.parent.mkdir()
        target.write_text(json.dumps(declaration), encoding="utf-8")
        return checkout

    def _grant(self, connection, repository_id: str, *, active: bool) -> None:  # type: ignore[no-untyped-def]
        with patch("engineering_platform.platform_admin.require_installation_owner", return_value="test-owner"):
            parallel_action_admission.set_repository_grant(
                connection, data_root=self.data_root, consumer_id="forge",
                project_id="project-test", repository_id=repository_id,
                reason="authority readback qualification", active=active,
            )

    def _start_http(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.httpd.data_root = self.data_root  # type: ignore[attr-defined]
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()

    def _stop_http(self) -> None:
        self.httpd.shutdown()
        self.worker.join(timeout=5)
        self.httpd.server_close()

    def _read(
        self, repository_id: str = "repository-a", *, project_id: str = "project-test",
        token: str | None = None, headers: dict[str, str | None] | None = None,
        port: int | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        selected = {
            "Authorization": f"Bearer {self.token if token is None else token}",
            "EP-Instance-ID": self.instance,
            "EP-Consumer-ID": "forge",
            "EP-GitHub-Repository": f"pcvantol/{repository_id}",
        }
        selected.update(headers or {})
        request = Request(
            f"http://127.0.0.1:{port or self.httpd.server_port}/v1/projects/{project_id}"
            f"/repositories/{repository_id}/consumer-authority",
            headers={key: value for key, value in selected.items() if value is not None},
        )
        try:
            with urlopen(request, timeout=5) as response:  # nosec B310
                return response.status, json.load(response), dict(response.headers)
        except HTTPError as error:
            with error:
                return error.code, json.load(error), dict(error.headers)

    def test_two_repositories_have_independent_stable_pinnable_authority(self) -> None:
        first = self._read("repository-a")
        second = self._read("repository-b")
        self.assertEqual((first[0], second[0]), (200, 200))
        a, b = first[1], second[1]
        schema = json.loads((Path(server.__file__).parent / "schemas"
                             / "repository-consumer-authority-v1.schema.json").read_text())
        self.assertEqual(set(a), set(schema["required"]))
        self.assertEqual(schema["properties"]["contract_version"]["const"],
                         a["contract_version"])
        self.assertEqual(a["contract_version"], "ep-repository-consumer-authority/v1")
        self.assertEqual((a["instance_id"], a["project_id"], a["consumer_id"]),
                         (self.instance, "project-test", "forge"))
        self.assertEqual((a["repository_role"], b["repository_role"]), ("authority", "child"))
        self.assertEqual((a["github_repository"], b["github_repository"]),
                         ("pcvantol/repository-a", "pcvantol/repository-b"))
        self.assertEqual(a["submission_authorization"], "PARALLEL_ACTION_INTAKE")
        self.assertFalse(a["dispatch_authorized"])
        self.assertNotIn(self.token, json.dumps(a))
        self.assertNotIn(str(self.roots["repository-a"]), json.dumps(a))
        self.assertNotEqual(a["binding_revision"], b["binding_revision"])
        self.assertNotEqual(a["authority_digest"], b["authority_digest"])
        self.assertEqual(first[2]["Cache-Control"], "no-store")
        self.assertEqual(self._read(headers={
            "EP-Authority-Revision": str(a["binding_revision"]),
            "EP-Authority-Digest": str(a["authority_digest"]),
        })[1], a)
        self._stop_http()
        self._start_http()
        self.assertEqual(self._read()[1], a)
        self.assertEqual(self._read("repository-b")[1], b)
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            for table in ("ep_submissions", "ep_parallel_action_intakes", "ep_execution_runs"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_authority_is_stable_across_new_http_service_processes(self) -> None:
        expected = self._read()[1]
        self._stop_http()
        try:
            for _ in range(2):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                source = (
                    "import http.server; from pathlib import Path; "
                    "from engineering_platform import server; "
                    f"handler=http.server.ThreadingHTTPServer(('127.0.0.1',{port}),server._HealthHandler); "
                    f"handler.data_root=Path({str(self.data_root)!r}); handler.serve_forever()"
                )
                environment = {**os.environ, "PYTHONPATH": str(Path(server.__file__).resolve().parents[1])}
                process = subprocess.Popen(  # nosec B603
                    [sys.executable, "-c", source], cwd=self.root,
                    env=environment, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        try:
                            response = self._read(port=port)
                            break
                        except OSError:
                            time.sleep(.05)
                    else:
                        self.fail("repository authority HTTP process did not start")
                    self.assertEqual(response[0], 200)
                    self.assertEqual(response[1], expected)
                finally:
                    process.terminate()
                    process.wait(timeout=5)
        finally:
            self._start_http()

    def test_wrong_identity_scope_and_pins_fail_closed(self) -> None:
        a = self._read()[1]
        for selected, expected in (
            ({"Authorization": None}, 401),
            ({"Authorization": "Bearer wrong"}, 401),
            ({"EP-Instance-ID": None}, 400),
            ({"EP-Consumer-ID": None}, 400),
            ({"EP-GitHub-Repository": None}, 400),
            ({"EP-Instance-ID": "wrong"}, 403),
            ({"EP-Consumer-ID": "other"}, 403),
            ({"EP-GitHub-Repository": "pcvantol/wrong"}, 403),
            ({"EP-Authority-Revision": "sha256:" + "0" * 64}, 409),
            ({"EP-Authority-Digest": "sha256:" + "0" * 64}, 409),
            ({"EP-Authority-Revision": "invalid"}, 400),
            ({"EP-Authority-Digest": "invalid"}, 400),
        ):
            with self.subTest(selected=selected):
                status, rejected, _ = self._read(headers=selected)
                self.assertEqual(status, expected)
                self.assertEqual(set(rejected), {"error"})
        self.assertEqual(self._read(token=self.other_token)[0], 403)
        self.assertEqual(self._read(project_id="other-project")[0], 403)
        self.assertEqual(self._read(repository_id="missing")[1],
                         {"error": "AUTHORITY_GRANT_INACTIVE"})
        self.assertEqual(self._read(headers={
            "EP-Authority-Revision": str(a["binding_revision"]),
            "EP-Authority-Digest": str(a["authority_digest"]),
        })[0], 200)

    def test_rebinding_and_grant_change_only_selected_repository(self) -> None:
        a = self._read("repository-a")[1]
        b = self._read("repository-b")[1]
        rebound = self._checkout("repository-b", suffix="-rebound")
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            local_repository_binding.bind_local_repository(
                connection, project_id="project-test", repository_id="repository-b",
                local_root=rebound, data_root=self.data_root, rebind=True,
            )
        changed = self._read("repository-b")[1]
        self.assertNotEqual(changed["binding_revision"], b["binding_revision"])
        self.assertNotEqual(changed["authority_digest"], b["authority_digest"])
        self.assertEqual(self._read("repository-a")[1], a)
        self.assertEqual(self._read("repository-b", headers={
            "EP-Authority-Revision": str(b["binding_revision"]),
        })[0], 409)
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            self._grant(connection, "repository-b", active=False)
        self.assertEqual(self._read("repository-b")[1], {"error": "AUTHORITY_GRANT_INACTIVE"})
        self.assertEqual(self._read("repository-a")[1], a)
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            self._grant(connection, "repository-b", active=True)
        self.assertNotEqual(self._read("repository-b")[1]["binding_revision"],
                            changed["binding_revision"])
        self.assertEqual(self._read("repository-a")[1], a)

    def test_sibling_registration_does_not_change_existing_authority(self) -> None:
        a = self._read("repository-a")[1]
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            project_topology.register_server_local_topology(
                connection, declaration=self._declaration("repository-b", "child"),
            )
        self.assertEqual(self._read("repository-a")[1], a)
        self.assertEqual(self._read(headers={
            "EP-Authority-Revision": str(a["binding_revision"]),
            "EP-Authority-Digest": str(a["authority_digest"]),
        })[0], 200)

    def test_project_authority_topology_must_match_registered_authority(self) -> None:
        a = self._read()[1]
        database = self.data_root / server.SERVER_DATABASE_FILENAME
        with sqlite_connection(database) as connection:
            connection.execute(
                "UPDATE ep_project_registrations SET attachment_contract=? WHERE project_id=?",
                (json.dumps({"schema_version": "1.0", "authority_repository_id": "missing"}),
                 "project-test"),
            )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_PROJECT_TOPOLOGY_DRIFT"})
        with sqlite_connection(database) as connection:
            connection.execute(
                "UPDATE ep_project_registrations SET attachment_contract=? WHERE project_id=?",
                (json.dumps({"schema_version": "1.0", "authority_repository_id": "repository-a"}),
                 "project-test"),
            )
            connection.execute(
                "UPDATE ep_repository_registrations SET role='child' WHERE repository_id='repository-a'",
            )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_PROJECT_TOPOLOGY_DRIFT"})
        with sqlite_connection(database) as connection:
            connection.execute(
                "UPDATE ep_repository_registrations SET role='authority' WHERE repository_id='repository-a'",
            )
        self.assertEqual(self._read()[1], a)

    def test_effective_push_origin_must_match_fetch_origin(self) -> None:
        checkout = self.roots["repository-a"]
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "--push", "origin",
             "https://github.com/pcvantol/different.git"], check=True,
        )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_GITHUB_REPOSITORY_MISMATCH"})
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "--push", "origin",
             "https://github.com/pcvantol/repository-a.git"], check=True,
        )
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "--add", "--push", "origin",
             "https://github.com/pcvantol/different.git"], check=True,
        )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_BINDING_UNAVAILABLE"})
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "--delete", "--push", "origin",
             "https://github.com/pcvantol/different.git"], check=True,
        )
        self.assertEqual(self._read()[0], 200)
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "config", "--unset-all", "remote.origin.pushurl"],
            check=True,
        )
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "config",
             "url.https://github.com/pcvantol/different.git.pushInsteadOf",
             "https://github.com/pcvantol/repository-a.git"], check=True,
        )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_GITHUB_REPOSITORY_MISMATCH"})

    def test_inactive_and_drifted_source_never_produce_authority(self) -> None:
        checkout = self.roots["repository-a"]
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "origin",
             "https://github.com/pcvantol/different.git"], check=True,
        )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_GITHUB_REPOSITORY_MISMATCH"})
        subprocess.run(  # nosec B603
            ["git", "-C", str(checkout), "remote", "set-url", "origin",
             "https://github.com/pcvantol/repository-a.git"], check=True,
        )
        target = config_path(checkout)
        declaration = json.loads(target.read_text())
        declaration["validation"] = {"kind": "command", "entrypoint": "changed"}
        target.write_text(json.dumps(declaration))
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_ATTACHMENT_DRIFT"})
        target.write_text(json.dumps(self._declaration("repository-a", "authority")))
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            local_repository_binding.unbind_local_repository(
                connection, project_id="project-test", repository_id="repository-a",
            )
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_BINDING_UNAVAILABLE"})
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            local_repository_binding.bind_local_repository(
                connection, project_id="project-test", repository_id="repository-a",
                local_root=checkout, data_root=self.data_root,
            )
            connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='project-test'")
        self.assertEqual(self._read()[1], {"error": "AUTHORITY_PROJECT_INACTIVE"})
        with sqlite_connection(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("UPDATE ep_project_registrations SET status='ACTIVE' WHERE project_id='project-test'")
            connection.execute("UPDATE ep_consumer_registrations SET status='REVOKED' WHERE consumer_id='forge'")
        self.assertEqual(self._read()[1], {"error": "UNAUTHENTICATED"})


if __name__ == "__main__":
    unittest.main()
