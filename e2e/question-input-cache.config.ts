import { defineConfig } from '@playwright/test'

/** Isolated question-input E2E: intentionally avoids the destructive legacy teardown. */
export default defineConfig({
  testDir: './tests',
  testMatch: 'question-input-cache.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    browserName: 'chromium',
    headless: true,
    launchOptions: {
      executablePath: process.env.E2E_CHROME_PATH || '/usr/bin/google-chrome',
    },
    viewport: { width: 1440, height: 960 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  reporter: [['list']],
})
