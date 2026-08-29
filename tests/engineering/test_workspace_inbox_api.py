from __future__ import annotations

import json
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
    publish,
    submit_human,
)
from tools.engineering.producer import parse_producer_submission
from tools.engineering.storage import open_storage


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
            "execution_context": {"context_version": "1.0", "action_intent": "VALIDATION_ONLY"},
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
            self.assertTrue(receipt.filename.startswith("producer-operator-validation-only-001-"))
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

    def test_operator_route_builds_explicit_nonlegacy_human_validation_only(self) -> None:
        envelope = build_human_envelope(
            prompt="Run the bounded validation controls.",
            producer_identity="operator-peter",
            action_intent="VALIDATION_ONLY",
            submission_id="human-validation-001",
        )
        parsed = parse_producer_submission(json.dumps(envelope))

        self.assertEqual(parsed.producer.producer_type, "HUMAN")
        self.assertEqual(parsed.producer.producer_id, "human:operator-peter")
        self.assertNotEqual(parsed.producer.producer_id, "legacy")
        self.assertEqual(parsed.contract_version, ENVELOPE_CONTRACT_VERSION)
        self.assertEqual(parsed.execution_context, {"context_version": "1.0", "action_intent": "VALIDATION_ONLY"})
        self.assertTrue(parsed.prompt.startswith("Execution Mode: Managed"))

    def test_operator_route_keeps_mutating_delivery_explicit_and_does_not_parse_prose(self) -> None:
        envelope = build_human_envelope(
            prompt="This qualification proof is validation only in prose.",
            producer_identity="human:operator-peter",
            action_intent="MUTATING_DELIVERY",
        )
        self.assertEqual(envelope["execution_context"], {"context_version": "1.0", "action_intent": "MUTATING_DELIVERY"})
        self.assertEqual(parse_producer_submission(json.dumps(envelope)).execution_context["action_intent"], "MUTATING_DELIVERY")

    def test_operator_route_rejects_invalid_identity_intent_and_execution_mode(self) -> None:
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "identity"):
            canonical_human_producer_id("legacy")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "intent"):
            build_human_envelope(prompt="work", producer_identity="operator", action_intent="INFERRED")
        with self.assertRaisesRegex(WorkspaceInboxSubmissionError, "Managed"):
            build_human_envelope(
                prompt="Execution Mode: Genesis\n\nwork", producer_identity="operator", action_intent="VALIDATION_ONLY"
            )

    def test_operator_route_persists_context_before_inbox_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                receipt = submit_human(
                    root, prompt="Run controls.", producer_identity="operator-peter",
                    action_intent="VALIDATION_ONLY", submission_id="human-persisted-001",
                )
            with open_storage(root) as connection:
                row = connection.execute(
                    "SELECT producer_id,producer_type,contract_version,execution_context_snapshot FROM execution_submissions WHERE submission_id=?",
                    (receipt.submission_id,),
                ).fetchone()
            self.assertEqual(row[:3], ("human:operator-peter", "HUMAN", "1.0"))
            self.assertEqual(json.loads(row[3])["action_intent"], "VALIDATION_ONLY")
            self.assertEqual(
                parse_producer_submission((inbox / receipt.filename).read_text(encoding="utf-8")).execution_context["action_intent"],
                "VALIDATION_ONLY",
            )
