"""Normalized errors shared by Execution Host responsibility modules."""
from __future__ import annotations
from functools import wraps


class CheckpointContinuationStopped(Exception):
    """A newer canonical checkpoint ended this invocation, without rebinding it."""

    def __init__(self, state: object) -> None:
        super().__init__("The canonical checkpoint superseded this continuation.")
        self.state = state


def checkpoint_continuation_boundary(function):
    """Return canonical truth only at the public invocation's outer boundary."""
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CheckpointContinuationStopped as stopped:
            return stopped.state
    return invoke


class RunnerError(RuntimeError):
    """A fail-closed engineering-runner diagnostic."""


class CodexInvocationError(RunnerError):
    """Separates safe checkpoint diagnostics from bounded console evidence."""

    def __init__(
        self,
        persistent_diagnostic: str,
        console_detail: str,
        *,
        next_action: str = "inspect_codex_cli",
        terminal_condition: str = "codex_invocation_failed",
        interruption_reason: str | None = None,
    ) -> None:
        super().__init__(persistent_diagnostic)
        self.console_detail = console_detail
        self.next_action = next_action
        self.terminal_condition = terminal_condition
        self.interruption_reason = interruption_reason

    @property
    def provider_turn_interrupted(self) -> bool:
        """Whether provider evidence proves a turn ended without a result."""
        return self.terminal_condition == "provider_turn_interrupted" and bool(self.interruption_reason)


class CodexHandoffTimeout(RunnerError):
    """A bounded agent hand-off exceeded its host-owned deadline."""


class ProviderReadinessBlocked(RunnerError):
    """A durable checkpoint is waiting for explicit local provider recovery."""

    def __init__(self, state: object) -> None:
        super().__init__("Provider readiness must be repaired before this action can continue.")
        self.state = state
