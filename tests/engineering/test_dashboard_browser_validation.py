from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.engineering import dashboard_browser_validation


class DashboardBrowserValidationTest(unittest.TestCase):
    def test_ci_delegates_a_single_requested_shard(self) -> None:
        completed = MagicMock(returncode=0)
        with patch("tools.engineering.dashboard_browser_validation.subprocess.run", return_value=completed) as run:
            self.assertEqual(dashboard_browser_validation._run_ci(Path("/repository"), ("--shard=2/4",)), 0)

        self.assertEqual(
            run.call_args.args[0],
            ("npx", "playwright", "test", "tests/engineering/dashboard.spec.mjs", "--shard=2/4"),
        )

    def test_local_batch_starts_four_one_worker_ci_shards(self) -> None:
        process = MagicMock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard_browser_validation._common_git_directory",
            return_value=Path(temporary),
        ), patch(
            "tools.engineering.dashboard_browser_validation.single_instance",
            return_value=nullcontext(),
        ), patch(
            "tools.engineering.dashboard_browser_validation.subprocess.Popen",
            side_effect=[process, process, process, process],
        ) as popen:
            self.assertEqual(dashboard_browser_validation._run_local_shards(Path(temporary)), 0)

        self.assertEqual(popen.call_count, 4)
        self.assertEqual([call.args[0][-2] for call in popen.call_args_list], ["--shard=1/4", "--shard=2/4", "--shard=3/4", "--shard=4/4"])
        self.assertTrue(all(call.kwargs["env"]["CI"] == "1" for call in popen.call_args_list))
        self.assertTrue(all(call.kwargs["start_new_session"] for call in popen.call_args_list))

    def test_local_batch_terminates_every_shard_group_when_its_deadline_expires(self) -> None:
        processes = []
        for pid in range(100, 104):
            process = MagicMock(pid=pid)
            process.poll.return_value = None
            process.wait.return_value = 0
            processes.append(process)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard_browser_validation._common_git_directory",
            return_value=Path(temporary),
        ), patch(
            "tools.engineering.dashboard_browser_validation.single_instance",
            return_value=nullcontext(),
        ), patch(
            "tools.engineering.dashboard_browser_validation.subprocess.Popen",
            side_effect=processes,
        ), patch(
            "tools.engineering.dashboard_browser_validation.time.monotonic",
            side_effect=[0, dashboard_browser_validation.LOCAL_BATCH_TIMEOUT_SECONDS + 1],
        ), patch("tools.engineering.dashboard_browser_validation.os.killpg") as killpg:
            self.assertEqual(dashboard_browser_validation._run_local_shards(Path(temporary)), 1)

        self.assertEqual(
            killpg.call_args_list,
            [
                ((100, signal.SIGTERM),),
                ((101, signal.SIGTERM),),
                ((102, signal.SIGTERM),),
                ((103, signal.SIGTERM),),
            ],
        )

    def test_local_batch_cleans_up_started_shards_when_a_later_spawn_fails(self) -> None:
        process = MagicMock(pid=100)
        process.poll.return_value = None
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard_browser_validation._common_git_directory",
            return_value=Path(temporary),
        ), patch(
            "tools.engineering.dashboard_browser_validation.single_instance",
            return_value=nullcontext(),
        ), patch(
            "tools.engineering.dashboard_browser_validation.subprocess.Popen",
            side_effect=[process, OSError("spawn failed")],
        ), patch("tools.engineering.dashboard_browser_validation.os.killpg") as killpg:
            with self.assertRaisesRegex(OSError, "spawn failed"):
                dashboard_browser_validation._run_local_shards(Path(temporary))

        killpg.assert_called_once_with(100, signal.SIGTERM)

    def test_local_batch_cleans_up_the_exited_failed_shard_group(self) -> None:
        failed = MagicMock(pid=100)
        failed.poll.return_value = 1
        failed.wait.return_value = 1
        running = []
        for pid in range(101, 104):
            process = MagicMock(pid=pid)
            process.poll.return_value = None
            process.wait.return_value = 0
            running.append(process)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard_browser_validation._common_git_directory",
            return_value=Path(temporary),
        ), patch(
            "tools.engineering.dashboard_browser_validation.single_instance",
            return_value=nullcontext(),
        ), patch(
            "tools.engineering.dashboard_browser_validation.subprocess.Popen",
            side_effect=[failed, *running],
        ), patch("tools.engineering.dashboard_browser_validation.os.killpg") as killpg:
            self.assertEqual(dashboard_browser_validation._run_local_shards(Path(temporary)), 1)

        self.assertEqual(
            killpg.call_args_list,
            [
                ((100, signal.SIGTERM),),
                ((101, signal.SIGTERM),),
                ((102, signal.SIGTERM),),
                ((103, signal.SIGTERM),),
            ],
        )

    def test_local_invocation_refuses_uncoordinated_playwright_arguments(self) -> None:
        with patch.dict("tools.engineering.dashboard_browser_validation.os.environ", {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "coordinated four-shard"):
                dashboard_browser_validation.main(("--workers=9",))
