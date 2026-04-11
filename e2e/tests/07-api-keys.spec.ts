/**
 * 07-api-keys.spec.ts — Test API key CRUD and auth.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api, apiRequestWithKey } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('07 — API Keys', () => {
  test('should create an API key with full access via UI', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/api-keys`);
    await page.waitForLoadState('networkidle');

    // Click New API Key
    await page.getByRole('button', { name: /new api key/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const keyName = `${testState.prefix}-apikey`;

    // Fill name
    await dialog.locator('input[placeholder*="n8n"]').fill(keyName);

    // Keep "Full access" (default)
    // Click Create Key
    await dialog.getByRole('button', { name: /create key/i }).click();

    // Wait for the key display step (shows the actual key value)
    await page.waitForTimeout(2000);

    // Look for the generated key value in the dialog
    // The key is shown in a monospace display
    const keyDisplay = dialog.locator('code, [class*="mono"], [class*="font-mono"]').first();
    let keyValue = '';
    if (await keyDisplay.isVisible({ timeout: 5000 }).catch(() => false)) {
      keyValue = (await keyDisplay.textContent()) || '';
    }

    // If we can't find it in code element, try to find any text that looks like a token
    if (!keyValue) {
      const allText = await dialog.textContent();
      // API keys often start with a prefix like "oc_" or similar
      const match = allText?.match(/[A-Za-z0-9_-]{20,}/);
      if (match) keyValue = match[0];
    }

    if (keyValue) {
      testState.apiKeyValue = keyValue.trim();
      console.log(`API key value starts with: ${keyValue.substring(0, 10)}...`);
    }

    // Click Done to close
    const doneBtn = dialog.getByRole('button', { name: /done/i });
    if (await doneBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await doneBtn.click();
    } else {
      // Close dialog by pressing Escape
      await page.keyboard.press('Escape');
    }

    await expect(dialog).toBeHidden({ timeout: 10_000 });

    // Verify key appears in list
    await page.waitForTimeout(1000);
    await expect(page.getByText(keyName)).toBeVisible({ timeout: 10_000 });

    // Get ID via API
    const keys = await api.get('/auth/api-keys/');
    const created = keys.find((k: any) => k.name === keyName);
    if (created) {
      testState.apiKeyId = created.id;
      console.log(`Created API key: ${created.id}`);
    }
  });

  test('should test API key authentication', async ({ testState }) => {
    test.skip(!testState.apiKeyValue, 'No API key value captured');

    // Use the API key to make a request
    const { status, data } = await apiRequestWithKey('GET', '/workspaces/', testState.apiKeyValue!);

    // Should succeed (200) since we gave full access
    expect(status).toBe(200);
    expect(Array.isArray(data)).toBe(true);
    console.log(`API key auth successful — found ${data.length} workspaces`);
  });

  test('should list API key permissions', async () => {
    const perms = await api.get('/auth/api-key-permissions/');
    expect(Array.isArray(perms)).toBe(true);
    expect(perms.length).toBeGreaterThan(0);
    console.log(`Available permissions: ${perms.length}`);
  });

  test('should show API keys on the page', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/api-keys`);
    await page.waitForLoadState('networkidle');

    if (testState.apiKeyId) {
      const keyName = `${testState.prefix}-apikey`;
      await expect(page.getByText(keyName)).toBeVisible({ timeout: 10_000 });
    }
  });
});
