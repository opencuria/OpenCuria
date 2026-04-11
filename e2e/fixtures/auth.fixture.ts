/**
 * Custom Playwright test fixture with auth state and test context.
 */
import { test as base, type Page } from '@playwright/test';
import { loadState, saveState, type TestState } from './test-context';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || 'admin@localhost.test';
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || 'OpenCuria2026!';

export type TestFixtures = {
  testState: TestState;
  authedPage: Page;
};

export const test = base.extend<TestFixtures>({
  testState: async ({}, use) => {
    const state = loadState();
    await use(state);
    saveState(state);
  },

  authedPage: async ({ page }, use) => {
    // Check if we already have auth in localStorage by going to dashboard
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // If we're on login page, log in
    if (page.url().includes('/login')) {
      await page.getByLabel('Email').fill(ADMIN_EMAIL);
      await page.getByLabel('Password').fill(ADMIN_PASSWORD);
      await page.getByRole('button', { name: /sign in/i }).click();
      await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15_000 });
    }

    // If on create-organization, skip (should already exist)
    if (page.url().includes('/create-organization')) {
      await page.goto(BASE_URL);
    }

    await use(page);
  },
});

export { expect } from '@playwright/test';
