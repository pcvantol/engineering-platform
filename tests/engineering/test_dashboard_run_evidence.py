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
                observed = canary.external_transport.snapshot()
                self.assertGreaterEqual(observed["readiness_metadata_requests"], 2)
                self.assertEqual(observed["metadata_requests"], 1)
                self.assertEqual(observed["metadata_sessions"], 1)
                self.assertEqual(observed["native_app_server_starts"], 0)
                self.assertEqual(canary.console_model_requests, 0)
                self.assertEqual(canary.effect_snapshot(), baseline)
            finally:
                canary.close()

    def test_removed_metadata_adapter_fails_before_live_process_and_account_use(self):
        from tests.engineering.dashboard_run_evidence_fixture import StoredConsoleCanary, CodexCliProvider
        from urllib.request import urlopen
        native_app_server = CodexCliProvider.app_server
        escaped = []
        def deny_start(*args, **kwargs):
            escaped.append("native_app_server")
            raise OSError("Independent rejecting boundary: no account/process allowed")
        with tempfile.TemporaryDirectory(prefix="ep-dashboard-removed-guard-") as installation, patch.dict(
                os.environ, {"ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT": installation}):
            canary = StoredConsoleCanary()
            try:
                origin = canary.start_listener()
                with patch.object(CodexCliProvider, "app_server", native_app_server), patch(
                        "engineering_platform.providers._start_process", deny_start):
                    with urlopen(origin + "/api/provider-capacity", timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        response.read()
                self.assertEqual(escaped, ["native_app_server"])
                self.assertEqual(canary.console_model_requests, 0)
                # The mandatory new observation fails under this deliberate
                # guard removal; the lower rejecting boundary prevents escape.
                with self.assertRaises(AssertionError):
                    self.assertGreater(canary.external_transport.snapshot()["metadata_requests"], 0)
            finally:
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
