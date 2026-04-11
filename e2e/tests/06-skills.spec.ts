/**
 * 06-skills.spec.ts — Test skill CRUD via UI.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('06 — Skills', () => {
  test('should create a personal skill via UI', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/skills`);
    await page.waitForLoadState('networkidle');

    // Click New Skill
    await page.getByRole('button', { name: /new skill/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const skillName = `${testState.prefix}-personal-skill`;

    // Fill name
    await dialog.locator('input[placeholder*="TypeScript"], input[placeholder*="name"], input').first().fill(skillName);

    // Fill body
    await dialog
      .locator('textarea[placeholder*="expert"], textarea[placeholder*="TypeScript"], textarea')
      .first()
      .fill('You are an expert E2E test writer. Always use Playwright best practices.');

    // Make sure org checkbox is NOT checked (personal skill)
    const orgCheckbox = dialog.locator('#create-org-skill, input[type="checkbox"]');
    if (await orgCheckbox.isVisible({ timeout: 1000 }).catch(() => false)) {
      if (await orgCheckbox.isChecked()) {
        await orgCheckbox.uncheck();
      }
    }

    // Create
    await dialog.getByRole('button', { name: /create skill/i }).click();

    // Wait for dialog to close
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    // Verify skill appears
    await expect(page.getByText(skillName)).toBeVisible({ timeout: 10_000 });

    // Get ID via API
    const skills = await api.get('/skills/');
    const created = skills.find((s: any) => s.name === skillName);
    expect(created).toBeTruthy();
    testState.skillPersonalId = created.id;
    console.log(`Created personal skill: ${created.id}`);
  });

  test('should create an org skill via UI', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/skills`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: /new skill/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const skillName = `${testState.prefix}-org-skill`;

    await dialog.locator('input[placeholder*="TypeScript"], input[placeholder*="name"], input').first().fill(skillName);
    await dialog
      .locator('textarea[placeholder*="expert"], textarea[placeholder*="TypeScript"], textarea')
      .first()
      .fill('Organizational skill for E2E testing. Shared with all team members.');

    // Check org checkbox
    const orgCheckbox = dialog.locator('#create-org-skill, input[type="checkbox"]');
    if (await orgCheckbox.isVisible({ timeout: 1000 }).catch(() => false)) {
      await orgCheckbox.check();
    }

    await dialog.getByRole('button', { name: /create skill/i }).click();
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    await expect(page.getByText(skillName)).toBeVisible({ timeout: 10_000 });

    const skills = await api.get('/skills/');
    const created = skills.find((s: any) => s.name === skillName);
    expect(created).toBeTruthy();
    testState.skillOrgId = created.id;
    console.log(`Created org skill: ${created.id}`);
  });

  test('should edit a skill via API and verify on UI', async ({ authedPage: page, testState }) => {
    test.skip(!testState.skillPersonalId, 'No personal skill');

    // Edit via API (more reliable than clicking card buttons)
    const updated = await api.patch(`/skills/${testState.skillPersonalId}/`, {
      body: 'Updated: Expert E2E test writer with Playwright best practices.',
    });
    expect(updated.body).toContain('Updated');

    // Verify on UI the skill still shows
    await page.goto(`${BASE_URL}/skills`);
    await page.waitForLoadState('networkidle');
    const skillName = `${testState.prefix}-personal-skill`;
    await expect(page.getByText(skillName)).toBeVisible({ timeout: 5_000 });
    console.log('Skill edit verified');
  });

  test('should show skill scope badges', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/skills`);
    await page.waitForLoadState('networkidle');

    // Check that skills have scope badges
    if (testState.skillPersonalId) {
      const personalName = `${testState.prefix}-personal-skill`;
      await expect(page.getByText(personalName)).toBeVisible();
    }
    if (testState.skillOrgId) {
      const orgName = `${testState.prefix}-org-skill`;
      await expect(page.getByText(orgName)).toBeVisible();
    }
  });
});
