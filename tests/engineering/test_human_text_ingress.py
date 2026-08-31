from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform.human_text_ingress import HumanTextIngressError, ingest, parse_text_submission
from engineering_platform.producer import parse_producer_submission
from engineering_platform.storage import open_storage


class HumanTextIngressTest(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = Path(directory)
        target = root / "tools" / "engineering"
        target.mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json"
        (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return root

    def test_strict_header_and_no_prose_inference(self) -> None:
        plain = parse_text_submission("validation only; dashboard proof\n")
        self.assertEqual((plain.action_intent, plain.validation_profile), ("MUTATING_DELIVERY", None))
        explicit = parse_text_submission("---\naction_intent: VALIDATION_ONLY\nvalidation_profile: DASHBOARD\n---\nwork\n")
        self.assertEqual((explicit.action_intent, explicit.validation_profile), ("VALIDATION_ONLY", "DASHBOARD"))
        for content in ("---\nproducer_id: forged\n---\nwork", "---\naction_intent: UNKNOWN\n---\nwork", "---\naction_intent: VALIDATION_ONLY\n---\nwork", "---\naction_intent: MUTATING_DELIVERY\n"):
            with self.assertRaises(HumanTextIngressError):
                parse_text_submission(content)

    def test_post_activation_text_is_canonical_human_json_and_archived_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            transport = root / "transport"
            inbox = transport / "Inbox"
            inbox.mkdir(parents=True)
            with patch.dict(os.environ, {"DJCONNECT_ENGINEERING_INBOX": str(transport)}, clear=False):
                self.assertEqual(ingest(root, inbox, read_source=lambda path: path.read_text(encoding="utf-8")), 0)
                source = inbox / "prompt.txt"
                source.write_text("only validate this in prose", encoding="utf-8")
                self.assertEqual(ingest(root, inbox, read_source=lambda path: path.read_text(encoding="utf-8")), 1)
                self.assertEqual(ingest(root, inbox, read_source=lambda path: path.read_text(encoding="utf-8")), 0)
            envelope = next(inbox.glob("producer-human-ingress-*.json"))
            parsed = parse_producer_submission(envelope.read_text(encoding="utf-8"))
            self.assertEqual(parsed.producer.producer_id, "human:operator-peter")
            self.assertEqual(parsed.execution_context, {"context_version": "1.0", "action_intent": "MUTATING_DELIVERY"})
            self.assertTrue((transport / "Accepted" / "prompt.txt").is_file())
            with open_storage(root) as connection:
                rows = connection.execute("SELECT submission_id FROM execution_submissions").fetchall()
            self.assertEqual(len(rows), 1)

    def test_invalid_text_is_rejected_and_pre_activation_text_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            inbox = root / "transport" / "Inbox"
            inbox.mkdir(parents=True)
            legacy = inbox / "legacy.txt"
            legacy.write_text("old prompt", encoding="utf-8")
            self.assertEqual(ingest(root, inbox, read_source=lambda path: path.read_text(encoding="utf-8")), 0)
            self.assertTrue(legacy.exists())
            bad = inbox / "bad.txt"
            bad.write_text("---\nunknown: x\n---\nwork", encoding="utf-8")
            self.assertEqual(ingest(root, inbox, read_source=lambda path: path.read_text(encoding="utf-8")), 1)
            self.assertTrue((inbox.parent / "Rejected" / "bad.txt").is_file())
