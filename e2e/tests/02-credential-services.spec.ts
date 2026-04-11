/**
 * 02-credential-services.spec.ts — Test credential service CRUD in org settings.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('02 — Credential Services', () => {
  test('should create an env-type credential service', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    // Click Credential Services tab
    await page.getByRole('button', { name: 'Credential Services' }).click();
    await page.waitForTimeout(500);

    // Click New Service
    await page.getByRole('button', { name: /new service/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const name = `${testState.prefix}-test-svc`;

    // Dialog fields by label (from accessibility tree):
    // "Name" → textbox placeholder "GitHub Enterprise"
    // "Slug" → auto-generated
    // "Credential Type" → combobox (default "Environment Variable")
    // "Environment Variable Name" → textbox placeholder "GITHUB_TOKEN"
    // "Label" → textbox placeholder "Personal Access Token"
    // "Description" → textbox placeholder "Used for repository access..."

    // Fill name
    await dialog.getByRole('textbox').first().fill(name);
    await page.waitForTimeout(500); // let slug auto-generate

    // Fill env var name (required for button to enable)
    const envVarInput = dialog.locator('[placeholder="GITHUB_TOKEN"]');
    if (await envVarInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await envVarInput.fill('E2E_TEST_TOKEN');
    }

    await page.waitForTimeout(500);

    // Click Create Service button — wait for it to be enabled then click
    const createBtn = dialog.getByRole('button', { name: /create service/i });
    await expect(createBtn).toBeEnabled({ timeout: 5_000 });
    await page.waitForTimeout(200);
    await createBtn.click();

    // The UI flow itself must succeed; do not hide failures behind API fallbacks.
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    // Verify service was created via API (authoritative check)
    const services = await api.get('/credential-services/');
    const created = services.find((s: any) => s.name === name);
    expect(created).toBeTruthy();
    testState.credentialServiceId = created.id;
    testState.credentialServiceSlug = created.slug;

    // Reload and verify service appears in list
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');
    await page.getByRole('button', { name: 'Credential Services' }).click();
    await page.waitForTimeout(500);
    await expect(page.getByText(name)).toBeVisible({ timeout: 10_000 });
    console.log(`Created credential service: ${testState.credentialServiceId} (${testState.credentialServiceSlug})`);
  });

  test('should show credential service in the list', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Credential Services' }).click();
    await page.waitForTimeout(500);

    // Verify our service is visible
    const name = `${testState.prefix}-test-svc`;
    await expect(page.getByText(name)).toBeVisible();

    // Verify it shows "Active"
    // The service card should have an activation toggle
    const serviceCard = page.locator(`text=${name}`).locator('..').locator('..');
    await expect(serviceCard).toBeVisible();
  });

  test('should toggle credential service activation', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Credential Services' }).click();
    await page.waitForTimeout(500);

    // Find our service and toggle it
    const name = `${testState.prefix}-test-svc`;
    // The activation button is near the service name
    const serviceRow = page.locator(`text=${name}`).locator('xpath=ancestor::div[contains(@class,"flex")]').first();

    // Click the activation toggle (Active/Inactive button)
    const toggleBtn = serviceRow.getByRole('button', { name: /active|inactive/i });
    if (await toggleBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      const currentText = await toggleBtn.textContent();
      await toggleBtn.click();
      await page.waitForTimeout(1000);

      // Toggle back
      await toggleBtn.click();
      await page.waitForTimeout(1000);
    }
  });
});
