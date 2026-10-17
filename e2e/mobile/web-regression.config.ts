import { defineConfig } from "@playwright/test";

/** Actual Vite app; existing tests provide isolated REST/Socket.IO fixtures. */
export default defineConfig({
  testDir: "../tests",
  testMatch: ["chat-image-preview.spec.ts", "chat-workspace-sidebar.spec.ts"],
  workers: 1,
  retries: 0,
  timeout: 120_000,
  outputDir: "/workspace/.opencuria/playwright/mobile/web-regression",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5179",
    headless: true,
    launchOptions: {
      executablePath:
        process.env.E2E_CHROME_PATH || "/usr/bin/google-chrome-stable",
    },
    screenshot: "only-on-failure",
    video: "off",
  },
  reporter: "list",
});
