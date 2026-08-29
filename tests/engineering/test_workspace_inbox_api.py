from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.producer import ENVELOPE_CONTRACT_NAME, ENVELOPE_CONTRACT_VERSION
from tools.engineering.workspace_inbox_api import WorkspaceInboxSubmissionError, publish
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
