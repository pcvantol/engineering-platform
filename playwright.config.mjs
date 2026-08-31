import { defineConfig } from "@playwright/test";

export default defineConfig({
  fullyParallel: true,
  // One isolated dashboard process keeps GitHub's hosted runner and its local
  // dashboard server stable. A bounded failure fan-out prevents one shared
  // layout regression from spending the whole job timeout on repeated browser
  // action timeouts.
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  maxFailures: process.env.CI ? 3 : undefined,
  snapshotPathTemplate: "{testDir}/{testFilePath}-snapshots/{arg}-{platform}{ext}",
});
