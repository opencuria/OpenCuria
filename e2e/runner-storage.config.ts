import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { chromium, defineConfig } from "@playwright/test";

const root = resolve(__dirname, "..");
const executablePath =
  process.env.E2E_CHROME_PATH ||
  (!existsSync(chromium.executablePath())
    ? [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
      ].find(existsSync)
    : undefined);
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:5181";

// Real Vite/Vue app, isolated backend fixtures. Never connects to the DB.
export default defineConfig({
  testDir: "./tests",
  testMatch: "runner-storage.spec.ts",
  outputDir: resolve(root, "../.opencuria/playwright/runner-storage-results"),
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    baseURL,
    headless: true,
    reducedMotion: "reduce",
    launchOptions: { executablePath },
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1100 } } },
    {
      name: "mobile",
      use: {
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5181 --strictPort",
    cwd: resolve(root, "webapp"),
    url: baseURL,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
