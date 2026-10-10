import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { test, expect } from "./dashboard_coverage_fixture.mjs";
import { chromium } from "@playwright/test";
import { DASHBOARD_MESSAGES } from "../../src/engineering_platform/assets/dashboard_locales.mjs";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const fixture = path.join(repository, "tests/engineering/dashboard_run_evidence_fixture.py");
// Every scenario owns its separate synthetic store/repository. Explicit
// parallel grouping lets the existing four shards balance these independent
// cases while keeping one worker per shard and all existing deadlines.
test.describe.configure({ mode: "parallel" });

async function passiveProcess(python, environment, dataRoot) {
  const child = spawn(python, [fixture, "--read", dataRoot], { cwd: path.dirname(python), env: environment, stdio: ["pipe", "pipe", "pipe"] });
  let buffer = "", stderr = "", pending; const queued = [];
  child.stderr.on("data", (data) => { stderr += data; });
  child.stdout.on("data", (data) => {
    buffer += data;
    while (buffer.includes("\n")) {
      const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (line.startsWith("{")) { const value = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(value); } else queued.push(value); }
    }
  });
  const next = () => queued.length ? Promise.resolve(queued.shift()) : new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Passive reader timeout: ${stderr}`)), 15_000);
    pending = (value) => { clearTimeout(timer); resolve(value); };
    child.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Passive reader exited ${code}: ${stderr}`)); });
  });
  return { child, next, ready: await next() };
}

const cases = ["prepared", "uncertain", "no-consumer", "no-capacity", "irrelevant", "dispositions", "withdrawn", "provider-recovered", "provider-blocked", "missing"]
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
  let buffer = "", stderr = "", pending;
  child.stderr.on("data", (data) => { stderr += data; });
  const messages = [];
  child.stdout.on("data", (data) => {
    buffer += data;
    while (buffer.includes("\n")) {
      const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (line.startsWith("{")) { const message = JSON.parse(line); if (pending) { const resolve = pending; pending = null; resolve(message); } else messages.push(message); }
    }
  });
  const next = () => messages.length ? Promise.resolve(messages.shift()) : new Promise((resolve, reject) => {
    const deadline = setTimeout(() => { pending = null; reject(new Error(`Canary timed out: ${stderr}`)); }, 30_000);
    pending = (message) => { clearTimeout(deadline); resolve(message); };
    child.once("exit", (code) => { clearTimeout(deadline); reject(new Error(`Canary exited ${code}: ${stderr}`)); });
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
    if (["provider-blocked", "missing"].includes(scenario)) await openStoredHistory();
    // Operator-wait is still an active run, not a fabricated terminal history.
    await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
    await expect(page.locator('[data-run-evidence="selection"] h3')).toHaveText(DASHBOARD_MESSAGES[locale]["run_evidence.specialists"]);
    const actualDetail = await page.request.get(`${ready.url.split("/?")[0]}/api/prompt-history/${ready.run_id}/details?project=project`);
    expect(actualDetail.ok()).toBe(true);
    const stored = await actualDetail.json();
    const noSpecialists = ["no-consumer", "no-capacity", "irrelevant", "missing"].includes(scenario);
    const runEvidence = stored.lifecycle.run_evidence;
    if (scenario === "missing") {
      expect(stored.lifecycle.available).toBe(false);
      expect(runEvidence).toBeUndefined();
      await expect(page.locator('[data-run-evidence="selection"]')).toContainText(DASHBOARD_MESSAGES[locale]["run_evidence.not_recorded"]);
    } else {
    expect(runEvidence.run_id).toBe(ready.run_id);
    expect(runEvidence.specialists.actual_model_invocation_count).toBe(noSpecialists ? 0 : 2);
    expect(runEvidence.publication.status).toBe(
      ({ prepared: "PREPARED", uncertain: "CREATE_UNCERTAIN", "provider-blocked": "NOT_RECORDED" })[scenario] || "RECONCILED",
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
      expect(stored.lifecycle.recovery.kind).toBe("provider_interruption");
      expect(stored.lifecycle.recovery.maximum_attempts).toBe(1);
      expect(stored.lifecycle.recovery.state).toBe("EXHAUSTED");
      expect(stored.lifecycle.run_evidence.specialists.findings.every((item) => item.disposition === "PROPOSED")).toBe(true);
    }
    if (["provider-blocked", "missing"].includes(scenario)) {
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
    const findings = page.locator('[data-run-evidence="findings"]');
    await expect(findings.locator("details[data-disposition]")).toHaveCount(noSpecialists ? 0 : scenario === "dispositions" ? 5 : 2);
    if (scenario === "dispositions") {
      expect(stored.lifecycle.run_evidence.specialists.duplicate_observations).toBe(1);
      expect(new Set(stored.lifecycle.run_evidence.specialists.findings.map((item) => item.disposition)))
        .toEqual(new Set(["ACCEPTED", "REJECTED", "DEFERRED", "VERIFIED"]));
    }
    if (!noSpecialists && scenario !== "provider-blocked") {
    await findings.locator('details[data-disposition="VERIFIED"] > summary').click();
    await expect(findings.locator('details[data-disposition="VERIFIED"]')).toContainText("README.md");
    await findings.locator("select").selectOption("VERIFIED");
    await expect(findings.locator('details[data-disposition="DEFERRED"]:visible')).toHaveCount(0);
    await findings.locator("select").selectOption("ALL");
    await expect(findings.locator('details[data-disposition="DEFERRED"]').first()).toBeVisible();
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
    if (["provider-blocked", "missing"].includes(scenario)) await openStoredHistory();
    await expect(page.locator("[data-run-evidence]")).toHaveCount(3);
    await page.reload({ waitUntil: "domcontentloaded" });
    if (scenario === "normal" && locale === "en" && theme === "dark" && width === 1280) {
      await page.context().setOffline(true);
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
        reader.child.stdin.write("snapshot\n");
        expect((await reader.next()).passive_effects).toEqual({ model: 0, git: 0 });
      } finally {
        await freshBrowser.close();
        if (reader.child.exitCode === null) {
          const stopped = new Promise((resolve) => reader.child.once("exit", resolve));
          reader.child.stdin.write("stop\n"); await stopped;
        }
      }
    }
    child.stdin.write("snapshot\n");
    const after = await next();
    expect(after.after).toEqual(ready.before);
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
    const manifestPath = testInfo.outputPath("fresh-evidence-manifest.json");
    writeFileSync(manifestPath, JSON.stringify({
      contract: "ep-console-run-evidence-screenshots/v1", qualification: installedPython ? "INSTALLED" : "SOURCE_CONVERGENCE",
      source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null, source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
      wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null, screenshots,
      assets: ready.assets,
    }, null, 2));
    expect(screenshots.slice(0, 3).map((item) => item.scenario)).toEqual(["selection", "findings", "publication"]);
    expect(screenshots).toHaveLength((verifiedFinding ? 4 : 3) + 1 + (scenario === "uncertain" ? 1 : 0));
    expect(screenshots.some((item) => item.scenario === "denied-project" && item.publication === null)).toBe(true);
    await testInfo.attach("fresh-evidence-manifest", { path: manifestPath, contentType: "application/json" });
  } finally {
    if (child.exitCode === null) {
      const stopped = new Promise((resolve) => child.once("exit", resolve));
      child.stdin.write("stop\n"); await stopped;
    }
  }
});
}
