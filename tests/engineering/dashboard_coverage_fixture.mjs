/* Browser-native V8 executable-line evidence; no application code injection. */
import { writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { test as base, expect } from "@playwright/test";
import { observeConsoleDocument } from "./dashboard_document_observer.mjs";

export { expect };
export const test = base.extend({
  dashboardCoverage: [async ({ page }, use, testInfo) => {
    await page.coverage.startJSCoverage({ resetOnNavigation: false, reportAnonymousScripts: false });
    const observation = await observeConsoleDocument(page);
    try {
      await use();
      if (testInfo.status !== testInfo.expectedStatus) await observation.failure(testInfo);
      const entries = await page.coverage.stopJSCoverage();
      const production = entries.filter((entry) => /\/assets\/dashboard(?:_[a-z_]+)?\.(?:js|mjs)(?:\?|$)/.test(entry.url))
        .map((entry) => ({ ...entry, sha256: createHash("sha256").update(entry.source).digest("hex") }));
      const output = testInfo.outputPath("dashboard-v8-coverage.json");
      writeFileSync(output, JSON.stringify({ contract: "ep-dashboard-v8-coverage/v1", test: testInfo.title,
        status: testInfo.status, entries: production }));
      await testInfo.attach("dashboard-v8-coverage", { path: output, contentType: "application/json" });
    } finally {
      await page.close();
      const receipt = await observation.closed();
      expect(receipt.actual_page_closed).toBe(true);
      expect(receipt.bytes).toBeGreaterThan(0);
    }
  }, { auto: true }],
});
