"""Controlled interleaving of the shared-state sequence in CodexCliClient.review.

This reproduces the relevant assignments and final result-copy pattern from
execution_executor.py at 62eb6c4631cc23b9e4d2a53043216be6f20bfaae. It is a
reduced harness, NOT a run of the original class, Codex, or installed EP.
A scheduler barrier stands in for a permitted thread context switch after
telemetry assignment but before the final return copies the shared fields.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
import json

@dataclass(frozen=True)
class ReviewerResult:
    reviewer: str
    contribution: str
    usage: dict
    runtime_metadata: dict
    churn: dict
    duration_seconds: float
    usage_snapshots: tuple

class SharedClient:
    def __init__(self, at_parse: Event, release: Event):
        self.at_parse = at_parse
        self.release = release

    def review(self, reviewer: str) -> ReviewerResult:
        # Same shared mutable instance-field pattern as the source method.
        self.last_usage = {}
        self.last_churn = {}
        self.last_context_escalations = ()
        self.last_execution_seconds = None
        self.last_runtime_metadata = {"provider": "codex_cli"}

        # Distinct mocked provider results; no LLM or external process is used.
        n = 101 if reviewer == "A" else 202
        stdout = json.dumps({"contribution": f"result_{reviewer}", "usage": {"input_tokens": n}})
        self.last_execution_seconds = float(n)
        self.last_usage = {"input_tokens": n}
        self.last_usage.update({"output_tokens": n + 1})
        self.last_usage_snapshots = ({"input_tokens": n},)
        self.last_churn = {"tool_loop_operations": n}
        self.last_runtime_metadata.update({"observed_invocation": reviewer})

        # _codex_final_message/json parsing is invocation-local in the source;
        # telemetry copied below is not. Force a possible context switch here.
        if reviewer == "A":
            self.at_parse.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test scheduling timed out")
        raw = json.loads(stdout)
        return ReviewerResult(
            reviewer, str(raw["contribution"]),
            usage=dict(self.last_usage),
            runtime_metadata=dict(self.last_runtime_metadata),
            churn=dict(self.last_churn),
            duration_seconds=self.last_execution_seconds,
            usage_snapshots=self.last_usage_snapshots,
        )

if __name__ == "__main__":
    at_parse, release = Event(), Event()
    client = SharedClient(at_parse, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(client.review, "A")
        if not at_parse.wait(timeout=5):
            release.set()
            raise RuntimeError("A did not reach the selected interleaving")
        try:
            b = pool.submit(client.review, "B").result(timeout=5)
        finally:
            release.set()
        a = a.result(timeout=5)
    assert a.reviewer == "A" and a.contribution == "result_A"
    assert a.usage["input_tokens"] == 202
    assert a.runtime_metadata["observed_invocation"] == "B"
    print(json.dumps({
        "scope": "reduced shared-state sequence harness, not the original class or live EP",
        "source_sha": "62eb6c4631cc23b9e4d2a53043216be6f20bfaae",
        "reviewer_A": {"contribution": a.contribution, "expected_input_tokens": 101,
                       "observed_input_tokens": a.usage["input_tokens"],
                       "observed_telemetry_invocation": a.runtime_metadata["observed_invocation"]},
        "reviewer_B": {"contribution": b.contribution, "input_tokens": b.usage["input_tokens"]},
        "result": "cross-review telemetry attribution reproduced; result content remains local",
    }, indent=2))
