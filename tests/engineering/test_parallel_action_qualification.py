"""PA-EQ installed boundary: Forge fixture, HTTP, real overlap and readback."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import parallel_action_admission as admission
from engineering_platform import server, submission_service
from engineering_platform.execution_timing import record_phase
from engineering_platform.lifecycle_worker import LifecycleWorker
from engineering_platform.provider_usage import ProviderInvocation, persist_provider_invocation
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_parallel_action_admission as pa_harness


# SHA-256 of Forge protected PA-F0 fixture at Forge main 0f3cc55c24fa580c.
FORGE_FIXTURE_SHA256 = "b938388fb7a031c574407b62f07cb3ed7b12d692170dd4acf81cba5a14a3ab9c"


def _provider_environment(rendezvous: Path) -> dict[str, str]:
    blocked = {"EP_QUALIFICATION_GITHUB_WRITE_FLOW",
               "EP_QUALIFICATION_GITHUB_REPOSITORY",
               "EP_QUALIFICATION_GITHUB_BRANCH"}
    return {**{key: value for key, value in os.environ.items()
               if key not in blocked},
            "EP_QUALIFICATION_PA_E2_OVERLAP_DIR": str(rendezvous)}


class ParallelActionInstalledQualificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = pa_harness.ParallelActionAdmissionTest(methodName="runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.assertEqual(sha256(pa_harness.FIXTURE.read_bytes()).hexdigest(),
                         FORGE_FIXTURE_SHA256)
        for repository_id, root in self.harness.repository_roots.items():
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin",
                            f"https://github.com/fixture/{repository_id}.git"), check=True)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.httpd.data_root = self.harness.root
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = (f"http://127.0.0.1:{self.httpd.server_port}"
                     "/v1/projects/project-test")
        self.authorization = {"Authorization": f"Bearer {self.harness.credential}"}

    def _stop_server(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _post(self, path: str, payload: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        request = Request(self.base + path, data=payload, method="POST",
                          headers={**headers, "Content-Type": "application/json"})
        with urlopen(request, timeout=15) as response:  # nosec B310 - isolated loopback
            return response.status, json.load(response)

    def _get(self, path: str, headers: dict[str, str] | None = None) -> dict:
        with urlopen(Request(self.base + path, headers=headers or self.authorization),
                     timeout=15) as response:  # nosec B310 - isolated loopback
            self.assertEqual(response.status, 200)
            return json.load(response)

    def _denied(self, request: Request, status: int, code: str) -> None:
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=15)  # nosec B310 - isolated loopback
        with closing(raised.exception) as error:
            body = json.load(error)
            self.assertEqual(error.code, status, body)
            self.assertEqual(body["error"], code)

    def _stage(self, action_id: str) -> dict:
        status, result = self._post(
            "/parallel-action-intakes", json.dumps(self.harness.graph).encode(),
            self._stage_headers(action_id),
        )
        self.assertEqual(status, 201)
        self.assertFalse(result["dispatch_authorized"])
        return result

    def _stage_headers(self, action_id: str) -> dict[str, str]:
        return {
            **self.authorization,
            "EP-Action-ID": action_id,
            "EP-Action-Revision": "1",
            "EP-Intent-ID": "intent-test",
            "EP-Intent-Revision": "1",
            "EP-Correlation-ID": "corr-" + action_id,
            "Idempotency-Key": "key-" + action_id,
            "EP-Write-Scope": "repository-only",
            "EP-Policy-Digest": admission.SUPPORTED_PA_E2_POLICY_DIGEST,
            "EP-Concurrency-Profile": "DIFFERENT_REPOSITORIES_V1",
        }

    def _submit(self, action_id: str, staged: dict) -> dict:
        payload = self._submission_payload(action_id, staged)
        status, result = self._post(
            "/submissions", json.dumps(payload).encode(), self.authorization,
        )
        self.assertEqual(status, 200)
        return result

    def test_forge_http_to_installed_ep_two_provider_processes_and_collection(self) -> None:
        # The protected graph is adapted only to this isolated EP instance and
        # two freshly initialized Git baselines; Forge's edges stay unchanged.
        original = json.loads(pa_harness.FIXTURE.read_bytes())
        self.assertEqual(
            [(row["action_id"], row["dependencies"]) for row in self.harness.graph["actions"]],
            [(row["action_id"], row["dependencies"]) for row in original["actions"]],
        )
        self._denied(Request(
            self.base + "/parallel-action-intakes",
            data=json.dumps(self.harness.graph).encode(), method="POST",
            headers={"Content-Type": "application/json"}), 401, "UNAUTHENTICATED")
        with sqlite_connection(self.harness.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_parallel_action_intakes").fetchone()[0], 0)
            foreign_token = submission_service.issue_consumer_credential(
                connection, consumer_id="foreign", project_id="project-test",
            )["credential"]
        foreign_headers = {**self._stage_headers("ACTION-A"),
                           "Authorization": "Bearer " + foreign_token,
                           "Content-Type": "application/json"}
        self._denied(Request(
            self.base + "/parallel-action-intakes",
            data=json.dumps(self.harness.graph).encode(), method="POST",
            headers=foreign_headers), 409, "SCOPE_UNAVAILABLE")
        with sqlite_connection(self.harness.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_parallel_action_intakes").fetchone()[0], 0)
        staged = {action: self._stage(action) for action in
                  ("ACTION-A", "ACTION-B", "ACTION-Q")}
        intake_a = str(staged["ACTION-A"]["intake_id"])
        intake_q = str(staged["ACTION-Q"]["intake_id"])
        self.assertEqual(self._get("/parallel-action-intakes/" + intake_q)["state"],
                         "WAITING_DEPENDENCY")
        self._denied(Request(
            self.base + "/parallel-action-intakes/" + intake_a + "/collection",
            headers={"Authorization": "Bearer " + foreign_token}), 404,
            "PARALLEL_COLLECTION_NOT_FOUND")

        valid_a = self._submission_payload("ACTION-A", staged["ACTION-A"])
        for authorization in ({}, {"Authorization": "Bearer " + foreign_token}):
            self._denied(Request(
                self.base + "/submissions", data=json.dumps(valid_a).encode(),
                method="POST", headers={**authorization,
                                        "Content-Type": "application/json"}),
                401 if not authorization else 403,
                "UNAUTHENTICATED" if not authorization else "PARALLEL_PRINCIPAL_REQUIRED")
        with sqlite_connection(self.harness.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 0)
        wrong_target = self._submission_payload("ACTION-A", staged["ACTION-A"])
        wrong_target["repository_id"] = "repository-b"
        self._denied(Request(
            self.base + "/submissions", data=json.dumps(wrong_target).encode(),
            method="POST", headers={**self.authorization,
                                    "Content-Type": "application/json"}),
            400, "INVALID_FORGE_PROVENANCE")

        accepted = {action: self._submit(action, staged[action])
                    for action in ("ACTION-A", "ACTION-B")}
        for result in accepted.values():
            self.assertEqual(result["transport"], "HTTP")
            self.assertFalse(result["duplicate"])
            self.assertTrue(result["receipt"])
        submissions = {action: str(result["submission_id"])
                       for action, result in accepted.items()}
        self.assertEqual(len(set(submissions.values())), 2)
        self._denied(Request(
            self.base + "/submissions",
            data=json.dumps(self._submission_payload("ACTION-Q", staged["ACTION-Q"])).encode(),
            method="POST", headers={**self.authorization,
                                    "Content-Type": "application/json"}),
            409, "WAITING_DEPENDENCY")

        worker = LifecycleWorker(self.harness.root)
        with patch.dict(os.environ, {"EP_QUALIFICATION_INITIALIZE_ONLY": "1"}):
            self.assertTrue(worker.run_once())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with sqlite_connection(self.harness.database) as connection:
                    rows = connection.execute(
                        "SELECT submission_id,run_id FROM ep_parity_lifecycle_dispatches "
                        "WHERE submission_id IN (?,?) AND state='RUNNING'",
                        tuple(submissions.values()),
                    ).fetchall()
                if len(rows) == 2 and worker.diagnostics().dispatched == 2:
                    break
                time.sleep(.02)
            self.assertEqual(len(rows), 2)
        run_for_submission = dict(rows)
        self.assertEqual(len(set(run_for_submission.values())), 2)

        rendezvous = Path(self.harness.temporary.name) / "pa-eq-provider-overlap"
        rendezvous.mkdir()
        driver = Path(__file__).parents[1] / "fixtures" / "pa_e2_provider_driver.py"
        env = _provider_environment(rendezvous)
        processes: list[subprocess.Popen[str]] = []
        try:
            for action in ("ACTION-A", "ACTION-B"):
                processes.append(subprocess.Popen(
                    (sys.executable, str(driver), str(self.harness.root), submissions[action]),
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if len(list(rendezvous.glob("*.provider-started"))) == 2:
                    break
                if any(process.poll() is not None for process in processes):
                    break
                time.sleep(.02)
            starts = {path.name.split(".")[0]: json.loads(path.read_text())
                      for path in rendezvous.glob("*.provider-started")}
            self.assertEqual(set(starts), {"repository-a", "repository-b"})
            self.assertEqual(len({row["pid"] for row in starts.values()}), 2)
            self.assertEqual(len({row["root"] for row in starts.values()}), 2)
            self.assertTrue(all(process.poll() is None for process in processes))
            with sqlite_connection(self.harness.database) as connection:
                held = connection.execute(
                    "SELECT run_id FROM ep_execution_leases "
                    "WHERE lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL"
                ).fetchall()
            self.assertEqual({row[0] for row in held}, set(run_for_submission.values()))
            self.assertEqual(self._get("/parallel-action-intakes/" + intake_q)["state"],
                             "WAITING_DEPENDENCY")
            (rendezvous / "release").write_text("go", encoding="utf-8")
            outputs = [process.communicate(timeout=20) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0, 0], outputs)
            self.assertEqual({json.loads(stdout)["run_id"] for stdout, _ in outputs},
                             set(run_for_submission.values()))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

        ends = {path.name.split(".")[0]: json.loads(path.read_text())
                for path in rendezvous.glob("*.provider-ended")}
        self.assertEqual(set(ends), set(starts))
        self.assertLess(max(row["monotonic_ns"] for row in starts.values()),
                        min(row["monotonic_ns"] for row in ends.values()))
        for action, repository_id in (("ACTION-A", "repository-a"),
                                      ("ACTION-B", "repository-b")):
            start, end = starts[repository_id], ends[repository_id]
            self.assertEqual(start["pid"], end["pid"])
            run_id = run_for_submission[submissions[action]]
            start_at = datetime.fromtimestamp(start["wall_ns"] / 1e9, timezone.utc)
            end_at = datetime.fromtimestamp(end["wall_ns"] / 1e9, timezone.utc)
            elapsed_ms = round((end["monotonic_ns"] - start["monotonic_ns"]) / 1e6)
            persist_provider_invocation(self.harness.root, ProviderInvocation(
                run_id, 1, "controlled_provider", None, "PROVIDER_EXECUTION",
                "IMPLEMENTATION", start_at.isoformat(), end_at.isoformat(),
                elapsed_ms, {}, invocation_id="pa-eq-" + action,
            ), central_database=self.harness.database)
            record_phase(self.harness.root, run_id, "TOTAL_EXECUTION",
                         started_at=start_at, completed_at=end_at)

        collection_path = "/parallel-action-intakes/" + intake_a + "/collection"
        model = self._get(collection_path)
        data = model["data"]["parallel_action_collection"]
        self.assertEqual({row["action_id"] for row in data["actions"]},
                         {"ACTION-A", "ACTION-B", "ACTION-Q"})
        by_action = {row["action_id"]: row for row in data["actions"]}
        for action in ("ACTION-A", "ACTION-B"):
            self.assertEqual(by_action[action]["runs"][0]["run_id"],
                             run_for_submission[submissions[action]])
        self.assertEqual(by_action["ACTION-Q"]["state"],
                         "NOT_EVALUATED_IN_COLLECTION")
        self.assertEqual(data["summary"]["run_count"], 2)
        self.assertTrue(data["summary"]["actual_provider_overlap"])
        self.assertGreater(data["summary"]["provider_multi_action_overlap_ms"], 0)
        self.assertNotEqual(data["summary"]["usage_metrics"]["input_tokens"]["coverage"],
                            "COMPLETE")
        self.assertEqual(data["mission_acceptance"], "NOT_EVALUATED_BY_EP")
        self.assertFalse(data["dispatch_authorized"])
        export = collection_path + "/export?locale=en&snapshot_id=" + model["snapshot_id"]
        downloaded = self._get(export + "&format=json")
        self.assertEqual(downloaded["snapshot_id"], model["snapshot_id"])
        self.assertEqual(downloaded["data"], model["data"])
        with urlopen(Request(self.base + export + "&format=markdown",
                             headers=self.authorization), timeout=15) as response:  # nosec B310
            markdown = response.read().decode()
        for action in ("ACTION-A", "ACTION-B", "ACTION-Q"):
            self.assertIn("Action: `" + action + "`", markdown)
        self.assertIn(model["snapshot_id"], markdown)
        complete = markdown.split("## Complete Action collection data (JSON)\n\n", 1)[1]
        complete = complete.split("\n## ", 1)[0]
        self.assertEqual(json.loads("\n".join(
            line[4:] for line in complete.splitlines() if line.startswith("    ")
        )), data)

    def _submission_payload(self, action_id: str, staged: dict) -> dict:
        request = self.harness.request(
            action_id, staged, policy_digest=admission.SUPPORTED_PA_E2_POLICY_DIGEST,
        )
        return {
            "repository_id": request.repository_id,
            "producer": {"id": request.producer_id, "type": request.producer_type,
                         "version": request.producer_version},
            "prompt": request.prompt, "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id, "mission_id": request.mission_id,
            "engineering_action_id": request.engineering_action_id,
            "constraints": request.constraints,
        }

    def test_controlled_provider_refuses_ambient_external_write_flow(self) -> None:
        from tests.fixtures.pa_e2_provider_driver import ControlledProvider

        with patch.dict(os.environ, {
            "EP_QUALIFICATION_GITHUB_WRITE_FLOW": "1",
            "EP_QUALIFICATION_GITHUB_REPOSITORY": "fixture/repository-a",
            "EP_QUALIFICATION_GITHUB_BRANCH": "unexpected",
        }):
            self.assertFalse(ControlledProvider._github_write_target(
                self.harness.repository_roots["repository-a"],
            ))
            child = _provider_environment(Path(self.harness.temporary.name))
            self.assertNotIn("EP_QUALIFICATION_GITHUB_WRITE_FLOW", child)
            self.assertNotIn("EP_QUALIFICATION_GITHUB_REPOSITORY", child)
            self.assertNotIn("EP_QUALIFICATION_GITHUB_BRANCH", child)
