import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  createTranslator,
  DASHBOARD_MESSAGES,
  OPERATIONAL_TRANSLATION_KEYS,
  SUPPORTED_LOCALES,
} from "../../tools/engineering/assets/dashboard_locales.mjs";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
let dashboard;
let dashboardRoot;
let dashboardUrl;

async function startDashboard(root) {
  return new Promise((resolve, reject) => {
    const process = spawn(
      "python3",
      [
        "-c",
        "from pathlib import Path; import sys; from tools.engineering.dashboard import DashboardHTTPServer, handler; server = DashboardHTTPServer(('127.0.0.1', 0), handler(Path(sys.argv[1]))); print(server.server_address[1], flush=True); server.serve_forever()",
        root,
      ],
      { cwd: repository, stdio: ["ignore", "pipe", "ignore"] },
    );
    let output = "";
    const timeout = setTimeout(() => {
      process.kill("SIGTERM");
      reject(new Error("Engineering Status test server did not report a port in time."));
    }, 10_000);
    process.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    process.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Engineering Status test server exited before startup (code ${code}).`));
    });
    process.stdout.on("data", (chunk) => {
      output += chunk;
      const port = Number.parseInt(output, 10);
      if (!Number.isInteger(port) || port <= 0) return;
      clearTimeout(timeout);
      resolve({ process, url: `http://127.0.0.1:${port}` });
    });
  });
}

async function waitForDashboard() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      if ((await fetch(`${dashboardUrl}/api/health`)).ok) return;
    } catch {
      // The isolated dashboard process is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("Engineering Status did not become healthy in time.");
}

test.beforeAll(async () => {
  dashboardRoot = mkdtempSync(path.join(tmpdir(), "djconnect-dashboard-test-"));
  const engineeringDirectory = path.join(dashboardRoot, "tools/engineering");
  mkdirSync(engineeringDirectory, { recursive: true });
  for (const filename of ["ENGINEERING_PLATFORM_CONFIG.json", "ENGINEERING_PLATFORM_VERSION.json"]) {
    copyFileSync(path.join(repository, "tools/engineering", filename), path.join(engineeringDirectory, filename));
  }
  const server = await startDashboard(dashboardRoot);
  dashboard = server.process;
  dashboardUrl = server.url;
  await waitForDashboard();
});

test.afterAll(() => {
  dashboard?.kill("SIGTERM");
  if (dashboardRoot) rmSync(dashboardRoot, { force: true, recursive: true });
});

async function openTitlebarOptions(page) {
  const content = page.locator("#dashboardTitlebarOptionsContent");
  if (await content.isHidden()) {
    await page.getByTestId("titlebar-options-toggle").click();
  }
}

async function selectDashboardLocale(page, language) {
  const nativeSelect = page.locator("#dashboardLocale");
  // Do not replace the page while its first snapshot is still hydrating.
  // Under the ten-worker CI profile that otherwise races the initial locale
  // read with a reload and can leave the splash state behind.
  await waitForDashboardReady(page);
  if (await nativeSelect.inputValue() === language) return;
  const localeReload = page.waitForEvent(
    "framenavigated",
    (frame) => frame === page.mainFrame(),
  );
  // The native select is deliberately hidden behind the accessible custom
  // picker. Force its deterministic change event here; interaction with the
  // visible picker is covered separately and this helper only verifies the
  // locale reload contract shared by both controls.
  await nativeSelect.selectOption(language, { force: true });
  await localeReload;
  await page.waitForLoadState("domcontentloaded");
  await waitForDashboardReady(page);
}

async function waitForDashboardReady(page) {
  // Under a fully-parallel browser run a dashboard reload can lose its first
  // local snapshot connection. Retry that read-only reload once instead of
  // waiting for the full test timeout and then repeating the entire test.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await page.waitForFunction(
        () => document.body?.classList.contains("dashboard-ready"),
        { timeout: 10_000 },
      );
      return;
    } catch (error) {
      if (attempt === 1) throw error;
      const navigation = page.waitForEvent("framenavigated", (frame) => frame === page.mainFrame());
      await page.evaluate(() => window.location.reload());
      await navigation;
      await page.waitForLoadState("domcontentloaded");
    }
  }
}

async function openDashboardPicker(picker) {
  // The dashboard owns a fixed, nested scroll region. Dispatch a pointer-like
  // click directly so picker behaviour tests do not depend on the headless
  // browser's document-level hit-testing outside that region. The dedicated
  // scroll-shell tests continue to cover physical positioning.
  await dispatchDashboardPointerClick(picker.locator(".dashboard-locale__button"));
}

async function chooseDashboardPickerOption(picker, value) {
  await dispatchDashboardPointerClick(picker.locator(`[role=option][data-dashboard-select-value="${value}"]`));
}

async function dispatchDashboardPointerClick(locator) {
  await locator.dispatchEvent("click", { detail: 1 });
}

async function scrollDashboardElementIntoView(locator) {
  await locator.evaluate((element) => {
    const region = document.querySelector(".dashboard-scroll-region");
    if (!(region instanceof HTMLElement) || getComputedStyle(region).overflowY === "visible") {
      element.scrollIntoView({ block: "center", inline: "nearest" });
      return;
    }
    const target = element.getBoundingClientRect(), container = region.getBoundingClientRect();
    region.scrollTop += target.top - container.top - (container.height - target.height) / 2;
  });
}

async function navigateDashboard(navigate) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await navigate();
    } catch (error) {
      lastError = error;
      const transientNavigationFailure = /ERR_EMPTY_RESPONSE|ERR_CONNECTION_RESET|chrome-error:\/\/chromewebdata\//.test(String(error));
      if (!transientNavigationFailure || attempt === 2) throw error;
      await waitForDashboard();
      await new Promise((resolve) => setTimeout(resolve, 100 * (attempt + 1)));
    }
  }
  throw lastError;
}

test.beforeEach(async ({ page }, testInfo) => {
  const goto = page.goto.bind(page);
  const reload = page.reload.bind(page);
  const prepareDashboard = async () => {
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    if (![
      "puts every mobile title-bar setting in a labelled expandable panel",
      "matches the iPhone portrait dashboard visual reference",
      "only starts pull-to-refresh from the scroll region's top edge",
      "keeps a green pull request visible until the operator merges or aborts it",
      "explains a merge-check category, retains its successful check time, and retries from the error dialog",
      "opens, closes and navigates prompt-history deeplinks without reloading",
    ].includes(testInfo.title)) {
      await openTitlebarOptions(page);
    }
  };
  page.goto = async (...arguments_) => {
    const response = await navigateDashboard(() => goto(...arguments_));
    await prepareDashboard();
    return response;
  };
  page.reload = async (...arguments_) => {
    const response = await navigateDashboard(() => reload(...arguments_));
    await prepareDashboard();
    return response;
  };
});

test.describe("Engineering Status browser smoke", () => {
  test.use({ viewport: { width: 390, height: 844 }, colorScheme: "dark", locale: "nl-NL", reducedMotion: "reduce" });

  test("keeps every supported UI catalog complete", () => {
    const canonicalKeys = Object.keys(DASHBOARD_MESSAGES.en).sort();
    for (const locale of SUPPORTED_LOCALES) {
      expect(Object.keys(DASHBOARD_MESSAGES[locale]).sort(), locale).toEqual(canonicalKeys);
      for (const key of canonicalKeys) expect(DASHBOARD_MESSAGES[locale][key], `${locale}:${key}`).toBeTruthy();
    }
  });

  test("does not show the fixed local audit-log setting", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#configurationAuditLogging")).toHaveCount(0);
  });

  test("does not show a separate safe-local-settings label", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#configurationControlsTitle")).toHaveCount(0);
  });

  test("groups free disk space above the component-detail refresh interval", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const configuration = page.locator("#configuration");
    await configuration.evaluate((element) => { element.open = true; });
    const section = configuration.locator("#configurationHostComponents");
    await expect(section).toHaveCount(1);
    await expect(section).toContainText("Lokale hostonderdelen");
    await expect(section.locator("#workspaceFreeDiskSpace")).toHaveCount(1);
    await expect(section.locator("#configurationComponentDetailsInterval")).toHaveCount(1);
    await expect(section).toHaveCSS("border-top-style", "solid");
    expect(await section.evaluate((element) => {
      const disk = element.querySelector("#workspaceFreeDiskSpace");
      const interval = element.querySelector("#configurationComponentDetailsInterval")?.closest("label");
      return Boolean(disk.compareDocumentPosition(interval) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
  });

  test("does not leave an empty configuration status container between grouped settings", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const configuration = page.locator("#configuration");
    await configuration.evaluate((element) => { element.open = true; });
    const statusContainer = configuration.locator(":scope > .configuration-controls");
    await expect(statusContainer).toBeHidden();
    await page.locator("#configurationStatus").evaluate((element) => { element.textContent = "Saved locally."; });
    await expect(statusContainer).toBeVisible();
  });

  test("places writable log settings in Logs and explains them", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const logs = page.locator("#componentLogs");
    await logs.evaluate((element) => { element.open = true; });
    for (const id of ["configurationLogRetention", "configurationLogLevel"]) {
      const control = page.locator(`#${id}`);
      await expect(control).toBeVisible();
      await expect(control.locator("xpath=ancestor::details[1]")).toHaveAttribute("id", "componentLogs");
      const label = control.locator("xpath=preceding-sibling::span");
      await expect(label).toHaveClass(/label/);
      await expect(label.locator(".configuration-info")).toHaveCount(1);
      await expect(label.locator(".configuration-info")).toHaveAttribute("aria-label", /.+/);
    }
    const settings = logs.locator(":scope > .log-settings");
    await expect(settings).toHaveCount(1);
    expect(await settings.evaluate((element) => element.previousElementSibling?.className)).toBe("technical-grid");
    await expect(settings).toHaveCSS("border-top-style", "solid");
  });

  test("places the platform-health refresh interval below platform components", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const platform = page.locator("#platformHealth");
    await platform.evaluate((element) => { element.open = true; });
    const control = page.locator("#configurationPlatformHealthInterval");
    await expect(control).toBeVisible();
    await expect(control.locator("xpath=ancestor::details[1]")).toHaveAttribute("id", "platformHealth");
    await expect(platform.locator(".platform-settings")).toHaveCount(1);
    expect(await platform.locator(".platform-settings").evaluate(
      (settings) => settings.previousElementSibling?.id,
    )).toBe("platformHealthComponents");
    await expect(platform.locator(".platform-settings")).toHaveCSS("border-top-style", "solid");
    const order = await platform.evaluate((element) => {
      const settings = element.querySelector(".platform-settings");
      const components = element.querySelector("#platformHealthComponents");
      return Boolean(components.compareDocumentPosition(settings) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(order).toBe(true);
  });

  test("shows the Inbox location below its label with a matching change action", async ({ page }) => {
    const selectedRoot = path.join(dashboardRoot, "selected-engineering-root");
    mkdirSync(path.join(selectedRoot, "Inbox"), { recursive: true });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const queue = page.locator("#queueItems");
    await queue.evaluate((element) => { element.open = true; });
    await page.locator("#queueItems").evaluate((element) => { element.open = true; });
    const location = page.locator("#configurationInboxLocation");
    await expect(location).toHaveText(/Inbox/);
    await expect(location).toHaveClass(/configuration-inbox-location/);
    const button = page.locator("#configurationInboxOpen");
    await expect(button).toHaveText("Locatie wijzigen");
    await expect(button).toBeEnabled();
    await expect(page.locator("#configurationInboxUnavailable")).toBeHidden();
    await expect(button).toHaveCSS("border-top-color", "rgb(129, 140, 248)");
    await page.route("**/api/configuration/inbox-location/browse", (route) => route.fulfill({
      json: { cancelled: false, value: selectedRoot },
    }));
    await button.click({ force: true });
    await page.locator("#configurationInboxModal").evaluate((element) => {
      if (!element.open) element.showModal();
    });
    await expect(page.locator("#configurationInboxModal .dashboard-modal-shell__panel")).toHaveCSS("border-top-color", "rgb(129, 140, 248)");
    await expect(page.locator("#configurationInboxSave")).toHaveCSS("background-color", "rgb(49, 48, 82)");
    const root = page.locator("#configurationInboxRoot");
    await expect(root).not.toHaveValue(/\/Inbox$/);
    await expect(root).toHaveCSS("width", /px/);
    await expect(root).toHaveCSS("display", "block");
    const browse = page.locator("#configurationInboxBrowse");
    await expect(browse).toHaveText("Lokale map kiezen");
    await browse.click({ force: true });
    await expect(root).toHaveValue(selectedRoot);
    await page.route("**/api/configuration/inbox-location", (route) => route.fulfill({
      json: { key: "inbox_root", value: selectedRoot },
    }));
    const saveRequested = page.waitForRequest((request) => request.url().endsWith("/api/configuration/inbox-location"));
    const saved = page.waitForResponse((response) => response.url().endsWith("/api/configuration/inbox-location"));
    await page.locator("#configurationInboxSave").click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#confirmationModal")).not.toBeVisible();
    await saveRequested;
    expect((await saved).ok()).toBeTruthy();
    await expect(page.locator("#configurationInboxStatus")).not.toBeEmpty();
    await expect(page.locator("#configurationInboxModal")).not.toBeVisible({ timeout: 2_000 });
    await expect(location).toHaveText(/selected-engineering-root\/Inbox$/);
    await expect(page.locator("#configurationInboxStatus")).toHaveClass(/configuration-status--saved/);
    await expect(page.locator("#configurationInboxStatus")).toHaveText(
      DASHBOARD_MESSAGES.nl["configuration.inbox_location_saved"],
    );
  });

  test("opens the displayed Engineering Inbox location through the approved Finder route", async ({ page }) => {
    let requestedDirectory = null;
    await page.route("**/api/open-local-directory", async (route) => {
      requestedDirectory = JSON.parse(route.request().postData()).directory_path;
      await route.fulfill({ status: 202, json: { opened_directory: requestedDirectory } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#queueItems").evaluate((element) => { element.open = true; });
    const location = page.locator("#configurationInboxLocation");
    const inboxPath = await location.textContent();
    await dispatchDashboardPointerClick(location);
    await expect.poll(() => requestedDirectory).toBe(inboxPath?.trim());
  });

  test("shows localized provider login states in Configuration", async ({ page }) => {
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "READY" },
        github: { provider: "GITHUB", state: "AUTH_REQUIRED" },
      },
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    const block = page.locator("#configurationProviderLoginStatus");
    await expect(block).toBeVisible();
    await expect(block).toContainText(DASHBOARD_MESSAGES.nl["configuration.provider_login_status"]);
    await expect(block.locator('[data-provider="CODEX"]')).toContainText(DASHBOARD_MESSAGES.nl["configuration.provider_status.READY"]);
    await expect(block.locator('[data-provider="GITHUB"]')).toContainText(DASHBOARD_MESSAGES.nl["configuration.provider_status.AUTH_REQUIRED"]);
    await expect(block.locator('[data-provider="CODEX"]')).toHaveAttribute("data-provider-state", "READY");
    await expect(block.locator('[data-provider="GITHUB"]')).toHaveAttribute("data-provider-state", "AUTH_REQUIRED");
    const login = block.locator('[data-provider="GITHUB"] [data-provider-repair]');
    await expect(login).toBeVisible();
    await expect(login).toHaveText(DASHBOARD_MESSAGES.nl["notification.provider_readiness.login"].replace("{provider}", "GitHub"));
    await expect(login).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await login.hover();
    await expect(login).toHaveCSS("background-color", "rgb(244, 195, 79)");
    await expect(block.locator('[data-provider="GITHUB"] [data-provider-logout]')).toBeHidden();
  });

  test("uses the compact destructive action contract for provider sign-out", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "READY" },
        github: { provider: "GITHUB", state: "READY" },
      },
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    const row = page.locator('[data-provider="CODEX"]');
    const logout = row.locator("[data-provider-logout]");
    const name = row.locator("strong");
    await expect(name).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(logout).toHaveCSS("min-height", "32px");
    await expect(logout).toHaveCSS("border-top-color", "rgb(255, 120, 153)");
    await expect(logout).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await logout.hover();
    await expect(logout).toHaveCSS("background-color", "rgb(255, 113, 143)");
    await expect(logout).toHaveCSS("color", "rgb(35, 19, 26)");
    const geometry = await row.evaluate((element) => {
      const logoutBox = element.querySelector("[data-provider-logout]")?.getBoundingClientRect();
      const labelBox = element.querySelector(".configuration-provider-status__label")?.getBoundingClientRect();
      return {
        height: element.getBoundingClientRect().height,
        logoutCentre: logoutBox ? logoutBox.top + (logoutBox.height / 2) : null,
        labelCentre: labelBox ? labelBox.top + (labelBox.height / 2) : null,
        dotRight: element.querySelector(".configuration-provider-status__dot")?.getBoundingClientRect().right,
        nameLeft: element.querySelector("strong")?.getBoundingClientRect().left,
      };
    });
    expect(geometry.height).toBeLessThanOrEqual(36);
    expect(geometry.logoutCentre).toBe(geometry.labelCentre);
    expect(geometry.nameLeft - geometry.dotRight).toBeLessThanOrEqual(12);
  });

  test("offers the confirmed compact install action beside an unavailable provider", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    let repairs = 0;
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "UNAVAILABLE" },
        github: { provider: "GITHUB", state: "READY" },
      },
    } }));
    await page.route("**/api/provider-login/repair", async (route) => {
      repairs += 1;
      expect(JSON.parse(route.request().postData() || "{}")).toEqual({ provider: "CODEX", action: "install" });
      await route.fulfill({ status: 202, json: { started: true } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    const row = page.locator('[data-provider="CODEX"]');
    const install = row.locator("[data-provider-repair]");
    await expect(install).toBeVisible();
    await expect(install).toHaveText(DASHBOARD_MESSAGES.nl["notification.provider_readiness.install"].replace("{provider}", "Codex"));
    await expect(row.locator("[data-provider-logout]")).toBeHidden();
    await expect(install).toHaveCSS("min-height", "32px");
    await expect(install).toHaveCSS("font-weight", "400");
    await expect(install).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await install.click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => repairs).toBe(1);
  });

  test("shows a sticky provider repair banner and never reports it as resolved before recheck", async ({ page }) => {
    let readinessCalls = 0;
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "AUTH_REQUIRED" },
        github: { provider: "GITHUB", state: "READY" },
      },
    } }));
    await page.route("**/api/provider-login/repair", async (route) => {
      readinessCalls += 1;
      await route.fulfill({ status: 202, json: { started: true } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    const banner = page.locator("#codexProviderReadinessBanner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.provider_readiness.auth_required"].replace("{provider}", "Codex"));
    await expect(page.locator("#githubProviderReadinessBanner")).toBeHidden();
    await expect(page.locator("#codexProviderReadinessAction")).toHaveText(DASHBOARD_MESSAGES.nl["notification.provider_readiness.login"].replace("{provider}", "Codex"));
    await expect(page.locator("#codexProviderReadinessAction")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(page.locator("#codexProviderReadinessAction")).toHaveCSS("color", "rgb(255, 244, 214)");
    await page.locator("#codexProviderReadinessAction").hover();
    await expect(page.locator("#codexProviderReadinessAction")).toHaveCSS("background-color", "rgb(244, 195, 79)");
    await page.locator("#codexProviderReadinessAction").click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalConfirm").click();
    await page.waitForTimeout(100);
    expect(readinessCalls).toBe(1);
    await expect(banner).toBeVisible();
  });

  test("does not leave an empty provider action button for an indeterminate check", async ({ page }) => {
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "CHECK_FAILED" },
        github: { provider: "GITHUB", state: "READY" },
      },
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await expect(page.locator("#codexProviderReadinessBanner")).toBeVisible();
    await expect(page.locator("#codexProviderReadinessAction")).toBeHidden();
  });

  test("clears a transient provider check failure when the dashboard becomes visible", async ({ page }) => {
    let checks = 0;
    await page.route("**/api/provider-login-status", async (route) => {
      checks += 1;
      if (checks === 1) {
        await route.abort("failed");
        return;
      }
      await route.fulfill({ json: { providers: {
        codex: { provider: "CODEX", state: "READY" },
        github: { provider: "GITHUB", state: "READY" },
      } } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await expect(page.locator("#codexProviderReadinessBanner")).toBeVisible();
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(page.locator("#codexProviderReadinessBanner")).toBeHidden();
    await expect(page.locator("#githubProviderReadinessBanner")).toBeHidden();
    expect(checks).toBeGreaterThanOrEqual(2);
  });

  test("shows each unavailable provider separately and serializes interactive repair", async ({ page }) => {
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "AUTH_REQUIRED" },
        github: { provider: "GITHUB", state: "UNAVAILABLE" },
      },
    } }));
    await page.route("**/api/provider-login/repair", (route) => route.fulfill({ status: 202, json: { started: true } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await expect(page.locator("#codexProviderReadinessBanner")).toBeVisible();
    await expect(page.locator("#githubProviderReadinessBanner")).toBeVisible();
    await page.locator("#codexProviderReadinessAction").click();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#githubProviderReadinessAction")).toBeDisabled();
  });

  test("disables the Inbox location action while the project queue has items", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.evaluate(() => window.queueItems([
      { filename: "waiting-assignment.md", title: "Waiting assignment" },
    ], 1));
    const button = page.locator("#configurationInboxOpen");
    const notice = page.locator("#configurationInboxUnavailable");
    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute("aria-describedby", "configurationInboxUnavailable");
    await expect(notice).toHaveText("Maak de Inbox eerst leeg voordat je de locatie wijzigt.");
  });

  test("shows Inbox watcher confirmation progress and never shows success after a restart failure", async ({ page }) => {
    let completeRequest;
    const requestHeld = new Promise((resolve) => { completeRequest = resolve; });
    await page.route("**/api/configuration/inbox-location", async (route) => {
      await requestHeld;
      await route.fulfill({ status: 503, json: { error_code: "inbox_watcher_restart_failed" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    await page.locator("#configurationInboxOpen").click({ force: true });
    await page.locator("#configurationInboxModal").evaluate((element) => {
      if (!element.open) element.showModal();
    });
    await page.locator("#configurationInboxRoot").evaluate((element) => { element.readOnly = false; });
    await page.locator("#configurationInboxRoot").fill("/private/new-engineering-root");
    await page.locator("#configurationInboxSave").click();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#confirmationModal")).not.toBeVisible();

    await expect(page.locator("#configurationInboxStatus")).toHaveText(
      DASHBOARD_MESSAGES.nl["configuration.inbox_location_restarting"],
    );
    await expect(page.locator("#configurationInboxSave")).toBeDisabled();
    await expect(page.locator("#configurationInboxBrowse")).toBeDisabled();
    completeRequest();
    await expect(page.locator("#configurationInboxStatus")).toHaveText(
      DASHBOARD_MESSAGES.nl["configuration.inbox_location_restart_failed"],
    );
    await expect(page.locator("#configurationInboxStatus")).not.toHaveClass(/configuration-status--saved/);
    await expect(page.locator("#configurationInboxModal")).toBeVisible();
  });

  test("projects local worktrees above open pull requests and refreshes their rows", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      const workspace = document.querySelector("#workspaceCard");
      window.renderWorkspaceWorktrees({ available: true, worktrees: [
        { path: "/workspace", branch: "main", commit: "123456789abc" },
        { path: "/tmp/polish", branch: "codex/polish", commit: "abcdef123456", active: true },
      ] });
      if (!document.querySelector("#workspaceOpenPullRequests")) {
        const pullRequests = document.createElement("section");
        pullRequests.id = "workspaceOpenPullRequests";
        workspace.append(pullRequests);
      }
    });
    const worktrees = page.locator("#workspaceWorktrees");
    await expect(worktrees).toContainText("Lokale worktrees en branches");
    await expect(worktrees).toContainText("codex/polish");
    await expect(worktrees.locator(".workspace-worktrees__active")).toHaveAttribute("aria-label", "Huidige actieve worktree");
    await expect(worktrees).toContainText("/tmp/polish");
    await expect(worktrees.locator(".workspace-worktrees__refresh")).toHaveCount(1);
    await expect(worktrees.locator(".workspace-worktrees__remove")).toHaveCount(0);
    await expect(worktrees.locator(".workspace-worktrees__path--open")).toHaveCount(2);
    await expect(worktrees).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(worktrees.locator("ul")).toHaveCSS("overflow-y", "auto");
    await expect(worktrees.locator("ul").first().locator("li").first()).toHaveCSS("padding-bottom", "16px");
    await expect(worktrees.locator(".workspace-branch-actions")).toContainText("Beoordeel losse lokale branches");
    await expect(worktrees.locator(".workspace-branch-actions")).toContainText("Switch naar FF main");
    expect(await page.evaluate(() => {
      const worktrees = document.querySelector("#workspaceWorktrees");
      const pullRequests = document.querySelector("#workspaceOpenPullRequests");
      return Boolean(worktrees.compareDocumentPosition(pullRequests) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    expect(await page.evaluate(() => {
      const worktrees = document.querySelector("#workspaceWorktrees");
      const actions = worktrees?.querySelector(".workspace-branch-actions");
      return actions?.previousElementSibling?.tagName;
    })).toBe("UL");
    await page.evaluate(() => window.renderWorkspaceWorktrees({ available: true, worktrees: [
      { path: null, branch: "main", commit: "fedcba987654", checked_out: false },
      { path: "/workspace", branch: "codex/refreshed", commit: "fedcba987654" },
    ] }));
    await expect(worktrees).toContainText("main");
    await expect(worktrees).toContainText("Niet lokaal uitgecheckt");
    await expect(worktrees).toContainText("codex/refreshed");
    await expect(worktrees).not.toContainText("codex/polish");
  });

  test("opens a current worktree folder in Finder from its path", async ({ page }) => {
    const projection = { available: true, worktrees: [
      { path: "/tmp/finder-worktree", branch: "codex/finder", commit: "abcdef123456" },
    ] };
    let requestedPath = null;
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { workspace_worktrees: projection } }));
    await page.route("**/api/open-worktree-folder", async (route) => {
      requestedPath = JSON.parse(route.request().postData()).worktree_path;
      await route.fulfill({ status: 202, json: { opened_worktree: requestedPath } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate((fixture) => window.renderWorkspaceWorktrees(fixture), projection);
    await dispatchDashboardPointerClick(page.locator(".workspace-worktrees__path--open"));
    await expect.poll(() => requestedPath).toBe("/tmp/finder-worktree");
  });

  test("opens displayed local workspace and checkout folders through the approved Finder route", async ({ page }) => {
    const requestedDirectories = [];
    await page.route("**/api/open-local-directory", async (route) => {
      requestedDirectories.push(JSON.parse(route.request().postData()).directory_path);
      await route.fulfill({ status: 202, json: { opened_directory: requestedDirectories.at(-1) } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    const workspace = page.locator("#workspaceCard .local-folder-link").first();
    const workspacePath = await workspace.textContent();
    const restingLinkColor = await workspace.evaluate((element) => getComputedStyle(element).color);
    await workspace.hover();
    await expect(workspace).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(workspace).toHaveCSS("color", restingLinkColor);
    await workspace.focus();
    await expect(workspace).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(workspace).toHaveCSS("color", restingLinkColor);
    await dispatchDashboardPointerClick(workspace);
    await expect.poll(() => requestedDirectories).toEqual([workspacePath]);

    await page.evaluate(() => rateLimits({
      provider: "Codex CLI", provider_version: "0.150.1",
      provider_path: "/Users/example/.local/share/engineering-platform/codex-cli",
      windows: [], reset_credits: 0,
    }));
    const installationPath = page.locator("#rateLimitProviderPath");
    await dispatchDashboardPointerClick(installationPath);
    await expect.poll(() => requestedDirectories).toEqual([
      workspacePath,
      "/Users/example/.local/share/engineering-platform/codex-cli",
    ]);

    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      run_id: "folder-route",
      execution_mode: "MANAGED",
      target_repository: "pcvantol/djconnect",
      checkout_path: "/Users/example/Documents/GitHub/djconnect",
      active_branch: "main",
    }, {}));
    const checkout = page.locator("#executionContext .local-folder-link");
    await expect(checkout).toHaveText("/Users/example/Documents/GitHub/djconnect");
    await dispatchDashboardPointerClick(checkout);
    await expect.poll(() => requestedDirectories).toEqual([
      workspacePath,
      "/Users/example/.local/share/engineering-platform/codex-cli",
      "/Users/example/Documents/GitHub/djconnect",
    ]);
  });

  test("analyses every worktree before showing a safe removal action", async ({ page }) => {
    const projection = { available: true, worktrees: [
      { path: "/workspace", branch: "main", commit: "123456789abc" },
      { path: "/tmp/merged", branch: "codex/merged", commit: "abcdef123456" },
      { path: "/tmp/open", branch: "codex/open", commit: "abcdef123457" },
    ] };
    await page.route("**/api/worktree-removal-analysis", (route) => route.fulfill({ json: {
      available: true, worktrees: [
        { path: "/workspace", branch: "main", decision: "baseline", reason: "main_baseline", removable: false },
        { path: "/tmp/merged", branch: "codex/merged", decision: "removable", reason: "safe_to_remove", removable: true, pull_request: { number: 964, url: "https://github.com/pcvantol/djconnect/pull/964", state: "MERGED" } },
        { path: "/tmp/open", branch: "codex/open", decision: "keep", reason: "pull_request_open", removable: false, pull_request: { number: 967, url: "https://github.com/pcvantol/djconnect/pull/967", state: "OPEN" } },
      ],
    } }));
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { workspace_worktrees: projection } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate((fixture) => window.renderWorkspaceWorktrees(fixture), projection);
    const worktrees = page.locator("#workspaceWorktrees");
    await expect(worktrees).toContainText("Analyse is nog niet uitgevoerd.");
    await dispatchDashboardPointerClick(worktrees.locator(".workspace-worktrees__refresh"));
    await expect(worktrees).toContainText("Veilig te verwijderen");
    await expect(worktrees).toContainText("de pull request staat nog open");
    await expect(worktrees.getByRole("link", { name: "Pull request #964 · MERGED" })).toBeVisible();
    await expect(worktrees.getByRole("link", { name: "Pull request #967 · OPEN" })).toBeVisible();
    await expect(worktrees.getByRole("button", { name: "Verwijder worktree" })).toHaveCount(1);
    await page.locator("#themeToggle").click();
    await expect(worktrees.locator(".workspace-worktrees__analysis--keep")).toHaveCSS("color", "rgb(143, 87, 0)");
  });

  test("confirms a safe per-worktree removal in the shared destructive modal", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    // This interaction supplies its own worktree projection. Keep the
    // dashboard's unrelated initial snapshot from replacing that fixture
    // while the confirmation flow is under test.
    const worktreeProjection = { available: true, worktrees: [
      { path: "/workspace", branch: "main", commit: "123456789abc" },
      { path: "/tmp/merged-worktree", branch: "codex/merged-worktree", commit: "abcdef123456" },
    ] };
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { workspace_worktrees: worktreeProjection } }));
    await page.route("**/api/worktree-removal-analysis", (route) => route.fulfill({ json: { available: true, worktrees: [
      { path: "/workspace", branch: "main", decision: "baseline", reason: "main_baseline", removable: false },
      { path: "/tmp/merged-worktree", branch: "codex/merged-worktree", decision: "removable", reason: "safe_to_remove", removable: true, pull_request: { number: 964, url: "https://github.com/pcvantol/djconnect/pull/964", state: "MERGED" } },
    ] } }));
    let removalRequests = 0;
    await page.route("**/api/safe-worktree-removal", async (route) => {
      removalRequests += 1;
      expect(JSON.parse(route.request().postData())).toEqual({
        worktree_path: "/tmp/merged-worktree",
        branch: "codex/merged-worktree",
      });
      await route.fulfill({ status: 202, json: {
        removed_worktree: "/tmp/merged-worktree",
        branch: "codex/merged-worktree",
        branch_pending_cleanup: true,
      } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate((fixture) => window.renderWorkspaceWorktrees(fixture), worktreeProjection);
    await dispatchDashboardPointerClick(page.locator("#workspaceWorktrees .workspace-worktrees__refresh"));

    const remove = page.getByRole("button", { name: "Verwijder worktree" });
    await expect(remove).toHaveCSS("border-color", "rgb(240, 128, 149)");
    expect(readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8")).toContain(
      ".workspace-worktrees .workspace-worktrees__remove:hover:not(:disabled){background:#ff718f!important;border-color:#ff718f!important;color:#23131a!important}",
    );
    const dashboardScript = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.js"), "utf8");
    expect(dashboardScript).toContain("void refreshAfterOperatorAction();");
    expect(dashboardScript).not.toContain("void refresh();");
    await dispatchDashboardPointerClick(remove);
    const confirmation = page.locator("#confirmationModal");
    await expect(confirmation).toBeVisible();
    await expect(page.locator("#confirmationModalTitle")).toHaveText("Veilige worktree verwijderen");
    await expect(page.locator("#confirmationModalText")).toContainText("main is schoon en gesynchroniseerd");
    await expect(page.locator("#confirmationModalText")).toContainText("codex/merged-worktree");
    await expect(page.locator("#confirmationModalText")).toContainText("Engineering Platform controleert opnieuw:");
    expect(await page.locator("#confirmationModalText").textContent()).toContain("\n\nDe branch blijft beschikbaar");
    await expect(page.locator("#confirmationModalConfirm")).toHaveClass(/dashboard-modal-shell__action--destructive/);
    await expect(confirmation.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    await page.locator("#confirmationModalCancel").click();
    expect(removalRequests).toBe(0);

    await dispatchDashboardPointerClick(remove);
    await page.locator("#confirmationModalConfirm").click();
    const result = page.locator("#workspaceBranchMainResultModal");
    await expect(result).toBeVisible();
    await expect(page.locator("#workspaceBranchMainResultTitle")).toHaveText("Worktree verwijderd");
    await expect(result).toContainText("Worktree van codex/merged-worktree verwijderd.");
    expect(removalRequests).toBe(1);
  });

  test("schedules a safe Engineering Platform switch to a registered worktree", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    const projection = { available: true, worktrees: [
      { path: "/workspace", branch: "main", commit: "123456789abc" },
      { path: "/tmp/selected-worktree", branch: "codex/selected-worktree", commit: "abcdef123456" },
    ] };
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { workspace_worktrees: projection } }));
    let switchPayload = null;
    await page.route("**/api/workspace-switch-to-worktree", async (route) => {
      switchPayload = JSON.parse(route.request().postData());
      await route.fulfill({ status: 202, json: {
        branch: "codex/selected-worktree",
        worktree_path: "/tmp/selected-worktree",
        engineering_platform: "restart_scheduled",
      } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate((fixture) => window.renderWorkspaceWorktrees(fixture), projection);

    const switchWorktree = page.getByRole("button", { name: "Schakel naar worktree" });
    await expect(switchWorktree).toBeVisible();
    await dispatchDashboardPointerClick(switchWorktree);
    await expect(page.locator("#confirmationModalTitle")).toHaveText("Naar worktree schakelen");
    await expect(page.locator("#confirmationModalText")).toContainText("de Inbox-queue is leeg");
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => switchPayload).toEqual({
      worktree_path: "/tmp/selected-worktree",
      branch: "codex/selected-worktree",
    });
    await expect(page.locator("#workspaceBranchMainResultModal")).toBeVisible();
    await expect(page.locator("#workspaceBranchMainResultTitle")).toHaveText("Worktree-switch gepland");
    await expect(page.locator("#workspaceBranchMainResultContent")).toContainText(
      "Engineering Platform start opnieuw vanuit codex/selected-worktree.",
    );
  });

  test("hides an acknowledged main switch while the old dashboard document awaits restart", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/workspace-switch-to-main", async (route) => {
      await route.fulfill({ status: 202, json: {
        previous_branch: "codex/polish-workspace-actions",
        branch: "main",
        synchronized: "true",
        engineering_platform: "restart_scheduled",
      } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#workspaceBranchMain").evaluate((element) => { element.hidden = false; });

    const switchMain = page.locator("#workspaceBranchMain");
    await expect(switchMain).toBeVisible();
    await dispatchDashboardPointerClick(switchMain);
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#workspaceBranchMainResultModal")).toBeVisible();
    await expect(switchMain).toBeHidden();
    expect(readFileSync(path.join(repository, "tools/engineering/assets/dashboard.js"), "utf8")).toContain(
      "workspaceMainSwitchScheduled || !workspaceGit.main_action_available",
    );
  });

  test("does not offer a worktree switch for the active worktree", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    const projection = { available: true, worktrees: [
      { path: "/workspace", branch: "main", commit: "123456789abc" },
      { path: "/tmp/current-worktree", branch: "codex/current-worktree", commit: "abcdef123456", active: true },
    ] };
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { workspace_worktrees: projection } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#workspaceCard").evaluate((element) => { element.open = true; });
    await page.evaluate((fixture) => window.renderWorkspaceWorktrees(fixture), projection);

    await expect(page.locator(".workspace-worktrees__active")).toBeVisible();
    await expect(page.getByRole("button", { name: "Schakel naar worktree" })).toHaveCount(0);
  });

  test("keeps project-scoped Inbox settings with the project queue", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const queue = page.locator("#queueItems");
    await queue.evaluate((element) => { element.open = true; });
    for (const id of ["configurationInboxOpen", "configurationInboxScanInterval", "configurationOpenPrInterval"]) {
      await expect(queue.locator(`#${id}`)).toHaveCount(1);
      await expect(page.locator(`#configuration #${id}`)).toHaveCount(0);
    }
    await expect(queue.locator(".queue-project-settings")).toHaveCount(1);
  });

  test("uses normal-weight labels for every dashboard button", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    const weights = await page.locator("button").evaluateAll(
      (buttons) => [...new Set(buttons.map((button) => getComputedStyle(button).fontWeight))],
    );
    expect(weights).toEqual(["400"]);
  });

  test("uses the language pulldown style for every single-choice select", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    for (const id of ["dashboardProject", "configurationLogRetention", "configurationLogLevel", "configurationComponentDetailsInterval", "logLevelFilter"]) {
      const select = page.locator(`#${id}`);
      const picker = select.locator("+ .dashboard-select-picker");
      await expect(picker).toHaveCount(1);
      await expect(picker.locator(".dashboard-locale__button")).toHaveCSS(
        "background-color",
        await page.locator("#dashboardLocaleButton").evaluate((button) => getComputedStyle(button).backgroundColor),
      );
    }
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const picker = page.locator("#configurationLogLevel + .dashboard-select-picker");
    await openDashboardPicker(picker);
    await expect(picker.locator("[role=listbox]")).toBeVisible();
    await expect(picker.locator("[role=option]")).toHaveText(["Informatie", "Debug"]);
  });

  test("does not move focus after a pointer chooses a log-settings pulldown value", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      const originalFocus = HTMLElement.prototype.focus;
      window.__dashboardSelectFocusOptions = [];
      HTMLElement.prototype.focus = function focus(options) {
        if (this.matches(".dashboard-select-picker .dashboard-locale__button")) {
          window.__dashboardSelectFocusOptions.push(options ?? null);
        }
        return originalFocus.call(this, options);
      };
    });
    const picker = page.locator("#configurationLogLevel + .dashboard-select-picker");
    await openDashboardPicker(picker);
    await chooseDashboardPickerOption(picker, "DEBUG");
    expect(await page.evaluate(() => window.__dashboardSelectFocusOptions)).toEqual([]);
    await expect(page.locator("#configuration .configuration-field")).toHaveCount(6);
  });

  test("stacks flat log settings pulldowns below their labels on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const configurationLoaded = page.waitForResponse("**/api/configuration");
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await Promise.all([configurationLoaded, snapshotLoaded]);
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });

    const picker = page.locator("#configurationLogRetention + .dashboard-select-picker");
    const layout = await picker.evaluate((element) => {
      const label = element.parentElement.querySelector(":scope > .label");
      const labelBounds = label.getBoundingClientRect();
      const pickerBounds = element.getBoundingClientRect();
      const buttonStyle = getComputedStyle(element.querySelector("button"));
      return {
        labelBottom: Math.round(labelBounds.bottom),
        pickerTop: Math.round(pickerBounds.top),
        width: Math.round(pickerBounds.width),
        parentWidth: Math.round(element.parentElement.getBoundingClientRect().width),
        backgroundImage: buttonStyle.backgroundImage,
        boxShadow: buttonStyle.boxShadow,
      };
    });
    expect(layout.pickerTop).toBeGreaterThanOrEqual(layout.labelBottom);
    expect(layout.width).toBeLessThanOrEqual(224);
    expect(layout.backgroundImage).toBe("none");
    expect(layout.boxShadow).toBe("none");
  });

  test("keeps the host-detail refresh picker compact and interactive on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#configuration").evaluate((element) => { element.open = true; });

    const picker = page.locator("#configurationComponentDetailsInterval + .dashboard-select-picker");
    await expect(picker).toBeVisible();
    await expect(picker.locator("xpath=../..")).toHaveClass(/configuration-controls/);
    const layout = await picker.evaluate((element) => ({
      pickerWidth: Math.round(element.getBoundingClientRect().width),
      sectionWidth: Math.round(element.closest(".configuration-host-components").getBoundingClientRect().width),
    }));
    expect(layout.pickerWidth).toBeLessThan(layout.sectionWidth);
    await openDashboardPicker(picker);
    await expect(picker.locator("[role=listbox]")).toBeVisible();
    await expect(picker.locator("[role=option]")).toHaveText(["5 seconden", "15 seconden", "30 seconden", "60 seconden"]);
  });

  test("keeps the multi-select event filter inside the Logs card on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await expect(page.locator("#componentLogControls")).not.toHaveAttribute("hidden", "");

    const bounds = await page.locator("#logEventFilter").evaluate((element) => {
      const filter = element.getBoundingClientRect();
      const card = element.closest("#componentLogs").getBoundingClientRect();
      return { cardLeft: Math.round(card.left), cardRight: Math.round(card.right), left: Math.round(filter.left), right: Math.round(filter.right) };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(bounds.cardLeft);
    expect(bounds.right).toBeLessThanOrEqual(bounds.cardRight);
  });

  test("stacks mobile log filters and reveals only the selected date controls", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await expect(page.locator("#componentLogControls")).not.toHaveAttribute("hidden", "");

    const layout = await page.evaluate(() => ["logFilter", "logLevelFilter", "logTimePreset"].map((id) => {
      const input = document.getElementById(id);
      const field = input.closest("label");
      const bounds = field.getBoundingClientRect();
      return { id, top: Math.round(bounds.top), width: Math.round(bounds.width), inputWidth: Math.round(input.getBoundingClientRect().width) };
    }));
    expect(layout.map((item) => item.top)).toEqual([...layout.map((item) => item.top)].sort((a, b) => a - b));
    expect(layout.every((item) => item.inputWidth <= item.width)).toBe(true);
    await expect(page.locator("#logSpecificDateControl")).toBeHidden();
    await expect(page.locator("#logDateFromControl")).toBeHidden();
    await expect(page.locator("#logDateToControl")).toBeHidden();

    await page.locator("#logTimePreset").selectOption("day");
    await expect(page.locator("#logSpecificDateControl")).toBeVisible();
    await expect(page.locator("#logDateFromControl")).toBeHidden();
    await expect(page.locator("#logDateToControl")).toBeHidden();

    await page.locator("#logTimePreset").selectOption("range");
    await expect(page.locator("#logSpecificDateControl")).toBeHidden();
    await expect(page.locator("#logDateFromControl")).toBeVisible();
    await expect(page.locator("#logDateToControl")).toBeVisible();
    const dateBounds = await page.locator("#logDateFrom, #logDateTo").evaluateAll((controls) => controls.map((input) => {
      const field = input.closest("label").getBoundingClientRect();
      const bounds = input.getBoundingClientRect();
      return { left: Math.round(bounds.left), right: Math.round(bounds.right), fieldLeft: Math.round(field.left), fieldRight: Math.round(field.right) };
    }));
    expect(dateBounds.every((item) => item.left >= item.fieldLeft && item.right <= item.fieldRight)).toBe(true);

    for (const preset of ["", "today", "yesterday"]) {
      await page.locator("#logTimePreset").selectOption(preset);
      await expect(page.locator("#logSpecificDateControl")).toBeHidden();
      await expect(page.locator("#logDateFromControl")).toBeHidden();
      await expect(page.locator("#logDateToControl")).toBeHidden();
    }
  });

  test("stacks the Inbox location action below its long path on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const configurationLoaded = page.waitForResponse("**/api/configuration");
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await Promise.all([configurationLoaded, snapshotLoaded]);
    await page.locator("#queueItems").evaluate((element) => { element.open = true; });

    const layout = await page.evaluate(() => {
      const field = document.querySelector(".configuration-inbox-field");
      const location = document.querySelector("#configurationInboxLocation");
      const action = document.querySelector("#configurationInboxOpen");
      const fieldBounds = field.getBoundingClientRect();
      const locationBounds = location.getBoundingClientRect();
      const actionBounds = action.getBoundingClientRect();
      return {
        actionTop: Math.round(actionBounds.top),
        locationBottom: Math.round(locationBounds.bottom),
        actionWidth: Math.round(actionBounds.width),
        fieldWidth: Math.round(fieldBounds.width),
      };
    });

    expect(layout.actionTop).toBeGreaterThanOrEqual(layout.locationBottom);
    expect(layout.actionWidth).toBe(layout.fieldWidth);
  });

  test("checks provider readiness on open and exposes the bounded refresh interval", async ({ page }) => {
    let readinessChecks = 0;
    const writes = [];
    await page.route("**/api/provider-login-status", async (route) => {
      readinessChecks += 1;
      await route.fulfill({ json: { providers: {
        codex: { provider: "CODEX", state: "READY" },
        github: { provider: "GITHUB", state: "READY" },
      } } });
    });
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, telemetry_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
          component_details_refresh_seconds: 5, provider_readiness_refresh_seconds: 300,
        } });
        return;
      }
      writes.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({ json: { key: "provider_readiness_refresh_seconds", previous: 300, value: 600 } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    const select = page.locator("#configurationProviderReadinessInterval");
    await expect(select).toHaveValue("300");
    await expect(select.locator("option[value='300']")).toHaveText("5 minuten");
    await expect(select.locator("xpath=ancestor::section[@id='configurationProviderLoginStatus'][1]")).toBeVisible();
    const providerStatus = page.locator("#configurationProviderLoginStatus");
    const hostComponents = page.locator("#configurationHostComponents");
    expect(await providerStatus.evaluate((element) => getComputedStyle(element).borderTopColor)).toBe(
      await hostComponents.evaluate((element) => getComputedStyle(element).borderTopColor),
    );
    expect(readinessChecks).toBeGreaterThanOrEqual(1);
    await select.selectOption("600");
    await expect.poll(() => writes).toEqual([{
      key: "provider_readiness_refresh_seconds", value: 600, previous: 300,
    }]);
    const saved = select.locator("xpath=ancestor::label[1]").locator(".configuration-field-status");
    await expect(saved).toHaveText(DASHBOARD_MESSAGES.nl["configuration.saved"]);
    await expect(saved).toHaveClass(/configuration-status--saved/);
  });

  test("persists the Codex capacity reserve from Available AI capacity", async ({ page }) => {
    const writes = [];
    // This fixture asserts a deliberately mocked capacity snapshot.  Do not
    // let the live event stream replace it with the test dashboard's own
    // (usually empty) background snapshot while the page is hydrating.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: {}, rate_limits: { provider: "Codex CLI", provider_version: "0.149.0", windows: [{ label: "5-hour window", used_percent: 20, resets_at: 1 }], reset_credits: 0 },
    } }));
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, telemetry_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15, component_details_refresh_seconds: 5,
          provider_readiness_refresh_seconds: 300, codex_capacity_reserve_percent: 0,
        } });
        return;
      }
      writes.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({ json: { key: "codex_capacity_reserve_percent", previous: 0, value: 25 } });
    });
    const configurationLoaded = page.waitForResponse("**/api/configuration");
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await Promise.all([configurationLoaded, snapshotLoaded]);
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await expect(page.locator("#rateLimitDetails")).toContainText("5-hour window");
    const select = page.locator("#configurationCodexCapacityReserve");
    await expect(select).toHaveValue("0");
    await expect(select.locator("option[value='25']")).toHaveText("25% reserve");
    await expect(select.locator("option[value='75']")).toHaveText("75% reserve");
    await expect(select.locator("xpath=ancestor::details[1]")).toHaveAttribute("id", "rateLimits");
    await select.selectOption("25", { force: true });
    await expect.poll(() => writes).toEqual([{
      key: "codex_capacity_reserve_percent", value: 25, previous: 0,
    }]);
    await expect(select.locator("xpath=ancestor::label[1]").locator(".configuration-field-status")).toHaveText(DASHBOARD_MESSAGES.nl["configuration.saved"]);
  });

  test("only offers Codex reserve values that fit the observed remaining capacity", async ({ page }) => {
    // Keep the controlled quota fixture authoritative for this test; the
    // production SSE stream is covered independently by stream tests.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: { watcher_state: "WATCHER_IDLE" }, rate_limits: { windows: [{ label: "5-hour window", used_percent: 53 }] },
    } }));
    await page.route("**/api/configuration", (route) => route.fulfill({ json: {
      log_retention_days: 90, telemetry_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
      open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15, component_details_refresh_seconds: 5,
      provider_readiness_refresh_seconds: 300, codex_capacity_reserve_percent: 0,
    } }));
    const configurationLoaded = page.waitForResponse("**/api/configuration");
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await Promise.all([configurationLoaded, snapshotLoaded]);
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    await expect(page.locator("#rateLimitDetails")).toContainText("5-hour window");
    const select = page.locator("#configurationCodexCapacityReserve");
    await expect(select.locator("option")).toHaveCount(6);
    expect(await select.locator("option").evaluateAll((options) => options.map((option) => option.value))).toEqual(["0", "5", "10", "15", "20", "25"]);
    await expect(select.locator("option[value='50']")).toHaveCount(0);
    await expect(select.locator("option[value='75']")).toHaveCount(0);
  });

  test("shows the capacity reserve banner and hides it immediately after lowering the reserve", async ({ page }) => {
    const writes = [];
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: { watcher_state: "WATCHER_IDLE" },
      rate_limits: { windows: [{ label: "5-hour window", used_percent: 80 }] },
    } }));
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, telemetry_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15, component_details_refresh_seconds: 5,
          provider_readiness_refresh_seconds: 300, codex_capacity_reserve_percent: 25,
        } });
        return;
      }
      writes.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({ json: { key: "codex_capacity_reserve_percent", previous: 25, value: 20 } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body?.classList.contains("dashboard-ready"));
    const banner = page.getByTestId("codex-capacity-reserve-banner");
    const select = page.locator("#configurationCodexCapacityReserve");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.codex_capacity_reserve.title"]);
    await expect(banner).toContainText("20%");
    await expect(banner).toContainText("25%");
    await banner.getByRole("link", { name: DASHBOARD_MESSAGES.nl["notification.codex_capacity_reserve.action"] }).click();
    await expect(page.locator("#rateLimits")).toHaveAttribute("open", "");
    await expect(select).toBeFocused();
    await select.selectOption("20", { force: true });
    await expect.poll(() => writes).toEqual([{
      key: "codex_capacity_reserve_percent", value: 20, previous: 25,
    }]);
    await expect(banner).toBeHidden();
  });

  test("persists a log-level pulldown choice exactly once", async ({ page }) => {
    const writes = [];
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, log_level: "DEBUG", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
          component_details_refresh_seconds: 5,
        } });
        return;
      }
      writes.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({ json: { key: "log_level", previous: "DEBUG", value: "INFO" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const select = page.locator("#configurationLogLevel");
    const picker = select.locator("+ .dashboard-select-picker");
    await expect(picker.locator(".dashboard-locale__button > span").first()).toHaveText("Debug");
    await openDashboardPicker(picker);
    await chooseDashboardPickerOption(picker, "INFO");
    await expect.poll(() => writes).toEqual([{ key: "log_level", value: "INFO", previous: "DEBUG" }]);
    await expect(select).toHaveValue("INFO");
    await expect(picker.locator(".dashboard-locale__button > span").first()).toHaveText("Informatie");
  });

  test("locks the visible log-level pulldown until its saved value is loaded or written", async ({ page }) => {
    let releaseInitialLoad;
    let releaseSave;
    const initialLoad = new Promise((resolve) => { releaseInitialLoad = resolve; });
    const save = new Promise((resolve) => { releaseSave = resolve; });
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await initialLoad;
        await route.fulfill({ json: {
          log_retention_days: 90, log_level: "DEBUG", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
          component_details_refresh_seconds: 5,
        } });
        return;
      }
      await save;
      await route.fulfill({ json: { key: "log_level", previous: "DEBUG", value: "INFO" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const select = page.locator("#configurationLogLevel");
    const picker = select.locator("+ .dashboard-select-picker");
    await expect(select).toBeDisabled();
    await expect(picker.locator(".dashboard-locale__button")).toBeDisabled();
    releaseInitialLoad();
    await expect(select).toBeEnabled();
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await openDashboardPicker(picker);
    await chooseDashboardPickerOption(picker, "INFO");
    await expect(select).toBeDisabled();
    await expect(picker.locator(".dashboard-locale__button")).toBeDisabled();
    releaseSave();
    await expect(select).toBeEnabled();
    await expect(picker.locator(".dashboard-locale__button")).toBeEnabled();
  });

  test("restores the log retention pulldown without saving when its removal warning is cancelled", async ({ page }) => {
    let configurationSaved = false;
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
          component_details_refresh_seconds: 5,
        } });
        return;
      }
      configurationSaved = true;
      await route.fulfill({ json: { key: "log_retention_days", value: 60 } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const retention = page.locator("#configurationLogRetention");
    const picker = retention.locator("+ .dashboard-select-picker");
    await expect(picker.locator(".dashboard-locale__button > span").first()).toHaveText("90 dagen");
    await retention.selectOption("60");
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalCancel").click();
    await expect(retention).toHaveValue("90");
    await expect(picker.locator(".dashboard-locale__button > span").first()).toHaveText("90 dagen");
    expect(configurationSaved).toBe(false);
  });

  test("restores telemetry retention without saving when its data-removal warning is cancelled", async ({ page }) => {
    let configurationSaved = false;
    await page.route("**/api/configuration", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: {
          log_retention_days: 90, telemetry_retention_days: 90, log_level: "INFO", inbox_scan_interval_seconds: 15,
          open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
          component_details_refresh_seconds: 5,
        } });
        return;
      }
      configurationSaved = true;
      await route.fulfill({ json: { key: "telemetry_retention_days", value: 60 } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{ date: "2026-08-25", prompt_count: 1 }]));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    const retention = page.locator("#configurationTelemetryRetention");
    await expect(retention).toHaveValue("90");
    await expect(retention).toBeEnabled();
    const picker = retention.locator("+ .dashboard-select-picker");
    await openDashboardPicker(picker);
    await chooseDashboardPickerOption(picker, "60");
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalCancel").click();
    await expect(retention).toHaveValue("90");
    expect(configurationSaved).toBe(false);
  });

  test("describes telemetry with the actually configured retention period", async ({ page }) => {
    await page.route("**/api/configuration", (route) => route.fulfill({ json: {
      log_retention_days: 90, telemetry_retention_days: 180, log_level: "INFO", inbox_scan_interval_seconds: 15,
      open_pr_check_interval_seconds: 30, platform_health_refresh_seconds: 15,
      component_details_refresh_seconds: 5,
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{ date: "2026-08-25", prompt_count: 1 }]));
    await expect(page.locator("#executionTelemetry .category-description")).toHaveText(
      "Operationele trends van de laatste 180 dagen. Telemetrie is geen repositorybewijs.",
    );
    await expect(page.locator("#configurationTelemetryRetention")).toHaveValue("180");
    await expect(page.locator("#executionTelemetry > #configurationTelemetryRetention")).toHaveCount(0);
    await expect(page.locator("#executionTelemetry > .telemetry-retention")).toHaveCount(1);
    expect(await page.locator("#executionTelemetry > .telemetry-retention").evaluate(
      (retention) => retention.previousElementSibling?.id,
    )).toBe("executionTelemetryPagination");
    await expect(page.locator("#executionTelemetry > .telemetry-retention")).toHaveCSS("border-top-style", "solid");
  });

  test("shows a GitHub rate-limit banner on page load and clears it on refresh", async ({ page }) => {
    let limited = true;
    await page.route("**/api/github-rate-limit", (route) => route.fulfill({
      json: limited ? { limited: true, reset_at: 1_786_162_124 } : { limited: false },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const banner = page.getByTestId("github-rate-limit-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("GitHub-ratelimiet bereikt");
    const refresh = banner.getByRole("button");
    await expect(refresh).toHaveCSS("background-color", "rgb(122, 34, 48)");
    await refresh.hover();
    await expect(refresh).toHaveCSS("background-color", "rgb(169, 43, 64)");
    limited = false;
    await refresh.click();
    await expect(banner).toBeHidden();
  });

  test("shows and monitors the bounded open pull-request check status", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const timerDelay = await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = "<ul></ul>";
      document.body.append(section);
      renderOpenPullRequests([{
        number: 925,
        title: "Check projection",
        url: "https://github.com/pcvantol/djconnect/pull/925",
        branch: "codex/check-projection",
        status: "waiting_for_checks",
        owner_approval: "pending",
      }]);
      const originalSetTimeout = window.setTimeout;
      let delay = null;
      window.setTimeout = (_, value) => {
        delay = value;
        return 1;
      };
      scheduleOpenPullRequestMonitor([{ status: "waiting_for_checks" }]);
      window.setTimeout = originalSetTimeout;
      return delay;
    });
    await expect(page.locator("#workspaceOpenPullRequests")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    const openPullRequestStatus = page.locator("#workspaceOpenPullRequests .open-pr-status");
    await expect(page.locator("#workspaceOpenPullRequests a")).toHaveText("PR #925 — Check projection ↗");
    expect(await page.locator("#workspaceOpenPullRequests li").evaluate((item) =>
      Array.from(item.children).map((element) => element.tagName),
    )).toEqual(["A", "CODE", "SPAN", "SPAN"]);
    await expect(openPullRequestStatus).toHaveClass(/open-pr-status--waiting_for_checks/);
    await expect(openPullRequestStatus).toHaveText("Wacht op afronden van controles");
    await expect(page.locator("#workspaceOpenPullRequests .open-pr-approval")).toHaveText("Owner approval wacht");
    expect(timerDelay).toBe(30_000);
    const allStatusTimerDelays = await page.evaluate(() => {
      const originalSetTimeout = window.setTimeout;
      const delays = [];
      window.setTimeout = (_, value) => {
        delays.push(value);
        return 1;
      };
      scheduleOpenPullRequestMonitor([{ status: "ready_to_merge" }]);
      scheduleOpenPullRequestMonitor([{ status: "issues" }]);
      window.setTimeout = originalSetTimeout;
      return delays;
    });
    expect(allStatusTimerDelays).toEqual([30_000, 30_000]);
    await page.evaluate(() => renderOpenPullRequests([{
      number: 925,
      title: "Check projection",
      url: "https://github.com/pcvantol/djconnect/pull/925",
      branch: "codex/check-projection",
      status: "ready_to_merge",
      owner_approval: "not_required",
    }]));
    await expect(page.locator("#workspaceOpenPullRequests .open-pr-approval")).toHaveText("Owner approval niet vereist");
    await expect(page.locator("#workspaceOpenPullRequests .open-pr-approval")).toHaveClass(/open-pr-approval--not_required/);
    await page.evaluate(() => renderOpenPullRequests([{
      number: 925,
      title: "Check projection",
      url: "https://github.com/pcvantol/djconnect/pull/925",
      branch: "codex/check-projection",
      status: "issues",
    }]));
    await expect(openPullRequestStatus).toHaveClass(/open-pr-status--issues/);
    await expect(openPullRequestStatus).toHaveText("Pull request heeft problemen");
    await page.evaluate(() => renderOpenPullRequests([{
      number: 925,
      title: "Check projection",
      url: "https://github.com/pcvantol/djconnect/pull/925",
      branch: "codex/check-projection",
      status: "branch_update_required",
    }]));
    await expect(openPullRequestStatus).toHaveClass(/open-pr-status--branch_update_required/);
    await expect(openPullRequestStatus).toHaveText("Branch bijwerken vereist");
    await page.evaluate(() => renderOpenPullRequests([{
      number: 925,
      title: "Check projection",
      url: "https://github.com/pcvantol/djconnect/pull/925",
      branch: "codex/check-projection",
      status: "ready_for_review",
    }]));
    await expect(openPullRequestStatus).toHaveClass(/open-pr-status--ready_for_review/);
    await expect(openPullRequestStatus).toHaveText("Klaar voor review");
    await page.evaluate(() => renderOpenPullRequests([{
      number: 925,
      title: "Check projection",
      url: "https://github.com/pcvantol/djconnect/pull/925",
      branch: "codex/check-projection",
      status: "ready_to_merge",
    }]));
    await expect(openPullRequestStatus).toHaveClass(/open-pr-status--ready_to_merge/);
    await expect(openPullRequestStatus).toHaveText("Klaar om te mergen");
    await page.getByTestId("theme-toggle").click();
    await expect(page.locator("#workspaceOpenPullRequests")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(page.locator("#workspaceOpenPullRequests a")).toHaveCSS("color", "rgb(114, 83, 17)");
    await expect(openPullRequestStatus).toHaveCSS("color", "rgb(24, 120, 67)");
  });

  test("manually refreshes every open pull request, including a previously green one", async ({ page }) => {
    let refreshes = 0;
    await page.route("**/api/open-pull-requests", async (route) => {
      refreshes += 1;
      await route.fulfill({ json: { pull_requests: [{
        number: 940, title: "Fresh GitHub state", url: "https://github.com/pcvantol/djconnect/pull/940",
        branch: "codex/ep-dashboard-polish", status: "waiting_for_checks", owner_approval: "not_required",
      }] } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = '<div class="workspace-open-prs__header"><strong>Openstaande pull requests</strong><button id="workspaceOpenPullRequestsRefresh" type="button">↻</button></div><ul></ul>';
      document.body.append(section);
      renderOpenPullRequests([{
        number: 940, title: "Previously green", url: "https://github.com/pcvantol/djconnect/pull/940",
        branch: "codex/ep-dashboard-polish", status: "ready_to_merge", owner_approval: "not_required",
      }]);
    });
    const refresh = page.locator("#workspaceOpenPullRequestsRefresh");
    await expect(refresh).toHaveText("↻");
    const refreshesBeforeClick = refreshes;
    await refresh.click();
    await expect.poll(() => refreshes).toBe(refreshesBeforeClick + 1);
    await expect(page.locator("#workspaceOpenPullRequests .open-pr-status")).toHaveClass(/open-pr-status--waiting_for_checks/);
  });

  test("dispatches owner authorization only after an explicit confirmation", async ({ page }) => {
    let dispatched = null;
    let refreshes = 0;
    await page.route("**/api/open-pull-requests/940/owner-authorization", async (route) => {
      dispatched = { method: route.request().method(), body: route.request().postData() };
      await route.fulfill({ status: 202, json: { queued: true, pull_request: 940 } });
    });
    await page.route("**/api/open-pull-requests", (route) => {
      refreshes += 1;
      return route.fulfill({ json: { pull_requests: [] } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = "<ul></ul>";
      document.body.append(section);
      renderOpenPullRequests([{
        number: 940,
        title: "High-risk delivery",
        url: "https://github.com/pcvantol/djconnect/pull/940",
        branch: "codex/ep-dashboard-polish",
        status: "waiting_for_checks",
        owner_approval: "pending",
        owner_authorization_requested: true,
      }]);
    });

    const authorize = page.locator("[data-open-pull-request-owner-authorization='940']");
    await expect(authorize).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.authorize_owner"]);
    await expect(authorize).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    await authorize.click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await expect(page.locator("#confirmationModal")).toHaveClass(/dashboard-modal-shell--owner-authorization/);
    await expect(page.locator("#confirmationModal .confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    expect(dispatched).toBeNull();
    const refreshesBeforeAuthorization = refreshes;
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => dispatched).toEqual({ method: "POST", body: "{}" });
    await expect.poll(() => refreshes).toBeGreaterThan(refreshesBeforeAuthorization);
    expect(readFileSync(path.join(repository, "tools/engineering/assets/dashboard.js"), "utf8"))
      .toContain("for (const delay of [900, 2500, 6000, 12000])");
  });

  test("queues one explicit repair for terminal failed pull-request checks", async ({ page }) => {
    let dispatched = null;
    await page.route("**/api/open-pull-requests/941/repair-failed-checks", async (route) => {
      dispatched = { method: route.request().method(), body: route.request().postData() };
      await route.fulfill({ status: 202, json: { queued: true, pull_request: 941 } });
    });
    await page.route("**/api/open-pull-requests", (route) => route.fulfill({ json: { pull_requests: [] } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.style.setProperty("--category-color", "#c7a6ff");
      section.innerHTML = "<ul></ul>";
      document.body.append(section);
      renderOpenPullRequests([{
        number: 941, title: "Human submitted", url: "https://github.com/pcvantol/djconnect/pull/941",
        branch: "feature/human-pr", status: "issues", owner_approval: "not_required",
        check_repair_available: true,
        failed_checks: ["Engineering Platform validation / validate", "Validate Home Assistant custom integration / validate / tests"],
      }]);
    });
    const repair = page.locator("[data-open-pull-request-check-repair='941']");
    await expect(repair).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.repair_failed_checks"]);
    await expect(repair).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    await expect(repair).toHaveCSS("padding-top", "9px");
    await expect(repair).toHaveCSS("font-size", "13px");
    await expect(page.locator("#workspaceOpenPullRequests ul")).toHaveCSS("overflow-y", "auto");
    await expect(page.locator("#workspaceOpenPullRequests li")).toHaveCSS("padding-bottom", "16px");
    await expect(page.locator("#workspaceOpenPullRequests li")).toHaveCSS("border-bottom-style", "solid");
    await repair.click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await expect(page.locator("#confirmationModal")).toHaveClass(/dashboard-modal-shell--check-repair/);
    await expect(page.locator("#confirmationModal")).not.toHaveClass(/dashboard-modal-shell--destructive/);
    await expect(page.locator(".confirmation-modal__list-label")).toHaveText(
      DASHBOARD_MESSAGES.nl["workspace.open_pull_request.repair_failed_checks_list"],
    );
    await expect(page.locator(".confirmation-modal__list li")).toHaveCount(2);
    await expect(page.locator(".confirmation-modal__list")).toContainText("Engineering Platform validation / validate");
    await expect(page.locator(".confirmation-modal__list")).toContainText("Validate Home Assistant custom integration / validate / tests");
    expect(dispatched).toBeNull();
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => dispatched).toEqual({ method: "POST", body: "{}" });
  });

  test("shows persisted repair progress and a link to the active GitHub checks", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = "<ul></ul>";
      document.body.append(section);
      renderOpenPullRequests([{
        number: 941, title: "Human submitted", url: "https://github.com/pcvantol/djconnect/pull/941",
        branch: "feature/human-pr", status: "waiting_for_checks", owner_approval: "not_required",
        check_repair_available: false, check_repair_state: "SUBMITTED",
      }]);
    });
    const progress = page.locator(".open-pr-check-repair-progress");
    await expect(progress).toContainText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.repair_active"]);
    await expect(progress.locator("a")).toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/941/checks");
    await expect(progress.locator(".open-pr-check-repair-progress__spinner")).toBeVisible();
  });

  test("keeps the repair button visible but disabled after its one focused repair", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = "<ul></ul>";
      document.body.append(section);
      renderOpenPullRequests([{
        number: 941, title: "Human submitted", url: "https://github.com/pcvantol/djconnect/pull/941",
        branch: "feature/human-pr", status: "issues", owner_approval: "not_required",
        check_repair_available: false, check_repair_completed_for_head: true,
      }]);
    });
    const repair = page.locator(".open-pr-check-repair");
    await expect(repair).toBeDisabled();
    await expect(repair).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.repair_completed"]);
    await expect(repair).toHaveAttribute("title", DASHBOARD_MESSAGES.nl["workspace.open_pull_request.repair_completed_explanation"]);
  });

  test("keeps the last known open pull requests visible when GitHub refresh is unavailable", async ({ page }) => {
    await page.route("**/api/open-pull-requests", (route) => route.fulfill({ status: 503, json: { error: "temporarily unavailable" } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const section = document.createElement("section");
      section.id = "workspaceOpenPullRequests";
      section.className = "workspace-open-prs";
      section.innerHTML = '<div class="workspace-open-prs__header"><strong>Openstaande pull requests</strong><button id="workspaceOpenPullRequestsRefresh" type="button">↻</button></div><ul></ul>';
      document.body.append(section);
      renderOpenPullRequests([{
        number: 940, title: "Last known pull request", url: "https://github.com/pcvantol/djconnect/pull/940",
        branch: "codex/ep-dashboard-polish", status: "waiting_for_checks", owner_approval: "not_required",
      }]);
    });
    await page.locator("#workspaceOpenPullRequestsRefresh").click();
    await expect(page.locator("#workspaceOpenPullRequests a")).toHaveText("PR #940 — Last known pull request ↗");
  });

  test("translates every operational phase and status in every supported locale", () => {
    for (const locale of SUPPORTED_LOCALES) {
      for (const key of OPERATIONAL_TRANSLATION_KEYS) {
        expect(DASHBOARD_MESSAGES[locale][key], `${locale}:${key}`).toBeTruthy();
      }
    }
  });

  test("translates reviewer and operational machine codes in every supported locale", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await waitForDashboardReady(page);
      await selectDashboardLocale(page, language);
      await page.locator("#autoRefresh").uncheck();
      await page.evaluate(() => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE",
        run_id: "localized-operational-codes",
        current_phase: "CAPABILITY_REVIEW",
        current_action: "poll_required_checks",
        reviewer_agents: [{
          reviewer: "HOME_ASSISTANT_INTEGRATION", capability: "ENGINEERING", status: "running",
        }],
      }, {}));
      await expect(page.locator("#action")).toHaveText(
        DASHBOARD_MESSAGES[language]["operational.poll_required_checks"],
      );
      await expect(page.locator(".reviewer-agent__name")).toHaveText(
        DASHBOARD_MESSAGES[language]["reviewer.home_assistant_integration"],
      );
      await page.evaluate(() => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE",
        run_id: "localized-operational-codes",
        current_action: "reconcile_rolling_records_on_main",
      }, {}));
      await expect(page.locator("#action")).toHaveText(
        DASHBOARD_MESSAGES[language]["operational.reconcile_rolling_records_on_main"],
      );
    }
  });

  test("translates the owner-authorization control in every supported locale", () => {
    const keys = [
      "workspace.open_pull_request.authorize_owner",
      "workspace.open_pull_request.authorize_owner_confirmation",
      "workspace.open_pull_request.owner_authorization_queued",
      "workspace.open_pull_request.owner_authorization_qualification_pending",
    ];
    for (const locale of SUPPORTED_LOCALES) {
      for (const key of keys) expect(DASHBOARD_MESSAGES[locale][key], `${locale}:${key}`).toBeTruthy();
    }
  });

  test("lists English first in both language selectors", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardLocaleButton").click();
    expect(await page.locator("#dashboardLocale option").evaluateAll(
      (options) => options.map((option) => option.value),
    )).toEqual(["en", "nl", "de", "fr", "es"]);
    expect(await page.locator("[data-dashboard-locale]").evaluateAll(
      (options) => options.map((option) => option.dataset.dashboardLocale),
    )).toEqual(["en", "nl", "de", "fr", "es"]);
  });

  test("shows only the custom language pulldown", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#dashboardLocale")).toBeHidden();
    await expect(page.locator("#dashboardLocaleButton")).toBeVisible();
    await expect(page.locator("#dashboardLocale + .dashboard-select-picker")).toHaveCount(0);
  });

  test("renders repository and workspace state codes as readable labels", () => {
    const labels = Object.fromEntries(SUPPORTED_LOCALES.map((locale) => {
      const translate = createTranslator(locale);
      return [locale, [
        translate("state.MERGED_RECONCILED"),
        translate("state.WORKSPACE_READY"),
        translate("state.WAIT_FOR_TERMINAL_EVIDENCE"),
      ]];
    }));
    expect(labels).toEqual({
      en: ["Merged and reconciled", "Workspace ready", "Waiting for final evidence"],
      nl: ["Samengevoegd en afgestemd", "Werkruimte gereed", "Wacht op afrondend bewijs"],
      de: ["Zusammengeführt und abgeglichen", "Arbeitsbereich bereit", "Warten auf abschließenden Nachweis"],
      fr: ["Fusionné et rapproché", "Espace de travail prêt", "En attente d’une preuve finale"],
      es: ["Fusionado y conciliado", "Espacio de trabajo listo", "Esperando evidencia final"],
    });
  });

  test("locks the iOS viewport scale to prevent input-focus zoom", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator('meta[name="viewport"]')).toHaveAttribute(
      "content",
      "width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover",
    );
  });

  test("uses catalogued copy for every UI label in every supported language", async ({ page }) => {
    test.slow();
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "IDLE", queue_depth: 0 } },
    }));
    const dashboardSource = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.js"),
      "utf8",
    );
    const staticallyRequestedKeys = [
      ...dashboardSource.matchAll(/\bt\(\s*["']([^"']+)["']\s*(?:,|\))/g),
    ].map((match) => match[1]);

    for (const language of SUPPORTED_LOCALES) {
      const sourceTranslator = createTranslator(language);
      for (const key of staticallyRequestedKeys) {
        expect(
          Object.hasOwn(DASHBOARD_MESSAGES[language], key),
          `${language} is missing the statically requested UI label: ${key}`,
        ).toBe(true);
      }

      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(
        () => typeof window.__djconnectDashboardLocalizationCalls === "function",
      );
      await page.waitForFunction(
        () => document.body.classList.contains("dashboard-ready"),
      );
      // Change language only after the asynchronous snapshot has rendered;
      // otherwise it may overwrite localized template placeholders.
      // A locale selection deliberately reloads the dashboard so persisted
      // client state is applied from a clean document. Wait for that specific
      // navigation before inspecting template bindings; otherwise a fast CI
      // worker can read the outgoing document while its localized text is
      // being replaced.
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page).toHaveTitle(sourceTranslator("dashboard.title"));
      await expect(page.locator("#dashboardAppleWebAppTitle")).toHaveAttribute(
        "content",
        sourceTranslator("dashboard.title"),
      );

      const templateBindings = await page.locator(
        "[data-i18n], [data-i18n-placeholder], [data-i18n-aria-label], [data-i18n-title]",
      ).evaluateAll((elements) => elements.flatMap((element) => [
        element.dataset.i18n && {
          key: element.dataset.i18n,
          property: "textContent",
          // Some localized labels contain a separately translated info control.
          // Validate the label's own text rather than concatenating that control's
          // visible "i" glyph onto it.
          value: [...element.childNodes]
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => node.textContent)
            .join("")
            .trim() || element.textContent,
        },
        element.dataset.i18nPlaceholder && {
          key: element.dataset.i18nPlaceholder,
          property: "placeholder",
          value: element.getAttribute("placeholder"),
        },
        element.dataset.i18nAriaLabel && {
          key: element.dataset.i18nAriaLabel,
          property: "aria-label",
          value: element.getAttribute("aria-label"),
        },
        element.dataset.i18nTitle && {
          key: element.dataset.i18nTitle,
          property: "title",
          value: element.getAttribute("title"),
        },
      ].filter(Boolean)));
      for (const binding of templateBindings.filter(
        ({ key }) => ![
          "format.loading",
          "format.unavailable",
          "logs.loading",
          "estimate.not_available",
          // Log tables replace this template accessibility name with their
          // component-specific localized name at runtime.
          "history.table_label",
          "logs.inbox_watcher",
          "logs.status_dashboard",
          // The indicator's accessible name is deliberately enriched at runtime
          // with the resolved status, e.g. "Prompt status: complete".
          "status.unknown",
        ].includes(key),
      )) {
        expect(
          binding.value,
          `${language}:${binding.key} must update the template ${binding.property}`,
        ).toBe(sourceTranslator(binding.key));
      }

      const calls = await page.evaluate(() =>
        window.__djconnectDashboardLocalizationCalls(),
      );
      expect(calls.length, `${language} should render localized dashboard copy`).toBeGreaterThan(0);

      for (const { key, values, fallback, text } of calls) {
        if (!Object.hasOwn(DASHBOARD_MESSAGES[language], key) && fallback !== key) {
          expect(
            text,
            `${language}:${key} must preserve its explicit data fallback`,
          ).toBe(fallback);
          continue;
        }
        expect(
          Object.hasOwn(DASHBOARD_MESSAGES[language], key),
          `${language} is missing the UI label used by the dashboard: ${key}`,
        ).toBe(true);
        expect(
          DASHBOARD_MESSAGES[language][key],
          `${language}:${key} must not be empty`,
        ).toBeTruthy();
        expect(
          text,
          `${language}:${key} differs from its source-catalogue value`,
        ).toBe(sourceTranslator(key, values, fallback));
      }
    }
  });

  test("enforces the source-to-interface localization contract in all five languages", async ({ page }) => {
    const dashboardSource = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.js"),
      "utf8",
    );
    const staticPresentationLiterals = [
      ...dashboardSource.matchAll(/(?:\.textContent|\.title)\s*=\s*(["'])(.*?)\1/g),
    ].map((match) => match[2]);

    // Visible words must come from t(). The remaining literals are deliberate
    // control glyphs, empty cleanup values, or the neutral empty-table mark.
    expect(new Set(staticPresentationLiterals)).toEqual(new Set([
      "", "⧉", "↓", "↑", "i", "×", "↺", "↻", "⌧", "▤", "✓", "✦", "◉", "⋯", "—", "⌄",
    ]));
    expect(dashboardSource).not.toMatch(/confirmDashboardAction\(\s*["']/);
    // Dashboard feedback must remain inside the shared modal system.  A
    // browser-native alert is unstyled, untranslated and blocks mobile UX.
    expect(dashboardSource).not.toMatch(/\b(?:window\.)?alert\s*\(/);

    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await page.waitForFunction(() => typeof window.chatMessage === "function");
      await page.evaluate(() => {
        document.querySelector("#chatMessages").replaceChildren();
        chatMessage("assistant", "Localized assistant reply");
      });

      const message = page.locator("#chatMessages .chat-message--assistant");
      await expect(message.locator(".chat-message__role")).toHaveText(
        DASHBOARD_MESSAGES[language]["chat.assistant"],
      );
      await expect(message.locator(".chat-message__copy")).toHaveAttribute(
        "title",
        DASHBOARD_MESSAGES[language]["copy.message"],
      );
      await expect(message.locator(".chat-message__copy")).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["copy.message"],
      );

      await page.evaluate(() => {
        updatePullRefresh(80);
        document.querySelector("#promptHistoryChatModal").showModal();
        document.querySelector("#copyChat").hidden = false;
        document.querySelector("#clearChat").hidden = false;
      });
      await expect(page.getByTestId("pull-refresh")).toHaveText(
        DASHBOARD_MESSAGES[language]["refresh.release_to_refresh"],
      );
      await expect(page.getByTestId("page-refresh")).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["refresh.page"],
      );
      await expect(page.getByTestId("page-refresh")).toHaveText("↻");
      await expect(page.locator("#copyChat")).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["chat.copy_title"],
      );
      await expect(page.locator("#componentLogs .log-table").first()).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["logs.inbox_entries"],
      );
      await expect(page.locator("#componentLogs .log-table").nth(1)).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["logs.dashboard_entries"],
      );
      await expect(page.getByTestId("copy-inbox-visible-log")).toHaveAttribute(
        "aria-label",
        DASHBOARD_MESSAGES[language]["logs.copy_visible"],
      );
      await page.locator("#clearChat").click();
      await expect(page.locator("#confirmationModalTitle")).toHaveText(
        DASHBOARD_MESSAGES[language]["chat.clear_title"],
      );
      await expect(page.locator("#confirmationModalText")).toHaveText(
        DASHBOARD_MESSAGES[language]["chat.clear_description"],
      );
      await page.keyboard.press("Escape");
      await page.locator("#promptHistoryChatModal").evaluate((modal) => modal.close());
    }
    expect(dashboardSource).toContain(
      '$("pageRefresh")?.addEventListener("click", refreshDashboard)',
    );
  });

  test("copies chat text synchronously for iOS Safari", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => typeof window.chatMessage === "function");
    await page.evaluate(() => {
      window.__copyFallbackCalls = 0;
      window.__clipboardCalls = 0;
      document.execCommand = (command) => {
        if (command === "copy") {
          window.__copyFallbackCalls += 1;
          window.__copyHost = document.activeElement.closest("dialog")?.id;
        }
        return command === "copy";
      };
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () => {
            window.__clipboardCalls += 1;
            return Promise.resolve();
          },
        },
      });
      Object.defineProperty(navigator, "platform", {
        configurable: true,
        value: "iPhone",
      });
      document.querySelector("#promptHistoryChatModal").showModal();
      chatMessage("assistant", "Copy this iOS-safe message");
    });

    await page.locator("#chatMessages .chat-message__copy").click();
    await expect(page.locator("#copyToast")).toHaveText(
      DASHBOARD_MESSAGES.nl["copy.success"],
    );
    await expect.poll(() => page.evaluate(() => ({
      fallback: window.__copyFallbackCalls,
      clipboard: window.__clipboardCalls,
      host: window.__copyHost,
    }))).toEqual({
      fallback: 1,
      clipboard: 0,
      host: "promptHistoryChatModal",
    });
  });

  test("copies the complete chat conversation", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      window.__copiedChat = "";
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: (value) => { window.__copiedChat = value; return Promise.resolve(); } },
      });
      chatHistory = [
        { role: "user", text: "First question" },
        { role: "assistant", text: "First answer" },
      ];
      renderChatHistory();
      document.querySelector("#promptHistoryChatModal").showModal();
    });
    await page.locator("#copyChat").click();
    await expect.poll(() => page.evaluate(() => window.__copiedChat)).toContain("First question");
    await expect.poll(() => page.evaluate(() => window.__copiedChat)).toContain("First answer");
  });

  test("uses the Clipboard API before the legacy fallback in modern browsers", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => typeof window.chatMessage === "function");
    await page.evaluate(() => {
      window.__copyFallbackCalls = 0;
      window.__clipboardCalls = 0;
      document.execCommand = () => {
        window.__copyFallbackCalls += 1;
        return true;
      };
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () => {
            window.__clipboardCalls += 1;
            return Promise.resolve();
          },
        },
      });
      document.querySelector("#promptHistoryChatModal").showModal();
      chatMessage("assistant", "Copy this browser message");
    });

    await page.locator("#chatMessages .chat-message__copy").click();
    await expect.poll(() => page.evaluate(() => ({
      fallback: window.__copyFallbackCalls,
      clipboard: window.__clipboardCalls,
    }))).toEqual({ fallback: 0, clipboard: 1 });
  });

  test("places the active prompt category first", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(
      page.locator("#engineering-dashboard-content").evaluate(
        (dashboard) => dashboard.firstElementChild?.id,
      ),
    ).resolves.toBe("currentRun");
  });

  test("opens execution details from prompt history in its dedicated modal", async ({ page }) => {
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: "inbox-modal", status: "BLOCKED", title: "Modal prompt", executed_at: "2026-08-04T08:00:00Z",
    }] } }));
    await page.route("**/api/prompt-history/inbox-modal/details", (route) => route.fulfill({
      json: {
        history: {
          run_id: "inbox-modal",
          status: "BLOCKED",
          title: "Modal prompt",
          executed_at: "2026-08-04T08:00:00Z",
          dismissed: true,
          dismissed_at: "2026-08-27T14:08:24.218289+00:00",
          blocking_reason: "The verified blocking reason belongs to this run.",
        },
        execution: { seconds: 42, total_seconds: 61 },
        evidence: ["Execution Host: Engineering Platform"],
        pull_requests: [
          { role: "implementation", number: 948, url: "https://github.com/pcvantol/djconnect/pull/948", commit_count: 2, check_count: 1, changed_file_count: 5 },
          { role: "finalization", number: 949, url: "https://github.com/pcvantol/djconnect/pull/949", commit_count: 1, check_count: 3, changed_file_count: 2 },
        ],
        commit_timeline: [{
          phase: "FINALIZE_AGENT",
          observed_at: "2026-08-04T08:01:00Z",
          commit_sha: "a".repeat(40),
          description: "finalization_commit_verified",
        }],
      },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
    });

    const historyRow = page.locator("#promptHistoryRows .prompt-history-row");
    await historyRow.waitFor({ state: "visible" });
    await dispatchDashboardPointerClick(historyRow);
    await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    await expect(page.locator("#promptHistoryDetailModal")).toHaveClass(/dashboard-modal-shell--evidence/);
    await expect(page.locator("#promptHistoryDetailModal .prompt-detail-modal__panel")).toHaveClass(/dashboard-modal-shell__panel/);
    await expect(page.locator("#promptHistoryDetailModal .prompt-detail-modal__header")).toHaveClass(/dashboard-modal-shell__header/);
    await expect(page.locator("#promptHistoryDetailDescription")).toHaveCSS("border-bottom-color", "rgb(141, 199, 255)");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Engineering Platform");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Blokkadereden");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("The verified blocking reason belongs to this run.");
    const localizedDismissedAt = await page.evaluate(
      () => formatTimestamp("2026-08-27T14:08:24.218289+00:00"),
    );
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Afgesloten op");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText(localizedDismissedAt);
    await expect(page.locator("#promptHistoryDetailContent"))
      .not.toContainText("2026-08-27T14:08:24.218289+00:00");
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-card--pull-requests")).toContainText("Commits: 2");
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-card--pull-requests")).toContainText("GitHub-controles: 1");
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-card--pull-requests")).toContainText("Gewijzigde bestanden: 5");
    const markdown = page.locator("#promptHistoryDetailDownloadMarkdown");
    const json = page.locator("#promptHistoryDetailDownloadJson");
    await expect(markdown).toHaveAttribute("aria-label", "Uitvoeringsdetails als Markdown downloaden voor Modal prompt");
    await expect(json).toHaveAttribute("aria-label", "Uitvoeringsdetails als JSON downloaden voor Modal prompt");
    await expect(json).toHaveText("{}");
    const markdownDownload = page.waitForEvent("download");
    await markdown.click();
    const downloadedMarkdown = await markdownDownload;
    expect(downloadedMarkdown.suggestedFilename()).toBe("execution-details-inbox-modal.md");
    const markdownContent = readFileSync(await downloadedMarkdown.path(), "utf8");
    expect(markdownContent).toContain("# Modal prompt");
    expect(markdownContent).toContain("## Uitvoering");
    expect(markdownContent).toContain("| Veld | Waarde |");
    expect(markdownContent).toContain("Blokkadereden");
    expect(markdownContent).toContain("The verified blocking reason belongs to this run.");
    expect(markdownContent).toContain("## Pull requests");
    expect(markdownContent).toContain("[#948](https://github.com/pcvantol/djconnect/pull/948)");
    expect(markdownContent).toContain("[#949](https://github.com/pcvantol/djconnect/pull/949)");
    expect(markdownContent).toContain("## Geverifieerde commit-tijdlijn");
    expect(markdownContent).toContain("`" + "a".repeat(40) + "`");
    expect(markdownContent).toContain("Finalisatiecommit geverifieerd");
    expect(markdownContent).not.toContain("```json");
    const jsonDownload = page.waitForEvent("download");
    await json.click();
    const downloadedJson = await jsonDownload;
    expect(downloadedJson.suggestedFilename()).toBe("execution-details-inbox-modal.json");
    const jsonContent = JSON.parse(readFileSync(await downloadedJson.path(), "utf8"));
    expect(jsonContent.history.run_id).toBe("inbox-modal");
    expect(jsonContent.pull_requests).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: "implementation", number: 948 }),
      expect.objectContaining({ role: "finalization", number: 949 }),
    ]));
    expect(jsonContent.commit_timeline).toEqual(expect.arrayContaining([
      expect.objectContaining({ phase: "FINALIZE_AGENT", commit_sha: "a".repeat(40) }),
    ]));
    await expect(page.locator("dialog[open]")).toHaveCount(1);
  });

  test("renders terminal status recovery as a historical detail card", async ({ page }) => {
    const runId = "inbox-status-recovery";
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: runId, status: "BLOCKED", title: "Status recovery", executed_at: "2026-08-17T05:42:00Z",
    }] } }));
    await page.route(`**/api/prompt-history/${runId}/details`, (route) => route.fulfill({ json: {
      history: { run_id: runId, status: "BLOCKED", title: "Status recovery" },
      lifecycle: {
        run_id: runId,
        available: true,
        terminal_state: "BLOCKED",
        recovery: { kind: "status_reconciliation", run_id: runId },
        steps: [],
      },
    } }));

    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    await dispatchDashboardPointerClick(page.locator("#promptHistoryRows .prompt-history-row"));

    const card = page.locator("#promptHistoryDetailContent .status-reconciliation-card");
    await expect(card).toBeVisible();
    await expect(card).toHaveClass(/prompt-detail-card/);
    await expect(card).not.toHaveClass(/operator-merge-wait/);
    await expect(card.locator("h3")).toHaveText(DASHBOARD_MESSAGES.nl["status_reconciliation.title"]);
    const lifecycle = page.locator("#promptHistoryDetailContent .execution-lifecycle--historical");
    await expect(lifecycle).toHaveCSS("background-color", "rgb(36, 36, 45)");
    await expect(lifecycle.locator("h3")).toHaveCSS("font-size", "18px");
  });

  test("opens, closes and navigates prompt-history deeplinks without reloading", async ({ page }) => {
    const runId = "inbox-deeplink";
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: runId, status: "COMPLETE", title: "Deeplink prompt", executed_at: "2026-08-04T08:00:00Z",
    }] } }));
    await page.route(`**/api/prompt-history/${runId}/details`, (route) => route.fulfill({ json: {
      history: { run_id: runId, status: "COMPLETE", title: "Deeplink prompt" }, execution: {}, evidence: [],
    } }));

    await page.goto(`${dashboardUrl}/?prompt=${runId}`, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal");
    await expect(modal).toBeVisible();
    await expect(page.locator("#promptHistoryDetailTitle")).toHaveText("Deeplink prompt");
    await expect(page).toHaveURL(new RegExp(`\\?prompt=${runId}$`));
    await expect(page.locator("#promptHistoryRows .prompt-history-open-link, #promptHistoryRows .prompt-history-copy-link")).toHaveCount(0);
    await expect(modal.locator(".prompt-history-run-id-copy")).toHaveAttribute(
      "aria-label", DASHBOARD_MESSAGES.nl["history.copy_link"].replace("{title}", runId),
    );

    await page.locator("#promptHistoryDetailClose").click();
    await expect(modal).not.toBeVisible();
    await expect(page).toHaveURL(dashboardUrl);
    await page.goBack();
    await expect(modal).toBeVisible();
    await page.goForward();
    await expect(modal).not.toBeVisible();
  });

  test("normalizes an unknown prompt-history deeplink without opening a modal", async ({ page }) => {
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: "inbox-known", status: "COMPLETE", title: "Known prompt",
    }] } }));

    await page.goto(`${dashboardUrl}/?prompt=unknown-run`, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    await expect(page).toHaveURL(dashboardUrl);
  });

  test("uses one uninterrupted category-colour selected-row treatment for prompt history on touch devices", async ({ page }) => {
    // Keep the client-side fixture stable: the initial history refresh can
    // otherwise replace this row after it has been rendered in CI.
    await page.route("**/api/events", (route) => route.abort());
    // A non-empty initial response prevents the production empty-history retry
    // from re-rendering this isolated fixture after it has been seeded below.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    await page.route("**/api/prompt-history/inbox-row-focus/details", (route) => route.fulfill({
      json: { history: { run_id: "inbox-row-focus", status: "COMPLETE", title: "Focused row" } },
    }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-row-focus",
        status: "COMPLETE",
        title: "Focused row",
        executed_at: "2026-08-04T08:00:00Z",
      }];
      renderPromptHistory();
    });
    const row = page.locator("#promptHistoryRows .prompt-history-row");
    await dispatchDashboardPointerClick(row);
    await expect(row).toHaveAttribute("data-selected", "true");
    const selection = await row.locator("td").evaluateAll((cells) => [
      getComputedStyle(cells[0]).boxShadow,
      getComputedStyle(cells[Math.floor(cells.length / 2)]).boxShadow,
      getComputedStyle(cells.at(-1)).boxShadow,
      getComputedStyle(cells[0]).backgroundColor,
      getComputedStyle(cells[0]).outlineStyle,
      getComputedStyle(cells[Math.floor(cells.length / 2)]).outlineStyle,
      getComputedStyle(cells.at(-1)).outlineStyle,
    ]);
    expect(selection[0]).toContain("2px 0px 0px 0px inset");
    expect(selection[1]).toBe("none");
    expect(selection[2]).toBe("none");
    expect(selection[3]).not.toBe("rgba(0, 0, 0, 0)");
    expect(selection.slice(4)).toEqual(["none", "none", "none"]);
    await page.locator("#promptHistoryDetailClose").click();
    await page.locator("#promptHistoryFilter").fill("Focused");
    await expect(row).toHaveAttribute("data-selected", "false");
  });

  test("prevents iOS long-press selection on prompt-history rows", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    const script = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.js"),
      "utf8",
    );
    expect(styles).toContain(".prompt-history-row,.prompt-history-row *{-webkit-touch-callout:none;-webkit-user-select:none;touch-action:manipulation;user-select:none}");
    expect(script).toContain('row.addEventListener("contextmenu", (event) => event.preventDefault());');
    expect(script).toContain('row.addEventListener("selectstart", (event) => event.preventDefault());');
  });

  test("keeps sortable table headers opaque on iOS", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).toContain("#engineering-dashboard-content .log-table th{");
    expect(styles).toContain("#engineering-dashboard-content .log-table th:is([tabindex],[role=\"button\"]):active{");
  });

  test("keeps execution context in one column at every viewport width", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).not.toMatch(/\.execution-context--primary\{columns:/);
    expect(styles).not.toContain(".execution-context--primary>strong{column-span:all}");
  });

  test("uses the active-execution card surface for lifecycle and execution context", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.setViewportSize({ width: 920, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      run_id: "consistent-active-run-surface",
      execution_mode: "MANAGED",
      target_repository: "pcvantol/djconnect",
      checkout_path: "/Users/example/Documents/GitHub/djconnect",
      active_branch: "main",
      lifecycle: {
        available: true,
        run_id: "consistent-active-run-surface",
        terminal_state: "ACTIVE",
        steps: [
          { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
          { id: "execute", presentation_key: "lifecycle.step.execute", state: "ACTIVE" },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator(".execution-lifecycle")).toHaveCount(1);
    await expect(page.locator("#executionContext")).toHaveCount(1);

    const statusAndContext = await page.locator("#currentRun .current-run__grid").evaluate((grid) => {
      const status = [...grid.children].find((child) => child.querySelector?.("#watcher"));
      const context = grid.querySelector("#executionContext");
      if (!status || !context) return null;
      const statusBox = status.getBoundingClientRect(), contextBox = context.getBoundingClientRect();
      return { statusX: statusBox.x, statusY: statusBox.y, contextX: contextBox.x, contextY: contextBox.y };
    });
    expect(statusAndContext).not.toBeNull();
    expect(statusAndContext.statusY).toBe(statusAndContext.contextY);
    expect(statusAndContext.statusX).toBeLessThan(statusAndContext.contextX);

    await expect(page.locator(".execution-lifecycle")).toHaveCSS("background-color", "rgb(27, 41, 49)");
    await expect(page.locator("#executionContext")).toHaveCSS("background-color", "rgb(27, 41, 49)");

    await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
    await expect(page.locator(".execution-lifecycle")).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await expect(page.locator("#executionContext")).toHaveCSS("background-color", "rgb(255, 255, 255)");
  });

  test("keeps lease-lost finalization visible for safe recovery", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_STALE",
      current_phase: "FINALIZE_AGENT",
      run_id: "inbox-lease-lost-finalization",
      execution_mode: "MANAGED",
      implementation_pr: 944,
      finalization_pr: 945,
    }, {}));

    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator("#currentRun")).toBeVisible();
    await expect(page.locator("#watcher")).toContainText(DASHBOARD_MESSAGES.nl["operational.stale_run"]);
    await expect(page.locator("#currentRun")).toContainText("inbox-lease-lost-finalization");
    await expect(page.locator("#currentRun")).not.toContainText("WATCHER_IDLE");
  });

  test("explains the managed and Genesis execution modes from the active execution", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: {
        watcher_state: "ENGINEERING_RUN_ACTIVE",
        run_id: "inbox-execution-mode",
        prompt_title: "Execution mode fixture",
        execution_mode: "MANAGED",
        target_repository: "pcvantol/djconnect",
        checkout_path: "/Users/example/Documents/GitHub/djconnect",
        active_branch: "main",
      } },
    }));
    const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await statusLoaded;
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const modeField = page.locator("#executionContext .execution-mode-field");
    await expect(modeField).toContainText("MANAGED");
    const info = modeField.locator(".execution-mode-info");
    await expect(info).toHaveAttribute("aria-label", DASHBOARD_MESSAGES.nl["execution_mode_info.open"]);
    await dispatchDashboardPointerClick(info);
    const modal = page.locator("#executionModeModal");
    await expect(modal).toBeVisible();
    await expect(modal.locator(".dashboard-modal-shell__panel")).toHaveCSS("border-top-color", "rgb(101, 197, 217)");
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["execution_mode_info.managed_body"]);
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["execution_mode_info.genesis_body"]);
    await page.locator("#executionModeModalClose").click();
    await expect(modal).not.toBeVisible();
  });

  test("renders execution-lifecycle nodes without button chrome", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-chrome",
      lifecycle: {
        available: true,
        run_id: "lifecycle-chrome",
        terminal_state: "ACTIVE",
        steps: [
          { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
          { id: "execute", presentation_key: "lifecycle.step.execute", state: "ACTIVE" },
        ],
      },
    }, {}));

    const nodes = page.locator(".execution-lifecycle__node");
    await expect(nodes).toHaveCount(2);
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator(".execution-lifecycle h3")).toHaveCSS("font-size", "14px");
    for (let index = 0; index < await nodes.count(); index += 1) {
      const node = nodes.nth(index);
      await expect(node).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(node).toHaveCSS("border-top-width", "0px");
      await expect(node).toHaveCSS("box-shadow", "none");
      await expect(node.locator("span").first()).toHaveCSS("border-top-width", "3px");
    }
    // The lifecycle inherits the accent of its enclosing execution surface.
    // Keep this assertion tied to that rendered accent instead of a stale
    // hard-coded palette value.
    const lifecycleAccent = await page.locator(".execution-lifecycle h3").evaluate(
      (element) => getComputedStyle(element).color,
    );
    await expect(nodes.nth(0).locator("span").first()).toHaveCSS("background-color", lifecycleAccent);
    await nodes.nth(0).evaluate((node) => {
      const region = document.querySelector(".dashboard-scroll-region");
      if (!region) return;
      const nodeRect = node.getBoundingClientRect();
      const regionRect = region.getBoundingClientRect();
      region.scrollTop += nodeRect.top - regionRect.top - 48;
    });
    await nodes.nth(0).hover();
    // Hover communicates an available detail through the shared house-style
    // border, while the node fill keeps the execution accent.
    await expect(nodes.nth(0).locator("span").first()).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(nodes.nth(0).locator("span").last()).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(nodes.nth(1).locator("span").first()).toHaveCSS("background-color", lifecycleAccent);
  });

  test("uses green only for the terminal complete lifecycle result", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "JOB_COMPLETED",
      run_id: "lifecycle-terminal-complete",
      lifecycle: {
        available: true,
        run_id: "lifecycle-terminal-complete",
        terminal_state: "COMPLETE",
        steps: [
          { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
          { id: "result", presentation_key: "lifecycle.step.result", state: "COMPLETE" },
        ],
      },
    }, {}));

    const nodes = page.locator(".execution-lifecycle__node");
    await expect(nodes).toHaveCount(2);
    await expect(nodes.nth(0).locator("span").first()).toHaveCSS("background-color", "rgb(101, 197, 217)");
    await expect(nodes.nth(1).locator("span").first()).toHaveCSS("background-color", "rgb(81, 216, 138)");
  });

  test("uses amber only for an operator merge wait, not ordinary active work", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "WAITING_FOR_OPERATOR_MERGE",
      run_id: "lifecycle-operator-wait",
      lifecycle: {
        available: true,
        run_id: "lifecycle-operator-wait",
        terminal_state: "ACTIVE",
        steps: [
          { id: "implement", presentation_key: "lifecycle.step.execute_agent", state: "COMPLETED" },
          { id: "finalization-merge", presentation_key: "lifecycle.step.wait_for_finalization_merge", state: "ACTIVE" },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const wait = page.locator(".execution-lifecycle__item--operator-wait");
    await expect(wait).toHaveCount(1);
    await expect(wait.locator(".execution-lifecycle__node").first()).toHaveClass(/operator-wait/);
    await expect(wait.locator("span").first()).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(wait.locator("span").first()).toHaveText("⌛");
    await expect(wait.locator("span").first()).toHaveCSS("font-size", "20px");
    await expect(page.locator(".execution-lifecycle__summary")).toContainText(
      DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_finalization_merge"],
    );
  });

  test("uses neutral connectors for lifecycle steps not yet reached", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-neutral-connectors",
      lifecycle: {
        available: true,
        run_id: "lifecycle-neutral-connectors",
        terminal_state: "ACTIVE",
        steps: [
          { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED" },
          { id: "implement", presentation_key: "lifecycle.step.execute_agent", state: "ACTIVE" },
          { id: "finalize", presentation_key: "lifecycle.step.finalize_agent", state: "PENDING" },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const connectors = page.locator(".execution-lifecycle__connector");
    await expect(connectors).toHaveCount(2);
    await expect(connectors.nth(0)).toHaveClass(/connector--reached/);
    await expect(connectors.nth(0)).toHaveCSS("background-color", "rgb(101, 197, 217)");
    await expect(connectors.nth(1)).not.toHaveClass(/connector--reached/);
    await expect(connectors.nth(1)).toHaveCSS("background-color", "rgb(154, 154, 163)");
  });

  test("uses a decorative rocket only for the lifecycle start boundary", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-start-glyph",
      lifecycle: {
        available: true,
        run_id: "lifecycle-start-glyph",
        terminal_state: "ACTIVE",
        steps: [
          { id: "START", presentation_key: "lifecycle.step.start", state: "START" },
          { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "PENDING" },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const start = page.locator(".execution-lifecycle__node--start");
    await expect(start).toHaveCount(1);
    await expect(start.locator("span").first()).toHaveText("🚀");
    await expect(start).toHaveAttribute("aria-label", /Start/);
  });

  test("shows pull-request check repair and keeps Merge visibly blocked", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-pr-check-repair",
      current_phase: "REPAIR_AGENT",
      current_action: "repair_bounded_validation_failure",
      lifecycle: {
        available: true,
        run_id: "lifecycle-pr-check-repair",
        terminal_state: "ACTIVE",
        steps: [
          { id: "repair", presentation_key: "lifecycle.step.repair_agent", state: "ACTIVE" },
          {
            id: "merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "BLOCKED",
            action_key: "state.repair_bounded_validation_failure",
          },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    await expect(page.locator("#action")).toHaveText(
      DASHBOARD_MESSAGES.nl["state.repair_bounded_validation_failure"],
    );
    const merge = page.locator(".execution-lifecycle__item--blocked .execution-lifecycle__node");
    await expect(merge).toHaveCount(1);
    await expect(merge.locator("span").first()).toHaveText("!");
    await expect(merge.locator("span").first()).not.toHaveText("✓");
  });

  test("shows localized local-validation iterations as lifecycle evidence", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-local-validation",
      lifecycle: {
        available: true,
        run_id: "lifecycle-local-validation",
        terminal_state: "ACTIVE",
        steps: [{
          id: "LOCAL_REPOSITORY_VALIDATION",
          presentation_key: "lifecycle.step.local_repository_validation",
          state: "ACTIVE",
          iteration_count: 2,
          repair_audit: [{
            iteration: "2", failed_checks: "Canonical tests", proposed_action: "Fix the bounded test.",
            agent_summary: "Validation passes.", commit_sha: "abcdef1", outcome: "validated",
          }],
        }],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const node = page.locator(".execution-lifecycle__node");
    await expect(node).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.step.local_repository_validation"]);
    await dispatchDashboardPointerClick(node);
    const detail = page.locator("#lifecycleDetailModal");
    await expect(detail).toBeVisible();
    await expect(detail).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.detail_local_validation_evidence"]);
    await expect(detail).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.detail_repair_iteration"].replace("{iteration}", "2"));
    await expect(detail).toContainText("Validation passes.");
  });

  test("places finalization pull-request repair after Finalization and before its merge", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-finalization-pr-check-repair",
      current_phase: "FINALIZATION_REPAIR_AGENT",
      current_action: "repair_bounded_validation_failure",
      lifecycle: {
        available: true,
        run_id: "lifecycle-finalization-pr-check-repair",
        terminal_state: "ACTIVE",
        steps: [
          { id: "finalize", presentation_key: "lifecycle.step.finalize_agent", state: "COMPLETED" },
          { id: "finalization-repair", presentation_key: "lifecycle.step.repair_agent", state: "ACTIVE", iteration_count: 1 },
          {
            id: "finalization-merge", presentation_key: "lifecycle.step.wait_for_finalization_merge", state: "BLOCKED",
            action_key: "state.repair_bounded_validation_failure",
          },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    await expect(page.locator("#phase")).toHaveText(
      DASHBOARD_MESSAGES.nl["lifecycle.step.repair_agent"],
    );
    const labels = await page.locator(".execution-lifecycle__node").allTextContents();
    expect(labels).toEqual([
      "✓" + DASHBOARD_MESSAGES.nl["lifecycle.step.finalize_agent"],
      DASHBOARD_MESSAGES.nl["lifecycle.step.repair_agent"],
      "!" + DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_finalization_merge"],
    ]);
    await expect(page.locator("#action")).toHaveText(
      DASHBOARD_MESSAGES.nl["state.repair_bounded_validation_failure"],
    );
  });

  test("places the estimate directly below execution identity and before the lifecycle", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-placement",
      lifecycle: {
        available: true,
        run_id: "lifecycle-placement",
        terminal_state: "ACTIVE",
        steps: [
          { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
          { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED" },
          { id: "implement", presentation_key: "lifecycle.step.implement", state: "ACTIVE" },
          { id: "repair", presentation_key: "lifecycle.step.repair_agent", state: "PENDING" },
          { id: "merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "PENDING" },
          { id: "finalize", presentation_key: "lifecycle.step.finalize_agent", state: "PENDING" },
          { id: "cleanup", presentation_key: "lifecycle.step.repository_cleanup", state: "PENDING" },
          { id: "terminal", presentation_key: "lifecycle.step.terminal", state: "PENDING" },
        ],
      },
    }, {}));

    const activeOrder = await page.locator("#currentRun .current-run__grid").evaluate((grid) =>
      [...grid.children].filter((item) => item.matches(".card,.execution-lifecycle")).map((item) => item.id === "executionIdentity" ? "identity"
        : item.querySelector("#executionEstimate") ? "estimate"
          : item.classList.contains("execution-lifecycle") ? "lifecycle" : item.id || "card"),
    );
    expect(activeOrder.slice(0, 3)).toEqual(["identity", "estimate", "lifecycle"]);
  });

  test("returns active-execution blocks to two columns when their container permits it", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.setViewportSize({ width: 920, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "responsive-current-run",
      lifecycle: {
        available: true,
        run_id: "responsive-current-run",
        terminal_state: "ACTIVE",
        steps: [{ id: "implement", presentation_key: "lifecycle.step.implement", state: "ACTIVE" }],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const columns = async () => page.locator("#currentRun .current-run__grid").evaluate((element) =>
      getComputedStyle(element).gridTemplateColumns.split(" ").length,
    );
    await expect.poll(columns).toBe(2);
    const [executionIdentity, executionEstimate] = await Promise.all([
      page.locator("#executionIdentity").boundingBox(),
      page.locator("#executionEstimate").locator("xpath=..").boundingBox(),
    ]);
    expect(executionIdentity).not.toBeNull();
    expect(executionEstimate).not.toBeNull();
    expect(executionIdentity.y).toBe(executionEstimate.y);
    expect(executionIdentity.x).toBeLessThan(executionEstimate.x);

    const contained = async () => page.locator("#currentRun").evaluate((run) => {
      const runRight = run.getBoundingClientRect().right;
      return [...run.querySelectorAll(".current-run__grid > *")].every((item) =>
        item.getBoundingClientRect().right <= runRight,
      );
    });
    await expect.poll(contained).toBe(true);
    const lifecycleScroll = page.locator("#currentRun .execution-lifecycle__scroll");
    await expect(lifecycleScroll).toHaveCSS("overflow-x", "auto");

    await page.setViewportSize({ width: 760, height: 844 });
    await expect.poll(columns).toBe(1);
  });

  test("keeps lifecycle steps transparent on iPhone and puts repair counts in their details", async ({ browser }) => {
    const context = await browser.newContext({ hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.evaluate(() => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", lifecycle: {
      available: true,
      run_id: "lifecycle-touch-contract",
      terminal_state: "ACTIVE",
      steps: [
        { id: "implement", presentation_key: "lifecycle.step.implement", state: "COMPLETED", iteration_count: 1 },
        { id: "merge", presentation_key: "lifecycle.step.merge", state: "ACTIVE" },
      ],
    } }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const node = page.locator(".execution-lifecycle__node").first();
    await expect(node).toHaveCSS("background-image", "none");
    await expect(node).toHaveCSS("backdrop-filter", "none");
    await expect(node).toHaveCSS("transition-property", "none");
    await node.focus();
    await expect(node).toHaveCSS("outline-style", "none");
    await expect(node).toHaveCSS("box-shadow", "none");
    await expect(page.locator(".execution-lifecycle__badge")).toHaveCount(0);
    await expect(page.locator(".execution-lifecycle__connector")).toHaveCount(1);
    await expect(page.locator(".execution-lifecycle__connector")).toHaveCSS("display", "block");
    await context.close();
  });

  test("gives the lifecycle detail modal the shared panel surface on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#lifecycleDetailModal");
    await modal.evaluate((element) => element.showModal());
    const panel = modal.locator(".lifecycle-detail-modal__panel");
    await expect(panel).toHaveCSS("border-top-width", "2px");
    await expect(panel).toHaveCSS("border-top-color", "rgb(101, 197, 217)");
    await expect(panel).toHaveCSS("border-top-left-radius", "18px");
    await expect(panel).toHaveCSS("overflow-y", "hidden");
    await expect(modal.locator("#lifecycleDetailContent")).toHaveCSS("overflow-y", "auto");
  });

  test("keeps execution-lifecycle connector lengths fixed for long labels", async ({ page }) => {
    // Isolate the layout fixture from the server-push stream. A live status
    // update may otherwise replace the injected lifecycle while measurements
    // are pending, making this visual contract nondeterministic.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-spacing",
      lifecycle: {
        available: true,
        run_id: "lifecycle-spacing",
        terminal_state: "ACTIVE",
        steps: [
          { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED" },
          { id: "repository_cleanup", presentation_key: "lifecycle.step.repository_cleanup", state: "COMPLETED" },
          { id: "terminal", presentation_key: "lifecycle.step.terminal", state: "ACTIVE" },
        ],
      },
    }, {}));

    await expect(page.locator(".execution-lifecycle__item")).toHaveCount(3);
    await expect(page.locator(".execution-lifecycle__item").nth(1).locator(".execution-lifecycle__node > span").last())
      .toHaveText("Repository opschoning");
    const spacing = await page.locator(".execution-lifecycle__item").evaluateAll((items) => items.map((item) => {
      const itemBox = item.getBoundingClientRect();
      const circleBox = item.querySelector(".execution-lifecycle__node > span")?.getBoundingClientRect();
      const connectorElement = item.querySelector(".execution-lifecycle__connector");
      const connector = connectorElement ? getComputedStyle(connectorElement) : null;
      const connectorBox = connectorElement?.getBoundingClientRect();
      return {
        itemWidth: itemBox.width,
        centre: circleBox ? circleBox.left + (circleBox.width / 2) : null,
        connectorLeft: Number.parseFloat(connector?.left),
        connectorWidth: Number.parseFloat(connector?.width),
        connectorColor: connector?.backgroundColor,
        connectorLayer: connector?.zIndex,
        connectorRenderedWidth: connectorBox?.width,
        connectorRenderedHeight: connectorBox?.height,
        connectorCentreY: connectorBox ? connectorBox.top + (connectorBox.height / 2) : null,
        circleCentreY: circleBox ? circleBox.top + (circleBox.height / 2) : null,
        nodeTextColor: getComputedStyle(item.querySelector(".execution-lifecycle__node")).color,
        selectedLabelColor: getComputedStyle(item.querySelector(".execution-lifecycle__node > span:last-child")).color,
      };
    }));

    expect(spacing[0].itemWidth).toBeGreaterThan(0);
    expect(spacing[1].itemWidth).toBe(spacing[0].itemWidth);
    expect(spacing[2].itemWidth).toBe(spacing[0].itemWidth);
    expect(spacing[1].centre - spacing[0].centre).toBe(spacing[0].itemWidth);
    expect(spacing[2].centre - spacing[1].centre).toBe(spacing[0].itemWidth);
    // The connector starts at the right edge of one 52px circle and ends at
    // the left edge of the next; it must not end at the current item boundary.
    expect(spacing[0].connectorLeft + spacing[0].connectorWidth).toBe(
      spacing[0].itemWidth + ((spacing[0].itemWidth / 2) - 26),
    );
    expect(spacing[0].connectorColor).not.toBe("rgba(0, 0, 0, 0)");
    expect(spacing[0].connectorColor).toBe("rgb(101, 197, 217)");
    expect(spacing[0].connectorLayer).toBe("3");
    expect(spacing[0].connectorRenderedWidth).toBeGreaterThan(0);
    expect(spacing[0].connectorRenderedHeight).toBeGreaterThan(0);
    expect(spacing[0].connectorCentreY).toBeCloseTo(spacing[0].circleCentreY, 5);
    expect(spacing[1].connectorCentreY).toBeCloseTo(spacing[1].circleCentreY, 5);
    expect(spacing[2].connectorColor).toBeUndefined();
    expect(spacing[0].selectedLabelColor).toBe(spacing[0].nodeTextColor);
    expect(spacing[1].selectedLabelColor).toBe(spacing[1].nodeTextColor);
  });

  test("opens a native lifecycle step detail with persisted phase timing", async ({ page }) => {
    // The fixture owns this lifecycle; a delayed server-push snapshot must not
    // replace it between injection and the interaction assertion.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-detail",
      lifecycle: {
        available: true, run_id: "lifecycle-detail", terminal_state: "ACTIVE",
        steps: [{
          id: "execute", presentation_key: "lifecycle.step.execute_agent", state: "ACTIVE",
          timing: { started_at: "2026-08-16T14:00:00Z", finished_at: "2026-08-16T14:03:00Z", spans: [{
            phase: "PROVIDER_EXECUTION", duration_ms: 12000, outcome: "COMPLETE",
          }] },
        }],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await dispatchDashboardPointerClick(page.locator(".execution-lifecycle__node"));
    const modal = page.locator("#lifecycleDetailModal");
    await expect(modal).toBeVisible();
    await expect(modal).toContainText("Implementatie");
    await expect(modal.locator("#lifecycleDetailTitle")).toHaveAttribute("data-lifecycle-status", "active");
    await expect(modal.locator(".lifecycle-detail-modal__status-indicator")).toHaveClass(/indicator--blue/);
    expect(await modal.locator("#lifecycleDetailTitle").evaluate(
      (title) => getComputedStyle(title, "::before").content,
    )).toBe('"●"');
    await expect(modal.locator(".lifecycle-detail-modal__content .field > span:last-child").first()).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["telemetry.phase.provider_execution"]);
    const phaseSecondary = await modal.evaluate((element) => {
      const expected = document.createElement("span");
      expected.style.color = "var(--modal-secondary-accent)";
      element.append(expected);
      const colour = getComputedStyle(expected).color;
      expected.remove();
      return colour;
    });
    await expect(modal.locator(".lifecycle-detail-modal__phase-list strong")).toHaveCSS("color", phaseSecondary);
    await expect(modal).toContainText("12 sec");
    await page.locator("#lifecycleDetailClose").click();
    await expect(modal).not.toBeVisible();
  });

  test("explains translated stale timing without downgrading a completed execution result", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await waitForDashboardReady(page);
      await selectDashboardLocale(page, language);
      await page.locator("#autoRefresh").uncheck();
      await page.evaluate(() => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE", current_phase: "COMPLETE", run_id: "terminal-stale-timing",
        lifecycle: {
          available: true, run_id: "terminal-stale-timing", terminal_state: "COMPLETE", steps: [{
            id: "TERMINAL", presentation_key: "lifecycle.step.terminal", state: "COMPLETE",
            timing: { started_at: "2026-08-16T14:00:00Z", finished_at: "2026-08-16T14:03:00Z", spans: [{
              phase: "TOTAL_EXECUTION", duration_ms: 180000, outcome: "STALE",
            }] },
          }],
        },
      }, {}));
      await page.locator("#currentRun").evaluate((element) => { element.open = true; });
      await page.locator(".execution-lifecycle__node").click();
      const modal = page.locator("#lifecycleDetailModal");
      await expect(modal).toContainText(DASHBOARD_MESSAGES[language]["lifecycle.state.complete"]);
      await expect(modal).toContainText(DASHBOARD_MESSAGES[language]["lifecycle.state.stale"]);
      await expect(modal).toContainText(DASHBOARD_MESSAGES[language]["lifecycle.detail_terminal_timing_stale"]);
      await expect(modal).not.toContainText("STALE");
      await page.locator("#lifecycleDetailClose").click();
    }
  });

  test("shows autonomous quality control as its own workflow node and detail modal", async ({ page }) => {
    // This fixture owns the lifecycle projection; an asynchronous server
    // snapshot must not replace its node while the click is being asserted.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "quality-control-visible",
      lifecycle: {
        available: true, run_id: "quality-control-visible", terminal_state: "ACTIVE",
        steps: [
          { id: "execute", presentation_key: "lifecycle.step.execute_agent", state: "COMPLETED" },
          { id: "quality", presentation_key: "lifecycle.step.quality_control_agent", state: "ACTIVE",
            timing: { started_at: "2026-08-16T14:00:00Z", spans: [{ phase: "QUALITY_CONTROL", duration_ms: 1000, outcome: "ACTIVE" }] },
            quality_evidence: [{ activity: "TEST_COVERAGE", result: "Gerichte regressietest toegevoegd." }] },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const qualityNode = page.locator(".execution-lifecycle__item").filter({ hasText: DASHBOARD_MESSAGES.nl["lifecycle.step.quality_control_agent"] });
    await expect(qualityNode).toHaveCount(1);
    await dispatchDashboardPointerClick(qualityNode.locator(".execution-lifecycle__node"));
    const modal = page.locator("#lifecycleDetailModal");
    await expect(modal).toBeVisible();
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.step.quality_control_agent"]);
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["telemetry.phase.quality_control"]);
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.detail_quality_evidence"]);
    await expect(modal).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.quality_evidence.test_coverage"]);
    await expect(modal).toContainText("Gerichte regressietest toegevoegd.");
    await expect(modal.locator(".lifecycle-detail-modal__status-indicator")).toHaveClass(/indicator--blue/);
  });

  test("summarizes repeated lifecycle phase timing records by phase", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      run_id: "lifecycle-phase-summary",
      lifecycle: {
        available: true, run_id: "lifecycle-phase-summary", terminal_state: "ACTIVE",
        steps: [{
          id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED",
          timing: { started_at: "2026-08-16T14:00:00Z", finished_at: "2026-08-16T14:03:00Z", spans: [
            { phase: "INITIALIZATION", duration_ms: 0, outcome: "COMPLETE" },
            { phase: "INITIALIZATION", duration_ms: 1000, outcome: "COMPLETE" },
            { phase: "INITIALIZATION", duration_ms: 2000, outcome: "COMPLETE" },
            { phase: "PROVIDER_EXECUTION", duration_ms: 12000, outcome: "COMPLETE" },
            { phase: "TOTAL_EXECUTION", duration_ms: 15000, outcome: "COMPLETE" },
          ] },
        }],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await page.locator(".execution-lifecycle__node").click();

    const phaseRows = page.locator("#lifecycleDetailModal .lifecycle-detail-modal__phase-list li");
    await expect(phaseRows).toHaveCount(3);
    await expect(phaseRows.nth(0)).toContainText(DASHBOARD_MESSAGES.nl["telemetry.phase.initialization"]);
    await expect(phaseRows.nth(0)).toContainText("3 sec");
    await expect(phaseRows.nth(1)).toContainText(DASHBOARD_MESSAGES.nl["telemetry.phase.provider_execution"]);
    await expect(phaseRows.nth(2)).toContainText(DASHBOARD_MESSAGES.nl["telemetry.phase.total_execution"]);
  });

  test("reveals an initially off-screen active lifecycle step after page load", async ({ page }) => {
    await page.setViewportSize({ width: 640, height: 900 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.addStyleTag({ content: ".execution-lifecycle__scroll { width: 300px !important; }" });
    await page.evaluate(() => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "lifecycle-reveal", lifecycle: {
      available: true, run_id: "lifecycle-reveal", terminal_state: "ACTIVE", steps: [
        { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
        { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED" },
        { id: "execute", presentation_key: "lifecycle.step.execute_agent", state: "COMPLETED" },
        { id: "repair", presentation_key: "lifecycle.step.repair_agent", state: "COMPLETED" },
        { id: "finalization-merge", presentation_key: "lifecycle.step.wait_for_finalization_merge", state: "ACTIVE" },
        { id: "cleanup", presentation_key: "lifecycle.step.repository_cleanup", state: "PENDING" },
      ],
    } }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const scroll = page.locator(".execution-lifecycle__scroll"), active = page.locator(".execution-lifecycle__node--active");
    await expect.poll(() => scroll.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    expect(await active.evaluate((element) => {
      const node = element.getBoundingClientRect(), container = element.closest(".execution-lifecycle__scroll").getBoundingClientRect();
      return node.left >= container.left && node.right <= container.right;
    })).toBe(true);
  });

  test("preserves the active lifecycle horizontal position across server refreshes", async ({ page }) => {
    await page.setViewportSize({ width: 640, height: 900 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.addStyleTag({ content: ".execution-lifecycle__scroll { width: 300px !important; }" });
    const lifecycle = {
      available: true,
      run_id: "lifecycle-scroll",
      terminal_state: "ACTIVE",
      steps: [
        { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
        { id: "initialize", presentation_key: "lifecycle.step.initialize", state: "COMPLETED" },
        { id: "execute", presentation_key: "lifecycle.step.execute_agent", state: "ACTIVE" },
        { id: "repair", presentation_key: "lifecycle.step.repair_agent", state: "PENDING" },
        { id: "merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "PENDING" },
        { id: "finalize", presentation_key: "lifecycle.step.finalization", state: "PENDING" },
        { id: "cleanup", presentation_key: "lifecycle.step.repository_cleanup", state: "PENDING" },
      ],
    };
    await page.evaluate((fixture) => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: fixture.run_id, lifecycle: fixture }, {}), lifecycle);
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const scroll = page.locator(".execution-lifecycle__scroll");
    await scroll.evaluate((element) => { element.scrollLeft = 80; });
    await expect.poll(() => scroll.evaluate((element) => element.scrollLeft)).toBe(80);

    await page.evaluate((fixture) => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: fixture.run_id, lifecycle: fixture }, {}), lifecycle);
    await expect(scroll).toHaveJSProperty("scrollLeft", 80);
  });

  test("keeps a green pull request visible until the operator merges or aborts it", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/open-pull-requests", (route) => route.fulfill({ json: { pull_requests: [{
      number: 832, title: "Merge wait fixture", url: "https://github.com/pcvantol/djconnect/pull/832",
      branch: "codex/merge-wait", status: "ready_to_merge",
      owner_approval: "approved",
    }] } }));
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: {
        watcher_state: "WAITING_FOR_OPERATOR_MERGE",
        current_phase: "WAIT_FOR_OPERATOR_MERGE",
        run_id: "inbox-merge-wait",
        pull_request: 832,
        target_repository: "pcvantol/djconnect",
        prompt_title: "Merge wait fixture",
        lifecycle: {
          available: true, run_id: "inbox-merge-wait", terminal_state: "ACTIVE",
          steps: [
            { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
            { id: "merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "ACTIVE" },
          ],
        },
      } },
    }));
    let abortRequested = false;
    await page.route("**/api/execution-merge-wait-abort", async (route) => {
      abortRequested = route.request().method() === "POST" &&
        (await route.request().postDataJSON()).run_id === "inbox-merge-wait";
      await route.fulfill({ json: { run_id: "inbox-merge-wait", dismissed: true } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const wait = page.locator("#operatorMergeWait");
    await expect(wait).toBeVisible();
    await expect(wait.locator("#operatorMergeWaitTitle")).toHaveText(DASHBOARD_MESSAGES.nl["merge_wait.title.implementation"]);
    await expect(wait.locator("#operatorMergeWaitPullRequestStatus")).toHaveClass(/open-pr-status--ready_to_merge/);
    await expect(wait.locator("#operatorMergeWaitPullRequestStatus")).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.ready_to_merge"]);
    await expect(wait.locator("#operatorMergeWaitOwnerApproval")).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.owner_approval_approved"]);
    await expect(page.locator(".execution-lifecycle + #operatorMergeWait")).toBeVisible();
    const mergeLink = wait.locator("a");
    const abort = wait.getByRole("button", { name: DASHBOARD_MESSAGES.nl["action.abort_execution"] });
    await expect(mergeLink).toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/832");
    await expect(mergeLink).toHaveCSS("border-top-left-radius", "10px");
    await expect(abort).toHaveCSS("border-top-left-radius", "10px");
    const actionLayout = await wait.locator(".operator-merge-wait__actions").evaluate((container) => {
      const [first, second] = Array.from(container.children).map((action) => action.getBoundingClientRect());
      return { first: { right: first.right, bottom: first.bottom }, second: { left: second.left, top: second.top } };
    });
    await expect(mergeLink).toHaveCSS("min-height", "40px");
    await expect(page.locator("#operatorMergeStatusCheck")).toHaveCSS("background-color", "rgb(32, 42, 54)");
    await expect(page.locator("#operatorMergeStatusCheck")).toHaveCSS("border-top-color", "rgb(141, 199, 255)");
    expect(
      actionLayout.first.bottom <= actionLayout.second.top || actionLayout.first.right <= actionLayout.second.left,
    ).toBe(true);
    expect(await mergeLink.evaluate((element) => getComputedStyle(element, "::before").content)).toBe('"↗"');
    expect(await abort.evaluate((element) => getComputedStyle(element, "::before").content)).toBe('"⊘"');
    await expect(page.locator("#operatorMergeWaitModal")).toBeVisible();
    const mergeModal = page.locator("#operatorMergeWaitModal");
    const modalPullRequest = page.locator("#operatorMergeWaitModalPullRequest");
    const modalAbort = page.locator("#operatorMergeWaitModalAbort");
    const modalStatusCheck = page.locator("#operatorMergeWaitModalStatusCheck");
    await expect(modalPullRequest).toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/832");
    await expect(mergeModal.locator("#operatorMergeWaitModalContextIntro")).toHaveText(
      `Deze hand-off is de ${DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_operator_merge"]} voor pull request #832.`,
    );
    await expect(mergeModal.locator("#operatorMergeWaitModalRunId")).toHaveText("inbox-merge-wait");
    await expect(mergeModal.locator("#operatorMergeWaitModalPrompt")).toHaveText("Merge wait fixture");
    await expect(mergeModal.locator("#operatorMergeWaitModalPullRequestStatus")).toHaveClass(/open-pr-status--ready_to_merge/);
    await expect(mergeModal.locator("#operatorMergeWaitModalPullRequestStatus")).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.ready_to_merge"]);
    await expect(mergeModal.locator("#operatorMergeWaitModalOwnerApproval")).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.owner_approval_approved"]);
    await expect(modalPullRequest).toHaveCSS("text-decoration-line", "none");
    await expect(modalPullRequest).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(modalAbort).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    await expect(modalPullRequest).toHaveCSS("background-color", "rgb(59, 40, 27)");
    await expect(modalAbort).toHaveCSS("background-color", "rgb(58, 32, 40)");
    await expect(modalAbort).toHaveCSS("color", "rgb(255, 217, 225)");
    await expect(modalPullRequest).toHaveCSS("display", "flex");
    await expect(modalPullRequest).toHaveCSS("align-items", "center");
    await expect(modalPullRequest).toHaveCSS("justify-content", "center");
    await expect(modalPullRequest).toHaveCSS("font-weight", "400");
    await expect(modalAbort).toHaveCSS("font-weight", "400");
    await expect(modalStatusCheck).toHaveText(DASHBOARD_MESSAGES.nl["merge_wait.check_status"]);
    await expect(modalStatusCheck).toHaveCSS("display", "flex");
    await expect(modalStatusCheck).toHaveCSS("justify-content", "center");
    expect(await modalStatusCheck.evaluate((element) => getComputedStyle(element, "::before").content)).toBe('"↻"');
    const mergeActionHeights = await mergeModal.locator(".dashboard-modal-shell__action").evaluateAll(
      (actions) => actions.map((action) => action.getBoundingClientRect().height),
    );
    expect(mergeActionHeights).toEqual([44, 44, 44]);
    expect(await modalPullRequest.evaluate((element) => getComputedStyle(element, "::before").content)).toBe('"↗"');
    expect(await modalAbort.evaluate((element) => getComputedStyle(element, "::before").content)).toBe('"⊘"');
    expect(await modalAbort.evaluate((element) => getComputedStyle(element, "::before").fontWeight)).toBe("700");
    expect(await modalAbort.evaluate((element) => getComputedStyle(element, "::before").textShadow)).not.toBe("none");
    await page.locator("#operatorMergeWaitModalClose").click();
    await abort.click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    const confirmationActionHeights = await page.locator("#confirmationModal .dashboard-modal-shell__action").evaluateAll(
      (actions) => actions.map((action) => action.getBoundingClientRect().height),
    );
    expect(confirmationActionHeights).toEqual([44, 44]);
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => abortRequested).toBe(true);
  });

  test("explains a merge-check category, retains its successful check time, and retries from the error dialog", async ({ page }) => {
    let checkCount = 0;
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: checkCount >= 2 ? {
      watcher_state: "ENGINEERING_RUN_ACTIVE", current_phase: "FINALIZE_AGENT", run_id: "inbox-merge-check",
    } : {
      watcher_state: "WAITING_FOR_OPERATOR_MERGE", current_phase: "WAIT_FOR_OPERATOR_MERGE",
      run_id: "inbox-merge-check", pull_request: 832, target_repository: "pcvantol/djconnect",
      merge_status_check: { last_successful_github_check_at: "2026-08-24T12:00:00Z" },
    } } }));
    await page.route("**/api/execution-merge-status-check", (route) => {
      checkCount += 1;
      return route.fulfill({ status: checkCount === 1 ? 409 : 202, json: checkCount === 1
        ? { verified: false, reason: "github_network_unavailable" }
        : { verified: true, continuation: "scheduled" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#operatorMergeWaitModalLastCheck")).toContainText(
      DASHBOARD_MESSAGES.nl["merge_wait.last_successful_check"].split(":")[0],
    );
    await page.locator("#operatorMergeWaitModalStatusCheck").click();
    await expect(page.locator("#dashboardErrorModal")).toBeVisible();
    await expect(page.locator("#dashboardErrorModalText")).toContainText(
      DASHBOARD_MESSAGES.nl["merge_wait.reason.github_network_unavailable"],
    );
    await expect(page.locator("#dashboardErrorModalRecover")).toHaveText(
      DASHBOARD_MESSAGES.nl["merge_wait.check_again"],
    );
    await page.locator("#dashboardErrorModalRecover").click();
    await expect.poll(() => checkCount).toBe(2);
    await expect(page.getByText(DASHBOARD_MESSAGES.nl["merge_wait.continuation_scheduled"])).toBeVisible();
  });

  test("opens a new handoff modal for the finalization pull request in the same run", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/open-pull-requests", (route) => route.fulfill({ json: { pull_requests: [{
      number: 841, title: "Finalization merge", url: "https://github.com/pcvantol/djconnect/pull/841",
      branch: "codex/finalization", status: "ready_for_review",
    }] } }));
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "WAITING_FOR_OPERATOR_MERGE", current_phase: "WAIT_FOR_OPERATOR_MERGE",
      run_id: "inbox-two-merges", pull_request: 840, target_repository: "pcvantol/djconnect",
    }, {}));
    const modal = page.locator("#operatorMergeWaitModal");
    await expect(modal).toBeVisible();
    await page.locator("#operatorMergeWaitModalClose").click();
    await page.evaluate(() => r({
      watcher_state: "WAITING_FOR_OPERATOR_MERGE", current_phase: "WAIT_FOR_OPERATOR_MERGE",
      run_id: "inbox-two-merges", pull_request: 841, finalization_pr: 841,
      target_repository: "pcvantol/djconnect",
      lifecycle: {
        available: true, run_id: "inbox-two-merges", terminal_state: "ACTIVE",
        steps: [
          { id: "implementation-merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "COMPLETED" },
          { id: "finalization", presentation_key: "lifecycle.step.finalize_agent", state: "COMPLETED" },
          { id: "finalization-merge", presentation_key: "lifecycle.step.wait_for_finalization_merge", state: "ACTIVE" },
        ],
      },
    }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(modal).toBeVisible();
    await expect(page.locator("#operatorMergeWaitModalPullRequest"))
      .toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/841");
    await expect(page.locator("#operatorMergeWaitModalContextIntro")).toHaveText(
      `Deze hand-off is de ${DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_finalization_merge"]} voor pull request #841.`,
    );
    await expect(page.locator("#operatorMergeWaitTitle")).toHaveText(DASHBOARD_MESSAGES.nl["merge_wait.title.finalization"]);
    await expect(page.locator("#operatorMergeWaitPullRequestStatus")).toHaveClass(/open-pr-status--ready_for_review/);
    await expect(page.locator("#operatorMergeWaitModalPullRequestStatus")).toHaveText(DASHBOARD_MESSAGES.nl["workspace.open_pull_request.ready_for_review"]);
    await expect(page.locator(".execution-lifecycle__item")).toHaveCount(3);
    await expect(page.locator(".execution-lifecycle__item--active .execution-lifecycle__node"))
      .toContainText(DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_finalization_merge"]);
  });

  test("renders automatic reconciliation without an operator handoff", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", current_phase: "RECONCILE_AGENT",
      run_id: "inbox-reconciliation",
      target_repository: "pcvantol/djconnect",
      lifecycle: {
        available: true, run_id: "inbox-reconciliation", terminal_state: "ACTIVE",
        steps: [
          { id: "RECONCILE_AGENT", presentation_key: "lifecycle.step.reconcile_agent", state: "ACTIVE" },
        ],
      },
    }, {}));
    const modal = page.locator("#operatorMergeWaitModal");
    await expect(modal).toBeHidden();
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await page.locator(".execution-lifecycle__item--active .execution-lifecycle__node").click();
    await expect(page.locator("#lifecycleDetailModal")).toBeVisible();
  });

  test("renders only the merge boundaries recorded for the lifecycle", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const render = (steps) => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", lifecycle: {
      available: true, run_id: "lifecycle-conditional-merges", terminal_state: "ACTIVE", steps,
    } }, {});
    await page.evaluate(render, [
      { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
      { id: "finalization", presentation_key: "lifecycle.step.finalize_agent", state: "ACTIVE" },
    ]);
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator(".execution-lifecycle__item")).toHaveCount(2);
    await page.evaluate(render, [
      { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
      { id: "merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "COMPLETED" },
      { id: "finalization", presentation_key: "lifecycle.step.finalize_agent", state: "ACTIVE" },
    ]);
    await expect(page.locator(".execution-lifecycle__item")).toHaveCount(3);
    await expect(page.locator(".execution-lifecycle__node")).toContainText([
      DASHBOARD_MESSAGES.nl["lifecycle.step.start"],
      DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_operator_merge"],
      DASHBOARD_MESSAGES.nl["lifecycle.step.finalize_agent"],
    ]);
  });

  test("uses explicit localized labels for PR repair and both merge hand-offs", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", lifecycle: {
      available: true, run_id: "lifecycle-explicit-labels", terminal_state: "ACTIVE",
      steps: [
        { id: "repair", presentation_key: "lifecycle.step.repair_agent", state: "COMPLETED" },
        { id: "implementation-merge", presentation_key: "lifecycle.step.wait_for_operator_merge", state: "COMPLETED" },
        { id: "finalization-merge", presentation_key: "lifecycle.step.wait_for_finalization_merge", state: "ACTIVE" },
      ],
    } }, {}));
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator(".execution-lifecycle__node")).toContainText([
      DASHBOARD_MESSAGES.nl["lifecycle.step.repair_agent"],
      DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_operator_merge"],
      DASHBOARD_MESSAGES.nl["lifecycle.step.wait_for_finalization_merge"],
    ]);
  });

  test("keeps sortable headers within one complete neutral cell edge in every table", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => window.executionTelemetry([
      { date: "2026-08-16", prompt_count: 4 },
    ]));
    const focusRule = await page.evaluate(() => [...document.styleSheets]
      .flatMap((sheet) => [...sheet.cssRules])
      .map((rule) => rule.cssText)
      .filter((cssText) => cssText.includes(".telemetry-table th.log-sortable:focus"))
      .at(-1));
    expect(focusRule).toContain("inset 0 0 0 1px");
    expect(focusRule).toContain("--dashboard-table-focus-border");
    expect(focusRule).toContain("outline: 0px !important");
  });

  test("renders a read-only Forge recommendation handoff with expandable alternatives", async ({ page }) => {
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: "inbox-handoff", status: "COMPLETE", title: "Forge handoff", executed_at: "2026-08-04T08:00:00Z",
    }] } }));
    await page.route("**/api/prompt-history/inbox-handoff/details", (route) => route.fulfill({ json: {
      history: { run_id: "inbox-handoff", status: "COMPLETE", title: "Forge handoff", executed_at: "2026-08-04T08:00:00Z" },
      recommendation_handoff: {
        artifact_path: "forge/recommendation.json", projection_status: "COMPLETE", missing_fields: [],
        recommendation: { title: "Mission Aurora", status: "RECOMMENDED", mission_origin: "PORTFOLIO_INTELLIGENCE", business_value: "High", confidence: "0.91", dependencies: ["DEC-7"], summary: "Highest value", decision_evidence: "DEC-7" },
        alternatives: [{ rank: 2, title: "Mission Borealis", ordering_reason: "Lower value" }],
      },
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    const historyRow = page.locator("#promptHistoryRows .prompt-history-row");
    await historyRow.waitFor({ state: "visible" });
    await dispatchDashboardPointerClick(historyRow);
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Mission Aurora");
    const alternatives = page.locator(".recommendation-alternatives");
    await alternatives.locator("summary").click();
    await expect(alternatives).toHaveAttribute("open", "");
    await expect(alternatives).toContainText("Mission Borealis");
    await expect(page.locator("#promptHistoryDetailContent button")).toHaveCount(1);
    await expect(page.locator("#promptHistoryDetailContent .prompt-history-run-id-copy")).toHaveCount(1);
  });

  test("uses the shared modal shell with contextual panels and neutral close controls", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    for (const [selector, modifier, accent] of [
      ["#componentModal", "dashboard-modal-shell--component", "rgb(163, 230, 53)"],
      ["#executionModeModal", "dashboard-modal-shell--evidence", "rgb(141, 199, 255)"],
      ["#confirmationModal", "dashboard-modal-shell--confirmation", "rgb(240, 182, 106)"],
      ["#dashboardErrorModal", "dashboard-modal-shell--confirmation", "rgb(240, 182, 106)"],
      ["#operatorMergeWaitModal", "dashboard-modal-shell--evidence", "rgb(141, 199, 255)"],
      ["#promptHistoryReportModal", "dashboard-modal-shell--evidence", "rgb(141, 199, 255)"],
      ["#promptHistoryDetailModal", "dashboard-modal-shell--evidence", "rgb(141, 199, 255)"],
      ["#promptHistoryChatModal", "dashboard-modal-shell--chat", "rgb(208, 164, 255)"],
    ]) {
      const modal = page.locator(selector);
      await expect(modal).toHaveClass(new RegExp(modifier));
      await modal.evaluate((element) => element.showModal());
      await expect(modal.locator(".dashboard-modal-shell__panel")).toHaveCSS("border-top-color", accent);
      await expect(modal.locator(".dashboard-modal-shell__close")).toHaveCSS("border-top-color", "rgb(146, 145, 155)");
      await modal.evaluate((element) => element.close());
    }
  });

  test("uses purpose-matched glyphs for confirmation and merge hand-off modals", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const glyph = async (selector) => page.locator(selector).evaluate(
      (heading) => getComputedStyle(heading, "::before").content,
    );
    await expect.poll(() => glyph("#operatorMergeWaitModalTitle")).toBe('"ⓘ"');
    await expect.poll(() => glyph("#confirmationModalTitle")).toBe('"ⓘ"');
    await expect.poll(() => glyph("#dashboardErrorModalTitle")).toBe('"×"');
    await page.evaluate(() => {
      document.querySelector("#confirmationModalTitle").dataset.modalGlyph = "question";
    });
    await expect.poll(() => glyph("#confirmationModalTitle")).toBe('"?"');
    expect(await page.locator("#confirmationModalTitle").evaluate(
      (heading) => getComputedStyle(heading, "::before").width,
    )).toBe("20px");
    await page.evaluate(() => {
      document.querySelector("#confirmationModalTitle").dataset.modalGlyph = "warning";
    });
    await expect.poll(() => glyph("#confirmationModalTitle")).toBe('"⚠"');
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModalTitle").dataset.modalGlyph = "analysis";
    });
    await expect.poll(() => glyph("#promptHistoryReportModalTitle")).toBe('"✦"');
    for (const selector of [
      "#operatorMergeWaitModalTitle",
      "#confirmationModalTitle",
      "#promptHistoryReportModalTitle",
      "#promptHistoryChatTitle",
    ]) {
      expect(await page.locator(selector).evaluate(
        (heading) => getComputedStyle(heading, "::before").fontSize,
      )).toBe("20px");
    }
    expect(await glyph("#promptHistoryDetailTitle")).toBe("none");
    await expect(page.locator("#promptHistoryReportModalTitle")).toHaveCSS("border-top-width", "0px");
    expect(await page.locator("#promptHistoryReportModalTitle").evaluate(
      (heading) => getComputedStyle(heading, "::before").borderTopWidth,
    )).toBe("0px");
  });

  test("keeps every modal close control visible in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    const shells = page.locator("dialog.dashboard-modal-shell");
    expect(await shells.count()).toBeGreaterThan(0);
    for (let index = 0; index < await shells.count(); index += 1) {
      const shell = shells.nth(index);
      await shell.evaluate((element) => element.showModal());
      const close = shell.locator(".dashboard-modal-shell__close");
      await expect(close).toBeVisible();
      await expect(close).toHaveCSS("border-top-width", "1px");
      await expect(close).toHaveCSS("border-top-style", "solid");
      await shell.evaluate((element) => element.close());
    }
  });

  test("never gives a modal close control the initial focus", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const shells = page.locator("dialog.dashboard-modal-shell");
    for (let index = 0; index < await shells.count(); index += 1) {
      const shell = shells.nth(index);
      await shell.evaluate((element) => element.showModal());
      const close = shell.locator(".dashboard-modal-shell__close");
      await expect.poll(() => close.evaluate((element) => document.activeElement !== element)).toBe(true);
      await expect(close).not.toBeFocused();
      await shell.evaluate((element) => element.close());
    }
  });

  test("uses the shared glyph weights for actions, disclosures and modal closes", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#pageRefresh span")).toHaveCSS("font-weight", "700");
    const modalCloses = page.locator("dialog.dashboard-modal-shell .dashboard-modal-shell__close");
    expect(await modalCloses.count()).toBeGreaterThan(0);
    for (let index = 0; index < await modalCloses.count(); index += 1) {
      await expect(modalCloses.nth(index)).toHaveCSS("font-weight", "400");
    }
    const modalTitleGlyphWeights = await page.locator(
      "dialog.dashboard-modal-shell :is(.component-modal h2,.confirmation-modal h2,.report-view-modal__title,.prompt-detail-modal__header h2,.prompt-chat-modal__header h2)",
    ).evaluateAll((titles) => titles.map((title) => getComputedStyle(title, "::before").fontWeight));
    expect(modalTitleGlyphWeights.every((weight) => weight === "700")).toBe(true);
    const disclosureWeight = await page.locator("#currentRun > summary").evaluate(
      (element) => getComputedStyle(element, "::before").fontWeight,
    );
    expect(disclosureWeight).toBe("700");
  });

  test("never draws a selected focus border around a modal shell", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const shells = page.locator("dialog.dashboard-modal-shell");
    expect(await shells.count()).toBeGreaterThan(0);
    for (let index = 0; index < await shells.count(); index += 1) {
      const shell = shells.nth(index);
      await shell.evaluate((element) => {
        element.showModal();
      });
      await expect(shell).toHaveCSS("outline-width", "0px");
      await expect(shell).toHaveCSS("outline-style", "none");
      await expect(shell).toHaveCSS("box-shadow", "none");
      await shell.evaluate((element) => element.close());
    }
  });

  test("applies the compact header and standard action scale to every modal", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const titles = [];
    for (const selector of [
      "#componentModal",
      "#executionModeModal",
      "#confirmationModal",
      "#dashboardErrorModal",
      "#promptHistoryReportModal",
      "#promptHistoryDetailModal",
      "#promptHistoryChatModal",
    ]) {
      const modal = page.locator(selector);
      await modal.evaluate((element) => element.showModal());
      const metrics = await modal.evaluate((element) => {
        const panel = element.querySelector(".dashboard-modal-shell__panel");
        const header = element.querySelector(".dashboard-modal-shell__header");
        const title = header.querySelector("h2");
        const close = header.querySelector(".dashboard-modal-shell__close");
        const headerStyle = getComputedStyle(header);
        const panelBox = panel.getBoundingClientRect();
        const headerBox = header.getBoundingClientRect();
        const closeBox = close.getBoundingClientRect();
        return {
          panelBackground: getComputedStyle(panel).backgroundColor,
          headerBackground: headerStyle.backgroundColor,
          paddingTop: headerStyle.paddingTop,
          paddingBottom: headerStyle.paddingBottom,
          titleSize: getComputedStyle(title).fontSize,
          closeWidth: Math.round(closeBox.width),
          closeHeight: Math.round(closeBox.height),
          headerStart: Math.abs(headerBox.left - (panelBox.left + parseFloat(getComputedStyle(panel).borderLeftWidth))),
          headerEnd: Math.abs((panelBox.right - parseFloat(getComputedStyle(panel).borderRightWidth)) - headerBox.right),
        };
      });
      expect(metrics.headerBackground).not.toBe(metrics.panelBackground);
      expect(metrics.paddingTop).toBe(metrics.paddingBottom);
      expect(metrics.closeWidth).toBe(32);
      expect(metrics.closeHeight).toBe(32);
      expect(metrics.headerStart).toBeLessThanOrEqual(1);
      expect(metrics.headerEnd).toBeLessThanOrEqual(1);
      titles.push(metrics.titleSize);
      await modal.evaluate((element) => element.close());
    }
    expect(new Set(titles)).toEqual(new Set(["20px"]));
  });

  test("presents localized retry preflight failures in the dashboard modal", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    let nativeDialog = false;
    page.on("dialog", () => { nativeDialog = true; });
    await page.evaluate(() => window.showDashboardError(
      "Preflight failed: Managed target is not on the expected branch main. Recovery: Switch the repository to main before submitting work.",
    ));
    const modal = page.locator("#dashboardErrorModal");
    await expect(modal).toBeVisible();
    await expect(page.locator("#dashboardErrorModalTitle")).toHaveText(DASHBOARD_MESSAGES.nl["ui.action_failed"]);
    await expect(page.locator("#dashboardErrorModalText")).toContainText("branch main");
    await expect(page.locator("#dashboardErrorModalDismiss")).toHaveText(DASHBOARD_MESSAGES.nl["action.close"]);
    expect(nativeDialog).toBe(false);
    await page.locator("#dashboardErrorModalDismiss").click();
    await expect(modal).not.toBeVisible();
  });

  test("localizes unstaged-change preflight failures for every supported language", async ({ page }) => {
    // The modal assertions own their projection; avoid a live snapshot racing
    // one of the locale-triggered page reloads under the parallel CI suite.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    const error = "Preflight failed: Unstaged changes are present. Recovery: Commit, stash, or remove unstaged changes before execution.";
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const language of SUPPORTED_LOCALES) {
      await selectDashboardLocale(page, language);
      await page.evaluate((message) => window.showDashboardError(message), error);
      const modal = page.locator("#dashboardErrorModal");
      await expect(modal).toBeVisible();
      const translate = createTranslator(language);
      await expect(page.locator("#dashboardErrorModalText")).toHaveText(
        translate("preflight.unstaged", {
          reason: translate("preflight.unstaged_reason"),
          recovery: translate("preflight.unstaged_recovery"),
        }),
      );
      await page.locator("#dashboardErrorModalDismiss").click();
      await expect(modal).not.toBeVisible();
    }
  });

  test("offers a safe branch synchronization recovery in a preflight error", async ({ page }) => {
    let recoveryRequested = false;
    await page.route("**/api/managed-branch-synchronization", async (route) => {
      recoveryRequested = true;
      await route.fulfill({ json: { branch: "main", upstream: "origin/main", watcher: "restarted" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.showDashboardError(
      "Preflight failed: Managed target is not synchronized with its upstream. Recovery: Synchronize the expected branch with its configured upstream.",
    ));
    const modal = page.locator("#dashboardErrorModal");
    await expect(modal).toBeVisible();
    await expect(page.locator("#dashboardErrorModalText")).toContainText("gesynchroniseerd met de upstream");
    await expect(page.locator("#dashboardErrorModalRecover")).toBeVisible();
    await expect(page.locator("#dashboardErrorModalRecover")).toHaveText(DASHBOARD_MESSAGES.nl["action.recover"]);
    await page.locator("#dashboardErrorModalRecover").hover();
    await expect(page.locator("#dashboardErrorModalRecover")).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await page.locator("#dashboardErrorModalRecover").click();
    await expect.poll(() => recoveryRequested).toBe(true);
    await expect(modal).not.toBeVisible();
  });

  test("keeps the site-wide scrollbar and action-size tokens explicit", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).toContain("::-webkit-scrollbar-thumb");
    expect(styles).toContain("scrollbar-color:");
    expect(styles).toMatch(/\.dashboard-action\s*\{[\s\S]*?height:\s*32px/);
    expect(styles).toMatch(/\.chat-message__copy\s*\{[\s\S]*?height:\s*25px/);
    expect(styles).toMatch(/\.chat-compose \.chat-send\s*\{[\s\S]*?height:\s*44px/);
  });

  test("keeps direct-touch controls free from a shared glass gradient", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).toContain("@media (hover:none) and (pointer:coarse)");
    expect(styles).not.toContain("backdrop-filter:blur(12px)");
    expect(styles).not.toContain("background-image:linear-gradient");
    expect(styles).toContain("-webkit-user-select:none");
  });

  test("keeps every visible iPhone button and pulldown on an opaque surface", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);

    const surfaces = await page.evaluate(() => [...document.querySelectorAll("button, select")]
      .filter((element) => {
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return bounds.width > 0 && bounds.height > 0 && style.visibility !== "hidden";
      })
      .map((element) => {
        const style = getComputedStyle(element);
        return { backgroundImage: style.backgroundImage, backdropFilter: style.backdropFilter };
      }));

    expect(surfaces.length).toBeGreaterThan(0);
    expect(surfaces.every((surface) => surface.backgroundImage === "none")).toBe(true);
    expect(surfaces.every((surface) => surface.backdropFilter === "none")).toBe(true);
    const optionsToggle = await page.getByTestId("titlebar-options-toggle").evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        appearance: style.appearance,
        backgroundImage: style.backgroundImage,
      };
    });
    expect(optionsToggle.appearance).toBe("none");
    expect(optionsToggle.backgroundImage).toBe("none");
  });

  test("keeps the execution-details modal as compact as the report modal on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal");
    const panel = modal.locator(".prompt-detail-modal__panel");

    await modal.evaluate((element) => {
      document.querySelector("#promptHistoryDetailContent").innerHTML =
        "<p>Uitvoeringsbewijs</p>".repeat(100);
      element.showModal();
    });

    const box = await panel.boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBeGreaterThanOrEqual(10);
    expect(box.width).toBeLessThanOrEqual(390 * 0.94 + 1);
    expect(box.height).toBeLessThanOrEqual(844 * 0.9 + 1);
    await expect(panel).toHaveCSS("border-top-left-radius", "18px");
    await expect(page.locator("#promptHistoryDetailContent")).toHaveCSS("scrollbar-gutter", "stable both-edges");
    await expect(page.locator("#promptHistoryDetailContent")).toHaveCSS("padding-left", "8px");
    await expect(page.locator("#promptHistoryDetailContent")).toHaveCSS("padding-right", "8px");
    expect(await page.locator("#promptHistoryDetailContent").evaluate(
      (element) => element.scrollHeight > element.clientHeight,
    )).toBe(true);
  });

  test("keeps confirmation actions above iPhone browser chrome for long copy", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#confirmationModal");
    await modal.evaluate((element) => {
      element.querySelector("#confirmationModalText").textContent = "Deze bevestiging bevat extra toelichting. ".repeat(80);
      element.showModal();
    });

    const actions = modal.locator("#confirmationModalCancel, #confirmationModalConfirm");
    await expect(actions).toHaveCount(2);
    await expect(actions.nth(0)).toBeVisible();
    await expect(actions.nth(1)).toBeVisible();
    const placement = await actions.evaluateAll((buttons) => ({
      viewportHeight: window.visualViewport?.height ?? window.innerHeight,
      bottoms: buttons.map((button) => button.getBoundingClientRect().bottom),
      // The operator merge-wait dialog shares this panel class but is closed
      // here. Scope the measurement to the open confirmation dialog.
      panelBottom: buttons[0].closest("#confirmationModal").querySelector(".confirmation-modal__panel").getBoundingClientRect().bottom,
    }));
    expect(Math.max(...placement.bottoms)).toBeLessThanOrEqual(placement.viewportHeight);
    expect(Math.max(...placement.bottoms)).toBeLessThanOrEqual(placement.panelBottom);
    await modal.evaluate((element) => element.close());
  });

  test("keeps the prompt-history AI chat inside the shared iPhone safe area", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const detailModal = page.locator("#promptHistoryDetailModal");
    const chatModal = page.locator("#promptHistoryChatModal");

    await detailModal.evaluate((element) => element.showModal());
    const detailBox = await detailModal.locator(".prompt-detail-modal__panel").boundingBox();
    await detailModal.evaluate((element) => element.close());
    await chatModal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message">Bericht</article>'.repeat(100);
      element.showModal();
    });
    const chatBox = await chatModal.locator(".prompt-chat-modal__panel").boundingBox();

    expect(detailBox).not.toBeNull();
    expect(chatBox).not.toBeNull();
    for (const box of [detailBox, chatBox]) {
      expect(box.x).toBeGreaterThanOrEqual(16);
      expect(box.x + box.width).toBeLessThanOrEqual(374);
      expect(box.y).toBeGreaterThanOrEqual(16);
      expect(box.y + box.height).toBeLessThanOrEqual(828);
    }
    expect(chatBox.height).toBeLessThanOrEqual(844 * 0.9 + 1);
    expect(await page.locator("#chatMessages").evaluate(
      (element) => element.scrollHeight > element.clientHeight,
    )).toBe(true);
  });

  test("shows the refresh timestamp in the bottom status bar", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#currentTime")).toHaveCount(0);
    await expect(page.locator(".footer #lastRefresh")).toContainText(
      "Laatst bijgewerkt:",
    );
    await expect(page.locator(".footer #updateMode")).toContainText(
      "Serverpush:",
    );
  });

  test("stacks footer status facts without overlap in narrow layouts", async ({ page }) => {
    for (const viewport of [{ width: 820, height: 760 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      const rows = await page.locator(".footer__item").evaluateAll((items) => items.map((item) => {
        const bounds = item.getBoundingClientRect();
        return { top: Math.round(bounds.top), bottom: Math.round(bounds.bottom) };
      }));
      expect(rows).toHaveLength(3);
      expect(rows[1].top).toBeGreaterThanOrEqual(rows[0].bottom);
      expect(rows[2].top).toBeGreaterThanOrEqual(rows[1].bottom);
    }
  });

  test("uses the selected locale service for copy and date formatting", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await selectDashboardLocale(page, "de");
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(page.locator(".footer #lastRefresh")).toContainText("Zuletzt aktualisiert:");
    await expect(page.locator("#dashboardLocale option:checked")).toHaveText("Deutsch");
  });

  test("changes visible interface copy for each supported language", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0 } },
    }));
    const expectations = [
      ["en", "Language", "Refresh automatically", "AI analysis", "Passed", "Execution", "Resume Queue", "Active execution", "Execution queue", "New assignments wait for execution in order of creation date.", "EP Operations", "Loading data…", "Diagnostics", "Engineering Platform version", "Automatic refresh is off"],
      ["nl", "Taal", "Automatisch vernieuwen", "AI-analyse", "Geslaagd", "Uitvoering", "Wachtrij hervatten", "Lopende uitvoering", "Wachtrij voor uitvoeringen", "Nieuwe opdrachten wachten op uitvoering in volgorde van aanmaakdatum.", "EP Operations", "Gegevens laden…", "Diagnose", "Engineering Platform-versie", "Automatisch vernieuwen is uit"],
      ["de", "Sprache", "Automatisch aktualisieren", "KI-Analyse", "Erfolgreich", "Ausführung", "Warteschlange fortsetzen", "Laufende Ausführung", "Ausführungswarteschlange", "Neue Aufträge warten in der Reihenfolge ihres Erstellungsdatums auf die Ausführung.", "EP Operations", "Daten werden geladen…", "Diagnose", "Engineering-Plattformversion", "Automatische Aktualisierung ist aus"],
      ["fr", "Langue", "Actualiser automatiquement", "Analyse IA", "Réussi", "Exécution", "Reprendre la file", "Exécution en cours", "File d’exécution", "Les nouvelles tâches attendent leur exécution dans l’ordre de leur création.", "EP Operations", "Chargement des données…", "Diagnostic", "Version d’Engineering Platform", "Actualisation automatique désactivée"],
      ["es", "Idioma", "Actualizar automáticamente", "Análisis de IA", "Superado", "Ejecución", "Reanudar cola", "Ejecución en curso", "Cola de ejecuciones", "Las nuevas tareas esperan ejecución por orden de fecha de creación.", "EP Operations", "Cargando datos…", "Diagnóstico", "Versión de Engineering Platform", "Actualización automática desactivada"],
    ];

    for (const [language, localeLabel, refreshLabel, analysisLabel, passLabel, detailTitle, queueAction, activePrompt, queueTitle, queueDescription, dashboardTitle, splashLoading, diagnosticsTitle, platformVersionLabel, refreshOffLabel] of expectations) {
      const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await statusLoaded;
      await page.waitForTimeout(0);
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator('.dashboard-locale > span[data-i18n="language.label"]')).toHaveText(localeLabel);
      await expect(page.locator(".auto-refresh-toggle span")).toHaveText(refreshLabel);
      await expect(page.locator("#promptHistoryAnalysisHeader")).toHaveText(analysisLabel);
      await expect(page.locator("#predecessorRetry")).toHaveText(queueAction);
      await expect(page.locator("#currentRun > summary .label")).toHaveText(activePrompt);
      await expect(page.locator("#queueItems > summary > strong")).toHaveText(queueTitle);
      await expect(page.locator("#queueItems > summary > .category-description").first()).toHaveText(queueDescription);
      await expect(page.locator("#queueItems > summary > [data-category-description]")).toHaveCount(1);
      await expect(page.locator("#queueSummary")).not.toHaveClass(/category-description/);
      await expect(page.locator("#dashboardTitle")).toHaveText(dashboardTitle);
      await expect(page.locator("#dashboardSplashTitle")).toHaveText(dashboardTitle);
      await expect(page.locator("#dashboardSplashLoading")).toHaveText(splashLoading);
      await expect(page.locator("#technicalDetails > summary > strong")).toHaveText(diagnosticsTitle);
      await expect(page.locator("#platformVersionLabel")).toHaveText(platformVersionLabel);
      await page.locator("#autoRefresh").check();
      await page.locator("#autoRefresh").uncheck();
      await expect(page.locator("#updateMode")).toHaveText(refreshOffLabel);
      expect(await page.title()).toBe(dashboardTitle);
      expect(await page.evaluate(() => enumLabel("PASS"))).toBe(passLabel);
      await page.evaluate(() => renderPromptHistoryDetail({
        history: { run_id: "inbox-localization", status: "COMPLETE", title: "Prompt", executed_at: "2026-08-03T20:53:29Z" },
      }));
      await expect(page.locator("#promptHistoryDetailContent h3").first()).toHaveText(detailTitle);
    }
  });

  test("localizes the AI chat question placeholder for every supported language", async ({ page }) => {
    const expectations = [
      ["en", "For example: what are the most important next steps from this report?"],
      ["nl", "Bijvoorbeeld: wat zijn de belangrijkste vervolgstappen uit dit rapport?"],
      ["de", "Zum Beispiel: Was sind die wichtigsten nächsten Schritte aus diesem Bericht?"],
      ["fr", "Par exemple : quelles sont les principales étapes suivantes de ce rapport ?"],
      ["es", "Por ejemplo: ¿cuáles son los pasos siguientes más importantes de este informe?"],
    ];

    for (const [language, placeholder] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("#chatInput")).toHaveAttribute("placeholder", placeholder);
    }
  });

  test("localizes dashboard chrome and dynamic runtime copy for every supported language", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "IDLE", queue_depth: 0 } },
    }));
    const expectations = [
      ["en", "Workspace location", "Specialist reviewers", "Run cumulative input tokens", "Use reset"],
      ["nl", "Werkruimtelocatie", "Specialistische reviewers", "Cumulatieve invoertokens van de run", "Gebruik reset"],
      ["de", "Arbeitsbereichspfad", "Spezialisierte Reviewer", "Kumulative Eingabetoken des Durchlaufs", "Zurücksetzung verwenden"],
      ["fr", "Emplacement de l’espace de travail", "Évaluateurs spécialisés", "Jetons d’entrée cumulés de l’exécution", "Utiliser la réinitialisation"],
      ["es", "Ubicación del espacio de trabajo", "Revisores especializados", "Tokens de entrada acumulados de la ejecución", "Usar restablecimiento"],
    ];
    for (const [language, workspaceLocation, reviewers, inputTokens, reset] of expectations) {
      const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      // Do not inject the localized runtime projection before the initial
      // snapshot response has completed; it could otherwise overwrite usage.
      await statusLoaded;
      // This test owns the injected runtime projection for all five locales.
      // Disable periodic refreshes so an unrelated later snapshot cannot
      // replace its usage payload halfway through the assertion loop.
      await page.locator("#autoRefresh").uncheck();
      // Selecting a locale persists the choice and reloads the document.
      // Wait for the new dashboard before injecting its runtime projection;
      // otherwise CI can write usage into the outgoing document and assert
      // against an empty replacement page.
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await page.waitForFunction(() => typeof window.r === "function");
      await page.evaluate(() => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE",
        run_id: "inbox-localized-runtime",
        current_phase: "CAPABILITY_REVIEW",
        reviewer_agents: [{ reviewer: "validation", capability: "ENGINEERING", status: "running" }],
      }, {
        usage: { input_tokens: 42 },
        rate_limits: { provider: "codex_cli", reset_credits: 1 },
      }));
      await expect(page.locator("#workspaceCard .field .label").nth(1)).toHaveText(workspaceLocation);
      await expect(page.locator("#activeReviewerAgents strong")).toHaveText(reviewers);
      await expect(page.locator("#usageDetails")).toContainText(inputTokens);
      await expect(page.locator("#rateLimitReset")).toHaveText(reset);
    }
  });

  test("projects cumulative provider input without relabeling it as request context", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await selectDashboardLocale(page, "en");
    await page.evaluate(() => renderPromptHistoryDetail({
      history: { run_id: "inbox-provider-usage", status: "COMPLETE", title: "Provider usage", executed_at: "2026-08-18T12:00:00Z" },
      usage: {
        input_tokens: 400,
        max_input_tokens_per_invocation: 300,
        actual_single_request_context_size: "UNAVAILABLE",
        active_context_size: "UNAVAILABLE",
        speed_state: "UNKNOWN",
        usage_authority: "AUTHORITATIVE",
      },
    }));

    expect(await page.locator("#promptHistoryDetailContent .prompt-detail-card")
      .filter({ hasText: "AI Provider Usage" })
      .locator(".field")
      .evaluateAll((fields) => fields.map((field) => [
        field.querySelector(".label")?.textContent,
        field.querySelector(".label")?.nextElementSibling?.textContent,
      ])))
      .toEqual([
        ["Run cumulative input tokens", "400"],
        ["Maximum provider invocation cumulative input", "300"],
        ["Actual single-request context size", "Unavailable"],
        ["Active context size", "Unavailable"],
        ["Speed state", "Unknown"],
        ["Usage authority", "Provider-observed"],
      ]);
  });

  test("shows linked Managed implementation and finalization PR evidence in execution details", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await selectDashboardLocale(page, "nl");
    await page.evaluate(() => renderPromptHistoryDetail({
      history: { run_id: "inbox-pr-evidence", status: "COMPLETE", title: "PR-evidence", executed_at: "2026-08-26T12:00:00Z", execution_mode: "MANAGED" },
      pull_requests: [
        { role: "implementation", number: 948, url: "https://github.com/pcvantol/djconnect/pull/948" },
        { role: "finalization", number: 949, url: "https://github.com/pcvantol/djconnect/pull/949" },
      ],
    }));
    await page.locator("#promptHistoryDetailModal").evaluate((modal) => modal.showModal());
    const card = page.locator("#promptHistoryDetailContent .prompt-detail-card--pull-requests");
    await expect(card.locator("h3")).toHaveText(DASHBOARD_MESSAGES.nl["detail.pull_requests"]);
    await expect(card.locator("a")).toHaveCount(2);
    await expect(card.locator("a").nth(0)).toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/948");
    await expect(card.locator("a").nth(0)).toHaveText("#948 ↗");
    await card.locator("a").nth(0).hover();
    await expect(card.locator("a").nth(0)).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(card.locator("a").nth(1)).toHaveAttribute("href", "https://github.com/pcvantol/djconnect/pull/949");
    const [contextBounds, cardBounds] = await Promise.all([
      page.locator("#promptHistoryDetailContent .prompt-detail-card--execution-context").boundingBox(),
      card.boundingBox(),
    ]);
    expect(contextBounds).not.toBeNull();
    expect(cardBounds).not.toBeNull();
    expect(cardBounds.x).toBeGreaterThan(contextBounds.x - 1);
    expect(cardBounds.y).toBeGreaterThan(contextBounds.y);

    await page.evaluate(() => renderPromptHistoryDetail({
      history: { run_id: "inbox-genesis-evidence", status: "COMPLETE", title: "Genesis", executed_at: "2026-08-26T12:00:00Z", execution_mode: "GENESIS" },
      pull_requests: [],
    }));
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-card--pull-requests")).toHaveCount(0);
  });

  test("pairs the verified commit timeline beside provider usage without extending the lifecycle", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await selectDashboardLocale(page, "nl");
    await page.evaluate(() => {
      renderPromptHistoryDetail({
        history: { run_id: "inbox-provider-review-layout", status: "COMPLETE", title: "Provider and reviews", executed_at: "2026-08-24T20:00:00Z" },
        usage: { input_tokens: 400, output_tokens: 40, provider_invocation_count: 8 },
        commit_timeline: Array.from({ length: 16 }, (_, index) => ({
          phase: "REPAIR_AGENT",
          observed_at: `2026-08-24T20:${String(index).padStart(2, "0")}:00+00:00`,
          commit_sha: String(index + 1).padStart(40, "a"),
          description: "pull_request_repair_commit_verified",
        })),
        reviewers: [
          { reviewer: "validation", capability: "ENGINEERING", status: "completed", accepted_recommendations: 2, selected_because: "validation-related objective" },
          { reviewer: "documentation", capability: "ENGINEERING", status: "completed", accepted_recommendations: 1, selected_because: "documentation-oriented objective" },
        ],
      });
      document.querySelector("#promptHistoryDetailModal").showModal();
    });

    const pair = page.locator("#promptHistoryDetailContent .prompt-detail-provider-review");
    const cards = pair.locator(".prompt-detail-card");
    await expect(cards).toHaveCount(2);
    const [usageBounds, timelineBounds] = await Promise.all([
      cards.nth(0).boundingBox(), cards.nth(1).boundingBox(),
    ]);
    expect(usageBounds).not.toBeNull();
    expect(timelineBounds).not.toBeNull();
    expect(timelineBounds.x).toBeGreaterThan(usageBounds.x);
    expect(Math.abs(timelineBounds.y - usageBounds.y)).toBeLessThanOrEqual(1);
    await expect(cards.nth(1)).toHaveCSS("align-self", "stretch");
    const timelineList = cards.nth(1).locator(".prompt-detail-commit-timeline__list");
    await expect(timelineList).toHaveCSS("overflow-y", "auto");
    await expect(timelineList).toHaveCSS("flex-grow", "1");
    await expect(timelineList).toHaveCSS("align-content", "start");
    const phaseCaption = cards.nth(1).locator(".prompt-detail-commit-timeline__phase h4");
    await expect(phaseCaption.locator(".prompt-detail-commit-timeline__kind")).toHaveText(DASHBOARD_MESSAGES.nl["detail.commit_type.repair"]);
    await expect(phaseCaption.locator(".prompt-detail-commit-timeline__phase-name")).toHaveText(DASHBOARD_MESSAGES.nl["state.REPAIR_AGENT"]);
    const stack = page.locator("#promptHistoryDetailContent .prompt-detail-provider-review-stack");
    const reviewersCard = stack.locator(".prompt-detail-card--reviewers");
    await expect(reviewersCard).toHaveCount(1);
    const [stackBounds, reviewerBounds] = await Promise.all([
      stack.boundingBox(), reviewersCard.boundingBox(),
    ]);
    expect(stackBounds).not.toBeNull();
    expect(reviewerBounds).not.toBeNull();
    expect(reviewerBounds.x).toBeGreaterThanOrEqual(stackBounds.x - 1);
    expect(reviewerBounds.x + reviewerBounds.width).toBeLessThanOrEqual(stackBounds.x + stackBounds.width + 1);

    await page.setViewportSize({ width: 390, height: 844 });
    const [narrowUsage, narrowTimeline] = await Promise.all([
      cards.nth(0).boundingBox(), cards.nth(1).boundingBox(),
    ]);
    expect(narrowUsage).not.toBeNull();
    expect(narrowTimeline).not.toBeNull();
    expect(Math.abs(narrowTimeline.x - narrowUsage.x)).toBeLessThanOrEqual(1);
    expect(narrowTimeline.y).toBeGreaterThan(narrowUsage.y);
  });

  test("formats preflight timestamps through the selected dashboard locale", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    expect(await page.evaluate(() => [
      formatTimestamp("2026-08-03T20:53:29.203403+00:00"),
      formatTimestamp("2026-08-03T20:53:29.354948+00:00"),
    ])).toEqual([
      "maandag 3 augustus 2026 om 22:53:29",
      "maandag 3 augustus 2026 om 22:53:29",
    ]);
  });

  test("puts preflight diagnostic clauses on separate lines", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      diagnostic: "Workspace Preflight blocked by worktree_unstaged (Repository). Expected: worktree_unstaged: PASS. Observed: Unstaged changes are present. Required action: Commit, stash, or remove unstaged changes before execution.",
    }, {}));
    await expect(page.locator("#diag")).toHaveText(
      "Workspace Preflight blocked by worktree_unstaged (Repository).\nExpected: worktree_unstaged: PASS.\nObserved: Unstaged changes are present.\nRequired action: Commit, stash, or remove unstaged changes before execution.",
    );
    await expect(page.locator("#diag")).toHaveCSS("white-space", "pre-line");
  });

  test("renders host, workspace and capability preflight fields through one presentation", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" }, build_commit: "" },
    }));
    const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await statusLoaded;
    await page.evaluate(() => r({}, {
      host_preflight: { outcome: "PASS", timestamp: "2026-08-03T20:53:29Z" },
      workspace_preflight: { outcome: "FAIL", timestamp: "2026-08-03T20:53:29Z" },
      capability_preflight: {
        outcome: "PASS",
        recoverability: "RETRYABLE",
        failure_origin: "CAPABILITY",
        recommendation: "Capability admission passed.",
      },
    }));
    await expect(page.locator("#hostPreflightStatus")).toHaveText("Geslaagd");
    await expect(page.locator("#workspacePreflightStatus")).toHaveText("Mislukt");
    await expect(page.locator("#capabilityPreflightStatus")).toHaveText("Geslaagd");
    await expect(page.locator("#capabilityRecoverability")).toHaveText("Opnieuw proberen mogelijk");
    await expect(page.locator("#capabilityFailureOrigin")).toHaveText("Capability");
    await expect(page.locator("#capabilityRecommendation")).toHaveText("Capabilitytoelating geslaagd.");
  });

  test("localizes dynamically rendered telemetry copy for every supported language", async ({ page }, testInfo) => {
    // This contract deliberately reloads the dashboard once per supported
    // locale. Give that bounded five-reload sequence room on a busy CI host;
    // it prevents a worker teardown from masquerading as a browser failure.
    testInfo.setTimeout(60_000);
    const expectations = [
      ["en", "Execution Host telemetry", "Operational trends for the last 90 days. Telemetry is not repository evidence."],
      ["nl", "Execution Host-telemetrie", "Operationele trends van de laatste 90 dagen. Telemetrie is geen repositorybewijs."],
      ["de", "Execution-Host-Telemetrie", "Betriebstrends der letzten 90 Tage. Telemetrie ist kein Repository-Nachweis."],
      ["fr", "Télémétrie de l’hôte d’exécution", "Tendances opérationnelles des 90 derniers jours. La télémétrie n’est pas une preuve de dépôt."],
      ["es", "Telemetría del host de ejecución", "Tendencias operativas de los últimos 90 días. La telemetría no es evidencia del repositorio."],
    ];
    for (const [language, title, description] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await page.waitForFunction(
        () => typeof window.executionTelemetry === "function",
      );
      await page.evaluate(() => {
        document.querySelector("#executionTelemetry")?.remove();
        window.executionTelemetry([]);
      });
      await expect(page.locator("#executionTelemetry summary strong")).toHaveText(title);
      await expect(page.locator("#executionTelemetry .category-description")).toHaveText(description);
    }
  });

  test("shows one recovered terminal run on its original telemetry date", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.waitForFunction(() => typeof window.executionTelemetry === "function");
    await page.evaluate(() => {
      document.querySelector("#executionTelemetry")?.remove();
      // This is the idempotent API projection after terminal recovery: the
      // browser receives a single daily count, never a recovery event stream.
      window.executionTelemetry([{
        date: "2026-08-25", prompt_count: 1, average_total_execution_seconds: 1443.574,
        average_queue_wait_seconds: 1.074, complete_count: 1, blocked_count: 0, failed_count: 0,
      }]);
    });
    const row = page.locator("#executionTelemetryRows .telemetry-row");
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("25-08-2026");
    await expect(row).toContainText("1");
  });

  test("localizes the telemetry detail title for every supported language", async ({ page }) => {
    const expectations = [
      ["en", "Execution Telemetry — 24-08-2026"],
      ["nl", "Uitvoeringstelemetrie — 24-08-2026"],
      ["de", "Ausführungstelemetrie — 24-08-2026"],
      ["fr", "Télémétrie d’exécution — 24-08-2026"],
      ["es", "Telemetría de ejecución — 24-08-2026"],
    ];
    await page.route("**/api/telemetry/**", (route) => route.fulfill({ json: {
      summary: {}, phases: [], bottlenecks: { top_time_consumers: [] }, runs: [],
    } }));
    for (const [language, title] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await page.locator("#autoRefresh").uncheck();
      await page.waitForFunction(() => typeof window.executionTelemetry === "function");
      await page.evaluate(() => window.executionTelemetry([{
        date: "2026-08-24", prompt_count: 1, average_total_execution_seconds: 0,
        average_queue_wait_seconds: 0, complete_count: 1, blocked_count: 0, failed_count: 0,
      }]));
      await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
      await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
      const telemetryRow = page.locator("#executionTelemetryRows .telemetry-row");
      await expect(telemetryRow).toHaveCount(1);
      await telemetryRow.evaluate((row) => row.click());
      await expect(page.locator("#telemetryDetailTitle")).toHaveText(title);
      await page.locator("#telemetryDetailClose").click();
    }
  });

  test("gives every table a coloured first column and sorts telemetry columns", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/telemetry*", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: {} } }));
    await page.route("**/api/events", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([
      { date: "2026-08-16", prompt_count: 4, average_total_execution_seconds: 80, average_queue_wait_seconds: 8, input_tokens: 400, output_tokens: 40, total_tokens: 440, complete_count: 4, blocked_count: 0, failed_count: 0 },
      { date: "2026-08-15", prompt_count: 1, average_total_execution_seconds: 40, average_queue_wait_seconds: 4, input_tokens: 100, output_tokens: 10, total_tokens: 110, complete_count: 1, blocked_count: 0, failed_count: 0 },
    ]));
    await page.getByTestId("theme-toggle").click();
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });

    const telemetryScroll = page.locator("#executionTelemetry .telemetry-scroll");
    await expect(telemetryScroll).toHaveCSS("border-top-style", "solid");
    await expect(telemetryScroll).toHaveCSS("border-top-left-radius", "9px");
    const firstColumnSurfaces = await page.locator(":is(.log-table,.telemetry-table) th:first-child").evaluateAll(
      (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
    );
    expect(firstColumnSurfaces.length).toBeGreaterThan(2);
    expect(firstColumnSurfaces.every((colour) => colour !== "rgba(0, 0, 0, 0)")).toBe(true);
    const telemetryHeaderSurfaces = await page.locator("#executionTelemetry .telemetry-table th:not(:first-child)").evaluateAll(
      (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
    );
    expect(telemetryHeaderSurfaces.length).toBeGreaterThan(1);
    expect(telemetryHeaderSurfaces.every((colour) => colour === "rgb(234, 240, 248)")).toBe(true);
    const telemetryCellSurfaces = await page.locator("#executionTelemetry .telemetry-table tbody td").evaluateAll(
      (cells) => cells.map((cell) => {
        const style = getComputedStyle(cell);
        return {
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
          backdropFilter: style.backdropFilter,
        };
      }),
    );
    expect(telemetryCellSurfaces.length).toBeGreaterThan(0);
    expect(telemetryCellSurfaces.every((surface) => surface.backgroundColor !== "rgba(0, 0, 0, 0)")).toBe(true);
    expect(telemetryCellSurfaces.every((surface) => surface.backgroundImage === "none")).toBe(true);
    expect(telemetryCellSurfaces.every((surface) => surface.backdropFilter === "none")).toBe(true);

    const date = page.locator('#executionTelemetry th[data-sort-key="date"]');
    await expect(date).toHaveAttribute("data-sort-indicator", "↓");
    const [telemetryHeader, logHeader] = await Promise.all([
      date.evaluate((header) => {
        const icon = getComputedStyle(header, "::after"), text = getComputedStyle(header);
        return { fontFamily: text.fontFamily, fontSize: text.fontSize, iconFontFamily: icon.fontFamily, iconFontSize: icon.fontSize, iconFontWeight: icon.fontWeight, iconLineHeight: icon.lineHeight };
      }),
      page.locator("#componentLogs .log-table th.log-sortable").first().evaluate((header) => {
        const icon = getComputedStyle(header, "::after"), text = getComputedStyle(header);
        return { fontFamily: text.fontFamily, fontSize: text.fontSize, iconFontFamily: icon.fontFamily, iconFontSize: icon.fontSize, iconFontWeight: icon.fontWeight, iconLineHeight: icon.lineHeight };
      }),
    ]);
    expect(telemetryHeader).toEqual(logHeader);
    await expect(page.locator("#executionTelemetryRows tr td").first()).toHaveText("16-08-2026");
    await dispatchDashboardPointerClick(date);
    await expect(date).toHaveAttribute("aria-sort", "ascending");
    await expect(date).toHaveAttribute("data-sort-indicator", "↑");
    await expect(page.locator("#executionTelemetryRows tr td").first()).toHaveText("15-08-2026");
    const prompts = page.locator('#executionTelemetry th[data-sort-key="prompt_count"]');
    await dispatchDashboardPointerClick(prompts);
    await expect(prompts).toHaveAttribute("aria-sort", "ascending");
    await expect(page.locator("#executionTelemetryRows tr td").first()).toHaveText("15-08-2026");
  });

  test("paginates execution telemetry at seven days per page", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => window.executionTelemetry(Array.from({ length: 8 }, (_, index) => ({
      date: `2026-08-${String(24 - index).padStart(2, "0")}`,
      prompt_count: index + 1,
    }))));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    const rows = page.locator("#executionTelemetryRows .telemetry-row");
    await expect(rows).toHaveCount(7);
    const pagination = page.locator("#executionTelemetryPagination");
    await expect(pagination).toHaveText(/Pagina 1 van 2 · 8 dagen/);
    await pagination.getByRole("button", { name: "Volgende" }).click();
    await expect(rows).toHaveCount(1);
    await expect(pagination).toHaveText(/Pagina 2 van 2 · 8 dagen/);
  });

  test("offers download, copy and confirmed clear actions for telemetry", async ({ page }) => {
    await page.route("**/api/telemetry/clear", (route) => route.fulfill({ json: { cleared: true, execution_runs: 1, daily_statistics: 1 } }));
    await page.route("**/api/events", (route) => route.fulfill({ json: {} }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-24", prompt_count: 1, average_total_execution_seconds: 60,
      average_queue_wait_seconds: 5, complete_count: 1, blocked_count: 0, failed_count: 0,
    }]));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    const actions = page.locator("#executionTelemetry .telemetry-actions");
    await expect(actions).toHaveCSS("justify-content", "flex-end");
    await expect(actions.getByRole("button", { name: "Telemetrie downloaden" })).toBeEnabled();
    await expect(actions.getByRole("button", { name: "Telemetrie kopiëren" })).toBeEnabled();
    await actions.getByRole("button", { name: "Telemetrie wissen" }).click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#executionTelemetryRows .telemetry-empty")).toBeVisible();
    await expect(actions.getByRole("button", { name: "Telemetrie wissen" })).toBeDisabled();
  });

  test("sorts telemetry detail tables with the same header treatment as logs", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      summary: {},
      phases: [
        { phase: "QUEUE_WAIT", average_ms: 6000, median_ms: 1000, total_ms: 17000, share_percent: 20, runs: 3 },
        { phase: "INITIALIZE", average_ms: 0, median_ms: 0, total_ms: 0, share_percent: 0, runs: 3 },
      ],
      bottlenecks: { top_time_consumers: [] },
      runs: [
        { run_id: "run-slower", started_at: "2026-08-16T14:33:31Z", status: "FAILED", total_duration_ms: 272000 },
        { run_id: "run-faster", started_at: "2026-08-16T14:30:31Z", status: "COMPLETE", total_duration_ms: 1000 },
      ],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-16", prompt_count: 1, average_total_execution_seconds: 0,
      average_queue_wait_seconds: 0, complete_count: 1, blocked_count: 0, failed_count: 0,
    }]));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    await dispatchDashboardPointerClick(page.locator("#executionTelemetryRows .telemetry-row"));
    const tables = page.locator("#telemetryDetailContent .telemetry-table");
    const phaseTable = tables.nth(0), runTable = tables.nth(1);
    await expect(phaseTable).toHaveClass(/telemetry-phase-table/);
    await expect(phaseTable.locator("th.log-sortable")).toHaveCount(6);
    await expect(runTable.locator("th.log-sortable")).toHaveCount(12);
    const phaseScroll = phaseTable.locator("xpath=.."), runScroll = runTable.locator("xpath=..");
    for (const scroll of [phaseScroll, runScroll]) {
      await expect(scroll).toHaveClass(/telemetry-detail-table-scroll/);
      await expect(scroll).toHaveAttribute("role", "region");
      await expect(scroll).not.toHaveAttribute("tabindex");
      await expect(scroll).toHaveCSS("border-top-style", "solid");
      await expect(scroll).toHaveCSS("border-top-width", "1px");
      await expect(scroll).toHaveCSS("border-top-left-radius", "9px");
      expect(await scroll.evaluate((element) => getComputedStyle(element).borderTopColor)).toBe(
        await page.locator("#executionTelemetry .telemetry-table th").first().evaluate((element) => getComputedStyle(element).borderBottomColor),
      );
    }
    const runScrollGeometry = await runScroll.evaluate((element) => {
      const modalContent = element.closest(".telemetry-detail-modal__content");
      return {
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        modalOverflowX: modalContent ? getComputedStyle(modalContent).overflowX : null,
      };
    });
    expect(runScrollGeometry.scrollWidth).toBeGreaterThan(runScrollGeometry.clientWidth);
    expect(runScrollGeometry.modalOverflowX).toBe("hidden");
    const phaseAverage = phaseTable.locator('th[data-sort-key="average_ms"]');
    await expect(phaseAverage).toHaveAttribute("data-sort-indicator", "↕");
    await dispatchDashboardPointerClick(phaseAverage);
    await expect(phaseAverage).toHaveAttribute("aria-sort", "ascending");
    await expect(phaseTable.locator("tbody tr td").first()).toHaveText("INITIALIZE");
    const runTotal = runTable.locator('th[data-sort-key="total_duration_ms"]');
    await dispatchDashboardPointerClick(runTotal);
    await expect(runTable.locator("tbody tr td").first()).toHaveText("run-faster");
    const phaseRow = phaseTable.locator("tbody tr").first();
    const phaseRowBackgrounds = await phaseRow.locator("td").evaluateAll(
      (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
    );
    expect(new Set(phaseRowBackgrounds).size).toBeGreaterThan(1);
    await phaseRow.hover();
    const hoveredPhaseRowBackgrounds = await phaseRow.locator("td").evaluateAll(
      (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
    );
    expect(new Set(hoveredPhaseRowBackgrounds).size).toBe(1);
    const [detailHeader, logHeader] = await Promise.all([
      phaseAverage.evaluate((header) => {
        const icon = getComputedStyle(header, "::after"), text = getComputedStyle(header);
        return { fontFamily: text.fontFamily, fontSize: text.fontSize, iconFontFamily: icon.fontFamily, iconFontSize: icon.fontSize, iconFontWeight: icon.fontWeight, iconLineHeight: icon.lineHeight };
      }),
      page.locator("#componentLogs .log-table th.log-sortable").first().evaluate((header) => {
        const icon = getComputedStyle(header, "::after"), text = getComputedStyle(header);
        return { fontFamily: text.fontFamily, fontSize: text.fontSize, iconFontFamily: icon.fontFamily, iconFontSize: icon.fontSize, iconFontWeight: icon.fontWeight, iconLineHeight: icon.lineHeight };
      }),
    ]);
    expect(detailHeader).toEqual(logHeader);
    await page.setViewportSize({ width: 390, height: 844 });
    const phaseScrollGeometry = await phaseScroll.evaluate((element) => {
      element.scrollLeft = 80;
      return { clientWidth: element.clientWidth, scrollLeft: element.scrollLeft, scrollWidth: element.scrollWidth };
    });
    expect(phaseScrollGeometry.scrollWidth).toBeGreaterThan(phaseScrollGeometry.clientWidth);
    expect(phaseScrollGeometry.scrollLeft).toBeGreaterThan(0);
  });

  test("keeps the telemetry detail modal within a mobile portrait viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#telemetryDetailModal");
    await modal.evaluate((element) => {
      const content = element.querySelector("#telemetryDetailContent");
      content.innerHTML = "<section style='height: 1800px'>Long telemetry evidence</section>";
      element.showModal();
    });
    const layout = await modal.evaluate((element) => {
      const panel = element.querySelector(".telemetry-detail-modal__panel");
      const content = element.querySelector("#telemetryDetailContent");
      const bounds = element.getBoundingClientRect();
      return {
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        bottom: bounds.bottom,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        panelHeight: panel.getBoundingClientRect().height,
        contentClientHeight: content.clientHeight,
        contentScrollHeight: content.scrollHeight,
      };
    });
    expect(layout.left).toBeGreaterThanOrEqual(24);
    expect(layout.right).toBeLessThanOrEqual(layout.viewportWidth - 24);
    expect(layout.top).toBeGreaterThanOrEqual(24);
    expect(layout.bottom).toBeLessThanOrEqual(layout.viewportHeight - 24);
    expect(layout.panelHeight).toBeLessThanOrEqual(layout.viewportHeight - 48);
    expect(layout.contentScrollHeight).toBeGreaterThan(layout.contentClientHeight);
    await modal.evaluate((element) => element.close());
  });

  test("renders telemetry run IDs as text actions instead of round icon buttons", () => {
    const script = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.js"), "utf8");
    const stylesheet = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8");
    expect(script).toContain('id.className = "telemetry-run-link"');
    expect(script).not.toContain('id.className = "dashboard-action"; id.textContent = run.run_id');
    expect(stylesheet).toContain(".telemetry-run-link{");
    expect(stylesheet).toContain("min-height:0;min-width:0");
    expect(stylesheet).toContain("text-decoration:none");
    expect(stylesheet).toContain(".telemetry-run-link:is(:hover,:active){background:transparent!important;color:inherit!important}");
    expect(stylesheet).toContain(".telemetry-run-link:is(:focus,:focus-visible){border-color:transparent!important;box-shadow:none!important;outline:0!important}");
    expect(stylesheet).toContain("#telemetryDetailContent .telemetry-run-link:is(:focus,:focus-visible,:active){border-color:transparent!important;box-shadow:none!important;outline:0!important}");
    expect(stylesheet).toContain(".telemetry-detail-modal .telemetry-run-link{");
    expect(stylesheet).toContain("Telemetry run IDs are text links in a data table");
  });

  test("uses the full rose row treatment for telemetry runs in the detail modal", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.abort());
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      summary: {}, phases: [], bottlenecks: { top_time_consumers: [] }, runs: [{
        run_id: "inbox-telemetry-row", status: "COMPLETE", total_duration_ms: 1000,
        queue_wait_ms: 0, provider_duration_ms: 500, validation_duration_ms: 200,
        external_wait_ms: 0, largest_phase: "PROVIDER_EXECUTION",
      }],
    } }));
    await page.route("**/api/prompt-history/**/details", (route) => route.fulfill({ json: { history: { run_id: "inbox-telemetry-row" } } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-16", prompt_count: 1, average_execution_seconds: 0,
      average_total_execution_seconds: 0, average_queue_wait_seconds: 0,
      complete_count: 1, blocked_count: 0, failed_count: 0,
    }]));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    await dispatchDashboardPointerClick(page.locator("#executionTelemetryRows .telemetry-row"));
    const runRow = page.locator("#telemetryDetailContent .telemetry-row");
    await expect(runRow).toHaveCount(1);
    await expect(runRow.locator(".telemetry-run-link")).toHaveCSS("text-decoration-line", "none");
    await runRow.hover();
    const hoverBackgrounds = await runRow.locator("td").evaluateAll((cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor));
    expect(new Set(hoverBackgrounds).size).toBe(1);
    expect(hoverBackgrounds[0]).not.toBe("rgba(0, 0, 0, 0)");
    const runId = runRow.locator(".telemetry-run-link");
    await runId.click();
    await expect(runRow).toHaveAttribute("data-selected", "true");
    const selectedBackgrounds = await runRow.locator("td").evaluateAll((cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor));
    expect(new Set(selectedBackgrounds).size).toBe(1);
    await runId.focus();
    await expect(runId).toHaveCSS("outline-style", "none");
    await expect(runId).toHaveCSS("box-shadow", "none");
    // Prompt-history navigation has dedicated coverage. Keep this presentation
    // test scoped to the telemetry detail row so asynchronous history loading
    // cannot close an unrelated modal during the assertion.
  });

  test("uses the rose telemetry accent throughout the telemetry detail modal", async ({ page }) => {
    // Keep the fixture-owned telemetry row from being replaced by the
    // asynchronous initial dashboard snapshot while this modal is opened.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      summary: { executions: 1, completed: 1, blocked: 0, failed: 0 },
      phases: [{ phase: "PROVIDER_EXECUTION", average_ms: 5000, median_ms: 5000, total_ms: 5000, share_percent: 100, runs: 1 }],
      bottlenecks: {},
      runs: [{ run_id: "modal-header-surface", status: "COMPLETE", total_duration_ms: 5000, queue_wait_ms: 0 }],
    } }));
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await snapshotLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-16", prompt_count: 1, average_execution_seconds: 0,
      average_total_execution_seconds: 0, average_queue_wait_seconds: 0,
      complete_count: 1, blocked_count: 0, failed_count: 0,
    }]));
    await page.locator("#executionTelemetry").evaluate((element) => { element.open = true; });
    await dispatchDashboardPointerClick(page.locator("#executionTelemetryRows tr"));
    const modal = page.locator("#telemetryDetailModal");
    await expect(modal).toBeVisible();
    await expect(modal.locator("#telemetryDetailTitle")).toHaveCSS("color", "rgb(251, 113, 133)");
    expect(await modal.locator("#telemetryDetailTitle").evaluate(
      (title) => getComputedStyle(title, "::before").content,
    )).toBe('"▥"');
    await expect(modal.locator(".dashboard-modal-shell__header")).toHaveCSS("border-bottom-color", "rgb(251, 113, 133)");
    const metricLabel = modal.locator(".telemetry-detail-metrics .label").first();
    await expect(metricLabel).toBeVisible();
    const metricInk = await modal.evaluate((element) => {
      const expected = document.createElement("span");
      expected.style.color = "var(--dashboard-modal-ink)";
      element.append(expected);
      const colour = getComputedStyle(expected).color;
      expected.remove();
      return colour;
    });
    await expect(modal.locator(".telemetry-detail-metrics .field > strong").first()).toHaveCSS("color", metricInk);
    const secondaryColours = await modal.evaluate((element) => {
      const sample = document.createElement("span");
      sample.style.color = "#c7a6ff";
      const expected = document.createElement("span");
      expected.style.color = "var(--modal-secondary-accent)";
      element.append(sample, expected);
      const result = {
        expected: getComputedStyle(expected).color,
        label: getComputedStyle(element.querySelector(".telemetry-detail-metrics .label")).color,
        legacyPurple: getComputedStyle(sample).color,
      };
      sample.remove(); expected.remove();
      return result;
    });
    expect(secondaryColours.label).toBe(secondaryColours.expected);
    expect(secondaryColours.label).not.toBe(secondaryColours.legacyPurple);
    await modal.evaluate(() => { document.documentElement.dataset.theme = "light"; });
    const detailHeaderSurfaces = await modal.locator(".telemetry-table th:not(:first-child)").evaluateAll(
      (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
    );
    expect(detailHeaderSurfaces.length).toBeGreaterThan(2);
    expect(detailHeaderSurfaces.every((colour) => colour === "rgb(234, 240, 248)")).toBe(true);
    await modal.evaluate((element) => element.close());
  });

  test("derives secondary modal tokens from the modal accent instead of a category-specific fallback", () => {
    const stylesheet = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8");
    expect(stylesheet).toContain("--modal-secondary-accent:color-mix(in srgb,var(--modal-accent) 62%,#fff)");
    expect(stylesheet).toContain("--modal-subcontainer-surface:color-mix(in srgb,var(--modal-accent) 8%,var(--modal-surface))");
    expect(stylesheet).toContain(".dashboard-modal-shell :is(.label,.field>.label){color:var(--modal-secondary-accent)}");
    expect(stylesheet).not.toContain(".lifecycle-detail-modal{--modal-accent:#65c5d9;--modal-secondary-accent:");
  });

  test("formats telemetry percentages with one localized decimal place", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      summary: {},
      phases: [],
      bottlenecks: { top_time_consumers: [{ phase: "PROVIDER_EXECUTION", share_percent: 61.848 }] },
      runs: [],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-16", prompt_count: 1, average_execution_seconds: 0,
      average_total_execution_seconds: 0, average_queue_wait_seconds: 0,
      complete_count: 0, blocked_count: 1, failed_count: 0,
    }]));
    await dispatchDashboardPointerClick(page.locator("#executionTelemetry > summary"));
    await dispatchDashboardPointerClick(page.locator("#executionTelemetryRows tr"));
    await expect(page.locator("#telemetryDetailContent")).toContainText("61,8%");
  });

  test("projects canonical bottlenecks and complete per-run telemetry detail", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      phase_telemetry_available: true,
      summary: { executions: 1, completed: 1, blocked: 0, failed: 0, total_wall_time: { average_ms: 120000, median_ms: 120000 }, active_processing_time: { average_ms: 90000 }, queue_wait: { average_ms: 12000 }, provider_execution: { average_ms: 40000 }, validation: { average_ms: 10000 }, external_wait: { average_ms: 20000 }, overhead: { average_ms: 28000 } },
      phases: [{ phase: "PROVIDER_EXECUTION", average_ms: 40000, median_ms: 40000, total_ms: 40000, share_percent: 33.333, runs: 1 }],
      bottlenecks: { longest_average_phase: { phase: "PROVIDER_EXECUTION" }, largest_accumulated_phase: { phase: "PROVIDER_EXECUTION" }, shares: { queue_wait: 10, provider_execution: 33.333, validation: 8.333, external_wait: 16.667, overhead: 23.333 }, top_time_consumers: [{ phase: "PROVIDER_EXECUTION", share_percent: 33.333 }] },
      runs: [{ run_id: "inbox-phase-detail", started_at: "2026-08-16T10:00:00+00:00", status: "COMPLETE", total_duration_ms: 120000, queue_wait_ms: 12000, provider_duration_ms: 40000, validation_duration_ms: 10000, external_wait_ms: 20000, largest_phase: "PROVIDER_EXECUTION", producer_type: "INBOX", repository: "pcvantol/djconnect", model: "gpt-5.6-terra", phase_telemetry: "RECORDED" }],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{ date: "2026-08-16", prompt_count: 1, average_total_execution_seconds: 120, average_queue_wait_seconds: 12, complete_count: 1, blocked_count: 0, failed_count: 0 }]));
    await dispatchDashboardPointerClick(page.locator("#executionTelemetry > summary"));
    await dispatchDashboardPointerClick(page.locator("#executionTelemetryRows .telemetry-row"));
    const content = page.locator("#telemetryDetailContent");
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.longest_average_phase"]);
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.largest_accumulated_phase"]);
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.share.queue_wait"]);
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.start_time"]);
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.producer_type"]);
    await expect(content).toContainText(DASHBOARD_MESSAGES.nl["telemetry.target_repository"]);
    await expect(content).toContainText("gpt-5.6-terra");
    await expect(content).toContainText("33,3%");
    await expect(content.locator(".dashboard-action, .execution-dismiss, .predecessor-retry")).toHaveCount(0);
  });

  test("uses one uninterrupted selected-row treatment for telemetry rows", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/telemetry/2026-08-16", (route) => route.fulfill({ json: {
      summary: {}, phases: [], bottlenecks: { top_time_consumers: [] }, runs: [],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.executionTelemetry([{
      date: "2026-08-16", prompt_count: 1, average_execution_seconds: 0,
      average_total_execution_seconds: 0, average_queue_wait_seconds: 0,
      complete_count: 0, blocked_count: 1, failed_count: 0,
    }]));
    await dispatchDashboardPointerClick(page.locator("#executionTelemetry > summary"));
    const row = page.locator("#executionTelemetryRows .telemetry-row");
    await dispatchDashboardPointerClick(row);
    await expect(row).toHaveAttribute("data-selected", "true");
    const selection = await row.locator("td").evaluateAll((cells) => [
      getComputedStyle(cells[0]).boxShadow,
      getComputedStyle(cells[Math.floor(cells.length / 2)]).boxShadow,
      getComputedStyle(cells.at(-1)).boxShadow,
      getComputedStyle(cells[0]).backgroundColor,
      getComputedStyle(cells[0]).outlineStyle,
      getComputedStyle(cells[Math.floor(cells.length / 2)]).outlineStyle,
      getComputedStyle(cells.at(-1)).outlineStyle,
    ]);
    expect(selection[0]).toContain("2px 0px 0px 0px inset");
    expect(selection[1]).toBe("none");
    expect(selection[2]).toBe("none");
    expect(selection[3]).not.toBe("rgba(0, 0, 0, 0)");
    expect(selection.slice(4)).toEqual(["none", "none", "none"]);
    await row.focus();
    await expect(row).toHaveCSS("outline-style", "none");
    await expect(row).toHaveCSS("box-shadow", "none");
  });

  test("localizes search and level filter controls for every supported language", async ({ page }, testInfo) => {
    // This contract performs five controlled locale reloads. Keep its
    // timeout independent from unrelated worker contention in the full run.
    testInfo.setTimeout(60_000);
    const expectations = [
      ["en", "Search", "Search all fields", "Level", "All levels", "Time period", ["All dates", "Today", "Yesterday", "Specific day", "Custom range"]],
      ["nl", "Zoeken", "Zoek in alle velden", "Niveau", "Alle niveaus", "Tijdvenster", ["Alle datums", "Vandaag", "Gisteren", "Specifieke dag", "Aangepast bereik"]],
      ["de", "Suchen", "Alle Felder durchsuchen", "Stufe", "Alle Stufen", "Zeitraum", ["Alle Daten", "Heute", "Gestern", "Bestimmter Tag", "Benutzerdefinierter Zeitraum"]],
      ["fr", "Rechercher", "Rechercher dans tous les champs", "Niveau", "Tous les niveaux", "Période", ["Toutes les dates", "Aujourd’hui", "Hier", "Jour précis", "Plage personnalisée"]],
      ["es", "Buscar", "Buscar en todos los campos", "Nivel", "Todos los niveles", "Periodo", ["Todas las fechas", "Hoy", "Ayer", "Día específico", "Intervalo personalizado"]],
    ];
    for (const [language, search, placeholder, level, allLevels, timePeriod, timeOptions] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator("label[for=logFilter]")).toHaveText(search);
      await expect(page.locator("#logFilter")).toHaveAttribute("placeholder", placeholder);
      await expect(page.locator("label[for=logLevelFilter]")).toContainText(level);
      await expect(page.locator("#logLevelFilter option[value='']")).toHaveText(allLevels);
      await expect(page.locator("label[for=logTimePreset]")).toContainText(timePeriod);
      await expect(page.locator("#logTimePreset option")).toHaveText(timeOptions);
    }
  });

  test("filters component logs by a specific day and an exact local time range", async ({ page }) => {
    await page.route("**/api/logs/**", (route) => route.fulfill({ contentType: "application/x-ndjson", body: "" }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.waitForFunction(() => componentLogsLoaded);
    await page.evaluate(() => {
      componentLogEntries.inbox = [
        { line: 1, timestamp: new Date(2026, 7, 16, 9, 0).toISOString(), level: "INFO", event: "watcher_started", runId: "day", details: "early-entry" },
        { line: 2, timestamp: new Date(2026, 7, 16, 11, 0).toISOString(), level: "INFO", event: "watcher_started", runId: "range", details: "range-entry" },
        { line: 3, timestamp: new Date(2026, 7, 17, 9, 0).toISOString(), level: "INFO", event: "watcher_started", runId: "other", details: "other day" },
      ];
      componentLogEntries.dashboard = [];
      componentLogServerPaged = false;
      renderComponentLogs();
    });
    await page.locator("#logTimePreset").selectOption("day");
    await expect(page.locator("#logSpecificDateControl")).toBeVisible();
    await page.locator("#logSpecificDate").fill("2026-08-16");
    await expect(page.locator("#inboxComponentLog")).toContainText("early-entry");
    await expect(page.locator("#inboxComponentLog")).toContainText("range-entry");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("other day");

    await page.locator("#logTimePreset").selectOption("range");
    await expect(page.locator("#logDateFromControl")).toBeVisible();
    await page.locator("#logDateFrom").fill("2026-08-16T10:30");
    await page.locator("#logDateTo").fill("2026-08-16T11:30");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("early-entry");
    await expect(page.locator("#inboxComponentLog")).toContainText("range-entry");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("other day");
  });

  test("keeps the log range end at or after its selected start", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#logTimePreset").selectOption("range");
    const from = page.locator("#logDateFrom"), to = page.locator("#logDateTo");
    await from.fill("2026-08-20T18:52");
    await expect(to).toHaveAttribute("min", "2026-08-20T18:52");
    await to.evaluate((input) => {
      input.value = "2026-08-01T18:52";
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expect(to).toHaveValue("2026-08-20T18:52");
  });

  test("clears an individual log date value with its date-field close glyph", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#logTimePreset").selectOption("range");
    const from = page.locator("#logDateFrom"), to = page.locator("#logDateTo"),
      clearEnd = page.locator('[data-clear-log-date="logDateTo"]');
    await from.fill("2026-08-20T18:52");
    await to.fill("2026-08-20T19:52");
    await expect(clearEnd).toBeVisible();
    await dispatchDashboardPointerClick(clearEnd);
    await expect(to).toHaveValue("");
    await expect(clearEnd).toBeHidden();
    await expect(to).toHaveAttribute("min", "2026-08-20T18:52");
  });

  test("filters component logs by the local today and yesterday presets", async ({ page }) => {
    await page.route("**/api/logs/**", (route) => route.fulfill({ contentType: "application/x-ndjson", body: "" }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.waitForFunction(() => componentLogsLoaded);
    await page.evaluate(() => {
      const now = new Date(), today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9),
        yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 9),
        older = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 2, 9);
      componentLogEntries.inbox = [
        { line: 1, timestamp: today.toISOString(), level: "INFO", event: "watcher_started", runId: "today", details: "today-entry" },
        { line: 2, timestamp: yesterday.toISOString(), level: "INFO", event: "watcher_started", runId: "yesterday", details: "yesterday-entry" },
        { line: 3, timestamp: older.toISOString(), level: "INFO", event: "watcher_started", runId: "older", details: "older-entry" },
      ];
      componentLogEntries.dashboard = [];
      renderComponentLogs();
    });
    await page.locator("#logTimePreset").selectOption("today");
    await expect(page.locator("#inboxComponentLog")).toContainText("today-entry");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("yesterday-entry");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("older-entry");

    await page.locator("#logTimePreset").selectOption("yesterday");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("today-entry");
    await expect(page.locator("#inboxComponentLog")).toContainText("yesterday-entry");
    await expect(page.locator("#inboxComponentLog")).not.toContainText("older-entry");
  });

  test("queries the complete retained component log before applying filters", async ({ page }) => {
    const inboxQueries = [];
    await page.route("**/api/logs/**", async (route) => {
      const url = new URL(route.request().url()), component = url.pathname.split("/").at(-1);
      if (component === "inbox") inboxQueries.push(url.searchParams);
      const historical = url.searchParams.has("start");
      await route.fulfill({ json: {
        entries: component === "inbox" ? [{
          line: historical ? 17 : 999,
          timestamp: historical ? "2026-08-26T09:00:00.000Z" : "2026-08-27T09:00:00.000Z",
          level: historical ? "WARNING" : "INFO",
          event: historical ? "historical_warning" : "newest_record",
          diagnostic: historical ? "needle from yesterday" : "newest record",
        }] : [],
        total: component === "inbox" ? 121 : 0,
        events: ["historical_warning", "newest_record"],
      } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.waitForFunction(() => componentLogsLoaded);

    await page.locator("#logTimePreset").selectOption("yesterday");
    await expect(page.locator("#inboxComponentLog")).toContainText("needle from yesterday");
    await page.locator("#logFilter").fill("needle");
    await page.locator("#logLevelFilter").selectOption("WARNING");
    await page.locator("#logEventFilter").selectOption("historical_warning");
    await expect.poll(() => inboxQueries.some((query) =>
      query.get("format") === "json"
      && query.has("start")
      && query.has("end")
      && query.get("search") === "needle"
      && query.get("level") === "WARNING"
      && query.getAll("event").includes("historical_warning"),
    )).toBe(true);
  });

  test("localizes component log table headings for every supported language", async ({ page }) => {
    const expectations = [
      ["en", "Inbox watcher", ["#", "Timestamp", "Level", "Event", "Run ID", "Details"]],
      ["nl", "Inbox-watcher", ["#", "Tijdstip", "Niveau", "Gebeurtenis", "Run-ID", "Details"]],
      ["de", "Inbox-Watcher", ["#", "Zeitpunkt", "Stufe", "Ereignis", "Run-ID", "Details"]],
      ["fr", "Surveillant de la boîte de réception", ["#", "Horodatage", "Niveau", "Événement", "ID d’exécution", "Détails"]],
      ["es", "Monitor de bandeja de entrada", ["#", "Marca de tiempo", "Nivel", "Evento", "ID de ejecución", "Detalles"]],
    ];
    for (const [language, title, headers] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator("#componentLogs .log-card-header strong").first()).toHaveText(title);
      await expect(page.locator("#inboxComponentLog").locator("xpath=preceding-sibling::thead[1]/tr/th")).toHaveText(headers);
    }
  });

  test("uses human-friendly localized event labels in component logs and their filter", async ({ page }) => {
    // This exercises five full locale reloads while the browser suite runs in
    // parallel.  Allow the isolated dashboard to become ready under CI load.
    test.slow();
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    await page.route("**/api/logs/**", (route) => route.fulfill({
      contentType: "application/x-ndjson",
      body: "",
    }));
    const expectations = [
      ["en", "Inbox watcher started", "Stale Git lock recovered"],
      ["nl", "Inbox-watcher gestart", "Verouderde Git-vergrendeling hersteld"],
      ["de", "Inbox-Watcher gestartet", "Veraltete Git-Sperre wiederhergestellt"],
      ["fr", "Surveillant de boîte de réception démarré", "Verrou Git obsolète récupéré"],
      ["es", "Monitor de bandeja de entrada iniciado", "Bloqueo Git obsoleto recuperado"],
    ];
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const [language, watcherStarted, staleLockRecovered] of expectations) {
      await selectDashboardLocale(page, language);
      await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
      await page.waitForFunction(() => componentLogsLoaded);
      await page.locator("#autoRefresh").uncheck();
      const rendered = await page.evaluate(() => {
        refreshComponentLogs = async () => {};
        componentLogEntries.inbox = [
          { line: 1, timestamp: "2026-08-16T09:00:00Z", level: "INFO", event: "watcher_started", runId: "run-1", details: "" },
          { line: 2, timestamp: "2026-08-16T09:01:00Z", level: "INFO", event: "stale_git_lock_recovered", runId: "run-2", details: "" },
        ];
        componentLogEntries.dashboard = [];
        renderComponentLogs();
        return {
          inbox: document.querySelector("#inboxComponentLog")?.textContent || "",
          events: Object.fromEntries(
            [...document.querySelectorAll("#logEventFilter option")].map((option) => [option.value, option.textContent || ""]),
          ),
        };
      });
      expect(rendered.inbox).toContain(watcherStarted);
      expect(rendered.inbox).toContain(staleLockRecovered);
      expect(rendered.events.watcher_started).toBe(watcherStarted);
      expect(rendered.events.stale_git_lock_recovered).toBe(staleLockRecovered);
    }
  });

  test("localizes prompt history column headings for every supported language", async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 1024, height: 844 });
    const expectations = [
      ["en", ["Run ID", "Status", "Execution title", "Executed at", "Report", "AI analysis", "AI chat", "Action", "Details"]],
      ["nl", ["Run-ID", "Status", "Uitvoeringstitel", "Uitgevoerd op", "Rapport", "AI-analyse", "AI-gesprek", "Actie", "Details"]],
      ["de", ["Run-ID", "Status", "Ausführungstitel", "Ausgeführt am", "Bericht", "KI-Analyse", "KI-Chat", "Aktion", "Details"]],
      ["fr", ["ID exéc.", "État", "Titre de l’exécution", "Exécuté le", "Rapport", "Analyse IA", "Chat IA", "Action", "Détails"]],
      ["es", ["ID ejec.", "Estado", "Título de la ejecución", "Ejecutado el", "Informe", "Análisis de IA", "Chat de IA", "Acción", "Detalles"]],
    ];
    for (const [language, headers] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await selectDashboardLocale(page, language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator("#promptHistory .log-table thead th")).toHaveText(headers);
    }
  });

  test("rerenders prompt history pagination in the selected language", async ({ page }) => {
    let historyRuns = [];
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: historyRuns } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await selectDashboardLocale(page, "en");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.waitForFunction(
      () => typeof window.renderPromptHistory === "function",
    );
    historyRuns = Array.from({ length: 101 }, (_, index) => ({
        run_id: `inbox-${index}`,
        title: `Prompt ${index}`,
        status: "COMPLETE",
      }));
    await page.evaluate((fixture) => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = fixture;
      renderPromptHistory();
    }, historyRuns);
    await expect(page.locator("#promptHistoryPagination")).toContainText("Page 1 of 11 · 101 executions");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(10);
    expect(await page.locator("#promptHistory .log-table-wrap").evaluate((wrap) => {
      const style = getComputedStyle(wrap), table = wrap.querySelector("table");
      return style.minHeight === "0px" && style.maxHeight === "none" &&
        Math.abs(wrap.getBoundingClientRect().height - table.getBoundingClientRect().height) <= 2;
    })).toBe(true);
    await expect(page.locator("#promptHistoryPagination button").first()).toHaveText("Previous");
    await expect(page.locator("#promptHistoryPagination button").last()).toHaveText("Next");
  });

  test("uses the prompt title column to fill available history table space", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    const layout = await page.locator("#promptHistory .log-table-wrap").evaluate((wrap) => {
      const table = wrap.querySelector("table");
      const titleHeader = table.querySelector("th:nth-child(3)");
      const statusHeader = table.querySelector("th:nth-child(1)");
      return {
        wrapWidth: Math.round(wrap.getBoundingClientRect().width),
        tableWidth: Math.round(table.getBoundingClientRect().width),
        titleWidth: Math.round(titleHeader.getBoundingClientRect().width),
        statusWidth: Math.round(statusHeader.getBoundingClientRect().width),
      };
    });
    expect(layout.tableWidth).toBeGreaterThanOrEqual(layout.wrapWidth - 2);
    // A terminal row can reserve one unwrapped action strip. Its bounded
    // overflow remains much smaller than a title column expanding freely.
    expect(layout.tableWidth).toBeLessThanOrEqual(layout.wrapWidth + 128);
    expect(layout.titleWidth).toBeGreaterThan(layout.statusWidth * 2.5);
  });

  test("sizes the visible status column before narrowing the prompt title", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    // Wait for the initial history request before replacing its fixture. Without
    // this, the asynchronous response can replace the row under measurement.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-dismissed-status",
        status: "FAILED",
        dismissed: true,
        title: "A deliberately long execution title that yields space to status",
        executed_at: "2026-08-04T08:00:00Z",
      }];
      renderPromptHistory();
    });
    const layout = await page.locator("#promptHistory .log-table").evaluate((table) => {
      const status = table.querySelector("tbody td:nth-child(2)");
      const title = table.querySelector("tbody td:nth-child(3)");
      return {
        statusWidth: Math.round(status.getBoundingClientRect().width),
        statusScrollWidth: Math.ceil(status.scrollWidth),
        titleTextWidth: Math.round(title.querySelector(".prompt-history-title").getBoundingClientRect().width),
        configuredTitleWidth: Number.parseInt(table.style.getPropertyValue("--prompt-history-title-width"), 10),
      };
    });
    expect(layout.statusWidth).toBeGreaterThanOrEqual(layout.statusScrollWidth);
    expect(layout.statusWidth).toBeGreaterThan(120);
    expect(layout.configuredTitleWidth).toBeLessThan(288);
    expect(layout.titleTextWidth).toBeLessThanOrEqual(layout.configuredTitleWidth + 1);
  });

  test("keeps terminal history actions on one wide-screen row beside a compact title", async ({ page }) => {
    const history = [{
      run_id: "inbox-actions",
      title: "Engineering Platform Increment — Producer Submission Envelope",
      status: "BLOCKED",
      can_retry: true,
    }];
    await page.setViewportSize({ width: 2048, height: 900 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: history } }));
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0, last_executed_run: "inbox-actions" } },
    }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      r({ last_executed_run: "inbox-actions", watcher_state: "WATCHER_IDLE" }, {});
    });
    const actions = page.locator("#promptHistoryRows .prompt-history-actions").first();
    await expect(actions).toHaveCSS("flex-wrap", "nowrap");
    const layout = await actions.evaluate((element) => ({
      height: Math.round(element.getBoundingClientRect().height),
      firstTop: Math.round(element.children[0].getBoundingClientRect().top),
      secondTop: Math.round(element.children[1].getBoundingClientRect().top),
      titleWidth: Math.round(element.closest("tr").children[2].getBoundingClientRect().width),
      actionCellDisplay: getComputedStyle(element.parentElement).display,
      actionCellBottom: Math.round(element.parentElement.getBoundingClientRect().bottom),
      titleCellBottom: Math.round(element.closest("tr").children[2].getBoundingClientRect().bottom),
    }));
    expect(layout.firstTop).toBe(layout.secondTop);
    expect(layout.height).toBeLessThanOrEqual(46);
    expect(layout.titleWidth).toBeLessThanOrEqual(384);
    expect(layout.actionCellDisplay).toBe("table-cell");
    expect(layout.actionCellBottom).toBe(layout.titleCellBottom);
  });

  test("shows only the final five Run-ID characters on an iPad-sized history table", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{ run_id: "inbox-00557e6587394f67b8b4cbde0748bce7", title: "Retry lineage", status: "COMPLETE" }];
      renderPromptHistory();
    });
    await expect(page.locator("#promptHistoryRows tr td").first()).toHaveText("8bce7");
  });

  test("uses the server retry projection for historical parent actions and lineage", async ({ page }) => {
    const retryHistory = [
      { run_id: "inbox-retryable", title: "Blocked without child", status: "BLOCKED", can_retry: true },
      { run_id: "inbox-failed-retryable", title: "Failed without child", status: "FAILED", can_retry: true },
      { run_id: "inbox-queued-parent", title: "Queued retry", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-queued-run-id", retry_status: "QUEUED" },
      { run_id: "inbox-active-parent", title: "Active child", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-active-child", retry_status: "ACTIVE" },
      { run_id: "inbox-complete-parent", title: "Completed child", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-complete-child", retry_status: "COMPLETE" },
    ];
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: retryHistory } }));
    await page.setViewportSize({ width: 1024, height: 844 });
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      renderPromptHistory();
    });
    await expect(page.locator("#promptHistoryRows .execution-history-action")).toHaveCount(4);
    await expect(page.locator("#promptHistoryRows .prompt-history-actions").first()).toHaveCSS("gap", "6px");
    await expect(page.locator("#promptHistoryRows .prompt-history-actions").first()).toHaveCSS("display", "flex");
    await expect(page.locator("#promptHistoryRows tr").nth(0)).toContainText("Uitvoering opnieuw proberen");
    await expect(page.locator("#promptHistoryRows tr").nth(1)).toContainText("Uitvoering opnieuw proberen");
    await expect(page.locator("#promptHistoryRows tr").nth(0)).toContainText("Uitvoering afsluiten");
    await expect(page.locator("#promptHistoryRows tr").nth(1)).toContainText("Uitvoering afsluiten");
    await expect(page.locator("#promptHistoryRows tr").nth(2)).toContainText("Nieuwe uitvoering in wachtrij");
    await expect(page.locator("#promptHistoryRows tr").nth(2)).not.toContainText("queued-run-id");
    await expect(page.locator("#promptHistoryRows tr").nth(3)).toContainText("Huidige nieuwe uitvoering: child");
    await expect(page.locator("#promptHistoryRows tr").nth(4)).toContainText("Vervangen door: child");
  });

  test("keeps prompt history horizontally scrollable only on an iPhone-sized viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    const scroll = await page.locator("#promptHistory .log-table-wrap").evaluate((wrap) => ({
      scrollWidth: wrap.scrollWidth,
      clientWidth: wrap.clientWidth,
    }));
    expect(scroll.scrollWidth).toBeGreaterThan(scroll.clientWidth);
    await expect(page.locator("#promptHistory .log-table-wrap")).toHaveCSS("touch-action", "pan-x pan-y");
  });

  test("shows and pins the Run-ID while preserving history-table horizontal access on an iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [
        { run_id: "inbox-zzzzz", title: "Mobiele Run-ID", status: "COMPLETE" },
        { run_id: "inbox-aaaaa", title: "Mobiele Run-ID", status: "COMPLETE" },
      ],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    const wrap = page.locator("#promptHistory .log-table-wrap");
    await expect(page.locator("#promptHistoryScrollHint")).toHaveText(
      "Veeg zijwaarts om alle geschiedeniskolommen te zien.",
    );
    await expect(wrap).toHaveAttribute("aria-describedby", "promptHistoryScrollHint");
    await expect(wrap).toHaveAttribute("tabindex", "0");
    const runIdHeader = page.locator('#promptHistory th[data-history-sort-key="run_id"]');
    await expect(page.locator("#promptHistoryRows tr td").first()).toHaveText("zzzzz");
    await dispatchDashboardPointerClick(runIdHeader);
    await expect(runIdHeader).toHaveAttribute("aria-sort", "ascending");
    await expect(runIdHeader).not.toBeFocused();
    await expect(page.locator("#promptHistoryRows tr td").first()).toHaveText("aaaaa");
    const layout = await wrap.evaluate((element) => {
      element.scrollLeft = 240;
      const runId = element.querySelector("thead th:first-child");
      const title = element.querySelector("thead th:nth-child(3)");
      return {
        wrapLeft: Math.round(element.getBoundingClientRect().left),
        runIdLeft: Math.round(runId.getBoundingClientRect().left),
        runIdWidth: Math.round(runId.getBoundingClientRect().width),
        titleWidth: Math.round(title.getBoundingClientRect().width),
      };
    });
    expect(Math.abs(layout.runIdLeft - layout.wrapLeft)).toBeLessThanOrEqual(2);
    expect(layout.runIdWidth).toBe(128);
    expect(layout.titleWidth).toBeGreaterThan(layout.runIdWidth);
    const runIdCell = page.locator("#promptHistoryRows tr td:first-child").first();
    await scrollDashboardElementIntoView(runIdCell);
    await runIdCell.hover({ force: true });
    expect(await runIdCell.evaluate((cell) => getComputedStyle(cell).backgroundColor)).not.toContain("/");
    await page.getByTestId("theme-toggle").click();
    await runIdCell.hover({ force: true });
    expect(await runIdCell.evaluate((cell) => getComputedStyle(cell).backgroundColor)).not.toContain("/");
  });

  test("uses the rose Run-ID surface throughout a wide light history table", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 844 });
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [
        { run_id: "inbox-rose-one", title: "Eerste uitvoering", status: "FAILED" },
        { run_id: "inbox-rose-two", title: "Tweede uitvoering", status: "FAILED" },
      ],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });

    const runIds = page.locator("#promptHistoryRows tr td:first-child");
    await expect(runIds).toHaveCount(2);
    await expect(runIds.nth(0)).toHaveCSS("background-color", "rgb(255, 241, 245)");
    await expect(runIds.nth(1)).toHaveCSS("background-color", "rgb(255, 241, 245)");
  });

  test("keeps every interactive table-row hover light in light mode", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.evaluate(() => {
      document.querySelector("main").insertAdjacentHTML("beforeend", `
        <section id="lightTableHoverRegression" style="--category-color:#f29ab2">
          <table class="log-table"><tbody><tr class="component-log-row"><td>log</td><td>entry</td></tr></tbody></table>
          <table class="log-table"><tbody><tr class="prompt-history-row"><td>run</td><td>history</td></tr></tbody></table>
          <table class="telemetry-table"><tbody><tr class="telemetry-row"><td>day</td><td>telemetry</td></tr></tbody></table>
        </section>
      `);
    });
    for (const row of await page.locator("#lightTableHoverRegression tbody tr").all()) {
      await row.hover();
      const backgrounds = await row.locator("td").evaluateAll(
        (cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor),
      );
      // The anchored first column may retain its own light category tint, but
      // no hover cell may fall back to the dark default table surface.
      expect(backgrounds).not.toContain("rgb(36, 36, 45)");
    }
  });

  test("keeps accessible table focus separate from the house-style selection ring", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; });
    const tableWrap = page.locator("#promptHistory .log-table-wrap");
    await tableWrap.focus();
    const focusStyle = await tableWrap.evaluate((element) => {
      const probe = document.createElement("span");
      probe.style.color = "var(--house-style)";
      document.body.append(probe);
      const houseStyle = getComputedStyle(probe).color;
      probe.style.boxShadow = "0 0 0 4px var(--dashboard-input-focus-ring)";
      const houseStyleRing = getComputedStyle(probe).boxShadow;
      probe.remove();
      const style = getComputedStyle(element);
      return {
        borderColor: style.borderColor,
        boxShadow: style.boxShadow,
        outlineColor: style.outlineColor,
        outlineStyle: style.outlineStyle,
        houseStyle,
        houseStyleRing,
      };
    });
    expect(focusStyle.borderColor).not.toBe(focusStyle.houseStyle);
    expect(focusStyle.outlineColor).not.toBe(focusStyle.houseStyle);
    expect(focusStyle.outlineStyle).toBe("solid");
    expect(focusStyle.boxShadow).not.toBe(focusStyle.houseStyleRing);
    expect(focusStyle.borderColor).toBe("rgb(141, 199, 255)");
  });

  test("matches the iPhone portrait dashboard visual reference", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/log/**", (route) => route.abort());
    await page.route("**/health", (route) => route.fulfill({ json: { components: {
      dashboard: { healthy: true }, inbox_watcher: { healthy: true }, dashboard_relay: { healthy: true },
    } } }));
    // This visual reference is for the running-execution surface. Keep the
    // machine-specific provider checks out of the fixture; their banners have
    // dedicated behavioural coverage elsewhere in this suite.
    await page.route("**/api/provider-login-status", (route) => route.fulfill({ json: {
      providers: {
        codex: { provider: "CODEX", state: "READY" },
        github: { provider: "GITHUB", state: "READY" },
      },
    } }));
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: {
        status: {
          watcher_state: "ENGINEERING_RUN_ACTIVE",
          current_phase: "EXECUTE_AGENT",
          current_action: "Capability review: validation",
          run_id: "inbox-iphone-reference",
          prompt_title: "Mobile dashboard visual reference",
          submitted_filename: "engineering-iphone-reference.txt",
          queue_depth: 1,
        },
      },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.classList.contains("dashboard-ready"),
    );
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.evaluate(async () => {
      document.querySelector("#dashboardTitlebarOptionsContent").hidden = true;
      document.querySelector("#dashboardTitlebarOptionsToggle").setAttribute("aria-expanded", "false");
      document.activeElement?.blur();
      window.scrollTo(0, 0);
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
      document.querySelector(".dashboard-scroll-region").scrollTop = 0;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    await expect.poll(() => page.evaluate(() => Math.round(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0))).toBe(0);
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const image = await page.screenshot({ animations: "disabled" });
    await testInfo.attach("iphone-portrait-dashboard", {
      body: image,
      contentType: "image/png",
    });
    await expect(page).toHaveScreenshot("iphone-portrait-dashboard.png", {
      animations: "disabled",
      // Linux font rasterization has a stable 0.525% delta from the checked-in
      // macOS reference. Keep a narrow cross-platform allowance while the
      // fixture assertions above continue to protect its critical status data.
      maxDiffPixelRatio: 0.006,
    });
  });

  test("localizes capability preflight recommendations", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    expect(await page.evaluate(() => capabilityRecommendation("Capability admission passed."))).toBe(
      "Capabilitytoelating geslaagd.",
    );
    expect(await page.evaluate(() => capabilityRecommendation(
      "Repair or upgrade the Execution Host before resubmitting.",
    ))).toBe("Herstel of upgrade de Execution Host voordat je opnieuw indient.");
  });

  test("renders preflight enums as localized labels", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    expect(await page.evaluate(() => [
      enumLabel("PASS"),
      enumLabel("RETRYABLE"),
      enumLabel("RETRYABLE_AFTER_HOST_REPAIR"),
      enumLabel("CAPABILITY"),
    ])).toEqual([
      "Geslaagd",
      "Opnieuw proberen mogelijk",
      "Opnieuw proberen na herstel van de Execution Host",
      "Capability",
    ]);
  });

  test("keeps the status bar at the bottom while dashboard content scrolls", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#engineering-dashboard-content").evaluate((content) => {
      content.style.minHeight = "2000px";
    });
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const footer = document.querySelector(".footer");
      region.scrollTop = 160;
      return {
        bodyOverflow: getComputedStyle(document.body).overflowY,
        regionOverflow: getComputedStyle(region).overflowY,
        regionScrolled: region.scrollTop > 0,
        footerBottom: Math.round(footer.getBoundingClientRect().bottom),
        viewportBottom: window.innerHeight,
      };
    });
    expect(layout.bodyOverflow).toBe("hidden");
    expect(layout.regionOverflow).toBe("auto");
    expect(layout.regionScrolled).toBe(true);
    expect(layout.footerBottom).toBeLessThanOrEqual(layout.viewportBottom);
  });

  test("resizes the dashboard scroll shell with the active viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.setViewportSize({ width: 1280, height: 560 });
    const layout = await page.evaluate(() => ({
      bodyHeight: Math.round(document.body.getBoundingClientRect().height),
      viewportHeight: window.innerHeight,
      scrollHeight: document.querySelector(".dashboard-scroll-region").clientHeight,
      footerBottom: Math.round(document.querySelector(".footer").getBoundingClientRect().bottom),
    }));
    expect(layout.bodyHeight).toBe(layout.viewportHeight);
    expect(layout.footerBottom).toBeLessThanOrEqual(layout.viewportHeight);
    expect(layout.scrollHeight).toBeGreaterThan(0);
  });

  test("keeps the desktop title bar flush with the scrolling region", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#engineering-dashboard-content").evaluate((content) => {
      content.style.minHeight = "2000px";
    });
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const titleBar = document.querySelector(".dashboard-titlebar");
      const banner = document.querySelector("#githubRateLimitBanner");
      banner.hidden = false;
      region.scrollTop = 160;
      return {
        regionTop: Math.round(region.getBoundingClientRect().top),
        titleBarTop: Math.round(titleBar.getBoundingClientRect().top),
        bannerTop: Math.round(banner.getBoundingClientRect().top),
        titleBarBottom: Math.round(titleBar.getBoundingClientRect().bottom),
      };
    });
    expect(layout.titleBarTop).toBe(layout.regionTop);
    expect(layout.bannerTop).toBe(layout.titleBarBottom);
  });

  test("keeps the wrapped desktop title bar and banner sticky in a narrow window", async ({ page }) => {
    await page.setViewportSize({ width: 1136, height: 458 });
    await page.route("**/api/github-rate-limit", (route) => route.fulfill({
      json: { limited: true, reset_at: 1_786_162_124 },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("github-rate-limit-banner")).toBeVisible();
    await expect(page.locator(".dashboard-sticky-header")).toHaveCSS("padding-bottom", "7px");
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const stickyHeader = document.querySelector(".dashboard-sticky-header");
      const titleBar = document.querySelector(".dashboard-titlebar");
      const banner = document.querySelector("#githubRateLimitBanner");
      document.querySelector("#engineering-dashboard-content").style.minHeight = "2600px";
      region.scrollTop = 180;
      return {
        regionTop: Math.round(region.getBoundingClientRect().top),
        stickyTop: Math.round(stickyHeader.getBoundingClientRect().top),
        titleTop: Math.round(titleBar.getBoundingClientRect().top),
        bannerTop: Math.round(banner.getBoundingClientRect().top),
        bannerBottom: Math.round(banner.getBoundingClientRect().bottom),
        titleBottom: Math.round(titleBar.getBoundingClientRect().bottom),
        stickyBottom: Math.round(stickyHeader.getBoundingClientRect().bottom),
      };
    });
    expect(layout.stickyTop).toBe(layout.regionTop);
    expect(layout.titleTop).toBe(layout.regionTop);
    expect(layout.bannerTop).toBe(layout.titleBottom);
    expect(layout.stickyBottom - layout.bannerBottom).toBeGreaterThanOrEqual(14);
  });

  test("does not move the dashboard scroll position when live content changes above it", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const layout = await page.evaluate(async () => {
      const region = document.querySelector(".dashboard-scroll-region");
      const content = document.querySelector("#engineering-dashboard-content");
      content.style.minHeight = "2600px";
      region.scrollTop = 600;
      const before = region.scrollTop;
      const update = document.createElement("div");
      update.style.height = "240px";
      content.prepend(update);
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return { before, after: region.scrollTop };
    });
    expect(layout.before).toBeGreaterThan(0);
    expect(layout.after).toBe(layout.before);
  });

  test("keeps iPhone configuration tooltips within the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#configuration").evaluate((element) => { element.open = true; });
    const tooltip = page.locator(".configuration-info").last();
    await tooltip.focus();
    const bounds = await tooltip.evaluate((element) => {
      const rect = element.getBoundingClientRect(), style = getComputedStyle(element, "::after");
      const width = Number.parseFloat(style.width);
      const left = element.dataset.tooltipSide === "left"
        ? rect.right - Number.parseFloat(style.right) - width
        : rect.left + Number.parseFloat(style.left);
      return { left, right: left + width, viewportWidth: window.innerWidth };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(-1);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewportWidth + 1);
  });

  test("keeps the title bar and banner sticky in a very narrow desktop window", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/github-rate-limit", (route) => route.fulfill({
      json: { limited: true, reset_at: 1_786_162_124 },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("github-rate-limit-banner")).toBeVisible();
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const stickyHeader = document.querySelector(".dashboard-sticky-header");
      const titleBar = document.querySelector(".dashboard-titlebar");
      const banner = document.querySelector("#githubRateLimitBanner");
      document.querySelector("#engineering-dashboard-content").style.minHeight = "2600px";
      region.scrollTop = 180;
      return {
        regionTop: Math.round(region.getBoundingClientRect().top),
        stickyTop: Math.round(stickyHeader.getBoundingClientRect().top),
        titleTop: Math.round(titleBar.getBoundingClientRect().top),
        titleBottom: Math.round(titleBar.getBoundingClientRect().bottom),
        bannerTop: Math.round(banner.getBoundingClientRect().top),
      };
    });
    expect(layout.stickyTop).toBe(layout.regionTop);
    expect(layout.titleTop).toBe(layout.regionTop);
    expect(layout.bannerTop).toBe(layout.titleBottom);
  });

  test("keeps status, refresh, and mobile options on one title-bar row", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const disclosure = page.getByTestId("titlebar-options-toggle");
    const content = page.locator("#dashboardTitlebarOptionsContent");
    if (await disclosure.getAttribute("aria-expanded") === "true") await disclosure.click();
    await expect(disclosure).toHaveAttribute("aria-expanded", "false");
    await expect(disclosure).toBeVisible();
    await expect(page.getByTestId("theme-toggle")).not.toBeVisible();

    await disclosure.click();
    await expect(disclosure).toHaveAttribute("aria-expanded", "true");
    await expect(content).toBeVisible();
    await expect(page.locator("#dashboardLocaleButton")).toBeVisible();
    await expect(page.locator("#dashboardLocaleButton")).toContainText("Nederlands");
    await expect(page.locator("#dashboardProject + .dashboard-select-picker")).toBeVisible();
    await expect(page.locator("#dashboardProject + .dashboard-select-picker .dashboard-locale__button")).not.toHaveText("");
    expect(await page.evaluate(() => document.querySelector(".dashboard-project")?.nextElementSibling?.matches(".dashboard-locale"))).toBe(true);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(disclosure).toHaveAttribute("aria-expanded", "true");
    await expect(content).toBeVisible();
    for (const label of [
      ".dashboard-titlebar__options-content .dashboard-project > span:first-child",
      ".dashboard-titlebar__options-content .dashboard-locale > span:first-child",
      ".dashboard-titlebar__options-content .theme-toggle__label",
      ".dashboard-titlebar__options-content .section-state-toggle__label",
      ".dashboard-titlebar__options-content .auto-refresh-toggle span",
    ]) {
      await expect(page.locator(label)).toBeVisible();
      expect((await page.locator(label).textContent()).trim()).not.toBe("");
    }

    const controls = await page.locator(
      ".dashboard-titlebar__options-content > .dashboard-project, .dashboard-titlebar__options-content > .dashboard-locale, .dashboard-titlebar__options-content > .theme-toggle, .dashboard-titlebar__options-content > .section-state-toggle, .dashboard-titlebar__options-content > .auto-refresh-toggle",
    ).evaluateAll((elements) => elements.map((element) => Math.round(element.getBoundingClientRect().top)));
    expect(controls).toEqual([...controls].sort((first, second) => first - second));
    const labelFonts = await page.locator([
      ".dashboard-titlebar__options-content .dashboard-project > span:first-child",
      ".dashboard-titlebar__options-content .dashboard-locale > span:first-child",
      ".dashboard-titlebar__options-content .theme-toggle__label",
      ".dashboard-titlebar__options-content .section-state-toggle__label",
      ".dashboard-titlebar__options-content .auto-refresh-toggle span",
    ].join(", ")).evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      return `${style.fontFamily}|${style.fontSize}|${style.fontWeight}|${style.lineHeight}`;
    }));
    expect(new Set(labelFonts).size).toBe(1);
    const titlebarLayout = await page.evaluate(() => {
      const health = document.querySelector("#dashboardHealth").getBoundingClientRect();
      const refresh = document.querySelector("#pageRefresh").getBoundingClientRect();
      const options = document.querySelector("#dashboardTitlebarOptionsToggle").getBoundingClientRect();
      return {
        healthTop: Math.round(health.top), healthLeft: Math.round(health.left),
        refreshTop: Math.round(refresh.top), refreshLeft: Math.round(refresh.left),
        optionsTop: Math.round(options.top), optionsLeft: Math.round(options.left),
      };
    });
    expect(Math.abs(titlebarLayout.healthTop - titlebarLayout.refreshTop)).toBeLessThanOrEqual(2);
    expect(Math.abs(titlebarLayout.refreshTop - titlebarLayout.optionsTop)).toBeLessThanOrEqual(2);
    expect(titlebarLayout.healthLeft).toBeLessThan(titlebarLayout.refreshLeft);
    expect(titlebarLayout.refreshLeft).toBeLessThan(titlebarLayout.optionsLeft);

    const panelLayout = await page.evaluate(() => {
      const titlebar = document.querySelector(".dashboard-titlebar").getBoundingClientRect();
      const panel = document.querySelector("#dashboardTitlebarOptionsContent").getBoundingClientRect();
      return {
        panelTop: Math.round(panel.top),
        titlebarBottom: Math.round(titlebar.bottom),
      };
    });
    expect(panelLayout.titlebarBottom).toBeGreaterThanOrEqual(panelLayout.panelTop);
  });

  test("keeps expanded compact options in the titlebar flow", async ({ page }) => {
    for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 844 }]) {
      await page.setViewportSize(viewport);
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await openTitlebarOptions(page);
      const layout = await page.evaluate(() => {
        const titlebar = document.querySelector(".dashboard-titlebar").getBoundingClientRect();
        const options = document.querySelector("#dashboardTitlebarOptionsContent").getBoundingClientRect();
        const queue = document.querySelector("#queueItems").getBoundingClientRect();
        return {
          optionsBottom: Math.round(options.bottom),
          titlebarBottom: Math.round(titlebar.bottom),
          queueTop: Math.round(queue.top),
        };
      });
      expect(layout.titlebarBottom).toBeGreaterThanOrEqual(layout.optionsBottom);
      expect(layout.queueTop).toBeGreaterThanOrEqual(layout.titlebarBottom);
    }
  });

  test("uses one dark-mode ink colour for title-bar option labels", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);
    const colours = await page.locator([
      ".dashboard-titlebar__options-content .dashboard-locale > span:first-child",
      ".dashboard-titlebar__options-content .theme-toggle__label",
      ".dashboard-titlebar__options-content .section-state-toggle__label",
    ].join(", ")).evaluateAll((elements) => elements.map((element) => getComputedStyle(element).color));
    expect(new Set(colours).size).toBe(1);
  });

  test("opens title-bar pulldowns in the narrow mobile options panel", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);

    const projectPicker = page.locator("#dashboardProject + .dashboard-select-picker");
    await openDashboardPicker(projectPicker);
    await expect(projectPicker.locator("[role=listbox]")).toBeVisible();
    await openDashboardPicker(projectPicker);
    await expect(projectPicker.locator("[role=listbox]")).toBeHidden();

    await page.locator("#dashboardLocaleButton").click();
    await expect(page.locator("#dashboardLocaleMenu")).toBeVisible();
  });

  test("aligns the refresh action with status at compact desktop widths", async ({ page }) => {
    await page.setViewportSize({ width: 1250, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const layout = await page.evaluate(() => {
      const health = document.querySelector("#dashboardHealth").getBoundingClientRect();
      const refresh = document.querySelector("#pageRefresh").getBoundingClientRect();
      return {
        healthCenter: health.top + health.height / 2,
        refreshCenter: refresh.top + refresh.height / 2,
      };
    });
    expect(Math.abs(layout.healthCenter - layout.refreshCenter)).toBeLessThanOrEqual(2);
    await expect(page.locator("#pageRefresh")).toHaveCSS("transform", "none");
    await expect(page.locator(".dashboard-titlebar__actions")).toHaveCSS("display", "grid");
  });

  test("uses the compact options disclosure before a narrow desktop crowds the title bar", async ({ page }) => {
    await page.setViewportSize({ width: 1200, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const disclosure = page.getByTestId("titlebar-options-toggle");
    const content = page.locator("#dashboardTitlebarOptionsContent");
    await expect(disclosure).toBeVisible();
    await expect(disclosure).toHaveAttribute("aria-controls", "dashboardTitlebarOptionsContent");
    if (await disclosure.getAttribute("aria-expanded") === "true") await expect(content).toBeVisible();
    else await expect(content).toBeHidden();
  });

  test("keeps every compact title-bar option reachable in a short viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 560 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);

    const layout = await page.evaluate(() => {
      const header = document.querySelector(".dashboard-sticky-header");
      const content = document.querySelector("#dashboardTitlebarOptionsContent");
      const lastOption = document.querySelector(".dashboard-titlebar__options-content > .auto-refresh-toggle");
      content.scrollTop = content.scrollHeight;
      const optionRect = lastOption.getBoundingClientRect();
      return {
        headerOverflow: getComputedStyle(header).overflowY,
        contentOverflow: getComputedStyle(content).overflowY,
        lastOptionOnTop: content.contains(document.elementFromPoint(
          optionRect.left + optionRect.width / 2,
          optionRect.top + optionRect.height / 2,
        )),
      };
    });
    expect(layout.headerOverflow).toBe("visible");
    expect(layout.contentOverflow).toBe("auto");
    expect(layout.lastOptionOnTop).toBe(true);
  });

  test("keeps title-bar options visible in a real laptop wrapper", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#dashboardTitlebarOptions")).toHaveCSS("display", "flex");
    await expect(page.getByTestId("titlebar-options-toggle")).toBeHidden();
    await expect(page.locator("#dashboardTitlebarOptionsContent")).toBeVisible();
    await expect(page.getByTestId("theme-toggle")).toBeVisible();
    const width = await page.locator("#dashboardTitlebarOptionsContent").evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    expect(width).toBeGreaterThan(200);
  });

  test("keeps each iPhone title-bar switch thumb inside its track", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);
    for (const toggle of [page.getByTestId("theme-toggle"), page.getByTestId("toggle-all-sections")]) {
      const pseudo = await toggle.evaluate((element) => ({
        trackPosition: getComputedStyle(element, "::before").position,
        trackRight: getComputedStyle(element, "::before").right,
        thumbRight: getComputedStyle(element, "::after").right,
      }));
      expect(pseudo).toEqual({
        trackPosition: "absolute",
        trackRight: "0px",
        thumbRight: "21px",
      });
    }
  });

  test.describe("iPhone direct touch", () => {
    test.use({ hasTouch: true, isMobile: true });

    test("persists every iPhone title-bar toggle after one direct touch at a time", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await openTitlebarOptions(page);

    const touch = async (locator) => {
      const box = await locator.boundingBox();
      expect(box).not.toBeNull();
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
    };
    const storedState = () => page.evaluate(() =>
      JSON.parse(localStorage.getItem("engineering-dashboard-client-state-v1") || "{}"),
    );
    const theme = page.getByTestId("theme-toggle");
    const allSections = page.getByTestId("toggle-all-sections");
    const autoRefresh = page.locator("#autoRefresh");

    await expect(theme).toHaveAttribute("aria-checked", "false");
    await touch(theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(theme).toHaveAttribute("aria-checked", "true");
    await expect.poll(storedState).toMatchObject({ theme: "light" });

    await expect(allSections).toHaveAttribute("aria-checked", "false");
    await touch(allSections);
    await expect(allSections).toHaveAttribute("aria-checked", "true");
    for (const id of ["workspaceCard", "queueItems", "promptHistory", "platformHealth", "technicalDetails", "componentLogs"])
      await expect(page.locator(`#${id}`)).toHaveAttribute("open", "");
    await expect.poll(storedState).toMatchObject({ allSectionsOpen: true });
    await expect.poll(() => page.evaluate(() => localStorage.getItem("engineering-dashboard-all-sections-open-v1"))).toBe("true");

    await expect(autoRefresh).toBeChecked();
    await touch(autoRefresh);
    await expect(autoRefresh).not.toBeChecked();
    await expect.poll(storedState).toMatchObject({ autoRefresh: false });

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.getByTestId("toggle-all-sections")).toHaveAttribute("aria-checked", "true");
      await expect(page.locator("#autoRefresh")).not.toBeChecked();
    });
  });

  test("restores the iPhone page position after an input loses focus", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const scrollPosition = await page.evaluate(async () => {
      document.querySelector("#engineering-dashboard-content").style.minHeight = "2400px";
      document.querySelector("#promptHistory").open = true;
      window.scrollTo(0, 180);
      const initial = window.scrollY;
      const input = document.querySelector("#promptHistoryFilter");
      input.focus();
      window.scrollTo(0, 420);
      input.blur();
      await new Promise((resolve) => setTimeout(resolve, 300));
      return { initial, restored: window.scrollY };
    });
    expect(scrollPosition.restored).toBe(scrollPosition.initial);
  });

  test("locks the iPhone background while a modal is open", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const position = await page.evaluate(async () => {
      document.querySelector("#engineering-dashboard-content").style.minHeight = "2400px";
      window.scrollTo(0, 180);
      const initial = window.scrollY;
      const modal = document.querySelector("#promptHistoryChatModal");
      modal.showModal();
      await new Promise((resolve) => requestAnimationFrame(resolve));
      const locked = {
        active: document.body.classList.contains("dashboard-modal-open"),
        top: getComputedStyle(document.body).top,
      };
      window.scrollTo(0, 420);
      modal.close();
      await new Promise((resolve) => requestAnimationFrame(resolve));
      return { initial, locked, restored: window.scrollY };
    });
    expect(position.locked).toEqual({ active: true, top: "0px" });
    expect(position.restored).toBe(position.initial);
  });

  test("extends the shared component-modal header rule beneath its close control", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const bounds = await page.locator("#componentModal").evaluate((modal) => {
      modal.showModal();
      const heading = modal.querySelector(".component-modal__header").getBoundingClientRect();
      const close = modal.querySelector(".component-modal__close").getBoundingClientRect();
      modal.close();
      return { headingRight: heading.right, closeRight: close.right };
    });
    expect(bounds.headingRight).toBeGreaterThanOrEqual(bounds.closeRight);
  });

  test("does not reserve a duplicate safe-area gutter in iPhone landscape", async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const layout = await page.locator(".dashboard-scroll-region").evaluate(
      (region) => {
        const style = getComputedStyle(region);
        const bodyStyle = getComputedStyle(document.body);
        return {
          bodyPaddingLeft: bodyStyle.paddingLeft,
          bodyPaddingRight: bodyStyle.paddingRight,
          paddingLeft: style.paddingLeft,
          paddingRight: style.paddingRight,
          scrollbarGutter: style.scrollbarGutter,
        };
      },
    );
    expect(layout).toEqual({
      bodyPaddingLeft: "8px",
      bodyPaddingRight: "8px",
      paddingLeft: "6px",
      paddingRight: "6px",
      scrollbarGutter: "auto",
    });
  });

  test("keeps the status column readable beside a visible action on iPhone landscape", async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 });
    // Avoid the production empty-history retry racing this visual fixture.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    // In compact landscape this control intentionally lives in the closed
    // Options disclosure. Disable refresh without changing that layout.
    await page.evaluate(() => {
      const control = document.querySelector("#autoRefresh");
      control.checked = false;
      control.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-landscape-status",
        title: "Landscape retry action",
        status: "BLOCKED",
        can_retry: true,
      }];
      renderPromptHistory();
    });
    const status = page.locator("#promptHistoryRows .prompt-history-status--blocked");
    await expect(status).toHaveText("Geblokkeerd");
    await expect(status).toBeVisible();
    await expect(status).toHaveCSS("white-space", "nowrap");
    expect((await status.boundingBox()).width).toBeGreaterThanOrEqual(120);
    await expect(page.locator("#promptHistoryRows .execution-history-action").first()).toBeVisible();
  });

  test("projects dismissed handling beside the immutable terminal outcome", async ({ page }) => {
    // Keep the injected terminal-state fixture stable across parallel runs.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-dismissed-status",
        title: "Dismissed blocked execution",
        status: "BLOCKED",
        dismissed: true,
        handling_state: "DISMISSED",
        executed_at: "2026-08-08T10:00:00Z",
        total_execution_seconds: 125,
      }];
      renderPromptHistory();
    });

    await expect(page.locator("#promptHistoryRows .prompt-history-status--blocked"))
      .toHaveText("Geblokkeerd · Afgesloten");
    await expect(page.locator("#promptHistoryRows .prompt-history-row")).toContainText("(3 min)");
    await expect(page.locator("#promptHistoryRows .execution-history-action")).toHaveCount(0);
    await page.evaluate(() => {
      promptHistoryEntries = [{
        run_id: "inbox-missing-duration", title: "Missing duration", status: "BLOCKED",
        executed_at: "2026-08-08T10:00:00Z", total_execution_seconds: null,
      }];
      renderPromptHistory();
    });
    await expect(page.locator("#promptHistoryRows .prompt-history-row")).not.toContainText("(0 min)");
  });

  test("keeps execution detail modal borders inside iPhone landscape safe areas", async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const selector of ["#promptHistoryDetailModal", "#confirmationModal", "#promptHistoryReportModal", "#promptHistoryChatModal", "#componentModal"]) {
      const dialog = page.locator(selector);
      await dialog.evaluate((element) => element.showModal());
      const panelBox = await dialog.locator(".dashboard-modal-shell__panel").boundingBox();
      expect(panelBox).not.toBeNull();
      expect(panelBox.x).toBeGreaterThanOrEqual(16);
      expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(828);
      await dialog.evaluate((element) => element.close());
    }
  });

  test("centres the pull-request wait modal inside the iPhone portrait viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const modal = page.locator("#operatorMergeWaitModal");
    await modal.evaluate((element) => element.showModal());
    const panel = modal.locator(".confirmation-modal__panel");
    const box = await panel.boundingBox();

    expect(box).not.toBeNull();
    expect(box.x).toBeGreaterThanOrEqual(12);
    expect(box.x + box.width).toBeLessThanOrEqual(378);
    expect(box.y).toBeGreaterThanOrEqual(12);
    expect(box.y + box.height).toBeLessThanOrEqual(832);
    expect(Math.round(box.x + box.width / 2)).toBe(195);
    expect(Math.round(box.y + box.height / 2)).toBe(422);
  });

  test("keeps every modal panel inside iPhone safe outer padding and focuses only a primary action", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const selectors = await page.locator("dialog.dashboard-modal-shell").evaluateAll(
      (dialogs) => dialogs.map((dialog) => "#" + dialog.id),
    );
    for (const selector of selectors) {
      const modal = page.locator(selector);
      await modal.evaluate((element) => element.showModal());
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      const layout = await modal.evaluate((element) => {
        const panel = element.querySelector(".dashboard-modal-shell__panel");
        const primary = element.querySelector("button.dashboard-modal-shell__action--primary:not([disabled]), a.dashboard-modal-shell__action--primary[href]");
        const panelBox = panel.getBoundingClientRect();
        return {
          focusedPrimary: primary ? document.activeElement === primary : false,
          focusedWithinModal: element.contains(document.activeElement),
          panel: { bottom: panelBox.bottom, left: panelBox.left, right: panelBox.right, top: panelBox.top },
        };
      });
      expect(layout.focusedWithinModal, selector).toBe(layout.focusedPrimary);
      expect(layout.panel.left, selector).toBeGreaterThanOrEqual(16);
      expect(layout.panel.right, selector).toBeLessThanOrEqual(374);
      expect(layout.panel.top, selector).toBeGreaterThanOrEqual(16);
      expect(layout.panel.bottom, selector).toBeLessThanOrEqual(828);
      await modal.evaluate((element) => element.close());
    }
  });

  test("uses documented content-width caps for modal families on desktop", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    for (const [selector, maximumWidth] of [
      ["#confirmationModal", 480],
      ["#lifecycleDetailModal", 680],
      ["#promptHistoryChatModal", 960],
      ["#promptHistoryReportModal", 1000],
      ["#promptHistoryDetailModal", 1100],
      ["#telemetryDetailModal", 1120],
    ]) {
      const modal = page.locator(selector);
      await modal.evaluate((element) => element.showModal());
      await expect(modal.locator(".dashboard-modal-shell__panel")).toHaveCSS(
        "width",
        `${maximumWidth}px`,
      );
      await modal.evaluate((element) => element.close());
    }
  });

  test("uses only the chat category divider inside the prompt-history AI modal", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");
    await modal.evaluate((element) => element.showModal());

    await expect(modal.locator(".prompt-chat-modal__description")).toHaveCSS("border-bottom-color", "rgb(208, 164, 255)");
    await expect(modal.locator(".codex-chat__details")).toHaveCSS("border-top-width", "0px");
  });

  test("uses a one-line AI chat composer on iPhone landscape", async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");
    await modal.evaluate((element) => element.showModal());
    const input = page.locator("#chatInput");
    const send = page.locator("#chatSend");
    await expect(input).toHaveCSS("height", "44px");
    await expect(input).toHaveCSS("resize", "none");
    await expect(send).toHaveCSS("position", "static");
    expect((await input.boundingBox()).height).toBe(44);
    expect((await send.boundingBox()).height).toBe(44);
  });

  test("labels the splash screen as loading data", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("dashboard-splash-icon")).toHaveAttribute("src", "/assets/operations-console/icon-transparent.png");
    await expect(page.getByTestId("dashboard-splash-icon")).toHaveAttribute("aria-hidden", "true");
    await expect(page.locator(".dashboard-splash__version")).toContainText("2.0.0");
    await expect(page.locator(".dashboard-splash__loading")).toHaveText("Gegevens laden…");
    await expect(page.locator(".dashboard-splash__version")).toHaveCSS("color", "rgb(240, 182, 106)");
    await expect(page.locator(".dashboard-splash__spinner")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
  });

  test("finishes dashboard startup without browser errors", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("body")).toHaveClass(/dashboard-ready/);
    expect(errors).toEqual([]);
  });

  test("uses the house-style orange for an active execution spinner", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.route("**/api/events", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#indicator").evaluate((element) => {
      document.querySelector("#currentRun").hidden = false;
      element.className = "indicator indicator--running";
    });
    await expect(page.locator("#indicator")).toHaveCSS("animation-name", "github-activity-ring");
    await expect(page.locator("#indicator")).toHaveCSS("animation-duration", "1.1s");
    await expect(page.locator("#indicator")).toHaveCSS("animation-iteration-count", "infinite");
    await expect(page.locator("#indicator")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(page.locator("#indicator")).toHaveCSS("will-change", "transform");
    const elapsed = await page.locator("#indicator").evaluate(async (element) => {
      const [animation] = element.getAnimations();
      if (!animation) return null;
      const before = Number(animation.currentTime);
      await new Promise((resolve) => window.setTimeout(resolve, 150));
      return Number(animation.currentTime) - before;
    });
    expect(elapsed).toBeGreaterThan(100);
  });

  test("loads the initial status before serverpush connects", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: {
          watcher_state: "WATCHER_IDLE",
          platform_version: "2.0.0",
          queue_depth: 0,
          queue_items: [],
        },
      rate_limits: {
          provider: "Codex CLI",
          provider_version: "0.146.0",
          provider_path: "/opt/homebrew/bin/codex",
          windows: [],
          reset_credits: 0,
        },
        component_versions: {},
      }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    await expect(page.locator("#platformVersion")).toHaveText("2.0.0");
    await expect(page.locator("#queueSummary")).not.toHaveText("Wachtrij laden…");
    await expect(page.locator("#queueSummary")).toHaveText("0 uitvoeringen in de wachtrij.");
    await expect(page.locator("#rateLimits")).toBeVisible();
    await expect(page.locator("#rateLimitProvider")).toHaveText("Codex CLI · 0.146.0");
    await expect(page.locator("#rateLimitProviderPath")).toHaveText("/opt/homebrew/bin/codex");
    await expect(page.locator("#rateLimitDetails")).toHaveCSS("font-size", "14px");
  });

  test("offers an explicit, version-pinned Codex CLI update only when one is available", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0, queue_items: [] }, rate_limits: { provider: "Codex CLI", provider_version: "0.149.0", windows: [], reset_credits: 0 } },
    }));
    await page.route("**/api/codex-cli-update", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({ json: { updated: true, current_version: "0.150.0" } });
        return;
      }
      await route.fulfill({ json: { state: "update_available", update_available: true, current_version: "0.149.0", latest_version: "0.150.0" } });
    });
    const updateCheck = page.waitForResponse("**/api/codex-cli-update");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#rateLimits").evaluate((element) => { element.open = true; });
    await updateCheck;
    await expect(page.locator("#codexCliUpdateStatus")).toHaveText("Update beschikbaar: 0.150.0");
    await expect(page.locator("#codexCliUpdate")).toBeVisible();
    await expect(page.locator("#codexCliUpdate")).toHaveCSS("background-color", "rgb(31, 91, 66)");
    await expect(page.locator("#codexCliUpdate")).toContainText("Update");
    expect(await page.evaluate(() => {
      const provider = document.querySelector("#rateLimitProvider");
      const status = document.querySelector("#codexCliUpdateStatus");
      return Boolean(provider?.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    expect(await page.locator("#codexCliUpdate").evaluate(
      (button) => button.nextElementSibling?.classList.contains("rate-limit-provider-path"),
    )).toBe(true);
    expect(await page.locator("#codexCliUpdate").evaluate(
      (button) => getComputedStyle(button, "::before").content,
    )).toBe('"↓"');
    await page.locator("#codexCliUpdate").click();
    await expect(page.locator("#confirmationModalText")).toContainText("deze machine");
    await expect(page.locator("#confirmationModalText")).not.toContainText("deze Mac");
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#rateLimitProvider")).toHaveText("Codex CLI · 0.150.0");
    await expect(page.locator("#codexCliUpdate")).toBeHidden();
    await expect(page.locator("#codexCliUpdateStatus")).toHaveText("Codex CLI is bijgewerkt naar 0.150.0.");
  });

  test("keeps an available Codex CLI update visible but disabled during an active execution", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "inbox-active", queue_depth: 0, queue_items: [] }, rate_limits: { provider: "Codex CLI", provider_version: "0.149.0", windows: [], reset_credits: 0 } },
    }));
    await page.route("**/api/codex-cli-update", (route) => route.fulfill({
      json: { state: "update_available", update_available: true, current_version: "0.149.0", latest_version: "0.150.0" },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const updateCheck = page.waitForResponse("**/api/codex-cli-update");
    await page.locator("#rateLimits").evaluate((element) => { element.open = true; });
    await updateCheck;
    await expect(page.locator("#codexCliUpdate")).toBeVisible();
    await expect(page.locator("#codexCliUpdate")).toBeDisabled();
    await expect(page.locator("#codexCliUpdateStatus")).toHaveText("De Codex CLI-update is beschikbaar, maar kan pas worden geïnstalleerd wanneer geen uitvoering actief is.");
  });

  test("never renders a stale Codex CLI update response for the installed version", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0, queue_items: [] }, rate_limits: { provider: "Codex CLI", provider_version: "0.150.0", windows: [], reset_credits: 0 } },
    }));
    await page.route("**/api/codex-cli-update", (route) => route.fulfill({
      // This is the old poll response that may arrive just after an install.
      json: { state: "update_available", update_available: true, current_version: "0.150.0", latest_version: "0.150.0" },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const updateCheck = page.waitForResponse("**/api/codex-cli-update");
    await page.locator("#rateLimits").evaluate((element) => { element.open = true; });
    await updateCheck;
    await expect(page.locator("#codexCliUpdate")).toBeHidden();
    await expect(page.locator("#codexCliUpdateStatus")).toHaveText("Codex CLI is actueel (0.150.0).");
  });

  test("exposes the structured Engineering Platform health projection", async ({ request }) => {
    const response = await request.get(`${dashboardUrl}/health`);
    expect([200, 503]).toContain(response.status());

    const health = await response.json();
    expect(health).toEqual(expect.objectContaining({
      health: health.healthy ? "ok" : "degraded",
      healthy: expect.any(Boolean),
      components: expect.objectContaining({
        dashboard: expect.objectContaining({ healthy: true, state: "running" }),
        inbox_watcher: expect.objectContaining({ healthy: expect.any(Boolean) }),
        dashboard_relay: expect.objectContaining({ healthy: expect.any(Boolean) }),
      }),
    }));
    expect(health.components.dashboard.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(health.components.inbox_watcher.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(health.components.dashboard.uptime_seconds).toEqual(expect.any(Number));
    expect(health.components.inbox_watcher).toHaveProperty("uptime_seconds");
    expect(health.components.dashboard_relay).toHaveProperty("uptime_seconds");
    expect(health.components).not.toHaveProperty("private_remote_access");

    const favicon = await request.get(`${dashboardUrl}/assets/operations-console/apple-touch-icon-dark.png`);
    expect(favicon.status()).toBe(200);
    expect(favicon.headers()["content-type"]).toContain("image/png");
    const stylesheet = await request.get(`${dashboardUrl}/assets/dashboard.css`);
    expect(stylesheet.status()).toBe(200);
    expect(stylesheet.headers()["content-type"]).toContain("text/css");
    const script = await request.get(`${dashboardUrl}/assets/dashboard.js`);
    expect(script.status()).toBe(200);
    expect(script.headers()["content-type"]).toContain("text/javascript");
    const locales = await request.get(`${dashboardUrl}/assets/dashboard_locales.mjs`);
    expect(locales.status()).toBe(200);
    expect(locales.headers()["content-type"]).toContain("text/javascript");
    const statusStore = await request.get(`${dashboardUrl}/assets/dashboard_status_store.mjs`);
    expect(statusStore.status()).toBe(200);
    expect(statusStore.headers()["content-type"]).toContain("text/javascript");
  });

  test("shows uptime only for locally owned processes", async ({ page }) => {
    // Keep the initial asynchronous health request from replacing the
    // deliberately rendered fixture while this presentation-only test runs.
    await page.route("**/health", (route) => route.fulfill({ json: { components: {} } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    // Read the fixture within the same browser task as the render. The page
    // also refreshes live health data on an interval, so awaiting a separate
    // locator operation can otherwise race the deliberately static fixture.
    const componentText = await page.evaluate(() => {
      renderPlatformHealth({ components: {
        dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "1.2.82", uptime_seconds: 3725 },
        inbox_watcher: { healthy: true, detail: "LaunchAgent is geladen", version: "1.1.4", uptime_seconds: 75 },
      }});
      return document.querySelector("#platformHealthComponents")?.textContent || "";
    });
    expect(componentText).toContain("Uptime 1u 2m");
    expect(componentText).toContain("Uptime 1m");
    expect(componentText).not.toContain("Statusopslag");
  });

  test("centres component information actions and balances component-card text padding", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await page.evaluate(() => renderPlatformHealth({ components: {
      dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "1.2.87", uptime_seconds: 1440 },
    }}));

    const alignment = await page.locator(".platform-health__component").first().evaluate((card) => {
      const box = card.getBoundingClientRect();
      const name = card.querySelector(".platform-health__component-name").getBoundingClientRect();
      const detail = card.querySelector(".platform-health__component-detail").getBoundingClientRect();
      const info = card.querySelector(".component-info").getBoundingClientRect();
      return {
        infoCentreOffset: Math.abs((info.top + info.height / 2) - (box.top + box.height / 2)),
        paddingDifference: Math.abs((name.top - box.top) - (box.bottom - detail.bottom)),
      };
    });

    expect(alignment.infoCentreOffset).toBeLessThan(1);
    expect(alignment.paddingDifference).toBeLessThan(1);
    await expect(page.locator(".platform-health__component").first()).toHaveAttribute("role", "button");
    await expect(page.locator(".platform-health__component").first()).toHaveAttribute("tabindex", "0");
  });

  test("uses a green information glyph for component actions", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await page.evaluate(() => renderPlatformHealth({ components: {
      dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "1.2.87" },
    }}));
    const info = page.locator(".platform-health .component-info").first();

    await expect(info).toHaveCSS("min-height", "32px");
    await expect(info).toHaveCSS("min-width", "32px");
    await expect(info).toHaveCSS("border-top-color", "rgb(163, 230, 53)");
    await expect(info).toHaveCSS("color", "rgb(163, 230, 53)");
  });

  test("keeps iPhone platform component cards on opaque, flat surfaces", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await page.evaluate(() => renderPlatformHealth({ components: {
      dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "2.0.0" },
      inbox_watcher: { healthy: true, detail: "LaunchAgent is geladen", version: "2.0.0" },
    }}));

    const surfaces = await page.locator("#platformHealth > summary, .platform-health__component").evaluateAll(
      (elements) => elements.map((element) => {
        const style = getComputedStyle(element);
        return {
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
          backdropFilter: style.backdropFilter,
        };
      }),
    );
    expect(surfaces.length).toBeGreaterThan(2);
    expect(surfaces.every((surface) => surface.backgroundColor !== "rgba(0, 0, 0, 0)")).toBe(true);
    expect(surfaces.every((surface) => surface.backgroundImage === "none")).toBe(true);
    expect(surfaces.every((surface) => surface.backdropFilter === "none")).toBe(true);
  });

  test("uses the execution host title for the core local component", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "inbox_watcher",
      healthy: true,
      detail: "connected",
      git_commit: "host-only-commit",
      processes: [{ pid: 123, memory_kib: 1024 }],
      launchd: {},
    }));

    const modal = page.locator("#componentModal");
    await expect(modal).toContainText("Engineering Execution Host");
  });

  test("renders the safe current Codex activity from the live status projection", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#platformVersion")).toHaveText("Niet beschikbaar");
    // Keep the fixture authoritative: a delayed initial SSE snapshot must not
    // overwrite this intentionally projected active run.
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      platform_version: "2.0.0",
      current_phase: "CAPABILITY_REVIEW",
      current_action: "Capability review: documentation",
      run_id: "activity-run",
      prompt_title: "Veilige voortgang",
      submitted_filename: "activity.md",
      workspace_progress: { modified: 3, created: 2, deleted: 1, codex_commands_executed: 17 },
    }, {}));

    await expect(page.locator("#currentRun")).toBeVisible();
    await expect(page.locator("#platformVersion")).toHaveText("2.0.0");
    await expect(page.locator("#phase")).toHaveText("Specialistenreview");
    await expect(page.locator("#action")).toHaveText("Documentatie voert een specialistenreview uit");
    await expect(page.locator("#action")).toHaveCSS("font-style", "italic");
    await expect(page.locator("#workspaceProgressValue")).toHaveText("3 gewijzigd · 2 nieuw · 1 verwijderd · 17 primaire Codex-opdrachten uitgevoerd · 0 reviewer-Codex-opdrachten uitgevoerd");

    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      current_action: "invoke_agent",
      run_id: "activity-run",
      prompt_title: "Veilige voortgang",
      submitted_filename: "activity.md",
    }, {}));
    await expect(page.locator("#action")).toHaveText("Codex voert de uitvoering uit");
  });

  test("places diagnosis beside the current deviation when its container has room", async ({ page }) => {
    await page.setViewportSize({ width: 920, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "HOST_PREFLIGHT_FAILED",
      current_phase: "INITIALIZE",
      diagnostic: "Host preflight failed",
    }, {
      host_preflight: { outcome: "FAILED" },
      current_drift: {
        drift_id: "managed_expected_branch",
        severity: "BLOCKING",
        affected_component: "managed_expected_branch",
        expected_value: "managed_expected_branch: PASS",
        observed_value: "Managed target is not on the expected branch main.",
        resolution_recommendation: "Switch the repository to main before submitting work.",
      },
    }));

    const columns = async () => page.locator("#technicalDetails .technical-grid").evaluate((element) =>
      getComputedStyle(element).gridTemplateColumns.split(" ").length,
    );
    await expect.poll(columns).toBe(2);
    const [driftBounds, diagnosisBounds] = await Promise.all([
      page.locator("#driftDiagnosticsCard").boundingBox(),
      page.locator("#technicalDiagnosticsCard").boundingBox(),
    ]);
    expect(diagnosisBounds).not.toBeNull();
    expect(driftBounds).not.toBeNull();
    expect(Math.abs(diagnosisBounds.width - driftBounds.width)).toBeLessThanOrEqual(1);
    expect(diagnosisBounds.x).toBeGreaterThan(driftBounds.x);
    expect(Math.abs(diagnosisBounds.y - driftBounds.y)).toBeLessThanOrEqual(1);

    await page.setViewportSize({ width: 760, height: 844 });
    await expect.poll(columns).toBe(1);
  });

  test("localizes projected managed-branch drift in every supported locale", async ({ page }) => {
    const drift = {
      drift_id: "managed-branch-drift",
      severity: "BLOCKING",
      affected_component: "managed_expected_branch",
      expected_value: "managed_expected_branch: PASS",
      observed_value: "Managed target is not on the expected branch main.",
      resolution_recommendation: "Switch the repository to main before submitting work.",
    };
    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await waitForDashboardReady(page);
      await selectDashboardLocale(page, language);
      await page.locator("#autoRefresh").uncheck();
      await page.evaluate((currentDrift) => r({
        watcher_state: "WORKSPACE_PREFLIGHT_FAILED",
        current_phase: "INITIALIZE",
        diagnostic: "Workspace preflight blocked by managed_expected_branch.",
      }, { current_drift: currentDrift }), drift);
      await expect(page.locator("#driftSeverity")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.drift.severity.blocking"],
      );
      await expect(page.locator("#driftComponent")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.drift.component.managed_expected_branch"],
      );
      await expect(page.locator("#driftExpected")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.drift.expected.managed_expected_branch"].replace("{branch}", "main"),
      );
      await expect(page.locator("#driftObserved")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.drift.observed.managed_expected_branch"].replace("{branch}", "main"),
      );
      await expect(page.locator("#driftResolution")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.drift.resolution.managed_expected_branch"].replace("{branch}", "main"),
      );
    }
  });

  test("localizes runtime and transport machine codes in every supported locale", async ({ page }) => {
    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await waitForDashboardReady(page);
      await selectDashboardLocale(page, language);
      await page.locator("#autoRefresh").uncheck();
      await page.evaluate(() => r({ watcher_state: "ENGINEERING_RUN_ACTIVE" }, {
        execution_host: { runtime: "codex_cli", runtime_prompt_transport: "icloud_inbox" },
      }));
      await expect(page.locator("#executionHostRuntime")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.runtime_value.codex_cli"],
      );
      await expect(page.locator("#executionHostTransport")).toHaveText(
        DASHBOARD_MESSAGES[language]["technical.runtime_transport_value.icloud_inbox"],
      );
      await expect(page.locator("#executionHostRuntime")).toHaveAttribute("title", "codex_cli");
      await expect(page.locator("#executionHostTransport")).toHaveAttribute("title", "icloud_inbox");
    }
  });

  test("shows the managed Codex CLI provenance in host preflight", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE" }, {
      host_preflight: {
        outcome: "PASS",
        runtime_version: "0.150.1",
        runtime_path: "/Users/example/.local/share/engineering-platform/codex-cli/bin/codex",
      },
    }));

    await expect(page.locator("#executionHostRuntimeVersion")).toHaveText("0.150.1");
    await expect(page.locator("#executionHostRuntimePath")).toHaveText(
      "/Users/example/.local/share/engineering-platform/codex-cli/bin/codex",
    );
    const language = await page.locator("#dashboardLocale").inputValue();
    await expect(page.locator("#technicalRuntimeVersionLabel")).toHaveText(
      DASHBOARD_MESSAGES[language]["detail.codex_cli_version"],
    );
    await expect(page.locator("#technicalRuntimePathLabel")).toHaveText(
      DASHBOARD_MESSAGES[language]["detail.codex_cli_installation_path"],
    );
  });

  test("shows technical diagnosis only for active or attention-needing executions", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const diagnosis = page.locator("#technicalDetails");
    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE", queue_depth: 0 }, {}));
    await expect(diagnosis).toBeHidden();

    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      run_id: "healthy-run",
    }, {}));
    await expect(diagnosis).toBeVisible();
    await expect(page.locator("#technicalHealthySummary")).toHaveText("Hostcontrole geslaagd");
    await expect(page.locator("#technicalDiagnosisDetails")).toBeHidden();

    await page.evaluate(() => r({
      watcher_state: "HOST_PREFLIGHT_FAILED",
      current_phase: "INITIALIZE",
      diagnostic: "Host preflight failed",
    }, { host_preflight: { outcome: "FAILED" } }));
    await expect(diagnosis).toBeVisible();
    await expect(page.locator("#technicalHealthySummary")).toBeHidden();
    await expect(page.locator("#technicalDiagnosisDetails")).toBeVisible();
    await expect(page.locator("#technicalDetailsDescription")).toContainText("herstelbewijs");
  });

  test("keeps specialist reviewer titles in the active-execution turquoise scale in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "review-run", current_phase: "CAPABILITY_REVIEW",
      reviewer_agents: [{ reviewer: "repository_governance", capability: "engineering", status: "completed" }],
    }, {}));
    await expect(page.locator(".reviewer-agent__name")).toHaveCSS("color", "rgb(24, 120, 132)");
  });

  test("shows live and completed reviewer status indicators", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "reviewer-status-run", current_phase: "CAPABILITY_REVIEW",
      reviewer_agents: [
        { reviewer: "validation", capability: "engineering", status: "running" },
        { reviewer: "documentation", capability: "engineering", status: "completed" },
      ],
    }, {}));

    const cards = page.locator(".reviewer-agent");
    await expect(cards).toHaveCount(2);
    await expect(cards.nth(0).locator(".reviewer-agent__status--running")).toHaveAttribute(
      "aria-label", /.+/,
    );
    await expect(cards.nth(1).locator(".reviewer-agent__status--completed")).toHaveText("✓");
    await expect(cards.nth(1).locator(".reviewer-agent__status--completed")).toHaveAttribute(
      "aria-label", /.+/,
    );
  });

  test("hides stale reviewer projections outside capability review", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await statusLoaded;
    await page.locator("#autoRefresh").uncheck();
    const reviewerAgents = [
      { reviewer: "validation", capability: "engineering", status: "running" },
      { reviewer: "documentation", capability: "engineering", status: "running" },
    ];
    for (const current_phase of ["FINALIZE_AGENT", "RECONCILE_AGENT"]) {
      await page.evaluate(({ current_phase, reviewer_agents }) => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE", current_phase,
        run_id: "reviewer-paused-run", reviewer_agents,
      }, {}), { current_phase, reviewer_agents: reviewerAgents });
      await expect(page.locator("#activeReviewerAgents")).toBeHidden();
      await expect(page.locator(".reviewer-agent")).toHaveCount(0);
    }
  });

  test("localizes specialist reviewer names and lifecycle status", async ({ page }) => {
    test.setTimeout(60_000);
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const reviewerAgents = [
      { reviewer: "repository_governance", capability: "engineering", status: "completed" },
      { reviewer: "validation", capability: "engineering", status: "running" },
    ];
    const renderReviewers = () => page.evaluate((reviewer_agents) => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "localized-reviewers", current_phase: "CAPABILITY_REVIEW",
      reviewer_agents,
    }, {}), reviewerAgents);
    const selectLocale = async (language) => {
      await selectDashboardLocale(page, language);
      await page.locator("#autoRefresh").uncheck();
      await renderReviewers();
    };

    await selectLocale("en");
    const cards = page.locator(".reviewer-agent");
    await expect(cards.nth(0).locator(".reviewer-agent__name")).toHaveText("Repository governance");
    await expect(cards.nth(0).locator(".reviewer-agent__meta")).toHaveText("Engineering · Completed");

    await selectLocale("nl");
    await expect(cards.nth(0).locator(".reviewer-agent__name")).toHaveText("Repositorygovernance");
    await expect(cards.nth(0).locator(".reviewer-agent__meta")).toHaveText("Engineering · Uitgevoerd");
    await expect(cards.nth(1).locator(".reviewer-agent__name")).toHaveText("Validatie");
    await expect(cards.nth(1).locator(".reviewer-agent__meta")).toHaveText("Engineering · Bezig");

    await selectLocale("de");
    await expect(cards.nth(0).locator(".reviewer-agent__name")).toHaveText("Repository-Governance");
    await expect(cards.nth(0).locator(".reviewer-agent__meta")).toHaveText("Engineering · Abgeschlossen");
    await expect(cards.nth(1).locator(".reviewer-agent__name")).toHaveText("Validierung");
    await expect(cards.nth(1).locator(".reviewer-agent__meta")).toHaveText("Engineering · Wird ausgeführt");

    await selectLocale("fr");
    await expect(cards.nth(0).locator(".reviewer-agent__name")).toHaveText("Gouvernance du dépôt");
    await expect(cards.nth(1).locator(".reviewer-agent__meta")).toHaveText("Ingénierie · En cours");

    await selectLocale("es");
    await expect(cards.nth(0).locator(".reviewer-agent__name")).toHaveText("Gobernanza del repositorio");
    await expect(cards.nth(1).locator(".reviewer-agent__meta")).toHaveText("Ingeniería · En curso");
  });

  test("wraps specialist reviewers into compact responsive tiles", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.setViewportSize({ width: 1440, height: 900 });
    const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await statusLoaded;
    await page.locator("#autoRefresh").uncheck();
    const reviewer_agents = ["repository_governance", "validation", "documentation", "finalization"]
      .map((reviewer) => ({ reviewer, capability: "engineering", status: "completed" }));
    await page.evaluate((reviewers) => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "review-grid", current_phase: "CAPABILITY_REVIEW", reviewer_agents: reviewers,
    }, {}), reviewer_agents);
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });

    const tiles = page.locator(".reviewer-agent");
    await expect(tiles).toHaveCount(4);
    await expect(tiles.first()).toBeVisible();
    const reviewerGrid = page.locator("#currentRun .current-run__grid");
    const reviewerCard = page.locator("#activeReviewerAgents");
    const [gridBounds, cardBounds] = await Promise.all([reviewerGrid.boundingBox(), reviewerCard.boundingBox()]);
    expect(gridBounds).not.toBeNull();
    expect(cardBounds).not.toBeNull();
    expect(Math.abs(cardBounds.width - gridBounds.width)).toBeLessThanOrEqual(1);
    const wideRows = await tiles.evaluateAll((elements) => elements.map((element) => Math.round(element.getBoundingClientRect().y)));
    expect(new Set(wideRows).size).toBe(1);

    await page.setViewportSize({ width: 390, height: 844 });
    const narrowRows = await tiles.evaluateAll((elements) => elements.map((element) => Math.round(element.getBoundingClientRect().y)));
    expect(new Set(narrowRows).size).toBe(4);
  });

  test("uses related primary and secondary accents for category titles and field labels", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      run_id: "paired-colours",
      prompt_title: "Kleurenhiërarchie",
      submitted_filename: "paired-colours.md",
    }, {}));

    await expect(page.locator("#currentRun > summary > .label")).toHaveCSS("color", "rgb(101, 197, 217)");
    await expect(page.locator("#currentRun .card .label").first()).toHaveCSS("color", "rgb(167, 231, 242)");
    await expect(page.locator("#technicalDetails .card .label").first()).toHaveCSS("color", "rgb(167, 231, 242)");
  });

  test("uses neutral content below the tinted heading of an expanded main category", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "dark";
      document.getElementById("workspaceCard").open = true;
    });

    const surfaces = await page.locator("#workspaceCard").evaluate((element) => {
      const summary = element.querySelector("summary");
      return {
        content: getComputedStyle(element).backgroundColor,
        heading: getComputedStyle(summary).backgroundColor,
      };
    });
    expect(surfaces.content).toBe("rgb(36, 36, 45)");
    expect(surfaces.heading).not.toBe(surfaces.content);
  });

  test("keeps main-category descriptions visible inside collapsed headings", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const workspace = page.locator("#workspaceCard");
    await workspace.evaluate((element) => { element.open = false; });

    const description = workspace.locator(":scope > summary > .category-description");
    await expect(description).toHaveText("De lokale werkruimte en opslag die voor dit project worden gebruikt.");
    await expect(description).toBeVisible();
    await expect(workspace.locator(":scope > summary")).toHaveCSS("border-bottom-width", "0px");
    await expect(workspace.locator(":scope > summary")).toHaveCSS("margin-bottom", "0px");
    await workspace.evaluate((element) => { element.open = true; });
    await expect(workspace.locator(":scope > summary")).toHaveCSS("border-bottom-width", "1px");
  });

  test("refines the active duration indication with comparable runtime history", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "EXECUTE_AGENT",
      run_id: "duration-run",
      prompt_characters: 1000,
    }, {
      duration_estimate: {
        sample_count: 3,
        lower_seconds: 1800,
        upper_seconds: 2400,
      },
    }));

    await expect(page.locator("#executionEstimate")).toHaveText("Indicatieve totale duur: 25–34 minuten");
    await expect(page.locator("#executionEstimateMeta")).toContainText(
      "3 vergelijkbare voltooide uitvoeringen",
    );
  });

  test("uses phase-aware comparable telemetry for the remaining duration", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: { status: { watcher_state: "WATCHER_IDLE" } } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE",
      current_phase: "FINALIZE_AGENT",
      run_id: "phase-aware-duration-run",
      prompt_characters: 1000,
    }, {
      duration_estimate: {
        sample_count: 3,
        phase_aware: true,
        phase_sample_count: 3,
        remaining_lower_seconds: 600,
        remaining_upper_seconds: 900,
      },
    }));

    await expect(page.locator("#executionEstimate")).toHaveText("Indicatief resterend: 10–15 minuten");
    await expect(page.locator("#executionEstimateMeta")).toContainText("3 vergelijkbare voltooide uitvoeringen");
  });

  test("shows the elapsed duration explanation only once without learned history", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" }, build_commit: "" },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", current_phase: "EXECUTE_AGENT", run_id: "duration-copy",
      prompt_characters: 1000,
    }, { prompt_started: { started_at: new Date().toISOString() }, duration_estimate: {} }));
    await expect(page.locator("#executionEstimateMeta")).toHaveText(
      "0 minuten verstreken.\nGebaseerd op opdrachtomvang, fase en verstreken tijd. Geen live Codex-voortgang of tokenverbruik.",
    );
  });

  test("keeps the active prompt category visible for a blocked predecessor", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#platformVersion")).toHaveText("Niet beschikbaar");
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({
      watcher_state: "WATCHER_IDLE",
      current_phase: "BLOCKED",
      current_action: "Wacht op een herstelde voorganger.",
      prompt_title: "Geblokkeerde voorganger",
      submitted_filename: "blocked.md",
      blocking_predecessor_run: "blocked-run",
      blocking_predecessor_title: "Geblokkeerde voorganger",
      blocking_predecessor_phase: "BLOCKED",
      predecessor_recovery_action: "Dien de herstelde prompt opnieuw in.",
      lifecycle: {
        available: true,
        run_id: "blocked-run",
        terminal_state: "BLOCKED",
        steps: [
          { id: "start", presentation_key: "lifecycle.step.start", state: "COMPLETED" },
          { id: "terminal", presentation_key: "lifecycle.step.terminal", state: "BLOCKED" },
        ],
      },
    }, {}));

    await expect(page.locator("#currentRun")).toBeVisible();
    await expect(page.locator("#currentRun")).toHaveAttribute("open", "");
    await expect(page.locator("#predecessorGate")).toBeVisible();
    await expect(page.locator("#predecessorGate")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(page.locator("#predecessorGate")).toHaveCSS("border-right-color", "rgb(240, 182, 106)");
    await expect(page.locator("#predecessorRun")).toHaveText("blocked-run");
    await expect(page.locator("#currentRun .execution-lifecycle")).toHaveAttribute("data-run-id", "blocked-run");
    await expect(page.locator("#currentRun .execution-lifecycle__item--blocked")).toHaveCount(1);
  });

  test("keeps a terminal blocked run out of Active Prompt", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#platformVersion")).toHaveText("Niet beschikbaar");
    await page.evaluate(() => r({
      watcher_state: "WATCHER_IDLE",
      prompt_title: "Geblokkeerde prompt",
      submitted_filename: "blocked.md",
      last_executed_run: "blocked-run",
      last_executed_title: "Geblokkeerde prompt",
      last_executed_filename: "blocked.md",
      last_executed_phase: "BLOCKED",
    }, {}));

    await expect(page.locator("#currentRun")).toBeHidden();
    await expect(page.locator("#predecessorGate")).toBeHidden();
    await expect(page.locator("#queueItems")).toBeVisible();
  });

  test("shows a sticky localized banner for a Codex usage-limit block", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "WATCHER_IDLE",
      current_phase: "BLOCKED",
      terminal_condition: "codex_usage_limit_reached",
    }, {}));
    const banner = page.getByTestId("codex-usage-limit-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.codex_usage_limit.title"]);
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.codex_usage_limit.body"]);
    // The shared header owns the sticky geometry for both limit banners.
    await expect(banner).toHaveCSS("position", "relative");
    await expect(banner).toHaveCSS("background-color", "rgb(91, 29, 39)");
    await expect(page.locator(".dashboard-sticky-header")).toHaveCSS("padding-bottom", "7px");
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const titleBar = document.querySelector(".dashboard-titlebar");
      const banner = document.querySelector("#codexUsageLimitBanner");
      document.querySelector("#engineering-dashboard-content").style.minHeight = "2600px";
      region.scrollTop = 180;
      return {
        regionTop: Math.round(region.getBoundingClientRect().top),
        titleTop: Math.round(titleBar.getBoundingClientRect().top),
        titleBottom: Math.round(titleBar.getBoundingClientRect().bottom),
        bannerTop: Math.round(banner.getBoundingClientRect().top),
      };
    });
    expect(layout.titleTop).toBe(layout.regionTop);
    expect(layout.bannerTop).toBe(layout.titleBottom);

    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE" }, {}));
    await expect(banner).toBeHidden();
  });

  test("warns below ten percent and turns red below five percent of a Codex limit", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const banner = page.getByTestId("codex-usage-limit-banner");

    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE" }, {
      rate_limits: { windows: [{ used_percent: 91 }] },
    }));
    await expect(banner).toBeVisible();
    await expect(banner).toHaveClass(/dashboard-status-banner--usage-warning/);
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.codex_usage_warning.title"]);
    await expect(banner).toContainText("9%");

    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE" }, {
      rate_limits: { windows: [{ used_percent: 96 }] },
    }));
    await expect(banner).toHaveClass(/dashboard-status-banner--usage-critical/);
    await expect(banner).toContainText(DASHBOARD_MESSAGES.nl["notification.codex_usage_critical.title"]);
    await expect(banner).toContainText("4%");

    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE" }, {
      rate_limits: { windows: [{ used_percent: 90 }] },
    }));
    await expect(banner).toBeHidden();
  });

  test("keeps a watcher-failed stale live run out of Active Prompt", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "JOB_FAILED",
      run_id: "inbox-failed",
      current_phase: "WAIT_FOR_TERMINAL_EVIDENCE",
      current_action: "poll_required_checks",
      last_executed_run: "inbox-failed",
      last_executed_phase: "FAILED",
    }, {}));

    await expect(page.locator("#currentRun")).toBeHidden();
  });

  test("keeps a stale finalization lifecycle visible without claiming an active runner", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ status: { watcher_state: "WATCHER_IDLE" } }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_STALE",
      run_id: "inbox-finalization-stale",
      current_phase: "FINALIZE_AGENT",
      current_action: "Execution Host ownership is stale; no execution is currently running.",
      lifecycle: {
        available: true,
        run_id: "inbox-finalization-stale",
        current_step: "FINALIZE_AGENT",
        steps: [{ id: "FINALIZE_AGENT", presentation_key: "lifecycle.step.finalize_agent", state: "ACTIVE" }],
      },
    }, {}));

    await expect(page.locator("#currentRun")).toBeVisible();
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    await expect(page.locator("#currentRun .execution-lifecycle")).toBeVisible();
    await expect(page.locator("#currentRun .execution-lifecycle__node--active")).toHaveCount(1);
    await expect(page.locator("#indicator")).not.toHaveClass(/indicator--running/);
  });

  test("allows the AI question field to grow only vertically", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#promptHistoryChatModal").evaluate((element) => element.showModal());
    await expect(page.locator("#chatInput")).toHaveCSS("resize", "vertical");
  });

  test("keeps comfortable inner padding when the AI question field is focused", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#promptHistoryChatModal").evaluate((element) => element.showModal());
    const input = page.locator("#chatInput");
    await input.focus();

    await expect(input).toHaveCSS("padding-top", "14px");
    await expect(input).toHaveCSS("padding-left", "16px");
    await expect(input).toHaveCSS("padding-bottom", "68px");
    await expect(input).toHaveCSS("padding-right", "68px");
  });

  test("bounds and sanitizes free-form dashboard input client-side", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#chatInput")).toHaveAttribute("maxlength", "2000");
    await expect(page.locator("#promptHistoryFilter")).toHaveAttribute("maxlength", "160");
    await expect(page.locator("#logFilter")).toHaveAttribute("maxlength", "160");

    const values = await page.locator("#chatInput, #promptHistoryFilter").evaluateAll((inputs) => Object.fromEntries(inputs.map((input) => {
      input.value = input.id === "chatInput"
        ? `eerste\r\ntweede\u202E${"x".repeat(2100)}`
        : `zoek\u0000term\n${"x".repeat(200)}`;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return [input.id, input.value];
    })));

    expect(values.chatInput).toHaveLength(2000);
    expect(values.chatInput).toContain("eerste\ntweede");
    expect(values.chatInput).not.toContain("\u202E");
    expect(values.promptHistoryFilter).toHaveLength(160);
    expect(values.promptHistoryFilter).toContain("zoekterm");
    expect(values.promptHistoryFilter).not.toContain("\u0000");
    expect(values.promptHistoryFilter).not.toContain("\n");
  });

  test("keeps platform health status text readable in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.evaluate(() => renderPlatformHealth({ components: {
      dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "1.2.87" },
    }}));

    await expect(page.locator(".platform-health__component").first()).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(page.locator(".platform-health__component-detail").first()).toHaveCSS("color", "rgb(24, 34, 48)");
  });

  test("renders component details in the light modal theme", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await expect(page.locator(".component-info").first()).toHaveCSS("color", "rgb(60, 116, 17)");
    await dispatchDashboardPointerClick(page.locator(".platform-health__component").first());

    await expect(page.locator("#componentModal")).toHaveAttribute("open", "");
    await expect(page.locator("#componentModal")).not.toBeFocused();
    await expect(page.locator(".component-modal__panel")).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await expect(page.locator(".component-modal__panel")).toHaveCSS("color", "rgb(24, 34, 48)");
    await expect(page.locator(".component-modal__header")).toHaveCSS("border-bottom-color", "rgb(163, 230, 53)");
    await expect(page.locator("#componentModalClose")).toHaveCSS("font-size", "18px");
    await expect(page.locator("#componentModalClose")).toHaveCSS("min-height", "32px");
    await expect(page.locator("#componentModalClose")).toHaveCSS("min-width", "32px");
  });

  test("centres the component modal panel within iPhone-width viewports", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "inbox_watcher",
      healthy: true,
      detail: "connected",
      launchd: {},
    }));

    const geometry = await page.locator("#componentModal").evaluate((modal) => {
      const panel = modal.querySelector(".component-modal__panel").getBoundingClientRect();
      const viewport = document.documentElement.clientWidth;
      return {
        containerWidth: modal.getBoundingClientRect().width,
        leftGutter: panel.left,
        rightGutter: viewport - panel.right,
      };
    });
    expect(geometry.containerWidth).toBe(390);
    expect(Math.abs(geometry.leftGutter - geometry.rightGutter)).toBeLessThanOrEqual(1);
  });

  test("uses a neutral hover fill for the component-detail close action", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "inbox_watcher",
      healthy: true,
      detail: "connected",
      launchd: {},
    }));
    const close = page.locator("#componentModalClose");

    await close.hover();
    await expect(close).toHaveCSS("background-color", "rgb(240, 182, 106)");
  });

  test("uses a green hover fill for the component restart action", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "dashboard",
      healthy: true,
      detail: "running",
      launchd: {},
      restart_supported: true,
    }));
    const restart = page.locator("#componentModalRestart");

    await restart.hover();
    await expect(restart).toHaveCSS("background-color", "rgb(163, 230, 53)");
    await expect(restart).toHaveCSS("color", "rgb(24, 35, 15)");
  });

  test("keeps the confirmation cancel action dark in dark mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "dashboard",
      healthy: true,
      detail: "running",
      launchd: {},
      restart_supported: true,
    }));
    await page.locator("#componentModalRestart").click();
    await page.mouse.move(0, 0);

    await expect(page.locator("#confirmationModalCancel")).toHaveCSS("background-color", "rgb(36, 36, 45)");
    await expect(page.locator("#confirmationModalCancel")).toHaveCSS("color", "rgb(247, 243, 238)");
  });

  test("renders house-orange confirmation dialogs as light surfaces in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.evaluate(() => showComponentModal({
      component: "dashboard",
      healthy: true,
      detail: "running",
      launchd: {},
      restart_supported: true,
    }));
    await page.locator("#componentModalRestart").click();

    await expect(page.locator("#confirmationModal")).toHaveAttribute("data-theme-mode", "light");
    await expect(page.locator("#confirmationModal .confirmation-modal__panel")).toHaveCSS("background-color", "rgb(247, 251, 255)");
    await expect(page.locator("#confirmationModal .confirmation-modal__panel")).toHaveCSS("color", "rgb(24, 34, 48)");
    await expect(page.locator("#confirmationModalText")).toHaveCSS("color", "rgb(24, 34, 48)");
    await expect(page.locator("#confirmationModalTitle")).toHaveCSS("color", "rgb(240, 182, 106)");
    await expect(page.locator("#confirmationModal .confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    expect(await page.locator("#confirmationModalConfirm").evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe("rgb(240, 182, 106)");
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await page.locator("#confirmationModalCancel").hover();
    await expect(page.locator("#confirmationModalCancel")).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(page.locator("#confirmationModalCancel")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await page.locator("#confirmationModalConfirm").hover();
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("background-color", "rgb(240, 182, 106)");
  });

  test("shows the splash screen before reloading the dashboard", async ({ page }) => {
    await page.route("**/api/components/dashboard/restart", (route) =>
      route.fulfill({ status: 202, json: { restarting: "dashboard" } }),
    );
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => showComponentModal({
      component: "dashboard",
      healthy: true,
      detail: "running",
      launchd: {},
      restart_supported: true,
    }));
    await page.locator("#componentModalRestart").click();
    await page.locator("#confirmationModalConfirm").click();

    await expect(page.getByTestId("dashboard-splash")).toBeVisible();
    await expect(page.locator("body")).not.toHaveClass(/dashboard-ready/);
  });

  test("refreshes uptime and memory while component details remain open", async ({ page }) => {
    let detailsRequest = 0;
    await page.route("**/api/components/dashboard/details", (route) => {
      detailsRequest += 1;
      return route.fulfill({ json: {
        component: "dashboard",
        healthy: true,
        detail: "HTTP-dashboard reageert",
        version: "1.2.87",
        uptime_seconds: detailsRequest === 1 ? 10 : 20,
        processes: [{ pid: 42, memory_kib: detailsRequest === 1 ? 1024 : 2048 }],
        launchd: {},
        restart_supported: true,
      } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await dispatchDashboardPointerClick(page.locator(".component-info").first());

    await expect(page.locator("#componentModalContent")).toContainText("Uptime10s");
    await expect(page.locator("#componentModalContent")).toContainText("PID 42: 1.0 MiB");
    await page.evaluate(() => refreshOpenComponentDetails());
    await expect(page.locator("#componentModalContent")).toContainText("Uptime20s");
    await expect(page.locator("#componentModalContent")).toContainText("PID 42: 2.0 MiB");

    await page.locator("#componentModalClose").click();
    await expect.poll(() => page.evaluate(() => componentDetailsRefreshTimer)).toBeNull();
  });

  test("renders prompt-history report actions as light surfaces in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModal").showModal();
      document.querySelector("#promptHistoryReportContent").textContent = "# Rapport\n\nInhoud";
      document.querySelector("#promptHistoryReportCopy").hidden = false;
      document.querySelector("#promptHistoryReportDownload").hidden = false;
    });
    for (const selector of ["#promptHistoryReportContent", "#promptHistoryReportCopy", "#promptHistoryReportDownload"]) {
      expect(await page.locator(selector).evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe("rgb(24, 24, 31)");
    }
    await expect(page.locator("#promptHistoryReportContent")).toHaveCSS(
      "background-color",
      await page.locator(".report-view-modal__panel").evaluate((element) => getComputedStyle(element).backgroundColor),
    );
    await expect(page.locator("#promptHistoryReportDownload")).toHaveText("⇩");
    expect(await page.locator("#promptHistoryReportDownload").evaluate((element) => getComputedStyle(element, "::before").content)).toContain("↓");
  });

  test("keeps report-modal actions and markdown code light in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModal").showModal();
      document.querySelector("#promptHistoryReportDownload").hidden = false;
      document.querySelector("#promptHistoryReportCopy").hidden = false;
      document.querySelector("#promptHistoryReportContent").innerHTML =
        "<p><code>inline code</code></p><pre>code block</pre>";
    });

    await expect(page.locator("#promptHistoryReportDownload")).toHaveCSS("background-color", "rgb(255, 248, 239)");
    await expect(page.locator("#promptHistoryReportDownload")).toHaveCSS("color", "rgb(100, 58, 19)");
    await expect(page.locator("#promptHistoryReportCopy")).toHaveCSS("background-color", "rgb(255, 247, 255)");
    await expect(page.locator("#promptHistoryReportCopy")).toHaveCSS("color", "rgb(104, 73, 138)");
    for (const selector of ["#promptHistoryReportContent code", "#promptHistoryReportContent pre"])
      await expect(page.locator(selector)).toHaveCSS("background-color", "rgb(238, 244, 251)");
  });

  test("uses light glyphs for dark prompt-history report actions", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.mouse.move(0, 0);
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModal").showModal();
      document.querySelector("#promptHistoryReportCopy").hidden = false;
      document.querySelector("#promptHistoryReportDownload").hidden = false;
    });
    const download = page.locator("#promptHistoryReportDownload");

    await expect(download).toHaveCSS("color", "rgb(255, 240, 220)");
    await expect(page.locator("#promptHistoryReportCopy")).toHaveCSS("color", "rgb(234, 220, 255)");
  });

  test("renders log actions in the light category style", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.getByTestId("theme-toggle").click();
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => renderLogPagination("inbox", 1, 1));

    await expect(page.getByTestId("clear-inbox-log")).toHaveText("⌧");
    await expect(page.getByTestId("clear-inbox-log")).toHaveCSS("background-color", "rgb(255, 241, 244)");
    await expect(page.getByTestId("download-inbox-log")).toHaveCSS("background-color", "rgb(255, 248, 239)");
    await expect(page.locator("#inboxLogPagination button").first()).toHaveCSS("background-color", "rgb(255, 243, 226)");
  });

  test("keeps component-log download and destructive clear hover treatments distinct", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => renderLogPagination("inbox", 1, 1));

    for (const action of [
      page.getByTestId("download-inbox-log"),
      page.getByTestId("download-dashboard-log"),
    ]) {
      await action.hover();
      await expect(action).toHaveCSS("background-color", "rgb(240, 182, 106)");
      await expect(action).toHaveCSS("color", "rgb(32, 24, 18)");
    }
    for (const action of [page.getByTestId("clear-inbox-log"), page.getByTestId("clear-dashboard-log")]) {
      await action.hover();
      await expect(action).toHaveCSS("background-color", "rgb(255, 113, 143)");
      await expect(action).toHaveCSS("color", "rgb(35, 19, 26)");
    }
  });

  test("fills the historical report action blue on hover", async ({ page }) => {
    // This verifies desktop pointer hover styling.  Keep it outside the
    // compact phone title-bar geometry covered by the dedicated mobile tests.
    await page.setViewportSize({ width: 1280, height: 900 });
    // Keep the fixture stable during the hover assertion. A live dashboard
    // event can legitimately rebuild the prompt-history row mid-hover.
    await page.addInitScript(() => {
      localStorage.setItem(
        "engineering-dashboard-client-state-v1",
        JSON.stringify({ autoRefresh: false }),
      );
    });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [{
      run_id: "report-hover",
      status: "COMPLETE",
      title: "Rapport hover",
      executed_at: "2026-08-02T10:00:00+00:00",
      git_commit: "abc1234",
      report_available: true,
    }] } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    const report = page.locator('[title="Bekijk engineeringrapport voor Rapport hover"]');

    await scrollDashboardElementIntoView(report);
    await report.hover({ force: true });
    await expect(report).toHaveCSS("background-color", "rgb(141, 199, 255)");
    await expect(report).toHaveCSS("color", "rgb(23, 35, 49)");
  });

  test("downloads each redacted component log", async ({ page }) => {
    await page.route("**/api/logs/**", (route) => route.fulfill({ contentType: "application/x-ndjson", body: '{"level":"INFO","event":"test"}\n' }));
    await page.route("**/api/audit/user-action", (route) => route.fulfill({ contentType: "application/json", body: '{"logged":true}' }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await waitForDashboardReady(page);
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });

    await page.evaluate(() => {
      URL.createObjectURL = () => "blob:component-log";
      HTMLAnchorElement.prototype.click = function click() { window.__componentLogDownload = this.download; };
    });
    for (const [testId, filename] of [["download-inbox-log", "inbox-watcher-log-"], ["download-dashboard-log", "statusdashboard-log-"]]) {
      await dispatchDashboardPointerClick(page.getByTestId(testId));
      await expect.poll(() => page.evaluate(() => window.__componentLogDownload)).toMatch(new RegExp(`^${filename}.*\\.ndjson$`));
    }
  });

  test("copies only the visible filtered component-log entries", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      window.__copiedVisibleLog = "";
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: (value) => {
            window.__copiedVisibleLog = value;
            return Promise.resolve();
          },
        },
      });
      componentLogEntries.inbox = [
        { line: 1, timestamp: "2026-08-07T10:00:00Z", level: "INFO", event: "retain_me", runId: "visible-run", details: "visible detail" },
        { line: 2, timestamp: "2026-08-07T10:01:00Z", level: "ERROR", event: "exclude_me", runId: "hidden-run", details: "hidden detail" },
      ];
      componentLogServerPaged = false;
      document.querySelector("#logFilter").value = "retain_me";
      independentLogPageStates.inbox = 1;
      renderComponentLogs();
    });

    await expect(page.locator("#inboxComponentLog tr")).toHaveCount(1);
    await dispatchDashboardPointerClick(page.getByTestId("copy-inbox-visible-log"));
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).toContain("retain_me");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).toContain("visible-run");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).not.toContain("exclude_me");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).not.toContain("hidden-run");
  });

  test("selects multiple component-log rows and copies the selected rows", async ({ page }) => {
    // This verifies desktop pointer hover and modifier selection.  Responsive
    // touch selection has dedicated mobile coverage elsewhere.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/logs/inbox", (route) => route.fulfill({ body: "" }));
    await page.route("**/api/logs/dashboard", (route) => route.fulfill({ body: "" }));
    // The dashboard loads persisted logs asynchronously.  Wait until that
    // initial projection is settled before installing this test's fixture;
    // otherwise it can replace the fixture halfway through the selection.
    const initialLogsLoaded = Promise.all([
      page.waitForResponse("**/api/logs/inbox?*"),
      page.waitForResponse("**/api/logs/dashboard?*"),
    ]);
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await initialLogsLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      componentLogEntries.inbox = [
        { line: 1, timestamp: "2026-08-07T10:00:00Z", level: "INFO", event: "first_event", runId: "first-run", details: "first detail" },
        { line: 2, timestamp: "2026-08-07T10:01:00Z", level: "ERROR", event: "second_event", runId: "second-run", details: "second detail" },
        { line: 3, timestamp: "2026-08-07T10:02:00Z", level: "WARNING", event: "third_event", runId: "third-run", details: "third detail" },
      ];
      componentLogEntries.dashboard = [];
      componentLogServerPaged = false;
      renderComponentLogs();
    });

    const rows = page.locator("#inboxComponentLog tr");
    await expect(rows).toHaveCount(3);
    const divider = await rows.nth(0).locator("td").first().evaluate((cell) => getComputedStyle(cell).borderBottomColor);
    expect(divider).not.toBe("rgb(61, 54, 81)");
    expect(divider).not.toBe("rgb(212, 222, 235)");
    await rows.nth(1).hover();
    const hoverRowSurface = await rows.nth(1).locator("td").evaluateAll((cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor));
    expect(new Set(hoverRowSurface).size).toBe(1);
    expect(hoverRowSurface[0]).not.toBe("rgba(0, 0, 0, 0)");
    await rows.nth(0).click();
    await rows.nth(2).click({ modifiers: ["Meta"] });
    await expect(rows.nth(0)).toHaveAttribute("aria-selected", "true");
    await expect(rows.nth(1)).toHaveAttribute("aria-selected", "false");
    await expect(rows.nth(2)).toHaveAttribute("aria-selected", "true");
    const selectedRowSurface = await rows.nth(0).evaluate((row) => {
      const cells = Array.from(row.cells);
      return {
        firstCellBackground: getComputedStyle(cells[0]).backgroundColor,
        firstCellShadow: getComputedStyle(cells[0]).boxShadow,
        rowOutline: getComputedStyle(row).outlineStyle,
        rowShadow: getComputedStyle(row).boxShadow,
        otherCellOutlines: cells.slice(1).map((cell) => getComputedStyle(cell).outlineStyle),
        otherCellShadows: cells.slice(1).map((cell) => getComputedStyle(cell).boxShadow),
      };
    });
    expect(selectedRowSurface.firstCellBackground).not.toBe("rgba(0, 0, 0, 0)");
    expect(selectedRowSurface.firstCellShadow).toContain("2px 0px 0px 0px inset");
    expect(selectedRowSurface.rowOutline).toBe("none");
    expect(selectedRowSurface.rowShadow).toBe("none");
    expect(selectedRowSurface.otherCellOutlines.every((outline) => outline === "none")).toBe(true);
    expect(selectedRowSurface.otherCellShadows.every((shadow) => shadow === "none")).toBe(true);
    await rows.nth(0).focus();
    await expect(rows.nth(0)).toHaveCSS("outline-style", "none");
    await expect(rows.nth(0)).toHaveCSS("box-shadow", "none");

    const copied = await page.evaluate(() => {
      const data = new DataTransfer();
      document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true, cancelable: true, clipboardData: data }));
      return data.getData("text/plain");
    });
    expect(copied).toContain("first_event");
    expect(copied).toContain("third_event");
    expect(copied).not.toContain("second_event");

    await rows.nth(0).click({ modifiers: ["Shift"] });
    await expect(rows.nth(1)).toHaveAttribute("aria-selected", "true");

    await page.locator("#logFilter").fill("first_event");
    expect(await page.evaluate(() => {
      const data = new DataTransfer();
      document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true, cancelable: true, clipboardData: data }));
      return data.getData("text/plain");
    })).toBe("");

    await page.locator("#inboxComponentLog tr").first().click();
    await page.locator(".reset-log-filters").click();
    expect(await page.evaluate(() => {
      const data = new DataTransfer();
      document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true, cancelable: true, clipboardData: data }));
      return data.getData("text/plain");
    })).toBe("");

    await page.evaluate(() => {
      document.querySelector("#logFilter").value = "";
      componentLogEntries.inbox = Array.from({ length: 51 }, (_, index) => ({
        line: index + 1,
        timestamp: `2026-08-07T10:${String(index).padStart(2, "0")}:00Z`,
        level: "INFO",
        event: `page_event_${index + 1}`,
        runId: `page-run-${index + 1}`,
        details: "page detail",
      }));
      document.querySelector("#logFilter").dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.locator("#inboxComponentLog tr").first().click();
    await page.locator("#inboxLogPagination button").last().click();
    expect(await page.evaluate(() => {
      const data = new DataTransfer();
      document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true, cancelable: true, clipboardData: data }));
      return data.getData("text/plain");
    })).toBe("");
  });

  test("uses the shared single-line circular border for download glyphs", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    for (const button of [
      page.getByTestId("download-inbox-log"),
      page.getByTestId("download-dashboard-log"),
    ]) {
      await expect(button).toHaveCSS("border-top-width", "1px");
      await expect(button).toHaveCSS("border-top-style", "solid");
      await expect(button).toHaveCSS("border-top-left-radius", "50%");
    }
  });

  test("uses shared semantic classes for download, copy and destructive actions", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const selector of ["#downloadChat", "#promptHistoryReportDownload", "#componentLogs .component-log-download", "#workspaceDatabaseDownload"]) {
      const action = page.locator(selector).first();
      await expect(action).toHaveClass(/dashboard-action--download/);
      await expect(action).toHaveCSS("font-size", "0px");
      await expect(action.evaluate((element) => getComputedStyle(element, "::before").content)).resolves.toBe('"↓"');
    }
    for (const selector of ["#copyChat", "#promptHistoryReportCopy", "#componentLogs .component-log-copy"]) {
      await expect(page.locator(selector).first()).toHaveClass(/dashboard-action--copy/);
    }
    for (const selector of ["#clearChat", "#componentLogs .clear-component-log"]) {
      await expect(page.locator(selector).first()).toHaveClass(/dashboard-action--destructive/);
    }
    expect(await page.locator("#componentLogs .log-card-actions").evaluateAll((actions) => actions.map(
      (action) => Array.from(action.children).map((button) => {
        if (button.classList.contains("component-log-download")) return "download";
        if (button.classList.contains("component-log-copy")) return "copy";
        return "clear";
      }),
    ))).toEqual([["download", "copy", "clear"], ["download", "copy", "clear"]]);
  });

  test("uses the generic orange download glyph in the prompt-scoped chat", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#promptHistoryChatModal").evaluate((element) => element.showModal());
    const download = page.locator("#downloadChat");
    await page.addStyleTag({ content: "#downloadChat[hidden]{display:flex!important}" });

    await expect(download).toHaveCSS("background-color", "rgb(59, 40, 27)");
    await expect(download).toHaveCSS("border-color", "rgb(240, 182, 106)");
    await download.hover();
    await expect(download).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(download).toHaveCSS("color", "rgb(32, 24, 18)");
  });

  test("fills the prompt-scoped AI question send action with its purple category on hover", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#promptHistoryChatModal").evaluate((element) => element.showModal());
    const send = page.locator("#chatSend");

    await send.hover();
    await expect(send).toHaveCSS("background-color", "rgb(208, 164, 255)");
    await expect(send).toHaveCSS("color", "rgb(23, 21, 26)");
    await send.evaluate((button) => { button.disabled = true; });
    await send.hover();
    await expect(send).toHaveCSS("background-color", "rgb(208, 164, 255)");
    await expect(send).toHaveCSS("color", "rgb(23, 21, 26)");
  });

  test("uses a destructive red surface for the prompt-scoped chat clear glyph", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.evaluate(() => {
      document.querySelector("#promptHistoryChatModal").showModal();
      document.querySelector("#clearChat").hidden = false;
    });
    const clear = page.locator("#clearChat");

    await expect(clear).toBeVisible();
    await expect(clear).toHaveCSS("background-color", "rgb(255, 241, 244)");
    await expect(clear).toHaveCSS("color", "rgb(179, 38, 73)");
    await clear.hover();
    await expect(clear).toHaveCSS("background-color", "rgb(255, 113, 143)");
    await expect(clear).toHaveCSS("color", "rgb(35, 19, 26)");
    await page.evaluate(() => {
      chatMessage("user", "Eigen bericht");
      chatMessage("assistant", "AI-antwoord");
    });
    await expect(page.locator(".chat-message--user .chat-message__copy")).toHaveCSS("color", "rgb(28, 78, 104)");
    await expect(page.locator(".chat-message--assistant .chat-message__copy")).toHaveCSS("color", "rgb(104, 73, 138)");
  });

  test("uses a destructive red surface for the component log clear glyph", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((details) => { details.open = true; });
    const clear = page.locator("#componentLogs .clear-component-log").first();
    await expect(clear).toHaveCSS("background-color", "rgb(58, 32, 40)");
    await expect(clear).toHaveCSS("border-color", "rgb(255, 113, 143)");
    await clear.hover();
    await expect(clear).toHaveCSS("background-color", "rgb(255, 113, 143)");
    await page.getByTestId("theme-toggle").click();
    await expect(clear).toHaveCSS("background-color", "rgb(255, 241, 244)");
    await expect(clear).toHaveCSS("color", "rgb(179, 38, 73)");
  });

  test("uses matching orange iOS-style toggles in the title bar", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const theme = page.getByTestId("theme-toggle");
    const allSections = page.getByTestId("toggle-all-sections");
    const autoRefresh = page.locator("#autoRefresh");

    await expect(autoRefresh).toHaveAttribute("role", "switch");
    await expect(autoRefresh).toHaveCSS("background-color", "rgb(240, 182, 106)");
    expect(await allSections.evaluate((element) => getComputedStyle(element, "::before").backgroundColor)).toBe("rgb(74, 74, 85)");
    expect(await allSections.evaluate((element) => getComputedStyle(element, "::after").backgroundColor)).toBe("rgb(247, 243, 238)");
    await theme.click();
    await allSections.click();
    await page.waitForTimeout(250);
    for (const toggle of [theme, allSections]) {
      await expect(toggle).toHaveAttribute("aria-checked", "true");
      expect(await toggle.evaluate((element) => getComputedStyle(element, "::before").backgroundColor)).toBe("rgb(240, 182, 106)");
    }
  });

  test("keeps title-bar switch housings free from shared mobile glass styling", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).not.toContain("backdrop-filter:blur(12px)");
    expect(styles).not.toContain("background-image:linear-gradient");
  });

  test("keeps iPhone title-bar option rows and locale picker free of card shadows", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("html").evaluate((element) => { element.dataset.theme = "light"; });
    await openTitlebarOptions(page);

    const styles = await page.evaluate(() => ({
      rows: [...document.querySelectorAll(".dashboard-titlebar__options-content > .dashboard-locale, .dashboard-titlebar__options-content > .theme-toggle, .dashboard-titlebar__options-content > .section-state-toggle, .dashboard-titlebar__options-content > .auto-refresh-toggle")]
        .map((element) => getComputedStyle(element).boxShadow),
      localePicker: getComputedStyle(document.querySelector("#dashboardLocaleButton")).boxShadow,
      themeThumb: getComputedStyle(document.querySelector("#themeToggle"), "::after").boxShadow,
      refreshThumb: getComputedStyle(document.querySelector("#autoRefresh"), "::after").boxShadow,
    }));

    expect(styles.rows).toEqual(["none", "none", "none", "none"]);
    expect(styles.localePicker).toBe("none");
    expect(styles.themeThumb).not.toBe("none");
    expect(styles.refreshThumb).not.toBe("none");
  });

  test("keeps the platform version labels orange in both themes", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const platformLabel = page.locator(".footer .label").first();
    await expect(platformLabel).toHaveCSS("color", "rgb(240, 182, 106)");
    await page.getByTestId("theme-toggle").click();
    await expect(platformLabel).toHaveCSS("color", "rgb(240, 182, 106)");
  });

  test("uses the centralized house style for toast and shared controls", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const houseStyle = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--house-style").trim());
    expect(houseStyle).toBe("#f0b66a");

    await page.evaluate(() => showCopyToast());
    await expect(page.getByTestId("copy-toast")).toHaveCSS("border-color", "rgb(240, 182, 106)");
    await expect(page.locator("#autoRefresh")).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(page.getByTestId("engineering-rate-limit-reset")).toHaveCSS("border-color", "rgb(81, 216, 138)");
  });

  test("keeps the copy toast above an open report modal", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModal").showModal();
      showCopyToast();
    });
    await expect(page.getByTestId("copy-toast")).toHaveClass(/copy-toast--visible/);
    await expect(page.getByTestId("copy-toast")).toBeVisible();
    expect(await page.getByTestId("copy-toast").evaluate(
      (toast) => toast.matches(":popover-open"),
    )).toBeTruthy();
    const box = await page.getByTestId("copy-toast").boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(844);
  });

  test("uses the house-orange focus contract and a light mobile options surface", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const styles = await page.evaluate(() => {
      const root = document.documentElement;
      root.dataset.theme = "light";
      const summary = document.querySelector("#dashboardTitlebarOptionsToggle");
      const optionsContent = document.querySelector("#dashboardTitlebarOptionsContent");
      summary.setAttribute("aria-expanded", "true");
      optionsContent.hidden = false;
      const input = document.querySelector("#autoRefresh");
      const refresh = document.querySelector("#pageRefresh");
      input.focus({ preventScroll: true });
      const inputStyle = getComputedStyle(input);
      const inputBorder = inputStyle.borderColor;
      const inputOutline = inputStyle.outlineStyle;
      const inputOutlineWidth = inputStyle.outlineWidth;
      const inputShadow = inputStyle.boxShadow;
      refresh.focus({ preventScroll: true });
      const refreshStyle = getComputedStyle(refresh);
      return {
        inputBorder,
        inputOutline,
        inputOutlineWidth,
        inputShadow,
        refreshBorder: refreshStyle.borderColor,
        refreshOutline: refreshStyle.outlineStyle,
        refreshShadow: refreshStyle.boxShadow,
        summaryBackground: getComputedStyle(summary).backgroundColor,
        summaryColor: getComputedStyle(summary).color,
      };
    });

    expect(styles.inputBorder).toBe("rgb(240, 182, 106)");
    expect(styles.inputOutline).toBe("solid");
    expect(styles.inputOutlineWidth).toBe("1px");
    expect(styles.inputShadow).toBe("none");
    expect(styles.refreshBorder).toBe("rgb(240, 182, 106)");
    expect(styles.refreshOutline).toBe("none");
    expect(styles.refreshShadow).toBe("none");
    expect(styles.summaryBackground).not.toBe("rgb(17, 19, 29)");
    expect(styles.summaryColor).toBe("rgb(24, 34, 48)");
  });

  test("uses house-style orange for every interactive focus family", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const focusColours = await page.evaluate(() => {
      document.querySelector("#componentLogs").open = true;
      const mergeModal = document.querySelector("#operatorMergeWaitModal");
      const mergeLink = document.querySelector("#operatorMergeWaitModalPullRequest");
      const chatModal = document.querySelector("#promptHistoryChatModal");
      const chatInput = document.querySelector("#chatInput");
      mergeLink.href = "https://github.com/pcvantol/djconnect/pull/840";
      const targets = [
        document.querySelector("#autoRefresh"),
        document.querySelector("#pageRefresh"),
        document.querySelector("#logFilter"),
        document.querySelector("#componentLogs .log-table th.log-sortable"),
      ];
      const colours = targets.map((target) => {
        target.focus({ preventScroll: true });
        return getComputedStyle(target).borderTopColor;
      });
      chatModal.showModal();
      chatInput.focus({ preventScroll: true });
      colours.push(getComputedStyle(chatInput).borderTopColor);
      chatModal.close();
      mergeModal.showModal();
      mergeLink.focus({ preventScroll: true });
      colours.push(getComputedStyle(mergeLink).borderTopColor);
      mergeModal.close();
      return colours;
    });
    expect(focusColours).toEqual([
      "rgb(240, 182, 106)",
      "rgb(240, 182, 106)",
      "rgb(240, 182, 106)",
      "rgb(255, 213, 155)",
      "rgb(240, 182, 106)",
      "rgb(240, 182, 106)",
    ]);
  });

  test("uses one selected border for a selectable host component", () => {
    const stylesheet = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8");
    expect(stylesheet).toContain(".platform-health__component:is(:hover,:focus,:focus-visible){\n  border:1px solid var(--house-style)!important;\n  box-shadow:none!important;\n  outline:0!important;");
  });

  test("uses normal field ink for native log date values", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#componentLogControls").evaluate((element) => { element.hidden = false; });
    await page.locator("#logTimePreset").selectOption("range");
    await page.locator("#logDateFrom").fill("2026-08-27T12:30");

    await expect(page.locator("#logSpecificDate")).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(page.locator("#logDateFrom")).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(page.locator("#logDateTo")).toHaveCSS("color", "rgb(247, 243, 238)");
    expect(await page.locator("#logDateFrom").evaluate((element) =>
      getComputedStyle(element, "::-webkit-datetime-edit-fields-wrapper").color,
    )).toBe("rgb(247, 243, 238)");
  });

  test("uses neutral ink for ordinary Markdown emphasis", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      const content = document.createElement("div"), emphasis = document.createElement("strong");
      content.id = "markdown-emphasis-test";
      content.className = "markdown-document";
      emphasis.textContent = "nadruk";
      content.append(emphasis);
      document.body.append(content);
    });
    const emphasis = page.locator("#markdown-emphasis-test strong");
    await expect(emphasis).toHaveCSS("color", "rgb(247, 243, 238)");

    await page.getByTestId("theme-toggle").click();
    await expect(emphasis).toHaveCSS("color", "rgb(24, 34, 48)");
  });

  test("shows only the date controls required by the selected log time window on desktop", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#componentLogControls").evaluate((element) => { element.hidden = false; });

    await page.locator("#logTimePreset").selectOption("day");
    await expect(page.locator("#logSpecificDateControl")).toBeVisible();
    await expect(page.locator("#logDateFromControl")).toBeHidden();
    await expect(page.locator("#logDateToControl")).toBeHidden();

    await page.locator("#logTimePreset").selectOption("range");
    await expect(page.locator("#logSpecificDateControl")).toBeHidden();
    await expect(page.locator("#logDateFromControl")).toBeVisible();
    await expect(page.locator("#logDateToControl")).toBeVisible();
  });

  test("keeps title-bar switch focus on the compact track", () => {
    const stylesheet = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8");
    expect(stylesheet).toContain(".execution-lifecycle__node,.theme-toggle,.section-state-toggle,.auto-refresh-toggle");
    expect(stylesheet).toContain("Unified focus contract: one product-coloured, one-pixel edge.");
    expect(stylesheet).toContain("border-color:var(--house-style)!important;");
    expect(stylesheet).toContain("outline:1px solid var(--house-style)!important;");
  });

  test("uses a dark locale picker surface in dark mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const styles = await page.evaluate(() => {
      document.documentElement.dataset.theme = "dark";
      const button = document.querySelector("#dashboardLocaleButton");
      const menu = document.querySelector("#dashboardLocaleMenu");
      menu.hidden = false;
      return {
        buttonBackground: getComputedStyle(button).backgroundColor,
        buttonColor: getComputedStyle(button).color,
        menuBackground: getComputedStyle(menu).backgroundColor,
        optionBackground: getComputedStyle(menu.querySelector("button")).backgroundColor,
        optionColor: getComputedStyle(menu.querySelector("button")).color,
      };
    });

    expect(styles.buttonBackground).toBe("rgb(37, 37, 48)");
    expect(styles.buttonColor).toBe("rgb(247, 243, 238)");
    expect(styles.menuBackground).toBe("rgb(37, 37, 48)");
    expect(styles.optionBackground).toBe("rgb(37, 37, 48)");
    expect(styles.optionColor).toBe("rgb(247, 243, 238)");
  });

  test("keeps category summaries out of the selected-input focus treatment", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const styles = await page.locator("#currentRun > summary").evaluate((summary) => {
      summary.focus({ preventScroll: true });
      const style = getComputedStyle(summary);
      return { outlineStyle: style.outlineStyle, shadow: style.boxShadow };
    });

    expect(styles.outlineStyle).toBe("none");
    expect(styles.shadow).toBe("none");
  });

  test("keeps the sticky title bar square while padding content evenly", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const padding = await page.locator(".dashboard-titlebar").evaluate((element) => {
      const style = getComputedStyle(element);
      return [style.paddingLeft, style.paddingRight, style.borderTopLeftRadius];
    });
    expect(padding).toEqual(["16px", "16px", "0px"]);
  });

  test("never renders a white focus ring on visible interactive elements", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      document.querySelectorAll("details").forEach((element) => { element.open = true; });
    });

    const violations = await page.evaluate(() => {
      const selector = [
        "button:not([disabled])",
        "input:not([disabled])",
        "select:not([disabled])",
        "textarea:not([disabled])",
        "summary",
        '[role="button"]:not([aria-disabled="true"])',
        "[tabindex]:not([tabindex='-1'])",
      ].join(",");
      const isVisible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
      };
      const hasWhite = (value) => /rgb\(\s*(?:2[4-5]\d|255)\s*,\s*(?:2[4-5]\d|255)\s*,\s*(?:2[4-5]\d|255)\s*\)/.test(value);

      return ["dark", "light"].flatMap((theme) => {
        document.documentElement.dataset.theme = theme;
        return [...document.querySelectorAll(selector)].filter(isVisible).flatMap((element) => {
          element.focus({ preventScroll: true });
          const style = getComputedStyle(element);
          const focusStyles = `${style.outlineColor} ${style.boxShadow}`;
          return hasWhite(focusStyles) ? [{
            theme,
            element: element.id || element.getAttribute("data-testid") || element.tagName,
            focusStyles,
          }] : [];
        });
      });
    });

    expect(violations).toEqual([]);
  });

  test("keeps the skip-link dashboard target focusable without outlining all categories", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const content = page.locator("#engineering-dashboard-content");

    await content.focus();
    await expect(content).toBeFocused();
    await expect(content).toHaveCSS("outline-style", "none");
    await expect(content).toHaveCSS("box-shadow", "none");
    await expect(content).toHaveCSS("border-top-width", "0px");
  });

  test("does not initially focus a report-modal shell", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryReportModal");

    await modal.evaluate((element) => element.showModal());
    await expect(modal).not.toBeFocused();
    await expect(modal).toHaveCSS("outline-style", "none");
    await expect(modal).toHaveCSS("box-shadow", "none");
  });

  test("does not initially focus a prompt-detail shell", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal");

    await modal.evaluate((element) => element.showModal());
    await expect(modal).not.toBeFocused();
    await expect(modal).toHaveCSS("outline-style", "none");
    await expect(modal).toHaveCSS("box-shadow", "none");
  });

  test("does not initially focus a prompt-chat shell", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");

    await modal.evaluate((element) => element.showModal());
    await expect(modal).not.toBeFocused();
    await expect(modal).toHaveCSS("outline-style", "none");
    await expect(modal).toHaveCSS("box-shadow", "none");
  });

  test("keeps the prompt-detail close action at the top right while scrolling", async ({ page }) => {
    await page.setViewportSize({ width: 720, height: 360 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal"),
      panel = modal.locator(".prompt-detail-modal__panel"),
      content = page.locator("#promptHistoryDetailContent"),
      header = modal.locator(".prompt-detail-modal__header"),
      close = page.locator("#promptHistoryDetailClose");

    await modal.evaluate((element) => {
      const content = document.querySelector("#promptHistoryDetailContent");
      content.innerHTML =
        "<p>Detailregel</p>".repeat(120);
      content.style.setProperty("height", "180px", "important");
      element.showModal();
    });
    const before = {
      close: await close.boundingBox(),
      header: await header.boundingBox(),
    };
    await expect.poll(() => content.evaluate(
      (element) => element.scrollHeight >= element.clientHeight + 180,
    )).toBe(true);
    const scrollTop = await content.evaluate((element) => {
      element.scrollTo(0, 180);
      return element.scrollTop;
    });
    expect(scrollTop).toBeGreaterThan(0);
    const after = {
      close: await close.boundingBox(),
      header: await header.boundingBox(),
    }, panelBox = await panel.boundingBox();

    expect(after.close.y).toBe(before.close.y);
    expect(after.header.y).toBe(before.header.y);
    expect(after.close.x + after.close.width).toBeGreaterThan(panelBox.x + panelBox.width - 48);
    await close.click();
    await expect(modal).not.toBeVisible();
  });

  test("keeps report-modal action colours semantically distinct in dark mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      document.querySelector("#promptHistoryReportModal").showModal();
      document.querySelector("#promptHistoryReportDownload").hidden = false;
      document.querySelector("#promptHistoryReportCopy").hidden = false;
    });
    const download = page.locator("#promptHistoryReportDownload");

    await download.hover();
    await expect(download).toHaveCSS("color", "rgb(32, 24, 18)");
    await expect(page.locator("#promptHistoryReportCopy")).toHaveCSS("color", "rgb(234, 220, 255)");
    await expect(page.locator("#promptHistoryReportClose")).toHaveCSS("color", "rgb(247, 243, 238)");
    await expect(page.locator(".report-view-modal__header")).toHaveCSS("border-bottom-color", "rgb(141, 199, 255)");
    await expect(page.locator("#promptHistoryReportClose")).toHaveCSS("font-size", "18px");
  });

  test("keeps a long report modal inside a short viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 300 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryReportModal");

    await modal.evaluate((element) => {
      document.querySelector("#promptHistoryReportContent").textContent = "Rapportregel\n".repeat(80);
      element.showModal();
    });
    const box = await modal.boundingBox();
    const panelBox = await modal.locator(".report-view-modal__panel").boundingBox();
    const actions = modal.locator(".report-view-modal__actions");
    const actionBoxBeforeScroll = await actions.boundingBox();
    const header = modal.locator(".report-view-modal__header");
    const content = modal.locator("#promptHistoryReportContent");
    const headerBox = await header.boundingBox();
    const contentBox = await content.boundingBox();
    await content.evaluate((element) => { element.scrollTop = 160; });
    const actionBoxAfterScroll = await actions.boundingBox();

    expect(box.y).toBe(0);
    expect(box.height).toBe(300);
    expect(panelBox.y).toBeGreaterThanOrEqual(18);
    expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(282);
    expect(actionBoxBeforeScroll.x + actionBoxBeforeScroll.width).toBeGreaterThan(panelBox.x + panelBox.width - 44);
    expect(contentBox.y).toBeGreaterThanOrEqual(headerBox.y + headerBox.height);
    await expect.poll(() => content.evaluate((element) => element.scrollTop)).toBe(160);
    expect(actionBoxAfterScroll.y).toBe(actionBoxBeforeScroll.y);
  });

  test("marks every dashboard element with the active light or dark theme", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    for (const theme of ["dark", "light"]) {
      await page.evaluate((activeTheme) => applyDashboardTheme(activeTheme), theme);
      const violations = await page.evaluate((activeTheme) => [...document.querySelectorAll("body, body *")]
        .filter((element) => !["SCRIPT", "STYLE"].includes(element.tagName))
        .filter((element) => element.dataset.themeMode !== activeTheme)
        .map((element) => ({
          element: element.id || element.getAttribute("data-testid") || element.tagName,
          themeMode: element.dataset.themeMode || null,
        })), theme);
      expect(violations).toEqual([]);
    }
  });

  test("shows the private dashboard and keeps completed work collapsed by default", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    // This test intentionally mutates projected state below.  Freeze the
    // client-side projection first so a legitimate SSE update cannot replace
    // that deterministic fixture midway through the assertions.
    await page.locator("#autoRefresh").uncheck();
    await expect(page.getByTestId("engineering-dashboard-title")).toHaveText("EP Operations");
    await expect(page.getByTestId("dashboard-splash")).toBeHidden();
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/assets/operations-console/manifest.webmanifest");
    await expect(page.locator('#dashboardFavicon')).toHaveAttribute("href", "/assets/operations-console/apple-touch-icon-dark.png?v=operations-console-2");
    await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute("href", "/assets/operations-console/apple-touch-icon-dark.png?v=operations-console-2");
    await expect(page.getByTestId("dashboard-app-icon")).toHaveAttribute("src", "/assets/operations-console/icon-transparent.png");
    await expect(page.getByTestId("engineering-workspace")).not.toHaveAttribute("open", "");
    expect(await page.getByTestId("engineering-workspace").evaluate((element) => element.parentElement.id)).toBe("engineering-dashboard-content");
    await expect(page.getByTestId("engineering-inbox-queue")).not.toHaveAttribute("open", "");
    await expect(page.getByTestId("platform-health")).not.toHaveAttribute("open", "");
    await expect(page.locator("#queueItems > summary .category-icon")).toHaveText("☷");
    await expect(page.locator("#workspaceCard > summary .category-icon")).toHaveText("⌂");
    await expect(page.locator("#rateLimits > summary .category-icon")).toHaveText("◔");
    await expect(page.locator("#technicalDetails > summary .category-icon")).toHaveText("⌘");
    await expect(page.locator("#componentLogs > summary .category-icon")).toHaveText("≡");
    await expect(page.locator("#configuration > summary .category-icon")).toHaveText("⚙︎");
    await expect(page.locator("#configuration > summary .category-description")).toHaveText("Huidige lokale instellingen en controle-intervallen van het Engineering Platform.");
    for (const selector of ["#workspaceCard > summary", "#queueItems > summary", "#rateLimits > summary", "#componentLogs > summary"]) {
      expect(await page.locator(selector).evaluate((summary) => getComputedStyle(summary, "::before").right)).toBe("0px");
    }
    const arrowGeometry = await page.locator("#queueItems").evaluate((category) => {
      const position = () => {
        const summary = category.querySelector("summary");
        const style = getComputedStyle(summary, "::before");
        return {
          arrowRight: summary.getBoundingClientRect().right - parseFloat(style.right),
          arrowTop: summary.getBoundingClientRect().top + parseFloat(style.top),
          width: style.width,
        };
      };
      const closed = position();
      category.open = true;
      const opened = position();
      category.open = false;
      return { closed, opened };
    });
    expect(arrowGeometry.closed.width).toBe("24px");
    expect(arrowGeometry.opened.width).toBe("24px");
    expect(Math.abs(arrowGeometry.closed.arrowRight - arrowGeometry.opened.arrowRight)).toBeLessThanOrEqual(0.1);
    expect(Math.abs(arrowGeometry.closed.arrowTop - arrowGeometry.opened.arrowTop)).toBeLessThanOrEqual(0.1);
    await expect(page.locator("#currentRun > summary > .current-run__category-description")).toHaveCount(1);
    await expect(page.locator(".current-run__category-description")).toHaveText("Voortgang, doorlooptijd en context van de uitvoering die nu actief is.");
    expect(await page.locator("#indicator").evaluate((element) => ({
      parentClass: element.parentElement.className,
      previousSiblingId: element.previousElementSibling?.id,
    }))).toEqual({ parentClass: "field execution-identity__run-id", previousSiblingId: "runId" });
    await expect(page.locator("#loadComponentLogs")).toHaveCount(0);
    await expect(page.getByTestId("pull-refresh")).toHaveText("Trek omlaag om te vernieuwen");
    await page.evaluate(() => showCopyToast());
    await expect(page.getByTestId("copy-toast")).toHaveText("Gekopieerd naar klembord");
    await expect(page.getByTestId("copy-toast")).toHaveClass(/copy-toast--visible/);
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await expect(page.locator(".component-info").first()).toBeVisible();
    await dispatchDashboardPointerClick(page.locator(".component-info").first());
    await expect(page.locator("#componentModal")).toHaveAttribute("open", "");
    await expect(page.locator("#componentModalTitle")).not.toHaveText("Componentinformatie");
    await page.locator("#componentModalClose").click();
    await expect(page.locator("#componentModal")).not.toHaveAttribute("open", "");
    await page.evaluate(() => executionTelemetry([{ date: "2026-08-01", prompt_count: 1, average_execution_seconds: 10, average_total_execution_seconds: 12, average_queue_wait_seconds: 2, input_tokens: 100, output_tokens: 20, total_tokens: 120, complete_count: 1, blocked_count: 0, failed_count: 0 }]));
    await expect(page.locator("#executionTelemetryRows tr td").first()).toHaveText("01-08-2026");
    expect(await page.evaluate(() => [
      document.getElementById("technicalDetails").nextElementSibling.id,
      document.getElementById("workspaceCard").nextElementSibling.id,
      document.getElementById("executionTelemetry").nextElementSibling.id,
      document.getElementById("platformHealth").nextElementSibling.id,
    ])).toEqual(["workspaceCard", "executionTelemetry", "platformHealth", "componentLogs"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
    expect(await page.evaluate(() => {
      const mainCategory = document.getElementById("componentLogs");
      const nestedCard = mainCategory.querySelector(".card");
      const widths = (element) => {
        const style = getComputedStyle(element);
        return [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth];
      };
      return { main: widths(mainCategory), nested: widths(nestedCard) };
    })).toEqual({
      main: ["2px", "2px", "2px", "2px"],
      nested: ["1px", "1px", "1px", "1px"],
    });
    await expect(page.locator("#componentLogControls")).not.toHaveAttribute("hidden", "");
  });

  test("sorts the two component-log tables independently", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const tables = page.locator("#componentLogs .log-table");
    await expect(tables).toHaveCount(2);

    const inboxLevel = tables.nth(0).locator('th[data-sort-key="level"]');
    const dashboardLevel = tables.nth(1).locator('th[data-sort-key="level"]');
    const dashboardTimestamp = tables.nth(1).locator('th[data-sort-key="timestamp"]');

    await inboxLevel.click();
    await expect(inboxLevel).toHaveAttribute("aria-sort", "ascending");
    await expect(dashboardLevel).toHaveAttribute("aria-sort", "none");
    await expect(dashboardTimestamp).toHaveAttribute("aria-sort", "descending");

    await dashboardLevel.click();
    await expect(dashboardLevel).toHaveAttribute("aria-sort", "ascending");
    await expect(inboxLevel).toHaveAttribute("aria-sort", "ascending");
  });

  test("uses the remaining component-log table width for Details", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const detailsWidth = await page.locator("#componentLogs .log-table").first().evaluate((table) => {
      const headers = table.querySelectorAll("th");
      return headers[headers.length - 1].getBoundingClientRect().width;
    });
    expect(detailsWidth).toBeGreaterThan(300);
  });

  test("keeps each component-log table horizontally scrollable on iPhone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });

    const tables = page.locator("#componentLogs .log-table");
    await expect(tables).toHaveCount(2);
    for (const table of [tables.nth(0), tables.nth(1)]) {
      const geometry = await table.evaluate((element) => {
        const container = element.parentElement;
        container.scrollLeft = 80;
        return {
          overflowX: getComputedStyle(container).overflowX,
          scrollable: container.scrollWidth > container.clientWidth,
          scrollLeft: container.scrollLeft,
          tableWidth: element.getBoundingClientRect().width,
          cellWhiteSpace: getComputedStyle(element.querySelector("td")).whiteSpace,
        };
      });
      expect(geometry).toMatchObject({
        overflowX: "auto",
        scrollable: true,
        scrollLeft: 80,
        cellWhiteSpace: "nowrap",
      });
      expect(geometry.tableWidth).toBeGreaterThanOrEqual(780);
    }
  });

  test("paginates the two component-log tables independently", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.waitForFunction(() => componentLogsLoaded);
    await page.waitForTimeout(350);
    await page.evaluate(() => {
      refreshComponentLogs = async () => {};
      // This fixture exercises the resilient local fallback. Production uses
      // server pagination, so an API page is never sliced again by the client.
      componentLogServerPaged = false;
      componentLogEntries.inbox = Array.from({ length: 51 }, (_, index) => ({
        line: index + 1,
        timestamp: `2026-08-02T00:${String(index).padStart(2, "0")}:00Z`,
        level: "INFO",
        event: `inbox_${index}`,
        runId: "—",
        details: "test",
      }));
      componentLogEntries.dashboard = Array.from({ length: 2 }, (_, index) => ({
        line: index + 1,
        timestamp: `2026-08-02T01:0${index}:00Z`,
        level: "INFO",
        event: `dashboard_${index}`,
        runId: "—",
        details: "test",
      }));
      independentLogPageStates.inbox = 1;
      independentLogPageStates.dashboard = 1;
      componentLogsLoaded = true;
      renderComponentLogs();
    });
    await expect(page.locator("#inboxComponentLog tr")).toHaveCount(50);
    await expect(page.locator("#inboxLogPagination")).toContainText("Pagina 1 van 2 · 51 regels");
    await expect(page.locator("#dashboardLogPagination")).toContainText("Pagina 1 van 1 · 2 regels");
    const previousInboxLogPage = page.locator("#inboxLogPagination").getByRole("button", { name: "Vorige" });
    await previousInboxLogPage.hover();
    await expect(previousInboxLogPage).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(previousInboxLogPage).toHaveCSS("color", "rgb(16, 21, 29)");
    const nextInboxLogPage = page.locator("#inboxLogPagination").getByRole("button", { name: "Volgende" });
    await nextInboxLogPage.hover();
    await expect(nextInboxLogPage).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(nextInboxLogPage).toHaveCSS("color", "rgb(32, 24, 18)");
    await nextInboxLogPage.click();
    await expect(page.locator("#inboxComponentLog tr")).toHaveCount(1);
    await expect(page.locator("#inboxLogPagination")).toContainText("Pagina 2 van 2 · 51 regels");
    await expect(page.locator("#dashboardComponentLog tr")).toHaveCount(2);
  });

  test("keeps a requested server-backed component-log page instead of clamping it to its 50 rows", async ({ page }) => {
    const requests = [];
    const entriesFor = (component, pageNumber) => Array.from(
      { length: pageNumber === 2 && component === "inbox" ? 1 : 50 },
      (_, index) => ({
        line: (pageNumber - 1) * 50 + index + 1,
        timestamp: `2026-08-02T12:${String(index).padStart(2, "0")}:00Z`,
        level: "INFO",
        event: `${component}_server_${pageNumber}_${index}`,
        run_id: "—",
        details: "server page fixture",
      }),
    );
    for (const component of ["inbox", "dashboard"]) {
      await page.route(`**/api/logs/${component}?*`, async (route) => {
        const pageNumber = Number(new URL(route.request().url()).searchParams.get("page")) || 1;
        requests.push(`${component}:${pageNumber}`);
        await route.fulfill({ json: {
          entries: entriesFor(component, pageNumber),
          total: component === "inbox" ? 51 : 50,
          events: [`${component}_server_1_0`],
        } });
      });
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await expect(page.locator("#inboxLogPagination")).toContainText("Pagina 1 van 2 · 51 regels");
    const next = page.locator("#inboxLogPagination").getByRole("button", { name: "Volgende" });
    await expect(next).toBeEnabled();
    await next.click();
    await expect(page.locator("#inboxLogPagination")).toContainText("Pagina 2 van 2 · 51 regels");
    await expect(page.locator("#inboxComponentLog")).toContainText("Inbox Server 2 0");
    await expect(page.locator("#dashboardLogPagination")).toContainText("Pagina 1 van 1 · 50 regels");
    expect(requests).toContain("inbox:2");
    expect(requests).toContain("dashboard:1");
  });

  test("shows a searchable, sortable and paginated prompt history", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    let historyRuns = [];
    await page.route("**/api/prompt-history", async (route) => {
      await route.fulfill({ json: { runs: historyRuns } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    historyRuns = Array.from({ length: 26 }, (_, index) => ({
      run_id: `inbox-history-${index}`,
      status: index % 2 ? "COMPLETE" : "FAILED",
      title: `Geschiedenis prompt ${String(index).padStart(2, "0")}`,
      executed_at: `2026-08-02T12:${String(index).padStart(2, "0")}:00Z`,
      git_commit: index % 2 ? "abcdef1" : null,
      report_available: index % 2 === 1,
      analysis_available: index % 2 === 1,
    }));
    await page.evaluate(() => {
      const legacyCommitHeader = document.createElement("th");
      legacyCommitHeader.dataset.historySortKey = "git_commit";
      legacyCommitHeader.textContent = "Git-commit";
      document.querySelector("#promptHistory thead tr").insertBefore(
        legacyCommitHeader,
        document.querySelector("#promptHistory thead tr").children[3],
      );
      document.querySelector("#promptHistory").open = true;
    });
    await page.evaluate((fixture) => {
      promptHistoryEntries = fixture;
      renderPromptHistory();
    }, historyRuns);

    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(10);
    await expect(page.locator("#promptHistory th")).toHaveCount(9);
    await expect(page.locator('#promptHistory th[data-history-sort-key="git_commit"]')).toHaveCount(0);
    const firstPromptHistoryRow = page.locator("#promptHistoryRows .prompt-history-row").first();
    await firstPromptHistoryRow.hover();
    const promptHistoryHover = await firstPromptHistoryRow.locator("td").evaluateAll((cells) => cells.map((cell) => getComputedStyle(cell).backgroundColor));
    expect(new Set(promptHistoryHover).size).toBe(1);
    expect(promptHistoryHover[0]).not.toBe("rgba(0, 0, 0, 0)");
    await expect(page.locator("#promptHistoryRows tr").first().locator("td")).toHaveCount(9);
    await expect(page.locator("#promptHistoryPagination")).toContainText("Pagina 1 van 3 · 26 uitvoeringen");
    const nextPromptHistoryPage = page.locator("#promptHistoryPagination").getByRole("button", { name: "Volgende" });
    await nextPromptHistoryPage.hover();
    await expect(nextPromptHistoryPage).toHaveCSS("background-color", "rgb(255, 113, 143)");
    await expect(nextPromptHistoryPage).toHaveCSS("color", "rgb(39, 25, 35)");
    await page.locator('#promptHistory th[data-history-sort-key="title"]').click();
    await expect(page.locator('#promptHistory th[data-history-sort-key="title"]')).toHaveAttribute("aria-sort", "ascending");
    await page.locator("#promptHistoryFilter").fill("prompt 25");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    const reportView = page.locator("#promptHistoryRows .prompt-history-report");
    await expect(reportView).toHaveCount(1);
    await expect(reportView).toHaveText("▤");
    await expect(reportView).toHaveCSS("border-top-color", "rgb(141, 199, 255)");
    await page.route("**/api/prompt-history/**/report", (route) => route.fulfill({
      contentType: "text/markdown",
      body: "# Historisch rapport\n\nDit rapport wordt in een dialoog getoond.",
    }));
    await reportView.click();
    await expect(page.locator("#promptHistoryReportModal")).toBeVisible();
    await expect(page.locator("#promptHistoryReportModal")).not.toBeFocused();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    await expect(page.locator("#promptHistoryReportModalTitle"))
      .toHaveText(DASHBOARD_MESSAGES.nl["history.execution_report_title"]);
    await expect(page.locator("#promptHistoryReportContent")).toContainText("Historisch rapport");
    await expect(page.locator("#promptHistoryReportDownload")).toBeVisible();
    await expect(page.locator("#promptHistoryReportCopy")).toBeVisible();
    await page.locator("#promptHistoryReportClose").click();
    await expect(page.locator("#promptHistoryReportModal")).not.toBeVisible();
    await page.route("**/api/prompt-history/**/details", (route) => route.fulfill({
      json: {
        history: { run_id: "inbox-history-25", status: "COMPLETE", title: "Geschiedenis prompt 25", executed_at: "2026-08-02T12:25:00Z", execution_mode: "GENESIS", repository: "pcvantol/djconnect", target_repository: "pcvantol/forge", target_checkout_path: "/Users/example/Documents/GitHub/forge", tracked_file_count: 1655, target_branch: "forge-phase-evidence", execution_metadata: { modified: 3, created: 2, deleted: 1, codex_commands_executed: 17 } },
        execution: { seconds: 42, total_seconds: 61 },
        runtime: { runtime_provider: "codex_cli", codex_cli_version: "0.146.0" },
        usage: { input_tokens: 120, output_tokens: 45 },
        commits: { "Genesis-commit": "abcdef1" },
        evidence: ["Execution Host: Engineering Platform"],
        reviewers: [],
        lifecycle: {
          available: true,
          run_id: "inbox-history-25",
          terminal_state: "COMPLETE",
          steps: [{
            id: "REPAIR_AGENT",
            state: "COMPLETED",
            presentation_key: "lifecycle.step.autonomous_quality_repair",
            repair_evidence_key: "lifecycle.detail_autonomous_quality_repair_evidence",
            timing: {
              started_at: "2026-08-02T12:25:00Z",
              finished_at: "2026-08-02T12:25:12Z",
              spans: [{ phase: "REPAIR", duration_ms: 12000, outcome: "COMPLETED" }],
            },
            repair_audit: [{ iteration: "1", failed_checks: "Ruff", proposed_action: "Repair Ruff.", agent_summary: "Updated lint configuration.", commit_sha: "abcdef1", outcome: "submitted_for_recheck" }],
          }],
        },
      },
    }));
    await dispatchDashboardPointerClick(page.locator("#promptHistoryRows tr td").nth(1));
    await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeFocused();
    const runtimeCard = page.locator("#promptHistoryDetailContent .prompt-detail-card").filter({
      has: page.locator("h3", { hasText: DASHBOARD_MESSAGES.nl["detail.runtime"] }),
    });
    await expect(runtimeCard).toContainText(DASHBOARD_MESSAGES.nl["technical.runtime_value.codex_cli"]);
    await expect(runtimeCard).not.toContainText("codex_cli");
    await page.locator("#promptHistoryDetailContent .execution-lifecycle__node").click();
    const lifecycleDetail = page.locator("#lifecycleDetailModal");
    await expect(lifecycleDetail).toBeVisible();
    await expect(lifecycleDetail).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.detail_autonomous_quality_repair_evidence"]);
    await expect(lifecycleDetail).toContainText(DASHBOARD_MESSAGES.nl["lifecycle.detail_repair_iteration"].replace("{iteration}", "1"));
    await expect(lifecycleDetail).toContainText("Updated lint configuration.");
    await expect(page.locator("#promptHistoryDetailContent")).not.toContainText(DASHBOARD_MESSAGES.nl["detail.repair_history"]);
    await expect(lifecycleDetail.locator(".lifecycle-detail-modal__panel")).toHaveCSS("border-top-color", "rgb(141, 199, 255)");
    await expect(lifecycleDetail.locator("#lifecycleDetailTitle")).toHaveCSS("color", "rgb(141, 199, 255)");
    await expect(lifecycleDetail.locator("#lifecycleDetailTitle")).toHaveAttribute("data-lifecycle-status", "completed");
    expect(await lifecycleDetail.locator("#lifecycleDetailTitle").evaluate(
      (title) => getComputedStyle(title, "::before").content,
    )).toBe('"✓"');
    const inheritedDetailTokens = await lifecycleDetail.evaluate((element) => {
      const secondary = document.createElement("span");
      secondary.style.color = "var(--modal-secondary-accent)";
      const divider = document.createElement("span");
      divider.style.borderBottom = "1px solid color-mix(in srgb,var(--modal-accent) 32%,transparent)";
      element.append(secondary, divider);
      const result = {
        label: getComputedStyle(element.querySelector(".lifecycle-detail-modal__content .label")).color,
        secondary: getComputedStyle(secondary).color,
        phaseDivider: getComputedStyle(element.querySelector(".lifecycle-detail-modal__phase-list li")).borderBottomColor,
        divider: getComputedStyle(divider).borderBottomColor,
      };
      secondary.remove();
      divider.remove();
      return result;
    });
    expect(inheritedDetailTokens.label).toBe(inheritedDetailTokens.secondary);
    expect(inheritedDetailTokens.phaseDivider).toBe(inheritedDetailTokens.divider);
    await page.locator("#lifecycleDetailClose").click();
    const executionSummary = page.locator("#promptHistoryDetailContent > .prompt-detail-leftbar > .prompt-detail-card--execution-summary");
    const executionContext = page.locator("#promptHistoryDetailContent > .prompt-detail-rightbar > .prompt-detail-card--execution-context");
    await expect(executionSummary).toHaveCount(1);
    await expect(executionContext).toHaveCount(1);
    await page.setViewportSize({ width: 1280, height: 900 });
    const desktopExecutionCards = await Promise.all([executionSummary.boundingBox(), executionContext.boundingBox()]);
    expect(desktopExecutionCards[0]).not.toBeNull();
    expect(desktopExecutionCards[1]).not.toBeNull();
    expect(desktopExecutionCards[0].x).toBeLessThan(desktopExecutionCards[1].x);
    expect(desktopExecutionCards[0].y).toBe(desktopExecutionCards[1].y);
    const detailSidebar = page.locator("#promptHistoryDetailContent > .prompt-detail-leftbar > .prompt-detail-sidebar");
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-leftbar")).toHaveCount(1);
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-rightbar")).toHaveCount(1);
    const desktopEvidenceCards = await Promise.all([executionSummary.boundingBox(), executionContext.boundingBox(), detailSidebar.boundingBox()]);
    expect(desktopEvidenceCards[2]).not.toBeNull();
    expect(desktopEvidenceCards[2].x).toBe(desktopEvidenceCards[0].x);
    expect(desktopEvidenceCards[2].y).toBeGreaterThan(desktopEvidenceCards[0].y);
    expect(desktopEvidenceCards[2].y).toBeLessThanOrEqual(
      desktopEvidenceCards[0].y + desktopEvidenceCards[0].height + 48,
    );
    expect(desktopEvidenceCards[2].y).toBeLessThanOrEqual(desktopEvidenceCards[1].y + desktopEvidenceCards[1].height);
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileExecutionCards = await Promise.all([executionSummary.boundingBox(), executionContext.boundingBox()]);
    expect(mobileExecutionCards[1].y).toBeGreaterThan(mobileExecutionCards[0].y);
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Engineering Platform");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("0.146.0");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Doelrepository");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("pcvantol/forge");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Lokale checkout");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("/Users/example/Documents/GitHub/forge");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Getrackte bestanden");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("1655");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Bestanden gewijzigd");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Codex-opdrachten uitgevoerd");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("17");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Uitvoeringsmodus");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("GENESIS");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Actieve branch");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("forge-phase-evidence");
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-status .indicator--green")).toHaveCount(1);
    await expect(detailSidebar).toHaveCount(1);
    await expect(detailSidebar).toContainText("Doorlooptijd");
    await expect(detailSidebar).toContainText("Runtime");
    await expect(detailSidebar).toContainText("Git-commit");
    await expect(detailSidebar).toContainText("Uitvoeringsbewijs");
    await expect(page.locator("#promptHistoryDetailContent")).not.toContainText("pcvantol/djconnect");
    await expect(page.locator("#promptHistoryDetailContent")).not.toContainText("Historisch rapport");
    await expect(page.locator("#promptHistoryDetailContent")).not.toContainText("Historische AI-analyse");
    await expect(page.locator("#promptHistoryReportModal")).not.toBeVisible();
    await page.locator("#promptHistoryDetailClose").click();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    const detailsView = page.locator("#promptHistoryRows .prompt-history-details");
    await expect(detailsView).toHaveCount(1);
    await expect(detailsView).toHaveText("i");
    await detailsView.click();
    await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    await page.locator("#promptHistoryDetailClose").click();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    const analysisView = page.locator("#promptHistoryRows .prompt-history-analysis").first();
    await expect(page.locator("#promptHistoryRows .prompt-history-analysis")).toHaveCount(1);
    await expect(page.locator("#promptHistoryRows a.prompt-history-analysis")).toHaveCount(0);
    await expect(analysisView).toHaveCSS("color", "rgb(141, 199, 255)");
    await page.route("**/api/prompt-history/**/analysis", (route) => route.fulfill({
      contentType: "text/markdown",
      body: "# Historische AI-analyse\n\nDit advies hoort bij precies deze uitvoering.",
    }));
    await analysisView.click();
    await expect(page.locator("#promptHistoryReportModal")).toBeVisible();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    await expect(page.locator("#promptHistoryReportModalTitle"))
      .toHaveText(DASHBOARD_MESSAGES.nl["table.analysis"]);
    await expect(page.locator("#promptHistoryReportContent")).toContainText("Historische AI-analyse");
    await expect(page.locator("#promptHistoryReportDownload")).toBeVisible();
    await page.locator("#promptHistoryReportClose").click();
    const chat = page.locator("#promptHistoryRows .prompt-history-chat");
    await expect(chat).toHaveCount(1);
    await expect(chat).toHaveText("⋯");
    await expect(chat).toHaveCSS("border-top-color", "rgb(208, 164, 255)");
    await expect(chat).toHaveCSS("color", "rgb(208, 164, 255)");
    await dispatchDashboardPointerClick(chat);
    await expect(page.locator("#promptHistoryChatModal")).toBeVisible();
    await expect(page.locator("#promptHistoryChatModal")).not.toBeFocused();
    await expect(page.locator("#promptHistoryChatTitle"))
      .toHaveText(DASHBOARD_MESSAGES.nl["history.execution_chat_title"]);
    let submittedRun;
    await page.route("**/api/codex-chat", async (route) => {
      submittedRun = route.request().postDataJSON().run_id;
      await route.fulfill({ json: { answer: "Dit advies hoort bij de geselecteerde prompt.", model: "Codex CLI" } });
    });
    await page.locator("#chatInput").fill("Wat is de volgende stap?");
    const chatSubmittedResponse = page.waitForResponse((response) => (
      response.url().includes("/api/codex-chat")
      && response.request().method() === "POST"
    ));
    await dispatchDashboardPointerClick(page.locator("#chatSend"));
    await chatSubmittedResponse;
    await expect(page.locator("#chatMessages")).toContainText("geselecteerde prompt");
    expect(submittedRun).toBe("inbox-history-25");
    await page.locator("#promptHistoryChatClose").click();
    await expect(page.locator("#promptHistoryChatModal")).not.toBeVisible();
    await expect(page.getByTestId("download-inbox-log")).toHaveCount(1);
  });

  test("retries one transiently empty prompt-history projection", async ({ page }) => {
    let requests = 0;
    await page.route("**/api/prompt-history", async (route) => {
      requests += 1;
      await route.fulfill({
        json: requests === 1
          ? { runs: [] }
          : {
              runs: [{
                run_id: "inbox-recovered-history",
                status: "COMPLETE",
                title: "Recovered prompt history",
                executed_at: "2026-08-04T19:00:00Z",
              }],
            },
      });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await expect(page.locator("#promptHistoryRows")).toContainText("Recovered prompt history", {
      timeout: 3_000,
    });
    expect(requests).toBe(2);
  });

  test("scrolls only chat bubbles inside a prompt-history conversation", async ({ page }) => {
    await page.setViewportSize({ width: 720, height: 520 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal"),
      messages = page.locator("#chatMessages"),
      header = modal.locator(".prompt-chat-modal__header"),
      input = page.locator("#chatInput");

    await modal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message">Bericht</article>'.repeat(100);
      element.showModal();
    });
    const before = {
      header: await header.boundingBox(),
      input: await input.boundingBox(),
    };
    await messages.evaluate((element) => { element.scrollTop = 160; });
    const after = {
      header: await header.boundingBox(),
      input: await input.boundingBox(),
    };

    await expect.poll(() => messages.evaluate((element) => element.scrollTop)).toBe(160);
    expect(after.header.y).toBe(before.header.y);
    expect(after.input.y).toBe(before.input.y);
  });

  test("keeps the chat composer and status inside the prompt-history modal", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");
    await modal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message chat-message--assistant">Lang bericht</article>'.repeat(60);
      document.querySelector("#chatStatus").textContent = "Codex denkt na…";
      element.showModal();
    });

    const bounds = await modal.evaluate((element) => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
      return { panel: rect(".prompt-chat-modal__panel"), chat: rect("#codexChat"), input: rect("#chatInput"), model: rect("#chatModel"), status: rect("#chatStatus") };
    });
    expect(bounds.input.bottom).toBeLessThanOrEqual(bounds.panel.bottom);
    expect(bounds.input.left).toBeGreaterThan(bounds.chat.left);
    expect(bounds.input.right).toBeLessThan(bounds.chat.right);
    expect(bounds.model.bottom).toBeLessThanOrEqual(bounds.panel.bottom);
    expect(bounds.status.bottom).toBeLessThanOrEqual(bounds.panel.bottom);
    expect(bounds.status.y).toBeGreaterThanOrEqual(bounds.input.bottom);
    expect(bounds.status.left).toBeGreaterThan(bounds.model.right);
  });

  test("keeps the thinking status beside the model after the AI question box is resized", async ({ page }) => {
    await page.setViewportSize({ width: 2048, height: 1152 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");
    await modal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message chat-message--assistant">Antwoord</article>'.repeat(30);
      document.querySelector("#chatInput").style.height = "260px";
      document.querySelector("#chatStatus").textContent = "Codex denkt na…";
      element.showModal();
    });

    const bounds = await modal.evaluate((element) => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
      return { panel: rect(".prompt-chat-modal__panel"), input: rect("#chatInput"), model: rect("#chatModel"), status: rect("#chatStatus") };
    });
    expect(bounds.model.bottom).toBeLessThanOrEqual(bounds.panel.bottom);
    expect(bounds.status.bottom).toBeLessThanOrEqual(bounds.panel.bottom);
    expect(bounds.status.y).toBeGreaterThanOrEqual(bounds.input.bottom);
    const labelY = await page.locator("#chatModel").evaluate((model) =>
      Math.round(model.closest(".field").querySelector(".label").getBoundingClientRect().y),
    );
    expect(Math.abs(Math.round(bounds.status.y) - labelY)).toBeLessThanOrEqual(8);
  });

  test("reserves space for the chat model and thinking state in a short viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 500 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");
    await modal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message chat-message--assistant">Lang bericht</article>'.repeat(60);
      document.querySelector("#chatInput").style.height = "260px";
      document.querySelector("#chatStatus").textContent = "Codex denkt na…";
      element.showModal();
    });

    const bounds = await modal.evaluate((element) => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
      return { panel: rect(".prompt-chat-modal__panel"), input: rect("#chatInput"), model: rect("#chatModel"), status: rect("#chatStatus") };
    });
    expect(bounds.input.height).toBeLessThanOrEqual(128);
    expect(bounds.status.y).toBeGreaterThanOrEqual(bounds.input.bottom);
    expect(bounds.input.bottom).toBeLessThanOrEqual(bounds.panel.bottom - 10);
    expect(bounds.model.bottom).toBeLessThanOrEqual(bounds.panel.bottom - 4);
  });

  test("uses a distinct purple surface for AI answers in a prompt-history conversation", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal"),
      panel = modal.locator(".prompt-chat-modal__panel");
    await modal.evaluate((element) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message chat-message--assistant">Antwoord</article>';
      element.showModal();
    });
    const answer = page.locator("#chatMessages .chat-message--assistant");

    await expect(answer).toHaveCSS("background-color", "rgb(60, 42, 77)");
    expect(await answer.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    )).not.toBe(await panel.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ));
  });

  test("uses the shared neutral surface for prompt-history detail and chat modal shells", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const selector of [
      ".prompt-detail-modal__panel",
      ".prompt-chat-modal__panel",
    ]) {
      await expect(page.locator(selector)).toHaveCSS("background-color", "rgb(36, 36, 45)");
    }
  });

  test("keeps the empty AI conversation surface neutral in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.locator("#promptHistoryChatModal").evaluate((element) => element.showModal());

    await expect(page.locator("#codexChat")).toHaveCSS("background-color", "rgb(247, 251, 255)");
  });

  test("uses purpose-matched glyphs in modal titles", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const [selector, glyph] of [
      ["#componentModalTitle", "⚙︎"],
      ["#confirmationModalTitle", "ⓘ"],
      ["#promptHistoryReportModalTitle", "▤"],
      ["#promptHistoryChatTitle", "⋯"],
    ]) {
      expect(await page.locator(selector).evaluate(
        (title) => getComputedStyle(title, "::before").content,
      )).toContain(glyph);
    }
    expect(await page.locator("#promptHistoryDetailTitle").evaluate(
      (title) => getComputedStyle(title, "::before").content,
    )).toBe("none");
  });

  test("sizes prompt-history chat bubbles to their content", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#promptHistoryChatModal").evaluate((modal) => {
      document.querySelector("#chatMessages").innerHTML =
        '<article class="chat-message chat-message--user"><span class="chat-message__role">Jij</span><div class="chat-message__body">Korte vraag</div></article>';
      modal.showModal();
    });

    const sizes = await page.locator("#chatMessages").evaluate((container) => {
      const message = container.querySelector(".chat-message");
      return {
        container: container.getBoundingClientRect().height,
        message: message.getBoundingClientRect().height,
      };
    });
    expect(sizes.container).toBeGreaterThan(200);
    expect(sizes.message).toBeLessThan(100);
  });

  test("retains terminal status colours in the light prompt-history table", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    // Avoid the production empty-history retry racing this visual fixture.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#themeToggle").click();
    await page.evaluate(() => {
      promptHistoryEntries = [
        { run_id: "inbox-complete", status: "COMPLETE", title: "Complete", executed_at: "2026-08-03T12:00:00Z" },
        { run_id: "inbox-blocked", status: "BLOCKED", title: "Blocked", executed_at: "2026-08-03T11:00:00Z" },
        { run_id: "inbox-failed", status: "FAILED", title: "Failed", executed_at: "2026-08-03T10:00:00Z" },
      ];
      renderPromptHistory();
    });
    await expect(page.locator(".prompt-history-status--complete")).toHaveCSS("color", "rgb(20, 134, 91)");
    await expect(page.locator(".prompt-history-status--blocked")).toHaveCSS("color", "rgb(166, 90, 0)");
    await expect(page.locator(".prompt-history-status--failed")).toHaveCSS("color", "rgb(180, 35, 64)");
  });

  test("searches prompt history by localized terminal status", async ({ page }) => {
    // Keep this fixture non-empty. The production empty-history retry is
    // intentional, but its delayed response can otherwise overwrite the
    // localized rows while this test is exercising the client-side filter.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [
      { run_id: "inbox-complete", status: "COMPLETE", title: "Completed prompt" },
      { run_id: "inbox-blocked", status: "BLOCKED", title: "Blocked prompt" },
    ] } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await selectDashboardLocale(page, "nl");
    await expect(page.locator("html")).toHaveAttribute("lang", "nl");
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });

    await page.locator("#promptHistoryFilter").fill("voltooid");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    await expect(page.locator("#promptHistoryRows")).toContainText("Voltooid");
    await page.locator("#promptHistoryFilter").fill("geblokkeerd");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    await expect(page.locator("#promptHistoryRows")).toContainText("Geblokkeerd");

    await selectDashboardLocale(page, "en");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.locator("#promptHistoryFilter").fill("complete");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    await page.locator("#promptHistoryFilter").fill("blocked");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
  });

  test("retains severity colours in the light component-log table", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "IDLE", queue_depth: 0 } },
    }));
    const snapshotLoaded = page.waitForResponse("**/api/dashboard-snapshot");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await snapshotLoaded;
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#themeToggle").click();
    await page.evaluate(() => {
      document.querySelector("#inboxComponentLog").innerHTML =
        '<tr><td class="log-level log-level--info">INFO</td><td class="log-level log-level--error">ERROR</td></tr>';
    });

    await expect(page.locator("#inboxComponentLog .log-level--info").first()).toHaveCSS("color", "rgb(23, 105, 170)");
    await expect(page.locator("#inboxComponentLog .log-level--error").first()).toHaveCSS("color", "rgb(180, 35, 64)");
  });

  test("uses a light inline-code surface in AI answers when light mode is enabled", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await page.evaluate(() => {
      const message = document.createElement("article");
      message.className = "chat-message chat-message--assistant";
      message.innerHTML = '<div class="chat-message__body"><p><code>git diff --check</code></p></div>';
      document.querySelector("#chatMessages").append(message);
    });
    await expect(page.locator(".chat-message--assistant code")).toHaveCSS("background-color", "rgb(233, 238, 246)");
    await expect(page.locator(".chat-message--assistant code")).toHaveCSS("color", "rgb(24, 34, 48)");
  });

  test("opens and closes all visible dashboard categories with the title-bar switch", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    const toggle = page.getByTestId("toggle-all-sections");
    await expect(toggle).toHaveAttribute("role", "switch");
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(toggle).toHaveAttribute("aria-label", "Alle secties openen");

    await toggle.evaluate((button) => button.click());
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(toggle).toHaveAttribute("aria-label", "Alle secties sluiten");
    for (const id of ["workspaceCard", "configuration", "promptHistory", "platformHealth", "componentLogs"]) {
      await expect(page.locator(`#${id}`)).toHaveAttribute("open", "");
    }
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(page.locator("#workspaceCard")).toHaveAttribute("open", "");

    await toggle.evaluate((button) => button.click());
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    for (const id of ["workspaceCard", "configuration", "platformHealth", "componentLogs"]) {
      await expect(page.locator(`#${id}`)).not.toHaveAttribute("open", "");
    }
  });

  test("switches between persisted dark and light dashboard themes", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const toggle = page.getByTestId("theme-toggle");
    await expect(toggle).toHaveAttribute("role", "switch");
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(toggle).toHaveAttribute("aria-label", "Donkere modus inschakelen");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(232, 237, 244)");
    await page.evaluate(() => rateLimits({ provider: "Codex CLI", provider_version: "0.146.0", windows: [], reset_credits: 1 }));
    await expect(page.locator("#rateLimitReset")).toHaveCSS("background-color", "rgb(232, 255, 245)");
    await expect(page.locator("#rateLimitReset")).toHaveCSS("color", "rgb(20, 90, 66)");
    await page.evaluate(() => r({
      watcher_state: "HOST_PREFLIGHT_FAILED",
      current_phase: "INITIALIZE",
      diagnostic: "Host preflight failed",
    }, { host_preflight: { outcome: "FAILED" } }));
    await expect(page.locator("#technicalDetails .technical-grid > .card").first()).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = Array.from({ length: 26 }, (_, index) => ({
        run_id: `light-theme-${index}`,
        status: "COMPLETE",
        title: `Lichte modus prompt ${index}`,
        executed_at: "2026-08-02T10:00:00+00:00",
        git_commit: null,
        report_available: false,
      }));
      promptHistoryPage = 1;
      renderPromptHistory();
    });
    const historyPagination = page.locator("#promptHistoryPagination");
    await expect(historyPagination.getByRole("button", { name: "Volgende" })).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await expect(historyPagination.getByRole("button", { name: "Vorige" })).not.toHaveCSS("background-color", "rgb(68, 43, 55)");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await toggle.click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  });

  test("fills the reset action with a brighter green on hover", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#rateLimits").evaluate((element) => { element.open = true; });
    await page.evaluate(() => rateLimits({ provider: "Codex CLI", provider_version: "0.146.0", windows: [], reset_credits: 1 }));
    const reset = page.locator("#rateLimitReset");

    // Keep the actual pointer over the target. This verifies the browser's
    // hover treatment without racing the initial asynchronous dashboard load.
    await reset.scrollIntoViewIfNeeded();
    await reset.hover();
    await expect(reset).toHaveCSS("background-color", "rgb(81, 216, 138)");
    await expect(reset).toHaveCSS("color", "rgb(17, 42, 32)");
  });

  test("shows the reset outcome instead of a generic failure for a valid conflict response", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: {
        status: { watcher_state: "WATCHER_IDLE", queue_depth: 0 },
        rate_limits: { provider: "Codex CLI", provider_version: "0.146.0", windows: [], reset_credits: 1 },
      },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#rateLimits").evaluate((element) => { element.open = true; });
    await page.evaluate(() => rateLimits({ provider: "Codex CLI", provider_version: "0.146.0", windows: [], reset_credits: 1 }));
    await page.route("**/api/rate-limit-reset", (route) => route.fulfill({
      status: 409,
      json: { outcome: "nothingToReset", rate_limits: { reset_credits: 1 } },
    }));

    await page.locator("#rateLimitReset").click();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#rateLimitResetStatus")).toHaveText("Er is op dit moment niets om te resetten.");
  });

  test("parses each newline-delimited JSON log entry separately", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const entries = await page.evaluate(() => structuredLogEntries(
      '{"timestamp":"2026-08-01T10:00:00+00:00","level":"INFO","event":"first"}\n'
      + '{"timestamp":"2026-08-01T10:01:00+00:00","level":"WARNING","event":"second"}',
    ));
    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.event)).toEqual(["first", "second"]);
    expect(entries.map((entry) => entry.level)).toEqual(["INFO", "WARNING"]);
  });

  test("hides empty HTTP-server placeholder debug messages", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const entries = await page.evaluate(() => structuredLogEntries(
      '{"level":"DEBUG","event":"http_server_message","diagnostic":"\\"%s\\" %s %s"}\n'
      + '{"level":"DEBUG","event":"http_request","diagnostic":"/health"}',
    ));
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ event: "http_request", details: "diagnostic: /health" });
  });

  test("formats displayed log timestamps as dd-MM-yyyy HH:mm:ss", async ({ page }) => {
    // Keep the entries injected below stable: a later server-push snapshot can
    // legitimately replace the component log with its empty-state projection.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE" }, component_log_versions: {} },
    }));
    await page.route("**/api/logs/**", (route) =>
      route.fulfill({ contentType: "application/x-ndjson", body: "" }),
    );
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const loadComponentLogs = page.locator("#loadComponentLogs");
    if (await loadComponentLogs.count()) await loadComponentLogs.click();
    await page.waitForFunction(() => componentLogsLoaded === true);
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      componentLogEntries.inbox = structuredLogEntries(
        '{"timestamp":"2026-08-02T19:26:10.878167+00:00","level":"INFO","event":"formatted"}\n'
        + '{"timestamp":"onbekend-tijdstip","level":"WARNING","event":"fallback"}',
      );
      componentLogEntries.dashboard = [];
      renderComponentLogs();
    });

    await expect(page.locator("#inboxComponentLog tr td").nth(1)).toContainText("02-08-2026");
    await expect(page.locator("#inboxComponentLog tr td").nth(1)).toContainText("21:26:10");
    await expect(page.locator("#inboxComponentLog tr").nth(1).locator("td").nth(1)).toHaveText("onbekend-tijdstip");
  });

  test("treats an absent component log as an empty state, not malformed JSON", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      componentLogEntries.inbox = structuredLogEntries("Nog geen applicatielog beschikbaar.");
      componentLogEntries.dashboard = [];
      renderComponentLogs();
    });

    const inboxText = await page.locator("#inboxComponentLog").textContent();
    expect(inboxText).toContain("Nog geen applicatielog beschikbaar.");
    expect(inboxText).not.toContain("ONGELDIGE JSON");
    expect(inboxText).not.toContain("onleesbare logregel");
  });

  test("keeps the Inbox queue visible when empty and numbers the oldest prompt first", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    const queue = page.getByTestId("engineering-inbox-queue");
    await page.evaluate(() => queueItems([], 0));
    await expect(queue).toBeVisible();
    await expect(queue).not.toHaveAttribute("open", "");
    await expect(queue.locator("summary")).toContainText("Wachtrij voor uitvoeringen");
    await expect(queue.locator(".category-description")).toHaveText("Nieuwe opdrachten wachten op uitvoering in volgorde van aanmaakdatum.");
    await dispatchDashboardPointerClick(queue.locator("summary"));
    await expect(page.locator("#queueSummary")).toHaveText("0 uitvoeringen in de wachtrij.");
    await expect(page.locator("#queueList")).toContainText("Geen Inbox-uitvoeringen wachten op uitvoering.");

    await page.evaluate(() => queueItems([
      { filename: "later.md", title: "Later uitvoeren", modified_at: "2026-08-02T10:02:00Z" },
      { filename: "earlier.md", title: "Eerst uitvoeren", modified_at: "2026-08-02T10:01:00Z" },
    ], 2));
    const entries = page.locator("#queueList .queue-item");
    await expect(entries).toHaveCount(2);
    await expect(entries.nth(0)).toContainText("1");
    await expect(entries.nth(0)).toContainText("Eerst uitvoeren");
    await expect(entries.nth(0)).toContainText("Bestandsnaam: earlier.md");
    await expect(entries.nth(0)).toHaveAttribute("aria-label", "Positie 1: Eerst uitvoeren");
    await expect(entries.nth(1)).toContainText("Later uitvoeren");
    await expect(page.locator("#queueSummary")).toHaveText("2 uitvoeringen in de wachtrij.");
  });

  test("defers one waiting Inbox item through a confirmed reversible action", async ({ page }) => {
    let deferred = false;
    const queued = [
      { filename: "defer-me.md", title: "Later uitvoeren", modified_at: "2026-08-02T10:01:00Z" },
      { filename: "keep-waiting.md", title: "Blijft wachten", modified_at: "2026-08-02T10:02:00Z" },
    ];
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: { watcher_state: "WATCHER_IDLE", queue_depth: deferred ? 1 : 2, queue_items: deferred ? queued.slice(1) : queued },
      component_versions: {}, telemetry: [], duration_estimate: {}, build_commit: "",
    } }));
    await page.route("**/api/queue-defer", async (route) => {
      expect(JSON.parse(route.request().postData() || "{}")).toEqual({ filename: "defer-me.md" });
      deferred = true;
      await route.fulfill({ status: 202, json: { filename: "defer-me.md", deferred_filename: "defer-me.md" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#queueItems").evaluate((element) => { element.open = true; });

    const deferButton = page.getByRole("button", { name: "Stel uit" }).first();
    await deferButton.hover();
    await expect(deferButton).toHaveCSS("background-color", "rgb(240, 182, 106)");
    await expect(deferButton).toHaveCSS("border-top-color", "rgb(240, 182, 106)");

    await deferButton.click();
    await expect(page.locator("#confirmationModalTitle")).toHaveText("Uitvoering uitstellen");
    await expect(page.locator("#confirmationModalText")).toContainText("Inbox/_deferred");
    await page.locator("#confirmationModalConfirm").click();

    await expect(page.locator("#queueList .queue-item")).toHaveCount(1);
    await expect(page.locator("#queueList")).toContainText("Blijft wachten");
    await expect(page.locator("#queueList")).not.toContainText("Later uitvoeren");
  });

  test("keeps a waiting Inbox item when deferring is cancelled", async ({ page }) => {
    let deferRequests = 0;
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: {
        watcher_state: "WATCHER_IDLE",
        queue_depth: 1,
        queue_items: [{ filename: "keep-me.md", title: "Blijf actief", modified_at: "2026-08-02T10:01:00Z" }],
      },
      component_versions: {}, telemetry: [], duration_estimate: {}, build_commit: "",
    } }));
    await page.route("**/api/queue-defer", async (route) => {
      deferRequests += 1;
      await route.fulfill({ status: 500, json: { error: "niet verwacht" } });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#queueItems").evaluate((element) => { element.open = true; });

    await page.getByRole("button", { name: "Stel uit" }).click();
    await page.locator("#confirmationModalCancel").click();

    await expect(page.locator("#queueList .queue-item")).toHaveCount(1);
    await expect(page.locator("#queueList")).toContainText("Blijf actief");
    expect(deferRequests).toBe(0);
  });

  test("shows the Codex CLI blocker in the Inbox queue", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0 } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({
      watcher_state: "HOST_PREFLIGHT_FAILED",
      diagnostic: "Execution Host Preflight blocked by runtime_invocation.",
      queue_depth: 1,
      queue_items: [{ filename: "waiting.txt", title: "Waiting prompt" }],
    }, {}));

    await dispatchDashboardPointerClick(page.getByTestId("engineering-inbox-queue").locator("summary"));
    await expect(page.locator("#inboxBlocker")).toBeVisible();
    await expect(page.locator("#inboxBlocker")).toHaveText(
      "De Inbox wacht omdat de lokale Codex CLI niet kan starten. Herstel dit handmatig met: npm install -g @openai/codex@latest",
    );
  });

  test("offers a confirmed repair for a queue blocked on a working branch", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    let repairRequested = false;
    await page.route("**/api/managed-branch-recovery", async (route) => {
      repairRequested = true;
      expect(route.request().postData()).toBe("{}");
      await route.fulfill({ json: { previous_branch: "codex/ui-polish", branch: "main", watcher: "restarted" }, status: 202 });
    });
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WORKSPACE_PREFLIGHT_FAILED", queue_depth: 1 } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({
      watcher_state: "WORKSPACE_PREFLIGHT_FAILED",
      diagnostic: "Workspace Preflight blocked by managed_expected_branch.",
      queue_depth: 1,
      queue_items: [{ filename: "waiting.txt", title: "Waiting prompt" }],
    }, {}));

    await dispatchDashboardPointerClick(page.getByTestId("engineering-inbox-queue").locator("summary"));
    const blocker = page.locator("#inboxBlocker");
    await expect(blocker).toHaveClass(/queue-blocker--error/);
    await expect(blocker).toContainText("Execution Host mag alleen werk vanaf main claimen.");
    const repair = blocker.getByRole("button", { name: "Herstel" });
    await expect(repair).toHaveCSS("background-color", "rgb(59, 40, 27)");
    await expect(repair).toHaveCSS("border-color", "rgb(240, 182, 106)");
    await expect(repair).toHaveCSS("border-radius", "8px");
    // The sticky readiness banner deliberately sits above page content and
    // makes native pointer hover non-deterministic in headless Chromium.
    // Verify the exact design-system rule while retaining the real recovery
    // interaction below as a separate behavioral assertion.
    const stylesheet = readFileSync(path.join(repository, "tools/engineering/assets/dashboard.css"), "utf8");
    expect(stylesheet).toContain(
      ".queue-blocker__repair:hover:not(:disabled){background:#f0b66a!important;border-color:#f0b66a!important;color:#201812!important}",
    );
    await dispatchDashboardPointerClick(repair);
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await expect(page.locator("#confirmationModalText")).toContainText("herstart de Inbox-watcher");
    await page.locator("#confirmationModalConfirm").click();
    await expect(blocker).toHaveText("Werkmap hersteld naar main; de Inbox-watcher draait weer.");
    expect(repairRequested).toBeTruthy();
  });

  test("shows and safely recovers a confirmed stale Git workspace lock", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    let recoveryRequested = false;
    await page.route("**/api/stale-git-lock-recovery", async (route) => {
      recoveryRequested = true;
      expect(route.request().postData()).toBe("{}");
      await route.fulfill({ json: { state: "free", recovered: true }, status: 202 });
    });
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0 } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE", queue_depth: 0 }, {
      workspace_git_lock: { state: "stale", stale: true, age_seconds: 360 },
    }));

    const lock = page.locator("#technicalGitLock");
    const repository = page.locator("#technicalRepositoryTitle").locator("xpath=ancestor::div[contains(@class, 'card')][1]");
    await expect(lock).toContainText("Werkmapvergrendeling");
    await expect(repository).toContainText("Werkmapvergrendeling");
    await expect(page.locator("#technicalWorkspaceStateInfo")).toHaveAttribute("aria-label", DASHBOARD_MESSAGES.nl["technical.workspace_status_help"]);
    await expect(page.locator("#technicalGitLockInfo")).toHaveAttribute("aria-label", DASHBOARD_MESSAGES.nl["technical.git_lock_help"]);
    await expect(lock).toContainText("Actief");
    await expect(lock).toContainText("Git voert een andere actie uit; nieuwe uitvoeringen wachten.");
    await lock.getByRole("button", { name: "Herstel vergrendeling" }).click();
    await expect(page.locator("#confirmationModal")).toBeVisible();
    await expect(page.locator("#confirmationModalText")).toContainText("uitsluitend de verouderde Git-indexvergrendeling");
    await page.locator("#confirmationModalConfirm").click();
    await expect(lock).toContainText("Vrij");
    await expect(lock).toContainText("De verouderde Git-vergrendeling is verwijderd.");
    expect(recoveryRequested).toBeTruthy();
  });

  test("offers a downloadable offline backup for the engineering database", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await dispatchDashboardPointerClick(page.locator("#configuration > summary"));
    await expect(page.locator("#workspaceFreeDiskSpace")).toHaveCount(1);
    await expect(page.locator("#workspaceFreeDiskSpace").locator("xpath=..")).toHaveAttribute("id", "configurationHostComponents");
    const databaseSection = page.locator(".workspace-database-section");
    await expect(databaseSection).toHaveCount(1);
    await expect(databaseSection.locator("xpath=..")).toHaveAttribute("id", "configuration");
    await expect(databaseSection).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(databaseSection.locator("#workspaceDatabaseHeading")).toHaveText("Engineering-database");
    for (const id of ["workspaceDatabaseField", "workspaceDatabaseSize", "workspaceSchemaVersion"])
      await expect(databaseSection.locator(`#${id}`)).toHaveCount(1);
    await expect(page.locator("#workspaceCard #workspaceDatabaseField")).toHaveCount(0);
    const download = page.locator("#workspaceDatabaseDownload");
    await expect(download).toBeVisible();
    await expect(download).toHaveAttribute("href", "/api/engineering-database/download?audit=download");
    await expect(download).toHaveAttribute("aria-label", "Databaseback-up downloaden");
  });

  test("groups fixed platform settings in a read-only configuration subsection", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await dispatchDashboardPointerClick(page.locator("#configuration > summary"));
    const settings = page.locator(".configuration-readonly-settings");
    await expect(settings).toHaveCount(1);
    await expect(settings).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(settings.locator("#configurationReadonlySettingsTitle")).toHaveText("Vaste platforminstellingen");
    await expect(settings.locator(".configuration-field")).toHaveCount(6);
  });

  test("scans stale local branches before confirming their cleanup", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    const branches = Array.from({ length: 28 }, (_, index) => ({
      name: `codex/stale-${String(index + 1).padStart(2, "0")}`,
      reason: "remote_absent_and_matches_main",
      removable: true,
    }));
    branches[0].pull_request = { number: 847, url: "https://github.com/pcvantol/djconnect/pull/847" };
    let releasePreview;
    let previewRequested;
    const previewRequest = new Promise((resolve) => { previewRequested = resolve; });
    await page.route("**/api/stale-local-branch-cleanup-preview", async (route) => {
      expect(route.request().postData()).toBe("{}");
      previewRequested();
      await new Promise((resolve) => { releasePreview = resolve; });
      await route.fulfill({ json: { branches, removable_branches: branches.map((branch) => branch.name) } });
    });
    await page.route("**/api/stale-local-branch-cleanup", async (route) => {
      expect(JSON.parse(route.request().postData())).toEqual({ branches: branches.map((branch) => branch.name) });
      await route.fulfill({ json: { removed: branches.map((branch) => branch.name), removed_count: branches.length }, status: 202 });
    });
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0 } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await dispatchDashboardPointerClick(page.locator("#workspaceCard > summary"));
    await page.locator("#workspaceBranchCleanup").evaluate((button) => { button.hidden = false; });
    await expect(page.locator("#workspaceBranchCleanup")).toHaveCSS("border-color", "rgb(243, 211, 106)");
    await expect(page.locator("#workspaceBranchCleanup")).toHaveCSS("background-color", "rgb(60, 53, 31)");
    expect(await page.locator("#workspaceBranchCleanup").evaluate(
      (button) => getComputedStyle(button, "::before").content,
    )).toBe('"⌕"');
    await dispatchDashboardPointerClick(page.getByRole("button", { name: "Beoordeel losse lokale branches" }));

    const confirmation = page.locator("#confirmationModal");
    await previewRequest;
    await expect(confirmation).toBeVisible();
    await expect(confirmation.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    await expect(confirmation.locator(".workspace-branch-cleanup__spinner")).toBeVisible();
    await expect(page.locator("#confirmationModalConfirm")).toBeDisabled();
    releasePreview();
    await expect(confirmation.locator(".workspace-branch-cleanup__spinner")).toHaveCount(0);
    await expect(page.locator("#confirmationModalConfirm")).toBeEnabled();
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    await expect(page.locator("#confirmationModalTitle")).toHaveText("Losse lokale branches veilig opruimen");
    await expect(page.locator("#confirmationModalText")).toContainText("worktrees blijven onaangeraakt");
    const candidates = confirmation.locator(".workspace-branch-cleanup__preview-list");
    await expect(candidates).toHaveCount(1);
    await expect(candidates.locator("li")).toHaveCount(28);
    await expect(candidates.first()).toContainText("codex/stale-01");
    await expect(candidates.first()).toContainText("Bestaat niet meer op origin; de inhoud is aantoonbaar in main gemergd.");
    await expect(candidates.first().getByRole("link", { name: "PR #847" })).toHaveAttribute(
      "href", "https://github.com/pcvantol/djconnect/pull/847",
    );
    await expect(candidates).toHaveCSS("overflow-y", "auto");
    await page.locator("#confirmationModalConfirm").click();

    const result = page.locator("#workspaceBranchCleanupResultModal");
    await expect(result).toBeVisible();
    await expect(result.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(243, 211, 106)");
    await expect(result).toContainText("28 losse lokale branch(es) verwijderd.");
    await expect(result.locator(".workspace-branch-cleanup__result-list li")).toHaveCount(28);
  });

  test("shows retained standalone branches without offering destructive cleanup", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/stale-local-branch-cleanup-preview", (route) => route.fulfill({ json: {
      branches: [
        { name: "codex/remote", reason: "remote_branch_exists", removable: false },
        { name: "codex/different", reason: "content_differs_from_main", removable: false },
      ],
      removable_branches: [],
    } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await dispatchDashboardPointerClick(page.locator("#workspaceCard > summary"));
    await page.locator("#workspaceBranchCleanup").evaluate((button) => { button.hidden = false; });
    await dispatchDashboardPointerClick(page.getByRole("button", { name: "Beoordeel losse lokale branches" }));

    const confirmation = page.locator("#confirmationModal");
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toContainText("Geen losse lokale branches zijn veilig te verwijderen.");
    await expect(confirmation.locator(".workspace-branch-cleanup__preview-list")).toContainText("codex/remote");
    await expect(confirmation.locator(".workspace-branch-cleanup__preview-list")).toContainText("de branch bestaat nog op origin");
    await expect(confirmation.locator(".workspace-branch-cleanup__preview-list")).toContainText("codex/different");
    await expect(confirmation.locator(".workspace-branch-cleanup__preview-list")).toContainText("de inhoud verschilt nog van main");
    await expect(page.locator("#confirmationModalConfirm")).toHaveText("Sluiten");
    await expect(page.locator("#confirmationModalCancel")).toBeHidden();
  });

  test("renders provider limit rows on separate lines", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => rateLimits({
      provider: "Codex CLI",
      provider_version: "0.146.0",
      provider_path: "/opt/homebrew/bin/codex",
      windows: [{ label: "Weekvenster", used_percent: 24, resets_at: 0 }],
      reset_credits: 2,
    }));
    await expect(page.locator("#rateLimitProvider")).toHaveText("Codex CLI · 0.146.0");
    await expect(page.locator("#rateLimitProviderPath")).toHaveText("/opt/homebrew/bin/codex");
    await expect(page.locator("#rateLimitDetails")).toHaveText(/Weekvenster: 76,0% beschikbaar.*Beschikbare resets: 2/s);
    expect(await page.locator("#rateLimitDetails").evaluate((element) => element.textContent)).toContain("\n");
    await page.evaluate(() => rateLimits({ provider: "Codex CLI", provider_version: "0.146.0", windows: [], reset_credits: 0 }));
    await expect(page.locator("#rateLimitProviderPath")).toHaveText("Niet beschikbaar");
  });

  test("renders an accessible rolling two-hour capacity trend", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => rateLimits(
      { provider: "Codex CLI", provider_version: "0.149.1", windows: [{ label: "Weekvenster", used_percent: 14, resets_at: 0 }] },
      [
        { at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(), remaining_percent: 92 },
        { at: new Date(Date.now() - 60 * 60 * 1000).toISOString(), remaining_percent: 86 },
      ],
    ));
    const trend = page.locator("#rateLimitTrend");
    await expect(trend).toContainText("Verloop beschikbare capaciteit");
    await expect(trend).toContainText("elke twee uur één opgeslagen meting");
    await expect(trend.locator(".rate-limit-trend__title")).toHaveCSS("font-size", "11px");
    await expect(trend.locator(".rate-limit-trend__title")).toHaveCSS("font-weight", "700");
    await expect(trend.locator("svg[role='img']")).toHaveAttribute("aria-labelledby", "rateLimitTrendSvgTitle");
    await expect(trend.locator(".rate-limit-trend__grid")).toHaveCount(13);
    const axisLabels = trend.locator(".rate-limit-trend__axis-label");
    await expect(axisLabels).toHaveCount(13);
    await expect(axisLabels.first()).toHaveCSS("font-size", "7px");
    await expect(axisLabels.first()).toHaveCSS("fill", "rgb(247, 243, 238)");
    await expect(trend.locator(".rate-limit-trend__line")).toHaveCount(1);
    await expect(trend.locator(".rate-limit-trend__point")).toHaveCount(2);
    await expect(trend.locator(".rate-limit-trend__line")).toHaveAttribute("d", / L /);
    await expect(trend.locator(".rate-limit-trend__point").first()).toHaveAttribute("r", "1.6");
    await expect(trend.locator(".rate-limit-trend__point").first()).toHaveCSS("fill", "rgb(72, 207, 153)");
    await expect(trend).not.toContainText("Nu 86,0% beschikbaar");
    await page.getByTestId("theme-toggle").click();
    await expect(axisLabels.first()).toHaveCSS("fill", "rgb(24, 34, 48)");
  });

  test("keeps dashboard view preferences in the browser", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const autoRefresh = page.locator("#autoRefresh");
    await expect(autoRefresh).toBeChecked();
    await page.evaluate(() => r({
      watcher_state: "HOST_PREFLIGHT_FAILED",
      current_phase: "INITIALIZE",
      diagnostic: "Host preflight failed",
    }, { host_preflight: { outcome: "FAILED" } }));
    await page.locator("#technicalDetails").evaluate((element) => { element.open = false; });
    await dispatchDashboardPointerClick(page.locator("#technicalDetails > summary"));
    await page.locator("#autoRefresh").uncheck();
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(autoRefresh).not.toBeChecked();
    await expect(page.locator("#technicalDetails")).toHaveAttribute("open", "");
  });

  test("uses an in-app confirmation modal before clearing each component log", async ({ page }) => {
    let postCount = 0;
    await page.route("**/api/logs/inbox", async (route) => {
      if (route.request().method() === "POST") {
        postCount += 1;
        await route.fulfill({ contentType: "application/json", body: '{"cleared":"inbox"}' });
        return;
      }
      await route.fulfill({ contentType: "text/plain", body: "" });
    });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    const nativeDialogs = [];
    page.on("dialog", async (dialog) => {
      nativeDialogs.push(dialog.type());
      await dialog.dismiss();
    });
    await page.getByTestId("clear-inbox-log").click();
    const modal = page.locator("#confirmationModal");
    await expect(modal).toBeVisible();
    await expect(page.locator("#confirmationModalCancel")).toBeFocused();
    await expect(modal.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    await expect(page.locator("#confirmationModal .confirmation-modal__header")).toHaveCSS("border-bottom-color", "rgb(255, 113, 143)");
    await expect(page.locator("#confirmationModalTitle")).toHaveCSS("color", "rgb(255, 113, 143)");
    expect(await page.locator("#confirmationModalTitle").evaluate((title) => getComputedStyle(title, "::before").color)).toBe("rgb(255, 113, 143)");
    expect(await page.locator("#confirmationModalConfirm").evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe("rgb(240, 182, 106)");
    for (const action of [page.locator("#confirmationModalCancel"), page.locator("#confirmationModalConfirm")]) {
      await action.hover();
      await expect(action).toHaveCSS("background-color", "rgb(255, 113, 143)");
      await expect(action).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    }
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("font-size", "13px");
    await expect(page.locator("#confirmationModalConfirm")).toHaveCSS("font-family", await page.locator("#rateLimitReset").evaluate((button) => getComputedStyle(button).fontFamily));
    await expect(modal).toContainText("De applicatielogs van Engineering Execution Host wissen?");
    await page.keyboard.press("Escape");
    await expect(modal).not.toBeVisible();
    expect(postCount).toBe(0);

    await page.getByTestId("clear-inbox-log").click();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.getByTestId("clear-dashboard-log")).toBeVisible();
    await expect.poll(() => postCount).toBe(1);
    expect(nativeDialogs).toEqual([]);
  });

  test("clears only the browser-local AI conversation through the in-app modal", async ({ page }) => {
    // Avoid the production empty-history retry detaching the chat action.
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: {
      runs: [{ run_id: "inbox-fixture", status: "COMPLETE", title: "Fixture" }],
    } }));
    await page.route("**/api/codex-chat", async (route) => {
      await route.fulfill({ contentType: "application/json", body: '{"answer":"De uitvoering is gereed.","model":"Codex CLI"}' });
    });
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-chat-clear",
        status: "COMPLETE",
        title: "Chat clear",
        executed_at: "2026-08-04T12:00:00Z",
      }];
      renderPromptHistory();
    });
    await dispatchDashboardPointerClick(page.locator("#promptHistoryRows .prompt-history-chat"));
    await page.locator("#chatInput").fill("Wat is de status?");
    await page.locator("#chatSend").click();

    await expect(page.locator("#clearChat")).toBeVisible();
    await page.locator("#clearChat").click();
    const modal = page.locator("#confirmationModal");
    await expect(modal).toContainText("Dit wist alleen de lokale chatweergave.");
    await page.locator("#confirmationModalCancel").click();
    await expect(page.locator("#chatMessages")).toContainText("Wat is de status?");

    await page.locator("#clearChat").click();
    await page.locator("#confirmationModalConfirm").click();
    await expect(page.locator("#chatMessages")).toBeEmpty();
    await expect(page.locator("#clearChat")).toBeHidden();
    await expect(page.locator("#downloadChat")).toBeHidden();
    await expect(page.locator("#copyChat")).toBeHidden();
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem("djconnect-engineering-chat-history"))).toBeNull();
  });

  test("refreshes status and prompt history immediately after dismissing an execution", async ({ page }) => {
    let historyReads = 0;
    let dismissed = false;
    // Keep the terminal snapshot deterministic: a live server-push event can
    // otherwise replace its last executed run while the operator acts on it.
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/prompt-history", async (route) => {
      historyReads += 1;
      await route.fulfill({ json: { runs: dismissed ? [{
        run_id: "inbox-dismiss", status: "BLOCKED", dismissed: true,
        title: "Dismissed prompt", executed_at: "2026-08-04T12:00:00Z",
      }] : [{
        run_id: "inbox-dismiss", status: "BLOCKED",
        title: "Dismissed prompt", executed_at: "2026-08-04T12:00:00Z",
      }] } });
    });
    await page.route("**/api/execution-dismiss", (route) => {
      dismissed = true;
      return route.fulfill({ json: { dismissed: "inbox-dismiss" } });
    });
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({ json: {
      status: { watcher_state: "WATCHER_IDLE", last_executed_run: "inbox-newer-terminal", queue_depth: 0, queue_items: [] },
      component_versions: {}, telemetry: [], duration_estimate: {}, build_commit: "",
    } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-dismiss", status: "BLOCKED",
        title: "Dismissed prompt", executed_at: "2026-08-04T12:00:00Z",
      }];
      renderPromptHistory();
    });
    const dismissButton = page.getByRole("button", { name: "Uitvoering afsluiten" });
    await expect(dismissButton).toBeVisible();
    await dispatchDashboardPointerClick(dismissButton);
    await page.locator("#confirmationModalConfirm").click();
    await expect.poll(() => historyReads).toBeGreaterThan(1);
    await expect(page.getByRole("button", { name: "Uitvoering afsluiten" })).toHaveCount(0);
  });

  test("shows the iPhone pull-to-refresh threshold", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => updatePullRefresh(72));
    await expect(page.getByTestId("pull-refresh")).toHaveText("Laat los om te vernieuwen");
    await expect(page.getByTestId("pull-refresh")).toHaveClass(/pull-refresh--visible/);
    await expect(page.getByTestId("pull-refresh")).toHaveCSS("border-color", "rgb(240, 182, 106)");

    await page.getByTestId("theme-toggle").click();
    await expect(page.getByTestId("pull-refresh")).toHaveCSS("background-color", "rgb(255, 244, 230)");
    await expect(page.getByTestId("pull-refresh")).toHaveCSS("border-color", "rgb(240, 182, 106)");
  });

  test("only starts pull-to-refresh from the scroll region's top edge", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const result = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const content = document.querySelector("#currentRun");
      const top = region.getBoundingClientRect().top;
      const move = (clientY) => {
        let prevented = false;
        movePullRefresh({
          touches: [{ clientY }],
          preventDefault: () => { prevented = true; },
        });
        return prevented;
      };

      window.scrollTo(0, 80);
      startPullRefresh({ touches: [{ clientY: top + 12 }], target: content });
      const ignoredWhileScrolling = move(top + 100);

      window.scrollTo(0, 0);
      startPullRefresh({ touches: [{ clientY: top + 72 }], target: content });
      const ignoredFromContent = move(top + 160);

      startPullRefresh({ touches: [{ clientY: top + 12 }], target: content });
      const handledAtTopEdge = move(top + 50);
      const visibleAtTopEdge = document.querySelector("#pullRefresh").classList.contains("pull-refresh--visible");
      endPullRefresh();
      return { ignoredWhileScrolling, ignoredFromContent, handledAtTopEdge, visibleAtTopEdge };
    });

    expect(result).toEqual({
      ignoredWhileScrolling: true,
      ignoredFromContent: true,
      handledAtTopEdge: true,
      visibleAtTopEdge: true,
    });
    await expect(page.locator(".dashboard-scroll-region")).toHaveCSS("padding-left", "14px");
    await expect(page.locator(".dashboard-scroll-region")).toHaveCSS("padding-right", "14px");
  });

  test("shows live platform readiness in the titlebar health indicator", async ({ page }) => {
    await page.route("**/health", (route) => route.fulfill({ json: { components: {
      dashboard: { healthy: true }, inbox_watcher: { healthy: true }, dashboard_relay: { healthy: true },
    } } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      renderPlatformHealth({ components: {
        dashboard: { healthy: true }, inbox_watcher: { healthy: true }, dashboard_relay: { healthy: true },
      } });
      r({ watcher_state: "WATCHER_IDLE", workspace_state: "WORKSPACE_READY", queue_depth: 0 }, {});
    });
    const indicator = page.getByTestId("dashboard-health-indicator");
    await expect(indicator).toHaveAttribute("data-health-state", "ready");
    await expect(page.locator("#dashboardHealthTooltip")).toBeHidden();
    await indicator.click();
    await expect(indicator).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("#dashboardHealthTooltip")).toContainText("Inbox-watcher");
    await expect(page.locator("#dashboardHealthTooltip")).toContainText("Geen uitvoering actief");
    await expect(page.locator("#dashboardHealthTooltip")).toContainText("Werkruimte gereed");

    await page.evaluate(() => r({ watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "inbox-active", workspace_state: "WORKSPACE_READY", queue_depth: 0 }, {}));
    await expect(indicator).toHaveAttribute("data-health-state", "active");
    await page.evaluate(() => r({ watcher_state: "WATCHER_IDLE", current_phase: "BLOCKED", workspace_state: "WORKSPACE_READY", queue_depth: 0 }, {}));
    await expect(indicator).toHaveAttribute("data-health-state", "blocked");
    await page.evaluate(() => r({ watcher_state: "HOST_PREFLIGHT_FAILED", workspace_state: "WORKSPACE_READY", queue_depth: 0 }, {}));
    await expect(indicator).toHaveAttribute("data-health-state", "error");
  });

  test("keeps the titlebar health tooltip inside a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    const indicator = page.getByTestId("dashboard-health-indicator");
    await indicator.click();
    const bounds = await page.locator("#dashboardHealthTooltip").evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, viewportWidth: window.innerWidth };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewportWidth);
  });

  test("anchors the desktop health popout below the status indicator", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const indicator = page.getByTestId("dashboard-health-indicator");
    await indicator.click();
    const layout = await page.evaluate(() => {
      const health = document.querySelector("#dashboardHealth").getBoundingClientRect();
      const tooltip = document.querySelector("#dashboardHealthTooltip").getBoundingClientRect();
      return { healthLeft: health.left, healthBottom: health.bottom, tooltipLeft: tooltip.left, tooltipTop: tooltip.top };
    });
    expect(Math.abs(layout.tooltipLeft - layout.healthLeft)).toBeLessThanOrEqual(2);
    expect(layout.tooltipTop).toBeGreaterThan(layout.healthBottom);
  });

  test("keeps the compact titlebar health tooltip above dashboard content", async ({ page }) => {
    await page.setViewportSize({ width: 1136, height: 768 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const indicator = page.getByTestId("dashboard-health-indicator");
    await indicator.click();
    const visible = await page.locator("#dashboardHealthTooltip").evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const top = document.elementsFromPoint(
        rect.left + Math.min(24, rect.width / 2),
        rect.top + Math.min(24, rect.height / 2),
      )[0];
      return { visible: top === element || element.contains(top), top: top?.id || top?.className };
    });
    expect(visible.visible, JSON.stringify(visible)).toBe(true);
  });

  test("keeps the compact titlebar health tooltip visible while reading it", async ({ page }) => {
    await page.setViewportSize({ width: 1136, height: 768 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const indicator = page.getByTestId("dashboard-health-indicator");
    const tooltip = page.locator("#dashboardHealthTooltip");
    await indicator.click();
    await expect(tooltip).toBeVisible();
    await tooltip.hover();
    await expect(tooltip).toBeVisible();
    const frontmost = await tooltip.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const target = document.elementsFromPoint(rect.left + 20, rect.top + 20)[0];
      return target === element || element.contains(target);
    });
    expect(frontmost).toBe(true);
  });
});
