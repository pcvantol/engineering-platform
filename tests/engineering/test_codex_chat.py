from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.codex_chat import CodexChatError, respond


class CodexChatTest(unittest.TestCase):
    def test_response_uses_only_last_run_context_in_an_ephemeral_read_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            job = root / ".djconnect" / "inbox-processing" / "job"
            reports = root / ".djconnect" / "reports"
            job.mkdir(parents=True)
            reports.mkdir(parents=True)
            (job / "job.json").write_text('{"run_id":"inbox-last"}', encoding="utf-8")
            (job / "prompt.md").write_text("# Laatste prompt", encoding="utf-8")
            (reports / "20260801_inbox-last.md").write_text("# Laatste rapport", encoding="utf-8")
            git = subprocess.CompletedProcess(("git",), 0, "## main\n", "")
            codex = subprocess.CompletedProcess(
                ("codex",),
                0,
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Veilig advies."},
                    }
                )
                + "\n",
                "",
            )
            with patch("tools.engineering.codex_chat.subprocess.run", side_effect=(git, codex)) as run:
                answer = respond(
                    root,
                    {"last_executed_run": "inbox-last", "last_executed_title": "Laatste prompt"},
                    "Wat is de volgende stap?",
                    [],
                )
            self.assertEqual(answer, "Veilig advies.")
            command = run.call_args_list[1].args[0]
            self.assertIn("--sandbox", command)
            self.assertIn("read-only", command)
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertNotIn("--add-dir", command)
            self.assertNotIn(str(root), command)
            self.assertIn("Laatste rapport", command[-1])
            self.assertIn("Laatste prompt", command[-1])

    def test_response_requires_a_completed_run_and_valid_bounded_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(CodexChatError):
                respond(root, {}, "Hallo", [])
            with self.assertRaises(CodexChatError):
                respond(root, {"last_executed_run": "inbox-last"}, "Hallo", [{}])
