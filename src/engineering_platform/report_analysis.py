"""Read-only, redacted Codex analysis of one terminal Engineering Report."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .agent_state import redact_diagnostic
from .providers import CodexCliProvider


MAX_ANALYSIS_LENGTH = 8_000
MAX_REPORT_CONTEXT_LENGTH = 300_000
_RUN_ID = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class AnalysisProcessingError(ValueError):
    """A safe, user-presentable reason for rejecting advisory output."""


_PROCESSING_REASONS = {
    "processed": "De Codex-analyse is volgens het verplichte structuurcontract verwerkt.",
    "report_unavailable": "Het Engineeringrapport was niet beschikbaar voor deze analyse.",
    "provider_failed": "De EP-beheerde Codex CLI kon de analyseopdracht niet afronden.",
    "provider_unavailable": "De EP-beheerde Codex CLI was niet beschikbaar voor de analyseopdracht.",
    "invalid_structured_response": "Codex gaf geen geldig gestructureerd analyseantwoord terug.",
}


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


def _markdown(payload: dict[str, object], *, processing_status: str = "processed") -> str:
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
            "## Analyseverwerking",
            f"- Status: `{processing_status}`",
            f"- Reden: {_PROCESSING_REASONS[processing_status]}",
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


def _fallback(root: Path, run_id: str, status: str, summary: str) -> Path:
    """Write a controlled processing reason without persisting provider output."""
    return _write(root, run_id, _markdown({"summary": summary}, processing_status=status))


def _payload(output: str) -> dict[str, object]:
    """Accept only an object containing each required structured field."""
    try:
        raw = json.loads(_final_message(output))
    except json.JSONDecodeError as error:
        raise AnalysisProcessingError("invalid structured response") from error
    if not isinstance(raw, dict) or set(_schema()["required"]) - set(raw):
        raise AnalysisProcessingError("invalid structured response")
    return raw


def _bounded_report_context(report_text: str) -> str:
    """Keep advisory input below the provider limit without altering report evidence."""
    if len(report_text) <= MAX_REPORT_CONTEXT_LENGTH:
        return report_text
    first = MAX_REPORT_CONTEXT_LENGTH // 2
    last = MAX_REPORT_CONTEXT_LENGTH - first
    omitted = len(report_text) - first - last
    return (
        report_text[:first]
        + f"\n\n[Analysecontext ingekort: {omitted} tekens uit het midden zijn niet aan Codex aangeboden. "
        "Het volledige Engineeringrapport blijft de bron van waarheid.]\n\n"
        + report_text[-last:]
    )


def analyze(root: Path, run_id: str, report: Path) -> Path:
    """Persist a bounded advisory analysis; failure never changes transaction outcome."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id is invalid")
    try:
        report_text = report.read_text(encoding="utf-8")
    except OSError:
        return _fallback(root, run_id, "report_unavailable", "Engineeringrapport was niet beschikbaar voor analyse.")
    prompt = """Analyseer uitsluitend het onderstaande Engineeringrapport. Voer geen commando's uit, wijzig geen bestanden, doe geen netwerkverzoeken en doe geen aannames buiten het rapport. Het resultaat is adviserend: repositorybewijs, commits, validatie en het terminale checkpoint zijn altijd leidend. Geef compacte, feitelijke Nederlandse tekst voor samenvatting, bevindingen, issues, risico's, volgende stappen en advies aan de Product Architect. Herhaal geen geheimen, promptinhoud of ruwe loguitvoer.\n\nENGINEERINGRAPPORT:\n""" + _bounded_report_context(report_text)
    schema_path: Path | None = None
    try:
        state_directory = root / ".engineering"
        state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=state_directory, delete=False) as handle:
            json.dump(_schema(), handle)
            schema_path = Path(handle.name)
        completed = CodexCliProvider().invoke(
            root,
            (
                "codex", "exec", "--sandbox", "read-only", "-C", str(root), "--json",
                "--output-schema", str(schema_path), "-",
            ),
            input_text=prompt,
        )
        if completed.returncode:
            return _fallback(root, run_id, "provider_failed", "Codex-analyse kon niet worden uitgevoerd. De Engineering-uitkomst blijft ongewijzigd.")
        raw = _payload(completed.stdout)
        return _write(root, run_id, _markdown(raw))
    except OSError:
        return _fallback(root, run_id, "provider_unavailable", "Codex-analyse kon niet worden uitgevoerd. De Engineering-uitkomst blijft ongewijzigd.")
    except AnalysisProcessingError:
        return _fallback(root, run_id, "invalid_structured_response", "Codex-analyse kon niet veilig worden verwerkt. De Engineering-uitkomst blijft ongewijzigd.")
    finally:
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
