import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testMatch: '12-scheduled-task-composer.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5178',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    launchOptions: {
      executablePath:
        process.env.CHROME_EXECUTABLE_PATH ||
        (process.platform === 'linux' ? '/usr/bin/google-chrome' : undefined),
    },
  },
  reporter: 'list',
  // This test reads an existing task and never saves/runs/deletes it. In
  // particular, don't register the project-wide cleanup/login fixtures.
  globalTeardown: undefined,
})
