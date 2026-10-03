import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { chromium, defineConfig } from "@playwright/test";

const repositoryRoot = resolve(__dirname, "..");
const artifacts = resolve(repositoryRoot, "..", ".opencuria/playwright");
// Prefer Playwright's matching browser; system Chrome is only a fallback.
const executablePath =
  process.env.E2E_CHROME_PATH ||
  (!existsSync(chromium.executablePath())
    ? [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
      ].find(existsSync)
    : undefined);
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:5179";

// Actual Vite app, isolated HTTP/Socket.IO fixtures; no login or DB cleanup.
export default defineConfig({
  testDir: "./tests",
  testMatch: "capture.spec.ts",
  outputDir: resolve(artifacts, "capture-results"),
  workers: 1,
  retries: 0,
  // Allow real Vite rendering under contention plus bounded artifact capture.
  timeout: 120_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL,
    viewport: { width: 1440, height: 960 },
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: { executablePath },
  },
  reporter: "list",
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5179 --strictPort",
    cwd: resolve(repositoryRoot, "webapp"),
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
