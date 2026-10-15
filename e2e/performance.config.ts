import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "webapp-performance.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: "/workspace/.opencuria/playwright/performance/results/test-output",
  use: {
    baseURL: process.env.E2E_PERF_BASE_URL || "http://127.0.0.1:4174",
    headless: true,
    launchOptions: {
      executablePath:
        process.env.E2E_CHROME_PATH || "/usr/bin/google-chrome-stable",
    },
    viewport: { width: 1440, height: 960 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  reporter: [["list"]],
});
