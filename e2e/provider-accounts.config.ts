import { defineConfig } from "@playwright/test";

/** Run against the isolated provider-account fixture, never production data. */
export default defineConfig({
  testDir: "./tests",
  testMatch: /provider-accounts\.spec\.ts/,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  outputDir: "./test-results/provider-accounts-playwright",
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5174",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
});
