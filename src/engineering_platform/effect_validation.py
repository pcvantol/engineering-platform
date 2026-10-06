"""Actual deterministic report, document and source-containment controls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import site
import sys
from urllib.parse import unquote, urlsplit

from .effect_contract import EffectContractError, digest, parse, validate_result
from .effect_workspace import read_json, verify_snapshot


HOST_CONTROLS = ("effect_source_binding", "effect_output_integrity", "effect_scope_containment",
                 "report_criteria_contract")


def control_ids(contract: dict[str, object]) -> tuple[str, ...]:
    result = HOST_CONTROLS
    if contract["mode"] in {"DOCUMENTATION_ONLY", "ARCHITECTURE_DESIGN_ONLY"}:
        result += ("document_content_links_schema",)
    if contract["mode"] == "ARCHITECTURE_DESIGN_ONLY":
        result += ("design_criteria_contract",)
    return result


def repository_command(root: Path) -> tuple[str, ...]:
    """Resolve the consumer's pinned entrypoint without a shell or EP fallback."""
    try:
        raw = json.loads((root / ".engineering-platform/repository.json").read_text())
        validation = raw["validation"]
        if validation["kind"] not in {"command", "script"} or not isinstance(validation["entrypoint"], str):
            raise ValueError("invalid validation")
        command = shlex.split(validation["entrypoint"])
        if not command or any(token in {";", "&&", "||", "|", ">", "<"} for token in command):
            raise ValueError("invalid command")
        assignments = []
        while command and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", command[0], re.DOTALL):
            assignment = command.pop(0)
            key, value = assignment.split("=", 1)
            if key == "PYTHONPATH":
                # Preserve the owning source selection and the selected
                # runtime's dependencies when its resolved binary is used.
                value = ":".join((value, *site.getsitepackages()))
            elif key in {"PATH", "HOME", "TMPDIR"} or key.startswith(("GIT_", "CODEX_", "LD_", "DYLD_")):
                raise ValueError("validation cannot replace host isolation settings")
            assignments.append(key + "=" + value)
        if not command:
            raise ValueError("validation executable is missing")
        if command[0] in {"python", "python3"}:
            command[0] = sys.executable
        return tuple((["/usr/bin/env", *assignments] if assignments else []) + command)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise EffectContractError("EFFECT_REQUIRED_VALIDATION_UNAVAILABLE") from error


def validate_document_links(result: dict[str, object], source_paths: set[str]) -> None:
    available = source_paths | {item["path"] for item in result["files"]}
    for item in result["files"]:
        if len(item["content"].strip()) < 40:
            raise EffectContractError("EFFECT_DOCUMENT_EMPTY")
        for reference in re.findall(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", item["content"]):
            link = urlsplit(reference)
            if link.scheme in {"https", "http", "mailto"} or not link.path:
                continue
            path = unquote(link.path)
            if link.scheme or path.startswith("/") or "\\" in path:
                raise EffectContractError("EFFECT_DOCUMENT_LINK_UNSAFE")
            parts = list(PurePosixPath(item["path"]).parent.parts)
            for part in path.split("/"):
                if part == "..":
                    if not parts:
                        raise EffectContractError("EFFECT_DOCUMENT_LINK_UNSAFE")
                    parts.pop()
                elif part not in {"", "."}:
                    parts.append(part)
            if "/".join(parts) not in available:
                raise EffectContractError("EFFECT_DOCUMENT_LINK_MISSING")


def execute(control: str, envelope: dict[str, object], snapshot: Path) -> None:
    contract = parse({"effect_contract": envelope["contract"]})
    manifest = envelope["source_manifest"]
    result = validate_result(contract, envelope["result"], set(manifest))
    if control not in control_ids(contract):
        raise EffectContractError("UNKNOWN_EFFECT_CONTROL")
    if control in {"effect_source_binding", "effect_output_integrity", "effect_scope_containment"}:
        if (envelope["contract_digest"] != digest(contract)
                or envelope["source_manifest_digest"] != digest(manifest)
                or envelope["binding"]["source_revision"] != contract["source_revision"]):
            raise EffectContractError("EFFECT_RESULT_BINDING_MISMATCH")
        verify_snapshot(snapshot, manifest)
    elif control == "document_content_links_schema":
        validate_document_links(result, set(manifest))
    elif control == "design_criteria_contract":
        # Quality must judge the substance. These sections make absence
        # deterministic and keep evidence-only and Git designs equivalent.
        text = (result["summary"] + "\n" + "\n".join(item["analysis"] for item in result["criteria"])
                + "\n" + "\n".join(item["content"] for item in result["files"]))
        for section in ("alternatives", "boundaries", "decision", "open questions"):
            if re.search(r"(?im)^#{1,6}\s+" + re.escape(section) + r"\s*$", text) is None:
                raise EffectContractError("EFFECT_DESIGN_SECTION_MISSING")


def main() -> int:
    try:
        control, report, expected, source = sys.argv[1:]
        execute(control, read_json(Path(report), expected), Path(source))
    except (OSError, KeyError, TypeError, ValueError):
        print("Effect control failed; inspect the bound result and source manifest.")
        return 1
    print("Effect control passed against the pinned result and source manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
