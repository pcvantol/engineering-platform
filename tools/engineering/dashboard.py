"""Private read-only Engineering Status dashboard; no transaction authority."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
from html import escape
import json
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit
from .platform_api import PlatformConfiguration
from .platform_api import PlatformConfigurationError
from .providers import TailscaleProvider
from .providers import LaunchdProvider
from .inbox_watcher import cloud_root

LABEL = "com.djconnect.engineering-dashboard"
DASHBOARD_VERSION = "1.1.0"
LOOPBACK_ADDRESS = "127.0.0.1"
CODEX_PROCESS = re.compile(r"(?:^|\s)(?:\S*/)?codex(?:\s|$)")


class DashboardHTTPServer(ThreadingHTTPServer):
    """Private dashboard listener with safe restart behavior."""

    allow_reuse_address = True


def _unavailable_status() -> bytes:
    """Return the complete, safe status shape when no projection exists yet."""
    return json.dumps(
        {
            "watcher_state": "REMOTE_ENGINEERING_DEGRADED",
            "current_phase": "status unavailable",
            "current_action": "Run Engineering Platform to publish a status update.",
            "run_id": None,
            "queue_depth": 0,
            "implementation_pr": None,
            "finalization_pr": None,
            "repository_state": "UNKNOWN",
            "workspace_state": "UNKNOWN",
            "diagnostic": "No local engineering status has been published yet.",
            "submitted_filename": None,
            "prompt_title": None,
            "last_executed_filename": None,
            "last_executed_title": None,
            "last_executed_run": None,
            "last_executed_phase": None,
        },
        separators=(",", ":"),
    ).encode()


def _status(root: Path) -> bytes:
    try:
        watcher = json.loads(cloud_root(repo=root).joinpath("status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, PlatformConfigurationError):
        watcher = {}
    try:
        live = json.loads((root / ".djconnect" / "status" / "current.json").read_text(encoding="utf-8"))
        projection = json.dumps(
            {
                "watcher_state": "ENGINEERING_RUN_ACTIVE",
                "current_phase": live.get("phase") or "INITIALIZE",
                "current_action": live.get("current_action") or "Engineering run is active.",
                "run_id": live.get("run_id"),
                "queue_depth": 0,
                "implementation_pr": live.get("implementation_pr"),
                "finalization_pr": live.get("finalization_pr"),
                "repository_state": live.get("repository_state") or "ACTIVE",
                "workspace_state": live.get("workspace_state") or "ACTIVE",
                "prompt_characters": live.get("prompt_characters"),
                "diagnostic": live.get("diagnostic"),
                "submitted_filename": watcher.get("submitted_filename"),
                "prompt_title": watcher.get("prompt_title"),
                "last_executed_filename": watcher.get("last_executed_filename"),
                "last_executed_title": watcher.get("last_executed_title"),
                "last_executed_run": watcher.get("last_executed_run"),
                "last_executed_phase": watcher.get("last_executed_phase"),
                "execution_mode": live.get("execution_mode"),
                "target_repository": live.get("target_repository"),
                "checkout_path": live.get("checkout_path"),
                "active_branch": live.get("active_branch"),
            },
            separators=(",", ":"),
        ).encode()
    except (OSError, json.JSONDecodeError):
        live, projection = None, None
    if live and live.get("phase") not in {"COMPLETE", "BLOCKED", "FAILED"}:
        return projection
    try:
        if watcher and (watcher.get("run_id") or watcher.get("last_executed_run")):
            return json.dumps(watcher, separators=(",", ":")).encode()
        return (root / ".djconnect" / "status" / "status.json").read_bytes()
    except OSError:
        return projection or _unavailable_status()


def _sse_status(root: Path) -> bytes:
    """Encode the status as a single SSE data line."""
    try:
        payload = json.loads(_status(root))
    except json.JSONDecodeError:
        payload = json.loads(_unavailable_status())
    return json.dumps(payload, separators=(",", ":")).encode()


def _latest_codex_log(root: Path) -> bytes:
    """Return only the latest locally redacted Codex diagnostic."""
    logs = sorted((root / ".djconnect" / "logs" / "codex").glob("*.log"))
    try:
        return logs[-1].read_bytes() if logs else b"No Codex CLI diagnostic is available."
    except OSError:
        return b"Codex CLI diagnostic is unavailable."


def _codex_process_metrics() -> bytes:
    """Return read-only local CPU evidence for currently running Codex CLI processes."""
    try:
        observed = subprocess.run(
            ("ps", "-axo", "pid=,pcpu=,command="),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        observed = None
    processes: list[dict[str, int | float]] = []
    if observed and observed.returncode == 0:
        for line in observed.stdout.splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) != 3 or not CODEX_PROCESS.search(parts[2]):
                continue
            try:
                processes.append({"pid": int(parts[0]), "cpu_percent": float(parts[1])})
            except ValueError:
                continue
    return json.dumps(
        {
            "process_count": len(processes),
            "cpu_percent": round(sum(item["cpu_percent"] for item in processes), 1),
            "gpu_status": "Niet beschikbaar: Codex-inference draait extern.",
        },
        separators=(",", ":"),
    ).encode()


def _report_for_run(root: Path, run_id: str | None) -> bytes:
    """Return report evidence only for the exact displayed terminal run."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b""
    try:
        reports = sorted((root / ".djconnect" / "reports").glob(f"*_{run_id}.md"))
        return reports[-1].read_bytes() if reports else b""
    except OSError:
        return b""


def _current_codex_log(root: Path) -> bytes:
    """Return the diagnostic for the exact run currently shown by the dashboard."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"No Codex CLI diagnostic is available for the current run."
    try:
        return (root / ".djconnect" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"No Codex CLI diagnostic is available for the current run."


def _last_executed_codex_log(root: Path) -> bytes:
    """Return only the log bound to the latest completed or failed Inbox run."""
    try:
        run_id = json.loads((root / ".djconnect" / "status" / "status.json").read_text(encoding="utf-8")).get("last_executed_run")
    except (OSError, json.JSONDecodeError):
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
        return b"No Codex CLI diagnostic is available for the last executed run."
    try:
        return (root / ".djconnect" / "logs" / "codex" / f"{run_id}.log").read_bytes()
    except OSError:
        return b"No Codex CLI diagnostic is available for the last executed run."


def _codex_usage(root: Path) -> bytes:
    """Return only CLI-reported usage bound to the displayed current or last run."""
    try:
        status = json.loads(_status(root))
        recorded = json.loads((root / ".djconnect" / "status" / "codex_usage.json").read_text(encoding="utf-8"))
        run_id = recorded.get("run_id")
        usage = recorded.get("usage")
    except (OSError, json.JSONDecodeError):
        return b"{}"
    if run_id not in {status.get("run_id"), status.get("last_executed_run")} or not isinstance(usage, dict):
        return b"{}"
    allowed = {key: value for key, value in usage.items() if isinstance(key, str) and isinstance(value, (int, float, str))}
    return json.dumps(allowed, separators=(",", ":")).encode()


def _completion_commits(root: Path) -> bytes:
    """Return only recorded commit evidence for a completed displayed run."""
    try:
        status = json.loads(_status(root))
        if status.get("current_phase") != "COMPLETE":
            return b"{}"
        run_id = status.get("run_id")
        if not isinstance(run_id, str):
            return b"{}"
        checkpoint = json.loads((root / ".djconnect" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return b"{}"
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    commits = {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}
    return json.dumps(commits, separators=(",", ":")).encode()


def _last_executed_commits(root: Path) -> bytes:
    """Return commit evidence bound to the final last-executed run only."""
    try:
        status = json.loads(_status(root))
        run_id = status.get("last_executed_run")
        phase = status.get("last_executed_phase")
        if not isinstance(run_id, str) or phase != "COMPLETE":
            return b"{}"
        checkpoint = json.loads(
            (root / ".djconnect" / "engineering-runs" / f"{run_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return b"{}"
    labels = {
        "genesis_commit_sha": "Genesis-commit",
        "implementation_merge_commit": "Implementatie-mergecommit",
        "finalization_merge_commit": "Finalisatie-mergecommit",
    }
    commits = {labels[key]: checkpoint[key] for key in labels if isinstance(checkpoint.get(key), str)}
    return json.dumps(commits, separators=(",", ":")).encode()


def _prompt_started(root: Path) -> bytes:
    """Return the recorded Inbox start time for the run currently displayed."""
    try:
        run_id = json.loads(_status(root)).get("run_id")
    except json.JSONDecodeError:
        run_id = None
    if not isinstance(run_id, str):
        return b"{}"
    for record in (root / ".djconnect" / "inbox-processing").glob("*/job.json"):
        try:
            job = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("run_id") == run_id and isinstance(job.get("received_at"), str):
            return json.dumps({"started_at": job["received_at"]}, separators=(",", ":")).encode()
    return b"{}"


def _build_commit(root: Path) -> str:
    """Return the local checked-out revision for read-only dashboard identification."""
    observed = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "--short=12", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    return observed.stdout.strip() if observed.returncode == 0 else "onbekend"


def _dashboard_html(title: str, build_commit: str = "onbekend") -> bytes:
    """Render the private dashboard with client-local, visible refresh timing."""
    page = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>$TITLE</title>
<style>
body{margin:0;background:#121217;color:#f7f3ee;font:16px system-ui;padding:max(20px,env(safe-area-inset-top)) 20px}.dashboard-grid{display:grid;gap:12px}
.card{background:#24242d;border-radius:16px;padding:16px;box-shadow:0 4px 18px #0005}.prompt-runs{display:grid;gap:8px}.prompt-runs__heading{color:#b9b6c0;font-size:13px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.prompt-runs__cards,.last-execution{display:grid;gap:12px}.card--previous{background:#202a36;border:1px solid #37506a;box-shadow:0 4px 18px #0005}.card--previous strong,.card--previous .label{color:#8dc7ff}.field{margin:10px 0 0}.label{display:block;color:#c7a6ff;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;margin-bottom:3px}.estimate-primary{font-size:20px;font-weight:650;margin:12px 0 0}.estimate-meta{color:#b9b6c0;font-size:13px;line-height:1.4;margin:8px 0 0}.final-status{align-items:center;display:flex;gap:7px;margin:0 0 10px}.indicator--small{height:9px;width:9px}.footer{color:#b9b6c0;font-size:13px;margin:28px 0 8px;text-align:center}.copy{float:right;background:#353541;color:#f7f3ee;border:1px solid #57576a;border-radius:8px;padding:7px 10px;font:14px system-ui}
strong{color:#c7a6ff}.status{display:flex;align-items:center;gap:10px}.indicator{width:12px;height:12px;border-radius:50%;background:#9a9aa3;box-shadow:0 0 8px #9a9aa388;flex:none}.indicator--green{background:#51d88a;box-shadow:0 0 8px #51d88a88}.indicator--yellow{background:#f4d35e;box-shadow:0 0 8px #f4d35e88}.indicator--orange{background:#ff9f43;box-shadow:0 0 8px #ff9f4388}.indicator--red{background:#ff6b6b;box-shadow:0 0 8px #ff6b6b88}.indicator--running{background:transparent;border:3px solid #ff9f43;border-right-color:transparent;box-sizing:border-box;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
pre{white-space:pre-wrap;word-break:break-word;margin:8px 0 0;font:12px ui-monospace,monospace}[hidden]{display:none}
@media (min-width:900px){body{max-width:1440px;margin:auto}.dashboard-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.prompt-runs,#report{grid-column:1 / -1}.prompt-runs__cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
<h1>$TITLE</h1>
<main class="dashboard-grid">
<div class="card"><div class="status"><span id="indicator" class="indicator" role="status" aria-label="Status onbekend"></span><strong>Promptstatus</strong></div><p class="field"><span class="label">Watcher</span><span id="watcher">Laden…</span></p><p class="field"><span class="label">Fase</span><span id="phase">Laden…</span></p><p class="field"><span class="label">Huidige actie</span><span id="action">Laden…</span></p></div>
<div class="card"><strong>Tijd</strong><p id="currentTime">Laden…</p><p id="lastRefresh">Laatst ververst: laden…</p><p id="nextRefresh">Volgende verversing: laden…</p></div>
<div class="card"><strong>Geschatte uitvoeringstijd</strong><p class="estimate-primary" id="executionEstimate">Nog niet beschikbaar…</p><p class="estimate-meta" id="executionEstimateMeta" hidden></p></div>
<div class="card" id="processMetrics" hidden><strong>Lokale Codex-processen</strong><p class="field"><span class="label">CPU-gebruik</span><span id="codexCpu">Laden…</span></p><p class="field"><span class="label">Actieve processen</span><span id="codexProcesses">Laden…</span></p><p class="field"><span class="label">GPU-gebruik</span><span id="codexGpu">Laden…</span></p></div>
<div class="card" id="usage" hidden><strong>Codex CLI-gebruik</strong><div class="field"><span class="label">Gerapporteerd verbruik</span><pre id="usageDetails"></pre></div></div>
<div class="card" id="commits" hidden><strong>Voltooiingscommits</strong><div class="field"><span class="label">Vastgelegd bewijs</span><pre id="completionCommits"></pre></div></div>
<section class="prompt-runs" id="promptRuns" aria-label="Promptuitvoeringen" hidden><div class="prompt-runs__heading">Promptuitvoeringen</div><div class="prompt-runs__cards">
<div class="card" id="current" hidden><strong>Huidige uitvoering</strong><p class="field"><span class="label">Prompttitel</span><span id="currentPrompt"></span></p><div class="field"><span class="label">Bestandsnaam</span><pre id="currentFile"></pre></div><div class="field"><span class="label">Codex CLI-diagnose</span><pre id="currentLog">Laden…</pre></div></div>
<div class="last-execution" id="lastExecution" hidden><div class="card card--previous"><div class="final-status"><span id="lastIndicator" class="indicator indicator--small" aria-hidden="true"></span><strong>Laatst uitgevoerd</strong><span id="lastFinalStatus"></span></div><p class="field"><span class="label">Prompttitel</span><span id="lastPrompt"></span></p><div class="field"><span class="label">Bestandsnaam</span><pre id="lastFile"></pre></div><div class="field" id="lastCommits" hidden><span class="label">Git-commit</span><pre id="lastCommitDetails"></pre></div><div class="field"><span class="label">Codex CLI-diagnose</span><pre id="lastLog">Laden…</pre></div></div><details class="card card--previous" id="report" hidden><summary><strong>Engineeringrapport</strong></summary><button class="copy" id="copyReport" type="button" title="Kopieer rapport" aria-label="Kopieer rapport">⧉ Kopieer</button><div class="field"><span class="label">Markdownrapport</span><pre id="reportContent">Open dit blok om het rapport te laden.</pre></div></details></div>
</div></section>
<div class="card"><strong>Uitvoering</strong><p class="field"><span class="label">Run-ID</span><span id="runId"></span></p><p class="field"><span class="label">Prompt gestart op</span><span id="promptStarted">Laden…</span></p><p class="field"><span class="label">Wachtrij</span><span id="queue"></span></p></div>
<div class="card" id="executionContext" hidden><strong>Uitvoeringscontext</strong><p class="field"><span class="label">Modus</span><span id="executionMode"></span></p><p class="field"><span class="label">Repository</span><span id="targetRepository"></span></p><div class="field"><span class="label">Lokale checkout</span><pre id="checkoutPath"></pre></div><p class="field"><span class="label">Actieve branch</span><span id="activeBranch"></span></p></div>
<div class="card"><strong>Pull requests</strong><p class="field"><span class="label">Implementatie</span><span id="implementation"></span></p><p class="field"><span class="label">Finalisatie</span><span id="finalization"></span></p></div>
<div class="card"><strong>Repository</strong><p class="field"><span class="label">Repositorystatus</span><span id="repositoryState"></span></p><p class="field"><span class="label">Werkruimtestatus</span><span id="workspaceState"></span></p></div>
<div class="card"><strong>Diagnose</strong><p id="diag"></p></div>
</main>
<footer class="footer"><span class="label">Engineering Platform-versie</span><span id="platformVersion">Laden…</span> · <span class="label">Git-commit</span><code>$BUILD_COMMIT</code></footer>
<script>
const $=id=>document.getElementById(id),REFRESH_SECONDS=5,DASHBOARD_BUILD="$BUILD_COMMIT",DASHBOARD_BUILD_KEY="djconnect-engineering-dashboard-build",
formatTime=new Intl.DateTimeFormat("nl-NL",{timeZone:"Europe/Amsterdam",dateStyle:"full",timeStyle:"medium"}),
fallback={watcher_state:"REMOTE_ENGINEERING_DEGRADED",current_phase:"status unavailable",current_action:"Refresh the dashboard after the Engineering Platform publishes status.",queue_depth:0,repository_state:"UNKNOWN",workspace_state:"UNKNOWN",diagnostic:"The status request could not be completed."};
let currentLogRun,lastLogRun,lastRefresh,nextRefresh,promptStartedAt,latestStatus;
const humanLabels={ENGINEERING_RUN_ACTIVE:"Engineering actief",EXECUTE_AGENT:"Uitvoering",invoke_agent:"Engineering uitvoeren",MERGED_RECONCILED:"Samengevoegd en afgestemd",WORKSPACE_READY:"Werkruimte gereed"};
function humanize(){for(const id of ["watcher","phase","action","repositoryState","workspaceState"]){const element=$(id);element.textContent=humanLabels[element.textContent]||element.textContent}}
function tone(x){const phase=x.current_phase||"",watcher=x.watcher_state||"";if(["BLOCKED","FAILED"].includes(phase)||["JOB_BLOCKED","JOB_FAILED"].includes(watcher))return "red";if(phase==="COMPLETE"||watcher==="JOB_COMPLETED")return "green";if(phase==="WAIT_FOR_TERMINAL_EVIDENCE"||watcher==="WAITING_FOR_REPOSITORY")return "yellow";if(["INITIALIZE","EXECUTE_AGENT","REPAIR_AGENT","FINALIZE_AGENT","REPOSITORY_CLEANUP"].includes(phase)||["RUNNER_STARTING","JOB_CLAIMED"].includes(watcher))return "orange";return "grey"}
function finalStatus(phase){if(phase==="COMPLETE")return ["green","Voltooid"];if(phase==="BLOCKED")return ["yellow","Geblokkeerd"];if(phase==="FAILED")return ["red","Mislukt"];return ["grey","Status onbekend"]}
function executionRange(x){const characters=Number(x.prompt_characters)||0;if(characters<=2000)return [6,10];if(characters<=6000)return [10,18];if(characters<=12000)return [16,26];return [24,38]}
function pluralMinutes(value){return value===1?"minuut":"minuten"}
function estimate(x){const phase=x.current_phase||"";if(phase==="INITIALIZE")return {summary:"Voorbereiding: minder dan 1 minuut",context:""};if(["EXECUTE_AGENT","REPAIR_AGENT"].includes(phase)){const [minimum,maximum]=executionRange(x);if(!promptStartedAt)return {summary:"Indicatieve totale duur: "+minimum+"–"+maximum+" minuten",context:"Gebaseerd op promptomvang en fase. Live Codex-voortgang is niet beschikbaar."};const elapsed=Math.max(0,Math.floor((Date.now()-promptStartedAt)/60000)),remainingMinimum=Math.max(1,minimum-elapsed),remainingMaximum=Math.max(remainingMinimum,maximum-elapsed);return {summary:"Indicatief resterend: "+remainingMinimum+"–"+remainingMaximum+" minuten",context:elapsed+" "+pluralMinutes(elapsed)+" verstreken · gebaseerd op promptomvang, fase en verstreken tijd. Geen live Codex-voortgang of tokenverbruik."}}if(phase==="FINALIZE_AGENT")return {summary:"Finalisatie in uitvoering",context:"De resterende tijd is pas betrouwbaar met live Codex-voortgang."};if(phase==="REPOSITORY_CLEANUP")return {summary:"Opschoning in uitvoering",context:"De resterende tijd hangt af van de lokale repository."};if(phase==="WAIT_FOR_TERMINAL_EVIDENCE")return {summary:"Wacht op externe verificatie",context:"Geen betrouwbare ETA."};if(phase==="COMPLETE")return {summary:"Voltooid",context:""};if(["BLOCKED","FAILED"].includes(phase))return {summary:"Gestopt; actie nodig",context:""};return {summary:"Nog niet beschikbaar",context:""}}
function renderEstimate(x){const value=estimate(x);$("executionEstimate").textContent=value.summary;$("executionEstimateMeta").textContent=value.context;$("executionEstimateMeta").hidden=!value.context}
function isActiveRun(x){return x.watcher_state==="ENGINEERING_RUN_ACTIVE"&&Boolean(x.run_id)}
function checkBuild(){fetch("/api/build",{cache:"no-store"}).then(x=>x.json()).then(x=>{if(x.build_commit===DASHBOARD_BUILD){sessionStorage.removeItem(DASHBOARD_BUILD_KEY);return}if(x.build_commit&&DASHBOARD_BUILD!=="onbekend"&&sessionStorage.getItem(DASHBOARD_BUILD_KEY)!==x.build_commit){sessionStorage.setItem(DASHBOARD_BUILD_KEY,x.build_commit);location.reload()}}).catch(()=>{})}
function clock(){let now=Date.now();$("currentTime").textContent=formatTime.format(new Date(now));$("lastRefresh").textContent="Laatst ververst: "+(lastRefresh?formatTime.format(lastRefresh):"laden…");$("nextRefresh").textContent="Volgende verversing: "+(nextRefresh?Math.max(0,Math.ceil((nextRefresh-now)/1000))+" seconden":"laden…")}
function l(id,url,run,last){if(run===(last?lastLogRun:currentLogRun))return;if(last)lastLogRun=run;else currentLogRun=run;$(id).textContent="Loading diagnostic…";fetch(url).then(x=>x.text()).then(x=>$(id).textContent=x).catch(()=>$(id).textContent="Codex CLI diagnostic is unavailable.")}
function usage(){const labels={input_tokens:"Invoertokens",cached_input_tokens:"Gecachete invoertokens",output_tokens:"Uitvoertokens",total_tokens:"Totaal tokens",cost:"Kosten",remaining:"Resterend beschikbaar",plan_remaining:"Resterend in plan",usage:"Gebruik"};fetch("/api/usage").then(x=>x.json()).then(x=>{let entries=Object.entries(x);$("usage").hidden=!entries.length;$("usageDetails").textContent=entries.map(([key,value])=>(labels[key]||key.replaceAll("_"," "))+": "+value).join("\\n")}).catch(()=>$("usage").hidden=true)}
function processMetrics(active){$("processMetrics").hidden=!active;if(!active)return;fetch("/api/process-metrics").then(x=>x.json()).then(x=>{$("codexCpu").textContent=Number(x.cpu_percent||0).toLocaleString("nl-NL",{maximumFractionDigits:1})+"%";$("codexProcesses").textContent=x.process_count??0;$("codexGpu").textContent=x.gpu_status||"Niet beschikbaar"}).catch(()=>{$("codexCpu").textContent="Niet beschikbaar";$("codexProcesses").textContent="Niet beschikbaar";$("codexGpu").textContent="Niet beschikbaar"})}
function commits(){fetch("/api/commits").then(x=>x.json()).then(x=>{let entries=Object.entries(x);$("commits").hidden=!entries.length;$("completionCommits").textContent=entries.map(([label,sha])=>label+": "+sha).join("\\n")}).catch(()=>$("commits").hidden=true)}
function lastCommits(){fetch("/api/commits/last-executed").then(x=>x.json()).then(x=>{let entries=Object.entries(x);$("lastCommits").hidden=!entries.length;$("lastCommitDetails").textContent=entries.map(([label,sha])=>label+": "+sha).join("\\n")}).catch(()=>$("lastCommits").hidden=true)}
function promptStarted(){fetch("/api/prompt-started").then(x=>x.json()).then(x=>{promptStartedAt=x.started_at?Date.parse(x.started_at):undefined;$("promptStarted").textContent=promptStartedAt?formatTime.format(new Date(promptStartedAt)):"Niet beschikbaar";if(latestStatus)renderEstimate(latestStatus)}).catch(()=>{$("promptStarted").textContent="Niet beschikbaar";promptStartedAt=undefined;if(latestStatus)renderEstimate(latestStatus)})}
let lastExecutedRun,reportLoaded=false,reportRequest;function report(){if(!lastExecutedRun)return Promise.resolve();if(reportLoaded)return reportRequest;reportLoaded=true;return reportRequest=fetch("/api/report/last-executed?run_id="+encodeURIComponent(lastExecutedRun)).then(x=>x.text()).then(x=>{if(!x){$("report").hidden=true;return}$("reportContent").textContent=x}).catch(()=>{$("reportContent").textContent="Engineeringrapport is niet beschikbaar."})}
function fallbackCopy(value){const area=document.createElement("textarea");area.value=value;area.setAttribute("readonly","");area.style.cssText="position:fixed;top:0;left:0;opacity:0";document.body.append(area);area.focus();area.select();area.setSelectionRange(0,area.value.length);const copied=document.execCommand("copy");area.remove();if(!copied)throw Error("copy unavailable")}
function copyText(value){return navigator.clipboard&&window.isSecureContext?navigator.clipboard.writeText(value).catch(()=>fallbackCopy(value)):Promise.resolve().then(()=>fallbackCopy(value))}
function copyReport(){report().then(()=>copyText($("reportContent").textContent)).then(()=>{$("copyReport").textContent="Gekopieerd";setTimeout(()=>{$("copyReport").textContent="⧉ Kopieer"},1500)}).catch(()=>{$("copyReport").textContent="Kopiëren mislukt"})}
function r(x){lastRefresh=new Date();nextRefresh=Date.now()+REFRESH_SECONDS*1000;clock();x=x&&typeof x==="object"?x:fallback;latestStatus=x;let active=isActiveRun(x),statusTone=tone(x),indicator=$("indicator"),previous=x.last_executed_run||null,lastStatus=finalStatus(x.last_executed_phase);if(previous!==lastExecutedRun){lastExecutedRun=previous;reportLoaded=false;reportRequest=undefined;$("report").open=false;$("reportContent").textContent="Open dit blok om het rapport te laden."}$("promptRuns").hidden=!active&&!previous;$("lastExecution").hidden=!previous;$("report").hidden=!previous;$("executionContext").hidden=!x.execution_mode;$("executionMode").textContent=x.execution_mode||"Niet beschikbaar";$("targetRepository").textContent=x.target_repository||"Niet beschikbaar";$("checkoutPath").textContent=x.checkout_path||"Niet beschikbaar";$("activeBranch").textContent=x.active_branch||"Niet beschikbaar";indicator.className="indicator indicator--"+statusTone+(active?" indicator--running":"");indicator.setAttribute("aria-label","Promptstatus: "+statusTone);$("lastIndicator").className="indicator indicator--small indicator--"+lastStatus[0];$("lastFinalStatus").textContent=lastStatus[1];$("watcher").textContent=x.watcher_state||fallback.watcher_state;$("phase").textContent=x.current_phase||"idle";$("action").textContent=x.current_action||"Geen actieve actie";renderEstimate(x);processMetrics(active);$("current").hidden=!active;$("currentPrompt").textContent=x.prompt_title||"Niet beschikbaar";$("currentFile").textContent=x.submitted_filename||"Niet beschikbaar";if(active)l("currentLog","/api/log/current",x.run_id||null,false);$("lastPrompt").textContent=x.last_executed_title||"Nog geen prompt uitgevoerd";$("lastFile").textContent=x.last_executed_filename||"Niet beschikbaar";l("lastLog","/api/log/last",previous,true);$("runId").textContent=x.run_id||"geen";$("queue").textContent=x.queue_depth??0;$("implementation").textContent=x.implementation_pr||"geen";$("finalization").textContent=x.finalization_pr||"geen";$("repositoryState").textContent=x.repository_state||"ONBEKEND";$("workspaceState").textContent=x.workspace_state||"ONBEKEND";$("diag").textContent=x.diagnostic||"Geen diagnose";$("platformVersion").textContent=x.platform_version||"Niet beschikbaar";usage();commits();lastCommits()}
function refresh(){fetch("/api/status").then(x=>{if(!x.ok)throw Error("status unavailable");return x.json()}).then(r).catch(()=>r(fallback))}
let e=new EventSource("/api/events");e.addEventListener("status",x=>{try{r(JSON.parse(x.data));humanize()}catch{r(fallback);humanize()}});$("report").addEventListener("toggle",()=>{$("report").open&&report()});$("copyReport").addEventListener("click",copyReport);setInterval(()=>{clock();humanize();if(nextRefresh&&Date.now()>=nextRefresh)refresh()},250);clock();refresh();checkBuild();setInterval(checkBuild,5000);setInterval(promptStarted,5000);promptStarted()
</script>"""
    return page.replace("$TITLE", escape(title)).replace("$BUILD_COMMIT", escape(build_commit)).encode()


def handler(root: Path):
    title = PlatformConfiguration.load(root).workspace.dashboard_title
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, content: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            if request.path == "/api/report/last-executed":
                run_id = parse_qs(request.query).get("run_id", [None])[0]
                return self._send(
                    _report_for_run(root, run_id), "text/markdown; charset=utf-8"
                )
            if self.path == "/api/status":
                return self._send(_status(root), "application/json; charset=utf-8")
            if self.path == "/api/build":
                return self._send(
                    json.dumps({"build_commit": _build_commit(root)}).encode(),
                    "application/json; charset=utf-8",
                )
            if self.path == "/api/health":
                return self._send(b'{"health":"ok"}', "application/json; charset=utf-8")
            if self.path == "/api/process-metrics":
                return self._send(_codex_process_metrics(), "application/json; charset=utf-8")
            if self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for _ in range(60):
                        self.wfile.write(b"event: status\ndata: " + _sse_status(root) + b"\n\n")
                        self.wfile.flush()
                        time.sleep(5)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/api/report/latest":
                try:
                    reports = sorted((root / ".djconnect" / "reports").glob("*.md"))
                    content = (
                        reports[-1].read_bytes() if reports else b"No local report is available."
                    )
                except OSError:
                    content = b"Report is unavailable."
                return self._send(content, "text/markdown; charset=utf-8")
            if self.path == "/api/log/latest":
                return self._send(_latest_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/log/current":
                return self._send(_current_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/log/last":
                return self._send(_last_executed_codex_log(root), "text/plain; charset=utf-8")
            if self.path == "/api/usage":
                return self._send(_codex_usage(root), "application/json; charset=utf-8")
            if self.path == "/api/commits":
                return self._send(_completion_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/commits/last-executed":
                return self._send(_last_executed_commits(root), "application/json; charset=utf-8")
            if self.path == "/api/prompt-started":
                return self._send(_prompt_started(root), "application/json; charset=utf-8")
            if self.path == "/":
                return self._send(_dashboard_html(title, _build_commit(root)), "text/html; charset=utf-8")
            self.send_error(404)

        def log_message(self, *_: object) -> None:
            pass

    return DashboardHandler


def binding_addresses(provider: TailscaleProvider | None = None) -> tuple[str, ...]:
    """Bind only loopback and the explicit local Tailscale address.

    The dashboard deliberately never binds a wildcard, LAN, public or Funnel
    address.  Tailnet policy remains the access boundary; this code changes no
    Tailscale configuration.
    """
    tailscale_address = (provider or TailscaleProvider()).ipv4_address()
    return (LOOPBACK_ADDRESS, *(() if tailscale_address is None else (tailscale_address,)))


def create_servers(
    root: Path, port: int = 8765, provider: TailscaleProvider | None = None
) -> tuple[DashboardHTTPServer, ...]:
    """Create the exact private listeners for the dashboard."""
    request_handler = handler(root)
    return tuple(
        DashboardHTTPServer((address, port), request_handler)
        for address in binding_addresses(provider)
    )


def run(root: Path, port: int = 8765, provider: TailscaleProvider | None = None) -> None:
    """Serve locally and, when present, over the authenticated Tailnet only."""
    servers = create_servers(root, port, provider)
    for server in servers[1:]:
        Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()


def launch_agent(repo: Path) -> Path:
    """Render the only owned per-user LaunchAgent; no network policy changes."""
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logs = repo / ".djconnect" / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    arguments = "".join(
        f"<string>{value}</string>"
        for value in (
            sys.executable,
            "-m",
            "tools.engineering.dashboard",
            "run",
            "--repo",
            str(repo),
        )
    )
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{arguments}</array><key>WorkingDirectory</key><string>{repo}</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer><key>StandardOutPath</key><string>{logs / "dashboard.out.log"}</string><key>StandardErrorPath</key><string>{logs / "dashboard.err.log"}</string></dict></plist>',
        encoding="utf-8",
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "install", "uninstall", "status", "doctor"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.command == "run":
        run(repo, port=args.port)
        return 0
    if args.command == "install":
        agent = launch_agent(repo)
        LaunchdProvider().install(LABEL, agent)
        return 0
    if args.command == "uninstall":
        LaunchdProvider().uninstall(agent)
        agent.unlink(missing_ok=True)
        return 0
    health = (repo / ".djconnect" / "status" / "status.json").is_file()
    remote_provider = TailscaleProvider()
    remote = remote_provider.status()
    tailscale_address = remote_provider.ipv4_address()
    state = "READY" if health and agent.is_file() and tailscale_address else "DEGRADED"
    action = (
        "Run Engineering Platform to publish a status update."
        if not health
        else "Connect Tailscale before using private iPhone dashboard access."
        if not tailscale_address
        else "Open the private dashboard through the local Tailscale address."
    )
    print(
        f"REMOTE_ENGINEERING_{state}\nprivate_remote_access={remote.detail}\n"
        f"tailscale_dashboard_address={tailscale_address or 'unavailable'}\n"
        f"Action: {action} No network configuration was changed."
    )
    return 0 if state == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
