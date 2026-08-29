"""Structural identities for repository-defined validation controls."""
from __future__ import annotations

import shlex


CANONICAL_DASHBOARD_COMMAND = "npm run test:engineering-dashboard"
_CANONICAL_DASHBOARD_TOKENS = ("npm", "run", "test:engineering-dashboard")
_SHELL_CONTROL_TOKENS = frozenset({";", "&&", "||", "|", "&", "<", ">"})


def canonical_validation_launcher(command: str) -> str | None:
    """Return the planned launcher carried by a transparent shell transport.

    A provider may report its ``/bin/zsh -lc`` transport command rather than
    the command requested by the validation plan.  Peel only that one
    lossless transport envelope; compositions and diagnostics remain ineligible.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return None
    if len(tokens) == 3 and tokens[0] in {"zsh", "/bin/zsh"} and tokens[1] == "-lc":
        return tokens[2]
    return command


def is_canonical_dashboard_command(command: str) -> bool:
    """Return whether *command* structurally invokes the dashboard launcher.

    The optional arguments after ``--`` belong to npm's script invocation.
    Browser-related command text, diagnostics, and shell compositions are not
    dashboard validation controls.
    """
    command = canonical_validation_launcher(command)
    if command is None:
        return False
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return False
    if any(token in _SHELL_CONTROL_TOKENS for token in tokens):
        return False
    return tokens == _CANONICAL_DASHBOARD_TOKENS or (
        len(tokens) > len(_CANONICAL_DASHBOARD_TOKENS)
        and tokens[:len(_CANONICAL_DASHBOARD_TOKENS)] == _CANONICAL_DASHBOARD_TOKENS
        and tokens[len(_CANONICAL_DASHBOARD_TOKENS)] == "--"
    )
