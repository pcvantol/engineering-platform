"""Normalized errors shared by Execution Host responsibility modules."""
from __future__ import annotations


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
    ) -> None:
        super().__init__(persistent_diagnostic)
        self.console_detail = console_detail
        self.next_action = next_action
        self.terminal_condition = terminal_condition


class CodexHandoffTimeout(RunnerError):
    """A bounded agent hand-off exceeded its host-owned deadline."""
