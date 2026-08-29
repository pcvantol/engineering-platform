"""Structural identities for repository-defined validation controls."""
from __future__ import annotations

import shlex


CANONICAL_DASHBOARD_COMMAND = "npm run test:engineering-dashboard"
_CANONICAL_DASHBOARD_TOKENS = ("npm", "run", "test:engineering-dashboard")
_SHELL_CONTROL_TOKENS = frozenset({";", "&&", "||", "|", "&", "<", ">"})


def is_canonical_dashboard_command(command: str) -> bool:
    """Return whether *command* structurally invokes the dashboard launcher.

    The optional arguments after ``--`` belong to npm's script invocation.
    Browser-related command text, diagnostics, and shell compositions are not
    dashboard validation controls.
    """
    if not isinstance(command, str) or not command.strip():
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
