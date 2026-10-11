import { existsSync } from "node:fs";
import { defineConfig } from "@playwright/test";

const viteUrl = process.env.CLIPBOARD_VITE_URL || "http://127.0.0.1:5173";
const port = new URL(viteUrl).port || "80";
const chromiumPath =
  process.env.CLIPBOARD_CHROMIUM_PATH ||
  (existsSync("/usr/bin/google-chrome") ? "/usr/bin/google-chrome" : undefined);

/**
 * Isolated browser clipboard regressions: no backend fixture setup, shared
 * global teardown, authentication, or cleanup of unrelated user data.
 */
export default defineConfig({
  testDir: "./tests",
  testMatch: "clipboard-copy.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  outputDir: "/workspace/.opencuria/playwright/clipboard-test-results",
  reporter: [["list"]],
  use: {
    baseURL: "http://clipboard-test.invalid",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
    viewport: { width: 1280, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        launchOptions: { executablePath: chromiumPath },
      },
    },
    { name: "webkit", use: { browserName: "webkit" } },
    {
      name: "chromium-mobile",
      grep: /mobile touchscreen/,
      use: {
        browserName: "chromium",
        launchOptions: { executablePath: chromiumPath },
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    cwd: "../webapp",
    url: `${viteUrl}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
