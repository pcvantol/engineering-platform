from __future__ import annotations

import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.producer import ENVELOPE_CONTRACT_NAME, ENVELOPE_CONTRACT_VERSION
from tools.engineering.workspace_inbox_api import (
    WorkspaceInboxSubmissionError,
    build_human_envelope,
    canonical_human_producer_id,
    main as workspace_inbox_main,
    publish,
    preview,
    submit_human,
)
from tools.engineering.producer import parse_producer_submission
from tools.engineering.storage import open_storage
from tools.engineering.validation_profile import producer_profile_payload


class WorkspaceInboxApiTest(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = Path(directory)
        target = root / "tools" / "engineering"
        target.mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json"
        (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return root

    def _envelope(self) -> str:
        return json.dumps({
            "contract": {"name": ENVELOPE_CONTRACT_NAME, "version": ENVELOPE_CONTRACT_VERSION},
            "submission": {"id": "forge-workspace-001", "submitted_at": "2026-08-25T00:00:00+00:00"},
            "producer": {"id": "forge-workspace", "type": "FORGE", "version": "1.0"},
            "prompt": {"text": "Execution Mode: Managed\n\nValidate this bounded request.", "metadata": {"source": "forge"}},
        })

    def _human_validation_only_envelope(self) -> str:
        return json.dumps({
            "contract": {"name": ENVELOPE_CONTRACT_NAME, "version": ENVELOPE_CONTRACT_VERSION},
            "submission": {"id": "operator-validation-only-001"},
            "producer": {"id": "operator", "type": "HUMAN"},
            "prompt": {"text": "Validate the selected controls."},
            "execution_context": {
                "context_version": "1.0", "action_intent": "VALIDATION_ONLY",
                "validation_profile": producer_profile_payload("DASHBOARD"),
            },
        })

    def test_valid_forge_envelope_is_atomically_published_to_the_physical_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                receipt = publish(root, self._envelope())
            self.assertEqual(receipt.inbox, inbox)
            self.assertTrue((inbox / receipt.filename).is_file())
            self.assertFalse(any(inbox.glob(".*.partial")))
            connection = open_storage(root)
            try:
                row = connection.execute(
                    "SELECT producer_type,prompt_content FROM execution_submissions WHERE submission_id=?",
                    (receipt.submission_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("FORGE", "Execution Mode: Managed\n\nValidate this bounded request."))

    def test_rejects_non_forge_and_invalid_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "complete trusted producer envelope") as error:
                publish(root, "plain text is not a Forge envelope")
            self.assertEqual(error.exception.code, "producer_envelope_required")

    def test_human_validation_only_context_is_persisted_before_inbox_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            (transport / "Inbox").mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                receipt = publish(root, self._human_validation_only_envelope())
            self.assertEqual(receipt.filename, "producer-operator-validation-only-001.json")
            connection = open_storage(root)
            try:
                row = connection.execute(
                    "SELECT producer_type,execution_context_snapshot FROM execution_submissions WHERE submission_id=?",
                    (receipt.submission_id,),
                ).fetchone()
            finally:
                connection.close()
        self.assertEqual(row[0], "HUMAN")
        self.assertEqual(json.loads(row[1])["action_intent"], "VALIDATION_ONLY")
        self.assertEqual(json.loads(row[1])["validation_profile"], producer_profile_payload("DASHBOARD"))

    def test_operator_route_builds_explicit_nonlegacy_human_validation_only(self) -> None:
        envelope = build_human_envelope(
            prompt="Run the bounded validation controls.",
            title="Bounded validation controls",
            producer_identity="operator-peter",
            action_intent="VALIDATION_ONLY",
            validation_profile="DASHBOARD",
            submission_id="human-validation-001",
        )
        parsed = parse_producer_submission(json.dumps(envelope))

        self.assertEqual(parsed.producer.producer_type, "HUMAN")
        self.assertEqual(parsed.producer.producer_id, "human:operator-peter")
        self.assertNotEqual(parsed.producer.producer_id, "legacy")
        self.assertEqual(parsed.contract_version, ENVELOPE_CONTRACT_VERSION)
        self.assertEqual(parsed.execution_context, {
            "context_version": "1.0", "action_intent": "VALIDATION_ONLY",
            "validation_profile": producer_profile_payload("DASHBOARD"),
        })
        self.assertTrue(parsed.prompt.startswith("Execution Mode: Managed"))
        self.assertEqual(parsed.envelope["prompt"]["metadata"]["title"], "Bounded validation controls")

    def test_operator_route_keeps_mutating_delivery_explicit_and_does_not_parse_prose(self) -> None:
        envelope = build_human_envelope(
            prompt="This qualification proof is validation only in prose.",
            title="Qualification proof",
            producer_identity="human:operator-peter",
            action_intent="MUTATING_DELIVERY",
        )
        self.assertEqual(envelope["execution_context"], {"context_version": "1.0", "action_intent": "MUTATING_DELIVERY"})
        self.assertEqual(parse_producer_submission(json.dumps(envelope)).execution_context["action_intent"], "MUTATING_DELIVERY")

    def test_operator_route_rejects_invalid_identity_intent_and_execution_mode(self) -> None:
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "identity"):
            canonical_human_producer_id("legacy")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "intent"):
            build_human_envelope(prompt="work", title="Work", producer_identity="operator", action_intent="INFERRED")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "title"):
            build_human_envelope(prompt="work", title="", producer_identity="operator", action_intent="MUTATING_DELIVERY")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "require a validation profile") as error:
            build_human_envelope(prompt="work", title="Work", producer_identity="operator", action_intent="VALIDATION_ONLY")
        self.assertEqual(error.exception.code, "validation_profile_required")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "validation profile") as error:
            build_human_envelope(
                prompt="work", title="Work", producer_identity="operator", action_intent="VALIDATION_ONLY", validation_profile="UNKNOWN"
            )
        self.assertEqual(error.exception.code, "invalid_validation_profile")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "Managed"):
            build_human_envelope(
                prompt="Execution Mode: Genesis\n\nwork", title="Work", producer_identity="operator", action_intent="VALIDATION_ONLY"
            )

    def test_operator_route_persists_context_before_inbox_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                receipt = submit_human(
                    root, prompt="Run controls.", title="Run controls", producer_identity="operator-peter",
                    action_intent="VALIDATION_ONLY", validation_profile="DASHBOARD", submission_id="human-persisted-001",
                )
            with open_storage(root) as connection:
                row = connection.execute(
                    "SELECT producer_id,producer_type,contract_version,execution_context_snapshot,prompt_metadata FROM execution_submissions WHERE submission_id=?",
                    (receipt.submission_id,),
                ).fetchone()
            self.assertEqual(row[:3], ("human:operator-peter", "HUMAN", "1.0"))
            self.assertEqual(json.loads(row[3])["action_intent"], "VALIDATION_ONLY")
            self.assertEqual(json.loads(row[3])["validation_profile"], producer_profile_payload("DASHBOARD"))
            self.assertEqual(json.loads(row[4])["title"], "Run controls")
            self.assertEqual(
                parse_producer_submission((inbox / receipt.filename).read_text(encoding="utf-8")).execution_context["action_intent"],
                "VALIDATION_ONLY",
            )
            self.assertEqual(
                parse_producer_submission((inbox / receipt.filename).read_text(encoding="utf-8")).execution_context["validation_profile"],
                producer_profile_payload("DASHBOARD"),
            )

    def test_cli_accepts_a_canonical_validation_profile_and_publishes_the_same_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            prompt = root / "objective.md"
            prompt.write_text("Run selected controls.", encoding="utf-8")
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                code = workspace_inbox_main([
                    "--root", str(root), "--prompt-file", str(prompt), "--producer-id", "operator-peter",
                    "--title", "Selected controls",
                    "--action-intent", "VALIDATION_ONLY", "--validation-profile", "DASHBOARD",
                    "--submission-id", "human-cli-001",
                ])
            self.assertEqual(code, 0)
            with open_storage(root) as connection:
                stored = connection.execute(
                    "SELECT execution_context_snapshot FROM execution_submissions WHERE submission_id='human-cli-001'"
                ).fetchone()
            published = parse_producer_submission((inbox / "producer-human-cli-001.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(stored[0]), published.execution_context)
        self.assertEqual(published.execution_context["validation_profile"], producer_profile_payload("DASHBOARD"))

    def test_dry_run_validates_and_previews_without_storage_or_inbox_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            prompt = root / "objective.md"
            prompt.write_text("Run selected controls.", encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False), patch("sys.stdout", output):
                code = workspace_inbox_main([
                    "--root", str(root), "--prompt-file", str(prompt), "--title", "Selected controls",
                    "--producer-id", "operator-peter", "--action-intent", "VALIDATION_ONLY",
                    "--validation-profile", "DASHBOARD", "--submission-id", "human-dry-run-001", "--dry-run",
                ])
            result = json.loads(output.getvalue())
            with open_storage(root) as connection:
                count = connection.execute("SELECT COUNT(*) FROM execution_submissions").fetchone()[0]
            self.assertEqual(code, 0)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["submission_id"], "human-dry-run-001")
            self.assertEqual(result["inbox"], str(inbox))
            self.assertEqual(result["prompt_metadata"], {"title": "Selected controls"})
            self.assertEqual(result["execution_context"]["validation_profile"], producer_profile_payload("DASHBOARD"))
            self.assertEqual(count, 0)
            self.assertEqual(list(inbox.iterdir()), [])

    def test_preview_rejects_an_unavailable_inbox_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            envelope = build_human_envelope(
                prompt="Run controls.", title="Run controls", producer_identity="operator-peter",
                action_intent="MUTATING_DELIVERY", submission_id="human-preview-001",
            )
            missing_transport = root / "missing-transport"
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(missing_transport)}, clear=False):
                with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "Inbox"):
                    preview(root, json.dumps(envelope))

    def test_invalid_profile_is_rejected_before_storage_or_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                with self.assertRaises(WorkspaceInboxSubmissionError):
                    submit_human(
                        root, prompt="validation_profile: DASHBOARD", title="Validation profile", producer_identity="operator-peter",
                        action_intent="VALIDATION_ONLY", validation_profile="not a tier", submission_id="human-invalid-001",
                    )
            with open_storage(root) as connection:
                count = connection.execute("SELECT COUNT(*) FROM execution_submissions").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(list(inbox.iterdir()), [])
