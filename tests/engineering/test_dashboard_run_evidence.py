"""Console readback of evidence produced by the real stored run services."""
from __future__ import annotations

import unittest
import copy
import tempfile
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from engineering_platform import server
from engineering_platform.capability_review import specialist_readback
from engineering_platform.dashboard_run_evidence import projection, _presentation
from engineering_platform.provider_usage import canonical_provider_invocations
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_specialist_selection_disposition as pipeline


class DashboardRunEvidenceTests(unittest.TestCase):
    def test_detached_view_redacts_host_paths_without_changing_stored_advice_or_identity(self):
        supplied = {"run_id": "run-own", "profile": "sha256:" + "a" * 64,
                    "paths": ["docs/readme.md", "validation://run-own/control", "https://example.test/docs/guide", "git://example.test/repo"],
                    "advice": ("Read /Users/example/file.txt", "Read C:\\Users\\example\\file.txt",
                               "Read \\\\server\\private\\file.txt", "Read ~/local.txt",
                               "Read file:///etc/local.txt", "Read FILE:///etc/local.txt", "Read </Users/example/file.txt>.", "Source:/Users/example/file.txt", "URI:FILE:///etc/local.txt", "<script>alert(1)</script>"),
                    "unknown": None, "count": 0}
        original = copy.deepcopy(supplied)
        flag = [False]
        detached = _presentation(supplied, flag)
        self.assertTrue(flag[0])
        self.assertEqual(supplied, original)
        self.assertEqual(detached["run_id"], supplied["run_id"])
        self.assertEqual(detached["profile"], supplied["profile"])
        self.assertEqual(detached["paths"], supplied["paths"])
        self.assertEqual(detached["advice"], ("Read [REDACTED]",) * 6 + ("Read <[REDACTED]>.", "Source:[REDACTED]", "URI:[REDACTED]", "<script>alert(1)</script>",))
        self.assertIsNone(detached["unknown"])
        self.assertEqual(detached["count"], 0)

    def test_actual_failed_uncertain_and_host_duplicate_outcomes_preserve_read_effects(self):
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        for mode in ("failed", "uncertain-result", "duplicate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="ep-run-evidence-installation-") as installation, patch.dict(
                    "os.environ", {"EP_QUALIFICATION_DETERMINISTIC_FLOW": "1",
                                   "ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
                # CI already installs the exact native dependency on PATH;
                # expose it through this test-owned managed-prefix contract.
                # An ambient prefix may refer to a removed prior fixture. No fake
                # runtime/configuration or product admission bypass.
                executable = shutil.which("codex")
                self.assertIsNotNone(executable)
                version = subprocess.check_output([executable, "--version"], text=True).strip()
                self.assertEqual(version, "codex-cli 0.160.1")
                native = Path(installation) / "native"
                (native / "bin").mkdir(parents=True)
                (native / "bin/codex").symlink_to(executable)
                os.environ["EP_MANAGED_CODEX_CLI_PREFIX"] = str(native)
                canary = StoredConsoleCanary(finding_mode=mode)
                try:
                    run_id = canary.generate()
                    before = canary.effect_snapshot()
                    evidence = server._central_console_lifecycle(canary.fixture.data, run_id)["run_evidence"]["specialists"]
                    if mode == "duplicate":
                        self.assertEqual(evidence["duplicate_finding_count"], 1)
                        self.assertEqual(evidence["duplicate_observations"], 0)
                        self.assertIn("DUPLICATE", {item["disposition"] for item in evidence["findings"]})
                    else:
                        expected = "FAILED" if mode == "failed" else "UNCERTAIN"
                        self.assertEqual({item["state"] for item in evidence["outcomes"]}, {expected})
                        if mode == "uncertain-result": self.assertIsNone(evidence["actual_model_invocation_count"])
                        self.assertEqual(evidence["failed_invocation_count"], 2 if mode == "failed" else 0)
                        self.assertEqual(evidence["successful_invocation_count"], 0)
                    self.assertEqual(canary.effect_snapshot(), before)
                finally:
                    canary.close()

    def test_missing_legacy_evidence_is_unknown_not_zero_or_authority(self):
        for checkpoint in ({}, {"specialist_records": []}, {"specialist_records": None}):
            with self.subTest(checkpoint=checkpoint):
                result = projection(checkpoint, "legacy")
                self.assertIsNone(result["specialists"]["actual_model_invocation_count"])
                self.assertEqual(result["specialists"]["state"], "NOT_RECORDED")
                self.assertEqual(result["publication"]["status"], "NOT_RECORDED")
                self.assertFalse(result["execution_authority"])

    def test_corrupt_and_foreign_publication_is_explicitly_unavailable(self):
        for intent in ([], {}, "PREPARED", {"run_id": "foreign"}):
            with self.subTest(intent=intent):
                result = projection({"specialist_records": "corrupt", "publication_intent": intent}, "own")
                self.assertEqual(result["specialists"]["state"], "UNAVAILABLE")
                self.assertEqual(result["publication"]["status"], "UNAVAILABLE")
                self.assertIsNone(result["specialists"]["actual_model_invocation_count"])

    def test_central_console_keeps_actual_specialist_disposition_and_publication_identity(self):
        # The fixture executes the production runner against real Git/CENTRAL.
        # Only external model/GitHub and capacity/readiness transport are doubles.
        case = pipeline.SpecialistPipelineTests()
        case.setUp()
        try:
            case.test_real_selection_primary_application_controls_assurance_and_publication_join()
            stored = case.store.load("specialist-run")
            expected = specialist_readback(stored.specialist_records)
            value = server._central_console_lifecycle(case.fixture.data, stored.run_id)
            evidence = value["run_evidence"]
            self.assertEqual(evidence["run_id"], stored.run_id)
            self.assertEqual(evidence["recorded_candidate_sha"], stored.assurance_profile["candidate_sha"])
            self.assertEqual(evidence["specialists"]["findings"], expected["findings"])
            self.assertEqual(evidence["specialists"]["completed_invocation_count"], 2)
            self.assertEqual(evidence["specialists"]["actual_model_invocation_count"], 2)
            self.assertEqual({item["invocation_id"] for item in evidence["specialists"]["invocations"]},
                             {item["invocation_id"] for item in expected["findings"]})
            self.assertEqual(evidence["publication"]["status"], stored.publication_intent["status"])
            self.assertEqual(evidence["publication"]["candidate_sha"], stored.publication_intent["candidate_sha"])
            with sqlite_connection(case.fixture.database) as connection:
                rows = canonical_provider_invocations(connection, stored.run_id)
            # Corrupt ledger evidence must never manufacture a started invocation.
            checkpoint = {"repository": stored.repository, "specialist_records": stored.specialist_records,
                          "publication_intent": stored.publication_intent}
            for key, invalid in (("invocation_id", None), ("churn", "invalid JSON"),
                                 ("role", "reviewer:foreign"), ("phase", None),
                                 ("run_id", "foreign-run"), ("started_at", None),
                                 ("started_at", "2026-10-10"), ("completed_at", "invalid timestamp")):
                damaged = [dict(row) for row in rows]
                target = next(row for row in damaged if row["phase"] == "CAPABILITY_REVIEW")
                target[key] = invalid
                with self.subTest(ledger_field=key):
                    result = projection(checkpoint, stored.run_id, invocations=damaged)
                    self.assertEqual(result["specialists"]["invocation_evidence_state"], "UNAVAILABLE")
                    self.assertIsNone(result["specialists"]["actual_model_invocation_count"])
            original = copy.deepcopy(checkpoint)
            selected = tuple(event for event in stored.specialist_records if event["kind"] == "SELECTION")
            not_started = projection({"repository": stored.repository, "specialist_records": selected}, stored.run_id)
            self.assertEqual(not_started["specialists"]["actual_model_invocation_count"], 0)
            self.assertEqual(not_started["specialists"]["invocation_evidence_state"], "NOT_STARTED")
            for observations in (None, [], [dict(row) for row in rows if row["phase"] not in
                                            {"CAPABILITY_REVIEW", "CAPABILITY_REVIEW_DISPATCH"}]):
                result = projection(checkpoint, stored.run_id, invocations=observations)
                self.assertIsNone(result["specialists"]["actual_model_invocation_count"])
            duplicate = projection(checkpoint, stored.run_id, invocations=rows + rows)
            self.assertEqual(duplicate["specialists"]["invocation_evidence_state"], "UNAVAILABLE")
            foreign = projection(checkpoint, "foreign-run", invocations=rows)
            self.assertEqual(foreign["specialists"]["state"], "UNAVAILABLE")
            self.assertEqual(foreign["publication"]["status"], "UNAVAILABLE")
            for _ in range(3):
                projection(checkpoint, stored.run_id, invocations=rows)
            self.assertEqual(checkpoint, original)
            self.assertEqual(case.store.load(stored.run_id).specialist_records, stored.specialist_records)
            with sqlite_connection(case.fixture.database) as connection:
                self.assertEqual(canonical_provider_invocations(connection, stored.run_id), rows)
        finally:
            case.doCleanups()


class DashboardExternalTransportTests(unittest.TestCase):
    def setUp(self):
        # Own the real pinned native context, including when an invalid prefix
        # was inherited. No login, capacity service or authority is substituted.
        self.native = tempfile.TemporaryDirectory(prefix="ep-metadata-native-")
        self.addCleanup(self.native.cleanup)
        executable = shutil.which("codex")
        self.assertIsNotNone(executable, "Declared pinned native CLI must be on PATH")
        self.assertEqual(subprocess.run((executable, "--version"), check=True, text=True,
                                      capture_output=True).stdout.strip(), "codex-cli 0.160.1")
        prefix = Path(self.native.name)
        (prefix / "bin").mkdir()
        (prefix / "bin/codex").symlink_to(Path(executable).resolve())
        patcher = patch.dict(os.environ, {"EP_MANAGED_CODEX_CLI_PREFIX": str(prefix)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_capacity_handler_uses_local_metadata_transport_and_preserves_execution(self):
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        from urllib.request import urlopen
        import json
        with tempfile.TemporaryDirectory(prefix="ep-dashboard-metadata-install-") as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            try:
                baseline = canary.effect_snapshot()
                origin = canary.start_listener()
                with urlopen(origin + "/api/provider-capacity", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.load(response)
                self.assertEqual(payload["rate_limits"]["windows"][0]["used_percent"], 0)
                with urlopen(origin + "/api/provider-login-status", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    readiness = json.load(response)["providers"]
                self.assertEqual(readiness["codex"]["state"], "READY")
                self.assertEqual(readiness["github"]["state"], "READY")
                observed = canary.transport_snapshot()
                self.assertGreaterEqual(observed["readiness_metadata_requests"], 2)
                self.assertEqual(observed["metadata_requests"], 1)
                self.assertEqual(observed["metadata_sessions"], 1)
                self.assertEqual(observed["native_app_server_starts"], 0)
                self.assertEqual(canary.console_model_requests, 0)
                self.assertEqual(canary.effect_snapshot(), baseline)
            finally:
                canary.close()

    def test_removed_metadata_adapter_fails_before_live_process_and_account_use(self):
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        from urllib.request import urlopen
        with tempfile.TemporaryDirectory(prefix="ep-dashboard-removed-guard-") as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            try:
                origin = canary.start_listener(omit_metadata_adapter=True)
                with urlopen(origin + "/api/provider-capacity", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
                self.assertEqual(canary.console_model_requests, 0)
                with self.assertRaises(AssertionError):
                    self.assertGreater(canary.transport_snapshot()["metadata_requests"], 0)
                with self.assertRaisesRegex(AssertionError, "cleanup failed"):
                    canary.close()
                self.assertEqual(canary.listener.closed["unexpected_native_attempts"], 1)
                self.assertTrue(canary.listener.closed["worker_ended"])
            finally:
                if canary.listener is None or canary.listener.closed is None:
                    canary.close()

    def test_unexpected_execution_transport_fails_closed_even_when_caller_catches_it(self):
        from tests.engineering.dashboard_run_evidence_fixture import IsolatedProviderTransport, CodexCliProvider
        transport = IsolatedProviderTransport().start()
        try:
            with patch("engineering_platform.providers._start_process") as native:
                with self.assertRaisesRegex(AssertionError, "Unexpected external"):
                    CodexCliProvider().spawn(Path.cwd(), ("codex", "exec", "unexpected"))
                native.assert_not_called()
            from engineering_platform.providers import LocalProcessProvider
            with patch("engineering_platform.providers.subprocess.run") as native:
                with self.assertRaisesRegex(AssertionError, "Unexpected external"):
                    LocalProcessProvider().execute(Path.cwd(), ("codex", "app-server"))
                native.assert_not_called()
            with self.assertRaisesRegex(AssertionError, "Unexpected external transport attempts"):
                transport.snapshot()
        finally:
            with self.assertRaises(AssertionError):
                transport.close()


class DashboardQuotaTransportTests(unittest.TestCase):
    setUp = DashboardExternalTransportTests.setUp

    def test_real_quota_parser_cold_warm_contexts_and_new_worker(self):
        import json
        from urllib.request import urlopen
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary, OwnedReadWorker
        for mode in ("available", "limited", "unavailable", "invalid"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as installation, patch.dict(
                    os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
                canary = StoredConsoleCanary(quota_mode=mode)
                worker = None
                try:
                    origin = canary.start_listener()
                    baseline = canary.effect_snapshot()
                    expected = {"limited": True, "reset_at": 2000000000} if mode == "limited" else {"limited": False}
                    for context in ("project", "none", None, "project"):
                        suffix = "?project=" + context if context else ""
                        with urlopen(origin + "/api/dashboard-snapshot" + suffix, timeout=5) as response:
                            snapshot = json.load(response)
                        self.assertEqual(snapshot["scope"], "PROJECT" if context else "PLATFORM")
                        self.assertEqual(snapshot.get("project_id"), context)
                        for _ in range(2):
                            with urlopen(origin + "/api/github-rate-limit", timeout=5) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(json.load(response), expected)
                    self.assertEqual(canary.transport_snapshot()["github_quota_requests"], 8)
                    self.assertEqual(canary.effect_snapshot(), baseline)
                    canary.listener.close()
                    worker = OwnedReadWorker(canary.fixture.data, quota_mode=mode)
                    with urlopen(worker.ready["url"].split("/?")[0] + "/api/github-rate-limit", timeout=5) as response:
                        self.assertEqual(json.load(response), expected)
                    final = worker.close()
                    self.assertEqual(final["transport_observations"]["transport_observations"]["github_quota_requests"], 1)
                    self.assertEqual(final["unexpected_native_attempts"], 0)
                    self.assertTrue(final["owned_process_group_ended"])
                finally:
                    if worker and worker.closed is None:
                        worker.close()
                    canary.close()

    def test_removing_only_quota_adapter_is_detected_before_native_use(self):
        import json
        from urllib.request import urlopen
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        with tempfile.TemporaryDirectory() as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            try:
                origin = canary.start_listener(omit_quota_adapter=True)
                before = canary.effect_snapshot()
                with urlopen(origin + "/api/github-rate-limit", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response), {"limited": False})
                self.assertEqual(canary.effect_snapshot(), before)
                with self.assertRaises(AssertionError):
                    self.assertGreater(canary.transport_snapshot()["github_quota_requests"], 0)
                with self.assertRaisesRegex(AssertionError, "cleanup failed"):
                    canary.close()
                self.assertEqual(canary.listener.closed["unexpected_native_attempts"], 1)
                self.assertTrue(canary.listener.closed["owned_process_group_ended"])
            finally:
                if canary.listener is None or canary.listener.closed is None:
                    canary.close()

    def test_unknown_and_malformed_github_commands_are_not_exempt(self):
        from engineering_platform.providers import GitHubProvider
        from tests.engineering.dashboard_run_evidence_fixture import IsolatedProviderTransport
        for args in (("api", "user"), ("api", "rate_limit", "--hostname", "other"), ("api",), ()):
            with self.subTest(args=args):
                transport = IsolatedProviderTransport().start()
                try:
                    with patch("engineering_platform.providers.subprocess.run") as native:
                        with self.assertRaisesRegex(AssertionError, "Unexpected external"):
                            GitHubProvider().github(*args)
                        native.assert_not_called()
                    with self.assertRaises(AssertionError):
                        transport.snapshot()
                finally:
                    with self.assertRaises(AssertionError):
                        transport.close()


class DashboardWorkerLifetimeTests(unittest.TestCase):
    setUp = DashboardExternalTransportTests.setUp
    def _paused_request_close(self, restore_fault=False, disconnect=False, quota=False):
        import json, threading, time
        from urllib.request import urlopen
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        with tempfile.TemporaryDirectory(prefix="ep-worker-lifetime-install-") as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            marker = canary.fixture.area / "external-version"
            results, errors = {}, []
            done = threading.Event()
            origin = canary.start_listener(marker=marker, restore_fault=restore_fault,
                                          quota_marker=marker if quota else None)
            def request():
                try:
                    with urlopen(origin + ("/api/github-rate-limit" if quota else "/api/provider-capacity"), timeout=10) as response:
                        results["status"] = response.status
                        results["payload"] = json.load(response)
                except BaseException as failure:
                    errors.append(failure)
            requester = threading.Thread(target=request)
            requester.start()
            def wait_file(path):
                deadline = time.monotonic() + 5
                while not path.exists():
                    self.assertLess(time.monotonic(), deadline, "Existing metadata deadline reached")
                    time.sleep(.01)
            closer = None
            try:
                wait_file(marker.with_suffix(".paused"))
                def close():
                    try:
                        canary.close()
                    except BaseException as failure:
                        results["cleanup_error"] = failure
                    finally:
                        done.set()
                closer = threading.Thread(target=close)
                closer.start()
                wait_file(marker.with_suffix(".closing"))
                self.assertFalse(done.is_set(), "Close must not finish while the real handler is paused")
                marker.with_suffix(".release").write_text("resume own external version boundary")
                requester.join(timeout=5)
                closer.join(timeout=5)
                self.assertFalse(requester.is_alive())
                self.assertFalse(closer.is_alive())
                self.assertFalse(errors, errors)
                self.assertEqual(results["status"], 200)
                final = canary.listener.closed
                self.assertTrue(final["worker_ended"])
                self.assertTrue(final["ordinary_handlers_completed"])
                if restore_fault:
                    self.assertIsInstance(results.get("cleanup_error"), AssertionError)
                    self.assertFalse(final["isolation_retained_through_worker_exit"])
                    self.assertEqual(final["unexpected_native_attempts"], 1)
                else:
                    self.assertNotIn("cleanup_error", results)
                    self.assertTrue(final["isolation_retained_through_worker_exit"])
                    self.assertEqual(final["unexpected_native_attempts"], 0)
                    observed = final["transport_observations"]["transport_observations"]
                    if quota:
                        self.assertEqual(results["payload"], {"limited": False})
                        self.assertEqual(observed["github_quota_requests"], 1)
                    else:
                        self.assertEqual(results["payload"]["rate_limits"]["windows"][0]["used_percent"], 0)
                        self.assertEqual(observed["metadata_requests"], 1)
            finally:
                if marker.parent.exists():
                    marker.with_suffix(".release").write_text("release on test exception")
                if closer:
                    closer.join(timeout=5)
                requester.join(timeout=5)
                if canary.listener.closed is None:
                    canary.close()

    def test_real_paused_handler_finishes_before_transport_and_data_teardown(self):
        self._paused_request_close()

    def test_real_paused_quota_handler_finishes_before_transport_and_data_teardown(self):
        self._paused_request_close(quota=True)

    def test_restored_original_teardown_fault_is_detected_without_native_escape(self):
        self._paused_request_close(restore_fault=True)

    def test_real_sse_worker_ends_with_isolation_retained_until_process_exit(self):
        from urllib.request import urlopen
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary
        for disconnected in (False, True):
            with self.subTest(client_disconnected=disconnected), tempfile.TemporaryDirectory(
                    prefix="ep-worker-stream-install-") as installation, patch.dict(
                    os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
                canary = StoredConsoleCanary()
                response = None
                try:
                    origin = canary.start_listener()
                    response = urlopen(origin + "/api/events?project=project", timeout=5)
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.readline().startswith(b"retry:"))
                    if disconnected:
                        response.close()
                    canary.close()
                    self.assertTrue(canary.cleanup_observation["worker_ended"])
                    self.assertTrue(canary.cleanup_observation["isolation_retained_through_worker_exit"])
                    self.assertEqual(canary.cleanup_observation["unexpected_native_attempts"], 0)
                    self.assertFalse(canary.fixture.area.exists())
                finally:
                    if response:
                        response.close()
                    if canary.listener.closed is None:
                        canary.close()

    def test_real_partial_listener_startup_ends_own_worker_before_restoring_guards(self):
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary, CodexCliProvider
        original = CodexCliProvider.app_server
        with tempfile.TemporaryDirectory(prefix="ep-worker-start-install-") as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            try:
                with self.assertRaisesRegex(AssertionError, "ended without an observation"):
                    canary.start_listener(bind_port=-1)
                self.assertIsNone(canary.listener)
                self.assertIsNot(CodexCliProvider.app_server, original)
            finally:
                canary.close()
            self.assertIs(CodexCliProvider.app_server, original)
            self.assertFalse(canary.fixture.area.exists())


class DashboardHandlerFailureEvidenceTests(unittest.TestCase):
    def test_unexpected_http_handler_failure_is_retained_after_request_quiescence(self):
        import contextlib
        import http.client
        import io
        import threading
        from http.server import BaseHTTPRequestHandler
        from urllib.request import urlopen
        from tests.engineering.dashboard_run_evidence_fixture import OwnedRequestServer

        class FaultHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.route(self.connection, self.path)
                raise RuntimeError("controlled diagnostic handler failure")

        listener = OwnedRequestServer(("127.0.0.1", 0), FaultHandler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                with self.assertRaises(http.client.RemoteDisconnected):
                    urlopen(f"http://127.0.0.1:{listener.server_address[1]}/api/configuration", timeout=5)
                receipt = listener.quiesce()
            finally:
                listener.shutdown()
                listener.server_close()
                thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(receipt["ordinary_quiescent"])
        self.assertEqual(receipt["unexpected_handler_errors"], 1)
        self.assertEqual(len(receipt["handler_errors"]), 1)
        self.assertEqual(receipt["handler_errors"][0]["path"], "/api/configuration")
        self.assertEqual(receipt["handler_errors"][0]["exception_type"], "RuntimeError")
        self.assertGreater(receipt["handler_errors"][0]["observed_monotonic_ns"], 0)


    def test_real_worker_rejects_hidden_handler_failure_without_native_effects(self):
        import socket
        from tests.engineering.dashboard_run_evidence_fixture import OwnedReadWorker
        with tempfile.TemporaryDirectory() as installation, tempfile.TemporaryDirectory() as data, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            worker = OwnedReadWorker(Path(data))
            try:
                port = int(worker.ready["url"].split(":")[2].split("/")[0])
                with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                    # The real handler's urlsplit raises on this malformed target.
                    # No successful response, account/model or store input is faked.
                    connection.sendall(b"GET http://[ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    self.assertEqual(connection.recv(4096), b"")
                with self.assertRaisesRegex(AssertionError, "cleanup failed"):
                    worker.close()
                receipt = worker.closed
                self.assertTrue(receipt["worker_ended"])
                self.assertTrue(receipt["owned_process_group_ended"])
                self.assertTrue(receipt["ordinary_handlers_completed"])
                self.assertTrue(receipt["isolation_retained_through_worker_exit"])
                self.assertEqual(receipt["unexpected_native_attempts"], 0)
                self.assertEqual(receipt["unexpected_handler_errors"], 1)
                self.assertEqual(receipt["handler_errors"][0]["exception_type"], "ValueError")
                self.assertEqual(receipt["transport_observations"]["passive_effects"], {"model": 0, "git": 0})
            finally:
                if worker.closed is None:
                    worker.close()
