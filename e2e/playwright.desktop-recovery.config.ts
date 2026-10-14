import { defineConfig } from '@playwright/test'

/**
 * Isolated, fully mocked desktop recovery regressions. This config deliberately
 * has no shared global teardown or backend fixtures: all API traffic is
 * intercepted in the browser and all artifacts stay under .opencuria.
 */
export default defineConfig({
  testDir: './tests',
  testMatch: 'desktop-recovery.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  globalTimeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: '/workspace/.opencuria/playwright/desktop-recovery-results',
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
    viewport: { width: 1440, height: 1000 },
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || '/usr/bin/google-chrome',
    },
  },
  projects: [{ name: 'desktop-recovery', use: { browserName: 'chromium' } }],
})
