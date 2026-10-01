import { defineConfig } from '@playwright/test'

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ??
  (process.env.NATIVE_KASM_CLIPBOARD_E2E === '1' ? '/usr/bin/google-chrome' : undefined)

export default defineConfig({
  testDir: './tests',
  testMatch: ['**/native-kasm-clipboard.spec.ts', '**/native-kasm-ui.spec.ts'],
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 20_000 },
  use: {
    browserName: 'chromium',
    headless: true,
    screenshot: 'only-on-failure',
    launchOptions: executablePath ? {
      executablePath,
      args: ['--no-sandbox', '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests'],
    } : { args: ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests'] },
  },
  outputDir: '/workspace/.opencuria/playwright/native-clipboard-results',
  reporter: 'list',
})
