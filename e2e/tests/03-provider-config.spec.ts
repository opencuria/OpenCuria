/**
 * 03-provider-config.spec.ts — Test harness Provider (OpenRouter) settings tab.
 */
import { test, expect } from '../fixtures/auth.fixture';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('03 — Provider (OpenRouter)', () => {
  test('should show Provider tab and form fields', async ({ authedPage: page }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Provider (OpenRouter)' }).click();

    await expect(page.getByText('OpenRouter Provider')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('#provider-api-key')).toBeVisible();
    await expect(page.locator('#provider-base-url')).toBeVisible();
    await expect(page.locator('#provider-default-model')).toBeVisible();
    await expect(page.locator('#provider-small-model')).toBeVisible();
    await expect(page.getByRole('button', { name: /save provider config/i })).toBeVisible();
  });

  test('should save provider config fields without requiring a working key', async ({
    authedPage: page,
    testState,
  }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Provider (OpenRouter)' }).click();
    await page.waitForTimeout(500);

    await page.locator('#provider-base-url').fill('https://openrouter.ai/api/v1');
    await page.locator('#provider-default-model').fill(`${testState.prefix}-default-model`);
    await page.locator('#provider-small-model').fill(`${testState.prefix}-small-model`);
    await page.locator('#provider-api-key').fill('sk-or-e2e-test-key-not-real');

    await page.getByRole('button', { name: /save provider config/i }).click();

    await expect(page.getByText(/saved key/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('#provider-default-model')).toHaveValue(
      `${testState.prefix}-default-model`,
    );
    await expect(page.locator('#provider-small-model')).toHaveValue(
      `${testState.prefix}-small-model`,
    );
  });
});
