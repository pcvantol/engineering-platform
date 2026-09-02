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
let currentLogRun, lastLogRun, lastRefresh, promptStartedAt, latestStatus, latestDashboardSnapshot, latestDurationEstimate, latestPlatformHealth,
  shownOperatorMergeWaitRun;
function formatTimestamp(value, fallback = t("format.timestamp_unavailable")) {
  const timestamp = Date.parse(String(value || ""));
  return Number.isFinite(timestamp) ? locale.dateTime(new Date(timestamp)) : fallback;
}
function formatPromptHistoryTimestamp(value, fallback = t("format.timestamp_unavailable")) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return fallback;
  return new Intl.DateTimeFormat(dashboardLocale, {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(timestamp));
}
function formatPromptHistoryDuration(value) {
  if (value === null || value === undefined || value === "") return "";
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  return t("history.total_duration_minutes", {
    minutes: new Intl.NumberFormat(dashboardLocale, { maximumFractionDigits: 0 }).format(
      Math.ceil(seconds / 60),
    ),
  });
}
function capabilityRecommendation(value) {
  const key = {
    "Capability admission passed.": "capability.recommendation.admission_passed",
    "Repair or upgrade the Execution Host before resubmitting.":
      "capability.recommendation.repair_host",
  }[String(value || "").trim()];
  return key ? t(key) : String(value || t("format.not_available"));
}
function formatDiagnostic(value) {
  return translate(value || t("value.no_diagnostics")).replace(
    /\.\s+(?=(?:Expected|Observed|Required action|Verwacht|Waargenomen|Vereiste actie):)/g,
    ".\n",
  );
}
function enumLabel(value, fallback = t("format.not_available")) {
  const enumValue = String(value || "").trim();
  if (!enumValue) return fallback;
  const key = `enum.${enumValue}`;
  return t(key, {}, enumValue);
}
function reviewerKey(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}
function reviewerLabel(value, fallback = t("ui.reviewer_default")) {
  const raw = String(value || "").trim();
  if (!raw) return fallback;
  return t(`reviewer.${reviewerKey(raw)}`, {}, raw.replaceAll("_", " "));
}
function reviewerStatusLabel(value, fallback = t("format.not_available")) {
  const raw = String(value || "").trim();
  if (!raw) return fallback;
  const normalized = reviewerKey(raw) === "uitgevoerd" ? "completed" : reviewerKey(raw);
  return t(`reviewer.status.${normalized}`, {}, raw.replaceAll("_", " "));
}
function reviewerCapabilityLabel(value, fallback = t("format.not_available")) {
  const raw = String(value || "").trim();
  return raw ? enumLabel(raw.toUpperCase(), raw) : fallback;
}
function driftPresentationValue(field, drift = {}) {
  const value = String(drift?.[field] || "").trim();
  if (!value) return t("format.not_available");
  const component = String(drift?.affected_component || "").trim();
  if (field === "severity" && value === "BLOCKING") return t("technical.drift.severity.blocking");
  if (component !== "managed_expected_branch") return value;
  const branch = (
    /^Managed target is not on the expected branch\s+(.+)\.$/.exec(String(drift.observed_value || ""))?.[1] ||
    /^Switch the repository to\s+(.+)\s+before submitting work\.$/.exec(String(drift.resolution_recommendation || ""))?.[1] ||
    "main"
  );
  if (field === "affected_component") return t("technical.drift.component.managed_expected_branch");
  if (field === "expected_value" && /^managed_expected_branch:\s*PASS$/i.test(value)) {
    return t("technical.drift.expected.managed_expected_branch", { branch });
  }
  if (field === "observed_value" && /^Managed target is not on the expected branch\s+.+\.$/.test(value)) {
    return t("technical.drift.observed.managed_expected_branch", { branch });
  }
  if (field === "resolution_recommendation" && /^Switch the repository to\s+.+\s+before submitting work\.$/.test(value)) {
    return t("technical.drift.resolution.managed_expected_branch", { branch });
  }
  return value;
}
function technicalMachineLabel(id, value, keyPrefix) {
  const element = $(id), raw = String(value || "").trim();
  if (!element) return;
  element.textContent = raw ? t(`${keyPrefix}.${raw}`, {}, raw) : t("format.not_available");
  element.title = raw;
}
function runtimeProviderLabel(value) {
  const raw = String(value || "").trim();
  return raw ? t("technical.runtime_value." + raw, {}, raw) : raw;
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
const OPERATIONAL_PRESENTATION_KEYS = {
  ENGINEERING_RUN_STALE: "operational.stale_run",
  CAPABILITY_REVIEW: "telemetry.phase.capability_review",
  invoke_agent: "operational.activity_invoke_agent",
  run_local_repository_validation: "operational.run_local_repository_validation",
  execute_required_validation_controls: "operational.execute_required_validation_controls",
  workspace_migration_blocked_by_active_dashboard: "operational.workspace_migration_blocked_by_active_dashboard",
  create_finalization: "operational.create_finalization",
  RECONCILE_AGENT: "lifecycle.step.reconcile_agent",
  FINALIZATION_REPAIR_AGENT: "lifecycle.step.repair_agent",
  poll_required_checks: "operational.poll_required_checks",
  reconcile_rolling_records_on_main: "operational.reconcile_rolling_records_on_main",
  WAIT_FOR_OPERATOR_MERGE: "lifecycle.step.wait_for_operator_merge",
  WAIT_FOR_FINALIZATION_MERGE: "lifecycle.step.wait_for_finalization_merge",
  WAIT_FOR_RECONCILIATION_MERGE: "lifecycle.step.wait_for_reconciliation_merge",
  "Waiting for the operator to merge the pull request.": "operational.waiting_for_operator_merge",
  "Execution Host ownership is stale; no execution is currently running.": "operational.stale_host_ownership",
};
function translate(value) {
  const raw = String(value || "");
  const capabilityReview = /^Capability review:\s*(.+)$/i.exec(raw);
  if (capabilityReview) {
    return t("operational.activity_capability_review", {
      reviewer: reviewerLabel(capabilityReview[1]),
    });
  }
  const presentationKey = OPERATIONAL_PRESENTATION_KEYS[raw];
  return presentationKey ? t(presentationKey) : t("state." + raw, {}, raw);
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
    ["WAITING_FOR_REPOSITORY", "WAITING_FOR_PREDECESSOR", "WAITING_FOR_OPERATOR_MERGE"].includes(watcher)
  )
    return "yellow";
  if (
    [
      "INITIALIZE",
      "EXECUTE_AGENT",
      "LOCAL_REPOSITORY_VALIDATION",
      "REPAIR_AGENT",
      "FINALIZATION_REPAIR_AGENT",
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
  // The exact runtime profile's size-adjusted history is more informative
  // than the coarse static table. Keep a small safety contribution from the
  // latter, with confidence increasing as more comparable runs exist.
  const historyWeight = samples >= 8 ? 0.9 : samples >= 4 ? 0.85 : 0.8;
  return [
    Math.max(1, Math.round(fallback[0] * (1 - historyWeight) + learnedMinimum * historyWeight)),
    Math.max(1, Math.round(fallback[1] * (1 - historyWeight) + learnedMaximum * historyWeight)),
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
function phaseAwareRange(estimate) {
  const samples = Number(estimate?.phase_sample_count) || 0,
    lower = Number(estimate?.remaining_lower_seconds),
    upper = Number(estimate?.remaining_upper_seconds);
  if (estimate?.phase_aware !== true || samples < 2 || !Number.isFinite(lower) || !Number.isFinite(upper)) return null;
  return [Math.max(1, Math.round(lower / 60)), Math.max(1, Math.ceil(upper / 60))];
}
function estimate(x, durationEstimate = {}) {
  const phase = x.current_phase || "";
  const phaseRange = phaseAwareRange(durationEstimate);
  if (["INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT", "FINALIZATION_REPAIR_AGENT", "FINALIZE_AGENT", "REPOSITORY_CLEANUP"].includes(phase) && phaseRange) {
    const [minimum, maximum] = phaseRange;
    return {
      summary: t("estimate.remaining", { minimum, maximum }),
      context: historicalContext(durationEstimate, t("estimate.total_context")),
    };
  }
  if (phase === "INITIALIZE")
    return { summary: t("estimate.initializing"), context: "" };
  if (["EXECUTE_AGENT", "REPAIR_AGENT", "FINALIZATION_REPAIR_AGENT"].includes(phase)) {
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
function isActiveRun(x = {}) {
  return ["ENGINEERING_RUN_ACTIVE", "WAITING_FOR_OPERATOR_MERGE"].includes(x.watcher_state) && Boolean(x.run_id);
}
function hasVisibleStaleLifecycle(x = {}) {
  return x.watcher_state === "ENGINEERING_RUN_STALE" && Boolean(x.run_id) &&
    x.current_phase !== "COMPLETE" && x.current_phase !== "BLOCKED" && x.current_phase !== "FAILED";
}
function watcherDelegatedToActiveHost(component, status = latestStatus) {
  return component?.component === "inbox_watcher" && component?.healthy !== true && isActiveRun(status || {});
}
function dashboardHealthPresentation(status = latestStatus, platformHealth = latestPlatformHealth) {
  const current = status && typeof status === "object" ? status : null,
    components = platformHealth?.components && typeof platformHealth.components === "object"
      ? platformHealth.components
      : null,
    dashboardHealthy = components?.dashboard?.healthy === true,
    watcherHealthy = components?.inbox_watcher?.healthy === true,
    relayHealthy = components?.dashboard_relay?.healthy === true,
    queueDepth = Math.max(0, Number(current?.queue_depth) || 0),
    watcherState = String(current?.watcher_state || ""),
    workspaceState = String(current?.workspace_state || ""),
    phase = String(current?.current_phase || "").toUpperCase(),
    watcherStateUpper = watcherState.toUpperCase(),
    active = isActiveRun(current || {}),
    watcherDelegatedToActiveHost = active && components && !watcherHealthy,
    workspaceActive = active && workspaceState === "ACTIVE",
    blocked = phase === "BLOCKED" || watcherStateUpper.includes("WAITING") || watcherStateUpper.includes("BLOCKED"),
    failed = phase === "FAILED" || watcherStateUpper.includes("FAILED") || watcherStateUpper.includes("DEGRADED") ||
      (components && (!dashboardHealthy || !relayHealthy || (!watcherHealthy && !watcherDelegatedToActiveHost)));
  let state = "unknown";
  if (failed) state = "error";
  else if (blocked || queueDepth > 0) state = "blocked";
  else if (active) state = "active";
  else if (dashboardHealthy && watcherHealthy && relayHealthy && watcherState === "WATCHER_IDLE" && workspaceState === "WORKSPACE_READY") state = "ready";
  const componentCheck = (name, componentKey, healthy, delegatedToActiveHost = false) => {
    const component = components?.[componentKey];
    const unavailable = components ? "not_running" : "unknown";
    const reasonCode = !healthy && components && typeof component?.reason_code === "string"
      ? component.reason_code
      : "";
    const reason = !healthy && components
      ? (reasonCode ? t("component.reason." + reasonCode) : String(component?.detail || component?.state || "").trim())
      : "";
    return [
      name,
      delegatedToActiveHost ? "execution_host_active" : healthy ? "running" : unavailable,
      delegatedToActiveHost ? "warning" : healthy ? "good" : components ? "bad" : "unknown",
      {},
      { component: components ? componentKey : null, reason: delegatedToActiveHost ? "" : reason },
    ];
  };
  const checks = [
    componentCheck("dashboard", "dashboard", dashboardHealthy),
    componentCheck("watcher", "inbox_watcher", watcherHealthy, watcherDelegatedToActiveHost),
    componentCheck("relay", "dashboard_relay", relayHealthy),
    ["execution", active ? "active" : phase === "BLOCKED" ? "blocked" : phase === "FAILED" ? "error" : "none_active", active ? "good" : phase === "BLOCKED" ? "warning" : phase === "FAILED" ? "bad" : "good"],
    ["queue", queueDepth ? "queue_waiting" : "queue_empty", queueDepth ? "warning" : "good", { count: queueDepth }],
    ["watcher_state", watcherState || "unknown", watcherState === "WATCHER_IDLE" ? "good" : watcherStateUpper.includes("FAILED") || watcherStateUpper.includes("DEGRADED") ? "bad" : watcherState ? "warning" : "unknown"],
    ["workspace", workspaceState || "unknown", workspaceState === "WORKSPACE_READY" || workspaceActive ? "good" : workspaceState ? "bad" : "unknown"],
  ];
  return { state, checks };
}
function renderDashboardHealth(status = latestStatus, platformHealth = latestPlatformHealth) {
  const indicator = $("dashboardHealthIndicator"), tooltipTitle = $("dashboardHealthTooltipTitle"), checks = $("dashboardHealthChecks"), accessibleLabel = $("dashboardHealthAccessibleLabel");
  if (!indicator || !tooltipTitle || !checks || !accessibleLabel) return;
  const presentation = dashboardHealthPresentation(status, platformHealth), title = t("dashboard.health." + presentation.state);
  indicator.dataset.healthState = presentation.state;
  indicator.setAttribute("aria-label", t("dashboard.health.title") + ": " + title);
  accessibleLabel.textContent = t("dashboard.health.title") + ": " + title;
  tooltipTitle.textContent = t("dashboard.health.title") + " · " + title;
  checks.replaceChildren();
  for (const [name, value, tone, values = {}, metadata = {}] of presentation.checks) {
    const item = document.createElement("li"), label = document.createElement("span"), result = document.createElement("span");
    item.dataset.health = tone;
    item.className = "dashboard-health__check";
    label.textContent = t("dashboard.health." + name);
    result.textContent = t("dashboard.health." + value, values, translate(value));
    result.className = "dashboard-health__value";
    item.append(label, result);
    if (metadata.reason) {
      const reason = document.createElement("span");
      reason.className = "dashboard-health__reason";
      reason.textContent = t("dashboard.health.reason", { reason: metadata.reason });
      item.append(reason);
    }
    if (metadata.component) {
      item.classList.add("dashboard-health__check--component");
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.setAttribute(
        "aria-label",
        t("component.more_information", { component: label.textContent }),
      );
      const linkIcon = document.createElement("span");
      linkIcon.className = "dashboard-health__check-link-icon";
      linkIcon.dataset.testid = "dashboard-health-component-link-icon";
      linkIcon.setAttribute("aria-hidden", "true");
      linkIcon.textContent = "↗";
      label.append(linkIcon);
      item.addEventListener("click", () => showComponentDetails(metadata.component));
      item.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        showComponentDetails(metadata.component);
      });
    }
    checks.append(item);
  }
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
function renderCapacityTrend(history) {
  const card = $("rateLimits"), details = $("rateLimitDetails");
  if (!card || !details) return;
  let trend = $("rateLimitTrend");
  if (!trend) {
    trend = document.createElement("section");
    trend.className = "rate-limit-trend";
    trend.id = "rateLimitTrend";
    const heading = document.createElement("h3"), description = document.createElement("p"), chart = document.createElement("div");
    heading.className = "rate-limit-trend__title";
    heading.id = "rateLimitTrendTitle";
    description.className = "rate-limit-trend__description";
    chart.className = "rate-limit-trend__chart";
    chart.id = "rateLimitTrendChart";
    trend.append(heading, description, chart);
    details.closest(".field")?.after(trend);
  }
  $("rateLimitTrendTitle").textContent = t("rate_limit.trend_title");
  trend.querySelector(".rate-limit-trend__description").textContent = t("rate_limit.trend_description");
  const points = (Array.isArray(history) ? history : [])
    .map((point) => ({ at: Date.parse(String(point?.at || "")), remaining: Number(point?.remaining_percent) }))
    .filter((point) => Number.isFinite(point.at) && Number.isFinite(point.remaining) && point.remaining >= 0 && point.remaining <= 100)
    .sort((left, right) => left.at - right.at);
  const chart = $("rateLimitTrendChart");
  chart.replaceChildren();
  if (!points.length) return;
  const latest = points.at(-1), latestPercent = locale.number(latest.remaining, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const namespace = "http://www.w3.org/2000/svg", svg = document.createElementNS(namespace, "svg"), title = document.createElementNS(namespace, "title"), width = 336, height = 120, padding = { top: 10, right: 8, bottom: 22, left: 32 }, now = Date.now(), start = now - 7 * 24 * 60 * 60 * 1000;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-labelledby", "rateLimitTrendSvgTitle");
  title.id = "rateLimitTrendSvgTitle";
  title.textContent = t("rate_limit.trend_aria", { percent: latestPercent });
  svg.append(title);
  const innerWidth = width - padding.left - padding.right, innerHeight = height - padding.top - padding.bottom;
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const grid = document.createElementNS(namespace, "line"), y = padding.top + innerHeight * fraction;
    grid.setAttribute("class", "rate-limit-trend__grid");
    grid.setAttribute("x1", String(padding.left)); grid.setAttribute("x2", String(width - padding.right));
    grid.setAttribute("y1", String(y)); grid.setAttribute("y2", String(y)); svg.append(grid);
    const label = document.createElementNS(namespace, "text");
    label.setAttribute("class", "rate-limit-trend__axis-label"); label.setAttribute("text-anchor", "end");
    label.setAttribute("x", String(padding.left - 5)); label.setAttribute("y", String(y + 3));
    label.textContent = `${locale.number((1 - fraction) * 100, { maximumFractionDigits: 0 })}%`; svg.append(label);
  }
  for (let day = 0; day <= 7; day += 1) {
    const grid = document.createElementNS(namespace, "line"), x = padding.left + innerWidth * (day / 7);
    grid.setAttribute("class", "rate-limit-trend__grid");
    grid.setAttribute("x1", String(x)); grid.setAttribute("x2", String(x));
    grid.setAttribute("y1", String(padding.top)); grid.setAttribute("y2", String(height - padding.bottom)); svg.append(grid);
  }
  const coordinates = points.map((point) => ({
    ...point,
    x: padding.left + Math.max(0, Math.min(1, (point.at - start) / (now - start))) * innerWidth,
    y: padding.top + (1 - point.remaining / 100) * innerHeight,
  }));
  let pathData = "";
  for (const point of coordinates) {
    pathData += !pathData
      ? `M ${point.x.toFixed(2)} ${point.y.toFixed(2)}`
      : ` L ${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
  }
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("class", "rate-limit-trend__line"); path.setAttribute("d", pathData); svg.append(path);
  for (const point of coordinates) {
    const marker = document.createElementNS(namespace, "circle");
    marker.setAttribute("class", "rate-limit-trend__point"); marker.setAttribute("cx", point.x.toFixed(2)); marker.setAttribute("cy", point.y.toFixed(2)); marker.setAttribute("r", "1.6"); svg.append(marker);
  }
  const dayFormatter = new Intl.DateTimeFormat(dashboardLocale, { weekday: "short" });
  // Label every day boundary: the rolling window runs from seven days ago through now.
  for (let day = 0; day <= 7; day += 1) {
    const label = document.createElementNS(namespace, "text"), x = padding.left + innerWidth * (day / 7);
    label.setAttribute("class", "rate-limit-trend__axis-label");
    if (day === 7) label.setAttribute("text-anchor", "end");
    else if (day > 0) label.setAttribute("text-anchor", "middle");
    label.setAttribute("x", String(x)); label.setAttribute("y", String(height - 4));
    label.textContent = dayFormatter.format(new Date(start + day * 24 * 60 * 60 * 1000)); svg.append(label);
  }
  chart.append(svg);
}
function rateLimits(x, history = latestDashboardSnapshot?.ai_capacity_history) {
  const windows = Array.isArray(x?.windows) ? x.windows : [],
    credits = Number.isInteger(x?.reset_credits) ? x.reset_credits : null,
    provider =
      typeof x?.provider === "string" ? x.provider : t("format.not_available"),
    version =
      typeof x?.provider_version === "string"
        ? x.provider_version
        : t("format.version_unavailable"),
    providerPath =
      typeof x?.provider_path === "string" && x.provider_path.trim()
        ? x.provider_path.trim()
        : t("format.not_available"),
    button = $("rateLimitReset");
  $("rateLimits").hidden =
    !windows.length && credits === null && provider === t("format.not_available");
  $("rateLimitProvider").textContent = provider + " · " + version;
  let providerPathElement = $("rateLimitProviderPath");
  if (!(providerPathElement instanceof HTMLButtonElement)) {
    providerPathElement = replaceWithLocalFolderButton(providerPathElement);
  }
  configureLocalFolderButton(providerPathElement, providerPath);
  let lines = windows.map((window) => {
    const remaining = Math.max(0, 100 - Number(window.used_percent || 0)),
      reset = Number(window.resets_at);
    return (
      window.label +
      ": " +
      t("rate_limit.available_reset", { remaining: locale.number(remaining, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) }) + " " +
      (Number.isFinite(reset)
        ? locale.dateTime(new Date(reset * 1e3))
        : t("format.unknown"))
    );
  });
  if (credits !== null) lines.push(t("ui.available_resets", { count: credits }));
  $("rateLimitDetails").textContent = lines.join(String.fromCharCode(10));
  renderCapacityTrend(history);
  button.hidden = !(credits > 0);
  button.disabled = false;
}
let latestCodexCliUpdateStatus = null;
function codexCliVersionParts(value) {
  const match = typeof value === "string" && /^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$/.exec(value);
  return match ? match.slice(1, 4).map(Number) : null;
}
function codexCliUpdateIsNewer(current, latest) {
  const currentParts = codexCliVersionParts(current), latestParts = codexCliVersionParts(latest);
  if (!currentParts || !latestParts) return false;
  return latestParts.some((part, index) => part !== currentParts[index]
    && latestParts.slice(0, index).every((previous, previousIndex) => previous === currentParts[previousIndex])
    && part > currentParts[index]);
}
function renderCodexCliUpdate(status) {
  const button = $("codexCliUpdate"), message = $("codexCliUpdateStatus");
  if (!button || !message) return;
  const current = typeof status?.current_version === "string" ? status.current_version : null,
    latest = typeof status?.latest_version === "string" ? status.latest_version : null,
    updateAvailable = Boolean(status?.update_available && codexCliUpdateIsNewer(current, latest)),
    executionActive = isActiveRun(latestStatus || {});
  // A pre-install polling response can arrive after the verified install response.
  // Never present an update unless its version is strictly newer than the active CLI.
  latestCodexCliUpdateStatus = { ...status, update_available: updateAvailable };
  button.hidden = !updateAvailable;
  button.disabled = Boolean(updateAvailable && executionActive);
  button.title = button.disabled ? t("ui.codex_cli_update_execution_active") : "";
  if (updateAvailable && executionActive) {
    message.textContent = t("ui.codex_cli_update_execution_active");
  } else if (updateAvailable && latest) {
    message.textContent = t("ui.codex_cli_update_available", { version: latest });
  } else if (current) {
    message.textContent = t("ui.codex_cli_current", { version: current });
  } else {
    message.textContent = t("ui.codex_cli_update_unavailable");
  }
}
async function checkCodexCliUpdate() {
  try {
    const response = await fetch("/api/codex-cli-update", { cache: "no-store" });
    if (!response.ok) throw Error();
    renderCodexCliUpdate(await response.json());
  } catch {
    renderCodexCliUpdate({ state: "unavailable", update_available: false });
  }
}
function installCodexCliUpdate() {
  const button = $("codexCliUpdate"), message = $("codexCliUpdateStatus");
  if (!button || button.hidden || button.disabled) return;
  confirmDashboardAction(
    t("ui.codex_cli_update"),
    t("ui.codex_cli_update_confirmation"),
    t("ui.codex_cli_update"),
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    message.textContent = t("ui.codex_cli_update_installing");
    fetch("/api/codex-cli-update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body?.error || "codex_cli_update_failed");
        const version = typeof result.body?.current_version === "string" ? result.body.current_version : "";
        if (version) $("rateLimitProvider").textContent = t("ui.codex_cli_provider", { version });
        latestCodexCliUpdateStatus = version
          ? { state: "current", update_available: false, current_version: version, latest_version: version }
          : null;
        button.hidden = true;
        message.textContent = result.body?.updated
          ? t("ui.codex_cli_updated", { version })
          : t("ui.codex_cli_current", { version });
      })
      .catch((error) => {
        const key = error instanceof Error ? error.message : "codex_cli_update_failed";
        message.textContent = t("ui." + key, {}, t("ui.codex_cli_update_failed"));
      })
      .finally(() => { button.disabled = false; });
  });
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
        const messages = {
          reset: t("ui.reset_used"),
          nothingToReset: t("ui.reset_nothing"),
          noCredit: t("ui.reset_no_credit"),
          alreadyRedeemed: t("ui.reset_already_redeemed"),
        };
        if (!result.ok && !messages[result.body?.outcome]) {
          throw Error(
            typeof result.body?.error === "string"
              ? result.body.error
              : t("ui.reset_failed"),
          );
        }
        status.textContent = messages[result.body.outcome] || t("ui.reset_processed");
        if (result.body.rate_limits) rateLimits(result.body.rate_limits);
      })
      .catch((error) => {
        status.textContent = error instanceof Error ? error.message : t("ui.reset_failed");
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
    locale.number(Number(x?.cpu_percent || 0), { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "%";
  $("codexProcesses").textContent = x?.process_count ?? 0;
  $("codexGpu").textContent = x?.gpu_status || t("format.not_available");
}
function reviewerPresentationState(status = {}) {
  const watcherState = String(status?.watcher_state || "").toUpperCase();
  if (watcherState === "WAITING_FOR_OPERATOR_MERGE") return "waiting_operator";
  if (watcherState === "ENGINEERING_RUN_STALE") return "stale";
  return "active";
}
function activeReviewerAgents(items, executionStatus = {}) {
  const reviewActive = String(executionStatus?.current_phase || "").toUpperCase() === "CAPABILITY_REVIEW";
  // A later phase may retain only a fully successful review as compact
  // historical evidence. Running, failed or incomplete projections stay
  // phase-scoped so they can never look like live reviewer work.
  const completedReview = Array.isArray(items) && items.length > 0
    && items.every((agent) => String(agent?.status || "").toLowerCase() === "completed");
  const agents = (reviewActive || completedReview) && Array.isArray(items) ? items : [];
  let card = $("activeReviewerAgents");
  if (!card) {
    card = document.createElement("section");
    card.id = "activeReviewerAgents";
    card.className = "card reviewer-agents";
    card.innerHTML = `<strong>${t("ui.reviewer_agents")}</strong><p class="estimate-meta" id="activeReviewerSummary"></p><div class="reviewer-agents__list" id="activeReviewerList"></div>`;
    $("currentRun")?.querySelector(".current-run__grid")?.append(card);
  }
  card.hidden = !agents.length;
  if (!agents.length) {
    $("activeReviewerSummary").textContent = "";
    $("activeReviewerList").replaceChildren();
    return;
  }
  const presentation = reviewerPresentationState(executionStatus),
    running = agents.filter((agent) => agent?.status === "running").length,
    completed = agents.filter((agent) => ["completed", "failed"].includes(agent?.status)).length,
    summary = $("activeReviewerSummary"),
    list = $("activeReviewerList");
  summary.textContent = presentation === "waiting_operator"
    ? t("ui.reviewer_waiting_operator", { count: agents.length })
    : presentation === "stale"
      ? t("ui.reviewer_stale", { count: agents.length })
      : running
        ? t("ui.reviewer_running", { running, count: agents.length })
        : t("ui.reviewer_completed", { completed, count: agents.length });
  list.replaceChildren();
  for (const agent of agents) {
    const row = document.createElement("article"), header = document.createElement("div"),
      name = document.createElement("p"), meta = document.createElement("p"),
      indicator = document.createElement("span"), rawStatus = String(agent?.status || "").toLowerCase(),
      status = rawStatus === "running" && presentation !== "active" ? presentation : rawStatus;
    const isRunning = status === "running";
    const isCompleted = ["completed", "uitgevoerd"].includes(status);
    row.className = "reviewer-agent";
    header.className = "reviewer-agent__header";
    name.className = "reviewer-agent__name";
    meta.className = "reviewer-agent__meta";
    name.textContent = reviewerLabel(agent.reviewer);
    meta.textContent = `${reviewerCapabilityLabel(agent.capability || "ENGINEERING")} · ${reviewerStatusLabel(status || "selected")}`;
    if (isRunning || isCompleted) {
      indicator.className = `reviewer-agent__status reviewer-agent__status--${isRunning ? "running" : "completed"}`;
      indicator.setAttribute("role", "status");
      indicator.setAttribute("aria-label", isRunning ? t("ui.reviewer_status_running") : t("ui.reviewer_status_completed"));
      indicator.setAttribute("title", indicator.getAttribute("aria-label"));
      if (isCompleted) indicator.textContent = "✓";
    }
    header.append(name, indicator);
    row.append(header, meta);
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
  syncInboxLocationChangeAvailability(depth);
  $("queueSummary").textContent =
    depth === 0
      ? t("queue.summary_zero")
      : t("queue.summary", {
        count: depth,
        item: locale.plural(depth, "queue.prompt", "queue.prompts"),
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
  const queueItemTitle = (item, filename) => {
    if (item.title_kind !== "producer_submission") return item.title || filename;
    const producerType = String(item.producer_type || "UNKNOWN").trim().toLowerCase();
    const actionIntent = String(item.action_intent || "UNSPECIFIED").trim().toLowerCase();
    return t("queue.producer_submission", {
      producer: t(`queue.producer.${producerType}`, {}, producerType),
      intent: t(`queue.intent.${actionIntent}`, {}, actionIntent.replaceAll("_", " ")),
    });
  };
  items.forEach((item, index) => {
    const row = document.createElement("li"),
      number = document.createElement("span"),
      body = document.createElement("div"),
      title = document.createElement("span"),
      meta = document.createElement("div"),
      modified = Date.parse(item.modified_at || ""),
      filename = item.filename || t("format.not_available"),
      displayTitle = queueItemTitle(item, filename);
    row.className = "queue-item";
    row.setAttribute(
      "aria-label",
      t("queue.position", { position: index + 1, title: displayTitle }),
    );
    number.className = "queue-item__number";
    number.textContent = String(index + 1);
    title.className = "queue-item__title";
    meta.className = "queue-item__meta";
    title.textContent = displayTitle;
    meta.textContent = t("queue.filename", {
      filename,
      modified: Number.isFinite(modified)
        ? locale.dateTime(new Date(modified))
        : t("format.timestamp_unavailable"),
    });
    const defer = item.queue_source === "CENTRAL" ? null : document.createElement("button");
    if (defer) {
      defer.className = "queue-defer";
      defer.type = "button";
      defer.textContent = t("queue.defer_action");
      defer.title = t("queue.defer_action");
      defer.setAttribute("aria-label", t("queue.defer_action"));
      defer.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        deferQueueItem(item, defer);
      });
    }
    body.append(title, meta);
    row.append(number, body);
    if (defer) row.append(defer);
    container.append(row);
  });
}
function deferQueueItem(item, button) {
  const filename = String(item?.filename || "");
  if (!filename) return;
  const displayTitle = item.title_kind === "producer_submission"
    ? t("queue.producer_submission", {
      producer: t(`queue.producer.${String(item.producer_type || "UNKNOWN").trim().toLowerCase()}`, {}, String(item.producer_type || "UNKNOWN")),
      intent: t(`queue.intent.${String(item.action_intent || "UNSPECIFIED").trim().toLowerCase()}`, {}, String(item.action_intent || "UNSPECIFIED").replaceAll("_", " ")),
    })
    : String(item.title || filename);
  confirmDashboardAction(
    t("queue.defer_title"),
    t("queue.defer_description", { title: displayTitle }),
    t("queue.defer_action"),
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    fetch("/api/queue-defer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json().catch(() => ({})) }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("queue.defer_failed"));
        if (latestStatus) {
          const items = (latestStatus.queue_items || []).filter((entry) => entry?.filename !== filename);
          latestStatus = { ...latestStatus, queue_items: items, queue_depth: Math.max(0, Number(latestStatus.queue_depth || items.length + 1) - 1) };
          queueItems(items, latestStatus.queue_depth);
        }
        return refreshDashboard();
      })
      .catch((error) => showDashboardError(error.message, t("queue.defer_failed")))
      .finally(() => { button.disabled = false; });
  });
}
function renderInboxBlocker(status) {
  const blocker = $("inboxBlocker"),
    runtimeInvocationFailed = status?.watcher_state === "HOST_PREFLIGHT_FAILED" &&
      String(status?.diagnostic || "").includes("runtime_invocation"),
    managedBranchBlocked = String(status?.diagnostic || "").includes("managed_expected_branch");
  blocker.replaceChildren();
  blocker.hidden = !(runtimeInvocationFailed || managedBranchBlocked);
  blocker.classList.toggle("queue-blocker--error", managedBranchBlocked);
  if (runtimeInvocationFailed) blocker.textContent = t("queue.runtime_invocation_blocked");
  if (!managedBranchBlocked) return;
  const message = document.createElement("p"), repair = document.createElement("button");
  message.textContent = t("queue.managed_branch_blocked");
  repair.className = "queue-blocker__repair";
  repair.type = "button";
  repair.textContent = t("queue.managed_branch_recovery_action");
  repair.addEventListener("click", submitManagedBranchRecovery);
  blocker.append(message, repair);
}
function renderWorkspaceGitLock(lock) {
  const state = $("technicalGitLockState"), detail = $("technicalGitLockDetail"),
    recover = $("technicalGitLockRecover"), recoveryStatus = $("technicalGitLockRecoveryStatus"),
    active = lock?.state === "active" || lock?.state === "stale",
    stale = lock?.stale === true;
  state.textContent = active ? t("technical.git_lock_active") : t("technical.git_lock_free");
  detail.hidden = !active;
  detail.textContent = active
    ? t("technical.git_lock_waiting") + (Number.isFinite(lock?.age_seconds)
      ? " " + t("technical.git_lock_since", { value: `${Math.max(1, Math.floor(lock.age_seconds / 60))} min` })
      : "") + (stale ? " " + t("technical.git_lock_stale") : "")
    : "";
  recover.hidden = !stale;
  recover.onclick = stale ? submitStaleGitLockRecovery : null;
  if (!stale) recoveryStatus.textContent = "";
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
function isUnhelpfulHttpServerDebugLog(entry) {
  return (
    String(entry?.level || "").toUpperCase() === "DEBUG" &&
    String(entry?.event || "").trim().toLowerCase() ===
      "http_server_message" &&
    String(entry?.diagnostic || "").trim() === '"%s" %s %s'
  );
}
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
        if (isUnhelpfulHttpServerDebugLog(entry)) return null;
        const known = new Set([
            "timestamp",
            "level",
            "event",
            "run_id",
            "component",
            "line",
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
    })
    .filter(Boolean);
}
function logEventLabel(value) {
  const event = String(value || "").trim();
  if (!event) return t("logs.unknown_event");
  const readable = event
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
  return t(`log_event.${event}`, {}, t("logs.event_fallback", { event: readable }));
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
const LOG_LEVEL_SEVERITY = Object.freeze({ DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 });
function logMeetsMinimumLevel(entry, minimum) {
  if (!minimum || minimum === "DEBUG") return true;
  const level = String(entry?.level || "").toUpperCase();
  return (LOG_LEVEL_SEVERITY[level] ?? -1) >= LOG_LEVEL_SEVERITY[minimum];
}
function localDayStart(value = new Date()) {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
}
function selectedLogTimeRange() {
  const preset = $("logTimePreset")?.value || "";
  if (preset === "today" || preset === "yesterday") {
    const now = new Date(), offset = preset === "yesterday" ? -1 : 0,
      start = localDayStart(new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset));
    return { start, end: start + 24 * 60 * 60 * 1000 };
  }
  if (preset === "day") {
    const value = $("logSpecificDate")?.value;
    if (!value) return null;
    const start = Date.parse(`${value}T00:00`);
    return Number.isFinite(start) ? { start, end: start + 24 * 60 * 60 * 1000 } : null;
  }
  if (preset === "range") {
    const from = Date.parse($("logDateFrom")?.value || ""), to = Date.parse($("logDateTo")?.value || "");
    if (!Number.isFinite(from) && !Number.isFinite(to)) return null;
    return {
      start: Number.isFinite(from) ? from : Number.NEGATIVE_INFINITY,
      end: Number.isFinite(to) ? to : Number.POSITIVE_INFINITY,
      inclusiveEnd: true,
    };
  }
  return null;
}
function updateLogTimeFilterControls() {
  const preset = $("logTimePreset")?.value || "";
  $("logSpecificDateControl").hidden = preset !== "day";
  $("logDateFromControl").hidden = preset !== "range";
  $("logDateToControl").hidden = preset !== "range";
  synchronizeLogRangeBounds();
}
function synchronizeLogRangeBounds() {
  const from = $("logDateFrom"), to = $("logDateTo");
  if (!from || !to) return;
  // datetime-local values use an order-preserving ISO-like representation.
  // Keep the native picker and any already entered end value valid together.
  to.min = from.value;
  if (from.value && to.value && to.value < from.value) to.value = from.value;
}
function updateLogDateClearButtons() {
  for (const input of document.querySelectorAll(
    "#logSpecificDate, #logDateFrom, #logDateTo",
  )) {
    const button = document.querySelector(
      `[data-clear-log-date="${input.id}"]`,
    );
    if (button) button.hidden = !input.value;
  }
}
function installLogDateClearButtons() {
  const label = t("action.clear_date");
  for (const input of document.querySelectorAll(
    "#logSpecificDate, #logDateFrom, #logDateTo",
  )) {
    const field = document.createElement("span"), button = document.createElement("button");
    field.className = "log-date-control__field";
    input.replaceWith(field);
    field.append(input);
    button.className = "log-date-control__clear";
    button.type = "button";
    button.hidden = !input.value;
    button.textContent = "×";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("data-i18n-title", "action.clear_date");
    button.setAttribute("data-i18n-aria-label", "action.clear_date");
    button.dataset.clearLogDate = input.id;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      input.value = "";
      synchronizeLogRangeBounds();
      updateLogDateClearButtons();
      refreshComponentLogsForFilters();
    });
    field.append(button);
    input.addEventListener("input", updateLogDateClearButtons);
  }
}
function entryMatchesLogTimeRange(entry) {
  const range = selectedLogTimeRange();
  if (!range) return true;
  const timestamp = logTimestamp(entry);
  if (!timestamp) return false;
  return timestamp >= range.start && (range.inclusiveEnd ? timestamp <= range.end : timestamp < range.end);
}
function loadComponentLogs() {
  // Retain this callable for old dashboard markup, but never reintroduce the
  // former latest-100 sample. Every interactive view is a server-filtered
  // page from the full retained SQLite history.
  refreshComponentLogs({}, true);
}
const CHAT_HISTORY_LIMIT = 20;
let chatContextRun = "";
let chatContextEntry = null;
let chatHistory = [];
function updateChatHistoryCount(count) {
  promptHistoryEntries = promptHistoryEntries.map((entry) =>
    entry.run_id === chatContextRun ? { ...entry, chat_message_count: count } : entry,
  );
  renderPromptHistory();
}
function normaliseChatMessages(value) {
  return Array.isArray(value)
    ? value.filter((entry) => entry && ["user", "assistant"].includes(entry.role) && typeof entry.text === "string")
        .map((entry) => ({
          role: entry.role,
          text: entry.text,
          created_at: typeof entry.created_at === "string" ? entry.created_at : "",
        }))
        .slice(-CHAT_HISTORY_LIMIT)
    : [];
}
function chatMessageTimestampLabel(value, now = new Date()) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return t("chat.timestamp_unavailable");
  const date = new Date(timestamp), elapsed = now.getTime() - timestamp;
  if (elapsed >= 0 && elapsed < 60_000) return t("chat.just_now");
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime(),
    messageDayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime(),
    dayDifference = Math.round((dayStart - messageDayStart) / 86_400_000),
    time = new Intl.DateTimeFormat(dashboardLocale, {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(date);
  let day;
  if (dayDifference === 0) day = t("chat.today");
  else if (dayDifference === 1) day = t("chat.yesterday");
  else if (dayDifference > 1 && dayDifference < 7)
    day = new Intl.DateTimeFormat(dashboardLocale, { weekday: "long" }).format(date);
  else
    day = new Intl.DateTimeFormat(dashboardLocale, {
      weekday: "long", day: "numeric", month: "long", year: "numeric",
    }).format(date);
  return `${day}, ${time}`;
}
function renderLegacyChatMessage(role, text, createdAt = new Date().toISOString()) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    timestamp = document.createElement("time"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = t(role === "user" ? "chat.user" : "chat.assistant");
  timestamp.className = "chat-message__timestamp";
  timestamp.dateTime = createdAt;
  timestamp.textContent = chatMessageTimestampLabel(createdAt);
  body.className = "chat-message__body";
  body.textContent = text;
  item.append(label, timestamp, body);
  $("chatMessages").append(item);
  item.scrollIntoView({ block: "nearest" });
}
function renderChatHistory() {
  const container = $("chatMessages");
  container.replaceChildren();
  chatHistory.forEach((entry) => chatMessage(entry.role, entry.text, entry.created_at));
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
  input.value = "";
  // Show the submitted question immediately. The persisted conversation remains
  // authoritative and replaces this optimistic projection once the reply is
  // returned by the server.
  const createdAt = new Date().toISOString();
  chatHistory = [...chatHistory, { role: "user", text: message, created_at: createdAt }]
    .slice(-CHAT_HISTORY_LIMIT);
  chatMessage("user", message, createdAt);
  updateChatActions();
  fetch("/api/codex-chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: message,
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
      $("chatModel").textContent =
        result.body.model || $("chatModel").textContent;
      chatHistory = normaliseChatMessages(result.body.messages);
      renderChatHistory();
      updateChatHistoryCount(chatHistory.length);
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
  chatContextEntry = { ...entry, run_id: chatContextRun };
  chatHistory = [];
  $("promptHistoryChatTitle").textContent = t("history.execution_chat_title");
  $("promptHistoryChatDescription").textContent = t("history.chat_description");
  $("chatStatus").textContent = t("history.chat_loading");
  $("chatSend").disabled = true;
  renderChatHistory();
  updateChatActions();
  const modal = $("promptHistoryChatModal");
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
  fetch("/api/prompt-history/" + encodeURIComponent(chatContextRun) + "/chat", { cache: "no-store" })
    .then(async (response) => ({ ok: response.ok, body: await response.json() }))
    .then((result) => {
      if (!result.ok) throw Error("chat unavailable");
      chatHistory = normaliseChatMessages(result.body.messages);
      renderChatHistory();
      updateChatActions();
      $("chatStatus").textContent = "";
    })
    .catch(() => { $("chatStatus").textContent = t("chat.unavailable"); })
    .finally(() => { $("chatSend").disabled = false; });
}
function fallbackCopy(value) {
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  // A modal dialog makes everything outside it inert. iOS Safari then refuses
  // to focus a temporary body-level textarea, so keep the selection inside the
  // active dialog when a copy action originates there.
  (document.querySelector("dialog[open]") || document.body).append(area);
  area.focus();
  area.select();
  area.setSelectionRange(0, area.value.length);
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw Error(t("copy.failed"));
}
function isIOSBrowser() {
  return /iPad|iPhone|iPod/.test(navigator.platform || navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}
function copyText(value) {
  const modernClipboard = navigator.clipboard && window.isSecureContext;
  // iOS Safari permits the legacy copy command only while the original tap is
  // still being handled. Other browsers use the Clipboard API first: some of
  // them report a successful legacy copy without updating the system clipboard.
  if (isIOSBrowser()) {
    try {
      fallbackCopy(value);
      showCopyToast();
      return Promise.resolve();
    } catch (fallbackError) {
      if (!modernClipboard) return Promise.reject(fallbackError);
      return navigator.clipboard.writeText(value).then(() => showCopyToast());
    }
  }
  if (modernClipboard)
    return navigator.clipboard.writeText(value).then(showCopyToast, () => {
      fallbackCopy(value);
      showCopyToast();
    });
  try {
    fallbackCopy(value);
    showCopyToast();
    return Promise.resolve();
  } catch (fallbackError) {
    return Promise.reject(fallbackError);
  }
}
let copyToastTimer;
function showDashboardToast(message) {
  const toast = $("copyToast");
  if (!toast) return;
  clearTimeout(copyToastTimer);
  toast.textContent = message;
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
function showCopyToast() { showDashboardToast(t("copy.success")); }
const PREFLIGHT_PRESENTATIONS = Object.freeze([
  ["host_preflight", [
    ["hostPreflightStatus", "outcome"],
    ["hostPreflightTimestamp", "timestamp", "timestamp"],
    ["executionHostRuntimeVersion", "runtime_version"],
    ["executionHostRuntimePath", "runtime_path"],
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
    const values = [["driftSeverity", "severity"], ["driftComponent", "affected_component"],
      ["driftExpected", "expected_value"], ["driftObserved", "observed_value"],
      ["driftResolution", "resolution_recommendation"]];
    for (const [id, field] of values) if ($(id)) $(id).textContent = driftPresentationValue(field, drift);
  }
}
function preflightNeedsAttention(preflight) {
  const outcome = String(preflight?.outcome || "").toUpperCase();
  return ["FAILED", "BLOCKED", "UNAVAILABLE"].includes(outcome);
}
function renderTechnicalDiagnosis(status = {}, snapshot = {}) {
  const section = $("technicalDetails"), summary = $("technicalHealthySummary"), grid = $("technicalDiagnosisDetails"), description = $("technicalDetailsDescription");
  if (!section || !summary || !grid || !description) return;
  const phase = String(status?.current_phase || "").toUpperCase();
  const watcher = String(status?.watcher_state || "").toUpperCase();
  const needsAttention = ["BLOCKED", "FAILED", "PAUSED"].includes(phase) ||
    ["JOB_BLOCKED", "JOB_FAILED", "HOST_PREFLIGHT_FAILED", "PAUSED"].includes(watcher) ||
    hasVisibleStaleLifecycle(status) ||
    Boolean(snapshot?.current_drift?.drift_id) ||
    snapshot?.workspace_git_lock?.stale === true ||
    [snapshot.host_preflight, snapshot.workspace_preflight, snapshot.capability_preflight].some(preflightNeedsAttention) ||
    Boolean(String(status?.diagnostic || "").trim());
  const active = isActiveRun(status);
  section.hidden = !active && !needsAttention;
  grid.hidden = !needsAttention;
  summary.hidden = !(active && !needsAttention);
  summary.textContent = active && !needsAttention ? t("technical.host_check_passed") : "";
  description.textContent = t(needsAttention ? "description.technical_details_attention" : "description.technical_details");
  // A healthy active run exposes the compact host-check summary, but should
  // not repeatedly expand the operator's page on every snapshot refresh.
  // Attention evidence remains deliberately prominent and opens this section.
  section.open = needsAttention;
}
function executionContextValue(value) {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (value && typeof value === "object") return value.title || value.objective || value.id || value.message || value.reference || value.value || "";
  return "";
}
function localFolderPath(value) {
  return typeof value === "string" && value.startsWith("/") ? value.trim() : "";
}
async function openLocalFolder(directoryPath) {
  const path = localFolderPath(directoryPath);
  if (!path) {
    showDashboardError(t("workspace.open_local_folder_failed"), t("workspace.open_local_folder_failed"));
    return;
  }
  try {
    const response = await fetch("/api/open-local-directory", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ directory_path: path }),
    });
    // A reverse proxy or a temporarily restarting dashboard can return a
    // non-JSON error page.  Do not surface a browser parser exception to the
    // operator; the route has one clear, actionable fallback instead.
    const outcome = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(outcome?.error || t("workspace.open_local_folder_failed"));
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    showDashboardError(
      message === "The string did not match the expected pattern."
        ? t("workspace.open_local_folder_failed")
        : message || t("workspace.open_local_folder_failed"),
      t("workspace.open_local_folder_failed"),
    );
  }
}
function configureLocalFolderButton(button, value, { containingFolder = false } = {}) {
  const path = localFolderPath(value);
  button.classList.add("local-folder-link");
  button.type = "button";
  button.textContent = String(value || t("format.not_available"));
  button.disabled = !path;
  button.onclick = null;
  button.removeAttribute("title");
  button.removeAttribute("aria-label");
  if (!path) {
    return;
  }
  const directoryPath = containingFolder ? path.replace(/\/[^/]+$/, "") : path;
  const label = t(
    containingFolder ? "workspace.open_containing_folder" : "workspace.open_local_folder",
    { path },
  );
  button.title = label;
  button.setAttribute("aria-label", label);
  button.onclick = () => void openLocalFolder(directoryPath);
}
function localFolderButton(value, options = {}) {
  const button = document.createElement("button");
  configureLocalFolderButton(button, value, options);
  return button;
}
function replaceWithLocalFolderButton(element, options = {}) {
  if (!element) return null;
  const button = localFolderButton(element.textContent.trim(), options);
  if (element.id) button.id = element.id;
  element.replaceWith(button);
  return button;
}
function executionContextField(label, value, badge = false, folder = false) {
  const field = document.createElement("p"), caption = document.createElement("span"), content = document.createElement("span");
  field.className = "field";
  caption.className = "label";
  caption.textContent = label;
  const supplied = executionContextValue(value);
  const output = folder && localFolderPath(supplied)
    ? localFolderButton(supplied)
    : content;
  if (output === content) content.textContent = supplied || t("execution_context.not_supplied");
  if (badge) output.classList.add("execution-context__phase");
  field.append(caption, output);
  return field;
}
function inheritModalAccent(modal, trigger) {
  const source = trigger?.closest(".current-run,[data-modal-accent-source],.dashboard-modal-shell");
  const sample = source && document.createElement("span");
  if (sample) {
    sample.style.color = "var(--category-color)";
    source.append(sample);
  }
  const accent = sample ? getComputedStyle(sample).color : "";
  sample?.remove();
  if (accent) modal.style.setProperty("--modal-parent-accent", accent);
  else modal.style.removeProperty("--modal-parent-accent");
}
function openExecutionModeModal(event) {
  const modal = $("executionModeModal");
  if (!modal) return;
  inheritModalAccent(modal, event?.currentTarget);
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
}
function executionModeField(value) {
  const field = executionContextField(t("field.execution_mode"), value);
  const content = field.lastElementChild;
  const row = document.createElement("span");
  row.className = "execution-mode-field__value";
  content.replaceWith(row);
  row.append(content, executionModeInfoButton());
  field.classList.add("execution-mode-field");
  return field;
}
function executionModeInfoButton() {
  const info = document.createElement("button");
  info.className = "component-info execution-mode-info";
  info.type = "button";
  info.setAttribute("aria-label", t("execution_mode_info.open"));
  info.title = t("execution_mode_info.open");
  info.innerHTML = '<span aria-hidden="true">i</span>';
  info.addEventListener("click", openExecutionModeModal);
  return info;
}
function renderExecutionContext(context, execution = {}) {
  const card = $("executionContext");
  if (!card) return;
  card.hidden = false;
  card.classList.add("execution-context--primary");
  const hostFields = [
    [t("field.execution_mode"), execution.execution_mode, true],
    [t("field.repository"), execution.target_repository],
    [t("detail.target_checkout"), execution.checkout_path],
    [t("ui.active_branch"), execution.active_branch],
  ].filter(([, value]) => executionContextValue(value));
  if (!context || typeof context !== "object") {
    card.replaceChildren(
      Object.assign(document.createElement("strong"), { textContent: t("ui.execution_context") }),
      ...hostFields.map(([label, value, isExecutionMode]) => isExecutionMode ? executionModeField(value) : executionContextField(label, value, false, label === t("detail.target_checkout"))),
      Object.assign(document.createElement("p"), { textContent: t("execution_context.not_supplied") }),
    );
    return;
  }
  const planningFields = [
    [t("detail.mission_id"), context.mission_id],
    [t("execution_context.mission_title"), context.mission_title],
    [t("execution_context.mission_lifecycle"), context.mission_lifecycle],
    [t("execution_context.business_summary"), context.business_summary],
    [t("execution_context.engineering_summary"), context.engineering_summary],
    [t("execution_context.current_intent"), context.current_intent],
    [t("execution_context.current_engineering_action"), context.current_engineering_action],
    [t("execution_context.execution_phase"), context.execution_phase, true],
    [t("execution_context.planning_confidence"), context.planning_confidence],
    [t("execution_context.current_iteration"), context.current_iteration],
    [t("execution_context.mission_progress"), context.mission_progress],
    [t("execution_context.last_runtime_update"), context.last_runtime_update || context.last_updated_timestamp],
    [t("execution_context.decision_evidence_reference"), context.decision_evidence_reference || context.decision_evidence],
    [t("execution_context.decision_type"), context.decision_type],
    [t("execution_context.execution_receipt_reference"), context.execution_receipt_reference || context.last_execution_receipt],
    [t("execution_context.dispatcher_state"), context.dispatcher_state],
    [t("execution_context.approved_mission_queue_state"), context.approved_mission_queue_state],
  ].filter(([, value]) => executionContextValue(value));
  const profile = context.validation_profile && typeof context.validation_profile === "object"
    ? context.validation_profile : null;
  const suppliedFields = [
    ...(context.action_intent ? [[
      t("execution_context.action_intent"),
      t(`execution_context.action_intent.${String(context.action_intent).toLowerCase()}`, {}, humanizeIdentifier(context.action_intent)),
    ]] : []),
    ...(profile?.tier ? [[t("execution_context.validation_profile"), humanizeIdentifier(profile.tier)]] : []),
    ...(profile?.version ? [[t("execution_context.validation_profile_version"), profile.version]] : []),
    ...(Array.isArray(profile?.required_controls) && profile.required_controls.length ? [[
      t("execution_context.required_validation_controls"),
      profile.required_controls.map(humanizeIdentifier).join(", "),
    ]] : []),
    ...(context.context_version ? [[t("execution_context.version"), context.context_version]] : []),
  ];
  card.replaceChildren(
    Object.assign(document.createElement("strong"), { textContent: t("ui.execution_context") }),
    ...hostFields.map(([label, value, isExecutionMode]) => isExecutionMode ? executionModeField(value) : executionContextField(label, value, false, label === t("detail.target_checkout"))),
    ...suppliedFields.map(([label, value]) => executionContextField(label, value)),
    ...(planningFields.length
      ? planningFields.map(([label, value, badge]) => executionContextField(label, value, badge))
      : [Object.assign(document.createElement("p"), {
        className: "execution-context__planning-empty",
        textContent: t("execution_context.planning_not_supplied"),
      })]),
  );
}
function renderOperatorMergeWait(x) {
  const card = $("operatorMergeWait"), pullRequest = Number(x.pull_request);
  if (!card) return;
  const waiting = x.current_phase === "WAIT_FOR_OPERATOR_MERGE" && Number.isInteger(pullRequest) && pullRequest > 0;
  card.hidden = !waiting;
  if (!waiting) return;
  placeOperatorMergeWait();
  const repository = String(x.target_repository || "").trim();
  const href = repository ? `https://github.com/${repository.split("/").map(encodeURIComponent).join("/")}/pull/${pullRequest}` : "#";
  const link = $("operatorMergePullRequest");
  link.href = href;
  link.textContent = t("merge_wait.open_pull_request", { number: pullRequest });
  $("operatorMergeWaitDescription").textContent = t("merge_wait.description", { number: pullRequest });
  const lastCheck = x.merge_status_check?.last_successful_github_check_at;
  const lastCheckText = typeof lastCheck === "string"
    ? t("merge_wait.last_successful_check", { timestamp: formatTimestamp(lastCheck) })
    : "";
  for (const id of ["operatorMergeWaitLastCheck", "operatorMergeWaitModalLastCheck"]) {
    const field = $(id);
    field.hidden = !lastCheckText;
    field.textContent = lastCheckText;
  }
  const modal = $("operatorMergeWaitModal");
  $("operatorMergeWaitModalDescription").textContent = t("merge_wait.description", { number: pullRequest });
  const mergeKey = Number(x.reconciliation_pr) === pullRequest
    ? "lifecycle.step.wait_for_reconciliation_merge"
    : Number(x.finalization_pr) === pullRequest
      ? "lifecycle.step.wait_for_finalization_merge"
      : "lifecycle.step.wait_for_operator_merge";
  const mergeTitleKey = mergeKey === "lifecycle.step.wait_for_finalization_merge"
    ? "merge_wait.title.finalization"
    : mergeKey === "lifecycle.step.wait_for_reconciliation_merge"
      ? "merge_wait.title.reconciliation"
      : "merge_wait.title.implementation";
  $("operatorMergeWaitTitle").textContent = t(mergeTitleKey);
  $("operatorMergeWaitModalTitle").textContent = t(mergeTitleKey);
  const pullRequestStatus = openPullRequestStatusByNumber.get(pullRequest) || "waiting_for_checks";
  for (const id of ["operatorMergeWaitPullRequestStatus", "operatorMergeWaitModalPullRequestStatus"]) {
    setOpenPullRequestStatus($(id), pullRequestStatus);
  }
  const ownerApproval = openPullRequestOwnerApprovalByNumber.get(pullRequest) || "pending";
  for (const id of ["operatorMergeWaitOwnerApproval", "operatorMergeWaitModalOwnerApproval"]) {
    setOpenPullRequestOwnerApproval($(id), ownerApproval);
  }
  $("operatorMergeWaitModalContextIntro").textContent = t("merge_wait.context_intro", {
    merge: t(mergeKey), number: pullRequest,
  });
  $("operatorMergeWaitModalRunId").textContent = String(x.run_id || t("format.not_available"));
  $("operatorMergeWaitModalPrompt").textContent = String(
    x.prompt_title || x.submitted_filename || t("format.not_available"),
  );
  $("operatorMergeWaitModalPullRequest").href = href;
  $("operatorMergeWaitModalPullRequest").textContent = t("merge_wait.open_pull_request", { number: pullRequest });
  const handoffKey = `${x.run_id || ""}:${pullRequest}`;
  if (shownOperatorMergeWaitRun !== handoffKey) {
    shownOperatorMergeWaitRun = handoffKey;
    if (!modal.open) modal.showModal();
  }
}
function codexLimitRemainingPercent(rateLimits) {
  const remaining = (Array.isArray(rateLimits?.windows) ? rateLimits.windows : [])
    .map((window) => Number(window?.used_percent))
    .filter(Number.isFinite)
    .map((used) => Math.max(0, Math.min(100, 100 - used)));
  return remaining.length ? Math.min(...remaining) : null;
}
function renderCodexUsageLimitBanner(x, rateLimits) {
  const banner = $("codexUsageLimitBanner");
  if (!banner) return;
  const terminalLimit = String(x?.terminal_condition || "") === "codex_usage_limit_reached";
  const remaining = codexLimitRemainingPercent(rateLimits);
  const critical = !terminalLimit && remaining !== null && remaining < 5;
  const warning = !terminalLimit && remaining !== null && remaining < 10;
  const state = terminalLimit ? "limit" : critical ? "critical" : warning ? "warning" : null;
  banner.hidden = !state;
  banner.className = "dashboard-status-banner dashboard-status-banner--usage-" + (state || "limit");
  if (!state) return;
  const title = banner.querySelector("strong"), body = banner.querySelector("span");
  if (terminalLimit) {
    title.textContent = t("notification.codex_usage_limit.title");
    body.textContent = t("notification.codex_usage_limit.body");
    return;
  }
  const percent = locale.number(remaining, { maximumFractionDigits: 0 });
  title.textContent = t("notification.codex_usage_" + state + ".title");
  body.textContent = t("notification.codex_usage_" + state + ".body", { percent });
}
function renderCodexCapacityReserveBanner(rateLimits) {
  const banner = $("codexCapacityReserveBanner"), message = $("codexCapacityReserveMessage");
  if (!banner || !message) return;
  const reserve = Number(dashboardConfiguration.codex_capacity_reserve_percent || 0);
  const remaining = codexLimitRemainingPercent(rateLimits);
  const reached = reserve > 0 && remaining !== null && remaining < reserve;
  banner.hidden = !reached;
  if (!reached) return;
  message.textContent = t("notification.codex_capacity_reserve.body", {
    remaining: locale.number(remaining, { maximumFractionDigits: 0 }),
    reserve: locale.number(reserve, { maximumFractionDigits: 0 }),
  });
}
const CODEX_CAPACITY_RESERVE_OPTIONS = Object.freeze([0, 5, 10, 15, 20, 25, 50, 75]);
function syncCodexCapacityReserveOptions(rateLimits = latestDashboardSnapshot?.rate_limits) {
  const select = $("configurationCodexCapacityReserve");
  if (!select) return;
  const remaining = codexLimitRemainingPercent(rateLimits);
  const selected = String(dashboardConfiguration.codex_capacity_reserve_percent ?? select.dataset.savedValue ?? 0);
  const allowed = CODEX_CAPACITY_RESERVE_OPTIONS.filter((value) => value === 0 || (remaining !== null && value <= remaining));
  select.replaceChildren(...allowed.map((value) => {
    const option = document.createElement("option");
    option.value = String(value);
    option.dataset.capacityReserveTemplate = value ? "configuration.capacity_reserve_percent" : "configuration.capacity_reserve_none";
    option.textContent = value ? t(option.dataset.capacityReserveTemplate, { percent: value }) : t(option.dataset.capacityReserveTemplate);
    return option;
  }));
  if (allowed.includes(Number(selected))) select.value = selected;
  syncDashboardSelectPicker(select);
}
let githubRateLimitRefreshInFlight = false;
async function refreshGithubRateLimit() {
  const banner = $("githubRateLimitBanner"), message = $("githubRateLimitMessage"), button = $("githubRateLimitRefresh");
  if (!banner || !message || !button || githubRateLimitRefreshInFlight) return;
  githubRateLimitRefreshInFlight = true;
  button.disabled = true;
  try {
    const response = await fetch("/api/github-rate-limit", { cache: "no-store" });
    const status = response.ok ? await response.json() : null;
    const limited = status?.limited === true;
    banner.hidden = !limited;
    if (limited) {
      const resetAt = Number(status?.reset_at);
      message.textContent = Number.isFinite(resetAt) && resetAt > 0
        ? t("notification.github_rate_limit.body_reset", { reset: locale.dateTime(new Date(resetAt * 1e3)) })
        : t("notification.github_rate_limit.body");
    }
  } catch {
    // A failed diagnostics poll is not proof of a GitHub quota condition.
    banner.hidden = true;
  } finally {
    button.disabled = false;
    githubRateLimitRefreshInFlight = false;
  }
}

// Browsers otherwise put initial dialog focus on the first close button.
// Confirmation dialogs focus their primary action, except when that action is
// destructive: then the safe secondary action is the deliberate default.
// Evidence-only modals deliberately leave focus outside the dialog so no
// control looks selected.
function resetDashboardModalInitialFocus(modal) {
  requestAnimationFrame(() => {
    if (!modal?.open) return;
    const secondary = modal.querySelector("button.dashboard-modal-shell__action:not(.dashboard-modal-shell__action--primary):not([disabled]), a.dashboard-modal-shell__action:not(.dashboard-modal-shell__action--primary)[href]");
    const primary = modal.querySelector("button.dashboard-modal-shell__action--primary:not([disabled]), a.dashboard-modal-shell__action--primary[href]");
    if (modal.classList.contains("dashboard-modal-shell--destructive") && secondary) {
      secondary.focus({ preventScroll: true });
      return;
    }
    if (primary) {
      primary.focus({ preventScroll: true });
      return;
    }
    if (modal.contains(document.activeElement)) document.activeElement.blur();
  });
}
document.querySelectorAll("dialog.dashboard-modal-shell").forEach((modal) => {
  modal.addEventListener("toggle", () => {
    if (modal.open) resetDashboardModalInitialFocus(modal);
  });
});
function lifecycleLabel(step) {
  return t(step?.presentation_key || "lifecycle.step.unknown", {}, String(step?.id || t("format.unknown")));
}
function lifecycleStateLabel(state) {
  return t("lifecycle.state." + String(state || "UNKNOWN").toLowerCase(), {}, String(state || "UNKNOWN"));
}
function isOperatorMergeStep(step) {
  const id = String(step?.id || "").toUpperCase();
  const key = String(step?.presentation_key || "");
  return ["WAIT_FOR_OPERATOR_MERGE", "WAIT_FOR_FINALIZATION_MERGE", "WAIT_FOR_RECONCILIATION_MERGE"].includes(id)
    || ["lifecycle.step.wait_for_operator_merge", "lifecycle.step.wait_for_finalization_merge", "lifecycle.step.wait_for_reconciliation_merge"].includes(key);
}
function isLifecycleStartStep(step) {
  return String(step?.id || "").toUpperCase() === "START"
    || String(step?.presentation_key || "") === "lifecycle.step.start";
}
function lifecycleDetailField(label, value) {
  const field = document.createElement("div"); field.className = "field";
  field.append(
    Object.assign(document.createElement("span"), { className: "label", textContent: label }),
    Object.assign(document.createElement("span"), { textContent: value || t("format.unavailable") }),
  );
  return field;
}
function lifecycleDetailStatusField(state) {
  const normalized = String(state || "UNKNOWN").toLowerCase();
  const indicator = document.createElement("span");
  indicator.className = "indicator lifecycle-detail-modal__status-indicator";
  indicator.setAttribute("aria-hidden", "true");
  if (["completed", "complete"].includes(normalized)) indicator.classList.add("indicator--green");
  else if (normalized === "active") indicator.classList.add("indicator--blue");
  else if (normalized === "blocked") indicator.classList.add("indicator--orange");
  else if (normalized === "failed") indicator.classList.add("indicator--red");
  const value = document.createElement("span");
  value.className = "lifecycle-detail-modal__status-value";
  value.textContent = lifecycleStateLabel(state);
  const field = document.createElement("div"); field.className = "field";
  const label = document.createElement("span");
  label.className = "label"; label.textContent = t("lifecycle.detail_state");
  const status = document.createElement("span");
  status.className = "lifecycle-detail-modal__status";
  status.append(indicator, value);
  field.append(label, status);
  return field;
}
function lifecyclePhaseTiming(spans) {
  const phases = new Map();
  for (const span of spans) {
    if (!span || typeof span !== "object") continue;
    const phase = String(span.phase || "").trim();
    if (!phase) continue;
    const previous = phases.get(phase) || { phase, duration_ms: 0, hasDuration: true, outcome: "UNKNOWN" };
    const duration = Number(span.duration_ms);
    if (Number.isFinite(duration) && duration >= 0) previous.duration_ms += duration;
    else previous.hasDuration = false;
    // Spans are stored in runtime order, so the final outcome is the one the
    // operator needs in the compact lifecycle view.
    previous.outcome = span.outcome;
    phases.set(phase, previous);
  }
  return [...phases.values()].map(({ hasDuration, ...phase }) => ({
    ...phase,
    duration_ms: hasDuration ? phase.duration_ms : null,
  }));
}
function lifecycleQualityEvidence(step) {
  const evidence = Array.isArray(step?.quality_evidence) ? step.quality_evidence : [];
  if (!evidence.length) return null;
  const section = document.createElement("section");
  section.className = "lifecycle-detail-modal__quality-evidence";
  section.append(Object.assign(document.createElement("h3"), {
    textContent: t("lifecycle.detail_quality_evidence"),
  }));
  const list = document.createElement("ol");
  list.className = "lifecycle-detail-modal__phase-list";
  for (const item of evidence) {
    if (!item || typeof item !== "object") continue;
    const activity = String(item.activity || "").trim();
    const result = String(item.result || "").trim();
    if (!activity || !result) continue;
    const row = document.createElement("li");
    row.append(
      Object.assign(document.createElement("strong"), {
        textContent: t("lifecycle.quality_evidence." + activity.toLowerCase(), {}, activity),
      }),
      Object.assign(document.createElement("span"), { textContent: result }),
    );
    list.append(row);
  }
  if (!list.childElementCount) return null;
  section.append(list);
  return section;
}
function lifecycleRepairEvidence(step) {
  const audit = Array.isArray(step?.repair_audit) ? step.repair_audit : [];
  if (!audit.length) return null;
  const section = document.createElement("section");
  section.className = "lifecycle-detail-modal__repair-evidence";
  section.append(Object.assign(document.createElement("h3"), {
    textContent: t(step?.id === "LOCAL_REPOSITORY_VALIDATION" ? "lifecycle.detail_local_validation_evidence" : step?.repair_evidence_key || "lifecycle.detail_repair_evidence"),
  }));
  for (const item of audit) {
    if (!item || typeof item !== "object") continue;
    const iteration = String(item.iteration || "").trim();
    if (!iteration) continue;
    const heading = document.createElement("h4");
    heading.textContent = t("lifecycle.detail_repair_iteration", { iteration });
    const grid = document.createElement("div");
    grid.className = "technical-grid";
    const outcome = String(item.outcome || "").trim();
    grid.append(
      lifecycleDetailField(t("detail.failed_checks"), String(item.failed_checks || t("detail.not_recorded"))),
      lifecycleDetailField(t("detail.proposed_action"), String(item.proposed_action || t("detail.not_recorded"))),
      lifecycleDetailField(t("detail.ai_repair_summary"), String(item.agent_summary || t("detail.not_recorded"))),
      lifecycleDetailField(t("detail.commit"), String(item.commit_sha || t("detail.not_recorded"))),
      lifecycleDetailField(t("detail.outcome"), t("lifecycle.repair_outcome." + outcome, {}, outcome || t("detail.not_recorded"))),
    );
    section.append(heading, grid);
  }
  return section.childElementCount > 1 ? section : null;
}
let lifecycleDetailTrigger = null;
function closeLifecycleDetail() {
  const modal = $("lifecycleDetailModal");
  if (modal?.open) modal.close();
}
function lifecycleDetailStatusKey(step) {
  const state = String(step?.state || "UNKNOWN").toLowerCase();
  return state === "active" && isOperatorMergeStep(step) ? "operator-wait" : state;
}
function openLifecycleDetail(step, trigger) {
  const modal = $("lifecycleDetailModal"), content = $("lifecycleDetailContent");
  if (!modal || !content) return;
  lifecycleDetailTrigger = trigger || document.activeElement;
  // A lifecycle detail is a child view: its modal chrome follows the nearest
  // parent surface, while the step state below still controls only its glyph.
  inheritModalAccent(modal, lifecycleDetailTrigger);
  const timing = step?.timing && typeof step.timing === "object" ? step.timing : {};
  const title = $("lifecycleDetailTitle");
  title.dataset.lifecycleStatus = lifecycleDetailStatusKey(step);
  title.textContent = t("lifecycle.detail_title", { step: lifecycleLabel(step) });
  content.replaceChildren();
  const overview = document.createElement("section"), grid = document.createElement("div");
  grid.className = "technical-grid";
  grid.append(
    lifecycleDetailStatusField(step?.state),
    lifecycleDetailField(t("lifecycle.detail_started_at"), formatTimestamp(timing.started_at || step?.started_at)),
    lifecycleDetailField(t("lifecycle.detail_finished_at"), formatTimestamp(timing.finished_at, t("format.unavailable"))),
  );
  if (Number.isInteger(step?.iteration_count) && step.iteration_count > 0) {
    grid.append(lifecycleDetailField(t("lifecycle.detail_iterations"), String(step.iteration_count)));
  }
  overview.append(grid); content.append(overview);
  const spans = lifecyclePhaseTiming(Array.isArray(timing.spans) ? timing.spans : []);
  const phaseTiming = document.createElement("section");
  phaseTiming.append(Object.assign(document.createElement("h3"), { textContent: t("lifecycle.detail_phase_timing") }));
  if (!spans.length) {
    phaseTiming.append(Object.assign(document.createElement("p"), { textContent: t("lifecycle.detail_no_phase_timing") }));
  } else {
    const list = document.createElement("ol"); list.className = "lifecycle-detail-modal__phase-list";
    for (const span of spans) {
      const item = document.createElement("li"), heading = document.createElement("strong"), meta = document.createElement("span");
      heading.textContent = telemetryLabel(span.phase);
      meta.textContent = t("lifecycle.detail_phase_meta", {
        duration: telemetryMs(span.duration_ms), outcome: lifecycleStateLabel(span.outcome),
      });
      item.append(heading, meta); list.append(item);
    }
    phaseTiming.append(list);
  }
  if (
    String(step?.id || "").toUpperCase() === "TERMINAL"
    && spans.some((span) => String(span.outcome || "").toUpperCase() === "STALE")
  ) {
    phaseTiming.append(Object.assign(document.createElement("p"), {
      className: "estimate-meta",
      textContent: t("lifecycle.detail_terminal_timing_stale"),
    }));
  }
  content.append(phaseTiming);
  const qualityEvidence = lifecycleQualityEvidence(step);
  if (qualityEvidence) content.append(qualityEvidence);
  const repairEvidence = lifecycleRepairEvidence(step);
  if (repairEvidence) content.append(repairEvidence);
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
}
$("lifecycleDetailClose")?.addEventListener("click", closeLifecycleDetail);
$("lifecycleDetailModal")?.addEventListener("close", () => { lifecycleDetailTrigger?.focus?.(); lifecycleDetailTrigger = null; });
function lifecycleFlow(projection, { historical = false } = {}) {
  const section = document.createElement("section");
  section.className = "execution-lifecycle" + (historical ? " execution-lifecycle--historical" : "");
  if (projection?.run_id) section.dataset.runId = projection.run_id;
  section.setAttribute("aria-label", t("lifecycle.title"));
  const heading = document.createElement("h3"); heading.textContent = t("lifecycle.title"); section.append(heading);
  if (!projection?.available) {
    section.append(Object.assign(document.createElement("p"), { className: "execution-lifecycle__unavailable", textContent: t("lifecycle.unavailable") }));
    return section;
  }
  const scroll = document.createElement("div"), list = document.createElement("ol");
  scroll.className = "execution-lifecycle__scroll"; list.className = "execution-lifecycle__path";
  const steps = Array.isArray(projection.steps) ? projection.steps : [];
  for (const [index, step] of steps.entries()) {
    const state = String(step?.state || "UNKNOWN").toLowerCase();
    const operatorWait = state === "active" && !historical && isOperatorMergeStep(step);
    const item = document.createElement("li"), button = document.createElement("button"), node = document.createElement("span"), label = document.createElement("span");
    item.className = "execution-lifecycle__item execution-lifecycle__item--" + state;
    if (operatorWait) item.classList.add("execution-lifecycle__item--operator-wait");
    button.type = "button"; button.className = "execution-lifecycle__node";
    if (state === "active" && !historical) button.classList.add("execution-lifecycle__node--active");
    if (operatorWait) button.classList.add("execution-lifecycle__node--operator-wait");
    if (isLifecycleStartStep(step)) button.classList.add("execution-lifecycle__node--start");
    const name = lifecycleLabel(step), status = lifecycleStateLabel(step?.state);
    button.setAttribute("aria-label", name + " — " + status);
    if (state === "active" && !historical && typeof projection.live_activity === "string") {
      const activity = projection.live_activity.slice(0, 160);
      button.title = t("lifecycle.live_activity", { activity });
      button.setAttribute("aria-label", name + " — " + status + ". " + button.title);
    }
    button.addEventListener("click", () => openLifecycleDetail(step, button));
    node.setAttribute("aria-hidden", "true"); node.textContent = state === "completed" ? "✓" : state === "complete" ? "✓" : state === "blocked" ? "!" : state === "failed" ? "×" : operatorWait ? "⌛" : isLifecycleStartStep(step) ? "🚀" : "";
    label.textContent = name;
    button.append(node, label);
    item.append(button);
    if (index < steps.length - 1) {
      const connector = document.createElement("span");
      connector.className = "execution-lifecycle__connector";
      const nextState = String(steps[index + 1]?.state || "UNKNOWN").toLowerCase();
      if (["active", "completed", "complete"].includes(nextState)) {
        connector.classList.add("execution-lifecycle__connector--reached");
      }
      connector.setAttribute("aria-hidden", "true");
      item.append(connector);
    }
    list.append(item);
  }
  scroll.append(list); section.append(scroll);
  const summary = document.createElement("p"); summary.className = "execution-lifecycle__summary";
  const currentStep = (projection.steps || []).find((step) => step?.state === "ACTIVE")
    || (projection.steps || []).find((step) => step?.state === projection?.terminal_state) || {};
  summary.textContent = t("lifecycle.summary", {
    step: lifecycleLabel(currentStep),
    status: isOperatorMergeStep(currentStep)
      ? lifecycleLabel(currentStep)
      : lifecycleStateLabel(projection.terminal_state || "ACTIVE"),
  });
  section.append(summary);
  return section;
}
function activeRunPullRequestEntries(execution) {
  const repository = String(execution?.target_repository || "").trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) return [];
  return [
    ["implementation", execution?.implementation_pr],
    ["finalization", execution?.finalization_pr],
  ].flatMap(([role, value]) => {
    const number = Number(value);
    if (!Number.isInteger(number) || number <= 0) return [];
    return [{
      role,
      number,
      url: `https://github.com/${repository.split("/").map(encodeURIComponent).join("/")}/pull/${number}`,
    }];
  });
}
function renderActivePullRequests(execution, lifecycle) {
  const current = $("currentRun")?.querySelector(".current-run__grid");
  if (!current) return;
  current.querySelector("#activeRunPullRequests")?.remove();
  const entries = activeRunPullRequestEntries(execution);
  if (!entries.length) return;
  const labels = {
    implementation: t("detail.implementation_pull_request"),
    finalization: t("detail.finalization_pull_request"),
  };
  const fields = entries.map((entry) => {
    const field = detailField(labels[entry.role], "");
    const link = document.createElement("a");
    link.className = "prompt-detail-pr-link";
    link.href = entry.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `#${entry.number} ↗`;
    link.setAttribute("aria-label", `${labels[entry.role]} #${entry.number}`);
    field.lastElementChild.replaceChildren(link);
    return field;
  });
  const card = promptDetailCard(
    t("detail.pull_requests"), fields, false, "active-run-pull-requests",
  );
  // In the active execution this is an operational card, not a modal detail
  // panel. Reuse the current-run card surface while historic detail cards stay flat.
  card.classList.add("card");
  card.id = "activeRunPullRequests";
  if (lifecycle?.parentElement === current) lifecycle.after(card);
  else current.append(card);
}
function renderActiveLifecycle(projection, execution = {}) {
  const current = $("currentRun")?.querySelector(".current-run__grid"); if (!current) return;
  const previous = current.querySelector(".execution-lifecycle"),
    previousScroll = previous?.querySelector(".execution-lifecycle__scroll"),
    sameRun = previous?.dataset.runId === String(projection?.run_id || ""),
    preservedScrollLeft = sameRun
      ? previousScroll?.scrollLeft || 0
      : 0;
  previous?.remove();
  if (projection?.run_id) {
    const lifecycle = lifecycleFlow(projection);
    const identity = $("executionIdentity"), estimate = $("executionEstimate")?.closest(".card");
    placeExecutionEstimate();
    // Keep run identity and its phase-aware estimate ahead of the read-only
    // lifecycle projection. The fallback preserves older dashboard shells.
    if (estimate?.parentElement === current) estimate.after(lifecycle);
    else if (identity?.parentElement === current) identity.after(lifecycle);
    else current.prepend(lifecycle);
    placeOperatorMergeWait();
    const reconciliation = $("statusReconciliation");
    if (reconciliation) {
      const eligible = projection?.recovery?.kind === "status_reconciliation" && $("operatorMergeWait")?.hidden;
      reconciliation.hidden = !eligible;
      if (eligible && reconciliation.parentElement === current) lifecycle.after(reconciliation);
    }
    if (preservedScrollLeft) {
      const nextScroll = lifecycle.querySelector(".execution-lifecycle__scroll");
      // Wait for the replacement path to take part in layout before restoring
      // the user's independent horizontal review position.
      requestAnimationFrame(() => { nextScroll.scrollLeft = preservedScrollLeft; });
    } else if (!sameRun) {
      revealActiveLifecycleStep(lifecycle.querySelector(".execution-lifecycle__scroll"));
    }
    renderActivePullRequests(execution, lifecycle);
  } else {
    renderActivePullRequests(execution, null);
  }
}
function statusReconciliationCard(recovery) {
  if (recovery?.kind !== "status_reconciliation" || !recovery.run_id) return null;
  const card = document.createElement("section"), title = document.createElement("h3"), description = document.createElement("p"), actions = document.createElement("div"), button = document.createElement("button"), result = document.createElement("p");
  card.className = "prompt-detail-card prompt-detail-card--wide status-reconciliation-card";
  title.textContent = t("status_reconciliation.title");
  description.textContent = t("status_reconciliation.description");
  actions.className = "operator-merge-wait__actions";
  button.type = "button"; button.className = "dashboard-action dashboard-action--primary";
  button.textContent = t("status_reconciliation.action");
  result.setAttribute("role", "status"); result.setAttribute("aria-live", "polite");
  button.addEventListener("click", () => requestStatusReconciliation(recovery, button, result));
  actions.append(button); card.append(title, description, actions, result);
  return card;
}
async function requestStatusReconciliation(recovery = latestStatus?.lifecycle?.recovery, button = $("statusReconciliationStart"), result = $("statusReconciliationResult")) {
  if (recovery?.kind !== "status_reconciliation" || !recovery.run_id) return;
  button.disabled = true;
  try {
    const previewResponse = await fetch("/api/status-reconciliation-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: recovery.run_id }) });
    const preview = await previewResponse.json();
    if (!previewResponse.ok) throw Error(preview.error || t("status_reconciliation.failed"));
    const confirmed = await confirmDashboardAction(t("status_reconciliation.title"), t("status_reconciliation.confirmation"), t("status_reconciliation.action"));
    if (!confirmed) return;
    const response = await fetch("/api/status-reconciliation", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: preview.run_id }) });
    const outcome = await response.json();
    if (!response.ok) throw Error(outcome.error || t("status_reconciliation.failed"));
    result.textContent = t("status_reconciliation.queued");
  } catch (error) {
    result.textContent = error.message || t("status_reconciliation.failed");
  } finally { button.disabled = false; }
}
function revealActiveLifecycleStep(scroll) {
  requestAnimationFrame(() => {
    const active = scroll?.querySelector(".execution-lifecycle__node--active");
    if (!active || scroll.scrollWidth <= scroll.clientWidth) return;
    const scrollBox = scroll.getBoundingClientRect(), activeBox = active.getBoundingClientRect();
    if (activeBox.left >= scrollBox.left && activeBox.right <= scrollBox.right) return;
    const desired = scroll.scrollLeft + activeBox.left - scrollBox.left
      - ((scroll.clientWidth - activeBox.width) / 2);
    scroll.scrollLeft = Math.max(0, Math.min(desired, scroll.scrollWidth - scroll.clientWidth));
  });
}
function placeExecutionEstimate() {
  const current = $("currentRun")?.querySelector(".current-run__grid"),
    identity = $("executionIdentity"), estimate = $("executionEstimate")?.closest(".card");
  if (current && identity?.parentElement === current && estimate?.parentElement === current) {
    identity.after(estimate);
  }
}
function placeOperatorMergeWait() {
  const current = $("currentRun")?.querySelector(".current-run__grid"),
    lifecycle = current?.querySelector(".execution-lifecycle"),
    wait = $("operatorMergeWait");
  if (current && lifecycle && wait?.parentElement === current) lifecycle.after(wait);
}
function renderEmergencyRecovery(recovery, status) {
  const card = $("emergencyRecovery"), button = $("emergencyRecoveryStart");
  if (!card || !button) return;
  const available = Boolean(recovery?.available && status?.run_id === recovery?.run_id);
  card.hidden = !available;
  button.disabled = !available;
}
async function startEmergencyRecovery() {
  const recovery = latestDashboardSnapshot?.emergency_recovery;
  if (!recovery?.available || !latestStatus?.run_id || recovery.run_id !== latestStatus.run_id) return;
  const confirmed = await confirmDashboardAction(
    t("emergency_recovery.title"),
    t("emergency_recovery.confirmation", { run_id: recovery.run_id, branch: recovery.branch }),
    t("emergency_recovery.action"),
    { destructive: true },
  );
  if (!confirmed) return;
  const button = $("emergencyRecoveryStart");
  button.disabled = true;
  try {
    const response = await fetch("/api/execution-emergency-rollback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: recovery.run_id }),
    });
    const outcome = await response.json();
    if (!response.ok) throw Error(outcome.error || t("emergency_recovery.failed"));
    await refreshAfterOperatorAction();
  } catch (error) {
    showDashboardError(error.message, t("emergency_recovery.failed"));
  } finally { button.disabled = false; }
}
function renderHealthStatus(x, snapshot = {}) {
  lastRefresh = new Date();
  clock();
  x = x && typeof x === "object" ? x : fallback;
  latestStatus = x;
  renderDashboardHealth(x, latestPlatformHealth);
  if (latestCodexCliUpdateStatus) renderCodexCliUpdate(latestCodexCliUpdateStatus);
  latestDashboardSnapshot = snapshot;
  latestDurationEstimate = snapshot.duration_estimate || {};
  let active = isActiveRun(x),
    visibleStaleLifecycle = hasVisibleStaleLifecycle(x),
    statusTone = tone(x),
    indicator = $("indicator"),
    components = snapshot.component_versions || {},
    blockedPredecessor = Boolean(x.blocking_predecessor_run);
  const precedingExecutionOnly = blockedPredecessor && !active && !visibleStaleLifecycle;
  // A terminal current.json/status projection is historical evidence, not an
  // active prompt.  The watcher owns the operational view; history owns the
  // completed, failed or blocked execution.
  $("currentRun").hidden = !(active || visibleStaleLifecycle || blockedPredecessor);
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
  $("currentRunTitle").textContent = precedingExecutionOnly
    ? t("recovery.blocked_predecessor_title")
    : t("section.active_prompt");
  $("executionIdentityTitle").textContent = precedingExecutionOnly
    ? t("recovery.preceding_execution")
    : t("detail.execution");
  $("promptStartedField").hidden = precedingExecutionOnly;
  renderExecutionContext(x.execution_context, x);
  renderActiveLifecycle(x.lifecycle, x);
  renderOperatorMergeWait(x);
  renderEmergencyRecovery(snapshot.emergency_recovery, x);
  renderCodexUsageLimitBanner(x, snapshot.rate_limits);
  renderCodexCapacityReserveBanner(snapshot.rate_limits);
  syncCodexCapacityReserveOptions(snapshot.rate_limits);
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
  const providerRecovery = x.provider_recovery || {};
  const providerRecoveryState = String(providerRecovery.state || "NOT_APPLICABLE");
  $("providerRecovery").hidden = !["RECOVERING", "RECOVERED", "EXHAUSTED"].includes(providerRecoveryState);
  $("providerRecoveryValue").textContent = providerRecoveryState === "RECOVERING"
    ? t("provider_recovery.recovering")
    : providerRecoveryState === "RECOVERED"
      ? t("provider_recovery.recovered")
      : providerRecoveryState === "EXHAUSTED"
        ? t("provider_recovery.exhausted")
        : "";
  const legacyProgress = x.workspace_progress || {};
  const liveSnapshot = x.live_worktree_snapshot || (x.workspace_progress ? {
    uncommitted: { modified: legacyProgress.modified, added: legacyProgress.created, deleted: legacyProgress.deleted },
  } : null);
  const workspaceProgress = liveSnapshot?.uncommitted || {};
  const reviewerCommands = Array.isArray(x.reviewer_agents)
    ? x.reviewer_agents.reduce((total, reviewer) => total + Math.max(0, Number(reviewer?.codex_commands_executed) || 0), 0)
    : 0;
  const activity = x.cumulative_activity || {
    primary_codex_commands_total: legacyProgress.codex_commands_executed,
    reviewer_codex_commands_total: reviewerCommands,
  };
  $("workspaceProgress").hidden = !liveSnapshot;
  $("workspaceProgressValue").textContent = [
    t("workspace_progress.live_snapshot"),
    t("workspace_progress.modified", { count: Number(workspaceProgress.modified) || 0 }),
    t("workspace_progress.created", { count: Number(workspaceProgress.added) || 0 }),
    t("workspace_progress.deleted", { count: Number(workspaceProgress.deleted) || 0 }),
    t("workspace_progress.primary_codex_commands", { count: Number(activity.primary_codex_commands_total) || 0 }),
    t("workspace_progress.reviewer_codex_commands", { count: Number(activity.reviewer_codex_commands_total) || 0 }),
    t("workspace_progress.host_validation_commands", { count: Number(activity.host_validation_commands_total) || 0 }),
  ].join(" · ");
  const executionHost = snapshot.execution_host || {};
  $("executionHostName").textContent = executionHost.name || t("format.not_available");
  $("executionHostVersion").textContent = executionHost.version || t("format.not_available");
  technicalMachineLabel("executionHostRuntime", executionHost.runtime, "technical.runtime_value");
  technicalMachineLabel("executionHostTransport", executionHost.runtime_prompt_transport, "technical.runtime_transport_value");
  renderWorkspaceGitLock(snapshot.workspace_git_lock);
  // Older dashboard fixtures and cached shells do not have Level 3 fields.
  // Keep the canonical status renderer backward compatible while they refresh.
  renderPreflightPresentation(snapshot);
  renderTechnicalDiagnosis(x, snapshot);
  if (!precedingExecutionOnly) promptStarted(snapshot.prompt_started);
  renderEstimate(x, latestDurationEstimate);
  processMetrics(active, snapshot.process_metrics);
  $("currentPrompt").textContent = precedingExecutionOnly
    ? (x.blocking_predecessor_title || x.blocking_predecessor_filename || t("format.not_available"))
    : (x.prompt_title || t("format.not_available"));
  $("currentFile").textContent = precedingExecutionOnly
    ? (x.blocking_predecessor_filename || t("format.not_available"))
    : (x.submitted_filename || t("format.not_available"));
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
  $("runId").textContent = precedingExecutionOnly
    ? x.blocking_predecessor_run
    : (x.run_id || t("value.none"));
  renderInboxBlocker(x);
  queueItems(x.queue_items, x.queue_depth);
  $("repositoryState").textContent = translate(x.repository_state || "UNKNOWN");
  $("workspaceState").textContent = translate(x.workspace_state || "UNKNOWN");
  $("diag").textContent = formatDiagnostic(x.diagnostic);
  $("platformVersion").textContent = x.platform_version || t("format.not_available");
  $("dashboardVersion").textContent =
    components.dashboard || t("format.not_available");
  $("workerVersion").textContent = components.worker || t("format.not_available");
  usage(snapshot.usage);
  rateLimits(snapshot.rate_limits, snapshot.ai_capacity_history);
  activeReviewerAgents(x.reviewer_agents, x);
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
// A successful main switch deliberately restarts the platform.  Until the old
// dashboard document is replaced it can still receive an earlier SSE snapshot,
// whose Git projection truthfully describes the *previous* branch.  Keep the
// acknowledged switch visible in this short hand-off window so that an old
// snapshot cannot offer the same action again.
let workspaceMainSwitchScheduled = false;
function renderWorkspaceGit(workspaceGit) {
  if (!workspaceGit || typeof workspaceGit !== "object") return;
  $("workspaceBranch").textContent =
    workspaceGit.branch || t("format.not_available");
  $("workspaceCommit").textContent =
    workspaceGit.commit || t("format.not_available");
  $("workspaceOriginMainCommit").textContent =
    workspaceGit.origin_main_commit || t("format.not_available");
  $("workspaceOriginMain").hidden = !workspaceGit.origin_main_available;
  $("workspaceBranchMain").hidden = workspaceMainSwitchScheduled || !workspaceGit.main_action_available;
}
let worktreeRemovalAnalysis = null;
function worktreeAnalysisKey(worktree) {
  return `${String(worktree?.path || "")}\u0000${String(worktree?.branch || "")}`;
}
function renderWorkspaceWorktrees(projection) {
  const workspace = $("workspaceCard");
  if (!workspace) return;
  // Branch actions operate on the same local Git topology as this projection.
  // Keep them with that evidence instead of visually separating them below
  // open pull requests.
  const branchActions = workspace.querySelector(".workspace-branch-actions");
  let section = $("workspaceWorktrees");
  if (!section) {
    section = document.createElement("section");
    section.id = "workspaceWorktrees";
    section.className = "workspace-worktrees";
    const anchor = $("workspaceOpenPullRequests") || workspace.querySelector(".workspace-branch-actions");
    workspace.insertBefore(section, anchor || null);
  }
  section.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "workspace-worktrees__header";
  const headingText = document.createElement("strong");
  headingText.textContent = t("workspace.local_worktrees");
  const analyze = document.createElement("button");
  analyze.className = "workspace-worktrees__refresh";
  analyze.type = "button";
  analyze.title = t("workspace.worktree_analysis_refresh");
  analyze.setAttribute("aria-label", t("workspace.worktree_analysis_refresh"));
  analyze.textContent = "↻";
  analyze.addEventListener("click", () => void refreshWorktreeRemovalAnalysis(analyze));
  heading.append(headingText, analyze);
  section.append(heading);
  const available = projection?.available === true;
  const worktrees = Array.isArray(projection?.worktrees) ? projection.worktrees : [];
  if (!available || !worktrees.length) {
    const empty = document.createElement("p");
    empty.className = "workspace-worktrees__empty";
    empty.textContent = available
      ? t("workspace.no_local_worktrees")
      : t("workspace.worktrees_unavailable");
    section.append(empty);
    if (branchActions) section.append(branchActions);
    return;
  }
  const list = document.createElement("ul");
  const analyses = new Map(Array.isArray(worktreeRemovalAnalysis?.worktrees)
    ? worktreeRemovalAnalysis.worktrees.map((item) => [worktreeAnalysisKey(item), item]) : []);
  worktrees.forEach((worktree) => {
    const item = document.createElement("li");
    const branch = document.createElement("code");
    const path = document.createElement("button");
    const commit = document.createElement("code");
    branch.className = "workspace-worktrees__branch";
    path.className = "workspace-worktrees__path workspace-worktrees__path--open";
    path.type = "button";
    commit.className = "workspace-worktrees__commit";
    branch.textContent = worktree?.branch || t("workspace.detached_head");
    if (worktree?.active === true) {
      const active = document.createElement("span");
      active.className = "workspace-worktrees__active";
      active.textContent = "◉";
      active.setAttribute("role", "img");
      active.setAttribute("aria-label", t("workspace.active_worktree"));
      active.title = t("workspace.active_worktree");
      branch.prepend(active);
    }
    path.textContent = worktree?.checked_out === false
      ? t("workspace.not_checked_out")
      : String(worktree?.path || t("format.not_available"));
    if (typeof worktree?.path === "string" && worktree.path) {
      path.title = t("workspace.open_worktree_folder", { path: worktree.path });
      path.setAttribute("aria-label", t("workspace.open_worktree_folder", { path: worktree.path }));
      path.addEventListener("click", () => void openWorktreeFolder(worktree.path));
    } else {
      path.disabled = true;
    }
    commit.textContent = String(worktree?.commit || t("format.not_available"));
    item.append(branch, path, commit);
    const analysis = analyses.get(worktreeAnalysisKey(worktree));
    if (analysis && worktree?.branch !== "main") {
      const conclusion = document.createElement("p");
      conclusion.className = `workspace-worktrees__analysis workspace-worktrees__analysis--${analysis.removable === true ? "removable" : "keep"}`;
      conclusion.textContent = t(`workspace.worktree_analysis_reason.${String(analysis.reason || "pull_request_unverified")}`);
      item.append(conclusion);
      if (analysis.pull_request?.url && Number.isInteger(analysis.pull_request?.number)) {
        const pr = document.createElement("a");
        pr.className = "workspace-worktrees__pr";
        pr.href = analysis.pull_request.url;
        pr.target = "_blank";
        pr.rel = "noreferrer";
        pr.textContent = `${t("workspace.worktree_analysis_pr", { number: analysis.pull_request.number, state: String(analysis.pull_request.state || "") })} ↗`;
        item.append(pr);
      }
    } else if (worktree?.branch !== "main") {
      const conclusion = document.createElement("p");
      conclusion.className = "workspace-worktrees__analysis";
      conclusion.textContent = t("workspace.worktree_analysis_not_run");
      item.append(conclusion);
    }
    if (
      worktree?.branch !== "main"
      && worktree?.active !== true
      && typeof worktree?.path === "string" && worktree.path
      && typeof worktree?.branch === "string" && worktree.branch
    ) {
      const switchWorktree = document.createElement("button");
      switchWorktree.className = "workspace-worktrees__switch";
      switchWorktree.type = "button";
      switchWorktree.textContent = t("workspace.worktree_switch_action");
      switchWorktree.addEventListener("click", () => void switchEngineeringPlatformToWorktree(worktree));
      item.append(switchWorktree);
    }
    if (analysis?.removable === true && typeof worktree?.path === "string" && (typeof worktree?.branch === "string" || typeof analysis?.head === "string")) {
      const remove = document.createElement("button");
      remove.className = "workspace-worktrees__remove";
      remove.type = "button";
      remove.textContent = t("workspace.worktree_remove_action");
      remove.addEventListener("click", () => removeSafeWorktree(worktree, analysis));
      item.append(remove);
    }
    list.append(item);
  });
  section.append(list);
  if (branchActions) section.append(branchActions);
}
async function openWorktreeFolder(worktreePath) {
  try {
    const response = await fetch("/api/open-worktree-folder", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ worktree_path: worktreePath }),
    });
    const outcome = await response.json();
    if (!response.ok) throw Error(outcome?.error || t("workspace.open_worktree_folder_failed"));
  } catch (error) {
    showDashboardError(error.message || t("workspace.open_worktree_folder_failed"), t("workspace.open_worktree_folder_failed"));
  }
}
async function refreshWorktreeRemovalAnalysis(button) {
  showDashboardToast(t("workspace.worktree_analysis_refreshing"));
  button.disabled = true;
  try {
    const response = await fetch("/api/worktree-removal-analysis", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const outcome = await response.json();
    if (!response.ok || outcome?.available !== true || !Array.isArray(outcome?.worktrees)) {
      throw Error(outcome?.error || t("workspace.worktree_analysis_failed"));
    }
    worktreeRemovalAnalysis = outcome;
    const snapshot = await fetch("/api/dashboard-snapshot", { cache: "no-store" }).then((result) => result.ok ? result.json() : null);
    if (snapshot?.workspace_worktrees) renderWorkspaceWorktrees(snapshot.workspace_worktrees);
  } catch (error) {
    showDashboardError(error.message || t("workspace.worktree_analysis_failed"), t("workspace.worktree_analysis_failed"));
  } finally {
    button.disabled = false;
  }
}
let openPullRequestMonitorIntervalMs = 30_000;
let openPullRequestMonitorTimer = null, openPullRequestMonitorInFlight = false;
const openPullRequestStatusByNumber = new Map();
const openPullRequestOwnerApprovalByNumber = new Map();
const OPEN_PULL_REQUEST_STATES = ["draft", "waiting_for_checks", "ready_for_review", "ready_to_merge", "branch_update_required", "issues"];
const OPEN_PULL_REQUEST_OWNER_APPROVAL_STATES = ["not_required", "pending", "approved", "changes_requested"];
function openPullRequestStatusKey(status) {
  return {
    draft: "workspace.open_pull_request.draft",
    waiting_for_checks: "workspace.open_pull_request.waiting_for_checks",
    ready_for_review: "workspace.open_pull_request.ready_for_review",
    ready_to_merge: "workspace.open_pull_request.ready_to_merge",
    branch_update_required: "workspace.open_pull_request.branch_update_required",
    issues: "workspace.open_pull_request.issues",
  }[status] || "workspace.open_pull_request.waiting_for_checks";
}
function localizeOpenPullRequestStatuses() {
  document.querySelectorAll(".open-pr-status").forEach((element) => {
    const status = OPEN_PULL_REQUEST_STATES
      .find((candidate) => element.classList.contains(`open-pr-status--${candidate}`)) || "waiting_for_checks";
    setOpenPullRequestStatus(element, status);
  });
}
function setOpenPullRequestStatus(element, status) {
  if (!element) return;
  const state = OPEN_PULL_REQUEST_STATES.includes(status) ? status : "waiting_for_checks";
  element.classList.remove(...OPEN_PULL_REQUEST_STATES.map((candidate) => `open-pr-status--${candidate}`));
  element.classList.add(`open-pr-status--${state}`);
  const label = t(openPullRequestStatusKey(state));
  element.querySelector(".open-pr-status__label").textContent = label;
  element.setAttribute("aria-label", label);
}
function setOpenPullRequestOwnerApproval(element, approval) {
  if (!element) return;
  const state = OPEN_PULL_REQUEST_OWNER_APPROVAL_STATES.includes(approval) ? approval : "pending";
  element.classList.remove(...OPEN_PULL_REQUEST_OWNER_APPROVAL_STATES.map((candidate) => `open-pr-approval--${candidate}`));
  element.classList.add(`open-pr-approval--${state}`);
  const label = t(`workspace.open_pull_request.owner_approval_${state}`);
  element.textContent = label;
  element.setAttribute("aria-label", label);
}
function renderOpenPullRequests(pullRequests) {
  if (!Array.isArray(pullRequests)) return;
  openPullRequestStatusByNumber.clear();
  openPullRequestOwnerApprovalByNumber.clear();
  pullRequests.forEach((pullRequest) => {
    if (Number.isInteger(pullRequest?.number) && pullRequest.number > 0) {
      openPullRequestStatusByNumber.set(pullRequest.number, OPEN_PULL_REQUEST_STATES.includes(pullRequest.status) ? pullRequest.status : "waiting_for_checks");
      openPullRequestOwnerApprovalByNumber.set(pullRequest.number, OPEN_PULL_REQUEST_OWNER_APPROVAL_STATES.includes(pullRequest.owner_approval) ? pullRequest.owner_approval : "pending");
    }
  });
  if (latestStatus) renderOperatorMergeWait(latestStatus);
  const section = $("workspaceOpenPullRequests");
  if (!section) return;
  const list = section.querySelector("ul");
  if (!list) return;
  list.replaceChildren(...pullRequests.map((pullRequest) => {
    const item = document.createElement("li"), link = document.createElement("a"), status = document.createElement("span"), dot = document.createElement("span"), label = document.createElement("span"), approval = document.createElement("span"), branch = document.createElement("code");
    const state = OPEN_PULL_REQUEST_STATES.includes(pullRequest.status) ? pullRequest.status : "waiting_for_checks";
    item.dataset.openPullRequest = String(pullRequest.number || "");
    link.href = String(pullRequest.url || "");
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = `PR #${pullRequest.number} — ${pullRequest.title || ""} ↗`;
    status.className = `open-pr-status open-pr-status--${state}`;
    dot.className = "open-pr-status__dot";
    dot.setAttribute("aria-hidden", "true");
    label.className = "open-pr-status__label";
    status.append(dot, label);
    approval.className = "open-pr-approval";
    setOpenPullRequestOwnerApproval(approval, pullRequest.owner_approval);
    branch.textContent = String(pullRequest.branch || "");
    item.append(link, branch, status, approval);
    if (pullRequest.owner_authorization_requested === true) {
      const authorize = document.createElement("button");
      authorize.className = "open-pr-owner-authorization";
      authorize.dataset.openPullRequestOwnerAuthorization = String(pullRequest.number || "");
      authorize.type = "button";
      authorize.textContent = t("workspace.open_pull_request.authorize_owner");
      authorize.title = t("workspace.open_pull_request.authorize_owner");
      item.append(authorize);
    }
    if (pullRequest.check_repair_available === true || pullRequest.check_repair_completed_for_head === true) {
      const repair = document.createElement("button");
      repair.className = "open-pr-check-repair";
      repair.type = "button";
      if (pullRequest.check_repair_completed_for_head === true) {
        repair.disabled = true;
        repair.textContent = t("workspace.open_pull_request.repair_completed");
        repair.title = t("workspace.open_pull_request.repair_completed_explanation");
      } else {
        repair.dataset.openPullRequestCheckRepair = String(pullRequest.number || "");
        repair.dataset.openPullRequestFailedChecks = JSON.stringify(
          Array.isArray(pullRequest.failed_checks) ? pullRequest.failed_checks : [],
        );
        repair.textContent = t("workspace.open_pull_request.repair_failed_checks");
        repair.title = t("workspace.open_pull_request.repair_failed_checks");
      }
      item.append(repair);
    }
    if (["QUEUED", "RUNNING", "SUBMITTED"].includes(String(pullRequest.check_repair_state || "").toUpperCase())) {
      const progress = document.createElement("div"), spinner = document.createElement("span"), message = document.createElement("span"), checksLink = document.createElement("a");
      progress.className = "open-pr-check-repair-progress";
      progress.setAttribute("role", "status");
      spinner.className = "open-pr-check-repair-progress__spinner";
      spinner.setAttribute("aria-hidden", "true");
      message.textContent = t("workspace.open_pull_request.repair_active");
      checksLink.href = `${String(pullRequest.url || "").replace(/\/+$/, "")}/checks`;
      checksLink.target = "_blank";
      checksLink.rel = "noreferrer";
      checksLink.textContent = t("workspace.open_pull_request.repair_active_checks");
      progress.append(spinner, message, checksLink);
      item.append(progress);
    }
    return item;
  }));
  localizeOpenPullRequestStatuses();
}
function scheduleOpenPullRequestMonitor(pullRequests) {
  clearTimeout(openPullRequestMonitorTimer);
  openPullRequestMonitorTimer = null;
  if (Array.isArray(pullRequests) && pullRequests.length > 0) {
    openPullRequestMonitorTimer = setTimeout(() => void refreshOpenPullRequests(), openPullRequestMonitorIntervalMs);
  }
}
async function refreshOpenPullRequests({ announce = false } = {}) {
  if (openPullRequestMonitorInFlight) return;
  if (announce) showDashboardToast(t("workspace.open_pull_requests_refreshing"));
  openPullRequestMonitorInFlight = true;
  const refreshButton = $("workspaceOpenPullRequestsRefresh");
  if (refreshButton) refreshButton.disabled = true;
  try {
    const response = await fetch("/api/open-pull-requests", { cache: "no-store" });
    const payload = response.ok ? await response.json() : null;
    const pullRequests = payload && Array.isArray(payload.pull_requests) ? payload.pull_requests : null;
    if (!pullRequests) return;
    renderOpenPullRequests(pullRequests);
    scheduleOpenPullRequestMonitor(pullRequests);
  } catch {
    // Keep the last known, non-authoritative projection visible and continue
    // checking every open PR: a new push can change a green status at any time.
    scheduleOpenPullRequestMonitor([...document.querySelectorAll(".open-pr-status")]);
  } finally {
    openPullRequestMonitorInFlight = false;
    if (refreshButton) refreshButton.disabled = false;
  }
}
function openPullRequestActionErrorMessage(error) {
  const code = String(error || "").trim();
  const key = `workspace.open_pull_request.${code}`;
  return DASHBOARD_MESSAGES[dashboardLocale]?.[key] || t("ui.action_failed");
}
function refreshOpenPullRequestsAfterAction() {
  void refreshOpenPullRequests();
  // Both owner authorization and one-shot repair are asynchronous GitHub
  // actions. Re-read their authoritative PR projection during the propagation
  // window instead of keeping a stale button until the normal polling interval.
  for (const delay of [900, 2500, 6000, 12000]) {
    setTimeout(() => void refreshOpenPullRequests(), delay);
  }
}
async function requestOpenPullRequestOwnerAuthorization(button) {
  const number = Number(button?.dataset.openPullRequestOwnerAuthorization);
  if (!Number.isInteger(number) || number < 1) return;
  const confirmed = await confirmDashboardAction(
    t("workspace.open_pull_request.authorize_owner"),
    t("workspace.open_pull_request.authorize_owner_confirmation"),
    t("workspace.open_pull_request.authorize_owner"),
    { accent: "#f3d36a", variant: "owner-authorization" },
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/open-pull-requests/${number}/owner-authorization`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw Error(payload?.error);
    showDashboardToast(t("workspace.open_pull_request.owner_authorization_queued"));
    refreshOpenPullRequestsAfterAction();
  } catch (error) {
    showDashboardToast(openPullRequestActionErrorMessage(error?.message));
  } finally {
    button.disabled = false;
  }
}
function failedCheckNamesFromButton(button) {
  try {
    const values = JSON.parse(button?.dataset.openPullRequestFailedChecks || "[]");
    return Array.isArray(values)
      ? values.filter((value) => typeof value === "string" && value.trim()).map((value) => value.trim())
      : [];
  } catch {
    return [];
  }
}
async function requestOpenPullRequestCheckRepair(button) {
  const number = Number(button?.dataset.openPullRequestCheckRepair);
  if (!Number.isInteger(number) || number < 1) return;
  const failedChecks = failedCheckNamesFromButton(button);
  const confirmed = await confirmDashboardAction(
    t("workspace.open_pull_request.repair_failed_checks"),
    t("workspace.open_pull_request.repair_failed_checks_confirmation"),
    t("workspace.open_pull_request.repair_failed_checks"),
    {
      accent: "#f3d36a",
      items: failedChecks,
      itemsLabel: t("workspace.open_pull_request.repair_failed_checks_list"),
      variant: "check-repair",
    },
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/open-pull-requests/${number}/repair-failed-checks`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw Error(payload?.error);
    showDashboardToast(t("workspace.open_pull_request.repair_failed_checks_queued"));
    refreshOpenPullRequestsAfterAction();
  } catch (error) {
    showDashboardToast(openPullRequestActionErrorMessage(error?.message));
  } finally {
    button.disabled = false;
  }
}
document.addEventListener("click", (event) => {
  const authorize = event.target.closest("[data-open-pull-request-owner-authorization]");
  if (authorize) void requestOpenPullRequestOwnerAuthorization(authorize);
  else if (event.target.closest("[data-open-pull-request-check-repair]")) void requestOpenPullRequestCheckRepair(event.target.closest("[data-open-pull-request-check-repair]"));
  else if (event.target.closest("#workspaceOpenPullRequestsRefresh")) void refreshOpenPullRequests({ announce: true });
});
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
    renderWorkspaceGit(snapshot.workspace_git);
    renderWorkspaceWorktrees(snapshot.workspace_worktrees);
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
let promptHistoryTerminalRun = null;
function startDashboardUpdates() {
  // Establish the data streams only after the page's controls and section
  // state have been initialized.  A fast local snapshot or SSE event must not
  // render into a partially constructed dashboard.
  void loadInitialDashboardStatus();
  const events = new EventSource("/api/events");
  events.addEventListener("dashboard", (x) => {
    if (!$("autoRefresh").checked) return;
    try {
      let snapshot = JSON.parse(x.data);
      receivedDashboardServerPush = true;
      dashboardStatusStore.update(snapshot.status, snapshot);
      renderWorkspaceGit(snapshot.workspace_git);
      renderWorkspaceWorktrees(snapshot.workspace_worktrees);
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
  events.onerror = () => {
    $("autoRefresh").checked &&
      setUpdateMode("refresh.reconnecting");
  };
}
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
    [["#technicalRepositoryTitle"], "technical.repository"],
    [["#technicalRepositoryStateLabel"], "technical.repository_status"],
    [["#technicalHostPreflightTitle"], "technical.host_preflight"],
    [["#technicalExecutionHostLabel"], "technical.execution_host"],
    [["#technicalExecutionHostVersionLabel"], "technical.execution_host_version"],
    [["#technicalRuntimeLabel"], "technical.runtime"],
    [["#technicalRuntimeVersionLabel"], "detail.codex_cli_version"],
    [["#technicalRuntimePathLabel"], "detail.codex_cli_installation_path"],
    [["#technicalRuntimePromptTransportLabel"], "technical.runtime_prompt_transport"],
    [["#technicalHostStatusLabel"], "technical.host_status"],
    [["#technicalLastCheckLabel"], "technical.last_check"],
    [["#technicalWorkspacePreflightStatusLabel"], "technical.workspace_status"],
    [["#technicalLastWorkspaceCheckLabel"], "technical.last_workspace_check"],
    [["#technicalCapabilityStatusLabel"], "technical.capability_status"],
    [["#technicalRecoverabilityLabel"], "technical.recoverability"],
    [["#technicalFailureOriginLabel"], "technical.failure_origin"],
    [["#technicalRecommendationLabel"], "technical.recommended_action"],
    [["#technicalDiagnosticsTitle"], "technical.diagnostics"],
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
  document.querySelectorAll(".component-log-copy").forEach((button) => {
    const label = t("logs.copy_visible");
    button.title = label;
    button.setAttribute("aria-label", label);
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
    "table.run_suffix", "table.status", "table.prompt_title", "table.executed_at", "table.report",
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
function chatMessage(role, text, createdAt = new Date().toISOString()) {
  let item = document.createElement("article"),
    label = document.createElement("span"),
    timestamp = document.createElement("time"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--" + role;
  label.className = "chat-message__role";
  label.textContent = t(role === "user" ? "chat.user" : "chat.assistant");
  timestamp.className = "chat-message__timestamp";
  timestamp.dateTime = createdAt;
  timestamp.textContent = chatMessageTimestampLabel(createdAt);
  body.className = "chat-message__body";
  body.textContent = text;
  item.append(label, timestamp, body);
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
    ["#technicalDetails", "⌘", "section.technical_details"],
    ["#componentLogs", "≡", "section.logs"],
    ["#configuration", "⚙︎", "section.configuration"],
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
    ["#configuration", "description.configuration"],
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
$("codexCliUpdate")?.addEventListener("click", installCodexCliUpdate);
$("rateLimits")?.addEventListener("toggle", () => {
  if ($("rateLimits").open) void checkCodexCliUpdate();
});
$("codexCapacityReserveAction")?.addEventListener("click", (event) => {
  event.preventDefault();
  const section = $("rateLimits"), control = $("configurationCodexCapacityReserve");
  if (!section) return;
  section.open = true;
  section.scrollIntoView({ behavior: "smooth", block: "start" });
  control?.focus({ preventScroll: true });
});
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
chatMessage = (role, text, createdAt) => {
  if (role !== "assistant") {
    plainChatMessage(role, text, createdAt);
    return;
  }
  const item = document.createElement("article"),
    label = document.createElement("span"),
    timestamp = document.createElement("time"),
    body = document.createElement("div");
  item.className = "chat-message chat-message--assistant";
  label.className = "chat-message__role";
  label.textContent = t("chat.assistant");
  timestamp.className = "chat-message__timestamp";
  timestamp.dateTime = createdAt || new Date().toISOString();
  timestamp.textContent = chatMessageTimestampLabel(timestamp.dateTime);
  body.className = "chat-message__body";
  renderMarkdownAnswer(body, text);
  item.append(label, timestamp, body);
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
chatMessage = (role, text, createdAt) => {
  chatMessageWithCopy(role, text, createdAt);
  addChatMessageCopyButton($("chatMessages").lastElementChild, text);
};
renderChatHistory();
$("chatSend").querySelector("span").textContent = "↑";
const LOG_PAGE_SIZE = 50,
  independentLogPageStates = { inbox: 1, dashboard: 1 },
  independentLogSortStates = {
    inbox: { key: "timestamp", direction: "desc" },
    dashboard: { key: "timestamp", direction: "desc" },
  },
  componentLogTotals = { inbox: 0, dashboard: 0 },
  componentLogAvailableEvents = { inbox: [], dashboard: [] },
  selectedComponentLogRows = { inbox: new Set(), dashboard: new Set() },
  componentLogSelectionAnchor = { inbox: null, dashboard: null };
let componentLogVersion = "",
  componentLogServerPaged = false,
  componentLogRequestId = 0;
function componentLogRequestUrl(component) {
  const query = new URLSearchParams({
    format: "json",
    page: String(independentLogPageStates[component]),
    page_size: String(LOG_PAGE_SIZE),
    sort: independentLogSortStates[component].key,
    direction: independentLogSortStates[component].direction,
  });
  const range = selectedLogTimeRange();
  if (range) {
    if (Number.isFinite(range.start)) query.set("start", new Date(range.start).toISOString());
    if (Number.isFinite(range.end)) query.set("end", new Date(range.end).toISOString());
    if (range.inclusiveEnd) query.set("inclusive_end", "1");
  }
  const search = $("logFilter").value.trim(), level = $("logLevelFilter").value;
  if (search) query.set("search", search);
  if (level) query.set("level", level);
  [...($("logEventFilter")?.selectedOptions || [])].forEach((option) => query.append("event", option.value));
  return "/api/logs/" + encodeURIComponent(component) + "?" + query;
}
function normalizedComponentLogEntries(records) {
  return structuredLogEntries((Array.isArray(records) ? records : []).map((record) => JSON.stringify(record)).join("\n"))
    .map((entry, index) => ({ ...entry, line: Number(records[index]?.line) || entry.line }));
}
function refreshComponentLogs(versions = {}, force = false) {
  const version = JSON.stringify(versions);
  if (!force && componentLogsLoaded && version === componentLogVersion) return Promise.resolve();
  componentLogVersion = version;
  const requestId = ++componentLogRequestId;
  return Promise.all([
    fetch(componentLogRequestUrl("inbox")).then((response) => response.ok ? response.json() : Promise.reject(Error("inbox logs unavailable"))),
    fetch(componentLogRequestUrl("dashboard")).then((response) => response.ok ? response.json() : Promise.reject(Error("dashboard logs unavailable"))),
  ])
    .then(([inbox, dashboard]) => {
      if (requestId !== componentLogRequestId) return;
      componentLogEntries.inbox = normalizedComponentLogEntries(inbox.entries);
      componentLogEntries.dashboard = normalizedComponentLogEntries(dashboard.entries);
      componentLogTotals.inbox = Number(inbox.total) || 0;
      componentLogTotals.dashboard = Number(dashboard.total) || 0;
      componentLogAvailableEvents.inbox = Array.isArray(inbox.events) ? inbox.events : [];
      componentLogAvailableEvents.dashboard = Array.isArray(dashboard.events) ? dashboard.events : [];
      componentLogServerPaged = true;
      componentLogsLoaded = true;
      $("componentLogControls").hidden = false;
      renderComponentLogs();
    })
    .catch(() => {
      if (requestId !== componentLogRequestId) return;
      componentLogServerPaged = false;
      componentLogsLoaded = true;
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
let healthRequestInFlight = false, platformHealthRefreshIntervalMs = 15e3, platformHealthRefreshTimer = null;
let componentDetailsRefreshIntervalMs = 5e3;
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
  const delegatedToActiveHost = watcherDelegatedToActiveHost(payload);
  componentDetailField(
    fields,
    t("component.status"),
    (delegatedToActiveHost ? t("dashboard.health.execution_host_active") : payload.healthy ? t("component.health_healthy") : t("component.health_unhealthy")) +
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
    resetDashboardModalInitialFocus(modal);
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
$("executionModeModalClose").addEventListener("click", () =>
  $("executionModeModal").close(),
);
$("executionModeModal").addEventListener("click", (event) => {
  if (event.target === $("executionModeModal")) $("executionModeModal").close();
});
$("operatorMergeWaitModalClose").addEventListener("click", () =>
  $("operatorMergeWaitModal").close(),
);
$("operatorMergeWaitModal").addEventListener("click", (event) => {
  if (event.target === $("operatorMergeWaitModal")) $("operatorMergeWaitModal").close();
});
function renderPlatformHealth(payload) {
  const container = $("platformHealthComponents");
  if (!container) return;
  latestPlatformHealth = payload && typeof payload === "object" ? payload : null;
  renderDashboardHealth(latestStatus, latestPlatformHealth);
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
      delegatedToActiveHost = watcherDelegatedToActiveHost({ component: key, healthy: componentHealthy }),
      version =
        typeof component?.version === "string"
          ? " · Versie " + component.version
          : "",
      uptime = formatComponentUptime(component?.uptime_seconds);
    item.className = "platform-health__component";
    item.dataset.health = delegatedToActiveHost ? "delegated" : String(componentHealthy);
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute(
      "aria-label",
      t("component.more_information", { component: healthComponentLabel(key) }),
    );
    indicator.className = healthIndicatorClass(delegatedToActiveHost || componentHealthy);
    indicator.setAttribute("aria-hidden", "true");
    name.className = "platform-health__component-name";
    name.textContent = healthComponentLabel(key);
    if (["dashboard", "inbox_watcher", "dashboard_relay"].includes(key)) {
      const linkIcon = document.createElement("span");
      linkIcon.className = "platform-health__component-link-icon";
      linkIcon.dataset.testid = "component-details-link-icon";
      linkIcon.setAttribute("aria-hidden", "true");
      linkIcon.textContent = "↗";
      name.append(linkIcon);
    }
    detail.className = "platform-health__component-detail";
    detail.textContent =
      (delegatedToActiveHost ? t("dashboard.health.execution_host_active") : componentHealthy ? t("component.health_healthy") : t("component.health_unhealthy")) +
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
function schedulePlatformHealthRefresh() {
  if (platformHealthRefreshTimer !== null) window.clearInterval(platformHealthRefreshTimer);
  platformHealthRefreshTimer = window.setInterval(refreshPlatformHealth, platformHealthRefreshIntervalMs);
}
schedulePlatformHealthRefresh();
function arrangeCurrentRunCategory() {
  const current = $("currentRun"),
    summary = current?.querySelector(":scope>summary"),
    prompt = $("currentPrompt"),
    indicator = $("indicator"),
    runId = $("runId");
  if (!current || !summary || !prompt || !indicator || !runId) return;
  let heading = summary.querySelector(".current-run__prompt-heading");
  if (!heading) {
    heading = document.createElement("div");
    heading.className = "current-run__prompt-heading";
    prompt.replaceWith(heading);
    heading.append(prompt);
  }
  const runIdField = runId.closest(".field");
  runIdField?.classList.add("execution-identity__run-id");
  runId.after(indicator);
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
    description.textContent = t("telemetry.description", { days: dashboardConfiguration.telemetry_retention_days || 90 });
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
const executionTelemetryColumns = [
  ["date", "telemetry.day"], ["prompt_count", "telemetry.prompts"],
  ["average_total_execution_seconds", "telemetry.average_total"],
  ["average_queue_wait_seconds", "telemetry.average_wait"],
  ["input_tokens", "telemetry.input"], ["output_tokens", "telemetry.output"],
  ["total_tokens", "telemetry.total"], ["complete_count", "telemetry.complete"],
  ["blocked_count", "telemetry.blocked"], ["failed_count", "telemetry.failed"],
];
const EXECUTION_TELEMETRY_PAGE_SIZE = 7;
let executionTelemetryRows = [], executionTelemetryPage = 1, executionTelemetrySort = { key: "date", direction: "desc" };
function telemetryComparableValue(row, key) {
  const value = row?.[key];
  return key === "date" ? String(value || "") : Number.isFinite(Number(value)) ? Number(value) : -1;
}
function sortedExecutionTelemetryRows() {
  const { key, direction } = executionTelemetrySort, multiplier = direction === "asc" ? 1 : -1;
  return [...executionTelemetryRows].sort((left, right) => {
    const leftValue = telemetryComparableValue(left, key), rightValue = telemetryComparableValue(right, key);
    return typeof leftValue === "number"
      ? (leftValue - rightValue) * multiplier
      : (leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0) * multiplier;
  });
}
function updateExecutionTelemetrySortHeaders() {
  document.querySelectorAll("#executionTelemetry .telemetry-table th[data-sort-key]").forEach((header) => {
    const active = header.dataset.sortKey === executionTelemetrySort.key;
    // Keep exactly the same text arrows as the component log table. Unlike
    // emoji variation glyphs, these inherit the header's mono font and baseline.
    header.dataset.sortIndicator = active
      ? executionTelemetrySort.direction === "asc" ? "↑" : "↓"
      : "↕";
    header.setAttribute("aria-sort", active ? executionTelemetrySort.direction === "asc" ? "ascending" : "descending" : "none");
  });
}
function blurSortableHeaderAfterPointerClick(event) {
  // Pointer activation should not leave a visual selection behind. Keyboard
  // activation keeps focus so the header remains operable and discoverable.
  if (event.detail > 0 && event.currentTarget instanceof HTMLElement)
    event.currentTarget.blur();
}
function setExecutionTelemetrySort(key) {
  executionTelemetrySort = executionTelemetrySort.key === key
    ? { key, direction: executionTelemetrySort.direction === "asc" ? "desc" : "asc" }
    : { key, direction: key === "date" ? "desc" : "asc" };
  executionTelemetryPage = 1;
  executionTelemetry(executionTelemetryRows);
}
function executionTelemetryText(rows = sortedExecutionTelemetryRows()) {
  const headings = executionTelemetryColumns.map(([, label]) => t(label));
  const values = rows.map((row) => [
    telemetryDate(row.date), row.prompt_count, telemetryDuration(row.average_total_execution_seconds),
    telemetryDuration(row.average_queue_wait_seconds), row.input_tokens ?? "—", row.output_tokens ?? "—",
    row.total_tokens ?? "—", row.complete_count, row.blocked_count, row.failed_count,
  ]);
  return [headings, ...values].map((line) => line.map((value) => String(value ?? "—").replaceAll("\t", " ").replaceAll("\n", " ")).join("\t")).join("\n");
}
function downloadExecutionTelemetry() {
  if (!executionTelemetryRows.length) return;
  const blob = new Blob([executionTelemetryText()], { type: "text/tab-separated-values;charset=utf-8" });
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url;
  link.download = "execution-host-telemetry.tsv";
  link.click();
  URL.revokeObjectURL(url);
  void recordUserAction("telemetry_downloaded");
}
async function copyExecutionTelemetry() {
  if (!executionTelemetryRows.length) return;
  await copyText(executionTelemetryText());
  void recordUserAction("telemetry_copied");
}
async function clearExecutionTelemetry() {
  if (!executionTelemetryRows.length) return;
  const confirmed = await confirmDashboardAction(
    t("telemetry.clear_title"), t("telemetry.clear_description"), t("telemetry.clear_title"),
    { destructive: true, accent: "#fb7185" },
  );
  if (!confirmed) return;
  try {
    const response = await fetch("/api/telemetry/clear", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.cleared) throw new Error(payload.error || t("telemetry.clear_failed"));
    executionTelemetryPage = 1;
    executionTelemetry([]);
    void recordUserAction("telemetry_cleared");
  } catch (error) {
    showDashboardToast(error instanceof Error && error.message ? error.message : t("telemetry.clear_failed"));
  }
}
function executionTelemetry(rows) {
  let panel = $("executionTelemetry"),
    body = $("executionTelemetryRows"),
    pagination = $("executionTelemetryPagination");
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
      tableBody = document.createElement("tbody"),
      navigation = document.createElement("nav"),
      actions = document.createElement("div"),
      download = document.createElement("button"),
      copy = document.createElement("button"),
      clear = document.createElement("button"),
      retention = document.createElement("div"),
      retentionLabel = document.createElement("label"),
      retentionText = document.createElement("span"),
      retentionSelect = document.createElement("select"),
      retentionStatus = document.createElement("p");
    title.textContent = t("telemetry.title");
    description.className = "category-description";
    description.textContent = t("telemetry.description", { days: dashboardConfiguration.telemetry_retention_days || 90 });
    retention.className = "configuration-controls telemetry-retention";
    retentionText.className = "label";
    retentionText.textContent = t("configuration.telemetry_retention");
    retentionSelect.id = "configurationTelemetryRetention";
    retentionSelect.setAttribute("aria-label", t("configuration.telemetry_retention"));
    for (const days of [30, 60, 90, 120, 180, 360]) {
      const option = document.createElement("option");
      option.value = String(days);
      option.textContent = t("configuration.days", { days });
      retentionSelect.append(option);
    }
    retentionSelect.value = String(dashboardConfiguration.telemetry_retention_days || 90);
    retentionSelect.dataset.savedValue = retentionSelect.value;
    retentionSelect.disabled = !dashboardConfigurationLoaded;
    retentionSelect.addEventListener("change", (event) => void saveDashboardConfiguration(event.currentTarget));
    retentionLabel.append(retentionText, retentionSelect);
    retentionStatus.id = "telemetryRetentionStatus";
    retentionStatus.setAttribute("role", "status");
    retentionStatus.setAttribute("aria-live", "polite");
    retention.append(retentionLabel, retentionStatus);
    scroll.className = "telemetry-scroll";
    table.className = "telemetry-table";
    table.setAttribute("aria-label", t("telemetry.table_label"));
    for (const [key, label] of executionTelemetryColumns) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.className = "log-sortable";
      cell.dataset.sortKey = key;
      cell.tabIndex = 0;
      cell.textContent = t(label);
      cell.setAttribute("aria-label", t("table.sort_by", { column: cell.textContent }));
      cell.addEventListener("click", (event) => {
        setExecutionTelemetrySort(key);
        blurSortableHeaderAfterPointerClick(event);
      });
      cell.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setExecutionTelemetrySort(key); }
      });
      headRow.append(cell);
    }
    head.append(headRow);
    tableBody.id = "executionTelemetryRows";
    table.append(head, tableBody);
    scroll.append(table);
    navigation.id = "executionTelemetryPagination";
    navigation.className = "log-pagination telemetry-pagination";
    navigation.setAttribute("aria-label", t("telemetry.pagination_label"));
    actions.className = "log-card-actions telemetry-actions";
    for (const [button, className, glyph, label, handler] of [
      [download, "dashboard-action dashboard-action--download telemetry-download", "↓", "telemetry.download", downloadExecutionTelemetry],
      [copy, "dashboard-action dashboard-action--copy telemetry-copy", "⧉", "telemetry.copy", copyExecutionTelemetry],
      [clear, "dashboard-action dashboard-action--destructive telemetry-clear", "⊠", "telemetry.clear_title", clearExecutionTelemetry],
    ]) {
      button.type = "button";
      button.className = className;
      button.textContent = glyph;
      button.title = t(label);
      button.setAttribute("aria-label", t(label));
      button.addEventListener("click", handler);
      actions.append(button);
    }
    summary.append(title, description);
    // Retention governs the historical data shown in the table, so keep it
    // directly below that table instead of separating it from its effect.
    panel.append(summary, actions, scroll, navigation, retention);
    const rate = $("rateLimits");
    rate?.insertAdjacentElement("afterend", panel);
    body = tableBody;
    pagination = navigation;
    enhanceDashboardSelectPicker(retentionSelect);
    addConfigurationControlInfo();
  }
  const telemetryRetention = $("configurationTelemetryRetention");
  if (telemetryRetention) {
    telemetryRetention.value = String(dashboardConfiguration.telemetry_retention_days || 90);
    telemetryRetention.dataset.savedValue = telemetryRetention.value;
    syncDashboardSelectPicker(telemetryRetention);
  }
  panel.querySelector(".category-description").textContent = t(
    "telemetry.description", { days: dashboardConfiguration.telemetry_retention_days || 90 },
  );
  executionTelemetryRows = (Array.isArray(rows) ? rows : []).filter((row) => row && typeof row === "object");
  panel.querySelectorAll(".telemetry-actions button").forEach((button) => button.disabled = !executionTelemetryRows.length);
  const sorted = sortedExecutionTelemetryRows(), pageCount = Math.max(1, Math.ceil(sorted.length / EXECUTION_TELEMETRY_PAGE_SIZE));
  executionTelemetryPage = Math.min(Math.max(1, executionTelemetryPage), pageCount);
  const visibleRows = sorted.slice(
    (executionTelemetryPage - 1) * EXECUTION_TELEMETRY_PAGE_SIZE,
    executionTelemetryPage * EXECUTION_TELEMETRY_PAGE_SIZE,
  );
  body.replaceChildren();
  for (const row of visibleRows) {
    const line = document.createElement("tr");
    line.className = "telemetry-row";
    line.tabIndex = 0;
    line.setAttribute("role", "button");
    line.setAttribute("aria-label", t("telemetry.open_details", { date: telemetryDate(row.date) }));
    const open = () => {
      body.querySelectorAll(".telemetry-row[data-selected=\"true\"]")
        .forEach((candidate) => candidate.dataset.selected = "false");
      line.dataset.selected = "true";
      openTelemetryDetail(row.date, line);
    };
    line.addEventListener("click", open);
    line.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
    for (const value of [
      telemetryDate(row.date),
      row.prompt_count,
      telemetryDuration(row.average_total_execution_seconds),
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
  pagination.replaceChildren();
  if (sorted.length > EXECUTION_TELEMETRY_PAGE_SIZE) {
    const summary = document.createElement("span"), previous = document.createElement("button"), next = document.createElement("button");
    summary.className = "log-pagination__summary";
    summary.textContent = t("telemetry.page", { page: executionTelemetryPage, pages: pageCount, count: sorted.length });
    previous.type = next.type = "button";
    previous.textContent = t("history.previous");
    next.textContent = t("history.next");
    previous.disabled = executionTelemetryPage <= 1;
    next.disabled = executionTelemetryPage >= pageCount;
    previous.addEventListener("click", () => { executionTelemetryPage -= 1; executionTelemetry(executionTelemetryRows); });
    next.addEventListener("click", () => { executionTelemetryPage += 1; executionTelemetry(executionTelemetryRows); });
    pagination.append(summary, previous, next);
  }
  updateExecutionTelemetrySortHeaders();
}
let telemetryDetailTrigger = null, telemetryDetailPayload = null, telemetryDetailDate = null, telemetryDetailRequestId = 0;
function telemetryMs(value) { return typeof value === "number" && value >= 0 ? telemetryDuration(value / 1000) : t("format.unavailable"); }
function telemetryPercent(value) {
  const percent = Number(value);
  return Number.isFinite(percent)
    ? locale.number(percent, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "%"
    : t("format.unavailable");
}
function telemetryMetric(label, value) {
  const field = document.createElement("div"); field.className = "field";
  field.append(Object.assign(document.createElement("span"), { className: "label", textContent: label }), Object.assign(document.createElement("strong"), { textContent: telemetryMs(value) }));
  return field;
}
function telemetryRunMetric(value, phaseTelemetry) {
  if (typeof value === "number" && value >= 0) return telemetryMs(value);
  return phaseTelemetry === "RECORDED" ? t("telemetry.not_executed") : t("telemetry.not_recorded_short");
}
function telemetryLabel(phase) { return t("telemetry.phase." + String(phase || "").toLowerCase(), {}, String(phase || t("format.unavailable"))); }
function telemetryDetailSortableTable(columns, rows, initialSort, appendRow) {
  let sort = initialSort;
  const table = document.createElement("table"), head = document.createElement("thead"), headRow = document.createElement("tr"), body = document.createElement("tbody"), headers = new Map();
  table.className = "telemetry-table";
  const compare = (left, right) => {
    if (typeof left === "number" && typeof right === "number") return left - right;
    return locale.compare(String(left ?? ""), String(right ?? ""));
  };
  const updateHeaders = () => headers.forEach((header, key) => {
    const active = sort.key === key;
    header.dataset.sortIndicator = active ? sort.direction === "asc" ? "↑" : "↓" : "↕";
    header.setAttribute("aria-sort", active ? sort.direction === "asc" ? "ascending" : "descending" : "none");
  });
  const renderRows = () => {
    const multiplier = sort.direction === "asc" ? 1 : -1;
    const ordered = [...rows].sort((left, right) => compare(
      columns.find((column) => column.key === sort.key).value(left),
      columns.find((column) => column.key === sort.key).value(right),
    ) * multiplier);
    body.replaceChildren();
    ordered.forEach((item) => appendRow(item, body));
    updateHeaders();
  };
  columns.forEach((column) => {
    const header = document.createElement("th");
    header.scope = "col";
    header.className = "log-sortable";
    header.dataset.sortKey = column.key;
    header.tabIndex = 0;
    header.textContent = t(column.label);
    header.setAttribute("aria-label", t("table.sort_by", { column: header.textContent }));
    const activate = (event) => {
      sort = sort.key === column.key
        ? { key: column.key, direction: sort.direction === "asc" ? "desc" : "asc" }
        : { key: column.key, direction: column.defaultDirection || "asc" };
      renderRows();
      blurSortableHeaderAfterPointerClick(event);
    };
    header.addEventListener("click", activate);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(event); }
    });
    headers.set(column.key, header);
    headRow.append(header);
  });
  head.append(headRow); table.append(head, body); renderRows();
  return table;
}
function setTelemetryDetailDownloads(payload, date) {
  telemetryDetailPayload = payload && typeof payload === "object" ? payload : null;
  telemetryDetailDate = telemetryDetailPayload && typeof date === "string" ? date : null;
  for (const [button, label] of [
    [$("telemetryDetailDownloadMarkdown"), "telemetry.download_detail_markdown"],
    [$("telemetryDetailDownloadJson"), "telemetry.download_detail_json"],
  ]) {
    const available = Boolean(telemetryDetailPayload && telemetryDetailDate);
    button.hidden = !available;
    button.disabled = !available;
    button.title = t(label, { date: telemetryDate(date) });
    button.setAttribute("aria-label", t(label, { date: telemetryDate(date) }));
  }
}
function telemetryMarkdownCell(value) {
  return String(value ?? t("format.unavailable")).replaceAll("|", "\\|").replaceAll(/[\r\n]+/g, " ").trim();
}
function telemetryMarkdownTable(headings, rows) {
  return [
    `| ${headings.map(telemetryMarkdownCell).join(" | ")} |`,
    `| ${headings.map(() => "---").join(" | ")} |`,
    ...rows.map((row) => `| ${row.map(telemetryMarkdownCell).join(" | ")} |`),
  ].join("\n");
}
function telemetryDetailMarkdown(detail, date) {
  const summary = detail?.summary || {}, phases = Array.isArray(detail?.phases) ? detail.phases : [];
  const runs = Array.isArray(detail?.runs) ? detail.runs : [], bottlenecks = detail?.bottlenecks || {};
  const summaryRows = [
    [t("telemetry.executions"), summary.executions ?? 0], [t("telemetry.complete"), summary.completed ?? 0],
    [t("telemetry.blocked"), summary.blocked ?? 0], [t("telemetry.failed"), summary.failed ?? 0],
    [t("telemetry.average_total"), telemetryMs(summary.total_wall_time?.average_ms)],
    [t("telemetry.median_total"), telemetryMs(summary.total_wall_time?.median_ms)],
    [t("telemetry.active_processing"), telemetryMs(summary.active_processing_time?.average_ms)],
    [t("telemetry.average_wait"), telemetryMs(summary.queue_wait?.average_ms)],
    [t("telemetry.provider"), telemetryMs(summary.provider_execution?.average_ms)],
    [t("telemetry.validation"), telemetryMs(summary.validation?.average_ms)],
    [t("telemetry.external_wait"), telemetryMs(summary.external_wait?.average_ms)],
    [t("telemetry.overhead"), telemetryMs(summary.overhead?.average_ms)],
  ];
  const phaseRows = phases.map((phase) => [telemetryLabel(phase.phase), telemetryMs(phase.average_ms), telemetryMs(phase.median_ms), telemetryMs(phase.total_ms), telemetryPercent(phase.share_percent), phase.runs]);
  const runRows = runs.map((run) => [
    run.run_id, run.started_at ? locale.dateTime(new Date(run.started_at)) : t("format.unavailable"), run.status,
    telemetryMs(run.total_duration_ms), telemetryRunMetric(run.queue_wait_ms, run.phase_telemetry), telemetryRunMetric(run.provider_duration_ms, run.phase_telemetry),
    telemetryRunMetric(run.validation_duration_ms, run.phase_telemetry), telemetryRunMetric(run.external_wait_ms, run.phase_telemetry),
    run.largest_phase ? telemetryLabel(run.largest_phase) : telemetryRunMetric(null, run.phase_telemetry), run.producer_type,
    run.repository, run.model,
  ]);
  const bottleneckLines = [
    `${t("telemetry.longest_average_phase")}: ${bottlenecks.longest_average_phase ? telemetryLabel(bottlenecks.longest_average_phase.phase) : t("format.unavailable")}`,
    `${t("telemetry.largest_accumulated_phase")}: ${bottlenecks.largest_accumulated_phase ? telemetryLabel(bottlenecks.largest_accumulated_phase.phase) : t("format.unavailable")}`,
    ...(Array.isArray(bottlenecks.top_time_consumers) ? bottlenecks.top_time_consumers.map((item) => `${telemetryLabel(item.phase)} — ${telemetryPercent(item.share_percent)}`) : []),
  ];
  return [
    `# ${t("telemetry.detail_title", { date: telemetryDate(date) })}`, "", t("telemetry.detail_description"), "",
    `## ${t("telemetry.summary")}`, "", telemetryMarkdownTable([t("table.details"), t("history.markdown_value")], summaryRows), "",
    `## ${t("telemetry.phase_timing")}`, "", phaseRows.length ? telemetryMarkdownTable([t("telemetry.phase"), t("telemetry.average"), t("telemetry.median"), t("telemetry.accumulated"), t("telemetry.share"), t("telemetry.runs")], phaseRows) : t("telemetry.not_recorded"), "",
    `## ${t("telemetry.bottlenecks")}`, "", ...bottleneckLines.map((line) => `- ${line}`), "",
    `## ${t("telemetry.runs")}`, "", runRows.length ? telemetryMarkdownTable([t("telemetry.run_id"), t("telemetry.start_time"), t("telemetry.status"), t("telemetry.average_total"), t("telemetry.average_wait"), t("telemetry.provider"), t("telemetry.validation"), t("telemetry.external_wait"), t("telemetry.largest_phase"), t("telemetry.producer_type"), t("telemetry.target_repository"), t("telemetry.model")], runRows) : t("format.unavailable"), "",
  ].join("\n");
}
function downloadTelemetryDetail(format) {
  if (!telemetryDetailPayload || !telemetryDetailDate) return;
  const markdown = format === "markdown";
  const content = markdown
    ? telemetryDetailMarkdown(telemetryDetailPayload, telemetryDetailDate)
    : JSON.stringify(telemetryDetailPayload, null, 2) + "\n";
  const url = URL.createObjectURL(new Blob([content], { type: markdown ? "text/markdown;charset=utf-8" : "application/json;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `execution-telemetry-${telemetryDetailDate}.${markdown ? "md" : "json"}`;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  void recordUserAction(markdown ? "telemetry_detail_markdown_downloaded" : "telemetry_detail_json_downloaded");
}
function closeTelemetryDetail() { const modal = $("telemetryDetailModal"); if (modal.open) modal.close(); }
function telemetryDetailTableScroll(table, label) {
  const scroll = document.createElement("div");
  scroll.className = "telemetry-detail-table-scroll";
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", label);
  scroll.append(table);
  return scroll;
}
function openTelemetryDetail(date, trigger) {
  if (!date) return;
  const requestId = ++telemetryDetailRequestId;
  telemetryDetailTrigger = trigger || document.activeElement;
  const modal = $("telemetryDetailModal"), content = $("telemetryDetailContent");
  $("telemetryDetailTitle").textContent = t("telemetry.detail_title", { date: telemetryDate(date) });
  $("telemetryDetailDescription").textContent = t("telemetry.detail_description");
  setTelemetryDetailDownloads(null, date);
  content.textContent = t("format.loading");
  if (!modal.open) modal.showModal(); resetDashboardModalInitialFocus(modal);
  fetch("/api/telemetry/" + encodeURIComponent(date), { cache: "no-store" })
    .then((response) => response.ok ? response.json() : Promise.reject())
    .then((detail) => {
      if (requestId !== telemetryDetailRequestId) return;
      setTelemetryDetailDownloads(detail, date);
      renderTelemetryDetail(detail, content);
    })
    .catch(() => { if (requestId === telemetryDetailRequestId) content.textContent = t("telemetry.details_unavailable"); });
}
function renderTelemetryDetail(detail, content) {
  content.replaceChildren(); const summary = detail?.summary || {};
  const metrics = document.createElement("section"); metrics.className = "telemetry-detail-metrics";
  metrics.append(Object.assign(document.createElement("h3"), { textContent: t("telemetry.summary") }));
  const grid = document.createElement("div"); grid.className = "technical-grid";
  for (const [label, value] of [[t("telemetry.executions"), summary.executions], [t("telemetry.complete"), summary.completed], [t("telemetry.blocked"), summary.blocked], [t("telemetry.failed"), summary.failed]]) grid.append(Object.assign(document.createElement("div"), { className: "field", textContent: `${label}: ${value ?? 0}` }));
  for (const [label, key] of [["telemetry.average_total", "total_wall_time"], ["telemetry.median_total", "total_wall_time"], ["telemetry.active_processing", "active_processing_time"], ["telemetry.average_wait", "queue_wait"], ["telemetry.provider", "provider_execution"], ["telemetry.validation", "validation"], ["telemetry.external_wait", "external_wait"], ["telemetry.overhead", "overhead"]]) grid.append(telemetryMetric(t(label), key === "total_wall_time" && label === "telemetry.median_total" ? summary[key]?.median_ms : summary[key]?.average_ms));
  metrics.append(grid); content.append(metrics);
  const phases = Array.isArray(detail?.phases) ? detail.phases : [];
  const phaseSection = document.createElement("section"); phaseSection.append(Object.assign(document.createElement("h3"), { textContent: t("telemetry.phase_timing") }));
  if (!phases.length) phaseSection.append(Object.assign(document.createElement("p"), { textContent: detail?.phase_telemetry_available ? t("telemetry.not_executed") : t("telemetry.not_recorded") }));
  else {
    const phaseColumns = [
      ["phase", "telemetry.phase"], ["average_ms", "telemetry.average"], ["median_ms", "telemetry.median"],
      ["total_ms", "telemetry.accumulated"], ["share_percent", "telemetry.share"], ["runs", "telemetry.runs"],
    ].map(([key, label]) => ({ key, label, value: (phase) => key === "phase" ? telemetryLabel(phase.phase) : Number(phase[key]) || 0 }));
    const phaseTable = telemetryDetailSortableTable(phaseColumns, phases, { key: "phase", direction: "asc" }, (phase, body) => {
      const row = document.createElement("tr");
      [telemetryLabel(phase.phase), telemetryMs(phase.average_ms), telemetryMs(phase.median_ms), telemetryMs(phase.total_ms), telemetryPercent(phase.share_percent), phase.runs]
        .forEach((value) => row.append(Object.assign(document.createElement("td"), { textContent: String(value) })));
      body.append(row);
    });
    phaseTable.classList.add("telemetry-phase-table");
    phaseSection.append(telemetryDetailTableScroll(phaseTable, t("telemetry.phase_timing")));
  }
  content.append(phaseSection);
  const bottlenecks = document.createElement("section"), top = detail?.bottlenecks?.top_time_consumers || [];
  bottlenecks.append(Object.assign(document.createElement("h3"), { textContent: t("telemetry.bottlenecks") }));
  const fields = document.createElement("div"); fields.className = "technical-grid";
  for (const [label, item] of [["telemetry.longest_average_phase", detail?.bottlenecks?.longest_average_phase], ["telemetry.largest_accumulated_phase", detail?.bottlenecks?.largest_accumulated_phase]]) fields.append(Object.assign(document.createElement("div"), { className: "field", textContent: `${t(label)}: ${item ? telemetryLabel(item.phase) : t("format.unavailable")}` }));
  for (const [label, value] of Object.entries(detail?.bottlenecks?.shares || {})) fields.append(Object.assign(document.createElement("div"), { className: "field", textContent: `${t("telemetry.share." + label)}: ${telemetryPercent(value)}` }));
  bottlenecks.append(fields);
  const list = document.createElement("ol"); for (const item of top) list.append(Object.assign(document.createElement("li"), { textContent: `${telemetryLabel(item.phase)} — ${telemetryPercent(item.share_percent)}` })); bottlenecks.append(list); content.append(bottlenecks);
  const runSection = document.createElement("section"), runs = Array.isArray(detail?.runs) ? detail.runs : [];
  runSection.append(Object.assign(document.createElement("h3"), { textContent: t("telemetry.runs") }));
  const runColumns = [
    ["run_id", "telemetry.run_id"], ["started_at", "telemetry.start_time", "desc"], ["status", "telemetry.status"],
    ["total_duration_ms", "telemetry.average_total"], ["queue_wait_ms", "telemetry.average_wait"], ["provider_duration_ms", "telemetry.provider"],
    ["validation_duration_ms", "telemetry.validation"], ["external_wait_ms", "telemetry.external_wait"], ["largest_phase", "telemetry.largest_phase"],
    ["producer_type", "telemetry.producer_type"], ["repository", "telemetry.target_repository"], ["model", "telemetry.model"],
  ].map(([key, label, defaultDirection]) => ({ key, label, defaultDirection, value: (run) => key === "largest_phase" ? telemetryLabel(run[key]) : key === "started_at" ? Date.parse(run[key]) || 0 : Number.isFinite(Number(run[key])) ? Number(run[key]) : String(run[key] || "") }));
  const runTable = telemetryDetailSortableTable(runColumns, runs, { key: "started_at", direction: "desc" }, (run, runBody) => {
    const row = document.createElement("tr"), id = document.createElement("button");
    row.className = "telemetry-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", String(run.run_id || t("format.unavailable")));
    id.type = "button";
    id.className = "telemetry-run-link";
    id.textContent = run.run_id;
    const open = () => {
      runBody.querySelectorAll('.telemetry-row[data-selected="true"]')
        .forEach((candidate) => { candidate.dataset.selected = "false"; });
      row.dataset.selected = "true";
      openPromptHistoryDetail({ run_id: run.run_id, title: run.run_id });
    };
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
    id.addEventListener("click", (event) => { event.stopPropagation(); open(); });
    const phaseTelemetry = run.phase_telemetry;
    const values = [id, run.started_at ? locale.dateTime(new Date(run.started_at)) : t("format.unavailable"), run.status, telemetryMs(run.total_duration_ms), telemetryRunMetric(run.queue_wait_ms, phaseTelemetry), telemetryRunMetric(run.provider_duration_ms, phaseTelemetry), telemetryRunMetric(run.validation_duration_ms, phaseTelemetry), telemetryRunMetric(run.external_wait_ms, phaseTelemetry), run.largest_phase ? telemetryLabel(run.largest_phase) : (phaseTelemetry === "RECORDED" ? t("telemetry.not_executed") : t("telemetry.not_recorded_short")), run.producer_type || t("format.unavailable"), run.repository || t("format.unavailable"), run.model || t("format.unavailable")];
    values.forEach((value) => {
      const cell = document.createElement("td");
      if (value instanceof Element) cell.append(value); else cell.textContent = String(value);
      row.append(cell);
    });
    runBody.append(row);
  });
  runSection.append(telemetryDetailTableScroll(runTable, t("telemetry.runs"))); content.append(runSection);
}
$("telemetryDetailClose").addEventListener("click", closeTelemetryDetail);
$("telemetryDetailDownloadMarkdown").addEventListener("click", () => downloadTelemetryDetail("markdown"));
$("telemetryDetailDownloadJson").addEventListener("click", () => downloadTelemetryDetail("json"));
$("telemetryDetailModal").addEventListener("close", () => { telemetryDetailRequestId += 1; telemetryDetailTrigger?.focus?.(); telemetryDetailTrigger = null; setTelemetryDetailDownloads(null, null); });
const renderTelemetryInOrder = executionTelemetry;
executionTelemetry = (rows) => {
  renderTelemetryInOrder(rows);
  arrangeOperationalCategories();
  addCategoryIcons();
};
window.executionTelemetry = executionTelemetry;
function updateFavicon() {
  const icon =
    document.documentElement.dataset.theme === "light"
      ? "/assets/operations-console/apple-touch-icon-light.png?v=operations-console-2"
      : "/assets/operations-console/apple-touch-icon-dark.png?v=operations-console-2";
  $("dashboardFavicon")?.setAttribute("href", icon);
  $("dashboardAppleTouchIcon")?.setAttribute("href", icon);
}
function renderDashboardTelemetry(snapshot) {
  updateFavicon();
  executionTelemetry(snapshot.telemetry);
}
updateFavicon();
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
  clearComponentLogSelection(component);
  if (componentLogServerPaged) void refreshComponentLogs({}, true);
  else renderComponentLogs();
}
document.querySelectorAll(".log-table").forEach((table) => {
  const component = logComponentForTable(table);
  const keys = ["line", "timestamp", "level", "event", "runId", "details"];
  table.querySelectorAll("th").forEach((header, index) => {
    const key = keys[index];
    header.classList.add("log-sortable");
    header.dataset.sortKey = key;
    header.tabIndex = 0;
    header.addEventListener("click", (event) => {
      setIndependentLogSort(component, key);
      blurSortableHeaderAfterPointerClick(event);
    });
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setIndependentLogSort(component, key);
      }
    });
  });
});
updateIndependentLogSortHeaders();
function componentLogRowKey(entry) {
  return [entry.line, entry.timestamp, entry.level, entry.event, entry.runId, entry.details]
    .map((value) => String(value ?? ""))
    .join("\u001f");
}
function componentLogText(entries) {
  const header = [
      t("table.number"),
      t("table.timestamp"),
      t("table.level"),
      t("table.event"),
      t("table.run_id"),
      t("table.details"),
    ].join("\t"),
    rows = entries.map((entry) => [
      entry.line,
      logTimestampText(entry.timestamp),
      entry.level,
      entry.event,
      entry.runId || "—",
      entry.details || "—",
    ].join("\t"));
  return [header, ...rows].join("\n");
}
function selectedComponentLogEntries(component) {
  const selected = selectedComponentLogRows[component];
  return componentLogEntries[component].filter((entry) => selected.has(componentLogRowKey(entry)));
}
function clearComponentLogSelection(component) {
  selectedComponentLogRows[component].clear();
  componentLogSelectionAnchor[component] = null;
}
function clearAllComponentLogSelections() {
  clearComponentLogSelection("inbox");
  clearComponentLogSelection("dashboard");
}
function selectComponentLogRow(component, key, event) {
  const selected = selectedComponentLogRows[component], visible = visibleComponentLogEntries(component),
    clickedIndex = visible.findIndex((entry) => componentLogRowKey(entry) === key),
    modifier = event.metaKey || event.ctrlKey;
  if (event.shiftKey && componentLogSelectionAnchor[component]) {
    const anchorIndex = visible.findIndex((entry) => componentLogRowKey(entry) === componentLogSelectionAnchor[component]);
    if (!modifier) selected.clear();
    for (const entry of visible.slice(Math.min(anchorIndex < 0 ? clickedIndex : anchorIndex, clickedIndex), Math.max(anchorIndex < 0 ? clickedIndex : anchorIndex, clickedIndex) + 1))
      selected.add(componentLogRowKey(entry));
  } else if (modifier) {
    if (selected.has(key)) selected.delete(key); else selected.add(key);
    componentLogSelectionAnchor[component] = key;
  } else {
    selected.clear();
    selected.add(key);
    componentLogSelectionAnchor[component] = key;
  }
  renderComponentLogs();
}
document.querySelectorAll(".log-table tbody").forEach((body) => {
  body.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-component-log-row]");
    if (row) selectComponentLogRow(row.dataset.component, row.dataset.componentLogRow, event);
  });
  body.addEventListener("keydown", (event) => {
    const row = event.target.closest("tr[data-component-log-row]");
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      selectComponentLogRow(row.dataset.component, row.dataset.componentLogRow, event);
    }
  });
});
document.addEventListener("copy", (event) => {
  if (window.getSelection()?.toString()) return;
  const entries = ["inbox", "dashboard"].flatMap((component) => selectedComponentLogEntries(component));
  if (!entries.length || !event.clipboardData) return;
  event.clipboardData.setData("text/plain", componentLogText(entries));
  event.preventDefault();
  void recordUserAction("component_log_rows_copied");
});
function filteredComponentLogEntries(component) {
  if (componentLogServerPaged) return componentLogEntries[component];
  const needle = locale.lower($("logFilter").value.trim()),
    level = $("logLevelFilter").value,
    events = new Set(
      [...($("logEventFilter")?.selectedOptions || [])].map(
        (option) => option.value,
      ),
    ),
    state = independentLogSortStates[component];
  return componentLogEntries[component]
    .filter((entry) => logMeetsMinimumLevel(entry, level))
    .filter((entry) => !events.size || events.has(String(entry.event || "")))
    .filter(entryMatchesLogTimeRange)
    .filter(
      (entry) =>
        !needle ||
        locale.lower([...Object.values(entry), logEventLabel(entry.event)].join(" ")).includes(needle),
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
function visibleComponentLogEntries(component) {
  const rows = filteredComponentLogEntries(component);
  // A server response contains just one page.  Do not derive the page count
  // from that response: doing so turns every requested page after page one
  // back into page one before it can be rendered.
  if (componentLogServerPaged) return rows;
  const pageCount = Math.max(1, Math.ceil(rows.length / LOG_PAGE_SIZE)),
    page = Math.min(
      Math.max(1, independentLogPageStates[component]),
      pageCount,
    );
  independentLogPageStates[component] = page;
  return rows.slice((page - 1) * LOG_PAGE_SIZE, page * LOG_PAGE_SIZE);
}
function updateLogValueFilters() {
  const entries = componentLogServerPaged
    ? [...componentLogAvailableEvents.inbox, ...componentLogAvailableEvents.dashboard].map((event) => ({ event }))
    : [...componentLogEntries.inbox, ...componentLogEntries.dashboard];
  for (const [id, key] of [["logEventFilter", "event"]]) {
    const select = $(id);
    if (!select) continue;
    const selected = new Set([...select.selectedOptions].map((option) => option.value));
    select.replaceChildren();
    [...new Set(entries.map((entry) => String(entry[key] || "")).filter(Boolean))].sort((a, b) => locale.compare(logEventLabel(a), logEventLabel(b))).forEach((value) => {
      const option = new Option(logEventLabel(value), value, false, selected.has(value)); select.add(option);
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
    clearComponentLogSelection(component);
    if (componentLogServerPaged) void refreshComponentLogs({}, true);
    else renderComponentLogs();
  });
  next.addEventListener("click", () => {
    independentLogPageStates[component] = page + 1;
    clearComponentLogSelection(component);
    if (componentLogServerPaged) void refreshComponentLogs({}, true);
    else renderComponentLogs();
  });
  navigation.append(summary, previous, next);
}
function renderComponentLogs() {
  localizeLogControls();
  updateLogValueFilters();
  for (const component of ["inbox", "dashboard"]) {
    const rows = filteredComponentLogEntries(component),
      body = $(component + "ComponentLog"),
      total = componentLogServerPaged ? componentLogTotals[component] : rows.length,
      pageCount = Math.max(1, Math.ceil(total / LOG_PAGE_SIZE)),
      visible = visibleComponentLogEntries(component);
    const copy = document.querySelector(`.component-log-copy[data-component="${component}"]`);
    if (copy) copy.disabled = !visible.length;
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
        const key = componentLogRowKey(entry), row = document.createElement("tr");
        row.className = "component-log-row";
        row.dataset.component = component;
        row.dataset.componentLogRow = key;
        row.tabIndex = 0;
        row.setAttribute("aria-selected", String(selectedComponentLogRows[component].has(key)));
        for (const [name, value] of [
          ["log-line-number", entry.line],
          ["", logTimestampText(entry.timestamp)],
          [
            "log-level log-level--" +
              locale.lower(entry.level).replaceAll(" ", "-"),
            entry.level,
          ],
          ["", logEventLabel(entry.event)],
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
    renderLogPagination(component, total, pageCount);
  }
  updateIndependentLogSortHeaders();
}
for (const [id, label] of [["logEventFilter", t("table.event")]]) {
  const control = document.createElement("label"), select = document.createElement("select");
  select.id = id; select.multiple = true; select.setAttribute("aria-label", label);
  control.htmlFor = id; control.append(label, select); $("componentLogControls").append(control);
  select.addEventListener("change", () => refreshComponentLogsForFilters());
}
function refreshComponentLogsForFilters() {
  independentLogPageStates.inbox = independentLogPageStates.dashboard = 1;
  clearAllComponentLogSelections();
  if (componentLogServerPaged) void refreshComponentLogs({}, true);
  else renderComponentLogs();
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
  $("logTimePreset").value = "";
  $("logSpecificDate").value = "";
  $("logDateFrom").value = "";
  $("logDateTo").value = "";
  syncDashboardSelectPicker($("logLevelFilter"));
  syncDashboardSelectPicker($("logTimePreset"));
  [...($("logEventFilter")?.options || [])].forEach((option) => { option.selected = false; });
  updateLogTimeFilterControls();
  refreshComponentLogsForFilters();
});
$("componentLogControls").append(resetLogFiltersButton);
$("logFilter").addEventListener("input", () => {
  refreshComponentLogsForFilters();
});
$("logLevelFilter").addEventListener("change", () => {
  refreshComponentLogsForFilters();
});
for (const id of ["logTimePreset", "logSpecificDate", "logDateFrom", "logDateTo"]) {
  $(id).addEventListener("change", () => {
    updateLogTimeFilterControls();
    refreshComponentLogsForFilters();
  });
}
$("logDateFrom").addEventListener("input", synchronizeLogRangeBounds);
installLogDateClearButtons();
updateLogTimeFilterControls();
renderComponentLogs();
function clearComponentLog(component, button) {
  const name =
    component === "inbox" ? t("component.execution_host") : t("logs.status_dashboard");
  confirmDashboardAction(
    t("action.clear_logs"),
    t("logs.clear_description", { component: name }),
    t("action.clear_logs"),
    { destructive: true },
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
function visibleComponentLogText(component) {
  return componentLogText(visibleComponentLogEntries(component));
}
function addComponentLogCopyButtons() {
  document.querySelectorAll(".component-log-download").forEach((download) => {
    if (download.parentElement.querySelector(".component-log-copy")) return;
    const button = document.createElement("button");
    button.className = "dashboard-action dashboard-action--copy component-log-copy";
    button.dataset.component = download.dataset.component;
    button.dataset.testid = "copy-" + download.dataset.component + "-visible-log";
    button.type = "button";
    button.textContent = "⧉";
    button.disabled = !visibleComponentLogEntries(button.dataset.component).length;
    button.title = t("logs.copy_visible");
    button.setAttribute("aria-label", t("logs.copy_visible"));
    button.addEventListener("click", () => {
      copyText(visibleComponentLogText(button.dataset.component))
        .then(() => void recordUserAction("component_visible_log_copied"))
        .catch(() => { button.title = t("copy.failed"); });
    });
    download.after(button);
  });
}
addComponentLogCopyButtons();
document.querySelectorAll(".clear-component-log").forEach((button) => {
  button.classList.add("dashboard-action", "dashboard-action--destructive");
  button.textContent = "⌧";
  button.title = t("action.clear_logs");
  button.setAttribute("aria-label", t("action.clear_logs"));
});
let pullRefreshStart = null,
  pullRefreshDistance = 0;
const pullRefresh = $("pullRefresh");
const dashboardScrollRegion = document.querySelector(".dashboard-scroll-region");
const pullRefreshActivationHeight = 40;
let modalBackgroundScrollTop = null;
function syncModalBackgroundScroll() {
  const hasOpenModal = Boolean(document.querySelector("dialog[open]"));
  if (hasOpenModal && modalBackgroundScrollTop === null) {
    modalBackgroundScrollTop = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.style.setProperty("--dashboard-modal-scroll-top", `-${modalBackgroundScrollTop}px`);
    document.body.classList.add("dashboard-modal-open");
    return;
  }
  if (!hasOpenModal && modalBackgroundScrollTop !== null) {
    const scrollTop = modalBackgroundScrollTop;
    modalBackgroundScrollTop = null;
    document.body.classList.remove("dashboard-modal-open");
    document.body.style.removeProperty("--dashboard-modal-scroll-top");
    window.scrollTo({ top: scrollTop, behavior: "auto" });
  }
}
new MutationObserver(syncModalBackgroundScroll).observe(document.body, {
  attributes: true,
  attributeFilter: ["open"],
  subtree: true,
});
function dashboardScrollTop() {
  if (window.matchMedia("(max-width:620px) and (orientation:portrait)").matches)
    return window.scrollY || document.documentElement.scrollTop || 0;
  return dashboardScrollRegion?.scrollTop || 0;
}
let inputFocusScrollTop = null;
function restoreIPhoneInputScroll() {
  if (inputFocusScrollTop === null) return;
  const scrollTop = inputFocusScrollTop;
  inputFocusScrollTop = null;
  window.setTimeout(() => window.scrollTo({ top: scrollTop, behavior: "auto" }), 250);
}
document.addEventListener("focusin", (event) => {
  if (
    !window.matchMedia("(max-width:620px) and (orientation:portrait)").matches ||
    !(event.target instanceof Element) ||
    !event.target.matches("input,select,textarea,[contenteditable=true]")
  ) return;
  inputFocusScrollTop = window.scrollY || document.documentElement.scrollTop || 0;
});
document.addEventListener("focusout", (event) => {
  if (
    event.target instanceof Element &&
    event.target.matches("input,select,textarea,[contenteditable=true]")
  ) restoreIPhoneInputScroll();
});
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
    dashboardScrollTop() > 0
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
  if (refresh) refreshDashboard();
}
function refreshDashboard() {
  pullRefresh.textContent = t("refresh.refreshing");
  pullRefresh.classList.add("pull-refresh--visible");
  pullRefresh.setAttribute("aria-hidden", "false");
  window.location.reload();
}
$("pageRefresh")?.addEventListener("click", refreshDashboard);
$("dashboardHealthIndicator")?.addEventListener("click", () => {
  const health = $("dashboardHealth"), indicator = $("dashboardHealthIndicator");
  if (!health || !indicator) return;
  const expanded = indicator.getAttribute("aria-expanded") === "true";
  health.classList.toggle("dashboard-health--open", !expanded);
  indicator.setAttribute("aria-expanded", String(!expanded));
});
document.addEventListener("click", (event) => {
  const health = $("dashboardHealth"), indicator = $("dashboardHealthIndicator"), tooltip = $("dashboardHealthTooltip");
  if (!health || !indicator || health.contains(event.target) || tooltip?.contains(event.target)) return;
  health.classList.remove("dashboard-health--open");
  indicator.setAttribute("aria-expanded", "false");
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  $("dashboardHealth")?.classList.remove("dashboard-health--open");
  $("dashboardHealthIndicator")?.setAttribute("aria-expanded", "false");
});
$("githubRateLimitRefresh")?.addEventListener("click", () => void refreshGithubRateLimit());
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
const PROMPT_HISTORY_DEEPLINK_PARAMETER = "prompt";
let promptHistoryEntries = [],
  promptHistoryPage = 1,
  promptHistorySelectedRunId = null,
  promptHistorySort = { key: "executed_at", direction: "desc" },
  promptHistoryDetailRunId = "",
  promptHistoryDetailLocationSyncing = false,
  promptHistoryDetailPayload = null;
function promptHistoryDetailFilename(extension) {
  return "execution-details-" + String(promptHistoryDetailRunId || "unknown").replace(/[^a-z0-9._-]+/gi, "-") + "." + extension;
}
function promptHistoryMarkdownText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(promptHistoryMarkdownText).join(", ");
  if (typeof value === "object") return Object.entries(value)
    .map(([key, item]) => `${promptHistoryMarkdownLabel(key)}: ${promptHistoryMarkdownText(item)}`)
    .join("; ");
  return String(value).replace(/\r?\n/g, "<br>").replace(/\|/g, "\\|");
}
function promptHistoryMarkdownLabel(key) {
  return String(key || "")
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/^./, (letter) => letter.toUpperCase());
}
function promptHistoryMarkdownTable(fields) {
  const rows = fields.filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!rows.length) return "";
  return ["| " + t("history.markdown_field") + " | " + t("history.markdown_value") + " |", "| --- | --- |", ...rows.map(([label, value]) => `| ${promptHistoryMarkdownText(label)} | ${promptHistoryMarkdownText(value)} |`)].join("\n") + "\n";
}
function promptHistoryMarkdownSection(title, fields) {
  const table = promptHistoryMarkdownTable(fields);
  return table ? `## ${promptHistoryMarkdownText(title)}\n\n${table}\n` : "";
}
function promptHistoryMarkdownList(title, values) {
  const items = Array.isArray(values) ? values.filter((value) => value !== null && value !== undefined && value !== "") : [];
  return items.length
    ? `## ${promptHistoryMarkdownText(title)}\n\n${items.map((value) => `- ${promptHistoryMarkdownText(value)}`).join("\n")}\n`
    : "";
}
function promptHistoryMarkdownPullRequests(pullRequests) {
  const labels = {
    implementation: t("detail.implementation_pull_request"),
    finalization: t("detail.finalization_pull_request"),
  };
  const items = promptDetailPullRequestEntries(pullRequests).map((item) => {
    const metrics = promptDetailPullRequestMetrics(item)
      .map(([label, value]) => `${promptHistoryMarkdownText(label)}: ${promptHistoryMarkdownText(value)}`)
      .join("; ");
    return `- ${promptHistoryMarkdownText(labels[item.role])}: [#${item.number}](${item.url})${metrics ? ` — ${metrics}` : ""}`;
  });
  return items.length ? `## ${promptHistoryMarkdownText(t("detail.pull_requests"))}\n\n${items.join("\n")}\n` : "";
}
function promptHistoryMarkdownCommitTimeline(entries) {
  const items = promptDetailCommitTimelineEntries(entries).map((item) => {
    const timestamp = Date.parse(item.observed_at);
    const observedAt = Number.isFinite(timestamp) ? locale.dateTime(new Date(timestamp)) : item.observed_at;
    const phase = t("state." + item.phase, {}, item.phase);
    const kind = t("detail.commit_type." + commitTimelineKind(item), {}, commitTimelineKind(item));
    const description = t("detail.commit_description." + item.description, {}, item.description);
    return `- ${promptHistoryMarkdownText(observedAt)} — ${promptHistoryMarkdownText(phase)} (${promptHistoryMarkdownText(kind)}) — \`${item.commit_sha}\` — ${promptHistoryMarkdownText(description)}`;
  });
  return items.length ? `## ${promptHistoryMarkdownText(t("detail.commit_timeline"))}\n\n${items.join("\n")}\n` : "";
}
function promptHistoryDetailMarkdown(payload, title) {
  const history = payload?.history && typeof payload.history === "object" ? payload.history : {};
  const execution = payload?.execution && typeof payload.execution === "object" ? payload.execution : {};
  const runtime = payload?.runtime && typeof payload.runtime === "object" ? payload.runtime : {};
  const usage = payload?.usage && typeof payload.usage === "object" ? payload.usage : {};
  const metadata = history.execution_metadata && typeof history.execution_metadata === "object" ? history.execution_metadata : {};
  const context = history.execution_context && typeof history.execution_context === "object" ? history.execution_context : {};
  const activity = history.execution_activity_summary && typeof history.execution_activity_summary === "object"
    ? history.execution_activity_summary : null;
  const timestamp = Date.parse(String(history.executed_at || ""));
  const sections = [
    promptHistoryMarkdownSection(t("detail.execution"), [
      [t("detail.prompt_title"), history.title || title],
      [t("detail.run_id"), history.run_id || promptHistoryDetailRunId],
      [t("detail.prompt_status"), promptHistoryStatus(history.status)],
      [t("detail.executed_at"), Number.isFinite(timestamp) ? locale.dateTime(new Date(timestamp)) : history.executed_at],
      [t("detail.execution_mode"), history.execution_mode],
      [t("detail.operator_handling"), history.emergency_cancelled_at ? t("handling.cancelled") : history.dismissed ? t("handling.dismissed") : t("handling.open")],
      ...(promptHistoryIsBlocked(history.status)
        ? [[t("detail.blocking_reason"), history.blocking_reason || history.execution_diagnostic]]
        : [[t("detail.execution_diagnostic"), history.execution_diagnostic]]),
    ]),
    promptHistoryMarkdownSection(t("detail.duration"), [
      [t("detail.agent_duration"), Number.isFinite(Number(execution.seconds)) ? durationText(Number(execution.seconds)) : null],
      [t("detail.total_duration"), Number.isFinite(Number(execution.total_seconds)) ? durationText(Number(execution.total_seconds)) : null],
    ]),
    promptHistoryMarkdownSection(t("ui.execution_context"), [
      [t("detail.producer"), history.producer_id],
      [t("detail.producer_type"), history.producer_type ? t(`enum.${history.producer_type}`) : null],
      [t("detail.producer_version"), history.producer_version],
      [t("detail.target_repository"), history.target_repository],
      [t("detail.target_branch"), history.target_branch],
      [t("detail.target_checkout"), history.target_checkout_path],
      [t("detail.tracked_files"), history.tracked_file_count],
      [t("detail.files_modified"), metadata.modified],
      [t("detail.files_created"), metadata.created],
      [t("detail.files_deleted"), metadata.deleted],
      [t("detail.codex_commands"), metadata.codex_commands_executed],
      ...Object.entries(context).map(([key, value]) => [promptHistoryMarkdownLabel(key), value]),
    ]),
    promptHistoryMarkdownSection(t("detail.runtime"), [
      [t("detail.runtime_provider"), runtimeProviderLabel(runtime.runtime_provider)],
      [t("detail.model"), runtime.model],
      [t("detail.reasoning_profile"), runtime.reasoning_profile],
      [t("detail.configuration_profile"), runtime.configuration_profile],
      [t("detail.codex_cli_version"), runtime.codex_cli_version],
      [t("detail.codex_cli_installation_path"), runtime.codex_cli_installation_path],
    ]),
    promptHistoryMarkdownSection(t("detail.provider_usage"), Object.entries(usage)
      .filter(([, value]) => value !== null && typeof value !== "object")
      .map(([key, value]) => [promptHistoryMarkdownLabel(key), value])),
    promptHistoryMarkdownSection(t("detail.execution_activity"), activity ? [
      [t("detail.activity_definition"), executionActivityDisplayValue(activity.activity?.codex_command_definition)],
      [t("detail.primary_codex_commands"), activity.activity?.primary_codex_commands_total],
      [t("detail.reviewer_codex_commands"), activity.activity?.reviewer_codex_commands_total],
      [t("detail.host_validation_commands"), activity.activity?.host_validation_commands_total],
      [t("detail.activity_total"), activity.activity?.overall_activity_total],
      [t("detail.delivery_baseline"), activity.terminal_delivery_diff?.transaction_baseline_sha],
      [t("detail.delivery_target"), activity.terminal_delivery_diff?.terminal_target_sha],
      [t("detail.delivery_paths"), activity.terminal_delivery_diff?.total_unique_changed_paths],
      [t("detail.delivery_renamed"), activity.terminal_delivery_diff?.renamed?.length],
    ] : [[t("detail.activity_unavailable"), t("detail.activity_unavailable")]]),
    promptHistoryMarkdownSection(t("detail.git_commit"), Object.entries(payload?.commits || {})),
    promptHistoryMarkdownPullRequests(payload?.pull_requests),
    promptHistoryMarkdownCommitTimeline(payload?.commit_timeline),
    promptHistoryMarkdownList(t("detail.execution_evidence"), payload?.evidence),
  ].filter(Boolean);
  return [`# ${promptHistoryMarkdownText(history.title || title || promptHistoryDetailRunId)}`, "", t("history.details_description"), "", ...sections].join("\n").trimEnd() + "\n";
}
function downloadPromptHistoryDetail(format) {
  if (!promptHistoryDetailPayload || !promptHistoryDetailRunId) return;
  const json = JSON.stringify(promptHistoryDetailPayload, null, 2);
  const title = String($("promptHistoryDetailTitle").textContent || promptHistoryDetailRunId).trim();
  const isMarkdown = format === "markdown";
  const content = isMarkdown ? promptHistoryDetailMarkdown(promptHistoryDetailPayload, title) : json + "\n";
  const url = URL.createObjectURL(new Blob([content], {
    type: isMarkdown ? "text/markdown;charset=utf-8" : "application/json;charset=utf-8",
  }));
  const link = document.createElement("a");
  link.href = url;
  link.download = promptHistoryDetailFilename(isMarkdown ? "md" : "json");
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  void recordUserAction(isMarkdown ? "prompt_history_details_markdown_downloaded" : "prompt_history_details_json_downloaded");
}
function setPromptHistoryDetailDownloads(payload) {
  promptHistoryDetailPayload = payload && typeof payload === "object" ? payload : null;
  const title = String($("promptHistoryDetailTitle").textContent || promptHistoryDetailRunId).trim();
  const markdown = $("promptHistoryDetailDownloadMarkdown"), json = $("promptHistoryDetailDownloadJson");
  for (const [button, label] of [[markdown, "history.download_details_markdown"], [json, "history.download_details_json"]]) {
    button.hidden = !promptHistoryDetailPayload;
    button.disabled = !promptHistoryDetailPayload;
    button.title = t(label, { title });
    button.setAttribute("aria-label", t(label, { title }));
  }
}
function promptHistoryDetailUrl(runId = "") {
  const url = new URL(window.location.href);
  if (runId) url.searchParams.set(PROMPT_HISTORY_DEEPLINK_PARAMETER, String(runId));
  else url.searchParams.delete(PROMPT_HISTORY_DEEPLINK_PARAMETER);
  return url;
}
function promptHistoryDetailRunFromUrl() {
  return new URLSearchParams(window.location.search)
    .get(PROMPT_HISTORY_DEEPLINK_PARAMETER) || "";
}
function updatePromptHistoryDetailUrl(runId, mode = "push") {
  const url = promptHistoryDetailUrl(runId);
  if (url.href === window.location.href) return;
  history[mode === "replace" ? "replaceState" : "pushState"]({}, "", url);
}
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
            [...Object.values(entry), promptHistoryDisplayStatus(entry)].join(" "),
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
function promptHistoryDisplayStatus(entry) {
  const outcome = promptHistoryStatus(entry?.status);
  if (entry?.emergency_cancelled_at) return outcome;
  return entry?.dismissed ? `${outcome} · ${t("handling.dismissed")}` : outcome;
}
function updatePromptHistoryColumnWidths(entries) {
  const table = document.querySelector("#promptHistory .log-table");
  if (!table) return;
  const probe = document.createElement("canvas").getContext("2d");
  if (!probe) return;
  const statusCell = document.querySelector("#promptHistoryRows .prompt-history-status");
  probe.font = getComputedStyle(statusCell || table).font;
  const widestStatus = [t("table.status"), ...entries.map(promptHistoryDisplayStatus)]
    .reduce((width, label) => Math.max(width, probe.measureText(label).width), 0);
  // Include the cell's inline padding.  The paired title width yields this
  // space first, so a long terminal/dismissed status never paints into it.
  const statusWidth = Math.max(120, Math.ceil(widestStatus + 16));
  const titleWidth = Math.max(144, 288 - Math.max(0, statusWidth - 120));
  const runIdWidth = 112;
  table.style.setProperty("--prompt-history-run-id-width", `${runIdWidth}px`);
  table.style.setProperty("--prompt-history-status-width", `${statusWidth}px`);
  table.style.setProperty("--prompt-history-title-width", `${titleWidth}px`);
  const header = table.tHead?.rows[0];
  if (!header) return;
  let columns = table.querySelector("colgroup");
  if (!columns) {
    columns = document.createElement("colgroup");
    table.prepend(columns);
  }
  while (columns.children.length < header.cells.length) {
    columns.append(document.createElement("col"));
  }
  while (columns.children.length > header.cells.length) columns.lastElementChild.remove();
  const headers = [...header.cells];
  const statusIndex = headers.findIndex(
    (cell) => cell.dataset.historySortKey === "status",
  );
  const runIdIndex = headers.findIndex(
    (cell) => cell.dataset.historySortKey === "run_id",
  );
  const titleIndex = headers.findIndex(
    (cell) => cell.dataset.historySortKey === "title",
  );
  if (runIdIndex >= 0) columns.children[runIdIndex].style.width = `${runIdWidth}px`;
  if (statusIndex >= 0) columns.children[statusIndex].style.width = `${statusWidth}px`;
  if (titleIndex >= 0) columns.children[titleIndex].style.width = `${titleWidth}px`;
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
    cell.colSpan = 9;
    cell.textContent = t("history.no_prompts");
    row.append(cell);
    body.append(row);
  } else
    {
    for (const entry of visible) {
      const row = document.createElement("tr"),
        status = document.createElement("td"),
        runSuffix = document.createElement("td"),
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
      row.dataset.selected = String(entry.run_id === promptHistorySelectedRunId);
      row.setAttribute("aria-label", t("history.open_details", { title: entry.title || entry.run_id }));
      row.addEventListener("contextmenu", (event) => event.preventDefault());
      row.addEventListener("selectstart", (event) => event.preventDefault());
      const openDetails = (event) => {
        if (event?.target?.closest("button,a")) return;
        promptHistorySelectedRunId = entry.run_id || null;
        body.querySelectorAll(".prompt-history-row[data-selected='true']").forEach((selectedRow) => {
          selectedRow.dataset.selected = "false";
        });
        row.dataset.selected = "true";
        openPromptHistoryDetail(entry);
      };
      row.addEventListener("click", openDetails);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDetails(event);
        }
      });
      const actionControls = document.createElement("div");
      actionControls.className = "prompt-history-actions";
      status.className =
        "prompt-history-status prompt-history-status--" +
        locale.lower(String(entry.status || ""));
      status.textContent = promptHistoryDisplayStatus(entry);
      runSuffix.textContent = String(entry.run_id || "—").slice(-5);
      const titleText = document.createElement("span");
      titleText.className = "prompt-history-title";
      titleText.textContent = String(
        entry.title || entry.run_id || t("retry.unavailable_title"),
      );
      title.append(titleText);
      if (entry.retry_status) {
        const lineage = document.createElement("span"),
          retryState = String(entry.retry_status).toLowerCase(),
          childSuffix = String(entry.retry_child_run_id || "").slice(-5);
        lineage.className = "prompt-history-lineage";
        lineage.textContent = t(
          retryState === "queued"
            ? "retry.queued"
            : retryState === "active"
              ? "retry.current_execution"
              : "retry.superseded_by",
          { run_id: childSuffix },
        );
        if (entry.retry_timestamp) {
          lineage.append(
            " · ",
            t("retry.started", {
              timestamp: formatPromptHistoryTimestamp(entry.retry_timestamp),
            }),
          );
        }
        title.append(lineage);
      }
      executed.textContent = formatPromptHistoryTimestamp(entry.executed_at) +
        formatPromptHistoryDuration(entry.total_execution_seconds);
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
          openPromptHistoryDocument(entry.run_id, "report");
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
          openPromptHistoryDocument(entry.run_id, "analysis");
        });
        analysis.append(view);
      } else analysis.textContent = "—";
      if (entry.run_id) {
        const button = document.createElement("button");
        button.className = "prompt-history-chat";
        if (Number(entry.chat_message_count) > 0)
          button.classList.add("prompt-history-chat--recorded");
        button.type = "button";
        button.title = t("history.open_chat", { title: title.textContent });
        button.setAttribute("aria-label", button.title);
        button.textContent = "⋯";
        button.addEventListener("click", () => openPromptHistoryChat(entry));
        chat.append(button);
      } else chat.textContent = "—";
      if (entry.can_retry === true && !entry.dismissed && entry.run_id) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "predecessor-retry execution-history-action";
        retry.textContent = t("action.retry_execution");
        retry.addEventListener("click", () => submitExecutionRetry(entry));
        actionControls.append(retry);
      }
      if (["BLOCKED", "FAILED"].includes(entry.status) && !entry.dismissed && !entry.retry_child_run_id && entry.run_id && !isActiveRun(latestStatus)) {
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "predecessor-retry execution-history-action execution-dismiss";
        dismiss.textContent = t("action.dismiss_execution");
        dismiss.addEventListener("click", () => dismissExecution(entry));
        actionControls.append(dismiss);
      }
      if (actionControls.childElementCount) action.append(actionControls);
      else action.textContent = "—";
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
      row.append(runSuffix);
      row.append(status);
      row.append(title, executed, report, analysis, chat, action, details);
      body.append(row);
    }
    }
  updatePromptHistoryColumnWidths(visible);
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
    promptHistorySelectedRunId = null;
    promptHistoryPage--;
    renderPromptHistory();
  });
  next.addEventListener("click", () => {
    promptHistorySelectedRunId = null;
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
      reconcilePromptHistoryDetailFromUrl();
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
      reconcilePromptHistoryDetailFromUrl();
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
  promptHistorySelectedRunId = null;
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
      promptHistorySelectedRunId = null;
      promptHistoryPage = 1;
      renderPromptHistory();
    };
    header.addEventListener("click", (event) => {
      sort();
      blurSortableHeaderAfterPointerClick(event);
    });
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
function installDashboardProjectSelector() {
  const options = $("dashboardTitlebarOptionsContent");
  const localeLabel = $("dashboardLocale")?.closest("label");
  if (!options || !localeLabel || $("dashboardProject")) return;
  const projectName = document.body.dataset.projectName?.trim() || "—";
  const projectId = document.body.dataset.projectId?.trim() || "legacy";
  const label = document.createElement("label");
  label.className = "dashboard-project";
  label.htmlFor = "dashboardProject";
  const text = document.createElement("span");
  text.dataset.i18n = "project.label";
  const select = document.createElement("select");
  select.id = "dashboardProject";
  select.dataset.i18nAriaLabel = "project.label";
  select.setAttribute("aria-label", t("project.label"));
  const option = document.createElement("option");
  option.value = projectId;
  option.textContent = projectName;
  select.append(option);
  label.append(text, select);
  options.insertBefore(label, localeLabel);
}
installDashboardProjectSelector();
const dashboardLocaleSelector = $("dashboardLocale");
const dashboardLocaleButton = $("dashboardLocaleButton"), dashboardLocaleMenu = $("dashboardLocaleMenu");
const dashboardTitlebarOptions = $("dashboardTitlebarOptions");
const dashboardTitlebarOptionsToggle = $("dashboardTitlebarOptionsToggle");
const dashboardTitlebarOptionsContent = $("dashboardTitlebarOptionsContent");
const compactTitlebarMedia = window.matchMedia("(max-width: 1240px)");
function syncTitlebarOptions() {
  const compact = compactTitlebarMedia.matches;
  const expanded = !compact || dashboardClientState.titlebarOptionsOpen === true;
  dashboardTitlebarOptionsToggle.hidden = !compact;
  dashboardTitlebarOptionsContent.hidden = !expanded;
  dashboardTitlebarOptionsToggle.setAttribute("aria-expanded", String(expanded));
}
compactTitlebarMedia.addEventListener("change", syncTitlebarOptions);
syncTitlebarOptions();
dashboardTitlebarOptionsToggle.addEventListener("click", () => {
  const expanded = dashboardTitlebarOptionsToggle.getAttribute("aria-expanded") === "true";
  dashboardClientState.titlebarOptionsOpen = !expanded;
  saveDashboardClientState();
  syncTitlebarOptions();
});
function setLocaleMenuOpen(open) {
  dashboardLocaleMenu.hidden = !open;
  dashboardLocaleButton.setAttribute("aria-expanded", String(open));
}
function updateLocalePicker() {
  $("dashboardLocaleValue").textContent = t("language." + dashboardLocale);
  document.querySelectorAll("[data-dashboard-locale]").forEach((option) => {
    const selected = option.dataset.dashboardLocale === dashboardLocale;
    option.textContent = t("language." + option.dataset.dashboardLocale);
    option.setAttribute("aria-selected", String(selected));
  });
}
function changeDashboardLocale(value) {
  dashboardLocale = normalizeLocale(value);
  locale = createLocaleService(dashboardLocale);
  dashboardClientState.locale = dashboardLocale;
  saveDashboardClientState();
  window.location.reload();
}
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
    [".dashboard-titlebar__options > summary span", "header.options"],
    [".dashboard-project > span", "project.label"],
    [".dashboard-locale span", "language.label"],
    ["#dashboardTitle", "dashboard.title"],
    ["#dashboardSplashTitle", "dashboard.title"],
    ["#dashboardSplashLoading", "dashboard.loading"],
    ["#platformVersionLabel", "footer.platform_version"],
    ["#confirmationModalCancel", "action.cancel"],
    ["#confirmationModalConfirm", "action.confirm"],
    ["#predecessorRetry", "recovery.action"],
    ["#queueItems > summary > strong", "section.inbox_queue"],
    ["#promptHistory > summary > strong", "section.prompt_history"],
    ["#currentRun > summary .label", "section.active_prompt"],
    ["#rateLimits > summary > strong", "section.remaining_usage"],
    ["#platformHealth > summary > strong", "section.platform_components"],
    ["#componentLogs > summary > strong", "section.logs"],
    ["#technicalDetails > summary > strong", "section.technical_details"],
    ["#technicalRepositoryTitle", "technical.repository"],
    ["#technicalRepositoryStateLabel", "technical.repository_status"],
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
  document.querySelectorAll("[data-workspace-label]").forEach((label) => {
    label.textContent = t(label.dataset.workspaceLabel);
  });
  document.querySelectorAll("#dashboardLocale option").forEach((option) => {
    option.textContent = t("language." + option.value);
  });
  updateLocalePicker();
  $("themeToggle").setAttribute("aria-label", t("header.enable_light"));
  $("toggleAllSections").setAttribute("aria-label", t("header.open_all"));
  $("dashboardSplashVersion").textContent = t("dashboard.platform_version", {
    version: $("dashboardSplashVersion").dataset.platformVersion,
  });
  setUpdateMode(updateModeKey);
  providerNeutralLabels();
  localizeOpenPullRequestStatuses();
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
  changeDashboardLocale(dashboardLocaleSelector.value);
});
dashboardLocaleButton.addEventListener("click", (event) => {
  // The picker is deliberately nested in its label so the native select keeps
  // an accessible name. Prevent the label's default activation from reopening
  // its visually-hidden native control and immediately dismissing the menu on
  // narrow direct-touch browsers.
  event.preventDefault();
  event.stopPropagation();
  setLocaleMenuOpen(dashboardLocaleMenu.hidden);
});
dashboardLocaleButton.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setLocaleMenuOpen(false);
});
dashboardLocaleMenu.addEventListener("click", (event) => {
  const option = event.target.closest("[data-dashboard-locale]");
  if (!option) return;
  dashboardLocaleSelector.value = option.dataset.dashboardLocale;
  changeDashboardLocale(option.dataset.dashboardLocale);
});
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".dashboard-locale__picker")) setLocaleMenuOpen(false);
});
const workspaceDatabaseField = $("workspaceDatabaseField");
if (workspaceDatabaseField) {
  const content = document.createElement("div"), download = document.createElement("a");
  content.className = "workspace-database__content";
  const path = workspaceDatabaseField.querySelector("pre");
  if (path) content.append(localFolderButton(path.textContent.trim(), { containingFolder: true }));
  download.className = "dashboard-action dashboard-action--download workspace-database__download";
  download.id = "workspaceDatabaseDownload";
  download.href = "/api/engineering-database/download?audit=download";
  download.download = "";
  download.dataset.i18nTitle = "workspace.download_database";
  download.dataset.i18nAriaLabel = "workspace.download_database";
  download.textContent = "↓";
  content.append(download);
  workspaceDatabaseField.append(content);
}
const workspaceLocation = document.querySelector('[data-workspace-label="ui.workspace_location"] + pre');
replaceWithLocalFolderButton(workspaceLocation);
replaceWithLocalFolderButton($("rateLimitProviderPath"));
applyDashboardLocale();
let dashboardConfiguration = {}, dashboardConfigurationLoaded = false;
const configurationFields = Object.freeze({
  configurationLogRetention: ["log_retention_days", Number],
  configurationTelemetryRetention: ["telemetry_retention_days", Number],
  configurationLogLevel: ["log_level", String],
  configurationInboxScanInterval: ["inbox_scan_interval_seconds", Number],
  configurationOpenPrInterval: ["open_pr_check_interval_seconds", Number],
  configurationDashboardStreamInterval: ["dashboard_stream_interval_seconds", Number],
  configurationProviderReadinessInterval: ["provider_readiness_refresh_seconds", Number],
  configurationCodexCapacityReserve: ["codex_capacity_reserve_percent", Number],
  configurationPlatformHealthInterval: ["platform_health_refresh_seconds", Number],
  configurationComponentDetailsInterval: ["component_details_refresh_seconds", Number],
  configurationDatabaseMaintenanceInterval: ["database_maintenance_interval_seconds", Number],
});
const dashboardSelectPickers = new Map();
function syncDashboardSelectPicker(select) {
  const picker = dashboardSelectPickers.get(select);
  if (!picker) return;
  const nativeOptions = [...select.options];
  const menuOptions = [...picker.menu.querySelectorAll("[data-dashboard-select-value]")];
  if (menuOptions.length !== nativeOptions.length || menuOptions.some((option, index) => option.dataset.dashboardSelectValue !== nativeOptions[index]?.value)) {
    picker.menu.replaceChildren(...nativeOptions.map((nativeOption) => {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "option");
      option.dataset.dashboardSelectValue = nativeOption.value;
      return option;
    }));
  }
  const selected = select.selectedOptions[0];
  picker.value.textContent = selected?.textContent || "";
  picker.button.disabled = select.disabled;
  picker.menu.querySelectorAll("[data-dashboard-select-value]").forEach((option, index) => {
    const nativeOption = nativeOptions[index];
    option.textContent = nativeOption?.textContent || "";
    option.disabled = select.disabled || nativeOption?.disabled === true;
    option.setAttribute("aria-selected", String(option.dataset.dashboardSelectValue === select.value));
  });
}
function setDashboardSelectPickerOpen(picker, open) {
  picker.menu.hidden = !open;
  picker.button.setAttribute("aria-expanded", String(open));
}
function enhanceDashboardSelectPicker(select) {
  // The locale control already has its own accessible custom picker in the
  // title bar.  Treating it as a generic select would render a second one.
  if (!(select instanceof HTMLSelectElement) || select.id === "dashboardLocale" || select.multiple || dashboardSelectPickers.has(select)) return;
  select.classList.add("dashboard-select__native");
  const picker = document.createElement("span"), button = document.createElement("button"), value = document.createElement("span"), arrow = document.createElement("span"), menu = document.createElement("span");
  const menuId = `${select.id}Menu`;
  picker.className = "dashboard-locale__picker dashboard-select-picker";
  button.className = "dashboard-locale__button";
  button.type = "button";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", menuId);
  button.setAttribute("aria-label", select.getAttribute("aria-label") || select.labels?.[0]?.textContent?.trim() || "");
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "⌄";
  button.append(value, arrow);
  menu.className = "dashboard-locale__menu";
  menu.id = menuId;
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  [...select.options].forEach((nativeOption) => {
    const option = document.createElement("button");
    option.type = "button";
    option.setAttribute("role", "option");
    option.dataset.dashboardSelectValue = nativeOption.value;
    option.disabled = nativeOption.disabled;
    menu.append(option);
  });
  picker.append(button, menu);
  select.after(picker);
  const state = { picker, button, value, menu };
  dashboardSelectPickers.set(select, state);
  const refresh = () => syncDashboardSelectPicker(select);
  select.addEventListener("change", refresh);
  button.addEventListener("click", (event) => {
    // Avoid the parent label activating the visually-hidden native select on
    // mobile Safari after the custom button has opened its listbox.
    event.preventDefault();
    event.stopPropagation();
    setDashboardSelectPickerOpen(state, menu.hidden);
  });
  button.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setDashboardSelectPickerOpen(state, false);
  });
  menu.addEventListener("click", (event) => {
    const option = event.target.closest("[data-dashboard-select-value]");
    if (!option || option.disabled) return;
    select.value = option.dataset.dashboardSelectValue;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    setDashboardSelectPickerOpen(state, false);
    // Mobile Safari may still change the visual viewport when focus returns
    // to this control after a pointer selection, despite preventScroll.
    // A pointer user does not need a new focus target; keyboard activation
    // does, and retains the accessible, scroll-safe return target.
    if (event.detail === 0) button.focus({ preventScroll: true });
    else option.blur();
  });
  refresh();
}
function syncInboxLocationChangeAvailability(queueDepth) {
  const button = $("configurationInboxOpen");
  if (!button) return;
  const field = button.closest(".configuration-field");
  let notice = $("configurationInboxUnavailable");
  if (!notice && field) {
    notice = document.createElement("p");
    notice.id = "configurationInboxUnavailable";
    notice.className = "configuration-inbox-unavailable";
    notice.setAttribute("role", "status");
    button.after(notice);
  }
  const inboxIsEmpty = Number.isInteger(queueDepth) && queueDepth === 0;
  button.disabled = !inboxIsEmpty;
  if (notice) {
    notice.hidden = inboxIsEmpty;
    notice.textContent = inboxIsEmpty ? "" : t("configuration.inbox_location_queue_not_empty");
    button.setAttribute("aria-describedby", notice.id);
  }
}
function enhanceDashboardSelectPickers() {
  document.querySelectorAll("select:not([multiple]):not(#dashboardLocale)").forEach(enhanceDashboardSelectPicker);
}
document.addEventListener("pointerdown", (event) => {
  dashboardSelectPickers.forEach((picker) => {
    if (!event.target.closest(".dashboard-select-picker")) setDashboardSelectPickerOpen(picker, false);
  });
});
function addConfigurationControlInfo() {
  for (const [id, helpKey] of [
    ["configurationLogRetention", "configuration.log_retention_help"],
    ["configurationTelemetryRetention", "configuration.telemetry_retention_help"],
    ["configurationLogLevel", "configuration.log_level_help"],
    ["configurationInboxScanInterval", "configuration.inbox_scan_interval_help"],
    ["configurationOpenPrInterval", "configuration.open_pr_interval_help"],
    ["configurationDashboardStreamInterval", "configuration.dashboard_stream_interval_help"],
    ["configurationProviderReadinessInterval", "configuration.provider_readiness_interval_help"],
    ["configurationCodexCapacityReserve", "configuration.codex_capacity_reserve_help"],
    ["configurationPlatformHealthInterval", "configuration.platform_health_interval_help"],
    ["configurationComponentDetailsInterval", "configuration.component_details_interval_help"],
    ["configurationDatabaseMaintenanceInterval", "configuration.database_maintenance_interval_help"],
  ]) {
    const control = $(id), label = control?.closest("label"), text = label?.querySelector(":scope > span");
    if (!text) continue;
    text.classList.add("label");
    let info = text.querySelector(".configuration-info");
    if (!info) {
      info = document.createElement("span");
      info.className = "configuration-info";
      info.setAttribute("role", "img");
      info.tabIndex = 0;
      info.textContent = "i";
      text.append(info);
    }
    const help = t(helpKey);
    info.title = help;
    info.setAttribute("aria-label", help);
  }
}
function renderConfigurationInboxLocation() {
  const button = $("configurationInboxOpen"), location = $("configurationInbox")?.textContent.trim();
  const field = button?.closest(".configuration-field"), label = field?.querySelector(".label");
  if (!field || !label) return;
  field.classList.add("configuration-inbox-field");
  let value = $("configurationInboxLocation");
  if (!value) {
    value = document.createElement("button");
    value.id = "configurationInboxLocation";
    value.className = "configuration-inbox-location local-folder-link";
    value.type = "button";
    label.after(value);
  }
  value.textContent = location || "—";
  const path = localFolderPath(location);
  value.disabled = !path;
  if (path) {
    const labelText = t("workspace.open_local_folder", { path });
    value.title = labelText;
    value.setAttribute("aria-label", labelText);
    value.onclick = () => void openLocalFolder(path);
  } else value.onclick = null;
}
function providerLoginStatusBlock() {
  const configuration = $("configuration");
  if (!configuration) return null;
  let block = $("configurationProviderLoginStatus");
  if (!block) {
    block = document.createElement("section");
    block.id = "configurationProviderLoginStatus";
    block.className = "configuration-provider-status";
    block.setAttribute("aria-live", "polite");
    const title = document.createElement("h2");
    title.textContent = t("configuration.provider_login_status");
    block.append(title);
    for (const provider of ["CODEX", "GITHUB"]) {
      const row = document.createElement("div");
      row.className = "configuration-provider-status__row";
      row.dataset.provider = provider;
      const repair = Object.assign(document.createElement("button"), { className: "configuration-provider-status__repair", type: "button", hidden: true });
      repair.dataset.providerRepair = provider;
      const logout = Object.assign(document.createElement("button"), { className: "configuration-provider-status__logout", type: "button", textContent: t("configuration.provider_logout") });
      logout.dataset.providerLogout = provider;
      row.append(
        Object.assign(document.createElement("span"), { className: "configuration-provider-status__dot", ariaHidden: "true" }),
        Object.assign(document.createElement("strong"), { textContent: provider === "CODEX" ? "Codex" : "GitHub" }),
        Object.assign(document.createElement("span"), { className: "configuration-provider-status__label" }),
        repair,
        logout,
      );
      block.append(row);
    }
    const runtimeBlock = validationEnvironmentBlock();
    configuration.querySelector("summary")?.after(block);
    block.after(runtimeBlock);
  }
  return block;
}
function validationEnvironmentDetail(label, attribute) {
  const detail = document.createElement("span");
  detail.className = "configuration-validation-environment__detail";
  const value = document.createElement("code");
  value.dataset[attribute] = "true";
  value.textContent = "—";
  detail.append(Object.assign(document.createElement("span"), { textContent: label }), value);
  return detail;
}
function validationEnvironmentBlock() {
  const runtimeBlock = document.createElement("section");
  runtimeBlock.id = "configurationValidationEnvironmentStatus";
  runtimeBlock.className = "configuration-validation-environment";
  runtimeBlock.setAttribute("aria-live", "polite");
  runtimeBlock.append(Object.assign(document.createElement("h3"), {
    textContent: t("configuration.validation_environment"),
  }));
  const runtime = document.createElement("div");
  runtime.className = "configuration-provider-status__row configuration-validation-environment__row";
  runtime.dataset.executionRuntime = "true";
  const repair = Object.assign(document.createElement("button"), {
    className: "configuration-provider-status__repair", type: "button", hidden: true,
    textContent: t("configuration.execution_runtime_repair"),
  });
  repair.dataset.executionRuntimeRepair = "true";
  const details = document.createElement("div");
  details.className = "configuration-validation-environment__details";
  details.append(
    validationEnvironmentDetail(t("configuration.python_path"), "executionRuntimePath"),
    validationEnvironmentDetail(t("configuration.python_version"), "executionRuntimeVersion"),
  );
  runtime.append(
    Object.assign(document.createElement("span"), { className: "configuration-provider-status__dot", ariaHidden: "true" }),
    Object.assign(document.createElement("strong"), { textContent: t("configuration.execution_runtime") }),
    Object.assign(document.createElement("span"), { className: "configuration-provider-status__label" }),
    repair,
  );
  runtimeBlock.append(runtime, details);
  return runtimeBlock;
}
const PROVIDER_READINESS_KEYS = Object.freeze(["codex", "github"]);
const CHECK_FAILED_PROVIDERS = Object.freeze({
  codex: { state: "CHECK_FAILED" },
  github: { state: "CHECK_FAILED" },
});
function syncStickyHeaderOffset() {
  const header = document.querySelector(".dashboard-sticky-header");
  if (!header) return;
  document.documentElement.style.setProperty(
    "--dashboard-sticky-header-height",
    `${Math.ceil(header.getBoundingClientRect().height)}px`,
  );
}
function syncFooterOffset() {
  const footer = document.querySelector(".footer");
  if (!footer) return;
  document.documentElement.style.setProperty(
    "--dashboard-footer-height",
    `${Math.ceil(footer.getBoundingClientRect().height)}px`,
  );
}
const stickyHeader = document.querySelector(".dashboard-sticky-header");
if (stickyHeader && typeof ResizeObserver === "function") {
  new ResizeObserver(syncStickyHeaderOffset).observe(stickyHeader);
}
const dashboardFooter = document.querySelector(".footer");
if (dashboardFooter && typeof ResizeObserver === "function") {
  new ResizeObserver(syncFooterOffset).observe(dashboardFooter);
}
syncStickyHeaderOffset();
syncFooterOffset();
const providerReadinessActions = new Map();
let providerInteractiveRepairInProgress = false;
function providerDisplayName(provider) {
  return provider === "CODEX" || provider === "codex" ? "Codex" : "GitHub";
}
function providerReadinessState(providers, provider) {
  return String(providers?.[provider]?.state || "CHECK_FAILED");
}
function renderProviderLoginStatus(block, providers) {
  renderProviderReadinessBanner(providers);
  block.querySelectorAll("[data-provider]").forEach((row) => {
    const provider = String(row.dataset.provider || "").toLowerCase();
    const state = providerReadinessState(providers, provider);
    const logout = row.querySelector("[data-provider-logout]"), repair = row.querySelector("[data-provider-repair]");
    row.dataset.providerState = state;
    row.querySelector(".configuration-provider-status__label").textContent = t(`configuration.provider_status.${state}`, {}, t("configuration.provider_status.CHECK_FAILED"));
    logout.hidden = state !== "READY";
    logout.disabled = state !== "READY";
    const action = state === "UNAVAILABLE" ? "install" : state === "AUTH_REQUIRED" ? "login" : null;
    repair.hidden = !action;
    repair.disabled = providerInteractiveRepairInProgress;
    repair.textContent = action ? t(`notification.provider_readiness.${action}`, { provider: providerDisplayName(provider) }) : "";
  });
}
function renderExecutionRuntimeStatus(runtime) {
  // The validation card is a sibling of the provider card, not its child.
  // Scope the lookup to that card so a successful runtime check is always
  // reflected in its own row.
  const validation = $("configurationValidationEnvironmentStatus");
  const row = validation?.querySelector("[data-execution-runtime]");
  const banner = $("executionRuntimeBanner"), title = $("executionRuntimeTitle"), message = $("executionRuntimeMessage"), bannerRepair = $("executionRuntimeRepair");
  if (!row || !banner || !title || !message || !bannerRepair) return;
  const state = String(runtime?.state || "CHECK_FAILED");
  const repair = row.querySelector("[data-execution-runtime-repair]");
  const executable = String(runtime?.executable || "").trim();
  const version = String(runtime?.version || "").trim();
  row.dataset.providerState = state;
  row.querySelector(".configuration-provider-status__label").textContent = t(`configuration.execution_runtime_status.${state}`, {}, t("configuration.execution_runtime_status.CHECK_FAILED"));
  const path = validation?.querySelector("[data-execution-runtime-path]");
  const runtimeVersion = validation?.querySelector("[data-execution-runtime-version]");
  if (path) path.textContent = executable || "—";
  if (runtimeVersion) runtimeVersion.textContent = version || "—";
  const needsRepair = state !== "READY";
  repair.hidden = !needsRepair;
  repair.disabled = providerInteractiveRepairInProgress;
  banner.hidden = !needsRepair;
  if (needsRepair) {
    banner.className = `dashboard-status-banner dashboard-status-banner--execution-runtime dashboard-status-banner--provider-${state.toLowerCase()}`;
    title.textContent = t("notification.execution_runtime.title");
    message.textContent = t(`notification.execution_runtime.${state.toLowerCase()}`, {}, t("notification.execution_runtime.check_failed"));
    bannerRepair.hidden = false;
    bannerRepair.disabled = providerInteractiveRepairInProgress;
    bannerRepair.textContent = t("configuration.execution_runtime_repair");
  }
  syncStickyHeaderOffset();
}
async function refreshProviderLoginStatus() {
  const block = providerLoginStatusBlock();
  if (!block) return;
  try {
    const [response, runtimeResponse] = await Promise.all([
      fetch("/api/provider-login-status", { cache: "no-store" }),
      fetch("/api/execution-runtime-status", { cache: "no-store" }),
    ]);
    const [payload, runtime] = await Promise.all([response.json(), runtimeResponse.json()]);
    if (!response.ok || !payload || typeof payload.providers !== "object") throw Error();
    renderProviderLoginStatus(block, payload.providers);
    renderExecutionRuntimeStatus(runtimeResponse.ok ? runtime : { state: "CHECK_FAILED" });
  } catch {
    renderProviderLoginStatus(block, CHECK_FAILED_PROVIDERS);
    renderExecutionRuntimeStatus({ state: "CHECK_FAILED" });
  }
}
let providerReadinessRefreshIntervalMs = 300_000, providerReadinessRefreshTimer = null;
function scheduleProviderReadinessRefresh() {
  if (providerReadinessRefreshTimer !== null) window.clearTimeout(providerReadinessRefreshTimer);
  providerReadinessRefreshTimer = window.setTimeout(async () => {
    if (document.visibilityState === "visible") await refreshProviderLoginStatus();
    scheduleProviderReadinessRefresh();
  }, providerReadinessRefreshIntervalMs);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  void refreshProviderLoginStatus();
  scheduleProviderReadinessRefresh();
});
function renderProviderReadinessBanner(providers) {
  PROVIDER_READINESS_KEYS.forEach((key) => {
    const prefix = key === "codex" ? "codex" : "github";
    const banner = $(`${prefix}ProviderReadinessBanner`), title = $(`${prefix}ProviderReadinessTitle`), message = $(`${prefix}ProviderReadinessMessage`), button = $(`${prefix}ProviderReadinessAction`);
    if (!banner || !title || !message || !button) return;
    const state = String(providers?.[key]?.state || "CHECK_FAILED");
    const provider = providerDisplayName(key);
    const action = state === "UNAVAILABLE" ? "install" : state === "AUTH_REQUIRED" ? "login" : null;
    banner.hidden = state === "READY";
    providerReadinessActions.set(key, action ? { provider: key.toUpperCase(), action } : null);
    if (banner.hidden) return;
    banner.className = `dashboard-status-banner dashboard-status-banner--provider-readiness dashboard-status-banner--provider-${state.toLowerCase()}`;
    title.textContent = t("notification.provider_readiness.title", { provider });
    message.textContent = t(`notification.provider_readiness.${state.toLowerCase()}`, { provider });
    button.hidden = !action;
    button.disabled = providerInteractiveRepairInProgress;
    button.textContent = action ? t(`notification.provider_readiness.${action}`, { provider }) : "";
  });
  syncStickyHeaderOffset();
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[id$='ProviderReadinessAction'], [data-provider-repair]");
  if (!button || providerInteractiveRepairInProgress) return;
  const key = button.dataset.providerRepair?.toLowerCase() || (button.id.startsWith("codex") ? "codex" : "github");
  const pending = providerReadinessActions.get(key);
  if (!pending) return;
  const { provider, action } = pending;
  const providerName = provider === "CODEX" ? "Codex" : "GitHub";
  const confirmed = await confirmDashboardAction(t("notification.provider_readiness.title", { provider: providerName }), t(`notification.provider_readiness.${action}_confirm`, { provider: providerName }), t(`notification.provider_readiness.${action}`, { provider: providerName }));
  if (!confirmed) return;
  providerInteractiveRepairInProgress = true;
  document.querySelectorAll("[id$='ProviderReadinessAction'], [data-provider-repair]").forEach((candidate) => { candidate.disabled = true; });
  button.disabled = true;
  try {
    const response = await fetch("/api/provider-login/repair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, action }) });
    if (!response.ok) throw Error();
  } catch {
    $(`${key}ProviderReadinessMessage`).textContent = t("notification.provider_readiness.repair_failed", { provider: providerName });
  } finally {
    window.setTimeout(async () => {
      providerInteractiveRepairInProgress = false;
      await refreshProviderLoginStatus();
    }, 1200);
  }
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("#executionRuntimeRepair, [data-execution-runtime-repair]");
  if (!button || providerInteractiveRepairInProgress) return;
  providerInteractiveRepairInProgress = true;
  document.querySelectorAll("#executionRuntimeRepair, [data-execution-runtime-repair]").forEach((candidate) => { candidate.disabled = true; });
  try {
    const response = await fetch("/api/execution-runtime/repair", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (!response.ok) throw Error();
  } catch {
    $("executionRuntimeMessage").textContent = t("notification.execution_runtime.repair_failed");
  } finally {
    providerInteractiveRepairInProgress = false;
    await refreshProviderLoginStatus();
  }
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-provider-logout]");
  if (!button || button.disabled) return;
  const provider = String(button.dataset.providerLogout || "");
  const confirmed = await confirmDashboardAction(t("configuration.provider_logout"), t("configuration.provider_logout_confirm", { provider: provider === "CODEX" ? "Codex" : "GitHub" }), t("configuration.provider_logout"), { destructive: true });
  if (!confirmed) return;
  button.disabled = true;
  try {
    const response = await fetch("/api/provider-login/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider }) });
    if (!response.ok) throw Error();
  } finally {
    await refreshProviderLoginStatus();
  }
});
const MACHINE_SCOPED_WORKSPACE_FIELD_IDS = Object.freeze([
  "workspaceFreeDiskSpace",
]);
const CONFIGURATION_CONTROL_SCOPES = Object.freeze([
  {
    containerClass: "queue-project-settings",
    fieldIds: ["configurationInboxScanInterval", "configurationOpenPrInterval"],
    parentId: "queueItems",
    statusId: "queueProjectSettingsStatus",
  },
  {
    containerClass: "log-settings",
    fieldIds: ["configurationLogRetention", "configurationLogLevel"],
    parentId: "componentLogs",
    statusId: "logSettingsStatus",
  },
  {
    containerClass: "platform-settings",
    fieldIds: ["configurationPlatformHealthInterval"],
    parentId: "platformHealth",
    statusId: "platformSettingsStatus",
  },
]);
function moveConfigurationControls({ beforeId, containerClass, fieldIds, parentId, statusId }) {
  const parent = $(parentId), before = beforeId ? $(beforeId) : null;
  if (!parent || (beforeId && !before)) return;
  let controls = parent.querySelector(`.${containerClass}`);
  if (!controls) {
    controls = document.createElement("div");
    controls.className = `configuration-controls ${containerClass}`;
    const status = document.createElement("p");
    status.id = statusId;
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    controls.append(status);
    parent.insertBefore(controls, before);
  }
  const status = $(statusId);
  fieldIds.forEach((id) => {
    const label = $(id)?.closest("label");
    if (label) controls.insertBefore(label, status);
  });
}
function moveProjectScopedConfiguration() {
  const queue = $("queueItems");
  const inboxField = $("configurationInboxOpen")?.closest(".configuration-field");
  if (!queue || !inboxField) return;
  queue.append(inboxField);
  moveConfigurationControls(CONFIGURATION_CONTROL_SCOPES[0]);
}
function moveMachineScopedWorkspaceDetails() {
  // Only a direct child is a valid insertion anchor for the machine-scoped
  // fields. Provider readiness owns a nested configuration group of its own.
  const configuration = $("configuration"), controls = configuration?.querySelector(":scope > .configuration-controls");
  if (!configuration || !controls) return;
  const databaseSection = configuration.querySelector(".workspace-database-section") || $("workspaceDatabaseField")?.closest(".workspace-database-section");
  if (!databaseSection) return;
  configuration.insertBefore(databaseSection, controls);
  MACHINE_SCOPED_WORKSPACE_FIELD_IDS.forEach((id) => {
    const field = $(id);
    if (field) databaseSection.insertBefore(field, $("workspaceDatabaseField"));
  });
}
function groupHostComponentConfiguration() {
  const configuration = $("configuration"), controls = configuration?.querySelector(":scope > .configuration-controls");
  const componentDetails = $("configurationComponentDetailsInterval")?.closest("label"), dashboardStatus = $("configurationDashboardStreamInterval")?.closest("label"), status = $("configurationStatus");
  if (!configuration || !controls || !componentDetails || !dashboardStatus || !status) return;
  let section = $("configurationHostComponents");
  if (!section) {
    section = document.createElement("section");
    section.id = "configurationHostComponents";
    section.className = "configuration-host-components";
    const title = document.createElement("h2");
    title.dataset.i18n = "configuration.dashboard_settings";
    section.append(title);
    configuration.insertBefore(section, controls);
  }
  section.querySelector("h2").textContent = t("configuration.dashboard_settings");
  let hostControls = section.querySelector(":scope > .configuration-controls");
  if (!hostControls) {
    hostControls = document.createElement("div");
    hostControls.className = "configuration-controls";
    section.append(hostControls);
  }
  hostControls.append(componentDetails, dashboardStatus, status);
}
function ensureProviderReadinessConfigurationControl() {
  if ($("configurationProviderReadinessInterval")) return;
  const block = providerLoginStatusBlock();
  if (!block) return;
  const controls = document.createElement("div"), label = document.createElement("label"), text = document.createElement("span"), select = document.createElement("select");
  controls.className = "configuration-controls configuration-provider-readiness-settings";
  text.className = "label";
  text.textContent = t("configuration.provider_readiness_interval");
  select.id = "configurationProviderReadinessInterval";
  [["60", "configuration.minute_1"], ["300", "configuration.minutes_5"], ["600", "configuration.minutes_10"]].forEach(([value, key]) => {
    const option = document.createElement("option");
    option.value = value;
    option.dataset.i18n = key;
    option.textContent = t(key);
    select.append(option);
  });
  label.htmlFor = select.id;
  label.append(text, select);
  controls.append(label);
  block.append(controls);
  enhanceDashboardSelectPicker(select);
}
function ensureCodexCapacityReserveConfigurationControl() {
  if ($("configurationCodexCapacityReserve")) return;
  const rateLimits = $("rateLimits"), status = $("rateLimitResetStatus");
  if (!rateLimits || !status) return;
  const controls = document.createElement("div"), label = document.createElement("label"), text = document.createElement("span"), select = document.createElement("select");
  controls.className = "configuration-controls capacity-reserve-configuration";
  text.className = "label";
  text.textContent = t("configuration.codex_capacity_reserve");
  select.id = "configurationCodexCapacityReserve";
  label.htmlFor = select.id;
  label.append(text, select);
  controls.append(label);
  status.after(controls);
  enhanceDashboardSelectPicker(select);
  syncCodexCapacityReserveOptions();
}
function localizeConfigurationOptions() {
  ensureProviderReadinessConfigurationControl();
  ensureCodexCapacityReserveConfigurationControl();
  const providerReadinessLabel = $("configurationProviderReadinessInterval")?.closest("label")?.querySelector(":scope > span");
  if (providerReadinessLabel) providerReadinessLabel.textContent = t("configuration.provider_readiness_interval");
  moveMachineScopedWorkspaceDetails();
  groupHostComponentConfiguration();
  moveProjectScopedConfiguration();
  CONFIGURATION_CONTROL_SCOPES.slice(1).forEach(moveConfigurationControls);
  // Every original control has now been placed in its scoped section. Remove
  // the empty source grid: its vertical margins otherwise remain between the
  // dashboard settings and fixed platform settings cards.
  const residualControls = $("configuration")?.querySelector(":scope > .configuration-controls");
  if (residualControls && !residualControls.childElementCount) residualControls.remove();
  addConfigurationControlInfo();
  renderConfigurationInboxLocation();
  providerLoginStatusBlock();
  document.querySelectorAll("#configurationLogRetention option, #configurationTelemetryRetention option").forEach((option) => {
    option.textContent = t("configuration.days", { days: option.value });
  });
  document.querySelectorAll("#configurationProviderReadinessInterval option").forEach((option) => {
    option.textContent = t(option.dataset.i18n);
  });
  document.querySelectorAll("#configurationDashboardStreamInterval option").forEach((option) => {
    option.textContent = option.value === "1"
      ? t("configuration.second_1")
      : t("configuration.seconds_value", { value: option.value });
  });
  syncCodexCapacityReserveOptions();
  dashboardSelectPickers.forEach((_, select) => syncDashboardSelectPicker(select));
}
function setDashboardConfigurationControlsDisabled(disabled) {
  Object.keys(configurationFields).forEach((id) => {
    const control = $(id);
    if (!control) return;
    control.disabled = disabled;
    syncDashboardSelectPicker(control);
  });
}
function positionConfigurationTooltip(info) {
  if (!window.matchMedia("(max-width:620px)").matches) {
    info.removeAttribute("data-tooltip-side");
    info.style.removeProperty("--configuration-tooltip-width");
    return;
  }
  const rect = info.getBoundingClientRect();
  const leftSpace = rect.left, rightSpace = window.innerWidth - rect.right;
  const side = leftSpace >= rightSpace ? "left" : "right";
  const available = Math.max(leftSpace, rightSpace) - 22;
  const width = Math.max(160, Math.min(280, window.innerWidth - 32, available));
  info.dataset.tooltipSide = side;
  info.style.setProperty("--configuration-tooltip-width", `${width}px`);
}
addConfigurationControlInfo();
const configurationInfoTooltips = [...document.querySelectorAll(".configuration-info")];
configurationInfoTooltips.forEach((info) => {
  info.addEventListener("pointerenter", () => positionConfigurationTooltip(info));
  info.addEventListener("focus", () => positionConfigurationTooltip(info));
});
window.addEventListener("resize", () => {
  configurationInfoTooltips.forEach((info) => positionConfigurationTooltip(info));
});
function configurationFieldStatus(control) {
  const field = control.closest("label");
  if (!field) return null;
  let status = field.querySelector(".configuration-field-status");
  if (!status) {
    status = document.createElement("span");
    status.className = "configuration-field-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    field.append(status);
  }
  return status;
}
async function saveDashboardConfiguration(control) {
  const [key, normalizer] = configurationFields[control.id] || [];
  if (!key) return;
  const value = normalizer(control.type === "checkbox" ? control.checked : control.value);
  const previous = control.dataset.savedValue || String(value);
  const retentionKey = key === "log_retention_days" || key === "telemetry_retention_days";
  if (retentionKey && Number(value) < Number(control.dataset.savedValue || value)) {
    const confirmed = await confirmDashboardAction(
      t(key === "telemetry_retention_days" ? "configuration.telemetry_retention" : "configuration.log_retention"),
      t(key === "telemetry_retention_days" ? "configuration.telemetry_retention_confirm" : "configuration.retention_confirm"),
      t("action.confirm"),
      { destructive: true },
    );
    if (!confirmed) {
      control.value = control.dataset.savedValue || String(value);
      syncDashboardSelectPicker(control);
      return;
    }
  }
  control.disabled = true;
  syncDashboardSelectPicker(control);
  try {
    const response = await fetch("/api/configuration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value, previous: normalizer(previous) }),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (response.status === 409 && payload.value !== undefined) {
        const savedValue = String(payload.value);
        control.value = savedValue;
        control.dataset.savedValue = savedValue;
        dashboardConfiguration = { ...dashboardConfiguration, [key]: payload.value };
        if (key === "codex_capacity_reserve_percent" && Number.isFinite(Number(payload.remaining_percent))) {
          syncCodexCapacityReserveOptions({ windows: [{ used_percent: 100 - Number(payload.remaining_percent) }] });
        } else {
          syncDashboardSelectPicker(control);
        }
      }
      const error = Error(payload.error_code || payload.error || "");
      error.remainingPercent = Number(payload.remaining_percent);
      throw error;
    }
    control.value = String(payload.value);
    control.dataset.savedValue = control.value;
    dashboardConfiguration = { ...dashboardConfiguration, [key]: payload.value };
    if (key === "codex_capacity_reserve_percent") {
      renderCodexCapacityReserveBanner(latestDashboardSnapshot?.rate_limits);
      syncCodexCapacityReserveOptions();
    }
    if (key === "open_pr_check_interval_seconds") {
      openPullRequestMonitorIntervalMs = Number(value) * 1e3;
      scheduleOpenPullRequestMonitor([...openPullRequestStatusByNumber.values()].map((status) => ({ status })));
    }
    if (key === "provider_readiness_refresh_seconds") {
      providerReadinessRefreshIntervalMs = Math.max(60_000, Number(value) * 1e3 || 300_000);
      scheduleProviderReadinessRefresh();
    }
    if (key === "platform_health_refresh_seconds") {
      platformHealthRefreshIntervalMs = Number(value) * 1e3;
      schedulePlatformHealthRefresh();
    }
    if (key === "component_details_refresh_seconds") {
      componentDetailsRefreshIntervalMs = Number(value) * 1e3;
      if (activeComponentDetails) startComponentDetailsRefresh(activeComponentDetails);
    }
    const status = configurationFieldStatus(control) || (control.closest(".queue-project-settings") ? $("queueProjectSettingsStatus") : control.closest(".log-settings") ? $("logSettingsStatus") : control.closest(".platform-settings") ? $("platformSettingsStatus") : $("telemetryRetentionStatus") || $("configurationStatus"));
    if (status) {
      status.textContent = t("configuration.saved");
      status.classList.add("configuration-status--saved");
    }
    if (key === "telemetry_retention_days") refreshDashboard();
  } catch (error) {
    const status = configurationFieldStatus(control) || (control.closest(".queue-project-settings") ? $("queueProjectSettingsStatus") : control.closest(".log-settings") ? $("logSettingsStatus") : control.closest(".platform-settings") ? $("platformSettingsStatus") : $("telemetryRetentionStatus") || $("configurationStatus"));
    if (status) {
      const code = String(error?.message || "");
      if (code === "codex_capacity_reserve_exceeds_remaining") {
        status.textContent = t("configuration.codex_capacity_reserve_exceeds_remaining", {
          remaining: locale.number(error.remainingPercent, { maximumFractionDigits: 0 }),
        });
      } else if (code === "codex_capacity_reserve_unavailable") {
        status.textContent = t("configuration.codex_capacity_reserve_unavailable");
      } else {
        status.textContent = t("configuration.save_failed");
      }
      status.classList.remove("configuration-status--saved");
    }
  } finally {
    control.disabled = false;
    syncDashboardSelectPicker(control);
  }
}
async function initializeDashboardConfiguration() {
  localizeConfigurationOptions();
  void refreshProviderLoginStatus();
  setDashboardConfigurationControlsDisabled(true);
  try {
    const response = await fetch("/api/configuration", { cache: "no-store" });
    if (!response.ok) throw Error();
    const configuration = await response.json();
    dashboardConfiguration = configuration;
    renderCodexCapacityReserveBanner(latestDashboardSnapshot?.rate_limits);
    syncCodexCapacityReserveOptions();
    Object.entries(configurationFields).forEach(([id, [key]]) => {
      const control = $(id);
      if (!control || configuration[key] === undefined) return;
      if (control.type === "checkbox") control.checked = configuration[key] === true;
      else {
        const savedValue = String(configuration[key]);
        control.value = savedValue;
        control.dataset.savedValue = savedValue;
      }
      syncDashboardSelectPicker(control);
    });
    syncCodexCapacityReserveOptions();
    openPullRequestMonitorIntervalMs = Number(configuration.open_pr_check_interval_seconds) * 1e3;
    providerReadinessRefreshIntervalMs = Math.max(60_000, Number(configuration.provider_readiness_refresh_seconds) * 1e3 || 300_000);
    platformHealthRefreshIntervalMs = Number(configuration.platform_health_refresh_seconds) * 1e3;
    componentDetailsRefreshIntervalMs = Number(configuration.component_details_refresh_seconds) * 1e3;
    schedulePlatformHealthRefresh();
    scheduleProviderReadinessRefresh();
  } catch {
    $("configurationStatus").textContent = t("configuration.load_failed");
    $("configurationStatus").classList.remove("configuration-status--saved");
  } finally {
    dashboardConfigurationLoaded = true;
    setDashboardConfigurationControlsDisabled(false);
  }
}
ensureProviderReadinessConfigurationControl();
ensureCodexCapacityReserveConfigurationControl();
Object.keys(configurationFields).forEach((id) => {
  $(id)?.addEventListener("change", (event) => void saveDashboardConfiguration(event.currentTarget));
});
$("configurationInboxOpen")?.addEventListener("click", () => {
  const modal = $("configurationInboxModal");
  const inbox = $("configurationInbox").textContent.trim();
  $("configurationInboxRoot").value = inbox.endsWith("/Inbox") ? inbox.slice(0, -"/Inbox".length) : inbox;
  $("configurationInboxStatus").textContent = "";
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
});
syncInboxLocationChangeAvailability();
$("configurationInboxModalClose")?.addEventListener("click", () => $("configurationInboxModal").close());
$("configurationInboxModalCloseAction")?.addEventListener("click", () => $("configurationInboxModal").close());
$("configurationInboxModal")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
$("configurationInboxBrowse")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  $("configurationInboxStatus").textContent = "";
  try {
    const response = await fetch("/api/configuration/inbox-location/browse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw Error(payload.error || t("configuration.inbox_location_failed"));
    if (!payload.cancelled && typeof payload.value === "string")
      $("configurationInboxRoot").value = payload.value;
  } catch (error) {
    $("configurationInboxStatus").textContent = error.message || t("configuration.inbox_location_failed");
  } finally {
    button.disabled = false;
  }
});
$("configurationInboxSave")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const root = $("configurationInboxRoot").value.trim();
  const confirmed = await confirmDashboardAction(
    t("configuration.inbox_location"),
    t("configuration.inbox_location_confirm", { path: root }),
    t("configuration.inbox_location_save"),
  );
  if (!confirmed) return;
  button.disabled = true;
  const browse = $("configurationInboxBrowse"), close = $("configurationInboxModalCloseAction");
  browse.disabled = true;
  close.disabled = true;
  $("configurationInboxRoot").readOnly = true;
  $("configurationInboxStatus").classList.remove("configuration-status--saved");
  $("configurationInboxStatus").textContent = t("configuration.inbox_location_restarting");
  try {
    const response = await fetch("/api/configuration/inbox-location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ inbox_root: root }),
    });
    const payload = await response.json();
    if (!response.ok) throw Error(
      payload.error_code === "inbox_not_empty"
        ? t("configuration.inbox_location_queue_not_empty")
        : payload.error_code === "inbox_watcher_restart_failed"
          ? t("configuration.inbox_location_restart_failed")
        : payload.error || t("configuration.inbox_location_failed"),
    );
    $("configurationInbox").textContent = `${payload.value}/Inbox`;
    renderConfigurationInboxLocation();
    $("configurationInboxStatus").textContent = t("configuration.inbox_location_saved");
    $("configurationInboxStatus").classList.add("configuration-status--saved");
    setTimeout(() => $("configurationInboxModal").close(), 700);
  } catch (error) {
    $("configurationInboxStatus").textContent = error.message || t("configuration.inbox_location_failed");
    $("configurationInboxStatus").classList.remove("configuration-status--saved");
  } finally {
    button.disabled = false;
    browse.disabled = false;
    close.disabled = false;
    $("configurationInboxRoot").readOnly = false;
  }
});
enhanceDashboardSelectPickers();
initializeDashboardConfiguration();
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
    "configuration",
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
  const context = chatContextEntry || {};
  const metadata = chatContextRun
    ? [
      "## " + t("detail.execution"),
      "",
      "**" + t("table.prompt_title").toLocaleUpperCase(dashboardLocale) + "**",
      String(context.title || chatContextRun),
      "",
      "**" + t("detail.run_id").toLocaleUpperCase(dashboardLocale) + "**",
      chatContextRun,
      "",
      "**" + t("table.executed_at").toLocaleUpperCase(dashboardLocale) + "**",
      formatTimestamp(context.executed_at),
    ].join("\n")
    : "";
  const entries = chatHistory
    .map(
      (entry) =>
        "## " +
        t(entry.role === "user" ? "chat.user" : "chat.assistant") +
        "\n\n" +
        "_" + chatMessageTimestampLabel(entry.created_at) + "_\n\n" +
        entry.text.trim(),
    )
    .filter(Boolean);
  return [
    "# " + t("chat.download_title"),
    metadata,
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
chatMessage = (role, text, createdAt) => {
  chatMessageWithDownload(role, text, createdAt);
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
  const chromeIcon = "/assets/operations-console/icon-transparent.png";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  themeColor.content = light ? "#f4f7fb" : "#15151d";
  document
    .querySelectorAll(".dashboard-app-icon,.dashboard-splash__icon")
    .forEach((image) => {
      image.src = chromeIcon;
    });
  themeToggle.setAttribute("aria-checked", String(light));
  themeToggle.setAttribute(
    "aria-label",
    light ? t("theme.enable_dark") : t("theme.enable_light"),
  );
  themeToggle.title = light ? t("theme.dark") : t("theme.light");
  updateFavicon();
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
const retryableReportAnalysisStatuses = new Set([
  "provider_failed", "provider_unavailable", "invalid_structured_response",
]);
function reportAnalysisCanRetry(text) {
  const status = String(text || "").match(/^- Status: `([a-z_]+)`$/m)?.[1];
  return retryableReportAnalysisStatuses.has(status);
}
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
function promptHistoryReportReloadButton() {
  let button = $("promptHistoryReportReload");
  if (button) return button;
  button = document.createElement("button");
  button.className = "dashboard-action dashboard-action--primary report-analysis-retry";
  button.id = "promptHistoryReportReload";
  button.type = "button";
  button.hidden = true;
  button.textContent = t("history.retry_report_load");
  button.addEventListener("click", () => { void reloadPromptHistoryReport(); });
  $("promptHistoryReportDownload").before(button);
  return button;
}
function loadPromptHistoryDocument() {
  const content = $("promptHistoryReportContent"),
    reload = promptHistoryReportReloadButton();
  reload.hidden = true;
  reload.disabled = true;
  $("promptHistoryReportCopy").hidden = true;
  $("promptHistoryReportDownload").hidden = true;
  $("promptHistoryReportRetry").hidden = true;
  content.replaceChildren();
  content.textContent = t(
    promptHistoryDocumentKind === "analysis"
      ? "history.analysis_loading"
      : "history.report_loading",
  );
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
      $("promptHistoryReportRetry").hidden =
        promptHistoryDocumentKind !== "analysis" || !reportAnalysisCanRetry(text);
    })
    .catch(() => {
      content.textContent = t(
        promptHistoryDocumentKind === "analysis"
          ? "history.analysis_unavailable"
          : "history.report_unavailable",
      );
      reload.hidden = promptHistoryDocumentKind !== "report";
      reload.disabled = false;
    });
}
function openPromptHistoryDocument(runId, kind = "report") {
  const modal = $("promptHistoryReportModal");
  promptHistoryReportRun = String(runId || "");
  promptHistoryDocumentKind = kind === "analysis" ? "analysis" : "report";
  promptHistoryReportText = "";
  $("promptHistoryReportModalTitle").dataset.modalGlyph = promptHistoryDocumentKind;
  $("promptHistoryReportModalTitle").textContent =
    promptHistoryDocumentKind === "analysis"
      ? t("table.analysis")
      : t("history.execution_report_title");
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
  loadPromptHistoryDocument();
}
async function reloadPromptHistoryReport() {
  if (promptHistoryDocumentKind !== "report" || !promptHistoryReportRun) return;
  loadPromptHistoryDocument();
}
async function retryPromptHistoryAnalysis() {
  if (promptHistoryDocumentKind !== "analysis" || !promptHistoryReportRun ||
      !reportAnalysisCanRetry(promptHistoryReportText)) return;
  const button = $("promptHistoryReportRetry"),
    content = $("promptHistoryReportContent");
  button.disabled = true;
  content.textContent = t("history.analysis_retry_loading");
  try {
    const response = await fetch(
      "/api/prompt-history/" + encodeURIComponent(promptHistoryReportRun) + "/analysis-retry",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    );
    const text = await response.text();
    if (!response.ok) {
      let detail = "";
      try { detail = JSON.parse(text).error || ""; } catch { /* controlled fallback below */ }
      throw Error(detail || t("history.analysis_retry_failed"));
    }
    promptHistoryReportText = text;
    renderMarkdownDocument(content, text);
    button.hidden = !reportAnalysisCanRetry(text);
    void recordUserAction("prompt_history_analysis_regenerated");
  } catch (error) {
    renderMarkdownDocument(content, promptHistoryReportText);
    showDashboardError(error instanceof Error ? error.message : "", t("history.analysis_retry_failed"));
  } finally {
    button.disabled = false;
  }
}
function detailField(label, value, preformatted = false, folder = false) {
  const field = document.createElement("p"),
    name = document.createElement("span"),
    output = document.createElement(preformatted ? "pre" : "span");
  field.className = "field";
  name.className = "label";
  name.textContent = label;
  const supplied = String(value ?? "—");
  const content = folder && localFolderPath(supplied)
    ? localFolderButton(supplied)
    : output;
  if (content === output) output.textContent = supplied;
  field.append(name, content);
  return field;
}
function detailListField(label, values) {
  const field = document.createElement("div"), name = document.createElement("span"), list = document.createElement("ul");
  field.className = "field detail-list-field";
  name.className = "label";
  name.textContent = label;
  const entries = Array.isArray(values) ? values.map((value) => String(value || "").trim()).filter(Boolean) : [];
  list.replaceChildren(...entries.map((value) => Object.assign(document.createElement("li"), {
    textContent: humanizeIdentifier(value),
  })));
  field.append(name, list);
  return field;
}
function detailExecutionModeField(value) {
  const field = detailField(t("detail.execution_mode"), value);
  const content = field.lastElementChild, row = document.createElement("span");
  row.className = "execution-mode-field__value";
  content.replaceWith(row);
  row.append(content, executionModeInfoButton());
  field.classList.add("execution-mode-field");
  return field;
}
function humanizeIdentifier(value) {
  const acronyms = { api: "API", ci: "CI", css: "CSS", html: "HTML", json: "JSON", ui: "UI" };
  return String(value || "").trim().split(/[_-]+/).filter(Boolean).map((part) => {
    const lower = part.toLowerCase();
    if (acronyms[lower]) return acronyms[lower];
    if (lower === "github") return "GitHub";
    if (lower === "python") return "Python";
    return lower.charAt(0).toUpperCase() + lower.slice(1);
  }).join(" ") || t("detail.not_recorded");
}
function executionContextSnapshotFields(context) {
  const profile = context?.validation_profile && typeof context.validation_profile === "object"
    ? context.validation_profile : null;
  const actionIntent = String(context?.action_intent || "").trim();
  return [
    ...(actionIntent ? [detailField(
      t("execution_context.action_intent"),
      t(`execution_context.action_intent.${actionIntent.toLowerCase()}`, {}, humanizeIdentifier(actionIntent)),
    )] : []),
    ...(profile?.tier ? [detailField(t("execution_context.validation_profile"), humanizeIdentifier(profile.tier))] : []),
    ...(profile?.version ? [detailField(t("execution_context.validation_profile_version"), profile.version)] : []),
    ...(Array.isArray(profile?.required_controls) && profile.required_controls.length
      ? [detailListField(t("execution_context.required_validation_controls"), profile.required_controls)] : []),
  ];
}
function promptHistoryRunIdField(runId) {
  const field = detailField(t("detail.run_id"), runId, true);
  field.classList.add("prompt-history-run-id-field");
  const copyLink = document.createElement("button");
  copyLink.className = "prompt-history-run-id-copy";
  copyLink.type = "button";
  copyLink.title = t("history.copy_link", { title: runId });
  copyLink.setAttribute("aria-label", copyLink.title);
  copyLink.textContent = "⧉";
  copyLink.addEventListener("click", () => {
    void copyText(promptHistoryDetailUrl(runId).href);
  });
  field.append(copyLink);
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
function promptHistoryIsBlocked(value) {
  return ["BLOCKED", "FAILED"].includes(String(value || "").toUpperCase());
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
function promptDetailCard(title, fields, wide = false, modifier = "") {
  const card = document.createElement("section"), heading = document.createElement("h3");
  card.className = "prompt-detail-card" + (wide ? " prompt-detail-card--wide" : "") + (modifier ? " " + modifier : "");
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
function promptDetailLeftbar(cards) {
  const sidebar = document.createElement("div");
  sidebar.className = "prompt-detail-leftbar";
  sidebar.append(...cards.filter(Boolean));
  return sidebar;
}
function promptDetailRightbar(cards) {
  const sidebar = document.createElement("div");
  sidebar.className = "prompt-detail-rightbar";
  sidebar.append(...cards.filter(Boolean));
  return sidebar;
}
function promptDetailDuration(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? durationText(seconds) : "—";
}
function promptDetailExecutionSections(history) {
  const timestamp = Date.parse(String(history.executed_at || ""));
  const context = history.execution_context && typeof history.execution_context === "object" ? history.execution_context : null;
  const contextFields = context ? [
    detailField(t("detail.mission_id"), executionContextValue(context.mission_id) || t("execution_context.not_supplied")),
    detailField(t("execution_context.business_summary"), executionContextValue(context.business_summary) || t("execution_context.not_supplied")),
    detailField(t("execution_context.engineering_summary"), executionContextValue(context.engineering_summary) || t("execution_context.not_supplied")),
    detailField(t("execution_context.execution_phase"), executionContextValue(context.execution_phase) || t("execution_context.not_supplied")),
    detailField(t("execution_context.mission_lifecycle"), executionContextValue(context.mission_lifecycle) || t("execution_context.not_supplied")),
    detailField(t("execution_context.decision_evidence_reference"), executionContextValue(context.decision_evidence_reference || context.decision_evidence) || t("execution_context.not_supplied")),
    detailField(t("execution_context.execution_receipt_reference"), executionContextValue(context.execution_receipt_reference || context.last_execution_receipt) || t("execution_context.not_supplied")),
    ...executionContextSnapshotFields(context),
  ] : [detailField(t("execution_context.snapshot"), t("execution_context.not_supplied"))];
  const summaryFields = [
    promptDetailStatusField(history.status),
    ...(promptHistoryIsBlocked(history.status) ? [detailField(
      t("detail.blocking_reason"),
      history.blocking_reason || history.execution_diagnostic || t("detail.not_recorded"),
      true,
    )] : []),
    detailField(t("detail.operator_handling"), history.emergency_cancelled_at ? t("handling.cancelled") : history.dismissed ? t("handling.dismissed") : t("handling.open")),
    ...(history.dismissed_at ? [detailField(
      t("detail.dismissed_at"),
      formatTimestamp(history.dismissed_at, String(history.dismissed_at)),
    )] : []),
    detailField(t("detail.prompt_title"), history.title),
    promptHistoryRunIdField(history.run_id),
    detailField(
      t("detail.executed_at"),
      Number.isFinite(timestamp)
        ? locale.dateTime(new Date(timestamp))
        : history.executed_at,
    ),
    ...(!promptHistoryIsBlocked(history.status) && executionContextValue(history.execution_diagnostic)
      ? [detailField(t("detail.execution_diagnostic"), history.execution_diagnostic, true)]
      : []),
  ];
  const contextMetadataFields = [
    detailExecutionModeField(history.execution_mode || t("detail.not_recorded")),
    detailField(t("detail.producer"), history.producer_id || t("detail.not_recorded")),
    detailField(t("detail.producer_type"), history.producer_type ? t(`enum.${history.producer_type}`) : t("detail.not_recorded")),
    detailField(t("detail.producer_version"), history.producer_version || t("detail.not_recorded")),
    detailField(t("detail.producer_submission_contract"), history.producer_submission_contract_version || t("execution_context.not_supplied")),
    detailField(t("detail.submission_id"), history.submission_id || t("execution_context.not_supplied"), true),
    detailField(t("execution_context.version"), history.execution_context_version || t("execution_context.not_supplied")),
    detailField(t("detail.mission_id"), history.mission_id || t("detail.not_recorded")),
    detailField(t("detail.engineering_action_id"), history.engineering_action_id || t("detail.not_recorded")),
    detailField(t("detail.correlation_id"), history.correlation_id || t("detail.not_recorded")),
    detailField(t("detail.target_repository"), history.target_repository || t("detail.not_recorded")),
    detailField(t("detail.target_branch"), history.target_branch || t("detail.not_recorded"), true),
    detailField(t("detail.target_checkout"), history.target_checkout_path || t("detail.not_recorded"), true, true),
    detailField(t("detail.tracked_files"), history.tracked_file_count ?? t("detail.not_recorded")),
    detailField(t("detail.files_modified"), history.execution_metadata?.modified ?? t("detail.not_recorded")),
    detailField(t("detail.files_created"), history.execution_metadata?.created ?? t("detail.not_recorded")),
    detailField(t("detail.files_deleted"), history.execution_metadata?.deleted ?? t("detail.not_recorded")),
    detailField(t("detail.codex_commands"), history.execution_metadata?.codex_commands_executed ?? t("detail.not_recorded")),
    ...contextFields,
  ];
  return [
    promptDetailCard(t("detail.execution"), summaryFields, false, "prompt-detail-card--execution-summary"),
    promptDetailCard(t("ui.execution_context"), contextMetadataFields, false, "prompt-detail-card--execution-context"),
  ];
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
    [t("detail.runtime_provider"), runtimeProviderLabel(runtime.runtime_provider)],
    [t("detail.model"), runtime.model],
    [t("detail.reasoning_profile"), runtime.reasoning_profile],
    [t("detail.configuration_profile"), runtime.configuration_profile],
    [t("detail.codex_cli_version"), runtime.codex_cli_version],
    [t("detail.codex_cli_installation_path"), runtime.codex_cli_installation_path, true],
  ]
    .filter(([, value]) => value)
    .map(([label, value, folder]) => detailField(label, value, false, folder));
  return fields.length ? promptDetailCard(t("detail.runtime"), fields) : null;
}
function promptDetailUsageSection(usage) {
  const labels = {
    input_tokens: t("detail.input_tokens"),
    cached_input_tokens: t("detail.cached_input_tokens"),
    uncached_input_tokens: t("detail.uncached_input_tokens"),
    output_tokens: t("detail.output_tokens"),
    total_tokens: t("detail.total_tokens"),
    provider_invocation_count: t("detail.provider_invocation_count"),
    max_input_tokens_per_invocation: t("detail.max_input_tokens"),
    usage_snapshot_count: t("detail.usage_snapshot_count"),
    intermediate_usage_delta_available: t("detail.intermediate_usage_delta_available"),
    maximum_incremental_input_tokens: t("detail.maximum_incremental_input_tokens"),
    actual_single_request_context_size: t("detail.actual_single_request_context_size"),
    active_context_size: t("detail.active_context_size"),
    estimated_credits: t("detail.estimated_credits"),
    estimated_eur: t("detail.estimated_eur"),
    speed_state: t("detail.speed_state"),
    usage_authority: t("detail.usage_authority"),
  };
  const visible = Object.entries(usage).filter(([key, value]) => labels[key] && value !== null && typeof value !== "object");
  const displayValue = (key, value) => {
    if (["actual_single_request_context_size", "active_context_size"].includes(key) && value === "UNAVAILABLE") {
      return t("format.unavailable");
    }
    if (key === "speed_state") return t(`provider_usage.speed.${String(value).toLowerCase()}`, {}, String(value));
    if (key === "usage_authority") return t(`provider_usage.authority.${String(value).toLowerCase()}`, {}, String(value));
    return value;
  };
  const fields = visible.map(([key, value]) =>
    detailField(
      labels[key] || key,
      key === "intermediate_usage_delta_available"
        ? (value ? t("detail.available") : t("format.unavailable"))
        : displayValue(key, value),
    ),
  );
  return fields.length ? promptDetailCard(t("detail.provider_usage"), fields) : null;
}
const EXECUTION_ACTIVITY_DISPLAY_KEYS = Object.freeze({
  "One persisted Codex CLI provider invocation. It is not a shell command, tool call, prompt, token count, or GitHub API request.": "detail.activity_definition_value",
  "GitHub evidence scoped to each PR; never summed into this run total.": "detail.delivery_pr_scope_value",
});
function executionActivityDisplayValue(value) {
  const key = EXECUTION_ACTIVITY_DISPLAY_KEYS[String(value || "")];
  return key ? t(key) : value;
}
function promptDetailExecutionActivitySection(activity) {
  if (!activity || typeof activity !== "object") {
    return promptDetailCard(t("detail.execution_activity"), [detailField(t("detail.activity_unavailable"), t("detail.activity_unavailable"))], false, "prompt-detail-card--execution-activity");
  }
  const counters = activity.activity || {};
  const diff = activity.terminal_delivery_diff || {};
  return promptDetailCard(t("detail.execution_activity"), [
    detailField(t("detail.activity_definition"), executionActivityDisplayValue(counters.codex_command_definition)),
    detailField(t("detail.primary_codex_commands"), counters.primary_codex_commands_total),
    detailField(t("detail.reviewer_codex_commands"), counters.reviewer_codex_commands_total),
    detailField(t("detail.host_validation_commands"), counters.host_validation_commands_total),
    detailField(t("detail.activity_total"), counters.overall_activity_total),
    detailField(t("detail.delivery_baseline"), diff.transaction_baseline_sha),
    detailField(t("detail.delivery_target"), diff.terminal_target_sha),
    detailField(t("detail.delivery_paths"), diff.total_unique_changed_paths),
    detailField(t("detail.delivery_renamed"), Array.isArray(diff.renamed) ? diff.renamed.length : undefined),
    detailField(t("detail.delivery_pr_scope"), executionActivityDisplayValue(diff.per_pr_changed_file_counts)),
  ], false, "prompt-detail-card--execution-activity");
}
function commitTimelineKind(item) {
  const mergeKinds = {
    implementation_merge_verified: "implementation_merge",
    finalization_merge_verified: "finalization_merge",
    reconciliation_merge_verified: "reconciliation_merge",
  };
  if (mergeKinds[item.description]) return mergeKinds[item.description];
  return {
    EXECUTE_AGENT: "implementation",
    LOCAL_REPOSITORY_VALIDATION: "validation",
    QUALITY_CONTROL_AGENT: "quality",
    REPAIR_AGENT: "repair",
    FINALIZE_AGENT: "finalization",
    RECONCILE_AGENT: "reconciliation",
  }[item.phase] || "other";
}
function promptDetailCommitTimelineEntries(entries) {
  return Array.isArray(entries) ? entries.filter((item) =>
    item && typeof item === "object" &&
    typeof item.phase === "string" &&
    typeof item.observed_at === "string" &&
    typeof item.commit_sha === "string" && /^[0-9a-f]{40}$/.test(item.commit_sha) &&
    typeof item.description === "string",
  ) : [];
}
function promptDetailCommitTimelineSection(entries) {
  const timeline = promptDetailCommitTimelineEntries(entries);
  const card = promptDetailCard(t("detail.commit_timeline"), [], false, "prompt-detail-card--commit-timeline");
  if (!timeline.length) {
    card.append(detailField(t("detail.recorded_evidence"), t("detail.commit_timeline_empty")));
    return card;
  }
  const list = document.createElement("ol");
  list.className = "prompt-detail-commit-timeline__list";
  const groups = timeline.reduce((current, item) => {
    const previous = current.at(-1);
    const kind = commitTimelineKind(item);
    if (!previous || previous.phase !== item.phase || previous.kind !== kind)
      current.push({ phase: item.phase, kind, entries: [] });
    current.at(-1).entries.push(item);
    return current;
  }, []);
  for (const group of groups) {
    const section = document.createElement("li");
    section.className = "prompt-detail-commit-timeline__phase prompt-detail-commit-timeline__phase--" + group.kind;
    section.dataset.commitKind = group.kind;
    const caption = document.createElement("h4");
    const kindBadge = document.createElement("span");
    kindBadge.className = "prompt-detail-commit-timeline__kind";
    kindBadge.textContent = t("detail.commit_type." + group.kind, {}, group.kind);
    const phaseName = document.createElement("span");
    phaseName.className = "prompt-detail-commit-timeline__phase-name";
    phaseName.textContent = t("state." + group.phase, {}, group.phase);
    caption.append(kindBadge, phaseName);
    const entries = document.createElement("ol");
    entries.className = "prompt-detail-commit-timeline__phase-entries";
    for (const item of group.entries) {
      const row = document.createElement("li");
      row.className = "prompt-detail-commit-timeline__item";
      const timestamp = Date.parse(item.observed_at);
      const meta = document.createElement("div");
      meta.className = "prompt-detail-commit-timeline__meta";
      meta.textContent = Number.isFinite(timestamp) ? locale.dateTime(new Date(timestamp)) : item.observed_at;
      const commit = document.createElement("code");
      commit.textContent = item.commit_sha;
      const description = document.createElement("span");
      description.textContent = t("detail.commit_description." + item.description, {}, item.description);
      row.append(meta, commit, description);
      entries.append(row);
    }
    section.append(caption, entries);
    list.append(section);
  }
  card.append(list);
  return card;
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
function promptDetailPullRequestEntries(pullRequests) {
  return Array.isArray(pullRequests) ? pullRequests.filter((item) =>
    item && ["implementation", "finalization"].includes(item.role) &&
    Number.isInteger(item.number) && item.number > 0 &&
    typeof item.url === "string" &&
    /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+$/.test(item.url),
  ) : [];
}
function promptDetailPullRequestMetrics(pullRequest) {
  const counts = [
    ["commit_count", "detail.pull_request_commits"],
    ["check_count", "detail.pull_request_checks"],
    ["changed_file_count", "detail.pull_request_changed_files"],
  ];
  return counts
    .filter(([key]) => Number.isInteger(pullRequest?.[key]) && pullRequest[key] >= 0)
    .map(([key, label]) => [t(label), locale.number(pullRequest[key])]);
}
function promptDetailPullRequestsSection(pullRequests) {
  const links = promptDetailPullRequestEntries(pullRequests);
  if (!links.length) return null;
  const labels = {
    implementation: t("detail.implementation_pull_request"),
    finalization: t("detail.finalization_pull_request"),
  };
  const fields = links.map((item) => {
    const label = labels[item.role], field = detailField(label, "");
    const link = document.createElement("a");
    link.className = "prompt-detail-pr-link";
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `#${item.number} ↗`;
    link.setAttribute("aria-label", `${label} #${item.number}`);
    field.lastElementChild.replaceChildren(link);
    const metrics = promptDetailPullRequestMetrics(item);
    if (metrics.length) {
      const summary = document.createElement("span");
      summary.className = "prompt-detail-pr-metrics";
      metrics.forEach(([metricLabel, value]) => {
        const metric = document.createElement("span");
        metric.className = "prompt-detail-pr-metric";
        metric.textContent = `${metricLabel}: ${value}`;
        summary.append(metric);
      });
      field.append(summary);
    }
    return field;
  });
  return promptDetailCard(t("detail.pull_requests"), fields, false, "prompt-detail-card--pull-requests");
}
function promptDetailEvidenceSection(evidence) {
  if (!evidence.length) return null;
  return promptDetailCard(
    t("detail.execution_evidence"),
    [detailField(t("detail.evidence"), evidence.join("\n"), true)],
  );
}
function promptDetailRecommendationHandoff(handoff) {
  if (!handoff || typeof handoff !== "object") return null;
  const recommendation = handoff.recommendation || {}, alternatives = Array.isArray(handoff.alternatives) ? handoff.alternatives : [];
  const missing = Array.isArray(handoff.missing_fields) ? handoff.missing_fields : [];
  const value = (item) => item || t("detail.not_recorded");
  const fields = [
    detailField(t("detail.recommendation_status"), value(recommendation.status)),
    detailField(t("detail.recommended_next_mission"), value(recommendation.title)),
    detailField(t("detail.mission_origin"), value(recommendation.mission_origin)),
    detailField(t("detail.business_value"), value(recommendation.business_value)),
    detailField(t("detail.confidence"), value(recommendation.confidence)),
    detailField(t("detail.dependencies"), Array.isArray(recommendation.dependencies) && recommendation.dependencies.length ? recommendation.dependencies.join("\n") : t("detail.not_recorded"), true),
    detailField(t("detail.evidence"), value(recommendation.summary), true),
    detailField(t("detail.decision_evidence"), value(recommendation.decision_evidence), true),
    detailField(t("detail.artefact_path"), value(handoff.artifact_path), true),
    detailField(t("detail.business_decision_not_recorded"), "—"),
    detailField(t("detail.mission_not_created"), "—"),
  ];
  if (handoff.projection_status === "INCOMPLETE")
    fields.unshift(detailField(t("detail.projection_incomplete"), missing.join("\n") || t("detail.not_recorded"), true));
  if (alternatives.length) {
    const expansion = document.createElement("details"), summary = document.createElement("summary"), list = document.createElement("ol");
    expansion.className = "recommendation-alternatives";
    summary.textContent = t("detail.alternatives");
    alternatives.forEach((alternative) => {
      const item = document.createElement("li");
      item.textContent = [alternative.rank, alternative.title, alternative.ordering_reason].filter(Boolean).join(" · ");
      list.append(item);
    });
    expansion.append(summary, list);
    fields.push(expansion);
  }
  return promptDetailCard(t("detail.recommendation_handoff"), fields, true);
}
function promptDetailReviewersSection(reviewers, { wide = true } = {}) {
  if (!reviewers.length) return null;
  const fields = reviewers.map((reviewer) =>
    detailField(
      reviewerLabel(reviewer.reviewer, t("detail.specialist_review")),
      t("detail.capability") + ": " +
        reviewerCapabilityLabel(reviewer.capability || "ENGINEERING") + " · " +
        reviewerStatusLabel(reviewer.status || "completed") + " · " +
        t("detail.accepted_recommendations") + ": " +
        (Number(reviewer.accepted_recommendations) || 0) + "\n" +
        t("detail.selected_because") + ": " +
        String(reviewer.selected_because || t("detail.not_recorded")),
      true,
    ),
  );
  return promptDetailCard(t("detail.specialist_reviews"), fields, wide, "prompt-detail-card--reviewers");
}
function promptDetailProviderReviewSections(usage, reviewers, commitTimeline) {
  const usageCard = promptDetailUsageSection(usage);
  const reviewerCard = promptDetailReviewersSection(reviewers, { wide: false });
  const timelineCard = promptDetailCommitTimelineSection(commitTimeline);
  if (timelineCard && usageCard) {
    const pair = document.createElement("section");
    pair.className = "prompt-detail-provider-review";
    pair.append(usageCard, timelineCard);
    if (!reviewerCard) return pair;
    const stack = document.createElement("section");
    stack.className = "prompt-detail-provider-review-stack";
    stack.append(pair, reviewerCard);
    return stack;
  }
  if (timelineCard) return timelineCard;
  if (!usageCard) return reviewerCard;
  if (!reviewerCard) return usageCard;
  const pair = document.createElement("section");
  pair.className = "prompt-detail-provider-review";
  pair.append(usageCard, reviewerCard);
  return pair;
}
function renderPromptHistoryDetail(payload) {
  const content = $("promptHistoryDetailContent"),
    history = payload?.history || {},
    execution = payload?.execution || {},
    runtime = payload?.runtime || {},
    usage = payload?.usage || {},
    commits = payload?.commits || {},
    pullRequests = payload?.pull_requests || [],
    commitTimeline = payload?.commit_timeline || [],
    evidence = Array.isArray(payload?.evidence) ? payload.evidence : [],
    reviewers = Array.isArray(payload?.reviewers) ? payload.reviewers : [],
    activity = history.execution_activity_summary,
    recommendationHandoff = payload?.recommendation_handoff;
  const [executionSummary, executionContext] = promptDetailExecutionSections(history);
  const executionActivity = promptDetailExecutionActivitySection(activity);
  if (typeof history.title === "string" && history.title.trim())
    $("promptHistoryDetailTitle").textContent = history.title.trim();
  setPromptHistoryDetailDownloads(payload);
  content.replaceChildren();
  content.append(
    ...[
      promptDetailLeftbar([
        executionSummary,
        promptDetailSidebar([
          promptDetailDurationSection(execution),
          executionActivity,
          promptDetailRuntimeSection(runtime),
          promptDetailCommitsSection(commits),
          promptDetailEvidenceSection(evidence),
        ]),
      ]),
      promptDetailRightbar([executionContext, promptDetailPullRequestsSection(pullRequests)]),
      lifecycleFlow(payload?.lifecycle, { historical: true }),
      statusReconciliationCard(payload?.lifecycle?.recovery),
      promptDetailProviderReviewSections(usage, reviewers, commitTimeline),
      promptDetailRecommendationHandoff(recommendationHandoff),
    ].filter(Boolean),
  );
}
function closePromptHistoryDetail() {
  const modal = $("promptHistoryDetailModal");
  if (modal.open) modal.close();
}
function openPromptHistoryDetail(entry, { updateUrl = true } = {}) {
  if (!entry?.run_id) return;
  const runId = String(entry.run_id);
  if (updateUrl) updatePromptHistoryDetailUrl(runId);
  promptHistoryDetailRunId = runId;
  const modal = $("promptHistoryDetailModal"), content = $("promptHistoryDetailContent");
  const title = typeof entry.title === "string" ? entry.title.trim() : "";
  $("promptHistoryDetailTitle").textContent = title && title !== runId
    ? title
    : t("history.details_loading");
  $("promptHistoryDetailDescription").textContent = t("history.details_description");
  setPromptHistoryDetailDownloads(null);
  content.textContent = t("history.details_loading");
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
  fetch("/api/prompt-history/" + encodeURIComponent(runId) + "/details", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : Promise.reject())
    .then((payload) => {
      if (promptHistoryDetailRunId === runId && modal.open) renderPromptHistoryDetail(payload);
    })
    .catch(() => {
      if (promptHistoryDetailRunId === runId && modal.open)
        content.textContent = t("history.details_unavailable");
    });
}
function reconcilePromptHistoryDetailFromUrl() {
  const runId = promptHistoryDetailRunFromUrl();
  const entry = promptHistoryEntries.find((candidate) => String(candidate?.run_id || "") === runId);
  if (!runId) {
    if (!promptHistoryDetailRunId) return;
    promptHistoryDetailLocationSyncing = true;
    closePromptHistoryDetail();
    promptHistoryDetailLocationSyncing = false;
    return;
  }
  if (!entry) {
    if (runId) updatePromptHistoryDetailUrl("", "replace");
    promptHistoryDetailLocationSyncing = true;
    closePromptHistoryDetail();
    promptHistoryDetailLocationSyncing = false;
    return;
  }
  if (promptHistoryDetailRunId !== runId || !$("promptHistoryDetailModal").open)
    openPromptHistoryDetail(entry, { updateUrl: false });
}
$("promptHistoryDetailClose").addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  closePromptHistoryDetail();
});
$("promptHistoryDetailDownloadMarkdown").addEventListener("click", () => downloadPromptHistoryDetail("markdown"));
$("promptHistoryDetailDownloadJson").addEventListener("click", () => downloadPromptHistoryDetail("json"));
$("promptHistoryDetailModal").addEventListener("click", (event) => {
  if (event.target.closest?.("#promptHistoryDetailClose")) {
    event.preventDefault();
    event.stopPropagation();
    closePromptHistoryDetail();
    return;
  }
  if (event.target === $("promptHistoryDetailModal")) closePromptHistoryDetail();
});
$("promptHistoryDetailModal").addEventListener("close", () => {
  promptHistoryDetailRunId = "";
  setPromptHistoryDetailDownloads(null);
  if (!promptHistoryDetailLocationSyncing && promptHistoryDetailRunFromUrl())
    updatePromptHistoryDetailUrl("");
});
window.addEventListener("popstate", reconcilePromptHistoryDetailFromUrl);
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
$("promptHistoryReportRetry").addEventListener("click", () => {
  void retryPromptHistoryAnalysis();
});
function renderPredecessorRetry(x) {
  const blocked = Boolean(x && x.blocking_predecessor_run),
    button = $("predecessorRetry"),
    status = $("predecessorRetryStatus");
  button.hidden = !blocked;
  button.disabled = isActiveRun(x || {});
  button.textContent = t("recovery.action");
  if (!blocked) status.textContent = "";
}
function submitPredecessorRetry() {
  const button = $("predecessorRetry"),
    status = $("predecessorRetryStatus"),
    run = latestStatus?.blocking_predecessor_run;
  if (!run || button.disabled) return;
  confirmDashboardAction(
    t("recovery.title"),
    t("recovery.details"),
    t("recovery.action"),
  ).then((confirmed) => {
    if (!confirmed) return;
    button.disabled = true;
    status.textContent = t("recovery.preparing");
    fetch("/api/execution-retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: run }),
    })
      .then(async (response) => ({
        ok: response.ok,
        body: await response.json(),
      }))
      .then((result) => {
        if (!result.ok)
          throw Error(
            result.body.error || t("recovery.failed"),
          );
        status.textContent =
          t("recovery.ready");
      })
      .catch((error) => {
        status.textContent =
          error.message || t("recovery.failed");
      })
      .finally(() => {
        button.disabled = false;
      });
  });
}
function submitManagedBranchRecovery() {
  confirmDashboardAction(
    t("queue.managed_branch_recovery_title"),
    t("queue.managed_branch_recovery"),
    t("queue.managed_branch_recovery_action"),
  ).then((confirmed) => {
    if (!confirmed) return;
    const blocker = $("inboxBlocker"), button = blocker.querySelector(".queue-blocker__repair");
    if (button) button.disabled = true;
    fetch("/api/managed-branch-recovery", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("queue.managed_branch_recovery_failed"));
        blocker.textContent = t("queue.managed_branch_recovery_ready");
        blocker.classList.remove("queue-blocker--error");
      })
      .catch((error) => {
        if (button) button.disabled = false;
        blocker.textContent = error.message || t("queue.managed_branch_recovery_failed");
      });
  });
}
function submitStaleGitLockRecovery() {
  confirmDashboardAction(
    t("technical.git_lock_recovery_title"),
    t("technical.git_lock_recovery"),
    t("technical.git_lock_recovery_action"),
  ).then((confirmed) => {
    if (!confirmed) return;
    const button = $("technicalGitLockRecover"), status = $("technicalGitLockRecoveryStatus");
    button.disabled = true;
    fetch("/api/stale-git-lock-recovery", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("technical.git_lock_recovery_failed"));
        renderWorkspaceGitLock({ state: "free", active: false, stale: false });
        status.textContent = t("technical.git_lock_recovery_ready");
      })
      .catch((error) => {
        status.textContent = error.message || t("technical.git_lock_recovery_failed");
        button.disabled = false;
      });
  });
}
function showWorkspaceBranchCleanupResult(outcome) {
  const modal = $("workspaceBranchCleanupResultModal"), content = $("workspaceBranchCleanupResultContent"),
    close = $("workspaceBranchCleanupResultClose"), dismiss = $("workspaceBranchCleanupResultDismiss"),
    removed = Array.isArray(outcome?.removed) ? outcome.removed.map(String) : [];
  modal.style.setProperty("--modal-parent-accent", workspaceModalAccent());
  content.replaceChildren(Object.assign(document.createElement("p"), {
    textContent: removed.length
      ? t("workspace.branch_cleanup_result_removed", { count: removed.length })
      : t("workspace.branch_cleanup_result_empty"),
  }));
  if (removed.length) {
    const branches = document.createElement("ul");
    branches.className = "workspace-branch-cleanup__result-list";
    for (const branch of removed) branches.append(Object.assign(document.createElement("li"), { textContent: branch }));
    content.append(branches);
  }
  const finish = () => {
    if (modal.open) modal.close();
    modal.style.removeProperty("--modal-parent-accent");
    close.onclick = dismiss.onclick = null;
  };
  close.onclick = dismiss.onclick = finish;
  modal.addEventListener("cancel", (event) => { event.preventDefault(); finish(); }, { once: true });
  modal.showModal();
  resetDashboardModalInitialFocus(modal);
}
function workspaceModalAccent() {
  return getComputedStyle($("workspaceCard")).getPropertyValue("--category-color").trim() || "#f3d36a";
}
function showWorkspaceBranchMainResult(message, titleKey = "workspace.branch_main_result_title") {
  const modal = $("workspaceBranchMainResultModal"), content = $("workspaceBranchMainResultContent"),
    close = $("workspaceBranchMainResultClose"), dismiss = $("workspaceBranchMainResultDismiss");
  $("workspaceBranchMainResultTitle").textContent = t(titleKey);
  modal.style.setProperty("--modal-parent-accent", workspaceModalAccent());
  content.replaceChildren(Object.assign(document.createElement("p"), { textContent: message }));
  const finish = () => {
    if (modal.open) modal.close();
    modal.style.removeProperty("--modal-parent-accent");
    close.onclick = dismiss.onclick = null;
  };
  close.onclick = dismiss.onclick = finish;
  modal.addEventListener("cancel", (event) => { event.preventDefault(); finish(); }, { once: true });
  modal.showModal();
  resetDashboardModalInitialFocus(modal);
}
async function switchToFastForwardMain() {
  const button = $("workspaceBranchMain");
  if (!button || button.disabled) return;
  const confirmed = await confirmDashboardAction(
    t("workspace.branch_main_title"), t("workspace.branch_main_confirmation"), t("workspace.branch_main_confirm_action"),
    { accent: workspaceModalAccent() },
  );
  if (!confirmed) return;
  let switchScheduled = false;
  button.disabled = true;
  try {
    const response = await fetch("/api/workspace-switch-to-main", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const outcome = await response.json();
    if (response.ok) {
      switchScheduled = true;
      workspaceMainSwitchScheduled = true;
      // The old page remains usable for a moment while the controlled restart
      // replaces it. Hide immediately, rather than waiting for a new status
      // snapshot that may still describe the branch we just left.
      button.hidden = true;
    }
    showWorkspaceBranchMainResult(response.ok
      ? t("workspace.branch_main_success", { previous_branch: outcome.previous_branch, branch: outcome.branch })
      : outcome.error || t("workspace.branch_main_failed"));
  } catch (error) {
    showWorkspaceBranchMainResult(error.message || t("workspace.branch_main_failed"));
  } finally {
    if (!switchScheduled) button.disabled = false;
  }
}
async function switchEngineeringPlatformToWorktree(worktree) {
  const path = String(worktree?.path || ""), branch = String(worktree?.branch || "");
  if (!path || !branch || branch === "main") return;
  const confirmed = await confirmDashboardAction(
    t("workspace.worktree_switch_title"),
    t("workspace.worktree_switch_confirmation", { branch, path }),
    t("workspace.worktree_switch_action"),
    { accent: workspaceModalAccent() },
  );
  if (!confirmed) return;
  try {
    const response = await fetch("/api/workspace-switch-to-worktree", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ worktree_path: path, branch }),
    });
    const outcome = await response.json();
    showWorkspaceBranchMainResult(
      response.ok
        ? t("workspace.worktree_switch_scheduled", { branch })
        : outcome?.error || t("workspace.worktree_switch_failed"),
      "workspace.worktree_switch_result_title",
    );
  } catch (error) {
    showWorkspaceBranchMainResult(
      error.message || t("workspace.worktree_switch_failed"),
      "workspace.worktree_switch_result_title",
    );
  }
}
async function cleanupStaleLocalBranches() {
  const button = $("workspaceBranchCleanup");
  if (!button || button.disabled) return;
  button.disabled = true;
  const confirmation = confirmDashboardAction(
    t("workspace.branch_cleanup_title"),
    t("workspace.branch_cleanup_scanning"),
    t("workspace.branch_cleanup_confirm_action"),
    { destructive: true, accent: workspaceModalAccent(), loading: true },
  );
  const modal = $("confirmationModal"), body = $("confirmationModalText"), confirm = $("confirmationModalConfirm");
  try {
    const previewResponse = await fetch("/api/stale-local-branch-cleanup-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const preview = await previewResponse.json();
    if (!previewResponse.ok) throw Error(preview.error || t("workspace.branch_cleanup_failed"));
    const branches = Array.isArray(preview?.branches) ? preview.branches : [];
    const removableBranches = Array.isArray(preview?.removable_branches)
      ? preview.removable_branches.map(String).filter(Boolean)
      : branches.filter((branch) => branch?.removable === true).map((branch) => String(branch?.name || "")).filter(Boolean);
    if (!branches.length) {
      if (modal.open) {
        body.replaceChildren(Object.assign(document.createElement("p"), {
          textContent: t("workspace.branch_cleanup_empty_in_modal"),
        }));
        confirm.textContent = t("action.close");
        confirm.disabled = false;
        $("confirmationModalCancel").hidden = true;
        confirm.classList.remove("dashboard-modal-shell__action--destructive");
        confirm.classList.add("dashboard-modal-shell__action--primary");
      }
      await confirmation;
      return;
    }
    if (!modal.open) {
      await confirmation;
      return;
    }
    const details = branches.map((branch) => ({
      name: String(branch?.name || ""),
      reason: t("workspace.branch_cleanup_reason." + String(branch?.reason || "")),
      pull_request: branch?.pull_request,
    }));
    body.replaceChildren(
      Object.assign(document.createElement("p"), {
        textContent: removableBranches.length
          ? t("workspace.branch_cleanup_confirmation")
          : t("workspace.branch_cleanup_no_safe_in_modal"),
      }),
      workspaceBranchCleanupDetails(details),
    );
    if (!removableBranches.length) {
      confirm.textContent = t("action.close");
      confirm.disabled = false;
      $("confirmationModalCancel").hidden = true;
      confirm.classList.remove("dashboard-modal-shell__action--destructive");
      confirm.classList.add("dashboard-modal-shell__action--primary");
      await confirmation;
      return;
    }
    confirm.disabled = false;
    const confirmed = await confirmation;
    if (!confirmed) return;
    const response = await fetch("/api/stale-local-branch-cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branches: removableBranches }),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || t("workspace.branch_cleanup_failed"));
    showWorkspaceBranchCleanupResult(result);
  } catch (error) {
    if (modal.open) modal.close();
    showDashboardError(error.message, t("workspace.branch_cleanup_failed"));
  } finally {
    button.disabled = false;
  }
}
async function removeSafeWorktree(worktree, analysis = null) {
  const path = String(worktree?.path || ""), branch = String(worktree?.branch || ""), head = String(analysis?.head || "");
  if (!path || (!branch && !head)) return;
  const target = branch || `${t("workspace.detached_head")} ${head.slice(0, 12)}`;
  const confirmed = await confirmDashboardAction(
    t("workspace.worktree_remove_title"),
    t("workspace.worktree_remove_confirmation", { branch: target, path }),
    t("workspace.worktree_remove_action"),
    { destructive: true },
  );
  if (!confirmed) return;
  try {
    const response = await fetch("/api/safe-worktree-removal", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(branch ? { worktree_path: path, branch } : { worktree_path: path, head }),
    });
    const outcome = await response.json();
    if (!response.ok) throw Error(outcome.error || t("workspace.worktree_remove_failed"));
    showWorkspaceBranchMainResult(
      t("workspace.worktree_remove_success", { branch: target }),
      "workspace.worktree_remove_result_title",
    );
    void refreshAfterOperatorAction();
  } catch (error) {
    showDashboardError(error.message || t("workspace.worktree_remove_failed"), t("workspace.worktree_remove_failed"));
  }
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
      .catch((error) => showDashboardError(error.message, t("retry.failed")));
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
      .catch((error) => showDashboardError(error.message, t("dismiss.failed")));
  });
}
function abortOperatorMergeWait() {
  const runId = latestStatus?.run_id;
  if (!runId) return;
  if ($("operatorMergeWaitModal").open) $("operatorMergeWaitModal").close();
  confirmDashboardAction(
    t("merge_wait.abort_title"),
    t("merge_wait.abort_description", { run_id: runId }),
    t("action.abort_execution"),
    { destructive: true },
  ).then((confirmed) => {
    if (!confirmed) return;
    fetch("/api/execution-merge-wait-abort", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId }) })
      .then(async (response) => ({ ok: response.ok, body: await response.json() }))
      .then((result) => {
        if (!result.ok) throw Error(result.body.error || t("merge_wait.abort_failed"));
        return refreshAfterOperatorAction({ dismissedRunId: runId });
      })
      .catch((error) => showDashboardError(error.message, t("merge_wait.abort_failed")));
  });
}
function checkOperatorMergeStatus(button) {
  const runId = latestStatus?.run_id;
  if (!runId) return;
  button.disabled = true;
  fetch("/api/execution-merge-status-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId }) })
    .then(async (response) => ({ ok: response.ok, body: await response.json() }))
    .then((result) => {
      const reason = String(result.body?.reason || "github_evidence_unavailable");
      if (result.body?.verified) {
        if ($("operatorMergeWaitModal").open) $("operatorMergeWaitModal").close();
        showDashboardToast(t("merge_wait.continuation_scheduled"));
        return refreshMergeContinuation();
      }
      // Re-read the authoritative snapshot before retaining a failure modal:
      // a background watcher can advance the same hand-off while this request
      // is in flight. The control remains available when it is still waiting.
      return refreshAfterOperatorAction().finally(() => {
        if (latestStatus?.current_phase === "WAIT_FOR_OPERATOR_MERGE") {
          showMergeStatusCheckError(reason);
        }
      });
    })
    .catch(() => showMergeStatusCheckError("github_cli_unavailable"))
    .finally(() => { button.disabled = false; });
}
async function refreshMergeContinuation() {
  for (const delay of [0, 300, 900, 1800]) {
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    await refreshAfterOperatorAction();
    if (latestStatus?.current_phase !== "WAIT_FOR_OPERATOR_MERGE") return;
  }
}
function showMergeStatusCheckError(reason) {
  const lastCheck = latestStatus?.merge_status_check?.last_successful_github_check_at;
  const timestamp = typeof lastCheck === "string" ? formatTimestamp(lastCheck) : "";
  const detail = [
    t(`merge_wait.reason.${reason}`),
    timestamp ? t("merge_wait.last_successful_check", { timestamp }) : "",
  ].filter(Boolean).join(" ");
  showDashboardError(detail, t("merge_wait.status_check_failed"), {
    label: t("merge_wait.check_again"),
    run: () => checkOperatorMergeStatus($("operatorMergeWaitModalStatusCheck")),
  });
}
$("predecessorRetry").addEventListener("click", submitPredecessorRetry);
$("operatorMergeAbort").addEventListener("click", abortOperatorMergeWait);
$("emergencyRecoveryStart")?.addEventListener("click", startEmergencyRecovery);
$("operatorMergeWaitModalAbort").addEventListener("click", abortOperatorMergeWait);
$("operatorMergeStatusCheck").addEventListener("click", (event) => checkOperatorMergeStatus(event.currentTarget));
$("operatorMergeWaitModalStatusCheck").addEventListener("click", (event) => checkOperatorMergeStatus(event.currentTarget));
$("statusReconciliationStart")?.addEventListener("click", requestStatusReconciliation);
$("workspaceBranchCleanup")?.addEventListener("click", cleanupStaleLocalBranches);
$("workspaceBranchMain")?.addEventListener("click", switchToFastForwardMain);
function workspaceBranchCleanupDetails(details) {
  const list = document.createElement("ul");
  list.className = "workspace-branch-cleanup__preview-list";
  for (const detail of details) {
    const item = document.createElement("li");
    item.append(
      Object.assign(document.createElement("code"), { textContent: detail.name }),
      Object.assign(document.createElement("span"), { textContent: detail.reason }),
    );
    if (detail.pull_request?.url && Number.isInteger(detail.pull_request.number)) {
      item.append(Object.assign(document.createElement("a"), {
        href: detail.pull_request.url,
        rel: "noreferrer",
        target: "_blank",
        textContent: `${t("workspace.branch_cleanup_pr_link", { number: detail.pull_request.number })} ↗`,
      }));
    }
    list.append(item);
  }
  return list;
}
function confirmDashboardAction(title, text, confirmLabel, { destructive = false, accent = "", details = [], items = [], itemsLabel = "", loading = false, variant = "" } = {}) {
  const modal = $("confirmationModal"),
    heading = $("confirmationModalTitle"),
    body = $("confirmationModalText"),
    close = $("confirmationModalClose"),
    cancel = $("confirmationModalCancel"),
    confirm = $("confirmationModalConfirm");
  heading.textContent = title;
  heading.dataset.modalGlyph = destructive ? "warning" : "question";
  body.replaceChildren(Object.assign(document.createElement("p"), { textContent: text }));
  if (loading) body.append(Object.assign(document.createElement("span"), {
    className: "workspace-branch-cleanup__spinner",
    role: "status",
    ariaLabel: text,
  }));
  if (details.length) {
    body.append(workspaceBranchCleanupDetails(details));
  }
  if (items.length) {
    const label = document.createElement("strong"), list = document.createElement("ul");
    label.className = "confirmation-modal__list-label";
    label.textContent = itemsLabel;
    list.className = "confirmation-modal__list";
    for (const item of items) list.append(Object.assign(document.createElement("li"), { textContent: item }));
    body.append(label, list);
  }
  confirm.textContent = confirmLabel;
  confirm.disabled = loading;
  cancel.hidden = false;
  confirm.classList.toggle("dashboard-modal-shell__action--primary", !destructive);
  confirm.classList.toggle("dashboard-modal-shell__action--destructive", destructive);
  modal.classList.toggle("dashboard-modal-shell--destructive", destructive);
  modal.classList.toggle("dashboard-modal-shell--owner-authorization", variant === "owner-authorization");
  modal.classList.toggle("dashboard-modal-shell--check-repair", variant === "check-repair");
  modal.style.setProperty("--modal-accent", accent || (destructive ? "#ff718f" : "#f0b66a"));
  return new Promise((resolve) => {
    const finish = (value) => {
      modal.close();
      modal.classList.remove("dashboard-modal-shell--destructive");
      modal.classList.remove("dashboard-modal-shell--owner-authorization");
      modal.classList.remove("dashboard-modal-shell--check-repair");
      confirm.classList.add("dashboard-modal-shell__action--primary");
      confirm.classList.remove("dashboard-modal-shell__action--destructive");
      confirm.disabled = false;
      cancel.hidden = false;
      delete heading.dataset.modalGlyph;
      close.onclick = cancel.onclick = confirm.onclick = null;
      resolve(value);
    };
    close.onclick = cancel.onclick = () => finish(false);
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
    resetDashboardModalInitialFocus(modal);
  });
}
function localizedDashboardError(message, fallback) {
  const raw = String(message || fallback || "").trim();
  const preflight = raw.match(/^Preflight (?:mislukt|failed):\s*(.*?)\s+(?:Herstel|Recovery):\s*(.*)$/iu);
  if (!preflight) return raw || t("ui.action_failed");
  const [, reason, recovery] = preflight;
  if (/^Untracked files are present\.?$/iu.test(reason))
    return t("preflight.untracked", {
      reason: t("preflight.untracked_reason"),
      recovery: t("preflight.untracked_recovery"),
    });
  if (/^Unstaged changes are present\.?$/iu.test(reason))
    return t("preflight.unstaged", {
      reason: t("preflight.unstaged_reason"),
      recovery: t("preflight.unstaged_recovery"),
    });
  const branch = reason.match(/^Managed target is not on the expected branch ([^.]+)\.?$/iu);
  if (branch)
    return t("preflight.branch", {
      branch: branch[1],
      reason: t("preflight.branch_reason", { branch: branch[1] }),
      recovery: t("preflight.branch_recovery", { branch: branch[1] }),
    });
  if (/^Managed target is not synchronized with its upstream\.?$/iu.test(reason))
    return t("preflight.sync", {
      reason: t("preflight.sync_reason"),
      recovery: t("preflight.sync_recovery"),
    });
  return t("preflight.generic", { reason, recovery });
}
function dashboardErrorRecovery(message) {
  const raw = String(message || "").trim();
  return /^Preflight (?:mislukt|failed):\s*Managed target is not synchronized with its upstream\.?\s+(?:Herstel|Recovery):/iu.test(raw)
    ? "managed_branch_synchronization"
    : null;
}
function showDashboardError(message, fallback, action = null) {
  const modal = $("dashboardErrorModal"),
    close = $("dashboardErrorModalClose"),
    dismiss = $("dashboardErrorModalDismiss"),
    recover = $("dashboardErrorModalRecover"),
    recovery = dashboardErrorRecovery(message),
    followUp = action || (recovery ? { label: t("action.recover") } : null);
  $("dashboardErrorModalTitle").textContent = t("ui.action_failed");
  $("dashboardErrorModalText").textContent = localizedDashboardError(message, fallback);
  recover.hidden = !followUp;
  recover.disabled = false;
  recover.textContent = followUp?.label || "";
  const finish = () => {
    if (modal.open) modal.close();
    close.onclick = dismiss.onclick = recover.onclick = null;
  };
  close.onclick = dismiss.onclick = finish;
  recover.onclick = action
    ? () => { finish(); action.run(); }
    : recovery === "managed_branch_synchronization"
    ? () => {
      recover.disabled = true;
      fetch("/api/managed-branch-synchronization", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then(async (response) => ({ ok: response.ok, body: await response.json() }))
        .then((result) => {
          if (!result.ok) throw Error(result.body.error || t("preflight.sync_failed"));
          finish();
          return refreshAfterOperatorAction();
        })
        .catch(() => {
          $("dashboardErrorModalText").textContent = t("preflight.sync_failed");
          recover.disabled = false;
        });
    }
    : null;
  modal.addEventListener("cancel", (event) => {
    event.preventDefault();
    finish();
  }, { once: true });
  if (!modal.open) modal.showModal();
  resetDashboardModalInitialFocus(modal);
}
function updateChatActions() {
  const visible = chatHistory.length > 0;
  $("downloadChat").hidden = !visible;
  $("copyChat").hidden = !visible;
  $("clearChat").hidden = !visible;
}
$("copyChat").addEventListener("click", () => {
  if (!chatHistory.length) return;
  copyText(chatHistoryMarkdown()).catch(() => {
    $("chatStatus").textContent = t("copy.failed");
  });
});
$("clearChat").addEventListener("click", () =>
  confirmDashboardAction(
    t("chat.clear_title"),
    t("chat.clear_description"),
    t("chat.clear_title"),
    { destructive: true },
  ).then((confirmed) => {
    if (!confirmed) return;
    if (!chatContextRun) return;
    $("clearChat").disabled = true;
    fetch("/api/codex-chat/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: chatContextRun }),
    })
      .then((response) => {
        if (!response.ok) throw Error("clear failed");
        chatHistory = [];
        renderChatHistory();
        updateChatHistoryCount(0);
      })
      .catch(() => { $("chatStatus").textContent = t("chat.unavailable"); })
      .finally(() => { $("clearChat").disabled = false; updateChatActions(); });
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
  chatMessageTimestampLabel,
  capabilityRecommendation,
  enumLabel,
  executionTelemetry,
  formatTimestamp,
  logEventLabel,
  queueItems,
  r,
  rateLimits,
  refreshDashboard,
  refreshOpenComponentDetails,
  renderChatHistory,
  renderComponentLogs,
  renderLogPagination,
  renderMarkdownAnswer,
  renderPromptHistoryDetail,
  renderPlatformHealth,
  renderPromptHistory,
  refreshOpenPullRequests,
  renderOpenPullRequests,
  renderWorkspaceWorktrees,
  scheduleOpenPullRequestMonitor,
  showComponentModal,
  showDashboardError,
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
  [
    "componentLogServerPaged",
    () => componentLogServerPaged,
    (value) => (componentLogServerPaged = Boolean(value)),
  ],
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

// Start after every DOM-dependent dashboard feature has completed setup.
localizeOpenPullRequestStatuses();
void refreshOpenPullRequests();
void refreshGithubRateLimit();
startDashboardUpdates();
