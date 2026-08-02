"""Read-only, redacted Codex analysis of one terminal Engineering Report."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from .agent_state import redact_diagnostic


MAX_ANALYSIS_LENGTH = 8_000
_RUN_ID = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _final_message(output: str) -> str:
    """Extract the terminal agent message from Codex JSONL output."""
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return output.strip().splitlines()[-1] if output.strip() else ""


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "issues": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "next_steps": {"type": "array", "items": {"type": "string"}},
            "product_architect_advice": {"type": "string"},
        },
        "required": (
            "summary",
            "findings",
            "issues",
            "risks",
            "next_steps",
            "product_architect_advice",
        ),
    }


def _markdown(payload: dict[str, object]) -> str:
    def items(value: object) -> str:
        values = value if isinstance(value, list) else []
        lines = [f"- {redact_diagnostic(str(item), limit=1_000)}" for item in values if isinstance(item, str)]
        return "\n".join(lines) if lines else "- Geen vastgesteld."

    return "\n".join(
        (
            "# Codex-analyse van Engineeringrapport",
            "",
            "Deze analyse is adviserend. Het terminale checkpoint, repositorybewijs, commits en validatie blijven de bron van waarheid.",
            "",
            "## Samenvatting",
            redact_diagnostic(str(payload.get("summary", "Analyse niet beschikbaar.")), limit=1_500),
            "",
            "## Bevindingen",
            items(payload.get("findings")),
            "",
            "## Issues",
            items(payload.get("issues")),
            "",
            "## Risico's",
            items(payload.get("risks")),
            "",
            "## Volgende stappen",
            items(payload.get("next_steps")),
            "",
            "## Advies aan Product Architect",
            redact_diagnostic(str(payload.get("product_architect_advice", "Geen aanvullend advies beschikbaar.")), limit=1_500),
            "",
        )
    )[:MAX_ANALYSIS_LENGTH]


def _write(root: Path, run_id: str, value: str) -> Path:
    directory = root / ".engineering" / "report-analysis"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{run_id}.md"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def analyze(root: Path, run_id: str, report: Path) -> Path:
    """Persist a bounded advisory analysis; failure never changes transaction outcome."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id is invalid")
    try:
        report_text = report.read_text(encoding="utf-8")
    except OSError:
        return _write(root, run_id, _markdown({"summary": "Engineeringrapport was niet beschikbaar voor analyse."}))
    prompt = """Analyseer uitsluitend het onderstaande Engineeringrapport. Voer geen commando's uit, wijzig geen bestanden, doe geen netwerkverzoeken en doe geen aannames buiten het rapport. Het resultaat is adviserend: repositorybewijs, commits, validatie en het terminale checkpoint zijn altijd leidend. Geef compacte, feitelijke Nederlandse tekst voor samenvatting, bevindingen, issues, risico's, volgende stappen en advies aan de Product Architect. Herhaal geen geheimen, promptinhoud of ruwe loguitvoer.\n\nENGINEERINGRAPPORT:\n""" + report_text
    schema_path: Path | None = None
    try:
        state_directory = root / ".engineering"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=state_directory, delete=False) as handle:
            json.dump(_schema(), handle)
            schema_path = Path(handle.name)
        completed = subprocess.run(
            (
                "codex", "exec", "--sandbox", "read-only", "-C", str(root), "--json",
                "--output-schema", str(schema_path), prompt,
            ),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            return _write(root, run_id, _markdown({"summary": "Codex-analyse kon niet worden uitgevoerd. De Engineering-uitkomst blijft ongewijzigd."}))
        raw = json.loads(_final_message(completed.stdout))
        if not isinstance(raw, dict):
            raise ValueError("analysis result is invalid")
        return _write(root, run_id, _markdown(raw))
    except (OSError, ValueError, json.JSONDecodeError):
        return _write(root, run_id, _markdown({"summary": "Codex-analyse kon niet veilig worden verwerkt. De Engineering-uitkomst blijft ongewijzigd."}))
    finally:
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
