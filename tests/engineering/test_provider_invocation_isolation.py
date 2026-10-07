"""Actual CLI adapter ownership; only the external provider transport is doubled."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest

from engineering_platform.capability_review import ReviewerResult, ReviewerSelection, run_reviews
from engineering_platform.execution_executor import CodexCliClient


class InvocationIsolationTests(unittest.TestCase):
    def test_shared_client_serializes_turn_and_callback_reconfiguration(self):
        entered, release, setter_started, setter_finished = (Event() for _ in range(4))
        observations = []

        class Transport:
            def invoke(self, root, command, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release provider transport")
                observations.append(client._activity_callback)
                payload = {"contract_version": "1.0", "contribution": "Independent review",
                           "recommendations": [], "findings": []}
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "type": "item.completed", "item": {
                        "type": "agent_message", "text": json.dumps(payload)}}), "")

        client = CodexCliClient(Transport())
        first, second = lambda _: None, lambda _: None
        client.set_activity_callback(first)
        def configure():
            setter_started.set()
            client.set_activity_callback(second)
            setter_finished.set()
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(2) as pool:
            review = pool.submit(client.review, Path(directory), ReviewerSelection("api", "API", 1.0), "objective")
            self.assertTrue(entered.wait(5))
            setter = pool.submit(configure)
            self.assertTrue(setter_started.wait(5))
            try:
                self.assertFalse(setter_finished.wait(.05))
            finally:
                release.set()
            self.assertFalse(review.result(timeout=5).failed)
            setter.result(timeout=5)
        self.assertEqual(observations, [first])
        self.assertIs(client._activity_callback, second)

    def test_failed_next_review_has_no_prior_usage_snapshots_or_cancellation(self):
        class Transport:
            def invoke(self, *args, **kwargs):
                raise RuntimeError("external transport failure")
        client = CodexCliClient(Transport())
        client.last_usage = {"input_tokens": 77}
        client.last_usage_snapshots = ({"input_tokens": 77},)
        client.last_churn = {"tool_loop_operations": 12}
        client._cancellation_observed = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "external transport"):
                client.review(Path(directory), ReviewerSelection("api", "API", 1.0), "objective")
        self.assertEqual(client.last_usage, {})
        self.assertEqual(client.last_usage_snapshots, ())
        self.assertEqual(client.last_churn, {})
        self.assertFalse(client.cancellation_observed())
        self.assertIsNone(client.last_execution_seconds)

    def test_review_coordinator_detaches_returned_observation_maps(self):
        original = ReviewerResult("api", "review", usage={"input_tokens": 11},
                                  churn={"tool_loop_operations": 1},
                                  usage_snapshots=({"input_tokens": 11},))
        class Client:
            def review(self, *args): return original
        result = run_reviews(Path("."), (ReviewerSelection("api", "API", 1.0),), "objective", Client())[0]
        original.usage["input_tokens"] = 999
        original.churn["tool_loop_operations"] = 999
        original.usage_snapshots[0]["input_tokens"] = 999
        self.assertEqual(result.usage["input_tokens"], 11)
        self.assertEqual(result.churn["tool_loop_operations"], 1)
        self.assertEqual(result.usage_snapshots[0]["input_tokens"], 11)
