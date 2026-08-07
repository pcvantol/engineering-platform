import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  createTranslator,
  DASHBOARD_MESSAGES,
  SUPPORTED_LOCALES,
} from "../../tools/engineering/assets/dashboard_locales.mjs";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
let dashboard;
let dashboardRoot;
let dashboardUrl;

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

test.beforeAll(async ({}, testInfo) => {
  const port = 8876 + testInfo.workerIndex;
  dashboardUrl = `http://127.0.0.1:${port}`;
  dashboardRoot = mkdtempSync(path.join(tmpdir(), "djconnect-dashboard-test-"));
  const engineeringDirectory = path.join(dashboardRoot, "tools/engineering");
  mkdirSync(engineeringDirectory, { recursive: true });
  for (const filename of ["ENGINEERING_PLATFORM_CONFIG.json", "ENGINEERING_PLATFORM_VERSION.json"]) {
    copyFileSync(path.join(repository, "tools/engineering", filename), path.join(engineeringDirectory, filename));
  }
  dashboard = spawn(
    "python3",
    [
      "-c",
      "from pathlib import Path; import sys; from tools.engineering.dashboard import DashboardHTTPServer, handler; DashboardHTTPServer(('127.0.0.1', int(sys.argv[2])), handler(Path(sys.argv[1]))).serve_forever()",
      dashboardRoot,
      String(port),
    ],
    { cwd: repository, stdio: "ignore" },
  );
  await waitForDashboard();
});

test.afterAll(() => {
  dashboard?.kill("SIGTERM");
  if (dashboardRoot) rmSync(dashboardRoot, { force: true, recursive: true });
});

async function openTitlebarOptions(page) {
  const options = page.locator("#dashboardTitlebarOptions");
  if (!(await options.evaluate((element) => element.open))) {
    await page.getByTestId("titlebar-options-toggle").click();
  }
}

test.beforeEach(async ({ page }, testInfo) => {
  const goto = page.goto.bind(page);
  page.goto = async (...arguments_) => {
    const response = await goto(...arguments_);
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    if (![
      "puts every mobile title-bar setting in a labelled expandable panel",
      "only starts pull-to-refresh from the scroll region's top edge",
    ].includes(testInfo.title)) {
      await openTitlebarOptions(page);
    }
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
      await page.locator("#dashboardLocale").selectOption(language);
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
          value: element.textContent,
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
      "", "⧉", "↑", "i", "↺", "⌧", "▤", "✦", "⋯", "—",
    ]));
    expect(dashboardSource).not.toMatch(/confirmDashboardAction\(\s*["']/);

    for (const language of SUPPORTED_LOCALES) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await page.locator("#dashboardLocale").selectOption(language);
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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
    await page.route("**/api/prompt-history/inbox-modal/details", (route) => route.fulfill({
      json: {
        history: {
          run_id: "inbox-modal",
          status: "COMPLETE",
          title: "Modal prompt",
          executed_at: "2026-08-04T08:00:00Z",
        },
        execution: { seconds: 42, total_seconds: 61 },
        evidence: ["Execution Host: Engineering Platform"],
      },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [{
        run_id: "inbox-modal",
        status: "COMPLETE",
        title: "Modal prompt",
        executed_at: "2026-08-04T08:00:00Z",
      }];
      renderPromptHistory();
    });

    await page.locator("#promptHistoryRows tr td").nth(1).click();
    await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    await expect(page.locator("#promptHistoryDetailModal")).toHaveClass(/dashboard-modal-shell--evidence/);
    await expect(page.locator("#promptHistoryDetailModal .prompt-detail-modal__panel")).toHaveClass(/dashboard-modal-shell__panel/);
    await expect(page.locator("#promptHistoryDetailModal .prompt-detail-modal__header")).toHaveClass(/dashboard-modal-shell__header/);
    await expect(page.locator("#promptHistoryDetailDescription")).toHaveCSS("border-bottom-color", "rgb(141, 199, 255)");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Engineering Platform");
    await expect(page.locator("dialog[open]")).toHaveCount(1);
  });

  test("draws the selected prompt-history row border on both table edges", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
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
    await row.focus();
    await expect(row).toBeFocused();
    await expect(row.locator("td").first()).toHaveCSS(
      "box-shadow",
      "rgb(240, 182, 106) 3px 0px 0px 0px inset",
    );
    await expect(row.locator("td").last()).toHaveCSS(
      "box-shadow",
      "rgb(240, 182, 106) -3px 0px 0px 0px inset",
    );
  });

  test("renders a read-only Forge recommendation handoff with expandable alternatives", async ({ page }) => {
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
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
    await page.evaluate(() => { document.querySelector("#promptHistory").open = true; promptHistoryEntries = [{ run_id: "inbox-handoff", status: "COMPLETE", title: "Forge handoff", executed_at: "2026-08-04T08:00:00Z" }]; renderPromptHistory(); });
    await page.locator("#promptHistoryRows tr td").nth(1).click();
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Mission Aurora");
    const alternatives = page.locator(".recommendation-alternatives");
    await alternatives.locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(alternatives).toHaveAttribute("open", "");
    await expect(alternatives).toContainText("Mission Borealis");
    await expect(page.locator("#promptHistoryDetailContent button")).toHaveCount(0);
  });

  test("uses the shared modal shell with contextual panels and neutral close controls", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    for (const [selector, modifier, accent] of [
      ["#componentModal", "dashboard-modal-shell--component", "rgb(163, 230, 53)"],
      ["#confirmationModal", "dashboard-modal-shell--confirmation", "rgb(240, 182, 106)"],
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

  test("applies the compact header and standard action scale to every modal", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const titles = [];
    for (const selector of [
      "#componentModal",
      "#confirmationModal",
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
        const closeBox = close.getBoundingClientRect();
        return {
          panelBackground: getComputedStyle(panel).backgroundColor,
          headerBackground: headerStyle.backgroundColor,
          paddingTop: headerStyle.paddingTop,
          paddingBottom: headerStyle.paddingBottom,
          titleSize: getComputedStyle(title).fontSize,
          closeWidth: Math.round(closeBox.width),
          closeHeight: Math.round(closeBox.height),
        };
      });
      expect(metrics.headerBackground).not.toBe(metrics.panelBackground);
      expect(metrics.paddingTop).toBe(metrics.paddingBottom);
      expect(metrics.closeWidth).toBe(32);
      expect(metrics.closeHeight).toBe(32);
      titles.push(metrics.titleSize);
      await modal.evaluate((element) => element.close());
    }
    expect(new Set(titles)).toEqual(new Set(["20px"]));
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

  test("gives direct-touch controls a temporary elevated glass press state", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).toContain("@media (hover:none) and (pointer:coarse)");
    expect(styles).toContain("backdrop-filter:blur(12px)");
    expect(styles).toContain("background-image:linear-gradient");
    expect(styles).toContain("scale(1.045)");
    expect(styles).toContain("-webkit-user-select:none");
    expect(styles).toContain(".dashboard-titlebar :is(.theme-toggle,.section-state-toggle)");
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
      panelBottom: document.querySelector(".confirmation-modal__panel").getBoundingClientRect().bottom,
    }));
    expect(Math.max(...placement.bottoms)).toBeLessThanOrEqual(placement.viewportHeight);
    expect(Math.max(...placement.bottoms)).toBeLessThanOrEqual(placement.panelBottom);
    await modal.evaluate((element) => element.close());
  });

  test("keeps the prompt-history AI chat as compact as the detail modal on iPhone", async ({ page }) => {
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
    expect(chatBox.x).toBeCloseTo(detailBox.x, 0);
    expect(chatBox.width).toBeCloseTo(detailBox.width, 0);
    expect(chatBox.height).toBeCloseTo(detailBox.height, 0);
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

  test("uses the selected locale service for copy and date formatting", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardLocale").selectOption("de");
    await page.waitForLoadState("domcontentloaded");
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(page.locator(".footer #lastRefresh")).toContainText("Zuletzt aktualisiert:");
    await expect(page.locator("#dashboardLocale option:checked")).toHaveText("Deutsch");
  });

  test("changes visible interface copy for each supported language", async ({ page }) => {
    const expectations = [
      ["en", "Language", "Refresh automatically", "AI analysis", "Passed", "Execution", "Resume Queue", "Active execution", "Execution queue", "New assignments wait for execution in order of creation date.", "Engineering Operations Console", "Loading data…", "Pull requests", "Implementation", "None", "Engineering Platform version", "Automatic refresh is off"],
      ["nl", "Taal", "Automatisch vernieuwen", "AI-analyse", "Geslaagd", "Uitvoering", "Wachtrij hervatten", "Lopende uitvoering", "Wachtrij voor uitvoeringen", "Nieuwe opdrachten wachten op uitvoering in volgorde van aanmaakdatum.", "Engineering Operationele console", "Gegevens laden…", "Pull requests", "Implementatie", "geen", "Engineering Platform-versie", "Automatisch vernieuwen is uit"],
      ["de", "Sprache", "Automatisch aktualisieren", "KI-Analyse", "Erfolgreich", "Ausführung", "Warteschlange fortsetzen", "Laufende Ausführung", "Ausführungswarteschlange", "Neue Aufträge warten in der Reihenfolge ihres Erstellungsdatums auf die Ausführung.", "Engineering-Betriebskonsole", "Daten werden geladen…", "Pull Requests", "Implementierung", "Keine", "Engineering-Plattformversion", "Automatische Aktualisierung ist aus"],
      ["fr", "Langue", "Actualiser automatiquement", "Analyse IA", "Réussi", "Exécution", "Reprendre la file", "Exécution en cours", "File d’exécution", "Les nouvelles tâches attendent leur exécution dans l’ordre de leur création.", "Console des opérations d’ingénierie", "Chargement des données…", "Pull requests", "Implémentation", "Aucun", "Version d’Engineering Platform", "Actualisation automatique désactivée"],
      ["es", "Idioma", "Actualizar automáticamente", "Análisis de IA", "Superado", "Ejecución", "Reanudar cola", "Ejecución en curso", "Cola de ejecuciones", "Las nuevas tareas esperan ejecución por orden de fecha de creación.", "Consola de operaciones de ingeniería", "Cargando datos…", "Solicitudes de extracción", "Implementación", "Ninguno", "Versión de Engineering Platform", "Actualización automática desactivada"],
    ];

    for (const [language, localeLabel, refreshLabel, analysisLabel, passLabel, detailTitle, queueAction, activePrompt, queueTitle, queueDescription, dashboardTitle, splashLoading, pullRequestsTitle, implementationLabel, noneLabel, platformVersionLabel, refreshOffLabel] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await page.locator("#dashboardLocale").selectOption(language);
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
      await expect(page.locator("#technicalPullRequestsTitle")).toHaveText(pullRequestsTitle);
      await expect(page.locator("#technicalImplementationLabel")).toHaveText(implementationLabel);
      await page.evaluate(() => r({}));
      await expect(page.locator("#implementation")).toHaveText(noneLabel);
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
      await page.locator("#dashboardLocale").selectOption(language);
      await expect(page.locator("#chatInput")).toHaveAttribute("placeholder", placeholder);
    }
  });

  test("localizes dashboard chrome and dynamic runtime copy for every supported language", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "IDLE", queue_depth: 0 } },
    }));
    const expectations = [
      ["en", "Workspace location", "Specialist reviewers", "Input tokens", "Use reset"],
      ["nl", "Werkruimtelocatie", "Specialistische reviewers", "Invoertokens", "Gebruik reset"],
      ["de", "Arbeitsbereichspfad", "Spezialisierte Reviewer", "Eingabetoken", "Zurücksetzung verwenden"],
      ["fr", "Emplacement de l’espace de travail", "Évaluateurs spécialisés", "Jetons d’entrée", "Utiliser la réinitialisation"],
      ["es", "Ubicación del espacio de trabajo", "Revisores especializados", "Tokens de entrada", "Usar restablecimiento"],
    ];
    for (const [language, workspaceLocation, reviewers, inputTokens, reset] of expectations) {
      const statusLoaded = page.waitForResponse("**/api/dashboard-snapshot");
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      // Do not inject the localized runtime projection before the initial
      // snapshot response has completed; it could otherwise overwrite usage.
      await statusLoaded;
      await page.locator("#dashboardLocale").selectOption(language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await page.waitForFunction(() => typeof window.r === "function");
      await page.evaluate(() => r({
        watcher_state: "ENGINEERING_RUN_ACTIVE",
        run_id: "inbox-localized-runtime",
        current_phase: "EXECUTE_AGENT",
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

  test("localizes dynamically rendered telemetry copy for every supported language", async ({ page }) => {
    const expectations = [
      ["en", "Execution Host telemetry", "Operational trends for the last seven days. Telemetry is not repository evidence."],
      ["nl", "Execution Host-telemetrie", "Operationele trends van de laatste zeven dagen. Telemetrie is geen repositorybewijs."],
      ["de", "Execution-Host-Telemetrie", "Betriebstrends der letzten sieben Tage. Telemetrie ist kein Repository-Nachweis."],
      ["fr", "Télémétrie de l’hôte d’exécution", "Tendances opérationnelles des sept derniers jours. La télémétrie n’est pas une preuve de dépôt."],
      ["es", "Telemetría del host de ejecución", "Tendencias operativas de los últimos siete días. La telemetría no es evidencia del repositorio."],
    ];
    for (const [language, title, description] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await page.locator("#dashboardLocale").selectOption(language);
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

  test("localizes search and level filter controls for every supported language", async ({ page }) => {
    const expectations = [
      ["en", "Search", "Search all fields", "Level", "All levels"],
      ["nl", "Zoeken", "Zoek in alle velden", "Niveau", "Alle niveaus"],
      ["de", "Suchen", "Alle Felder durchsuchen", "Stufe", "Alle Stufen"],
      ["fr", "Rechercher", "Rechercher dans tous les champs", "Niveau", "Tous les niveaux"],
      ["es", "Buscar", "Buscar en todos los campos", "Nivel", "Todos los niveles"],
    ];
    for (const [language, search, placeholder, level, allLevels] of expectations) {
      await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
      await page.locator("#dashboardLocale").selectOption(language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator("label[for=logFilter]")).toHaveText(search);
      await expect(page.locator("#logFilter")).toHaveAttribute("placeholder", placeholder);
      await expect(page.locator("label[for=logLevelFilter]")).toContainText(level);
      await expect(page.locator("#logLevelFilter option[value='']")).toHaveText(allLevels);
    }
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
      await page.locator("#dashboardLocale").selectOption(language);
      await expect(page.locator("html")).toHaveAttribute("lang", language);
      await expect(page.locator("#componentLogs .log-card-header strong").first()).toHaveText(title);
      await expect(page.locator("#inboxComponentLog").locator("xpath=preceding-sibling::thead[1]/tr/th")).toHaveText(headers);
    }
  });

  test("localizes prompt history column headings for every supported language", async ({ page }) => {
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
      await page.locator("#dashboardLocale").selectOption(language);
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
    await page.locator("#dashboardLocale").selectOption("en");
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

  test("keeps terminal history actions on one wide-screen row beside a compact title", async ({ page }) => {
    await page.setViewportSize({ width: 2048, height: 900 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      json: { status: { watcher_state: "WATCHER_IDLE", queue_depth: 0, last_executed_run: "inbox-actions" } },
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      r({ last_executed_run: "inbox-actions", watcher_state: "WATCHER_IDLE" }, {});
      promptHistoryEntries = [{
        run_id: "inbox-actions",
        title: "Engineering Platform Increment — Producer Submission Envelope",
        status: "BLOCKED",
        can_retry: true,
      }];
      renderPromptHistory();
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
    await page.setViewportSize({ width: 1024, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => {
      document.querySelector("#promptHistory").open = true;
      promptHistoryEntries = [
        { run_id: "inbox-retryable", title: "Blocked without child", status: "BLOCKED", can_retry: true },
        { run_id: "inbox-failed-retryable", title: "Failed without child", status: "FAILED", can_retry: true },
        { run_id: "inbox-queued-parent", title: "Queued retry", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-queued-run-id", retry_status: "QUEUED" },
        { run_id: "inbox-active-parent", title: "Active child", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-active-child", retry_status: "ACTIVE" },
        { run_id: "inbox-complete-parent", title: "Completed child", status: "BLOCKED", can_retry: false, retry_child_run_id: "inbox-complete-child", retry_status: "COMPLETE" },
      ];
      renderPromptHistory();
    });
    await expect(page.locator("#promptHistoryRows .execution-history-action")).toHaveCount(2);
    await expect(page.locator("#promptHistoryRows .prompt-history-actions").first()).toHaveCSS("gap", "6px");
    await expect(page.locator("#promptHistoryRows .prompt-history-actions").first()).toHaveCSS("display", "flex");
    await expect(page.locator("#promptHistoryRows tr").nth(0)).toContainText("Uitvoering opnieuw proberen");
    await expect(page.locator("#promptHistoryRows tr").nth(1)).toContainText("Uitvoering opnieuw proberen");
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
  });

  test("matches the iPhone portrait dashboard visual reference", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/log/**", (route) => route.abort());
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
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#dashboardTitlebarOptions").evaluate((element) => { element.open = false; });
    await page.locator("#currentRun").evaluate((element) => { element.open = true; });
    const image = await page.screenshot({ animations: "disabled" });
    await testInfo.attach("iphone-portrait-dashboard", {
      body: image,
      contentType: "image/png",
    });
    await expect(page).toHaveScreenshot("iphone-portrait-dashboard.png", {
      animations: "disabled",
      maxDiffPixelRatio: 0.005,
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

  test("keeps the desktop title bar flush with the scrolling region", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#engineering-dashboard-content").evaluate((content) => {
      content.style.minHeight = "2000px";
    });
    const layout = await page.evaluate(() => {
      const region = document.querySelector(".dashboard-scroll-region");
      const titleBar = document.querySelector(".dashboard-titlebar");
      region.scrollTop = 160;
      return {
        regionTop: Math.round(region.getBoundingClientRect().top),
        titleBarTop: Math.round(titleBar.getBoundingClientRect().top),
      };
    });
    expect(layout.titleBarTop).toBe(layout.regionTop);
  });

  test("scrolls the title bar out of view on iPhone portrait", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const layout = await page.evaluate(() => {
      const titleBar = document.querySelector(".dashboard-titlebar");
      window.scrollTo(0, titleBar.offsetHeight + 20);
      return {
        position: getComputedStyle(titleBar).position,
        titleBottom: titleBar.getBoundingClientRect().bottom,
        viewportTop: 0,
      };
    });
    expect(layout.position).toBe("relative");
    expect(layout.titleBottom).toBeLessThan(layout.viewportTop);
  });

  test("puts every mobile title-bar setting in a labelled expandable panel", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    const options = page.locator("#dashboardTitlebarOptions");
    const disclosure = page.getByTestId("titlebar-options-toggle");
    await expect(options).not.toHaveAttribute("open", "");
    await expect(disclosure).toBeVisible();
    await expect(page.getByTestId("theme-toggle")).not.toBeVisible();

    await disclosure.click();
    await expect(options).toHaveAttribute("open", "");
    await expect(page.locator("#dashboardLocaleButton")).toBeVisible();
    await expect(page.locator("#dashboardLocaleButton")).toContainText("Nederlands");
    for (const label of [
      ".dashboard-titlebar__options-content .dashboard-locale > span:first-child",
      ".dashboard-titlebar__options-content .theme-toggle__label",
      ".dashboard-titlebar__options-content .section-state-toggle__label",
      ".dashboard-titlebar__options-content .auto-refresh-toggle span",
    ]) {
      await expect(page.locator(label)).toBeVisible();
      expect((await page.locator(label).textContent()).trim()).not.toBe("");
    }

    const controls = await page.locator(
      ".dashboard-titlebar__options-content > .dashboard-locale, .dashboard-titlebar__options-content > .theme-toggle, .dashboard-titlebar__options-content > .section-state-toggle, .dashboard-titlebar__options-content > .auto-refresh-toggle",
    ).evaluateAll((elements) => elements.map((element) => Math.round(element.getBoundingClientRect().top)));
    expect(controls).toEqual([...controls].sort((first, second) => first - second));
    const titlebarLayout = await page.evaluate(() => {
      const refresh = document.querySelector("#pageRefresh").getBoundingClientRect();
      const options = document.querySelector("#dashboardTitlebarOptions").getBoundingClientRect();
      return { refreshBottom: Math.round(refresh.bottom), optionsTop: Math.round(options.top) };
    });
    expect(titlebarLayout.refreshBottom).toBeLessThanOrEqual(titlebarLayout.optionsTop);
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
    expect(position.locked).toEqual({ active: true, top: "-180px" });
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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
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
    await expect(page.locator("#promptHistoryRows .execution-history-action")).toBeVisible();
  });

  test("keeps execution detail modal borders inside iPhone landscape safe areas", async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 });
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal");
    await modal.evaluate((element) => element.showModal());
    const box = await modal.boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBe(12);
    expect(box.width).toBe(820);

    await modal.evaluate((element) => element.close());
    const confirmation = page.locator("#confirmationModal");
    await confirmation.evaluate((element) => element.showModal());
    const confirmationBox = await confirmation.boundingBox();
    expect(confirmationBox).not.toBeNull();
    expect(confirmationBox.x).toBe(12);
    expect(confirmationBox.width).toBe(820);
    const confirmationPanelBox = await confirmation.locator(".confirmation-modal__panel").boundingBox();
    expect(confirmationPanelBox).not.toBeNull();
    expect(Math.round(confirmationPanelBox.x + confirmationPanelBox.width / 2)).toBe(422);

    await confirmation.evaluate((element) => element.close());
    for (const selector of ["#promptHistoryReportModal", "#promptHistoryChatModal"]) {
      const dialog = page.locator(selector);
      await dialog.evaluate((element) => element.showModal());
      const dialogBox = await dialog.boundingBox();
      expect(dialogBox).not.toBeNull();
      expect(dialogBox.x).toBe(12);
      expect(dialogBox.width).toBe(820);
      await dialog.evaluate((element) => element.close());
    }

    const component = page.locator("#componentModal");
    await component.evaluate((element) => element.showModal());
    const panelBox = await component.locator(".component-modal__panel").boundingBox();
    expect(panelBox).not.toBeNull();
    expect(panelBox.x).toBeGreaterThanOrEqual(12);
    expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(832);
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
    await expect(page.locator(".dashboard-splash__version")).toContainText("1.5.0");
    await expect(page.locator(".dashboard-splash__loading")).toHaveText("Gegevens laden…");
    await expect(page.locator(".dashboard-splash__version")).toHaveCSS("color", "rgb(240, 182, 106)");
    await expect(page.locator(".dashboard-splash__spinner")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
  });

  test("uses the house-style orange for an active execution spinner", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
    await page.locator("#indicator").evaluate((element) => {
      element.className = "indicator indicator--running";
    });
    await expect(page.locator("#indicator")).toHaveCSS("animation-name", "github-activity-ring");
    await expect(page.locator("#indicator")).toHaveCSS("animation-duration", "1.1s");
    await expect(page.locator("#indicator")).toHaveCSS("animation-iteration-count", "infinite");
    await expect(page.locator("#indicator")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
    await expect(page.locator("#indicator")).toHaveCSS("will-change", "transform");
  });

  test("loads the initial status before serverpush connects", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/dashboard-snapshot", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: {
          watcher_state: "WATCHER_IDLE",
          platform_version: "1.5.0",
          queue_depth: 0,
          queue_items: [],
        },
        rate_limits: {
          provider: "Codex CLI",
          provider_version: "0.146.0",
          windows: [],
          reset_credits: 0,
        },
        component_versions: {},
      }),
    }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });

    await expect(page.locator("#platformVersion")).toHaveText("1.5.0");
    await expect(page.locator("#queueSummary")).not.toHaveText("Wachtrij laden…");
    await expect(page.locator("#queueSummary")).toHaveText("0 uitvoeringen in de wachtrij.");
    await expect(page.locator("#rateLimits")).toBeVisible();
    await expect(page.locator("#rateLimitProvider")).toHaveText("Codex CLI · 0.146.0");
    await expect(page.locator("#rateLimitDetails")).toHaveCSS("font-size", "14px");
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

    const favicon = await request.get(`${dashboardUrl}/assets/engineering-status-icon.svg`);
    expect(favicon.status()).toBe(200);
    expect(favicon.headers()["content-type"]).toContain("image/svg+xml");
    const homescreenIcon = await request.get(`${dashboardUrl}/assets/engineering-status-icon-180.png`);
    expect(homescreenIcon.status()).toBe(200);
    expect(homescreenIcon.headers()["content-type"]).toContain("image/png");
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
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => renderPlatformHealth({ components: {
      dashboard: { healthy: true, detail: "HTTP-dashboard reageert", version: "1.2.82", uptime_seconds: 3725 },
      inbox_watcher: { healthy: true, detail: "LaunchAgent is geladen", version: "1.1.4", uptime_seconds: 75 },
    }}));

    const componentText = await page.locator("#platformHealthComponents").textContent();
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

    const alignment = await page.locator(".platform-health__component").evaluate((card) => {
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
      platform_version: "1.5.0",
      current_phase: "EXECUTE_AGENT",
      current_action: "Codex bewerkt bestanden",
      run_id: "activity-run",
      prompt_title: "Veilige voortgang",
      submitted_filename: "activity.md",
    }, {}));

    await expect(page.locator("#currentRun")).toBeVisible();
    await expect(page.locator("#platformVersion")).toHaveText("1.5.0");
    await expect(page.locator("#action")).toHaveText("Codex bewerkt bestanden");
    await expect(page.locator("#action")).toHaveCSS("font-style", "italic");
  });

  test("keeps specialist reviewer titles blue in light mode", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#themeToggle").click();
    await page.evaluate(() => r({
      watcher_state: "ENGINEERING_RUN_ACTIVE", run_id: "review-run",
      reviewer_agents: [{ reviewer: "repository_governance", capability: "engineering", status: "completed" }],
    }, {}));
    await expect(page.locator(".reviewer-agent__name")).toHaveCSS("color", "rgb(47, 134, 189)");
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
    await expect(page.locator("#technicalDetails .card .label").first()).toHaveCSS("color", "rgb(255, 213, 155)");
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

    await expect(page.locator("#executionEstimate")).toHaveText("Indicatieve totale duur: 22–30 minuten");
    await expect(page.locator("#executionEstimateMeta")).toContainText(
      "3 vergelijkbare voltooide uitvoeringen",
    );
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
    }, {}));

    await expect(page.locator("#currentRun")).toBeVisible();
    await expect(page.locator("#currentRun")).toHaveAttribute("open", "");
    await expect(page.locator("#predecessorGate")).toBeVisible();
    await expect(page.locator("#predecessorRun")).toHaveText("blocked-run");
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

    await expect(page.locator(".platform-health__component-detail")).toHaveCSS("color", "rgb(24, 34, 48)");
  });

  test("renders component details in the light modal theme", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("theme-toggle").click();
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await expect(page.locator(".component-info").first()).toHaveCSS("color", "rgb(60, 116, 17)");
    await page.locator(".platform-health__component").first().click();

    await expect(page.locator("#componentModal")).toHaveAttribute("open", "");
    await expect(page.locator("#componentModal")).toBeFocused();
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
    await expect(page.locator(".confirmation-modal__panel")).toHaveCSS("background-color", "rgb(247, 251, 255)");
    await expect(page.locator(".confirmation-modal__panel")).toHaveCSS("color", "rgb(24, 34, 48)");
    await expect(page.locator("#confirmationModalText")).toHaveCSS("color", "rgb(24, 34, 48)");
    await expect(page.locator("#confirmationModalTitle")).toHaveCSS("color", "rgb(240, 182, 106)");
    await expect(page.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(240, 182, 106)");
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
    await page.locator(".component-info").first().click();

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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#promptHistory").evaluate((element) => { element.open = true; });
    await page.evaluate(() => {
      promptHistoryEntries = [{
        run_id: "report-hover",
        status: "COMPLETE",
        title: "Rapport hover",
        executed_at: "2026-08-02T10:00:00+00:00",
        git_commit: "abc1234",
        report_available: true,
      }];
      promptHistoryPage = 1;
      renderPromptHistory();
    });
    const report = page.locator('[title="Bekijk engineeringrapport voor Rapport hover"]');

    await report.hover();
    await expect(report).toHaveCSS("background-color", "rgb(141, 199, 255)");
    await expect(report).toHaveCSS("color", "rgb(23, 35, 49)");
  });

  test("downloads each redacted component log", async ({ page }) => {
    await page.route("**/api/logs/**", (route) => route.fulfill({ contentType: "application/x-ndjson", body: '{"level":"INFO","event":"test"}\n' }));
    await page.route("**/api/audit/user-action", (route) => route.fulfill({ contentType: "application/json", body: '{"logged":true}' }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#dashboardSplash").evaluate((element) => { element.hidden = true; });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });

    await page.evaluate(() => {
      URL.createObjectURL = () => "blob:component-log";
      HTMLAnchorElement.prototype.click = function click() { window.__componentLogDownload = this.download; };
    });
    for (const [testId, filename] of [["download-inbox-log", "inbox-watcher-log-"], ["download-dashboard-log", "statusdashboard-log-"]]) {
      await page.getByTestId(testId).click();
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
      document.querySelector("#logFilter").value = "retain_me";
      independentLogPageStates.inbox = 1;
      renderComponentLogs();
    });

    await expect(page.locator("#inboxComponentLog tr")).toHaveCount(1);
    await page.getByTestId("copy-inbox-visible-log").click();
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).toContain("retain_me");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).toContain("visible-run");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).not.toContain("exclude_me");
    await expect.poll(() => page.evaluate(() => window.__copiedVisibleLog)).not.toContain("hidden-run");
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
    for (const selector of ["#downloadChat", "#promptHistoryReportDownload", "#componentLogs .component-log-download"]) {
      await expect(page.locator(selector).first()).toHaveClass(/dashboard-action--download/);
    }
    for (const selector of ["#copyChat", "#promptHistoryReportCopy", "#componentLogs .component-log-copy"]) {
      await expect(page.locator(selector).first()).toHaveClass(/dashboard-action--copy/);
    }
    for (const selector of ["#clearChat", "#componentLogs .clear-component-log"]) {
      await expect(page.locator(selector).first()).toHaveClass(/dashboard-action--destructive/);
    }
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
    await theme.click();
    await allSections.click();
    await page.waitForTimeout(250);
    for (const toggle of [theme, allSections]) {
      await expect(toggle).toHaveAttribute("aria-checked", "true");
      expect(await toggle.evaluate((element) => getComputedStyle(element, "::before").backgroundColor)).toBe("rgb(240, 182, 106)");
    }
  });

  test("keeps title-bar switch housings free from the generic mobile glass layer", () => {
    const styles = readFileSync(
      path.join(repository, "tools/engineering/assets/dashboard.css"),
      "utf8",
    );
    expect(styles).toContain(
      ".dashboard-titlebar :is(.theme-toggle,.section-state-toggle){",
    );
    expect(styles).toContain("background-image:none;");
    expect(styles).toContain("backdrop-filter:none;");
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
    await expect(page.locator(".rate-limit-reset")).toHaveCSS("border-color", "rgb(81, 216, 138)");
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
      const summary = document.querySelector("#dashboardTitlebarOptions > summary");
      document.querySelector("#dashboardTitlebarOptions").open = true;
      const input = document.querySelector("#autoRefresh");
      input.focus({ preventScroll: true });
      return {
        focusOutline: getComputedStyle(input).outlineColor,
        summaryBackground: getComputedStyle(summary).backgroundColor,
        summaryColor: getComputedStyle(summary).color,
      };
    });

    expect(styles.focusOutline).toBe("rgb(240, 182, 106)");
    expect(styles.summaryBackground).not.toBe("rgb(17, 19, 29)");
    expect(styles.summaryColor).toBe("rgb(24, 34, 48)");
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

  test("keeps report-modal shell focusable without a visible selection ring", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryReportModal");

    await modal.evaluate((element) => { element.showModal(); element.focus(); });
    await expect(modal).toBeFocused();
    await expect(modal).toHaveCSS("outline-style", "none");
    await expect(modal).toHaveCSS("box-shadow", "none");
  });

  test("keeps prompt-detail shell focusable without a visible selection ring", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryDetailModal");

    await modal.evaluate((element) => { element.showModal(); element.focus(); });
    await expect(modal).toBeFocused();
    await expect(modal).toHaveCSS("outline-style", "none");
    await expect(modal).toHaveCSS("box-shadow", "none");
  });

  test("keeps prompt-chat shell focusable without a visible selection ring", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const modal = page.locator("#promptHistoryChatModal");

    await modal.evaluate((element) => { element.showModal(); element.focus(); });
    await expect(modal).toBeFocused();
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
      document.querySelector("#promptHistoryDetailContent").innerHTML =
        "<p>Detailregel</p>".repeat(120);
      element.showModal();
    });
    const before = {
      close: await close.boundingBox(),
      header: await header.boundingBox(),
    };
    await content.evaluate((element) => { element.scrollTop = 180; });
    const after = {
      close: await close.boundingBox(),
      header: await header.boundingBox(),
    }, panelBox = await panel.boundingBox();

    await expect.poll(() => content.evaluate((element) => element.scrollTop)).toBe(180);
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

    expect(box.y).toBeGreaterThanOrEqual(18);
    expect(box.y + box.height).toBeLessThanOrEqual(282);
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
    await expect(page.getByTestId("engineering-dashboard-title")).toHaveText("Engineering Operationele console");
    await expect(page.getByTestId("dashboard-splash")).toBeHidden();
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/assets/operations-console/manifest.webmanifest");
    await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute("href", "/assets/operations-console/apple-touch-icon-dark.png");
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
    expect(await page.locator("#indicator").evaluate((element) => element.parentElement.className)).toBe("current-run__prompt-heading");
    await expect(page.locator("#loadComponentLogs")).toHaveCount(0);
    await expect(page.getByTestId("pull-refresh")).toHaveText("Trek omlaag om te vernieuwen");
    await page.evaluate(() => showCopyToast());
    await expect(page.getByTestId("copy-toast")).toHaveText("Gekopieerd naar klembord");
    await expect(page.getByTestId("copy-toast")).toHaveClass(/copy-toast--visible/);
    await page.locator("#platformHealth").evaluate((element) => { element.open = true; });
    await expect(page.locator(".component-info").first()).toBeVisible();
    await page.locator(".component-info").first().click();
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
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#componentLogs").evaluate((element) => { element.open = true; });
    await page.locator("#autoRefresh").uncheck();
    await page.waitForFunction(() => componentLogsLoaded);
    await page.waitForTimeout(350);
    await page.evaluate(() => {
      refreshComponentLogs = async () => {};
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

  test("shows a searchable, sortable and paginated prompt history", async ({ page }) => {
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
    await expect(page.locator("#promptHistory th")).toHaveCount(8);
    await expect(page.locator('#promptHistory th[data-history-sort-key="git_commit"]')).toHaveCount(0);
    await expect(page.locator("#promptHistoryRows tr").first().locator("td")).toHaveCount(8);
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
    await expect(page.locator("#promptHistoryReportModal")).toBeFocused();
    await expect(page.locator("#promptHistoryDetailModal")).not.toBeVisible();
    await expect(page.locator("#promptHistoryReportContent")).toContainText("Historisch rapport");
    await expect(page.locator("#promptHistoryReportDownload")).toBeVisible();
    await expect(page.locator("#promptHistoryReportCopy")).toBeVisible();
    await page.locator("#promptHistoryReportClose").click();
    await expect(page.locator("#promptHistoryReportModal")).not.toBeVisible();
    await page.route("**/api/prompt-history/**/details", (route) => route.fulfill({
      json: {
        history: { run_id: "inbox-history-25", status: "COMPLETE", title: "Geschiedenis prompt 25", executed_at: "2026-08-02T12:25:00Z", execution_mode: "GENESIS", repository: "pcvantol/djconnect", target_repository: "pcvantol/forge", target_checkout_path: "/Users/example/Documents/GitHub/forge", tracked_file_count: 1655, target_branch: "forge-phase-evidence" },
        execution: { seconds: 42, total_seconds: 61 },
        runtime: { runtime_provider: "codex_cli", codex_cli_version: "0.146.0" },
        usage: { input_tokens: 120, output_tokens: 45 },
        commits: { "Genesis-commit": "abcdef1" },
        evidence: ["Execution Host: Engineering Platform"],
        reviewers: [],
      },
    }));
    await page.locator("#promptHistoryRows tr td").nth(1).click();
    await expect(page.locator("#promptHistoryDetailModal")).toBeVisible();
    await expect(page.locator("#promptHistoryDetailModal")).toBeFocused();
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Engineering Platform");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("0.146.0");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Doelrepository");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("pcvantol/forge");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Lokale checkout");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("/Users/example/Documents/GitHub/forge");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Getrackte bestanden");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("1655");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Uitvoeringsmodus");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("GENESIS");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("Actieve branch");
    await expect(page.locator("#promptHistoryDetailContent")).toContainText("forge-phase-evidence");
    await expect(page.locator("#promptHistoryDetailContent .prompt-detail-status .indicator--green")).toHaveCount(1);
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-sidebar")).toHaveCount(1);
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-sidebar")).toContainText("Doorlooptijd");
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-sidebar")).toContainText("Runtime");
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-sidebar")).toContainText("Git-commit");
    await expect(page.locator("#promptHistoryDetailContent > .prompt-detail-sidebar")).toContainText("Uitvoeringsbewijs");
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
    await expect(page.locator("#promptHistoryReportContent")).toContainText("Historische AI-analyse");
    await expect(page.locator("#promptHistoryReportDownload")).toBeVisible();
    await page.locator("#promptHistoryReportClose").click();
    const chat = page.locator("#promptHistoryRows .prompt-history-chat");
    await expect(chat).toHaveCount(1);
    await expect(chat).toHaveText("⋯");
    await expect(chat).toHaveCSS("border-top-color", "rgb(208, 164, 255)");
    await expect(chat).toHaveCSS("color", "rgb(208, 164, 255)");
    await chat.click();
    await expect(page.locator("#promptHistoryChatModal")).toBeVisible();
    await expect(page.locator("#promptHistoryChatModal")).toBeFocused();
    let submittedRun;
    await page.route("**/api/codex-chat", async (route) => {
      submittedRun = route.request().postDataJSON().run_id;
      await route.fulfill({ json: { answer: "Dit advies hoort bij de geselecteerde prompt.", model: "Codex CLI" } });
    });
    await page.locator("#chatInput").fill("Wat is de volgende stap?");
    await page.locator("#chatInput").press("Control+Enter");
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

  test("uses purpose-matched glyphs in modal titles", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    for (const [selector, glyph] of [
      ["#componentModalTitle", "⚙"],
      ["#confirmationModalTitle", "!"],
      ["#promptHistoryReportModalTitle", "▤"],
      ["#promptHistoryDetailTitle", "i"],
      ["#promptHistoryChatTitle", "⋯"],
    ]) {
      expect(await page.locator(selector).evaluate(
        (title) => getComputedStyle(title, "::before").content,
      )).toContain(glyph);
    }
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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
    const historyLoaded = page.waitForResponse("**/api/prompt-history");
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await historyLoaded;
    await page.locator("#autoRefresh").uncheck();
    await page.locator("#dashboardLocale").selectOption("nl");
    await expect(page.locator("html")).toHaveAttribute("lang", "nl");
    await page.evaluate(() => {
      promptHistoryEntries = [
        { run_id: "inbox-complete", status: "COMPLETE", title: "Completed prompt" },
        { run_id: "inbox-blocked", status: "BLOCKED", title: "Blocked prompt" },
      ];
      renderPromptHistory();
      document.querySelector("#promptHistory").open = true;
    });

    await page.locator("#promptHistoryFilter").fill("voltooid");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    await expect(page.locator("#promptHistoryRows")).toContainText("Voltooid");
    await page.locator("#promptHistoryFilter").fill("geblokkeerd");
    await expect(page.locator("#promptHistoryRows tr")).toHaveCount(1);
    await expect(page.locator("#promptHistoryRows")).toContainText("Geblokkeerd");

    await page.locator("#dashboardLocale").selectOption("en");
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
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.classList.contains("dashboard-ready"));
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
    for (const id of ["workspaceCard", "promptHistory", "platformHealth", "technicalDetails", "componentLogs"]) {
      await expect(page.locator(`#${id}`)).toHaveAttribute("open", "");
    }

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(page.locator("#workspaceCard")).toHaveAttribute("open", "");

    await toggle.evaluate((button) => button.click());
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    for (const id of ["workspaceCard", "platformHealth", "technicalDetails", "componentLogs"]) {
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

  test("formats displayed log timestamps as dd-MM-yyyy HH:mm:ss", async ({ page }) => {
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
    await queue.locator("summary").click();
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

    await page.getByRole("button", { name: "Stel uit" }).first().click();
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

    await page.getByTestId("engineering-inbox-queue").locator("summary").click();
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

    await page.getByTestId("engineering-inbox-queue").locator("summary").click();
    const blocker = page.locator("#inboxBlocker");
    await expect(blocker).toHaveClass(/queue-blocker--error/);
    await expect(blocker).toContainText("Execution Host mag alleen werk vanaf main claimen.");
    const repair = blocker.getByRole("button", { name: "Herstel" });
    await expect(repair).toHaveCSS("background-color", "rgb(59, 40, 27)");
    await expect(repair).toHaveCSS("border-color", "rgb(240, 182, 106)");
    await expect(repair).toHaveCSS("border-radius", "8px");
    await repair.click();
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

    await page.locator("#technicalDetails > summary").click();
    const lock = page.locator("#technicalGitLock");
    await expect(lock).toContainText("Werkmapvergrendeling");
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

  test("renders provider limit rows on separate lines", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    await page.locator("#autoRefresh").uncheck();
    await page.evaluate(() => rateLimits({
      provider: "Codex CLI",
      provider_version: "0.146.0",
      windows: [{ label: "Weekvenster", used_percent: 24, resets_at: 0 }],
      reset_credits: 2,
    }));
    await expect(page.locator("#rateLimitProvider")).toHaveText("Codex CLI · 0.146.0");
    await expect(page.locator("#rateLimitDetails")).toHaveText(/Weekvenster: 76% beschikbaar.*Beschikbare resets: 2/s);
    expect(await page.locator("#rateLimitDetails").evaluate((element) => element.textContent)).toContain("\n");
  });

  test("keeps dashboard view preferences in the browser", async ({ page }) => {
    await page.goto(dashboardUrl, { waitUntil: "domcontentloaded" });
    const autoRefresh = page.locator("#autoRefresh");
    await expect(autoRefresh).toBeChecked();
    await page.locator("#technicalDetails > summary").click();
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
    await expect(modal).toBeFocused();
    await expect(modal.locator(".confirmation-modal__panel")).toHaveCSS("border-top-color", "rgb(255, 113, 143)");
    await expect(page.locator(".confirmation-modal__header")).toHaveCSS("border-bottom-color", "rgb(255, 113, 143)");
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
    await page.route("**/api/prompt-history", (route) => route.fulfill({ json: { runs: [] } }));
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
    await page.locator("#promptHistoryRows .prompt-history-chat").click();
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
      status: { watcher_state: "WATCHER_IDLE", last_executed_run: "inbox-dismiss", queue_depth: 0, queue_items: [] },
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
    await dismissButton.click();
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
      ignoredWhileScrolling: false,
      ignoredFromContent: false,
      handledAtTopEdge: true,
      visibleAtTopEdge: true,
    });
    await expect(page.locator(".dashboard-scroll-region")).toHaveCSS("padding-left", "6px");
    await expect(page.locator(".dashboard-scroll-region")).toHaveCSS("padding-right", "6px");
  });
});
