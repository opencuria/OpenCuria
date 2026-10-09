import { defineConfig } from "@playwright/test";

const enabled = process.env.E2E_CLAUDE_AGENT_LIVE === "1";

export default defineConfig({
  testDir: "./tests",
  testMatch: ["claude-agent-live.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 150_000,
  expect: { timeout: 20_000 },
  forbidOnly: true,
  reporter: [
    ["list"],
    [
      "html",
      { open: "never", outputFolder: "playwright-report/claude-agent-live" },
    ],
  ],
  outputDir: "test-results/claude-agent-live",
  globalTeardown: undefined,
  use: {
    baseURL: "http://127.0.0.1:5177",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  ...(enabled ? {} : { grep: /__LIVE_E2E_OPT_IN_REQUIRED__/ }),
});
