"""Deprecated compatibility alias for :mod:`tools.engineering.execution_host`.

New integrations must import the provider-neutral Execution Host module.  The
alias remains only so existing local automation and external test extensions
continue to resolve during the migration.
"""

from __future__ import annotations

import sys

from . import execution_host as _execution_host


sys.modules[__name__] = _execution_host
