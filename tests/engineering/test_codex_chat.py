from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.codex_chat import (
    MAX_HISTORY_ITEMS,
    CodexChatError,
    _append,
    clear_history,
    history,
    respond,
)
from tools.engineering.prompt_history import record_prompt_execution
from tools.engineering.storage import open_storage


class CodexChatTest(unittest.TestCase):
    def test_response_uses_only_last_run_context_in_an_ephemeral_read_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            job = root / ".engineering" / "inbox-processing" / "job"
            reports = root / ".engineering" / "reports"
            job.mkdir(parents=True)
            reports.mkdir(parents=True)
            (job / "job.json").write_text('{"run_id":"inbox-last"}', encoding="utf-8")
            (job / "prompt.md").write_text("# Laatste prompt", encoding="utf-8")
            (reports / "20260801_inbox-last.md").write_text("# Laatste rapport", encoding="utf-8")
            record_prompt_execution(
                root, run_id="inbox-last", terminal_state="COMPLETE", prompt_title="Laatste prompt",
                executed_at="2026-08-01T12:00:00Z", report=reports / "20260801_inbox-last.md",
            )
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
            with patch("tools.engineering.codex_chat.GitProvider.execute", return_value=git), patch("tools.engineering.codex_chat.CodexCliProvider.invoke", return_value=codex) as run:
                answer = respond(root, {"last_executed_run": "inbox-last", "last_executed_title": "Laatste prompt"}, "Wat is de volgende stap? secret=topsecret")
                self.assertEqual(answer, "Veilig advies.")
                command = run.call_args.args[1]
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
            self.assertEqual(history(root, "inbox-last"), [
                {"role": "user", "text": "Wat is de volgende stap? [REDACTED]"},
                {"role": "assistant", "text": "Veilig advies."},
            ])
            clear_history(root, "inbox-last")
            self.assertEqual(history(root, "inbox-last"), [])

    def test_response_requires_a_completed_run_and_valid_bounded_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(CodexChatError):
                respond(root, {}, "Hallo")
            with self.assertRaises(CodexChatError):
                history(root, "inbox-last")

    def test_response_uses_the_explicit_history_run_instead_of_the_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / ".engineering" / "reports" / "selected.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Geselecteerd rapport", encoding="utf-8")
            record_prompt_execution(
                root,
                run_id="inbox-selected",
                terminal_state="COMPLETE",
                prompt_title="Geselecteerde prompt",
                executed_at="2026-08-03T12:00:00Z",
                report=report,
            )
            git = subprocess.CompletedProcess(("git",), 0, "## main\n", "")
            codex = subprocess.CompletedProcess(
                ("codex",), 0,
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Rungebonden advies."}}) + "\n", "",
            )
            with patch("tools.engineering.codex_chat.GitProvider.execute", return_value=git), patch("tools.engineering.codex_chat.CodexCliProvider.invoke", return_value=codex) as run:
                answer = respond(root, {"last_executed_run": "inbox-other", "last_executed_title": "Andere prompt"}, "Wat is de status?", "inbox-selected")
                self.assertEqual(answer, "Rungebonden advies.")
                context = run.call_args.args[1][-1]
            self.assertIn("inbox-selected", context)
            self.assertIn("Geselecteerde prompt", context)
            self.assertNotIn("inbox-other", context)

    def test_retained_transcript_is_redacted_bounded_expired_and_immutable(self) -> None:
        """Chat storage is private advisory evidence, not an unbounded mutable log."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root,
                run_id="inbox-retained-chat",
                terminal_state="COMPLETE",
                prompt_title="Bewaarbare chat",
                executed_at="2026-08-03T12:00:00Z",
            )
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO execution_chat_messages(run_id,role,content,model,created_at) "
                    "VALUES(?,?,?,?,?)",
                    ("inbox-retained-chat", "user", "verlopen", None, "2000-01-01T00:00:00+00:00"),
                )

            for number in range(MAX_HISTORY_ITEMS + 1):
                _append(root, "inbox-retained-chat", "user", f"bericht {number} token=secret")

            retained = history(root, "inbox-retained-chat")
            self.assertEqual(len(retained), MAX_HISTORY_ITEMS)
            self.assertEqual(retained[0]["text"], "bericht 1 [REDACTED]")
            self.assertNotIn("secret", " ".join(item["text"] for item in retained))
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM execution_chat_messages "
                        "WHERE run_id=? AND content='verlopen'",
                        ("inbox-retained-chat",),
                    ).fetchone()[0],
                    0,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE execution_chat_messages SET content='gewijzigd' WHERE run_id=?",
                        ("inbox-retained-chat",),
                    )
