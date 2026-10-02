import { randomUUID } from 'node:crypto'
import { defineConfig } from '@playwright/test'

process.env.E2E_RUN_ID ??= randomUUID().slice(0, 8)
process.env.E2E_FIXTURE_STATE ??= `/workspace/.opencuria/plugin-credentials-${process.env.E2E_RUN_ID}.json`
process.env.SQLITE_PATH ??= '/workspace/.opencuria/plugin-review/plugin-e2e.sqlite3'
process.env.E2E_API_URL ??= 'http://127.0.0.1:8001/api/v1'
process.env.E2E_BASE_URL ??= 'http://127.0.0.1:5174'
process.env.MCP_OAUTH_CALLBACK_URL ??= 'http://127.0.0.1:8001/api/v1/mcp-oauth/callback/'

/** Isolated runner for the plugin/credential contract; intentionally no legacy teardown. */
export default defineConfig({
  testDir: './tests',
  testMatch: /plugins-credentials\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 12_000 },
  globalSetup: './fixtures/plugin-credentials-setup.ts',
  globalTeardown: './fixtures/plugin-credentials-teardown.ts',
  outputDir: process.env.E2E_OUTPUT_DIR || './test-results/focused-playwright',
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 1000 },
  },
  projects: [{ name: 'chromium-focused', use: { browserName: 'chromium' } }],
})
