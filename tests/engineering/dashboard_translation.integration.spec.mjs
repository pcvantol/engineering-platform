import { spawn, execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { DASHBOARD_MESSAGES } from "../../src/engineering_platform/assets/dashboard_locales.mjs";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const fixture = path.join(repository, "tests/engineering/dashboard_translation_fixture.py");
const sourceDiagnostic = "Validation blocked. Expected: usable scratch. Observed: missing directory.";
const expectedTranslation = (locale, source) => `${locale}::${Array.from(source).slice(0, 80).join("")}`;

function providerJournal(root) {
  try { return readFileSync(path.join(root, "provider.jsonl"), "utf8").trim().split("\n").filter(Boolean).map(JSON.parse); }
  catch (error) { if (error.code === "ENOENT") return []; throw error; }
}

function processExists(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === "ESRCH") return false; throw error; }
}

async function startFixture(root, diagnostic = sourceDiagnostic) {
  const installation = path.join(root, "installation"), isolatedHome = path.join(root, "home");
  mkdirSync(installation); mkdirSync(isolatedHome);
  copyFileSync(fixture, path.join(root, "provider.py"));
  chmodSync(path.join(root, "provider.py"), 0o700);
  // Wheel qualification supplies an isolated installed Python and runs the
  // fixture with no source PYTHONPATH from an unrelated temporary directory.
  const installedPython = process.env.EP_TRANSLATION_INSTALLED_PYTHON;
  const environment = {
    ...process.env, HOME: isolatedHome, XDG_DATA_HOME: path.join(root, "xdg"),
    ENGINEERING_PLATFORM_TEST_INSTALLATION_ROOT: installation,
    PYTHONPATH: path.join(repository, "src"), EP_TRANSLATION_FIXTURE_ROOT: root,
    EP_TRANSLATION_FIXTURE_DIAGNOSTIC: diagnostic,
  };
  if (installedPython) delete environment.PYTHONPATH;
  // No operator credential can be consumed by the harmless executable.
  delete environment.OPENAI_API_KEY; delete environment.CODEX_API_KEY;
  const python = installedPython || "python3";
  const child = spawn(python, [fixture, "--server", root], { cwd: installedPython ? root : repository, env: environment, stdio: ["ignore", "pipe", "pipe"] });
  let output = "", errors = "";
  child.stderr.on("data", (data) => { errors += data; });
  const port = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { child.kill("SIGTERM"); reject(new Error(`Translation fixture startup timeout: ${errors}`)); }, 15_000);
    child.once("error", (error) => { clearTimeout(timeout); reject(error); });
    child.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Translation fixture exited ${code}: ${errors}`)); });
    child.stdout.on("data", (data) => {
      output += data;
      const port = Number.parseInt(output, 10);
      if (port > 0) { clearTimeout(timeout); resolve(port); }
    });
  });
  const origin = JSON.parse(readFileSync(path.join(root, "module-origin.json"), "utf8"));
  expect(origin.deadline_seconds).toBe(1.5);
  if (installedPython) {
    expect(origin.translation).toContain("site-packages/engineering_platform/");
    expect(origin.providers).toContain("site-packages/engineering_platform/");
    expect(origin.translation.startsWith(repository)).toBe(false);
  }
  return { child, environment, python, url: `http://127.0.0.1:${port}/?project=translation-fixture` };
}

test.describe("real dynamic translation provider boundary", () => {
  let root, server;
  test.beforeEach(async ({ page }, testInfo) => {
    root = mkdtempSync(path.join(tmpdir(), "ep-translation-integrated-"));
    server = await startFixture(root, testInfo.title.includes("known deterministic")
      ? "Provider action exceeded the 1-minute host-owned deadline." : sourceDiagnostic);
    await page.addInitScript(() => localStorage.setItem("engineering-dashboard-client-state-v1", JSON.stringify({ locale: "en" })));
    await page.goto(server.url, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#dashboardLocale")).toHaveValue("en");
  });
  test.afterEach(async () => {
    // Reads only immutable evidence tables: ordinary UI audit rows are allowed.
    try {
      const after = execFileSync(server.python, [fixture, "--evidence", root], { cwd: root, env: server.environment, encoding: "utf8" }).trim();
      expect(JSON.parse(after)).toEqual(JSON.parse(readFileSync(path.join(root, "evidence-before.json"), "utf8")));
      const pids = providerJournal(root).filter((entry) => entry.event === "start").map((entry) => entry.pid);
      await expect.poll(() => pids.filter(processExists), { timeout: 5_000 }).toEqual([]);
    } finally {
      if (server?.child && server.child.exitCode === null && server.child.signalCode === null) {
        const exited = new Promise((resolve) => server.child.once("exit", resolve));
        server.child.kill("SIGTERM"); await exited;
      }
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("keeps a real historical modal open across delayed en→nl→de→en and all supported locales", async ({ page }) => {
    await expect(page.locator("#currentLog")).toContainText("usable scratch");
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    const details = page.locator("#promptHistoryRows .prompt-history-details");
    await expect(details).toHaveCount(1);
    await details.click();
    const modal = page.locator("#promptHistoryDetailModal");
    await expect(modal).toBeVisible();
    await expect(modal).toContainText("usable scratch");
    const originalUrl = page.url();
    const navigation = [];
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) navigation.push(frame.url()); });
    // The custom picker hides its native select, and the open dialog makes
    // background controls inert. Dispatch the production select change action
    // without closing the historical context under test.
    await page.locator("#dashboardLocale").selectOption("nl", { force: true });
    await expect.poll(() => providerJournal(root).filter((entry) => entry.event === "start" && entry.locale === "nl").length).toBe(1);
    await page.locator("#dashboardLocale").selectOption("de", { force: true });
    await expect(modal).toContainText("Validierung blockiert");
    await expect(page.locator("#currentLog")).toContainText("Validierung blockiert");
    await expect.poll(() => providerJournal(root).filter((entry) => entry.event === "finish").length).toBe(2);
    await expect(modal).toContainText("Validierung blockiert");
    await expect(modal).not.toContainText("Validatie geblokkeerd");
    const finishes = providerJournal(root).filter((entry) => entry.event === "finish");
    expect(finishes.map((entry) => entry.locale)).toEqual(["de", "nl"]);
    for (const [locale, translation] of [["fr", "Validation bloquée"], ["es", "Validación bloqueada"], ["nl", "Validatie geblokkeerd"]]) {
      await page.locator("#dashboardLocale").selectOption(locale, { force: true });
      await expect(modal).toBeVisible();
      await expect(modal).toContainText(translation);
    }
    const count = providerJournal(root).filter((entry) => entry.event === "start").length;
    await page.locator("#dashboardLocale").selectOption("en", { force: true });
    await expect(modal).toContainText("Validation blocked.\nExpected: usable scratch.\nObserved: missing directory.");
    await expect(page.locator("#currentLog")).toContainText("usable scratch");
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(count);
    expect(navigation).toEqual([]); expect(page.url()).toBe(originalUrl);
    expect(new URL(page.url()).searchParams.get("project")).toBe("translation-fixture");
    // Blocking reason and execution diagnostic are separate visible views of
    // the same immutable source, but produce one provider input per locale.
    for (const entry of providerJournal(root).filter((entry) => entry.event === "start")) expect(entry.texts).toEqual([sourceDiagnostic]);
  });

  test("enforces the real subprocess deadline, exact fallback, and explicit retry", async ({ page }) => {
    const source = "deadline-once::Original evidence is retained in full. Expected: usable scratch. Observed: missing directory.";
    await installInventory(page, [source]);
    const started = Date.now();
    await page.evaluate(() => window.translationFixture.localize());
    expect(Date.now() - started).toBeLessThan(3_000);
    const item = page.locator("#translation-inventory p");
    await expect(item).toHaveText(source);
    await expect(item).toHaveAttribute("data-translation-state", "source");
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(1);
    await page.evaluate(async () => { for (let index = 0; index < 10; index += 1) await window.translationFixture.localize(); });
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(1);
    await page.evaluate(async () => { window.translationFixture.client.retry(); await window.translationFixture.localize(); });
    await expect(item).toHaveText(expectedTranslation("nl", source));
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(2);
  });

  test("releases an aborted browser request while the bounded provider finishes safely", async ({ page }) => {
    const source = "changed::Browser abort preserves every source character.";
    await installInventory(page, [source], { requestTimeoutMs: 100 });
    await page.evaluate(() => window.translationFixture.localize());
    await expect(page.locator("#translation-inventory p")).toHaveText(source);
    await expect(page.locator("#translation-inventory p")).toHaveAttribute("data-translation-state", "source");
    await expect.poll(() => providerJournal(root).filter((entry) => entry.event === "finish").length).toBe(1);
    await page.evaluate(async () => { window.translationFixture.client.retry(); await window.translationFixture.localize(); });
    await expect(page.locator("#translation-inventory p")).toHaveText(expectedTranslation("nl", source));
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(1);
  });

  test("renders known deterministic diagnostics without any translation provider", async ({ page }) => {
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    await page.locator("#promptHistoryRows .prompt-history-details").click();
    for (const locale of ["nl", "de", "fr", "es", "en"]) {
      await page.locator("#dashboardLocale").selectOption(locale, { force: true });
      await expect(page.locator("#promptHistoryDetailModal")).toContainText(DASHBOARD_MESSAGES[locale]["operational.provider_deadline_exceeded"].replace("{minutes}", "1"));
    }
    expect(providerJournal(root)).toEqual([]);
  });

  test("preserves modal focus, scroll, project and unsent form text across live translation", async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 650 });
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    await page.locator("#promptHistoryRows .prompt-history-chat").click();
    await page.locator("#chatInput").fill("Unsent original prompt /tmp/scratch --validate deadbeef");
    await page.locator("#promptHistoryChatClose").click();
    await page.locator("#promptHistoryRows .prompt-history-details").click();
    const modal = page.locator("#promptHistoryDetailModal"), copy = modal.locator(".prompt-history-run-id-copy");
    await copy.focus();
    const position = await page.locator("#promptHistoryDetailContent").evaluate((element) => {
      element.scrollTop = Math.min(80, element.scrollHeight - element.clientHeight); return element.scrollTop;
    });
    expect(position).toBeGreaterThan(0);
    const url = page.url(), mutations = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && !["/api/dashboard-translate", "/api/audit/user-action"].includes(new URL(request.url()).pathname)) mutations.push(request.url());
    });
    await page.locator("#dashboardLocale").selectOption("nl", { force: true });
    await expect(modal).toContainText("Validatie geblokkeerd");
    await expect(copy).toBeFocused();
    expect(await page.locator("#promptHistoryDetailContent").evaluate((element) => element.scrollTop)).toBe(position);
    await expect(page.locator("#chatInput")).toHaveValue("Unsent original prompt /tmp/scratch --validate deadbeef");
    await expect(page.locator("#dashboardProject")).toHaveValue("translation-fixture");
    expect(page.url()).toBe(url); expect(mutations).toEqual([]);
  });

  test("passes serialized ASCII and Unicode batches through real route and service validation", async ({ page }) => {
    const sources = [
      ...Array.from({ length: 7 }, (_, index) => `ascii-${index}:` + "a".repeat(3992)),
      ...Array.from({ length: 4 }, (_, index) => `漢${index}` + "字".repeat(2998)),
      "exact-item:" + "a".repeat(4085), "non-bmp:" + "😀".repeat(4088),
      "accented:" + "é".repeat(3500),
      "escaping:" + '"\\\n'.repeat(1200),
      ...Array.from({ length: 10 }, (_, index) => `short-unique-${index}`),
      "over-item:" + "😀".repeat(4087),
    ];
    const accepted = sources.slice(0, -1), requests = [];
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/dashboard-translate") requests.push(request); });
    await installInventory(page, sources);
    await page.evaluate(() => window.translationFixture.localize());
    const translated = await page.locator("#translation-inventory p").allTextContents();
    expect(translated).toEqual([...accepted.map((source) => expectedTranslation("nl", source)), sources.at(-1)]);
    expect(requests.length).toBeGreaterThan(3);
    const sent = [];
    for (const request of requests) {
      const body = request.postData(), payload = JSON.parse(body);
      expect(Buffer.byteLength(body, "utf8")).toBeLessThanOrEqual(32768);
      expect(payload.texts.length).toBeLessThanOrEqual(8);
      expect(payload.texts.reduce((count, text) => count + Array.from(text).length, 0)).toBeLessThanOrEqual(24000);
      expect(payload.texts.every((text) => Array.from(text).length <= 4096)).toBe(true);
      sent.push(...payload.texts);
      expect((await request.response()).status()).toBe(200);
    }
    expect(sent).toEqual(accepted);
    expect(providerJournal(root).filter((entry) => entry.event === "start").flatMap((entry) => entry.texts).sort()).toEqual([...accepted].sort());
    await expect(page.locator("#translation-inventory p").last()).toHaveAttribute("data-translation-state", "source");
  });

  test("matches Python blank-text validation without dropping BOM or blocking valid evidence", async ({ page }) => {
    const sources = ["\u0085", "\u001c\u001d\u001e\u001f", "Valid evidence alongside Python whitespace.", "\ufeff"];
    const requests = [];
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/dashboard-translate") requests.push(request); });
    await installInventory(page, sources);
    await page.evaluate(() => window.translationFixture.localize());
    const rows = page.locator("#translation-inventory p");
    expect(await rows.allTextContents()).toEqual([
      sources[0], sources[1], expectedTranslation("nl", sources[2]), expectedTranslation("nl", sources[3]),
    ]);
    await expect(rows.nth(0)).toHaveAttribute("data-translation-state", "source");
    await expect(rows.nth(1)).toHaveAttribute("data-translation-state", "source");
    expect(requests).toHaveLength(1);
    expect(requests[0].postDataJSON()).toEqual({ locale: "nl", texts: sources.slice(2) });
    expect((await requests[0].response()).status()).toBe(200);
    expect(providerJournal(root).filter((entry) => entry.event === "start").flatMap((entry) => entry.texts)).toEqual(sources.slice(2));
  });

  test("rejects invalid request boundaries without invoking any provider", async ({ page }) => {
    const endpoint = new URL("/api/dashboard-translate?project=translation-fixture", server.url).href;
    const cases = [
      { json: { locale: "xx", texts: ["valid"] }, status: 400 },
      { json: { locale: "nl", texts: ["x".repeat(4097)] }, status: 400 },
      { json: { locale: "nl", texts: Array.from({ length: 9 }, (_, index) => `text-${index}`) }, status: 400 },
      { json: { locale: "nl", texts: Array.from({ length: 6 }, (_, index) => `${index}` + "x".repeat(3999)).concat("x") }, status: 400 },
      { json: { locale: "nl", texts: ["valid"], unexpected: true }, status: 400 },
      { body: "{", status: 400 },
      { body: '{"locale":"nl","texts":' + "[".repeat(2000) + '"nested"' + "]".repeat(2000) + "}", status: 400 },
      { json: { locale: "nl", texts: ["valid"] }, origin: "http://untrusted.invalid", status: 403 },
      { json: { locale: "nl", texts: ["valid"] }, project: "unknown", status: 409 },
      { json: { locale: "nl", texts: ["valid"] }, project: "", status: 409 },
    ];
    for (const item of cases) {
      const url = new URL(endpoint);
      if (item.project !== undefined) { url.search = item.project ? `?project=${item.project}` : ""; }
      const result = await page.request.post(url.href, { data: item.body ?? JSON.stringify(item.json), headers: { "Content-Type": "application/json", Origin: item.origin ?? new URL(server.url).origin } });
      expect(result.status()).toBe(item.status);
    }
    const english = await page.request.post(endpoint, { data: { locale: "en", texts: ["Original English evidence."] } });
    expect(english.status()).toBe(200); expect(await english.json()).toEqual({ translations: ["Original English evidence."] });
    expect(providerJournal(root)).toEqual([]);
  });

  test("accepts exact total-character and serialized-byte limits and rejects one extra byte", async ({ page }) => {
    const endpoint = new URL("/api/dashboard-translate?project=translation-fixture", server.url).href;
    const totals = Array.from({ length: 6 }, (_, index) => `total-${index}:` + "a".repeat(3992));
    expect(totals.reduce((size, text) => size + Array.from(text).length, 0)).toBe(24000);
    await installInventory(page, totals);
    await page.evaluate(() => window.translationFixture.localize());
    expect(await page.locator("#translation-inventory p").allTextContents()).toEqual(totals.map((source) => expectedTranslation("nl", source)));
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(1);
    const byteTexts = ["byte-a:" + "\u0001".repeat(4089), "byte-b:"];
    const remaining = 32768 - Buffer.byteLength(JSON.stringify({ locale: "nl", texts: byteTexts }), "utf8");
    byteTexts[1] += "\u0001".repeat(Math.floor(remaining / 6)) + "a".repeat(remaining % 6);
    expect(Buffer.byteLength(JSON.stringify({ locale: "nl", texts: byteTexts }), "utf8")).toBe(32768);
    const requests = [];
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/dashboard-translate") requests.push(request); });
    await installInventory(page, byteTexts);
    await page.evaluate(() => window.translationFixture.localize());
    expect(requests).toHaveLength(1);
    expect(Buffer.byteLength(requests[0].postData(), "utf8")).toBe(32768);
    expect((await requests[0].response()).status()).toBe(200);
    expect(await page.locator("#translation-inventory p").allTextContents()).toEqual(byteTexts.map((source) => expectedTranslation("nl", source)));
    byteTexts[1] += "a";
    const over = await page.request.post(endpoint, { data: JSON.stringify({ locale: "nl", texts: byteTexts }), headers: { "Content-Type": "application/json" } });
    expect(over.status()).toBe(400);
    expect(providerJournal(root).filter((entry) => entry.event === "start")).toHaveLength(2);
  });

  test("rejects invalid provider results and releases inflight without automatic retry", async ({ page }) => {
    for (const kind of ["cardinality", "invalid", "oversized", "malformed"]) {
      const source = `${kind}::Evidence <img src=x onerror=alert(1)> remains literal.`;
      await installInventory(page, [source]);
      await page.evaluate(() => window.translationFixture.localize());
      await expect(page.locator("#translation-inventory p")).toHaveText(source);
      await expect(page.locator("#translation-inventory p")).toHaveAttribute("data-translation-state", "source");
      await expect(page.locator("#translation-inventory img")).toHaveCount(0);
      await page.evaluate(() => window.translationFixture.localize());
      expect(providerJournal(root).filter((entry) => entry.event === "start" && entry.texts.includes(source))).toHaveLength(1);
    }
  });

  test("projects markup-looking untrusted source and successful provider output as literal text", async ({ page }) => {
    const source = '<img src=x onerror="window.__translationInjected=true"> Untrusted diagnostic.';
    await page.evaluate(() => { window.__translationInjected = false; });
    await installInventory(page, [source]);
    await page.evaluate(() => window.translationFixture.localize());
    await expect(page.locator("#translation-inventory p")).toHaveText(expectedTranslation("nl", source));
    await expect(page.locator("#translation-inventory img")).toHaveCount(0);
    expect(await page.evaluate(() => window.__translationInjected)).toBe(false);
    expect(providerJournal(root).filter((entry) => entry.event === "start").flatMap((entry) => entry.texts)).toEqual([source]);
  });

  test("deduplicates two views and ignores changed or detached source while requests are pending", async ({ page }) => {
    const old = "changed::The previous source must not return.", replacement = "Fresh evidence replaces the previous source.";
    await installInventory(page, [old, old, "changed::Detached evidence."]);
    await page.evaluate(() => { window.translationFixture.first = window.translationFixture.localize(); });
    await expect.poll(() => providerJournal(root).filter((entry) => entry.event === "start").length).toBe(1);
    await page.evaluate((replacement) => {
      const state = window.translationFixture;
      state.rows[0].source = replacement; state.rows[0].element.textContent = replacement;
      state.rows[2].element.remove();
      state.second = state.client.localize([state.rows[0]]);
    }, replacement);
    await page.evaluate(async () => { const state = window.translationFixture; await Promise.all([state.first, state.second]); });
    const rows = page.locator("#translation-inventory p");
    await expect(rows.nth(0)).toHaveText(expectedTranslation("nl", replacement));
    await expect(rows.nth(1)).toHaveText(expectedTranslation("nl", old));
    expect(providerJournal(root).filter((entry) => entry.event === "start").flatMap((entry) => entry.texts).filter((source) => source === old)).toHaveLength(1);
  });

  test("bounds concurrent provider work while preserving 600 visible results", async ({ page }) => {
    const sources = Array.from({ length: 600 }, (_, index) => `stress::${String(index).padStart(3, "0")} Unique visible evidence.`);
    await installInventory(page, sources);
    await page.evaluate(() => window.translationFixture.localize());
    expect(await page.locator("#translation-inventory p").allTextContents()).toEqual(sources.map((source) => expectedTranslation("nl", source)));
    let active = 0, maximum = 0;
    for (const event of providerJournal(root).sort((a, b) => a.time - b.time)) {
      active += event.event === "start" ? 1 : -1; maximum = Math.max(maximum, active);
    }
    expect(active).toBe(0); expect(maximum).toBeLessThanOrEqual(2); expect(maximum).toBeGreaterThan(0);
    const inputs = providerJournal(root).filter((entry) => entry.event === "start").flatMap((entry) => entry.texts);
    expect(inputs.sort()).toEqual([...sources].sort());
  });
});

async function installInventory(page, sources, options = {}) {
  await page.evaluate(async ({ sources, options }) => {
    // Imports the exact shipped module through the production asset route. It
    // uses native fetch, so requests traverse the selected-project wrapper,
    // real handler/service and controlled external provider executable.
    const { createDynamicEvidenceLocalizer } = await import("/assets/dashboard_translation.mjs");
    document.getElementById("translation-inventory")?.remove();
    const container = document.createElement("section"); container.id = "translation-inventory"; document.body.append(container);
    const rows = sources.map((source) => {
      const element = document.createElement("p"); element.textContent = source; container.append(element); return { source, element };
    });
    const client = createDynamicEvidenceLocalizer({ getLocale: () => "nl", sourceFallbackTitle: () => "Original evidence: translation unavailable.", ...options });
    window.translationFixture = { client, rows, localize: () => client.localize(rows) };
  }, { sources, options });
}
