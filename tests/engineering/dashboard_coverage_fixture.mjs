/* Browser-native V8 executable-line evidence; no application code injection. */
import { writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { test as base, expect } from "@playwright/test";

export { expect };
export const test = base.extend({
  dashboardCoverage: [async ({ page }, use, testInfo) => {
    await page.coverage.startJSCoverage({ resetOnNavigation: false, reportAnonymousScripts: false });
    await use();
    const entries = await page.coverage.stopJSCoverage();
    const production = entries.filter((entry) => /\/assets\/dashboard(?:_[a-z_]+)?\.(?:js|mjs)(?:\?|$)/.test(entry.url))
      .map((entry) => ({ ...entry, sha256: createHash("sha256").update(entry.source).digest("hex") }));
    const output = testInfo.outputPath("dashboard-v8-coverage.json");
    writeFileSync(output, JSON.stringify({ contract: "ep-dashboard-v8-coverage/v1", test: testInfo.title,
      status: testInfo.status, entries: production }));
    await testInfo.attach("dashboard-v8-coverage", { path: output, contentType: "application/json" });
  }, { auto: true }],
});
