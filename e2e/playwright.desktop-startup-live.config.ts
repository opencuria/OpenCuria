import { defineConfig } from "@playwright/test";

/**
 * Isolated config for the opt-in live desktop regression. Do not add the
 * shared e2e global teardown: it intentionally has no dependency on, or cleanup
 * responsibility for, other specs' persisted resources.
 *
 * Example:
 * E2E_DESKTOP_STARTUP_LIVE=1 \
 * E2E_DESKTOP_STARTUP_WORKSPACE_ID=<running-workspace-uuid> \
 * E2E_BASE_URL=http://127.0.0.1:5173 \
 * npx playwright test --config=playwright.desktop-startup-live.config.ts
 */
export default defineConfig({
  testDir: "./tests",
  testMatch: "desktop-startup-live.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  globalTimeout: 120_000,
  expect: { timeout: 15_000 },
  outputDir: "/workspace/.opencuria/playwright/desktop-startup-live-results",
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5173",
    headless: false,
    screenshot: "off",
    trace: "off",
    video: "off",
    viewport: { width: 1440, height: 1000 },
    launchOptions: {
      executablePath:
        process.env.PLAYWRIGHT_CHROMIUM_PATH || "/usr/bin/google-chrome",
    },
  },
  projects: [{ name: "chromium-live", use: { browserName: "chromium" } }],
});
