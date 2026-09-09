"""Isolated source-logic reproductions; not the EP test suite or installed proof.

Source: pcvantol/engineering-platform @
62eb6c4631cc23b9e4d2a53043216be6f20bfaae
Functions copied from provider_context.py and provider_usage.py.
Only their standard-library dependencies are supplied here.
"""
from dataclasses import dataclass
from enum import StrEnum
import json
import re

class ProviderRole(StrEnum):
    SPECIALIST_REVIEW = "SPECIALIST_REVIEW"
    IMPLEMENTATION = "IMPLEMENTATION"
    QUALITY_REVIEW = "QUALITY_REVIEW"
    REPAIR = "REPAIR"
    FINALIZATION = "FINALIZATION"

@dataclass(frozen=True)
class ContextProjection:
    role: ProviderRole
    text: str
    source_item_count: int
    omitted_low_priority_count: int

_ROLE_BUDGETS = {
    ProviderRole.SPECIALIST_REVIEW: 18_000,
    ProviderRole.IMPLEMENTATION: 60_000,
    ProviderRole.QUALITY_REVIEW: 22_000,
    ProviderRole.REPAIR: 18_000,
    ProviderRole.FINALIZATION: 18_000,
}
_MANDATORY_HEADINGS = re.compile(
    r"\b(?:objective|doel|acceptance|acceptatie|constraint|beperking|"
    r"safety|veilig|authority|autoriteit|validation|validatie|required|"
    r"verplicht|non-negotiable|niet-onderhandelbaar|scope|niet wijzigen|do not)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")

def project_context(role: ProviderRole, objective: str) -> ContextProjection:
    if role == ProviderRole.IMPLEMENTATION:
        return ContextProjection(role, objective, 1, 0)
    sections = _markdown_sections(objective)
    selected = [section for heading, section in sections if heading == "preamble" or _MANDATORY_HEADINGS.search(heading)]
    if not selected:
        return ContextProjection(role, objective, 1, 0)
    budget = _ROLE_BUDGETS[role]
    included: list[str] = []
    used = 0
    for section in selected:
        size = len(section.encode("utf-8"))
        if included and used + size > budget:
            continue
        included.append(section)
        used += size
    text = "\n\n".join(included)
    return ContextProjection(role, text, len(sections), max(0, len(sections) - len(included)))

def _markdown_sections(value: str) -> list[tuple[str, str]]:
    lines = value.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            starts.append((index, match.group(1)))
    if not starts:
        return []
    result: list[tuple[str, str]] = []
    if starts[0][0]:
        preamble = "\n".join(lines[:starts[0][0]]).strip()
        if preamble:
            result.append(("preamble", preamble))
    for ordinal, (start, heading) in enumerate(starts):
        end = starts[ordinal + 1][0] if ordinal + 1 < len(starts) else len(lines)
        result.append((heading, "\n".join(lines[start:end]).strip()))
    return result

def churn_from_jsonl(*outputs: str) -> dict[str, int]:
    result = {
        key: 0
        for key in (
            "file_read_count", "distinct_files_read", "repeated_file_read_count",
            "glob_search_calls", "grep_calls", "shell_command_calls", "test_commands",
            "tool_output_bytes", "maximum_tool_output_bytes", "passing_test_output_bytes",
            "failed_test_diagnostic_bytes", "git_output_bytes", "github_output_bytes",
            "historical_commit_queries", "historical_commit_results", "historical_pr_queries",
            "historical_pr_results", "historical_context_bytes", "tool_loop_operations",
        )
    }
    reads: set[str] = set()
    historical_observed = False
    for output in outputs:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            command = item.get("command")
            if not isinstance(command, str):
                continue
            normalized = command.casefold()
            result["shell_command_calls"] += 1
            result["tool_loop_operations"] += 1
            is_test = bool(re.search(r"\b(?:pytest|unittest|tox|nox|playwright)\b", normalized))
            if is_test:
                result["test_commands"] += 1
            if re.search(r"\b(?:rg|grep)\b", normalized):
                result["grep_calls"] += 1
            if re.search(r"\b(?:find|rg\s+--files|glob)\b", normalized):
                result["glob_search_calls"] += 1
            if re.search(r"\b(?:cat|sed|head|tail|less|awk)\b", normalized):
                fingerprint = re.sub(r"\s+", " ", command.strip())[:512]
                result["file_read_count"] += 1
                if fingerprint in reads:
                    result["repeated_file_read_count"] += 1
                reads.add(fingerprint)
            raw = item.get("aggregated_output", item.get("output", ""))
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
            result["tool_output_bytes"] += size
            result["maximum_tool_output_bytes"] = max(result["maximum_tool_output_bytes"], size)
            if is_test:
                if item.get("exit_code") == 0:
                    result["passing_test_output_bytes"] += size
                else:
                    result["failed_test_diagnostic_bytes"] += size
            if re.search(r"\b(?:git)\b", normalized):
                result["git_output_bytes"] += size
            if re.search(r"\b(?:gh)\b", normalized):
                result["github_output_bytes"] += size
            if re.search(r"\bgit\s+(?:log|blame)\b", normalized):
                historical_observed = True
                result["historical_commit_queries"] += 1
                result["historical_commit_results"] += len(raw.splitlines()) if isinstance(raw, str) else 0
                result["historical_context_bytes"] += size
            if re.search(r"\bgh\s+(?:pr\s+list|search\s+prs)\b", normalized):
                historical_observed = True
                result["historical_pr_queries"] += 1
                result["historical_pr_results"] += len(raw.splitlines()) if isinstance(raw, str) else 0
                result["historical_context_bytes"] += size
    result["distinct_files_read"] = len(reads)
    if not historical_observed:
        for key in (
            "historical_commit_queries", "historical_commit_results", "historical_pr_queries",
            "historical_pr_results", "historical_context_bytes",
        ):
            result.pop(key)
    return result

if __name__ == "__main__":
    objective = "# Objective\n" + "x" * 17990 + "\n\n# Safety\nNEVER_PUBLISH_BEFORE_ASSURANCE\n\n# Acceptance criteria\nREQUIRED_NEGATIVE_TEST\n"
    p = project_context(ProviderRole.SPECIALIST_REVIEW, objective)
    assert "NEVER_PUBLISH_BEFORE_ASSURANCE" not in p.text
    assert "REQUIRED_NEGATIVE_TEST" not in p.text
    assert p.omitted_low_priority_count == 2
    item = {"id": "cmd-1", "type": "command_execution", "command": "cat sample.py"}
    events = [
        {"type": "item.started", "item": {**item, "status": "in_progress"}},
        {"type": "item.completed", "item": {**item, "status": "completed", "exit_code": 0, "aggregated_output": "pass\n"}},
    ]
    churn = churn_from_jsonl("\n".join(json.dumps(e) for e in events))
    assert churn["shell_command_calls"] == 2
    assert churn["repeated_file_read_count"] == 1
    print(json.dumps({
        "source_sha": "62eb6c4631cc23b9e4d2a53043216be6f20bfaae",
        "scope": "isolated copied function bodies; not full-suite or installed proof",
        "context": {"projected_bytes": len(p.text.encode()), "omitted_sections": p.omitted_low_priority_count,
                    "safety_preserved": "NEVER_PUBLISH_BEFORE_ASSURANCE" in p.text,
                    "acceptance_preserved": "REQUIRED_NEGATIVE_TEST" in p.text},
        "one_command_two_events": {"actual_distinct_command_ids": 1,
                    "reported_shell_command_calls": churn["shell_command_calls"],
                    "reported_repeated_file_reads": churn["repeated_file_read_count"]},
    }, indent=2))
