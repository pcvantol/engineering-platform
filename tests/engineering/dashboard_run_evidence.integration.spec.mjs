import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { test, expect } from "./dashboard_coverage_fixture.mjs";
import { chromium } from "@playwright/test";
import { observeConsoleDocument } from "./dashboard_document_observer.mjs";
import { retainWorkerLogs, finishWorkerLogs } from "./dashboard_worker_logs.mjs";
import { DASHBOARD_MESSAGES } from "../../src/engineering_platform/assets/dashboard_locales.mjs";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const fixture = path.join(repository, "tests/engineering/dashboard_run_evidence_fixture.py");
// Every scenario owns its separate synthetic store/repository. Explicit
// parallel grouping lets the existing four shards balance these independent
// cases while keeping one worker per shard and all existing deadlines.
test.describe.configure({ mode: "parallel" });

async function passiveProcess(python, environment, dataRoot) {
  const child = spawn(python, [fixture, "--read", dataRoot], { cwd: path.dirname(python), env: environment, stdio: ["pipe", "pipe", "pipe"] });
  retainWorkerLogs(child, test.info(), "passive-reader");
  let buffer = "", pending; const queued = [];
  child.stdout.on("data", (data) => {
    buffer += data;
    while (buffer.includes("\n")) {
      const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (line.startsWith("{")) { const value = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(value); } else queued.push(value); }
    }
  });
  const next = () => queued.length ? Promise.resolve(queued.shift()) : new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Passive reader timeout: raw worker logs retained privately`)), 15_000);
    pending = (value) => { clearTimeout(timer); resolve(value); };
    child.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Passive reader exited ${code}: raw worker logs retained privately`)); });
  });
  return { child, next, ready: await next() };
}

async function stopIsolatedCanary(child, next, testInfo, label = "canary") {
  let final, workerLogs;
  try {
    if (child.exitCode !== null) throw new Error(`Isolated ${label} exited before verified cleanup: ${child.exitCode}`);
    const stopped = new Promise((resolve) => child.once("exit", resolve));
    child.stdin.write("stop\n");
    final = await next();
    expect(final.closed).toEqual(expect.objectContaining({ worker_ended: true, owned_process_group_ended: true, request_admission_closed: true,
      ordinary_handlers_completed: true, isolation_retained_through_worker_exit: true, unexpected_native_attempts: 0 }));
    expect(final.closed.transport_observations.passive_effects).toEqual({ model: 0, git: 0 });
    await stopped;
    expect(child.exitCode).toBe(0);
  } finally {
    // Preserve and inspect raw errors after the last pipe closes, even if a
    // cleanup assertion failed. An earlier ordinary receipt cannot cover SSE.
    workerLogs = await finishWorkerLogs(child, testInfo);
  }
  const receipt = { contract: "ep-console-read-worker-cleanup/v1", source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null,
    source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null, wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null,
    label, cleanup: final.closed, worker_logs: workerLogs };
  const output = testInfo.outputPath(`${label}-final-cleanup.json`);
  writeFileSync(output, JSON.stringify(receipt, null, 2));
  await testInfo.attach(`${label}-final-cleanup`, { path: output, contentType: "application/json" });
  const manifest = testInfo.outputPath("fresh-evidence-manifest.json");
  try {
    const data = JSON.parse(readFileSync(manifest));
    data.final_cleanup = final.closed;
    data.worker_logs = workerLogs;
    writeFileSync(manifest, JSON.stringify(data, null, 2));
    await testInfo.attach("fresh-evidence-manifest-closed", { path: manifest, contentType: "application/json" });
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

const cases = ["prepared", "uncertain", "no-consumer", "no-capacity", "irrelevant", "dispositions", "withdrawn", "provider-recovered", "provider-blocked", "missing", "privacy", "failed", "uncertain-result", "duplicate", "read-revoked"]
  .map((scenario) => ({ scenario, locale: "en", theme: "dark", width: 1280, height: 720 }));
for (const locale of ["en", "nl", "de", "fr", "es"]) for (const theme of ["dark", "light"]) {
  for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
    cases.push({ scenario: "normal", locale, theme, ...viewport });
  }
}
for (const { scenario, locale, theme, width, height } of cases) {
test(`reads actual ${scenario} ${locale}/${theme}/${width} dispatcher evidence without execution effects`, async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const environment = { ...process.env, PYTHONPATH: path.join(repository, "src"), EP_QUALIFICATION_DETERMINISTIC_FLOW: "1" };
  const installedPython = process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON;
  if (installedPython) delete environment.PYTHONPATH;
  const child = spawn(installedPython || process.env.EP_BROWSER_PYTHON || "python3", [fixture, scenario], {
    cwd: installedPython ? path.dirname(installedPython) : repository, env: environment, stdio: ["pipe", "pipe", "pipe"],
  });
  retainWorkerLogs(child, testInfo, "canary");
  let buffer = "", pending;
  const messages = [];
  child.stdout.on("data", (data) => {
    buffer += data;
    while (buffer.includes("\n")) {
      const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (line.startsWith("{")) { const message = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(message); } else messages.push(message); }
    }
  });
  const next = () => messages.length ? Promise.resolve(messages.shift()) : new Promise((resolve, reject) => {
    const deadline = setTimeout(() => { pending = null; reject(new Error(`Canary timed out: raw worker logs retained privately`)); }, 30_000);
    pending = (message) => { clearTimeout(deadline); resolve(message); };
    child.once("exit", (code) => { clearTimeout(deadline); reject(new Error(`Canary exited ${code}: raw worker logs retained privately`)); });
  });
  try {
    const ready = await next();
    if (installedPython) expect(ready.module).toContain("site-packages/engineering_platform/");
    if (process.env.EP_RUN_EVIDENCE_REQUIRE_INSTALLED === "1") {
      expect(installedPython).toBeTruthy();
      expect(process.env.EP_RUN_EVIDENCE_SOURCE_SHA).toMatch(/^[0-9a-f]{40}$/);
      expect(process.env.EP_RUN_EVIDENCE_SOURCE_TREE).toMatch(/^[0-9a-f]{40}$/);
      expect(process.env.EP_RUN_EVIDENCE_WHEEL_SHA256).toMatch(/^[0-9a-f]{64}$/);
    }
    await page.setViewportSize({ width, height });
    await page.goto(ready.url, { waitUntil: "domcontentloaded" });
    for (const [name, expected] of Object.entries(ready.assets)) {
      const asset = await page.request.get(`${ready.url.split("/?")[0]}/assets/${name}`);
      expect(asset.ok()).toBe(true);
      expect(createHash("sha256").update(await asset.body()).digest("hex")).toBe(expected);
    }
    if ((locale !== "en" || theme !== "dark") && !await page.locator("#dashboardLocaleButton").isVisible()) {
      await page.locator("#dashboardTitlebarOptionsToggle").click();
    }
    if (locale !== "en") {
      await page.locator("#dashboardLocaleButton").click();
      await page.locator(`#dashboardLocaleMenu [data-dashboard-locale="${locale}"]`).click();
    }
    if (await page.locator("html").getAttribute("data-theme") !== theme) await page.locator("#themeToggle").click();
    if (await page.locator("#dashboardTitlebarOptionsToggle").isVisible()
        && await page.locator("#dashboardTitlebarOptionsToggle").getAttribute("aria-expanded") === "true") {
      await page.locator("#dashboardTitlebarOptionsToggle").click();
    }
    const openStoredHistory = async () => {
      if (await page.locator("#promptHistory").getAttribute("open") === null) await page.locator("#promptHistory > summary").click();
      await expect(page.locator("#promptHistoryRows .prompt-history-row")).toHaveCount(1);
      await page.locator("#promptHistoryRows .prompt-history-row").press("Enter");
      await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    };
    if (["provider-blocked", "missing", "uncertain-result", "failed"].includes(scenario)) await openStoredHistory();
    // Operator-wait is still an active run, not a fabricated terminal history.
    await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
    await expect(page.locator('[data-run-evidence="selection"] h3')).toHaveText(DASHBOARD_MESSAGES[locale]["run_evidence.specialists"]);
    const actualDetail = await page.request.get(`${ready.url.split("/?")[0]}/api/prompt-history/${ready.run_id}/details?project=project`);
    expect(actualDetail.ok()).toBe(true);
    const stored = await actualDetail.json();
    const noSpecialists = ["no-consumer", "no-capacity", "irrelevant", "missing"].includes(scenario);
    const runEvidence = stored.lifecycle.run_evidence;
    if (scenario === "privacy") {
      expect(JSON.stringify(runEvidence)).not.toContain("/Users/qualification/local.txt");
      expect(JSON.stringify(runEvidence)).not.toContain("/Users/qualification/private-note.txt");
      expect(JSON.stringify(runEvidence)).not.toContain("/Users/qualification/label-note.txt");
      expect(runEvidence.presentation_redacted).toBe(true);
      await expect(page.locator('[data-run-evidence="selection"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.host_paths_hidden"]);
      await expect(page.locator('[data-run-evidence="findings"]')).not.toContainText("/Users/qualification/local.txt");
      await expect(page.locator("[data-run-evidence] script")).toHaveCount(0);
      await expect(page.locator('[data-run-evidence="findings"]')).toContainText("<script>alert(1)</script>");
    }
    if (scenario === "missing") {
      expect(stored.lifecycle.available).toBe(false);
      expect(runEvidence).toBeUndefined();
      await expect(page.locator('[data-run-evidence="selection"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.not_recorded"]);
    } else {
    expect(runEvidence.run_id).toBe(ready.run_id);
    expect(runEvidence.specialists.actual_model_invocation_count).toBe(noSpecialists ? 0 : scenario === "uncertain-result" ? null : 2);
    expect(runEvidence.publication.status).toBe(
      ({ prepared: "PREPARED", uncertain: "CREATE_UNCERTAIN", "provider-blocked": "NOT_RECORDED", "uncertain-result": "NOT_RECORDED", failed: "NOT_RECORDED" })[scenario] || "RECONCILED",
    );
    }
    if (scenario === "withdrawn") {
      expect(stored.lifecycle.run_evidence.current_repository_binding).toBe("UNBOUND");
      expect(stored.lifecycle.run_evidence.execution_authority).toBe(false);
      await expect(page.locator('[data-run-evidence="publication"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.binding_unbound"]);
    }
    if (scenario === "provider-recovered") {
      expect(stored.lifecycle.recovery.kind).toBe("provider_interruption");
      expect(stored.lifecycle.recovery.state).toBe("RECOVERED");
      expect(stored.lifecycle.recovery.maximum_attempts).toBe(1);
      expect(stored.lifecycle.recovery.triggering_invocation_id).not.toBe(stored.lifecycle.recovery.replacement_invocation_id);
      expect(stored.lifecycle.recovery.last_observed_at).toBeTruthy();
    }
    if (scenario === "provider-blocked") {
      expect(ready.before.generation_model_requests).toBe(1);
      expect(stored.lifecycle.recovery.kind).toBe("provider_interruption");
      expect(stored.lifecycle.recovery.maximum_attempts).toBe(1);
      expect(stored.lifecycle.recovery.state).toBe("EXHAUSTED");
      expect(stored.lifecycle.run_evidence.specialists.findings.every((item) => item.disposition === "PROPOSED")).toBe(true);
    }
    if (["provider-blocked", "missing", "uncertain-result", "failed"].includes(scenario)) {
      const markdownEvent = page.waitForEvent("download");
      await page.locator("#promptHistoryDetailDownloadMarkdown").click();
      const markdown = await markdownEvent;
      const text = readFileSync(await markdown.path(), "utf8");
      expect(text).toContain(ready.run_id);
      expect(text).toContain(DASHBOARD_MESSAGES[locale]["run_evidence.specialists"]);
      const jsonEvent = page.waitForEvent("download");
      await page.locator("#promptHistoryDetailDownloadJson").click();
      const json = await jsonEvent;
      const exported = JSON.parse(readFileSync(await json.path(), "utf8"));
      expect(exported.history.run_id).toBe(ready.run_id);
      expect(exported.lifecycle.run_evidence).toEqual(runEvidence);
    }
    if (["failed", "uncertain-result"].includes(scenario)) {
      const expected = scenario === "failed" ? "FAILED" : "UNCERTAIN";
      expect(runEvidence.specialists.outcomes.every((item) => item.state === expected)).toBe(true);
      expect(runEvidence.specialists.failed_invocation_count).toBe(scenario === "failed" ? 2 : 0);
      expect(runEvidence.specialists.successful_invocation_count).toBe(0);
      await expect(page.locator('[data-run-evidence="selection"]')).toContainText(DASHBOARD_MESSAGES[locale][`run_evidence.state.${expected.toLowerCase()}`]);

      const event = page.waitForEvent("download");
      await page.locator("#promptHistoryDetailDownloadJson").click();
      const download = await event;
      expect(JSON.parse(readFileSync(await download.path(), "utf8")).lifecycle.run_evidence).toEqual(runEvidence);

    }
    if (scenario === "duplicate") {
      expect(runEvidence.specialists.duplicate_finding_count).toBe(1);
      expect(runEvidence.specialists.duplicate_observations).toBe(0);
      await expect(page.locator('[data-run-evidence="findings"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.state.duplicate"]);
      await page.locator('[data-run-evidence="findings"] select').selectOption("DUPLICATE");
      await expect(page.locator('details[data-disposition="DUPLICATE"]:visible')).toHaveCount(1);
      await expect(page.locator('details[data-disposition="VERIFIED"]:visible')).toHaveCount(0);
      await page.locator('[data-run-evidence="findings"] select').selectOption("ALL");
    }
    const findings = page.locator('[data-run-evidence="findings"]');
    await expect(findings.locator("details[data-disposition]")).toHaveCount(noSpecialists || ["failed", "uncertain-result"].includes(scenario) ? 0 : scenario === "dispositions" ? 5 : 2);
    if (scenario === "dispositions") {
      expect(stored.lifecycle.run_evidence.specialists.duplicate_observations).toBe(1);
      expect(new Set(stored.lifecycle.run_evidence.specialists.findings.map((item) => item.disposition)))
        .toEqual(new Set(["ACCEPTED", "REJECTED", "DEFERRED", "VERIFIED"]));
    }
    if (!noSpecialists && !["provider-blocked", "failed", "uncertain-result"].includes(scenario)) {
    await findings.locator('details[data-disposition="VERIFIED"] > summary').click();
    await expect(findings.locator('details[data-disposition="VERIFIED"]')).toContainText("README.md");
    await findings.locator("select").selectOption("VERIFIED");
    await expect(findings.locator('details[data-disposition="DEFERRED"]:visible')).toHaveCount(0);
    await findings.locator("select").selectOption("ALL");
    if (scenario !== "duplicate") await expect(findings.locator('details[data-disposition="DEFERRED"]').first()).toBeVisible();
    }
    const screenshots = [];
    const capture = async (view, suffix, target = page.locator(`[data-run-evidence="${view}"]`), evidence = runEvidence) => {
      const imagePath = testInfo.outputPath(`${view}-${suffix}-fresh.png`);
      await target.screenshot({ path: imagePath, animations: "disabled" });
      screenshots.push({ scenario: view, file: path.basename(imagePath),
        sha256: createHash("sha256").update(readFileSync(imagePath)).digest("hex"),
        run_scenario: testInfo.title, run_id: ready.run_id, finding_ids: (evidence?.specialists?.findings || []).map((item) => item.id),
        invocation_ids: (evidence?.specialists?.invocations || []).map((item) => item.invocation_id),
        publication: evidence?.publication || null,
        locale: await page.locator("#dashboardLocale").inputValue(),
        theme: await page.locator("html").getAttribute("data-theme"), viewport: page.viewportSize(),
      });
      await testInfo.attach(`${view}-${suffix}-fresh`, { path: imagePath, contentType: "image/png" });
    };
    for (const view of ["selection", "findings", "publication"]) {
      await capture(view, "desktop");
    }
    const verifiedFinding = runEvidence?.specialists.findings.find((item) => item.disposition === "VERIFIED");
    if (verifiedFinding) {
      const detail = findings.locator('details[data-disposition="VERIFIED"]');
      if (await detail.getAttribute("open") === null) await detail.locator("summary").click();
      const controls = detail.locator(".field").filter({ hasText: verifiedFinding.control_refs[0] });
      await controls.scrollIntoViewIfNeeded();
      await expect(controls).toBeVisible();
      await capture("verification-controls", "native", findings);
    }
    const rootUrl = ready.url.split("/?")[0];
    const foreign = await page.request.get(`${rootUrl}/api/prompt-history/${ready.run_id}/details?project=other`);
    expect(foreign.status()).toBe(404);
    await page.goto(`${rootUrl}/?project=other`, { waitUntil: "domcontentloaded" });
    await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
    for (const finding of runEvidence?.specialists.findings || []) {
      await expect(page.locator("body")).not.toContainText(finding.id);
    }
    await capture("denied-project", "no-data", page.locator("body"), null);
    await page.goto(ready.url, { waitUntil: "domcontentloaded" });
    if (["provider-blocked", "missing", "uncertain-result", "failed"].includes(scenario)) await openStoredHistory();
    await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
    await page.reload({ waitUntil: "domcontentloaded" });
    if (["provider-blocked", "uncertain-result", "failed"].includes(scenario)) {
      // Reload retains the genuine history deep link. Wait for its native
      // opening, then finish closing that session before testing a new one.
      await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
      await page.locator("#promptHistoryDetailClose").click();
      await expect(page).not.toHaveURL(/(?:\?|&)prompt=/);
      // Delay transport forwarding only; the eventual response is produced
      // by the real handler. No success payload or auth service is replaced.
      let releaseRead, readObserved;
      const heldRead = new Promise((resolve) => { releaseRead = resolve; });
      const observedRead = new Promise((resolve) => { readObserved = resolve; });
      const detailPattern = "**/api/prompt-history/*/details";
      await page.route(detailPattern, async (route) => { readObserved(); await heldRead; await route.continue(); });
      await openStoredHistory();
      await observedRead;
      await page.locator("#promptHistoryDetailClose").click();
      const actualLateResponse = page.waitForResponse((response) => response.url().includes(`/api/prompt-history/${ready.run_id}/details`));
      releaseRead(); await actualLateResponse;
      await page.unroute(detailPattern);
      await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
      await expect(page.locator("#promptHistoryDetailContent")).toBeEmpty();
    }
    if (scenario === "normal" && locale === "en" && theme === "dark" && width === 1280) {
      await page.context().setOffline(true);
      expect(await page.evaluate(async () => {
        try { await fetch("/api/dashboard-snapshot", { cache: "no-store" }); return false; } catch { return true; }
      })).toBe(true);
      await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
      await page.context().setOffline(false);
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
    }
    if (scenario === "uncertain" || scenario === "provider-recovered" || (scenario === "normal" && locale === "en" && theme === "dark" && width === 1280)) {
      const reader = await passiveProcess(installedPython || process.env.EP_BROWSER_PYTHON || "python3", environment, ready.data_root);
      const freshBrowser = await chromium.launch();
      try {
        if (installedPython) expect(reader.ready.module).toContain("site-packages/engineering_platform/");
        const freshPage = await freshBrowser.newPage({ viewport: { width, height } });
        await freshPage.goto(reader.ready.url, { waitUntil: "domcontentloaded" });
        await expect(freshPage.locator("[data-run-evidence]")).toHaveCount(3);
        const response = await freshPage.request.get(`${reader.ready.url.split("/?")[0]}/api/prompt-history/${ready.run_id}/details?project=project`);
        expect(response.ok()).toBe(true);
        expect((await response.json()).lifecycle.run_evidence).toEqual(stored.lifecycle.run_evidence);
        const capacity = await freshPage.request.get(`${reader.ready.url.split("/?")[0]}/api/provider-capacity`);
        expect(capacity.ok()).toBe(true); expect((await capacity.json()).rate_limits.windows[0].used_percent).toBe(0);
        reader.child.stdin.write("snapshot\n");
        const observed = await reader.next();
        expect(observed.passive_effects).toEqual({ model: 0, git: 0 });
        expect(observed.transport_observations.metadata_requests).toBeGreaterThan(0);
        expect(observed.transport_observations.unexpected_transport_attempts).toBe(0);
        expect(observed.transport_observations.native_app_server_starts).toBe(0);
      } finally {
        await freshBrowser.close();
        await stopIsolatedCanary(reader.child, reader.next, testInfo, "passive-reader");
      }
    }
    child.stdin.write("snapshot\n");
    const after = await next();
    expect(after.after).toEqual(ready.before);
    expect(after.transport_observations.metadata_requests).toBeGreaterThan(0);
    expect(after.transport_observations.unexpected_transport_attempts).toBe(0);
    expect(after.transport_observations.native_app_server_starts).toBe(0);
    if (scenario === "uncertain") {
      child.stdin.write("reconcile\n");
      const reconciled = await next();
      expect(reconciled.reconciled_baseline.creates).toBe(ready.before.creates);
      expect(reconciled.reconciled_baseline.model_calls).toEqual(ready.before.model_calls);
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(page.locator('[data-run-evidence="publication"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.state.reconciled"]);
      const receipt = await page.request.get(`${rootUrl}/api/prompt-history/${ready.run_id}/details?project=project`);
      const currentEvidence = (await receipt.json()).lifecycle.run_evidence;
      const current = currentEvidence.publication;
      expect(current.run_id).toBe(runEvidence.publication.run_id);
      expect(current.candidate_sha).toBe(runEvidence.publication.candidate_sha);
      expect(current.branch).toBe(runEvidence.publication.branch);
      expect(current.status).toBe("RECONCILED");
      await capture("publication", "same-run-reconciled", page.locator('[data-run-evidence="publication"]'), currentEvidence);
      child.stdin.write("snapshot\n");
      expect((await next()).after).toEqual(reconciled.reconciled_baseline);
    }
    const historicalTarget = scenario === "normal" && locale === "en" && theme === "dark" && width === 1280;
    if (historicalTarget) {
      child.stdin.write("advance-target\n");
      const advanced = await next();
      expect(advanced.target_head).not.toBe(runEvidence.recorded_candidate_sha);
      await page.reload({ waitUntil: "domcontentloaded" });
      const recordedFinding = page.locator('[data-run-evidence="findings"] details[data-disposition="VERIFIED"]');
      await recordedFinding.locator("summary").click();
      await expect(recordedFinding.locator(".field").filter({ hasText: runEvidence.recorded_candidate_sha }).first()).toBeVisible();
      await expect(page.locator('[data-run-evidence="findings"]')).not.toContainText(advanced.target_head);
      const historical = await page.request.get(`${rootUrl}/api/prompt-history/${ready.run_id}/details?project=project`);
      expect((await historical.json()).lifecycle.run_evidence).toEqual(runEvidence);
      await capture("historical-verification", "later-target-head", page.locator('[data-run-evidence="findings"]'));
      screenshots.at(-1).observed_target_head = advanced.target_head;
      child.stdin.write("snapshot\n");
      expect((await next()).after).toEqual(advanced.advanced_baseline);
    }
    if (scenario === "read-revoked") {
      child.stdin.write("revoke-read\n");
      const revoked = await next();
      const denied = await page.request.get(`${rootUrl}/api/prompt-history/${ready.run_id}/details?project=project`);
      expect(denied.status()).toBe(409);
      await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      await expect(page.locator("#promptHistoryDetailContent")).toBeEmpty();
      for (const item of runEvidence.specialists.findings) await expect(page.locator("body")).not.toContainText(item.id);
      await capture("read-revoked", "scope-cleared", page.locator("body"), null);
      child.stdin.write("snapshot\n");
      expect((await next()).after).toEqual(revoked.revoked_baseline);
    }
    const manifestPath = testInfo.outputPath("fresh-evidence-manifest.json");
    writeFileSync(manifestPath, JSON.stringify({
      contract: "ep-console-run-evidence-screenshots/v1", qualification: process.env.EP_RUN_EVIDENCE_CONVERGENCE === "1" ? "INSTALLED_CONVERGENCE" : installedPython ? "INSTALLED" : "SOURCE_CONVERGENCE",
      source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null, source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
      wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null, screenshots,
      assets: ready.assets, external_transport_observations: after.transport_observations,
    }, null, 2));
    expect(screenshots.slice(0, 3).map((item) => item.scenario)).toEqual(["selection", "findings", "publication"]);
    expect(screenshots).toHaveLength((verifiedFinding ? 4 : 3) + 1 + (scenario === "uncertain" ? 1 : 0) + (historicalTarget ? 1 : 0) + (scenario === "read-revoked" ? 1 : 0));
    expect(screenshots.some((item) => item.scenario === "denied-project" && item.publication === null)).toBe(true);
    await testInfo.attach("fresh-evidence-manifest", { path: manifestPath, contentType: "application/json" });
  } finally {
    await stopIsolatedCanary(child, next, testInfo);
  }
});
}

for (const revoked of [false, true]) {
  test(`operator snapshot after real retry ${revoked ? "cannot restore revoked scope" : "preserves permitted scope"}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const environment = { ...process.env, EP_QUALIFICATION_DETERMINISTIC_FLOW: "1", PYTHONPATH: path.join(repository, "src") };
    const installedPython = process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON;
    if (installedPython) delete environment.PYTHONPATH;
    const child = spawn(installedPython || process.env.EP_BROWSER_PYTHON || "python3", [fixture, "provider-blocked"], {
      cwd: installedPython ? path.dirname(installedPython) : repository, env: environment, stdio: ["pipe", "pipe", "pipe"],
    });
    retainWorkerLogs(child, testInfo, "canary");
    let buffer = "", pending; const messages = [];
      child.stdout.on("data", (data) => {
      buffer += data;
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        if (line.startsWith("{")) { const value = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(value); } else messages.push(value); }
      }
    });
    const next = () => messages.length ? Promise.resolve(messages.shift()) : new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(new Error(`Actual retry canary timed out: raw worker logs retained privately`)), 30_000);
      pending = (value) => { clearTimeout(deadline); resolve(value); };
      child.once("exit", (code) => { clearTimeout(deadline); reject(new Error(`Actual retry canary exited ${code}: raw worker logs retained privately`)); });
    });
    const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
    const held = deferred(), fetchAllowed = deferred(), fetched = deferred(), responseAllowed = deferred(), delivered = deferred();
    let armed = false, captured = false, actualResponse, actualBytes, snapshot;
    try {
      const ready = await next();
      if (installedPython) expect(ready.module).toContain("site-packages/engineering_platform/");
      if (process.env.EP_RUN_EVIDENCE_REQUIRE_INSTALLED === "1") expect(installedPython).toBeTruthy();
      await page.setViewportSize({ width: 1280, height: 720 });
      await page.goto(ready.url, { waitUntil: "domcontentloaded" });
      await page.route("**/api/dashboard-snapshot*", async (route) => {
        if (!armed || captured) { await route.continue(); return; }
        captured = true; held.resolve(); await fetchAllowed.promise;
        actualResponse = await route.fetch(); actualBytes = await actualResponse.body(); snapshot = JSON.parse(actualBytes.toString()); fetched.resolve();
        await responseAllowed.promise;
        // Original status/body/headers: only delivery is delayed, never forged.
        await route.fulfill({ response: actualResponse }); delivered.resolve();
      });
      if (!revoked) await page.locator("#autoRefresh").uncheck();
      if (await page.locator("#promptHistory").getAttribute("open") === null) await page.locator("#promptHistory > summary").click();
      await expect(page.locator("#promptHistoryRows .prompt-history-row")).toHaveCount(1);
      await page.locator("#promptHistoryRows .execution-history-action").filter({ hasText: "Retry execution" }).click();
      armed = true;
      const retryAck = page.waitForResponse((response) => response.url().includes("/api/execution-retry") && response.request().method() === "POST");
      await page.locator("#confirmationModalConfirm").click();
      expect((await retryAck).status()).toBe(200); await held.promise;
      child.stdin.write("dispatch-retry\n"); const successor = await next();
      expect(successor.state).toBe("WAIT_FOR_OPERATOR_MERGE");
      fetchAllowed.resolve(); await fetched.promise;
      expect(actualResponse.status()).toBe(200);
      expect(snapshot.status.run_id).toBe(successor.run_id);
      const evidence = snapshot.status.lifecycle.run_evidence;
      expect(evidence.run_id).toBe(successor.run_id);
      let baseline = successor.baseline;
      if (revoked) {
        await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
        child.stdin.write("revoke-read\n"); baseline = (await next()).revoked_baseline;
        const denied = await page.request.get(`${ready.url.split("/?")[0]}/api/prompt-history/${successor.run_id}/details?project=project`);
        expect(denied.status()).toBe(409);
        await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      } else {
        // Native auto-refresh is off: only the genuine operator response may
        // establish the successor's active cards in the paired positive case.
        await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      }
      responseAllowed.resolve(); await delivered.promise;
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await expect(page.locator("[data-run-evidence]")).toHaveCount(revoked ? 0 : 3);
      if (revoked) {
        await expect(page.locator("#promptHistoryDetailContent")).toBeEmpty();
        for (const finding of evidence.specialists.findings) await expect(page.locator("body")).not.toContainText(finding.id);
      } else await expect(page.locator('[data-run-evidence="selection"]')).toContainText(successor.run_id);
      child.stdin.write("snapshot\n"); expect((await next()).after).toEqual(baseline);
      const imagePath = testInfo.outputPath(`operator-${revoked ? "revoked" : "positive"}-fresh.png`);
      await page.screenshot({ path: imagePath, fullPage: true, animations: "disabled" });
      const capture = { scenario: revoked ? "operator-revoked" : "operator-positive", file: path.basename(imagePath),
        sha256: createHash("sha256").update(readFileSync(imagePath)).digest("hex"), run_scenario: testInfo.title,
        run_id: successor.run_id, predecessor_run_id: ready.run_id, finding_ids: revoked ? [] : evidence.specialists.findings.map((item) => item.id),
        invocation_ids: revoked ? [] : evidence.specialists.invocations.map((item) => item.invocation_id), publication: revoked ? null : evidence.publication,
        actual_response_sha256: createHash("sha256").update(actualBytes).digest("hex"),
        locale: await page.locator("#dashboardLocale").inputValue(), theme: await page.locator("html").getAttribute("data-theme"), viewport: page.viewportSize() };
      const manifestPath = testInfo.outputPath("fresh-evidence-manifest.json");
      writeFileSync(manifestPath, JSON.stringify({ contract: "ep-console-run-evidence-screenshots/v1", qualification: process.env.EP_RUN_EVIDENCE_CONVERGENCE === "1" ? "INSTALLED_CONVERGENCE" : installedPython ? "INSTALLED" : "SOURCE_CONVERGENCE",
        source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null, source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
        wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null, assets: ready.assets, screenshots: [capture] }, null, 2));
      await testInfo.attach("fresh-evidence-manifest", { path: manifestPath, contentType: "application/json" });
    } finally {
      fetchAllowed.resolve(); responseAllowed.resolve();
      await stopIsolatedCanary(child, next, testInfo);
    }
  });
}

for (const ingress of ["http", "sse"]) for (const quotaMode of ["available", "limited", "unavailable", "invalid"]) {
  test(`actual PLATFORM no-project ${ingress} snapshot preserves strict selected scope${quotaMode === "available" ? "" : ` quota-${quotaMode}`}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const installedPython = process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON;
    const environment = { ...process.env, EP_QUALIFICATION_DETERMINISTIC_FLOW: "1", PYTHONPATH: path.join(repository, "src") };
    if (installedPython) delete environment.PYTHONPATH;
    if (process.env.EP_RUN_EVIDENCE_REQUIRE_INSTALLED === "1") expect(installedPython).toBeTruthy();
    const child = spawn(installedPython || process.env.EP_BROWSER_PYTHON || "python3", [fixture, `quota-${quotaMode}`], {
      cwd: installedPython ? path.dirname(installedPython) : repository, env: environment, stdio: ["pipe", "pipe", "pipe"],
    });
    retainWorkerLogs(child, testInfo, "canary");
    let buffer = "", pending; const messages = [];
      child.stdout.on("data", (data) => {
      buffer += data;
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        if (line.startsWith("{")) { const value = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(value); } else messages.push(value); }
      }
    });
    const next = () => messages.length ? Promise.resolve(messages.shift()) : new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`PLATFORM canary timeout: raw worker logs retained privately`)), 30_000);
      pending = (value) => { clearTimeout(timer); resolve(value); };
      child.once("exit", (code) => { clearTimeout(timer); reject(new Error(`PLATFORM canary exited ${code}: raw worker logs retained privately`)); });
    });
    try {
      const ready = await next(), platformUrl = new URL(ready.url); platformUrl.search = "";
      const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      const observer = await observeConsoleDocument(page), contexts = [];
      const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
      const streams = [];
      cdp.on("Network.eventSourceMessageReceived", (event) => {
        if (event.eventName === "dashboard") streams.push(JSON.parse(event.data));
      });
      if (ingress === "http") await page.route("**/api/events*", (route) => route.abort());
      else await page.route("**/api/dashboard-snapshot*", (route) => {
        return route.abort(); // Real selected AND PLATFORM events independently populate the UI.
      });
      await page.goto(ready.url, { waitUntil: "domcontentloaded" });
      await expect(page.locator("#dashboardSplash")).toBeHidden();
      contexts.push(await observer.scope(ingress, "project"));
      const quotaExpected = quotaMode === "limited" ? { limited: true, reset_at: 2000000000 } : { limited: false };
      // Both first/cold and repeat reads use the actual installed handler and
      // parser, including error/invalid transport responses. Quota has no cache.
      for (let index = 0; index < 2; index += 1) {
        const quota = await page.request.get(new URL("/api/github-rate-limit", ready.url).href);
        expect(quota.status()).toBe(200); expect(await quota.json()).toEqual(quotaExpected);
      }
      contexts.push(await observer.select("none", ingress));
      expect(await page.evaluate(() => window.ENGINEERING_PLATFORM_NO_PROJECT === true)).toBe(false);
      const noneResponse = await page.request.get(new URL("/api/dashboard-snapshot?project=none", ready.url).href);
      expect(noneResponse.status()).toBe(200);
      const none = await noneResponse.json();
      expect(none.scope).toBe("PROJECT"); expect(none.project_id).toBe("none");
      if (ingress === "sse") await expect.poll(() => streams.filter((snapshot) => snapshot.scope === "PROJECT" && snapshot.project_id === "none").length).toBeGreaterThan(0);
      await expect(page.locator("#platformVersion")).toHaveText(none.status.platform_version);
      await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      contexts.push(await observer.select("project", ingress));
      await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
      const selectedResponse = await page.request.get(new URL("/api/dashboard-snapshot?project=project", ready.url).href);
      const platformResponse = await page.request.get(new URL("/api/dashboard-snapshot", ready.url).href);
      expect(selectedResponse.status()).toBe(200); expect(platformResponse.status()).toBe(200);
      const selected = await selectedResponse.json(), platform = await platformResponse.json();
      expect(selected.project_id).toBe("project");
      expect(platform.scope).toBe("PLATFORM"); expect(platform.project_id).toBeUndefined();
      await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
      const http = ingress === "http" ? page.waitForResponse((response) => {
        const url = new URL(response.url());
        return url.pathname === "/api/dashboard-snapshot" && !url.searchParams.get("project") && response.status() === 200;
      }) : null;
      contexts.push(await observer.select(null, ingress));
      await expect(page.locator("body")).toHaveAttribute("data-project-id", "none");
      await expect(page.locator("#dashboardSplash")).toBeHidden();
      if (http) expect((await (await http).json()).scope).toBe("PLATFORM");
      else await expect.poll(() => streams.filter((snapshot) => snapshot.scope === "PLATFORM").length).toBeGreaterThan(0);
      await expect(page.locator("#platformVersion")).toHaveText(platform.status.platform_version);
      if (ingress === "sse") {
        const lang = await page.locator("html").getAttribute("lang");
        await expect(page.locator("#updateMode")).toHaveText(DASHBOARD_MESSAGES[lang]["refresh.connected"]);
      }
      await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      await expect(page.locator("[data-run-evidence]")).toHaveCount(0);
      const browserQuota = await observer.quota(quotaExpected);
      if (quotaMode === "limited") {
        await expect(page.locator("#githubRateLimitBanner")).toBeVisible();
        const repeated = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/github-rate-limit");
        await page.locator("#githubRateLimitRefresh").click();
        expect(await (await repeated).json()).toEqual(quotaExpected);
      } else {
        await expect(page.locator("#githubRateLimitBanner")).toBeHidden();
      }
      const foreign = await page.request.get(new URL(`/api/prompt-history/${ready.run_id}/details?project=other`, ready.url).href);
      expect(foreign.status()).toBe(404);
      child.stdin.write("snapshot\n"); const after = await next();
      expect(after.after).toEqual(ready.before);
    expect(after.transport_observations.metadata_requests).toBeGreaterThan(0);
    expect(after.transport_observations.github_quota_requests).toBeGreaterThanOrEqual(3);
    expect(after.transport_observations.unexpected_transport_attempts).toBe(0);
    expect(after.transport_observations.native_app_server_starts).toBe(0); expect(errors).toEqual([]);
      const imagePath = testInfo.outputPath(`platform-${ingress}-fresh.png`);
      await page.screenshot({ path: imagePath, fullPage: true, animations: "disabled" });
      const capture = { scenario: `platform-${ingress}`, file: path.basename(imagePath),
        sha256: createHash("sha256").update(readFileSync(imagePath)).digest("hex"), run_scenario: testInfo.title,
        run_id: ready.run_id, finding_ids: [], invocation_ids: [], publication: null,
        document_contexts: contexts, browser_quota: browserQuota, direct_quota_controls: 2,
        actual_platform_snapshot_sha256: createHash("sha256").update(JSON.stringify(platform)).digest("hex"),
        locale: await page.locator("#dashboardLocale").inputValue(), theme: await page.locator("html").getAttribute("data-theme"), viewport: page.viewportSize() };
      const manifestPath = testInfo.outputPath("fresh-evidence-manifest.json");
      writeFileSync(manifestPath, JSON.stringify({ contract: "ep-console-run-evidence-screenshots/v1", qualification: process.env.EP_RUN_EVIDENCE_CONVERGENCE === "1" ? "INSTALLED_CONVERGENCE" : installedPython ? "INSTALLED" : "SOURCE_CONVERGENCE",
        source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null, source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
        wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null, assets: ready.assets, screenshots: [capture] }, null, 2));
      await testInfo.attach("fresh-evidence-manifest", { path: manifestPath, contentType: "application/json" });
      contexts.push(await observer.select("project", ingress));
      await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
      child.stdin.write("snapshot\n"); expect((await next()).after).toEqual(ready.before);
      const finished = JSON.parse(readFileSync(manifestPath));
      finished.screenshots[0].document_contexts = contexts;
      writeFileSync(manifestPath, JSON.stringify(finished, null, 2));
    } finally {
      await stopIsolatedCanary(child, next, testInfo);
    }
  });
}

async function causalStoredCanary(scenario) {
  const python = process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON || process.env.EP_BROWSER_PYTHON || 'python3';
  const env = { ...process.env, EP_QUALIFICATION_DETERMINISTIC_FLOW: '1', PYTHONPATH: path.join(repository, 'src') };
  if (process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON) delete env.PYTHONPATH;
  const child = spawn(python, [fixture, scenario], { cwd: path.dirname(python), env, stdio: ['pipe', 'pipe', 'pipe'] });
  retainWorkerLogs(child, test.info(), 'canary');
  let buffer = '', pending; const queued = [];
  child.stdout.on('data', d => {
    buffer += d;
    while (buffer.includes('\n')) {
      const index = buffer.indexOf('\n'), line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
      if (line.startsWith('{')) { const value = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(value); } else queued.push(value); }
    }
  });
  const next = () => queued.length ? Promise.resolve(queued.shift()) : new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Causal canary timeout: raw worker logs retained privately`)), 30_000);
    pending = value => { clearTimeout(timer); resolve(value); };
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`Causal canary exited ${code}: raw worker logs retained privately`)); });
  });
  return { child, next, ready: await next() };
}

for (const project of ['project', 'none', null]) {
  test(`current HTTP scope probe repairs first interrupted snapshot ${project ?? 'PLATFORM'}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const canary = await causalStoredCanary('normal');
    try {
      const observer = await observeConsoleDocument(page); let first = true;
      await page.route('**/api/events*', route => route.abort());
      await page.route('**/api/dashboard-snapshot*', route => {
        if (first) { first = false; return route.abort(); }
        return route.continue();
      });
      const url = new URL(canary.ready.url); if (project === null) url.search = ''; else url.searchParams.set('project', project);
      await page.goto(url.href, { waitUntil: 'domcontentloaded' });
      const applied = await observer.scope('http', project);
      expect(observer.records().some(record => record.path === '/api/dashboard-snapshot' && record.failure)).toBe(true);
      canary.child.stdin.write('snapshot\n'); const after = await canary.next(); expect(after.after).toEqual(canary.ready.before);
      const output = testInfo.outputPath('causal-context-readback.json');
      writeFileSync(output, JSON.stringify({ project, first_client_request_interrupted: true, applied, requests: observer.records() }, null, 2));
      await testInfo.attach('causal-context-readback', { path: output, contentType: 'application/json' });
      await causalManifest(page, testInfo, canary.ready, `causal-http-${project ?? 'PLATFORM'}`, { applied });
    } finally { await stopIsolatedCanary(canary.child, canary.next, testInfo); }
  });
}

for (const mode of ['available', 'unavailable']) {
  test(`browser quota completion distinguishes pending transport ${mode}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const canary = await causalStoredCanary(`quota-${mode}`), observer = await observeConsoleDocument(page);
    let release, entered; const gate = new Promise(resolve => { release = resolve; }), started = new Promise(resolve => { entered = resolve; });
    try {
      await page.route('**/api/dashboard-snapshot*', route => route.abort());
      await page.route('**/api/github-rate-limit', async route => { entered(); await gate; await route.continue(); });
      const url = new URL(canary.ready.url); url.search = '';
      await page.goto(url.href, { waitUntil: 'domcontentloaded' }); await started;
      const context = await observer.scope('sse', null);
      for (let index = 0; index < 2; index += 1) {
        const response = await page.request.get(new URL('/api/github-rate-limit', url).href);
        expect(response.status()).toBe(200); expect(await response.json()).toEqual({ limited: false });
      }
      await expect(page.locator('#githubRateLimitBanner')).toBeHidden();
      canary.child.stdin.write('snapshot\n'); const pending = await canary.next();
      expect(pending.transport_observations.github_quota_requests).toBe(2);
      // The original assertion is genuinely red at the wrong completion point.
      expect(() => expect(pending.transport_observations.github_quota_requests).toBeGreaterThanOrEqual(3)).toThrow();
      expect(observer.records().filter(record => record.path === '/api/github-rate-limit' && record.completed)).toHaveLength(0);
      release(); const browser = await observer.quota({ limited: false });
      expect(browser.document_id).toBe(context.document_id);
      canary.child.stdin.write('snapshot\n'); const after = await canary.next();
      expect(after.transport_observations.github_quota_requests).toBe(3); expect(after.after).toEqual(canary.ready.before);
      const output = testInfo.outputPath('causal-quota-readback.json');
      writeFileSync(output, JSON.stringify({ mode, direct_controls: 2, pending_count: 2, completed_count: 3, browser, context }, null, 2));
      await testInfo.attach('causal-quota-readback', { path: output, contentType: 'application/json' });
      await causalManifest(page, testInfo, canary.ready, `causal-quota-${mode}`, { context, browser, direct_controls: 2 });
    } finally { release(); await stopIsolatedCanary(canary.child, canary.next, testInfo); }
  });
}


async function causalManifest(page, testInfo, ready, scenario, proof) {
  const imagePath = testInfo.outputPath(`${scenario}-fresh.png`);
  await page.screenshot({ path: imagePath, fullPage: true, animations: 'disabled' });
  const capture = { scenario, file: path.basename(imagePath), sha256: createHash('sha256').update(readFileSync(imagePath)).digest('hex'),
    run_scenario: testInfo.title, run_id: ready.run_id, finding_ids: [], invocation_ids: [], publication: null,
    locale: await page.locator('#dashboardLocale').count() ? await page.locator('#dashboardLocale').inputValue() : null, theme: await page.locator('html').getAttribute('data-theme'), viewport: page.viewportSize(), proof };
  const manifest = testInfo.outputPath('fresh-evidence-manifest.json');
  writeFileSync(manifest, JSON.stringify({ contract: 'ep-console-run-evidence-screenshots/v1',
    qualification: process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON
      ? (process.env.EP_RUN_EVIDENCE_CONVERGENCE === '1' ? 'INSTALLED_CONVERGENCE' : 'INSTALLED') : 'SOURCE',
    source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null, source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
    wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null, assets: ready.assets, screenshots: [capture] }, null, 2));
  await testInfo.attach('fresh-evidence-manifest', { path: manifest, contentType: 'application/json' });
}

for (const scenario of ['provider-blocked', 'failed']) for (const action of ['Enter', 'click']) {
  test(`first stored history opening survives a genuine duplicate index ${scenario}/${action}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const canary = await causalStoredCanary(scenario), observer = await observeConsoleDocument(page);
    let count = 0, release, entered;
    const held = new Promise(resolve => { release = resolve; }), observed = new Promise(resolve => { entered = resolve; });
    try {
      await page.route('**/api/prompt-history', async route => {
        if (++count === 2) { entered(); await held; }
        await route.continue();
      });
      await page.goto(canary.ready.url, { waitUntil: 'domcontentloaded' });
      if (await page.locator('#promptHistory').getAttribute('open') === null) await page.locator('#promptHistory > summary').click();
      const row = page.locator('#promptHistoryRows .prompt-history-row');
      await expect(row).toHaveCount(1);
      await observed;
      await row.focus();
      const response = page.waitForResponse(value => new URL(value.url()).pathname === '/api/prompt-history');
      release();
      await (await response).finished();
      // Settle actual response/application tasks, without sleep/retry/opening.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await expect(row).toBeFocused();
      if (action === 'Enter') await page.keyboard.press('Enter');
      else await row.click();
      await expect(page.locator('#promptHistoryDetailModal')).toBeVisible();
      await expect(page.locator('#promptHistoryDetailContent [data-run-evidence]')).toHaveCount(3);
      expect(new URL(page.url()).searchParams.get('prompt')).toBe(canary.ready.run_id);
      canary.child.stdin.write('snapshot\n');
      expect((await canary.next()).after).toEqual(canary.ready.before);
      await causalManifest(page, testInfo, canary.ready, `duplicate-index-${action}`, { action, index_reads: count,
        focused_run: canary.ready.run_id, requests: observer.records(), modal_open: true });
    } finally {
      release();
      await stopIsolatedCanary(canary.child, canary.next, testInfo);
    }
  });
}

for (const mode of ['revoked', 'unavailable-open', 'unavailable-initial']) {
  test(`real project stream closes its document scope ${mode}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const canary = await causalStoredCanary('provider-blocked'), observer = await observeConsoleDocument(page);
    let release = () => {}, entered;
    const observed = new Promise(resolve => { entered = resolve; });
    try {
      await page.addInitScript(() => {
        const Native = EventSource;
        window.__streams = [];
        window.EventSource = class extends Native {
          constructor(...args) { super(...args); window.__streams.push(this); }
        };
      });
      if (mode === 'unavailable-initial') {
        const held = new Promise(resolve => { release = resolve; });
        await page.route('**/api/events*', async route => { entered(); await held; await route.continue(); });
      }
      await page.goto(canary.ready.url, { waitUntil: 'domcontentloaded' });
      if (await page.locator('#promptHistory').getAttribute('open') === null) await page.locator('#promptHistory > summary').click();
      await expect(page.locator('#promptHistoryRows .prompt-history-row')).toHaveCount(1);
      if (mode === 'unavailable-initial') await observed;
      else await expect.poll(() => page.evaluate(() => window.__streams[0]?.readyState)).toBe(1);
      canary.child.stdin.write(mode === 'revoked' ? 'revoke-read\n' : 'suspend-read-source\n');
      const changed = await canary.next();
      release();
      await expect(page.locator('body')).toHaveAttribute('data-console-read-outcome', mode === 'revoked' ? 'DENIED' : 'UNAVAILABLE');
      await expect.poll(() => page.evaluate(() => window.__streams[0]?.readyState)).toBe(2);
      await expect(page.locator('[data-run-evidence]')).toHaveCount(0);
      await expect(page.locator('#promptHistoryRows .prompt-history-row')).toHaveCount(0);
      canary.child.stdin.write(mode === 'revoked' ? 'restore-read\n' : 'restore-read-source\n');
      const restored = await canary.next();
      if (mode !== 'revoked') {
        // Ancillary diagnostics may be retained; replacement installation
        // authority must never be bootstrapped by a health/read request.
        expect(restored.retained_recreated_files.filter(item =>
          /(?:^|\/)(?:epdata\.sqlite(?:-journal|-wal|-shm)?|runtime-identity\.json|server\.json)$/.test(item.path))).toEqual([]);
      }
      // A genuine successful owning response cannot implicitly regrant this
      // ended document. A new authorised document is the recovery boundary.
      const current = await page.request.get(new URL('/api/dashboard-snapshot?project=project', canary.ready.url).href);
      expect(current.status()).toBe(200);
      expect((await current.json()).project_id).toBe('project');
      expect(await page.evaluate(() => window.__streams.map(stream => stream.readyState))).toEqual([2]);
      await expect(page.locator('#promptHistoryRows .prompt-history-row')).toHaveCount(0);
      await causalManifest(page, testInfo, canary.ready, `closed-scope-${mode}`, { mode,
        changed: mode === 'revoked' ? { revoked: true } : changed, requests: observer.records(), streams: [2], protected_cards: 0 });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#promptHistoryRows .prompt-history-row')).toHaveCount(1);
      await page.locator('#promptHistoryRows .prompt-history-row').click();
      await expect(page.locator('#promptHistoryDetailContent [data-run-evidence]')).toHaveCount(3);
      canary.child.stdin.write('snapshot\n');
      expect((await canary.next()).after).toEqual(restored.restored_baseline);
    } finally {
      release();
      await stopIsolatedCanary(canary.child, canary.next, testInfo);
    }
  });
}

test('current history document retains deep link after an interrupted index read', async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const canary = await causalStoredCanary('provider-blocked');
  try {
    const observer = await observeConsoleDocument(page); let first = true;
    await page.route('**/api/events*', route => route.abort());
    await page.route('**/api/prompt-history', route => { if (first) { first = false; return route.abort(); } return route.continue(); });
    const history = page.waitForResponse(response => new URL(response.url()).pathname === '/api/prompt-history' && response.status() === 200);
    const url = new URL(canary.ready.url); url.searchParams.set('prompt', canary.ready.run_id);
    await page.goto(url.href, { waitUntil: 'domcontentloaded' });
    const index = await (await history).json(); expect(index.runs.some(run => run.run_id === canary.ready.run_id)).toBe(true);
    const applied = await observer.scope('http', 'project');
    await expect(page).toHaveURL(url.href); await expect(page.locator('#promptHistoryDetailModal')).toBeVisible();
    await expect(page.locator('#promptHistoryDetailContent [data-run-evidence]')).toHaveCount(3);
    canary.child.stdin.write('snapshot\n'); const after = await canary.next(); expect(after.after).toEqual(canary.ready.before);
    await causalManifest(page, testInfo, canary.ready, 'causal-history-document', { applied, original_url_retained: true, direct_quota_controls: 0 });
  } finally { await stopIsolatedCanary(canary.child, canary.next, testInfo); }
});

for (const mode of ['control', 'index-recovery', 'detail-recovery', 'index-unavailable', 'close-index', 'close-detail', 'project-detail', 'revoked-detail', 'missing']) {
  test(`opened history detail recovery preserves current intent ${mode}`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    const canary = await causalStoredCanary('provider-blocked');
    const releases = []; let indexCount = 0, detailCount = 0, lateDetailReady = false, lateDeliveryAcknowledged = false, lateResponseProof, revokedBaseline;
    const gate = () => { let release; const promise = new Promise(resolve => { release = resolve; }); releases.push(release); return { promise, release }; };
    const secondIndex = gate(), retryIndex = gate(), lateDetail = gate();
    const observer = await observeConsoleDocument(page), detailBodies = [];
    const detailPath = `/api/prompt-history/${canary.ready.run_id}/details`;
    page.on('response', async response => {
      if (new URL(response.url()).pathname === detailPath && response.status() === 200) {
        try { detailBodies.push(await response.json()); } catch { /* Aborted document never supplies a usable payload. */ }
      }
    });
    try {
      await page.route('**/api/events*', route => route.abort());
      await page.route('**/api/prompt-history', async route => {
        indexCount += 1;
        if (indexCount === 2) { await secondIndex.promise; return mode === 'control' || mode === 'detail-recovery' || mode === 'missing' ? route.continue() : route.abort(); }
        if (indexCount === 3) { await retryIndex.promise; if (mode === 'index-unavailable') return route.abort(); }
        return route.continue();
      });
      await page.route('**/api/prompt-history/*/details', async route => {
        detailCount += 1;
        if (mode === 'detail-recovery' && detailCount === 1) return route.abort();
        if (['close-detail', 'project-detail', 'revoked-detail'].includes(mode) && detailCount === 2) {
          // Obtain the genuine authorized response before the user's next action,
          // then delay only its delivery. Its bytes and status are unchanged.
          const response = await route.fetch();
          const body = await response.body();
          lateResponseProof = { status: response.status(), sha256: createHash('sha256').update(body).digest('hex'), run_id: JSON.parse(body).history?.run_id };
          lateDetailReady = true; await lateDetail.promise;
          await route.fulfill({ response }); lateDeliveryAcknowledged = true; return;
        }
        return route.continue();
      });
      const url = new URL(canary.ready.url); url.searchParams.set('prompt', mode === 'missing' ? 'absent-stored-run' : canary.ready.run_id);
      await page.goto(url.href, { waitUntil: 'domcontentloaded' });
      const modal = page.locator('#promptHistoryDetailModal'), content = page.locator('#promptHistoryDetailContent');
      const cards = content.locator('[data-run-evidence]'), downloadButton = page.locator('#promptHistoryDetailDownloadJson');
      let document;
      if (['detail-recovery', 'missing'].includes(mode)) {
        await expect.poll(() => observer.records().some(record => record.path === '/api/dashboard-snapshot' && record.completed && record.response?.scope === 'PROJECT' && record.response?.project_id === 'project')).toBe(true);
        const documentId = await observer.documentId();
        document = observer.records().find(record => record.document_id === documentId && record.path === '/api/dashboard-snapshot' && record.completed);
        expect(document.response.project_id).toBe('project');
        await expect(page.locator('body')).toHaveAttribute('data-project-id', 'project');
        await expect(page.locator('#platformVersion')).toHaveText(document.response.version);
      } else document = await observer.scope('http', 'project');
      if (mode === 'missing') {
        await expect(modal).toBeHidden(); await expect(cards).toHaveCount(0); await expect(downloadButton).toBeHidden();
        await expect.poll(() => new URL(page.url()).searchParams.get('prompt')).toBeNull();
        secondIndex.release();
        await expect.poll(() => observer.records().filter(record => record.path === '/api/prompt-history' && record.completed).length).toBe(2);
        await expect(modal).toBeHidden();
      } else {
        if (mode === 'detail-recovery') {
          await expect(content).toHaveText('Execution details are unavailable.');
          await expect(cards).toHaveCount(0); await expect(downloadButton).toBeDisabled();
        } else {
          await expect(modal).toBeVisible(); await expect(cards).toHaveCount(3); await expect(downloadButton).toBeEnabled();
        }
        await expect.poll(() => indexCount).toBe(2);
        secondIndex.release();
        if (!['control', 'detail-recovery'].includes(mode)) {
          await expect(content).toHaveText('Execution details are unavailable.');
          await expect(cards).toHaveCount(0); await expect(downloadButton).toBeDisabled();
          if (mode === 'close-index') await page.locator('#promptHistoryDetailClose').click();
          retryIndex.release();
        }
        if (['close-detail', 'project-detail', 'revoked-detail'].includes(mode)) {
          await expect.poll(() => lateDetailReady).toBe(true);
          await expect(downloadButton).toBeDisabled();
          if (mode === 'close-detail') await page.locator('#promptHistoryDetailClose').click();
          else {
            if (mode === 'revoked-detail') { canary.child.stdin.write('revoke-read\n'); revokedBaseline = (await canary.next()).revoked_baseline; }
            const target = new URL(canary.ready.url);
            if (mode === 'project-detail') target.search = '';
            await page.goto(target.href, { waitUntil: 'domcontentloaded' });
          }
          lateDetail.release();
          if (mode === 'close-detail') {
            await expect.poll(() => observer.records().filter(record => record.path === detailPath && record.completed).length).toBe(2);
          } else {
            expect(await observer.documentId()).not.toBe(document.document_id);
            await expect.poll(() => lateDeliveryAcknowledged).toBe(true);
            expect(lateResponseProof).toEqual(expect.objectContaining({ status: 200, run_id: canary.ready.run_id }));
          }
          await expect(modal).toBeHidden(); await expect(cards).toHaveCount(0);
          if (mode === 'close-detail') await expect(downloadButton).toBeDisabled();
          else await expect(downloadButton).toBeHidden();
          if (mode === 'project-detail') await observer.scope('http', null);
          if (mode === 'revoked-detail') await expect(page.locator('[data-run-evidence]')).toHaveCount(0);
        } else if (mode === 'close-index' || mode === 'index-unavailable') {
          if (mode === 'close-index') {
            await expect.poll(() => observer.records().filter(record => record.path === '/api/prompt-history' && record.completed).length).toBe(2);
            await expect(modal).toBeHidden(); await expect.poll(() => new URL(page.url()).searchParams.get('prompt')).toBeNull();
          } else {
            await expect.poll(() => indexCount).toBe(3);
            await expect(content).toHaveText('Execution details are unavailable.'); await expect(modal).toBeVisible();
            expect(new URL(page.url()).searchParams.get('prompt')).toBe(canary.ready.run_id);
          }
          await expect(cards).toHaveCount(0); await expect(downloadButton).toBeDisabled();
        } else {
          await expect(modal).toBeVisible(); await expect(cards).toHaveCount(3); await expect(downloadButton).toBeEnabled();
          expect(new URL(page.url()).searchParams.get('prompt')).toBe(canary.ready.run_id);
          await expect.poll(() => detailBodies.length).toBe(mode === 'control' || mode === 'detail-recovery' ? 1 : 2);
          const current = detailBodies.at(-1); expect(current.history.run_id).toBe(canary.ready.run_id);
          const event = page.waitForEvent('download'); await downloadButton.click();
          const exported = JSON.parse(readFileSync(await (await event).path(), 'utf8'));
          expect(exported.history.run_id).toBe(canary.ready.run_id);
          expect(exported.lifecycle.run_evidence).toEqual(current.lifecycle.run_evidence);
        }
      }
      canary.child.stdin.write('snapshot\n'); const after = await canary.next();
      // Withdrawal uses a real authority operation; compare to its returned baseline.
      expect(after.after).toEqual(mode === 'revoked-detail' ? revokedBaseline : canary.ready.before);
      expect(after.transport_observations.native_app_server_starts).toBe(0);
      await causalManifest(page, testInfo, canary.ready, `opened-history-${mode}`, {
        mode, document, index_reads: indexCount, detail_reads: detailCount,
        run_id: canary.ready.run_id, final_url: page.url(), detail_cards: await cards.count(),
        exports_enabled: await downloadButton.isVisible() && await downloadButton.isEnabled(),
        late_response: lateResponseProof, late_delivery_acknowledged: lateDeliveryAcknowledged, requests: observer.records(),
      });
    } finally {
      for (const release of releases) release();
      await stopIsolatedCanary(canary.child, canary.next, testInfo);
    }
  });
}

// Layout uses the same genuine stored dispatcher run as the delivery probes.
// A DOM-only visible card over an empty Server snapshot is not stable input:
// the existing first-read recovery correctly removes that artificial card.
test.describe("iPhone direct touch: stored active execution", () => {
  test.use({ hasTouch: true, isMobile: true });
  for (const interrupted of [false, true]) {
    test(`keeps simultaneous status and options above the active execution ${interrupted ? "after first-read recovery" : "current read"}`, async ({ page }, testInfo) => {
      const canary = await causalStoredCanary("normal");
      const observer = await observeConsoleDocument(page);
      let first = true;
      try {
        await page.setViewportSize({ width: 390, height: 844 });
        await page.route("**/api/events*", route => route.abort());
        if (interrupted) await page.route("**/api/dashboard-snapshot*", route => {
          if (first) { first = false; return route.abort(); }
          return route.continue();
        });
        await page.goto(canary.ready.url, { waitUntil: "domcontentloaded" });
        const document = await observer.scope("http", "project");
        await expect(page.locator("#currentRun")).toBeVisible();
        // Only presentation density is varied, never run/authority evidence.
        await page.evaluate(() => {
          const checks = document.querySelector("#dashboardHealthChecks");
          checks.replaceChildren(...Array.from({ length: 12 }, (_, index) => {
            const item = document.createElement("li");
            item.dataset.health = "good";
            item.append(
              Object.assign(document.createElement("span"), { textContent: `Statuscontrole ${index + 1}` }),
              Object.assign(document.createElement("span"), { textContent: "Gezond" }),
            );
            return item;
          }));
        });
        await page.getByTestId("titlebar-options-toggle").click();
        await expect(page.getByTestId("titlebar-options-toggle")).toHaveAttribute("aria-expanded", "true");
        await page.getByTestId("dashboard-health-indicator").click();
        await expect(page.getByTestId("dashboard-health-indicator")).toHaveAttribute("aria-expanded", "true");
        await expect(page.locator("#currentRun")).toBeVisible();
        const layout = await page.evaluate(() => {
          const rect = selector => document.querySelector(selector).getBoundingClientRect();
          const header = document.querySelector(".dashboard-sticky-header");
          const status = rect("#dashboardHealthTooltip"), options = rect("#dashboardTitlebarOptionsContent");
          return {
            headerBottom: Math.round(header.getBoundingClientRect().bottom),
            headerMaxHeight: getComputedStyle(header).maxHeight,
            headerOverflowY: getComputedStyle(header).overflowY,
            lowestPanelBottom: Math.round(Math.max(status.bottom, options.bottom)),
            runTop: Math.round(rect("#currentRun").top),
          };
        });
        expect(layout.headerMaxHeight).toBe("none");
        expect(layout.headerOverflowY).toBe("visible");
        expect(layout.headerBottom).toBeGreaterThanOrEqual(layout.lowestPanelBottom);
        expect(layout.runTop).toBeGreaterThanOrEqual(layout.lowestPanelBottom);
        if (interrupted) expect(observer.records().some(record => record.path === "/api/dashboard-snapshot" && record.failure)).toBe(true);
        canary.child.stdin.write("snapshot\n");
        expect((await canary.next()).after).toEqual(canary.ready.before);
        await causalManifest(page, testInfo, canary.ready, `iphone-panels-${interrupted ? "recovered" : "current"}`, { document, layout, first_read_interrupted: interrupted });
      } finally { await stopIsolatedCanary(canary.child, canary.next, testInfo); }
    });
  }
});
