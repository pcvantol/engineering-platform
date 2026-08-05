import { createDashboardStatusStore } from "./dashboard_status_store.mjs";
import { createLocaleService, normalizeLocale, preferredLocale } from "./dashboard_locales.mjs";

function initialDashboardLocale() {
  try {
    return preferredLocale(JSON.parse(localStorage.getItem("engineering-dashboard-client-state-v1") || "{}"));
  } catch {
    return preferredLocale({});
  }
}
let dashboardLocale = initialDashboardLocale(), locale = createLocaleService(dashboardLocale);
const localizationCalls = new Map();
function t(key, values = {}, fallback = key) {
  const text = locale.t(key, values, fallback);
  localizationCalls.set(JSON.stringify([key, values, fallback]), {
    key: String(key),
    values,
    fallback,
    text,
  });
  return text;
}
// Kept deliberately read-only for the browser regression suite.  It makes
// every dashboard copy lookup auditable without adding a second translation
// path or relying on a hand-maintained list of visible labels.
window.__djconnectDashboardLocalizationCalls = () => [...localizationCalls.values()];
document.documentElement.lang = dashboardLocale;

const $ = (id) => document.getElementById(id),
  DASHBOARD_BUILD = window.DJCONNECT_DASHBOARD_BUILD || "",
  DASHBOARD_BUILD_KEY = "djconnect-engineering-dashboard-build",
  fallback = {
    watcher_state: "REMOTE_ENGINEERING_DEGRADED",
    current_phase: "UNKNOWN",
    current_action: t("dashboard.status_unavailable"),
    queue_depth: 0,
    repository_state: "UNKNOWN",
    workspace_state: "UNKNOWN",
    diagnostic: t("dashboard.status_unavailable"),
  };
let currentLogRun, lastLogRun, lastRefresh, promptStartedAt, latestStatus, latestDurationEstimate;
function formatTimestamp(value, fallback = t("format.timestamp_unavailable")) {
  const timestamp = Date.parse(String(value || ""));
  return Number.isFinite(timestamp) ? locale.dateTime(new Date(timestamp)) : fallback;
}
function capabilityRecommendation(value) {
  const key = {
    "Capability admission passed.": "capability.recommendation.admission_passed",
    "Repair or upgrade the Execution Host before resubmitting.":
      "capability.recommendation.repair_host",
  }[String(value || "").trim()];
  return key ? t(key) : String(value || t("format.not_available"));
}
function enumLabel(value, fallback = t("format.not_available")) {
  const enumValue = String(value || "").trim();
  if (!enumValue) return fallback;
  const key = `enum.${enumValue}`;
  return t(key, {}, enumValue);
}
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
function translate(value) {
  return t("state." + value, {}, value);
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
  if (phase === "COMPLETE") return ["green", t("status.complete")];
  if (phase === "BLOCKED") return ["yellow", t("status.blocked")];
  if (phase === "FAILED") return ["red", t("status.failed")];
  return ["grey", t("status.unknown")];
}
function executionRange(x) {
  const characters = Number(x.prompt_characters) || 0;
  if (characters <= 2e3) return [6, 10];
  if (characters <= 6e3) return [10, 18];
  if (characters <= 12e3) return [16, 26];
  return [24, 38];
}
function pluralMinutes(value) {
  return locale.plural(value, "unit.minute", "unit.minutes");
}
function historicalRange(estimate, fallback) {
  const samples = Number(estimate?.sample_count) || 0,
    lower = Number(estimate?.lower_seconds),
    upper = Number(estimate?.upper_seconds);
  if (samples < 2 || !Number.isFinite(lower) || !Number.isFinite(upper)) return fallback;
  const learnedMinimum = Math.max(1, Math.round(lower / 60)),
    learnedMaximum = Math.max(learnedMinimum, Math.ceil(upper / 60));
  // Prompt size is represented both in the static range and in the
  // size-adjusted history.  Retain a conservative static contribution.
  return [
    Math.max(1, Math.round(fallback[0] * 0.35 + learnedMinimum * 0.65)),
    Math.max(1, Math.round(fallback[1] * 0.35 + learnedMaximum * 0.65)),
  ];
}
function historicalContext(estimate, fallback) {
  const samples = Number(estimate?.sample_count) || 0;
  return samples >= 2
    ? t("estimate.historical_context", { count: samples })
    : fallback;
}
function hasHistoricalEstimate(estimate) {
  return (Number(estimate?.sample_count) || 0) >= 2;
}
function estimate(x, durationEstimate = {}) {
  const phase = x.current_phase || "";
  if (phase === "INITIALIZE")
    return { summary: t("estimate.initializing"), context: "" };
  if (["EXECUTE_AGENT", "REPAIR_AGENT"].includes(phase)) {
    const [minimum, maximum] = historicalRange(durationEstimate, executionRange(x));
    if (!promptStartedAt)
      return {
        summary: t("estimate.total", { minimum, maximum }),
        context: historicalContext(durationEstimate, t("estimate.total_context")),
      };
    const elapsed = Math.max(
        0,
        Math.floor((Date.now() - promptStartedAt) / 6e4),
      ),
      remainingMinimum = Math.max(1, minimum - elapsed),
      remainingMaximum = Math.max(remainingMinimum, maximum - elapsed);
    const elapsedContext = t("estimate.elapsed", { elapsed, minutes: pluralMinutes(elapsed) });
    return {
      summary: t("estimate.remaining", { minimum: remainingMinimum, maximum: remainingMaximum }),
      context: hasHistoricalEstimate(durationEstimate)
        ? `${elapsedContext}\n${historicalContext(durationEstimate, "")}`
        : elapsedContext,
    };
  }
  if (phase === "FINALIZE_AGENT")
    return {
      summary: t("estimate.finalizing"),
      context: t("estimate.finalizing_context"),
    };
  if (phase === "REPOSITORY_CLEANUP")
    return {
      summary: t("estimate.cleanup"),
      context: t("estimate.cleanup_context"),
    };
  if (phase === "WAIT_FOR_TERMINAL_EVIDENCE")
    return {
      summary: t("estimate.waiting"),
      context: t("estimate.waiting_context"),
    };
  if (phase === "COMPLETE") return { summary: t("status.complete"), context: "" };
  if (["BLOCKED", "FAILED"].includes(phase))
    return { summary: t("estimate.action_required"), context: "" };
  return { summary: t("estimate.not_available"), context: "" };
}
function renderEstimate(x, durationEstimate = latestDurationEstimate) {
  const value = estimate(x, durationEstimate);
  $("executionEstimate").textContent = value.summary;
  $("executionEstimateMeta").textContent = value.context;
  $("executionEstimateMeta").hidden = !value.context;
}
function isActiveRun(x) {
  return x.watcher_state === "ENGINEERING_RUN_ACTIVE" && Boolean(x.run_id);
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
    t("format.last_updated", { value: lastRefresh ? locale.dateTime(lastRefresh) : t("format.loading") });
}
function l(id, url, run, last, container) {
  if (run === (last ? lastLogRun : currentLogRun)) return;
  if (last) lastLogRun = run;
  else currentLogRun = run;
  $(id).textContent = t("ui.diagnostic_loading");
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
          ? t("ui.diagnostic_unavailable_history")
          : t("ui.diagnostic_unavailable_active");
    })
    .catch(() => {
      $(container).hidden = false;
      $(id).textContent = last
        ? t("ui.diagnostic_unavailable_history")
        : t("ui.diagnostic_unavailable_active");
    });
}
function usage(x) {
  const labels = {
    input_tokens: t("detail.input_tokens"),
    cached_input_tokens: t("ui.cached_input_tokens"),
    output_tokens: t("ui.output_tokens"),
    total_tokens: t("ui.total_tokens"),
    cost: t("detail.cost"),
    remaining: t("ui.remaining_available"),
    plan_remaining: t("detail.plan_remaining"),
    usage: t("ui.usage"),
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
      typeof x?.provider === "string" ? x.provider : t("format.not_available"),
    version =
      typeof x?.provider_version === "string"
        ? x.provider_version
        : t("format.version_unavailable"),
    button = $("rateLimitReset");
  $("rateLimits").hidden =
    !windows.length && credits === null && provider === t("format.not_available");
  $("rateLimitProvider").textContent = provider + " · " + version;
  let lines = windows.map((window) => {
    const remaining = Math.max(0, 100 - Number(window.used_percent || 0)),
      reset = Number(window.resets_at);
    return (
      window.label +
      ": " +
      t("rate_limit.available_reset", { remaining }) + " " +
      (Number.isFinite(reset)
        ? locale.dateTime(new Date(reset * 1e3))
        : t("format.unknown"))
    );
  });
  if (credits !== null) lines.push(t("ui.available_resets", { count: credits }));
  $("rateLimitDetails").textContent = lines.join(String.fromCharCode(10));
  button.hidden = !(credits > 0);
  button.disabled = false;
}
function consumeRateLimitReset() {
  const button = $("rateLimitReset"),
    status = $("rateLimitResetStatus");
  if (button.hidden || button.disabled) return;
  confirmDashboardAction(
    t("ui.reset_ready"),
    t("ui.reset_confirmation"),
    t("ui.reset_ready"),
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    status.textContent = t("ui.reset_in_progress");
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
        if (!result.ok) throw Error(t("ui.reset_failed"));
        const messages = {
          reset: t("ui.reset_used"),
          nothingToReset: t("ui.reset_nothing"),
          noCredit: t("ui.reset_no_credit"),
          alreadyRedeemed: t("ui.reset_already_redeemed"),
        };
        status.textContent = messages[result.body.outcome] || t("ui.reset_processed");
        if (result.body.rate_limits) rateLimits(result.body.rate_limits);
      })
      .catch(() => {
        status.textContent = t("ui.reset_failed");
      })
      .finally(() => {
        button.disabled = false;
      });
  });
}
function processMetrics(active, x) {
  $("processMetrics").hidden = !active;
  if (!active) return;
  $("codexCpu").textContent =
    locale.number(Number(x?.cpu_percent || 0), { maximumFractionDigits: 1 }) + "%";
  $("codexProcesses").textContent = x?.process_count ?? 0;
  $("codexGpu").textContent = x?.gpu_status || t("format.not_available");
}
function activeReviewerAgents(items) {
  const agents = Array.isArray(items) ? items : [];
  let card = $("activeReviewerAgents");
  if (!card) {
    card = document.createElement("section");
    card.id = "activeReviewerAgents";
    card.className = "card reviewer-agents";
    card.innerHTML = `<strong>${t("ui.reviewer_agents")}</strong><p class="estimate-meta" id="activeReviewerSummary"></p><div class="reviewer-agents__list" id="activeReviewerList"></div>`;
    $("currentRun")?.querySelector(".current-run__grid")?.append(card);
  }
  card.hidden = !agents.length;
  if (!agents.length) return;
  const running = agents.filter((agent) => agent?.status === "running").length,
    completed = agents.filter((agent) => ["completed", "failed"].includes(agent?.status)).length,
    summary = $("activeReviewerSummary"),
    list = $("activeReviewerList");
  summary.textContent = running
    ? t("ui.reviewer_running", { running, count: agents.length })
    : t("ui.reviewer_completed", { completed, count: agents.length });
  list.replaceChildren();
  for (const agent of agents) {
    const row = document.createElement("article"), name = document.createElement("p"), meta = document.createElement("p");
    row.className = "reviewer-agent";
    name.className = "reviewer-agent__name";
    meta.className = "reviewer-agent__meta";
    name.textContent = String(agent.reviewer || t("ui.reviewer_default")).replaceAll("_", " ");
    meta.textContent = `${enumLabel(agent.capability || "ENGINEERING")} · ${enumLabel(agent.status || "SELECTED")}`;
    row.append(name, meta);
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
        return locale.compare(left.filename, right.filename);
      }),
    container = $("queueList"),
    depth =
      Number.isInteger(queueDepth) && queueDepth >= 0
        ? queueDepth
        : items.length;
  $("queueSummary").textContent =
    depth === 0
      ? t("queue.summary_zero")
      : t("queue.summary", {
        count: depth,
        prompt: locale.plural(depth, "queue.prompt", "queue.prompts"),
        shown: depth > items.length ? t("queue.shown", { count: items.length }) : "",
      });
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "queue-empty";
    empty.textContent = t("queue.empty");
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
      filename = item.filename || t("format.not_available");
    row.className = "queue-item";
    row.setAttribute(
      "aria-label",
      t("queue.position", { position: index + 1, title: item.title || filename }),
    );
    number.className = "queue-item__number";
    number.textContent = String(index + 1);
    title.className = "queue-item__title";
    meta.className = "queue-item__meta";
    title.textContent = item.title || filename;
    meta.textContent = t("queue.filename", {
      filename,
      modified: Number.isFinite(modified)
        ? locale.dateTime(new Date(modified))
        : t("format.timestamp_unavailable"),
    });
    body.append(title, meta);
    row.append(number, body);
    container.append(row);
  });
}
function promptStarted(x) {
  promptStartedAt = x?.started_at ? Date.parse(x.started_at) : undefined;
  $("promptStarted").textContent = promptStartedAt
    ? locale.dateTime(new Date(promptStartedAt))
    : t("format.not_available");
  if (latestStatus) renderEstimate(latestStatus, latestDurationEstimate);
}
function renderMarkdownDocument(target, value) {
  target.replaceChildren();
  renderMarkdownAnswer(target, value);
}
let componentLogsLoaded = false,
  componentLogEntries = { inbox: [], dashboard: [] };
function structuredLogEntries(text) {
  const normalized = String(text ?? "").trim();
  if (!normalized || !normalized.startsWith("{")) return [];
  return normalized
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
          level: String(entry.level || t("logs.unknown_level")).toUpperCase(),
          event: String(entry.event || t("logs.unknown_event")),
          runId: entry.run_id == null ? "" : String(entry.run_id),
          details: details,
        };
      } catch {
        return {
          line: index + 1,
          timestamp: "",
          level: t("logs.invalid_json"),
          event: t("logs.unreadable"),
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
  return locale.logDateTime(new Date(parsed));
}
function loadComponentLogs() {
  if (componentLogsLoaded) return;
  $("loadComponentLogs").disabled = true;
  $("loadComponentLogs").textContent = t("logs.loading");
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
      $("loadComponentLogs").textContent = t("logs.loaded");
    })
    .catch(() => {
      componentLogEntries.inbox = structuredLogEntries(
        JSON.stringify({ level: "ERROR", event: "inbox_log_unavailable", diagnostic: t("logs.inbox_unavailable") }),
      );
      componentLogEntries.dashboard = structuredLogEntries(
        JSON.stringify({ level: "ERROR", event: "dashboard_log_unavailable", diagnostic: t("logs.dashboard_unavailable") }),
      );
      $("componentLogControls").hidden = false;
      renderComponentLogs();
      $("loadComponentLogs").disabled = false;
      $("loadComponentLogs").textContent = t("logs.retry");
    });
}
const CHAT_HISTORY_KEY = "djconnect-engineering-chat-history",
  CHAT_HISTORY_LIMIT = 20;
let chatContextRun = "";
function chatHistoryStorageKey(run = chatContextRun) {
  return CHAT_HISTORY_KEY + ":" + String(run || "none");
}
function loadChatHistory(run) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(chatHistoryStorageKey(run)) || "[]");
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
let chatHistory = [];
function persistChatHistory() {
  if (chatContextRun)
    sessionStorage.setItem(chatHistoryStorageKey(), JSON.stringify(chatHistory));
}
function renderLegacyChatMessage(role, text) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = t(role === "user" ? "chat.user" : "chat.assistant");
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
  // Chat context is selected explicitly from Prompt History, never inferred
  // from whichever terminal run happens to be latest in the status stream.
}
function askCodex() {
  let input = $("chatInput"),
    message = input.value.trim();
  if (!message || !chatContextRun || $("chatSend").disabled) return;
  $("chatSend").disabled = true;
  $("chatStatus").textContent = t("chat.thinking");
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
      run_id: chatContextRun,
    }),
  })
    .then(async (response) => ({
      ok: response.ok,
      body: await response.json(),
    }))
    .then((result) => {
      if (!result.ok)
        throw Error(t("chat.unavailable"));
      let answer = result.body.answer;
      $("chatModel").textContent =
        result.body.model || $("chatModel").textContent;
      chatHistory.push({ role: "assistant", text: answer });
      chatHistory = chatHistory.slice(-CHAT_HISTORY_LIMIT);
      persistChatHistory();
      chatMessage("assistant", answer);
      $("chatStatus").textContent = "";
    })
    .catch(() => {
      $("chatStatus").textContent = t("chat.unavailable");
    })
    .finally(() => {
      $("chatSend").disabled = false;
    });
}
function closePromptHistoryChat() {
  const modal = $("promptHistoryChatModal");
  if (modal.open) modal.close();
}
function openPromptHistoryChat(entry) {
  if (!entry?.run_id) return;
  chatContextRun = String(entry.run_id);
  chatHistory = loadChatHistory(chatContextRun);
  $("promptHistoryChatTitle").textContent = t("history.chat_title", {
    title: entry.title || entry.run_id,
  });
  $("promptHistoryChatDescription").textContent = t("history.chat_description");
  $("chatStatus").textContent = "";
  renderChatHistory();
  updateChatActions();
  const modal = $("promptHistoryChatModal");
  if (!modal.open) modal.showModal();
  modal.focus();
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
  if (!copied) throw Error(t("copy.failed"));
}
function copyText(value) {
  // iOS Safari permits the legacy copy command only while the original tap is
  // still being handled. Do that synchronously, then use the modern API only
  // when the browser does not support the fallback.
  try {
    fallbackCopy(value);
    showCopyToast();
    return Promise.resolve();
  } catch (fallbackError) {
    if (!navigator.clipboard || !window.isSecureContext)
      return Promise.reject(fallbackError);
    return navigator.clipboard.writeText(value).then(() => showCopyToast());
  }
}
let copyToastTimer;
function showCopyToast() {
  const toast = $("copyToast");
  if (!toast) return;
  clearTimeout(copyToastTimer);
  toast.textContent = t("copy.success");
  toast.hidden = false;
  if (typeof toast.showPopover === "function" && !toast.matches(":popover-open"))
    toast.showPopover();
  requestAnimationFrame(() => toast.classList.add("copy-toast--visible"));
  copyToastTimer = setTimeout(() => {
    toast.classList.remove("copy-toast--visible");
    setTimeout(() => {
      if (typeof toast.hidePopover === "function" && toast.matches(":popover-open"))
        toast.hidePopover();
      toast.hidden = true;
    }, 180);
  }, 2200);
}
const PREFLIGHT_PRESENTATIONS = Object.freeze([
  ["host_preflight", [
    ["hostPreflightStatus", "outcome"],
    ["hostPreflightTimestamp", "timestamp", "timestamp"],
  ]],
  ["workspace_preflight", [
    ["workspacePreflightStatus", "outcome"],
    ["workspacePreflightTimestamp", "timestamp", "timestamp"],
  ]],
  ["capability_preflight", [
    ["capabilityPreflightStatus", "outcome"],
    ["capabilityRecoverability", "recoverability"],
    ["capabilityFailureOrigin", "failure_origin"],
    ["capabilityRecommendation", "recommendation", "recommendation"],
  ]],
]);
function renderPreflightValue(key, value, formatter) {
  if (formatter === "timestamp")
    return formatTimestamp(value, t("format.not_available"));
  if (formatter === "recommendation") return capabilityRecommendation(value);
  return enumLabel(value, key === "failure_origin" ? "—" : undefined);
}
function renderPreflightPresentation(snapshot = {}) {
  for (const [preflightKey, fields] of PREFLIGHT_PRESENTATIONS) {
    const preflight = snapshot[preflightKey] || {};
    for (const [id, key, formatter] of fields) {
    const element = $(id);
    if (!element) continue;
      element.textContent = renderPreflightValue(key, preflight[key], formatter);
    }
  }
  const drift = snapshot.current_drift || {}, card = $("driftDiagnosticsCard");
  if (card) {
    card.hidden = !drift.drift_id;
    const values = [["driftSeverity", drift.severity], ["driftComponent", drift.affected_component],
      ["driftExpected", drift.expected_value], ["driftObserved", drift.observed_value],
      ["driftResolution", drift.resolution_recommendation]];
    for (const [id, value] of values) if ($(id)) $(id).textContent = value || t("format.not_available");
  }
}
function renderHealthStatus(x, snapshot = {}) {
  lastRefresh = new Date();
  clock();
  x = x && typeof x === "object" ? x : fallback;
  latestStatus = x;
  latestDurationEstimate = snapshot.duration_estimate || {};
  let active = isActiveRun(x),
    statusTone = tone(x),
    indicator = $("indicator"),
    components = snapshot.component_versions || {},
    blockedPredecessor = Boolean(x.blocking_predecessor_run);
  // A terminal current.json/status projection is historical evidence, not an
  // active prompt.  The watcher owns the operational view; history owns the
  // completed, failed or blocked execution.
  $("currentRun").hidden = !(active || blockedPredecessor);
  $("predecessorGate").hidden = !blockedPredecessor;
  $("predecessorRun").textContent =
    x.blocking_predecessor_run || t("format.not_available");
  $("predecessorPrompt").textContent =
    x.blocking_predecessor_title ||
    x.blocking_predecessor_filename ||
    t("format.not_available");
  $("predecessorPhase").textContent = translate(
    x.blocking_predecessor_phase || t("format.not_available"),
  );
  $("predecessorAction").textContent =
    x.predecessor_recovery_action || t("format.not_available");
  $("executionContext").hidden = !x.execution_mode;
  $("executionMode").textContent = x.execution_mode || t("format.not_available");
  $("targetRepository").textContent = x.target_repository || t("format.not_available");
  $("checkoutPath").textContent = x.checkout_path || t("format.not_available");
  $("activeBranch").textContent = x.active_branch || t("format.not_available");
  indicator.className =
    "indicator indicator--" +
    statusTone +
    (active ? " indicator--running" : "");
  indicator.setAttribute("aria-label", t("detail.prompt_status") + ": " + statusTone);
  $("watcher").textContent = translate(
    x.watcher_state || fallback.watcher_state,
  );
  $("phase").textContent = translate(
    x.current_phase || "idle",
  );
  $("action").textContent = translate(
    x.current_action || t("ui.no_active_action"),
  );
  const executionHost = snapshot.execution_host || {};
  $("executionHostName").textContent = executionHost.name || t("format.not_available");
  $("executionHostVersion").textContent = executionHost.version || t("format.not_available");
  $("executionHostRuntime").textContent = executionHost.runtime || t("format.not_available");
  $("executionHostTransport").textContent = executionHost.runtime_prompt_transport || t("format.not_available");
  // Older dashboard fixtures and cached shells do not have Level 3 fields.
  // Keep the canonical status renderer backward compatible while they refresh.
  renderPreflightPresentation(snapshot);
  promptStarted(snapshot.prompt_started);
  renderEstimate(x, latestDurationEstimate);
  processMetrics(active, snapshot.process_metrics);
  $("currentPrompt").textContent = x.prompt_title || t("format.not_available");
  $("currentFile").textContent = x.submitted_filename || t("format.not_available");
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
  $("runId").textContent = x.run_id || t("value.none");
  queueItems(x.queue_items, x.queue_depth);
  $("implementation").textContent = x.implementation_pr || t("value.none");
  $("finalization").textContent = x.finalization_pr || t("value.none");
  $("repositoryState").textContent = translate(x.repository_state || "UNKNOWN");
  $("workspaceState").textContent = translate(x.workspace_state || "UNKNOWN");
  $("diag").textContent = translate(x.diagnostic || t("value.no_diagnostics"));
  $("platformVersion").textContent = x.platform_version || t("format.not_available");
  $("dashboardVersion").textContent =
    components.dashboard || t("format.not_available");
  $("workerVersion").textContent = components.worker || t("format.not_available");
  usage(snapshot.usage);
  rateLimits(snapshot.rate_limits);
  activeReviewerAgents(x.reviewer_agents);
}
let activePromptCategoryRun;
function renderRunCategory(x) {
  const active = x && typeof x === "object" && isActiveRun(x),
    blockedPredecessor = Boolean(x?.blocking_predecessor_run),
    current = $("currentRun");
  const currentRunKey = active
    ? x.run_id
    : blockedPredecessor
      ? x.blocking_predecessor_run
      : null;
  if (currentRunKey && current && currentRunKey !== activePromptCategoryRun) {
    activePromptCategoryRun = currentRunKey;
    current.open = blockedPredecessor;
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
  localizeTechnicalDetails();
  renderRunCategory(status);
  renderLogsForSnapshot(snapshot);
  renderDashboardTelemetry(snapshot);
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
let receivedDashboardServerPush = false, updateModeKey = "refresh.connecting";
function setUpdateMode(key) {
  updateModeKey = key;
  $("updateMode").textContent = t(key);
}
async function loadInitialDashboardStatus() {
  try {
    const response = await fetch("/api/dashboard-snapshot", {
      cache: "no-store",
    });
    if (!response.ok) throw Error(t("dashboard.status_unavailable"));
    const snapshot = await response.json();
    if (!snapshot || typeof snapshot.status !== "object")
      throw Error(t("dashboard.status_invalid"));
    if (receivedDashboardServerPush) return;
    dashboardStatusStore.update(snapshot.status, snapshot);
    humanize();
    checkBuild(snapshot.build_commit);
    setUpdateMode("refresh.connecting");
  } catch {
    if (receivedDashboardServerPush) return;
    dashboardStatusStore.update(fallback);
    humanize();
    setUpdateMode("refresh.failed_reconnecting");
  }
}
void loadInitialDashboardStatus();
let e = new EventSource("/api/events");
let promptHistoryTerminalRun = null;
e.addEventListener("dashboard", (x) => {
  if (!$("autoRefresh").checked) return;
  try {
    let snapshot = JSON.parse(x.data);
    receivedDashboardServerPush = true;
    dashboardStatusStore.update(snapshot.status, snapshot);
    const terminalRun = snapshot.status?.last_executed_run;
    if (terminalRun && terminalRun !== promptHistoryTerminalRun) {
      promptHistoryTerminalRun = terminalRun;
      void refreshPromptHistory();
    }
    humanize();
    checkBuild(snapshot.build_commit);
    setUpdateMode("refresh.connected");
  } catch {
    dashboardStatusStore.update(fallback);
    humanize();
    setUpdateMode("refresh.invalid");
  }
});
e.onerror = () => {
  $("autoRefresh").checked &&
    setUpdateMode("refresh.reconnecting");
};
$("loadComponentLogs").addEventListener("click", loadComponentLogs);
$("chatSend").addEventListener("click", askCodex);
$("chatInput").addEventListener("keydown", (event) => {
  // Enter remains available for multi-line questions. Ctrl+Enter is the
  // explicit send shortcut on every platform; Cmd+Enter remains its macOS
  // counterpart. Do not submit while an IME composition is still active.
  if (
    !event.isComposing &&
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey)
  ) {
    event.preventDefault();
    askCodex();
  }
});
$("promptHistoryChatClose").addEventListener("click", closePromptHistoryChat);
$("promptHistoryChatModal").addEventListener("click", (event) => {
  if (event.target === $("promptHistoryChatModal")) closePromptHistoryChat();
});
renderChatHistory();
setInterval(() => {
  reconcileChatContext(latestStatus?.last_executed_run);
  clock();
}, 250);
clock();
function logValue(entry, key) {
  if (key === "line") return Number(entry.line) || 0;
  if (key === "timestamp") return logTimestamp(entry);
  return locale.lower(String(entry[key] || ""));
}
function providerNeutralLabels() {
  const labels = [
    ["#processMetrics>strong", "ui.local_ai_processes"],
    ["#usage>strong", "section.ai_provider_usage"],
    ["#currentDiagnostic>strong", "section.ai_execution_diagnostics"],
    ["#rateLimits .label", "section.ai_provider_limits"],
    ["#codexChat>strong", "section.ai_conversation"],
    ["#chatMessages", "section.ai_conversation"],
    ["label[for=chatInput]", "section.new_ai_question"],
  ];
  labels.forEach(([selector, key]) => {
    const element = document.querySelector(selector);
    if (element) {
      const text = t(key);
      element.textContent = text;
      if (selector === "#chatMessages")
        element.setAttribute("aria-label", text);
    }
  });
  $("chatInput").setAttribute("placeholder", t("history.chat_placeholder"));
}
function localizeTechnicalDetails() {
  const labels = [
    [["#technicalPullRequestsTitle", "#technicalDetails .technical-grid > .card:nth-child(1) > strong"], "technical.pull_requests"],
    [["#technicalImplementationLabel", "#technicalDetails .technical-grid > .card:nth-child(1) .field:nth-of-type(1) .label"], "technical.implementation"],
    [["#technicalFinalizationLabel", "#technicalDetails .technical-grid > .card:nth-child(1) .field:nth-of-type(2) .label"], "technical.finalization"],
    [["#technicalRepositoryTitle", "#technicalDetails .technical-grid > .card:nth-child(2) > strong"], "technical.repository"],
    [["#technicalRepositoryStateLabel", "#technicalDetails .technical-grid > .card:nth-child(2) .field:nth-of-type(1) .label"], "technical.repository_status"],
    [["#technicalWorkspaceStateLabel", "#technicalDetails .technical-grid > .card:nth-child(2) .field:nth-of-type(2) .label"], "technical.workspace_status"],
    [["#technicalHostPreflightTitle", "#technicalDetails .technical-grid > .card:nth-child(3) > strong"], "technical.host_preflight"],
    [["#technicalExecutionHostLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(1) .label"], "technical.execution_host"],
    [["#technicalExecutionHostVersionLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(2) .label"], "technical.execution_host_version"],
    [["#technicalRuntimeLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(3) .label"], "technical.runtime"],
    [["#technicalRuntimePromptTransportLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(4) .label"], "technical.runtime_prompt_transport"],
    [["#technicalHostStatusLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(5) .label"], "technical.host_status"],
    [["#technicalLastCheckLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(6) .label"], "technical.last_check"],
    [["#technicalWorkspacePreflightStatusLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(7) .label"], "technical.workspace_status"],
    [["#technicalLastWorkspaceCheckLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(8) .label"], "technical.last_workspace_check"],
    [["#technicalCapabilityStatusLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(9) .label"], "technical.capability_status"],
    [["#technicalRecoverabilityLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(10) .label"], "technical.recoverability"],
    [["#technicalFailureOriginLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(11) .label"], "technical.failure_origin"],
    [["#technicalRecommendationLabel", "#technicalDetails .technical-grid > .card:nth-child(3) .field:nth-of-type(12) .label"], "technical.recommended_action"],
    [["#technicalDiagnosticsTitle", "#technicalDetails .technical-grid > .card:nth-child(4) > strong"], "technical.diagnostics"],
  ];
  labels.forEach(([selectors, key]) => {
    const element = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
    if (element) element.textContent = t(key);
  });
}
function setControlLabel(selector, key) {
  const label = document.querySelector(selector);
  const text = label && [...label.childNodes].find((node) => node.nodeType === Node.TEXT_NODE);
  if (text) text.nodeValue = t(key);
}
function localizeLogControls() {
  setControlLabel("label[for=promptHistoryFilter]", "filter.search");
  setControlLabel("label[for=logFilter]", "filter.search");
  setControlLabel("label[for=logLevelFilter]", "filter.level");
  setControlLabel("label[for=logEventFilter]", "table.event");
  ["promptHistoryFilter", "logFilter"].forEach((id) => {
    const input = $(id);
    if (input) input.placeholder = t("filter.search_placeholder");
  });
  const optionKeys = [
    ["", "filter.all_levels"], ["ERROR", "filter.error"],
    ["WARNING", "filter.warning"], ["INFO", "filter.info"], ["DEBUG", "filter.debug"],
  ];
  optionKeys.forEach(([value, key]) => {
    const option = document.querySelector(`#logLevelFilter option[value="${value}"]`);
    if (option) option.textContent = t(key);
  });
  const cardTitles = ["logs.inbox_watcher", "logs.status_dashboard"];
  document.querySelectorAll("#componentLogs .log-card-header strong").forEach((title, index) => {
    if (cardTitles[index]) title.textContent = t(cardTitles[index]);
  });
  const headers = [
    "table.number", "table.timestamp", "table.level", "table.event",
    "table.run_id", "table.details",
  ];
  document.querySelectorAll("#componentLogs .log-table thead th").forEach((header, index) => {
    if (headers[index % headers.length]) header.textContent = t(headers[index % headers.length]);
  });
  const reset = document.querySelector(".reset-log-filters");
  if (reset) {
    const label = t("action.reset_log_filters");
    reset.title = label;
    reset.setAttribute("aria-label", label);
  }
}
function localizePromptHistoryTable() {
  const headers = [
    "table.status", "table.prompt_title", "table.executed_at", "table.report",
    "table.analysis", "table.chat", "table.action", "table.details",
  ];
  document.querySelectorAll("#promptHistory .log-table thead th").forEach((header, index) => {
    if (headers[index]) header.textContent = t(headers[index]);
  });
  document.querySelector("#promptHistory .log-table")?.setAttribute(
    "aria-label",
    t("history.table_label"),
  );
}
function localizeTemplateBindings() {
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  });
  document.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.title = t(element.dataset.i18nTitle);
  });
}
function chatMessage(role, text) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = t(role === "user" ? "chat.user" : "chat.assistant");
  body.className = "chat-message__body";
  body.textContent = text;
  item.append(label, body);
  $("chatMessages").append(item);
  item.scrollIntoView({ block: "nearest" });
}
providerNeutralLabels();
function addCategoryIcons() {
  for (const [selector, glyph, labelKey] of [
    ["#workspaceCard", "⌂", "section.workspace"],
    ["#queueItems", "☷", "section.inbox_queue"],
    ["#promptHistory", "◫", "section.prompt_history"],
    ["#platformHealth", "◈", "section.platform_components"],
    ["#rateLimits", "◔", "section.remaining_usage"],
    ["#executionTelemetry", "▥", "section.execution_host_telemetry"],
    ["#technicalDetails", "⚙", "section.technical_details"],
    ["#componentLogs", "≡", "section.logs"],
    ["#currentRun", "▤", "section.active_prompt"],
  ]) {
    const summary = document.querySelector(selector + ">summary"),
      label = t(labelKey);
    if (!summary) continue;
    let icon = summary.querySelector(".category-icon");
    if (!icon) {
      icon = document.createElement("span");
      const title = summary.querySelector("strong,.label");
      icon.className = "category-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = glyph;
      if (title) title.before(icon);
      else summary.prepend(icon);
    }
    icon.title = label;
  }
}
addCategoryIcons();
function addCategoryDescriptions() {
  const descriptions = [
    [".workspace-card", "description.workspace"],
    ["#queueItems", "description.inbox_queue"],
    ["#promptHistory", "description.prompt_history"],
    ["#rateLimits", "description.remaining_usage"],
    ["#componentLogs", "description.logs"],
    ["#engineering-dashboard-content>.technical-details:not(#componentLogs)", "description.technical_details"],
    ["#platformHealth", "description.platform_components"],
  ];
  for (const [selector, key] of descriptions) {
    const category = document.querySelector(selector),
      summary = category?.querySelector(":scope>summary");
    if (!category || !summary) continue;
    let description =
      summary.querySelector(":scope>[data-category-description]") ||
      category.querySelector(":scope>.category-description");
    if (!description) {
      description = document.createElement("p");
      summary.append(description);
    }
    description.textContent = t(key);
    description.classList.add("category-description");
    description.dataset.categoryDescription = "";
    summary.append(description);
  }
}
addCategoryDescriptions();
function arrangeOperationalCategories() {
  const technical = $("technicalDetails"),
    workspace = $("workspaceCard"),
    telemetry = $("executionTelemetry"),
    health = $("platformHealth"),
    logs = $("componentLogs");
  if (!technical) return;
  let anchor = technical;
  if (workspace) {
    anchor.insertAdjacentElement("afterend", workspace);
    anchor = workspace;
  }
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
  document.querySelectorAll("#componentLogs .log-table").forEach((table, index) => {
    table.setAttribute(
      "aria-label",
        index === 0 ? t("logs.inbox_entries") : t("logs.dashboard_entries"),
    );
    table.querySelectorAll("th.log-sortable").forEach((header) => {
      header.setAttribute("role", "button");
      header.setAttribute(
        "aria-label",
        t("table.sort_by", { column: header.textContent.trim() }),
      );
    });
  });
  let live = $("dashboardStatusAnnouncement");
  if (!live) {
    live = document.createElement("div");
    live.className = "sr-only";
    live.id = "dashboardStatusAnnouncement";
    live.setAttribute("role", "status");
    live.setAttribute("aria-live", "polite");
    live.setAttribute("aria-atomic", "true");
    document.body.append(live);
  }
  if (indicator.dataset.localizationObserver === "true") return;
  indicator.dataset.localizationObserver = "true";
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
  label.textContent = t("chat.assistant");
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
  button.title = t("copy.message");
  button.setAttribute("aria-label", t("copy.message"));
  button.textContent = "⧉";
  button.addEventListener("click", () => {
    copyText(String(text))
      .then(() => void recordUserAction("chat_message_copied"))
      .catch(() => {
        button.title = t("copy.failed");
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
      componentLogEntries.inbox = structuredLogEntries(JSON.stringify({
        level: "ERROR", event: "inbox_log_unavailable", diagnostic: t("logs.inbox_unavailable"),
      }));
      componentLogEntries.dashboard = structuredLogEntries(JSON.stringify({
        level: "ERROR", event: "dashboard_log_unavailable", diagnostic: t("logs.dashboard_unavailable"),
      }));
      $("componentLogControls").hidden = false;
      renderComponentLogs();
    });
}
function enableLiveComponentLogs() {
  const button = $("loadComponentLogs"),
    description = document.querySelector("#componentLogs .estimate-meta");
  button?.remove();
  if (description) description.textContent = t("description.logs");
  $("componentLogControls").hidden = false;
  refreshComponentLogs();
}
function renderLogsForSnapshot(snapshot) {
  refreshComponentLogs(snapshot.component_log_versions || {});
}
enableLiveComponentLogs();
function healthComponentLabel(component) {
  return {
    dashboard: t("logs.status_dashboard"),
    inbox_watcher: t("component.execution_host"),
    dashboard_relay: t("component.dashboard_relay"),
  }[component] || component;
}
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
    return t("ui.no_local_process");
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
  title.textContent = healthComponentLabel(payload.component) || t("component.component_information");
  content.replaceChildren();
  const fields = document.createElement("dl");
  componentDetailField(fields, t("component.machine"), payload.machine);
  componentDetailField(
    fields,
    t("component.status"),
    (payload.healthy ? t("component.health_healthy") : t("component.health_unhealthy")) +
      " · " +
      (payload.detail || payload.state || t("ui.no_component_explanation")),
  );
  componentDetailField(fields, t("component.version"), payload.version);
  componentDetailField(
    fields,
    t("component.uptime"),
    formatComponentUptime(payload.uptime_seconds),
  );
  componentDetailField(fields, t("detail.git_commit"), payload.git_commit);
  componentDetailField(
    fields,
    t("component.executable_path"),
    Array.isArray(launchd.program_arguments) && launchd.program_arguments.length
      ? launchd.program_arguments[0]
      : payload.executable_path,
  );
  componentDetailField(fields, t("component.launchd_label"), launchd.label);
  componentDetailField(fields, t("component.launch_agent"), launchd.plist_path);
  componentDetailField(
    fields,
    t("component.launchd_configuration"),
    launchd.label
      ? (launchd.loaded ? t("component.health_healthy") : t("component.health_unhealthy")) +
          " · " + t("component.start_at_load") + ": " +
          (launchd.run_at_load ? "✓" : "—") +
          " · " + t("component.keep_active") + ": " +
          (launchd.keep_alive ? "✓" : "—")
      : null,
  );
  componentDetailField(
    fields,
    t("component.current_memory"),
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
      throw Error(payload.error || t("ui.component_information_unavailable"));
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
    !legacyConfirmation(t("ui.component_restart_confirmation"))
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
      throw Error(payload.error || t("ui.component_restart_failed"));
    $("componentModalStatus").textContent = t("ui.component_restart_requested");
  } catch (error) {
    $("componentModalStatus").textContent =
      error.message || t("ui.component_restart_failed");
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
    message.textContent = t("ui.component_health_unavailable");
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
      t("component.more_information", { component: healthComponentLabel(key) }),
    );
    indicator.className = healthIndicatorClass(componentHealthy);
    indicator.setAttribute("aria-hidden", "true");
    name.className = "platform-health__component-name";
    name.textContent = healthComponentLabel(key);
    detail.className = "platform-health__component-detail";
    detail.textContent =
      (componentHealthy ? t("component.health_healthy") : t("component.health_unhealthy")) +
      " · " +
      String(component?.detail || component?.state || t("ui.no_component_explanation")) +
      version +
      (uptime ? " · " + t("component.uptime") + " " + uptime : "");
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
  let description = summary.querySelector(
    ":scope>.current-run__category-description",
  );
  if (!description) {
    description = document.createElement("p");
    description.className = "current-run__category-description";
    summary.append(description);
  }
  description.textContent = t("description.active_prompt");
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
    title.textContent = t("telemetry.title");
    description.className = "category-description";
    description.textContent = t("telemetry.description");
    scroll.className = "telemetry-scroll";
    table.className = "telemetry-table";
    table.setAttribute("aria-label", t("telemetry.table_label"));
    for (const label of [
      "telemetry.day", "telemetry.prompts", "telemetry.average_execution",
      "telemetry.average_total", "telemetry.average_wait", "telemetry.input",
      "telemetry.output", "telemetry.total", "telemetry.complete",
      "telemetry.blocked", "telemetry.failed",
    ].map((key) => t(key))) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      headRow.append(cell);
    }
    head.append(headRow);
    tableBody.id = "executionTelemetryRows";
    table.append(head, tableBody);
    scroll.append(table);
    summary.append(title, description);
    panel.append(summary, scroll);
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
    cell.textContent = t("telemetry.empty");
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
    title.textContent = t("telemetry.title");
    description.className = "category-description";
    description.textContent = t("telemetry.description");
    scroll.className = "telemetry-scroll";
    table.className = "telemetry-table";
    table.setAttribute("aria-label", t("telemetry.table_label"));
    for (const label of [
      "telemetry.day", "telemetry.prompts", "telemetry.average_execution",
      "telemetry.average_wait", "telemetry.input", "telemetry.output",
      "telemetry.total", "telemetry.complete", "telemetry.blocked",
      "telemetry.failed",
    ].map((key) => t(key))) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      headRow.append(cell);
    }
    head.append(headRow);
    tableBody.id = "executionTelemetryRows";
    table.append(head, tableBody);
    scroll.append(table);
    summary.append(title, description);
    panel.append(summary, scroll);
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
    cell.textContent = t("telemetry.empty");
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
window.executionTelemetry = executionTelemetry;
function updateFavicon() {
  $("dashboardFavicon").href = "/assets/engineering-status-icon.svg";
}
function renderDashboardTelemetry(snapshot) {
  updateFavicon();
  executionTelemetry(snapshot.telemetry);
}
updateFavicon();
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
function setIndependentLogSort(component, key) {
  const state = independentLogSortStates[component];
  independentLogSortStates[component] =
    state.key === key
      ? { key: key, direction: state.direction === "asc" ? "desc" : "asc" }
      : { key: key, direction: key === "timestamp" ? "desc" : "asc" };
  independentLogPageStates[component] = 1;
  renderComponentLogs();
}
document.querySelectorAll(".log-table").forEach((table) => {
  const component = logComponentForTable(table);
  const keys = ["line", "timestamp", "level", "event", "runId", "details"];
  table.querySelectorAll("th").forEach((header, index) => {
    const key = keys[index];
    header.classList.add("log-sortable");
    header.dataset.sortKey = key;
    header.tabIndex = 0;
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
  const needle = locale.lower($("logFilter").value.trim()),
    level = $("logLevelFilter").value,
    events = new Set([...$("logEventFilter").selectedOptions].map((option) => option.value)),
    state = independentLogSortStates[component];
  return componentLogEntries[component]
    .filter((entry) => !level || entry.level === level)
    .filter((entry) => !events.size || events.has(String(entry.event || "")))
    .filter(
      (entry) =>
        !needle ||
        locale.lower(Object.values(entry).join(" ")).includes(needle),
    )
    .sort((left, right) => {
      const first = logValue(left, state.key),
        second = logValue(right, state.key),
        result =
          typeof first === "number" && typeof second === "number"
            ? first - second
            : locale.compare(first, second);
      return state.direction === "asc" ? result : -result;
    });
}
function updateLogValueFilters() {
  const entries = [...componentLogEntries.inbox, ...componentLogEntries.dashboard];
  for (const [id, key] of [["logEventFilter", "event"]]) {
    const select = $(id), selected = new Set([...select.selectedOptions].map((option) => option.value));
    select.replaceChildren();
    [...new Set(entries.map((entry) => String(entry[key] || "")).filter(Boolean))].sort((a, b) => locale.compare(a, b)).forEach((value) => {
      const option = new Option(value, value, false, selected.has(value)); select.add(option);
    });
  }
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
    ? t("logs.page", { page, pages: pageCount, count: total })
    : t("logs.no_entries");
  previous.type = next.type = "button";
  previous.textContent = t("history.previous");
  next.textContent = t("history.next");
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
function renderComponentLogs() {
  localizeLogControls();
  updateLogValueFilters();
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
      cell.textContent = componentLogEntries[component].length
        ? t("logs.empty")
        : t("logs.not_available");
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
              locale.lower(entry.level).replaceAll(" ", "-"),
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
for (const [id, label] of [["logEventFilter", t("table.event")]]) {
  const control = document.createElement("label"), select = document.createElement("select");
  select.id = id; select.multiple = true; select.setAttribute("aria-label", label);
  control.htmlFor = id; control.append(label, select); $("componentLogControls").append(control);
  select.addEventListener("change", () => { independentLogPageStates.inbox = independentLogPageStates.dashboard = 1; renderComponentLogs(); });
}
const resetLogFiltersButton = document.createElement("button");
resetLogFiltersButton.className = "reset-log-filters";
resetLogFiltersButton.type = "button";
resetLogFiltersButton.textContent = "↺";
const resetLogFiltersLabel = t("action.reset_log_filters");
resetLogFiltersButton.title = resetLogFiltersLabel;
resetLogFiltersButton.setAttribute("aria-label", resetLogFiltersLabel);
resetLogFiltersButton.addEventListener("click", () => {
  $("logFilter").value = "";
  $("logLevelFilter").value = "";
  [...$("logEventFilter").options].forEach((option) => { option.selected = false; });
  independentLogPageStates.inbox = independentLogPageStates.dashboard = 1;
  renderComponentLogs();
});
$("componentLogControls").append(resetLogFiltersButton);
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
    component === "inbox" ? t("component.execution_host") : t("logs.status_dashboard");
  confirmDashboardAction(
    t("action.clear_logs"),
    t("logs.clear_description", { component: name }),
    t("action.clear_logs"),
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
        throw Error(payload.error || t("logs.clear_failed"));
      }
      componentLogEntries[component] = structuredLogEntries(
        await fetch("/api/logs/" + encodeURIComponent(component)).then(
          (response) => response.text(),
        ),
      );
      componentLogVersion = "";
      renderComponentLogs();
    } catch {
      button.title = t("logs.clear_failed");
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
  if (!name) return Promise.reject(Error(t("logs.unknown_component")));
  return fetch("/api/logs/" + encodeURIComponent(component), {
    cache: "no-store",
  })
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(Error(t("logs.download_unavailable"))),
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
      button.title = t("logs.download_unavailable");
    }),
  ),
);
document.querySelectorAll(".clear-component-log").forEach((button) => {
  button.classList.add("clear-component-log--glyph");
  button.textContent = "⌫";
  button.title = t("action.clear_logs");
  button.setAttribute("aria-label", t("action.clear_logs"));
});
let pullRefreshStart = null,
  pullRefreshDistance = 0;
const pullRefresh = $("pullRefresh");
const dashboardScrollRegion = document.querySelector(".dashboard-scroll-region");
const pullRefreshActivationHeight = 40;
function updatePullRefresh(distance) {
  pullRefreshDistance = Math.max(0, Math.min(distance, 112));
  const ready = pullRefreshDistance >= 72;
  pullRefresh.classList.toggle(
    "pull-refresh--visible",
    pullRefreshDistance > 8,
  );
  pullRefresh.textContent = t(
    ready ? "refresh.release_to_refresh" : "refresh.pull_to_refresh",
  );
  pullRefresh.setAttribute("aria-hidden", String(pullRefreshDistance <= 8));
}
function startPullRefresh(event) {
  if (
    event.touches.length !== 1 ||
    (dashboardScrollRegion && dashboardScrollRegion.scrollTop > 0)
  )
    return;
  const target = event.target;
  if (
    target instanceof Element &&
    target.closest("input,textarea,select,button,[contenteditable=true]")
  )
    return;
  const touch = event.touches[0];
  const scrollRegionTop = dashboardScrollRegion?.getBoundingClientRect().top ?? 0;
  if (touch.clientY > scrollRegionTop + pullRefreshActivationHeight) return;
  pullRefreshStart = touch.clientY;
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
    pullRefresh.textContent = t("refresh.refreshing");
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
// Keep one predictable, scan-friendly history page: ten executions are shown
// at once and the next set is reached through the paginator rather than an
// inner vertical scrollbar.
const PROMPT_HISTORY_PAGE_SIZE = 10;
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
  const needle = locale.lower($("promptHistoryFilter").value.trim());
  return promptHistoryEntries
    .filter(
      (entry) =>
        !needle ||
        locale
          .lower(
            [...Object.values(entry), promptHistoryStatus(entry.status)].join(" "),
          )
          .includes(needle),
    )
    .sort((left, right) => {
      const first = promptHistoryValue(left, promptHistorySort.key),
        second = promptHistoryValue(right, promptHistorySort.key),
        result =
          typeof first === "number" && typeof second === "number"
            ? first - second
            : locale.compare(first, second);
      return promptHistorySort.direction === "asc" ? result : -result;
    });
}
function promptHistoryStatus(value) {
  return t("status." + String(value || "unknown").toLowerCase());
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
function repairPromptHistoryHeader() {
  // A dashboard can briefly retain its HTML shell while its script has already
  // updated. Remove the retired commit column so that those mixed revisions
  // cannot offset the prompt-history actions by one column.
  document
    .querySelectorAll('#promptHistory th[data-history-sort-key="git_commit"]')
    .forEach((header) => header.remove());
}
function renderPromptHistory() {
  repairPromptHistoryHeader();
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
    cell.colSpan = 8;
    cell.textContent = t("history.no_prompts");
    row.append(cell);
    body.append(row);
  } else
    for (const entry of visible) {
      const row = document.createElement("tr"),
        status = document.createElement("td"),
        title = document.createElement("td"),
        executed = document.createElement("td"),
        report = document.createElement("td"),
        analysis = document.createElement("td"),
        chat = document.createElement("td"),
        action = document.createElement("td"),
        details = document.createElement("td"),
        timestamp = Date.parse(String(entry.executed_at || ""));
      row.className = "prompt-history-row";
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", t("history.open_details", { title: entry.title || entry.run_id }));
      const openDetails = (event) => {
        if (event?.target?.closest("button,a")) return;
        openPromptHistoryDetail(entry);
      };
      row.addEventListener("click", openDetails);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDetails(event);
        }
      });
      action.className = "prompt-history-actions";
      status.className =
        "prompt-history-status prompt-history-status--" +
        locale.lower(String(entry.status || ""));
      status.textContent = promptHistoryStatus(entry.status);
      title.textContent = String(
        entry.title || entry.run_id || t("retry.unavailable_title"),
      );
      executed.textContent = Number.isFinite(timestamp)
        ? locale.dateTime(new Date(timestamp))
        : String(entry.executed_at || t("format.timestamp_unavailable"));
      if (entry.report_available && entry.run_id) {
        const view = document.createElement("button");
        view.className = "prompt-history-report";
        view.type = "button";
        view.title = t("history.view_report", { title: title.textContent });
        view.setAttribute("aria-label", view.title);
        view.textContent = "▤";
        view.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          openPromptHistoryDocument(entry.run_id, title.textContent, "report");
        });
        report.append(view);
      } else report.textContent = "—";
      if (entry.analysis_available && entry.run_id) {
        const view = document.createElement("button");
        view.className = "prompt-history-analysis";
        view.type = "button";
        view.title = t("history.view_analysis", { title: title.textContent });
        view.setAttribute("aria-label", view.title);
        view.textContent = "✦";
        view.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          openPromptHistoryDocument(entry.run_id, title.textContent, "analysis");
        });
        analysis.append(view);
      } else analysis.textContent = "—";
      if (entry.run_id) {
        const button = document.createElement("button");
        button.className = "prompt-history-chat";
        button.type = "button";
        button.title = t("history.open_chat", { title: title.textContent });
        button.setAttribute("aria-label", button.title);
        button.textContent = "💬";
        button.addEventListener("click", () => openPromptHistoryChat(entry));
        chat.append(button);
      } else chat.textContent = "—";
      if (entry.status === "BLOCKED" && entry.run_id) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "predecessor-retry execution-history-action";
        retry.textContent = t("action.retry_execution");
        retry.addEventListener("click", () => submitExecutionRetry(entry));
        action.append(retry);
      }
      if (["BLOCKED", "FAILED"].includes(entry.status) && !entry.dismissed && entry.run_id && entry.run_id === latestStatus?.last_executed_run && !isActiveRun(latestStatus)) {
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "predecessor-retry execution-history-action";
        dismiss.textContent = t("action.dismiss_execution");
        dismiss.addEventListener("click", () => dismissExecution(entry));
        action.append(dismiss);
      }
      if (!action.childElementCount) action.textContent = "—";
      if (entry.run_id) {
        const button = document.createElement("button");
        button.className = "prompt-history-details";
        button.type = "button";
        button.title = t("history.open_details", { title: title.textContent });
        button.setAttribute("aria-label", button.title);
        button.textContent = "i";
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          openPromptHistoryDetail(entry);
        });
        details.append(button);
      } else details.textContent = "—";
      row.append(status, title, executed, report, analysis, chat, action, details);
      body.append(row);
    }
  navigation.replaceChildren();
  const summary = document.createElement("span"),
    previous = document.createElement("button"),
    next = document.createElement("button");
  summary.className = "log-pagination__summary";
  summary.textContent = rows.length
    ? t("history.page", { page: promptHistoryPage, pages: pageCount, count: rows.length })
    : t("history.no_results");
  previous.type = next.type = "button";
  previous.textContent = t("history.previous");
  next.textContent = t("history.next");
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
let promptHistoryRefreshRetry = null;
function refreshPromptHistory({ retryEmptyOnce = true } = {}) {
  return fetch("/api/prompt-history", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : Promise.reject()))
    .then((payload) => {
      if (!Array.isArray(payload?.runs)) throw Error("invalid prompt history");
      promptHistoryEntries = payload.runs;
      renderPromptHistory();
      // The history index can be briefly unavailable while the watcher commits
      // a terminal run. Retry one empty initial projection so a transient
      // SQLite lock never leaves the visible history blank until a reload.
      if (!promptHistoryEntries.length && retryEmptyOnce) {
        clearTimeout(promptHistoryRefreshRetry);
        promptHistoryRefreshRetry = setTimeout(() => {
          void refreshPromptHistory({ retryEmptyOnce: false });
        }, 1_000);
      }
    })
    .catch(() => {
      promptHistoryEntries = [];
      renderPromptHistory();
      if (retryEmptyOnce) {
        clearTimeout(promptHistoryRefreshRetry);
        promptHistoryRefreshRetry = setTimeout(() => {
          void refreshPromptHistory({ retryEmptyOnce: false });
        }, 1_000);
      }
    });
}
async function refreshAfterOperatorAction({ dismissedRunId = null } = {}) {
  // The operator just received a successful acknowledgement. Reflect it in
  // the visible history immediately, then reconcile from storage. This keeps
  // a slow status snapshot from leaving a stale dismiss action on screen.
  if (dismissedRunId) {
    promptHistoryEntries = promptHistoryEntries.map((entry) =>
      entry.run_id === dismissedRunId ? { ...entry, dismissed: true } : entry,
    );
    renderPromptHistory();
  }
  const snapshot = await fetch("/api/dashboard-snapshot", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : null))
    .catch(() => null);
  if (snapshot && typeof snapshot.status === "object") {
    dashboardStatusStore.update(snapshot.status, snapshot);
    humanize();
    checkBuild(snapshot.build_commit);
  }
  await refreshPromptHistory();
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
const dashboardLocaleSelector = $("dashboardLocale");
function applyDashboardLocale() {
  document.documentElement.lang = dashboardLocale;
  document.title = t("dashboard.title");
  $("dashboardAppleWebAppTitle").content = t("dashboard.title");
  dashboardLocaleSelector.value = dashboardLocale;
  localizeTemplateBindings();
  const replacements = [
    [".skip-link", "header.skip"],
    [".theme-toggle__label", "header.theme"],
    [".section-state-toggle__label", "header.expand"],
    [".auto-refresh-toggle span", "header.auto_refresh"],
    [".dashboard-locale span", "language.label"],
    ["#dashboardTitle", "dashboard.title"],
    ["#dashboardSplashTitle", "dashboard.title"],
    ["#dashboardSplashLoading", "dashboard.loading"],
    ["#platformVersionLabel", "footer.platform_version"],
    ["#confirmationModalCancel", "action.cancel"],
    ["#confirmationModalConfirm", "action.confirm"],
    ["#predecessorRetry", "action.resume_queue"],
    ["#queueItems > summary > strong", "section.inbox_queue"],
    ["#promptHistory > summary > strong", "section.prompt_history"],
    ["#currentRun > summary .label", "section.active_prompt"],
    ["#rateLimits > summary > strong", "section.remaining_usage"],
    ["#platformHealth > summary > strong", "section.platform_components"],
    ["#componentLogs > summary > strong", "section.logs"],
    ["#technicalDetails > summary > strong", "section.technical_details"],
    ["#technicalPullRequestsTitle", "technical.pull_requests"],
    ["#technicalImplementationLabel", "technical.implementation"],
    ["#technicalFinalizationLabel", "technical.finalization"],
    ["#technicalRepositoryTitle", "technical.repository"],
    ["#technicalRepositoryStateLabel", "technical.repository_status"],
    ["#technicalWorkspaceStateLabel", "technical.workspace_status"],
    ["#technicalHostPreflightTitle", "technical.host_preflight"],
    ["#technicalExecutionHostLabel", "technical.execution_host"],
    ["#technicalExecutionHostVersionLabel", "technical.execution_host_version"],
    ["#technicalRuntimeLabel", "technical.runtime"],
    ["#technicalRuntimePromptTransportLabel", "technical.runtime_prompt_transport"],
    ["#technicalHostStatusLabel", "technical.host_status"],
    ["#technicalLastCheckLabel", "technical.last_check"],
    ["#technicalWorkspacePreflightStatusLabel", "technical.workspace_status"],
    ["#technicalLastWorkspaceCheckLabel", "technical.last_workspace_check"],
    ["#technicalCapabilityStatusLabel", "technical.capability_status"],
    ["#technicalRecoverabilityLabel", "technical.recoverability"],
    ["#technicalFailureOriginLabel", "technical.failure_origin"],
    ["#technicalRecommendationLabel", "technical.recommended_action"],
    ["#technicalDiagnosticsTitle", "technical.diagnostics"],
    ["#workspaceCard > summary > strong", "section.workspace"],
    ["#promptHistoryAnalysisHeader", "table.analysis"],
    ["#promptHistoryChatHeader", "table.chat"],
  ];
  replacements.forEach(([selector, key]) => {
    const element = document.querySelector(selector);
    if (element) element.textContent = t(key);
  });
  const workspaceKeys = [
    "workspace.name", "ui.workspace_location", "detail.tracked_files",
    "workspace.database", "workspace.database_size", "workspace.schema_version",
  ];
  document.querySelectorAll("#workspaceCard .field .label").forEach((label, index) => {
    if (workspaceKeys[index]) label.textContent = t(workspaceKeys[index]);
  });
  document.querySelectorAll("#dashboardLocale option").forEach((option) => {
    option.textContent = t("language." + option.value);
  });
  $("themeToggle").setAttribute("aria-label", t("header.enable_light"));
  $("toggleAllSections").setAttribute("aria-label", t("header.open_all"));
  $("dashboardSplashVersion").textContent = t("dashboard.platform_version", {
    version: $("dashboardSplashVersion").dataset.platformVersion,
  });
  setUpdateMode(updateModeKey);
  providerNeutralLabels();
  localizeTechnicalDetails();
  localizeLogControls();
  localizePromptHistoryTable();
  applyAccessibility();
  renderPromptHistory();
  addCategoryIcons();
  addCategoryDescriptions();
  arrangeCurrentRunCategory();
}
dashboardLocaleSelector.addEventListener("change", () => {
  dashboardLocale = normalizeLocale(dashboardLocaleSelector.value);
  locale = createLocaleService(dashboardLocale);
  dashboardClientState.locale = dashboardLocale;
  saveDashboardClientState();
  window.location.reload();
});
applyDashboardLocale();
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
    "platformHealth",
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
    allOpen ? t("sections.close_all") : t("sections.open_all"),
  );
  allSectionsToggle.title = allOpen ? t("sections.close") : t("sections.open");
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
  setUpdateMode(autoRefreshToggle.checked ? "refresh.connected" : "refresh.off");
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
        t(entry.role === "user" ? "chat.user" : "chat.assistant") +
        "\n\n" +
        entry.text.trim(),
    )
    .filter(Boolean);
  return [
    "# " + t("chat.download_title"),
    "",
    t("chat.download_model", { model: $("chatModel").textContent.trim() }),
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
    light ? t("theme.enable_dark") : t("theme.enable_light"),
  );
  themeToggle.title = light ? t("theme.dark") : t("theme.light");
  applyThemeModeAttributes();
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
applyThemeModeAttributes();
new MutationObserver((records) => {
  for (const record of records)
    for (const node of record.addedNodes)
      if (node instanceof Element) applyThemeModeAttributes(node);
}).observe(document.body, { childList: true, subtree: true });
$("rateLimitProvider")?.previousElementSibling?.replaceChildren(
  t("ui.current_ai_provider"),
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
                    detail: t("ui.component_restart_started"),
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
        $("componentModalStatus").textContent = t("ui.component_restart_available");
        return;
      }
      renderPlatformHealth(payload);
    } catch {}
  }
  $("componentModalStatus").textContent = t("ui.component_restart_waiting");
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
$("downloadChat")?.addEventListener(
  "click",
  () => void recordUserAction("chat_downloaded"),
);
let promptHistoryReportText = "",
  promptHistoryReportRun = "",
  promptHistoryDocumentKind = "report";
function promptHistoryReportFilename() {
  return (
    (promptHistoryDocumentKind === "analysis" ? "ai-analysis-" : "engineering-report-") +
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
  void recordUserAction(
    promptHistoryDocumentKind === "analysis"
      ? "prompt_history_analysis_downloaded"
      : "prompt_history_report_downloaded",
  );
}
function openPromptHistoryDocument(runId, title, kind = "report") {
  const modal = $("promptHistoryReportModal"),
    content = $("promptHistoryReportContent");
  promptHistoryReportRun = String(runId || "");
  promptHistoryDocumentKind = kind === "analysis" ? "analysis" : "report";
  promptHistoryReportText = "";
  $("promptHistoryReportModalTitle").textContent =
    promptHistoryDocumentKind === "analysis"
      ? t("history.analysis_title", { title })
      : title || t("history.report_title");
  $("promptHistoryReportCopy").hidden = true;
  $("promptHistoryReportDownload").hidden = true;
  content.replaceChildren();
  content.textContent = t(
    promptHistoryDocumentKind === "analysis"
      ? "history.analysis_loading"
      : "history.report_loading",
  );
  if (!modal.open) modal.showModal();
  modal.focus();
  fetch(
    "/api/prompt-history/" +
      encodeURIComponent(promptHistoryReportRun) +
      (promptHistoryDocumentKind === "analysis" ? "/analysis" : "/report"),
    { cache: "no-store" },
  )
    .then((response) =>
      response.ok
        ? response.text()
        : Promise.reject(
            Error(
              t(
                promptHistoryDocumentKind === "analysis"
                  ? "history.analysis_unavailable"
                  : "history.report_unavailable",
              ),
            ),
          ),
    )
    .then((text) => {
      if (!text)
        throw Error(
          t(
            promptHistoryDocumentKind === "analysis"
              ? "history.analysis_unavailable"
              : "history.report_unavailable",
          ),
        );
      promptHistoryReportText = text;
      renderMarkdownDocument(content, text);
      $("promptHistoryReportCopy").hidden = false;
      $("promptHistoryReportDownload").hidden = false;
    })
    .catch(() => {
      content.textContent = t(
        promptHistoryDocumentKind === "analysis"
          ? "history.analysis_unavailable"
          : "history.report_unavailable",
      );
    });
}
function detailField(label, value, preformatted = false) {
  const field = document.createElement("p"),
    name = document.createElement("span"),
    output = document.createElement(preformatted ? "pre" : "span");
  field.className = "field";
  name.className = "label";
  name.textContent = label;
  output.textContent = String(value ?? "—");
  field.append(name, output);
  return field;
}
function promptHistoryStatusTone(value) {
  switch (String(value || "").toUpperCase()) {
    case "COMPLETE": return "green";
    case "BLOCKED": return "orange";
    case "FAILED": return "red";
    default: return "grey";
  }
}
function promptDetailStatusField(value) {
  const field = detailField(t("detail.prompt_status"), "");
  const output = field.lastElementChild;
  output.className = "prompt-detail-status";
  const indicator = document.createElement("span");
  indicator.className = "indicator indicator--small indicator--" + promptHistoryStatusTone(value);
  indicator.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.textContent = promptHistoryStatus(value);
  output.replaceChildren(indicator, text);
  return field;
}
function promptDetailCard(title, fields, wide = false) {
  const card = document.createElement("section"), heading = document.createElement("h3");
  card.className = "prompt-detail-card" + (wide ? " prompt-detail-card--wide" : "");
  heading.textContent = title;
  card.append(heading, ...fields);
  return card;
}
function promptDetailSidebar(cards) {
  const sidebar = document.createElement("div");
  sidebar.className = "prompt-detail-sidebar";
  sidebar.append(...cards.filter(Boolean));
  return sidebar;
}
function promptDetailDuration(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? durationText(seconds) : "—";
}
function promptDetailExecutionSection(history) {
  const timestamp = Date.parse(String(history.executed_at || ""));
  return promptDetailCard(t("detail.execution"), [
    promptDetailStatusField(history.status),
    detailField(t("detail.prompt_title"), history.title),
    detailField(t("detail.run_id"), history.run_id, true),
    detailField(
      t("detail.executed_at"),
      Number.isFinite(timestamp)
        ? locale.dateTime(new Date(timestamp))
        : history.executed_at,
    ),
    detailField(t("detail.execution_mode"), history.execution_mode || t("detail.not_recorded")),
    detailField(t("detail.producer"), history.producer_id || t("detail.not_recorded")),
    detailField(t("detail.producer_type"), history.producer_type || t("detail.not_recorded")),
    detailField(t("detail.producer_version"), history.producer_version || t("detail.not_recorded")),
    detailField(t("detail.mission_id"), history.mission_id || t("detail.not_recorded")),
    detailField(t("detail.engineering_action_id"), history.engineering_action_id || t("detail.not_recorded")),
    detailField(t("detail.correlation_id"), history.correlation_id || t("detail.not_recorded")),
    detailField(t("detail.target_repository"), history.target_repository || t("detail.not_recorded")),
    detailField(t("ui.active_branch"), history.target_branch || t("detail.not_recorded"), true),
    detailField(t("detail.target_checkout"), history.target_checkout_path || t("detail.not_recorded"), true),
    detailField(t("detail.tracked_files"), history.tracked_file_count ?? t("detail.not_recorded")),
  ]);
}
function promptDetailDurationSection(execution) {
  return promptDetailCard(t("detail.duration"), [
    detailField(
      t("detail.agent_duration"),
      promptDetailDuration(execution.seconds),
    ),
    detailField(
      t("detail.total_duration"),
      promptDetailDuration(execution.total_seconds),
    ),
  ]);
}
function promptDetailRuntimeSection(runtime) {
  const fields = [
    [t("detail.runtime_provider"), runtime.runtime_provider],
    [t("detail.model"), runtime.model],
    [t("detail.reasoning_profile"), runtime.reasoning_profile],
    [t("detail.configuration_profile"), runtime.configuration_profile],
    [t("detail.codex_cli_version"), runtime.codex_cli_version],
  ]
    .filter(([, value]) => value)
    .map(([label, value]) => detailField(label, value));
  return fields.length ? promptDetailCard(t("detail.runtime"), fields) : null;
}
function promptDetailUsageSection(usage) {
  const labels = {
    input_tokens: t("detail.input_tokens"),
    output_tokens: t("detail.output_tokens"),
    total_tokens: t("detail.total_tokens"),
  };
  const fields = Object.entries(usage).map(([key, value]) =>
    detailField(labels[key] || key, value),
  );
  return fields.length ? promptDetailCard(t("detail.provider_usage"), fields) : null;
}
function promptDetailCommitsSection(commits) {
  if (!Object.keys(commits).length) return null;
  const evidence = Object.entries(commits)
    .map(([label, value]) => label + ": " + value)
    .join("\n");
  return promptDetailCard(t("detail.git_commit"), [
    detailField(t("detail.recorded_evidence"), evidence, true),
  ]);
}
function promptDetailEvidenceSection(evidence) {
  if (!evidence.length) return null;
  return promptDetailCard(
    t("detail.execution_evidence"),
    [detailField(t("detail.evidence"), evidence.join("\n"), true)],
  );
}
function promptDetailReviewersSection(reviewers) {
  if (!reviewers.length) return null;
  const fields = reviewers.map((reviewer) =>
    detailField(
      String(reviewer.reviewer || t("detail.specialist_review")).replaceAll("_", " "),
      t("detail.capability") + ": " +
        String(reviewer.capability || "engineering") + " · " +
        String(reviewer.status || t("detail.completed")) + " · " +
        t("detail.accepted_recommendations") + ": " +
        (Number(reviewer.accepted_recommendations) || 0) + "\n" +
        t("detail.selected_because") + ": " +
        String(reviewer.selected_because || t("detail.not_recorded")),
      true,
    ),
  );
  return promptDetailCard(t("detail.specialist_reviews"), fields, true);
}
function renderPromptHistoryDetail(payload) {
  const content = $("promptHistoryDetailContent"),
    history = payload?.history || {},
    execution = payload?.execution || {},
    runtime = payload?.runtime || {},
    usage = payload?.usage || {},
    commits = payload?.commits || {},
    evidence = Array.isArray(payload?.evidence) ? payload.evidence : [],
    reviewers = Array.isArray(payload?.reviewers) ? payload.reviewers : [];
  content.replaceChildren();
  content.append(
    ...[
      promptDetailExecutionSection(history),
      promptDetailSidebar([
        promptDetailDurationSection(execution),
        promptDetailRuntimeSection(runtime),
        promptDetailCommitsSection(commits),
        promptDetailEvidenceSection(evidence),
      ]),
      promptDetailUsageSection(usage),
      promptDetailReviewersSection(reviewers),
    ].filter(Boolean),
  );
}
function closePromptHistoryDetail() {
  const modal = $("promptHistoryDetailModal");
  if (modal.open) modal.close();
}
function openPromptHistoryDetail(entry) {
  if (!entry?.run_id) return;
  const modal = $("promptHistoryDetailModal"), content = $("promptHistoryDetailContent");
  $("promptHistoryDetailTitle").textContent = String(entry.title || entry.run_id);
  $("promptHistoryDetailDescription").textContent = t("history.details_description");
  content.textContent = t("history.details_loading");
  if (!modal.open) modal.showModal();
  modal.focus();
  fetch("/api/prompt-history/" + encodeURIComponent(entry.run_id) + "/details", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : Promise.reject())
    .then(renderPromptHistoryDetail)
    .catch(() => { content.textContent = t("history.details_unavailable"); });
}
$("promptHistoryDetailClose").addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  closePromptHistoryDetail();
});
$("promptHistoryDetailModal").addEventListener("click", (event) => {
  if (event.target.closest?.("#promptHistoryDetailClose")) {
    event.preventDefault();
    event.stopPropagation();
    closePromptHistoryDetail();
    return;
  }
  if (event.target === $("promptHistoryDetailModal")) closePromptHistoryDetail();
});
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
      () => void recordUserAction(
        promptHistoryDocumentKind === "analysis"
          ? "prompt_history_analysis_copied"
          : "prompt_history_report_copied",
      ),
    );
});
$("promptHistoryReportDownload").addEventListener(
  "click",
  downloadPromptHistoryReport,
);
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
    t("queue_recovery.title"),
    t("queue_recovery.details"),
    t("action.resume_queue"),
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    status.textContent = t("queue_recovery.preparing");
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
            result.body.error || t("queue_recovery.failed"),
          );
        status.textContent =
          t("queue_recovery.ready");
      })
      .catch((error) => {
        status.textContent =
          error.message || t("queue_recovery.failed");
      })
      .finally(() => {
        button.disabled = false;
      });
  });
}
function submitExecutionRetry(entry) {
  if (!entry?.run_id) return;
  const title = String(entry.title || t("retry.unavailable_title"));
  const repository = String(entry.repository || t("retry.unavailable_repository"));
  const mode = String(entry.execution_mode || t("retry.unavailable_mode"));
  confirmDashboardAction(
    t("retry.title"),
    t("retry.details", { run_id: entry.run_id, title, repository, mode }),
    t("action.retry_execution"),
  ).then((confirmed) => {
    if (!confirmed) return;
    fetch("/api/execution-retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: entry.run_id }),
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("retry.failed"));
        return refreshAfterOperatorAction();
      })
      .catch((error) => window.alert(error.message || t("retry.failed")));
  });
}
function dismissExecution(entry) {
  if (!entry?.run_id) return;
  confirmDashboardAction(
    t("dismiss.title"),
    t("dismiss.details", { run_id: entry.run_id, title: String(entry.title || t("retry.unavailable_title")), state: t("status." + String(entry.status || "unknown").toLowerCase()) }),
    t("action.dismiss_execution"),
  ).then((confirmed) => {
    if (!confirmed) return;
    fetch("/api/execution-dismiss", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: entry.run_id }) })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("dismiss.failed"));
        return refreshAfterOperatorAction({ dismissedRunId: entry.run_id });
      })
      .catch((error) => window.alert(error.message || t("dismiss.failed")));
  });
}
$("predecessorRetry").addEventListener("click", submitPredecessorRetry);
function confirmDashboardAction(title, text, confirmLabel) {
  const modal = $("confirmationModal"),
    heading = $("confirmationModalTitle"),
    body = $("confirmationModalText"),
    cancel = $("confirmationModalCancel"),
    confirm = $("confirmationModalConfirm");
  heading.textContent = title;
  body.textContent = text;
  confirm.textContent = confirmLabel;
  modal.style.setProperty("--confirmation-color", "#f0b66a");
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
    t("chat.clear_title"),
    t("chat.clear_description"),
    t("chat.clear_title"),
  ).then((confirmed) => {
    if (!confirmed) return;
    chatHistory = [];
    if (chatContextRun) sessionStorage.removeItem(chatHistoryStorageKey());
    renderChatHistory();
    updateChatActions();
  }),
);
const updateChatDownloadWithClear = updateChatDownloadAvailability;
updateChatDownloadAvailability = () => {
  updateChatDownloadWithClear();
  updateChatActions();
};
function showDashboardReloadSplash() {
  const splash = $("dashboardSplash");
  splash.hidden = false;
  document.body.classList.remove("dashboard-ready");
}
async function restartPlatformComponent(button) {
  const component = button.dataset.component;
  if (!component) return;
  const confirmed = await confirmDashboardAction(
    t("component.restart_title"),
    t("component.restart_description", { component: healthComponentLabel(component) || t("component.no_component") }),
    t("component.restart_title"),
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
    if (!response.ok) throw Error(t("ui.component_restart_failed"));
    if (component === "dashboard") {
      $("componentModalStatus").textContent =
        t("ui.component_restart_started");
      showDashboardReloadSplash();
      window.setTimeout(() => window.location.reload(), 750);
      return;
    }
    $("componentModalStatus").textContent = t("ui.component_restart_started");
  } catch {
    $("componentModalStatus").textContent = t("ui.component_restart_failed");
  } finally {
    button.disabled = false;
  }
}
function legacyConfirmation() {
  return false;
}
function legacyDashboardError() {
  const status = $("componentModalStatus");
  if (status) status.textContent = t("ui.component_information_unavailable");
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
  capabilityRecommendation,
  enumLabel,
  executionTelemetry,
  formatTimestamp,
  queueItems,
  r,
  rateLimits,
  refreshOpenComponentDetails,
  renderChatHistory,
  renderComponentLogs,
  renderLogPagination,
  renderMarkdownAnswer,
  renderPromptHistoryDetail,
  renderPlatformHealth,
  renderPromptHistory,
  showComponentModal,
  showCopyToast,
  startPullRefresh,
  structuredLogEntries,
  movePullRefresh,
  endPullRefresh,
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
