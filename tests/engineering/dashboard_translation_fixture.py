#!/usr/bin/env python3
"""Isolated real Console/HTTP/provider fixture; never invokes a managed runtime.

Only executable selection and the translation service deadline are substituted.
The production handler, validation, CodexCliProvider.invoke, subprocess capture,
output validation and browser module all remain real.  This file also acts as
the harmless selected executable: ``exec ... INPUT:`` receives the exact Codex
arguments produced by the translation service and emits its JSON event format.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


SOURCE = os.environ.get("EP_TRANSLATION_FIXTURE_DIAGNOSTIC", "Validation blocked. Expected: usable scratch. Observed: missing directory.")
DEADLINE_SECONDS = 1.5


def journal(root: Path, record: dict[str, object]) -> None:
    descriptor = os.open(root / "provider.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def provider() -> int:
    root = Path(os.environ["EP_TRANSLATION_FIXTURE_ROOT"])
    instruction = sys.argv[-1]
    target = re.search(r"target locale `([^`]+)`", instruction).group(1)
    sources = json.loads(instruction.split("\n\nINPUT:\n", 1)[1])
    previous = (root / "provider.jsonl").read_text() if (root / "provider.jsonl").exists() else ""
    journal(root, {"event": "start", "pid": os.getpid(), "locale": target, "texts": sources, "time": time.monotonic()})
    if any(text.startswith("deadline::") for text in sources):
        time.sleep(4)
    if any(text.startswith("deadline-once::") and text not in previous for text in sources):
        time.sleep(4)
    if any(text.startswith("race::") or text == SOURCE for text in sources):
        time.sleep(0.4 if target == "nl" else 0.05)
    if any(text.startswith("changed::") for text in sources):
        time.sleep(0.35)
    if any(text.startswith("stress::") for text in sources):
        time.sleep(0.025)
    translations = [f"{target}::{text[:80]}" for text in sources]
    for index, source in enumerate(sources):
        if source == SOURCE:
            translations[index] = {
                "nl": "Validatie geblokkeerd. Verwacht: bruikbare werkmap. Waargenomen: ontbrekende map.",
                "de": "Validierung blockiert. Expected: nutzbares Arbeitsverzeichnis. Observed: fehlendes Verzeichnis.",
                "fr": "Validation bloquée. Expected: répertoire utilisable. Observed: répertoire absent.",
                "es": "Validación bloqueada. Expected: directorio utilizable. Observed: directorio ausente.",
            }[target]
    if any(text.startswith("cardinality::") for text in sources):
        translations.pop()
    if any(text.startswith("invalid::") for text in sources):
        translations[0] = {"not": "a string"}
    if any(text.startswith("oversized::") for text in sources):
        translations[0] = "x" * 6145
    payload = {"translations": translations}
    text = "not json" if any(source.startswith("malformed::") for source in sources) else json.dumps(payload)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}), flush=True)
    journal(root, {"event": "finish", "pid": os.getpid(), "locale": target, "time": time.monotonic()})
    return 0


def evidence(root: Path) -> dict[str, object]:
    with sqlite3.connect(root / "epdata.sqlite") as connection:
        stored = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("ep_execution_runs", "ep_submissions", "ep_parity_lifecycle_dispatches", "execution_lifecycle_events")
        }
        stored["active_diagnostic"] = connection.execute(
            "SELECT payload FROM engineering_component_logs WHERE json_extract(payload, '$.run_id')='translation-active' ORDER BY id",
        ).fetchall()
        return stored


def serve(root: Path) -> None:
    from http.server import ThreadingHTTPServer
    from engineering_platform import dashboard_translation, project_topology, providers
    from engineering_platform.server import _HealthHandler, initialize

    # The real CodexCliProvider resolves this harmless executable; invoke itself
    # is deliberately not mocked.  The subprocess inherits an isolated HOME.
    providers.codex_cli_executable = lambda: str(root / "provider.py")
    dashboard_translation.CHAT_TIMEOUT_SECONDS = DEADLINE_SECONDS
    (root / "module-origin.json").write_text(json.dumps({
        "translation": str(Path(dashboard_translation.__file__).resolve()),
        "providers": str(Path(providers.__file__).resolve()),
        "deadline_seconds": DEADLINE_SECONDS,
    }), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    initialize(root, bind_port=server.server_address[1])
    with sqlite3.connect(root / "epdata.sqlite") as connection:
        project_topology.register_server_local_topology(connection, declaration={
            "schema_version": "1.0", "project": {"id": "translation-fixture", "authority_repository_id": "translation-repository"},
            "repository": {"id": "translation-repository", "role": "authority"}, "validation": {"kind": "none"},
        })
        connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", (
            "translation-history", "translation-fixture", "BLOCKED", "2026-09-14T10:00:00Z", "2026-09-14T10:01:00Z", "MANAGED",
        ))
        connection.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "translation-submission", "translation-fixture", "translation-repository", "test", "TEST", "HTTP",
            "Original technical prompt: /tmp/scratch --validate deadbeef", "fixture-digest", "{}", "QUEUED", "ADMITTED", "2026-09-14T10:00:00Z",
        ))
        connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (
            "translation-submission", "translation-fixture", "translation-repository", "translation-history", "BLOCKED", "CENTRAL:prompt", "2026-09-14T10:00:00Z", "2026-09-14T10:01:00Z",
        ))
        connection.execute("INSERT INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,?)", (
            "translation-history", "BLOCKED", json.dumps({"diagnostic": SOURCE}), "2026-09-14T10:01:00Z",
        ))
        connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at,execution_mode) VALUES(?,?,?,?,?,?)", (
            "translation-active", "translation-fixture", "RUNNING", "2026-09-14T10:02:00Z", "2026-09-14T10:03:00Z", "MANAGED",
        ))
        connection.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "translation-active-submission", "translation-fixture", "translation-repository", "test", "TEST", "HTTP",
            "Original active prompt: /tmp/scratch --validate deadbeef", "active-fixture-digest", "{}", "QUEUED", "ADMITTED", "2026-09-14T10:02:00Z",
        ))
        connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (
            "translation-active-submission", "translation-fixture", "translation-repository", "translation-active", "RUNNING", "CENTRAL:prompt", "2026-09-14T10:02:00Z", "2026-09-14T10:03:00Z",
        ))
        connection.execute("INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)", (
            "operations_console", json.dumps({"run_id": "translation-active", "diagnostic": SOURCE}), "2026-09-14T10:03:00Z",
        ))
    (root / "evidence-before.json").write_text(json.dumps(evidence(root), sort_keys=True), encoding="utf-8")
    server.data_root = root
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if sys.argv[1] == "exec":
        raise SystemExit(provider())
    if sys.argv[1] == "--version":
        print("codex-cli 0.0.0-translation-fixture")
        raise SystemExit(0)
    if sys.argv[1] == "--evidence":
        print(json.dumps(evidence(Path(sys.argv[2])), sort_keys=True))
    elif sys.argv[1] == "--server":
        serve(Path(sys.argv[2]))
    else:
        # Health probes may ask for login status or app-server support. The
        # harmless executable has neither; never turn their arguments into a
        # data-root pathname or start an unintended fixture server.
        raise SystemExit(2)
