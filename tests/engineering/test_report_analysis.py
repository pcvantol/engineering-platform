from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.report_analysis import analyze


class ReportAnalysisTest(unittest.TestCase):
    @patch("tools.engineering.providers.subprocess.run")
    def test_analysis_is_read_only_redacted_and_persisted_per_run(self, run: object) -> None:
        payload = {
            "summary": "Een veilige samenvatting.",
            "findings": ["Bevinding."],
            "issues": [],
            "risks": ["Risico."],
            "next_steps": ["Volgende stap."],
            "product_architect_advice": "Houd de scope begrensd.",
        }
        run.return_value = subprocess.CompletedProcess(
            ("codex",),
            0,
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}),
            "",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.md"
            report.write_text("# Engineering Report\n", encoding="utf-8")
            result = analyze(root, "inbox-last", report)
            content = result.read_text(encoding="utf-8")
        command = run.call_args.args[0]
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("## Bevindingen", content)
        self.assertIn("## Advies aan Product Architect", content)

    @patch("tools.engineering.providers.subprocess.run")
    def test_analysis_failure_is_advisory_and_does_not_raise(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(("codex",), 1, "", "unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.md"
            report.write_text("# Engineering Report\n", encoding="utf-8")
            result = analyze(root, "inbox-last", report)
            content = result.read_text(encoding="utf-8")
        self.assertIn("Engineering-uitkomst blijft ongewijzigd", content)
