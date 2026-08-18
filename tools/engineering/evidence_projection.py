"""Invocation-local bounded projections for oversized shell evidence.

The Engineering runner never persists raw tool output.  Codex executes shell
tools inside its own provider turn, so the temporary PATH proxy created here is
the only safe interception point: it preserves exit status, returns small or
source output unchanged, and makes an explicit raw expansion available only to
the same invocation via ``DJCONNECT_EVIDENCE_EXPAND=1``.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
from tempfile import TemporaryDirectory
from typing import Iterable, Mapping


# Limits are category-specific because passing logs are repetitive, while a
# failure needs an assertion, traceback and surrounding context to be useful.
PASSING_TEST_LIMIT = 768
FAILED_DIAGNOSTIC_LIMIT = 12_288
SEARCH_MATCH_LIMIT = 24
SEARCH_LINE_LIMIT = 240
GIT_FACT_LIMIT = 2_048
GITHUB_FACT_LIMIT = 4_096
PROXIED_TOOLS = ("git", "gh", "rg", "grep", "pytest", "python", "python3", "npm", "npx")


@dataclass(frozen=True)
class EvidenceProjection:
    category: str
    text: str
    raw_bytes: int
    projected_bytes: int
    more_evidence_available: bool


def category_for(command: Iterable[str]) -> str:
    values = tuple(command)
    executable = Path(values[0]).name if values else ""
    normalized = " ".join(values).casefold()
    if executable in {"rg", "grep"}:
        return "search"
    if executable == "git":
        return "git"
    if executable == "gh":
        return "github"
    if executable in {"pytest", "npm", "npx"} or (
        executable in {"python", "python3"} and "unittest" in normalized
    ):
        return "test"
    return "other"


def _bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _bounded_lines(value: str, *, count: int, width: int) -> str:
    return "\n".join(line[:width] for line in value.splitlines()[:count])


def _failure_tail(value: str) -> str:
    lines = value.splitlines()
    if _bytes(value) <= FAILED_DIAGNOSTIC_LIMIT:
        return value
    head = "\n".join(lines[:30])
    tail = "\n".join(lines[-90:])
    return f"{head}\n… FAILED_DIAGNOSTIC_MIDDLE_OMITTED …\n{tail}"[-FAILED_DIAGNOSTIC_LIMIT:]


def project_output(command: Iterable[str], output: str, exit_code: int) -> EvidenceProjection:
    """Project one completed command without losing the raw expansion path."""
    category = category_for(command)
    raw_bytes = _bytes(output)
    if not output or category == "other":
        return EvidenceProjection(category, output, raw_bytes, raw_bytes, False)
    if category == "test":
        if exit_code:
            text = _failure_tail(output)
            more = text != output
        elif raw_bytes > PASSING_TEST_LIMIT:
            passed = next((line for line in output.splitlines() if "passed" in line.casefold()), "PASS")
            text = f"PASSING_TEST_OUTPUT_BOUNDED\n{passed[:PASSING_TEST_LIMIT // 2]}\nMORE_EVIDENCE_AVAILABLE: set DJCONNECT_EVIDENCE_EXPAND=1 for this command."
            more = True
        else:
            text, more = output, False
    elif category == "search" and len(output.splitlines()) > SEARCH_MATCH_LIMIT:
        text = _bounded_lines(output, count=SEARCH_MATCH_LIMIT, width=SEARCH_LINE_LIMIT)
        text += f"\nMATCHES_BOUNDED: shown={SEARCH_MATCH_LIMIT} total={len(output.splitlines())}\nMORE_EVIDENCE_AVAILABLE: set DJCONNECT_EVIDENCE_EXPAND=1 for this command."
        more = True
    elif category == "git" and raw_bytes > GIT_FACT_LIMIT:
        text = _bounded_lines(output, count=32, width=SEARCH_LINE_LIMIT)
        text += "\nGIT_EVIDENCE_BOUNDED\nMORE_EVIDENCE_AVAILABLE: set DJCONNECT_EVIDENCE_EXPAND=1 for this command."
        more = True
    elif category == "github" and raw_bytes > GITHUB_FACT_LIMIT:
        text = _bounded_lines(output, count=48, width=SEARCH_LINE_LIMIT)
        text += "\nGITHUB_EVIDENCE_BOUNDED\nMORE_EVIDENCE_AVAILABLE: set DJCONNECT_EVIDENCE_EXPAND=1 for this command."
        more = True
    else:
        text, more = output, False
    return EvidenceProjection(category, text, raw_bytes, _bytes(text), more)


class ToolProxyEnvironment:
    """Temporary PATH proxy. It retains no output after the invocation ends."""

    def __init__(self) -> None:
        self._temporary: TemporaryDirectory[str] | None = None

    def __enter__(self) -> Mapping[str, str]:
        self._temporary = TemporaryDirectory(prefix="djconnect-evidence-")
        directory = Path(self._temporary.name)
        repository_root = Path(__file__).resolve().parents[2]
        for name in PROXIED_TOOLS:
            launcher = directory / name
            launcher.write_text(
                f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(repository_root)!r})\nfrom tools.engineering.evidence_projection import proxy_main\nproxy_main({name!r})\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)
        environment = dict(os.environ)
        environment["DJCONNECT_EVIDENCE_ORIGINAL_PATH"] = environment.get("PATH", os.defpath)
        environment["PATH"] = f"{directory}{os.pathsep}{environment['DJCONNECT_EVIDENCE_ORIGINAL_PATH']}"
        return environment

    def __exit__(self, *_: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def proxy_main(name: str | None = None) -> None:
    """Run the proxied command and emit only its bounded invocation-local view."""
    name = name or Path(sys.argv[0]).name
    original_path = os.environ.get("DJCONNECT_EVIDENCE_ORIGINAL_PATH", os.defpath)
    executable = shutil.which(name, path=original_path)
    if executable is None:
        raise SystemExit(f"Evidence proxy could not resolve {name}.")
    completed = subprocess.run(  # nosec B603
        (executable, *sys.argv[1:]), text=True, capture_output=True,
        env={**os.environ, "PATH": original_path}, check=False,
    )
    raw = f"{completed.stdout}{completed.stderr}"
    if os.environ.get("DJCONNECT_EVIDENCE_EXPAND") == "1":
        sys.stdout.write(raw)
    else:
        sys.stdout.write(project_output((name, *sys.argv[1:]), raw, completed.returncode).text)
    raise SystemExit(completed.returncode)


def deterministic_fixture() -> dict[str, object]:
    """Comparable raw/projection fixture; it contains no provider or user data."""
    cases = (
        (("rg", "needle"), "\n".join(f"match {item}" for item in range(120)), 0),
        (("git", "log", "--oneline"), "\n".join(f"{item:040x} commit" for item in range(90)), 0),
        (("pytest",), "\n".join(". passing" for _ in range(500)) + "\n500 passed", 0),
        (("pytest",), "FAILED test_example\nAssertionError: expected x\n" + "trace\n" * 400, 1),
    )
    projected = tuple(project_output(command, output, code) for command, output, code in cases)
    raw = sum(item.raw_bytes for item in projected)
    bounded = sum(item.projected_bytes for item in projected)
    return {"raw_tool_output_bytes": raw, "projected_tool_output_bytes": bounded, "suppressed_tool_output_bytes": raw - bounded, "reduction": 0 if not raw else (raw - bounded) / raw, "required_evidence_retained": True, "more_evidence_available": any(item.more_evidence_available for item in projected)}
