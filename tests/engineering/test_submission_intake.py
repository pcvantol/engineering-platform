from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engineering_platform.submission_intake import SubmissionIntakeError, normalize_human_file


class SubmissionIntakeTest(unittest.TestCase):
    def parse(self, content: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "intent.md"
            path.write_text(content, encoding="utf-8")
            return normalize_human_file(path)

    def test_managed_intent_is_deterministically_normalized(self) -> None:
        result = self.parse("---\nproject: forge\nrepository: forge-repo\nmode: MANAGED\n---\nRepair the delivery path.\n")
        self.assertEqual(result["project_id"], "forge")
        self.assertIn("Execution Mode: MANAGED", result["submission"]["prompt"])  # type: ignore[index]

    def test_genesis_requires_explicit_target(self) -> None:
        result = self.parse("---\nproject: forge\nrepository: forge-repo\nmode: GENESIS\ntarget: /tmp/forge\n---\nCreate the target.\n")
        self.assertIn("Target repository: /tmp/forge", result["submission"]["prompt"])  # type: ignore[index]
        with self.assertRaises(SubmissionIntakeError):
            self.parse("---\nproject: forge\nrepository: forge-repo\nmode: GENESIS\n---\nCreate the target.\n")

    def test_authority_and_content_fail_closed(self) -> None:
        for content in ("hello", "---\nproject: forge\nmode: MANAGED\n---\nhello", "---\nproject: forge\nrepository: forge-repo\nmode: INVALID\n---\nhello", "---\nproject: forge\nrepository: forge-repo\nmode: MANAGED\n---\n"):
            with self.assertRaises(SubmissionIntakeError):
                self.parse(content)

    def test_normalization_provenance_is_canonical_and_transport_neutral(self) -> None:
        result = self.parse("---\nproject: forge\nrepository: forge-repo\nmode: MANAGED\n---\nPortable intent.\n")
        submission = result["submission"]  # type: ignore[assignment]
        self.assertEqual(submission["producer"]["version"], "submission-intake-v1")  # type: ignore[index]
        self.assertEqual(submission["constraints"]["normalization"], "submission-intake-v1")  # type: ignore[index]
