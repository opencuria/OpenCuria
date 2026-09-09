/**
 * 03-provider-config.spec.ts — Provider connections + small model + agent configs.
 */
import { test, expect } from '../fixtures/auth.fixture';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('03 — Provider & Agents', () => {
  test('should show Provider tab with small model and Agents tab with agent rows', async ({
    authedPage: page,
  }) => {
    await page.goto(`${BASE_URL}/?settings=provider`);
    await expect(page.getByTestId('settings-sheet')).toBeVisible({ timeout: 10_000 });
    await page.waitForLoadState('networkidle');

    await page.getByTestId('settings-nav-provider').first().click();
    await expect(page.locator('#provider-small-model')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('save-default-models')).toBeVisible();

    await page.getByTestId('settings-nav-agents').first().click();
    await expect(page.getByTestId('agent-row-build')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('save-agent-configs')).toBeVisible();
  });

  test('should keep small-model save available without requiring a working key', async ({
    authedPage: page,
  }) => {
    await page.goto(`${BASE_URL}/?settings=provider`);
    await expect(page.getByTestId('settings-sheet')).toBeVisible({ timeout: 10_000 });
    await page.waitForLoadState('networkidle');

    await page.getByTestId('settings-nav-provider').first().click();
    await expect(page.locator('#provider-small-model')).toBeVisible({ timeout: 10_000 });
    // Save button exists; it stays disabled until the small model changes.
    await expect(page.getByTestId('save-default-models')).toBeVisible();
  });
});
