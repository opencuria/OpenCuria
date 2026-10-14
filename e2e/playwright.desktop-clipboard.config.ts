import { existsSync } from 'node:fs'
import { defineConfig } from '@playwright/test'

const viteUrl = process.env.CLIPBOARD_VITE_URL || 'http://127.0.0.1:5173'
const port = new URL(viteUrl).port || '80'
const chromiumPath =
  process.env.CLIPBOARD_CHROMIUM_PATH ||
  (existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined)

/** Isolated DesktopSurface clipboard regressions; no shared setup or teardown. */
export default defineConfig({
  testDir: './tests',
  testMatch: 'desktop-clipboard.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  outputDir: '/workspace/.opencuria/playwright/desktop-clipboard-results',
  reporter: [['list']],
  use: {
    baseURL: 'http://desktop-clipboard.invalid',
    headless: true,
    ignoreHTTPSErrors: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
    viewport: { width: 1280, height: 900 },
  },
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        launchOptions: { executablePath: chromiumPath },
      },
    },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    cwd: '../webapp',
    url: `${viteUrl}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
