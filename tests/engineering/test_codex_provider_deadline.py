from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from engineering_platform import dashboard_translation, providers


class CodexProviderDeadlineTests(unittest.TestCase):
    def test_explicit_deadline_kills_and_reaps_the_actual_child_in_every_variant(self) -> None:
        for environment, input_text in ((None, None), ({"DEADLINE_TEST": "yes"}, None), (None, "input"), ({"DEADLINE_TEST": "yes"}, "input")):
            with self.subTest(environment=environment, input_text=input_text), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pid_file = root / "child.pid"
                script = "import os,pathlib,time; pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(4)"
                with patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
                    provider = providers.CodexCliProvider()
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    provider.invoke(root, ("codex", "-c", script), timeout=1, environment=environment, input_text=input_text)
                self.assertLess(time.monotonic() - started, 3)
                pid = int(pid_file.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)

    def test_without_deadline_keeps_execute_and_captures_the_complete_process_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            root = Path(temporary)
            provider = providers.CodexCliProvider()
            with patch.object(provider, "execute", wraps=provider.execute) as execute:
                result = provider.invoke(root, ("codex", "-c", "import sys,time; time.sleep(.05); print('finished'); print('diagnostic',file=sys.stderr); sys.exit(3)"))
            execute.assert_called_once()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "finished\n", "diagnostic\n"))

    def test_deadline_variant_retains_managed_executable_environment_input_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            root = Path(temporary)
            result = providers.CodexCliProvider().invoke(
                root,
                ("codex", "-c", "import os,sys,json; print(json.dumps([os.getcwd(),os.environ.get('DEADLINE_TEST'),sys.stdin.read()])); print('stderr',file=sys.stderr)"),
                timeout=2, environment={"DEADLINE_TEST": "explicit"}, input_text="complete\névidence",
            )
        self.assertEqual(json.loads(result.stdout), [str(root.resolve()), "explicit", "complete\névidence"])
        self.assertEqual(result.stderr, "stderr\n")

    def test_translation_deadline_uses_the_real_provider_and_a_retry_can_succeed(self) -> None:
        dashboard_translation._cache.clear()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "codex"
            pid_file = root / "child.pid"
            launcher.write_text(
                f"#!{sys.executable}\nimport json,os,pathlib,time\n"
                f"pid_file=pathlib.Path({str(pid_file)!r})\n"
                "if not pid_file.exists():\n"
                "    pid_file.write_text(str(os.getpid()))\n"
                "    time.sleep(4)\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'translations':['Bron blijft intact.']})}}))\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            with patch("engineering_platform.providers.codex_cli_executable", return_value=str(launcher)), patch.object(dashboard_translation, "CHAT_TIMEOUT_SECONDS", 1):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "^DASHBOARD_TRANSLATION_TIMEOUT$"):
                    dashboard_translation.translate("nl", ["Source remains intact."])
                self.assertEqual(dashboard_translation._cache, {})
                pid = int(pid_file.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
                self.assertEqual(dashboard_translation.translate("nl", ["Source remains intact."]), ["Bron blijft intact."])

    def test_output_limit_counts_both_pipes_and_kills_and_reaps_the_child(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as temporary, patch(
                "engineering_platform.providers.codex_cli_executable", return_value=sys.executable,
            ):
                root = Path(temporary)
                script = (
                    "import os,pathlib,sys,time; pathlib.Path('child.pid').write_text(str(os.getpid())); "
                    f"sys.{stream}.write('x'*4097); sys.{stream}.flush(); time.sleep(5)"
                )
                started = time.monotonic()
                with self.assertRaises(providers.ProviderOutputLimitExceeded):
                    providers.CodexCliProvider().invoke(root, ("codex", "-c", script), timeout=2, max_output_bytes=4096)
                self.assertLess(time.monotonic() - started, 1.5)
                pid = int((root / "child.pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)

    def test_bounded_capture_retains_exact_limit_environment_input_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            result = providers.CodexCliProvider().invoke(
                Path(temporary),
                ("codex", "-c", "import os,sys; sys.stdout.write(os.environ['DEADLINE_TEST']+sys.stdin.read()); sys.stderr.write('xy'); sys.exit(7)"),
                environment={"DEADLINE_TEST": "ab"}, input_text="é", timeout=2, max_output_bytes=6,
            )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "abé", "xy"))

    def test_output_limit_is_combined_across_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            with self.assertRaises(providers.ProviderOutputLimitExceeded):
                providers.CodexCliProvider().invoke(
                    Path(temporary), ("codex", "-c", "import sys; sys.stdout.write('x'*2048); sys.stderr.write('y'*2049)"),
                    timeout=2, max_output_bytes=4096,
                )

    def test_bounded_capture_closes_empty_input_and_preserves_universal_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            result = providers.CodexCliProvider().invoke(
                Path(temporary), ("codex", "-c", "import sys; assert not sys.stdin.read(); sys.stdout.buffer.write(b'one\\r\\ntwo\\r')"),
                input_text="", max_output_bytes=100,
            )
        self.assertEqual(result.stdout, "one\ntwo\n")

    def test_bounded_deadline_also_applies_when_child_does_not_read_input_or_closes_output(self) -> None:
        scripts = (
            "import time; time.sleep(2)",
            "import os,time; os.close(1); os.close(2); time.sleep(2)",
        )
        for script in scripts:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as temporary, patch(
                "engineering_platform.providers.codex_cli_executable", return_value=sys.executable,
            ):
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    providers.CodexCliProvider().invoke(
                        Path(temporary), ("codex", "-c", script), input_text="x" * 1_000_000,
                        timeout=0.2, max_output_bytes=4096,
                    )
                self.assertLess(time.monotonic() - started, 1.5)

    def test_bounded_capture_accepts_early_stdin_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            result = providers.CodexCliProvider().invoke(
                Path(temporary), ("codex", "-c", "print('done')"),
                input_text="x" * 1_000_000, timeout=2, max_output_bytes=4096,
            )
        self.assertEqual(result.stdout, "done\n")

    def test_successful_bounded_invocation_does_not_leave_its_descendant_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.providers.codex_cli_executable", return_value=sys.executable):
            root = Path(temporary)
            child_script = "import os,pathlib,time; pathlib.Path('descendant.pid').write_text(str(os.getpid())); time.sleep(4)"
            launcher_script = (
                "import pathlib,subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable,'-c',{child_script!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "while not pathlib.Path('descendant.pid').exists(): time.sleep(.01)\n"
                "print('done')\n"
            )
            result = providers.CodexCliProvider().invoke(
                root, ("codex", "-c", launcher_script), timeout=2, max_output_bytes=4096,
            )
            self.assertEqual(result.stdout, "done\n")
            child_pid = int((root / "descendant.pid").read_text())
            try:
                status = subprocess.run(("ps", "-o", "stat=", "-p", str(child_pid)), text=True, capture_output=True, check=False).stdout.strip()
                self.assertTrue(not status or status.startswith("Z"), f"Owned descendant remains alive: {status}")
            finally:
                # A failing regression must also clean up its harmless fixture.
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_translation_rejects_excess_provider_output_before_parsing(self) -> None:
        dashboard_translation._cache.clear()
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "codex"
            launcher.write_text(f"#!{sys.executable}\nprint('x'*4097)\n", encoding="utf-8")
            launcher.chmod(0o700)
            with patch("engineering_platform.providers.codex_cli_executable", return_value=str(launcher)), patch.object(
                dashboard_translation, "MAX_PROVIDER_OUTPUT_BYTES", 4096,
            ):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "^DASHBOARD_TRANSLATION_OUTPUT_INVALID$"):
                    dashboard_translation.translate("nl", ["Source remains intact."])
                self.assertEqual(dashboard_translation._cache, {})

    def test_translation_rejects_invalid_provider_encoding_as_a_safe_failure(self) -> None:
        dashboard_translation._cache.clear()
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "codex"
            launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(bytes([255]))\n", encoding="utf-8")
            launcher.chmod(0o700)
            with patch("engineering_platform.providers.codex_cli_executable", return_value=str(launcher)):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "^DASHBOARD_TRANSLATION_OUTPUT_INVALID$"):
                    dashboard_translation.translate("nl", ["Source remains intact."])
                self.assertEqual(dashboard_translation._cache, {})


if __name__ == "__main__":
    unittest.main()
