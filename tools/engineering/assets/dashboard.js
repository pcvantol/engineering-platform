import { createDashboardStatusStore } from "./dashboard_status_store.mjs";

const $ = (id) => document.getElementById(id),
  DASHBOARD_BUILD = window.DJCONNECT_DASHBOARD_BUILD || "",
  DASHBOARD_BUILD_KEY = "djconnect-engineering-dashboard-build",
  formatTime = new Intl.DateTimeFormat("nl-NL", {
    timeZone: "Europe/Amsterdam",
    dateStyle: "full",
    timeStyle: "medium",
  }),
  fallback = {
    watcher_state: "REMOTE_ENGINEERING_DEGRADED",
    current_phase: "status niet beschikbaar",
    current_action:
      "Ververs het dashboard nadat het Engineering Platform een statusupdate heeft gepubliceerd.",
    queue_depth: 0,
    repository_state: "UNKNOWN",
    workspace_state: "UNKNOWN",
    diagnostic: "Het statusverzoek kon niet worden voltooid.",
  };
let currentLogRun, lastLogRun, lastRefresh, promptStartedAt, latestStatus;
function sanitizeFreeText(value, maximumLength, multiline = false) {
  const normalized = String(value ?? "")
    .normalize("NFC")
    .replace(/\r\n?/g, "\n")
    .replace(
      /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g,
      "",
    );
  return (multiline ? normalized : normalized.replace(/\n+/g, " ")).slice(
    0,
    maximumLength,
  );
}
function sanitizeDeclaredFreeInput(element) {
  const maximumLength =
      Number(element.maxLength) > 0 ? Number(element.maxLength) : 160,
    clean = sanitizeFreeText(
      element.value,
      maximumLength,
      element.dataset.sanitize === "multiline",
    );
  if (element.value !== clean) element.value = clean;
  return clean;
}
document
  .querySelectorAll("input[data-sanitize],textarea[data-sanitize]")
  .forEach((element) =>
    element.addEventListener("input", () => sanitizeDeclaredFreeInput(element)),
  );
const humanLabels = {
  ENGINEERING_RUN_ACTIVE: "Engineering actief",
  WATCHER_IDLE: "Watcher wacht",
  REMOTE_ENGINEERING_DEGRADED: "Engineeringstatus beperkt beschikbaar",
  JOB_CLAIMED: "Opdracht opgepakt",
  RUNNER_STARTING: "Uitvoering wordt gestart",
  REPORT_PUBLISHING: "Rapport wordt gepubliceerd",
  JOB_COMPLETED: "Opdracht voltooid",
  JOB_BLOCKED: "Opdracht geblokkeerd",
  JOB_FAILED: "Opdracht mislukt",
  WAITING_FOR_REPOSITORY: "Wacht op repository",
  WAITING_FOR_PREDECESSOR: "Wacht op voorafgaande prompt",
  INITIALIZE: "Voorbereiding",
  EXECUTE_AGENT: "Uitvoering",
  REPAIR_AGENT: "Herstel",
  FINALIZE_AGENT: "Finalisatie",
  REPOSITORY_CLEANUP: "Opschoning repository",
  COMPLETE: "Voltooid",
  BLOCKED: "Geblokkeerd",
  FAILED: "Mislukt",
  invoke_agent: "Engineering uitvoeren",
  repository_reconciled: "Repository afgestemd",
  MERGED_RECONCILED: "Samengevoegd en afgestemd",
  WORKSPACE_READY: "Werkruimte gereed",
  ACTIVE: "Actief",
  UNKNOWN: "Onbekend",
  "status unavailable": "status niet beschikbaar",
};
const dutchDiagnostics = {
  "Engineering report was not available for delivery.":
    "Engineeringrapport kon niet worden afgeleverd.",
  "Runner ended without a safe terminal report.":
    "De runner stopte zonder een veilig eindrapport.",
  "An existing engineering transaction remains active.":
    "Een bestaande engineeringuitvoering is nog actief.",
  "Duplicate job digest remains recorded.":
    "Een dubbele opdracht is al geregistreerd.",
  "Another watcher owns the local inbox lock.":
    "Een andere watcher beheert de lokale Inbox-vergrendeling.",
  "No local engineering status has been published yet.":
    "Er is nog geen lokale engineeringstatus gepubliceerd.",
  "The status request could not be completed.":
    "Het statusverzoek kon niet worden voltooid.",
};
function translate(value) {
  return humanLabels[value] || dutchDiagnostics[value] || value;
}
function humanize() {
  for (const id of [
    "watcher",
    "phase",
    "action",
    "repositoryState",
    "workspaceState",
    "diag",
  ]) {
    const element = $(id);
    element.textContent = translate(element.textContent);
  }
}
function tone(x) {
  const phase = x.current_phase || "",
    watcher = x.watcher_state || "";
  if (
    ["BLOCKED", "FAILED"].includes(phase) ||
    ["JOB_BLOCKED", "JOB_FAILED"].includes(watcher)
  )
    return "red";
  if (phase === "COMPLETE" || watcher === "JOB_COMPLETED") return "green";
  if (
    phase === "WAIT_FOR_TERMINAL_EVIDENCE" ||
    ["WAITING_FOR_REPOSITORY", "WAITING_FOR_PREDECESSOR"].includes(watcher)
  )
    return "yellow";
  if (
    [
      "INITIALIZE",
      "EXECUTE_AGENT",
      "REPAIR_AGENT",
      "FINALIZE_AGENT",
      "REPOSITORY_CLEANUP",
    ].includes(phase) ||
    ["RUNNER_STARTING", "JOB_CLAIMED"].includes(watcher)
  )
    return "orange";
  return "grey";
}
function finalStatus(phase) {
  if (phase === "COMPLETE") return ["green", "Voltooid"];
  if (phase === "BLOCKED") return ["yellow", "Geblokkeerd"];
  if (phase === "FAILED") return ["red", "Mislukt"];
  return ["grey", "Status onbekend"];
}
function executionRange(x) {
  const characters = Number(x.prompt_characters) || 0;
  if (characters <= 2e3) return [6, 10];
  if (characters <= 6e3) return [10, 18];
  if (characters <= 12e3) return [16, 26];
  return [24, 38];
}
function pluralMinutes(value) {
  return value === 1 ? "minuut" : "minuten";
}
function estimate(x) {
  const phase = x.current_phase || "";
  if (phase === "INITIALIZE")
    return { summary: "Voorbereiding: minder dan 1 minuut", context: "" };
  if (["EXECUTE_AGENT", "REPAIR_AGENT"].includes(phase)) {
    const [minimum, maximum] = executionRange(x);
    if (!promptStartedAt)
      return {
        summary:
          "Indicatieve totale duur: " + minimum + "–" + maximum + " minuten",
        context:
          "Gebaseerd op promptomvang en fase. Live Codex-voortgang is niet beschikbaar.",
      };
    const elapsed = Math.max(
        0,
        Math.floor((Date.now() - promptStartedAt) / 6e4),
      ),
      remainingMinimum = Math.max(1, minimum - elapsed),
      remainingMaximum = Math.max(remainingMinimum, maximum - elapsed);
    return {
      summary:
        "Indicatief resterend: " +
        remainingMinimum +
        "–" +
        remainingMaximum +
        " minuten",
      context:
        elapsed +
        " " +
        pluralMinutes(elapsed) +
        " verstreken." +
        String.fromCharCode(10) +
        "gebaseerd op promptomvang, fase en verstreken tijd. Geen live Codex-voortgang of tokenverbruik.",
    };
  }
  if (phase === "FINALIZE_AGENT")
    return {
      summary: "Finalisatie in uitvoering",
      context:
        "De resterende tijd is pas betrouwbaar met live Codex-voortgang.",
    };
  if (phase === "REPOSITORY_CLEANUP")
    return {
      summary: "Opschoning in uitvoering",
      context: "De resterende tijd hangt af van de lokale repository.",
    };
  if (phase === "WAIT_FOR_TERMINAL_EVIDENCE")
    return {
      summary: "Wacht op externe verificatie",
      context: "Geen betrouwbare ETA.",
    };
  if (phase === "COMPLETE") return { summary: "Voltooid", context: "" };
  if (["BLOCKED", "FAILED"].includes(phase))
    return { summary: "Gestopt; actie nodig", context: "" };
  return { summary: "Nog niet beschikbaar", context: "" };
}
function renderEstimate(x) {
  const value = estimate(x);
  $("executionEstimate").textContent = value.summary;
  $("executionEstimateMeta").textContent = value.context;
  $("executionEstimateMeta").hidden = !value.context;
}
function isActiveRun(x) {
  return x.watcher_state === "ENGINEERING_RUN_ACTIVE" && Boolean(x.run_id);
}
function isTerminalBlockedRun(x) {
  return String(x?.last_executed_phase || "").toUpperCase() === "BLOCKED";
}
function checkBuild(build) {
  if (build === DASHBOARD_BUILD) {
    sessionStorage.removeItem(DASHBOARD_BUILD_KEY);
    return;
  }
  if (
    build &&
    DASHBOARD_BUILD !== "onbekend" &&
    sessionStorage.getItem(DASHBOARD_BUILD_KEY) !== build
  ) {
    sessionStorage.setItem(DASHBOARD_BUILD_KEY, build);
    location.reload();
  }
}
function clock() {
  $("lastRefresh").textContent =
    "Laatst bijgewerkt: " +
    (lastRefresh ? formatTime.format(lastRefresh) : "laden…");
}
function l(id, url, run, last, container) {
  if (run === (last ? lastLogRun : currentLogRun)) return;
  if (last) lastLogRun = run;
  else currentLogRun = run;
  $(id).textContent = "Diagnose laden…";
  fetch(url)
    .then((x) => x.text())
    .then((x) => {
      const available =
        Boolean(x) &&
        !x.startsWith("No Codex CLI diagnostic is available") &&
        !x.startsWith("Geen Codex CLI-diagnose beschikbaar");
      $(container).hidden = false;
      $(id).textContent = available
        ? x
        : last
          ? "Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze uitgevoerde prompt."
          : "Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze actieve prompt.";
    })
    .catch(() => {
      $(container).hidden = false;
      $(id).textContent = last
        ? "Er is geen AI-uitvoeringsdiagnose beschikbaar voor deze uitgevoerde prompt."
        : "AI-uitvoeringsdiagnose is niet beschikbaar voor deze actieve prompt.";
    });
}
function usage(x) {
  const labels = {
    input_tokens: "Invoertokens",
    cached_input_tokens: "Gecachete invoertokens",
    output_tokens: "Uitvoertokens",
    total_tokens: "Totaal tokens",
    cost: "Kosten",
    remaining: "Resterend beschikbaar",
    plan_remaining: "Resterend in plan",
    usage: "Gebruik",
  };
  let entries = Object.entries(x || {});
  $("usage").hidden = !entries.length;
  $("usageDetails").textContent = entries
    .map(
      ([key, value]) =>
        (labels[key] || key.replaceAll("_", " ")) + ": " + value,
    )
    .join(String.fromCharCode(10));
}
function rateLimits(x) {
  const windows = Array.isArray(x?.windows) ? x.windows : [],
    credits = Number.isInteger(x?.reset_credits) ? x.reset_credits : null,
    provider =
      typeof x?.provider === "string" ? x.provider : "Niet beschikbaar",
    version =
      typeof x?.provider_version === "string"
        ? x.provider_version
        : "versie niet beschikbaar",
    button = $("rateLimitReset");
  $("rateLimits").hidden =
    !windows.length && credits === null && provider === "Niet beschikbaar";
  $("rateLimitProvider").textContent = provider + " · " + version;
  let lines = windows.map((window) => {
    const remaining = Math.max(0, 100 - Number(window.used_percent || 0)),
      reset = Number(window.resets_at);
    return (
      window.label +
      ": " +
      remaining +
      "% beschikbaar · reset " +
      (Number.isFinite(reset)
        ? formatTime.format(new Date(reset * 1e3))
        : "onbekend")
    );
  });
  if (credits !== null) lines.push("Beschikbare resets: " + credits);
  $("rateLimitDetails").textContent = lines.join(String.fromCharCode(10));
  button.hidden = !(credits > 0);
  button.disabled = false;
}
function consumeRateLimitReset() {
  const button = $("rateLimitReset"),
    status = $("rateLimitResetStatus");
  if (button.hidden || button.disabled) return;
  confirmDashboardAction(
    "Gebruik reset",
    "Deze actie verbruikt één beschikbare resetcredit.",
    "Gebruik reset",
    "#51d88a",
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    status.textContent = "Reset gebruiken…";
    fetch("/api/rate-limit-reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => ({
        ok: response.ok,
        body: await response.json(),
      }))
      .then((result) => {
        if (!result.ok)
          throw Error(result.body.error || "Reset kon niet worden uitgevoerd.");
        const messages = {
          reset: "Reset gebruikt. De gebruikslimieten zijn bijgewerkt.",
          nothingToReset: "Er is op dit moment niets om te resetten.",
          noCredit: "Er is geen resetcredit beschikbaar.",
          alreadyRedeemed: "Deze resetcredit is al gebruikt.",
        };
        status.textContent = messages[result.body.outcome] || "Reset verwerkt.";
        if (result.body.rate_limits) rateLimits(result.body.rate_limits);
      })
      .catch((error) => {
        status.textContent = error.message;
      })
      .finally(() => {
        button.disabled = false;
      });
  });
}
function lastUsage(x) {
  const labels = {
    input_tokens: "Invoertokens",
    cached_input_tokens: "Gecachete invoertokens",
    output_tokens: "Uitvoertokens",
    total_tokens: "Totaal tokens",
    cost: "Kosten",
    remaining: "Resterend beschikbaar",
    plan_remaining: "Resterend in plan",
    usage: "Gebruik",
  };
  let entries = Object.entries(x || {});
  $("lastUsage").hidden = !entries.length;
  $("lastUsageDetails").textContent = entries
    .map(
      ([key, value]) =>
        (labels[key] || key.replaceAll("_", " ")) + ": " + value,
    )
    .join(String.fromCharCode(10));
}
function lastRuntimeMetadata(metadata) {
  const fields = [
    ["runtime_provider", "lastRuntimeProvider", "lastRuntimeProviderValue"],
    ["model", "lastModel", "lastModelValue"],
    ["reasoning_profile", "lastReasoningProfile", "lastReasoningProfileValue"],
    [
      "configuration_profile",
      "lastConfigurationProfile",
      "lastConfigurationProfileValue",
    ],
    ["codex_cli_version", "lastCodexCliVersion", "lastCodexCliVersionValue"],
  ];
  for (const [key, fieldId, valueId] of fields) {
    const value =
      metadata &&
      typeof metadata[key] === "string" &&
      metadata[key] !== "not reported"
        ? metadata[key]
        : "";
    $(fieldId).hidden = !value;
    $(valueId).textContent = value;
  }
}
function processMetrics(active, x) {
  $("processMetrics").hidden = !active;
  if (!active) return;
  $("codexCpu").textContent =
    Number(x?.cpu_percent || 0).toLocaleString("nl-NL", {
      maximumFractionDigits: 1,
    }) + "%";
  $("codexProcesses").textContent = x?.process_count ?? 0;
  $("codexGpu").textContent = x?.gpu_status || "Niet beschikbaar";
}
function commits(x) {
  let entries = Object.entries(x || {});
  $("commits").hidden = !entries.length;
  $("completionCommits").textContent = entries
    .map(([label, sha]) => label + ": " + sha)
    .join(String.fromCharCode(10));
}
function lastCommits(x) {
  let entries = Object.entries(x || {});
  $("lastCommits").hidden = !entries.length;
  $("lastCommitDetails").textContent = entries
    .map(([label, sha]) => label + ": " + sha)
    .join(String.fromCharCode(10));
}
function renderLegacyLastExecutionTime(x) {
  let seconds = Number(x?.seconds),
    field = $("lastExecutionTime"),
    value = $("lastExecutionTimeValue");
  if (!field) {
    field = document.createElement("div");
    value = document.createElement("span");
    const label = document.createElement("span");
    field.className = "field";
    field.id = "lastExecutionTime";
    value.id = "lastExecutionTimeValue";
    label.className = "label";
    label.textContent = "Codex CLI-uitvoeringstijd";
    field.append(label, value);
    $("lastFile").closest(".field").insertAdjacentElement("afterend", field);
  }
  field.hidden = !Number.isFinite(seconds) || seconds < 0;
  if (field.hidden) return;
  const hours = Math.floor(seconds / 3600),
    minutes = Math.floor((seconds % 3600) / 60),
    remaining = Math.round(seconds % 60);
  value.textContent =
    (hours ? hours + " u " : "") +
    (minutes ? minutes + " min " : "") +
    remaining +
    " sec";
}
function reviewerAgents(items) {
  const agents = Array.isArray(items) ? items : [],
    card = $("reviewerAgents"),
    list = $("reviewerAgentList");
  card.hidden = !agents.length;
  list.replaceChildren();
  for (const agent of agents) {
    if (!agent || typeof agent !== "object") continue;
    const row = document.createElement("article"),
      name = document.createElement("p"),
      capability = document.createElement("p"),
      reason = document.createElement("p"),
      recommendations = Number(agent.accepted_recommendations) || 0;
    row.className = "reviewer-agent";
    name.className = "reviewer-agent__name";
    capability.className = reason.className = "reviewer-agent__meta";
    name.textContent = String(
      agent.reviewer || "Specialistische review",
    ).replaceAll("_", " ");
    capability.textContent =
      "Capaciteit: " +
      String(agent.capability || "engineering") +
      " · " +
      String(agent.status || "Uitgevoerd") +
      " · Gebruikte aanbevelingen: " +
      recommendations;
    reason.textContent =
      "Geselecteerd voor: " +
      String(agent.selected_because || "Niet vastgelegd.");
    row.append(name, capability, reason);
    list.append(row);
  }
}
function queueItems(x, queueDepth) {
  const items = (Array.isArray(x) ? x : [])
      .filter((item) => item && typeof item === "object")
      .sort((left, right) => {
        const first = Date.parse(left.modified_at || ""),
          second = Date.parse(right.modified_at || "");
        if (
          Number.isFinite(first) &&
          Number.isFinite(second) &&
          first !== second
        )
          return first - second;
        if (Number.isFinite(first) !== Number.isFinite(second))
          return Number.isFinite(first) ? -1 : 1;
        return String(left.filename || "").localeCompare(
          String(right.filename || ""),
          "nl",
        );
      }),
    container = $("queueList"),
    depth =
      Number.isInteger(queueDepth) && queueDepth >= 0
        ? queueDepth
        : items.length;
  $("queueSummary").textContent =
    depth === 0
      ? "0 prompts in de wachtrij."
      : depth +
        " " +
        (depth === 1 ? "prompt" : "prompts") +
        " in de wachtrij." +
        (depth > items.length
          ? " De eerste " + items.length + " worden getoond."
          : "");
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "queue-empty";
    empty.textContent = "Geen Inbox-prompts wachten op uitvoering.";
    container.append(empty);
    return;
  }
  items.forEach((item, index) => {
    const row = document.createElement("li"),
      number = document.createElement("span"),
      body = document.createElement("div"),
      title = document.createElement("span"),
      meta = document.createElement("div"),
      modified = Date.parse(item.modified_at || ""),
      filename = item.filename || "Bestandsnaam niet beschikbaar";
    row.className = "queue-item";
    row.setAttribute(
      "aria-label",
      "Positie " + (index + 1) + ": " + (item.title || filename),
    );
    number.className = "queue-item__number";
    number.textContent = String(index + 1);
    title.className = "queue-item__title";
    meta.className = "queue-item__meta";
    title.textContent = item.title || filename;
    meta.textContent =
      "Bestandsnaam: " +
      filename +
      " · gewijzigd: " +
      (Number.isFinite(modified)
        ? formatTime.format(new Date(modified))
        : "Tijdstip niet beschikbaar");
    body.append(title, meta);
    row.append(number, body);
    container.append(row);
  });
}
function promptStarted(x) {
  promptStartedAt = x?.started_at ? Date.parse(x.started_at) : undefined;
  $("promptStarted").textContent = promptStartedAt
    ? formatTime.format(new Date(promptStartedAt))
    : "Niet beschikbaar";
  if (latestStatus) renderEstimate(latestStatus);
}
let lastExecutedRun,
  reportLoaded = false,
  reportRequest,
  analysisLoaded = false,
  analysisRequest;
function renderMarkdownDocument(target, value) {
  target.replaceChildren();
  renderMarkdownAnswer(target, value);
}
function lastTargetEvidence(value) {
  const labels = [
    ["Execution Host", "Execution Host"],
    ["Target Repository", "Target repository"],
    ["Target Commit", "Target commit"],
  ];
  const details = labels
    .map(([reportLabel, displayLabel]) => {
      const match = String(value || "").match(
        new RegExp("^- " + reportLabel + ": `([^`\\n]+)`$", "m"),
      );
      return match ? displayLabel + ": " + match[1] : "";
    })
    .filter(Boolean);
  const changed = (String(value || "").match(/^- Changed file: `/gm) || []).length;
  if (changed) details.push("Evidence Bundle: " + changed + " gewijzigde bestanden");
  let field = $("lastTargetEvidence");
  if (!field) {
    field = document.createElement("div");
    field.className = "field";
    field.id = "lastTargetEvidence";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = "Uitvoeringsbewijs";
    const output = document.createElement("pre");
    output.id = "lastTargetEvidenceValue";
    field.append(label, output);
    $("lastFile").closest(".field").insertAdjacentElement("afterend", field);
  }
  field.hidden = !details.length;
  $("lastTargetEvidenceValue").textContent = details.join(String.fromCharCode(10));
}
function report() {
  if (!lastExecutedRun) return Promise.resolve();
  if (reportLoaded) return reportRequest;
  reportLoaded = true;
  return (reportRequest = fetch(
    "/api/report/last-executed?run_id=" + encodeURIComponent(lastExecutedRun),
  )
    .then((x) => x.text())
    .then((x) => {
      if (!x) {
        $("report").hidden = true;
        return;
      }
      renderMarkdownDocument($("reportContent"), x);
      lastTargetEvidence(x);
    })
    .catch(() => {
      $("reportContent").textContent =
        "Engineeringrapport is niet beschikbaar.";
    }));
}
function analysis() {
  if (!lastExecutedRun) return Promise.resolve();
  if (analysisLoaded) return analysisRequest;
  analysisLoaded = true;
  return (analysisRequest = fetch(
    "/api/report-analysis/last-executed?run_id=" +
      encodeURIComponent(lastExecutedRun),
  )
    .then((x) => x.text())
    .then((x) => {
      if (!x) {
        $("reportAnalysis").hidden = true;
        return;
      }
      renderMarkdownDocument($("reportAnalysisContent"), x);
    })
    .catch(() => {
      $("reportAnalysisContent").textContent =
        "Codex-analyse is niet beschikbaar.";
    }));
}
let componentLogsLoaded = false,
  componentLogEntries = { inbox: [], dashboard: [] };
function structuredLogEntries(text) {
  return String(text || "")
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line, index) => {
      try {
        const entry = JSON.parse(line);
        if (!entry || typeof entry !== "object" || Array.isArray(entry))
          throw Error("not an object");
        const known = new Set([
            "timestamp",
            "level",
            "event",
            "run_id",
            "component",
          ]),
          details = Object.entries(entry)
            .filter(([key]) => !known.has(key))
            .map(
              ([key, value]) =>
                key +
                ": " +
                (typeof value === "string" ? value : JSON.stringify(value)),
            )
            .join(" · ");
        return {
          line: index + 1,
          timestamp: String(entry.timestamp || ""),
          level: String(entry.level || "ONBEKEND").toUpperCase(),
          event: String(entry.event || "onbekend"),
          runId: entry.run_id == null ? "" : String(entry.run_id),
          details: details,
        };
      } catch {
        return {
          line: index + 1,
          timestamp: "",
          level: "ONGELDIGE JSON",
          event: "onleesbare logregel",
          runId: "",
          details: line,
        };
      }
    });
}
function logTimestamp(entry) {
  const value = Date.parse(entry.timestamp);
  return Number.isFinite(value) ? value : 0;
}
function logTimestampText(value) {
  const parsed = Date.parse(String(value || ""));
  if (!Number.isFinite(parsed)) return value ? String(value) : "—";
  const parts = new Intl.DateTimeFormat("nl-NL", {
    timeZone: "Europe/Amsterdam",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(parsed)).reduce((result, part) => {
    result[part.type] = part.value;
    return result;
  }, {});
  return `${parts.day}-${parts.month}-${parts.year} ${parts.hour}:${parts.minute}:${parts.second}`;
}
function renderLegacyComponentLogs() {
  const needle = $("logFilter").value.trim().toLocaleLowerCase("nl-NL"),
    level = $("logLevelFilter").value,
    sort = $("logSort").value;
  for (const component of ["inbox", "dashboard"]) {
    const rows = componentLogEntries[component]
        .filter((entry) => !level || entry.level === level)
        .filter(
          (entry) =>
            !needle ||
            Object.values(entry)
              .join(" ")
              .toLocaleLowerCase("nl-NL")
              .includes(needle),
        )
        .sort((left, right) =>
          sort === "oldest"
            ? logTimestamp(left) - logTimestamp(right)
            : sort === "level"
              ? left.level.localeCompare(right.level, "nl")
              : sort === "event"
                ? left.event.localeCompare(right.event, "nl")
                : logTimestamp(right) - logTimestamp(left),
        ),
      body = $(component + "ComponentLog");
    body.replaceChildren();
    if (!rows.length) {
      const cell = document.createElement("td"),
        row = document.createElement("tr");
      cell.className = "log-empty";
      cell.colSpan = 6;
      cell.textContent = "Geen logregels voor deze selectie.";
      row.append(cell);
      body.append(row);
      continue;
    }
    for (const entry of rows) {
      const row = document.createElement("tr");
      for (const [name, value] of [
        ["log-line-number", entry.line],
        ["", logTimestampText(entry.timestamp)],
        [
          "log-level log-level--" + entry.level.toLocaleLowerCase("nl-NL"),
          entry.level,
        ],
        ["", entry.event],
        ["", entry.runId || "—"],
        ["", entry.details || "—"],
      ]) {
        const cell = document.createElement("td");
        cell.className = name;
        cell.textContent = value;
        row.append(cell);
      }
      body.append(row);
    }
  }
}
function loadComponentLogs() {
  if (componentLogsLoaded) return;
  $("loadComponentLogs").disabled = true;
  $("loadComponentLogs").textContent = "Logs laden…";
  Promise.all([
    fetch("/api/logs/inbox").then((x) => x.text()),
    fetch("/api/logs/dashboard").then((x) => x.text()),
  ])
    .then(([inbox, dashboard]) => {
      componentLogEntries.inbox = structuredLogEntries(inbox);
      componentLogEntries.dashboard = structuredLogEntries(dashboard);
      componentLogsLoaded = true;
      $("componentLogControls").hidden = false;
      renderComponentLogs();
      $("loadComponentLogs").textContent = "Logs geladen";
    })
    .catch(() => {
      componentLogEntries.inbox = structuredLogEntries(
        '{"level":"ERROR","event":"inbox_log_unavailable","diagnostic":"Inbox-log is niet beschikbaar."}',
      );
      componentLogEntries.dashboard = structuredLogEntries(
        '{"level":"ERROR","event":"dashboard_log_unavailable","diagnostic":"Dashboard-log is niet beschikbaar."}',
      );
      $("componentLogControls").hidden = false;
      renderComponentLogs();
      $("loadComponentLogs").disabled = false;
      $("loadComponentLogs").textContent = "Opnieuw proberen";
    });
}
const CHAT_HISTORY_KEY = "djconnect-engineering-chat-history",
  CHAT_CONTEXT_KEY = "djconnect-engineering-chat-context",
  CHAT_HISTORY_LIMIT = 20;
function loadChatHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(CHAT_HISTORY_KEY) || "[]");
    return Array.isArray(saved)
      ? saved
          .filter(
            (entry) =>
              entry &&
              ["user", "assistant"].includes(entry.role) &&
              typeof entry.text === "string",
          )
          .slice(-CHAT_HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}
let chatHistory = loadChatHistory();
function persistChatHistory() {
  sessionStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(chatHistory));
}
function renderLegacyChatMessage(role, text) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = role === "user" ? "Jij" : "Codex";
  body.className = "chat-message__body";
  body.textContent = text;
  item.append(label, body);
  $("chatMessages").append(item);
  item.scrollIntoView({ block: "nearest" });
}
function renderChatHistory() {
  const container = $("chatMessages");
  container.replaceChildren();
  chatHistory.forEach((entry) => chatMessage(entry.role, entry.text));
}
function reconcileChatContext(run) {
  if (!latestStatus) return;
  const context = run || "none";
  if (sessionStorage.getItem(CHAT_CONTEXT_KEY) === context) return;
  chatHistory = [];
  sessionStorage.removeItem(CHAT_HISTORY_KEY);
  sessionStorage.setItem(CHAT_CONTEXT_KEY, context);
  renderChatHistory();
}
function askCodex() {
  let input = $("chatInput"),
    message = input.value.trim();
  if (!message || $("chatSend").disabled) return;
  $("chatSend").disabled = true;
  $("chatStatus").textContent = "Codex denkt na…";
  chatHistory.push({ role: "user", text: message });
  chatHistory = chatHistory.slice(-CHAT_HISTORY_LIMIT);
  persistChatHistory();
  chatMessage("user", message);
  input.value = "";
  fetch("/api/codex-chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: message,
      history: chatHistory.slice(0, -1).slice(-6),
    }),
  })
    .then(async (response) => ({
      ok: response.ok,
      body: await response.json(),
    }))
    .then((result) => {
      if (!result.ok)
        throw Error(result.body.error || "Codex Gesprek is niet beschikbaar.");
      let answer = result.body.answer;
      $("chatModel").textContent =
        result.body.model || $("chatModel").textContent;
      chatHistory.push({ role: "assistant", text: answer });
      chatHistory = chatHistory.slice(-CHAT_HISTORY_LIMIT);
      persistChatHistory();
      chatMessage("assistant", answer);
      $("chatStatus").textContent = "";
    })
    .catch((error) => {
      $("chatStatus").textContent = error.message;
    })
    .finally(() => {
      $("chatSend").disabled = false;
    });
}
function fallbackCopy(value) {
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(area);
  area.focus();
  area.select();
  area.setSelectionRange(0, area.value.length);
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw Error("copy unavailable");
}
function copyText(value) {
  const copy =
    navigator.clipboard && window.isSecureContext
      ? navigator.clipboard.writeText(value).catch(() => fallbackCopy(value))
      : Promise.resolve().then(() => fallbackCopy(value));
  return copy.then(() => {
    showCopyToast();
  });
}
let copyToastTimer;
function showCopyToast() {
  const toast = $("copyToast");
  if (!toast) return;
  clearTimeout(copyToastTimer);
  toast.textContent = "Gekopieerd naar klembord";
  toast.hidden = false;
  requestAnimationFrame(() => toast.classList.add("copy-toast--visible"));
  copyToastTimer = setTimeout(() => {
    toast.classList.remove("copy-toast--visible");
    setTimeout(() => {
      toast.hidden = true;
    }, 180);
  }, 2200);
}
function copyReport() {
  report()
    .then(() => copyText($("reportContent").textContent))
    .catch(() => {
      $("copyReport").textContent = "Kopiëren mislukt";
    });
}
function copyReportAnalysis() {
  analysis()
    .then(() => copyText($("reportAnalysisContent").textContent))
    .catch(() => {
      $("copyReportAnalysis").textContent = "Kopiëren mislukt";
    });
}
function addReportAnalysisCopy() {
  const card = $("reportAnalysis");
  if (!card || $("copyReportAnalysis")) return;
  const button = document.createElement("button");
  button.className = "copy";
  button.id = "copyReportAnalysis";
  button.type = "button";
  button.title = "Kopieer analyse";
  button.setAttribute("aria-label", "Kopieer analyse");
  button.textContent = "⧉ Kopieer";
  button.addEventListener("click", copyReportAnalysis);
  card.querySelector("summary").insertAdjacentElement("afterend", button);
}
addReportAnalysisCopy();
function renderHealthStatus(x, snapshot = {}) {
  lastRefresh = new Date();
  clock();
  x = x && typeof x === "object" ? x : fallback;
  latestStatus = x;
  let active = isActiveRun(x),
    statusTone = tone(x),
    indicator = $("indicator"),
    previous = x.last_executed_run || null,
    lastStatus = finalStatus(x.last_executed_phase),
    components = snapshot.component_versions || {},
    blockedPredecessor = Boolean(x.blocking_predecessor_run),
    terminalBlocked = isTerminalBlockedRun(x),
    blocked = blockedPredecessor || terminalBlocked;
  if (previous !== lastExecutedRun) {
    lastExecutedRun = previous;
    reportLoaded = false;
    reportRequest = undefined;
    analysisLoaded = false;
    analysisRequest = undefined;
    $("report").open = false;
    $("reportAnalysis").open = false;
    $("reportContent").textContent = "Open dit blok om het rapport te laden.";
    $("reportAnalysisContent").textContent =
      "Open dit blok om de analyse te laden.";
  }
  $("currentRun").hidden = !(active || blocked);
  $("promptRuns").hidden = !previous;
  $("lastExecution").hidden = !previous;
  $("report").hidden = !previous;
  $("reportAnalysis").hidden = !previous;
  if (previous) report();
  $("predecessorGate").hidden = !blockedPredecessor;
  $("predecessorRun").textContent =
    x.blocking_predecessor_run || "Niet beschikbaar";
  $("predecessorPrompt").textContent =
    x.blocking_predecessor_title ||
    x.blocking_predecessor_filename ||
    "Niet beschikbaar";
  $("predecessorPhase").textContent = translate(
    x.blocking_predecessor_phase || "Niet beschikbaar",
  );
  $("predecessorAction").textContent =
    x.predecessor_recovery_action || "Niet beschikbaar";
  $("executionContext").hidden = !x.execution_mode;
  $("executionMode").textContent = x.execution_mode || "Niet beschikbaar";
  $("targetRepository").textContent = x.target_repository || "Niet beschikbaar";
  $("checkoutPath").textContent = x.checkout_path || "Niet beschikbaar";
  $("activeBranch").textContent = x.active_branch || "Niet beschikbaar";
  indicator.className =
    "indicator indicator--" +
    statusTone +
    (active ? " indicator--running" : "");
  indicator.setAttribute("aria-label", "Promptstatus: " + statusTone);
  $("lastIndicator").className =
    "indicator indicator--small indicator--" + lastStatus[0];
  $("lastFinalStatus").textContent = lastStatus[1];
  $("watcher").textContent = translate(
    x.watcher_state || fallback.watcher_state,
  );
  $("phase").textContent = translate(
    x.current_phase || (terminalBlocked ? x.last_executed_phase : "idle"),
  );
  $("action").textContent = translate(
    x.current_action ||
      (terminalBlocked
        ? "Herstel de geblokkeerde prompt om opnieuw uit te voeren."
        : "Geen actieve actie"),
  );
  const preflight = snapshot.host_preflight || {};
  $("hostPreflightStatus").textContent = preflight.outcome || "Niet beschikbaar";
  $("hostPreflightTimestamp").textContent = preflight.timestamp || "Nog niet uitgevoerd";
  promptStarted(snapshot.prompt_started);
  renderEstimate(x);
  processMetrics(active, snapshot.process_metrics);
  $("currentPrompt").textContent =
    x.prompt_title || (terminalBlocked ? x.last_executed_title : null) || "Niet beschikbaar";
  $("currentFile").textContent =
    x.submitted_filename ||
    (terminalBlocked ? x.last_executed_filename : null) ||
    "Niet beschikbaar";
  if (!active || x.run_id !== currentLogRun)
    $("currentDiagnostic").hidden = true;
  if (active)
    l(
      "currentLog",
      "/api/log/current",
      x.run_id || null,
      false,
      "currentDiagnostic",
    );
  $("lastPrompt").textContent =
    x.last_executed_title || "Nog geen prompt uitgevoerd";
  $("lastFile").textContent = x.last_executed_filename || "Niet beschikbaar";
  $("lastDiagnostic").hidden = lastStatus[0] === "green";
  if (previous && lastStatus[0] !== "green")
    l("lastLog", "/api/log/last", previous, true, "lastDiagnostic");
  $("runId").textContent =
    x.run_id || (terminalBlocked ? x.last_executed_run : null) || "geen";
  $("queue").textContent = x.queue_depth ?? 0;
  queueItems(x.queue_items, x.queue_depth);
  $("implementation").textContent = x.implementation_pr || "geen";
  $("finalization").textContent = x.finalization_pr || "geen";
  $("repositoryState").textContent = translate(x.repository_state || "UNKNOWN");
  $("workspaceState").textContent = translate(x.workspace_state || "UNKNOWN");
  $("diag").textContent = translate(x.diagnostic || "Geen diagnose");
  $("platformVersion").textContent = x.platform_version || "Niet beschikbaar";
  $("dashboardVersion").textContent =
    components.dashboard || "Niet beschikbaar";
  $("workerVersion").textContent = components.worker || "Niet beschikbaar";
  usage(snapshot.usage);
  rateLimits(snapshot.rate_limits);
  lastUsage(snapshot.last_executed_usage);
  commits(snapshot.completion_commits);
  lastCommits(snapshot.last_executed_commits);
  reviewerAgents(snapshot.last_executed_reviewer_agents);
}
let lastExecutionCategoryRun, activePromptCategoryRun;
function renderRunCategory(x) {
  const active = x && typeof x === "object" && isActiveRun(x),
    blockedPredecessor = Boolean(x?.blocking_predecessor_run),
    terminalBlocked = isTerminalBlockedRun(x),
    blocked = blockedPredecessor || terminalBlocked,
    current = $("currentRun"),
    previous = x && typeof x === "object" ? x.last_executed_run || null : null,
    group = $("lastExecutionGroup");
  const currentRunKey = active
    ? x.run_id
    : blockedPredecessor
      ? x.blocking_predecessor_run
      : terminalBlocked
        ? x.last_executed_run
        : null;
  if (currentRunKey && current && currentRunKey !== activePromptCategoryRun) {
    activePromptCategoryRun = currentRunKey;
    current.open = blocked;
  }
  if (!group) return;
  group.hidden = !previous;
  if (previous !== lastExecutionCategoryRun) {
    lastExecutionCategoryRun = previous;
    group.open = false;
  }
}
const dashboardStatusStore = createDashboardStatusStore({
  fallback,
  render: renderDashboardStatus,
});
function renderChatStatus(status) {
  reconcileChatContext(status.last_executed_run);
}
function renderComponentDetails() {
  void refreshOpenComponentDetails();
}
function renderDashboardStatus(status, snapshot) {
  renderHealthStatus(status, snapshot);
  renderRunCategory(status);
  renderReportAvailability(status, snapshot);
  renderLogsForSnapshot(snapshot);
  renderDashboardTelemetry(snapshot);
  renderExecutionEvidence(snapshot);
  renderPredecessorRetry(status);
  renderChatStatus(status);
  renderComponentDetails();
  updateAllSectionsToggle();
  hideDashboardSplash();
  void refreshPlatformHealth();
}
function r(status, snapshot = {}) {
  dashboardStatusStore.update(status, snapshot);
}
let receivedDashboardServerPush = false;
async function loadInitialDashboardStatus() {
  try {
    const response = await fetch("/api/dashboard-snapshot", {
      cache: "no-store",
    });
    if (!response.ok) throw Error("Dashboardstatus is niet beschikbaar.");
    const snapshot = await response.json();
    if (!snapshot || typeof snapshot.status !== "object")
      throw Error("Dashboardstatus is ongeldig.");
    if (receivedDashboardServerPush) return;
    dashboardStatusStore.update(snapshot.status, snapshot);
    humanize();
    checkBuild(snapshot.build_commit);
    $("updateMode").textContent = "Status geladen; serverpush verbinden…";
  } catch {
    if (receivedDashboardServerPush) return;
    dashboardStatusStore.update(fallback);
    humanize();
    $("updateMode").textContent =
      "Status laden mislukt; serverpush opnieuw verbinden…";
  }
}
void loadInitialDashboardStatus();
let e = new EventSource("/api/events");
e.addEventListener("dashboard", (x) => {
  if (!$("autoRefresh").checked) return;
  try {
    let snapshot = JSON.parse(x.data);
    receivedDashboardServerPush = true;
    dashboardStatusStore.update(snapshot.status, snapshot);
    humanize();
    checkBuild(snapshot.build_commit);
    $("updateMode").textContent = "Serverpush: verbonden";
  } catch {
    dashboardStatusStore.update(fallback);
    humanize();
    $("updateMode").textContent = "Serverpush: update ongeldig";
  }
});
e.onerror = () => {
  $("autoRefresh").checked &&
    ($("updateMode").textContent = "Serverpush: opnieuw verbinden…");
};
$("report").addEventListener("toggle", () => {
  $("report").open && report();
});
$("reportAnalysis").addEventListener("toggle", () => {
  $("reportAnalysis").open && analysis();
});
$("copyReport").addEventListener("click", copyReport);
$("loadComponentLogs").addEventListener("click", loadComponentLogs);
for (const id of ["logFilter", "logLevelFilter", "logSort"])
  $(id).addEventListener("input", renderComponentLogs);
$("chatSend").addEventListener("click", askCodex);
$("chatInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    askCodex();
  }
});
renderChatHistory();
setInterval(() => {
  reconcileChatContext(latestStatus?.last_executed_run);
  clock();
}, 250);
clock();
let logSortState = { key: "timestamp", direction: "desc" };
function logValue(entry, key) {
  if (key === "line") return Number(entry.line) || 0;
  if (key === "timestamp") return logTimestamp(entry);
  return String(entry[key] || "").toLocaleLowerCase("nl-NL");
}
function updateLogSortHeaders() {
  document
    .querySelectorAll(".log-table th[data-sort-key]")
    .forEach((header) => {
      const active = header.dataset.sortKey === logSortState.key;
      header.dataset.sortIndicator = active
        ? logSortState.direction === "asc"
          ? "↑"
          : "↓"
        : "↕";
      header.setAttribute(
        "aria-sort",
        active
          ? logSortState.direction === "asc"
            ? "ascending"
            : "descending"
          : "none",
      );
    });
}
function setLogSort(key) {
  logSortState =
    logSortState.key === key
      ? {
          key: key,
          direction: logSortState.direction === "asc" ? "desc" : "asc",
        }
      : { key: key, direction: key === "timestamp" ? "desc" : "asc" };
  updateLogSortHeaders();
  renderComponentLogs();
}
function renderSortedComponentLogs() {
  const needle = $("logFilter").value.trim().toLocaleLowerCase("nl-NL"),
    level = $("logLevelFilter").value;
  for (const component of ["inbox", "dashboard"]) {
    const rows = componentLogEntries[component]
        .filter((entry) => !level || entry.level === level)
        .filter(
          (entry) =>
            !needle ||
            Object.values(entry)
              .join(" ")
              .toLocaleLowerCase("nl-NL")
              .includes(needle),
        )
        .sort((left, right) => {
          const first = logValue(left, logSortState.key),
            second = logValue(right, logSortState.key),
            result =
              typeof first === "number" && typeof second === "number"
                ? first - second
                : String(first).localeCompare(String(second), "nl");
          return logSortState.direction === "asc" ? result : -result;
        }),
      body = $(component + "ComponentLog");
    body.replaceChildren();
    if (!rows.length) {
      const cell = document.createElement("td"),
        row = document.createElement("tr");
      cell.className = "log-empty";
      cell.colSpan = 6;
      cell.textContent = "Geen logregels voor deze selectie.";
      row.append(cell);
      body.append(row);
      continue;
    }
    for (const entry of rows) {
      const row = document.createElement("tr");
      for (const [name, value] of [
        ["log-line-number", entry.line],
        ["", logTimestampText(entry.timestamp)],
        [
          "log-level log-level--" +
            entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ", "-"),
          entry.level,
        ],
        ["", entry.event],
        ["", entry.runId || "—"],
        ["", entry.details || "—"],
      ]) {
        const cell = document.createElement("td");
        cell.className = name;
        cell.textContent = value;
        row.append(cell);
      }
      body.append(row);
    }
  }
}
function configureLogSortHeaders() {
  const keys = ["line", "timestamp", "level", "event", "runId", "details"];
  document.querySelectorAll(".log-table").forEach((table) =>
    table.querySelectorAll("th").forEach((header, index) => {
      const key = keys[index];
      header.classList.add("log-sortable");
      header.dataset.sortKey = key;
      header.tabIndex = 0;
      header.addEventListener("click", () => setLogSort(key));
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          setLogSort(key);
        }
      });
    }),
  );
  updateLogSortHeaders();
}
function providerNeutralLabels() {
  const labels = [
    ["#processMetrics>strong", "Lokale AI-processen"],
    ["#usage>strong", "AI-providergebruik"],
    ["#currentDiagnostic>strong", "AI-uitvoeringsdiagnose"],
    ["#rateLimits .label", "AI-providerlimieten"],
    ["#lastUsage .label", "AI-providergebruik"],
    ["#lastDiagnostic .label", "AI-uitvoeringsdiagnose"],
    ["#reportAnalysis summary strong", "AI-analyse van rapport"],
    ["#codexChat>strong", "AI-gesprek"],
    ["#chatMessages", "Gesprek met AI-assistent"],
    ["label[for=chatInput]", "Nieuwe vraag aan AI-assistent"],
  ];
  labels.forEach(([selector, text]) => {
    const element = document.querySelector(selector);
    if (element) {
      element.textContent = text;
      if (selector === "#chatMessages")
        element.setAttribute("aria-label", text);
    }
  });
}
function chatMessage(role, text) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = role === "user" ? "Jij" : "AI-assistent";
  body.className = "chat-message__body";
  body.textContent = text;
  item.append(label, body);
  $("chatMessages").append(item);
  item.scrollIntoView({ block: "nearest" });
}
configureLogSortHeaders();
providerNeutralLabels();
function groupLastExecution() {
  const group = $("lastExecutionGroup");
  if (!group || group.tagName === "DETAILS") return;
  const category = document.createElement("details"),
    summary = document.createElement("summary"),
    title = document.createElement("strong"),
    content = document.createElement("div");
  category.id = group.id;
  category.className = group.className;
  category.dataset.testid = "last-executed-prompt-category";
  category.hidden = group.hidden;
  title.textContent = "Laatst uitgevoerde prompt";
  summary.append(title);
  content.className = "last-execution-group__content";
  while (group.firstChild) content.append(group.firstChild);
  category.append(summary, content);
  group.replaceWith(category);
}
groupLastExecution();
function addCategoryIcons() {
  for (const [selector, glyph, label] of [
    ["#workspaceCard", "⌂", "Werkruimte"],
    ["#queueItems", "☷", "Inbox-wachtrij"],
    ["#promptHistory", "◫", "Promptgeschiedenis"],
    ["#platformHealth", "◈", "Platformonderdelen"],
    ["#rateLimits", "◔", "Resterend gebruik"],
    ["#executionTelemetry", "▥", "Execution Host-telemetrie"],
    ["#lastExecutionGroup", "◷", "Laatst uitgevoerde prompt"],
    ["#codexChat", "✦", "AI-gesprek"],
    ["#technicalDetails", "⚙", "Technische details"],
    ["#componentLogs", "≡", "Logs"],
    ["#currentRun", "▤", "Actieve prompt"],
  ]) {
    const summary = document.querySelector(selector + ">summary");
    if (!summary || summary.querySelector(".category-icon")) continue;
    const icon = document.createElement("span"),
      title = summary.querySelector("strong,.label");
    icon.className = "category-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = glyph;
    icon.title = label;
    if (title) title.before(icon);
    else summary.prepend(icon);
  }
}
addCategoryIcons();
function addCategoryDescriptions() {
  const descriptions = [
    [".workspace-card", "De actieve werkruimte van dit project."],
    ["#queueItems", "Prompts worden uitgevoerd op volgorde van aanmaakdatum."],
    [
      "#promptHistory",
      "Alle terminale Engineering Platform-uitvoeringen, lokaal gecachet in de Engineering SQLite-opslag.",
    ],
    [
      "#rateLimits",
      "Beschikbare gebruiksruimte en resets van de actieve AI-provider.",
    ],
    [
      "#lastExecutionGroup",
      "De meest recent uitgevoerde prompt, met bewijs, rapport en analyse.",
    ],
    [
      "#componentLogs",
      "Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush.",
    ],
    [
      "#codexChat",
      "Stel korte, alleen-lezen vragen over de laatst uitgevoerde prompt en het bijbehorende rapport. Dit start geen engineering of wijzigingen.",
    ],
    [
      "#engineering-dashboard-content>.technical-details:not(#componentLogs)",
      "Operationele details over pull requests, repository, werkruimte en diagnose.",
    ],
  ];
  for (const [selector, text] of descriptions) {
    const category = document.querySelector(selector),
      summary = category?.querySelector(":scope>summary");
    if (!category || !summary) continue;
    let description =
      category.querySelector(":scope>.category-description") ||
      category.querySelector(":scope>.estimate-meta");
    if (!description) {
      description = document.createElement("p");
      description.textContent = text;
      summary.insertAdjacentElement("afterend", description);
    }
    description.classList.add("category-description");
  }
}
addCategoryDescriptions();
function arrangeOperationalCategories() {
  const technical = $("technicalDetails"),
    telemetry = $("executionTelemetry"),
    health = $("platformHealth"),
    logs = $("componentLogs");
  if (!technical) return;
  let anchor = technical;
  if (telemetry) {
    anchor.insertAdjacentElement("afterend", telemetry);
    anchor = telemetry;
  }
  if (health) {
    anchor.insertAdjacentElement("afterend", health);
    anchor = health;
  }
  if (logs) anchor.insertAdjacentElement("afterend", logs);
}
arrangeOperationalCategories();
$("rateLimitReset").addEventListener("click", consumeRateLimitReset);
function addTestIds() {
  const toTestId = (value) =>
    "engineering-" +
    value.replace(/[A-Z]/g, (letter) => "-" + letter.toLowerCase());
  document
    .querySelector("main")
    ?.setAttribute("data-testid", "engineering-dashboard");
  document
    .querySelector("h1")
    ?.setAttribute("data-testid", "engineering-dashboard-title");
  document.querySelectorAll("[id]").forEach((element) => {
    if (!element.dataset.testid) element.dataset.testid = toTestId(element.id);
  });
  document
    .querySelectorAll(".log-table")
    .forEach(
      (table, index) =>
        (table.dataset.testid = "engineering-log-table-" + (index + 1)),
    );
}
addTestIds();
function applyAccessibility() {
  const indicator = $("indicator"),
    chatStatus = $("chatStatus"),
    messages = $("chatMessages");
  indicator.setAttribute("role", "status");
  indicator.setAttribute("aria-live", "polite");
  indicator.setAttribute("aria-atomic", "true");
  chatStatus.setAttribute("role", "status");
  chatStatus.setAttribute("aria-live", "polite");
  messages.setAttribute("role", "log");
  messages.setAttribute("aria-relevant", "additions text");
  document.querySelectorAll(".log-table").forEach((table, index) => {
    table.setAttribute(
      "aria-label",
      index === 0
        ? "Logregels van Inbox-watcher"
        : "Logregels van Statusdashboard",
    );
    table.querySelectorAll("th.log-sortable").forEach((header) => {
      header.setAttribute("role", "button");
      header.setAttribute(
        "aria-label",
        header.textContent.trim() + " sorteren",
      );
    });
  });
  const live = document.createElement("div");
  live.className = "sr-only";
  live.id = "dashboardStatusAnnouncement";
  live.setAttribute("role", "status");
  live.setAttribute("aria-live", "polite");
  live.setAttribute("aria-atomic", "true");
  document.body.append(live);
  let previous = "";
  new MutationObserver(() => {
    const message = indicator.getAttribute("aria-label") || "";
    if (message && message !== previous) {
      previous = message;
      live.textContent = message;
    }
  }).observe(indicator, { attributes: true, attributeFilter: ["aria-label"] });
}
applyAccessibility();
renderChatHistory();
function appendMarkdownInline(target, value) {
  const pattern =
    /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\*[^*]+\*)/g;
  let offset = 0;
  for (const token of String(value).matchAll(pattern)) {
    target.append(document.createTextNode(value.slice(offset, token.index)));
    const text = token[0];
    if (text.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = text.slice(2, -2);
      target.append(strong);
    } else if (text.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = text.slice(1, -1);
      target.append(code);
    } else if (text.startsWith("[")) {
      const match = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(text),
        link = document.createElement("a");
      link.href = match[2];
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      link.textContent = match[1];
      target.append(link);
    } else {
      const emphasis = document.createElement("em");
      emphasis.textContent = text.slice(1, -1);
      target.append(emphasis);
    }
    offset = token.index + text.length;
  }
  target.append(document.createTextNode(value.slice(offset)));
}
function renderMarkdownAnswer(target, value) {
  const newline = String.fromCharCode(10);
  let codeLines = null,
    list = null,
    listType = "";
  for (const line of String(value).split(newline)) {
    if (line.startsWith("```")) {
      if (codeLines === null) {
        codeLines = [];
      } else {
        const pre = document.createElement("pre"),
          code = document.createElement("code");
        code.textContent = codeLines.join(newline);
        pre.append(code);
        target.append(pre);
        codeLines = null;
      }
      list = null;
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line),
      bullet = /^[-*]\s+(.+)$/.exec(line),
      ordered = /^\d+\.\s+(.+)$/.exec(line);
    if (heading) {
      const element = document.createElement(
        "h" + Math.min(heading[1].length + 2, 4),
      );
      appendMarkdownInline(element, heading[2]);
      target.append(element);
      list = null;
      continue;
    }
    if (/^ {0,3}([-*_])\1\1+\s*$/.test(line)) {
      target.append(document.createElement("hr"));
      list = null;
      continue;
    }
    if (bullet || ordered) {
      const type = bullet ? "ul" : "ol";
      if (!list || listType !== type) {
        list = document.createElement(type);
        listType = type;
        target.append(list);
      }
      const item = document.createElement("li");
      appendMarkdownInline(item, (bullet || ordered)[1]);
      list.append(item);
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const paragraph = document.createElement("p");
    appendMarkdownInline(paragraph, line);
    target.append(paragraph);
  }
  if (codeLines !== null) {
    const pre = document.createElement("pre"),
      code = document.createElement("code");
    code.textContent = codeLines.join(newline);
    pre.append(code);
    target.append(pre);
  }
}
const plainChatMessage = chatMessage;
chatMessage = (role, text) => {
  if (role !== "assistant") {
    plainChatMessage(role, text);
    return;
  }
  const item = document.createElement("article"),
    label = document.createElement("span"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--assistant";
  label.className = "chat-message__role";
  label.textContent = "AI-assistent";
  body.className = "chat-message__body";
  renderMarkdownAnswer(body, text);
  item.append(label, body);
  $("chatMessages").append(item);
  item.scrollIntoView({ block: "nearest" });
};
renderChatHistory();
function addChatMessageCopyButton(item, text) {
  if (!item || item.querySelector(".chat-message__copy")) return;
  const button = document.createElement("button");
  button.className = "chat-message__copy";
  button.type = "button";
  button.title = "Kopieer bericht";
  button.setAttribute("aria-label", "Kopieer bericht");
  button.textContent = "⧉";
  button.addEventListener("click", () => {
    copyText(String(text))
      .then(() => void recordUserAction("chat_message_copied"))
      .catch(() => {
        button.title = "Kopiëren mislukt";
      });
  });
  item.append(button);
}
const chatMessageWithCopy = chatMessage;
chatMessage = (role, text) => {
  chatMessageWithCopy(role, text);
  addChatMessageCopyButton($("chatMessages").lastElementChild, text);
};
renderChatHistory();
$("chatSend").querySelector("span").textContent = "↑";
let componentLogVersion = "";
function refreshComponentLogs(versions = {}) {
  const version = JSON.stringify(versions);
  if (componentLogsLoaded && version === componentLogVersion) return;
  componentLogVersion = version;
  Promise.all([
    fetch("/api/logs/inbox").then((response) => response.text()),
    fetch("/api/logs/dashboard").then((response) => response.text()),
  ])
    .then(([inbox, dashboard]) => {
      componentLogEntries.inbox = structuredLogEntries(inbox);
      componentLogEntries.dashboard = structuredLogEntries(dashboard);
      componentLogsLoaded = true;
      $("componentLogControls").hidden = false;
      renderComponentLogs();
    })
    .catch(() => {
      componentLogEntries.inbox = structuredLogEntries(
        '{"level":"ERROR","event":"inbox_log_unavailable","diagnostic":"Inbox-log is niet beschikbaar."}',
      );
      componentLogEntries.dashboard = structuredLogEntries(
        '{"level":"ERROR","event":"dashboard_log_unavailable","diagnostic":"Dashboard-log is niet beschikbaar."}',
      );
      $("componentLogControls").hidden = false;
      renderComponentLogs();
    });
}
function enableLiveComponentLogs() {
  const button = $("loadComponentLogs"),
    description = document.querySelector("#componentLogs .estimate-meta");
  button?.remove();
  if (description)
    description.textContent =
      "Geredigeerde, roterende logs van watcher en dashboard. Automatisch bijgewerkt via serverpush.";
  $("componentLogControls").hidden = false;
  refreshComponentLogs();
}
function renderLogsForSnapshot(snapshot) {
  refreshComponentLogs(snapshot.component_log_versions || {});
}
enableLiveComponentLogs();
const healthComponentLabels = {
  dashboard: "Statusdashboard",
  inbox_watcher: "Inbox-watcher",
  dashboard_relay: "Dashboardrelay",
};
let healthRequestInFlight = false;
const componentDetailsRefreshIntervalMs = 5e3;
let activeComponentDetails = null,
  componentDetailsRefreshTimer = null,
  componentDetailsRefreshInFlight = false;
function healthIndicatorClass(healthy) {
  return healthy ? "indicator indicator--green" : "indicator indicator--red";
}
function formatComponentUptime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds),
    days = Math.floor(total / 86400),
    hours = Math.floor((total % 86400) / 3600),
    minutes = Math.floor((total % 3600) / 60);
  return days
    ? days + "d " + hours + "u"
    : hours
      ? hours + "u " + minutes + "m"
      : minutes
        ? minutes + "m"
        : total + "s";
}
function componentDetailField(list, label, value) {
  if (value === null || value === undefined || value === "") return;
  const term = document.createElement("dt"),
    description = document.createElement("dd"),
    entry = document.createElement("div");
  term.textContent = label;
  description.textContent = String(value);
  entry.append(term, description);
  list.append(entry);
}
function componentMemory(processes) {
  if (!Array.isArray(processes) || !processes.length)
    return "Geen lokaal proces gevonden";
  return processes
    .map(
      (process) =>
        "PID " +
        process.pid +
        ": " +
        (Number(process.memory_kib || 0) / 1024).toFixed(1) +
        " MiB",
    )
    .join(" · ");
}
function showComponentModal(payload) {
  const modal = $("componentModal"),
    content = $("componentModalContent"),
    title = $("componentModalTitle"),
    restart = $("componentModalRestart"),
    status = $("componentModalStatus"),
    launchd = payload.launchd || {};
  title.textContent =
    healthComponentLabels[payload.component] || "Componentinformatie";
  content.replaceChildren();
  const fields = document.createElement("dl");
  componentDetailField(fields, "Machine", payload.machine);
  componentDetailField(
    fields,
    "Status",
    (payload.healthy ? "Gezond" : "Niet gezond") +
      " · " +
      (payload.detail || payload.state || "Geen toelichting"),
  );
  componentDetailField(fields, "Versie", payload.version);
  componentDetailField(
    fields,
    "Uptime",
    formatComponentUptime(payload.uptime_seconds),
  );
  componentDetailField(fields, "Git-commit", payload.git_commit);
  componentDetailField(
    fields,
    "Uitvoerbaar pad",
    Array.isArray(launchd.program_arguments) && launchd.program_arguments.length
      ? launchd.program_arguments[0]
      : payload.executable_path,
  );
  componentDetailField(fields, "Launchd-label", launchd.label);
  componentDetailField(fields, "LaunchAgent", launchd.plist_path);
  componentDetailField(
    fields,
    "Launchd-instellingen",
    launchd.label
      ? (launchd.loaded ? "Geladen" : "Niet geladen") +
          " · Start bij laden: " +
          (launchd.run_at_load ? "ja" : "nee") +
          " · Blijf actief: " +
          (launchd.keep_alive ? "ja" : "nee")
      : null,
  );
  componentDetailField(
    fields,
    "Huidig geheugen",
    componentMemory(payload.processes),
  );
  content.append(fields);
  restart.hidden = !payload.restart_supported;
  restart.dataset.component = payload.component;
  if (!modal.open) {
    status.textContent = "";
    modal.showModal();
    modal.focus();
  }
}
async function requestComponentDetails(component, showError = true) {
  try {
    const response = await fetch(
        "/api/components/" + encodeURIComponent(component) + "/details",
        { cache: "no-store" },
      ),
      payload = await response.json();
    if (!response.ok)
      throw Error(payload.error || "Componentinformatie is niet beschikbaar.");
    showComponentModal(payload);
    return true;
  } catch (error) {
    if (showError) legacyDashboardError();
    return false;
  }
}
function stopComponentDetailsRefresh() {
  if (componentDetailsRefreshTimer !== null) {
    window.clearInterval(componentDetailsRefreshTimer);
    componentDetailsRefreshTimer = null;
  }
  activeComponentDetails = null;
}
function startComponentDetailsRefresh(component) {
  stopComponentDetailsRefresh();
  activeComponentDetails = component;
  componentDetailsRefreshTimer = window.setInterval(
    () => void refreshOpenComponentDetails(),
    componentDetailsRefreshIntervalMs,
  );
}
async function refreshOpenComponentDetails() {
  const modal = $("componentModal");
  if (!activeComponentDetails || !modal.open || componentDetailsRefreshInFlight)
    return;
  componentDetailsRefreshInFlight = true;
  try {
    await requestComponentDetails(activeComponentDetails, false);
  } finally {
    componentDetailsRefreshInFlight = false;
  }
}
async function showComponentDetails(component) {
  const shown = await requestComponentDetails(component);
  if (shown) startComponentDetailsRefresh(component);
}
async function restartDashboardComponent() {
  const restart = $("componentModalRestart"),
    component = restart.dataset.component;
  if (!component) return;
  if (
    !legacyConfirmation(
      "Weet je zeker dat je dit Engineering Platform-onderdeel wilt herstarten?",
    )
  )
    return;
  restart.disabled = true;
  try {
    const response = await fetch(
        "/api/components/" + encodeURIComponent(component) + "/restart",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      ),
      payload = await response.json();
    if (!response.ok)
      throw Error(payload.error || "Herstarten is niet gelukt.");
    $("componentModalStatus").textContent =
      "Herstartverzoek verzonden. De component komt zo opnieuw beschikbaar.";
  } catch (error) {
    $("componentModalStatus").textContent =
      error.message || "Herstarten is niet gelukt.";
  } finally {
    restart.disabled = false;
  }
}
$("componentModalClose").addEventListener("click", () =>
  $("componentModal").close(),
);
$("componentModal").addEventListener("click", (event) => {
  if (event.target === $("componentModal")) $("componentModal").close();
});
$("componentModal").addEventListener("close", stopComponentDetailsRefresh);
function renderPlatformHealth(payload) {
  const container = $("platformHealthComponents");
  if (!container) return;
  const components =
    payload && typeof payload.components === "object"
      ? payload.components
      : null;
  container.replaceChildren();
  if (!components) {
    const message = document.createElement("p");
    message.className = "platform-health__empty";
    message.textContent =
      "De live gezondheidscontrole is tijdelijk niet beschikbaar.";
    container.append(message);
    return;
  }
  for (const [key, component] of Object.entries(components)) {
    const item = document.createElement("article"),
      indicator = document.createElement("span"),
      name = document.createElement("span"),
      detail = document.createElement("span"),
      info = document.createElement("span"),
      componentHealthy = Boolean(component?.healthy),
      version =
        typeof component?.version === "string"
          ? " · Versie " + component.version
          : "",
      uptime = formatComponentUptime(component?.uptime_seconds);
    item.className = "platform-health__component";
    item.dataset.health = String(componentHealthy);
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute(
      "aria-label",
      "Meer informatie over " + (healthComponentLabels[key] || key),
    );
    indicator.className = healthIndicatorClass(componentHealthy);
    indicator.setAttribute("aria-hidden", "true");
    name.className = "platform-health__component-name";
    name.textContent = healthComponentLabels[key] || key;
    detail.className = "platform-health__component-detail";
    detail.textContent =
      (componentHealthy ? "Gezond" : "Niet gezond") +
      " · " +
      String(component?.detail || component?.state || "Geen toelichting") +
      version +
      (uptime ? " · Uptime " + uptime : "");
    info.className = "component-info";
    info.textContent = "i";
    info.setAttribute("aria-hidden", "true");
    item.addEventListener("click", () => showComponentDetails(key));
    item.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      showComponentDetails(key);
    });
    item.append(indicator, name, detail, info);
    container.append(item);
  }
}
async function refreshPlatformHealth() {
  if (healthRequestInFlight) return;
  healthRequestInFlight = true;
  try {
    const response = await fetch("/health", { cache: "no-store" }),
      payload = await response.json();
    renderPlatformHealth(payload);
  } catch {
    renderPlatformHealth(null);
  } finally {
    healthRequestInFlight = false;
  }
}
refreshPlatformHealth();
window.setInterval(refreshPlatformHealth, 15e3);
function flattenMarkdownPanels() {
  for (const [panelId, contentId] of [
    ["report", "reportContent"],
    ["reportAnalysis", "reportAnalysisContent"],
  ]) {
    const panel = $(panelId),
      content = $(contentId),
      field = content?.closest(".field");
    if (panel && field && field.parentElement === panel)
      field.replaceWith(content);
  }
}
flattenMarkdownPanels();
function compactCopyButton(buttonId, contentId) {
  const button = $(buttonId),
    content = $(contentId);
  if (!button || !content) return;
  let wrapper = content.parentElement;
  if (!wrapper.classList.contains("markdown-copy-wrap")) {
    wrapper = document.createElement("div");
    wrapper.className = "markdown-copy-wrap";
    content.replaceWith(wrapper);
    wrapper.append(content);
  }
  button.classList.add("copy--glyph");
  button.textContent = "⧉";
  wrapper.append(button);
}
function compactReportCopyButtons() {
  compactCopyButton("copyReport", "reportContent");
  compactCopyButton("copyReportAnalysis", "reportAnalysisContent");
}
compactReportCopyButtons();
function downloadLastExecutedDocument(endpoint, filenamePrefix) {
  if (!lastExecutedRun)
    return Promise.reject(Error("Geen uitgevoerde prompt beschikbaar."));
  return fetch(endpoint + "?run_id=" + encodeURIComponent(lastExecutedRun))
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(Error("Download is niet beschikbaar.")),
    )
    .then((text) => {
      if (!text) throw Error("Download is niet beschikbaar.");
      const link = document.createElement("a"),
        url = URL.createObjectURL(
          new Blob([text], { type: "text/markdown;charset=utf-8" }),
        ),
        safeRun = String(lastExecutedRun).replace(/[^a-z0-9._-]+/gi, "-");
      link.href = url;
      link.download = filenamePrefix + "-" + safeRun + ".md";
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    });
}
function addDownloadButton(
  panelId,
  contentId,
  buttonId,
  filenamePrefix,
  label,
) {
  const panel = $(panelId),
    content = $(contentId);
  if (!panel || !content || $(buttonId)) return;
  const button = document.createElement("button");
  button.className = "download download--glyph";
  button.id = buttonId;
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.textContent = "⇩";
  button.hidden = true;
  button.addEventListener("click", () =>
    downloadLastExecutedDocument(
      panelId === "report"
        ? "/api/report/last-executed"
        : "/api/report-analysis/last-executed",
      filenamePrefix,
    ).catch(() => {
      button.title = "Download is niet beschikbaar.";
    }),
  );
  const wrapper = content.parentElement;
  button.classList.add("download--glyph");
  wrapper.append(button);
}
addDownloadButton(
  "report",
  "reportContent",
  "downloadReport",
  "engineering-report",
  "Download rapport",
);
addDownloadButton(
  "reportAnalysis",
  "reportAnalysisContent",
  "downloadReportAnalysis",
  "ai-analyse",
  "Download AI-analyse",
);
const originalCopyAvailable = copyAvailable;
copyAvailable = (id, available) => {
  originalCopyAvailable(id, available);
  const downloads = {
    copyReport: "downloadReport",
    copyReportAnalysis: "downloadReportAnalysis",
  };
  if (downloads[id]) originalCopyAvailable(downloads[id], available);
};
function placeFinalStatusIndicator() {
  const indicator = $("lastIndicator"),
    status = $("lastFinalStatus");
  if (indicator && status) status.before(indicator);
}
placeFinalStatusIndicator();
function copyAvailable(id, available) {
  const button = $(id);
  if (button) button.hidden = !available;
}
function updateCopyAvailability() {
  const unavailable = (value) =>
    !value ||
    value.startsWith("Open dit blok") ||
    value.includes("is niet beschikbaar.") ||
    value.startsWith("Er is geen AI-analyse");
  copyAvailable(
    "copyReport",
    !unavailable($("reportContent")?.textContent?.trim()),
  );
  copyAvailable(
    "copyReportAnalysis",
    !unavailable($("reportAnalysisContent")?.textContent?.trim()),
  );
}
copyAvailable("copyReport", false);
copyAvailable("copyReportAnalysis", false);
const reportWithCopyAvailability = report;
report = () =>
  reportWithCopyAvailability().then((value) => {
    updateCopyAvailability();
    return value;
  });
const analysisWithCopyAvailability = analysis;
analysis = () =>
  analysisWithCopyAvailability().then((value) => {
    updateCopyAvailability();
    return value;
  });
let copyAvailabilityRun, displayedAnalysisAvailable;
function renderReportAvailability(x, snapshot) {
  const run = x && typeof x === "object" ? x.last_executed_run || null : null;
  if (Object.hasOwn(snapshot, "last_executed_report_analysis_available")) {
    displayedAnalysisAvailable = Boolean(
      snapshot.last_executed_report_analysis_available,
    );
    if (displayedAnalysisAvailable === false && run) {
      $("reportAnalysisContent").textContent =
        "Er is geen AI-analyse beschikbaar voor deze uitgevoerde prompt.";
      copyAvailable("copyReportAnalysis", false);
    }
  }
  if (run !== copyAvailabilityRun) {
    copyAvailabilityRun = run;
    copyAvailable("copyReport", false);
    copyAvailable("copyReportAnalysis", false);
  }
}
const analysisWithAvailability = analysis;
analysis = () => {
  if (displayedAnalysisAvailable === false) return Promise.resolve();
  return analysisWithAvailability();
};
function arrangeCurrentRunCategory() {
  const current = $("currentRun"),
    summary = current?.querySelector(":scope>summary"),
    prompt = $("currentPrompt"),
    indicator = $("indicator");
  if (!current || !summary || !prompt || !indicator) return;
  let heading = summary.querySelector(".current-run__prompt-heading");
  if (!heading) {
    heading = document.createElement("div");
    heading.className = "current-run__prompt-heading";
    prompt.replaceWith(heading);
    heading.append(prompt);
  }
  heading.append(indicator);
  let description = current.querySelector(
    ":scope>.current-run__category-description",
  );
  if (!description) {
    description = document.createElement("p");
    description.className = "current-run__category-description";
    description.textContent =
      "De actieve engineeringprompt, met actuele voortgang, uitvoeringstijd en uitvoeringscontext.";
    summary.insertAdjacentElement("afterend", description);
  }
}
function placeCurrentRunFirst() {
  const dashboard = $("engineering-dashboard-content"),
    current = $("currentRun");
  if (dashboard && current && dashboard.firstElementChild !== current)
    dashboard.prepend(current);
}
placeCurrentRunFirst();
arrangeCurrentRunCategory();
function durationText(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const hours = Math.floor(seconds / 3600),
    minutes = Math.floor((seconds % 3600) / 60),
    remaining = Math.round(seconds % 60);
  return (
    (hours ? hours + " u " : "") +
    (minutes ? minutes + " min " : "") +
    remaining +
    " sec"
  );
}
function executionTimeField(id, label, after) {
  let field = $(id),
    value = $(id + "Value");
  if (!field) {
    field = document.createElement("div");
    value = document.createElement("span");
    const fieldLabel = document.createElement("span");
    field.className = "field";
    field.id = id;
    value.id = id + "Value";
    fieldLabel.className = "label";
    fieldLabel.textContent = label;
    field.append(fieldLabel, value);
    after.insertAdjacentElement("afterend", field);
  }
  return [field, value];
}
function lastExecutionTime(x) {
  const agent = Number(x?.seconds),
    total = Number(x?.total_seconds),
    finishedAt = Date.parse(x?.finished_at || ""),
    file = $("lastFile").closest(".field"),
    [finishedField, finishedValue] = executionTimeField(
      "lastExecutionFinishedAt",
      "Uitgevoerd op",
      file,
    ),
    [agentField, agentValue] = executionTimeField(
      "lastExecutionTime",
      "Codex CLI-uitvoeringstijd",
      finishedField,
    ),
    [totalField, totalValue] = executionTimeField(
      "lastTotalExecutionTime",
      "Totale doorlooptijd",
      agentField,
    );
  finishedField.hidden = !Number.isFinite(finishedAt);
  agentField.hidden = !Number.isFinite(agent) || agent < 0;
  totalField.hidden = !Number.isFinite(total) || total < 0;
  if (!finishedField.hidden)
    finishedValue.textContent = formatTime.format(new Date(finishedAt));
  if (!agentField.hidden) agentValue.textContent = durationText(agent);
  if (!totalField.hidden) totalValue.textContent = durationText(total);
}
function renderLegacyExecutionTelemetry(rows) {
  let panel = $("executionTelemetry"),
    body = $("executionTelemetryRows");
  if (!panel) {
    panel = document.createElement("details");
    panel.id = "executionTelemetry";
    panel.className = "telemetry";
    const summary = document.createElement("summary"),
      title = document.createElement("strong"),
      description = document.createElement("p"),
      scroll = document.createElement("div"),
      table = document.createElement("table"),
      head = document.createElement("thead"),
      headRow = document.createElement("tr"),
      tableBody = document.createElement("tbody");
    title.textContent = "Execution Host-telemetrie";
    summary.append(title);
    description.className = "category-description";
    description.textContent =
      "Operationele trends van de laatste zeven dagen. Telemetrie is geen repositorybewijs.";
    scroll.className = "telemetry-scroll";
    table.className = "telemetry-table";
    table.setAttribute("aria-label", "Dagelijkse Execution Host-telemetrie");
    for (const label of [
      "Dag",
      "Prompts",
      "Gem. AI-tijd",
      "Gem. totaal",
      "Gem. wachttijd",
      "Input",
      "Output",
      "Totaal",
      "Voltooid",
      "Geblokkeerd",
      "Mislukt",
    ]) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      headRow.append(cell);
    }
    head.append(headRow);
    tableBody.id = "executionTelemetryRows";
    table.append(head, tableBody);
    scroll.append(table);
    panel.append(summary, description, scroll);
    const rate = $("rateLimits");
    rate?.insertAdjacentElement("afterend", panel);
    body = tableBody;
  }
  body.replaceChildren();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!row || typeof row !== "object") continue;
    const line = document.createElement("tr");
    for (const value of [
      telemetryDate(row.date),
      row.prompt_count,
      durationText(row.average_execution_seconds),
      durationText(row.average_total_execution_seconds),
      durationText(row.average_queue_wait_seconds),
      row.input_tokens ?? "—",
      row.output_tokens ?? "—",
      row.total_tokens ?? "—",
      row.complete_count,
      row.blocked_count,
      row.failed_count,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "—");
      line.append(cell);
    }
    body.append(line);
  }
  if (!body.children.length) {
    const line = document.createElement("tr"),
      cell = document.createElement("td");
    cell.colSpan = 11;
    cell.className = "telemetry-empty";
    cell.textContent =
      "Nog geen voltooide Execution Host-telemetrie beschikbaar.";
    line.append(cell);
    body.append(line);
  }
}
function telemetryDuration(seconds) {
  if (typeof seconds !== "number" || seconds < 0) return "—";
  const minutes = Math.floor(seconds / 60),
    remaining = Math.round(seconds % 60);
  return (minutes ? minutes + " min " : "") + remaining + " sec";
}
function telemetryDate(value) {
  const match = typeof value === "string" && value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? match[3] + "-" + match[2] + "-" + match[1] : String(value ?? "—");
}
function executionTelemetry(rows) {
  let panel = $("executionTelemetry"),
    body = $("executionTelemetryRows");
  if (!panel) {
    panel = document.createElement("details");
    panel.id = "executionTelemetry";
    panel.className = "telemetry";
    const summary = document.createElement("summary"),
      title = document.createElement("strong"),
      description = document.createElement("p"),
      scroll = document.createElement("div"),
      table = document.createElement("table"),
      head = document.createElement("thead"),
      headRow = document.createElement("tr"),
      tableBody = document.createElement("tbody");
    title.textContent = "Execution Host-telemetrie";
    summary.append(title);
    description.className = "category-description";
    description.textContent =
      "Operationele trends van de laatste zeven dagen. Telemetrie is geen repositorybewijs.";
    scroll.className = "telemetry-scroll";
    table.className = "telemetry-table";
    table.setAttribute("aria-label", "Dagelijkse Execution Host-telemetrie");
    for (const label of [
      "Dag",
      "Prompts",
      "Gem. uitvoering",
      "Gem. wachttijd",
      "Input",
      "Output",
      "Totaal",
      "Voltooid",
      "Geblokkeerd",
      "Mislukt",
    ]) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      headRow.append(cell);
    }
    head.append(headRow);
    tableBody.id = "executionTelemetryRows";
    table.append(head, tableBody);
    scroll.append(table);
    panel.append(summary, description, scroll);
    const rate = $("rateLimits");
    rate?.insertAdjacentElement("afterend", panel);
    body = tableBody;
  }
  body.replaceChildren();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!row || typeof row !== "object") continue;
    const line = document.createElement("tr");
    for (const value of [
      telemetryDate(row.date),
      row.prompt_count,
      telemetryDuration(row.average_execution_seconds),
      telemetryDuration(row.average_queue_wait_seconds),
      row.input_tokens ?? "—",
      row.output_tokens ?? "—",
      row.total_tokens ?? "—",
      row.complete_count,
      row.blocked_count,
      row.failed_count,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "—");
      line.append(cell);
    }
    body.append(line);
  }
  if (!body.children.length) {
    const line = document.createElement("tr"),
      cell = document.createElement("td");
    cell.colSpan = 10;
    cell.className = "telemetry-empty";
    cell.textContent =
      "Nog geen voltooide Execution Host-telemetrie beschikbaar.";
    line.append(cell);
    body.append(line);
  }
}
const renderTelemetryInOrder = executionTelemetry;
executionTelemetry = (rows) => {
  renderTelemetryInOrder(rows);
  arrangeOperationalCategories();
  addCategoryIcons();
};
function updateFavicon() {
  $("dashboardFavicon").href = "/assets/engineering-status-icon.svg";
}
function renderDashboardTelemetry(snapshot) {
  updateFavicon();
  executionTelemetry(snapshot.telemetry);
}
updateFavicon();
function renderExecutionEvidence(snapshot) {
  lastExecutionTime(snapshot.last_executed_execution);
  lastRuntimeMetadata(snapshot.last_executed_runtime_metadata);
  reviewerAgents(snapshot.last_executed_reviewer_agents);
}
const independentLogSortStates = {
  inbox: { key: "timestamp", direction: "desc" },
  dashboard: { key: "timestamp", direction: "desc" },
};
function logComponentForTable(table) {
  return table.querySelector("#inboxComponentLog") ? "inbox" : "dashboard";
}
function updateIndependentLogSortHeaders() {
  document.querySelectorAll(".log-table").forEach((table) => {
    const state = independentLogSortStates[logComponentForTable(table)];
    table.querySelectorAll("th[data-sort-key]").forEach((header) => {
      const active = header.dataset.sortKey === state.key;
      header.dataset.sortIndicator = active
        ? state.direction === "asc"
          ? "↑"
          : "↓"
        : "↕";
      header.setAttribute(
        "aria-sort",
        active
          ? state.direction === "asc"
            ? "ascending"
            : "descending"
          : "none",
      );
    });
  });
}
function renderComponentLogs() {
  const needle = $("logFilter").value.trim().toLocaleLowerCase("nl-NL"),
    level = $("logLevelFilter").value;
  for (const component of ["inbox", "dashboard"]) {
    const state = independentLogSortStates[component],
      rows = componentLogEntries[component]
        .filter((entry) => !level || entry.level === level)
        .filter(
          (entry) =>
            !needle ||
            Object.values(entry)
              .join(" ")
              .toLocaleLowerCase("nl-NL")
              .includes(needle),
        )
        .sort((left, right) => {
          const first = logValue(left, state.key),
            second = logValue(right, state.key),
            result =
              typeof first === "number" && typeof second === "number"
                ? first - second
                : String(first).localeCompare(String(second), "nl");
          return state.direction === "asc" ? result : -result;
        }),
      body = $(component + "ComponentLog");
    body.replaceChildren();
    if (!rows.length) {
      const cell = document.createElement("td"),
        row = document.createElement("tr");
      cell.className = "log-empty";
      cell.colSpan = 6;
      cell.textContent = "Geen logregels voor deze selectie.";
      row.append(cell);
      body.append(row);
      continue;
    }
    for (const entry of rows) {
      const row = document.createElement("tr");
      for (const [name, value] of [
        ["log-line-number", entry.line],
        ["", logTimestampText(entry.timestamp)],
        [
          "log-level log-level--" +
            entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ", "-"),
          entry.level,
        ],
        ["", entry.event],
        ["", entry.runId || "—"],
        ["", entry.details || "—"],
      ]) {
        const cell = document.createElement("td");
        cell.className = name;
        cell.textContent = value;
        row.append(cell);
      }
      body.append(row);
    }
  }
  updateIndependentLogSortHeaders();
}
function setIndependentLogSort(component, key) {
  const state = independentLogSortStates[component];
  independentLogSortStates[component] =
    state.key === key
      ? { key: key, direction: state.direction === "asc" ? "desc" : "asc" }
      : { key: key, direction: key === "timestamp" ? "desc" : "asc" };
  renderComponentLogs();
}
document.querySelectorAll(".log-table").forEach((table) => {
  const component = logComponentForTable(table);
  table.querySelectorAll("th[data-sort-key]").forEach((header) => {
    const key = header.dataset.sortKey;
    header.addEventListener("click", () =>
      setIndependentLogSort(component, key),
    );
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setIndependentLogSort(component, key);
      }
    });
  });
});
updateIndependentLogSortHeaders();
const LOG_PAGE_SIZE = 50,
  independentLogPageStates = { inbox: 1, dashboard: 1 };
function filteredComponentLogEntries(component) {
  const needle = $("logFilter").value.trim().toLocaleLowerCase("nl-NL"),
    level = $("logLevelFilter").value,
    state = independentLogSortStates[component];
  return componentLogEntries[component]
    .filter((entry) => !level || entry.level === level)
    .filter(
      (entry) =>
        !needle ||
        Object.values(entry)
          .join(" ")
          .toLocaleLowerCase("nl-NL")
          .includes(needle),
    )
    .sort((left, right) => {
      const first = logValue(left, state.key),
        second = logValue(right, state.key),
        result =
          typeof first === "number" && typeof second === "number"
            ? first - second
            : String(first).localeCompare(String(second), "nl");
      return state.direction === "asc" ? result : -result;
    });
}
function renderLogPagination(component, total, pageCount) {
  const navigation = $(component + "LogPagination");
  navigation.replaceChildren();
  const summary = document.createElement("span"),
    previous = document.createElement("button"),
    next = document.createElement("button"),
    page = Math.min(
      Math.max(1, independentLogPageStates[component]),
      pageCount || 1,
    );
  independentLogPageStates[component] = page;
  summary.className = "log-pagination__summary";
  summary.textContent = total
    ? "Pagina " + page + " van " + pageCount + " · " + total + " regels"
    : "Geen logregels";
  previous.type = next.type = "button";
  previous.textContent = "Vorige";
  next.textContent = "Volgende";
  previous.disabled = page <= 1;
  next.disabled = page >= pageCount;
  previous.addEventListener("click", () => {
    independentLogPageStates[component] = page - 1;
    renderComponentLogs();
  });
  next.addEventListener("click", () => {
    independentLogPageStates[component] = page + 1;
    renderComponentLogs();
  });
  navigation.append(summary, previous, next);
}
function renderPaginatedComponentLogs() {
  for (const component of ["inbox", "dashboard"]) {
    const rows = filteredComponentLogEntries(component),
      body = $(component + "ComponentLog"),
      pageCount = Math.max(1, Math.ceil(rows.length / LOG_PAGE_SIZE)),
      page = Math.min(
        Math.max(1, independentLogPageStates[component]),
        pageCount,
      ),
      visible = rows.slice((page - 1) * LOG_PAGE_SIZE, page * LOG_PAGE_SIZE);
    independentLogPageStates[component] = page;
    body.replaceChildren();
    if (!visible.length) {
      const cell = document.createElement("td"),
        row = document.createElement("tr");
      cell.className = "log-empty";
      cell.colSpan = 6;
      cell.textContent = "Geen logregels voor deze selectie.";
      row.append(cell);
      body.append(row);
    } else
      for (const entry of visible) {
        const row = document.createElement("tr");
        for (const [name, value] of [
          ["log-line-number", entry.line],
          ["", logTimestampText(entry.timestamp)],
          [
            "log-level log-level--" +
              entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ", "-"),
            entry.level,
          ],
          ["", entry.event],
          ["", entry.runId || "—"],
          ["", entry.details || "—"],
        ]) {
          const cell = document.createElement("td");
          cell.className = name;
          cell.textContent = value;
          row.append(cell);
        }
        body.append(row);
      }
    renderLogPagination(component, rows.length, pageCount);
  }
  updateIndependentLogSortHeaders();
}
const setIndependentLogSortWithPagination = setIndependentLogSort;
setIndependentLogSort = (component, key) => {
  independentLogPageStates[component] = 1;
  setIndependentLogSortWithPagination(component, key);
  renderPaginatedComponentLogs();
};
const renderComponentLogsWithPagination = renderComponentLogs;
renderComponentLogs = () => renderPaginatedComponentLogs();
$("logFilter").addEventListener("input", () => {
  independentLogPageStates.inbox = independentLogPageStates.dashboard = 1;
  renderComponentLogs();
});
$("logLevelFilter").addEventListener("change", () => {
  independentLogPageStates.inbox = independentLogPageStates.dashboard = 1;
  renderComponentLogs();
});
renderComponentLogs();
function clearComponentLog(component, button) {
  const name =
    component === "inbox" ? "Engineering Execution Host" : "Statusdashboard";
  confirmDashboardAction(
    "Logs wissen",
    "Wis de applicatielogs van " +
      name +
      "? Dit kan niet ongedaan worden gemaakt.",
    "Logs wissen",
    "#f0b66a",
  ).then(async (confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    try {
      const response = await fetch(
        "/api/logs/" + encodeURIComponent(component),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw Error(payload.error || "Logs wissen is niet gelukt.");
      }
      componentLogEntries[component] = structuredLogEntries(
        await fetch("/api/logs/" + encodeURIComponent(component)).then(
          (response) => response.text(),
        ),
      );
      componentLogVersion = "";
      renderComponentLogs();
    } catch {
      button.title = "Logs wissen is niet gelukt.";
    } finally {
      button.disabled = false;
    }
  });
}
document
  .querySelectorAll(".clear-component-log")
  .forEach((button) =>
    button.addEventListener("click", () =>
      clearComponentLog(button.dataset.component, button),
    ),
  );
function downloadComponentLog(component) {
  const names = { inbox: "inbox-watcher", dashboard: "statusdashboard" },
    name = names[component];
  if (!name) return Promise.reject(Error("Onbekend logonderdeel."));
  return fetch("/api/logs/" + encodeURIComponent(component), {
    cache: "no-store",
  })
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(Error("Logdownload is niet beschikbaar.")),
    )
    .then((text) => {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-"),
        link = document.createElement("a"),
        url = URL.createObjectURL(
          new Blob([text], { type: "application/x-ndjson;charset=utf-8" }),
        );
      link.href = url;
      link.download = name + "-log-" + stamp + ".ndjson";
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      return recordUserAction("component_log_downloaded");
    });
}
document.querySelectorAll(".component-log-download").forEach((button) =>
  button.addEventListener("click", () =>
    downloadComponentLog(button.dataset.component).catch(() => {
      button.title = "Logdownload is niet beschikbaar.";
    }),
  ),
);
document.querySelectorAll(".clear-component-log").forEach((button) => {
  button.classList.add("clear-component-log--glyph");
  button.textContent = "⌫";
  button.title = "Wis log";
  button.setAttribute("aria-label", "Wis log");
});
let pullRefreshStart = null,
  pullRefreshDistance = 0;
const pullRefresh = $("pullRefresh");
function updatePullRefresh(distance) {
  pullRefreshDistance = Math.max(0, Math.min(distance, 112));
  const ready = pullRefreshDistance >= 72;
  pullRefresh.classList.toggle(
    "pull-refresh--visible",
    pullRefreshDistance > 8,
  );
  pullRefresh.textContent = ready
    ? "Laat los om te vernieuwen"
    : "Trek omlaag om te vernieuwen";
  pullRefresh.setAttribute("aria-hidden", String(pullRefreshDistance <= 8));
}
function startPullRefresh(event) {
  if (window.scrollY > 0 || event.touches.length !== 1) return;
  const target = event.target;
  if (
    target instanceof Element &&
    target.closest("input,textarea,select,button,[contenteditable=true]")
  )
    return;
  pullRefreshStart = event.touches[0].clientY;
  pullRefreshDistance = 0;
}
function movePullRefresh(event) {
  if (pullRefreshStart === null || event.touches.length !== 1) return;
  const distance = event.touches[0].clientY - pullRefreshStart;
  if (distance <= 0) {
    updatePullRefresh(0);
    return;
  }
  event.preventDefault();
  updatePullRefresh(distance);
}
function endPullRefresh() {
  const refresh = pullRefreshDistance >= 72;
  pullRefreshStart = null;
  updatePullRefresh(0);
  if (refresh) {
    pullRefresh.textContent = "Dashboard vernieuwen…";
    pullRefresh.classList.add("pull-refresh--visible");
    pullRefresh.setAttribute("aria-hidden", "false");
    window.location.reload();
  }
}
document.addEventListener("touchstart", startPullRefresh, { passive: true });
document.addEventListener("touchmove", movePullRefresh, { passive: false });
document.addEventListener("touchend", endPullRefresh, { passive: true });
document.addEventListener("touchcancel", endPullRefresh, { passive: true });
function hideDashboardSplash() {
  const splash = $("dashboardSplash");
  if (!splash || document.body.classList.contains("dashboard-ready")) return;
  document.body.classList.add("dashboard-ready");
  splash.setAttribute("aria-hidden", "true");
  setTimeout(() => {
    splash.hidden = true;
  }, 260);
}
setTimeout(hideDashboardSplash, 8e3);
const PROMPT_HISTORY_PAGE_SIZE = 25;
let promptHistoryEntries = [],
  promptHistoryPage = 1,
  promptHistorySort = { key: "executed_at", direction: "desc" };
function promptHistoryValue(entry, key) {
  const value = entry?.[key];
  if (key === "executed_at") {
    const parsed = Date.parse(String(value || ""));
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return String(value || "");
}
function filteredPromptHistory() {
  const needle = $("promptHistoryFilter")
    .value.trim()
    .toLocaleLowerCase("nl-NL");
  return promptHistoryEntries
    .filter(
      (entry) =>
        !needle ||
        Object.values(entry)
          .join(" ")
          .toLocaleLowerCase("nl-NL")
          .includes(needle),
    )
    .sort((left, right) => {
      const first = promptHistoryValue(left, promptHistorySort.key),
        second = promptHistoryValue(right, promptHistorySort.key),
        result =
          typeof first === "number" && typeof second === "number"
            ? first - second
            : String(first).localeCompare(String(second), "nl");
      return promptHistorySort.direction === "asc" ? result : -result;
    });
}
function promptHistoryStatus(value) {
  return (
    { COMPLETE: "Voltooid", BLOCKED: "Geblokkeerd", FAILED: "Mislukt" }[
      value
    ] || "Onbekend"
  );
}
function updatePromptHistoryHeaders() {
  document
    .querySelectorAll("#promptHistory th[data-history-sort-key]")
    .forEach((header) => {
      const active = header.dataset.historySortKey === promptHistorySort.key;
      header.classList.add("log-sortable");
      header.tabIndex = 0;
      header.setAttribute("role", "button");
      header.setAttribute(
        "aria-sort",
        active
          ? promptHistorySort.direction === "asc"
            ? "ascending"
            : "descending"
          : "none",
      );
      header.dataset.sortIndicator = active
        ? promptHistorySort.direction === "asc"
          ? "↑"
          : "↓"
        : "↕";
    });
}
function renderPromptHistory() {
  const rows = filteredPromptHistory(),
    body = $("promptHistoryRows"),
    navigation = $("promptHistoryPagination"),
    pageCount = Math.max(1, Math.ceil(rows.length / PROMPT_HISTORY_PAGE_SIZE));
  promptHistoryPage = Math.min(Math.max(1, promptHistoryPage), pageCount);
  body.replaceChildren();
  const visible = rows.slice(
    (promptHistoryPage - 1) * PROMPT_HISTORY_PAGE_SIZE,
    promptHistoryPage * PROMPT_HISTORY_PAGE_SIZE,
  );
  if (!visible.length) {
    const row = document.createElement("tr"),
      cell = document.createElement("td");
    cell.className = "log-empty";
    cell.colSpan = 6;
    cell.textContent = "Geen prompts in de geschiedenis voor deze selectie.";
    row.append(cell);
    body.append(row);
  } else
    for (const entry of visible) {
      const row = document.createElement("tr"),
        status = document.createElement("td"),
        title = document.createElement("td"),
        executed = document.createElement("td"),
        commit = document.createElement("td"),
        report = document.createElement("td"),
        action = document.createElement("td"),
        timestamp = Date.parse(String(entry.executed_at || ""));
      status.className =
        "prompt-history-status prompt-history-status--" +
        String(entry.status || "").toLocaleLowerCase("nl-NL");
      status.textContent = promptHistoryStatus(entry.status);
      title.textContent = String(
        entry.title || entry.run_id || "Prompttitel niet beschikbaar",
      );
      executed.textContent = Number.isFinite(timestamp)
        ? formatTime.format(new Date(timestamp))
        : String(entry.executed_at || "Tijdstip niet beschikbaar");
      commit.textContent = entry.git_commit || "—";
      if (entry.report_available && entry.run_id) {
        const link = document.createElement("a");
        link.className = "prompt-history-report";
        link.href =
          "/api/prompt-history/" + encodeURIComponent(entry.run_id) + "/report";
        link.download = "engineering-report-" + entry.run_id + ".md";
        link.title = "Download engineeringrapport";
        link.setAttribute(
          "aria-label",
          "Download engineeringrapport voor " + title.textContent,
        );
        link.textContent = "⇩";
        report.append(link);
      } else report.textContent = "—";
      if (entry.status === "BLOCKED" && entry.run_id) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "predecessor-retry";
        retry.textContent = "Retry Execution";
        retry.addEventListener("click", () => submitExecutionRetry(entry));
        action.append(retry);
      } else action.textContent = "—";
      row.append(status, title, executed, commit, report, action);
      body.append(row);
    }
  navigation.replaceChildren();
  const summary = document.createElement("span"),
    previous = document.createElement("button"),
    next = document.createElement("button");
  summary.className = "log-pagination__summary";
  summary.textContent = rows.length
    ? "Pagina " +
      promptHistoryPage +
      " van " +
      pageCount +
      " · " +
      rows.length +
      " prompts"
    : "Geen prompts";
  previous.type = next.type = "button";
  previous.textContent = "Vorige";
  next.textContent = "Volgende";
  previous.disabled = promptHistoryPage <= 1;
  next.disabled = promptHistoryPage >= pageCount;
  previous.addEventListener("click", () => {
    promptHistoryPage--;
    renderPromptHistory();
  });
  next.addEventListener("click", () => {
    promptHistoryPage++;
    renderPromptHistory();
  });
  navigation.append(summary, previous, next);
  updatePromptHistoryHeaders();
}
function refreshPromptHistory() {
  return fetch("/api/prompt-history")
    .then((response) => (response.ok ? response.json() : Promise.reject()))
    .then((payload) => {
      promptHistoryEntries = Array.isArray(payload?.runs) ? payload.runs : [];
      renderPromptHistory();
    })
    .catch(() => {
      promptHistoryEntries = [];
      renderPromptHistory();
    });
}
$("promptHistoryFilter").addEventListener("input", () => {
  promptHistoryPage = 1;
  renderPromptHistory();
});
document
  .querySelectorAll("#promptHistory th[data-history-sort-key]")
  .forEach((header) => {
    const sort = () => {
      const key = header.dataset.historySortKey;
      if (promptHistorySort.key === key)
        promptHistorySort.direction =
          promptHistorySort.direction === "asc" ? "desc" : "asc";
      else promptHistorySort = { key: key, direction: "asc" };
      promptHistoryPage = 1;
      renderPromptHistory();
    };
    header.addEventListener("click", sort);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sort();
      }
    });
  });
refreshPromptHistory();
const parseStructuredLogEntries = structuredLogEntries;
structuredLogEntries = (text) => {
  const normalized = String(text ?? "").trim();
  return !normalized || normalized === "Nog geen applicatielog beschikbaar."
    ? []
    : parseStructuredLogEntries(normalized);
};
renderPaginatedComponentLogs = () => {
  for (const component of ["inbox", "dashboard"]) {
    const entries = componentLogEntries[component],
      rows = filteredComponentLogEntries(component),
      body = $(component + "ComponentLog"),
      pageCount = Math.max(1, Math.ceil(rows.length / LOG_PAGE_SIZE)),
      page = Math.min(
        Math.max(1, independentLogPageStates[component]),
        pageCount,
      ),
      visible = rows.slice((page - 1) * LOG_PAGE_SIZE, page * LOG_PAGE_SIZE);
    independentLogPageStates[component] = page;
    body.replaceChildren();
    if (!visible.length) {
      const cell = document.createElement("td"),
        row = document.createElement("tr");
      cell.className = "log-empty";
      cell.colSpan = 6;
      cell.textContent = entries.length
        ? "Geen logregels voor deze selectie."
        : "Nog geen applicatielog beschikbaar.";
      row.append(cell);
      body.append(row);
    } else
      for (const entry of visible) {
        const row = document.createElement("tr");
        for (const [name, value] of [
          ["log-line-number", entry.line],
          ["", logTimestampText(entry.timestamp)],
          [
            "log-level log-level--" +
              entry.level.toLocaleLowerCase("nl-NL").replaceAll(" ", "-"),
            entry.level,
          ],
          ["", entry.event],
          ["", entry.runId || "—"],
          ["", entry.details || "—"],
        ]) {
          const cell = document.createElement("td");
          cell.className = name;
          cell.textContent = value;
          row.append(cell);
        }
        body.append(row);
      }
    renderLogPagination(component, rows.length, pageCount);
  }
  updateIndependentLogSortHeaders();
};
renderComponentLogs();
const DASHBOARD_CLIENT_STATE_KEY = "engineering-dashboard-client-state-v1",
  ALL_SECTIONS_STATE_KEY = "engineering-dashboard-all-sections-open-v1";
function loadDashboardClientState() {
  try {
    const stored = JSON.parse(
      localStorage.getItem(DASHBOARD_CLIENT_STATE_KEY) || "{}",
    );
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}
const dashboardClientState = loadDashboardClientState();
function loadAllSectionsIntent() {
  try {
    const stored = localStorage.getItem(ALL_SECTIONS_STATE_KEY);
    if (stored === "true" || stored === "false") return stored === "true";
  } catch {}
  return typeof dashboardClientState.allSectionsOpen === "boolean"
    ? dashboardClientState.allSectionsOpen
    : null;
}
let allSectionsIntent = loadAllSectionsIntent();
function saveAllSectionsIntent(open) {
  allSectionsIntent = open;
  dashboardClientState.allSectionsOpen = open;
  try {
    localStorage.setItem(ALL_SECTIONS_STATE_KEY, String(open));
  } catch {}
}
function clearAllSectionsIntent() {
  allSectionsIntent = null;
  delete dashboardClientState.allSectionsOpen;
  try {
    localStorage.removeItem(ALL_SECTIONS_STATE_KEY);
  } catch {}
}
function saveDashboardClientState() {
  try {
    localStorage.setItem(
      DASHBOARD_CLIENT_STATE_KEY,
      JSON.stringify(dashboardClientState),
    );
  } catch {}
}
function restoreDashboardDetails(root = document) {
  const details = dashboardClientState.details || {};
  root.querySelectorAll?.("details[id]").forEach((element) => {
    if (typeof allSectionsIntent === "boolean")
      element.open = allSectionsIntent;
    else if (Object.hasOwn(details, element.id))
      element.open = Boolean(details[element.id]);
  });
}
const autoRefreshToggle = $("autoRefresh"),
  allSectionsToggle = $("toggleAllSections"),
  dashboardCategoryIds = [
    "workspaceCard",
    "queueItems",
    "currentRun",
    "rateLimits",
    "executionTelemetry",
    "lastExecutionGroup",
    "platformHealth",
    "codexChat",
    "technicalDetails",
    "componentLogs",
  ];
function visibleDashboardCategories() {
  return dashboardCategories().filter(
    (element) => !element.hidden && !element.closest("[hidden]"),
  );
}
function dashboardCategories() {
  return dashboardCategoryIds
    .map((id) => $(id))
    .filter(
      (element) => element instanceof HTMLDetailsElement,
    );
}
function updateAllSectionsToggle() {
  const categories = visibleDashboardCategories(),
    allOpen =
      typeof allSectionsIntent === "boolean"
        ? allSectionsIntent
        : categories.length > 0 && categories.every((category) => category.open);
  allSectionsToggle.setAttribute("aria-checked", String(allOpen));
  allSectionsToggle.setAttribute(
    "aria-label",
    allOpen ? "Alle secties sluiten" : "Alle secties openen",
  );
  allSectionsToggle.title = allOpen ? "Alles sluiten" : "Alles openen";
}
function setAllSections(open) {
  const details = { ...(dashboardClientState.details || {}) };
  for (const category of dashboardCategories()) {
    category.open = open;
    details[category.id] = open;
  }
  dashboardClientState.details = details;
  saveAllSectionsIntent(open);
  saveDashboardClientState();
  updateAllSectionsToggle();
}
allSectionsToggle.addEventListener("click", () =>
  setAllSections(allSectionsToggle.getAttribute("aria-checked") !== "true"),
);
autoRefreshToggle.checked = dashboardClientState.autoRefresh !== false;
autoRefreshToggle.addEventListener("change", () => {
  dashboardClientState.autoRefresh = autoRefreshToggle.checked;
  saveDashboardClientState();
  $("updateMode").textContent = autoRefreshToggle.checked
    ? "Serverpush: verbonden"
    : "Automatisch vernieuwen is uit";
});
document.addEventListener(
  "toggle",
  (event) => {
    const element = event.target;
    if (element instanceof HTMLDetailsElement && element.id) {
      dashboardClientState.details = {
        ...(dashboardClientState.details || {}),
        [element.id]: element.open,
      };
      saveDashboardClientState();
      updateAllSectionsToggle();
    }
  },
  true,
);
function clearAllSectionsIntentFromManualToggle(event) {
  const summary = event.target.closest?.("details[id] > summary");
  if (!summary) return;
  clearAllSectionsIntent();
  saveDashboardClientState();
}
document.addEventListener("click", clearAllSectionsIntentFromManualToggle);
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ")
    clearAllSectionsIntentFromManualToggle(event);
});

for (const component of ["inbox", "dashboard"]) {
  const saved = dashboardClientState.logSorts?.[component];
  if (
    saved &&
    ["line", "timestamp", "level", "event", "runId", "details"].includes(
      saved.key,
    ) &&
    ["asc", "desc"].includes(saved.direction)
  )
    independentLogSortStates[component] = saved;
}
document.addEventListener("click", (event) => {
  if (event.target.closest(".log-table th[data-sort-key]"))
    setTimeout(() => {
      dashboardClientState.logSorts = structuredClone(independentLogSortStates);
      saveDashboardClientState();
    }, 0);
});
restoreDashboardDetails();
new MutationObserver((records) => {
  for (const record of records)
    for (const node of record.addedNodes)
      if (node instanceof Element) restoreDashboardDetails(node);
  updateAllSectionsToggle();
}).observe($("engineering-dashboard-content"), {
  childList: true,
  subtree: true,
});
updateAllSectionsToggle();
updateIndependentLogSortHeaders();
function chatHistoryMarkdown() {
  const entries = chatHistory
    .map(
      (entry) =>
        "## " +
        (entry.role === "user" ? "Jij" : "AI-assistent") +
        "\n\n" +
        entry.text.trim(),
    )
    .filter(Boolean);
  return [
    "# AI-gesprek",
    "",
    "Model: " + $("chatModel").textContent.trim(),
    "",
    ...entries,
  ].join("\n\n");
}
function updateChatDownloadAvailability() {
  const button = $("downloadChat");
  if (button) button.hidden = chatHistory.length === 0;
}
function downloadChatHistory() {
  if (!chatHistory.length) return;
  const url = URL.createObjectURL(
      new Blob([chatHistoryMarkdown()], {
        type: "text/markdown;charset=utf-8",
      }),
    ),
    link = document.createElement("a");
  link.href = url;
  link.download =
    "ai-gesprek-" + new Date().toISOString().replace(/[:.]/g, "-") + ".md";
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
const renderChatHistoryWithDownload = renderChatHistory;
renderChatHistory = () => {
  renderChatHistoryWithDownload();
  updateChatDownloadAvailability();
};
const chatMessageWithDownload = chatMessage;
chatMessage = (role, text) => {
  chatMessageWithDownload(role, text);
  updateChatDownloadAvailability();
};
$("downloadChat").addEventListener("click", downloadChatHistory);
updateChatDownloadAvailability();
const promptHistoryCategory = $("promptHistory");
if (
  promptHistoryCategory &&
  Object.hasOwn(dashboardClientState.details || {}, "promptHistory")
)
  promptHistoryCategory.open = Boolean(
    dashboardClientState.details.promptHistory,
  );
dashboardCategoryIds.splice(2, 0, "promptHistory");
if (typeof allSectionsIntent === "boolean") setAllSections(allSectionsIntent);
updateAllSectionsToggle();
const themeToggle = $("themeToggle"),
  themeColor = $("dashboardThemeColor");
function applyDashboardTheme(theme) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  themeColor.content = light ? "#f4f7fb" : "#15151d";
  themeToggle.setAttribute("aria-checked", String(light));
  themeToggle.setAttribute(
    "aria-label",
    light ? "Donkere modus inschakelen" : "Lichte modus inschakelen",
  );
  themeToggle.title = light ? "Donkere modus" : "Lichte modus";
}
applyDashboardTheme(dashboardClientState.theme === "light" ? "light" : "dark");
themeToggle.addEventListener("click", () => {
  dashboardClientState.theme =
    document.documentElement.dataset.theme === "light" ? "dark" : "light";
  saveDashboardClientState();
  applyDashboardTheme(dashboardClientState.theme);
});
function applyThemeModeAttributes(root = document.body) {
  const theme =
      document.documentElement.dataset.theme === "light" ? "light" : "dark",
    elements = [];
  if (root instanceof Element) elements.push(root);
  if (root?.querySelectorAll) elements.push(...root.querySelectorAll("*"));
  for (const element of elements)
    if (!["SCRIPT", "STYLE"].includes(element.tagName))
      element.dataset.themeMode = theme;
}
const applyDashboardThemeWithElementAttributes = applyDashboardTheme;
applyDashboardTheme = (theme) => {
  applyDashboardThemeWithElementAttributes(theme);
  applyThemeModeAttributes();
};
applyThemeModeAttributes();
new MutationObserver((records) => {
  for (const record of records)
    for (const node of record.addedNodes)
      if (node instanceof Element) applyThemeModeAttributes(node);
}).observe(document.body, { childList: true, subtree: true });
$("rateLimitProvider")?.previousElementSibling?.replaceChildren(
  "Huidige AI-provider",
);
let latestPlatformHealthPayload = null;
const restartingPlatformComponents = new Set();
const renderPlatformHealthWithRestartState = renderPlatformHealth;
renderPlatformHealth = (payload) => {
  latestPlatformHealthPayload = payload;
  const components =
    payload && typeof payload.components === "object"
      ? Object.fromEntries(
          Object.entries(payload.components).map(([key, component]) =>
            restartingPlatformComponents.has(key)
              ? [
                  key,
                  {
                    ...component,
                    healthy: false,
                    state: "restarting",
                    detail: "Herstart wordt uitgevoerd",
                  },
                ]
              : [key, component],
          ),
        )
      : null;
  return renderPlatformHealthWithRestartState(
    components ? { ...payload, components: components } : payload,
  );
};
async function confirmComponentRestart(component) {
  for (let attempt = 0; attempt < 5; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 1250));
    try {
      const response = await fetch("/health", { cache: "no-store" }),
        payload = await response.json();
      if (response.ok && payload?.components?.[component]?.healthy) {
        restartingPlatformComponents.delete(component);
        renderPlatformHealth(payload);
        $("componentModalStatus").textContent =
          "Component is opnieuw beschikbaar.";
        return;
      }
      renderPlatformHealth(payload);
    } catch {}
  }
  $("componentModalStatus").textContent =
    "De component komt nog niet gezond terug; controle loopt door.";
}
const formatComponentUptimeForMeasuredValues = formatComponentUptime;
formatComponentUptime = (value) => {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0
    ? formatComponentUptimeForMeasuredValues(value)
    : "";
};
function recordUserAction(action) {
  return fetch("/api/audit/user-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: action }),
  }).catch(() => undefined);
}
function downloadLegacyLastExecutedDocument(endpoint, filenamePrefix) {
  if (!lastExecutedRun)
    return Promise.reject(Error("Geen uitgevoerde prompt beschikbaar."));
  const separator = endpoint.includes("?") ? "&" : "?";
  return fetch(
    endpoint +
      separator +
      "run_id=" +
      encodeURIComponent(lastExecutedRun) +
      "&audit=download",
  )
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(Error("Download is niet beschikbaar.")),
    )
    .then((text) => {
      if (!text) throw Error("Download is niet beschikbaar.");
      const link = document.createElement("a"),
        url = URL.createObjectURL(
          new Blob([text], { type: "text/markdown;charset=utf-8" }),
        ),
        safeRun = String(lastExecutedRun).replace(/[^a-z0-9._-]+/gi, "-");
      link.href = url;
      link.download = filenamePrefix + "-" + safeRun + ".md";
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    });
}
$("downloadChat")?.addEventListener(
  "click",
  () => void recordUserAction("chat_downloaded"),
);
$("copyReport")?.addEventListener(
  "click",
  () => void recordUserAction("report_copied"),
);
$("copyReportAnalysis")?.addEventListener(
  "click",
  () => void recordUserAction("report_analysis_copied"),
);
let promptHistoryReportText = "",
  promptHistoryReportRun = "";
function promptHistoryReportFilename() {
  return (
    "engineering-report-" +
    String(promptHistoryReportRun || "unknown").replace(
      /[^a-z0-9._-]+/gi,
      "-",
    ) +
    ".md"
  );
}
function closePromptHistoryReport() {
  const modal = $("promptHistoryReportModal");
  if (modal.open) modal.close();
}
function downloadPromptHistoryReport() {
  if (!promptHistoryReportText) return;
  const url = URL.createObjectURL(
      new Blob([promptHistoryReportText], {
        type: "text/markdown;charset=utf-8",
      }),
    ),
    link = document.createElement("a");
  link.href = url;
  link.download = promptHistoryReportFilename();
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  void recordUserAction("prompt_history_report_downloaded");
}
function openPromptHistoryReport(runId, title) {
  const modal = $("promptHistoryReportModal"),
    content = $("promptHistoryReportContent");
  promptHistoryReportRun = String(runId || "");
  promptHistoryReportText = "";
  $("promptHistoryReportModalTitle").textContent =
    title || "Engineeringrapport";
  $("promptHistoryReportCopy").hidden = true;
  $("promptHistoryReportDownload").hidden = true;
  content.replaceChildren();
  content.textContent = "Rapport laden…";
  if (!modal.open) modal.showModal();
  modal.focus();
  fetch(
    "/api/prompt-history/" +
      encodeURIComponent(promptHistoryReportRun) +
      "/report",
    { cache: "no-store" },
  )
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(Error("Rapport is niet beschikbaar.")),
    )
    .then((text) => {
      if (!text) throw Error("Rapport is niet beschikbaar.");
      promptHistoryReportText = text;
      renderMarkdownDocument(content, text);
      $("promptHistoryReportCopy").hidden = false;
      $("promptHistoryReportDownload").hidden = false;
    })
    .catch(() => {
      content.textContent =
        "Engineeringrapport is niet beschikbaar voor deze prompt.";
    });
}
$("promptHistoryReportClose").addEventListener(
  "click",
  closePromptHistoryReport,
);
$("promptHistoryReportModal").addEventListener("click", (event) => {
  if (event.target === $("promptHistoryReportModal"))
    closePromptHistoryReport();
});
$("promptHistoryReportCopy").addEventListener("click", () => {
  if (promptHistoryReportText)
    copyText(promptHistoryReportText).then(
      () => void recordUserAction("prompt_history_report_copied"),
    );
});
$("promptHistoryReportDownload").addEventListener(
  "click",
  downloadPromptHistoryReport,
);
const renderPromptHistoryWithReportView = renderPromptHistory;
renderPromptHistory = () => {
  renderPromptHistoryWithReportView();
  document
    .querySelectorAll("#promptHistoryRows a.prompt-history-report")
    .forEach((link) => {
      const button = document.createElement("button");
      button.className = "prompt-history-report";
      button.type = "button";
      button.title = "Bekijk engineeringrapport";
      button.setAttribute(
        "aria-label",
        "Bekijk engineeringrapport voor " +
          (link.getAttribute("aria-label") || "deze prompt").replace(
            "Download engineeringrapport voor ",
            "",
          ),
      );
      button.textContent = "▤";
      button.addEventListener("click", () =>
        openPromptHistoryReport(
          link.href.split("/report")[0].split("/").pop(),
          button
            .getAttribute("aria-label")
            .replace("Bekijk engineeringrapport voor ", ""),
        ),
      );
      link.replaceWith(button);
    });
};
function renderPredecessorRetry(x) {
  const blocked = Boolean(x && x.blocking_predecessor_run),
    button = $("predecessorRetry"),
    status = $("predecessorRetryStatus");
  button.hidden = !blocked;
  button.disabled = isActiveRun(x || {});
  if (!blocked) status.textContent = "";
}
function submitPredecessorRetry() {
  const button = $("predecessorRetry"),
    status = $("predecessorRetryStatus"),
    run = latestStatus?.blocking_predecessor_run;
  if (!run || button.disabled) return;
  confirmDashboardAction(
    "Resume Queue",
    "Deze queue recovery herstelt de wachtende Inbox-volgorde. De oorspronkelijke geblokkeerde uitvoering blijft onveranderd.",
    "Resume Queue",
    "#f0b66a",
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    status.textContent = "Queue recovery wordt klaargezet…";
    fetch("/api/queue-recovery", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => ({
        ok: response.ok,
        body: await response.json(),
      }))
      .then((result) => {
        if (!result.ok)
          throw Error(
            result.body.error || "Queue recovery kon niet worden gestart.",
          );
        status.textContent =
          "Queue recovery staat klaar; de watcher hervat na de vervangende uitvoering.";
      })
      .catch((error) => {
        status.textContent =
          error.message || "Queue recovery kon niet worden gestart.";
      })
      .finally(() => {
        button.disabled = false;
      });
  });
}
function submitExecutionRetry(entry) {
  if (!entry?.run_id) return;
  const title = String(entry.title || "Prompttitel niet beschikbaar");
  const repository = String(entry.repository || "repository context not recorded");
  const mode = String(entry.execution_mode || "execution mode not recorded");
  confirmDashboardAction(
    "Retry Execution",
    "Run ID: " + entry.run_id + "\nPrompt title: " + title + "\nRepository: " + repository + "\nExecution Mode: " + mode + "\n\nA new engineering execution will start using the current repository state. The original execution remains unchanged. A Retry relationship will be recorded.",
    "Retry Execution",
    "#f0b66a",
  ).then((confirmed) => {
    if (!confirmed) return;
    fetch("/api/execution-retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: entry.run_id }),
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || "Retry Execution kon niet worden gestart.");
        return refreshPromptHistory();
      })
      .catch((error) => window.alert(error.message || "Retry Execution kon niet worden gestart."));
  });
}
$("predecessorRetry").addEventListener("click", submitPredecessorRetry);
function confirmDashboardAction(title, text, confirmLabel, color = "#c7a6ff") {
  const modal = $("confirmationModal"),
    heading = $("confirmationModalTitle"),
    body = $("confirmationModalText"),
    cancel = $("confirmationModalCancel"),
    confirm = $("confirmationModalConfirm");
  heading.textContent = title;
  body.textContent = text;
  confirm.textContent = confirmLabel;
  modal.style.setProperty("--confirmation-color", color);
  return new Promise((resolve) => {
    const finish = (value) => {
      modal.close();
      cancel.onclick = confirm.onclick = null;
      resolve(value);
    };
    cancel.onclick = () => finish(false);
    confirm.onclick = () => finish(true);
    modal.addEventListener(
      "cancel",
      (event) => {
        event.preventDefault();
        finish(false);
      },
      { once: true },
    );
    modal.showModal();
    modal.focus();
  });
}
function updateChatActions() {
  const visible = chatHistory.length > 0;
  $("downloadChat").hidden = !visible;
  $("clearChat").hidden = !visible;
}
$("clearChat").addEventListener("click", () =>
  confirmDashboardAction(
    "Chat wissen",
    "Dit wist alleen de lokale chatweergave. Promptgeschiedenis en rapporten blijven behouden.",
    "Chat wissen",
    "#d0a4ff",
  ).then((confirmed) => {
    if (!confirmed) return;
    chatHistory = [];
    sessionStorage.removeItem(CHAT_HISTORY_KEY);
    renderChatHistory();
    updateChatActions();
  }),
);
const updateChatDownloadWithClear = updateChatDownloadAvailability;
updateChatDownloadAvailability = () => {
  updateChatDownloadWithClear();
  updateChatActions();
};
healthComponentLabels.inbox_watcher = "Engineering Execution Host";
function showDashboardReloadSplash() {
  const splash = $("dashboardSplash");
  splash.hidden = false;
  document.body.classList.remove("dashboard-ready");
}
async function restartPlatformComponent(button) {
  const component = button.dataset.component;
  if (!component) return;
  const confirmed = await confirmDashboardAction(
    "Component herstarten",
    "Herstart " + (healthComponentLabels[component] || "dit onderdeel") + "?",
    "Herstarten",
    "#a3e635",
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    const response = await fetch(
        "/api/components/" + encodeURIComponent(component) + "/restart",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      ),
      payload = await response.json();
    if (!response.ok)
      throw Error(payload.error || "Herstarten is niet gelukt.");
    if (component === "dashboard") {
      $("componentModalStatus").textContent =
        "Statusdashboard wordt opnieuw geladen…";
      showDashboardReloadSplash();
      window.setTimeout(() => window.location.reload(), 750);
      return;
    }
    $("componentModalStatus").textContent = "Herstartverzoek verzonden.";
  } catch (error) {
    $("componentModalStatus").textContent =
      error.message || "Herstarten is niet gelukt.";
  } finally {
    button.disabled = false;
  }
}
function legacyConfirmation() {
  return false;
}
function legacyDashboardError() {
  const status = $("componentModalStatus");
  if (status) status.textContent = "Componentinformatie is niet beschikbaar.";
}
const dashboardActionHandlers = {
  rateLimitReset: (button) => consumeRateLimitReset(button),
  predecessorRetry: (button) => submitPredecessorRetry(button),
  componentModalRestart: (button) => restartPlatformComponent(button),
};
document.addEventListener(
  "click",
  (event) => {
    const button = event.target.closest("button");
    const handler = button && dashboardActionHandlers[button.id];
    if (!handler) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    handler(button);
  },
  true,
);

// The dashboard's browser tests exercise these public presentation operations
// directly. Application state remains private to the status store.
Object.assign(window, {
  applyDashboardTheme,
  chatHistoryMarkdown,
  chatMessage,
  executionTelemetry,
  lastExecutionTime,
  lastRuntimeMetadata,
  queueItems,
  r,
  rateLimits,
  refreshOpenComponentDetails,
  renderChatHistory,
  renderComponentLogs,
  renderLogPagination,
  renderMarkdownAnswer,
  renderPlatformHealth,
  renderPromptHistory,
  showComponentModal,
  showCopyToast,
  structuredLogEntries,
  updatePullRefresh,
});
for (const binding of [
  ["chatHistory", () => chatHistory, (value) => (chatHistory = value)],
  ["componentDetailsRefreshTimer", () => componentDetailsRefreshTimer],
  [
    "componentLogsLoaded",
    () => componentLogsLoaded,
    (value) => (componentLogsLoaded = value),
  ],
  ["componentLogEntries", () => componentLogEntries],
  ["independentLogPageStates", () => independentLogPageStates],
  [
    "promptHistoryEntries",
    () => promptHistoryEntries,
    (value) => (promptHistoryEntries = value),
  ],
  [
    "promptHistoryPage",
    () => promptHistoryPage,
    (value) => (promptHistoryPage = value),
  ],
  [
    "refreshComponentLogs",
    () => refreshComponentLogs,
    (value) => (refreshComponentLogs = value),
  ],
]) {
  Object.defineProperty(window, binding[0], {
    get: binding[1],
    set: binding[2],
  });
}
