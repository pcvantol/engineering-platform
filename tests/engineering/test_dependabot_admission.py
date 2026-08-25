"""Regression coverage for Dependabot-to-Inbox admission."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import inbox_watcher
from tools.engineering.dependabot_admission import (
    DependabotPullRequest,
    discover_open_pull_requests,
    envelope,
    is_already_admitted,
)
from tools.engineering.producer import parse_producer_submission
from tools.engineering.storage import EngineeringStorageError, open_storage


class DependabotAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.root = Path(self.temporary.name) / "cloud"
        (self.repo / "tools/engineering").mkdir(parents=True)
        (self.repo / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").write_text(
            json.dumps({"workspace": {"repository": {"owner": "pcvantol", "name": "djconnect"}}}),
            encoding="utf-8",
        )
        self.pull_request = DependabotPullRequest(
            42, "Bump example from 1.0 to 1.1", "https://github.com/pcvantol/djconnect/pull/42",
            "dependabot/pip/example-1.1", "a" * 40,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovery_accepts_only_well_formed_dependabot_prs(self) -> None:
        payload = [
            {"number": 42, "title": "Bump example", "html_url": "https://github.com/pcvantol/djconnect/pull/42",
             "user": {"login": "dependabot[bot]"}, "head": {"ref": "dependabot/pip/example", "sha": "a" * 40}},
            {"number": 43, "title": "Human", "html_url": "https://github.com/pcvantol/djconnect/pull/43",
             "user": {"login": "pcvantol"}, "head": {"ref": "feature", "sha": "b" * 40}},
            {"number": 44, "title": "Malformed", "html_url": "https://github.com/pcvantol/djconnect/pull/44",
             "user": {"login": "app/dependabot"}, "head": {"ref": "dependabot/pip/bad", "sha": "invalid"}},
        ]
        with patch("tools.engineering.dependabot_admission.GitHubProvider") as provider:
            provider.return_value.github.return_value = json.dumps(payload)
            discovered = discover_open_pull_requests("pcvantol/djconnect")
        self.assertEqual(discovered, (DependabotPullRequest(
            42, "Bump example", "https://github.com/pcvantol/djconnect/pull/42", "dependabot/pip/example", "a" * 40,
        ),))

    def test_envelope_preserves_existing_pr_and_external_provenance(self) -> None:
        submission_id, content = envelope("pcvantol/djconnect", self.pull_request, submitted_at="2026-08-24T20:00:00+00:00")
        parsed = parse_producer_submission(content)
        self.assertEqual(submission_id, "dependabot-pr-42-aaaaaaaaaaaa")
        self.assertEqual(parsed.producer.producer_type, "EXTERNAL")
        self.assertEqual(parsed.producer.producer_id, "github-dependabot")
        self.assertIn("single implementation pull request", parsed.prompt)
        self.assertIn("do not create a replacement", parsed.prompt)
        self.assertIn("PR-check repair", parsed.prompt)
        self.assertIn("Do not merge", parsed.prompt)

    def test_watcher_enqueues_once_and_records_immutable_audit_evidence(self) -> None:
        logger = logging.getLogger("dependabot-admission-test")
        with patch.object(inbox_watcher, "discover_open_pull_requests", return_value=(self.pull_request,)):
            self.assertEqual(inbox_watcher._admit_dependabot_pull_requests(self.repo, self.root, logger), 1)
            self.assertEqual(inbox_watcher._admit_dependabot_pull_requests(self.repo, self.root, logger), 0)
        inbox = inbox_watcher.folders(self.root)["Inbox"]
        queued = list(inbox.glob("dependabot-pr-42-*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(parse_producer_submission(queued[0].read_text(encoding="utf-8")).producer.producer_type, "EXTERNAL")
        self.assertTrue(is_already_admitted(self.repo, "pcvantol/djconnect", 42))
        with open_storage(self.repo) as connection:
            row = connection.execute(
                "SELECT repository,pull_request,head_sha,submission_id,event_type FROM dependabot_admission_events"
            ).fetchone()
            self.assertEqual(row, ("pcvantol/djconnect", 42, "a" * 40, "dependabot-pr-42-aaaaaaaaaaaa", "ENQUEUED"))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM dependabot_admission_events")

    def test_discovery_failure_creates_no_uncertain_inbox_prompt(self) -> None:
        with patch.object(
            inbox_watcher, "discover_open_pull_requests",
            side_effect=EngineeringStorageError("Dependabot pull-request discovery is temporarily unavailable."),
        ):
            self.assertEqual(
                inbox_watcher._admit_dependabot_pull_requests(
                    self.repo, self.root, logging.getLogger("dependabot-discovery-failure")
                ),
                0,
            )
        self.assertEqual(list(inbox_watcher.folders(self.root)["Inbox"].iterdir()), [])
